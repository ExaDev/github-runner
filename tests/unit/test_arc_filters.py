"""Tests for the github_runner_arc role's profile expansion, sizing (uniform and measured) and image-reference filters."""

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
        self.assertEqual((default["namespace"], default["release"], default["app_secret"], default["scale_set_labels"]), ("arc-runners-example", "example-runners", "example-github-app", ["example-runners"]))
        self.assertEqual((builder["namespace"], builder["release"], builder["scale_set_labels"]), ("arc-runners-example-builder", "example-runners-builder", ["image-builder"]))
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

    def test_names_can_be_overridden_for_an_existing_install(self) -> None:
        result = arc.arc_profiles([org("Other-Org", [{"namespace": "arc-runners", "release_name": "ci-runners", "scale_set_labels": [], "values_file": "v.yaml"}], app_secret_name="ci-runners-github-app")])
        self.assertEqual(result["errors"], [])
        profile = result["profiles"][0]
        self.assertEqual((profile["namespace"], profile["release"], profile["app_secret"], profile["scale_set_labels"]), ("arc-runners", "ci-runners", "ci-runners-github-app", []))

    def test_the_app_secret_name_applies_to_every_profile_of_its_org(self) -> None:
        result = arc.arc_profiles([org(profiles=[{"max_runners": 1}, {"suffix": "-b", "max_runners": 1}], app_secret_name="shared-app")])
        self.assertEqual([profile["app_secret"] for profile in result["profiles"]], ["shared-app", "shared-app"])

    def test_scale_set_labels_can_carry_several_labels(self) -> None:
        result = arc.arc_profiles([org(profiles=[{"max_runners": 1, "scale_set_labels": ["a", "b"]}])])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["profiles"][0]["scale_set_labels"], ["a", "b"])

    def test_invalid_name_overrides_are_errors(self) -> None:
        cases = [
            ({"max_runners": 1, "runs_on_label": "a", "scale_set_labels": ["b"]}, {}, "Example scale_set_profiles[0]: set runs_on_label or scale_set_labels, not both"),
            ({"max_runners": 1, "scale_set_labels": "a"}, {}, "Example scale_set_profiles[0].scale_set_labels must be a list of non-empty strings (empty sets no scaleSetLabels)"),
            ({"max_runners": 1, "scale_set_labels": [""]}, {}, "Example scale_set_profiles[0].scale_set_labels must be a list of non-empty strings (empty sets no scaleSetLabels)"),
            ({"max_runners": 1, "namespace": ""}, {}, "Example scale_set_profiles[0].namespace must be a non-empty string when set"),
            ({"max_runners": 1, "release_name": 5}, {}, "Example scale_set_profiles[0].release_name must be a non-empty string when set"),
            ({"max_runners": 1}, {"app_secret_name": ""}, "Example.app_secret_name must be a non-empty string when set"),
        ]
        for profile, extra, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, arc.arc_profiles([org(profiles=[profile], **extra)])["errors"])

    def test_an_overridden_name_must_still_be_a_valid_kubernetes_name(self) -> None:
        result = arc.arc_profiles([org(profiles=[{"max_runners": 1, "namespace": "Not_Valid"}])])
        self.assertTrue(any("the derived namespace 'Not_Valid' is not a valid Kubernetes name" in message for message in result["errors"]))

    def test_two_profiles_of_an_org_sharing_a_release_name_is_an_error(self) -> None:
        result = arc.arc_profiles([org(profiles=[{"max_runners": 1, "release_name": "ci"}, {"suffix": "-b", "max_runners": 1, "release_name": "ci"}])])
        self.assertTrue(any("both use release name 'ci'" in message for message in result["errors"]), result["errors"])
        other_orgs = arc.arc_profiles([org("A", [{"max_runners": 1, "release_name": "ci"}]), org("B", [{"max_runners": 1, "release_name": "ci", "namespace": "b-ci"}])])
        self.assertEqual(other_orgs["errors"], [])

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
        self.assertIn("X: app_id is required when the role writes the App Secret (github_runner_arc_manage_secrets); with an existing App Secret it is read from that Secret's github_app_id", result["errors"])
        self.assertIn("X: image is required", result["errors"])
        self.assertIn("github_runner_arc_orgs[1] has no name", result["errors"])
        self.assertIn("Y: scale_set_profiles must be a non-empty list", result["errors"])

    def test_app_id_is_optional_when_the_role_does_not_write_the_app_secret(self) -> None:
        without = {"name": "Example", "image": "ghcr.io/example/runner:1", "scale_set_profiles": [{"max_runners": 1}]}
        self.assertEqual(arc.arc_profiles([without], require_app_id=False)["errors"], [])
        self.assertEqual(arc.arc_profiles([without])["errors"], ["Example: app_id is required when the role writes the App Secret (github_runner_arc_manage_secrets); with an existing App Secret it is read from that Secret's github_app_id"])
        self.assertIn("Example: image is required", arc.arc_profiles([{**without, "image": ""}], require_app_id=False)["errors"])

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

    def test_an_explicit_max_runners_takes_precedence_over_sizing(self) -> None:
        profiles = arc.arc_profiles([org(profiles=[{"max_runners": 5, "sizing": SIZING}, {"suffix": "-s", "sizing": SIZING}, {"suffix": "-v", "values_file": "v.yaml"}, {"suffix": "-a", "autoscale": True, "values_file": "v.yaml", "sizing": SIZING}])])["profiles"]
        self.assertEqual([profile["max_runners"] for profile in profiles], [5, 24, None, None])

    def test_an_explicit_max_runners_applies_with_a_values_file_and_on_the_autoscaled_profile(self) -> None:
        profiles = arc.arc_profiles([org(profiles=[{"values_file": "v.yaml", "max_runners": 7}, {"suffix": "-a", "autoscale": True, "values_file": "v.yaml", "max_runners": 2, "sizing": SIZING}])])["profiles"]
        self.assertEqual([profile["max_runners"] for profile in profiles], [7, 2])

    def test_an_invalid_max_runners_is_an_error(self) -> None:
        for value in (-1, 1.5, "many", True):
            with self.subTest(value=value):
                result = arc.arc_profiles([org(profiles=[{"values_file": "v.yaml", "max_runners": value}])])
                self.assertTrue(any("max_runners must be a whole number of at least 0" in message for message in result["errors"]), result["errors"])
                self.assertIsNone(result["profiles"][0]["max_runners"])

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


MEASURED = {"measured": True, "pod_cpu_request": 1, "pod_memory_request_gib": 2}


def node(name: str, cpu: str = "4", memory: str = "8Gi", labels: dict[str, str] | None = None, **spec: Any) -> dict[str, Any]:
    """Return a Node object as kubernetes.core.k8s_info returns it."""
    return {"metadata": {"name": name, "labels": labels or {}}, "spec": spec, "status": {"allocatable": {"cpu": cpu, "memory": memory}}}


def pod(node_name: str, cpu: str = "0", memory: str = "0", namespace: str = "default", phase: str = "Running", **spec: Any) -> dict[str, Any]:
    """Return a Pod object with one container requesting cpu and memory."""
    return {
        "metadata": {"name": f"p-{node_name}", "namespace": namespace},
        "spec": {"nodeName": node_name, "containers": [{"name": "c", "resources": {"requests": {"cpu": cpu, "memory": memory}}}], **spec},
        "status": {"phase": phase},
    }


class QuantityTest(unittest.TestCase):
    def test_parses_kubernetes_quantities(self) -> None:
        cases = {"3500m": "3.5", "4": "4", "0.5": "0.5", "1500u": "0.0015", "128Mi": str(128 * 1024**2), "2Gi": str(2 * 1024**3), "1G": "1000000000", "2e3": "2000", "1E": str(10**18), "12Ki": "12288"}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(arc.arc_quantity(text), arc.Decimal(expected))

    def test_rejects_what_is_not_a_quantity(self) -> None:
        for text in ("", "Mi", "1 Gi", "1Xi", "-"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                arc.arc_quantity(text)


class MeasuredMaxRunnersTest(unittest.TestCase):
    def test_measured_sizing_checks_only_the_pod_reserve_and_margin(self) -> None:
        result = arc.arc_max_runners({**MEASURED, "reserve_cpu": 0.5, "reserve_memory_gib": 1, "safety_margin": 0.5})
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["measured"])
        self.assertIsNone(result["max_runners"])
        self.assertEqual((result["pod_cpu"], result["reserve_cpu"], result["reserve_memory_gib"]), (1.0, 0.5, 1.0))
        uniform = arc.arc_max_runners({**MEASURED, "node_count": 3})
        self.assertIn("sizing has an unknown key 'node_count' (measured sizing reads the nodes from the cluster)", uniform["errors"])
        self.assertIn("sizing.measured must be a boolean (got 'sometimes')", arc.arc_max_runners({**SIZING, "measured": "sometimes"})["errors"])
        self.assertEqual(arc.arc_max_runners({**SIZING, "measured": False})["max_runners"], 24)

    def test_a_measured_profile_has_no_static_max_runners(self) -> None:
        result = arc.arc_profiles([org(profiles=[{"sizing": MEASURED}, {"suffix": "-m", "max_runners": 3, "sizing": MEASURED}])])
        self.assertEqual(result["errors"], [])
        self.assertEqual([profile["max_runners"] for profile in result["profiles"]], [None, 3])
        self.assertTrue(result["profiles"][0]["sizing"]["measured"])

    def test_sums_what_fits_on_each_node_after_its_requests(self) -> None:
        nodes = [node("small", "4", "8Gi"), node("big", "16", "32Gi")]
        pods = [pod("small", "1500m", "3Gi"), pod("big", "2", "4Gi"), pod("big", "500m", "1Gi")]
        result = arc.arc_measured_max_runners(MEASURED, nodes, pods)
        self.assertEqual(result["errors"], [])
        # big: CPU (16 - 2.5) / 1 = 13, memory (32 - 5) / 2 = 13.5 -> 13. small: CPU 2.5 -> 2, memory 5 / 2 -> 2.
        self.assertEqual([(entry["name"], entry["by_cpu"], entry["by_memory"], entry["runners"]) for entry in result["nodes"]], [("big", 13, 13, 13), ("small", 2, 2, 2)])
        self.assertEqual((result["theoretical"], result["max_runners"]), (15, 15))
        self.assertEqual(result["nodes"][1]["requested_memory_gib"], 3.0)

    def test_reserve_sidecar_and_margin_all_reduce_the_result(self) -> None:
        sizing = {**MEASURED, "sidecar_cpu_request": 0.5, "sidecar_memory_request_gib": 1, "reserve_cpu": 1, "reserve_memory_gib": 2, "safety_margin": 0.75}
        # CPU (8 - 1) / 1.5 = 4.67 -> 4; memory (16 - 2) / 3 = 4.67 -> 4; two nodes = 8; * 0.75 = 6.
        result = arc.arc_measured_max_runners(sizing, [node("a", "8", "16Gi"), node("b", "8", "16Gi")], [])
        self.assertEqual((result["theoretical"], result["max_runners"]), (8, 6))

    def test_the_tighter_resource_bounds_each_node(self) -> None:
        result = arc.arc_measured_max_runners(MEASURED, [node("a", "10", "4Gi")], [])
        self.assertEqual((result["nodes"][0]["by_cpu"], result["nodes"][0]["by_memory"], result["max_runners"]), (10, 2, 2))

    def test_a_node_already_over_committed_contributes_nothing(self) -> None:
        result = arc.arc_measured_max_runners(MEASURED, [node("full", "2", "4Gi"), node("free", "2", "4Gi")], [pod("full", "3", "1Gi")])
        self.assertEqual([entry["runners"] for entry in result["nodes"]], [2, 0])
        self.assertEqual(result["max_runners"], 2)

    def test_only_nodes_runner_pods_can_use_count(self) -> None:
        nodes = [
            node("labelled", labels={"ci": "true"}),
            node("unlabelled"),
            node("tainted", labels={"ci": "true"}, taints=[{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]),
            node("preferring", labels={"ci": "true"}, taints=[{"key": "spot", "effect": "PreferNoSchedule"}]),
        ]
        result = arc.arc_measured_max_runners(MEASURED, nodes, [], {"ci": "true"})
        self.assertEqual([entry["name"] for entry in result["nodes"]], ["labelled", "preferring"])
        self.assertEqual(result["skipped_nodes"], [
            {"name": "tainted", "reason": "has the taint node-role.kubernetes.io/control-plane:NoSchedule"},
            {"name": "unlabelled", "reason": "does not have the label ci=true"},
        ])

    def test_a_node_in_a_passing_condition_still_counts(self) -> None:
        # The ceiling lasts until the next run, so a node that is briefly unreachable or cordoned for maintenance is sized as it will be when it is back.
        nodes = [
            node("unreachable", taints=[{"key": "node.kubernetes.io/unreachable", "effect": "NoSchedule"}, {"key": "node.kubernetes.io/unreachable", "effect": "NoExecute"}]),
            node("cordoned", unschedulable=True, taints=[{"key": "node.kubernetes.io/unschedulable", "effect": "NoSchedule"}]),
        ]
        result = arc.arc_measured_max_runners(MEASURED, nodes, [])
        self.assertEqual(result["skipped_nodes"], [])
        self.assertEqual({entry["name"]: entry["condition"] for entry in result["nodes"]}, {
            "cordoned": "is cordoned, has the taint node.kubernetes.io/unschedulable:NoSchedule",
            "unreachable": "has the taint node.kubernetes.io/unreachable:NoSchedule, has the taint node.kubernetes.io/unreachable:NoExecute",
        })
        self.assertEqual(result["max_runners"], 8)

    def test_runner_pods_and_finished_pods_are_not_counted(self) -> None:
        pods = [pod("a", "2", "4Gi", namespace="arc-runners-example"), pod("a", "2", "4Gi", phase="Succeeded"), pod("a", "2", "4Gi", phase="Failed"), pod("a", "1", "2Gi")]
        result = arc.arc_measured_max_runners(MEASURED, [node("a", "4", "8Gi")], pods, excluded_namespaces=["arc-runners-example"])
        self.assertEqual((result["nodes"][0]["requested_cpu"], result["max_runners"]), (1.0, 3))

    def test_pod_requests_follow_the_schedulers_rules(self) -> None:
        # Apps and the restartable sidecar run together (1 + 0.5); the ordinary init container before the sidecar peaks at 2 alone; overhead adds 0.25.
        init = [{"name": "setup", "resources": {"requests": {"cpu": "2"}}}, {"name": "proxy", "restartPolicy": "Always", "resources": {"requests": {"cpu": "500m"}}}]
        heavy_init = pod("a", "1", initContainers=init, overhead={"cpu": "250m"})
        self.assertEqual(arc._pod_requests(heavy_init)[0], arc.Decimal("2.25"))
        # An ordinary init container after the sidecar runs beside it: 1.5 + 0.5 = 2 beats the apps' 1 + 0.5.
        late_init = pod("a", "1", initContainers=[init[1], {"name": "late", "resources": {"requests": {"cpu": "1500m"}}}])
        self.assertEqual(arc._pod_requests(late_init)[0], arc.Decimal("2"))
        self.assertEqual(arc._pod_requests({"spec": {"containers": [{"name": "c"}]}}), (arc.Decimal(0), arc.Decimal(0)))

    def test_no_eligible_node_is_an_error_not_zero_runners(self) -> None:
        result = arc.arc_measured_max_runners(MEASURED, [node("a")], [], {"ci": "true"})
        self.assertEqual(result["errors"], ["no node is eligible for runner pods with the labels ci=true"])
        self.assertIsNone(result["max_runners"])

    def test_invalid_input_is_reported(self) -> None:
        self.assertEqual(arc.arc_measured_max_runners(SIZING, [node("a")], [])["errors"], ["sizing is not measured (set measured: true)"])
        self.assertIn("sizing.pod_cpu_request is required", arc.arc_measured_max_runners({"measured": True, "pod_memory_request_gib": 1}, [node("a")], [])["errors"])
        bad = arc.arc_measured_max_runners(MEASURED, [node("a", cpu="lots")], [])
        self.assertEqual(bad["errors"], ["node a: not a Kubernetes quantity: 'lots'"])


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
