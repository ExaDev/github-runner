"""Tests for the cluster role's etcd timing settings: the defaults, the checks in tasks/etcd_timing.yml (run through the role with tests/cluster_versions/checks.yml), the server's .env, the Compose file, and the flags node-entrypoint.sh passes to k3s."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_cluster"
ENTRYPOINT = ROLE / "files" / "k3s" / "node-entrypoint.sh"
COMPOSE = ROLE / "files" / "docker-compose.yml"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())

HEARTBEAT = "github_runner_cluster_etcd_heartbeat_interval"
ELECTION = "github_runner_cluster_etcd_election_timeout"


def ansible_env() -> dict[str, str]:
    return {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_RETRY_FILES_ENABLED": "false"}


class DefaultsTest(unittest.TestCase):
    def test_both_are_unset_so_k3s_keeps_its_own_timing(self) -> None:
        self.assertEqual(DEFAULTS[HEARTBEAT], "")
        self.assertEqual(DEFAULTS[ELECTION], "")


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class RoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)

    def check(self, hosts: dict[str, dict[str, Any]] | None = None, **variables: Any) -> subprocess.CompletedProcess[str]:
        """Run the topology and etcd timing checks on a group of hosts (three servers unless ``hosts`` says otherwise), with ``variables`` as group variables."""
        inventory = self.work / "inventory.yml"
        group = {"vars": variables, "hosts": hosts or {"node1": {}, "node2": {}, "node3": {}}}
        inventory.write_text(yaml.safe_dump({"all": {"children": {"github_runner_cluster": group}}}))
        args = [ANSIBLE_PLAYBOOK, "-i", str(inventory), str(ROOT / "tests" / "cluster_versions" / "checks.yml"), "-e", "check_tasks=topology,etcd_timing"]
        return subprocess.run(args, capture_output=True, text=True, check=False, env=ansible_env(), cwd=self.work)

    def test_neither_set_passes(self) -> None:
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_valid_pair_passes(self) -> None:
        for heartbeat, election in ((500, 5000), ("250", "1250"), (1, 5)):
            with self.subTest(heartbeat=heartbeat, election=election):
                result = self.check(**{HEARTBEAT: heartbeat, ELECTION: election})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_election_timeout_under_five_times_the_heartbeat_is_refused(self) -> None:
        for heartbeat, election in ((500, 2499), (1, 4)):
            with self.subTest(heartbeat=heartbeat, election=election):
                result = self.check(**{HEARTBEAT: heartbeat, ELECTION: election})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"must be at least five times github_runner_cluster_etcd_heartbeat_interval ({heartbeat} ms), which is {5 * heartbeat} ms", result.stdout)

    def test_one_set_without_the_other_is_refused(self) -> None:
        for variables in ({HEARTBEAT: 500}, {ELECTION: 5000}, {HEARTBEAT: 500, ELECTION: ""}):
            with self.subTest(variables=variables):
                result = self.check(**variables)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("set both or neither", result.stdout)

    def test_a_value_that_is_not_a_positive_whole_number_is_refused(self) -> None:
        for value in ("abc", "1.5", -100, 0, "010", True):
            with self.subTest(heartbeat=value):
                result = self.check(**{HEARTBEAT: value, ELECTION: 100000})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be a positive whole number of milliseconds", result.stdout)
            with self.subTest(election=value):
                result = self.check(**{HEARTBEAT: 100, ELECTION: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be a positive whole number of milliseconds", result.stdout)

    def test_servers_with_different_timing_are_refused_on_every_host(self) -> None:
        hosts = {"node1": {}, "node2": {HEARTBEAT: 500, ELECTION: 5000}, "node3": {}}
        result = self.check(hosts)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("node1: k3s's own; node2: 500 ms heartbeat, 5000 ms election; node3: k3s's own", result.stdout.replace("\n", " "))
        self.assertEqual(sorted(re.findall(r"fatal: \[(node\d)\]", result.stdout)), ["node1", "node2", "node3"])

    def test_servers_that_differ_in_one_value_are_refused(self) -> None:
        hosts = {"node1": {HEARTBEAT: 500, ELECTION: 5000}, "node2": {HEARTBEAT: 500, ELECTION: 6000}, "node3": {HEARTBEAT: 500, ELECTION: 5000}}
        result = self.check(hosts)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("do not all have the same etcd timing", result.stdout)

    def test_the_same_timing_set_per_host_passes(self) -> None:
        result = self.check({name: {HEARTBEAT: 500, ELECTION: 5000} for name in ("node1", "node2", "node3")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_agent_with_other_timing_is_not_compared_with_the_servers(self) -> None:
        # Agents run no etcd, so only the servers must agree; the fourth host is an agent (three servers are chosen automatically from four hosts).
        hosts = {"node1": {HEARTBEAT: 500, ELECTION: 5000}, "node2": {HEARTBEAT: 500, ELECTION: 5000}, "node3": {HEARTBEAT: 500, ELECTION: 5000}, "node4": {}}
        result = self.check(hosts)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def render_env(self, *extra: str) -> list[str]:
        env = self.work / "env"
        args = [ANSIBLE_PLAYBOOK, str(ROOT / "tests" / "cluster_versions" / "render_env.yml"), "-e", f"env_output={env}", "-e", "github_runner_cluster_dir=/srv/github-runner", *extra]
        result = subprocess.run(args, capture_output=True, text=True, check=False, env=ansible_env(), cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return env.read_text().splitlines()

    def test_the_env_carries_both_values_when_set(self) -> None:
        # The agent service takes neither from it: see ComposeFileTest.
        lines = self.render_env("-e", f"{HEARTBEAT}=500", "-e", f"{ELECTION}=5000")
        self.assertIn("K3S_ETCD_HEARTBEAT_INTERVAL=500", lines)
        self.assertIn("K3S_ETCD_ELECTION_TIMEOUT=5000", lines)

    def test_the_env_names_neither_by_default(self) -> None:
        self.assertEqual([line for line in self.render_env() if "ETCD" in line], [])


class ComposeFileTest(unittest.TestCase):
    def test_only_the_server_service_takes_them_and_empty_by_default(self) -> None:
        services = yaml.safe_load(COMPOSE.read_text())["services"]
        self.assertIn("K3S_ETCD_HEARTBEAT_INTERVAL=${K3S_ETCD_HEARTBEAT_INTERVAL:-}", services["k3s"]["environment"])
        self.assertIn("K3S_ETCD_ELECTION_TIMEOUT=${K3S_ETCD_ELECTION_TIMEOUT:-}", services["k3s"]["environment"])
        for name, service in services.items():
            if name != "k3s":
                with self.subTest(service=name):
                    self.assertEqual([entry for entry in service.get("environment", []) if "ETCD" in entry], [])


def has_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode == 0


@unittest.skipUnless(has_compose(), "docker compose is not installed")
class ComposeRenderTest(unittest.TestCase):
    def environment(self, **env: str) -> dict[str, str]:
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("DOCKER_", "COMPOSE_", "K3S_", "TAILSCALE_"))}
        args = ["docker", "compose", "--project-directory", tempfile.gettempdir(), "--project-name", "example", "-f", str(COMPOSE), "--profile", "server", "--profile", "agent", "config", "--format", "json"]
        base = {"K3S_TOKEN": "t", "K3S_TLS_SAN_LIST": "n", "K3S_VERSION": "v1.34.1-k3s1", "TAILSCALE_VERSION": "v1.102.4"}
        result = subprocess.run(args, capture_output=True, text=True, check=False, env=clean | base | env)
        self.assertEqual(result.returncode, 0, result.stderr)
        environment: dict[str, str] = json.loads(result.stdout)["services"]["k3s"]["environment"]
        return environment

    def test_the_container_gets_the_values_from_the_env_file(self) -> None:
        environment = self.environment(K3S_ETCD_HEARTBEAT_INTERVAL="500", K3S_ETCD_ELECTION_TIMEOUT="5000")
        self.assertEqual(environment["K3S_ETCD_HEARTBEAT_INTERVAL"], "500")
        self.assertEqual(environment["K3S_ETCD_ELECTION_TIMEOUT"], "5000")

    def test_the_container_gets_empty_values_when_the_env_file_has_none(self) -> None:
        environment = self.environment()
        self.assertEqual(environment["K3S_ETCD_HEARTBEAT_INTERVAL"], "")
        self.assertEqual(environment["K3S_ETCD_ELECTION_TIMEOUT"], "")


def function_source(name: str) -> str:
    """The definition of a shell function in the entrypoint, from its first line to the closing brace at the start of a line."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", ENTRYPOINT.read_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, f"{name} is not defined in node-entrypoint.sh"
    return match.group(0)


class EntrypointTest(unittest.TestCase):
    """Runs the entrypoint's own argument-list functions in a shell, without Tailscale or k3s: the script starts tailscaled and execs k3s at top level, so only the functions and the call that sets the timing flags are taken from it."""

    def server_args(self, builder: str, **env: str) -> list[str]:
        script = "\n".join(
            [
                "set -u",
                'vpn_auth="name=tailscale,joinKey=example"',
                'mesh_ip="100.64.0.1"',
                'regrade_flags=""',
                # The server's join URL as set_join_url chooses it when the role directory names none (test_node_entrypoint.py tests the choice).
                'join_url="${K3S_JOIN_SERVER_URL:-}"',
                function_source("set_etcd_timing_flags"),
                function_source("set_api_port_flags"),
                function_source(builder),
                "set_etcd_timing_flags",
                "set_api_port_flags",
                f"{builder}",
                'printf "%s\\n" "$server_args"',
            ]
        )
        clean = {key: value for key, value in os.environ.items() if not key.startswith("K3S_")}
        result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=False, env=clean | env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_the_script_calls_the_function_that_sets_the_flags(self) -> None:
        self.assertRegex(ENTRYPOINT.read_text(), re.compile(r"^set_etcd_timing_flags$", re.MULTILINE))

    def test_both_flags_follow_the_other_server_arguments_on_the_mesh_and_off_it(self) -> None:
        for builder in ("mesh_server_args", "direct_server_args"):
            with self.subTest(builder=builder):
                args = self.server_args(builder, K3S_ETCD_HEARTBEAT_INTERVAL="500", K3S_ETCD_ELECTION_TIMEOUT="5000", K3S_TLS_SAN_LIST="a b")
                self.assertEqual(args[-2:], ["--etcd-arg=heartbeat-interval=500", "--etcd-arg=election-timeout=5000"])
                self.assertEqual(args.count("--tls-san"), 2)

    def test_a_joining_server_gets_them_too(self) -> None:
        args = self.server_args("mesh_server_args", K3S_ETCD_HEARTBEAT_INTERVAL="500", K3S_ETCD_ELECTION_TIMEOUT="5000", K3S_JOIN_SERVER_URL="https://bootstrap.example.internal:6443")
        self.assertIn("--server", args)
        self.assertIn("--etcd-arg=heartbeat-interval=500", args)
        self.assertIn("--etcd-arg=election-timeout=5000", args)

    def test_neither_variable_leaves_the_arguments_without_any_etcd_arg(self) -> None:
        for builder in ("mesh_server_args", "direct_server_args"):
            with self.subTest(builder=builder):
                self.assertEqual([arg for arg in self.server_args(builder) if "etcd" in arg], [])
                self.assertEqual([arg for arg in self.server_args(builder, K3S_ETCD_HEARTBEAT_INTERVAL="", K3S_ETCD_ELECTION_TIMEOUT="") if "etcd" in arg], [])

    def test_the_arguments_without_the_variables_are_what_they_were_before(self) -> None:
        self.assertEqual(self.server_args("direct_server_args"), ["--disable=traefik", "--disable-network-policy", "--write-kubeconfig-mode=644"])
        self.assertEqual(
            self.server_args("mesh_server_args"),
            ["--disable=traefik", "--disable-network-policy", "--write-kubeconfig-mode=644", "--vpn-auth=name=tailscale,joinKey=example", "--resolv-conf=/etc/k3s-resolv.conf", "--node-ip=100.64.0.1", "--cluster-init"],
        )

    def test_the_agent_command_line_does_not_take_them(self) -> None:
        agent_line = next(line for line in ENTRYPOINT.read_text().splitlines() if "k3s agent" in line)
        self.assertNotIn("etcd", agent_line)


if __name__ == "__main__":
    unittest.main()
