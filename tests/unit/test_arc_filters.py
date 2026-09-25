"""Tests for the github_runner_arc role's profile expansion, sizing and image-reference filters."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from typing import Any

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "filter" / "arc.py"
_spec = importlib.util.spec_from_file_location("arc_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
arc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arc)


def org(name: str = "Example", profiles: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    """Return a minimal valid org entry."""
    return {"name": name, "app_id": 1, "image": "ghcr.io/example/runner:latest", "scale_set_profiles": profiles if profiles is not None else [{"suffix": "", "max_runners": 2}], **extra}


SIZING = {
    "node_allocatable_cpu": 8,
    "node_allocatable_memory_gib": 16,
    "node_count": 3,
    "pod_cpu_request": 1,
    "pod_memory_request_gib": 2,
}


class ProfilesTest(unittest.TestCase):
    def test_derives_names_from_the_org_and_suffix(self) -> None:
        result = arc.arc_profiles([org(profiles=[{"suffix": "", "max_runners": 1}, {"suffix": "-builder", "values_file": "b.yaml", "runs_on_label": "image-builder", "node_selector": {"kubernetes.io/hostname": "n1"}}])])
        self.assertEqual(result["errors"], [])
        default, builder = result["profiles"]
        self.assertEqual((default["namespace"], default["release"], default["app_secret"], default["runs_on_label"]), ("arc-runners-example", "example-runners", "example-github-app", "example-runners"))
        self.assertEqual((builder["namespace"], builder["release"], builder["runs_on_label"]), ("arc-runners-example-builder", "example-runners-builder", "image-builder"))
        self.assertEqual(builder["node_selector"], {"kubernetes.io/hostname": "n1"})
        self.assertEqual(default["node_selector"], {})
        self.assertIsNone(result["autoscaled"])

    def test_a_missing_suffix_means_the_unsuffixed_profile(self) -> None:
        result = arc.arc_profiles([org(profiles=[{"values_file": "v.yaml"}])])
        self.assertEqual(result["profiles"][0]["release"], "example-runners")

    def test_the_same_org_twice_is_an_error_even_in_a_different_case(self) -> None:
        result = arc.arc_profiles([org("Example"), org("example")])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("configured more than once", result["errors"][0])

    def test_two_profiles_sharing_a_namespace_is_an_error(self) -> None:
        result = arc.arc_profiles([org(profiles=[{"suffix": "-a"}, {"suffix": "-a"}])])
        self.assertTrue(any("both map to namespace" in message for message in result["errors"]))

    def test_one_autoscaled_profile_is_returned(self) -> None:
        result = arc.arc_profiles([org(profiles=[{"suffix": "", "autoscale": True, "max_runners": 1}, {"suffix": "-b", "max_runners": 1}])])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["autoscaled"]["release"], "example-runners")

    def test_more_than_one_autoscaled_profile_is_an_error_across_orgs(self) -> None:
        result = arc.arc_profiles([org("A", [{"autoscale": True}]), org("B", [{"autoscale": "yes"}])])
        self.assertTrue(any("At most one" in message for message in result["errors"]))
        self.assertIsNone(result["autoscaled"])

    def test_invalid_autoscale_value_is_an_error(self) -> None:
        result = arc.arc_profiles([org(profiles=[{"autoscale": "sometimes"}])])
        self.assertTrue(any("autoscale must be a boolean" in message for message in result["errors"]))

    def test_missing_required_org_fields_are_errors(self) -> None:
        result = arc.arc_profiles([{"name": "X", "scale_set_profiles": [{}]}, {"app_id": 1}, {"name": "Y", "app_id": 1, "image": "i", "scale_set_profiles": []}])
        self.assertIn("X: app_id is required", result["errors"])
        self.assertIn("X: image is required", result["errors"])
        self.assertIn("github_runner_arc_orgs[1] has no name", result["errors"])
        self.assertIn("Y: scale_set_profiles must be a non-empty list", result["errors"])

    def test_a_private_key_and_a_1password_reference_together_are_an_error(self) -> None:
        both = dict(org(), private_key="pem", private_key_op_reference="op://v/i/f")
        self.assertIn("Example: set private_key or private_key_op_reference, not both (the 1Password adapter fills private_key from the reference)", arc.arc_profiles([both])["errors"])
        self.assertEqual(arc.arc_profiles([dict(org(), private_key="pem")])["errors"], [])
        self.assertEqual(arc.arc_profiles([dict(org(), private_key_op_reference="op://v/i/f")])["errors"], [])

    def test_an_invalid_kubernetes_name_is_an_error(self) -> None:
        result = arc.arc_profiles([org(profiles=[{"suffix": "_bad"}])])
        self.assertTrue(any("not a valid Kubernetes name" in message for message in result["errors"]))

    def test_sizing_is_evaluated_per_profile_and_its_errors_reported(self) -> None:
        good = arc.arc_profiles([org(profiles=[{"sizing": SIZING}])])
        self.assertEqual(good["errors"], [])
        self.assertEqual(good["profiles"][0]["sizing"]["max_runners"], 24)
        bad = arc.arc_profiles([org(profiles=[{"sizing": {"node_count": 1}}])])
        self.assertTrue(any("sizing.node_allocatable_cpu is required" in message for message in bad["errors"]))

    def test_a_non_list_is_an_error(self) -> None:
        self.assertEqual(arc.arc_profiles("nope")["errors"], ["github_runner_arc_orgs must be a list"])

    def test_the_role_values_template_needs_a_static_max_runners(self) -> None:
        self.assertEqual(arc.arc_profiles([org(profiles=[{"max_runners": 1}, {"suffix": "-s", "sizing": SIZING}, {"suffix": "-v", "values_file": "v.yaml"}])])["errors"], [])
        missing = arc.arc_profiles([org(profiles=[{}])])["errors"]
        self.assertEqual(missing, ["Example scale_set_profiles[0]: a profile with no values_file needs max_runners or sizing"])
        autoscaled = arc.arc_profiles([org(profiles=[{"autoscale": True, "sizing": SIZING}])])["errors"]
        self.assertEqual(autoscaled, ["Example scale_set_profiles[0]: a profile with no values_file needs max_runners"])

    def test_settings_carries_the_profile_keys(self) -> None:
        profile = {"suffix": "", "max_runners": 4, "container_mode": "dind"}
        self.assertEqual(arc.arc_profiles([org(profiles=[profile])])["profiles"][0]["settings"], profile)


class MaxRunnersTest(unittest.TestCase):
    def test_the_tighter_resource_bounds_pods_per_node(self) -> None:
        # CPU allows 8 / 1 = 8 per node; memory allows 16 / 3 = 5, so memory is the bound.
        result = arc.arc_max_runners({**SIZING, "pod_memory_request_gib": 3})
        self.assertEqual((result["per_node_by_cpu"], result["per_node_by_memory"], result["per_node"]), (8, 5, 5))
        self.assertEqual((result["theoretical"], result["max_runners"]), (15, 15))

    def test_baseline_sidecar_and_margin_all_reduce_the_result(self) -> None:
        result = arc.arc_max_runners({**SIZING, "baseline_cpu_fraction": 0.1, "baseline_memory_fraction": 0.25, "sidecar_cpu_request": 0.25, "sidecar_memory_request_gib": 0.5, "safety_margin": 0.8})
        # CPU: 8 * 0.9 = 7.2 / 1.25 = 5.76 -> 5. Memory: 16 * 0.75 = 12 / 2.5 = 4.8 -> 4. 4 per node * 3 nodes = 12, * 0.8 = 9.6 -> 9.
        self.assertEqual(result["errors"], [])
        self.assertEqual((result["per_node"], result["theoretical"], result["max_runners"]), (4, 12, 9))
        self.assertAlmostEqual(result["usable_cpu"], 7.2)

    def test_floors_are_exact_for_decimal_inputs(self) -> None:
        # 0.3 / 0.1 is 2.9999999999999996 in binary floating point; the filter must still fit 3 pods.
        result = arc.arc_max_runners({"node_allocatable_cpu": 0.3, "node_allocatable_memory_gib": 10, "node_count": 1, "pod_cpu_request": 0.1, "pod_memory_request_gib": 1})
        self.assertEqual(result["per_node_by_cpu"], 3)

    def test_a_pod_larger_than_a_node_gives_zero(self) -> None:
        self.assertEqual(arc.arc_max_runners({**SIZING, "pod_memory_request_gib": 32})["max_runners"], 0)

    def test_invalid_inputs_are_reported_without_a_result(self) -> None:
        cases = [
            ({**SIZING, "node_count": 0}, "sizing.node_count must be at least 1"),
            ({**SIZING, "node_count": 1.5}, "sizing.node_count must be a whole number"),
            ({**SIZING, "pod_cpu_request": 0}, "sizing.pod_cpu_request must be greater than 0"),
            ({**SIZING, "safety_margin": 1.2}, "sizing.safety_margin must be at most 1"),
            ({**SIZING, "safety_margin": 0}, "sizing.safety_margin must be greater than 0"),
            ({**SIZING, "baseline_memory_fraction": 1}, "sizing.baseline_memory_fraction must be less than 1"),
            ({**SIZING, "pod_cpu_request": "two"}, "sizing.pod_cpu_request must be a number"),
            ({**SIZING, "pod_cpu_request": True}, "sizing.pod_cpu_request must be a number"),
            ({**SIZING, "extra": 1}, "sizing has an unknown key 'extra'"),
        ]
        for sizing, expected in cases:
            with self.subTest(expected=expected):
                result = arc.arc_max_runners(sizing)
                self.assertIsNone(result["max_runners"])
                self.assertTrue(any(message.startswith(expected) for message in result["errors"]), result["errors"])

    def test_a_non_mapping_is_an_error(self) -> None:
        self.assertEqual(arc.arc_max_runners([1])["errors"], ["sizing must be a mapping"])


class ImageRefTest(unittest.TestCase):
    def test_splits_registry_repository_and_tag(self) -> None:
        cases = {
            "ghcr.io/example/runner:1.2": ("ghcr.io", "example/runner", "1.2"),
            "ghcr.io/example/runner": ("ghcr.io", "example/runner", "latest"),
            "ghcr.io/example/runner@sha256:abc": ("ghcr.io", "example/runner", "sha256:abc"),
            "registry.local:5000/team/image:tag": ("registry.local:5000", "team/image", "tag"),
            "localhost/image": ("localhost", "image", "latest"),
            "docker:dind": ("docker.io", "library/docker", "dind"),
            "example/runner:2": ("docker.io", "example/runner", "2"),
        }
        for image, expected in cases.items():
            with self.subTest(image=image):
                result = arc.arc_image_ref(image)
                self.assertEqual((result["registry"], result["repository"], result["reference"]), expected)


if __name__ == "__main__":
    unittest.main()
