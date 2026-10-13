"""Tests for how the github_runner_arc role applies its Helm releases: the Helm version filter (plugins/filter/arc.py's arc_helm_server_side) and the options every kubernetes.core.helm task in the roles passes, so a release the autoscaler has patched since its last upgrade can still be upgraded on Helm 4."""

from __future__ import annotations

import importlib.util
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "filter" / "arc.py"
_spec = importlib.util.spec_from_file_location("arc_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
arc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arc)

FACT = "github_runner_arc_helm_server_side"
HELM_MODULES = ("kubernetes.core.helm", "helm")


def tasks(items: Any) -> Iterator[dict[str, Any]]:
    """Yield every task in a task list, descending into block, rescue and always."""
    for task in items or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from tasks(task.get(key))


def helm_tasks() -> list[tuple[str, dict[str, Any]]]:
    """Return every task in the roles that calls the helm module, with the file it is in."""
    found = []
    for path in sorted(ROOT.glob("roles/*/tasks/*.yml")):
        for task in tasks(yaml.safe_load(path.read_text())):
            for module in HELM_MODULES:
                if module in task:
                    found.append((f"{path.relative_to(ROOT)}: {task.get('name')}", task[module]))
    return found


class HelmServerSideTest(unittest.TestCase):
    def test_helm_4_and_later_apply_server_side(self) -> None:
        for version in ("v4.3.0+gbec5b06", "v4.0.0", "v5.1.2", "4.3.0"):
            with self.subTest(version=version):
                self.assertTrue(arc.arc_helm_server_side(version))

    def test_helm_3_does_not(self) -> None:
        for version in ("v3.16.2+g13654a5", "v3.0.0"):
            with self.subTest(version=version):
                self.assertFalse(arc.arc_helm_server_side(version))

    def test_an_unreadable_version_is_an_error_not_a_guess(self) -> None:
        for version in ("", "unknown", "vX.1"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                arc.arc_helm_server_side(version)


class HelmTasksTest(unittest.TestCase):
    def test_the_role_installs_helm_releases(self) -> None:
        names = [name for name, _ in helm_tasks()]
        self.assertTrue(any("install_controller.yml" in name for name in names), names)
        self.assertTrue(any("install_scale_set_profile.yml" in name for name in names), names)

    def test_every_helm_release_forces_conflicts_on_helm_4(self) -> None:
        for name, args in helm_tasks():
            with self.subTest(task=name):
                self.assertIn(FACT, str(args.get("force_conflicts")))
                self.assertIn(FACT, str(args.get("server_side")))
                self.assertNotIn("force", args, "force means a client-side replace, which cannot be combined with force_conflicts")

    def test_the_version_is_read_before_any_release_is_installed(self) -> None:
        main = list(tasks(yaml.safe_load((ROOT / "roles" / "github_runner_arc" / "tasks" / "main.yml").read_text())))
        names = [task.get("name", "") for task in main]
        decide = next(index for index, task in enumerate(main) if FACT in str(task.get("ansible.builtin.set_fact", {})))
        controller = next(index for index, task in enumerate(main) if task.get("ansible.builtin.include_tasks") == "install_controller.yml")
        orgs = next(index for index, task in enumerate(main) if task.get("ansible.builtin.include_tasks") == "install_org.yml")
        self.assertLess(decide, controller, names)
        self.assertLess(decide, orgs, names)
        read = main[decide - 1]
        self.assertEqual(read["ansible.builtin.command"]["argv"], ["helm", "version", "--short"])
        self.assertFalse(read["check_mode"], "check mode must still read the version, since the helm tasks' arguments depend on it")
        self.assertEqual(main[decide]["when"], read["when"])
        self.assertEqual(main[controller]["when"], read["when"])


if __name__ == "__main__":
    unittest.main()
