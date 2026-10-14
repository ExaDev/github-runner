"""Tests for how the github_runner_arc role installs the node cache pruner: the manifests templates/node-cache-pruner.yaml.j2 renders (through tests/node_cache_pruner/render.yml, with ansible-playbook), the decision tasks/decide_node_cache_pruner.yml makes (through tests/node_cache_pruner/decide.yml), and the shape of tasks/install_node_cache_pruner.yml."""

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
ROLE = ROOT / "roles" / "github_runner_arc"
RENDER = ROOT / "tests" / "node_cache_pruner" / "render.yml"
DECIDE = ROOT / "tests" / "node_cache_pruner" / "decide.yml"
KEPT = ROOT / "tests" / "node_cache_pruner" / "kept.yml"

_SPEC = importlib.util.spec_from_file_location("arc_filters", ROOT / "plugins" / "filter" / "arc.py")
assert _SPEC is not None and _SPEC.loader is not None
arc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(arc)

PROFILES = [
    {"namespace": "arc-a", "release": "main", "target": "arc-a/main"},
    {"namespace": "arc-b", "release": "builders", "target": "arc-b/builders"},
]
DIGEST_IMAGE = "docker.io/library/busybox:1.37.0@sha256:" + "2" * 64
CATALOGUE = {"pnpm-store": {"node_cache": {"init_image": DIGEST_IMAGE, "dirs": {"STORE": "pnpm"}}}, "node": {"image": "ghcr.io/example/node:22"}}


def ansible(playbook: Path, **variables: Any) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as work:
        extra = Path(work) / "extra.json"
        extra.write_text(json.dumps(variables))
        env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_HOME": str(Path(work) / "home")}
        return subprocess.run([os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(playbook), "-e", f"@{extra}"], env=env, cwd=work, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)


def render(**role_vars: Any) -> list[dict[str, Any]]:
    """Render the pruner's manifests, in the order the template writes them."""
    with tempfile.TemporaryDirectory() as work:
        output = Path(work) / "pruner.yaml"
        variables = {"github_runner_arc_node_cache_profiles": PROFILES, "github_runner_arc_labelled_nodes": ["node-a", "node-b"], "github_runner_arc_node_label_key": "example/ci", "github_runner_arc_node_label_value": "true", **role_vars}
        completed = ansible(RENDER, pruner_output=str(output), **variables)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        documents: list[dict[str, Any]] = [document for document in yaml.safe_load_all(output.read_text()) if document]
        return documents


def decide(fleet_targets: list[str], existing: int, **role_vars: Any) -> dict[str, Any]:
    """Run the decision for a fleet of the given profile targets, each with the pnpm-store capability when its target is listed in with_cache, and `existing` pruners already installed."""
    profiles = [{"namespace": target.split("/")[0], "release": target.split("/")[1], "target": target, "capabilities": ["pnpm-store" if target in role_vars.pop("with_cache", fleet_targets) else "node"]} for target in fleet_targets]
    with tempfile.TemporaryDirectory() as work:
        output = Path(work) / "decision.json"
        completed = ansible(DECIDE, decide_output=str(output), github_runner_arc_fleet={"profiles": profiles}, github_runner_arc_capabilities=CATALOGUE, github_runner_arc_node_cache_existing_pruners=[{}] * existing, **role_vars)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        decision: dict[str, Any] = json.loads(output.read_text())
        return decision


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = render()
        cls.by_kind: dict[str, list[dict[str, Any]]] = {}
        for document in cls.documents:
            cls.by_kind.setdefault(document["kind"], []).append(document)
        [cls.daemonset] = cls.by_kind["DaemonSet"]
        cls.pod = cls.daemonset["spec"]["template"]["spec"]
        [cls.container] = cls.pod["containers"]

    def test_each_cache_profile_gets_a_role_and_binding_in_its_own_namespace_named_for_its_release(self) -> None:
        roles = {(role["metadata"]["namespace"], role["metadata"]["name"]) for role in self.by_kind["Role"]}
        self.assertEqual(roles, {("arc-a", "node-cache-pruner-main"), ("arc-b", "node-cache-pruner-builders")})
        bindings = {(binding["metadata"]["namespace"], binding["metadata"]["name"]) for binding in self.by_kind["RoleBinding"]}
        self.assertEqual(bindings, roles)
        for role in self.by_kind["Role"]:
            self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}])
        for binding in self.by_kind["RoleBinding"]:
            self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "node-cache-pruner", "namespace": "github-runner-platform"}])
            self.assertEqual(binding["roleRef"]["name"], binding["metadata"]["name"])

    def test_there_is_no_cluster_wide_pod_access(self) -> None:
        rules = [rule for role in self.by_kind.get("ClusterRole", []) for rule in role["rules"]]
        self.assertEqual([rule for rule in rules if "pods" in rule["resources"]], [])

    def test_the_cluster_role_reaches_only_the_labelled_nodes_by_name(self) -> None:
        [role] = self.by_kind["ClusterRole"]
        self.assertEqual(role["metadata"]["name"], "node-cache-pruner-github-runner-platform-nodes")
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["nodes"], "resourceNames": ["node-a", "node-b"], "verbs": ["get", "patch"]}])
        [binding] = self.by_kind["ClusterRoleBinding"]
        self.assertEqual(binding["roleRef"]["name"], role["metadata"]["name"])

    def test_with_no_labelled_nodes_there_is_no_node_rule_rather_than_one_for_every_node(self) -> None:
        documents = render(github_runner_arc_labelled_nodes=[])
        self.assertEqual([document["kind"] for document in documents if document["kind"] in ("ClusterRole", "ClusterRoleBinding")], [])

    def test_the_daemonset_runs_only_on_the_labelled_nodes_and_tolerates_every_taint(self) -> None:
        self.assertEqual(self.pod["nodeSelector"], {"example/ci": "true"})
        self.assertEqual(self.pod["tolerations"], [{"operator": "Exists"}])

    def test_the_pruner_is_told_the_limits_and_the_caches_that_are_wanted(self) -> None:
        env = {item["name"]: item.get("value") for item in self.container["env"]}
        self.assertEqual({key: env[key] for key in ("LIMIT_GIB", "DRAIN_FACTOR", "CHECK_SECONDS", "WANTED_CACHES")}, {"LIMIT_GIB": "20", "DRAIN_FACTOR": "2", "CHECK_SECONDS": "300", "WANTED_CACHES": "arc-a/main arc-b/builders"})
        self.assertEqual(render(github_runner_arc_node_cache_size_limit_gib=5, github_runner_arc_node_cache_drain_factor=3, github_runner_arc_node_cache_prune_seconds=60)[-1]["spec"]["template"]["spec"]["containers"][0]["env"][1:4], [{"name": "LIMIT_GIB", "value": "5"}, {"name": "DRAIN_FACTOR", "value": "3"}, {"name": "CHECK_SECONDS", "value": "60"}])

    def test_the_container_has_the_least_privilege_it_needs(self) -> None:
        self.assertEqual(self.container["securityContext"], {"runAsUser": 0, "runAsNonRoot": False, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"], "add": ["DAC_OVERRIDE"]}})
        mounts = {mount["name"]: mount["mountPath"] for mount in self.container["volumeMounts"]}
        self.assertEqual(mounts, {"node-cache": "/host-node-cache", "tmp": "/tmp"})

    def test_the_cache_root_is_the_hostpath_the_pods_directories_sit_under(self) -> None:
        volumes = {volume["name"]: volume for volume in self.pod["volumes"]}
        self.assertEqual(volumes["node-cache"]["hostPath"], {"path": "/var/lib/github-runner/node-cache", "type": "DirectoryOrCreate"})
        self.assertEqual(render(github_runner_arc_node_cache_host_path="/srv/ci/cache")[-1]["spec"]["template"]["spec"]["volumes"][0]["hostPath"]["path"], "/srv/ci/cache")

    def test_the_daemonset_comes_last_so_its_account_and_permissions_exist_first(self) -> None:
        self.assertEqual(self.documents[-1]["kind"], "DaemonSet")
        self.assertEqual(self.documents[0]["kind"], "ServiceAccount")


class DecisionTest(unittest.TestCase):
    def test_a_profile_with_a_node_cache_wants_the_pruner(self) -> None:
        self.assertEqual(decide(["arc-a/main"], existing=0), {"wanted": True, "targets": ["arc-a/main"]})

    def test_only_profiles_with_the_capability_are_cache_profiles(self) -> None:
        self.assertEqual(decide(["arc-a/main", "arc-b/other"], existing=0, with_cache=["arc-b/other"]), {"wanted": True, "targets": ["arc-b/other"]})

    def test_with_no_node_cache_anywhere_and_no_pruner_installed_it_is_not_wanted(self) -> None:
        self.assertEqual(decide(["arc-a/main"], existing=0, with_cache=[]), {"wanted": False, "targets": []})

    def test_an_installed_pruner_stays_to_clear_the_directories_a_removed_capability_left(self) -> None:
        self.assertEqual(decide(["arc-a/main"], existing=1, with_cache=[]), {"wanted": True, "targets": []})

    def test_a_size_limit_of_zero_removes_it_whatever_is_installed(self) -> None:
        self.assertEqual(decide(["arc-a/main"], existing=1, github_runner_arc_node_cache_size_limit_gib=0)["wanted"], False)


class InstallTasksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tasks = yaml.safe_load((ROLE / "tasks" / "install_node_cache_pruner.yml").read_text())

    def task(self, prefix: str) -> dict[str, Any]:
        [task] = [task for task in self.tasks if task["name"].startswith(prefix)]
        return task

    def test_it_looks_for_an_installed_pruner_before_it_decides(self) -> None:
        names = [task["name"] for task in self.tasks]
        self.assertLess(names.index("Node cache: look for a pruner already installed"), names.index("Node cache: decide what the pruner needs"))

    def test_a_wanted_pruner_needs_the_node_label_key(self) -> None:
        assertion = self.task("Node cache: the pruner needs the nodes")
        self.assertEqual(assertion["when"], "github_runner_arc_node_cache_pruner_wanted | bool")
        self.assertEqual(assertion["ansible.builtin.assert"]["that"], "github_runner_arc_node_label_key | length > 0")

    def test_the_pull_secret_is_chosen_here_not_inherited_from_the_platform_install(self) -> None:
        block = self.task("Node cache: install the pruner")["block"]
        self.assertIn("choose_platform_pull_secrets.yml", [task.get("ansible.builtin.include_tasks") for task in block])

    def test_not_wanted_removes_the_daemonset_account_and_cluster_permissions(self) -> None:
        removal = self.task("Node cache: remove the pruner when it is not wanted")
        self.assertEqual(removal["when"], "not (github_runner_arc_node_cache_pruner_wanted | bool)")
        self.assertEqual(removal["kubernetes.core.k8s"]["state"], "absent")
        kinds = [item["kind"] for item in removal["loop"]]
        self.assertEqual(kinds, ["DaemonSet", "ClusterRoleBinding", "ClusterRole", "ServiceAccount"])

    def kept_targets(self, wanted: bool) -> list[str]:
        """Evaluate the targets whose pruner access is kept, taken from the task's own variable, for a pruner that is or is not wanted."""
        task = self.task("Node cache: remove the pruner's access")
        self.assertEqual(task["kubernetes.core.k8s"]["state"], "absent")
        expression = task["vars"]["github_runner_arc_node_cache_kept_targets"]
        with tempfile.TemporaryDirectory() as work:
            template, output = Path(work) / "kept.j2", Path(work) / "kept.json"
            template.write_text("{{ (" + expression.strip().removeprefix("{{").removesuffix("}}") + ") | to_json }}")
            completed = ansible(KEPT, kept_template=str(template), kept_output=str(output), github_runner_arc_node_cache_pruner_wanted=wanted, github_runner_arc_node_cache_profiles=PROFILES)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            kept: list[str] = json.loads(output.read_text())
            return kept

    def test_a_wanted_pruner_keeps_the_access_of_profiles_with_a_cache_and_removes_the_rest(self) -> None:
        self.assertEqual(self.kept_targets(True), ["arc-a/main", "arc-b/builders"])

    def test_the_access_of_every_profile_is_removed_when_the_pruner_is_not_wanted(self) -> None:
        self.assertEqual(self.kept_targets(False), [])

    def test_the_platform_install_and_the_pruner_share_the_pull_secret_choice(self) -> None:
        platform = (ROLE / "tasks" / "install_platform.yml").read_text()
        self.assertIn("choose_platform_pull_secrets.yml", platform)
        self.assertNotIn("github_runner_arc_platform_image_pull_secrets: >-", platform)


class ProfilesFilterTest(unittest.TestCase):
    def test_it_lists_profiles_carrying_a_node_cache_capability_in_order(self) -> None:
        profiles = [{"namespace": "a", "release": "r1", "target": "a/r1", "capabilities": ["node"]}, {"namespace": "b", "release": "r2", "target": "b/r2", "capabilities": ["node", "pnpm-store"]}, {"namespace": "c", "release": "r3", "target": "c/r3", "capabilities": ["pnpm-store"]}]
        self.assertEqual(arc.arc_node_cache_profiles(profiles, CATALOGUE), [{"namespace": "b", "release": "r2", "target": "b/r2"}, {"namespace": "c", "release": "r3", "target": "c/r3"}])

    def test_it_lists_none_when_no_profile_names_one(self) -> None:
        self.assertEqual(arc.arc_node_cache_profiles([{"namespace": "a", "release": "r", "target": "a/r", "capabilities": []}], CATALOGUE), [])


if __name__ == "__main__":
    unittest.main()
