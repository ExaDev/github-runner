"""Tests for scale-set profile capabilities: the catalogue checks and the pod changes in plugins/filter/arc.py, the values the role renders through tasks/render_scale_set_values.yml (run by ansible-playbook from tests/capabilities/render.yml), and the job-started hook dispatcher in roles/github_runner_arc/files/job-started.sh, run with the real bash."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "filter" / "arc.py"
RENDER = ROOT / "tests" / "capabilities" / "render.yml"
DISPATCHER = ROOT / "roles" / "github_runner_arc" / "files" / "job-started.sh"
_spec = importlib.util.spec_from_file_location("arc_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
arc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arc)

TOOL_CACHE = "/opt/hostedtoolcache"
SYSROOT = "/opt/sysroot"
HOOKS_DIR = "/etc/github-runner/hooks"
CONFIGMAP = "github-runner-job-started"
NODE_IMAGE = "ghcr.io/example/toolcache-node:22.11.0"
CATALOGUE: dict[str, Any] = {
    "node": {"image": NODE_IMAGE},
    "terraform": {"image": "ghcr.io/example/toolcache-terraform@sha256:" + "0" * 64, "path": "terraform/1.9.8"},
    "mpi": {"sysroot_image": "ghcr.io/example/sysroot-openmpi:5.0.11", "env": {"OPAL_PREFIX": SYSROOT}},
    "libpq": {"sysroot_image": "ghcr.io/example/sysroot-libpq@sha256:" + "1" * 64},
    "git-identity": {"job_started_hook": "git config --global user.name example\n"},
    "proxy": {"job_started_hook": "echo HTTPS_PROXY=http://proxy.example:3128 >> \"$GITHUB_ENV\"\n"},
}
# The role's default values template's runner container, as it renders for a profile with no values_file.
DEFAULT_VALUES: dict[str, Any] = {"minRunners": 0, "maxRunners": 2, "template": {"spec": {"containers": [{"name": "runner", "command": ["/home/runner/run.sh"]}]}}}


def expanded(profile: dict[str, Any]) -> dict[str, Any]:
    """Return the one profile arc_profiles expands from profile, failing on any error."""
    result = arc.arc_profiles([{"name": "Example", "image": "ghcr.io/example/runner:1", "scale_set_profiles": [profile]}], require_app_id=False)
    assert result["errors"] == [], result["errors"]
    return result["profiles"][0]


def pod(values: dict[str, Any], names: list[str]) -> dict[str, Any]:
    """Add names' capabilities from CATALOGUE to values with the role's default settings."""
    result: dict[str, Any] = arc.arc_capability_pod(values, names, CATALOGUE, TOOL_CACHE, SYSROOT, 1001, 1001, HOOKS_DIR, CONFIGMAP)
    return result


def runner(values: dict[str, Any]) -> dict[str, Any]:
    """Return the runner container from rendered values."""
    container: dict[str, Any] = next(container for container in values["template"]["spec"]["containers"] if container["name"] == "runner")
    return container


class ProfileCapabilitiesTest(unittest.TestCase):
    def test_a_profile_carries_its_capabilities_in_order(self) -> None:
        self.assertEqual(expanded({"max_runners": 1, "capabilities": ["terraform", "node"]})["capabilities"], ["terraform", "node"])

    def test_a_profile_without_capabilities_has_none(self) -> None:
        self.assertEqual(expanded({"max_runners": 1})["capabilities"], [])

    def test_capabilities_must_be_a_list_of_distinct_names(self) -> None:
        for capabilities, message in (("node", "must be a list of capability names"), (["node", ""], "must be a list of capability names"), (["node", "node"], "names node more than once")):
            with self.subTest(capabilities=capabilities):
                result = arc.arc_profiles([{"name": "Example", "image": "i:1", "scale_set_profiles": [{"max_runners": 1, "capabilities": capabilities}]}], require_app_id=False)
                self.assertTrue(any(message in error for error in result["errors"]), result["errors"])


class CatalogueTest(unittest.TestCase):
    def errors(self, catalogue: Any, capabilities: list[str] | None = None) -> list[str]:
        result: list[str] = arc.arc_capability_errors([expanded({"max_runners": 1, "capabilities": capabilities or []})], catalogue)
        return result

    def test_a_valid_catalogue_and_profile_have_no_errors(self) -> None:
        self.assertEqual(self.errors(CATALOGUE, list(CATALOGUE)), [])

    def test_a_profile_naming_an_undefined_capability_is_an_error(self) -> None:
        self.assertEqual(self.errors(CATALOGUE, ["node", "python"]), ["Example: capability 'python' is not defined in github_runner_arc_capabilities"])

    def test_bad_entries_are_errors(self) -> None:
        cases = {
            "an image floating on latest": ({"node": {"image": "ghcr.io/example/node:latest"}}, "must be pinned"),
            "an untagged image": ({"node": {"image": "ghcr.io/example/node"}}, "must be pinned"),
            "both kinds at once": ({"node": {"image": NODE_IMAGE, "job_started_hook": "true"}}, "exactly one of image"),
            "neither kind": ({"node": {}}, "exactly one of image"),
            "an unknown key": ({"node": {"image": NODE_IMAGE, "version": "22"}}, "unknown key 'version'"),
            "a path on a hook": ({"setup": {"job_started_hook": "true", "path": "node/22.11.0"}}, "only for a tool-cache capability"),
            "a path with no version": ({"node": {"image": NODE_IMAGE, "path": "node"}}, "must be <tool>/<version>"),
            "a path leaving the tool cache": ({"node": {"image": NODE_IMAGE, "path": "node/../.."}}, "must be <tool>/<version>"),
            "an empty hook": ({"setup": {"job_started_hook": "  "}}, "non-empty script"),
            "a name that is not a DNS label": ({"Node_22": {"image": NODE_IMAGE}}, "a capability name must be"),
            "a name too long for its init container": ({"n" * 53: {"image": NODE_IMAGE}}, "a capability name must be"),
            "an entry that is not a mapping": ({"node": NODE_IMAGE}, "must be a mapping"),
        }
        for case, (catalogue, message) in cases.items():
            with self.subTest(case):
                errors = self.errors(catalogue)
                self.assertTrue(any(message in error for error in errors), errors)

    def test_bad_system_library_entries_are_errors(self) -> None:
        image = "ghcr.io/example/sysroot-openmpi:5.0.11"
        cases = {
            "a sysroot image floating on latest": ({"mpi": {"sysroot_image": "ghcr.io/example/sysroot-openmpi:latest"}}, "sysroot_image must be pinned"),
            "an untagged sysroot image": ({"mpi": {"sysroot_image": "ghcr.io/example/sysroot-openmpi"}}, "sysroot_image must be pinned"),
            "a tool image and a sysroot image at once": ({"mpi": {"image": NODE_IMAGE, "sysroot_image": image}}, "exactly one of image"),
            "env on a tool-cache capability": ({"node": {"image": NODE_IMAGE, "env": {"A": "1"}}}, "env is only for a system-library capability"),
            "env on a hook": ({"setup": {"job_started_hook": "true", "env": {"A": "1"}}}, "env is only for a system-library capability"),
            "a path on a system-library capability": ({"mpi": {"sysroot_image": image, "path": "mpi/5"}}, "only for a tool-cache capability"),
            "env that is not a mapping": ({"mpi": {"sysroot_image": image, "env": ["OPAL_PREFIX=/opt/sysroot"]}}, "must be a mapping of variable name to value"),
            "a variable name that is not one": ({"mpi": {"sysroot_image": image, "env": {"OPAL-PREFIX": "/opt/sysroot"}}}, "is not a variable name"),
            "a variable starting with a digit": ({"mpi": {"sysroot_image": image, "env": {"1X": "y"}}}, "is not a variable name"),
            "a search path the role sets": ({"mpi": {"sysroot_image": image, "env": {"LD_LIBRARY_PATH": "/x"}}}, "LD_LIBRARY_PATH is set by the role itself"),
            "PATH": ({"mpi": {"sysroot_image": image, "env": {"PATH": "/x"}}}, "PATH is set by the role itself"),
            "the tool cache variable": ({"mpi": {"sysroot_image": image, "env": {"RUNNER_TOOL_CACHE": "/x"}}}, "RUNNER_TOOL_CACHE is set by the role itself"),
            "a number": ({"mpi": {"sysroot_image": image, "env": {"OMPI_MCA_rmaps_base_oversubscribe": 1}}}, "must be a single-line string"),
            "a value with a line break, which would add a second GITHUB_ENV line": ({"mpi": {"sysroot_image": image, "env": {"A": "x\nLD_PRELOAD=/evil.so"}}}, "must be a single-line string"),
        }
        for case, (catalogue, message) in cases.items():
            with self.subTest(case):
                errors = self.errors(catalogue)
                self.assertTrue(any(message in error for error in errors), errors)

    def test_two_system_libraries_setting_one_variable_differently_in_a_profile_is_an_error(self) -> None:
        catalogue = {"a": {"sysroot_image": "ghcr.io/example/a:1", "env": {"SHARED": "one"}}, "b": {"sysroot_image": "ghcr.io/example/b:1", "env": {"SHARED": "two"}}, "c": {"sysroot_image": "ghcr.io/example/c:1", "env": {"SHARED": "one"}}}
        self.assertEqual(self.errors(catalogue, ["a", "b"]), ["Example: capabilities 'a' and 'b' set SHARED to different values"])
        # The same value twice agrees, and capabilities that are defined but not in the same profile never conflict.
        self.assertEqual(self.errors(catalogue, ["a", "c"]), [])
        self.assertEqual(self.errors(catalogue, ["b"]), [])

    def test_a_digest_pins_an_image(self) -> None:
        self.assertEqual(self.errors({"node": {"image": "ghcr.io/example/node@sha256:" + "a" * 64}}), [])

    def test_a_catalogue_that_is_not_a_mapping_is_an_error(self) -> None:
        self.assertEqual(self.errors(["node"]), ["github_runner_arc_capabilities must be a mapping of capability name to capability"])


class CapabilityPodTest(unittest.TestCase):
    def test_no_capabilities_leaves_the_values_alone(self) -> None:
        self.assertEqual(pod(DEFAULT_VALUES, []), {"errors": [], "values": DEFAULT_VALUES, "hooks": {}})

    def test_a_tool_cache_capability_adds_an_init_container_copying_into_the_runners_tool_cache(self) -> None:
        result = pod(DEFAULT_VALUES, ["node"])
        self.assertEqual(result["errors"], [])
        spec = result["values"]["template"]["spec"]
        self.assertEqual(
            spec["initContainers"],
            [
                {
                    "name": "capability-node",
                    "image": NODE_IMAGE,
                    "command": ["/bin/sh", "-c", 'cp -R /toolcache/. "$1"/', "copy-tool-cache", TOOL_CACHE],
                    "securityContext": {"runAsUser": 1001, "runAsGroup": 1001, "runAsNonRoot": True, "allowPrivilegeEscalation": False},
                    "volumeMounts": [{"name": "tool-cache", "mountPath": TOOL_CACHE}],
                }
            ],
        )
        self.assertEqual(spec["volumes"], [{"name": "tool-cache", "emptyDir": {}}])
        self.assertEqual(runner(result["values"])["env"], [{"name": "RUNNER_TOOL_CACHE", "value": TOOL_CACHE}])
        self.assertEqual(runner(result["values"])["volumeMounts"], [{"name": "tool-cache", "mountPath": TOOL_CACHE}])
        # Without a hook or a tool path there is nothing for the runner to run at job start.
        self.assertEqual(result["hooks"], {})
        self.assertNotIn("ACTIONS_RUNNER_HOOK_JOB_STARTED", [variable["name"] for variable in runner(result["values"])["env"]])
        # The values given are not changed in place.
        self.assertNotIn("initContainers", DEFAULT_VALUES["template"]["spec"])

    def test_hooks_and_tool_paths_go_into_the_job_started_configmap_in_the_profiles_order(self) -> None:
        result = pod(DEFAULT_VALUES, ["proxy", "terraform", "node", "git-identity"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["hooks"], {"tool-paths": "terraform/1.9.8\n", "1-proxy.sh": CATALOGUE["proxy"]["job_started_hook"], "2-git-identity.sh": CATALOGUE["git-identity"]["job_started_hook"]})
        spec = result["values"]["template"]["spec"]
        self.assertEqual([container["name"] for container in spec["initContainers"]], ["capability-terraform", "capability-node"])
        hooks_volume = spec["volumes"][1]
        self.assertEqual(
            hooks_volume,
            {
                "name": "job-started-hooks",
                "configMap": {
                    "name": CONFIGMAP,
                    "defaultMode": 0o444,
                    "items": [
                        {"key": "job-started.sh", "path": "job-started.sh"},
                        {"key": "tool-paths", "path": "tool-paths"},
                        {"key": "1-proxy.sh", "path": "job-started.d/1-proxy.sh"},
                        {"key": "2-git-identity.sh", "path": "job-started.d/2-git-identity.sh"},
                    ],
                },
            },
        )
        self.assertIn({"name": "ACTIONS_RUNNER_HOOK_JOB_STARTED", "value": f"{HOOKS_DIR}/job-started.sh"}, runner(result["values"])["env"])
        self.assertIn({"name": "job-started-hooks", "mountPath": HOOKS_DIR, "readOnly": True}, runner(result["values"])["volumeMounts"])

    def test_a_hook_alone_needs_no_tool_cache(self) -> None:
        result = pod(DEFAULT_VALUES, ["git-identity"])
        spec = result["values"]["template"]["spec"]
        self.assertNotIn("initContainers", spec)
        self.assertEqual([volume["name"] for volume in spec["volumes"]], ["job-started-hooks"])
        self.assertEqual([variable["name"] for variable in runner(result["values"])["env"]], ["ACTIONS_RUNNER_HOOK_JOB_STARTED"])

    def test_the_values_own_init_containers_volumes_env_and_mounts_are_kept_ahead_of_the_capabilities(self) -> None:
        values = {
            "template": {
                "spec": {
                    "initContainers": [{"name": "own-init", "image": "busybox:1"}],
                    "volumes": [{"name": "own-volume", "emptyDir": {}}],
                    "containers": [{"name": "sidecar", "image": "s:1"}, {"name": "runner", "env": [{"name": "OWN", "value": "1"}], "volumeMounts": [{"name": "own-volume", "mountPath": "/own"}]}],
                }
            }
        }
        result = pod(values, ["node"])
        spec = result["values"]["template"]["spec"]
        self.assertEqual([container["name"] for container in spec["initContainers"]], ["own-init", "capability-node"])
        self.assertEqual([volume["name"] for volume in spec["volumes"]], ["own-volume", "tool-cache"])
        self.assertEqual([variable["name"] for variable in runner(result["values"])["env"]], ["OWN", "RUNNER_TOOL_CACHE"])
        self.assertEqual([mount["name"] for mount in runner(result["values"])["volumeMounts"]], ["own-volume", "tool-cache"])
        self.assertEqual(spec["containers"][0], {"name": "sidecar", "image": "s:1"})

    def test_values_that_cannot_carry_capabilities_are_errors(self) -> None:
        cases = {
            "a kubernetes container mode": ({**DEFAULT_VALUES, "containerMode": {"type": "kubernetes"}}, ["node"], "runs them in a separate pod"),
            "a kubernetes-novolume container mode": ({**DEFAULT_VALUES, "containerMode": {"type": "kubernetes-novolume"}}, ["git-identity"], "runs them in a separate pod"),
            "no runner container": ({"template": {"spec": {"containers": [{"name": "other"}]}}}, ["node"], "define the runner container"),
            "a runner that sets RUNNER_TOOL_CACHE itself": ({"template": {"spec": {"containers": [{"name": "runner", "env": [{"name": "RUNNER_TOOL_CACHE", "value": "/x"}]}]}}}, ["node"], "already sets RUNNER_TOOL_CACHE"),
            "a runner that sets its own job-started hook": ({"template": {"spec": {"containers": [{"name": "runner", "env": [{"name": "ACTIONS_RUNNER_HOOK_JOB_STARTED", "value": "/x.sh"}]}]}}}, ["proxy"], "already sets ACTIONS_RUNNER_HOOK_JOB_STARTED"),
            "a volume already named tool-cache": ({"template": {"spec": {"volumes": [{"name": "tool-cache", "emptyDir": {}}], "containers": [{"name": "runner"}]}}}, ["node"], "volume named tool-cache"),
        }
        for case, (values, names, message) in cases.items():
            with self.subTest(case):
                result = pod(values, names)
                self.assertTrue(any(message in error for error in result["errors"]), result["errors"])
                self.assertIs(result["values"], values)

    def test_a_system_library_capability_copies_its_sysroot_and_the_hook_extends_the_search_paths(self) -> None:
        result = pod(DEFAULT_VALUES, ["mpi"])
        self.assertEqual(result["errors"], [])
        spec = result["values"]["template"]["spec"]
        self.assertEqual(
            spec["initContainers"],
            [
                {
                    "name": "capability-mpi",
                    "image": CATALOGUE["mpi"]["sysroot_image"],
                    "command": ["/bin/sh", "-c", 'cp -R /sysroot/. "$1"/', "copy-sysroot", SYSROOT],
                    "securityContext": {"runAsUser": 1001, "runAsGroup": 1001, "runAsNonRoot": True, "allowPrivilegeEscalation": False},
                    "volumeMounts": [{"name": "sysroot", "mountPath": SYSROOT}],
                }
            ],
        )
        self.assertEqual([volume["name"] for volume in spec["volumes"]], ["sysroot", "job-started-hooks"])
        self.assertEqual(runner(result["values"])["volumeMounts"], [{"name": "sysroot", "mountPath": SYSROOT}, {"name": "job-started-hooks", "mountPath": HOOKS_DIR, "readOnly": True}])
        # The search paths are set by the hook at job start, never on the container, whose PATH would replace the image's own.
        self.assertEqual(runner(result["values"])["env"], [{"name": "ACTIONS_RUNNER_HOOK_JOB_STARTED", "value": f"{HOOKS_DIR}/job-started.sh"}])
        self.assertEqual(
            result["hooks"],
            {
                "sysroot-env": "PATH+=/opt/sysroot/bin\nLD_LIBRARY_PATH+=/opt/sysroot/lib\nLIBRARY_PATH+=/opt/sysroot/lib\nCPATH+=/opt/sysroot/include\nPKG_CONFIG_PATH+=/opt/sysroot/lib/pkgconfig\nCMAKE_PREFIX_PATH+=/opt/sysroot\nOPAL_PREFIX=/opt/sysroot\n",
            },
        )
        self.assertIn({"key": "sysroot-env", "path": "sysroot-env"}, spec["volumes"][1]["configMap"]["items"])

    def test_every_kind_together_keeps_the_profiles_order_and_shares_one_sysroot(self) -> None:
        result = pod(DEFAULT_VALUES, ["libpq", "node", "git-identity", "mpi", "terraform"])
        self.assertEqual(result["errors"], [])
        spec = result["values"]["template"]["spec"]
        self.assertEqual([container["name"] for container in spec["initContainers"]], ["capability-libpq", "capability-node", "capability-mpi", "capability-terraform"])
        self.assertEqual([container["volumeMounts"][0]["name"] for container in spec["initContainers"]], ["sysroot", "tool-cache", "sysroot", "tool-cache"])
        self.assertEqual([volume["name"] for volume in spec["volumes"]], ["tool-cache", "sysroot", "job-started-hooks"])
        self.assertEqual([item["path"] for item in spec["volumes"][2]["configMap"]["items"]], ["job-started.sh", "tool-paths", "sysroot-env", "job-started.d/1-git-identity.sh"])
        self.assertEqual([variable["name"] for variable in runner(result["values"])["env"]], ["RUNNER_TOOL_CACHE", "ACTIONS_RUNNER_HOOK_JOB_STARTED"])
        self.assertTrue(result["hooks"]["sysroot-env"].endswith("CMAKE_PREFIX_PATH+=/opt/sysroot\nOPAL_PREFIX=/opt/sysroot\n"))

    def test_the_sysroot_follows_its_configured_path(self) -> None:
        result = arc.arc_capability_pod(DEFAULT_VALUES, ["mpi"], {"mpi": {"sysroot_image": "ghcr.io/example/m:1"}}, TOOL_CACHE, "/srv/libs/", 1001, 1001, HOOKS_DIR, CONFIGMAP)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["values"]["template"]["spec"]["initContainers"][0]["command"][-1], "/srv/libs/")
        self.assertEqual(result["hooks"]["sysroot-env"].splitlines()[:2], ["PATH+=/srv/libs/bin", "LD_LIBRARY_PATH+=/srv/libs/lib"])

    def test_a_sysroot_path_that_cannot_carry_a_sysroot_is_an_error(self) -> None:
        cases = {
            "a relative path": ("opt/sysroot", "must be an absolute directory"),
            "the root": ("/", "must be an absolute directory"),
            "a colon, which would split the search paths": ("/opt/sys:root", "must be an absolute directory"),
            "the tool cache": (TOOL_CACHE, "overlaps the tool cache path"),
            "inside the tool cache": (f"{TOOL_CACHE}/sysroot", "overlaps the tool cache path"),
            "around the hooks directory": ("/etc/github-runner", "overlaps the job-started hooks directory"),
        }
        for case, (path, message) in cases.items():
            with self.subTest(case):
                result = arc.arc_capability_pod(DEFAULT_VALUES, ["mpi"], CATALOGUE, TOOL_CACHE, path, 1001, 1001, HOOKS_DIR, CONFIGMAP)
                self.assertTrue(any(message in error for error in result["errors"]), result["errors"])
                self.assertIs(result["values"], DEFAULT_VALUES)

    def test_the_sysroot_path_is_not_checked_for_a_profile_without_a_system_library(self) -> None:
        self.assertEqual(arc.arc_capability_pod(DEFAULT_VALUES, ["node"], CATALOGUE, TOOL_CACHE, "relative", 1001, 1001, HOOKS_DIR, CONFIGMAP)["errors"], [])

    def test_a_volume_already_named_sysroot_is_an_error(self) -> None:
        values = {"template": {"spec": {"volumes": [{"name": "sysroot", "emptyDir": {}}], "containers": [{"name": "runner"}]}}}
        result = pod(values, ["libpq"])
        self.assertIn("the values already define a volume named sysroot, which capabilities add", result["errors"])

    def test_dind_mode_keeps_the_capabilities_beside_the_charts_own_containers(self) -> None:
        # In dind mode the chart builds the runner and dind containers itself but keeps the values' init containers, volumes (other than work) and the runner's env and mounts, so the capabilities are added the same way.
        result = pod({**DEFAULT_VALUES, "containerMode": {"type": "dind"}}, ["node"])
        self.assertEqual(result["errors"], [])
        self.assertEqual([container["name"] for container in result["values"]["template"]["spec"]["initContainers"]], ["capability-node"])


class RenderedValuesTest(unittest.TestCase):
    """The values the role installs, rendered through its own tasks file, so the wiring between the filter, the overlay and the hook data is covered too."""

    def render(self, profile: dict[str, Any]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as work:
            output = Path(work) / "rendered.json"
            extra = Path(work) / "extra.json"
            extra.write_text(json.dumps({"profile": expanded(profile), "github_runner_arc_capabilities": CATALOGUE, "render_output": str(output)}))
            env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_HOME": work, "ANSIBLE_LOCAL_TEMP": work}
            completed = subprocess.run([os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(RENDER), "-e", f"@{extra}"], env=env, cwd=work, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            rendered: dict[str, Any] = json.loads(output.read_text())
            return rendered

    def test_the_role_renders_the_capabilities_into_the_release_values_and_the_hook_configmap(self) -> None:
        rendered = self.render({"max_runners": 3, "capabilities": ["node", "terraform", "git-identity"]})
        values = rendered["values"]
        self.assertEqual(values["maxRunners"], 3)
        self.assertEqual([container["name"] for container in values["template"]["spec"]["initContainers"]], ["capability-node", "capability-terraform"])
        self.assertEqual(runner(values)["command"], ["/home/runner/run.sh"])
        self.assertEqual({variable["name"]: variable["value"] for variable in runner(values)["env"]}, {"RUNNER_TOOL_CACHE": TOOL_CACHE, "ACTIONS_RUNNER_HOOK_JOB_STARTED": f"{HOOKS_DIR}/job-started.sh"})
        job_started = rendered["job_started"]
        self.assertEqual(set(job_started), {"job-started.sh", "tool-paths", "1-git-identity.sh"})
        # The dispatcher comes from the role's own file (the file lookup drops its final newline).
        self.assertEqual(job_started["job-started.sh"], DISPATCHER.read_text().rstrip("\n"))
        self.assertEqual(job_started["tool-paths"], "terraform/1.9.8\n")

    def test_the_role_renders_a_system_library_capability_at_its_sysroot_path(self) -> None:
        rendered = self.render({"max_runners": 1, "capabilities": ["mpi"]})
        values = rendered["values"]
        self.assertEqual(values["template"]["spec"]["initContainers"][0]["volumeMounts"], [{"name": "sysroot", "mountPath": SYSROOT}])
        self.assertIn({"name": "sysroot", "mountPath": SYSROOT}, runner(values)["volumeMounts"])
        self.assertEqual(set(rendered["job_started"]), {"job-started.sh", "sysroot-env"})
        self.assertIn("LD_LIBRARY_PATH+=/opt/sysroot/lib\n", rendered["job_started"]["sysroot-env"])

    def test_a_profile_without_capabilities_renders_as_before(self) -> None:
        rendered = self.render({"max_runners": 3, "container_mode": "dind"})
        self.assertEqual(rendered["job_started"], {})
        self.assertNotIn("initContainers", rendered["values"]["template"]["spec"])
        self.assertNotIn("env", runner(rendered["values"]))


class DispatcherTest(unittest.TestCase):
    """The job-started hook, run as the runner runs it (bash -e <path>) from a directory laid out as the ConfigMap volume mounts it."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.hooks = self.dir / "hooks"
        (self.hooks / "job-started.d").mkdir(parents=True)
        shutil.copy(DISPATCHER, self.hooks / "job-started.sh")
        self.cache = self.dir / "toolcache"
        self.github_path = self.dir / "github_path"
        self.github_path.touch()
        self.log = self.dir / "log"

    def cache_tool(self, tool: str, version: str, arch: str, subdirectory: str = "") -> Path:
        directory = self.cache / tool / version / arch / subdirectory
        directory.mkdir(parents=True)
        (self.cache / tool / version / f"{arch}.complete").touch()
        return directory

    def run_hook(self) -> subprocess.CompletedProcess[str]:
        # Without the GITHUB_ENV of a workflow these tests may themselves run in, so nothing is written to it.
        env = {key: value for key, value in os.environ.items() if key != "GITHUB_ENV"}
        env.update({"RUNNER_TOOL_CACHE": str(self.cache), "GITHUB_PATH": str(self.github_path), "HOOK_LOG": str(self.log)})
        return subprocess.run(["bash", "-e", str(self.hooks / "job-started.sh")], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def test_tool_paths_are_put_on_path_for_the_cached_architecture(self) -> None:
        terraform = self.cache_tool("terraform", "1.9.8", "arm64")
        node_bin = self.cache_tool("node", "22.11.0", "x64", "bin")
        (self.hooks / "tool-paths").write_text("terraform/1.9.8\nnode/22.11.0/bin\n")
        completed = self.run_hook()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.github_path.read_text().splitlines(), [str(terraform).rstrip("/"), str(node_bin)])

    def test_scripts_run_in_name_order_after_the_tool_paths(self) -> None:
        self.cache_tool("terraform", "1.9.8", "x64")
        (self.hooks / "tool-paths").write_text("terraform/1.9.8\n")
        (self.hooks / "job-started.d" / "2-second.sh").write_text('echo second >> "$HOOK_LOG"\n')
        (self.hooks / "job-started.d" / "1-first.sh").write_text('echo "first $(grep -c "" "$GITHUB_PATH")" >> "$HOOK_LOG"\n')
        completed = self.run_hook()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.log.read_text().split("\n")[:2], ["first 1", "second"])

    def test_with_nothing_mounted_beyond_the_dispatcher_it_does_nothing(self) -> None:
        completed = self.run_hook()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.github_path.read_text(), "")

    def test_a_failing_script_fails_the_job_and_stops_the_rest(self) -> None:
        (self.hooks / "job-started.d" / "1-broken.sh").write_text("false\necho after-false >> \"$HOOK_LOG\"\n")
        (self.hooks / "job-started.d" / "2-later.sh").write_text('echo later >> "$HOOK_LOG"\n')
        completed = self.run_hook()
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_a_tool_path_that_is_not_cached_fails_the_job(self) -> None:
        (self.hooks / "tool-paths").write_text("terraform/1.9.8\n")
        completed = self.run_hook()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("terraform/1.9.8 is not in the tool cache", completed.stderr)

    def test_a_tool_cached_for_two_architectures_fails_the_job(self) -> None:
        self.cache_tool("terraform", "1.9.8", "x64")
        self.cache_tool("terraform", "1.9.8", "arm64")
        (self.hooks / "tool-paths").write_text("terraform/1.9.8\n")
        completed = self.run_hook()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("more than one architecture", completed.stderr)

    def test_the_sysroot_env_prepends_search_paths_and_sets_variables_for_the_steps_and_later_scripts(self) -> None:
        github_env = self.dir / "github_env"
        github_env.touch()
        (self.hooks / "sysroot-env").write_text("PATH+=/opt/sysroot/bin\nLD_LIBRARY_PATH+=/opt/sysroot/lib\nCPATH+=/opt/sysroot/include\nOPAL_PREFIX=/opt/sysroot\nEXAMPLE_FLAGS=a=b c\n")
        (self.hooks / "job-started.d" / "1-check.sh").write_text('echo "$LD_LIBRARY_PATH|$CPATH|$OPAL_PREFIX|$EXAMPLE_FLAGS|${PATH%%:*}" >> "$HOOK_LOG"\n')
        env = {key: value for key, value in os.environ.items() if key not in {"CPATH", "OPAL_PREFIX", "EXAMPLE_FLAGS"}}
        env.update({"RUNNER_TOOL_CACHE": str(self.cache), "GITHUB_PATH": str(self.github_path), "GITHUB_ENV": str(github_env), "HOOK_LOG": str(self.log), "LD_LIBRARY_PATH": "/usr/local/lib"})
        completed = subprocess.run(["bash", "-e", str(self.hooks / "job-started.sh")], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.github_path.read_text(), "/opt/sysroot/bin\n")
        # An existing value is kept after the sysroot's directory, an unset one is not given an empty entry, and a value may hold '=' and spaces.
        self.assertEqual(github_env.read_text().splitlines(), ["LD_LIBRARY_PATH=/opt/sysroot/lib:/usr/local/lib", "CPATH=/opt/sysroot/include", "OPAL_PREFIX=/opt/sysroot", "EXAMPLE_FLAGS=a=b c"])
        self.assertEqual(self.log.read_text().splitlines(), ["/opt/sysroot/lib:/usr/local/lib|/opt/sysroot/include|/opt/sysroot|a=b c|/opt/sysroot/bin"])

    def test_the_sysroot_pkg_config_files_survive_a_step_that_replaces_pkg_config_path(self) -> None:
        # actions/setup-python exports PKG_CONFIG_PATH as its own directory alone (src/find-python.ts), so the sysroot's .pc files must be reachable through a variable it leaves alone.
        self.assertIsNotNone(shutil.which("pkg-config"), "this test needs pkg-config on PATH")
        sysroot = self.dir / "sysroot"
        (sysroot / "lib" / "pkgconfig").mkdir(parents=True)
        (sysroot / "lib" / "pkgconfig" / "fixture.pc").write_text("Name: fixture\nDescription: fixture\nVersion: 1\n")
        python = self.dir / "python" / "lib" / "pkgconfig"
        python.mkdir(parents=True)
        github_env = self.dir / "github_env"
        github_env.touch()
        (self.hooks / "sysroot-env").write_text(f"PKG_CONFIG_PATH+={sysroot}/lib/pkgconfig\n")
        env = {key: value for key, value in os.environ.items() if key not in {"PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR"}}
        env.update({"RUNNER_TOOL_CACHE": str(self.cache), "GITHUB_PATH": str(self.github_path), "GITHUB_ENV": str(github_env)})
        completed = subprocess.run(["bash", "-e", str(self.hooks / "job-started.sh")], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # The next steps start from the hook's GITHUB_ENV, then setup-python overwrites PKG_CONFIG_PATH.
        step_env = {key: value for key, value in env.items() if key != "GITHUB_ENV"}
        step_env.update(line.split("=", 1) for line in github_env.read_text().splitlines())
        self.assertEqual(subprocess.run(["pkg-config", "--exists", "fixture"], env=step_env).returncode, 0)
        step_env["PKG_CONFIG_PATH"] = str(python)
        self.assertEqual(subprocess.run(["pkg-config", "--exists", "fixture"], env=step_env).returncode, 0)
        # The distribution's own .pc files stay reachable, which a bare PKG_CONFIG_LIBDIR of the sysroot would lose.
        default_path = subprocess.run(["pkg-config", "--variable=pc_path", "pkg-config"], env=env, capture_output=True, text=True, check=True).stdout.strip()
        self.assertTrue(step_env["PKG_CONFIG_LIBDIR"].endswith(f":{default_path}"), step_env["PKG_CONFIG_LIBDIR"])

    def test_a_sysroot_env_without_github_env_fails_the_job(self) -> None:
        (self.hooks / "sysroot-env").write_text("OPAL_PREFIX=/opt/sysroot\n")
        completed = self.run_hook()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("GITHUB_ENV is not set", completed.stderr)

    def test_a_missing_subdirectory_fails_the_job(self) -> None:
        self.cache_tool("node", "22.11.0", "x64")
        (self.hooks / "tool-paths").write_text("node/22.11.0/bin\n")
        completed = self.run_hook()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("does not exist", completed.stderr)


if __name__ == "__main__":
    unittest.main()
