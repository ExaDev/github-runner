"""Tests for the cluster role's settings for the role controller: the defaults, the checks in tasks/voters.yml (run through the role with tests/cluster_versions/checks.yml), and the Node labels that publish which hosts are eligible and pinned, in the Docker node's .env and Compose files, the entrypoint's --node-label flags, and a native node's k3s configuration."""

from __future__ import annotations

import json
import os
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
NODE_LABELS_COMPOSE = ROLE / "files" / "docker-compose.node-labels.yml"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())

CONTROLLER = "github_runner_cluster_role_controller"
VOTERS = "github_runner_cluster_voters"
ELIGIBLE = "github_runner_cluster_voter_eligible"
PINNED = "github_runner_cluster_voter_pinned"
ELIGIBLE_LABEL = "github-runner.exadev.com/voter-eligible"
PINNED_LABEL = "github-runner.exadev.com/voter-pinned"
LINK_LABEL = "github-runner.exadev.com/link"
MAX_SERVERS = "github_runner_cluster_max_servers"


def ansible_env() -> dict[str, str]:
    return {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_RETRY_FILES_ENABLED": "false"}


def hosts(count: int, **per_host: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """count hosts named node1 to nodeN, with the given variables on the named ones."""
    return {f"node{index}": dict(per_host.get(f"node{index}", {})) for index in range(1, count + 1)}


class DefaultsTest(unittest.TestCase):
    def test_nothing_is_on_eligible_or_pinned_by_default_and_the_target_is_three(self) -> None:
        self.assertIs(DEFAULTS[CONTROLLER], False)
        self.assertIs(DEFAULTS[ELIGIBLE], False)
        self.assertIs(DEFAULTS[PINNED], False)
        self.assertEqual(DEFAULTS[VOTERS], 3)

    def test_the_label_names_are_the_ones_the_controller_reads(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_voter_eligible_label"], ELIGIBLE_LABEL)
        self.assertEqual(DEFAULTS["github_runner_cluster_voter_pinned_label"], PINNED_LABEL)
        self.assertEqual(DEFAULTS["github_runner_cluster_link_label"], LINK_LABEL)

    def test_the_default_target_is_a_valid_one_for_the_default_maximum(self) -> None:
        self.assertEqual(DEFAULTS[VOTERS] % 2, 1)
        self.assertGreaterEqual(DEFAULTS[VOTERS], 3)
        self.assertLessEqual(DEFAULTS[VOTERS], DEFAULTS[MAX_SERVERS] - 1)


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class RoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)

    def check(self, group_hosts: dict[str, dict[str, Any]] | None = None, **variables: Any) -> subprocess.CompletedProcess[str]:
        """Run the topology and role controller checks on a group of hosts (five, so that targets up to 5 and a maximum of 6 can be tried), with ``variables`` as group variables."""
        inventory = self.work / "inventory.yml"
        group = {"vars": variables, "hosts": group_hosts or hosts(5)}
        inventory.write_text(yaml.safe_dump({"all": {"children": {"github_runner_cluster": group}}}))
        args = [ANSIBLE_PLAYBOOK, "-i", str(inventory), str(ROOT / "tests" / "cluster_versions" / "checks.yml"), "-e", "check_tasks=topology,voters"]
        return subprocess.run(args, capture_output=True, text=True, check=False, env=ansible_env(), cwd=self.work)

    def refused(self, message: str, **variables: Any) -> None:
        result = self.check(**variables)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stdout.replace("\n", " "))
        self.assertIn("Nothing was changed", result.stdout.replace("\n", " "))

    def test_nothing_is_checked_while_the_controller_is_off(self) -> None:
        for variables in ({}, {VOTERS: 4}, {VOTERS: 2}, {VOTERS: 5}, {ELIGIBLE: True}):
            with self.subTest(variables=variables):
                result = self.check(**variables)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_defaults_are_valid_with_the_controller_on_and_no_host_eligible(self) -> None:
        result = self.check(**{CONTROLLER: True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_even_target_is_refused(self) -> None:
        for voters in (4, 2, 6):
            with self.subTest(voters=voters):
                self.refused("must be odd", **{CONTROLLER: True, VOTERS: voters, MAX_SERVERS: 9})

    def test_a_target_under_three_is_refused(self) -> None:
        self.refused("must be at least 3", **{CONTROLLER: True, VOTERS: 1})

    def test_a_target_without_a_place_of_headroom_below_the_maximum_is_refused(self) -> None:
        self.refused("can be at most one less than the most servers allowed (4)", **{CONTROLLER: True, VOTERS: 5, MAX_SERVERS: 5})
        self.refused("can be at most one less than the most servers allowed (2)", **{CONTROLLER: True, VOTERS: 3, MAX_SERVERS: 3})

    def test_the_largest_target_that_leaves_headroom_is_accepted(self) -> None:
        for voters, maximum in ((3, 4), (5, 6), (3, 5)):
            with self.subTest(voters=voters, maximum=maximum):
                result = self.check(**{CONTROLLER: True, VOTERS: voters, MAX_SERVERS: maximum})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_target_that_is_not_a_whole_number_is_refused(self) -> None:
        for voters in ("three", "3.5", 0, -3, "03"):
            with self.subTest(voters=voters):
                self.refused("must be a whole number", **{CONTROLLER: True, VOTERS: voters})

    def test_fewer_eligible_hosts_than_the_target_is_refused_once_any_is_marked(self) -> None:
        group = hosts(5, node1={ELIGIBLE: True}, node2={ELIGIBLE: True})
        result = self.check(group, **{CONTROLLER: True})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("2 host(s) of github_runner_cluster set github_runner_cluster_voter_eligible (node1, node2), fewer than github_runner_cluster_voters (3)", result.stdout.replace("\n", " "))

    def test_as_many_eligible_hosts_as_the_target_or_more_pass(self) -> None:
        for count in (3, 4, 5):
            with self.subTest(eligible=count):
                group = hosts(5, **{f"node{index}": {ELIGIBLE: True} for index in range(1, count + 1)})
                result = self.check(group, **{CONTROLLER: True})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_pinned_host_does_not_count_as_eligible(self) -> None:
        group = hosts(5, **{f"node{index}": {PINNED: True} for index in range(1, 6)})
        self.assertEqual(self.check(group, **{CONTROLLER: True}).returncode, 0)

    def test_a_datastore_with_no_etcd_is_refused(self) -> None:
        self.refused("has no etcd votes to move", **{CONTROLLER: True, "github_runner_cluster_datastore": "sqlite"})

    def render_env(self, *extra: str) -> tuple[list[str], list[str], list[str]]:
        env, agent, files = self.work / "env", self.work / "env-agent", self.work / "files.json"
        args = [ANSIBLE_PLAYBOOK, str(ROOT / "tests" / "cluster_versions" / "render_env.yml"), "-e", f"env_output={env}", "-e", f"env_agent_output={agent}", "-e", f"compose_files_output={files}", "-e", "github_runner_cluster_dir=/srv/github-runner", *extra]
        result = subprocess.run(args, capture_output=True, text=True, check=False, env=ansible_env(), cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return env.read_text().splitlines(), agent.read_text().splitlines(), json.loads(files.read_text())

    def test_a_host_that_uses_none_of_it_keeps_its_configuration_exactly(self) -> None:
        server, agent, files = self.render_env()
        self.assertEqual([line for line in server + agent if "LABEL" in line], [])
        self.assertEqual(files, ["docker-compose.yml"], "no override file, so an upgrade recreates no container")

    def test_an_eligible_host_publishes_eligible_true_and_pinned_false_in_its_env_and_its_compose_files(self) -> None:
        server, agent, files = self.render_env("-e", f"{ELIGIBLE}=true")
        expected = f"K3S_NODE_LABELS={ELIGIBLE_LABEL}=true {PINNED_LABEL}=false {LINK_LABEL}=wired"
        self.assertIn(expected, server)
        self.assertIn(expected, agent)
        self.assertEqual(files, ["docker-compose.yml", "docker-compose.node-labels.yml", "docker-compose.fleet-role.yml"])

    def test_a_pinned_host_publishes_pinned_true(self) -> None:
        server, _, _ = self.render_env("-e", f"{PINNED}=true")
        self.assertIn(f"K3S_NODE_LABELS={ELIGIBLE_LABEL}=false {PINNED_LABEL}=true {LINK_LABEL}=wired", server)

    def test_with_the_controller_on_every_host_publishes_both_as_false_so_that_a_setting_turned_off_is_written_over(self) -> None:
        server, agent, files = self.render_env("-e", f"{CONTROLLER}=true")
        for lines in (server, agent):
            self.assertIn(f"K3S_NODE_LABELS={ELIGIBLE_LABEL}=false {PINNED_LABEL}=false {LINK_LABEL}=wired", lines)
        self.assertIn("docker-compose.node-labels.yml", files)
        self.assertIn("docker-compose.fleet-role.yml", files)

    def test_the_label_names_can_be_changed(self) -> None:
        server, _, _ = self.render_env("-e", f"{ELIGIBLE}=true", "-e", "github_runner_cluster_voter_eligible_label=example.com/candidate")
        self.assertIn(f"K3S_NODE_LABELS=example.com/candidate=true {PINNED_LABEL}=false {LINK_LABEL}=wired", server)

    def test_the_link_label_carries_the_medium_read_on_the_host(self) -> None:
        for medium in ("wifi", "unknown"):
            with self.subTest(medium):
                server, agent, _ = self.render_env("-e", f"{CONTROLLER}=true", "-e", f"github_runner_cluster_link={medium}", "-e", "github_runner_cluster_link_label=example.com/link")
                for lines in (server, agent):
                    self.assertIn(f"K3S_NODE_LABELS={ELIGIBLE_LABEL}=false {PINNED_LABEL}=false example.com/link={medium}", lines)


class ComposeOverrideTest(unittest.TestCase):
    def test_it_gives_both_services_the_labels_from_the_env_file(self) -> None:
        services = yaml.safe_load(NODE_LABELS_COMPOSE.read_text())["services"]
        self.assertEqual(sorted(services), ["k3s", "k3s-agent"])
        for name, service in services.items():
            with self.subTest(service=name):
                self.assertEqual(service["environment"], ["K3S_NODE_LABELS=${K3S_NODE_LABELS:-}"])

    def test_the_base_file_does_not_mention_it_so_a_host_without_labels_is_unchanged(self) -> None:
        self.assertNotIn("K3S_NODE_LABELS", (ROLE / "files" / "docker-compose.yml").read_text())

    def test_the_role_copies_it_to_the_node(self) -> None:
        self.assertIn("docker-compose.node-labels.yml", [item["src"] for item in next(task for task in yaml.safe_load((ROLE / "tasks" / "main.yml").read_text()) if "loop" in task and any("docker-compose.yml" == entry.get("src") for entry in task["loop"]))["loop"]])


def has_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode == 0


@unittest.skipUnless(has_compose(), "docker compose is not installed")
class ComposeMergeTest(unittest.TestCase):
    def environment(self, *files: Path, **env: str) -> dict[str, dict[str, str]]:
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("DOCKER_", "COMPOSE_", "K3S_", "TAILSCALE_"))}
        args = ["docker", "compose", "--project-directory", tempfile.gettempdir(), "--project-name", "example"]
        for file in files:
            args += ["-f", str(file)]
        args += ["--profile", "server", "--profile", "agent", "config", "--format", "json"]
        base = {"K3S_TOKEN": "t", "K3S_TLS_SAN_LIST": "n", "K3S_VERSION": "v1.34.1-k3s1", "TAILSCALE_VERSION": "v1.102.4"}
        result = subprocess.run(args, capture_output=True, text=True, check=False, env=clean | base | env)
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        return {name: services[name]["environment"] for name in ("k3s", "k3s-agent")}

    def test_merged_the_containers_get_the_labels(self) -> None:
        labels = f"{ELIGIBLE_LABEL}=true {PINNED_LABEL}=false"
        merged = self.environment(ROLE / "files" / "docker-compose.yml", NODE_LABELS_COMPOSE, K3S_NODE_LABELS=labels)
        for service in merged.values():
            self.assertEqual(service["K3S_NODE_LABELS"], labels)

    def test_unmerged_the_containers_have_no_such_variable(self) -> None:
        for service in self.environment(ROLE / "files" / "docker-compose.yml", K3S_NODE_LABELS="ignored").values():
            self.assertNotIn("K3S_NODE_LABELS", service)


def function_source(name: str) -> str:
    import re

    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", ENTRYPOINT.read_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, f"{name} is not defined in node-entrypoint.sh"
    return match.group(0)


class EntrypointTest(unittest.TestCase):
    """Runs the entrypoint's own function in a shell, without Tailscale or k3s."""

    def flags(self, **env: str) -> list[str]:
        script = "\n".join(["set -u", function_source("set_node_label_flags"), "set_node_label_flags", 'printf "%s\\n" "$node_label_flags"'])
        clean = {key: value for key, value in os.environ.items() if not key.startswith("K3S_")}
        result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=False, env=clean | env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_each_label_becomes_a_node_label_flag(self) -> None:
        self.assertEqual(self.flags(K3S_NODE_LABELS=f"{ELIGIBLE_LABEL}=true {PINNED_LABEL}=false"), ["--node-label", f"{ELIGIBLE_LABEL}=true", "--node-label", f"{PINNED_LABEL}=false"])

    def test_none_set_gives_no_flag(self) -> None:
        self.assertEqual(self.flags(), [])
        self.assertEqual(self.flags(K3S_NODE_LABELS=""), [])

    def test_the_flags_reach_every_k3s_command_line_the_entrypoint_builds(self) -> None:
        source = ENTRYPOINT.read_text()
        for command in ("/bin/k3s server", "exec /bin/k3s server", "exec /bin/k3s agent"):
            lines = [line for line in source.splitlines() if command in line and "$node_taint_flags" in line]
            self.assertTrue(lines, command)
            for line in lines:
                self.assertIn("$node_label_flags", line)


if __name__ == "__main__":
    unittest.main()
