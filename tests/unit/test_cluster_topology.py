"""Tests for the github_runner_cluster role's server and agent selection filter."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from typing import Any

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "filter" / "cluster_topology.py"
_spec = importlib.util.spec_from_file_location("cluster_topology", PLUGIN)
assert _spec is not None and _spec.loader is not None
cluster_topology = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cluster_topology)


def topology(hosts: dict[str, dict[str, Any]], max_servers: int = 5, api_port: int = 6443) -> dict[str, Any]:
    """Run the filter over hosts, whose insertion order stands in for inventory order."""
    return cluster_topology.github_runner_cluster_topology(list(hosts), hosts, max_servers, api_port)


def plain(count: int) -> dict[str, dict[str, Any]]:
    """Return count hosts named h1..hN with no overrides."""
    return {f"h{index}": {} for index in range(1, count + 1)}


class ServerCountTest(unittest.TestCase):
    def test_the_largest_odd_count_up_to_five_hosts_become_servers(self) -> None:
        for count, servers in [(1, 1), (2, 1), (3, 3), (4, 3), (5, 5), (6, 5), (9, 5)]:
            with self.subTest(hosts=count):
                result = topology(plain(count))
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["servers"], [f"h{index}" for index in range(1, servers + 1)])
                agents = [name for name, host in result["hosts"].items() if host["role"] == "agent"]
                self.assertEqual(agents, [f"h{index}" for index in range(servers + 1, count + 1)])

    def test_max_servers_caps_the_count_at_its_largest_odd_value(self) -> None:
        self.assertEqual(topology(plain(6), max_servers=3)["servers"], ["h1", "h2", "h3"])
        self.assertEqual(topology(plain(6), max_servers=4)["servers"], ["h1", "h2", "h3"])
        self.assertEqual(topology(plain(6), max_servers=1)["servers"], ["h1"])

    def test_max_servers_below_one_is_an_error(self) -> None:
        self.assertTrue(topology(plain(3), max_servers=0)["errors"])


class BootstrapAndJoinTest(unittest.TestCase):
    def test_the_first_server_bootstraps_and_every_other_node_joins_it_by_address(self) -> None:
        hosts = {name: {"github_runner_cluster_tailnet_domain": "example.ts.net"} for name in ["a", "b", "c", "d"]}
        hosts["a"]["github_runner_cluster_node_name"] = "k3s-server"
        result = topology(hosts)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["bootstrap"], "a")
        self.assertEqual(result["hosts"]["a"]["server_url"], "")
        for name in ["b", "c", "d"]:
            self.assertEqual(result["hosts"][name]["server_url"], "https://k3s-server.example.ts.net:6443")
        self.assertEqual(result["hosts"]["d"]["role"], "agent")

    def test_every_node_gets_every_server_address_as_tls_sans_in_inventory_order(self) -> None:
        hosts = {name: {"github_runner_cluster_tailnet_domain": "example.ts.net"} for name in ["a", "b", "c", "d"]}
        result = topology(hosts)
        expected = ["a.example.ts.net", "b.example.ts.net", "c.example.ts.net"]
        for host in result["hosts"].values():
            self.assertEqual(host["tls_sans"], expected)

    def test_without_a_tailnet_domain_the_address_is_the_bare_node_name(self) -> None:
        result = topology(plain(3))
        self.assertEqual(result["hosts"]["h2"]["server_url"], "https://h1:6443")

    def test_a_node_address_override_replaces_the_derived_address(self) -> None:
        hosts = plain(3)
        hosts["h1"]["github_runner_cluster_node_address"] = "10.0.0.1"
        result = topology(hosts, api_port=7443)
        self.assertEqual(result["hosts"]["h3"]["server_url"], "https://10.0.0.1:7443")
        self.assertEqual(result["hosts"]["h3"]["tls_sans"], ["10.0.0.1", "h2", "h3"])


class OverrideTest(unittest.TestCase):
    def test_an_agent_override_moves_the_server_places_down_the_inventory(self) -> None:
        hosts = plain(4)
        hosts["h1"]["github_runner_cluster_node_role"] = "agent"
        result = topology(hosts)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["servers"], ["h2", "h3", "h4"])
        self.assertEqual(result["bootstrap"], "h2")
        self.assertEqual(result["hosts"]["h1"]["server_url"], "https://h2:6443")

    def test_a_bootstrap_override_picks_the_bootstrap_host_and_makes_it_a_server(self) -> None:
        hosts = plain(4)
        hosts["h4"]["github_runner_cluster_bootstrap"] = True
        result = topology(hosts)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["bootstrap"], "h4")
        self.assertEqual(result["servers"], ["h1", "h2", "h4"])
        self.assertEqual(result["hosts"]["h1"]["server_url"], "https://h4:6443")

    def test_a_false_bootstrap_override_passes_the_bootstrap_to_the_next_server(self) -> None:
        hosts = plain(3)
        hosts["h1"]["github_runner_cluster_bootstrap"] = "false"
        self.assertEqual(topology(hosts)["bootstrap"], "h2")

    def test_a_tls_sans_override_replaces_the_automatic_list_for_that_host_only(self) -> None:
        hosts = plain(3)
        hosts["h2"]["github_runner_cluster_tls_sans"] = ["custom.example"]
        result = topology(hosts)
        self.assertEqual(result["hosts"]["h2"]["tls_sans"], ["custom.example"])
        self.assertEqual(result["hosts"]["h1"]["tls_sans"], ["h1", "h2", "h3"])

    def test_a_single_host_with_a_server_url_joins_a_cluster_outside_the_inventory(self) -> None:
        for role in ["server", "agent"]:
            with self.subTest(role=role):
                hosts = {"solo": {"github_runner_cluster_node_role": role, "github_runner_cluster_server_url": "https://elsewhere:6443"}}
                result = topology(hosts)
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["bootstrap"], "")
                self.assertEqual(result["hosts"]["solo"]["role"], role)
                self.assertEqual(result["hosts"]["solo"]["server_url"], "https://elsewhere:6443")


class ValidationTest(unittest.TestCase):
    def assert_error(self, hosts: dict[str, dict[str, Any]], fragment: str) -> None:
        errors = topology(hosts)["errors"]
        self.assertTrue(any(fragment in error for error in errors), errors)

    def test_a_server_override_takes_a_server_place_from_inventory_order(self) -> None:
        hosts = plain(2)
        hosts["h2"]["github_runner_cluster_node_role"] = "server"
        result = topology(hosts)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["servers"], ["h2"])

    def test_an_even_server_count_is_an_error(self) -> None:
        hosts = plain(3)
        hosts["h1"]["github_runner_cluster_node_role"] = "server"
        hosts["h2"]["github_runner_cluster_node_role"] = "server"
        hosts["h3"]["github_runner_cluster_node_role"] = "agent"
        self.assert_error(hosts, "odd number")

    def test_no_servers_at_all_is_an_error(self) -> None:
        hosts = plain(2)
        for host in hosts.values():
            host["github_runner_cluster_node_role"] = "agent"
        self.assert_error(hosts, "nothing to join")

    def test_more_than_one_bootstrap_host_is_an_error(self) -> None:
        hosts = plain(3)
        hosts["h1"]["github_runner_cluster_bootstrap"] = True
        hosts["h2"]["github_runner_cluster_bootstrap"] = True
        self.assert_error(hosts, "More than one host")

    def test_an_agent_cannot_bootstrap(self) -> None:
        hosts = plain(3)
        hosts["h3"]["github_runner_cluster_node_role"] = "agent"
        hosts["h3"]["github_runner_cluster_bootstrap"] = True
        self.assert_error(hosts, "is an agent")

    def test_a_host_cannot_both_bootstrap_and_join(self) -> None:
        hosts = plain(1)
        hosts["h1"]["github_runner_cluster_bootstrap"] = True
        hosts["h1"]["github_runner_cluster_server_url"] = "https://elsewhere:6443"
        self.assert_error(hosts, "not both")

    def test_an_unknown_node_role_is_an_error(self) -> None:
        hosts = plain(1)
        hosts["h1"]["github_runner_cluster_node_role"] = "controller"
        self.assert_error(hosts, "must be 'server' or 'agent'")


if __name__ == "__main__":
    unittest.main()
