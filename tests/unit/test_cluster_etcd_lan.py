"""Tests for moving etcd's peer traffic between k3s servers onto their LAN addresses (github_runner_cluster_etcd_lan_address and github_runner_cluster_etcd_peers_over_lan): the filters that check the inventory, read etcd's member list and decide which member moves (plugins/filter/cluster_etcd_lan.py), the flags node-entrypoint.sh passes to k3s, the server's .env and Compose file list (templates/env.j2 and defaults/main.yml, rendered by tests/cluster_versions/render_env.yml), the Compose override that passes the two variables, and where the move sits in a server's turn. A host that sets neither keeps exactly the arguments, .env and Compose files it had."""

from __future__ import annotations

import importlib.util
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
OVERRIDE = ROLE / "files" / "docker-compose.etcd-lan.yml"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())

MESH_IP = "100.64.0.1"
LAN_IP = "192.0.2.11"
# The arguments mesh_server_args built before the LAN address existed, for a bootstrap server and for a joining server with two TLS names: a host that sets no address must keep them exactly.
BOOTSTRAP_ARGS = ["--disable=traefik", "--disable-network-policy", "--write-kubeconfig-mode=644", "--vpn-auth=name=tailscale,joinKey=example", "--resolv-conf=/etc/k3s-resolv.conf", f"--node-ip={MESH_IP}", "--cluster-init"]
JOIN_ARGS = [*BOOTSTRAP_ARGS[:-1], "--server", "https://node1.example.internal:6443", "--tls-san", "node1.example.internal", "--tls-san", "node2.example.internal"]


def _filters() -> Any:
    spec = importlib.util.spec_from_file_location("cluster_etcd_lan", ROOT / "plugins" / "filter" / "cluster_etcd_lan.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FILTERS = _filters()


def host(name: str, **overrides: Any) -> dict[str, Any]:
    """A server of the group as tasks/etcd_lan.yml describes it to cluster_etcd_lan_problems: a Docker node on the host network with an address in the LAN."""
    index = int(name[-1])
    return {"name": name, "node_name": name, "role": "server", "eligible": False, "node_mode": "compose", "network_mode": "host", "lan_cidr": "192.0.2.0/24", "mesh": "tailscale", "address": f"192.0.2.1{index}", "peers_over_lan": True} | overrides


GROUP = [host("node1"), host("node2"), host("node3")]


def member_list(*members: tuple[int, str, list[str]]) -> str:
    """etcdctl member list -w json output, whose IDs are 64-bit integers."""
    return json.dumps({"header": {"cluster_id": 1}, "members": [{"ID": ident, "name": name, "peerURLs": urls, "clientURLs": []} for ident, name, urls in members]})


class ProblemsTest(unittest.TestCase):
    def problems(self, hosts: list[dict[str, Any]], peers_over_lan: bool = True) -> list[str]:
        result: list[str] = FILTERS.cluster_etcd_lan_problems(hosts, peers_over_lan)
        return result

    def test_three_servers_on_the_host_network_with_addresses_of_their_own_pass(self) -> None:
        self.assertEqual(self.problems(GROUP), [])

    def test_addresses_without_the_switch_pass_and_an_agent_without_one_is_not_asked_for_one(self) -> None:
        group = [host(name, peers_over_lan=False) for name in ("node1", "node2", "node3")] + [host("node4", role="agent", address="", peers_over_lan=False)]
        self.assertEqual(self.problems(group, peers_over_lan=False), [])

    def test_a_server_or_an_eligible_host_without_an_address_is_named(self) -> None:
        problems = self.problems([host("node1"), host("node2", address=""), host("node3"), host("node4", role="agent", eligible=True, address="")])
        self.assertEqual(len(problems), 2)
        self.assertIn("node2 is a server but sets no", problems[0])
        self.assertIn("node4 is eligible for a vote but sets no", problems[1])

    def test_the_bridge_network_is_refused_with_why(self) -> None:
        (problem,) = self.problems([host("node1"), host("node2", network_mode="bridge"), host("node3")])
        self.assertIn("node2", problem)
        self.assertIn("etcd's check of the peer certificate against the connection's source", problem)

    def test_a_native_node_is_refused(self) -> None:
        (problem,) = self.problems([host("node1"), host("node2", node_mode="native"), host("node3")])
        self.assertIn("node2 is a native node", problem)

    def test_an_address_outside_the_lan_not_ipv4_or_taken_twice_is_named(self) -> None:
        self.assertIn("is not in github_runner_cluster_lan_cidr", self.problems([host("node1", address="198.51.100.1"), host("node2"), host("node3")])[0])
        self.assertIn("is not an IPv4 address", self.problems([host("node1", address="node1.lan"), host("node2"), host("node3")])[0])
        self.assertIn("node2 and node1 both set", self.problems([host("node1"), host("node2", address="192.0.2.11"), host("node3")])[0])

    def test_an_agent_that_cannot_take_a_vote_may_not_set_one(self) -> None:
        (problem,) = self.problems([*GROUP, host("node4", role="agent")])
        self.assertIn("node4 sets github_runner_cluster_etcd_lan_address but is neither a server nor eligible", problem)

    def test_the_switch_must_be_the_same_on_every_host(self) -> None:
        (problem,) = self.problems([host("node1"), host("node2", peers_over_lan=False), host("node3")])
        self.assertIn("node2 sets github_runner_cluster_etcd_peers_over_lan to False", problem)

    def test_headscale_in_cluster_is_refused_with_the_switch_on_and_allowed_with_it_off(self) -> None:
        group = [host(name, mesh="headscale_in_cluster") for name in ("node1", "node2", "node3")]
        self.assertTrue(all("headscale_in_cluster" in problem for problem in self.problems(group)))
        self.assertEqual(self.problems([entry | {"peers_over_lan": False} for entry in group], peers_over_lan=False), [])


class MembersTest(unittest.TestCase):
    def test_ids_come_back_in_hexadecimal_and_peer_urls_as_hosts(self) -> None:
        (member,) = FILTERS.cluster_etcd_members(member_list((12345678901234567890, "node1-0a1b2c3d", [f"https://{MESH_IP}:2380"])))
        self.assertEqual(member, {"id": "ab54a98ceb1f0ad2", "name": "node1-0a1b2c3d", "peer_hosts": [MESH_IP], "learner": False})

    def test_a_node_s_member_is_found_by_k3s_s_naming(self) -> None:
        members = FILTERS.cluster_etcd_members(member_list((1, "node1-0a1b2c3d", [f"https://{MESH_IP}:2380"]), (2, "node10-0a1b2c3d", ["https://100.64.0.10:2380"])))
        self.assertEqual(FILTERS.cluster_etcd_member_of(members, "node1")["id"], "1")
        self.assertIsNone(FILTERS.cluster_etcd_member_of(members, "node2"))

    def test_two_members_for_one_node_are_refused(self) -> None:
        members = FILTERS.cluster_etcd_members(member_list((1, "node1-0a1b2c3d", []), (2, "node1-ffffffff", [])))
        with self.assertRaisesRegex(Exception, "lists 2 members for node1"):
            FILTERS.cluster_etcd_member_of(members, "node1")

    def test_output_that_is_not_a_member_list_is_refused(self) -> None:
        for output in ("", "{}", '{"members": [{"name": "x"}]}', member_list((1, "a-00000000", ["unix://socket"]))):
            with self.subTest(output=output), self.assertRaises(Exception):
                FILTERS.cluster_etcd_members(output)


class DecisionTest(unittest.TestCase):
    def test_only_the_first_member_that_would_move_moves(self) -> None:
        hosts = [{"name": "node1", "switch": ""}, {"name": "node2", "switch": "to_lan"}, {"name": "node3", "switch": "to_lan"}]
        self.assertEqual(FILTERS.cluster_etcd_lan_switches(hosts), ["node2"])
        self.assertEqual(FILTERS.cluster_etcd_lan_switches([{"name": "node1", "switch": ""}]), [])

    def test_a_certificate_without_its_own_address_or_an_unread_one_holds_the_move(self) -> None:
        certificates = [{"item": "node2", "rc": 0, "stdout_lines": ["100.64.0.2", "192.0.2.12"]}, {"item": "node3", "rc": 0, "stdout_lines": ["100.64.0.3"]}, {"item": "node4", "unreachable": True}]
        readiness = FILTERS.cluster_etcd_lan_readiness(certificates, [], {"node2": "192.0.2.12", "node3": "192.0.2.13", "node4": "192.0.2.14"})
        self.assertEqual(readiness, {"unnamed": ["node3", "node4"], "unreached": []})

    def test_a_server_that_does_not_reach_the_address_keeps_the_member_on_the_mesh(self) -> None:
        certificates = [{"item": "node2", "rc": 0, "stdout_lines": ["192.0.2.12"]}]
        readiness = FILTERS.cluster_etcd_lan_readiness(certificates, [{"item": "node2", "rc": 1}], {"node2": "192.0.2.12"})
        self.assertEqual(readiness, {"unnamed": [], "unreached": ["node2"]})


def function_source(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", ENTRYPOINT.read_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, f"{name} is not defined in node-entrypoint.sh"
    return match.group(0)


class EntrypointTest(unittest.TestCase):
    """mesh_server_args, run in a shell with the other flag sets empty, as test_cluster_etcd_timing.py runs it."""

    def server_args(self, **env: str) -> list[str]:
        script = "\n".join(
            [
                "set -u",
                'vpn_auth="name=tailscale,joinKey=example"',
                f'mesh_ip="{MESH_IP}"',
                'regrade_flags=""',
                'api_port_flags=""',
                'etcd_timing_flags=""',
                'join_url="${K3S_JOIN_SERVER_URL:-}"',
                function_source("mesh_server_args"),
                "mesh_server_args",
                'printf "%s\\n" "$server_args"',
            ]
        )
        clean = {key: value for key, value in os.environ.items() if not key.startswith("K3S_")}
        result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=False, env=clean | env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_without_the_variables_the_arguments_are_what_they_were(self) -> None:
        self.assertEqual(self.server_args(), BOOTSTRAP_ARGS)
        self.assertEqual(self.server_args(K3S_ETCD_LAN_ADDRESS="", K3S_ETCD_PEER_ADDRESS=""), BOOTSTRAP_ARGS)
        joining = {"K3S_JOIN_SERVER_URL": "https://node1.example.internal:6443", "K3S_TLS_SAN_LIST": "node1.example.internal node2.example.internal"}
        self.assertEqual(self.server_args(**joining), JOIN_ARGS)

    def test_the_lan_address_alone_names_it_in_the_certificates_and_keeps_etcd_on_the_mesh(self) -> None:
        args = self.server_args(K3S_ETCD_LAN_ADDRESS=LAN_IP, K3S_TLS_SAN_LIST="node1.example.internal")
        self.assertIn(f"--node-ip={MESH_IP}", args)
        self.assertEqual(args[-4:], ["--tls-san", "node1.example.internal", "--tls-san", LAN_IP])
        self.assertIn("--vpn-auth=name=tailscale,joinKey=example", args)

    def test_a_member_on_the_lan_starts_etcd_there_with_the_mesh_still_carrying_the_node(self) -> None:
        args = self.server_args(K3S_ETCD_LAN_ADDRESS=LAN_IP, K3S_ETCD_PEER_ADDRESS=LAN_IP)
        self.assertIn(f"--node-ip={LAN_IP}", args)
        self.assertNotIn(f"--node-ip={MESH_IP}", args)
        self.assertEqual(args[-2:], ["--tls-san", LAN_IP])
        # The node, flannel and the API stay on the mesh through --vpn-auth, which overrides the kubelet's node IP and the advertise address.
        self.assertIn("--vpn-auth=name=tailscale,joinKey=example", args)

    def test_the_agent_command_line_takes_neither(self) -> None:
        agent_line = next(line for line in ENTRYPOINT.read_text().splitlines() if "k3s agent" in line)
        self.assertNotIn("ETCD", agent_line)
        self.assertNotIn("node-ip", agent_line)


class EnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)

    def render(self, *extra: str) -> tuple[list[str], list[str]]:
        env = self.work / "env"
        files = self.work / "files.json"
        args = [ANSIBLE_PLAYBOOK, str(ROOT / "tests" / "cluster_versions" / "render_env.yml"), "-e", f"env_output={env}", "-e", f"compose_files_output={files}", "-e", "github_runner_cluster_dir=/srv/github-runner", *extra]
        environment = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_RETRY_FILES_ENABLED": "false"}
        result = subprocess.run(args, capture_output=True, text=True, check=False, env=environment, cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        compose_files: list[str] = json.loads(files.read_text())
        return env.read_text().splitlines(), compose_files

    @unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
    def test_no_address_writes_nothing_new_and_adds_no_compose_file(self) -> None:
        lines, files = self.render()
        self.assertEqual([line for line in lines if "ETCD_LAN" in line or "ETCD_PEER" in line], [])
        self.assertEqual(files, ["docker-compose.yml"])
        self.assertEqual(self.render("-e", "github_runner_cluster_etcd_peers_over_lan=true"), (lines, files))

    @unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
    def test_the_address_is_written_and_its_override_merged_before_the_host_network(self) -> None:
        lines, files = self.render("-e", f"github_runner_cluster_etcd_lan_address={LAN_IP}", "-e", "github_runner_cluster_network_mode=host")
        self.assertIn(f"K3S_ETCD_LAN_ADDRESS={LAN_IP}", lines)
        self.assertEqual([line for line in lines if line.startswith("K3S_ETCD_PEER_ADDRESS")], [])
        self.assertEqual(files, ["docker-compose.yml", "docker-compose.etcd-lan.yml", "docker-compose.host-network.yml"])

    @unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
    def test_a_member_on_the_lan_gets_the_peer_address_too(self) -> None:
        lines, _ = self.render("-e", f"github_runner_cluster_etcd_lan_address={LAN_IP}", "-e", '{"github_runner_cluster_etcd_peer_on_lan": true}')
        self.assertIn(f"K3S_ETCD_LAN_ADDRESS={LAN_IP}", lines)
        self.assertIn(f"K3S_ETCD_PEER_ADDRESS={LAN_IP}", lines)


class ComposeOverrideTest(unittest.TestCase):
    def test_only_the_server_service_takes_the_two_variables_and_only_from_the_override(self) -> None:
        services = yaml.safe_load(OVERRIDE.read_text())["services"]
        self.assertEqual(list(services), ["k3s"])
        names = [entry.split("=", 1)[0] for entry in services["k3s"]["environment"]]
        self.assertEqual(names, ["K3S_ETCD_LAN_ADDRESS", "K3S_ETCD_PEER_ADDRESS"])
        self.assertNotIn("ETCD_LAN", (ROLE / "files" / "docker-compose.yml").read_text())
        self.assertNotIn("ETCD_PEER", (ROLE / "files" / "docker-compose.yml").read_text())

    def test_the_role_writes_the_override_and_counts_it_as_its_own(self) -> None:
        self.assertIn("docker-compose.etcd-lan.yml", DEFAULTS["github_runner_cluster_compose_files_written"])
        copied = next(task for task in yaml.safe_load((ROLE / "tasks" / "main.yml").read_text()) if task.get("name") == "Place the collection's Compose file and k3s build context in it")
        self.assertIn({"src": "docker-compose.etcd-lan.yml", "mode": "0644"}, copied["loop"])


class DefaultsTest(unittest.TestCase):
    def test_nothing_moves_by_default(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_etcd_lan_address"], "")
        self.assertIs(DEFAULTS["github_runner_cluster_etcd_peers_over_lan"], False)
        self.assertIs(DEFAULTS["github_runner_cluster_etcd_peer_on_lan"], False)
        self.assertEqual(DEFAULTS["github_runner_cluster_etcd_lan_switch"], "")

    def test_the_etcdctl_image_is_pinned_to_a_release(self) -> None:
        self.assertRegex(DEFAULTS["github_runner_cluster_etcdctl_image"], r"^gcr\.io/etcd-development/etcd:v[0-9]+\.[0-9]+\.[0-9]+$")


class TurnTest(unittest.TestCase):
    def test_the_move_takes_the_place_of_the_plain_start_in_the_moving_server_s_turn(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "start_server.yml").read_text())
        includes = [task.get("ansible.builtin.include_tasks") for task in tasks]
        self.assertEqual(includes, ["server_recreate_check.yml", None, None, "server_compose_up.yml", "server_ready.yml", "etcd_lan_move.yml"])
        for task in tasks[3:5]:
            self.assertIn("github_runner_cluster_etcd_lan_switch == ''", task["when"])
        self.assertEqual(tasks[5]["when"], "github_runner_cluster_etcd_lan_switch | length > 0")

    def test_the_move_updates_etcd_only_after_checking_and_moves_back_on_any_failure(self) -> None:
        (report, move) = yaml.safe_load((ROLE / "tasks" / "etcd_lan_move.yml").read_text())
        self.assertEqual(report["when"], "ansible_check_mode")
        names = [task["name"] for task in move["block"]]
        update = names.index("Update the member's peer URL in etcd")
        self.assertLess(names.index("Read the health of every etcd member before the move"), update)
        self.assertLess(names.index("Read every server's log for etcd reads that could not reach a quorum"), update)
        self.assertEqual(move["block"][update + 1]["ansible.builtin.set_fact"], {"github_runner_cluster_etcd_lan_updated": True})
        # .env is written back first whatever failed, since the run wrote it for the new address before the server's turn.
        self.assertEqual(move["rescue"][1]["ansible.builtin.template"]["src"], "env.j2")
        self.assertEqual(move["rescue"][1]["vars"], {"github_runner_cluster_etcd_peer_on_lan": "{{ github_runner_cluster_etcd_lan_switch == 'to_mesh' }}"})
        self.assertNotIn("when", move["rescue"][1])
        rescue_includes = [task.get("ansible.builtin.include_tasks") for task in move["rescue"][2]["block"]]
        self.assertEqual(rescue_includes, ["etcd_lan_update_back.yml", "server_compose_up.yml", "server_ready.yml"])
        self.assertEqual(move["rescue"][2]["when"], "github_runner_cluster_etcd_lan_updated | default(false) | bool")
        self.assertIn("ansible.builtin.fail", move["rescue"][-1])

    def test_the_decision_comes_before_anything_changes_on_the_host(self) -> None:
        names = [task.get("ansible.builtin.include_tasks") for task in yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())]
        self.assertEqual(names.index("etcd_lan.yml"), names.index("host_network.yml") + 1)


if __name__ == "__main__":
    unittest.main()
