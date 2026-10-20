"""Tests for how the github_runner_arc role installs the node recovery watcher's opt-in prune pass: the manifests templates/node-recovery.yaml.j2 renders (through tests/node_recovery/render.yml, with ansible-playbook) and the settings checks of tasks/validate.yml."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_arc"
RENDER = ROOT / "tests" / "node_recovery" / "render.yml"
# Renders one template file with the role's defaults and constants, the way the role evaluates an expression taken from a task.
EXPRESSION = ROOT / "tests" / "node_cache_pruner" / "kept.yml"
AFTER_SECONDS = 120
PRUNE_AFTER_SECONDS = 600
PRUNE_LABEL = "github-runner.exadev.io/removable"
AFTER = "github_runner_arc_node_recovery_prune_after_seconds"
LABEL = "github_runner_arc_node_recovery_prune_label"
ON = {AFTER: PRUNE_AFTER_SECONDS}


def ansible(playbook: Path, **variables: Any) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as work:
        extra = Path(work) / "extra.json"
        extra.write_text(json.dumps(variables))
        env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_HOME": str(Path(work) / "home")}
        return subprocess.run([os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(playbook), "-e", f"@{extra}"], env=env, cwd=work, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)


def render(**role_vars: Any) -> dict[str, dict[str, Any]]:
    """Render the watcher's manifests and return them by kind."""
    with tempfile.TemporaryDirectory() as work:
        output = Path(work) / "recovery.yaml"
        completed = ansible(RENDER, recovery_output=str(output), **role_vars)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        documents = [document for document in yaml.safe_load_all(output.read_text()) if document]
    by_kind = {document["kind"]: document for document in documents if document["kind"] != "PriorityClass"}
    assert len(by_kind) == len(documents) - 1, "two manifests of one kind"
    return by_kind


def container_env(manifests: dict[str, dict[str, Any]]) -> dict[str, Any]:
    [container] = manifests["Deployment"]["spec"]["template"]["spec"]["containers"]
    return {item["name"]: item.get("value", item.get("valueFrom")) for item in container["env"]}


class ManifestTest(unittest.TestCase):
    def test_the_cluster_role_cannot_delete_nodes_when_pruning_is_off(self) -> None:
        for role_vars in ({}, {"github_runner_arc_node_recovery_prune_after_seconds": ""}):
            with self.subTest(role_vars):
                manifests = render(**role_vars)
                self.assertEqual(manifests["ClusterRole"]["rules"], [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list", "patch"]}])

    def test_pruning_off_leaves_the_prune_settings_out_of_the_watchers_environment(self) -> None:
        env = container_env(render())
        self.assertEqual([name for name in env if "PRUNE" in name], [])

    def test_the_cluster_role_can_delete_nodes_and_nothing_else_new_when_pruning_is_on(self) -> None:
        manifests = render(github_runner_arc_node_recovery_prune_after_seconds=PRUNE_AFTER_SECONDS)
        self.assertEqual(manifests["ClusterRole"]["rules"], [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list", "patch", "delete"]}])

    def test_the_pod_is_unpinned_when_pruning_is_off(self) -> None:
        pod = render()["Deployment"]["spec"]["template"]["spec"]
        self.assertNotIn("affinity", pod)
        self.assertNotIn("node-role.kubernetes.io/control-plane", [t.get("key") for t in pod["tolerations"]])

    def test_the_pod_that_can_delete_nodes_runs_only_on_control_plane_nodes(self) -> None:
        pod = render(github_runner_arc_node_recovery_prune_after_seconds=PRUNE_AFTER_SECONDS)["Deployment"]["spec"]["template"]["spec"]
        required = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        self.assertEqual(required["nodeSelectorTerms"], [{"matchExpressions": [{"key": "node-role.kubernetes.io/control-plane", "operator": "Exists"}]}])
        self.assertIn({"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}, pod["tolerations"])
        self.assertEqual(len([t for t in pod["tolerations"] if t.get("tolerationSeconds") == 10]), 2, "the eviction tolerations stay")

    def test_the_threshold_and_label_reach_the_watcher_as_strings(self) -> None:
        env = container_env(render(github_runner_arc_node_recovery_prune_after_seconds=PRUNE_AFTER_SECONDS))
        self.assertEqual((env["NODE_RECOVERY_PRUNE_AFTER_SECONDS"], env["NODE_RECOVERY_PRUNE_LABEL"]), (str(PRUNE_AFTER_SECONDS), PRUNE_LABEL))

    def test_the_label_is_configurable(self) -> None:
        env = container_env(render(github_runner_arc_node_recovery_prune_after_seconds=PRUNE_AFTER_SECONDS, github_runner_arc_node_recovery_prune_label="example.com/disposable"))
        self.assertEqual(env["NODE_RECOVERY_PRUNE_LABEL"], "example.com/disposable")

    def test_the_binding_still_grants_the_role_to_the_watchers_service_account(self) -> None:
        manifests = render(github_runner_arc_node_recovery_prune_after_seconds=PRUNE_AFTER_SECONDS)
        self.assertEqual(manifests["ClusterRoleBinding"]["roleRef"]["name"], manifests["ClusterRole"]["metadata"]["name"])
        self.assertEqual(manifests["ClusterRoleBinding"]["subjects"][0]["name"], manifests["ServiceAccount"]["metadata"]["name"])


class ValidationTest(unittest.TestCase):
    """The prune settings' checks in tasks/validate.yml, evaluated by Ansible itself the way the role evaluates them, every case in one run."""

    AFTER = AFTER
    LABEL = LABEL
    # Each case is a name and the role variables it sets over the defaults; the results hold, per case, which checks it fails.
    CASES: dict[str, dict[str, Any]] = {
        "defaults": {},
        "number": {AFTER: AFTER_SECONDS + 1},
        "string": {AFTER: str(PRUNE_AFTER_SECONDS)},
        "equal": {AFTER: AFTER_SECONDS},
        "below": {AFTER: AFTER_SECONDS - 1},
        "one": {AFTER: 1},
        "taint threshold raised above it": {"github_runner_arc_node_recovery_after_seconds": 900, **ON},
        **{f"not a number {value}": {AFTER: value} for value in ("ten minutes", "0", "-5", "1.5", "0600")},
        **{f"bad label {label}": {**ON, LABEL: label} for label in ("", "has space", "/leading-slash", "-leading-dash", "a/b/c", "UPPER.example.com/x", "x" * 64)},
        **{f"good label {label}": {**ON, LABEL: label} for label in (PRUNE_LABEL, "removable", "example.com/Disposable_node-1.x")},
        "bad label while off": {LABEL: "not a label"},
        "off": {AFTER: ""},
        "bad label while the watcher is off": {"github_runner_arc_node_recovery_enabled": False, AFTER: "ten minutes", LABEL: "not a label"},
    }

    @classmethod
    def setUpClass(cls) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "validate.yml").read_text())
        [task] = [task for task in tasks if task["name"].startswith("Fail on invalid controller, metrics, probe, pull Secret, node recovery")]
        cls.checks = {check["msg"].split(" ", 1)[0]: check["failed"] for check in task["loop"] if "node_recovery_prune" in check["msg"]}
        # A Jinja set shadows the role variable of the same name for the expressions after it, so one template evaluates every case.
        lines = []
        for name, role_vars in cls.CASES.items():
            sets = "".join(f"{{% set {variable} = {json.dumps(value)} %}}" for variable, value in role_vars.items())
            for setting, expression in cls.checks.items():
                lines.append(f"{{% with %}}{sets}{{{{ [{json.dumps(name)}, {json.dumps(setting)}, ({expression.strip().removeprefix('{{').removesuffix('}}')}) | bool] | to_json }}}}{{% endwith %}}")
        with tempfile.TemporaryDirectory() as work:
            template, output = Path(work) / "failed.j2", Path(work) / "failed.txt"
            template.write_text(";".join(lines))
            completed = ansible(EXPRESSION, kept_template=str(template), kept_output=str(output))
            assert completed.returncode == 0, completed.stdout + completed.stderr
            cls.results: dict[str, dict[str, bool]] = {}
            for line in output.read_text().split(";"):
                name, setting, failed = json.loads(line)
                cls.results.setdefault(name, {})[setting] = failed

    def fails(self, case: str, setting: str | None = None) -> bool:
        return self.results[case][setting or self.AFTER]

    def test_the_two_settings_each_have_a_check(self) -> None:
        self.assertEqual(sorted(self.checks), [self.AFTER, self.LABEL])

    def test_the_defaults_are_valid(self) -> None:
        self.assertEqual(self.results["defaults"], {self.AFTER: False, self.LABEL: False})

    def test_a_threshold_greater_than_the_taint_threshold_is_valid_as_a_number_or_a_string(self) -> None:
        self.assertEqual((self.fails("number"), self.fails("string")), (False, False))

    def test_a_threshold_not_greater_than_the_taint_threshold_is_refused(self) -> None:
        self.assertEqual((self.fails("equal"), self.fails("below"), self.fails("one")), (True, True, True))

    def test_the_threshold_is_compared_with_the_taint_threshold_the_role_is_given(self) -> None:
        self.assertTrue(self.fails("taint threshold raised above it"))

    def test_a_threshold_that_is_not_a_whole_number_is_refused(self) -> None:
        for case in [c for c in self.CASES if c.startswith("not a number")]:
            with self.subTest(case):
                self.assertTrue(self.fails(case))

    def test_a_label_that_is_not_a_label_key_is_refused(self) -> None:
        for case in [c for c in self.CASES if c.startswith("bad label ") and "while" not in c]:
            with self.subTest(case):
                self.assertTrue(self.fails(case, self.LABEL))

    def test_label_keys_with_and_without_a_prefix_are_valid(self) -> None:
        for case in [c for c in self.CASES if c.startswith("good label")]:
            with self.subTest(case):
                self.assertFalse(self.fails(case, self.LABEL))

    def test_nothing_is_checked_while_pruning_or_the_watcher_is_off(self) -> None:
        self.assertEqual(self.results["bad label while off"], {self.AFTER: False, self.LABEL: False})
        self.assertEqual(self.results["off"], {self.AFTER: False, self.LABEL: False})
        self.assertEqual(self.results["bad label while the watcher is off"], {self.AFTER: False, self.LABEL: False})


if __name__ == "__main__":
    unittest.main()
