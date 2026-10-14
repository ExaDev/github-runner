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
# The fleet: the two cache profiles and one without a cache, as arc_profiles expands them (the template and the tasks read namespace, release and target).
FLEET = {"profiles": [*PROFILES, {"namespace": "arc-c", "release": "plain", "target": "arc-c/plain"}]}
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
        variables = {"github_runner_arc_fleet": FLEET, "github_runner_arc_node_cache_profiles": PROFILES, "github_runner_arc_node_label_key": "example/ci", "github_runner_arc_node_label_value": "true", **role_vars}
        completed = ansible(RENDER, pruner_output=str(output), **variables)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        documents: list[dict[str, Any]] = [document for document in yaml.safe_load_all(output.read_text()) if document]
        return documents


def decide(fleet_targets: list[str], **role_vars: Any) -> dict[str, Any]:
    """Run the decision for a fleet of the given profile targets, each with the pnpm-store capability when its target is listed in with_cache."""
    profiles = [{"namespace": target.split("/")[0], "release": target.split("/")[1], "target": target, "capabilities": ["pnpm-store" if target in role_vars.pop("with_cache", fleet_targets) else "node"]} for target in fleet_targets]
    with tempfile.TemporaryDirectory() as work:
        output = Path(work) / "decision.json"
        completed = ansible(DECIDE, decide_output=str(output), github_runner_arc_fleet={"profiles": profiles}, github_runner_arc_capabilities=CATALOGUE, **role_vars)
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

    def test_every_fleet_profile_gets_a_role_and_binding_in_its_own_namespace_named_for_its_release(self) -> None:
        # Including the profile without a cache, so the pruner can still list its pods while it clears a directory the removed capability left.
        roles = {(role["metadata"]["namespace"], role["metadata"]["name"]) for role in self.by_kind["Role"]}
        self.assertEqual(roles, {("arc-a", "node-cache-pruner-main"), ("arc-b", "node-cache-pruner-builders"), ("arc-c", "node-cache-pruner-plain")})
        bindings = {(binding["metadata"]["namespace"], binding["metadata"]["name"]) for binding in self.by_kind["RoleBinding"]}
        self.assertEqual(bindings, roles)
        for role in self.by_kind["Role"]:
            self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}])
        for binding in self.by_kind["RoleBinding"]:
            self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "node-cache-pruner", "namespace": "github-runner-platform"}])
            self.assertEqual(binding["roleRef"]["name"], binding["metadata"]["name"])

    def test_nothing_is_granted_cluster_wide(self) -> None:
        self.assertEqual(sorted(set(self.by_kind)), ["DaemonSet", "Role", "RoleBinding", "ServiceAccount"])

    def test_the_daemonset_runs_only_on_the_labelled_nodes_and_tolerates_every_taint(self) -> None:
        self.assertEqual(self.pod["nodeSelector"], {"example/ci": "true"})
        self.assertEqual(self.pod["tolerations"], [{"operator": "Exists"}])

    def test_the_pruner_is_told_the_limits_the_host_root_and_the_caches_that_are_wanted(self) -> None:
        env = {item["name"]: item.get("value") for item in self.container["env"]}
        self.assertEqual({key: env[key] for key in ("LIMIT_GIB", "HARD_LIMIT_FACTOR", "CHECK_SECONDS", "HOST_CACHE_ROOT", "WANTED_CACHES")}, {"LIMIT_GIB": "20", "HARD_LIMIT_FACTOR": "2", "CHECK_SECONDS": "300", "HOST_CACHE_ROOT": "/var/lib/github-runner/node-cache", "WANTED_CACHES": "arc-a/main arc-b/builders"})
        changed = render(github_runner_arc_node_cache_size_limit_gib=5, github_runner_arc_node_cache_hard_limit_factor=3, github_runner_arc_node_cache_prune_seconds=60, github_runner_arc_node_cache_host_path="/srv/ci/cache")[-1]["spec"]["template"]["spec"]["containers"][0]["env"]
        changed_env = {item["name"]: item.get("value") for item in changed}
        self.assertEqual({key: changed_env[key] for key in ("LIMIT_GIB", "HARD_LIMIT_FACTOR", "CHECK_SECONDS", "HOST_CACHE_ROOT")}, {"LIMIT_GIB": "5", "HARD_LIMIT_FACTOR": "3", "CHECK_SECONDS": "60", "HOST_CACHE_ROOT": "/srv/ci/cache"})

    def test_the_container_has_the_least_privilege_it_needs(self) -> None:
        self.assertEqual(self.container["securityContext"], {"runAsUser": 0, "runAsNonRoot": False, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"], "add": ["DAC_OVERRIDE", "FOWNER"]}})
        mounts = {mount["name"]: mount["mountPath"] for mount in self.container["volumeMounts"]}
        self.assertEqual(mounts, {"node-cache": "/host-node-cache", "tmp": "/tmp"})

    def test_the_cache_root_is_the_hostpath_the_pods_directories_sit_under(self) -> None:
        volumes = {volume["name"]: volume for volume in self.pod["volumes"]}
        self.assertEqual(volumes["node-cache"]["hostPath"], {"path": "/var/lib/github-runner/node-cache", "type": "DirectoryOrCreate"})

    def test_the_daemonset_comes_last_so_its_account_and_permissions_exist_first(self) -> None:
        self.assertEqual(self.documents[-1]["kind"], "DaemonSet")
        self.assertEqual(self.documents[0]["kind"], "ServiceAccount")


class DecisionTest(unittest.TestCase):
    def test_a_profile_with_a_node_cache_wants_the_pruner(self) -> None:
        self.assertEqual(decide(["arc-a/main"]), {"wanted": True, "targets": ["arc-a/main"]})

    def test_only_profiles_with_the_capability_are_cache_profiles(self) -> None:
        self.assertEqual(decide(["arc-a/main", "arc-b/other"], with_cache=["arc-b/other"]), {"wanted": True, "targets": ["arc-b/other"]})

    def test_with_no_node_cache_anywhere_it_is_not_wanted_so_it_follows_the_fleet(self) -> None:
        self.assertEqual(decide(["arc-a/main"], with_cache=[]), {"wanted": False, "targets": []})

    def test_a_size_limit_of_zero_means_it_is_not_wanted(self) -> None:
        self.assertEqual(decide(["arc-a/main"], github_runner_arc_node_cache_size_limit_gib=0)["wanted"], False)


class InstallTasksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tasks = yaml.safe_load((ROLE / "tasks" / "install_node_cache_pruner.yml").read_text())

    def task(self, prefix: str) -> dict[str, Any]:
        [task] = [task for task in self.tasks if task["name"].startswith(prefix)]
        return task

    def test_a_wanted_pruner_needs_the_node_label_key(self) -> None:
        assertion = self.task("Node cache: the pruner needs the nodes")
        self.assertEqual(assertion["when"], "github_runner_arc_node_cache_pruner_wanted | bool")
        self.assertEqual(assertion["ansible.builtin.assert"]["that"], "github_runner_arc_node_label_key | length > 0")

    def test_the_pull_secret_is_chosen_here_not_inherited_from_the_platform_install(self) -> None:
        block = self.task("Node cache: install the pruner")["block"]
        self.assertIn("choose_platform_pull_secrets.yml", [task.get("ansible.builtin.include_tasks") for task in block])

    def test_the_namespaces_of_every_fleet_profile_are_created_for_the_roles(self) -> None:
        block = self.task("Node cache: install the pruner")["block"]
        [namespaces] = [task for task in block if task["name"].startswith("Node cache: the namespaces")]
        self.assertIn("github_runner_arc_fleet.profiles", namespaces["loop"])

    def test_not_wanted_removes_the_daemonset_and_account_then_every_fleet_profiles_access(self) -> None:
        removal = self.task("Node cache: remove the pruner when it is not wanted")
        self.assertEqual(removal["when"], "not (github_runner_arc_node_cache_pruner_wanted | bool)")
        first, second = removal["block"]
        self.assertEqual([item["kind"] for item in first["loop"]], ["DaemonSet", "ServiceAccount"])
        self.assertEqual(first["kubernetes.core.k8s"]["state"], "absent")
        self.assertEqual(second["kubernetes.core.k8s"]["state"], "absent")
        self.assertIn("github_runner_arc_fleet.profiles", second["loop"])
        # Bindings go before the Roles they bind.
        self.assertTrue(second["loop"].startswith("{{ ['RoleBinding', 'Role']"), second["loop"])

    def test_there_is_no_lookup_of_a_pruner_already_installed_so_the_pruner_follows_the_fleet(self) -> None:
        self.assertEqual([task["name"] for task in self.tasks if "k8s_info" in str(task)], [])

    def test_the_platform_install_and_the_pruner_share_the_pull_secret_choice(self) -> None:
        platform = (ROLE / "tasks" / "install_platform.yml").read_text()
        self.assertIn("choose_platform_pull_secrets.yml", platform)
        self.assertNotIn("github_runner_arc_platform_image_pull_secrets: >-", platform)


class ValidationTest(unittest.TestCase):
    """The settings checks of tasks/validate.yml, evaluated through the role's own filters the way the role evaluates them."""

    @classmethod
    def setUpClass(cls) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "validate.yml").read_text())
        [task] = [task for task in tasks if task["name"] == "Fail on invalid node cache settings"]
        cls.expression: str = task["vars"]["github_runner_arc_node_cache_problems"]

    def problems(self, **role_vars: Any) -> list[str]:
        with tempfile.TemporaryDirectory() as work:
            template, output = Path(work) / "problems.j2", Path(work) / "problems.json"
            template.write_text("{{ (" + self.expression.strip().removeprefix("{{").removesuffix("}}") + ") | to_json }}")
            completed = ansible(KEPT, kept_template=str(template), kept_output=str(output), **role_vars)
            assert completed.returncode == 0, completed.stdout + completed.stderr
            found: list[str] = json.loads(output.read_text())
            return found

    def test_the_defaults_are_valid(self) -> None:
        self.assertEqual(self.problems(), [])

    def test_the_host_path_root_is_checked_whether_or_not_any_profile_has_a_node_cache(self) -> None:
        for path in ("/var/lib", "/var", "/opt", "/srv", "/mnt", "/data", "/data/cache", "/etc/ci/cache", "/var/lib/kubelet/cache", "/tmp/ci/cache", "relative/path/here"):
            with self.subTest(path):
                self.assertNotEqual(self.problems(github_runner_arc_node_cache_host_path=path), [], path)

    def test_a_dedicated_directory_of_a_data_area_is_valid(self) -> None:
        for path in ("/var/lib/github-runner/node-cache", "/opt/ci/node-cache", "/srv/runners/cache", "/mnt/disk/ci/cache"):
            with self.subTest(path):
                self.assertEqual(self.problems(github_runner_arc_node_cache_host_path=path), [], path)

    def test_the_mount_path_is_checked(self) -> None:
        for path in ("relative", "/", "/node:cache"):
            with self.subTest(path):
                self.assertNotEqual(self.problems(github_runner_arc_node_cache_path=path), [], path)

    def test_numeric_settings_out_of_range_are_errors(self) -> None:
        for name, value in (("github_runner_arc_node_cache_size_limit_gib", -1), ("github_runner_arc_node_cache_prune_seconds", 0), ("github_runner_arc_node_cache_hard_limit_factor", 0)):
            with self.subTest(name):
                self.assertNotEqual(self.problems(**{name: value}), [], name)
        self.assertEqual(self.problems(github_runner_arc_node_cache_size_limit_gib=0), [])


class ProfilesFilterTest(unittest.TestCase):
    def test_it_lists_profiles_carrying_a_node_cache_capability_in_order(self) -> None:
        profiles = [{"namespace": "a", "release": "r1", "target": "a/r1", "capabilities": ["node"]}, {"namespace": "b", "release": "r2", "target": "b/r2", "capabilities": ["node", "pnpm-store"]}, {"namespace": "c", "release": "r3", "target": "c/r3", "capabilities": ["pnpm-store"]}]
        self.assertEqual(arc.arc_node_cache_profiles(profiles, CATALOGUE), [{"namespace": "b", "release": "r2", "target": "b/r2"}, {"namespace": "c", "release": "r3", "target": "c/r3"}])

    def test_it_lists_none_when_no_profile_names_one(self) -> None:
        self.assertEqual(arc.arc_node_cache_profiles([{"namespace": "a", "release": "r", "target": "a/r", "capabilities": []}], CATALOGUE), [])


if __name__ == "__main__":
    unittest.main()
