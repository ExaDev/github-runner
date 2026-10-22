"""Tests for how the cluster role recreates servers one at a time: the filters (plugins/filter/cluster_recreate.py), the check before a server's container is recreated (tasks/server_recreate_check.yml), the wait after it (tasks/server_ready.yml) and the servers' turns (tasks/start_servers.yml), run through the role (tests/node_password/run.yml) against a stand-in for docker and a stand-in for the Compose module, both first on the playbook's PATH and collection path so that no real Docker daemon, kubectl or launchd is ever reached."""

from __future__ import annotations

import importlib.util
import itertools
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cluster_stand_ins import (
    HASH_NEW,
    HASH_OLD,
    ROOT,
    RoleTestCase,
    flatten,
    has_compose,
    recap,
    render_compose,
    role_tasks,
)

PLUGIN = ROOT / "plugins" / "filter" / "cluster_recreate.py"
_spec = importlib.util.spec_from_file_location("cluster_recreate_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
recreate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recreate)


class RenderedHashTest(unittest.TestCase):
    OUTPUT = f"docker-prune {'1' * 64}\nk3s {HASH_NEW}\n"

    def test_the_hash_of_the_named_service_is_picked(self) -> None:
        self.assertEqual(recreate.cluster_rendered_config_hash(self.OUTPUT, "k3s"), HASH_NEW)
        self.assertEqual(recreate.cluster_rendered_config_hash(self.OUTPUT, "docker-prune"), "1" * 64)

    def test_a_service_that_is_not_in_the_output_is_an_error_not_a_match(self) -> None:
        for service in ("k3s-agent", "k3", "3s"):
            with self.subTest(service=service), self.assertRaises(recreate.AnsibleFilterError):
                recreate.cluster_rendered_config_hash(self.OUTPUT, service)

    def test_an_empty_output_is_an_error(self) -> None:
        with self.assertRaises(recreate.AnsibleFilterError):
            recreate.cluster_rendered_config_hash("", "k3s")

    def test_unusable_input_is_an_error(self) -> None:
        for output, service in ((None, "k3s"), (self.OUTPUT, ""), (self.OUTPUT, None)):
            with self.subTest(output=output, service=service), self.assertRaises(recreate.AnsibleFilterError):
                recreate.cluster_rendered_config_hash(output, service)


class RecreateReasonTest(unittest.TestCase):
    def test_equal_hashes_give_no_reason(self) -> None:
        self.assertEqual(recreate.cluster_recreate_reason(HASH_OLD, HASH_OLD), "")
        self.assertEqual(recreate.cluster_recreate_reason(f" {HASH_OLD}\n", HASH_OLD), "")

    def test_different_hashes_name_both_without_the_whole_hash(self) -> None:
        reason = recreate.cluster_recreate_reason(HASH_OLD, HASH_NEW)
        self.assertIn("a" * recreate.SHOWN_HASH_LENGTH, reason)
        self.assertIn("b" * recreate.SHOWN_HASH_LENGTH, reason)
        self.assertNotIn(HASH_OLD, reason)
        self.assertNotIn(HASH_NEW, reason)

    def test_a_container_with_no_hash_counts_as_different(self) -> None:
        self.assertIn("carries no Compose configuration hash", recreate.cluster_recreate_reason("", HASH_NEW))

    def test_a_rendered_hash_that_was_not_read_is_an_error(self) -> None:
        for rendered in ("", "  ", None):
            with self.subTest(rendered=rendered), self.assertRaises(recreate.AnsibleFilterError):
                recreate.cluster_recreate_reason(HASH_OLD, rendered)


def read_of(stdout: str, rc: int = 0, **extra: Any) -> dict[str, Any]:
    return {"rc": rc, "stdout": stdout, "stderr": "", **extra}


class RecreateBlockersTest(unittest.TestCase):
    def test_every_other_server_ready_blocks_nothing(self) -> None:
        self.assertEqual(recreate.cluster_recreate_blockers(["b", "c"], read_of("a True\nb True\nc True\n")), [])

    def test_no_other_server_blocks_nothing_and_needs_no_read(self) -> None:
        self.assertEqual(recreate.cluster_recreate_blockers([], {"skipped": True}), [])

    def test_a_server_that_is_not_ready_is_named_with_its_condition(self) -> None:
        self.assertEqual(
            recreate.cluster_recreate_blockers(["b", "c"], read_of("a True\nb False\nc Unknown\n")),
            ["server b is not Ready (its Ready condition is False)", "server c is not Ready (its Ready condition is Unknown)"],
        )

    def test_a_server_with_no_condition_yet_is_not_ready(self) -> None:
        self.assertEqual(recreate.cluster_recreate_blockers(["b"], read_of("b \n")), ["server b is not Ready (its Ready condition is unknown)"])

    def test_a_server_missing_from_the_list_is_not_registered(self) -> None:
        self.assertEqual(recreate.cluster_recreate_blockers(["b", "c"], read_of("a True\nb True\n")), ["server c is not registered"])

    def test_agents_and_this_node_in_the_list_do_not_matter(self) -> None:
        self.assertEqual(recreate.cluster_recreate_blockers(["b"], read_of("a False\nb True\nagent1 False\n")), [])

    def test_a_node_name_that_only_contains_a_server_s_is_not_that_server(self) -> None:
        self.assertEqual(recreate.cluster_recreate_blockers(["node1", "node2"], read_of("node10 True\nnode2 True\n")), ["server node1 is not registered"])

    def test_a_cluster_still_being_brought_up_has_no_quorum_to_keep(self) -> None:
        # The bootstrap server restarted before any other server has joined: only itself is listed.
        self.assertEqual(recreate.cluster_recreate_blockers(["b", "c"], read_of("a False\n")), [])
        self.assertEqual(recreate.cluster_recreate_blockers(["b", "c"], read_of("")), [])

    def test_once_any_other_server_is_registered_a_missing_one_blocks(self) -> None:
        self.assertEqual(recreate.cluster_recreate_blockers(["b", "c"], read_of("a True\nc True\n")), ["server b is not registered"])

    def test_a_failed_read_blocks_with_docker_s_message(self) -> None:
        found = recreate.cluster_recreate_blockers(["b"], {"rc": 1, "stdout": "", "stderr": "warning\nThe connection to the server 127.0.0.1:6443 was refused\n"})
        self.assertEqual(found, ["the nodes could not be listed through this node's own container (The connection to the server 127.0.0.1:6443 was refused)"])

    def test_a_failed_read_without_a_message_names_the_exit_status(self) -> None:
        self.assertEqual(recreate.cluster_recreate_blockers(["b"], {"rc": 7, "stdout": "", "stderr": ""}), ["the nodes could not be listed through this node's own container (exit status 7)"])

    def test_a_timed_out_read_blocks_on_every_ansible_core(self) -> None:
        for result in (
            {"failed": True, "timedout": {"period": 2}, "msg": "Task failed: Timed out after 2 second(s)."},
            {"failed": True, "msg": "The ansible.builtin.command action failed to execute in the expected time frame (2) and was terminated"},
        ):
            with self.subTest(result=result):
                self.assertIn("within github_runner_cluster_cluster_read_timeout", recreate.cluster_recreate_blockers(["b"], result)[0])

    def test_unusable_input_is_an_error(self) -> None:
        for peers, result in (("b", read_of("")), ([1], read_of("")), (["b"], "text")):
            with self.subTest(peers=peers), self.assertRaises(recreate.AnsibleFilterError):
                recreate.cluster_recreate_blockers(peers, result)

    def test_registered_peers_are_the_ones_the_cluster_lists(self) -> None:
        self.assertEqual(recreate.cluster_peers_registered(["b", "c", "d"], read_of("a True\nd False\nb True\n")), ["b", "d"])
        self.assertEqual(recreate.cluster_peers_registered(["b"], read_of("a True\n")), [])
        self.assertEqual(recreate.cluster_peers_registered(["b"], {"rc": 1, "stdout": "b True\n", "stderr": "refused"}), [])

    def test_the_filters_are_registered(self) -> None:
        filters = recreate.FilterModule().filters()
        self.assertIs(filters["cluster_peers_registered"], recreate.cluster_peers_registered)
        self.assertIs(filters["cluster_rendered_config_hash"], recreate.cluster_rendered_config_hash)
        self.assertIs(filters["cluster_recreate_reason"], recreate.cluster_recreate_reason)
        self.assertIs(filters["cluster_recreate_blockers"], recreate.cluster_recreate_blockers)


class ServerRecreateCheckTest(RoleTestCase):
    def check(self, *extra: str, check: bool = False, run_on: str = "node1", servers: tuple[str, ...] = ("node1", "node2", "node3"), **hostvars: Any) -> subprocess.CompletedProcess[str]:
        hosts, group_vars = self.hosts(servers, **hostvars)
        return self.run_tasks("server_recreate_check", hosts, group_vars, *extra, check=check, limit=run_on)

    def drifted(self, **nodes: str) -> None:
        """node1's container runs one configuration, Compose now renders another, and the cluster's nodes are as given."""
        self.container("node1-k3s", config_hash=HASH_OLD)
        self.rendered("node1", HASH_NEW)
        self.nodes(**({"node1": "True", "node2": "True", "node3": "True"} | nodes))

    def test_no_container_means_a_first_start_with_nothing_to_compare_or_guard(self) -> None:
        self.rendered("node1", HASH_NEW)
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(recap(result.stdout, "node1")["changed"], 0)
        self.assertEqual([call for call in self.calls() if " compose " in call or " exec " in call], [])

    def test_a_container_whose_hash_matches_is_kept_and_nothing_else_is_read(self) -> None:
        self.container("node1-k3s", config_hash=HASH_NEW)
        self.rendered("node1", HASH_NEW)
        result = self.check(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(recap(result.stdout, "node1")["changed"], 0)
        self.assertNotIn("Compose would recreate node1-k3s", result.stdout)
        self.assertEqual([call for call in self.calls() if " exec " in call], [])

    def test_the_rendered_hash_is_read_from_the_hosts_files_the_way_the_role_starts_the_project(self) -> None:
        self.drifted()
        self.assertEqual(self.check().returncode, 0)
        directory = self.work / "nodes" / "node1"
        self.assertIn(
            f"- compose --project-name node1 --project-directory {directory} --file={directory}/docker-compose.yml --profile server config --hash *",
            self.calls(),
        )

    def test_a_different_hash_with_every_other_server_ready_goes_ahead(self) -> None:
        self.drifted()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(recap(result.stdout, "node1")["changed"], 0)
        self.assertIn("- exec node1-k3s kubectl get nodes --request-timeout=30s -o jsonpath=", " ".join(self.calls()))

    def test_check_mode_reports_the_recreate_as_a_change_with_the_reason(self) -> None:
        self.drifted()
        result = self.check(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(recap(result.stdout, "node1")["changed"], 1)
        self.assertIn("Compose would recreate node1-k3s on node1", result.stdout)
        self.assertIn(f"the running container's Compose configuration hash {'a' * 12} differs from the rendered configuration's {'b' * 12}", result.stdout)
        self.assertIn("Every other server reports Ready, so the recreate may go ahead.", result.stdout)

    def test_a_server_that_is_not_ready_stops_the_recreate_and_is_named(self) -> None:
        self.drifted(node3="False")
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Not recreating node1-k3s on node1", result.stdout)
        self.assertIn("server node3 is not Ready (its Ready condition is False)", result.stdout)
        self.assertIn("Nothing was changed.", result.stdout)

    def test_check_mode_refuses_in_the_same_case(self) -> None:
        self.drifted(node2="False", node3="Unknown")
        result = self.check(check=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("server node2 is not Ready (its Ready condition is False); server node3 is not Ready (its Ready condition is Unknown)", result.stdout)

    def test_a_server_that_has_not_registered_stops_the_recreate(self) -> None:
        self.drifted()
        self.nodes(node1="True", node2="True")
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("server node3 is not registered", result.stdout)

    def test_an_agent_that_is_not_ready_does_not_stop_it(self) -> None:
        self.drifted(agent1="False")
        self.assertEqual(self.check().returncode, 0)

    def test_a_cluster_that_cannot_be_read_stops_the_recreate_with_the_reason(self) -> None:
        self.drifted()
        self.write("containers", "node1-k3s.kubectl-fails", text="")
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the nodes could not be listed through this node's own container (The connection to the server 127.0.0.1:6443 was refused)", result.stdout)

    def test_a_hash_that_is_missing_from_the_container_counts_as_different(self) -> None:
        self.container("node1-k3s", config_hash=None)
        self.rendered("node1", HASH_NEW)
        self.nodes(node1="True", node2="True", node3="False")
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the running container carries no Compose configuration hash", result.stdout)

    def test_a_stopped_container_is_recreated_without_asking_the_others(self) -> None:
        # It is not a member of the quorum while it is stopped, so replacing it takes nothing away.
        self.container("node1-k3s", state="exited", config_hash=HASH_OLD)
        self.rendered("node1", HASH_NEW)
        self.nodes(node2="False", node3="False")
        result = self.check(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("The container is not running, so the recreate takes no member away from the cluster.", result.stdout)
        self.assertEqual([call for call in self.calls() if " exec " in call], [])

    def test_a_cluster_of_one_server_has_nobody_to_wait_for(self) -> None:
        self.drifted()
        result = self.check(servers=("node1",))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls() if " exec " in call], [])

    def test_the_rendered_configuration_having_no_line_for_the_service_fails_rather_than_passing(self) -> None:
        self.container("node1-k3s", config_hash=HASH_OLD)
        self.write("rendered", "node1.txt", text=f"docker-prune {'1' * 64}\n")
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("printed no configuration hash for the service 'k3s'", result.stdout)


class ServerReadyTest(RoleTestCase):
    def wait(self, **extra: Any) -> subprocess.CompletedProcess[str]:
        hosts, group_vars = self.hosts(("node1",), **extra)
        return self.run_tasks("server_ready", hosts, group_vars)

    def lease_reads(self) -> int:
        return len([call for call in self.calls() if " kubectl get lease " in call])

    def test_a_node_waits_for_a_heartbeat_from_the_new_container(self) -> None:
        # The old container's Ready condition is still in the API: it counts for nothing until the lease shows the new kubelet.
        self.container("node1-k3s")
        self.nodes(node1="True")
        result = self.wait()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.lease_reads(), 2)
        self.assertIn("- exec node1-k3s kubectl get lease --namespace kube-node-lease node1 -o jsonpath={.spec.renewTime}", self.calls())
        self.assertIn("- inspect --format {{.State.StartedAt}} node1-k3s", self.calls())
        self.assertEqual(recap(result.stdout, "node1")["changed"], 0)

    def test_a_node_that_becomes_ready_ends_the_wait(self) -> None:
        self.container("node1-k3s")
        self.nodes(node1="False")
        result = self.wait()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.stub / "cluster.nodes").read_text().split(), ["node1", "True"])

    def test_a_node_that_never_becomes_ready_fails_the_run_after_the_timeout_derived_attempts(self) -> None:
        self.container("node1-k3s")
        self.nodes(node1="True")
        self.write("never-ready-node1", text="")
        result = self.wait(github_runner_cluster_server_ready_timeout=20)
        self.assertNotEqual(result.returncode, 0)
        # 20 seconds in attempts of 5 seconds' wait and 5 seconds' delay is two retries after the first attempt.
        self.assertEqual(self.lease_reads(), 3)

    def test_a_container_whose_api_is_not_listening_yet_is_retried(self) -> None:
        # docker exec fails at once while the container is not running; the wait carries on until the timeout, then fails.
        self.container("node1-k3s", state="restarting")
        result = self.wait(github_runner_cluster_server_ready_timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not running", result.stdout)


class StartServersTest(RoleTestCase):
    def start(self, *extra: str, check: bool = False, limit: str | None = None, servers: tuple[str, ...] = ("node1", "node2", "node3"), agents: tuple[str, ...] = (), **hostvars: Any) -> subprocess.CompletedProcess[str]:
        hosts, group_vars = self.hosts(servers, agents, **hostvars)
        return self.run_tasks("start_servers", hosts, group_vars, *extra, check=check, limit=limit)

    def running_cluster(self, hashes: dict[str, str]) -> None:
        for name, config_hash in hashes.items():
            self.container(f"{name}-k3s", config_hash=config_hash)
            self.rendered(name, HASH_NEW)
        self.nodes(**{name: "True" for name in hashes})

    def registered_nodes(self) -> dict[str, str]:
        """The nodes the stub holds as registered, each with its Ready condition, whatever order they registered in."""
        words = (self.stub / "cluster.nodes").read_text().split()
        return dict(zip(words[::2], words[1::2], strict=True))

    def turns_do_not_overlap(self) -> None:
        """Every server's calls in the log lie wholly before or wholly after every other's, whichever went first."""
        spans = sorted((min(self.events(f"node{n}")), max(self.events(f"node{n}"))) for n in (1, 2, 3))
        for (_, ended), (began, _) in itertools.pairwise(spans):
            self.assertLess(ended, began)

    def events(self, node: str) -> list[int]:
        """The positions in the calls log of everything that concerns one node's turn."""
        return [index for index, call in enumerate(self.calls()) if f"--project-name {node} " in call or ("-k3s kubectl" in call and f"{node}-k3s" in call) or call.startswith(f"module up {node} ")]

    def test_every_server_whose_configuration_changed_is_recreated(self) -> None:
        self.running_cluster({"node1": HASH_OLD, "node2": HASH_OLD, "node3": HASH_OLD})
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ups = [call for call in self.calls() if call.startswith("module up")]
        # Which server goes first is the order the hosts' results arrive in, so only the set is fixed.
        self.assertCountEqual(ups, [f"module up node{n} profiles=server check=False" for n in (1, 2, 3)])
        self.assertEqual(self.registered_nodes(), {"node1": "True", "node2": "True", "node3": "True"})

    def test_a_server_is_ready_again_before_the_next_servers_turn_begins(self) -> None:
        # Each recreate takes its node out of Ready until the role has waited for it, so a turn that began before the wait would find that node not Ready and refuse: the run passing shows the turns do not overlap, and the log shows the order.
        self.running_cluster({"node1": HASH_OLD, "node2": HASH_OLD, "node3": HASH_OLD})
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.turns_do_not_overlap()
        for n in (1, 2, 3):
            self.assertIn(f"- exec node{n}-k3s kubectl get lease --namespace kube-node-lease node{n} -o jsonpath={{.spec.renewTime}}", self.calls())

    def test_a_node_the_api_still_reports_ready_does_not_end_the_wait_before_its_heartbeat(self) -> None:
        # The API keeps the old container's Ready condition for a while, so a turn that trusted it would end at once and the next server would be recreated beside a server still coming back. Here the condition never leaves Ready, and only the lease tells.
        self.write("stale-ready", text="")
        self.running_cluster({"node1": HASH_OLD, "node2": HASH_OLD, "node3": HASH_OLD})
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for n in (1, 2, 3):
            reads = [call for call in self.calls() if f"kubectl get lease --namespace kube-node-lease node{n} " in call]
            self.assertEqual(len(reads), 2, "the first read still showed the old container's heartbeat")
        self.turns_do_not_overlap()

    def test_a_bootstrap_server_restarted_before_anything_joined_it_is_recreated_without_a_wait(self) -> None:
        # The first run of a cluster that runs its own mesh starts the bootstrap server early, with settings that change before the other servers exist: only itself is registered, so there is no quorum to keep and nothing to wait for.
        self.running_cluster({"node1": HASH_OLD})
        self.rendered("node2", HASH_NEW)
        self.rendered("node3", HASH_NEW)
        self.nodes(node1="True")
        # Only the bootstrap server takes a turn: the others' turns come in no fixed order, and one that came first would register itself before node1's turn and leave node1 a peer to wait for.
        result = self.start(limit="node1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls() if call.startswith("module up node1")], ["module up node1 profiles=server check=False"])
        self.assertEqual([call for call in self.calls() if " kubectl get lease " in call], [])

    def test_a_server_whose_configuration_is_unchanged_is_started_without_a_wait(self) -> None:
        self.running_cluster({"node1": HASH_NEW, "node2": HASH_NEW, "node3": HASH_NEW})
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls() if " kubectl " in call], [])
        self.assertEqual(len([call for call in self.calls() if call.startswith("module up")]), 3)

    def test_a_first_start_waits_for_nothing(self) -> None:
        for n in (1, 2, 3):
            self.rendered(f"node{n}", HASH_NEW)
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls() if " kubectl " in call], [])
        self.assertEqual(len([call for call in self.calls() if call.startswith("module up")]), 3)

    def test_servers_are_not_recreated_while_another_is_down(self) -> None:
        self.running_cluster({"node1": HASH_OLD, "node2": HASH_OLD, "node3": HASH_NEW})
        self.nodes(node1="True", node2="True", node3="False")
        result = self.start()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call for call in self.calls() if call.startswith("module up") and "node3" not in call], [])
        self.assertIn("fatal: [node1]", result.stdout)
        self.assertIn("fatal: [node2]", result.stdout)
        self.assertNotIn("fatal: [node3]", result.stdout)
        self.assertIn("server node3 is not Ready", result.stdout)

    def test_a_server_that_does_not_come_back_stops_the_run_before_the_next_is_touched(self) -> None:
        # No server comes back, so whichever goes first fails its wait and no other server is touched.
        self.running_cluster({"node1": HASH_OLD, "node2": HASH_OLD, "node3": HASH_OLD})
        for n in (1, 2, 3):
            self.write(f"never-ready-node{n}", text="")
        result = self.start(github_runner_cluster_server_ready_timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len([call for call in self.calls() if call.startswith("module up")]), 1)
        self.assertEqual(len(re.findall(r"fatal: \[node\d\]", result.stdout)), 1)

    def test_a_server_whose_old_ready_condition_lingers_but_never_returns_still_stops_the_run(self) -> None:
        # The API still says Ready for the server that failed to come back, so no other server's check would refuse: only ending the play for every host keeps the rest untouched.
        self.write("stale-ready", text="")
        self.running_cluster({"node1": HASH_OLD, "node2": HASH_OLD, "node3": HASH_OLD})
        for n in (1, 2, 3):
            self.write(f"never-ready-node{n}", text="")
        result = self.start(github_runner_cluster_server_ready_timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len([call for call in self.calls() if call.startswith("module up")]), 1)

    def test_a_limited_run_gives_a_turn_to_the_hosts_in_the_play_only(self) -> None:
        self.running_cluster({"node1": HASH_OLD, "node2": HASH_OLD, "node3": HASH_OLD})
        result = self.start(limit="node2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls() if call.startswith("module up")], ["module up node2 profiles=server check=False"])

    def test_check_mode_reports_every_recreate_and_changes_nothing(self) -> None:
        self.running_cluster({"node1": HASH_OLD, "node2": HASH_OLD, "node3": HASH_OLD})
        result = self.start(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for n in (1, 2, 3):
            self.assertIn(f"Compose would recreate node{n}-k3s on node{n}", result.stdout)
            self.assertEqual(recap(result.stdout, f"node{n}")["changed"], 1)
        self.assertEqual([call for call in self.calls() if call.startswith("module up") and "check=False" in call], [])
        self.assertEqual([call for call in self.calls() if " kubectl get lease " in call], [])
        self.assertEqual((self.stub / "cluster.nodes").read_text().split(), ["node1", "True", "node2", "True", "node3", "True"])

    def test_check_mode_on_a_first_run_reports_the_start_it_cannot_ask_compose_about(self) -> None:
        # A first run's dry run creates no project directory, and Compose cannot read a project directory that does not exist.
        hosts, group_vars = self.hosts(("node1", "node2", "node3"))
        for name in hosts:
            (self.work / "nodes" / name).rmdir()
        result = self.run_tasks("start_servers", hosts, group_vars, check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for n in (1, 2, 3):
            self.assertIn(f"Would create {self.work / 'nodes' / f'node{n}'} and start the k3s server there, as node{n}-k3s", result.stdout.replace("\n", " "))
        self.assertEqual([call for call in self.calls() if call.startswith("module up")], [])
        self.assertFalse((self.work / "nodes").joinpath("node1").exists())

    def test_check_mode_asks_compose_once_the_project_directory_exists(self) -> None:
        for n in (1, 2, 3):
            self.rendered(f"node{n}", HASH_NEW)
        result = self.start(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertCountEqual([call for call in self.calls() if call.startswith("module up")], [f"module up node{n} profiles=server check=True" for n in (1, 2, 3)])
        self.assertNotIn("Would create", result.stdout)

    def test_check_mode_refuses_what_a_run_would_refuse(self) -> None:
        self.running_cluster({"node1": HASH_OLD, "node2": HASH_NEW, "node3": HASH_NEW})
        self.nodes(node1="True", node2="False", node3="True")
        result = self.start(check=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fatal: [node1]", result.stdout)
        self.assertIn("server node2 is not Ready", result.stdout)


@unittest.skipUnless(has_compose(), "docker compose is not installed")
class ComposeHashTest(unittest.TestCase):
    def test_the_real_output_of_the_hash_command_is_what_the_filter_reads(self) -> None:
        # The format the role parses, as the installed Compose prints it, one line per service of the active profiles.
        result = render_compose("config", "--hash", "*")
        self.assertEqual(result.returncode, 0, result.stderr)
        for service in ("k3s", "k3s-agent"):
            with self.subTest(service=service):
                self.assertRegex(recreate.cluster_rendered_config_hash(result.stdout, service), r"^[0-9a-f]{64}$")

    def test_a_volume_added_to_a_service_changes_only_that_service_s_hash(self) -> None:
        # Why a node that gains the password volume is recreated once, and why the role seeds the volume before that run.
        with tempfile.TemporaryDirectory() as work:
            override = Path(work) / "override.yml"
            override.write_text("services:\n  k3s:\n    volumes:\n      - extra:/extra\nvolumes:\n  extra: {}\n")
            before = render_compose("config", "--hash", "*")
            after = render_compose("-f", str(override), "config", "--hash", "*")
        self.assertEqual((before.returncode, after.returncode), (0, 0), before.stderr + after.stderr)
        hash_of = recreate.cluster_rendered_config_hash
        self.assertNotEqual(hash_of(before.stdout, "k3s"), hash_of(after.stdout, "k3s"))
        self.assertEqual(hash_of(before.stdout, "k3s-agent"), hash_of(after.stdout, "k3s-agent"))


class TaskStructureTest(unittest.TestCase):
    def test_servers_start_through_their_turns_and_agents_do_not(self) -> None:
        tasks = flatten(role_tasks("main.yml"))
        starts = [task for task in tasks if "community.docker.docker_compose_v2" in task]
        self.assertEqual([task["community.docker.docker_compose_v2"]["profiles"] for task in starts], [["agent"]])
        self.assertIn("start_servers.yml", [task.get("ansible.builtin.include_tasks") for task in tasks])
        self.assertNotIn("server_recreate_check.yml", [task.get("ansible.builtin.include_tasks") for task in tasks], "agents are exempt from the quorum guard")

    def test_a_turn_checks_before_it_starts_and_waits_only_afterwards(self) -> None:
        tasks = role_tasks("start_server.yml")
        order = [task.get("ansible.builtin.include_tasks") or next(key for key in task if key.startswith(("community.docker", "ansible.builtin."))) for task in tasks]
        self.assertEqual(order, ["server_recreate_check.yml", "ansible.builtin.stat", "ansible.builtin.debug", "community.docker.docker_compose_v2", "server_ready.yml"])
        self.assertEqual(tasks[3]["community.docker.docker_compose_v2"]["profiles"], ["server"])
        self.assertIn("not ansible_check_mode", tasks[4]["when"])

    def test_the_turns_loop_over_the_servers_in_the_play_and_run_for_one_host_each(self) -> None:
        (task,) = role_tasks("start_servers.yml")
        self.assertEqual(task["ansible.builtin.include_tasks"], "start_server.yml")
        self.assertIn("topology.servers", task["loop"])
        self.assertIn("ansible_play_hosts_all", task["loop"])
        self.assertEqual(task["when"], f"{task['loop_control']['loop_var']} == inventory_hostname")


if __name__ == "__main__":
    unittest.main()
