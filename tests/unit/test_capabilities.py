"""Tests for scale-set profile capabilities: the catalogue checks and the pod changes in plugins/filter/arc.py, the values the role renders through tasks/render_scale_set_values.yml (run by ansible-playbook from tests/capabilities/render.yml), and the job-started hook dispatcher in roles/github_runner_arc/files/job-started.sh, run with the real bash."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

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
NODE_CACHE = "/node-cache"
NODE_CACHE_HOST = "/var/lib/example/node-cache/arc-runners/example-runners"
HOOKS_DIR = "/etc/github-runner/hooks"
CONFIGMAP = "github-runner-job-started"
NODE_IMAGE = "ghcr.io/example/toolcache-node:22.11.0"
BUSYBOX = "docker.io/library/busybox:1.37.0@sha256:" + "2" * 64
CATALOGUE: dict[str, Any] = {
    "node": {"image": NODE_IMAGE},
    "terraform": {"image": "ghcr.io/example/toolcache-terraform@sha256:" + "0" * 64, "path": "terraform/1.9.8"},
    "mpi": {"sysroot_image": "ghcr.io/example/sysroot-openmpi:5.0.11", "env": {"OPAL_PREFIX": SYSROOT}},
    "libpq": {"sysroot_image": "ghcr.io/example/sysroot-libpq@sha256:" + "1" * 64},
    "git-identity": {"job_started_hook": "git config --global user.name example\n"},
    "pnpm-store": {
        "node_cache": {"init_image": BUSYBOX, "dirs": {"pnpm_config_store_dir": "pnpm"}},
        "env": {"pnpm_config_enable_global_virtual_store": "true"},
    },
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
    result: dict[str, Any] = arc.arc_capability_pod(values, names, CATALOGUE, TOOL_CACHE, SYSROOT, NODE_CACHE, NODE_CACHE_HOST, 1001, 1001, HOOKS_DIR, CONFIGMAP, None)
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

    RESERVED = ("store-prepare", "store-publish", "store-sysroot", "store-tool-cache", "store-publish-externals")

    def test_a_capability_named_like_one_of_the_stores_init_containers_is_an_error_with_the_store_on_or_off(self) -> None:
        # capability-<name> would be the name of the store's own container, and the API server rejects a pod with two of one name.
        for name in self.RESERVED:
            for store in (True, False):
                with self.subTest(name=name, store=store):
                    errors: list[str] = arc.arc_capability_errors([expanded({"max_runners": 1, "capability_store": store, "capabilities": [name]})], {name: {"image": f"ghcr.io/example/x:1@sha256:{DIGEST_A}"}})
                    self.assertEqual(len(errors), 1, errors)
                    self.assertIn(f"github_runner_arc_capabilities.{name}", errors[0])
                    self.assertIn(f"capability-{name}", errors[0])
                    self.assertIn("capability store's own init containers", errors[0])

    def test_a_reserved_name_is_an_error_for_every_kind_of_capability_and_a_non_mapping_entry(self) -> None:
        for entry in ({"sysroot_image": f"ghcr.io/example/x:1@sha256:{DIGEST_A}"}, {"job_started_hook": "true"}, {"node_cache": {"init_image": BUSYBOX, "dirs": {"STORE": "pnpm"}}}, "not a mapping"):
            with self.subTest(entry=entry):
                errors = self.errors({"store-publish": entry})
                self.assertTrue(any("capability-store-publish" in error for error in errors), errors)

    def test_every_init_container_the_role_names_beside_the_capabilities_is_reserved(self) -> None:
        # Render everything the store adds (the preparation, the publishing, the merge, the tool-cache links and the dind externals' publishing): each name that is not capability-<a capability's name> is one a capability's container could clash with.
        spec = arc.arc_capability_store_externals(store_pod(DIND_VALUES, ["node", "mpi"])["values"], STORE, 1001, 1001)["values"]["template"]["spec"]
        names = {container["name"] for container in spec["initContainers"] if container["name"].startswith("capability-")} - {"capability-node", "capability-mpi"}
        self.assertEqual(names, {f"capability-{name}" for name in self.RESERVED})

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

    def test_bad_node_cache_entries_are_errors(self) -> None:
        init = BUSYBOX
        cases = {
            "a node_cache that is not a mapping": ({"store": {"node_cache": init}}, "must be a mapping with init_image and dirs"),
            "a floating init image": ({"store": {"node_cache": {"init_image": "docker.io/library/busybox:latest", "dirs": {"STORE": "pnpm"}}}}, "must be pinned"),
            "an init image pinned by a tag alone": ({"store": {"node_cache": {"init_image": "docker.io/library/busybox:1.37.0", "dirs": {"STORE": "pnpm"}}}}, "must be pinned by digest"),
            "an init image with a short digest": ({"store": {"node_cache": {"init_image": "docker.io/library/busybox:1.37.0@sha256:abc", "dirs": {"STORE": "pnpm"}}}}, "must be pinned by digest"),
            "an init image with a digest and more after it": ({"store": {"node_cache": {"init_image": BUSYBOX + "x", "dirs": {"STORE": "pnpm"}}}}, "must be pinned by digest"),
            "no init image": ({"store": {"node_cache": {"dirs": {"STORE": "pnpm"}}}}, "init_image"),
            "no dirs": ({"store": {"node_cache": {"init_image": init}}}, "non-empty mapping"),
            "an empty dirs": ({"store": {"node_cache": {"init_image": init, "dirs": {}}}}, "non-empty mapping"),
            "an unknown key": ({"store": {"node_cache": {"init_image": init, "dirs": {"STORE": "pnpm"}, "size": 1}}}, "unknown key 'size'"),
            "a variable name that is not one": ({"store": {"node_cache": {"init_image": init, "dirs": {"my-store": "pnpm"}}}}, "not an environment variable name"),
            "a runner variable": ({"store": {"node_cache": {"init_image": init, "dirs": {"RUNNER_TOOL_CACHE": "pnpm"}}}}, "may not set RUNNER_TOOL_CACHE"),
            "a subdirectory that leaves the cache": ({"store": {"node_cache": {"init_image": init, "dirs": {"STORE": "../x"}}}}, "must be one directory name"),
            "a subdirectory with a slash": ({"store": {"node_cache": {"init_image": init, "dirs": {"STORE": "a/b"}}}}, "must be one directory name"),
            "one subdirectory for two variables": ({"store": {"node_cache": {"init_image": init, "dirs": {"A": "pnpm", "B": "pnpm"}}}}, "more than one variable"),
            "a node cache and a tool image": ({"store": {"image": NODE_IMAGE, "node_cache": {"init_image": init, "dirs": {"A": "pnpm"}}}}, "exactly one of image"),
            "env on a tool image": ({"node": {"image": NODE_IMAGE, "env": {"A": "b"}}}, "only for a system-library capability"),
        }
        for case, (catalogue, message) in cases.items():
            with self.subTest(case):
                errors = self.errors(catalogue)
                self.assertTrue(any(message in error for error in errors), errors)

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
        result = arc.arc_capability_pod(DEFAULT_VALUES, ["mpi"], {"mpi": {"sysroot_image": "ghcr.io/example/m:1"}}, TOOL_CACHE, "/srv/libs/", NODE_CACHE, NODE_CACHE_HOST, 1001, 1001, HOOKS_DIR, CONFIGMAP, None)
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
                result = arc.arc_capability_pod(DEFAULT_VALUES, ["mpi"], CATALOGUE, TOOL_CACHE, path, NODE_CACHE, NODE_CACHE_HOST, 1001, 1001, HOOKS_DIR, CONFIGMAP, None)
                self.assertTrue(any(message in error for error in result["errors"]), result["errors"])
                self.assertIs(result["values"], DEFAULT_VALUES)

    def test_the_sysroot_path_is_not_checked_for_a_profile_without_a_system_library(self) -> None:
        self.assertEqual(arc.arc_capability_pod(DEFAULT_VALUES, ["node"], CATALOGUE, TOOL_CACHE, "relative", NODE_CACHE, NODE_CACHE_HOST, 1001, 1001, HOOKS_DIR, CONFIGMAP, None)["errors"], [])

    def test_a_volume_already_named_sysroot_is_an_error(self) -> None:
        values = {"template": {"spec": {"volumes": [{"name": "sysroot", "emptyDir": {}}], "containers": [{"name": "runner"}]}}}
        result = pod(values, ["libpq"])
        self.assertIn("the values already define a volume named sysroot, which capabilities add", result["errors"])

    def test_a_node_cache_capability_mounts_one_node_directory_and_prepares_its_subdirectories_as_the_runner_user(self) -> None:
        result = pod(DEFAULT_VALUES, ["pnpm-store"])
        self.assertEqual(result["errors"], [])
        spec = result["values"]["template"]["spec"]
        self.assertEqual(spec["volumes"], [{"name": "node-cache", "hostPath": {"path": NODE_CACHE_HOST, "type": "DirectoryOrCreate"}}])
        [init] = spec["initContainers"]
        self.assertEqual(init["name"], "capability-pnpm-store")
        self.assertEqual(init["image"], BUSYBOX)
        self.assertEqual(init["command"][-3:], ["1001:1001", "/node-cache", "/node-cache/pnpm"])
        # Explicit, so a pod-level non-root securityContext is overridden. Root needs DAC_OVERRIDE as well as CHOWN: the node directory is already the runner user's (the command gives it away, and an earlier pod or the pruner leaves it so), and root without it cannot create /node-cache/pnpm in it.
        self.assertEqual(init["securityContext"], {"runAsUser": 0, "runAsNonRoot": False, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "DAC_OVERRIDE"]}})
        self.assertEqual(init["volumeMounts"], [{"name": "node-cache", "mountPath": NODE_CACHE}])
        container = runner(result["values"])
        self.assertEqual(container["volumeMounts"], [{"name": "node-cache", "mountPath": NODE_CACHE}])
        self.assertEqual({variable["name"]: variable["value"] for variable in container["env"]}, {"pnpm_config_store_dir": "/node-cache/pnpm", "pnpm_config_enable_global_virtual_store": "true"})
        self.assertEqual(result["hooks"], {})

    def test_the_init_container_command_makes_and_gives_away_every_directory_it_names(self) -> None:
        init = pod(DEFAULT_VALUES, ["pnpm-store"])["values"]["template"]["spec"]["initContainers"][0]
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            (bin_dir / "chown").write_text('#!/bin/sh\necho "$@" >> "$CHOWN_LOG"\n')
            (bin_dir / "chown").chmod(0o755)
            # The container mounts the cache at /node-cache; here it is a directory of the test, so the command's paths are rewritten onto it.
            command = [argument.replace("/node-cache", str(root / "cache")) for argument in init["command"]]
            log = root / "chown.log"
            subprocess.run(command, check=True, env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CHOWN_LOG": str(log)}, capture_output=True)
            self.assertTrue((root / "cache" / "pnpm").is_dir())
            self.assertEqual(sorted(log.read_text().splitlines()), sorted([f"1001:1001 {root / 'cache'}", f"1001:1001 {root / 'cache' / 'pnpm'}"]))
            # Running it again over what exists changes nothing and does not fail.
            subprocess.run(command, check=True, env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CHOWN_LOG": str(log)}, capture_output=True)
            self.assertTrue((root / "cache" / "pnpm").is_dir())

    def test_a_node_cache_capability_shares_one_volume_with_every_capability_of_the_profile(self) -> None:
        catalogue = {**CATALOGUE, "yarn-cache": {"node_cache": {"init_image": BUSYBOX, "dirs": {"YARN_CACHE_FOLDER": "yarn"}}}}
        result = arc.arc_capability_pod(DEFAULT_VALUES, ["pnpm-store", "yarn-cache", "node"], catalogue, TOOL_CACHE, SYSROOT, NODE_CACHE, NODE_CACHE_HOST, 1001, 1001, HOOKS_DIR, CONFIGMAP, None)
        self.assertEqual(result["errors"], [])
        spec = result["values"]["template"]["spec"]
        self.assertEqual([volume["name"] for volume in spec["volumes"]], ["tool-cache", "node-cache"])
        self.assertEqual([container["name"] for container in spec["initContainers"]], ["capability-pnpm-store", "capability-yarn-cache", "capability-node"])
        self.assertEqual({variable["name"] for variable in runner(result["values"])["env"]}, {"RUNNER_TOOL_CACHE", "pnpm_config_store_dir", "pnpm_config_enable_global_virtual_store", "YARN_CACHE_FOLDER"})

    def test_a_profile_without_a_node_cache_capability_gets_no_node_volume_and_its_path_is_not_checked(self) -> None:
        result = arc.arc_capability_pod(DEFAULT_VALUES, ["node"], CATALOGUE, TOOL_CACHE, SYSROOT, "relative", "also/relative", 1001, 1001, HOOKS_DIR, CONFIGMAP, None)
        self.assertEqual(result["errors"], [])
        self.assertNotIn("node-cache", [volume["name"] for volume in result["values"]["template"]["spec"]["volumes"]])

    def test_node_cache_paths_that_cannot_carry_a_cache_are_errors(self) -> None:
        cases = {
            "a relative mount path": ("relative", NODE_CACHE_HOST, "absolute directory other than /"),
            "the root as mount path": ("/", NODE_CACHE_HOST, "absolute directory other than /"),
            "the tool cache's path": (TOOL_CACHE, NODE_CACHE_HOST, "overlaps the tool cache path"),
            "inside the sysroot": (f"{SYSROOT}/cache", NODE_CACHE_HOST, "overlaps the sysroot path"),
            "a colon in the mount path": ("/node:cache", NODE_CACHE_HOST, "no ':' or line break"),
            "a relative host directory": (NODE_CACHE, "var/lib/cache", "host directory must be an absolute path"),
            "a trailing slash on the host directory": (NODE_CACHE, NODE_CACHE_HOST + "/", "repeated '/' or a trailing '/'"),
            "a doubled slash in the host directory": (NODE_CACHE, "/var/lib//example/node-cache/arc/r", "repeated '/' or a trailing '/'"),
            "a colon in the host directory": (NODE_CACHE, "/var/lib/a:b/node-cache/arc/r", "without ':'"),
        }
        for case, (mount, host, message) in cases.items():
            with self.subTest(case):
                result = arc.arc_capability_pod(DEFAULT_VALUES, ["pnpm-store"], CATALOGUE, TOOL_CACHE, SYSROOT, mount, host, 1001, 1001, HOOKS_DIR, CONFIGMAP, None)
                self.assertTrue(any(message in error for error in result["errors"]), result["errors"])

    def test_two_node_cache_capabilities_setting_one_variable_differently_is_an_error(self) -> None:
        catalogue = {
            "a": {"node_cache": {"init_image": BUSYBOX, "dirs": {"STORE": "one"}}},
            "b": {"node_cache": {"init_image": BUSYBOX, "dirs": {"STORE": "two"}}},
        }
        result = arc.arc_capability_pod(DEFAULT_VALUES, ["a", "b"], catalogue, TOOL_CACHE, SYSROOT, NODE_CACHE, NODE_CACHE_HOST, 1001, 1001, HOOKS_DIR, CONFIGMAP, None)
        self.assertEqual(result["errors"], ["node-cache capabilities a and b set STORE to different values"])

    def test_a_volume_already_named_node_cache_is_an_error(self) -> None:
        values = {"template": {"spec": {"containers": [{"name": "runner"}], "volumes": [{"name": "node-cache", "emptyDir": {}}]}}}
        result = pod(values, ["pnpm-store"])
        self.assertEqual(result["errors"], ["the values already define a volume named node-cache, which capabilities add"])

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

    def test_the_role_renders_a_node_cache_capability_into_a_per_profile_node_directory(self) -> None:
        rendered = self.render({"max_runners": 1, "capabilities": ["pnpm-store"]})
        profile = expanded({"max_runners": 1})
        [volume] = rendered["values"]["template"]["spec"]["volumes"]
        self.assertEqual(volume["hostPath"], {"path": f"/var/lib/github-runner/node-cache/{profile['namespace']}/{profile['release']}", "type": "DirectoryOrCreate"})
        self.assertEqual(runner(rendered["values"])["volumeMounts"], [{"name": "node-cache", "mountPath": "/node-cache"}])
        self.assertEqual(rendered["job_started"], {})

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



DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_RUNNER = "d" * 64
# The store entries of those digests: each is named for the kind of payload it holds as well as the image's digest.
KEY_A = f"toolcache-{DIGEST_A}"
KEY_B = f"sysroot-{DIGEST_B}"
KEY_C = f"sysroot-{DIGEST_C}"
KEY_RUNNER = f"externals-{DIGEST_RUNNER}"
STORE = {"host_path": "/var/lib/example/capability-store", "path": "/opt/capability-store", "image": BUSYBOX}
STORE_CATALOGUE: dict[str, Any] = {
    "node": {"image": f"ghcr.io/example/toolcache-node:22@sha256:{DIGEST_A}"},
    "mpi": {"sysroot_image": f"ghcr.io/example/sysroot-openmpi:5@sha256:{DIGEST_B}", "env": {"OPAL_PREFIX": SYSROOT}},
    "libpq": {"sysroot_image": f"ghcr.io/example/sysroot-libpq:16@sha256:{DIGEST_C}"},
    "pnpm-store": CATALOGUE["pnpm-store"],
    "untagged": {"image": "ghcr.io/example/toolcache-go:1.23"},
}


def store_pod(values: dict[str, Any], names: list[str], catalogue: dict[str, Any] = STORE_CATALOGUE) -> dict[str, Any]:
    """Add names' capabilities with the capability store on."""
    result: dict[str, Any] = arc.arc_capability_pod(values, names, catalogue, TOOL_CACHE, SYSROOT, NODE_CACHE, NODE_CACHE_HOST, 1001, 1001, HOOKS_DIR, CONFIGMAP, STORE)
    return result


class CapabilityStoreProfileTest(unittest.TestCase):
    def test_a_profile_copies_into_each_pod_unless_it_turns_the_store_on(self) -> None:
        self.assertFalse(expanded({"max_runners": 1})["capability_store"])
        self.assertTrue(expanded({"max_runners": 1, "capability_store": True})["capability_store"])

    def test_a_capability_store_that_is_not_a_boolean_is_an_error(self) -> None:
        result = arc.arc_profiles([{"name": "Example", "image": "i:1", "scale_set_profiles": [{"max_runners": 1, "capability_store": "sometimes"}]}], require_app_id=False)
        self.assertTrue(any("capability_store must be a boolean" in error for error in result["errors"]), result["errors"])


class CapabilityStoreSettingsTest(unittest.TestCase):
    def test_a_dedicated_store_directory_is_accepted(self) -> None:
        self.assertEqual(arc.arc_capability_store_host_path_errors("/var/lib/github-runner/capability-store"), [])
        self.assertEqual(arc.arc_capability_store_host_path_errors("/srv/ci/runner-capability-store"), [])

    def test_a_store_directory_that_is_not_dedicated_is_refused(self) -> None:
        for path, message in (
            ("var/lib/x/capability-store", "absolute directory"),
            ("/var/lib//capability-store", "must not contain '//'"),
            ("/srv/capability-store", "at least 3 levels deep"),
            ("/var/lib/github-runner/store", "named capability-store"),
            ("/var/lib/kubelet/x/capability-store", "inside /var/lib/kubelet"),
        ):
            with self.subTest(path=path):
                errors = arc.arc_capability_store_host_path_errors(path)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(message, errors[0])

    def test_the_store_mount_path_must_be_an_absolute_directory(self) -> None:
        self.assertEqual(arc.arc_capability_store_path_errors("/opt/capability-store"), [])
        for path in ("relative", "/", "/a:b", 3):
            with self.subTest(path=path):
                self.assertEqual(len(arc.arc_capability_store_path_errors(path)), 1)

    def test_an_entry_is_named_for_its_images_digest_and_payload_kind_and_an_unpinned_image_has_no_name(self) -> None:
        self.assertEqual(arc.arc_capability_store_key(f"ghcr.io/example/x:1@sha256:{DIGEST_A}", "toolcache"), KEY_A)
        self.assertIsNone(arc.arc_capability_store_key("ghcr.io/example/x:1", "toolcache"))


class CapabilityStoreArgumentSpecTest(unittest.TestCase):
    """The role's argument specification declares the store's settings with the defaults the role has."""

    ROLE = ROOT / "roles" / "github_runner_arc"

    def test_every_capability_store_default_is_declared_with_its_default_and_the_profile_key_is_described(self) -> None:
        defaults = yaml.safe_load((self.ROLE / "defaults" / "main.yml").read_text())
        specification = yaml.safe_load((self.ROLE / "meta" / "argument_specs.yml").read_text())["argument_specs"]["main"]["options"]
        names = sorted(name for name in defaults if name.startswith("github_runner_arc_capability_store_"))
        self.assertEqual(names, ["github_runner_arc_capability_store_host_path", "github_runner_arc_capability_store_image", "github_runner_arc_capability_store_path"])
        for name in names:
            with self.subTest(name):
                self.assertEqual(specification[name]["default"], defaults[name])
                self.assertEqual(specification[name]["type"], "str")
        self.assertIn("capability_store", " ".join(specification["github_runner_arc_orgs"]["description"]))


class CapabilityStorePodTest(unittest.TestCase):
    def test_without_the_store_the_copies_stay_in_each_pod(self) -> None:
        result = arc.arc_capability_pod(DEFAULT_VALUES, ["mpi"], STORE_CATALOGUE, TOOL_CACHE, SYSROOT, NODE_CACHE, NODE_CACHE_HOST, 1001, 1001, HOOKS_DIR, CONFIGMAP, None)
        spec = result["values"]["template"]["spec"]
        self.assertEqual([volume["name"] for volume in spec["volumes"]], ["sysroot", "job-started-hooks"])
        self.assertNotIn("metadata", result["values"]["template"])

    def test_with_the_store_each_payload_is_copied_once_per_node_and_published_before_the_pod_uses_it(self) -> None:
        result = store_pod(DEFAULT_VALUES, ["node", "mpi", "libpq", "pnpm-store"])
        self.assertEqual(result["errors"], [])
        template = result["values"]["template"]
        spec = template["spec"]
        names = [container["name"] for container in spec["initContainers"]]
        self.assertEqual(names, ["capability-store-prepare", "capability-node", "capability-mpi", "capability-libpq", "capability-store-publish", "capability-store-sysroot", "capability-store-tool-cache", "capability-pnpm-store"])
        containers = {container["name"]: container for container in spec["initContainers"]}
        # The copy runs the capability's own image as the runner user, only when the store lacks the entry, into a name of its own under .incoming.
        copier = containers["capability-node"]
        self.assertEqual(copier["image"], STORE_CATALOGUE["node"]["image"])
        self.assertEqual(copier["command"][3:], ["copy-to-capability-store", "/opt/capability-store", KEY_A, "/toolcache", "capability-node"])
        self.assertEqual(copier["securityContext"], {"runAsUser": 1001, "runAsGroup": 1001, "runAsNonRoot": True, "allowPrivilegeEscalation": False})
        self.assertEqual(copier["env"], [{"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}])
        self.assertEqual(containers["capability-mpi"]["command"][6], "/sysroot")
        self.assertEqual(containers["capability-store-publish"]["command"][4:], ["/opt/capability-store", KEY_A, KEY_B, KEY_C])
        self.assertEqual(containers["capability-store-publish"]["image"], BUSYBOX)
        # Only the preparation runs as root, with the two capabilities it needs.
        self.assertEqual(containers["capability-store-prepare"]["securityContext"]["capabilities"], {"drop": ["ALL"], "add": ["CHOWN", "DAC_OVERRIDE"]})
        merged = containers["capability-store-sysroot"]["command"][5]
        self.assertRegex(merged, r"^merged-sysroot-[0-9a-f]{64}$")
        self.assertEqual(containers["capability-store-sysroot"]["command"][6:], [KEY_B, KEY_C])
        self.assertEqual(containers["capability-store-tool-cache"]["command"][4:], ["/opt/capability-store", TOOL_CACHE, KEY_A])
        self.assertEqual(containers["capability-store-tool-cache"]["volumeMounts"], [{"name": "capability-store", "mountPath": "/opt/capability-store", "readOnly": True}, {"name": "tool-cache", "mountPath": TOOL_CACHE}])
        volumes = {volume["name"]: volume for volume in spec["volumes"]}
        self.assertEqual(volumes["capability-store"], {"name": "capability-store", "hostPath": {"path": STORE["host_path"], "type": "DirectoryOrCreate"}})
        self.assertEqual(volumes["tool-cache"], {"name": "tool-cache", "emptyDir": {}})
        self.assertNotIn("sysroot", volumes)
        mounts = runner(result["values"])["volumeMounts"]
        self.assertIn({"name": "capability-store", "mountPath": f"/opt/capability-store/{KEY_A}", "subPath": KEY_A, "readOnly": True}, mounts)
        self.assertIn({"name": "capability-store", "mountPath": SYSROOT, "subPath": merged, "readOnly": True}, mounts)
        self.assertIn({"name": "tool-cache", "mountPath": TOOL_CACHE}, mounts)
        self.assertEqual(template["metadata"]["annotations"]["github-runner.exadev.io/capability-store-keys"], f"{KEY_A} {KEY_B} {KEY_C} {merged}")

    def store_mounts(self, names: list[str], catalogue: dict[str, Any] = STORE_CATALOGUE) -> list[dict[str, Any]]:
        """The runner container's mounts of the store volume."""
        result = store_pod(DEFAULT_VALUES, names, catalogue)
        self.assertEqual(result["errors"], [])
        return [mount for mount in runner(result["values"])["volumeMounts"] if mount["name"] == "capability-store"]

    def test_the_runner_container_mounts_only_the_entries_its_pod_uses_and_never_the_stores_root(self) -> None:
        mounts = self.store_mounts(["node", "mpi", "libpq"])
        merged = next(container for container in store_pod(DEFAULT_VALUES, ["node", "mpi", "libpq"])["values"]["template"]["spec"]["initContainers"] if container["name"] == "capability-store-sysroot")["command"][5]
        # The tool entry, where the tool tree's links point, and the merged sysroot; not the system libraries' own entries, which the merge hard-linked from.
        self.assertEqual(
            mounts,
            [
                {"name": "capability-store", "mountPath": f"/opt/capability-store/{KEY_A}", "subPath": KEY_A, "readOnly": True},
                {"name": "capability-store", "mountPath": SYSROOT, "subPath": merged, "readOnly": True},
            ],
        )
        for mount in mounts:
            self.assertEqual(mount["readOnly"], True)
            self.assertIn("subPath", mount)
            self.assertNotEqual(mount["mountPath"], "/opt/capability-store")

    def test_the_tool_trees_links_resolve_through_the_runner_containers_mounts(self) -> None:
        catalogue = {**STORE_CATALOGUE, "second": {"image": f"ghcr.io/example/toolcache-go:1@sha256:{DIGEST_B}"}}
        spec = store_pod(DEFAULT_VALUES, ["node", "second"], catalogue)["values"]["template"]["spec"]
        link = next(container for container in spec["initContainers"] if container["name"] == "capability-store-tool-cache")
        # The links point at <root>/<key>/..., so the runner container must mount exactly each of those keys' directories at that path.
        root, keys = link["command"][4], link["command"][6:]
        self.assertEqual(keys, [KEY_A, f"toolcache-{DIGEST_B}"])
        mounted = {mount["mountPath"]: mount["subPath"] for mount in runner({"template": {"spec": spec}})["volumeMounts"] if mount["name"] == "capability-store"}
        self.assertEqual(mounted, {f"{root}/{key}": key for key in keys})

    def test_a_profile_with_only_system_libraries_mounts_only_its_merged_sysroot(self) -> None:
        mounts = self.store_mounts(["mpi", "libpq"])
        self.assertEqual([mount["mountPath"] for mount in mounts], [SYSROOT])

    def test_the_init_containers_that_write_to_the_store_still_mount_its_root(self) -> None:
        spec = store_pod(DEFAULT_VALUES, ["node", "mpi"])["values"]["template"]["spec"]
        for container in spec["initContainers"]:
            if container["name"].startswith("capability-store-") or container["name"].startswith("capability-"):
                mounts = [mount for mount in container.get("volumeMounts", []) if mount["name"] == "capability-store"]
                self.assertEqual([mount["mountPath"] for mount in mounts], ["/opt/capability-store"], container["name"])

    def test_a_different_order_of_system_libraries_is_a_different_merged_sysroot(self) -> None:
        first = store_pod(DEFAULT_VALUES, ["mpi", "libpq"])["values"]["template"]["spec"]["initContainers"]
        second = store_pod(DEFAULT_VALUES, ["libpq", "mpi"])["values"]["template"]["spec"]["initContainers"]
        merged = [next(container for container in containers if container["name"] == "capability-store-sysroot")["command"][5] for containers in (first, second)]
        self.assertNotEqual(merged[0], merged[1])

    def merge_of(self, names: list[str], catalogue: dict[str, Any]) -> dict[str, Any]:
        """The sysroot merge's container for names."""
        result = store_pod(DEFAULT_VALUES, names, catalogue)
        self.assertEqual(result["errors"], [])
        return next(container for container in result["values"]["template"]["spec"]["initContainers"] if container["name"] == "capability-store-sysroot")

    def test_two_system_library_capabilities_on_one_image_merge_that_entry_once_into_the_entry_one_would(self) -> None:
        image = f"ghcr.io/example/libs:1@sha256:{DIGEST_B}"
        once = self.merge_of(["first"], {"first": {"sysroot_image": image}})
        twice = self.merge_of(["first", "second"], {"first": {"sysroot_image": image}, "second": {"sysroot_image": image}})
        self.assertEqual(twice["command"][5:], [once["command"][5], KEY_B])

    def test_a_system_library_entry_named_again_keeps_its_last_position_so_its_files_still_win(self) -> None:
        # a, b, a merges b's files and then a's, so a's win a shared file, as the per-pod copies in that order did: the entries are [b, a], never [a, b], which would let b's win.
        catalogue = {"a": {"sysroot_image": f"ghcr.io/example/a:1@sha256:{DIGEST_B}"}, "b": {"sysroot_image": f"ghcr.io/example/b:1@sha256:{DIGEST_C}"}, "again": {"sysroot_image": f"ghcr.io/example/a:1@sha256:{DIGEST_B}"}}
        merge = self.merge_of(["a", "b", "again"], catalogue)
        self.assertEqual(merge["command"][6:], [KEY_C, KEY_B])
        self.assertNotEqual(merge["command"][5], self.merge_of(["a", "b"], catalogue)["command"][5])

    def test_one_image_used_for_a_tool_cache_and_a_sysroot_is_two_entries_each_copied_from_its_own_payload(self) -> None:
        image = f"ghcr.io/example/both:1@sha256:{DIGEST_A}"
        result = store_pod(DEFAULT_VALUES, ["tools", "libraries"], {"tools": {"image": image}, "libraries": {"sysroot_image": image}})
        self.assertEqual(result["errors"], [])
        containers = {container["name"]: container for container in result["values"]["template"]["spec"]["initContainers"]}
        tools, libraries = containers["capability-tools"]["command"], containers["capability-libraries"]["command"]
        self.assertEqual((tools[4:6], tools[6]), (["/opt/capability-store", f"toolcache-{DIGEST_A}"], "/toolcache"))
        self.assertEqual((libraries[4:6], libraries[6]), (["/opt/capability-store", f"sysroot-{DIGEST_A}"], "/sysroot"))
        self.assertEqual(containers["capability-store-publish"]["command"][4:], ["/opt/capability-store", f"toolcache-{DIGEST_A}", f"sysroot-{DIGEST_A}"])
        self.assertEqual(containers["capability-store-sysroot"]["command"][6:], [f"sysroot-{DIGEST_A}"])
        self.assertEqual(containers["capability-store-tool-cache"]["command"][6:], [f"toolcache-{DIGEST_A}"])
        self.assertEqual(result["values"]["template"]["metadata"]["annotations"]["github-runner.exadev.io/capability-store-keys"].split()[:2], [f"toolcache-{DIGEST_A}", f"sysroot-{DIGEST_A}"])

    def test_a_runner_image_that_is_also_a_tool_image_has_a_separate_entry_for_the_dind_externals(self) -> None:
        values = store_pod(DIND_VALUES, ["runner-tools"], {"runner-tools": {"image": RUNNER_IMAGE}})["values"]
        result = arc.arc_capability_store_externals(values, STORE, 1001, 1001)
        self.assertEqual(result["errors"], [])
        keys = result["values"]["template"]["metadata"]["annotations"]["github-runner.exadev.io/capability-store-keys"].split()
        self.assertEqual(keys, [f"toolcache-{DIGEST_RUNNER}", f"externals-{DIGEST_RUNNER}"])

    def test_with_the_store_an_unpinned_capability_image_is_an_error(self) -> None:
        result = store_pod(DEFAULT_VALUES, ["untagged"])
        self.assertEqual(result["errors"], ["capability untagged's image must be pinned by digest (name@sha256:<64 hex digits>) for the capability store, since its store entry is named for the digest"])

    def test_a_store_path_that_overlaps_a_capability_mount_is_an_error(self) -> None:
        result = arc.arc_capability_pod(DEFAULT_VALUES, ["node"], STORE_CATALOGUE, TOOL_CACHE, SYSROOT, NODE_CACHE, NODE_CACHE_HOST, 1001, 1001, HOOKS_DIR, CONFIGMAP, {**STORE, "path": f"{TOOL_CACHE}/store"})
        self.assertEqual(len(result["errors"]), 1, result["errors"])
        self.assertIn("capability store path", result["errors"][0])

    def test_a_profile_with_only_hooks_or_a_node_cache_uses_no_store(self) -> None:
        result = store_pod(DEFAULT_VALUES, ["pnpm-store"])
        self.assertNotIn("capability-store", [volume["name"] for volume in result["values"]["template"]["spec"]["volumes"]])
        self.assertNotIn("metadata", result["values"]["template"])


DIND_VALUES: dict[str, Any] = {
    "template": {
        "spec": {
            "initContainers": [
                {"name": "init-dind-externals", "image": f"ghcr.io/example/runner:2@sha256:{DIGEST_RUNNER}", "command": ["cp", "-r", "/home/runner/externals/.", "/home/runner/tmpDir/"], "volumeMounts": [{"name": "dind-externals", "mountPath": "/home/runner/tmpDir"}]},
                {"name": "init-dind-mtu", "image": BUSYBOX},
            ],
            "containers": [
                {"name": "runner", "volumeMounts": [{"name": "work", "mountPath": "/home/runner/_work"}]},
                {"name": "dind", "volumeMounts": [{"name": "work", "mountPath": "/home/runner/_work"}, {"name": "dind-externals", "mountPath": "/home/runner/externals"}]},
            ],
            "volumes": [{"name": "work", "emptyDir": {}}, {"name": "dind-externals", "emptyDir": {}}],
        }
    }
}


class CapabilityStoreExternalsTest(unittest.TestCase):
    def test_the_dind_externals_copy_moves_into_the_store_and_dind_mounts_its_entry_read_only(self) -> None:
        result = arc.arc_capability_store_externals(DIND_VALUES, STORE, 1001, 1001)
        self.assertEqual(result["errors"], [])
        template = result["values"]["template"]
        spec = template["spec"]
        self.assertEqual([container["name"] for container in spec["initContainers"]], ["capability-store-prepare", "init-dind-externals", "capability-store-publish-externals", "init-dind-mtu"])
        copier = spec["initContainers"][1]
        self.assertEqual(copier["command"][3:], ["copy-to-capability-store", "/opt/capability-store", KEY_RUNNER, "/home/runner/externals", "init-dind-externals"])
        self.assertEqual([volume["name"] for volume in spec["volumes"]], ["work", "capability-store"])
        dind = next(container for container in spec["containers"] if container["name"] == "dind")
        self.assertEqual(dind["volumeMounts"][1], {"name": "capability-store", "mountPath": "/home/runner/externals", "subPath": KEY_RUNNER, "readOnly": True})
        self.assertEqual(template["metadata"]["annotations"]["github-runner.exadev.io/capability-store-keys"], KEY_RUNNER)
        self.assertEqual(DIND_VALUES["template"]["spec"]["volumes"][1]["name"], "dind-externals")

    def test_after_the_capabilities_the_store_is_prepared_once_and_the_annotation_keeps_both_lists(self) -> None:
        values = store_pod(DIND_VALUES, ["node"])["values"]
        result = arc.arc_capability_store_externals(values, STORE, 1001, 1001)
        spec = result["values"]["template"]["spec"]
        names = [container["name"] for container in spec["initContainers"]]
        # The values' own externals copy comes before the capabilities' containers, so the preparation moves ahead of it.
        self.assertEqual(names[:3], ["capability-store-prepare", "init-dind-externals", "capability-store-publish-externals"])
        self.assertEqual(names.count("capability-store-prepare"), 1)
        self.assertEqual([volume["name"] for volume in spec["volumes"]].count("capability-store"), 1)
        self.assertEqual(result["values"]["template"]["metadata"]["annotations"]["github-runner.exadev.io/capability-store-keys"], f"{KEY_A} {KEY_RUNNER}")

    def test_without_the_store_or_without_an_externals_copy_the_values_are_left_alone(self) -> None:
        self.assertIs(arc.arc_capability_store_externals(DIND_VALUES, None, 1001, 1001)["values"], DIND_VALUES)
        self.assertIs(arc.arc_capability_store_externals(DEFAULT_VALUES, STORE, 1001, 1001)["values"], DEFAULT_VALUES)

    def test_an_unpinned_runner_image_cannot_use_the_store(self) -> None:
        values = json.loads(json.dumps(DIND_VALUES))
        values["template"]["spec"]["initContainers"][0]["image"] = "ghcr.io/example/runner:2"
        self.assertIn("pinned by digest", arc.arc_capability_store_externals(values, STORE, 1001, 1001)["errors"][0])


RUNNER_IMAGE = f"ghcr.io/example/runner:2@sha256:{DIGEST_RUNNER}"
# Docker-in-Docker the way the chart's Kubernetes 1.29 and later layout writes it, and the way a values file can: dind as a native sidecar, a container in initContainers whose restartPolicy is Always.
DIND_SIDECAR_VALUES: dict[str, Any] = {
    "template": {
        "spec": {
            "initContainers": [
                {"name": "init-dind-externals", "image": RUNNER_IMAGE, "command": ["cp"], "args": ["-r", "/home/runner/externals/.", "/home/runner/tmpDir/"], "volumeMounts": [{"name": "dind-externals", "mountPath": "/home/runner/tmpDir"}]},
                {"name": "dind", "image": "docker:dind", "restartPolicy": "Always", "volumeMounts": [{"name": "work", "mountPath": "/home/runner/_work"}, {"name": "dind-externals", "mountPath": "/home/runner/externals"}]},
            ],
            "containers": [{"name": "runner", "volumeMounts": [{"name": "work", "mountPath": "/home/runner/_work"}]}],
            "volumes": [{"name": "work", "emptyDir": {}}, {"name": "dind-externals", "emptyDir": {}}],
        }
    }
}


EXTERNALS_STORE_MOUNT = {"name": "capability-store", "mountPath": "/home/runner/externals", "subPath": KEY_RUNNER, "readOnly": True}


def assert_every_mount_has_its_volume(test: unittest.TestCase, spec: dict[str, Any]) -> None:
    """Fail unless every volumeMount of every container and init container names a volume of the pod, as the API server requires."""
    volumes = {volume["name"] for volume in spec["volumes"]}
    for container in [*spec.get("containers", []), *spec.get("initContainers", [])]:
        for mount in container.get("volumeMounts", []):
            test.assertIn(mount["name"], volumes, f"{container['name']} mounts {mount['name']}, which the pod does not define")


class CapabilityStoreExternalsLayoutTest(unittest.TestCase):
    """The dind sidecar's externals mount is rewritten wherever the dind container is, so no container keeps a mount of the removed volume."""

    def dind(self, spec: dict[str, Any]) -> dict[str, Any]:
        return next(container for container in [*spec["containers"], *spec["initContainers"]] if container["name"] == "dind")

    def test_dind_as_an_ordinary_container_mounts_the_store_entry_and_every_mount_has_its_volume(self) -> None:
        spec = arc.arc_capability_store_externals(DIND_VALUES, STORE, 1001, 1001)["values"]["template"]["spec"]
        assert_every_mount_has_its_volume(self, spec)
        self.assertIn(EXTERNALS_STORE_MOUNT, self.dind(spec)["volumeMounts"])

    def test_dind_as_a_native_sidecar_mounts_the_store_entry_and_every_mount_has_its_volume(self) -> None:
        result = arc.arc_capability_store_externals(DIND_SIDECAR_VALUES, STORE, 1001, 1001)
        self.assertEqual(result["errors"], [])
        spec = result["values"]["template"]["spec"]
        assert_every_mount_has_its_volume(self, spec)
        self.assertNotIn("dind-externals", [volume["name"] for volume in spec["volumes"]])
        dind = self.dind(spec)
        self.assertIn(EXTERNALS_STORE_MOUNT, dind["volumeMounts"])
        self.assertEqual(dind["restartPolicy"], "Always")
        # The copy and publish containers the function inserted mount the store and nothing of the removed volume, and the sidecar starts after both.
        names = [container["name"] for container in spec["initContainers"]]
        self.assertEqual(names, ["capability-store-prepare", "init-dind-externals", "capability-store-publish-externals", "dind"])

    def test_the_registry_cache_wiring_then_the_store_leaves_every_mount_with_its_volume(self) -> None:
        # The role's order: capabilities, then the registry cache (which writes the chart's dind sidecar out under initContainers), then the store's externals.
        values = json.loads(json.dumps(DEFAULT_VALUES))
        values["containerMode"] = {"type": "dind"}
        values = store_pod(values, ["node", "mpi"])["values"]
        cache = arc.arc_registry_cache([{"name": "hub", "host": "docker.io"}], "registry-cache", "registry-cache", "cluster.local", 5000)
        self.assertEqual(cache["errors"], [])
        wired = arc.arc_registry_cache_pod(values, cache, RUNNER_IMAGE, "registry-cache", "/home/runner/.buildx")
        self.assertEqual(wired["errors"], [])
        result = arc.arc_capability_store_externals(wired["values"], STORE, 1001, 1001)
        self.assertEqual(result["errors"], [])
        spec = result["values"]["template"]["spec"]
        assert_every_mount_has_its_volume(self, spec)
        self.assertNotIn("dind-externals", [volume["name"] for volume in spec["volumes"]])
        self.assertIn("dind", [container["name"] for container in spec["initContainers"]])
        self.assertIn(EXTERNALS_STORE_MOUNT, self.dind(spec)["volumeMounts"])
        self.assertEqual(result["values"]["template"]["metadata"]["annotations"]["github-runner.exadev.io/capability-store-keys"].split()[-1], KEY_RUNNER)

    def test_the_store_functions_leave_every_mount_with_its_volume_for_a_profile_without_dind(self) -> None:
        spec = store_pod(DEFAULT_VALUES, ["node", "mpi", "libpq", "pnpm-store"])["values"]["template"]["spec"]
        assert_every_mount_has_its_volume(self, spec)


class CapabilityStoreValidationTest(unittest.TestCase):
    """What a profile with capability_store needs is reported by validation, with the messages the render reports, so that it fails before anything is installed."""

    UNPINNED = {"tools": {"image": "ghcr.io/example/toolcache-node:22"}, "libraries": {"sysroot_image": "ghcr.io/example/sysroot-libpq:16"}}

    def profile(self, store: bool = True, names: list[str] | None = None, **settings: Any) -> dict[str, Any]:
        return expanded({"max_runners": 1, "capability_store": store, "capabilities": names or [], **settings})

    def catalogue_errors(self, profile: dict[str, Any], catalogue: dict[str, Any]) -> list[str]:
        errors: list[str] = arc.arc_capability_errors([profile], catalogue)
        return errors

    def store_errors(self, profile: dict[str, Any], catalogue: dict[str, Any], store_path: str = "/opt/capability-store", registry_cache: bool = False) -> list[str]:
        errors: list[str] = arc.arc_capability_store_errors([profile], catalogue, store_path, TOOL_CACHE, SYSROOT, HOOKS_DIR, NODE_CACHE, registry_cache)
        return errors

    def render_errors(self, names: list[str], catalogue: dict[str, Any], store: dict[str, str] = STORE) -> list[str]:
        errors: list[str] = arc.arc_capability_pod(DEFAULT_VALUES, names, catalogue, TOOL_CACHE, SYSROOT, NODE_CACHE, NODE_CACHE_HOST, 1001, 1001, HOOKS_DIR, CONFIGMAP, store)["errors"]
        return errors

    def test_a_capability_image_not_pinned_by_digest_is_a_validation_error_with_the_stores_profile_and_the_renders_message(self) -> None:
        for name, catalogue in (("tool", {"tools": self.UNPINNED["tools"]}), ("library", {"libraries": self.UNPINNED["libraries"]})):
            with self.subTest(name):
                capability = next(iter(catalogue))
                errors = self.catalogue_errors(self.profile(names=[capability]), catalogue)
                self.assertEqual(errors, [f"Example: {message}" for message in self.render_errors([capability], catalogue)])
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("pinned by digest", errors[0])

    def test_a_profile_without_the_store_needs_no_digest_and_a_digest_passes(self) -> None:
        self.assertEqual(self.catalogue_errors(self.profile(store=False, names=["tools", "libraries"]), self.UNPINNED), [])
        self.assertEqual(self.catalogue_errors(self.profile(names=["node", "mpi", "libpq"]), STORE_CATALOGUE), [])

    def test_each_unpinned_capability_of_a_profile_is_reported_once_and_hooks_need_no_digest(self) -> None:
        catalogue = {**self.UNPINNED, "hook": {"job_started_hook": "true"}}
        errors = self.catalogue_errors(self.profile(names=["tools", "hook", "libraries"]), catalogue)
        self.assertEqual(len(errors), 2, errors)
        self.assertIn("capability tools's", errors[0])
        self.assertIn("capability libraries's", errors[1])

    def test_a_store_path_inside_the_tool_cache_is_a_validation_error_with_the_renders_message(self) -> None:
        inside = f"{TOOL_CACHE}/store"
        expected = self.render_errors(["node"], STORE_CATALOGUE, {**STORE, "path": inside})
        self.assertEqual(len(expected), 1, expected)
        self.assertEqual(self.store_errors(self.profile(names=["node"]), STORE_CATALOGUE, inside), [f"Example: {message}" for message in expected])

    def test_the_store_path_is_checked_against_every_directory_the_capabilities_mount(self) -> None:
        for what, path in (("tool cache", f"{TOOL_CACHE}/x"), ("sysroot", SYSROOT), ("job-started hooks", f"{HOOKS_DIR}/x"), ("node cache", "/"), ("node cache", f"{NODE_CACHE}/store")):
            with self.subTest(what=what, path=path):
                self.assertEqual(len(self.store_errors(self.profile(names=["node"]), STORE_CATALOGUE, path)), 1)
        self.assertEqual(self.store_errors(self.profile(names=["node"]), STORE_CATALOGUE), [])

    def test_a_profile_that_holds_nothing_in_the_store_or_does_not_use_it_needs_no_separate_path(self) -> None:
        inside = f"{TOOL_CACHE}/store"
        self.assertEqual(self.store_errors(self.profile(names=["pnpm-store"]), STORE_CATALOGUE, inside), [])
        self.assertEqual(self.store_errors(self.profile(store=False, names=["node"]), STORE_CATALOGUE, inside), [])

    def dind_profile(self, image: str, **settings: Any) -> dict[str, Any]:
        result = arc.arc_profiles([{"name": "Example", "image": image, "scale_set_profiles": [{"max_runners": 1, "capability_store": True, **settings}]}], require_app_id=False)
        self.assertEqual(result["errors"], [])
        profile: dict[str, Any] = result["profiles"][0]
        return profile

    def test_an_unpinned_runner_image_is_a_validation_error_for_the_dind_externals_the_registry_cache_writes_out(self) -> None:
        profile = self.dind_profile("ghcr.io/example/runner:2", container_mode="dind")
        errors = self.store_errors(profile, {}, registry_cache=True)
        values = {"template": {"spec": {"initContainers": [{"name": "init-dind-externals", "image": "ghcr.io/example/runner:2"}]}}}
        self.assertEqual(errors, [f"Example: {message}" for message in arc.arc_capability_store_externals(values, STORE, 1001, 1001)["errors"]])
        self.assertEqual(len(errors), 1, errors)

    def test_a_digest_pinned_runner_image_or_no_registry_cache_or_no_dind_leaves_the_externals_unchecked(self) -> None:
        self.assertEqual(self.store_errors(self.dind_profile(RUNNER_IMAGE, container_mode="dind"), {}, registry_cache=True), [])
        self.assertEqual(self.store_errors(self.dind_profile("ghcr.io/example/runner:2", container_mode="dind"), {}, registry_cache=False), [])
        self.assertEqual(self.store_errors(self.dind_profile("ghcr.io/example/runner:2"), {}, registry_cache=True), [])
        # A values file's own dind wiring is not known here; the render checks its externals copy.
        self.assertEqual(self.store_errors(self.dind_profile("ghcr.io/example/runner:2", values_file="values.yaml", container_mode="dind"), {}, registry_cache=True), [])


class CapabilityStoreImageContractTest(unittest.TestCase):
    """The applets the store image must provide, as the defaults and the README state them, are exactly the ones the init containers that run it use."""

    # Utilities a script could call; the shell's own builtins (echo, printf, test, [) are not among them, since the image's sh provides those.
    UTILITIES = ("awk", "basename", "cat", "chgrp", "chmod", "chown", "cp", "cut", "date", "dd", "df", "dirname", "du", "find", "grep", "head", "ln", "ls", "mkdir", "mktemp", "mv", "readlink", "rm", "rmdir", "sed", "sort", "stat", "sync", "tail", "tar", "touch", "tr", "wc", "xargs")
    STATEMENT = re.compile(r"Its ((?:\w+, )+\w+ and \w+) must be busybox's or GNU's")

    @classmethod
    def used(cls) -> set[str]:
        """The utilities the store image's init containers run: sh itself, and each utility named as a word in their commands."""
        spec = store_pod(DEFAULT_VALUES, ["node", "mpi", "libpq"])["values"]["template"]["spec"]
        scripts = [container["command"][2] for container in spec["initContainers"] if container["image"] == STORE["image"]]
        assert len(scripts) == 4, scripts
        return {"sh", *(utility for utility in cls.UTILITIES if any(re.search(rf"(?<![\w./$-]){utility}(?![\w-])", script) for script in scripts))}

    @classmethod
    def stated(cls, text: str) -> set[str]:
        match = cls.STATEMENT.search(text.replace("`", ""))
        assert match is not None
        return set(re.split(r", | and ", match.group(1)))

    def test_the_defaults_name_exactly_the_applets_the_containers_use(self) -> None:
        self.assertEqual(self.stated((ROOT / "roles" / "github_runner_arc" / "defaults" / "main.yml").read_text()), self.used())

    def test_the_readme_names_exactly_the_applets_the_containers_use(self) -> None:
        self.assertEqual(self.stated((ROOT / "roles" / "github_runner_arc" / "README.md").read_text()), self.used())

    def test_the_applets_include_find_for_the_tool_cache_links_and_not_ln_which_no_script_uses(self) -> None:
        self.assertIn("find", self.used())
        self.assertNotIn("ln", self.used())


class CapabilityStoreScriptTest(unittest.TestCase):
    """Runs the store's init container commands with sh against a temporary store, as the pods would: the copy, the publishing (including two pods racing to publish one image), the sysroot merge and the tool cache links. Needs a mv with -T and a cp with -l, as busybox and GNU coreutils have."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        # Registered second so that it runs first: a tree with directories lacking owner write permission cannot be removed until it is writable again.
        self.addCleanup(self.make_writable, self.work)
        self.root = self.work / "store"
        (self.root / ".incoming").mkdir(parents=True)

    @staticmethod
    def make_writable(directory: Path) -> None:
        """Give every directory under directory (and itself) owner read, write and search permission, so that it can be removed."""
        for path in [directory, *directory.rglob("*")]:
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)

    @staticmethod
    def make_read_only(directory: Path) -> None:
        """Remove owner write permission from every directory under directory (and itself), deepest first, as a Go module cache's directories are (mode 0555)."""
        for path in sorted([directory, *directory.rglob("*")], key=lambda candidate: len(candidate.parts), reverse=True):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o555)

    def run_container(self, container: dict[str, Any], uid: str, rewrite: dict[str, str]) -> subprocess.CompletedProcess[str]:
        """Run an init container's command with sh, as pod uid, with its paths rewritten to the temporary ones."""
        command = [rewrite.get(argument, argument) for argument in container["command"]]
        return subprocess.run(command, env={**os.environ, "POD_UID": uid}, capture_output=True, text=True, check=False)

    def payload(self, name: str, files: dict[str, str]) -> Path:
        directory = self.work / name
        for relative, content in files.items():
            (directory / relative).parent.mkdir(parents=True, exist_ok=True)
            (directory / relative).write_text(content)
        return directory

    def containers(self, names: list[str]) -> dict[str, dict[str, Any]]:
        spec = store_pod(DEFAULT_VALUES, names)["values"]["template"]["spec"]
        return {container["name"]: container for container in spec["initContainers"]}

    def test_a_payload_is_copied_once_and_published_and_a_later_pod_copies_nothing(self) -> None:
        toolcache = self.payload("toolcache", {"node/22.0.0/x64/bin/node": "binary", "node/22.0.0/x64.complete": ""})
        containers = self.containers(["node"])
        rewrite = {"/opt/capability-store": str(self.root), "/toolcache": str(toolcache)}
        for uid in ("pod-1", "pod-2"):
            self.assertEqual(self.run_container(containers["capability-node"], uid, rewrite).returncode, 0)
            published = self.run_container(containers["capability-store-publish"], uid, rewrite)
            self.assertEqual(published.returncode, 0, published.stderr)
            self.assertEqual(sorted(path.name for path in (self.root / ".incoming").iterdir()), [])
        self.assertEqual((self.root / KEY_A / "node/22.0.0/x64/bin/node").read_text(), "binary")

    def test_of_two_pods_copying_one_image_at_once_the_first_to_publish_wins_and_the_other_copy_is_deleted(self) -> None:
        toolcache = self.payload("toolcache", {"node/22.0.0/x64/bin/node": "binary"})
        containers = self.containers(["node"])
        rewrite = {"/opt/capability-store": str(self.root), "/toolcache": str(toolcache)}
        # Both copy before either publishes, as two pods starting together on a node without the entry would.
        for uid in ("pod-1", "pod-2"):
            self.assertEqual(self.run_container(containers["capability-node"], uid, rewrite).returncode, 0)
        (self.root / ".incoming" / f"{KEY_A}.pod-1.capability-node" / "marker").write_text("pod-1")
        for uid in ("pod-1", "pod-2"):
            published = self.run_container(containers["capability-store-publish"], uid, rewrite)
            self.assertEqual(published.returncode, 0, published.stderr)
        self.assertEqual((self.root / KEY_A / "marker").read_text(), "pod-1")
        self.assertFalse((self.root / KEY_A / f"{KEY_A}.pod-2.capability-node").exists())
        self.assertEqual(list((self.root / ".incoming").iterdir()), [])

    def test_one_image_used_for_a_tool_cache_and_a_sysroot_publishes_each_payload_under_its_own_entry(self) -> None:
        image = f"ghcr.io/example/both:1@sha256:{DIGEST_A}"
        spec = store_pod(DEFAULT_VALUES, ["tools", "libraries"], {"tools": {"image": image}, "libraries": {"sysroot_image": image}})["values"]["template"]["spec"]
        containers = {container["name"]: container for container in spec["initContainers"]}
        toolcache = self.payload("toolcache", {"node/22.0.0/x64/bin/node": "binary"})
        sysroot = self.payload("sysroot", {"lib/libmpi.so": "library"})
        rewrite = {"/opt/capability-store": str(self.root), "/toolcache": str(toolcache), "/sysroot": str(sysroot)}
        for name in ("capability-tools", "capability-libraries", "capability-store-publish"):
            completed = self.run_container(containers[name], "pod-1", rewrite)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.layout(self.root / f"toolcache-{DIGEST_A}"), self.layout(toolcache))
        self.assertEqual(self.layout(self.root / f"sysroot-{DIGEST_A}"), self.layout(sysroot))

    def layout(self, directory: Path) -> dict[str, str]:
        """Return every path under directory with what it holds (a file's content, a link's target, a directory's marker), so two trees compare by layout and content."""
        return {str(path.relative_to(directory)): path.readlink().as_posix() if path.is_symlink() else path.read_text() if path.is_file() else "<dir>" for path in sorted(directory.rglob("*"))}

    def fake_sync(self, exit_status: int) -> dict[str, str]:
        """Return an environment whose sync records whether the copy was still unpublished when it ran, in sync.log, and exits with exit_status. It takes the copy's and the entry's paths from the environment, since the test sets them for it."""
        bin_dir = self.work / "fake-bin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / "sync"
        script.write_text('#!/bin/sh\n{ [ -d "$SYNC_INCOMING" ] && printf "incoming " || printf "no-incoming "; [ -d "$SYNC_ENTRY" ] && echo published || echo unpublished; } >> "$SYNC_LOG"\nexit ' + str(exit_status) + "\n")
        script.chmod(0o755)
        return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "SYNC_LOG": str(self.work / "sync.log")}

    def run_with_env(self, container: dict[str, Any], uid: str, rewrite: dict[str, str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        command = [rewrite.get(argument, argument) for argument in container["command"]]
        return subprocess.run(command, env={**os.environ, "POD_UID": uid, **env}, capture_output=True, text=True, check=False)

    def test_a_copy_is_flushed_to_disk_before_it_is_renamed_into_the_store(self) -> None:
        toolcache = self.payload("toolcache", {"node/22.0.0/x64/bin/node": "binary"})
        containers = self.containers(["node"])
        rewrite = {"/opt/capability-store": str(self.root), "/toolcache": str(toolcache)}
        self.assertEqual(self.run_container(containers["capability-node"], "pod-1", rewrite).returncode, 0)
        env = {**self.fake_sync(0), "SYNC_INCOMING": str(self.root / ".incoming" / f"{KEY_A}.pod-1.capability-node"), "SYNC_ENTRY": str(self.root / KEY_A)}
        published = self.run_with_env(containers["capability-store-publish"], "pod-1", rewrite, env)
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertEqual((self.work / "sync.log").read_text(), "incoming unpublished\n")
        self.assertEqual((self.root / KEY_A / "node/22.0.0/x64/bin/node").read_text(), "binary")

    def test_a_copy_that_cannot_be_flushed_is_not_published(self) -> None:
        toolcache = self.payload("toolcache", {"node/22.0.0/x64/bin/node": "binary"})
        containers = self.containers(["node"])
        rewrite = {"/opt/capability-store": str(self.root), "/toolcache": str(toolcache)}
        self.assertEqual(self.run_container(containers["capability-node"], "pod-1", rewrite).returncode, 0)
        env = {**self.fake_sync(1), "SYNC_INCOMING": "unused", "SYNC_ENTRY": "unused"}
        published = self.run_with_env(containers["capability-store-publish"], "pod-1", rewrite, env)
        self.assertNotEqual(published.returncode, 0)
        self.assertFalse((self.root / KEY_A).exists())

    def test_a_merged_sysroot_is_flushed_to_disk_before_it_is_renamed_into_the_store(self) -> None:
        for digest, files in ((KEY_B, {"lib/libmpi.so": "mpi"}), (KEY_C, {"lib/libpq.so": "pq"})):
            for relative, content in files.items():
                (self.root / digest / relative).parent.mkdir(parents=True, exist_ok=True)
                (self.root / digest / relative).write_text(content)
        merge = self.containers(["mpi", "libpq"])["capability-store-sysroot"]
        merged = merge["command"][5]
        env = {**self.fake_sync(0), "SYNC_INCOMING": str(self.root / ".incoming" / f"{merged}.pod-1.merge"), "SYNC_ENTRY": str(self.root / merged)}
        completed = self.run_with_env(merge, "pod-1", {"/opt/capability-store": str(self.root)}, env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual((self.work / "sync.log").read_text(), "incoming unpublished\n")
        self.assertTrue((self.root / merged / "lib/libpq.so").is_file())

    def assert_rerun_after_a_partial_copy_publishes_the_source_layout(self, copy: dict[str, Any], publish: dict[str, Any], key: str, source: Path, rewrite: dict[str, str]) -> None:
        """Leave what a copy killed part-way leaves in the pod's own destination (including a directory without owner write permission and content below it), run the copy twice (the kubelet reruns a pod's init containers after a sandbox change, and the second run finds the first's complete copy, whose own source may hold such directories), publish, and require the entry to hold exactly the source's layout and content."""
        debris = self.root / ".incoming" / f"{key}.pod-1.{copy['name']}"
        (debris / "bin").mkdir(parents=True)
        (debris / "bin/partial").write_text("half a file")
        (debris / source.name).mkdir()
        (debris / source.name / "stale").write_text("nested by a copy onto an existing destination")
        (debris / "pkg/mod/cache").mkdir(parents=True)
        (debris / "pkg/mod/cache/download").write_text("a Go module cache's read-only directory")
        self.make_read_only(debris / "pkg")
        for _ in range(2):
            completed = self.run_container(copy, "pod-1", rewrite)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        published = self.run_container(publish, "pod-1", rewrite)
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertEqual(self.layout(self.root / key), self.layout(source))
        self.assertEqual(list((self.root / ".incoming").iterdir()), [])

    def test_a_capability_copy_rerun_after_a_partial_copy_publishes_exactly_the_payloads_layout(self) -> None:
        toolcache = self.payload("toolcache", {"node/22.0.0/x64/bin/node": "binary", "node/22.0.0/x64.complete": "", "go/pkg/mod/example@v1/file": "module"})
        self.make_read_only(toolcache / "go")
        containers = self.containers(["node"])
        self.assert_rerun_after_a_partial_copy_publishes_the_source_layout(containers["capability-node"], containers["capability-store-publish"], KEY_A, toolcache, {"/opt/capability-store": str(self.root), "/toolcache": str(toolcache)})

    def test_the_dind_externals_copy_rerun_after_a_partial_copy_publishes_exactly_the_externals_layout(self) -> None:
        externals = self.payload("externals", {"node20/bin/node": "binary", "node20/lib/libnode.so": "library", "node20/read-only/file": "content"})
        self.make_read_only(externals / "node20/read-only")
        spec = arc.arc_capability_store_externals(DIND_VALUES, STORE, 1001, 1001)["values"]["template"]["spec"]
        containers = {container["name"]: container for container in spec["initContainers"]}
        self.assert_rerun_after_a_partial_copy_publishes_the_source_layout(containers["init-dind-externals"], containers["capability-store-publish-externals"], KEY_RUNNER, externals, {"/opt/capability-store": str(self.root), "/home/runner/externals": str(externals)})

    def test_two_capabilities_on_one_system_library_image_link_that_entry_into_the_merge_once(self) -> None:
        # busybox's cp -alf of one entry into the merge twice fails on the second as the same file, so the pod never started; GNU cp tolerates it, so the test records each cp call and requires one for the entry.
        image = f"ghcr.io/example/libs:1@sha256:{DIGEST_B}"
        (self.root / KEY_B / "lib").mkdir(parents=True)
        (self.root / KEY_B / "lib/libfoo.so.1").write_text("foo")
        spec = store_pod(DEFAULT_VALUES, ["first", "second"], {"first": {"sysroot_image": image}, "second": {"sysroot_image": image}})["values"]["template"]["spec"]
        merge = next(container for container in spec["initContainers"] if container["name"] == "capability-store-sysroot")
        bin_dir = self.work / "cp-bin"
        bin_dir.mkdir()
        recorder = bin_dir / "cp"
        recorder.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CP_LOG"\nexec ' + str(shutil.which("cp")) + ' "$@"\n')
        recorder.chmod(0o755)
        log = self.work / "cp.log"
        completed = self.run_with_env(merge, "pod-1", {"/opt/capability-store": str(self.root)}, {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CP_LOG": str(log)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len([line for line in log.read_text().splitlines() if f"{self.root}/{KEY_B}/." in line]), 1)
        self.assertEqual(self.layout(self.root / merge["command"][5]), {"lib": "<dir>", "lib/libfoo.so.1": "foo"})

    def test_a_sysroot_merge_rerun_after_a_partial_merge_publishes_only_the_libraries(self) -> None:
        (self.root / KEY_B / "lib").mkdir(parents=True)
        (self.root / KEY_B / "lib/libmpi.so").write_text("mpi")
        merge = self.containers(["mpi"])["capability-store-sysroot"]
        merged = merge["command"][5]
        debris = self.root / ".incoming" / f"{merged}.pod-1.merge"
        (debris / "lib").mkdir(parents=True)
        (debris / "lib/partial").write_text("half a file")
        (debris / "share/doc").mkdir(parents=True)
        (debris / "share/doc/readme").write_text("a read-only directory's content")
        self.make_read_only(debris / "share")
        completed = self.run_container(merge, "pod-1", {"/opt/capability-store": str(self.root)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.layout(self.root / merged), {"lib": "<dir>", "lib/libmpi.so": "mpi"})

    def test_a_sysroot_merge_that_loses_the_race_deletes_its_copy_even_when_it_holds_read_only_directories(self) -> None:
        (self.root / KEY_B / "lib/private").mkdir(parents=True)
        (self.root / KEY_B / "lib/private/libmpi.so").write_text("mpi")
        self.make_read_only(self.root / KEY_B)
        merge = self.containers(["mpi"])["capability-store-sysroot"]
        merged = merge["command"][5]
        # Another pod publishes the merged entry while this pod's sync runs, as two pods starting together on a node without it would.
        bin_dir = self.work / "race-bin"
        bin_dir.mkdir()
        sync = bin_dir / "sync"
        sync.write_text('#!/bin/sh\nmkdir -p "$RACE_ENTRY" && printf winner > "$RACE_ENTRY/marker"\n')
        sync.chmod(0o755)
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "RACE_ENTRY": str(self.root / merged)}
        completed = self.run_with_env(merge, "pod-1", {"/opt/capability-store": str(self.root)}, env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.layout(self.root / merged), {"marker": "winner"})
        self.assertEqual(list((self.root / ".incoming").iterdir()), [])

    def test_of_two_pods_publishing_one_image_the_loser_deletes_its_copy_even_when_it_holds_read_only_directories(self) -> None:
        toolcache = self.payload("toolcache", {"go/pkg/mod/example@v1/file": "module", "go/bin/go": "binary"})
        self.make_read_only(toolcache / "go")
        containers = self.containers(["node"])
        rewrite = {"/opt/capability-store": str(self.root), "/toolcache": str(toolcache)}
        for uid in ("pod-1", "pod-2"):
            self.assertEqual(self.run_container(containers["capability-node"], uid, rewrite).returncode, 0)
        for uid in ("pod-1", "pod-2"):
            published = self.run_container(containers["capability-store-publish"], uid, rewrite)
            self.assertEqual(published.returncode, 0, published.stderr)
        self.assertEqual(self.layout(self.root / KEY_A), self.layout(toolcache))
        self.assertEqual(list((self.root / ".incoming").iterdir()), [])

    def test_a_payload_whose_root_directory_is_read_only_is_copied_and_published_with_the_source_layout(self) -> None:
        # A tool cache after chmod -R a-w has a root of mode 0555, which the copy keeps, and renaming a directory into another parent needs write permission on the directory itself.
        toolcache = self.payload("toolcache", {"node/22.0.0/x64/bin/node": "binary", "node/22.0.0/x64.complete": ""})
        externals = self.payload("externals", {"node20/bin/node": "binary"})
        for source in (toolcache, externals):
            self.make_read_only(source)
        for copy_name, publish_name, key, source, mount in (
            ("capability-node", "capability-store-publish", KEY_A, toolcache, "/toolcache"),
            ("init-dind-externals", "capability-store-publish-externals", KEY_RUNNER, externals, "/home/runner/externals"),
        ):
            with self.subTest(copy=copy_name):
                spec = arc.arc_capability_store_externals(store_pod(DIND_VALUES, ["node"])["values"], STORE, 1001, 1001)["values"]["template"]["spec"]
                containers = {container["name"]: container for container in spec["initContainers"]}
                rewrite = {"/opt/capability-store": str(self.root), mount: str(source)}
                self.assertFalse(source.stat().st_mode & stat.S_IWUSR)
                copied = self.run_container(containers[copy_name], "pod-1", rewrite)
                self.assertEqual(copied.returncode, 0, copied.stderr)
                published = self.run_container(containers[publish_name], "pod-1", rewrite)
                self.assertEqual(published.returncode, 0, published.stderr)
                self.assertEqual(self.layout(self.root / key), self.layout(source))
                self.assertEqual(list((self.root / ".incoming").iterdir()), [])

    def test_a_rename_that_fails_for_another_reason_than_a_lost_race_reports_the_error_of_mv(self) -> None:
        toolcache = self.payload("toolcache", {"node/22.0.0/x64/bin/node": "binary"})
        containers = self.containers(["node"])
        rewrite = {"/opt/capability-store": str(self.root), "/toolcache": str(toolcache)}
        self.assertEqual(self.run_container(containers["capability-node"], "pod-1", rewrite).returncode, 0)
        # A store root the pod cannot write to: the entry cannot be created, which is no lost race.
        self.root.chmod(0o555)
        published = self.run_container(containers["capability-store-publish"], "pod-1", rewrite)
        self.assertNotEqual(published.returncode, 0)
        self.assertIn(f"{KEY_A} is not in", published.stderr)
        self.assertIn("Permission denied", published.stderr)

    def test_a_lost_publish_race_is_not_an_error_and_prints_nothing(self) -> None:
        toolcache = self.payload("toolcache", {"node/22.0.0/x64/bin/node": "binary"})
        containers = self.containers(["node"])
        rewrite = {"/opt/capability-store": str(self.root), "/toolcache": str(toolcache)}
        for uid in ("pod-1", "pod-2"):
            self.assertEqual(self.run_container(containers["capability-node"], uid, rewrite).returncode, 0)
        for uid in ("pod-1", "pod-2"):
            published = self.run_container(containers["capability-store-publish"], uid, rewrite)
            self.assertEqual((published.returncode, published.stderr), (0, ""))

    def test_a_sysroot_merged_from_entries_whose_root_is_read_only_is_published(self) -> None:
        # The merge's destination takes the mode of the first entry's root (cp -a), and renaming it into the store needs write permission on it.
        for digest, files in ((KEY_B, {"lib/libmpi.so": "mpi"}), (KEY_C, {"lib/libpq.so": "pq"})):
            for relative, content in files.items():
                (self.root / digest / relative).parent.mkdir(parents=True, exist_ok=True)
                (self.root / digest / relative).write_text(content)
            self.make_read_only(self.root / digest)
        merge = self.containers(["mpi", "libpq"])["capability-store-sysroot"]
        merged = merge["command"][5]
        completed = self.run_container(merge, "pod-1", {"/opt/capability-store": str(self.root)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.layout(self.root / merged), {"lib": "<dir>", "lib/libmpi.so": "mpi", "lib/libpq.so": "pq"})
        self.assertEqual(list((self.root / ".incoming").iterdir()), [])

    def store_entry_with_read_only_directories(self) -> tuple[Path, str]:
        """Publish a system library's entry whose files are mode 0444 and whose directories are read-only, and return the file and the merged sysroot's entry name for it."""
        library = self.root / KEY_B / "lib/libmpi.so"
        library.parent.mkdir(parents=True)
        library.write_text("mpi")
        library.chmod(0o444)
        self.make_read_only(self.root / KEY_B)
        return library, self.containers(["mpi"])["capability-store-sysroot"]["command"][5]

    def test_a_merge_that_loses_the_race_leaves_the_modes_of_the_entries_files_alone(self) -> None:
        # The merge's files are hard links to the entry's, so making them writable would change the entry's own.
        library, merged = self.store_entry_with_read_only_directories()
        bin_dir = self.work / "race-bin"
        bin_dir.mkdir()
        sync = bin_dir / "sync"
        sync.write_text('#!/bin/sh\nmkdir -p "$RACE_ENTRY"\n')
        sync.chmod(0o755)
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "RACE_ENTRY": str(self.root / merged)}
        completed = self.run_with_env(self.containers(["mpi"])["capability-store-sysroot"], "pod-1", {"/opt/capability-store": str(self.root)}, env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(stat.S_IMODE(library.stat().st_mode), 0o444)
        self.assertEqual(list((self.root / ".incoming").iterdir()), [])

    def test_a_merge_rerun_after_a_partial_merge_leaves_the_modes_of_the_entries_files_alone(self) -> None:
        library, merged = self.store_entry_with_read_only_directories()
        merge = self.containers(["mpi"])["capability-store-sysroot"]
        # What a merge killed after its links and before its rename leaves: hard links into the entry under directories without owner write permission.
        debris = self.root / ".incoming" / f"{merged}.pod-1.merge"
        debris.mkdir()
        subprocess.run(["cp", "-al", f"{self.root / KEY_B}/.", f"{debris}/"], check=True)
        completed = self.run_container(merge, "pod-1", {"/opt/capability-store": str(self.root)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(stat.S_IMODE(library.stat().st_mode), 0o444)
        self.assertEqual(self.layout(self.root / merged), {"lib": "<dir>", "lib/libmpi.so": "mpi"})

    def test_publishing_fails_when_the_entry_is_neither_copied_nor_in_the_store(self) -> None:
        containers = self.containers(["node"])
        published = self.run_container(containers["capability-store-publish"], "pod-1", {"/opt/capability-store": str(self.root)})
        self.assertNotEqual(published.returncode, 0)
        self.assertIn(f"{KEY_A} is not in", published.stderr)

    def test_the_merged_sysroot_links_every_library_and_a_later_one_wins_a_shared_file(self) -> None:
        for digest, files in ((KEY_B, {"lib/libmpi.so": "mpi", "include/shared.h": "from mpi"}), (KEY_C, {"lib/libpq.so": "pq", "include/shared.h": "from libpq"})):
            for relative, content in files.items():
                (self.root / digest / relative).parent.mkdir(parents=True, exist_ok=True)
                (self.root / digest / relative).write_text(content)
        merge = self.containers(["mpi", "libpq"])["capability-store-sysroot"]
        merged = merge["command"][5]
        completed = self.run_container(merge, "pod-1", {"/opt/capability-store": str(self.root)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual((self.root / merged / "include/shared.h").read_text(), "from libpq")
        self.assertEqual((self.root / merged / "lib/libmpi.so").stat().st_ino, (self.root / KEY_B / "lib/libmpi.so").stat().st_ino)
        # A second pod finds it published and does nothing.
        self.assertEqual(self.run_container(merge, "pod-2", {"/opt/capability-store": str(self.root)}).returncode, 0)
        self.assertEqual(list((self.root / ".incoming").iterdir()), [])

    def tool_tree(self, files: dict[str, str] | None = None) -> Path:
        """Fill the store's entry for the node capability with a tool tree shaped like the Actions tool cache's Node.js, as a store entry is after publishing, and return its <arch> directory."""
        arch = self.root / KEY_A / "node/22.0.0/x64"
        for relative, content in (files or {"bin/node": "binary", "lib/node_modules/npm/bin/npm-cli.js": "npm", "lib/node_modules/npm/package.json": "{}", "include/node/node.h": "header", "dir with space/a file": "spaced", ".hidden/file": "hidden"}).items():
            (arch / relative).parent.mkdir(parents=True, exist_ok=True)
            (arch / relative).write_text(content)
        (arch / "bin/node").chmod(0o755)
        (arch / "empty").mkdir()
        (arch / "bin/npm").symlink_to("../lib/node_modules/npm/bin/npm-cli.js")
        (arch / "bin/absolute").symlink_to("/absolute/target")
        (arch / "libdir").symlink_to("lib")
        (arch.parent / "x64.complete").write_text("")
        return arch

    def link_tool_cache(self, cache: Path, uid: str = "pod-1") -> None:
        link = self.containers(["node"])["capability-store-tool-cache"]
        completed = self.run_container(link, uid, {"/opt/capability-store": str(self.root), TOOL_CACHE: str(cache)})
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def snapshot(self, directory: Path) -> dict[str, str]:
        """Return every path under directory with what it holds (a file's content, a link's target, a directory's name), so a change anywhere shows."""
        return {str(path.relative_to(directory)): path.readlink().as_posix() if path.is_symlink() else path.read_text() if path.is_file() else "<dir>" for path in sorted(directory.rglob("*"))}

    def test_the_tool_cache_rebuilds_each_tool_tree_as_directories_holding_a_link_to_each_store_file_and_the_marker(self) -> None:
        arch = self.tool_tree()
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        self.link_tool_cache(cache)
        tree = cache / "node/22.0.0/x64"
        self.assertTrue(tree.is_dir() and not tree.is_symlink())
        for directory in ("bin", "lib/node_modules/npm", "include/node", "dir with space", ".hidden", "empty"):
            self.assertTrue((tree / directory).is_dir() and not (tree / directory).is_symlink(), directory)
        for file, content in (("lib/node_modules/npm/package.json", "{}"), ("include/node/node.h", "header"), ("dir with space/a file", "spaced"), (".hidden/file", "hidden")):
            self.assertEqual((tree / file).readlink(), arch / file, file)
            self.assertEqual((tree / file).read_text(), content)
        self.assertTrue((cache / "node/22.0.0/x64.complete").is_file())
        self.assertFalse((cache / "node/22.0.0/x64.complete").is_symlink())

    def test_a_job_can_create_replace_and_remove_files_and_directories_in_a_tool_tree_and_the_store_is_unchanged(self) -> None:
        arch = self.tool_tree()
        before = self.snapshot(self.root)
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        self.link_tool_cache(cache)
        tree = cache / "node/22.0.0/x64"
        # What npm install -g and corepack enable do: create a package directory and a shim beside the launcher, replace an installed package's file, remove another.
        (tree / "lib/node_modules/added").mkdir()
        (tree / "lib/node_modules/added/index.js").write_text("added")
        (tree / "bin/shim").symlink_to("../lib/node_modules/added/index.js")
        (tree / "lib/node_modules/npm/package.json").unlink()
        (tree / "lib/node_modules/npm/package.json").write_text('{"version": "2"}')
        (tree / "include/node/node.h").unlink()
        (tree / "empty/inside").write_text("created")
        self.assertEqual((tree / "lib/node_modules/npm/package.json").read_text(), '{"version": "2"}')
        self.assertFalse((tree / "include/node/node.h").exists())
        self.assertEqual(self.snapshot(self.root), before)
        self.assertEqual((arch / "include/node/node.h").read_text(), "header")

    def test_a_launcher_is_a_copy_and_not_a_link_so_the_program_finds_its_own_tree_in_the_pod(self) -> None:
        self.tool_tree()
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        self.link_tool_cache(cache)
        launcher = cache / "node/22.0.0/x64/bin/node"
        self.assertTrue(launcher.is_file() and not launcher.is_symlink())
        self.assertEqual(launcher.read_text(), "binary")
        self.assertTrue(os.access(launcher, os.X_OK))
        # A program that locates its tree from its resolved executable path sees the pod's tree.
        self.assertEqual(launcher.resolve(), launcher.parent.resolve() / launcher.name)
        # A file elsewhere in the tree stays a link.
        self.assertTrue((cache / "node/22.0.0/x64/lib/node_modules/npm/package.json").is_symlink())

    def test_a_link_in_the_store_is_copied_so_its_relative_target_resolves_inside_the_pods_tree(self) -> None:
        self.tool_tree()
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        self.link_tool_cache(cache)
        tree = cache / "node/22.0.0/x64"
        self.assertEqual((tree / "bin/npm").readlink(), Path("../lib/node_modules/npm/bin/npm-cli.js"))
        self.assertEqual((tree / "bin/absolute").readlink(), Path("/absolute/target"))
        self.assertEqual((tree / "libdir").readlink(), Path("lib"))
        self.assertEqual((tree / "libdir").resolve(), (tree / "lib").resolve())
        self.assertEqual((tree / "libdir/node_modules/npm/package.json").read_text(), "{}")

    def aliased_tool_tree(self, alias: str) -> Path:
        """Fill the store's entry for the node capability with a tool whose <arch> directories are an alias of a real one (an image aliasing x64 and amd64), with the alias pointing at target, and return the version directory."""
        version = self.root / KEY_A / "node/22.0.0"
        (version / "real/bin").mkdir(parents=True)
        (version / "real/bin/node").write_text("binary")
        (version / "real/README").write_text("readme")
        (version / "x64").symlink_to(alias)
        (version / "x64.complete").write_text("")
        return version

    def test_an_arch_directory_that_is_a_link_is_copied_as_the_link_and_the_store_is_never_written_through_it(self) -> None:
        version = self.aliased_tool_tree("real")
        before = self.snapshot(self.root)
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        self.link_tool_cache(cache)
        tree = cache / "node/22.0.0"
        # The alias is the pod's own link, naming the pod's own directory beside it, and the marker for it is copied.
        self.assertEqual((tree / "x64").readlink(), Path("real"))
        self.assertEqual((tree / "x64/README").resolve(), (tree / "real/README").resolve())
        self.assertEqual((tree / "x64/README").read_text(), "readme")
        self.assertTrue((tree / "x64.complete").is_file() and not (tree / "x64.complete").is_symlink())
        self.assertTrue((tree / "real").is_dir() and not (tree / "real").is_symlink())
        self.assertEqual((tree / "real/bin/node").read_text(), "binary")
        # A job can change the tool through either name, and the store is unchanged.
        (tree / "x64/created").write_text("created")
        self.assertEqual((tree / "real/created").read_text(), "created")
        self.assertEqual(self.snapshot(self.root), before)
        self.assertFalse((version / "real/created").exists())

    def test_linking_again_over_an_aliased_tool_tree_leaves_the_same_tree(self) -> None:
        self.aliased_tool_tree("real")
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        self.link_tool_cache(cache)
        first = self.snapshot(cache)
        self.link_tool_cache(cache)
        self.assertEqual(self.snapshot(cache), first)

    def test_an_alias_of_an_alias_and_each_alias_marker_are_copied(self) -> None:
        version = self.aliased_tool_tree("real")
        (version / "amd64").symlink_to("x64")
        (version / "amd64.complete").write_text("")
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        self.link_tool_cache(cache)
        tree = cache / "node/22.0.0"
        self.assertEqual((tree / "amd64").readlink(), Path("x64"))
        self.assertEqual((tree / "amd64/bin/node").read_text(), "binary")
        self.assertTrue((tree / "amd64.complete").is_file())

    def test_an_absolute_link_for_an_arch_directory_is_copied_as_written_and_not_into_the_store(self) -> None:
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        # One that names a path in the pod's own tool cache, as an image built for that path writes it, and one that names the store itself.
        for target in (cache / "node/22.0.0/real", self.root / KEY_A / "node/22.0.0/real"):
            with self.subTest(target=str(target)):
                self.aliased_tool_tree(str(target))
                before = self.snapshot(self.root)
                self.link_tool_cache(cache)
                self.assertEqual((cache / "node/22.0.0/x64").readlink(), target)
                self.assertEqual((cache / "node/22.0.0/x64/bin/node").read_text(), "binary")
                self.assertEqual(self.snapshot(self.root), before)
                shutil.rmtree(self.root / KEY_A)
                shutil.rmtree(cache)
                cache.mkdir()

    def test_linking_the_tool_cache_again_leaves_the_same_tree(self) -> None:
        self.tool_tree()
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        self.link_tool_cache(cache)
        first = self.snapshot(cache)
        self.link_tool_cache(cache)
        self.assertEqual(self.snapshot(cache), first)

    def test_every_tool_version_and_architecture_of_every_entry_is_linked_beside_the_others(self) -> None:
        self.tool_tree()
        other = self.root / KEY_B / "python/3.12.0/x64"
        (other / "bin").mkdir(parents=True)
        (other / "bin/python3").write_text("python")
        (other.parent / "x64.complete").write_text("")
        (self.root / KEY_A / "node/22.0.0/arm64/bin").mkdir(parents=True)
        (self.root / KEY_A / "node/22.0.0/arm64/bin/node").write_text("arm")
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        link = dict(self.containers(["node"])["capability-store-tool-cache"])
        link["command"] = [*link["command"], KEY_B]
        completed = self.run_container(link, "pod-1", {"/opt/capability-store": str(self.root), TOOL_CACHE: str(cache)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual((cache / "python/3.12.0/x64/bin/python3").read_text(), "python")
        self.assertEqual((cache / "node/22.0.0/arm64/bin/node").read_text(), "arm")
        self.assertTrue((cache / "node/22.0.0/x64/bin/node").is_file())
        self.assertFalse((cache / "node/22.0.0/arm64.complete").exists())

    def read_only_tool_tree(self) -> Path:
        """Return the store's <arch> directory of a tool tree whose directories, at several depths and including <arch> itself, lack owner write permission, as a Go module cache's do (mode 0555), holding a file and a link at the deepest level."""
        arch = self.tool_tree()
        (arch / "lib/deep/er").mkdir(parents=True)
        (arch / "lib/deep/er/file").write_text("deep")
        (arch / "lib/deep/er/link").symlink_to("file")
        self.make_read_only(arch)
        return arch

    def test_a_tool_tree_the_store_holds_read_only_is_linked_and_every_directory_in_the_pod_is_writable(self) -> None:
        self.read_only_tool_tree()
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        self.link_tool_cache(cache)
        tree = cache / "node/22.0.0/x64"
        directories = [tree, *(path for path in tree.rglob("*") if path.is_dir() and not path.is_symlink())]
        self.assertGreater(len(directories), 6)
        for directory in directories:
            self.assertTrue(directory.stat().st_mode & stat.S_IWUSR, directory)
        self.assertEqual((tree / "lib/deep/er/link").readlink(), Path("file"))
        self.assertEqual((tree / "lib/deep/er/file").read_text(), "deep")

    def test_a_job_can_change_a_directory_the_store_holds_read_only_and_the_store_is_unchanged(self) -> None:
        arch = self.read_only_tool_tree()
        before = self.snapshot(self.root)
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        self.link_tool_cache(cache)
        deepest = cache / "node/22.0.0/x64/lib/deep/er"
        (deepest / "created").write_text("created")
        (deepest / "file").unlink()
        (deepest / "file").write_text("replaced")
        (deepest / "link").unlink()
        (cache / "node/22.0.0/x64/lib/new").mkdir()
        self.assertEqual((deepest / "file").read_text(), "replaced")
        self.assertEqual(self.snapshot(self.root), before)
        self.assertEqual((arch / "lib/deep/er/file").read_text(), "deep")
        self.assertFalse((arch / "lib/deep/er/created").exists())

    def test_linking_again_over_a_pod_tree_whose_directories_were_left_read_only_converges(self) -> None:
        self.read_only_tool_tree()
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        self.link_tool_cache(cache)
        first = self.snapshot(cache)
        # What a run killed after it built the directories and before it made them writable leaves.
        self.make_read_only(cache / "node")
        self.link_tool_cache(cache)
        self.assertEqual(self.snapshot(cache), first)
        self.assertTrue((cache / "node/22.0.0/x64/lib/deep/er").stat().st_mode & stat.S_IWUSR)

    def test_linking_again_replaces_a_completeness_marker_that_is_read_only(self) -> None:
        # The marker is copied as the store holds it, so a read-only one in the store is read-only in the pod, and a rerun must still replace it; a marker the pod's copy alone lost write permission on is the same case.
        for store_mode, pod_mode in ((0o444, None), (0o644, 0o444)):
            with self.subTest(store_mode=oct(store_mode), pod_mode=pod_mode and oct(pod_mode)):
                self.work = Path(tempfile.mkdtemp(dir=self.work))
                self.root = self.work / "store"
                (self.root / ".incoming").mkdir(parents=True)
                self.tool_tree()
                (self.root / KEY_A / "node/22.0.0/x64.complete").chmod(store_mode)
                cache = self.work / "toolcache-pod"
                cache.mkdir()
                self.link_tool_cache(cache)
                marker = cache / "node/22.0.0/x64.complete"
                if pod_mode is not None:
                    marker.chmod(pod_mode)
                self.assertFalse(marker.stat().st_mode & stat.S_IWUSR)
                first = self.snapshot(cache)
                self.link_tool_cache(cache)
                self.assertEqual(self.snapshot(cache), first)
                self.assertTrue(marker.is_file() and not marker.is_symlink())

    def test_the_pods_directories_keep_the_stores_modes_when_those_are_writable(self) -> None:
        arch = self.tool_tree()
        (arch / "bin").chmod(0o750)
        cache = self.work / "toolcache-pod"
        cache.mkdir()
        self.link_tool_cache(cache)
        tree = cache / "node/22.0.0/x64"
        for relative in (".", "bin", "lib", "lib/node_modules/npm", "empty"):
            self.assertEqual(stat.S_IMODE((tree / relative).stat().st_mode), stat.S_IMODE((arch / relative).stat().st_mode), relative)

    def test_linking_fails_when_an_entry_cannot_be_linked(self) -> None:
        self.tool_tree()
        cache = self.work / "toolcache-pod"
        cache.write_text("a file where the tool cache directory should be")
        link = self.containers(["node"])["capability-store-tool-cache"]
        self.assertNotEqual(self.run_container(link, "pod-1", {"/opt/capability-store": str(self.root), TOOL_CACHE: str(cache)}).returncode, 0)


if __name__ == "__main__":
    unittest.main()
