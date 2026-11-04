"""Tests for how the cluster role records a Docker host's role on the host and keeps it across runs: the filters (plugins/filter/cluster_role.py's cluster_host_role and plugins/filter/cluster_guard.py's cluster_role_lease_guard), the decision and its refusals (tasks/docker_role.yml), the record (tasks/docker_role_record.yml), the servers' turns and the regrade service following the recorded role (tasks/start_servers.yml, tasks/regrade_service.yml), the refusal of every run while the role controller holds the regrade Lease (tasks/role_lease_guard.yml), and the one .env both services read (templates/env.j2), run through the role (tests/node_password/run.yml, tests/regrade/service.yml, tests/cluster_versions/render_env.yml) against the stand-ins for docker and the Compose module, so that no real Docker daemon or cluster is reached."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from cluster_stand_ins import ANSIBLE_PLAYBOOK, HASH_NEW, ROLE, ROOT, RoleTestCase, ansible_env, flatten, has_compose, recap, role_tasks


def _filters(name: str) -> Any:
    """The filter plugin plugins/filter/<name>.py, loaded from its file."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "plugins" / "filter" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cluster_role = _filters("cluster_role")
cluster_guard = _filters("cluster_guard")

DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
HOLDER = DEFAULTS["github_runner_cluster_role_lease_holder"]
LEASE_NAME = DEFAULTS["github_runner_cluster_regrade_lease_name"]
LEASE_NAMESPACE = DEFAULTS["github_runner_cluster_regrade_lease_namespace"]
SWAP_ID = DEFAULTS["github_runner_cluster_role_lease_swap_id_annotation"]
COMPOSE = ROLE / "files" / "docker-compose.yml"
# Long enough that a Lease renewed this long ago is still unexpired however slowly a test runs, and the time it has run out by.
DURATION = 3600


def lease(holder: str, renewed_ago: int, duration: int = DURATION, **annotations: str) -> str:
    """A Lease as `kubectl get lease -o json` prints it, held by ``holder`` and last renewed ``renewed_ago`` seconds ago."""
    renewed = datetime.fromtimestamp(time.time() - renewed_ago, UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return json.dumps({"apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": {"name": LEASE_NAME, "namespace": LEASE_NAMESPACE, "annotations": annotations}, "spec": {"holderIdentity": holder, "leaseDurationSeconds": duration, "acquireTime": renewed, "renewTime": renewed}}, indent=2)


def read(host: str, stdout: str, rc: int = 0, **extra: Any) -> dict[str, Any]:
    """One server's read of the Lease, as the guard registers it: ``stopped``, or ``running``, the reading host's clock as epoch seconds and the Lease as kubectl prints it (nothing when there is none)."""
    return {"item": host, "rc": rc, "stdout": stdout, "stderr": "", **extra}


def running(host: str, text: str = "", now: float | None = None) -> dict[str, Any]:
    return read(host, f"running\n{int(time.time() if now is None else now)}\n{text}")


class LeaseGuardFilterTest(unittest.TestCase):
    @staticmethod
    def guard(reads: Any, holder: str) -> dict[str, str]:
        verdict: dict[str, str] = cluster_guard.cluster_role_lease_guard(reads, holder, SWAP_ID)
        return verdict

    def test_a_lease_the_controller_holds_and_has_renewed_in_time_is_held(self) -> None:
        verdict = self.guard([running("server1", lease(HOLDER, 60, **{SWAP_ID: "20990101T000000Z-server1-laptop"}))], HOLDER)
        self.assertEqual(verdict["state"], "held")
        self.assertIn("20990101T000000Z-server1-laptop", verdict["message"])
        self.assertIn("server1", verdict["message"])

    def test_a_lease_that_has_run_out_another_holder_and_no_holder_are_free(self) -> None:
        for text in (lease(HOLDER, DURATION + 60), lease("server2", 60), lease("", 60), ""):
            with self.subTest(text=text[:80]):
                self.assertEqual(self.guard([running("server1", text)], HOLDER)["state"], "free")

    def test_a_lease_with_no_renewal_counts_from_when_it_was_taken(self) -> None:
        taken = json.loads(lease(HOLDER, 60))
        del taken["spec"]["renewTime"]
        self.assertEqual(self.guard([running("server1", json.dumps(taken))], HOLDER)["state"], "held")

    def test_expiry_is_judged_by_the_reading_hosts_clock(self) -> None:
        text = lease(HOLDER, 60)
        self.assertEqual(self.guard([running("server1", text, now=time.time() + DURATION)], HOLDER)["state"], "free")

    def test_no_server_running_is_no_cluster_and_no_lease(self) -> None:
        self.assertEqual(self.guard([read("server1", "stopped"), read("server2", "stopped")], HOLDER)["state"], "free")
        self.assertEqual(self.guard([], HOLDER)["state"], "free")

    def test_one_answer_decides_and_a_server_that_could_not_be_read_does_not_matter_then(self) -> None:
        unread = read("server1", "", rc=1, stderr="Cannot connect to the Docker daemon")
        self.assertEqual(self.guard([unread, running("server2", lease(HOLDER, 60))], HOLDER)["state"], "held")
        self.assertEqual(self.guard([unread, running("server2", "")], HOLDER)["state"], "free")

    def test_no_answer_with_a_server_that_could_not_be_read_cannot_be_told(self) -> None:
        verdict = self.guard([read("server1", "", rc=1, stderr="Cannot connect to the Docker daemon"), read("server2", "stopped"), read("server3", "", unreachable=True, msg="ssh: connect timed out")], HOLDER)
        self.assertEqual(verdict["state"], "unknown")
        self.assertIn("server1 could not be read (Cannot connect to the Docker daemon)", verdict["message"])
        self.assertIn("server3 could not be read (ssh: connect timed out)", verdict["message"])

    def test_an_answer_that_is_not_a_lease_is_an_error(self) -> None:
        for stdout in ("running\nnot-a-time\n", "running\n0\nnot json", "running\n0\n[]", "something else"):
            with self.subTest(stdout=stdout), self.assertRaises(cluster_guard.AnsibleFilterError):
                self.guard([read("server1", stdout)], HOLDER)

    def test_the_filters_are_registered(self) -> None:
        self.assertEqual(sorted(cluster_role.FilterModule().filters()), ["cluster_host_role"])
        self.assertIn("cluster_role_lease_guard", cluster_guard.FilterModule().filters())


class DockerRoleTest(RoleTestCase):
    """docker_role.yml, docker_role_record.yml, start_servers.yml and role_lease_guard.yml, through the role against the stand-ins."""

    def setUp(self) -> None:
        super().setUp()
        self.inventory = self.hosts(("node1", "node2", "node3"), ("node4",), eligible=("node3", "node4"))
        # Every run of the role creates the role directory before it records a role (tasks/main.yml).
        for host in self.inventory[0]:
            self.role_file(host).parent.mkdir()

    def role_file(self, host: str) -> Path:
        return self.work / "nodes" / host / "fleet-role.d" / "role"

    def record(self, host: str, role: str) -> None:
        self.role_file(host).write_text(f"{role}\n")

    def tasks(self, tasks: str, check: bool = False, limit: str | None = None) -> subprocess.CompletedProcess[str]:
        hosts, group_vars = self.inventory
        return self.run_tasks(tasks, hosts, group_vars, check=check, limit=limit)

    def roles_reported(self, result: subprocess.CompletedProcess[str]) -> dict[str, str]:
        """The role each host's run decided on, as docker_role.yml reports it."""
        return dict(re.findall(r'"msg": "(node\d) is to run as (server|agent)\b', result.stdout))

    def test_an_eligible_host_keeps_the_role_it_records_over_the_inventory(self) -> None:
        self.record("node3", "agent")
        self.record("node4", "server")
        result = self.tasks("docker_role")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.roles_reported(result), {"node1": "server", "node2": "server", "node3": "agent", "node4": "server"})

    def test_a_host_that_is_not_eligible_follows_the_inventory_and_a_new_eligible_host_starts_from_it(self) -> None:
        result = self.tasks("docker_role")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.roles_reported(result), {"node1": "server", "node2": "server", "node3": "server", "node4": "agent"})

    def test_the_role_is_recorded_for_an_eligible_host_only_and_only_when_absent(self) -> None:
        # Written as the role controller may write it, without the newline a run of the role ends its own record with, so that writing over it would change its bytes.
        self.role_file("node4").write_text("server")
        result = self.tasks("docker_role,docker_role_record")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.role_file("node3").read_text(), "server\n")
        self.assertEqual(self.role_file("node4").read_text(), "server", "a recorded role is never written over")
        for host in ("node1", "node2"):
            self.assertFalse(self.role_file(host).exists(), host)
        self.assertEqual((recap(result.stdout, "node3")["changed"], recap(result.stdout, "node4")["changed"]), (1, 0))

    def test_the_servers_turns_follow_the_recorded_role(self) -> None:
        self.record("node3", "agent")
        self.record("node4", "server")
        for name in ("node1", "node2", "node4"):
            self.rendered(name, HASH_NEW)
        result = self.tasks("docker_role,start_servers")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertCountEqual([call for call in self.calls() if call.startswith("module up")], [f"module up node{n} profiles=server check=False" for n in (1, 2, 4)])

    def refused(self, host: str, *needles: str, tasks: str = "docker_role", check: bool = False) -> subprocess.CompletedProcess[str]:
        result = self.tasks(tasks, check=check)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"fatal: [{host}]", result.stdout)
        flat = " ".join(result.stdout.split())
        for needle in needles:
            self.assertIn(needle, flat)
        self.assertEqual(self.mutating(), [], "nothing was changed")
        return result

    def test_a_host_that_is_not_eligible_and_runs_the_other_role_is_refused(self) -> None:
        self.container("node2-k3s-agent")
        result = self.refused("node2", "its agent container (node2-k3s-agent) runs, but the inventory gives it the role server", "Nothing was changed.")
        self.assertNotIn("fatal: [node1]", result.stdout)

    def test_an_eligible_host_whose_container_differs_from_its_record_is_refused(self) -> None:
        self.record("node3", "agent")
        self.container("node3-k3s")
        self.refused("node3", "its server container (node3-k3s) runs, but its role directory records agent")

    def test_an_eligible_host_with_no_record_whose_container_differs_from_the_inventory_is_refused_before_anything_is_recorded(self) -> None:
        self.container("node4-k3s")
        self.refused("node4", "its server container (node4-k3s) runs, but the inventory gives it the role agent", tasks="docker_role,docker_role_record")
        self.assertFalse(self.role_file("node4").exists())

    def test_both_containers_running_is_refused(self) -> None:
        self.container("node1-k3s")
        self.container("node1-k3s-agent")
        self.refused("node1", "both of its node containers run (node1-k3s and node1-k3s-agent)")

    def test_a_record_left_on_a_host_that_is_no_longer_eligible_is_refused(self) -> None:
        self.record("node2", "agent")
        self.refused("node2", "its role directory records agent", "the inventory gives it the role server", str(self.role_file("node2")))

    def test_every_run_is_refused_while_the_role_controller_holds_the_lease(self) -> None:
        self.container("node1-k3s")
        self.write("fleet-regrade.lease", text=lease(HOLDER, 60, **{SWAP_ID: "20990101T000000Z-node2-node4"}))
        result = self.tasks("role_lease_guard")
        self.assertNotEqual(result.returncode, 0)
        for n in (1, 2, 3, 4):
            self.assertIn(f"fatal: [node{n}]", result.stdout)
        self.assertIn("20990101T000000Z-node2-node4", result.stdout)
        self.assertIn(f"exec node1-k3s kubectl get lease {LEASE_NAME} --namespace {LEASE_NAMESPACE} --ignore-not-found", "\n".join(self.calls()))

    def test_a_lease_regrading_holds_or_one_that_has_run_out_lets_the_run_go_on(self) -> None:
        self.container("node1-k3s")
        for text in (lease("node2", 60), lease(HOLDER, DURATION + 60), ""):
            with self.subTest(text=text[:60]):
                self.write("fleet-regrade.lease", text=text)
                result = self.tasks("role_lease_guard")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_cluster_that_cannot_be_read_refuses_and_one_that_is_not_running_does_not(self) -> None:
        result = self.tasks("role_lease_guard")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.write("daemon-down", text="")
        result = self.tasks("role_lease_guard")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("node1 could not be read", result.stdout)

    def test_check_mode_reports_the_refusals(self) -> None:
        self.container("node2-k3s-agent")
        self.refused("node2", "the inventory gives it the role server", check=True)
        self.write("fleet-regrade.lease", text=lease(HOLDER, 60))
        self.container("node1-k3s")
        result = self.tasks("role_lease_guard", check=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fatal: [node3]", result.stdout)
        self.assertIn(f"holds the Lease {LEASE_NAMESPACE}/{LEASE_NAME}", " ".join(result.stdout.split()))


class RegradeServiceTest(unittest.TestCase):
    """regrade_service.yml for a host whose recorded role is agent, with regrading on, against a stand-in docker that records its calls."""

    def test_the_regrade_service_is_removed_from_a_host_recorded_as_an_agent_and_never_started(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            stub = Path(work) / "bin" / "docker"
            stub.parent.mkdir()
            stub.write_text('#!/bin/sh\necho "$*" >> "$STUB_CALLS"\ncase "$1" in rm) echo "$3" ;; *) echo "unexpected docker call: $*" >&2; exit 2 ;; esac\n')
            stub.chmod(0o755)
            calls = Path(work) / "calls"
            env = {**ansible_env(), "PATH": f"{stub.parent}:{os.environ['PATH']}", "STUB_CALLS": str(calls)}
            args = [ANSIBLE_PLAYBOOK, str(ROOT / "tests" / "regrade" / "service.yml"), "-e", "github_runner_cluster_regrade=true", "-e", "github_runner_cluster_docker_role=agent"]
            result = subprocess.run(args, capture_output=True, text=True, check=False, env=env, cwd=work)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(calls.read_text().splitlines(), ["rm --force github-runner-regrade-1"])


class EnvTest(unittest.TestCase):
    """The one .env every Docker host gets (templates/env.j2), rendered by tests/cluster_versions/render_env.yml."""

    def render(self, work: Path, *extra: str) -> str:
        output = work / "env"
        args = [ANSIBLE_PLAYBOOK, str(ROOT / "tests" / "cluster_versions" / "render_env.yml"), "-e", f"env_output={output}", "-e", "github_runner_cluster_dir=/srv/github-runner", "-e", "github_runner_cluster_effective_server_url=https://node1.example.internal:6443", *extra]
        result = subprocess.run(args, capture_output=True, text=True, check=False, env=ansible_env(), cwd=work)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return output.read_text()

    def test_there_is_one_template_and_it_is_the_same_for_either_role(self) -> None:
        self.assertFalse((ROLE / "templates" / "env-agent.j2").exists())
        with tempfile.TemporaryDirectory() as work:
            server = self.render(Path(work), "-e", "github_runner_cluster_effective_node_role=server", "-e", "github_runner_cluster_docker_role=server")
            agent = self.render(Path(work), "-e", "github_runner_cluster_effective_node_role=agent", "-e", "github_runner_cluster_docker_role=agent")
        self.assertEqual(server, agent)
        lines = server.splitlines()
        names = {line.split("=", 1)[1] for line in lines if line.startswith(("K3S_NODE_NAME=", "K3S_AGENT_NODE_NAME="))}
        self.assertEqual(len(names), 1, "both services join under the host's one node name")
        for line in ("K3S_JOIN_SERVER_URL=https://node1.example.internal:6443", "K3S_AGENT_SERVER_URL=https://node1.example.internal:6443", "K3S_CONTAINER_NAME=github-runner-k3s", "K3S_AGENT_CONTAINER_NAME=github-runner-k3s-agent"):
            self.assertIn(line, lines)

    def test_the_bootstrap_hosts_agent_joins_the_first_other_server_since_it_has_no_bootstrap_host_to_dial(self) -> None:
        # A bootstrap host has no server URL of its own, and a role switch makes it an agent that must join the cluster it left.
        peers = ["https://node2.example.internal:6443", "https://node3.example.internal:6443"]
        extra = ["-e", json.dumps({"github_runner_cluster_effective_server_url": "", "github_runner_cluster_topology": {"hosts": {"localhost": {"peer_server_urls": peers}}}})]
        with tempfile.TemporaryDirectory() as work:
            lines = self.render(Path(work), *extra).splitlines()
        self.assertIn("K3S_JOIN_SERVER_URL=", lines, "the bootstrap server itself still joins nothing")
        self.assertIn(f"K3S_AGENT_SERVER_URL={peers[0]}", lines)

    @unittest.skipUnless(has_compose(), "docker compose is not installed")
    def test_each_service_renders_as_it_did_from_the_env_its_role_got_before(self) -> None:
        # Each service's configuration hash, which Compose compares with the running container's to decide whether to recreate it, is the same from the one .env as from the .env a host of its role got before there was one: a server's held every variable but the agent's own, and an agent's held its own (its server URL and node name, from the same settings as the server's) beside the shared ones and the variables Compose requires of every service whichever profile is active. So an upgrade recreates no running node.
        agent_own = ("K3S_AGENT_CONTAINER_NAME", "K3S_AGENT_SERVER_URL", "K3S_AGENT_NODE_NAME")
        server_only = ("K3S_JOIN_SERVER_URL", "K3S_DATASTORE", "K3S_NODE_NAME", "K3S_ETCD_HEARTBEAT_INTERVAL", "K3S_ETCD_ELECTION_TIMEOUT")
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("DOCKER_", "COMPOSE_", "K3S_", "TAILSCALE_"))}
        with tempfile.TemporaryDirectory() as work:
            settings = {"github_runner_cluster_etcd_heartbeat_interval": 500, "github_runner_cluster_etcd_election_timeout": 5000, "github_runner_cluster_datastore": "sqlite", "github_runner_cluster_node_taints": ["example.com/taint=true:NoSchedule"], "github_runner_cluster_mesh_control_server_url": "https://headscale.example.internal"}
            text = self.render(Path(work), "-e", json.dumps(settings))
            full = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in text.splitlines() if "=" in line and not line.startswith(("#", "COMPOSE_"))}
            before = {
                "k3s": {key: value for key, value in full.items() if key not in agent_own},
                "k3s-agent": {key: value for key, value in full.items() if key not in server_only and key not in agent_own}
                | {"K3S_AGENT_CONTAINER_NAME": full["K3S_AGENT_CONTAINER_NAME"], "K3S_AGENT_SERVER_URL": full["K3S_JOIN_SERVER_URL"], "K3S_AGENT_NODE_NAME": full["K3S_NODE_NAME"]},
            }

            def config_hash(service: str, variables: dict[str, str], name: str) -> str:
                env_file = Path(work) / f"{service}-{name}.env"
                env_file.write_text("".join(f"{key}={value}\n" for key, value in variables.items()))
                result = subprocess.run(["docker", "compose", "--project-directory", work, "--project-name", "example", "--env-file", str(env_file), "-f", str(COMPOSE), "--profile", "server", "--profile", "agent", "config", "--hash", service], capture_output=True, text=True, check=False, env=clean)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.split()[1]

            for service, variables in before.items():
                with self.subTest(service=service):
                    self.assertEqual(config_hash(service, full, "now"), config_hash(service, variables, "before"))


class TaskStructureTest(unittest.TestCase):
    def test_the_docker_path_decides_the_role_and_checks_the_lease_before_it_writes_anything(self) -> None:
        tasks = role_tasks("main.yml")
        includes = [task.get("ansible.builtin.include_tasks") for task in tasks]
        decided, guarded = includes.index("docker_role.yml"), includes.index("role_lease_guard.yml")
        self.assertEqual((includes.index("topology.yml") + 1, decided + 1), (decided, guarded))
        first_write = next(index for index, task in enumerate(tasks) if "ansible.builtin.file" in task)
        self.assertLess(guarded, first_write)
        self.assertEqual(includes.index("docker_role_record.yml"), first_write + 1)

    def test_the_native_path_checks_the_lease_right_after_its_topology(self) -> None:
        includes = [task.get("ansible.builtin.include_tasks") for task in role_tasks("native/main.yml")]
        self.assertEqual(includes.index("role_lease_guard.yml"), includes.index("topology.yml") + 1)

    def test_the_profile_the_container_and_the_regrade_service_follow_the_recorded_role(self) -> None:
        tasks = role_tasks("main.yml")
        conditions = [str(task.get("when", "")) for task in tasks if "block" in task]
        self.assertIn('github_runner_cluster_docker_role == "server"', conditions)
        self.assertIn('github_runner_cluster_docker_role == "agent"', conditions)
        self.assertNotIn("effective_node_role", (ROLE / "tasks" / "main.yml").read_text())
        for name in ("github_runner_cluster_node_container", "github_runner_cluster_node_service"):
            self.assertIn("github_runner_cluster_docker_role", DEFAULTS[name])
        self.assertIn("regrade_service.yml", [task.get("ansible.builtin.include_tasks") for task in flatten(tasks) if "block" not in task])
        self.assertIn("github_runner_cluster_docker_role", (ROLE / "tasks" / "regrade_service.yml").read_text())

    def test_the_recorded_role_file_is_the_one_the_compose_override_mounts(self) -> None:
        override = yaml.safe_load((ROLE / "files" / "docker-compose.fleet-role.yml").read_text())
        mounted = override["services"]["k3s"]["volumes"][0].split(":")[0]
        self.assertEqual(DEFAULTS["github_runner_cluster_docker_role_file"], "{{ github_runner_cluster_dir }}/" + mounted.removeprefix("./") + "/role")


if __name__ == "__main__":
    unittest.main()
