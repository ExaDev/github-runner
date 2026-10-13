"""Tests for the filter the cluster role uses to tell whether recreating a node container would be safe for the cluster (plugins/filter/cluster_guard.py)."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from typing import Any

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "filter" / "cluster_guard.py"
_spec = importlib.util.spec_from_file_location("cluster_guard_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

def node(name: str, ready: bool = True, server: bool = True) -> dict[str, Any]:
    labels = {"node-role.kubernetes.io/control-plane": "true", "node-role.kubernetes.io/etcd": "true"} if server else {}
    return {"metadata": {"name": name, "labels": labels}, "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown"}]}}


def pod(name: str, node_name: str, runner: bool = True, phase: str = "Running") -> dict[str, Any]:
    labels = {"actions-ephemeral-runner": "True"} if runner else {"app": "other"}
    return {"metadata": {"name": name, "namespace": "runners", "labels": labels}, "spec": {"nodeName": node_name}, "status": {"phase": phase}}


def etcd(name: str, healthy: bool = True, unreachable: bool = False) -> dict[str, Any]:
    """One loop result of the role's etcd read through a server's container."""
    if unreachable:
        return {"item": {"key": f"host-{name}", "value": {"node_name": name}}, "unreachable": True, "msg": "ssh: connect to host"}
    return {"item": {"key": f"host-{name}", "value": {"node_name": name}}, "rc": 0 if healthy else 1, "stdout": "ok" if healthy else "", "stderr": "" if healthy else "etcd failed: reason withheld"}


def blockers(nodes: list[dict[str, Any]], pods: list[dict[str, Any]], name: str, servers: list[str] | None = None, reports: list[dict[str, Any]] | None = None) -> list[str]:
    """The guard with the inventory's servers defaulting to the labelled ones, and every other server's etcd healthy unless given."""
    if servers is None:
        servers = [n["metadata"]["name"] for n in nodes if n["metadata"]["labels"]]
    if reports is None:
        reports = [etcd(server) for server in servers if server != name]
    result: list[str] = guard.cluster_restart_blockers(nodes, pods, name, servers, reports)
    return result


class ClusterRestartBlockersTest(unittest.TestCase):
    def test_three_healthy_servers_and_no_runners_may_restart(self) -> None:
        self.assertEqual(blockers([node("a"), node("b"), node("c")], [], "a"), [])

    def test_a_server_may_not_restart_while_another_server_is_not_ready(self) -> None:
        found = blockers([node("a"), node("b"), node("c", ready=False)], [], "a")
        self.assertEqual(len(found), 1)
        self.assertIn("1 of 3 servers with a Ready node and a healthy etcd, fewer than the 2", found[0])
        self.assertIn("c (not Ready)", found[0])

    def test_a_ready_server_whose_etcd_is_unhealthy_does_not_count(self) -> None:
        for report in (etcd("c", healthy=False), etcd("c", unreachable=True)):
            with self.subTest(report=report):
                found = blockers([node("a"), node("b"), node("c")], [], "a", reports=[etcd("b"), report])
                self.assertEqual(len(found), 1)
                self.assertIn("c (etcd not reported healthy)", found[0])

    def test_a_server_whose_etcd_was_not_read_does_not_count(self) -> None:
        self.assertNotEqual(blockers([node("a"), node("b"), node("c")], [], "a", reports=[etcd("b")]), [])

    def test_a_server_the_inventory_does_not_name_counts_against_the_restart(self) -> None:
        self.assertEqual(blockers([node("a"), node("b"), node("c"), node("d"), node("x")], [], "a", servers=["a", "b", "c", "d"]), [])
        found = blockers([node("a"), node("b"), node("x"), node("y")], [], "a", servers=["a", "b"])
        self.assertIn("x (not in the inventory, so its etcd was not read)", found[0])

    def test_servers_the_cluster_does_not_label_refuse_the_restart(self) -> None:
        unlabelled = [node("a", server=False), node("b", server=False), node("c", server=False, ready=False)]
        found = blockers(unlabelled, [], "a", servers=["a", "b", "c"])
        self.assertTrue(any("does not label a, b, c as server nodes" in blocker for blocker in found), found)

    def test_a_server_missing_from_the_node_list_does_not_count(self) -> None:
        found = blockers([node("a"), node("b")], [], "a", servers=["a", "b", "c"])
        self.assertIn("c (not in the node list)", found[0])

    def test_five_servers_tolerate_one_other_unhealthy_but_not_two(self) -> None:
        servers = [node(name) for name in "abcde"]
        self.assertEqual(blockers([*servers[:4], node("e", ready=False)], [], "a"), [])
        self.assertNotEqual(blockers([*servers[:3], node("d", ready=False), node("e", ready=False)], [], "a"), [])

    def test_this_nodes_own_readiness_does_not_count(self) -> None:
        self.assertEqual(blockers([node("a", ready=False), node("b"), node("c")], [], "a"), [])

    def test_two_servers_can_never_lose_one(self) -> None:
        self.assertNotEqual(blockers([node("a"), node("b")], [], "a"), [])

    def test_a_single_server_has_no_quorum_to_lose(self) -> None:
        self.assertEqual(blockers([node("a"), node("w", server=False)], [], "a"), [])

    def test_an_agent_ignores_the_servers_health(self) -> None:
        nodes = [node("a"), node("b", ready=False), node("c", ready=False), node("w", server=False)]
        self.assertEqual(blockers(nodes, [], "w", reports=[]), [])

    def test_an_unfinished_runner_pod_on_this_node_blocks(self) -> None:
        for phase in ("Running", "Pending"):
            with self.subTest(phase=phase):
                found = blockers([node("a"), node("b"), node("c")], [pod("runner-1", "a", phase=phase)], "a")
                self.assertEqual(len(found), 1)
                self.assertIn("runners/runner-1", found[0])

    def test_finished_runners_other_pods_and_other_nodes_runners_do_not_block(self) -> None:
        pods = [pod("done", "a", phase="Succeeded"), pod("failed", "a", phase="Failed"), pod("web", "a", runner=False), pod("elsewhere", "b")]
        self.assertEqual(blockers([node("a"), node("b"), node("c")], pods, "a"), [])

    def test_both_reasons_are_given_together(self) -> None:
        self.assertEqual(len(blockers([node("a"), node("b", ready=False), node("c")], [pod("runner-1", "a")], "a")), 2)

    def test_a_node_missing_from_the_cluster_blocks(self) -> None:
        self.assertIn("not in the cluster's node list", blockers([node("b")], [], "a", servers=["a", "b"])[0])

    def test_input_that_is_not_lists_is_an_error(self) -> None:
        with self.assertRaises(guard.AnsibleFilterError):
            guard.cluster_restart_blockers([], [], "a", "a", [])

    def test_the_filters_are_registered(self) -> None:
        self.assertIs(guard.FilterModule().filters()["addresses_in_network"], guard.addresses_in_network)

    def test_the_restart_blockers_filter_is_registered(self) -> None:
        self.assertIs(guard.FilterModule().filters()["cluster_restart_blockers"], guard.cluster_restart_blockers)


class ClusterGuardStateTest(unittest.TestCase):
    STATE = '{"nodes": {"items": [{"metadata": {"name": "a"}}]}, "pods": {"items": []}}'

    def test_the_first_server_that_answered_gives_the_state(self) -> None:
        results = [{"item": "a", "rc": 1, "stderr": "refused"}, {"item": "b", "rc": 0, "stdout": self.STATE}, {"item": "c", "rc": 0, "stdout": '{"nodes": {"items": []}, "pods": {"items": []}}'}]
        self.assertEqual(guard.cluster_guard_state(results), {"state": {"nodes": [{"metadata": {"name": "a"}}], "pods": []}, "problem": ""})

    def test_no_server_answering_says_why_for_each(self) -> None:
        found = guard.cluster_guard_state([{"item": "a", "rc": 1, "stderr": "x\nrefused"}, {"item": "b", "unreachable": True}, {"item": "c", "rc": 0, "stdout": "not json"}])
        self.assertIsNone(found["state"])
        self.assertIn("a could not be read (refused); b could not be reached; c gave an answer that is not the cluster's state", found["problem"])

    def test_a_read_cut_off_by_its_timeout_or_with_only_a_message_is_named(self) -> None:
        for result in ({"item": "a", "failed": True, "timedout": {"period": 2}, "msg": "Task failed: Timed out after 2 second(s)."}, {"item": "a", "failed": True, "msg": "The ansible.builtin.shell action failed to execute in the expected time frame (2) and was terminated"}):
            with self.subTest(result=result):
                self.assertIn("a did not answer within github_runner_cluster_cluster_read_timeout", guard.cluster_guard_state([result])["problem"])
        self.assertIn("a could not be read (docker: command not found)", guard.cluster_guard_state([{"item": "a", "failed": True, "msg": "docker: command not found"}])["problem"])

    def test_no_server_in_the_inventory_says_so(self) -> None:
        found = guard.cluster_guard_state([])
        self.assertIsNone(found["state"])
        self.assertIn("The inventory holds no server of this cluster", found["problem"])

    def test_input_that_is_not_a_list_is_an_error(self) -> None:
        with self.assertRaises(guard.AnsibleFilterError):
            guard.cluster_guard_state("a")
        with self.assertRaises(guard.AnsibleFilterError):
            guard.cluster_guard_state(["a"])

    def test_the_filter_is_registered(self) -> None:
        self.assertIs(guard.FilterModule().filters()["cluster_guard_state"], guard.cluster_guard_state)


IP_ADDR = [
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever",
    "2: eth0    inet 192.168.5.1/24 metric 200 brd 192.168.5.255 scope global dynamic eth0\\       valid_lft 85000sec",
    "3: col0    inet 192.0.2.44/24 metric 300 brd 192.0.2.255 scope global dynamic col0\\       valid_lft 85000sec",
    "4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever",
]


class AddressesInNetworkTest(unittest.TestCase):
    def test_an_address_in_the_network_is_found(self) -> None:
        self.assertEqual(guard.addresses_in_network(IP_ADDR, "192.0.2.0/24"), ["192.0.2.44"])

    def test_no_address_in_the_network_is_an_empty_list(self) -> None:
        self.assertEqual(guard.addresses_in_network(IP_ADDR, "198.51.100.0/24"), [])

    def test_the_network_is_matched_by_prefix_not_by_text(self) -> None:
        self.assertEqual(guard.addresses_in_network(IP_ADDR, "192.0.0.0/16"), ["192.0.2.44"])
        self.assertEqual(guard.addresses_in_network(["5: x    inet 192.0.20.4/24 scope global x"], "192.0.2.0/24"), [])

    def test_lines_without_an_address_are_skipped(self) -> None:
        self.assertEqual(guard.addresses_in_network(["", "garbage", "6: tun    inet"], "192.0.2.0/24"), [])

    def test_an_invalid_network_is_an_error(self) -> None:
        for network in ("", "192.0.2.0/33", "not-a-network", "2001:db8::/32"):
            with self.subTest(network=network), self.assertRaises(guard.AnsibleFilterError):
                guard.addresses_in_network(IP_ADDR, network)

    def test_a_string_instead_of_lines_is_an_error(self) -> None:
        with self.assertRaises(guard.AnsibleFilterError):
            guard.addresses_in_network("\\n".join(IP_ADDR), "192.0.2.0/24")


if __name__ == "__main__":
    unittest.main()
