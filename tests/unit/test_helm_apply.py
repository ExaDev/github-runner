"""Tests for how the github_runner_arc role applies its Helm releases: the Helm version filter (plugins/filter/arc.py's arc_helm_server_side) and the options every kubernetes.core.helm task in the roles passes, so a release the autoscaler has patched since its last upgrade can still be upgraded on Helm 4, and a Helm 3 host is passed neither option. The options are rendered by ansible-playbook itself, against a stand-in for the helm module that only reports the arguments it receives."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml
from ansible.module_utils.parsing.convert_bool import boolean

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "filter" / "arc.py"
_spec = importlib.util.spec_from_file_location("arc_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
arc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arc)

FACT = "github_runner_arc_helm_server_side"
HELM_MODULES = ("kubernetes.core.helm", "helm")
OPTIONS = ("server_side", "force_conflicts")

# Stands in for kubernetes.core.helm: an old-style module (WANT_JSON) that accepts any arguments and reports the ones Ansible passed it, so an omitted option is absent from the result.
FAKE_HELM = textwrap.dedent(
    """\
    #!/usr/bin/python
    # WANT_JSON
    import json, sys
    args = json.load(open(sys.argv[1]))
    print(json.dumps({"changed": False, "received": {key: value for key, value in args.items() if not key.startswith("_ansible")}}))
    """
)


def tasks(items: Any) -> Iterator[dict[str, Any]]:
    """Yield every task in a task list, descending into block, rescue and always."""
    for task in items or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from tasks(task.get(key))


def helm_tasks() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Return every task in the roles that calls the helm module, as its file and name, the whole task (for keywords such as environment) and the module's arguments."""
    found = []
    for path in sorted(ROOT.glob("roles/*/tasks/**/*.yml")):
        for task in tasks(yaml.safe_load(path.read_text())):
            for module in HELM_MODULES:
                if module in task:
                    found.append((f"{path.relative_to(ROOT)}: {task.get('name')}", task, task[module]))
    return found


def render_options(options: dict[str, Any], server_side: bool) -> dict[str, Any]:
    """Run a task with the given helm options, exactly as the role writes them, through ansible-playbook with the version fact set, and return the arguments the module received."""
    with tempfile.TemporaryDirectory() as work:
        root = Path(work)
        module = root / "collections" / "ansible_collections" / "kubernetes" / "core" / "plugins" / "modules" / "helm.py"
        module.parent.mkdir(parents=True)
        module.write_text(FAKE_HELM)
        (module.parents[2] / "galaxy.yml").write_text("namespace: kubernetes\nname: core\nversion: 6.6.0\n")
        output = root / "received.json"
        (root / "helm-task.yml").write_text(yaml.safe_dump([{"name": "Run the helm task's options", "kubernetes.core.helm": options, "register": "helm_result"}]))
        (root / "play.yml").write_text(yaml.safe_dump([{
            "hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": {FACT: server_side},
            "tasks": [
                {"ansible.builtin.include_tasks": str(root / "helm-task.yml")},
                {"ansible.builtin.copy": {"content": "{{ helm_result.received | to_json }}", "dest": str(output), "mode": "0644"}},
            ],
        }]))
        env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(root / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false"}
        subprocess.run([os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(root / "play.yml")], check=True, env=env, cwd=work, capture_output=True, text=True)
        received: dict[str, Any] = json.loads(output.read_text())
    return received


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
        names = [name for name, _, _ in helm_tasks()]
        self.assertTrue(any("install_controller.yml" in name for name in names), names)
        self.assertTrue(any("install_scale_set_profile.yml" in name for name in names), names)

    def test_every_helm_release_forces_conflicts_on_helm_4(self) -> None:
        for name, _, args in helm_tasks():
            with self.subTest(task=name):
                self.assertIn(FACT, str(args.get("force_conflicts")))
                self.assertIn(FACT, str(args.get("server_side")))
                self.assertNotIn("force", args, "force means a client-side replace, which cannot be combined with force_conflicts")

    def test_helm_4_gets_server_side_apply_with_forced_conflicts(self) -> None:
        for name, _, args in helm_tasks():
            with self.subTest(task=name):
                received = render_options({key: args[key] for key in OPTIONS if key in args}, True)
                self.assertEqual(set(received), set(OPTIONS))
                # "true", not "auto": with auto Helm picks client-side apply for a release first installed that way, and then refuses to force conflicts.
                self.assertEqual(str(received["server_side"]), "true")
                self.assertTrue(boolean(received["force_conflicts"]))

    def test_helm_3_is_passed_neither_option(self) -> None:
        # Below Helm 4 kubernetes.core.helm refuses server_side in any form and force_conflicts when it is true. Both are omitted rather than passed as false, so the role does not depend on how the module treats a false force_conflicts (and on Helm 3 needs no module version that knows the option). Only this test guards that: tests/helm_apply/run.sh's Helm 3 phase passes with a false force_conflicts.
        for name, _, args in helm_tasks():
            with self.subTest(task=name):
                self.assertEqual(render_options({key: args[key] for key in OPTIONS if key in args}, False), {})

    def test_every_helm_release_renders_its_diff_against_the_cluster(self) -> None:
        # kubernetes.core.helm runs helm diff on an existing release whenever the plugin is installed, in check mode or not, and helm-diff's default rendering (helm template) has no cluster access, so the scale-set chart's lookup of the controller's Deployment fails the task. The task sets the variable itself rather than leaving it to every caller's shell.
        for name, task, _ in helm_tasks():
            with self.subTest(task=name):
                self.assertEqual(str(task.get("environment", {}).get("HELM_DIFF_USE_UPGRADE_DRY_RUN")), "true")

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
