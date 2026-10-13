"""Tests for the cluster role's tasks/host_network.yml, run with ansible-playbook (tests/host_network/play.yml) against a stand-in docker command that records what it is asked and answers from files the test writes: the network mode must be one the role knows; with the host network the Docker host must have an address on the LAN and no other Tailscale device; and moving a running node between networks is refused while the cluster needs it or cannot be read, or when another node moves in the same run. Nothing the tasks run may change anything. Also what tasks/main.yml decides about the network, read from the file."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAY = ROOT / "tests" / "host_network" / "play.yml"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
LAN = "192.0.2.0/24"
ON_LAN = "3: col0    inet 192.0.2.44/24 brd 192.0.2.255 scope global col0\\       valid_lft 85000sec"
OFF_LAN = "2: eth0    inet 192.168.5.1/24 brd 192.168.5.255 scope global eth0\\       valid_lft 85000sec"

# The stand-in docker appends its arguments and DOCKER_CONTEXT to calls.log and answers from files in the work directory: docker ps lists container-state (empty: no such container), docker run prints addresses, a "--" line and links, and docker exec answers kubectl: /readyz/etcd from etcd-<container> (no file: the read fails), the node list from nodes.json and the pods from pods.json. A daemon-down file makes every call fail, kubectl-nodes-fails (or kubectl-nodes-fails-<container>) makes the node list fail, and hang-<context> makes docker exec on that context sleep for the seconds it holds.
STUBS = {
    "docker": """\
        #!/bin/sh
        echo "docker ${DOCKER_CONTEXT:--} $*" >> "$STUB_DIR/calls.log"
        [ -e "$STUB_DIR/daemon-down" ] && { echo "Cannot connect to the Docker daemon" >&2; exit 1; }
        case "$1" in
          ps) cat "$STUB_DIR/container-state" ;;
          run) cat "$STUB_DIR/addresses"; echo --; cat "$STUB_DIR/links" ;;
          exec)
            [ -e "$STUB_DIR/hang-${DOCKER_CONTEXT:--}" ] && sleep "$(cat "$STUB_DIR/hang-${DOCKER_CONTEXT:--}")"
            case "$5" in
              --request-timeout=*) [ -e "$STUB_DIR/etcd-$2" ] || { echo "Error from server" >&2; exit 1; }; cat "$STUB_DIR/etcd-$2" ;;
              nodes) [ -e "$STUB_DIR/kubectl-nodes-fails" ] || [ -e "$STUB_DIR/kubectl-nodes-fails-$2" ] && { echo "The connection to the server 127.0.0.1:6443 was refused" >&2; exit 1; }; cat "$STUB_DIR/nodes.json" ;;
              pods) cat "$STUB_DIR/pods.json" ;;
            esac ;;
        esac
        """,
}
LINKS = "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN\\    link/loopback 00:00:00:00:00:00\n2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel state UP\\    link/ether 02:00:00:00:00:01"
TAILSCALE_LINK = "5: tailscale0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1280 qdisc fq_codel state UNKNOWN\\    link/none"
TOPOLOGY = {"hosts": {name: {"role": "server", "bootstrap": name == "node1", "node_name": name} for name in ("node1", "node2", "node3")}}


def node(name: str, ready: bool = True, labelled: bool = True) -> dict[str, Any]:
    labels = {"node-role.kubernetes.io/etcd": "true"} if labelled else {}
    return {"metadata": {"name": name, "labels": labels}, "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown"}]}}


RUNNER = {"metadata": {"name": "runner-1", "namespace": "runners", "labels": {"actions-ephemeral-runner": "True"}}, "spec": {"nodeName": "node1"}, "status": {"phase": "Running"}}
HEALTHY = [node("node1"), node("node2"), node("node3")]


def failed_task(stdout: str) -> str | None:
    """Return the name of the task whose header last precedes a "fatal:" line in ansible-playbook's output, or None when nothing failed."""
    task = None
    for line in stdout.splitlines():
        if line.startswith("TASK [") and line.rstrip("*").rstrip().endswith("]"):
            task = line[len("TASK [") : line.rstrip("*").rstrip().rindex("]")]
        elif line.startswith("fatal: "):
            return task
    return None


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class HostNetworkRoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        for name, script in STUBS.items():
            path = self.bin / name
            path.write_text(textwrap.dedent(script))
            path.chmod(0o755)
        inventory = {name: {"github_runner_cluster_container_name": f"{name}-k3s"} for name in ("node1", "node2", "node3")}
        inventory["node2"]["github_runner_cluster_docker_context"] = "ctx2"
        inventory["node3"]["github_runner_cluster_docker_context"] = "ctx3"
        (self.work / "inventory.json").write_text(json.dumps({"all": {"hosts": inventory}}))
        self.state(addresses=[OFF_LAN, ON_LAN], container="running github-runner_default", nodes=HEALTHY, pods=[])
        self.etcd("node2-k3s", "node3-k3s")

    def state(self, addresses: list[str], container: str, nodes: list[dict[str, Any]], pods: list[dict[str, Any]], links: str = LINKS) -> None:
        (self.work / "addresses").write_text("\n".join(addresses) + "\n")
        (self.work / "links").write_text(links + "\n")
        (self.work / "container-state").write_text(container + "\n" if container else "")
        (self.work / "nodes.json").write_text(json.dumps({"items": nodes}))
        (self.work / "pods.json").write_text(json.dumps({"items": pods}))

    def etcd(self, *healthy: str) -> None:
        for old in self.work.glob("etcd-*"):
            old.unlink()
        for container in healthy:
            (self.work / f"etcd-{container}").write_text("ok")

    def run_tasks(self, *args: str, limit: str = "node1", timeout: int | None = None, **role_vars: Any) -> subprocess.CompletedProcess[str]:
        extra = {
            "github_runner_cluster_network_mode": "host",
            "github_runner_cluster_lan_cidr": LAN,
            "github_runner_cluster_effective_node_role": "server",
            "github_runner_cluster_effective_node_name": "node1",
            "github_runner_cluster_topology": TOPOLOGY,
            **role_vars,
        }
        extra_file = self.work / "extra.json"
        extra_file.write_text(json.dumps(extra))
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "STUB_DIR": str(self.work),
            "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"),
            "ANSIBLE_LOCALHOST_WARNING": "false",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
            "ANSIBLE_RETRY_FILES_ENABLED": "false",
        }
        command = [ANSIBLE_PLAYBOOK, "-i", str(self.work / "inventory.json"), str(PLAY), "--limit", limit, "-e", f"@{extra_file}", *args]
        return subprocess.run(command, capture_output=True, text=True, check=False, env=env, cwd=self.work, timeout=timeout)

    def calls(self) -> list[str]:
        log = self.work / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_moving_a_healthy_node_to_the_host_network_passes_after_asking_the_cluster(self) -> None:
        result = self.run_tasks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertIn("docker - run --rm --network host busybox:1.37 sh -c ip -4 -o addr show && echo -- && ip -o link show", calls)
        self.assertTrue("docker - exec github-runner-k3s kubectl get nodes --request-timeout=30s -o json" in calls, calls)
        self.assertIn("docker - exec github-runner-k3s kubectl get pods --all-namespaces --request-timeout=30s --field-selector spec.nodeName=node1 -o json", calls)
        self.assertIn("docker ctx2 exec node2-k3s kubectl get --request-timeout=30s --raw /readyz/etcd", calls)
        self.assertIn("docker ctx3 exec node3-k3s kubectl get --request-timeout=30s --raw /readyz/etcd", calls)

    def test_the_cluster_reads_run_under_check(self) -> None:
        self.state(addresses=[ON_LAN], container="running github-runner_default", nodes=[node("node1"), node("node2"), node("node3", ready=False)], pods=[])
        result = self.run_tasks("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("node3 (not Ready)", result.stdout)

    def test_a_healthy_cluster_passes_under_check(self) -> None:
        result = self.run_tasks("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_server_whose_etcd_is_not_healthy_refuses_the_move(self) -> None:
        self.etcd("node2-k3s")
        result = self.run_tasks()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("node3 (etcd not reported healthy)", result.stdout)

    def test_servers_without_role_labels_refuse_the_move(self) -> None:
        self.state(addresses=[ON_LAN], container="running github-runner_default", nodes=[node("node1", labelled=False), node("node2", labelled=False), node("node3", ready=False, labelled=False)], pods=[])
        result = self.run_tasks()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not label node1, node2, node3 as server nodes", result.stdout)

    def test_a_hung_node_read_on_this_host_is_cut_off_and_another_server_answers(self) -> None:
        (self.work / "hang--").write_text("60")
        result = self.run_tasks(github_runner_cluster_cluster_read_timeout=2, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("docker ctx2 exec node2-k3s kubectl get nodes --request-timeout=2s -o json", self.calls())

    def test_a_hung_etcd_read_is_cut_off_and_counts_as_unhealthy(self) -> None:
        (self.work / "hang-ctx3").write_text("60")
        result = self.run_tasks(github_runner_cluster_cluster_read_timeout=2, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("node3 (etcd not reported healthy)", result.stdout)

    def test_a_cluster_no_server_can_be_read_through_refuses_and_says_why(self) -> None:
        (self.work / "kubectl-nodes-fails").write_text("")
        result = self.run_tasks()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(failed_task(result.stdout), "Fail if no server of the inventory could be read")
        self.assertIn("node1 could not be read (The connection to the server 127.0.0.1:6443 was refused)", result.stdout)
        self.assertIn("node3 could not be read", result.stdout)

    def test_the_cluster_is_read_through_another_server_when_this_one_cannot_list_it(self) -> None:
        (self.work / "kubectl-nodes-fails-github-runner-k3s").write_text("")
        result = self.run_tasks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("docker ctx2 exec node2-k3s kubectl get nodes --request-timeout=30s -o json", self.calls())

    def test_an_agent_in_a_group_with_no_bootstrap_host_reads_the_servers(self) -> None:
        topology = {"hosts": {"node1": {"role": "server", "bootstrap": False, "node_name": "node1"}, "node2": {"role": "agent", "bootstrap": False, "node_name": "node2"}}}
        self.state(addresses=[ON_LAN], container="running github-runner_default", nodes=[node("node1"), node("node2", labelled=False)], pods=[])
        result = self.run_tasks(limit="node2", github_runner_cluster_effective_node_role="agent", github_runner_cluster_effective_node_name="node2", github_runner_cluster_topology=topology)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("docker - exec node1-k3s kubectl get nodes --request-timeout=30s -o json", self.calls())

    def test_an_agent_whose_servers_are_outside_the_inventory_says_why_it_cannot_move(self) -> None:
        topology = {"hosts": {"node2": {"role": "agent", "bootstrap": False, "node_name": "node2"}}}
        result = self.run_tasks(limit="node2", github_runner_cluster_effective_node_role="agent", github_runner_cluster_effective_node_name="node2", github_runner_cluster_topology=topology)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("The inventory holds no server of this cluster", result.stdout)

    def test_a_docker_daemon_that_cannot_be_reached_fails_the_run(self) -> None:
        (self.work / "daemon-down").write_text("")
        result = self.run_tasks(github_runner_cluster_network_mode="bridge", github_runner_cluster_lan_cidr="")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(failed_task(result.stdout), "Read whether the node's container runs, and on which network")

    def test_an_unknown_network_mode_fails_before_running_anything(self) -> None:
        result = self.run_tasks(github_runner_cluster_network_mode="macvlan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("it must be bridge", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_two_nodes_moving_in_one_run_are_refused(self) -> None:
        result = self.run_tasks(limit="node1,node2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("This run would move node1, node2 to another network at once", result.stdout)
        self.assertFalse(any(" kubectl get " in call for call in self.calls()))

    def test_another_tailscale_device_on_the_host_network_refuses_host_mode(self) -> None:
        for container in ("running github-runner_default", "", "exited host"):
            with self.subTest(container=container):
                self.state(addresses=[ON_LAN], container=container, nodes=HEALTHY, pods=[], links=LINKS + "\n" + TAILSCALE_LINK)
                result = self.run_tasks()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("already has a Tailscale device on its network (tailscale0)", result.stdout)

    def test_the_node_s_own_tailscale_device_is_expected_once_it_runs_on_the_host_network(self) -> None:
        self.state(addresses=[ON_LAN], container="running host", nodes=HEALTHY, pods=[], links=LINKS + "\n" + TAILSCALE_LINK)
        result = self.run_tasks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_address_on_the_lan_fails_and_names_the_addresses_found(self) -> None:
        self.state(addresses=[OFF_LAN], container="running github-runner_default", nodes=HEALTHY, pods=[])
        result = self.run_tasks()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no address in 192.0.2.0/24", result.stdout)
        self.assertIn("192.168.5.1/24", result.stdout)
        self.assertFalse(any(" kubectl get " in call for call in self.calls()))

    def test_the_host_network_without_a_lan_fails_before_running_anything(self) -> None:
        result = self.run_tasks(github_runner_cluster_lan_cidr="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("github_runner_cluster_lan_cidr is empty", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_server_not_ready_refuses_the_move(self) -> None:
        self.state(addresses=[ON_LAN], container="running github-runner_default", nodes=[node("node1"), node("node2"), node("node3", ready=False)], pods=[])
        result = self.run_tasks()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Not moving node1 to the host network", result.stdout)
        self.assertIn("node3 (not Ready)", result.stdout)

    def test_a_runner_pod_on_the_node_refuses_the_move(self) -> None:
        self.state(addresses=[ON_LAN], container="running github-runner_default", nodes=HEALTHY, pods=[RUNNER])
        result = self.run_tasks()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runners/runner-1", result.stdout)

    def test_an_agent_reads_the_cluster_through_the_bootstrap_server_and_no_etcd(self) -> None:
        self.state(addresses=[ON_LAN], container="running github-runner_default", nodes=[*HEALTHY, node("node4", labelled=False)], pods=[])
        result = self.run_tasks(limit="node2", github_runner_cluster_effective_node_role="agent", github_runner_cluster_effective_node_name="node4")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any("/readyz/etcd" in call for call in self.calls()))
        self.assertIn("docker - exec node1-k3s kubectl get nodes --request-timeout=30s -o json", self.calls())
        self.assertIn("docker - ps --all --filter name=^/github-runner-k3s-agent$ --format {{.State}} {{.Networks}}", self.calls())

    def test_a_node_already_on_the_host_network_is_not_checked_against_the_cluster(self) -> None:
        self.state(addresses=[ON_LAN], container="running host", nodes=[node("node1"), node("node2", ready=False), node("node3", ready=False)], pods=[RUNNER])
        result = self.run_tasks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(" kubectl get " in call for call in self.calls()))

    def test_a_node_that_is_not_running_or_missing_is_not_checked_against_the_cluster(self) -> None:
        for container in ("exited github-runner_default", ""):
            with self.subTest(container=container):
                self.state(addresses=[ON_LAN], container=container, nodes=[node("node1"), node("node2", ready=False)], pods=[RUNNER])
                (self.work / "calls.log").unlink(missing_ok=True)
                result = self.run_tasks()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(any(" kubectl get " in call for call in self.calls()))

    def test_moving_back_to_bridge_is_guarded_too_and_needs_no_lan(self) -> None:
        self.state(addresses=[OFF_LAN], container="running host", nodes=HEALTHY, pods=[RUNNER])
        result = self.run_tasks(github_runner_cluster_network_mode="bridge", github_runner_cluster_lan_cidr="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Not moving node1 to the bridge network", result.stdout)
        self.assertFalse(any(" run " in call for call in self.calls()))

    def test_bridge_staying_bridge_reads_only_the_container(self) -> None:
        result = self.run_tasks(github_runner_cluster_network_mode="bridge", github_runner_cluster_lan_cidr="")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call.split()[2] for call in self.calls()], ["ps"])


class MainTasksTest(unittest.TestCase):
    """What tasks/main.yml decides about the network mode, read from the file, since it runs only against a real Docker host."""

    def test_the_loopback_api_wait_runs_only_on_the_host_network(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles" / "github_runner_cluster" / "tasks" / "main.yml").read_text())
        wait = next(task for task in tasks if "ansible.builtin.wait_for" in task)
        self.assertEqual(wait["ansible.builtin.wait_for"]["host"], "127.0.0.1")
        self.assertIn("github_runner_cluster_network_mode == 'host'", wait["when"])

    def test_the_network_checks_run_before_anything_changes(self) -> None:
        tasks = yaml.safe_load((ROOT / "roles" / "github_runner_cluster" / "tasks" / "main.yml").read_text())
        includes = [task.get("ansible.builtin.include_tasks") for task in tasks]
        first_change = next(index for index, task in enumerate(tasks) if "ansible.builtin.file" in task or "ansible.builtin.copy" in task or "block" in task)
        self.assertLess(includes.index("host_network.yml"), first_change)


if __name__ == "__main__":
    unittest.main()
