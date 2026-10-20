"""Tests for how the cluster role keeps a macOS host's container runtime running across reboots: the launchd agent template (tests/runtime_autostart/render_plist.yml), the variable defaults, the macOS-only guard in tasks/main.yml, and tasks/runtime.yml and tasks/end_checks.yml run through the role (tests/runtime_autostart/run.yml) against stand-ins for docker, colima, launchctl and plutil."""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_cluster"
RENDER = ROOT / "tests" / "runtime_autostart" / "render_plist.yml"
RUN = ROOT / "tests" / "runtime_autostart" / "run.yml"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
UID = 501
HOME = "/Users/example"
DARWIN_ONLY = "ansible_facts['os_family'] == 'Darwin'"


def ansible_env() -> dict[str, str]:
    return {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_RETRY_FILES_ENABLED": "false"}


def render_plist(**role_vars: Any) -> bytes:
    """Render the launchd agent with ansible-playbook and return the property list."""
    with tempfile.TemporaryDirectory() as work:
        output = Path(work) / "agent.plist"
        extra = Path(work) / "extra.json"
        extra.write_text(json.dumps({"plist_output": str(output), **role_vars}))
        subprocess.run([ANSIBLE_PLAYBOOK, str(RENDER), "-e", f"@{extra}"], check=True, env=ansible_env(), cwd=work, capture_output=True, text=True)
        return output.read_bytes()


class DefaultsTest(unittest.TestCase):
    def test_the_autostart_is_on_and_the_runtime_is_taken_from_the_docker_context(self) -> None:
        self.assertIs(DEFAULTS["github_runner_cluster_runtime_autostart"], True)
        self.assertEqual(DEFAULTS["github_runner_cluster_runtime"], "auto")
        self.assertEqual(DEFAULTS["github_runner_cluster_colima_profile"], "")

    def test_the_path_names_both_homebrew_prefixes_and_the_system_directories(self) -> None:
        entries = DEFAULTS["github_runner_cluster_runtime_path"].split(":")
        for prefix in ("/opt/homebrew", "/usr/local"):
            self.assertIn(f"{prefix}/bin", entries)
            self.assertIn(f"{prefix}/sbin", entries)
        for system in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            self.assertIn(system, entries)

    def test_the_agent_is_per_profile_and_lives_in_the_connecting_users_launch_agents(self) -> None:
        self.assertIn("{{ github_runner_cluster_effective_runtime.colima_profile }}", DEFAULTS["github_runner_cluster_runtime_launchd_label"])
        plist = DEFAULTS["github_runner_cluster_runtime_agent_plist"]
        self.assertTrue(plist.startswith("{{ ansible_facts['env']['HOME'] }}/Library/LaunchAgents/"))
        self.assertTrue(plist.endswith("{{ github_runner_cluster_runtime_launchd_label }}.plist"))

    def test_the_node_container_follows_the_node_s_role(self) -> None:
        template = DEFAULTS["github_runner_cluster_node_container"]
        self.assertIn("github_runner_cluster_container_name", template)
        self.assertIn("'github-runner-k3s-agent'", template)

    def test_every_new_variable_is_in_the_readme(self) -> None:
        readme = (ROLE / "README.md").read_text()
        for name in ("github_runner_cluster_runtime_autostart", "github_runner_cluster_runtime", "github_runner_cluster_colima_profile", "github_runner_cluster_runtime_path"):
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", readme)


class GuardTest(unittest.TestCase):
    def test_both_includes_run_on_macos_only(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        blocks = [task for task in tasks if "block" in task]
        everything = tasks + [inner for block in blocks for inner in block["block"]]
        for tasks_file in ("runtime.yml", "end_checks.yml"):
            includes = [task for task in everything if task.get("ansible.builtin.include_tasks") == tasks_file]
            with self.subTest(tasks_file=tasks_file):
                self.assertTrue(includes)
                for include in includes:
                    self.assertEqual(include["when"], DARWIN_ONLY)

    def test_the_runtime_is_up_before_anything_reads_the_daemon(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        order = [task.get("ansible.builtin.include_tasks") or task.get("ansible.builtin.command") for task in tasks]
        self.assertLess(order.index("runtime.yml"), order.index("versions.yml"))
        self.assertLess(order.index("runtime.yml"), order.index("running_version.yml"))
        self.assertLess(order.index("docker_context.yml"), order.index("runtime.yml"))

    def test_the_end_checks_follow_the_compose_start_on_servers_and_agents(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        for role in ("server", "agent"):
            [block] = [task for task in tasks if "block" in task and task.get("when") == f'github_runner_cluster_effective_node_role == "{role}"']
            steps = [step.get("ansible.builtin.include_tasks") or ("compose" if "community.docker.docker_compose_v2" in step else None) for step in block["block"]]
            with self.subTest(role=role):
                self.assertLess(steps.index("compose"), steps.index("end_checks.yml"))


class PlistTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = render_plist()
        cls.plist = plistlib.loads(cls.document)

    def test_it_starts_the_default_profile_without_changing_the_current_docker_context(self) -> None:
        self.assertEqual(self.plist["ProgramArguments"], ["/usr/bin/env", "colima", "start", "--profile", "default", "--activate=false"])

    def test_it_runs_once_at_login(self) -> None:
        self.assertIs(self.plist["RunAtLoad"], True)
        self.assertNotIn("KeepAlive", self.plist)

    def test_the_vm_outlives_the_job_that_started_it(self) -> None:
        # colima start returns once the VM is up, leaving lima's host agent running; launchd kills what a finished job leaves in its process group unless told to abandon it.
        self.assertIs(self.plist["AbandonProcessGroup"], True)

    def test_the_path_is_the_roles_with_both_homebrew_prefixes(self) -> None:
        self.assertEqual(self.plist["EnvironmentVariables"], {"PATH": DEFAULTS["github_runner_cluster_runtime_path"]})

    def test_the_label_names_the_profile_and_the_log_is_in_the_users_logs(self) -> None:
        self.assertEqual(self.plist["Label"], "com.exadev.github-runner.colima.default")
        self.assertEqual(self.plist["StandardOutPath"], f"{HOME}/Library/Logs/com.exadev.github-runner.colima.default.log")
        self.assertEqual(self.plist["StandardErrorPath"], self.plist["StandardOutPath"])

    def test_another_profile_gets_its_own_label_and_argument(self) -> None:
        plist = plistlib.loads(render_plist(docker_context="colima-example"))
        self.assertEqual(plist["Label"], "com.exadev.github-runner.colima.example")
        self.assertEqual(plist["ProgramArguments"][3:5], ["--profile", "example"])

    def test_a_profile_set_for_a_context_that_does_not_say_is_used(self) -> None:
        plist = plistlib.loads(render_plist(docker_context="", github_runner_cluster_runtime="colima", github_runner_cluster_colima_profile="build"))
        self.assertEqual((plist["Label"], plist["ProgramArguments"][4]), ("com.exadev.github-runner.colima.build", "build"))

    def test_a_path_with_markup_characters_is_escaped_not_injected(self) -> None:
        path = "/opt/a&b:/opt/<c>:/usr/bin"
        document = render_plist(github_runner_cluster_runtime_path=path)
        self.assertEqual(plistlib.loads(document)["EnvironmentVariables"]["PATH"], path)
        self.assertNotIn(b"a&b", document)

    @unittest.skipUnless(shutil.which("plutil"), "plutil is not installed")
    def test_plutil_accepts_it(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".plist") as handle:
            handle.write(self.document)
            handle.flush()
            result = subprocess.run(["plutil", "-lint", handle.name], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_comment_cannot_end_the_document_early(self) -> None:
        # A double hyphen inside an XML comment is not well formed.
        comment = re.search(rb"<!--(.*?)-->", self.document, re.DOTALL)
        assert comment is not None
        self.assertNotIn(b"--", comment.group(1))


# Stands in for docker, colima, launchctl and plutil, which are all this one script under four names. State is files in $RUNTIME_STUB: runtime-up (the Docker daemon answers), session (the user has a login session), loaded (the agent is loaded), <container>.state (what docker ps prints), colima-start-fails; every call is appended to calls.log with the Docker context it ran with.
STUB = """#!{python}
import os, sys
stub = os.environ["RUNTIME_STUB"]
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.path.join(stub, "calls.log"), "a") as log:
    log.write(os.environ.get("DOCKER_CONTEXT", "-") + " " + name + " " + " ".join(args) + "\\n")
def exists(file):
    return os.path.exists(os.path.join(stub, file))
def touch(file):
    open(os.path.join(stub, file), "w").close()
if name == "docker":
    if not exists("runtime-up"):
        sys.exit("Cannot connect to the Docker daemon")
    if args[0] == "info":
        print("27.0.0")
    elif args[0] == "ps":
        container = args[args.index("--filter") + 1].removeprefix("name=^/").removesuffix("$")
        if exists(container + ".state"):
            print(open(os.path.join(stub, container + ".state")).read(), end="")
    else:
        sys.exit("unexpected docker call: " + " ".join(args))
elif name == "colima":
    if args[0] != "start" or args[-1] != "--activate=false":
        sys.exit("unexpected colima call: " + " ".join(args))
    if exists("colima-start-fails"):
        sys.exit("colima: the VM did not start")
    touch("runtime-up")
elif name == "launchctl":
    if args[0] == "print":
        sys.exit(0 if exists("loaded" if args[1].count("/") == 2 else "session") else 113)
    elif args[0] == "bootstrap":
        if not os.path.exists(args[2]):
            sys.exit("Bootstrap failed: no such plist")
        touch("loaded")
    elif args[0] == "bootout":
        os.remove(os.path.join(stub, "loaded"))
    else:
        sys.exit("unexpected launchctl call: " + " ".join(args))
elif name == "plutil":
    if args[0] != "-lint" or b"<plist" not in open(args[1], "rb").read():
        sys.exit("plutil: not a property list")
else:
    sys.exit("unexpected stand-in: " + name)
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
        self.home = self.work / "home"
        self.stub.mkdir()
        self.home.mkdir()
        self.bin = self.work / "bin"
        self.bin.mkdir()
        for name in ("docker", "colima", "launchctl", "plutil"):
            tool = self.bin / name
            tool.write_text(STUB.format(python=sys.executable))
            tool.chmod(0o755)
        self.plist = self.home / "Library" / "LaunchAgents" / "com.exadev.github-runner.colima.default.plist"
        # What `docker ps` prints for the node's container once the runtime answers; the end checks need it running.
        (self.stub / "github-runner-k3s.state").write_text("running")

    def state(self, *flags: str) -> None:
        for flag in flags:
            (self.stub / flag).write_text("")

    def run_role(self, *extra: str, tasks: str = "runtime,end_checks", context: str = "colima", os_family: str = "Darwin", **variables: Any) -> subprocess.CompletedProcess[str]:
        host = {
            "runtime_os_family": os_family,
            "runtime_uid": UID,
            "runtime_home": str(self.home),
            "runtime_tasks": tasks,
            "github_runner_cluster_docker_cli_context": context,
            "github_runner_cluster_docker_cli_environment": {"DOCKER_CONTEXT": context} if context else {},
            "github_runner_cluster_effective_node_role": "server",
            "github_runner_cluster_runtime_path": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            **variables,
        }
        inventory = self.work / "inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"hosts": {"node1": host}}}))
        # The stand-ins come first on the playbook's own PATH as well as the tasks', so a tool a task runs without the role's PATH still cannot reach this machine's real launchd, Colima or Docker.
        env = ansible_env() | {"RUNTIME_STUB": str(self.stub), "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
        return subprocess.run([ANSIBLE_PLAYBOOK, "-i", str(inventory), str(RUN), *extra], capture_output=True, text=True, check=False, env=env, cwd=self.work)

    def calls(self) -> list[str]:
        log = self.stub / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def changes(self) -> list[str]:
        return [call for call in self.calls() if re.match(r"\S+ (colima|launchctl (bootstrap|bootout))", call)]

    def recap(self, result: subprocess.CompletedProcess[str]) -> dict[str, int]:
        match = re.search(r"node1\s+: ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", result.stdout)
        assert match is not None, result.stdout + result.stderr
        return dict(zip(("ok", "changed", "unreachable", "failed"), map(int, match.groups())))

    def test_a_stopped_colima_is_started_and_the_agent_installed_and_loaded(self) -> None:
        self.state("session")
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), ["colima colima start --profile default --activate=false".replace("colima colima", "colima colima"), f"colima launchctl bootstrap gui/{UID} {self.plist}"])
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["ProgramArguments"][3:5], ["--profile", "default"])
        self.assertEqual(self.plist.stat().st_mode & 0o777, 0o644)
        self.assertTrue((self.stub / "loaded").exists())

    def test_every_docker_call_uses_the_hosts_docker_context(self) -> None:
        self.state("session")
        self.run_role(context="colima-example")
        docker_calls = [call for call in self.calls() if " docker " in call]
        self.assertTrue(docker_calls)
        self.assertTrue(all(call.startswith("colima-example ") for call in docker_calls), docker_calls)
        self.assertIn("colima-example colima start --profile example --activate=false", self.calls())

    def test_a_second_run_changes_nothing(self) -> None:
        self.state("session")
        self.run_role()
        before = self.calls()
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual([call for call in self.calls()[len(before) :] if not re.match(r"\S+ (docker (info|ps)|launchctl print)", call)], [])
        self.assertNotIn("bootstrap", " ".join(self.calls()[len(before) :]))

    def test_a_running_runtime_is_not_started_again_but_still_gets_the_agent(self) -> None:
        self.state("session", "runtime-up")
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [f"colima launchctl bootstrap gui/{UID} {self.plist}"])
        self.assertTrue(self.plist.exists())

    def test_a_changed_definition_is_unloaded_before_it_is_loaded(self) -> None:
        self.state("session")
        self.run_role()
        self.plist.write_text(self.plist.read_text().replace("start", "stop"))
        before = len(self.changes())
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes()[before:], [f"colima launchctl bootout gui/{UID}/com.exadev.github-runner.colima.default", f"colima launchctl bootstrap gui/{UID} {self.plist}"])
        self.assertIn("<string>start</string>", self.plist.read_text())

    def test_without_a_login_session_the_agent_is_installed_and_left_for_the_next_login(self) -> None:
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.plist.exists())
        self.assertNotIn("bootstrap", " ".join(self.calls()))
        self.assertIn("loads at the next login", result.stdout)

    def test_a_malformed_property_list_never_replaces_the_installed_one(self) -> None:
        self.state("session")
        self.run_role()
        installed = self.plist.read_bytes()
        (self.bin / "plutil").write_text("#!/bin/sh\nexit 1\n")
        result = self.run_role("-e", "github_runner_cluster_runtime_path=" + f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin{os.pathsep}/changed")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.plist.read_bytes(), installed)

    def test_a_colima_that_will_not_start_fails_the_run_at_that_task(self) -> None:
        self.state("session", "colima-start-fails")
        result = self.run_role()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(failed_task(result.stdout), "Start the Colima profile if it is stopped")
        self.assertFalse(self.plist.exists())

    def test_autostart_off_installs_nothing_and_a_stopped_runtime_fails_with_the_fix(self) -> None:
        self.state("session")
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(failed_task(result.stdout), "Fail unless the container runtime is up")
        self.assertIn("Colima profile default is not running on node1. Start it with `colima start --profile default`", result.stdout)
        self.assertEqual(self.changes(), [])
        self.assertFalse(self.plist.exists())

    def test_autostart_off_with_a_running_runtime_passes(self) -> None:
        self.state("runtime-up")
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])

    def test_docker_desktop_is_never_started_and_a_stopped_one_says_which_setting_to_turn_on(self) -> None:
        self.state("session")
        result = self.run_role(context="desktop-linux")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docker Desktop is not running on node1, and this role cannot start it", result.stdout)
        self.assertIn("Settings > General > Start Docker Desktop when you sign in to your computer", result.stdout)
        self.assertEqual(self.changes(), [])
        self.assertFalse(self.plist.parent.exists())

    def test_a_running_docker_desktop_installs_nothing(self) -> None:
        self.state("session", "runtime-up")
        result = self.run_role(context="desktop-linux")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertFalse(self.plist.parent.exists())

    def test_another_runtime_is_only_checked(self) -> None:
        result = self.run_role(context="orbstack")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("The container runtime is not running on node1", result.stdout)
        self.assertEqual(self.changes(), [])

    def test_a_linux_host_is_not_touched(self) -> None:
        result = self.run_role(os_family="Debian")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.home / "Library").exists())

    def test_check_mode_starts_and_installs_nothing_and_does_not_fail_on_a_stopped_runtime(self) -> None:
        self.state("session")
        result = self.run_role("--check", "--diff")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertFalse(self.plist.exists())
        self.assertIn("com.exadev.github-runner.colima.default", result.stdout, "the diff shows the property list")

    def test_check_mode_reports_a_changed_definition(self) -> None:
        self.state("session")
        self.run_role()
        self.plist.write_text(self.plist.read_text().replace("start", "stop"))
        result = self.run_role("--check", "--diff")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreater(self.recap(result)["changed"], 0)
        self.assertIn("stop", self.plist.read_text())

    def test_the_end_checks_pass_with_the_runtime_up_and_the_container_running(self) -> None:
        self.state("runtime-up")
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_end_checks_fail_when_the_container_is_not_running(self) -> None:
        self.state("runtime-up")
        for state in ("exited", "restarting", ""):
            with self.subTest(state=state):
                (self.stub / "github-runner-k3s.state").write_text(state)
                result = self.run_role(github_runner_cluster_runtime_autostart=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(failed_task(result.stdout), "Fail unless this node's k3s container is running")
                self.assertIn("The k3s container github-runner-k3s on node1 is", result.stdout)
                self.assertIn("docker logs github-runner-k3s", result.stdout)

    def test_the_end_checks_catch_a_runtime_that_went_down_during_the_run(self) -> None:
        result = self.run_role(tasks="end_checks", github_runner_cluster_runtime_autostart=False, github_runner_cluster_effective_runtime={"kind": "colima", "colima_profile": "default"}, github_runner_cluster_runtime_environment={"PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin", "DOCKER_CONTEXT": "colima"}, github_runner_cluster_runtime_down_message="the runtime is down")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(failed_task(result.stdout), "Fail unless the container runtime is up")
        self.assertIn("the runtime is down", result.stdout)

    def test_an_agent_host_is_checked_for_the_agent_container(self) -> None:
        self.state("runtime-up")
        result = self.run_role(github_runner_cluster_runtime_autostart=False, github_runner_cluster_effective_node_role="agent")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("The k3s container github-runner-k3s-agent on node1 is", result.stdout)
        (self.stub / "github-runner-k3s-agent.state").write_text("running")
        self.assertEqual(self.run_role(github_runner_cluster_runtime_autostart=False, github_runner_cluster_effective_node_role="agent").returncode, 0)

    def test_a_container_renamed_by_an_extra_compose_file_is_the_one_checked(self) -> None:
        self.state("runtime-up")
        (self.stub / "example-k3s.state").write_text("running")
        result = self.run_role(github_runner_cluster_runtime_autostart=False, github_runner_cluster_container_name="example-k3s")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
