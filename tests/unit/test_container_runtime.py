"""Tests for the filter that tells which container runtime a macOS host's Docker context belongs to."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "filter" / "container_runtime.py"
_spec = importlib.util.spec_from_file_location("container_runtime_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
container_runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(container_runtime)

runtime = container_runtime.cluster_container_runtime


class AutoDetectionTest(unittest.TestCase):
    def test_the_default_colima_context_is_the_default_profile(self) -> None:
        self.assertEqual(runtime("colima"), {"kind": "colima", "colima_profile": "default"})

    def test_another_profile_s_context_names_the_profile(self) -> None:
        self.assertEqual(runtime("colima-example"), {"kind": "colima", "colima_profile": "example"})
        self.assertEqual(runtime("colima-build-2", "auto"), {"kind": "colima", "colima_profile": "build-2"})

    def test_docker_desktop_s_context_is_docker_desktop(self) -> None:
        self.assertEqual(runtime("desktop-linux"), {"kind": "docker_desktop", "colima_profile": ""})

    def test_any_other_context_or_none_is_another_runtime(self) -> None:
        for context in ("default", "orbstack", "colimas", "colima-", "", None, "  "):
            with self.subTest(context=context):
                self.assertEqual(runtime(context), {"kind": "other", "colima_profile": ""})

    def test_surrounding_whitespace_is_not_part_of_the_name(self) -> None:
        self.assertEqual(runtime(" colima-example\n"), {"kind": "colima", "colima_profile": "example"})


class RequestedRuntimeTest(unittest.TestCase):
    def test_colima_can_be_named_for_a_context_that_does_not_say(self) -> None:
        self.assertEqual(runtime("", "colima"), {"kind": "colima", "colima_profile": "default"})
        self.assertEqual(runtime("orbstack", "colima", "example"), {"kind": "colima", "colima_profile": "example"})

    def test_a_named_runtime_wins_over_the_context(self) -> None:
        self.assertEqual(runtime("colima", "docker_desktop"), {"kind": "docker_desktop", "colima_profile": ""})
        self.assertEqual(runtime("colima", "other"), {"kind": "other", "colima_profile": ""})
        self.assertEqual(runtime("desktop-linux", "colima"), {"kind": "colima", "colima_profile": "default"})

    def test_only_colima_has_a_profile(self) -> None:
        self.assertEqual(runtime("desktop-linux", "docker_desktop", "example"), {"kind": "docker_desktop", "colima_profile": ""})

    def test_a_profile_that_agrees_with_the_context_is_accepted(self) -> None:
        self.assertEqual(runtime("colima-example", "auto", "example"), {"kind": "colima", "colima_profile": "example"})
        self.assertEqual(runtime("colima", "colima", "default"), {"kind": "colima", "colima_profile": "default"})

    def test_a_profile_that_names_another_vm_than_the_context_is_an_error(self) -> None:
        for context, profile in (("colima-example", "other"), ("colima", "example"), ("colima-example", "default")):
            with self.subTest(context=context, profile=profile), self.assertRaises(container_runtime.AnsibleFilterError):
                runtime(context, "auto", profile)


class InvalidInputTest(unittest.TestCase):
    def test_an_unknown_runtime_is_an_error(self) -> None:
        for value in ("docker-desktop", "podman", "COLIMA"):
            with self.subTest(value=value), self.assertRaises(container_runtime.AnsibleFilterError):
                runtime("colima", value)

    def test_an_empty_runtime_is_auto(self) -> None:
        self.assertEqual(runtime("colima", ""), runtime("colima", "auto"))
        self.assertEqual(runtime("colima", None), runtime("colima", "auto"))

    def test_a_profile_that_is_not_a_lima_instance_name_is_an_error(self) -> None:
        for profile in ("a b", "-x", "x-", "a--b", "a/b", "a&b", "a<b", "x\n--help", "../x", "a'b", 'a"b'):
            with self.subTest(profile=profile), self.assertRaises(container_runtime.AnsibleFilterError):
                runtime("colima", "colima", profile)

    def test_a_profile_may_use_single_separators(self) -> None:
        for profile in ("a", "A1", "build-2", "build_2", "build.2", "a-b.c_d"):
            with self.subTest(profile=profile):
                self.assertEqual(runtime("", "colima", profile), {"kind": "colima", "colima_profile": profile})

    def test_a_value_that_is_not_a_string_is_an_error(self) -> None:
        for args in ((["colima"],), ("colima", ["auto"]), ("colima", "colima", 3)):
            with self.subTest(args=args), self.assertRaises(container_runtime.AnsibleFilterError):
                runtime(*args)

    def test_the_filter_is_registered(self) -> None:
        self.assertIs(container_runtime.FilterModule().filters()["cluster_container_runtime"], runtime)


if __name__ == "__main__":
    unittest.main()
