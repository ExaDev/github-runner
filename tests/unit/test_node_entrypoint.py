"""Tests for what the node entrypoint does before Tailscale and k3s start: the role directory's guards that keep a host from running the service its role file does not name, or a server whose etcd member was removed, the join URL a promoted server takes from the role directory, and the refusal of a former member that has nothing to join, which would otherwise bootstrap a second cluster. The entrypoint starts tailscaled and execs k3s at top level, so the assignments, the functions and the guard block are taken from it and run in a shell, with a stand-in for sleep on PATH."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_cluster"
ENTRYPOINT = ROLE / "files" / "k3s" / "node-entrypoint.sh"
FLEET_ROLE_COMPOSE = ROLE / "files" / "docker-compose.fleet-role.yml"
MOUNT = "./fleet-role.d:/etc/github-runner:ro"
JOIN_URL = "https://node2.example.internal:6443"
BOOTSTRAP_URL = "https://bootstrap.example.internal:6443"

# Stands in for sleep: records its arguments, then sends the shell that started it in the background the SIGTERM `docker stop` would, so a test of the idle loop ends, and ends as a stopped container's would.
SLEEP_STUB = """#!/bin/sh
echo "$*" >> "$STUB/sleep"
kill -TERM "$PPID"
"""


def function_source(name: str) -> str:
    """The definition of a shell function in the entrypoint, from its first line to the closing brace at the start of a line."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", ENTRYPOINT.read_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, f"{name} is not defined in node-entrypoint.sh"
    return match.group(0)


def assignment(name: str) -> str:
    """The top-level assignment of a variable in the entrypoint, on one line."""
    match = re.search(rf"^{name}=.*$", ENTRYPOINT.read_text(), re.MULTILINE)
    assert match is not None, f"{name} is not assigned in node-entrypoint.sh"
    return match.group(0)


def guard_block() -> str:
    """The top-level lines that run the guards, from the comment above them to the line before tailscaled's directories are made."""
    match = re.search(r"^# Before anything else starts.*?\n(.*?)\n\n# k3s's own vpn-auth integration", ENTRYPOINT.read_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, "no guard block before Tailscale starts in node-entrypoint.sh"
    return match.group(1)


class EntrypointGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.role_dir = self.work / "fleet-role.d"
        self.role_dir.mkdir()
        self.server_dir = self.work / "k3s" / "server"
        self.stub = self.work / "stub"
        self.stub.mkdir()
        bin_dir = self.work / "bin"
        bin_dir.mkdir()
        sleep = bin_dir / "sleep"
        sleep.write_text(SLEEP_STUB)
        sleep.chmod(0o755)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    def start(self, role: str, **env: str) -> subprocess.CompletedProcess[str]:
        """Runs the entrypoint's guards as the service ``role`` starts, then, if they let it through, builds a server's k3s arguments as the server case does and prints them after the word started."""
        script = "\n".join(
            [
                "set -u",
                f"role={role}",
                assignment("fleet_role_dir"),
                assignment("k3s_server_dir"),
                'vpn_auth="name=tailscale,joinKey=example"',
                'mesh_ip="100.64.0.1"',
                'api_port_flags=""',
                'etcd_timing_flags=""',
                'regrade_flags=""',
                function_source("idle"),
                function_source("fleet_role_guard"),
                function_source("set_join_url"),
                function_source("refuse_second_cluster"),
                function_source("mesh_server_args"),
                guard_block(),
                'if [ "$role" = server ]; then mesh_server_args; else server_args=""; fi',
                'printf "started %s\\n" "$server_args"',
            ]
        )
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("K3S_", "FLEET_ROLE_"))}
        settings = {"FLEET_ROLE_DIR": str(self.role_dir), "K3S_SERVER_DIR": str(self.server_dir), "STUB": str(self.stub), "PATH": self.path}
        return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=False, env=clean | settings | env, timeout=60)

    def server_args(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("started"), result.stdout + result.stderr)
        return result.stdout.split()[1:]

    def idled(self, result: subprocess.CompletedProcess[str]) -> None:
        """Asserts the start idled: it waited on sleep infinity, ended only through the stop signal (status 0, as `docker stop` leaves it) and got no further."""
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("started", result.stdout)
        self.assertEqual((self.stub / "sleep").read_text().splitlines(), ["infinity"])

    def former_member(self) -> None:
        (self.server_dir / "db.demoted" / "etcd").mkdir(parents=True)

    def test_the_join_url_file_takes_precedence_over_the_inventorys_join_url(self) -> None:
        (self.role_dir / "join-url").write_text(JOIN_URL + "\n")
        args = self.server_args(self.start("server", K3S_JOIN_SERVER_URL=BOOTSTRAP_URL))
        self.assertEqual(args[args.index("--server") + 1], JOIN_URL)
        self.assertNotIn(BOOTSTRAP_URL, args)
        self.assertNotIn("--cluster-init", args)
        # An empty file names no server, so the inventory's still counts.
        (self.role_dir / "join-url").write_text("")
        args = self.server_args(self.start("server", K3S_JOIN_SERVER_URL=BOOTSTRAP_URL))
        self.assertEqual(args[args.index("--server") + 1], BOOTSTRAP_URL)

    def test_a_server_with_its_datastore_and_nothing_to_join_keeps_cluster_init(self) -> None:
        (self.server_dir / "db" / "etcd").mkdir(parents=True)
        # A copy an earlier demotion moved aside beside it changes nothing: k3s reuses the datastore it finds and ignores the flag.
        self.former_member()
        args = self.server_args(self.start("server"))
        self.assertIn("--cluster-init", args)
        self.assertNotIn("--server", args)

    def test_a_former_member_with_nothing_to_join_is_refused_and_k3s_is_not_started(self) -> None:
        self.former_member()
        result = self.start("server")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("started", result.stdout)
        self.assertIn("has no server to join, so starting k3s would bootstrap a second cluster; not starting it", result.stderr)
        self.assertIn(f"join-url in the role directory ({self.role_dir}", result.stderr)
        # It exits rather than idling, so it never waited.
        self.assertFalse((self.stub / "sleep").exists())
        # With a join URL, from either source, it joins the cluster that exists.
        for env, written in (({"K3S_JOIN_SERVER_URL": BOOTSTRAP_URL}, ""), ({}, JOIN_URL)):
            with self.subTest(env=env, written=written):
                (self.role_dir / "join-url").write_text(written)
                args = self.server_args(self.start("server", **env))
                self.assertIn("--server", args)
                self.assertNotIn("--cluster-init", args)

    def test_a_first_bootstrap_with_no_datastore_at_all_passes_cluster_init(self) -> None:
        args = self.server_args(self.start("server"))
        self.assertIn("--cluster-init", args)
        self.assertNotIn("--server", args)

    def test_a_service_whose_role_the_role_file_does_not_name_idles(self) -> None:
        for role, recorded in (("server", "agent\n"), ("agent", "server"), ("server", "")):
            with self.subTest(role=role, recorded=recorded):
                (self.stub / "sleep").unlink(missing_ok=True)
                (self.role_dir / "role").write_text(recorded)
                result = self.start(role, K3S_JOIN_SERVER_URL=BOOTSTRAP_URL)
                self.idled(result)
                self.assertIn(f"{self.role_dir}/role names '{recorded.strip()}', not {role}, so k3s is not started", result.stderr)

    def test_a_server_whose_member_was_removed_idles_whatever_its_role_file_says(self) -> None:
        (self.server_dir / "db" / "etcd").mkdir(parents=True)
        (self.server_dir / "db" / "etcd" / "tombstone").touch()
        for recorded in (None, "server"):
            with self.subTest(recorded=recorded):
                (self.stub / "sleep").unlink(missing_ok=True)
                if recorded is not None:
                    (self.role_dir / "role").write_text(recorded)
                result = self.start("server", K3S_JOIN_SERVER_URL=BOOTSTRAP_URL)
                self.idled(result)
                self.assertIn("db/etcd/tombstone says this server's etcd member was removed from the cluster", result.stderr)
        # The tombstone is the server's: the agent service never runs etcd and starts as before.
        (self.role_dir / "role").write_text("agent")
        self.assertEqual(self.start("agent").returncode, 0)

    def test_with_no_role_file_each_service_starts_as_it_always_has(self) -> None:
        (self.server_dir / "db" / "etcd").mkdir(parents=True)
        self.assertEqual(
            self.server_args(self.start("server", K3S_JOIN_SERVER_URL=BOOTSTRAP_URL)),
            ["--disable=traefik", "--disable-network-policy", "--write-kubeconfig-mode=644", "--vpn-auth=name=tailscale,joinKey=example", "--resolv-conf=/etc/k3s-resolv.conf", "--node-ip=100.64.0.1", "--server", BOOTSTRAP_URL],
        )
        self.assertEqual(self.start("agent").returncode, 0)
        self.assertFalse((self.stub / "sleep").exists())
        # A role file naming the service's own role changes nothing either.
        (self.role_dir / "role").write_text("server\n")
        self.assertIn("--server", self.server_args(self.start("server", K3S_JOIN_SERVER_URL=BOOTSTRAP_URL)))

    def test_the_guards_run_before_tailscale_starts_and_the_server_joins_the_url_they_chose(self) -> None:
        text = ENTRYPOINT.read_text()
        self.assertLess(text.index(guard_block()), text.index("tailscaled --state="))
        match = re.search(r"^  server\)\n(.*?)^    ;;\n", text, re.MULTILINE | re.DOTALL)
        assert match is not None, "no server case in node-entrypoint.sh"
        self.assertIn('server_join_host=$(join_host_of "$join_url")', match.group(1))
        self.assertLess(match.group(1).index('wait_for_mesh_host "$server_join_host"'), match.group(1).index("exec /bin/k3s server"))


def has_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode == 0


class RoleDirectoryTest(unittest.TestCase):
    """The role directory reaches both services through an override of its own, which the role places and lists only for a host that takes part in the role controller (test_cluster_voters.py checks the list), and the directory the role creates for it."""

    def test_the_override_mounts_the_directory_read_only_into_both_services_and_nothing_else(self) -> None:
        services = yaml.safe_load(FLEET_ROLE_COMPOSE.read_text())["services"]
        # The regrade service reads the role file too, to idle on a host it no longer names a server (regrade/fleet-regrade.sh).
        self.assertEqual(services, {"k3s": {"volumes": [MOUNT]}, "k3s-agent": {"volumes": [MOUNT]}, "regrade": {"volumes": [MOUNT]}})
        self.assertEqual(assignment("fleet_role_dir"), "fleet_role_dir=${FLEET_ROLE_DIR:-/etc/github-runner}")
        # The base file does not mount it, so a host the controller never moves is not recreated by an upgrade.
        self.assertNotIn("fleet-role.d", (ROLE / "files" / "docker-compose.yml").read_text())

    def test_the_role_places_the_override_and_creates_the_directory_before_any_compose_command(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        names = [task["name"] for task in tasks]
        create = next(task for task in tasks if task.get("ansible.builtin.file", {}).get("state") == "directory")
        self.assertIn({"path": "fleet-role.d", "mode": "0755"}, create["loop"])
        place = next(task for task in tasks if "loop" in task and any(entry.get("src") == "docker-compose.yml" for entry in task["loop"]))
        self.assertIn({"src": "docker-compose.fleet-role.yml", "mode": "0644"}, place["loop"])
        self.assertLess(names.index(create["name"]), names.index("Set up the mesh the nodes join, and publish its join key and control server"))
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        self.assertIn("docker-compose.fleet-role.yml", defaults["github_runner_cluster_compose_files_written"])

    @unittest.skipUnless(has_compose(), "docker compose is not installed")
    def test_merged_both_containers_mount_the_projects_directory_read_only(self) -> None:
        project = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, project)
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("DOCKER_", "COMPOSE_", "K3S_", "TAILSCALE_"))}
        base = {"K3S_TOKEN": "t", "K3S_TLS_SAN_LIST": "n", "K3S_VERSION": "v1.34.1-k3s1", "TAILSCALE_VERSION": "v1.102.4"}
        args = ["docker", "compose", "--project-directory", str(project), "--project-name", "example", "-f", str(ROLE / "files" / "docker-compose.yml"), "-f", str(FLEET_ROLE_COMPOSE), "--profile", "server", "--profile", "agent", "config", "--format", "json"]
        result = subprocess.run(args, capture_output=True, text=True, check=False, env=clean | base)
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        for name in ("k3s", "k3s-agent"):
            with self.subTest(service=name):
                mounts = [volume for volume in services[name]["volumes"] if volume["target"] == "/etc/github-runner"]
                self.assertEqual([(mount["type"], mount["source"], mount["read_only"]) for mount in mounts], [("bind", str(project / "fleet-role.d"), True)])


if __name__ == "__main__":
    unittest.main()
