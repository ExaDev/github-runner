"""Tests for how the cluster role pins the versions every node's k3s image is built from: the filters (plugins/filter/cluster_versions.py), the checks in tasks/versions.yml and tasks/running_version.yml (run through the role with tests/cluster_versions/checks.yml), the .env the role writes (tests/cluster_versions/render_env.yml), the Compose file and Dockerfile that consume it, and bootstrap.sh's mapping of .env.bootstrap onto the role's variables."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_cluster"
PLUGIN = ROOT / "plugins" / "filter" / "cluster_versions.py"
_spec = importlib.util.spec_from_file_location("cluster_versions_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
versions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(versions)

ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
COMPOSE = ROLE / "files" / "docker-compose.yml"
DOCKERFILE = ROLE / "files" / "k3s" / "Dockerfile"


def ansible_env() -> dict[str, str]:
    return {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_RETRY_FILES_ENABLED": "false"}


TESTED = DEFAULTS["github_runner_cluster_k3s_tested_version"]


class DefaultsTest(unittest.TestCase):
    def test_the_defaults_are_pinned_tags(self) -> None:
        self.assertEqual(versions.cluster_version_errors(TESTED, DEFAULTS["github_runner_cluster_tailscale_version"]), [])

    def test_the_k3s_version_defaults_to_the_tested_one(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_k3s_version"], "{{ github_runner_cluster_k3s_tested_version }}")


class VersionErrorsTest(unittest.TestCase):
    def test_pinned_tags_pass(self) -> None:
        self.assertEqual(versions.cluster_version_errors("v1.34.1-k3s1", "v1.102.4"), [])

    def test_latest_and_other_floating_or_malformed_values_fail(self) -> None:
        for k3s in ("latest", "", None, "v1.34", "v1.34.1+k3s1", "1.34.1-k3s1", "stable"):
            with self.subTest(k3s=k3s):
                self.assertIn("github_runner_cluster_k3s_version", " ".join(versions.cluster_version_errors(k3s, "v1.102.4")))
        for tailscale in ("latest", "stable", "", "1.102.4", "v1.102", None):
            with self.subTest(tailscale=tailscale):
                self.assertIn("github_runner_cluster_tailscale_version", " ".join(versions.cluster_version_errors("v1.34.1-k3s1", tailscale)))

    def test_both_problems_are_reported(self) -> None:
        self.assertEqual(len(versions.cluster_version_errors("latest", "latest")), 2)


class MinorSkewTest(unittest.TestCase):
    def test_one_version_everywhere_is_no_skew(self) -> None:
        self.assertEqual(versions.cluster_k3s_minor_skew(["a", "b"], {"a": {}, "b": {}}, "v1.34.1-k3s1"), [])

    def test_patch_and_build_differences_within_a_minor_are_no_skew(self) -> None:
        hostvars = {"a": {"github_runner_cluster_k3s_version": "v1.34.1-k3s1"}, "b": {"github_runner_cluster_k3s_version": "v1.34.6-k3s2"}}
        self.assertEqual(versions.cluster_k3s_minor_skew(["a", "b"], hostvars, "v1.34.1-k3s1"), [])

    def test_another_minor_on_one_host_lists_every_host(self) -> None:
        hostvars = {"a": {}, "b": {"github_runner_cluster_k3s_version": "v1.35.0-k3s1"}, "c": {}}
        self.assertEqual(versions.cluster_k3s_minor_skew(["a", "b", "c"], hostvars, "v1.34.1-k3s1"), ["a: v1.34.1-k3s1", "b: v1.35.0-k3s1", "c: v1.34.1-k3s1"])

    def test_another_major_with_the_same_minor_number_is_skew(self) -> None:
        hostvars = {"a": {"github_runner_cluster_k3s_version": "v2.34.0-k3s1"}, "b": {}}
        self.assertNotEqual(versions.cluster_k3s_minor_skew(["a", "b"], hostvars, "v1.34.1-k3s1"), [])

    def test_unusable_input_is_an_error(self) -> None:
        with self.assertRaises(versions.AnsibleFilterError):
            versions.cluster_k3s_minor_skew("a", {}, "v1.34.1-k3s1")

    def test_the_filters_are_registered(self) -> None:
        filters = versions.FilterModule().filters()
        self.assertIs(filters["cluster_version_errors"], versions.cluster_version_errors)
        self.assertIs(filters["cluster_k3s_minor_skew"], versions.cluster_k3s_minor_skew)


class TaskOrderTest(unittest.TestCase):
    def test_the_role_checks_the_versions_before_it_changes_the_host(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        includes = [task.get("ansible.builtin.include_tasks") for task in tasks]
        first_change = next(index for index, task in enumerate(tasks) if "ansible.builtin.file" in task or "ansible.builtin.copy" in task or "block" in task)
        for checks in ("versions.yml",):
            with self.subTest(checks=checks):
                self.assertIn(checks, includes)
                self.assertLess(includes.index(checks), first_change)


class DockerfileTest(unittest.TestCase):
    def test_both_versions_are_build_arguments_without_a_default(self) -> None:
        lines = DOCKERFILE.read_text().splitlines()
        args = [line.split()[1] for line in lines if line.startswith("ARG ")]
        self.assertEqual(sorted(args), ["K3S_VERSION", "TAILSCALE_VERSION"], "an ARG with a default (NAME=value) would let a build float")

    def test_every_base_image_is_taken_from_a_build_argument(self) -> None:
        bases = [line.split()[1] for line in DOCKERFILE.read_text().splitlines() if line.startswith("FROM ")]
        self.assertEqual(bases, ["docker.io/tailscale/tailscale:${TAILSCALE_VERSION}", "rancher/k3s:${K3S_VERSION}"])
        self.assertNotIn(":latest", DOCKERFILE.read_text())


class ComposeFileTest(unittest.TestCase):
    def test_every_service_built_from_the_k3s_context_requires_both_versions(self) -> None:
        # Compose stops at the first unset required variable in the whole file, so the render test below cannot tell whether a second service would also refuse; this reads each service's own arguments.
        services = yaml.safe_load(COMPOSE.read_text())["services"]
        built = {name: service["build"]["args"] for name, service in services.items() if "build" in service}
        self.assertEqual(sorted(built), ["k3s", "k3s-agent"])
        for name, args in built.items():
            for variable in ("K3S_VERSION", "TAILSCALE_VERSION"):
                with self.subTest(service=name, variable=variable):
                    self.assertRegex(args[variable], rf"^\$\{{{variable}:\?[^}}]+\}}$")


def has_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode == 0


@unittest.skipUnless(has_compose(), "docker compose is not installed")
class ComposeTest(unittest.TestCase):
    def render(self, **env: str) -> subprocess.CompletedProcess[str]:
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("DOCKER_", "COMPOSE_", "K3S_", "TAILSCALE_"))}
        args = ["docker", "compose", "--project-directory", tempfile.gettempdir(), "--project-name", "example", "-f", str(COMPOSE), "--profile", "server", "--profile", "agent", "config", "--format", "json"]
        return subprocess.run(args, capture_output=True, text=True, check=False, env=clean | {"K3S_TOKEN": "t", "K3S_TLS_SAN_LIST": "n"} | env)

    def test_both_node_services_build_from_the_pinned_versions(self) -> None:
        result = self.render(K3S_VERSION="v1.34.1-k3s1", TAILSCALE_VERSION="v1.102.4")
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)["services"]
        for name in ("k3s", "k3s-agent"):
            with self.subTest(service=name):
                self.assertEqual(services[name]["build"]["args"], {"K3S_VERSION": "v1.34.1-k3s1", "TAILSCALE_VERSION": "v1.102.4"})

    def test_a_missing_version_refuses_to_render(self) -> None:
        for env, missing in (({"TAILSCALE_VERSION": "v1.102.4"}, "K3S_VERSION"), ({"K3S_VERSION": "v1.34.1-k3s1"}, "TAILSCALE_VERSION")):
            with self.subTest(missing=missing):
                result = self.render(**env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{missing} must be set in .env", result.stderr)


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class RoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)

    def run_checks(self, hosts: dict[str, dict[str, Any]], *extra: str, group_vars: dict[str, Any] | None = None, check_tasks: str = "versions", timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        inventory = self.work / "inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"children": {"github_runner_cluster": {"vars": group_vars or {}, "hosts": hosts}}}}))
        args = [ANSIBLE_PLAYBOOK, "-i", str(inventory), str(ROOT / "tests" / "cluster_versions" / "checks.yml"), "-e", f"check_tasks={check_tasks}", *extra]
        return subprocess.run(args, capture_output=True, text=True, check=False, env=ansible_env(), cwd=self.work, timeout=timeout)

    def skew(self, host_versions: dict[str, str | None], *extra: str) -> subprocess.CompletedProcess[str]:
        hosts = {name: ({"github_runner_cluster_k3s_version": version} if version else {}) for name, version in host_versions.items()}
        return self.run_checks(hosts, *extra)

    def test_the_defaults_everywhere_pass(self) -> None:
        result = self.skew({"node1": None, "node2": None, "node3": None})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_one_version_for_the_whole_group_passes(self) -> None:
        result = self.run_checks({"node1": {}, "node2": {}}, group_vars={"github_runner_cluster_k3s_version": "v1.35.0-k3s1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_latest_fails_before_anything_else(self) -> None:
        result = self.skew({"node1": None}, "-e", "github_runner_cluster_k3s_version=latest")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("never latest", result.stdout)

    def test_a_host_on_another_minor_fails_every_host_including_itself(self) -> None:
        result = self.skew({"node1": "v1.36.0-k3s1", "node2": None, "node3": None})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"node1: v1.36.0-k3s1, node2: {TESTED}, node3: {TESTED}", result.stdout)
        self.assertEqual(sorted(re.findall(r"fatal: \[(node\d)\]", result.stdout)), ["node1", "node2", "node3"])

    def test_the_host_on_another_minor_fails_when_run_on_its_own(self) -> None:
        result = self.skew({"node1": "v1.36.0-k3s1", "node2": None, "node3": None}, "--limit", "node1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fatal: [node1]", result.stdout)

    def render_env(self, *extra: str) -> tuple[list[str], list[str]]:
        env, agent = self.work / "env", self.work / "env-agent"
        args = [ANSIBLE_PLAYBOOK, str(ROOT / "tests" / "cluster_versions" / "render_env.yml"), "-e", f"env_output={env}", "-e", f"env_agent_output={agent}", "-e", "github_runner_cluster_dir=/srv/github-runner", *extra]
        result = subprocess.run(args, capture_output=True, text=True, check=False, env=ansible_env(), cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return env.read_text().splitlines(), agent.read_text().splitlines()

    def test_the_env_files_carry_both_versions(self) -> None:
        for lines in self.render_env("-e", "github_runner_cluster_tailscale_version=v1.90.0"):
            self.assertIn(f"K3S_VERSION={TESTED}", lines)
            self.assertIn("TAILSCALE_VERSION=v1.90.0", lines)

if __name__ == "__main__":
    unittest.main()
