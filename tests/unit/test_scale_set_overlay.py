"""Tests for the github_runner_arc role's scale-set overlay, rendered by ansible-playbook from tests/scale_set_overlay/render.yml: the runner pods' placement and maxRunners, with and without burst nodes."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "filter" / "arc.py"
RENDER = ROOT / "tests" / "scale_set_overlay" / "render.yml"
RENDER_CONTROLLER = ROOT / "tests" / "scale_set_overlay" / "render_controller.yml"
RENDER_VALUES = ROOT / "tests" / "capabilities" / "render.yml"
_spec = importlib.util.spec_from_file_location("arc_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
arc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arc)

ELIGIBLE = "example.com/ci-eligible"
BURST_KEY = "example.com/ci-burst"
BURST = {"node_label_key": BURST_KEY, "tolerations": [{"key": BURST_KEY, "operator": "Exists", "effect": "NoSchedule"}], "max_runners": 6}
SIZING = {"node_allocatable_cpu": 8, "node_allocatable_memory_gib": 16, "node_count": 3, "pod_cpu_request": 1, "pod_memory_request_gib": 2}
MEASURED = {"measured": True, "pod_cpu_request": 1, "pod_memory_request_gib": 2}


def expanded(profile: dict[str, Any]) -> dict[str, Any]:
    """Return the one profile arc_profiles expands from profile, failing on any error."""
    result = arc.arc_profiles([{"name": "Example", "image": "ghcr.io/example/runner:1", "scale_set_profiles": [profile]}], require_app_id=False)
    assert result["errors"] == [], result["errors"]
    return result["profiles"][0]


def render(profile: dict[str, Any], **role_vars: Any) -> dict[str, Any]:
    """Render the overlay for profile with ansible-playbook and return it parsed."""
    with tempfile.TemporaryDirectory() as work:
        output = Path(work) / "overlay.yaml"
        extra = Path(work) / "extra.json"
        extra.write_text(json.dumps({"profile": profile, "overlay_output": str(output), "github_runner_arc_listener_probes_enabled": False, **role_vars}))
        env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false"}
        subprocess.run([os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(RENDER), "-e", f"@{extra}"], check=True, env=env, cwd=work, capture_output=True, text=True)
        rendered: dict[str, Any] = yaml.safe_load(output.read_text())
        return rendered


def render_values(profile: dict[str, Any], **role_vars: Any) -> dict[str, Any]:
    """Render the release's values for profile through the role's own tasks/render_scale_set_values.yml, with no capabilities, and return them parsed."""
    with tempfile.TemporaryDirectory() as work:
        output = Path(work) / "rendered.json"
        extra = Path(work) / "extra.json"
        extra.write_text(json.dumps({"profile": profile, "github_runner_arc_capabilities": {}, "render_output": str(output), **role_vars}))
        env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_HOME": work, "ANSIBLE_LOCAL_TEMP": work}
        subprocess.run([os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(RENDER_VALUES), "-e", f"@{extra}"], check=True, env=env, cwd=work, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        values: dict[str, Any] = json.loads(output.read_text())["values"]
        return values


def render_controller(**role_vars: Any) -> dict[str, Any]:
    """Render the controller values with ansible-playbook and return them parsed, an empty dict when nothing is set."""
    with tempfile.TemporaryDirectory() as work:
        output = Path(work) / "controller.yaml"
        extra = Path(work) / "extra.json"
        extra.write_text(json.dumps({"overlay_output": str(output), **role_vars}))
        env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false"}
        subprocess.run([os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(RENDER_CONTROLLER), "-e", f"@{extra}"], check=True, env=env, cwd=work, capture_output=True, text=True)
        rendered: dict[str, Any] | None = yaml.safe_load(output.read_text())
        return rendered or {}


class ControllerTest(unittest.TestCase):
    def test_the_controller_runs_under_the_platform_priority_class_only_when_asked(self) -> None:
        self.assertEqual(render_controller(github_runner_arc_control_pods_priority_enabled=True)["priorityClassName"], "github-runner-platform")
        self.assertEqual(render_controller(github_runner_arc_control_pods_priority_enabled=True, github_runner_arc_platform_priority_class_name="ci-control")["priorityClassName"], "ci-control")
        self.assertNotIn("priorityClassName", render_controller())


class OverlayTest(unittest.TestCase):
    def test_without_burst_the_runner_pods_keep_the_eligibility_node_selector(self) -> None:
        overlay = render(expanded({"sizing": SIZING, "node_selector": {"kubernetes.io/arch": "amd64"}}), github_runner_arc_node_label_key=ELIGIBLE, github_runner_arc_node_label_value="true")
        self.assertEqual(overlay["template"]["spec"], {"nodeSelector": {ELIGIBLE: "true", "kubernetes.io/arch": "amd64"}})
        self.assertEqual(overlay["maxRunners"], 24)
        self.assertEqual(overlay["listenerTemplate"]["spec"]["nodeSelector"], {ELIGIBLE: "true"})

    def test_the_listener_runs_under_the_platform_priority_class_only_when_asked(self) -> None:
        profile = expanded({"sizing": SIZING})
        asked = render(profile, github_runner_arc_control_pods_priority_enabled=True)
        self.assertEqual(asked["listenerTemplate"]["spec"]["priorityClassName"], "github-runner-platform")
        self.assertEqual(render(profile, github_runner_arc_control_pods_priority_enabled=True, github_runner_arc_platform_priority_class_name="ci-control")["listenerTemplate"]["spec"]["priorityClassName"], "ci-control")
        self.assertNotIn("listenerTemplate", render(profile))
        self.assertNotIn("priorityClassName", render(profile, github_runner_arc_node_label_key=ELIGIBLE, github_runner_arc_node_label_value="true")["listenerTemplate"]["spec"])

    def test_burst_places_the_runner_pods_by_affinity_and_adds_the_burst_runners(self) -> None:
        overlay = render(expanded({"sizing": SIZING, "burst": BURST}), github_runner_arc_node_label_key=ELIGIBLE, github_runner_arc_node_label_value="true")
        spec = overlay["template"]["spec"]
        self.assertEqual(spec["nodeSelector"], {})
        self.assertEqual(spec["tolerations"], BURST["tolerations"])
        terms = spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        self.assertEqual(terms, [{"matchExpressions": [{"key": ELIGIBLE, "operator": "In", "values": ["true"]}]}, {"matchExpressions": [{"key": BURST_KEY, "operator": "Exists"}]}])
        self.assertEqual(overlay["maxRunners"], 24 + BURST["max_runners"])
        # The listener must stay on a fixed node, since a burst node can be removed under it.
        self.assertEqual(overlay["listenerTemplate"]["spec"]["nodeSelector"], {ELIGIBLE: "true"})

    def test_an_exclusive_burst_profile_runs_on_burst_nodes_only_with_its_own_ceiling(self) -> None:
        overlay = render(expanded({"values_file": "v.yaml", "burst": {**BURST, "exclusive": True}}), github_runner_arc_node_label_key=ELIGIBLE, github_runner_arc_node_label_value="true")
        self.assertEqual(overlay["maxRunners"], BURST["max_runners"])
        spec = overlay["template"]["spec"]
        self.assertEqual(spec["nodeSelector"], {})
        self.assertEqual(spec["affinity"]["nodeAffinity"], {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": BURST_KEY, "operator": "Exists"}]}]}})
        # The listener still runs on a fixed node.
        self.assertEqual(overlay["listenerTemplate"]["spec"]["nodeSelector"], {ELIGIBLE: "true"})

    def test_burst_runners_are_added_to_a_measured_ceiling(self) -> None:
        profile = expanded({"sizing": MEASURED, "burst": BURST})
        overlay = render(profile, github_runner_arc_measured_sizing={profile["label"]: {"nodes": [{"runners": 3}, {"runners": 2}], "theoretical": 5, "max_runners": 4}})
        self.assertEqual(overlay["maxRunners"], 4 + BURST["max_runners"])
        self.assertEqual(overlay["template"]["spec"]["tolerations"], BURST["tolerations"])


class ActiveDeadlineTest(unittest.TestCase):
    def test_unset_leaves_the_runner_pod_spec_exactly_as_without_it(self) -> None:
        overlay = render(expanded({"sizing": SIZING}), github_runner_arc_node_label_key=ELIGIBLE, github_runner_arc_node_label_value="true")
        self.assertEqual(overlay["template"], {"spec": {"nodeSelector": {ELIGIBLE: "true"}}})

    def test_set_adds_it_to_the_runner_pod_spec_and_nothing_else(self) -> None:
        profile = expanded({"sizing": SIZING})
        without = render(profile, github_runner_arc_node_label_key=ELIGIBLE, github_runner_arc_node_label_value="true")
        overlay = render(profile, github_runner_arc_node_label_key=ELIGIBLE, github_runner_arc_node_label_value="true", github_runner_arc_runner_active_deadline_seconds=21600)
        self.assertEqual(overlay["template"]["spec"].pop("activeDeadlineSeconds"), 21600)
        self.assertEqual(overlay, without)



class ListenerResourcesTest(unittest.TestCase):
    RESOURCES = {"requests": {"memory": "32Mi", "cpu": "10m"}, "limits": {"memory": "64Mi"}}
    # Each setting that makes the overlay build the listener container, and none of them.
    CASES: dict[str, dict[str, Any]] = {
        "nothing else": {},
        "the node label key": {"github_runner_arc_node_label_key": ELIGIBLE, "github_runner_arc_node_label_value": "true"},
        "the probes": {"github_runner_arc_listener_probes_enabled": True},
        "the priority class": {"github_runner_arc_control_pods_priority_enabled": True},
    }

    def test_empty_adds_no_listener_container_and_no_resources(self) -> None:
        profile = expanded({"sizing": SIZING})
        self.assertNotIn("listenerTemplate", render(profile, github_runner_arc_listener_resources={}))
        for case, role_vars in self.CASES.items():
            if not role_vars:
                continue
            with self.subTest(case):
                containers = render(profile, github_runner_arc_listener_resources={}, **role_vars)["listenerTemplate"]["spec"]["containers"]
                self.assertEqual([container["name"] for container in containers], ["listener"])
                self.assertNotIn("resources", containers[0])

    def test_set_puts_them_on_the_listener_container_and_nothing_else(self) -> None:
        profile = expanded({"sizing": SIZING})
        for case, role_vars in self.CASES.items():
            with self.subTest(case):
                overlay = render(profile, github_runner_arc_listener_resources=self.RESOURCES, **role_vars)
                containers = overlay["listenerTemplate"]["spec"]["containers"]
                self.assertEqual([container["name"] for container in containers], ["listener"])
                self.assertEqual(containers[0].pop("resources"), self.RESOURCES)
                without = render(profile, **role_vars)
                # With no other listener setting the overlay has no listenerTemplate of its own, so the listener container is all it adds.
                without.setdefault("listenerTemplate", {"spec": {"containers": [{"name": "listener"}]}})
                self.assertEqual(overlay, without)

    def test_set_reaches_the_release_values_over_a_values_file_listener(self) -> None:
        # The overlay's containers list replaces the values file's, so the resources the variable sets are the ones the listener pod gets.
        with tempfile.TemporaryDirectory() as work:
            values_file = Path(work) / "values.yaml"
            values_file.write_text(yaml.safe_dump({"listenerTemplate": {"spec": {"containers": [{"name": "listener", "resources": {"requests": {"memory": "1Mi"}}}]}}, "template": {"spec": {"containers": [{"name": "runner", "image": "ghcr.io/example/runner:1", "command": ["/home/runner/run.sh"]}]}}}))
            values = render_values(expanded({"max_runners": 2, "values_file": str(values_file)}), github_runner_arc_listener_resources=self.RESOURCES)
        self.assertEqual(values["listenerTemplate"]["spec"]["containers"], [{"name": "listener", "resources": self.RESOURCES}])


if __name__ == "__main__":
    unittest.main()
