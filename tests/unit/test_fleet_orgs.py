"""Tests for how the github_runner_arc role's validation assembles the fleet (tasks/validate.yml): this host's orgs and heartbeat gist id must count however they were supplied, including as a play variable, which other hosts' hostvars cannot show. Runs the real validation tasks in a play on localhost; nothing reaches a cluster or GitHub (a stand-in gh records any call)."""

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

REPO = Path(__file__).resolve().parents[2]

# The same arguments tasks/measure_sizing.yml passes, so the test sees the ceiling the role would set.
PLAYBOOK = textwrap.dedent(
    """\
    - name: Validate as a consumer's play would
      hosts: localhost
      connection: local
      gather_facts: false
      vars: {play_vars}
      tasks:
        - name: Validate
          ansible.builtin.include_role:
            name: exadev.github_runner.github_runner_arc
            tasks_from: validate.yml

        - name: Measure as the role would
          ansible.builtin.set_fact:
            measured: >-
              {{{{ github_runner_arc_local.profiles[0].settings.sizing
                   | exadev.github_runner.arc_measured_max_runners(nodes, pods, {{}},
                       github_runner_arc_fleet.profiles | map(attribute='namespace') | list) }}}}
          when: nodes is defined
    """
)

SIZING = {"measured": True, "pod_cpu_request": 1, "pod_memory_request_gib": 1}


def orgs(name: str = "example-org", namespace: str = "example-runners") -> list[dict[str, Any]]:
    """Return a github_runner_arc_orgs list with one measured profile in namespace."""
    return [{"name": name, "app_id": 1, "image": "ghcr.io/example/runner:1", "scale_set_profiles": [{"namespace": namespace, "values_file": str(REPO / "galaxy.yml"), "sizing": SIZING}]}]


def run(play_vars: dict[str, Any], inventory: str, host_vars: dict[str, dict[str, Any]] | None = None) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], str]:
    """Run the play with play_vars against inventory (INI text), returning the process, localhost's facts and the stand-in gh's log."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "inventory.ini").write_text(inventory)
        for host, variables in (host_vars or {}).items():
            (root / "host_vars").mkdir(exist_ok=True)
            (root / "host_vars" / f"{host}.yml").write_text(json.dumps(variables))
        (root / "play.yml").write_text(PLAYBOOK.format(play_vars=json.dumps(play_vars)))
        bin_dir = root / "bin"
        bin_dir.mkdir()
        gh_log = root / "gh.log"
        (bin_dir / "gh").write_text(f"#!/bin/sh\necho \"$@\" >> {gh_log}\nexit 1\n")
        (bin_dir / "gh").chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ANSIBLE_COLLECTIONS_PATH": str(REPO / "playbooks" / "collections"),
            "ANSIBLE_STDOUT_CALLBACK": "json",
            "ANSIBLE_LOCALHOST_WARNING": "false",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
            "ANSIBLE_HOME": directory,
            "ANSIBLE_LOCAL_TEMP": directory,
        }
        result = subprocess.run(["ansible-playbook", "-i", str(root / "inventory.ini"), str(root / "play.yml")], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        facts: dict[str, Any] = {}
        report = json.loads(result.stdout)
        for play in report["plays"]:
            for task in play["tasks"]:
                facts.update(task["hosts"].get("localhost", {}).get("ansible_facts", {}))
        called = gh_log.read_text() if gh_log.exists() else ""
        return result, facts, called


def namespaces(facts: dict[str, Any]) -> list[str]:
    """Return the namespaces of every profile in the assembled fleet."""
    return [profile["namespace"] for profile in facts["github_runner_arc_fleet"]["profiles"]]


# One node with 4 CPUs, 3 of them requested by two runner pods already running in the scale set's namespace and 1 by something else: 2 runners fit (4 less the other workload's 1, whole runners of 1 CPU, capped by memory at 2), whatever is running now.
NODES = [{"metadata": {"name": "node-a", "labels": {}}, "spec": {}, "status": {"allocatable": {"cpu": "4", "memory": "2Gi"}}}]
PODS = [
    {"metadata": {"name": f"runner-{index}", "namespace": "example-runners"}, "spec": {"nodeName": "node-a", "containers": [{"resources": {"requests": {"cpu": "1", "memory": "512Mi"}}}]}, "status": {"phase": "Running"}}
    for index in range(2)
] + [{"metadata": {"name": "other", "namespace": "default"}, "spec": {"nodeName": "node-a", "containers": [{"resources": {"requests": {"cpu": "1", "memory": "0"}}}]}, "status": {"phase": "Running"}}]

BASE = {"github_runner_arc_heartbeat_bootstrap_gist": False}
LOCALHOST_INVENTORY = "[local]\nlocalhost ansible_connection=local\n"


@unittest.skipIf(shutil.which("ansible-playbook") is None, "ansible-playbook is not installed")
class FleetOrgsTest(unittest.TestCase):
    def test_orgs_passed_as_a_play_variable_are_in_the_fleet(self) -> None:
        result, facts, _ = run(BASE | {"github_runner_arc_orgs": orgs()}, LOCALHOST_INVENTORY)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        self.assertEqual(namespaces(facts), ["example-runners"])

    def test_running_runner_pods_do_not_lower_a_measured_ceiling_set_from_a_play_variable(self) -> None:
        result, facts, _ = run(BASE | {"github_runner_arc_orgs": orgs(), "nodes": NODES, "pods": PODS}, LOCALHOST_INVENTORY)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        self.assertEqual(facts["measured"]["errors"], [])
        self.assertEqual(facts["measured"]["nodes"][0]["requested_cpu"], 1.0)
        self.assertEqual(facts["measured"]["max_runners"], 2)

    def test_an_implicit_localhost_counts_too(self) -> None:
        result, facts, _ = run(BASE | {"github_runner_arc_orgs": orgs()}, "[nothing]\n")
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        self.assertEqual(namespaces(facts), ["example-runners"])

    def test_other_hosts_orgs_still_come_from_their_hostvars(self) -> None:
        inventory = LOCALHOST_INVENTORY + "[other]\nother-host ansible_connection=local\n"
        result, facts, _ = run(BASE | {"github_runner_arc_orgs": orgs()}, inventory, {"other-host": {"github_runner_arc_orgs": orgs("other-org", "other-runners")}})
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        self.assertEqual(sorted(namespaces(facts)), ["example-runners", "other-runners"])

    def test_this_hosts_inventory_orgs_are_counted_once(self) -> None:
        result, facts, _ = run(BASE, LOCALHOST_INVENTORY, {"localhost": {"github_runner_arc_orgs": orgs()}})
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        self.assertEqual(namespaces(facts), ["example-runners"])

    def test_the_same_org_on_another_host_is_still_a_duplicate(self) -> None:
        inventory = LOCALHOST_INVENTORY + "[other]\nother-host ansible_connection=local\n"
        result, _, _ = run(BASE | {"github_runner_arc_orgs": orgs()}, inventory, {"other-host": {"github_runner_arc_orgs": orgs(namespace="other-runners")}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("configured more than once", result.stdout)

    def test_a_gist_id_passed_as_a_play_variable_needs_no_bootstrap(self) -> None:
        result, _, called = run({"github_runner_arc_heartbeat_bootstrap_gist": True, "github_runner_arc_heartbeat_gist_id": "0123abcd"}, LOCALHOST_INVENTORY)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        self.assertEqual(called, "", "gh was called to create a gist")


if __name__ == "__main__":
    unittest.main()
