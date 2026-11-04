"""Tests for the Compose files the cluster role names to Compose (tasks/compose_files_in_use.yml): under --check the role writes nothing, so a file it would add to a host that has run it before (docker-compose.node-labels.yml, for a host that turns the role controller on) is not there, and a read-only Compose command or the Compose module that names it fails, which broke `--check` before a deploy on every host that had run the role. The stand-ins fail as Compose does on a file that is not there (tests/unit/cluster_stand_ins.py), and the tests run the role's own task files in check mode and in a real run, on a server and on an agent."""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path
from typing import Any

import yaml
from cluster_stand_ins import HASH_NEW, HASH_OLD, ROOT, RoleTestCase

ROLE = ROOT / "roles" / "github_runner_cluster"
LABELS = "docker-compose.node-labels.yml"
API_PORT = "docker-compose.api-port.yml"
WITH_LABELS = ["docker-compose.yml", LABELS]


class FilesInUseTest(RoleTestCase):
    def on_host(self, node: str, *files: str) -> None:
        """Puts these Compose files in the node's project directory, as a host that has run the role before has."""
        (self.work / "nodes" / node).mkdir(parents=True, exist_ok=True)
        for name in files:
            (self.work / "nodes" / node / name).write_text("services: {}\n")

    def server(self, *extra: str, check: bool, tasks: str = "compose_files_in_use,start_servers", **hostvars: Any) -> subprocess.CompletedProcess[str]:
        hosts, group_vars = self.hosts(("node1", "node2", "node3"), **hostvars)
        return self.run_tasks(tasks, hosts, group_vars, *extra, check=check, limit="node1")

    def agent(self, *extra: str, check: bool, **hostvars: Any) -> subprocess.CompletedProcess[str]:
        hosts, group_vars = self.hosts(("node1", "node2", "node3"), ("agent1",), **hostvars)
        return self.run_tasks("compose_files_in_use,start_agent", hosts, group_vars, *extra, check=check, limit="agent1")

    def running_server(self) -> None:
        self.container("node1-k3s", config_hash=HASH_OLD)
        self.rendered("node1", HASH_NEW)
        self.nodes(node1="True", node2="True", node3="True")


class CheckModeTest(FilesInUseTest):
    def test_a_server_check_run_names_only_the_files_the_host_has_and_says_it_would_gain_the_rest(self) -> None:
        self.running_server()
        self.on_host("node1")
        result = self.server(check=True, github_runner_cluster_compose_file_list=WITH_LABELS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.compose_files("config-files.log"), ["node1: docker-compose.yml"])
        self.assertIn("Compose would recreate node1-k3s on node1: the running container's Compose configuration hash", result.stdout.replace("\n", " "))

    def test_the_reason_names_the_file_when_the_rest_of_the_configuration_is_unchanged(self) -> None:
        # The hash Compose renders from the files the host has equals the container's, but the run writes a file that adds to the configuration, so the container would be recreated all the same.
        self.container("node1-k3s", config_hash=HASH_NEW)
        self.rendered("node1", HASH_NEW)
        self.nodes(node1="True", node2="True", node3="True")
        result = self.server(check=True, github_runner_cluster_compose_file_list=WITH_LABELS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Compose would recreate node1-k3s on node1: the configuration gains {LABELS}, which a real run writes", result.stdout.replace("\n", " "))

    def test_nothing_is_reported_when_every_file_is_there_and_the_hashes_agree(self) -> None:
        self.container("node1-k3s", config_hash=HASH_NEW)
        self.rendered("node1", HASH_NEW)
        self.nodes(node1="True", node2="True", node3="True")
        self.on_host("node1", LABELS)
        result = self.server(check=True, github_runner_cluster_compose_file_list=WITH_LABELS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Compose would recreate node1-k3s", result.stdout)
        self.assertEqual(self.compose_files("config-files.log"), [f"node1: {','.join(WITH_LABELS)}"])

    def test_the_compose_module_is_given_the_files_the_host_has(self) -> None:
        self.running_server()
        self.on_host("node1", API_PORT)
        files = ["docker-compose.yml", API_PORT, LABELS]
        result = self.server(check=True, github_runner_cluster_compose_file_list=files)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.compose_files("compose-files.log"), [f"node1: docker-compose.yml,{API_PORT}"])

    def test_with_only_the_base_file_left_the_module_is_given_none_and_reads_the_default(self) -> None:
        self.running_server()
        result = self.server(check=True, github_runner_cluster_compose_file_list=WITH_LABELS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.compose_files("compose-files.log"), ["node1: docker-compose.yml"])

    def test_an_agent_check_run_names_only_the_files_the_host_has(self) -> None:
        self.on_host("agent1")
        result = self.agent(check=True, github_runner_cluster_compose_file_list=["docker-compose.yml", API_PORT, LABELS])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.compose_files("compose-files.log"), ["agent1: docker-compose.yml"])

    def test_an_agent_with_the_file_already_there_is_given_it(self) -> None:
        self.on_host("agent1", LABELS)
        result = self.agent(check=True, github_runner_cluster_compose_file_list=WITH_LABELS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.compose_files("compose-files.log"), [f"agent1: {','.join(WITH_LABELS)}"])

    def test_a_users_own_missing_compose_file_is_still_an_error_in_a_check_run(self) -> None:
        self.running_server()
        result = self.server(check=True, github_runner_cluster_compose_file_list=["docker-compose.yml", "compose.mine.yml"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compose.mine.yml", result.stdout)

    def test_a_host_whose_directory_does_not_exist_yet_still_skips_the_start_and_reports_it(self) -> None:
        self.running_server()
        hosts, group_vars = self.hosts(("node1", "node2", "node3"), github_runner_cluster_compose_file_list=WITH_LABELS)
        hosts["node1"]["github_runner_cluster_dir"] = str(self.work / "nodes" / "not-yet")
        result = self.run_tasks("compose_files_in_use,start_servers", hosts, group_vars, check=True, limit="node1")
        self.assertIn("Would create", result.stdout)


class RealRunTest(FilesInUseTest):
    def test_a_real_run_names_the_whole_list_once_the_role_has_written_it(self) -> None:
        self.running_server()
        self.on_host("node1", *WITH_LABELS)
        result = self.server(check=False, github_runner_cluster_compose_file_list=WITH_LABELS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.compose_files("config-files.log"), [f"node1: {','.join(WITH_LABELS)}"])
        self.assertEqual(self.compose_files("compose-files.log"), [f"node1: {','.join(WITH_LABELS)}"])

    def test_a_real_run_does_not_leave_a_file_out_that_is_missing_because_that_is_an_error(self) -> None:
        # The role writes its files before any Compose command, so one that is missing at a real run's Compose command is a failure to see, not one to hide.
        self.running_server()
        result = self.server(check=False, github_runner_cluster_compose_file_list=WITH_LABELS)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(LABELS, result.stdout)


class OrderTest(unittest.TestCase):
    """The first real run writes a file before the first Compose command that names it, and no Compose command names the list directly."""

    TASKS = ROLE / "tasks"

    def main_tasks(self) -> list[dict[str, Any]]:
        return yaml.safe_load((self.TASKS / "main.yml").read_text())

    def test_the_files_are_placed_then_the_list_is_worked_out_then_the_mesh_and_every_start_run(self) -> None:
        names = [task["name"] for task in self.main_tasks()]
        placed = names.index("Place the collection's Compose file and k3s build context in it")
        worked_out = names.index("Work out which Compose files a Compose command can name")
        self.assertEqual(worked_out, placed + 1)
        for later in ("Set up the mesh the nodes join, and publish its join key and control server", "Set up and start this host as a k3s server, bootstrapping or joining", "Join as a k3s agent"):
            matching = [index for index, name in enumerate(names) if name.startswith(later[:40])]
            self.assertTrue(matching, later)
            self.assertGreater(matching[0], worked_out, later)

    def test_no_task_names_the_whole_list_to_a_compose_command_except_where_it_is_defined_or_checked(self) -> None:
        allowed = {"defaults/main.yml", "tasks/compose_files.yml", "tasks/compose_files_in_use.yml", "templates/env.j2"}
        offenders = []
        for path in sorted(ROLE.rglob("*")):
            relative = str(path.relative_to(ROLE))
            if path.is_file() and relative not in allowed and path.suffix in {".yml", ".j2"} and not relative.startswith(("README", "meta/", "files/")):
                if re.search(r"compose_file_list", path.read_text()):
                    offenders.append(relative)
        self.assertEqual(offenders, [], "these use the whole list where a Compose command needs github_runner_cluster_compose_files_in_use")

    def test_the_defaults_give_the_whole_list_when_nothing_has_been_worked_out(self) -> None:
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        self.assertEqual(defaults["github_runner_cluster_compose_files_unwritten"], [])
        self.assertIn("github_runner_cluster_compose_file_list", defaults["github_runner_cluster_compose_files_in_use"])

    def test_the_files_the_role_writes_are_the_ones_it_copies_and_the_mesh_provider_writes(self) -> None:
        copied = [item["src"] for task in self.main_tasks() if task["name"].startswith("Place the collection's Compose file") for item in task["loop"] if item["src"].startswith("docker-compose.")]
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        self.assertCountEqual(copied + ["docker-compose.mesh.yml"], defaults["github_runner_cluster_compose_files_written"])


if __name__ == "__main__":
    unittest.main()
