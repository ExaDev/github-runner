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


class OverlayTest(unittest.TestCase):
    def test_without_burst_the_runner_pods_keep_the_eligibility_node_selector(self) -> None:
        overlay = render(expanded({"sizing": SIZING, "node_selector": {"kubernetes.io/arch": "amd64"}}), github_runner_arc_node_label_key=ELIGIBLE, github_runner_arc_node_label_value="true")
        self.assertEqual(overlay["template"]["spec"], {"nodeSelector": {ELIGIBLE: "true", "kubernetes.io/arch": "amd64"}})
        self.assertEqual(overlay["maxRunners"], 24)
        self.assertEqual(overlay["listenerTemplate"]["spec"]["nodeSelector"], {ELIGIBLE: "true"})

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


if __name__ == "__main__":
    unittest.main()
