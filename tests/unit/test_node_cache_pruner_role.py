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
INSTALL = ROOT / "tests" / "node_cache_pruner" / "install.yml"
CAPABILITIES_RENDER = ROOT / "tests" / "capabilities" / "render.yml"

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

    def test_the_roles_and_bindings_carry_the_label_the_install_finds_them_by(self) -> None:
        for kind in ("Role", "RoleBinding"):
            for document in self.by_kind[kind]:
                self.assertEqual(document["metadata"]["labels"]["app.kubernetes.io/name"], "node-cache-pruner")

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


# A stand-in for the kubernetes.core modules the install tasks call: it appends the module name and its arguments to the file named by STUB_LOG, reports no change and finds no resources, so the tasks' control flow runs and what they ask the cluster for can be read.
STUB_MODULE = """from ansible.module_utils.basic import AnsibleModule
import json, os

ARGUMENTS = ("api_version", "apply", "definition", "kind", "label_selectors", "name", "namespace", "state", "wait")


def main():
    module = AnsibleModule(argument_spec={key: {"type": "raw"} for key in ARGUMENTS}, supports_check_mode=True)
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write(json.dumps({"module": MODULE, **module.params}) + "\\n")
    found = json.loads(os.environ.get("STUB_INFO", "{}")).get(module.params.get("kind"), []) if MODULE == "k8s_info" else []
    module.exit_json(changed=False, resources=found)


main()
"""

# What the role's own defaults leave to the includer.
INSTALL_VARIABLES = {
    "github_runner_arc_capabilities": CATALOGUE,
    "github_runner_arc_node_label_key": "example/ci",
    "github_runner_arc_node_label_value": "true",
    "github_runner_arc_pull_secret_in_use": False,
    "github_runner_arc_manage_image_pull_secret": False,
    "github_runner_arc_image_pull_secret_source": "none",
    "github_runner_arc_image_pull_secret_name": "pull",
}


def access(kind: str, namespace: str, release: str) -> dict[str, Any]:
    """A Role or RoleBinding of the pruner as the cluster returns it."""
    return {"kind": kind, "metadata": {"namespace": namespace, "name": f"node-cache-pruner-{release}"}}


def install(fleet_targets: list[str], with_cache: list[str] | None = None, existing: list[dict[str, Any]] | None = None, **role_vars: Any) -> list[dict[str, Any]]:
    """Run the role's pruner install tasks against the stand-in kubernetes.core, and return the module calls it recorded. Each fleet profile carries the pnpm-store capability when its target is in with_cache (all of them when it is None)."""
    profiles = [{"namespace": t.split("/")[0], "release": t.split("/")[1], "target": t, "capabilities": ["pnpm-store"] if with_cache is None or t in with_cache else ["node"]} for t in fleet_targets]
    with tempfile.TemporaryDirectory() as work:
        modules = Path(work) / "stubs" / "ansible_collections" / "kubernetes" / "core" / "plugins" / "modules"
        modules.mkdir(parents=True)
        for module in ("k8s", "k8s_info"):
            (modules / f"{module}.py").write_text(STUB_MODULE.replace("MODULE", repr(module)))
        log = Path(work) / "calls.jsonl"
        extra = Path(work) / "extra.json"
        extra.write_text(json.dumps({**INSTALL_VARIABLES, "github_runner_arc_fleet": {"profiles": profiles}, **role_vars}))
        env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": os.pathsep.join([str(Path(work) / "stubs"), str(ROOT / "playbooks" / "collections")]), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_HOME": str(Path(work) / "home"), "STUB_LOG": str(log), "STUB_INFO": json.dumps({kind: [item for item in existing or [] if item["kind"] == kind] for kind in ("Role", "RoleBinding")})}
        completed = subprocess.run([os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(INSTALL), "-e", f"@{extra}"], env=env, cwd=work, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def removals(calls: list[dict[str, Any]]) -> list[tuple[str, str | None, str]]:
    """The (kind, namespace, name) of every resource the calls ask to have removed."""
    return [(call["kind"], call.get("namespace"), call["name"]) for call in calls if call.get("state") == "absent"]


def applied(calls: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """The (kind, name) of every resource the calls ask to have present, in order."""
    return [(call["definition"]["kind"], call["definition"]["metadata"]["name"]) for call in calls if call.get("definition")]


class InstallTasksTest(unittest.TestCase):
    """The role's install tasks, run against a stand-in for kubernetes.core."""

    FLEET = ["arc-a/main", "arc-b/builders", "arc-c/plain"]

    def test_the_cluster_wide_pod_access_an_earlier_release_granted_is_removed_whether_or_not_the_pruner_is_wanted(self) -> None:
        stale = [("ClusterRoleBinding", None, "node-cache-pruner"), ("ClusterRole", None, "node-cache-pruner")]
        for wanted in (True, False):
            with self.subTest(wanted=wanted):
                calls = install(self.FLEET, with_cache=None if wanted else [])
                self.assertEqual([removal for removal in removals(calls) if removal[1] is None], stale)

    def test_the_stale_access_is_removed_before_anything_is_installed(self) -> None:
        calls = install(self.FLEET)
        self.assertEqual((calls[0]["state"], calls[0]["kind"]), ("absent", "ClusterRoleBinding"))

    def test_a_wanted_pruner_installs_the_account_a_role_and_binding_per_fleet_profile_and_the_daemonset_last(self) -> None:
        calls = install(self.FLEET, with_cache=["arc-a/main"])
        installed = [item for item in applied(calls) if item[0] != "Namespace"]
        self.assertEqual(installed[0], ("ServiceAccount", "node-cache-pruner"))
        self.assertEqual(installed[-1], ("DaemonSet", "node-cache-pruner"))
        self.assertEqual(sorted(name for kind, name in installed if kind == "Role"), ["node-cache-pruner-builders", "node-cache-pruner-main", "node-cache-pruner-plain"])
        self.assertEqual(sorted(name for kind, name in installed if kind == "RoleBinding"), ["node-cache-pruner-builders", "node-cache-pruner-main", "node-cache-pruner-plain"])
        self.assertEqual([removal for removal in removals(calls) if removal[1] is not None], [])

    def test_the_namespaces_of_every_fleet_profile_are_created_for_the_roles(self) -> None:
        namespaces = {call["definition"]["metadata"]["name"] for call in install(self.FLEET) if (call.get("definition") or {}).get("kind") == "Namespace"}
        self.assertEqual(namespaces, {"github-runner-platform", "arc-a", "arc-b", "arc-c"})

    def test_a_wanted_pruner_without_the_node_label_key_fails_rather_than_running_everywhere(self) -> None:
        with self.assertRaises(AssertionError) as caught:
            install(self.FLEET, github_runner_arc_node_label_key="")
        self.assertIn("github_runner_arc_node_label_key must be set", str(caught.exception))

    def test_not_wanted_removes_the_daemonset_and_account_then_every_binding_and_role_the_cluster_has(self) -> None:
        existing = [access(kind, namespace, release) for kind in ("Role", "RoleBinding") for namespace, release in (("arc-a", "main"), ("arc-c", "plain"), ("arc-gone", "old"))]
        calls = install(self.FLEET, with_cache=[], existing=existing)
        namespaced = [removal for removal in removals(calls) if removal[1] is not None]
        self.assertEqual(namespaced[:2], [("DaemonSet", "github-runner-platform", "node-cache-pruner"), ("ServiceAccount", "github-runner-platform", "node-cache-pruner")])
        self.assertEqual(namespaced[2:], [(kind, namespace, f"node-cache-pruner-{release}") for kind in ("RoleBinding", "Role") for namespace, release in (("arc-a", "main"), ("arc-c", "plain"), ("arc-gone", "old"))])
        self.assertEqual(applied(calls), [])

    def test_a_wanted_pruner_removes_the_access_of_a_profile_that_has_left_the_fleet_and_keeps_the_fleets(self) -> None:
        existing = [access(kind, namespace, release) for kind in ("Role", "RoleBinding") for namespace, release in (("arc-a", "main"), ("arc-gone", "old"))]
        calls = install(self.FLEET, existing=existing)
        self.assertEqual([removal for removal in removals(calls) if removal[1] is not None], [("RoleBinding", "arc-gone", "node-cache-pruner-old"), ("Role", "arc-gone", "node-cache-pruner-old")])

    def test_a_wanted_pruner_with_nothing_stale_removes_no_access(self) -> None:
        existing = [access(kind, "arc-a", "main") for kind in ("Role", "RoleBinding")]
        self.assertEqual([removal for removal in removals(install(self.FLEET, existing=existing)) if removal[1] is not None], [])

    def test_the_pruners_access_is_looked_up_by_its_label_in_every_namespace(self) -> None:
        calls = install(self.FLEET)
        lookups = [call for call in calls if call["module"] == "k8s_info"]
        self.assertEqual([call["kind"] for call in lookups], ["Role", "RoleBinding"])
        self.assertTrue(all(call["namespace"] is None for call in lookups))
        self.assertTrue(all(call["label_selectors"] == ["app.kubernetes.io/name=node-cache-pruner"] for call in lookups))

    def test_a_size_limit_of_zero_removes_it_like_no_cache_at_all(self) -> None:
        calls = install(self.FLEET, github_runner_arc_node_cache_size_limit_gib=0)
        self.assertEqual(applied(calls), [])
        self.assertIn(("DaemonSet", "github-runner-platform", "node-cache-pruner"), removals(calls))

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
        bad = ("/var/lib", "/var", "/opt", "/srv", "/mnt", "/data", "/data/cache", "/etc/ci/node-cache", "/var/lib/kubelet/node-cache", "/tmp/ci/node-cache", "relative/path/node-cache",
               "/var/lib/etcd", "/var/lib/dpkg", "/var/lib/apt", "/var/lib/systemd", "/var/lib/cni", "/var/lib/kubernetes", "/var/lib/k0s", "/var/lib/calico", "/var/lib/containers", "/var/lib/snapd", "/srv/a/b", "/mnt/a/b",
               "/node-cache", "/opt/node-cache", "/var/lib/github-runner/node-cache/", "/var/lib/github-runner/node-cache//", "/var/lib//github-runner/node-cache", "/var/lib/github-runner/node-cachex", "/var/lib/github-runner/../etc/node-cache", "//var/lib/github-runner/node-cache")
        for path in bad:
            with self.subTest(path):
                self.assertNotEqual(self.problems(github_runner_arc_node_cache_host_path=path), [], path)

    def test_a_dedicated_directory_of_a_data_area_is_valid(self) -> None:
        for path in ("/var/lib/github-runner/node-cache", "/opt/ci/node-cache", "/srv/runners/ci-node-cache", "/mnt/disk/node-cache", "/var/lib/x/a1-node-cache"):
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


class HostPathRootTest(unittest.TestCase):
    """arc_node_cache_host_path_errors, directly: the final component, the deny-list as prefixes, and case."""

    def test_the_last_component_must_be_node_cache_or_a_lower_case_name_ending_in_it(self) -> None:
        for name in ("x-node-cache-", " node-cache", "...node-cache", "node-cache.", "Node-Cache", "xnode-cache", "-node-cache", "a_node-cache", "node-cachex", ".node-cache", "a b-node-cache", "A-node-cache"):
            with self.subTest(name):
                self.assertNotEqual(arc.arc_node_cache_host_path_errors(f"/var/lib/github-runner/{name}"), [], name)
        for name in ("node-cache", "x-node-cache", "ci-runners-node-cache", "a1-b2-node-cache"):
            with self.subTest(name):
                self.assertEqual(arc.arc_node_cache_host_path_errors(f"/var/lib/github-runner/{name}"), [], name)

    def test_node_directories_are_refused_as_prefixes_however_deep_the_cache_sits(self) -> None:
        for directory in ("/var/lib/etcd", "/var/lib/kubelet", "/var/lib/rancher", "/var/lib/containerd", "/var/lib/docker", "/var/lib/dpkg", "/var/lib/apt", "/var/lib/systemd", "/var/lib/cni", "/var/lib/kubernetes", "/var/lib/k0s", "/var/lib/calico", "/var/lib/containers", "/var/lib/snapd", "/etc", "/usr/local", "/tmp", "/var/log"):
            for suffix in ("/node-cache", "/deep/er/node-cache"):
                with self.subTest(directory + suffix):
                    self.assertNotEqual(arc.arc_node_cache_host_path_errors(directory + suffix), [], directory + suffix)

    def test_the_deny_list_is_checked_without_regard_to_case(self) -> None:
        for path in ("/VAR/LIB/ETCD/node-cache", "/Var/Lib/Kubelet/x/node-cache", "/ETC/ci/node-cache"):
            with self.subTest(path):
                self.assertNotEqual(arc.arc_node_cache_host_path_errors(path), [], path)

    def test_a_directory_next_to_a_denied_one_is_not_refused_for_sharing_its_prefix(self) -> None:
        self.assertEqual(arc.arc_node_cache_host_path_errors("/var/lib/etcdish/node-cache"), [])
        self.assertEqual(arc.arc_node_cache_host_path_errors("/var/lib/github-runner/docker-node-cache"), [])


class DocumentedExampleTest(unittest.TestCase):
    def example(self) -> dict[str, Any]:
        readme = (ROLE / "README.md").read_text()
        blocks = [block for block in readme.split("```yaml\n")[1:] if "pnpm-store:" in block]
        assert blocks, "the README has no pnpm-store example"
        document: dict[str, Any] = yaml.safe_load(blocks[0].split("```")[0])
        return document

    def test_the_readmes_pnpm_example_validates_and_renders_into_a_pod(self) -> None:
        catalogue = self.example()["github_runner_arc_capabilities"]
        profile = arc.arc_profiles([{"name": "Example", "image": "ghcr.io/example/runner:1", "scale_set_profiles": [{"max_runners": 1, "capabilities": ["pnpm-store"]}]}], require_app_id=False)["profiles"][0]
        self.assertEqual(arc.arc_capability_errors([profile], catalogue), [])
        values = {"template": {"spec": {"containers": [{"name": "runner", "command": ["/home/runner/run.sh"]}]}}}
        result = arc.arc_capability_pod(values, ["pnpm-store"], catalogue, "/opt/hostedtoolcache", "/opt/sysroot", "/node-cache", "/var/lib/github-runner/node-cache/arc/main", 1001, 1001, "/etc/github-runner/hooks", "job-started")
        self.assertEqual(result["errors"], [])

    def test_the_example_turns_the_global_virtual_store_on_because_the_text_says_symbolic_links_need_it(self) -> None:
        readme = (ROLE / "README.md").read_text()
        self.assertIn("global virtual store", readme)
        env = {item["name"]: item["value"] for item in arc.arc_capability_pod({"template": {"spec": {"containers": [{"name": "runner"}]}}}, ["pnpm-store"], self.example()["github_runner_arc_capabilities"], "/a", "/b", "/node-cache", "/var/lib/x/node-cache/a/b", 1001, 1001, "/h", "c")["values"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["pnpm_config_enable_global_virtual_store"], "true")
        self.assertEqual(env["pnpm_config_store_dir"], "/node-cache/pnpm")


class PathConsistencyTest(unittest.TestCase):
    """The pods' volume path and the pruner's idea of it, both built from github_runner_arc_node_cache_host_path, whatever way it is written."""

    def pod_host_path(self, base: str) -> tuple[str, dict[str, Any]]:
        profile = arc.arc_profiles([{"name": "Example", "image": "ghcr.io/example/runner:1", "scale_set_profiles": [{"max_runners": 1, "capabilities": ["pnpm-store"]}]}], require_app_id=False)["profiles"][0]
        with tempfile.TemporaryDirectory() as work:
            output = Path(work) / "rendered.json"
            completed = ansible(CAPABILITIES_RENDER, profile=profile, github_runner_arc_capabilities=CATALOGUE, github_runner_arc_node_cache_host_path=base, render_output=str(output))
            assert completed.returncode == 0, completed.stdout + completed.stderr
            [volume] = [volume for volume in json.loads(output.read_text())["values"]["template"]["spec"]["volumes"] if volume["name"] == "node-cache"]
            path: str = volume["hostPath"]["path"]
            return path, profile

    def test_the_pods_directory_is_the_pruners_root_joined_with_the_namespace_and_release_however_the_base_is_written(self) -> None:
        for base in ("/var/lib/x/node-cache", "/var/lib/x/node-cache/", "/var/lib//x///node-cache//"):
            with self.subTest(base):
                path, profile = self.pod_host_path(base)
                [daemonset] = [document for document in render(github_runner_arc_node_cache_host_path=base) if document["kind"] == "DaemonSet"]
                env = {item["name"]: item.get("value") for item in daemonset["spec"]["template"]["spec"]["containers"][0]["env"]}
                self.assertEqual(path, f"{env['HOST_CACHE_ROOT']}/{profile['namespace']}/{profile['release']}")
                self.assertEqual(path, f"/var/lib/x/node-cache/{profile['namespace']}/{profile['release']}")
                self.assertEqual(daemonset["spec"]["template"]["spec"]["volumes"][0]["hostPath"]["path"], "/var/lib/x/node-cache")


class ProfilesFilterTest(unittest.TestCase):
    def test_it_lists_profiles_carrying_a_node_cache_capability_in_order(self) -> None:
        profiles = [{"namespace": "a", "release": "r1", "target": "a/r1", "capabilities": ["node"]}, {"namespace": "b", "release": "r2", "target": "b/r2", "capabilities": ["node", "pnpm-store"]}, {"namespace": "c", "release": "r3", "target": "c/r3", "capabilities": ["pnpm-store"]}]
        self.assertEqual(arc.arc_node_cache_profiles(profiles, CATALOGUE), [{"namespace": "b", "release": "r2", "target": "b/r2"}, {"namespace": "c", "release": "r3", "target": "c/r3"}])

    def test_it_lists_none_when_no_profile_names_one(self) -> None:
        self.assertEqual(arc.arc_node_cache_profiles([{"namespace": "a", "release": "r", "target": "a/r", "capabilities": []}], CATALOGUE), [])


if __name__ == "__main__":
    unittest.main()
