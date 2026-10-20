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
        self.assertIs(DEFAULTS["github_runner_cluster_allow_k3s_version_jump"], False)


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
        self.assertIs(filters["cluster_k3s_version_change_blockers"], versions.cluster_k3s_version_change_blockers)


def answered(host: str, nodes: str) -> dict[str, Any]:
    """A server's read of the cluster that answered, as the role's loop registers it."""
    return {"item": host, "rc": 0, "stdout": "running\n" + nodes, "stderr": ""}


def stopped(host: str) -> dict[str, Any]:
    return {"item": host, "rc": 0, "stdout": "stopped", "stderr": ""}


def failed(host: str, reason: str = "The connection to the server 127.0.0.1:6443 was refused") -> dict[str, Any]:
    return {"item": host, "rc": 1, "stdout": "running", "stderr": reason}


class VersionChangeBlockersTest(unittest.TestCase):
    OWN = "k3s version v1.34.1+k3s1 (24fc436e)\n"

    def blockers(self, pin: str, own: str = "", reads: list[dict[str, Any]] | None = None) -> list[str]:
        result: list[str] = versions.cluster_k3s_version_change_blockers(pin, "node1", own, reads or [])
        return result

    def test_nothing_running_has_nothing_to_compare(self) -> None:
        self.assertEqual(self.blockers("v1.40.0-k3s1"), [])
        self.assertEqual(self.blockers("v1.40.0-k3s1", reads=[stopped("a"), stopped("b")]), [])

    def test_the_same_minor_and_one_minor_up_pass(self) -> None:
        for pin in ("v1.34.1-k3s1", "v1.34.6-k3s2", "v1.35.0-k3s1"):
            with self.subTest(pin=pin):
                self.assertEqual(self.blockers(pin, self.OWN, [answered("a", "node1 v1.34.1+k3s1\nnode2 v1.34.1+k3s1\n")]), [])

    def test_an_older_minor_than_the_container_runs_is_refused(self) -> None:
        self.assertEqual(self.blockers("v1.33.5-k3s1", self.OWN), ["This node's own container (node1) runs v1.34.1+k3s1, newer than v1.33.5-k3s1; k3s cannot be downgraded."])

    def test_two_minors_above_a_node_is_refused(self) -> None:
        self.assertEqual(self.blockers("v1.36.0-k3s1", reads=[answered("a", "node2 v1.34.1+k3s1\nnode3 v1.35.2+k3s1")]), ["Node node2 runs v1.34.1+k3s1, more than one minor away from v1.36.0-k3s1; k3s upgrades the cluster one minor at a time."])

    def test_another_major_is_refused(self) -> None:
        for pin in ("v2.35.0-k3s1", "v0.36.0-k3s1"):
            with self.subTest(pin=pin):
                self.assertEqual(len(self.blockers(pin, reads=[answered("a", "node2 v1.34.1+k3s1")])), 1)

    def test_a_version_that_cannot_be_read_is_refused(self) -> None:
        self.assertEqual(self.blockers("v1.34.1-k3s1", reads=[answered("a", "node2 <none>")]), ["Node node2 reports '<none>', which is not a k3s version this check can read."])

    def test_every_server_that_answers_counts(self) -> None:
        reads = [stopped("a"), answered("b", "node2 v1.36.0+k3s1 true"), answered("c", "node3 v1.36.0+k3s1")]
        self.assertEqual(self.blockers("v1.34.1-k3s1", reads=reads), ["Server node2 runs v1.36.0+k3s1, a newer minor than v1.34.1-k3s1; the servers cannot go back a minor.", "Agent node3 runs v1.36.0+k3s1, a newer minor than v1.34.1-k3s1; a kubelet may not be newer than the API servers."])

    def test_one_server_that_answers_is_enough(self) -> None:
        self.assertEqual(self.blockers("v1.34.1-k3s1", reads=[failed("a"), {"item": "b", "unreachable": True, "msg": "ssh: connect"}, answered("c", "node1 v1.34.1+k3s1")]), [])

    def test_a_cluster_that_no_server_can_be_read_through_is_refused(self) -> None:
        found = self.blockers("v1.34.1-k3s1", reads=[failed("a"), stopped("b"), {"item": "c", "unreachable": True, "msg": "ssh: connect to host c"}])
        self.assertEqual(found, ["The cluster could not be read through any server of the inventory, so what it runs cannot be told: a could not be read (The connection to the server 127.0.0.1:6443 was refused); c could not be reached (ssh: connect to host c)."])

    def test_an_answer_that_cannot_be_read_counts_as_unread(self) -> None:
        self.assertIn("gave an answer this check cannot read", self.blockers("v1.34.1-k3s1", reads=[{"item": "a", "rc": 0, "stdout": "", "stderr": ""}])[0])

    def test_a_patch_or_build_downgrade_is_refused(self) -> None:
        for pin in ("v1.34.0-k3s1", "v1.34.1-k3s0"):
            with self.subTest(pin=pin):
                self.assertEqual(self.blockers(pin, self.OWN), [f"This node's own container (node1) runs v1.34.1+k3s1, newer than {pin}; k3s cannot be downgraded."])
        self.assertEqual(self.blockers("v1.34.1-k3s2", self.OWN), [])

    def test_a_downgrade_to_an_older_major_is_refused_as_a_downgrade(self) -> None:
        self.assertEqual(self.blockers("v1.40.0-k3s1", reads=[answered("a", "node2 v2.1.0+k3s1 true")]), ["Node node2 runs v2.1.0+k3s1, more than one minor away from v1.40.0-k3s1; k3s upgrades the cluster one minor at a time."])

    def test_a_node_two_servers_report_is_named_once(self) -> None:
        reads = [answered("a", "node2 v1.36.0+k3s1 true"), answered("b", "node2 v1.36.0+k3s1 true")]
        self.assertEqual(len(self.blockers("v1.34.1-k3s1", reads=reads)), 1)

    def test_an_agent_may_not_move_above_any_server(self) -> None:
        reads = [answered("a", "server1 v1.34.1+k3s1 true\nserver2 v1.35.0+k3s1 true\nagent1 v1.34.1+k3s1")]
        found = versions.cluster_k3s_version_change_blockers("v1.35.0-k3s1", "agent1", "", reads, "agent")
        self.assertEqual(found, ["Server server1 runs v1.34.1+k3s1, an older minor than v1.35.0-k3s1; an agent's kubelet may not be newer than the API servers, so upgrade the servers first."])
        self.assertEqual(versions.cluster_k3s_version_change_blockers("v1.35.0-k3s1", "server3", "", reads, "server"), [])
        upgraded = [answered("a", "server1 v1.35.0+k3s1 true\nserver2 v1.35.0+k3s1 true\nagent1 v1.34.1+k3s1")]
        self.assertEqual(versions.cluster_k3s_version_change_blockers("v1.35.0-k3s1", "agent1", "k3s version v1.34.1+k3s1 (x)", upgraded, "agent"), [])
        self.assertEqual(versions.cluster_k3s_version_change_blockers("v1.35.1-k3s1", "agent1", "", upgraded, "agent"), [])

    def test_an_agent_whose_image_is_already_built_may_take_a_pin_above_the_servers(self) -> None:
        reads = [answered("a", "server1 v1.34.1+k3s1 true\nagent1 v1.34.1+k3s1")]
        self.assertEqual(versions.cluster_k3s_version_change_blockers("v1.35.0-k3s1", "agent1", "k3s version v1.34.1+k3s1 (x)", reads, "agent", False), [])

    def test_a_timed_out_read_counts_as_unread_on_every_ansible_core(self) -> None:
        for result in (
            {"item": "a", "failed": True, "timedout": {"period": 2}, "msg": "Task failed: Timed out after 2 second(s)."},
            {"item": "a", "failed": True, "msg": "The ansible.builtin.shell action failed to execute in the expected time frame (2) and was terminated"},
        ):
            with self.subTest(result=result):
                self.assertIn("a did not answer within github_runner_cluster_cluster_read_timeout", self.blockers("v1.34.1-k3s1", reads=[result])[0])

    def test_a_failure_with_only_a_message_names_it(self) -> None:
        found = self.blockers("v1.34.1-k3s1", reads=[{"item": "a", "failed": True, "msg": "docker: command not found"}])
        self.assertIn("a could not be read (docker: command not found)", found[0])

    def test_patch_releases_of_other_nodes_are_not_compared(self) -> None:
        reads = [answered("a", "server1 v1.35.0+k3s1 true\nagent1 v1.35.1+k3s1")]
        self.assertEqual(versions.cluster_k3s_version_change_blockers("v1.35.0-k3s1", "server1", "", reads, "server"), [])
        servers = [answered("a", "server1 v1.35.1+k3s1 true")]
        self.assertEqual(versions.cluster_k3s_version_change_blockers("v1.35.0-k3s1", "agent1", "", servers, "agent"), [])
        self.assertEqual(versions.cluster_k3s_version_change_blockers("v1.35.2-k3s1", "agent1", "", servers, "agent"), [])

    def test_a_server_may_not_be_built_below_a_node_s_minor(self) -> None:
        reads = [answered("a", "server1 v1.34.1+k3s1 true\nagent1 v1.35.0+k3s1")]
        self.assertEqual(versions.cluster_k3s_version_change_blockers("v1.34.1-k3s1", "server1", "", reads, "server"), ["Agent agent1 runs v1.35.0+k3s1, a newer minor than v1.34.1-k3s1; a kubelet may not be newer than the API servers."])

    def test_this_node_s_own_entry_in_the_node_list_counts_as_this_node(self) -> None:
        reads = [answered("a", "server1 v1.35.0+k3s1 true\nserver2 v1.35.0+k3s1 true")]
        self.assertEqual(versions.cluster_k3s_version_change_blockers("v1.34.9-k3s1", "server1", "", reads, "server")[0], "Node server1 runs v1.35.0+k3s1, newer than v1.34.9-k3s1; k3s cannot be downgraded.")

    def test_unusable_input_is_an_error(self) -> None:
        with self.assertRaises(versions.AnsibleFilterError):
            versions.cluster_k3s_version_change_blockers("v1.34.1-k3s1", "node1", "", [], "worker")
        with self.assertRaises(versions.AnsibleFilterError):
            self.blockers("latest")
        with self.assertRaises(versions.AnsibleFilterError):
            versions.cluster_k3s_version_change_blockers("v1.34.1-k3s1", "node1", None, [])
        with self.assertRaises(versions.AnsibleFilterError):
            versions.cluster_k3s_version_change_blockers("v1.34.1-k3s1", "node1", "", ["not a result"])


class TaskOrderTest(unittest.TestCase):
    def test_the_role_checks_the_versions_before_it_changes_the_host(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        includes = [task.get("ansible.builtin.include_tasks") for task in tasks]
        first_change = next(index for index, task in enumerate(tasks) if "ansible.builtin.file" in task or "ansible.builtin.copy" in task or "block" in task)
        for checks in ("versions.yml", "running_version.yml"):
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
        built = {name: service["build"]["args"] for name, service in services.items() if service.get("build", {}).get("context") == "./k3s"}
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


# Stands in for the docker CLI in the running-version checks: answers `docker ps --all --filter name=^/<container>$ --format ...` and `docker exec <container> k3s --version` or `kubectl get nodes` from files in $DOCKER_STUB (<container>.state, <container>.version, <container>.nodes, <container>.kubectl-fails), fails every call when $DOCKER_STUB/daemon-down exists, or daemon-down-<context> for that DOCKER_CONTEXT, sleeps for the seconds hang-<context> holds before answering, answers `docker image ls --quiet <image>` from <image>.image, and logs each call with its DOCKER_CONTEXT.
DOCKER_STUB = """#!/usr/bin/env python3
import os, re, sys, time
stub = os.environ["DOCKER_STUB"]
args = sys.argv[1:]
with open(os.path.join(stub, "calls.log"), "a") as log:
    log.write(os.environ.get("DOCKER_CONTEXT", "-") + " " + " ".join(args) + "\\n")
def read(name):
    path = os.path.join(stub, name)
    return open(path).read() if os.path.exists(path) else None
if read("daemon-down") is not None or read("daemon-down-" + os.environ.get("DOCKER_CONTEXT", "-")) is not None:
    sys.exit("Cannot connect to the Docker daemon")
if read("hang-" + os.environ.get("DOCKER_CONTEXT", "-")) is not None:
    time.sleep(int(read("hang-" + os.environ.get("DOCKER_CONTEXT", "-"))))
if args[0] == "ps":
    container = re.match(r"name=\\^/(.*)\\$$", args[args.index("--filter") + 1]).group(1)
    print(read(container + ".state") or "", end="")
elif args[:3] == ["image", "ls", "--quiet"]:
    print("sha256:0123" if read(args[3] + ".image") is not None else "")
elif args[0] == "exec" and args[2:4] == ["k3s", "--version"]:
    print(read(args[1] + ".version"))
elif args[0] == "exec" and args[2:4] == ["kubectl", "get"]:
    if read(args[1] + ".kubectl-fails") is not None:
        sys.exit("The connection to the server 127.0.0.1:6443 was refused")
    print(read(args[1] + ".nodes"), end="")
else:
    sys.exit("unexpected docker call: " + " ".join(args))
"""


def failed_task(stdout: str) -> str | None:
    """Return the name of the task whose header last precedes a "fatal:" line in ansible-playbook's output, or None when nothing failed."""
    task = None
    for line in stdout.splitlines():
        header = re.match(r"TASK \[(?:[^:\]]+ : )?(.*)\] \*+$", line)
        if header:
            task = header.group(1)
        elif line.startswith("fatal: "):
            return task
    return None


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class RoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.stub = self.work / "stub"
        (self.work / "bin").mkdir()
        self.stub.mkdir()
        docker = self.work / "bin" / "docker"
        docker.write_text(DOCKER_STUB)
        docker.chmod(0o755)

    def run_checks(self, hosts: dict[str, dict[str, Any]], *extra: str, group_vars: dict[str, Any] | None = None, check_tasks: str = "versions", timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        inventory = self.work / "inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"children": {"github_runner_cluster": {"vars": group_vars or {}, "hosts": hosts}}}}))
        args = [ANSIBLE_PLAYBOOK, "-i", str(inventory), str(ROOT / "tests" / "cluster_versions" / "checks.yml"), "-e", f"check_tasks={check_tasks}", *extra]
        env = ansible_env() | {"PATH": f"{self.work / 'bin'}{os.pathsep}{os.environ['PATH']}", "DOCKER_STUB": str(self.stub)}
        return subprocess.run(args, capture_output=True, text=True, check=False, env=env, cwd=self.work, timeout=timeout)

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

    def running(self, *extra: str, pin: str | None = None, run_on: str = "node2", roles: dict[str, str] | None = None, bootstrap: str | None = "node1", unreachable: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        """Run tasks/running_version.yml on one host of a three-host group (servers unless ``roles`` says otherwise) whose topology names ``bootstrap`` as its bootstrap host, or none; ``unreachable`` names a host reached over SSH on a port nothing listens on."""
        roles = {"node1": "server", "node2": "server", "node3": "server", **(roles or {})}
        topology = {"hosts": {name: {"role": role, "bootstrap": name == bootstrap, "node_name": name} for name, role in roles.items()}}
        common: dict[str, Any] = {"github_runner_cluster_topology": topology, **({"github_runner_cluster_k3s_version": pin} if pin else {})}
        hosts = {
            name: {
                "github_runner_cluster_effective_node_role": role,
                "github_runner_cluster_effective_node_name": name,
                "github_runner_cluster_container_name": f"{name}-k3s",
                "github_runner_cluster_docker_context": f"ctx-{name}",
                "github_runner_cluster_docker_cli_environment": {"DOCKER_CONTEXT": f"ctx-{name}"},
            }
            for name, role in roles.items()
        }
        if unreachable:
            hosts[unreachable] |= {"ansible_connection": "ssh", "ansible_host": "127.0.0.1", "ansible_port": 1}
        return self.run_checks(hosts, "--limit", run_on, *extra, group_vars=common, check_tasks="running_version", timeout=timeout)

    def container(self, name: str, version: str | None = None, nodes: str = "", kubectl_fails: bool = False) -> None:
        (self.stub / f"{name}.state").write_text("running" if version else "exited")
        if version:
            (self.stub / f"{name}.version").write_text(f"k3s version {version} (24fc436e)")
            (self.stub / f"{name}.nodes").write_text(nodes)
        if kubectl_fails:
            (self.stub / f"{name}.kubectl-fails").write_text("")

    def calls(self) -> list[str]:
        log = self.stub / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_a_first_bootstrap_with_nothing_running_passes_and_says_so(self) -> None:
        result = self.running(pin="v1.40.0-k3s1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ("node1", "node2", "node3"):
            self.assertIn(f"ctx-{name} ps --all --filter name=^/{name}-k3s$ --format {{{{.State}}}}", self.calls())
        self.assertIn("runs its container, so there is no cluster to compare with yet", result.stdout)

    def test_a_read_that_answers_says_nothing_about_a_first_bootstrap(self) -> None:
        self.container("node1-k3s", "v1.34.1+k3s1", "node1 v1.34.1+k3s1 true\n")
        result = self.running()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("no cluster to compare with yet", result.stdout)
        self.assertNotIn("only this node's own container was compared", result.stdout)

    def test_a_hung_docker_daemon_on_another_server_is_cut_off_and_named(self) -> None:
        self.container("node1-k3s", "v1.34.1+k3s1", "node1 v1.34.1+k3s1 true\n")
        (self.stub / "hang-ctx-node3").write_text("60")
        result = self.running("-e", "github_runner_cluster_cluster_read_timeout=2", roles={"node2": "agent"}, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (self.stub / "daemon-down-ctx-node1").write_text("")
        result = self.running("-e", "github_runner_cluster_cluster_read_timeout=2", roles={"node2": "agent"}, timeout=40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("node3 did not answer within github_runner_cluster_cluster_read_timeout", result.stdout)

    def test_a_hung_docker_daemon_on_this_host_is_cut_off_at_its_own_read(self) -> None:
        (self.stub / "hang-ctx-node2").write_text("60")
        result = self.running("-e", "github_runner_cluster_cluster_read_timeout=2", timeout=40)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(failed_task(result.stdout), "Read the k3s version this node's running container reports")

    def test_the_documented_upgrade_passes_servers_first_then_agents(self) -> None:
        """README, Versions: the new pin is written to every host by one run of the role, then the servers are rebuilt by hand, then the agents; the role runs on the whole group at each step."""
        roles = {"node1": "server", "node2": "agent", "node3": "server"}
        def cluster(server: str, agent: str) -> None:
            nodes = f"node1 {server} true\nnode2 {agent}\nnode3 {server} true\n"
            for name, version in (("node1-k3s", server), ("node3-k3s", server)):
                self.container(name, version, nodes)
            self.container("github-runner-k3s-agent", agent)
            (self.stub / "github-runner-k3s-agent.image").write_text("")
        for server, agent in (("v1.34.1+k3s1", "v1.34.1+k3s1"), ("v1.35.0+k3s1", "v1.34.1+k3s1"), ("v1.35.0+k3s1", "v1.35.0+k3s1")):
            with self.subTest(servers=server, agent=agent):
                cluster(server, agent)
                result = self.running(pin="v1.35.0-k3s1", roles=roles, run_on="all")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("ctx-node2 image ls --quiet github-runner-k3s-agent", self.calls())
                self.assertEqual(sorted(set(re.findall(r"skipping: \[(node\d)\]", result.stdout.split("Refuse a k3s version outside the upgrade path")[-1]))), ["node1", "node2", "node3"])

    def test_an_agent_built_above_the_servers_is_refused(self) -> None:
        roles = {"node1": "server", "node2": "agent", "node3": "server"}
        self.container("node1-k3s", "v1.34.1+k3s1", "node1 v1.34.1+k3s1 true\nnode3 v1.34.1+k3s1 true\n")
        result = self.running(pin="v1.35.0-k3s1", roles=roles)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Server node1 runs v1.34.1+k3s1, an older minor than v1.35.0-k3s1; an agent's kubelet may not be newer than the API servers", result.stdout)
        self.assertIn("ctx-node2 image ls --quiet github-runner-k3s-agent", self.calls())

    def test_a_read_timeout_that_is_not_a_positive_whole_number_is_refused(self) -> None:
        result = self.running("-e", '{"github_runner_cluster_cluster_read_timeout": "30\\n"}')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("it must be a positive", result.stdout)
        for value in ("0", "30s", "-1"):
            with self.subTest(value=value):
                result = self.running("-e", f"github_runner_cluster_cluster_read_timeout={value}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("it must be a positive", result.stdout)

    def test_an_unreachable_server_is_named_and_another_server_still_answers(self) -> None:
        self.container("node1-k3s", "v1.32.0+k3s1", "node1 v1.32.0+k3s1 true\n")
        result = self.running(roles={"node2": "agent"}, unreachable="node3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Node node1 runs v1.32.0+k3s1, more than one minor away", result.stdout)
        (self.stub / "node1-k3s.kubectl-fails").write_text("")
        result = self.running(roles={"node2": "agent"}, unreachable="node3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("node3 could not be reached", result.stdout)

    def test_a_server_reads_the_cluster_through_every_server_with_their_contexts(self) -> None:
        nodes = "node1 v1.34.1+k3s1\nnode2 v1.34.1+k3s1\nnode3 v1.34.1+k3s1\n"
        for name in ("node1", "node2", "node3"):
            self.container(f"{name}-k3s", "v1.34.1+k3s1", nodes)
        result = self.running(pin="v1.35.0-k3s1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ctx-node2 exec node2-k3s k3s --version", self.calls())
        for name in ("node1", "node2", "node3"):
            self.assertIn(f"ctx-{name} exec {name}-k3s kubectl get nodes --request-timeout=30s -o jsonpath={{range .items[*]}}{{.metadata.name}}{{\" \"}}{{.status.nodeInfo.kubeletVersion}}{{\" \"}}{{.metadata.labels.node-role\\.kubernetes\\.io/control-plane}}{{\"\\n\"}}{{end}}", self.calls())

    def test_a_version_two_minors_above_the_cluster_is_refused(self) -> None:
        self.container("node2-k3s", "v1.34.1+k3s1", "node1 v1.34.1+k3s1\nnode2 v1.34.1+k3s1\n")
        result = self.running(pin="v1.36.0-k3s1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("more than one minor away from v1.36.0-k3s1", result.stdout)
        self.assertIn("github_runner_cluster_allow_k3s_version_jump=true", result.stdout)

    def test_both_reads_run_under_check(self) -> None:
        for role, own in (("server", "v1.34.1+k3s1"), ("agent", None)):
            with self.subTest(role=role):
                self.container("node2-k3s", own, "node1 v1.33.0+k3s1\n")
                self.container("node1-k3s", "v1.34.1+k3s1", "node1 v1.33.0+k3s1\n")
                result = self.running("--check", pin="v1.35.0-k3s1", roles={"node2": role})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Node node1 runs v1.33.0+k3s1", result.stdout)

    def test_the_own_container_read_runs_under_check(self) -> None:
        self.container("node2-k3s", "v1.36.0+k3s1")
        result = self.running("--check", pin="v1.35.0-k3s1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("This node's own container (node2) runs v1.36.0+k3s1", result.stdout)

    def test_the_default_is_refused_on_a_cluster_that_runs_a_newer_minor(self) -> None:
        self.container("node2-k3s", "v1.35.2+k3s1", "node1 v1.35.2+k3s1\nnode2 v1.35.2+k3s1\n")
        result = self.running()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"newer than {TESTED}; k3s cannot be downgraded", result.stdout)

    def test_a_stopped_bootstrap_server_does_not_hide_a_newer_cluster(self) -> None:
        self.container("node1-k3s")
        for name in ("node2", "node3"):
            self.container(f"{name}-k3s", "v1.36.0+k3s1", "node1 v1.34.1+k3s1 true\nnode2 v1.36.0+k3s1 true\nnode3 v1.36.0+k3s1 true\n")
        result = self.running(run_on="node1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"Server node2 runs v1.36.0+k3s1, a newer minor than {TESTED}", result.stdout)

    def test_a_bootstrap_server_whose_api_fails_does_not_stop_a_read_through_another(self) -> None:
        self.container("node1-k3s", "v1.34.1+k3s1", kubectl_fails=True)
        self.container("node3-k3s", "v1.34.1+k3s1", "node1 v1.34.1+k3s1\nnode3 v1.34.1+k3s1\n")
        result = self.running(roles={"node2": "agent"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_cluster_no_server_can_be_read_through_is_refused_and_the_override_skips_the_reads(self) -> None:
        self.container("node1-k3s", "v1.34.1+k3s1", kubectl_fails=True)
        (self.stub / "daemon-down-ctx-node3").write_text("")
        result = self.running(roles={"node2": "agent"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("The cluster could not be read through any server of the inventory", result.stdout)
        self.assertIn("node1 could not be read (The connection to the server 127.0.0.1:6443 was refused)", result.stdout)
        self.assertIn("node3 could not be read (Cannot connect to the Docker daemon)", result.stdout)
        (self.stub / "calls.log").unlink()
        result = self.running("-e", "github_runner_cluster_allow_k3s_version_jump=true", roles={"node2": "agent"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_the_override_lets_one_run_past_the_refusal(self) -> None:
        self.container("node2-k3s", "v1.35.2+k3s1", "node2 v1.35.2+k3s1\n")
        result = self.running("-e", "github_runner_cluster_allow_k3s_version_jump=true")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_an_agent_reads_the_cluster_through_the_servers_with_their_contexts(self) -> None:
        self.container("node1-k3s", "v1.34.1+k3s1", "node1 v1.34.1+k3s1\nnode2 v1.33.0+k3s1\n")
        result = self.running(pin="v1.35.0-k3s1", roles={"node2": "agent"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Node node2 runs v1.33.0+k3s1", result.stdout)
        self.assertIn("ctx-node2 ps --all --filter name=^/github-runner-k3s-agent$ --format {{.State}}", self.calls())
        self.assertFalse([call for call in self.calls() if "node2-k3s" in call], "an agent read a server container on its own host")

    def test_a_group_with_no_bootstrap_host_reads_its_servers(self) -> None:
        self.container("node3-k3s", "v1.32.0+k3s1", "node3 v1.32.0+k3s1 true\n")
        result = self.running(roles={"node2": "agent"}, bootstrap=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Node node3 runs v1.32.0+k3s1, more than one minor away", result.stdout)

    def test_a_running_server_that_cannot_list_its_nodes_alone_refuses(self) -> None:
        self.container("node2-k3s", "v1.34.1+k3s1", kubectl_fails=True)
        result = self.running(pin="v1.34.1-k3s1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("node2 could not be read", result.stdout)

    def test_a_docker_daemon_that_cannot_be_reached_fails_the_run(self) -> None:
        (self.stub / "daemon-down").write_text("")
        result = self.running(pin="v1.34.1-k3s1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(failed_task(result.stdout), "Read the k3s version this node's running container reports")

    def bootstrap_shaped(self, role: str) -> subprocess.CompletedProcess[str]:
        """The topology and version checks on the one-host inventory bootstrap.sh builds: group all, joining a server outside the inventory."""
        inventory = self.work / "bootstrap.ini"
        inventory.write_text("localhost ansible_connection=local\n")
        variables = self.work / "bootstrap.json"
        variables.write_text(json.dumps({
            "github_runner_cluster_group": "all",
            "github_runner_cluster_node_role": role,
            "github_runner_cluster_node_name": "joiner",
            "github_runner_cluster_server_url": "https://server.example.internal:6443",
            "github_runner_cluster_tls_sans": [],
            "github_runner_cluster_docker_cli_environment": {},
            "check_tasks": "topology,running_version",
            "check_hosts": "all",
        }))
        args = [ANSIBLE_PLAYBOOK, "-i", str(inventory), str(ROOT / "tests" / "cluster_versions" / "checks.yml"), "-e", f"@{variables}"]
        env = ansible_env() | {"PATH": f"{self.work / 'bin'}{os.pathsep}{os.environ['PATH']}", "DOCKER_STUB": str(self.stub)}
        return subprocess.run(args, capture_output=True, text=True, check=False, env=env, cwd=self.work)

    def test_a_bootstrap_sh_agent_compares_only_its_own_container_and_says_why(self) -> None:
        self.container("github-runner-k3s-agent", "v1.36.0+k3s1")
        result = self.bootstrap_shaped("agent")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("This node's own container (joiner) runs v1.36.0+k3s1", result.stdout)
        self.assertIn("No server of all is in the inventory", result.stdout)
        (self.stub / "github-runner-k3s-agent.state").write_text("exited")
        result = self.bootstrap_shaped("agent")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_bootstrap_sh_joining_server_reads_its_own_container(self) -> None:
        result = self.bootstrap_shaped("server")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all joins a cluster whose other servers are outside the inventory, and none of its own servers runs its container", result.stdout)
        self.assertNotIn("no cluster to compare with yet", result.stdout)
        self.container("github-runner-k3s", "v1.34.1+k3s1", "joiner v1.34.1+k3s1 true\nother v1.36.0+k3s1 true\n")
        result = self.bootstrap_shaped("server")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Server other runs v1.36.0+k3s1, a newer minor than", result.stdout)

    def test_a_compose_file_path_with_a_colon_is_refused(self) -> None:
        for extra in ('{"github_runner_cluster_compose_files": ["extra:1.yml"]}', '{"github_runner_cluster_dir": "/srv/node:1"}'):
            with self.subTest(extra=extra):
                result = self.run_checks({"node1": {"github_runner_cluster_dir": "/srv/node1"}}, "-e", extra, check_tasks="compose_files")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cannot hold a path with a colon in it", result.stdout)
        result = self.run_checks({"node1": {"github_runner_cluster_dir": "/srv/node1"}}, "-e", '{"github_runner_cluster_compose_files": ["/srv/extra.yml"]}', check_tasks="compose_files")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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

    def test_the_env_files_name_the_compose_files_and_project(self) -> None:
        for lines in self.render_env("-e", '{"github_runner_cluster_compose_files": ["compose.extra.yml", "/elsewhere/absolute.yml"], "github_runner_cluster_compose_project": "example"}'):
            self.assertIn("COMPOSE_FILE=/srv/github-runner/docker-compose.yml:/srv/github-runner/compose.extra.yml:/elsewhere/absolute.yml", lines)
            self.assertIn("COMPOSE_PROJECT_NAME=example", lines)

    @unittest.skipUnless(has_compose(), "docker compose is not installed")
    def test_a_compose_command_run_by_hand_uses_the_role_s_files_and_project(self) -> None:
        project = self.work / "project"
        project.mkdir()
        server_env, _ = self.render_env("-e", json.dumps({"github_runner_cluster_compose_files": ["compose.extra.yml"], "github_runner_cluster_compose_project": "example", "github_runner_cluster_dir": str(project)}))
        shutil.copy(COMPOSE, project / "docker-compose.yml")
        (project / "compose.extra.yml").write_text("services:\n  k3s:\n    labels:\n      example.extra: applied\n")
        (project / ".env").write_text("\n".join(server_env) + "\n")
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("DOCKER_", "COMPOSE_", "K3S_", "TAILSCALE_"))}
        for cwd, args in ((project, []), (self.work, ["--project-directory", str(project)])):
            with self.subTest(cwd=str(cwd)):
                result = subprocess.run(["docker", "compose", *args, "--profile", "server", "config", "--format", "json"], capture_output=True, text=True, check=False, cwd=cwd, env=clean)
                self.assertEqual(result.returncode, 0, result.stderr)
                config = json.loads(result.stdout)
                self.assertEqual(config["name"], "example")
                self.assertEqual(config["services"]["k3s"]["labels"], {"example.extra": "applied"})


class BootstrapTest(unittest.TestCase):
    """bootstrap.sh run against stand-ins for its virtualenv's ansible-galaxy and ansible-playbook, which record the variables file it passes."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        shutil.copy(ROOT / "bootstrap.sh", self.work / "bootstrap.sh")
        (self.work / "ansible").mkdir()
        venv = self.work / ".ansible-venv" / "bin"
        venv.mkdir(parents=True)
        for name, body in (("ansible-galaxy", "exit 0"), ("ansible-playbook", 'for arg in "$@"; do case "$arg" in @*) cp "${arg#@}" "$CAPTURE";; esac; done')):
            (venv / name).write_text(f"#!/bin/sh\n{body}\n")
            (venv / name).chmod(0o755)

    def variables(self, **settings: str) -> dict[str, Any]:
        (self.work / ".env.bootstrap").write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
        capture = self.work / "vars.json"
        result = subprocess.run(["bash", str(self.work / "bootstrap.sh")], capture_output=True, text=True, check=False, env={**os.environ, "CAPTURE": str(capture)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        loaded: dict[str, Any] = json.loads(capture.read_text())
        return loaded

    @unittest.skipIf(shutil.which("jq") is None, "jq is not installed")
    def test_empty_versions_leave_the_role_s_pinned_defaults(self) -> None:
        variables = self.variables(K3S_TOKEN="t", K3S_VERSION="", TAILSCALE_VERSION="")
        self.assertNotIn("github_runner_cluster_k3s_version", variables)
        self.assertNotIn("github_runner_cluster_tailscale_version", variables)

    @unittest.skipIf(shutil.which("jq") is None, "jq is not installed")
    def test_set_versions_are_passed_through(self) -> None:
        variables = self.variables(K3S_TOKEN="t", K3S_VERSION="v1.35.0-k3s1", TAILSCALE_VERSION="v1.90.0")
        self.assertEqual(variables["github_runner_cluster_k3s_version"], "v1.35.0-k3s1")
        self.assertEqual(variables["github_runner_cluster_tailscale_version"], "v1.90.0")


if __name__ == "__main__":
    unittest.main()
