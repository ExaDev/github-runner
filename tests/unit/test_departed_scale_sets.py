"""Tests for how the github_runner_arc role removes a scale set whose profile has left the inventory: the marker tasks/install_scale_set_profile.yml puts on every scale set's namespace, where tasks/main.yml runs the removal, and tasks/remove_departed_scale_sets.yml run (through tests/departed_scale_sets/remove.yml, with ansible-playbook) against stand-ins for kubernetes.core and the helm client that record every call. The decisions themselves are tested in test_arc_filters.py."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_arc"
REMOVE = ROOT / "tests" / "departed_scale_sets" / "remove.yml"
VARS = yaml.safe_load((ROLE / "vars" / "main.yml").read_text())
LABELS: dict[str, str] = VARS["github_runner_arc_scale_set_namespace_labels"]
RELEASE_ANNOTATION: str = VARS["github_runner_arc_scale_set_release_annotation"]

# Stands in for kubernetes.core's k8s, k8s_info and helm: records the arguments and whether the run is in check mode, and answers k8s_info from STUB_INFO, keyed by kind, or by kind and namespace for a namespaced lookup.
STUB_MODULE = """from ansible.module_utils.basic import AnsibleModule
import json, os

ARGUMENTS = ("api_version", "definition", "kind", "label_selectors", "merge_type", "name", "namespace", "release_namespace", "state", "wait")


def main():
    module = AnsibleModule(argument_spec={key: {"type": "raw"} for key in ARGUMENTS}, supports_check_mode=True)
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write(json.dumps({"module": MODULE, "check_mode": module.check_mode, **module.params}) + "\\n")
    info = json.loads(os.environ.get("STUB_INFO", "{}"))
    kind, namespace = module.params.get("kind"), module.params.get("namespace")
    found = info.get(f"{kind}/{namespace}" if namespace else kind, []) if MODULE == "k8s_info" else []
    module.exit_json(changed=MODULE != "k8s_info", resources=found)


main()
"""

# Stands in for the helm client the tasks run for `helm list`: records the arguments and prints STUB_RELEASES' list for the namespace asked about.
STUB_HELM = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({"module": "helm-cli", "argv": args}) + "\\n")
namespace = args[args.index("--namespace") + 1]
print(json.dumps(json.loads(os.environ.get("STUB_RELEASES", "{}")).get(namespace, [])))
"""

FLEET = {"profiles": [{"namespace": "arc-runners-example", "release": "example-runners"}]}


def namespace(name: str, release: str = "gone-runners") -> dict[str, Any]:
    """A Namespace carrying the role's marker, as the cluster returns it."""
    return {"kind": "Namespace", "metadata": {"name": name, "labels": LABELS, "annotations": {RELEASE_ANNOTATION: release}}}


def chart_release(name: str, chart: str = "gha-runner-scale-set-0.14.1") -> dict[str, str]:
    """A release as `helm list --output json` reports it."""
    return {"name": name, "chart": chart, "status": "deployed"}


def runner(namespace_name: str, name: str, phase: str) -> dict[str, Any]:
    """An EphemeralRunner as the cluster returns it."""
    return {"kind": "EphemeralRunner", "metadata": {"name": name, "namespace": namespace_name}, "status": {"phase": phase}}


def run(marked: list[dict[str, Any]], releases: dict[str, list[dict[str, str]]] | None = None, info: dict[str, list[dict[str, Any]]] | None = None, check: bool = False, **role_vars: Any) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    """Run the removal tasks against a cluster holding the marked namespaces, the Helm releases of each namespace and any other objects (keyed as STUB_MODULE reads them), and return the playbook's outcome and every call it made, in order."""
    with tempfile.TemporaryDirectory() as work:
        modules = Path(work) / "stubs" / "ansible_collections" / "kubernetes" / "core" / "plugins" / "modules"
        modules.mkdir(parents=True)
        for module in ("k8s", "k8s_info", "helm"):
            (modules / f"{module}.py").write_text(STUB_MODULE.replace("MODULE", repr(module)))
        bin_dir = Path(work) / "bin"
        bin_dir.mkdir()
        helm = bin_dir / "helm"
        helm.write_text(STUB_HELM)
        helm.chmod(helm.stat().st_mode | stat.S_IXUSR)
        log = Path(work) / "calls.jsonl"
        extra = Path(work) / "extra.json"
        extra.write_text(json.dumps({"github_runner_arc_fleet": FLEET, "github_runner_arc_kubeconfig_path": "", **role_vars}))
        env = {
            **os.environ,
            "PATH": os.pathsep.join([str(bin_dir), os.environ["PATH"]]),
            "ANSIBLE_COLLECTIONS_PATH": os.pathsep.join([str(Path(work) / "stubs"), str(ROOT / "playbooks" / "collections")]),
            "ANSIBLE_LOCALHOST_WARNING": "false",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
            "ANSIBLE_HOME": str(Path(work) / "home"),
            "STUB_LOG": str(log),
            "STUB_INFO": json.dumps({"Namespace": marked, **(info or {})}),
            "STUB_RELEASES": json.dumps(releases or {}),
        }
        command = [os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(REMOVE), "-e", f"@{extra}", *(["--check"] if check else [])]
        completed = subprocess.run(command, env=env, cwd=work, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return completed, calls


def changes(calls: list[dict[str, Any]]) -> list[tuple[str, str | None, str | None]]:
    """Every call that changes the cluster, as (what, namespace, name): a release uninstalled, an object deleted or patched."""
    found: list[tuple[str, str | None, str | None]] = []
    for call in calls:
        if call["module"] == "helm" and call["state"] == "absent":
            found.append(("uninstall", call["release_namespace"], call["name"]))
        elif call["module"] == "k8s" and call["state"] in ("absent", "patched"):
            found.append((f"{call['state']} {call['kind']}", call.get("namespace"), call["name"]))
    return found


class RemovalTasksTest(unittest.TestCase):
    """The removal tasks, run against stand-ins for the cluster."""

    def test_nothing_departed_changes_nothing_and_runs_no_helm(self) -> None:
        completed, calls = run([namespace("arc-runners-example", "example-runners")])
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(changes(calls), [])
        self.assertEqual([call for call in calls if call["module"] == "helm-cli"], [])

    def test_the_marked_namespaces_are_found_by_every_marker_label(self) -> None:
        _, calls = run([])
        [lookup] = [call for call in calls if call["module"] == "k8s_info" and call["kind"] == "Namespace"]
        self.assertEqual(sorted(lookup["label_selectors"]), sorted(f"{key}={value}" for key, value in LABELS.items()))

    def test_a_departed_idle_scale_set_is_uninstalled_and_then_its_namespace_deleted(self) -> None:
        completed, calls = run([namespace("arc-runners-example", "example-runners"), namespace("arc-runners-gone")], {"arc-runners-gone": [chart_release("gone-runners")]})
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(changes(calls), [("uninstall", "arc-runners-gone", "gone-runners"), ("absent Namespace", None, "arc-runners-gone")])
        uninstall = next(call for call in calls if call["module"] == "helm")
        self.assertTrue(uninstall["wait"])
        self.assertIn("Uninstalling release gone-runners and deleting namespace arc-runners-gone", completed.stdout)

    def test_the_releases_are_listed_in_every_state_in_the_departed_namespace_only(self) -> None:
        _, calls = run([namespace("arc-runners-example", "example-runners"), namespace("arc-runners-gone")])
        self.assertEqual([call["argv"] for call in calls if call["module"] == "helm-cli"], [["list", "--namespace", "arc-runners-gone", "--all", "--output", "json"]])

    def test_a_release_already_gone_leaves_only_the_namespace_to_delete(self) -> None:
        completed, calls = run([namespace("arc-runners-gone")])
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(changes(calls), [("absent Namespace", None, "arc-runners-gone")])

    def test_check_mode_reports_the_same_removal_and_passes_check_mode_to_every_change(self) -> None:
        completed, calls = run([namespace("arc-runners-gone")], {"arc-runners-gone": [chart_release("gone-runners")]}, check=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(changes(calls), [("uninstall", "arc-runners-gone", "gone-runners"), ("absent Namespace", None, "arc-runners-gone")])
        self.assertTrue(all(call["check_mode"] for call in calls if call["module"] in ("helm", "k8s")))
        # The release list is read in check mode too, since the decision depends on it.
        self.assertEqual(len([call for call in calls if call["module"] == "helm-cli"]), 1)

    def test_a_refusal_fails_the_run_before_anything_is_removed(self) -> None:
        cases = {
            "the controller's release": ([namespace("arc-runners-gone")], {"arc-runners-gone": [chart_release("arc", "gha-runner-scale-set-controller-0.14.1")]}, {}),
            "another release": ([namespace("arc-runners-gone"), namespace("arc-runners-idle", "idle-runners")], {"arc-runners-gone": [chart_release("gone-runners"), chart_release("someone-elses", "redis-1.0.0")]}, {}),
            "an unfinished runner": ([namespace("arc-runners-gone"), namespace("arc-runners-idle", "idle-runners")], {"arc-runners-gone": [chart_release("gone-runners")]}, {"EphemeralRunner/arc-runners-gone": [runner("arc-runners-gone", "busy-runner", "Running")]}),
            "a protected namespace": ([namespace("kube-system"), namespace("arc-runners-idle", "idle-runners")], {}, {}),
        }
        for case, (marked, releases, info) in cases.items():
            for check in (False, True):
                with self.subTest(case=case, check=check):
                    completed, calls = run(marked, releases, info, check=check)
                    self.assertNotEqual(completed.returncode, 0, completed.stdout)
                    self.assertEqual(changes(calls), [])

    def test_unfinished_runners_are_named_in_the_refusal(self) -> None:
        completed, _ = run([namespace("arc-runners-gone")], {"arc-runners-gone": [chart_release("gone-runners")]}, {"EphemeralRunner/arc-runners-gone": [runner("arc-runners-gone", "busy-runner", "Running")]})
        self.assertIn("busy-runner", completed.stdout)
        self.assertIn("arc-runners-gone", completed.stdout)

    def test_drain_scales_a_busy_scale_set_to_no_runners_removes_nothing_and_fails_naming_its_runners(self) -> None:
        runner_set = {"kind": "AutoscalingRunnerSet", "metadata": {"name": "gone-runners", "namespace": "arc-runners-gone"}}
        completed, calls = run(
            [namespace("arc-runners-gone"), namespace("arc-runners-idle", "idle-runners")],
            {"arc-runners-gone": [chart_release("gone-runners")], "arc-runners-idle": [chart_release("idle-runners")]},
            {"EphemeralRunner/arc-runners-gone": [runner("arc-runners-gone", "busy-runner", "Running")], "AutoscalingRunnerSet/arc-runners-gone": [runner_set]},
            github_runner_arc_drain_departed_scale_sets=True,
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("arc-runners-gone is draining, still finishing: busy-runner", completed.stdout)
        # The idle departed scale set is removed in the same run; the busy one is only scaled down.
        self.assertEqual(changes(calls), [("uninstall", "arc-runners-idle", "idle-runners"), ("absent Namespace", None, "arc-runners-idle"), ("patched AutoscalingRunnerSet", "arc-runners-gone", "gone-runners")])
        patch = next(call for call in calls if call["module"] == "k8s" and call["state"] == "patched")
        self.assertEqual(patch["definition"], {"spec": {"minRunners": 0, "maxRunners": 0}})

    def test_drain_is_off_by_default(self) -> None:
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        self.assertIs(defaults["github_runner_arc_drain_departed_scale_sets"], False)
        options = yaml.safe_load((ROLE / "meta" / "argument_specs.yml").read_text())["argument_specs"]["main"]["options"]
        self.assertIs(options["github_runner_arc_drain_departed_scale_sets"]["default"], False)


class MarkerTest(unittest.TestCase):
    """The marker the install puts on a scale set's namespace, and where the removal runs."""

    def test_every_scale_set_namespace_is_marked_with_its_release_on_every_install(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "install_scale_set_profile.yml").read_text())
        [create] = [task for task in tasks if isinstance((task.get("kubernetes.core.k8s") or {}).get("definition"), dict) and task["kubernetes.core.k8s"]["definition"].get("kind") == "Namespace"]
        self.assertNotIn("when", create)
        metadata = create["kubernetes.core.k8s"]["definition"]["metadata"]
        self.assertEqual(metadata["labels"], "{{ github_runner_arc_scale_set_namespace_labels }}")
        self.assertEqual(metadata["annotations"], "{{ {github_runner_arc_scale_set_release_annotation: profile.release} }}")

    def test_the_marker_names_the_role_as_manager_and_the_protected_namespaces_include_the_roles_own(self) -> None:
        self.assertEqual(LABELS["app.kubernetes.io/managed-by"], "exadev.github_runner")
        protected = VARS["github_runner_arc_scale_set_protected_namespaces"]
        for name in ("github_runner_arc_controller_namespace", "github_runner_arc_platform_namespace", "github_runner_arc_registry_cache_namespace", "'kube-system'", "'default'"):
            with self.subTest(name=name):
                self.assertIn(name, protected)

    def test_the_removal_runs_after_every_profile_is_installed_and_before_the_stamp(self) -> None:
        main = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        block = next(task for task in main if "block" in task)["block"]
        included = [task.get("ansible.builtin.include_tasks") for task in block]
        self.assertLess(included.index("install_org.yml"), included.index("remove_departed_scale_sets.yml"))
        self.assertLess(included.index("remove_departed_scale_sets.yml"), included.index("record_deployed_state.yml"))
        self.assertNotIn("when", block[included.index("remove_departed_scale_sets.yml")])


if __name__ == "__main__":
    unittest.main()
