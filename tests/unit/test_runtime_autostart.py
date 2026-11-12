"""Tests for how the cluster role keeps a macOS host's container runtime running across reboots: the launchd agent and daemon template (tests/runtime_autostart/render_plist.yml), the variable defaults, the macOS-only guard in tasks/main.yml, and tasks/runtime.yml and tasks/end_checks.yml run through the role (tests/runtime_autostart/run.yml) against stand-ins for docker, colima, launchctl, plutil and sudo."""

from __future__ import annotations

import grp
import json
import os
import platform
import plistlib
import pwd
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
HOST_OS = ROOT / "tests" / "runtime_autostart" / "host_os.yml"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
UID = 501
HOME = "/Users/example"
DARWIN_ONLY = "ansible_facts['system'] == 'Darwin'"
MODULES_FILE = "/etc/modules-load.d/br_netfilter.conf"
SYSCTL_FILE = "/etc/sysctl.d/90-github-runner-br-netfilter.conf"


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

    def test_the_node_profile_starts_at_boot_and_any_other_at_login_unless_a_host_says_so(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_runtime_autostart_at"], "auto")
        self.assertEqual(DEFAULTS["github_runner_cluster_runtime_boot_profile"], "node")

    def test_the_daemon_is_in_the_system_launch_daemons_directory_and_owned_by_root(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_runtime_daemon_plist"], "/Library/LaunchDaemons/{{ github_runner_cluster_runtime_launchd_label }}.plist")
        self.assertEqual((DEFAULTS["github_runner_cluster_runtime_daemon_owner"], DEFAULTS["github_runner_cluster_runtime_daemon_group"]), ("root", "wheel"))

    def test_the_node_container_follows_the_node_s_role(self) -> None:
        template = DEFAULTS["github_runner_cluster_node_container"]
        self.assertIn("github_runner_cluster_container_name", template)
        self.assertIn("github_runner_cluster_agent_container_name", template)

    def test_every_new_variable_is_in_the_readme(self) -> None:
        readme = (ROLE / "README.md").read_text()
        for name in ("github_runner_cluster_runtime_autostart", "github_runner_cluster_runtime_autostart_at", "github_runner_cluster_runtime_boot_profile", "github_runner_cluster_runtime", "github_runner_cluster_colima_profile", "github_runner_cluster_runtime_path"):
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", readme)


class GuardTest(unittest.TestCase):
    def test_the_vm_needs_the_module_and_both_bridge_sysctls_on(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_colima_kernel_module"], "br_netfilter")
        self.assertEqual(DEFAULTS["github_runner_cluster_colima_sysctl_settings"], {"net.bridge.bridge-nf-call-iptables": 1, "net.bridge.bridge-nf-call-ip6tables": 1})
        self.assertEqual(DEFAULTS["github_runner_cluster_colima_bridge_proc_dir"], "/proc/sys/net/bridge")
        self.assertEqual(DEFAULTS["github_runner_cluster_colima_modules_load_file"], MODULES_FILE)
        self.assertEqual(DEFAULTS["github_runner_cluster_colima_sysctl_file"], SYSCTL_FILE)

    def test_the_vm_is_prepared_last_in_the_runtime_tasks_and_only_for_colima_once_it_answers(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "runtime.yml").read_text())
        names = [task.get("ansible.builtin.include_tasks") or ("fail" if "ansible.builtin.fail" in task else None) for task in tasks]
        self.assertEqual(names[-3:], ["fail", "colima_kernel.yml", "colima_route_guard.yml"])
        for task in tasks[-2:]:
            self.assertEqual(task["when"], ["github_runner_cluster_effective_runtime.kind == 'colima'", "github_runner_cluster_runtime_up | bool"])
            self.assertNotIn("github_runner_cluster_runtime_autostart", json.dumps(task))

    def test_the_readmes_say_what_the_role_does_to_the_vm(self) -> None:
        readme = (ROLE / "README.md").read_text()
        self.assertIn("**Colima kernel.**", readme)
        self.assertIn(MODULES_FILE, readme)
        self.assertIn(SYSCTL_FILE, readme)
        self.assertIn("colima_kernel.yml", (ROOT / "docs" / "non-obvious-behaviour.md").read_text())

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

    def test_the_operating_system_is_read_before_either_condition_uses_it(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        order = [task.get("ansible.builtin.include_tasks") for task in tasks]
        self.assertLess(order.index("host_os.yml"), order.index("runtime.yml"))
        self.assertNotIn("when", tasks[order.index("host_os.yml")])

    def test_the_runtime_tasks_gather_the_user_and_environment_they_use_when_the_caller_did_not(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "runtime.yml").read_text())
        gather = tasks[0]["ansible.builtin.setup"]
        self.assertEqual(gather["gather_subset"], ["!all", "!min", "user", "env"])
        self.assertEqual(tasks[0]["when"], "'user_uid' not in ansible_facts or 'env' not in ansible_facts")
        used = "".join(path.read_text() for path in sorted((ROLE / "tasks").glob("runtime*.yml"))) + (ROLE / "defaults" / "main.yml").read_text() + (ROLE / "templates" / "colima-launchd.plist.j2").read_text()
        # user_id is in the user subset gathered above, with user_uid.
        self.assertEqual(set(re.findall(r"ansible_facts\['(\w+)'\]", used)) - {"system"}, {"user_uid", "user_id", "env"})

    def test_the_runtime_is_up_before_anything_reads_the_daemon(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        order = [task.get("ansible.builtin.include_tasks") or task.get("ansible.builtin.command") for task in tasks]
        self.assertLess(order.index("runtime.yml"), order.index("versions.yml"))
        self.assertLess(order.index("runtime.yml"), order.index("running_version.yml"))
        self.assertLess(order.index("docker_context.yml"), order.index("runtime.yml"))

    def test_the_end_checks_follow_the_compose_start_on_servers_and_agents(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        for role in ("server", "agent"):
            [block] = [task for task in tasks if "block" in task and task.get("when") == f'github_runner_cluster_docker_role == "{role}"']
            # A server starts through its turn (tasks/start_servers.yml) and an agent through tasks/start_agent.yml, each of which runs the Compose start.
            steps = [("compose" if step.get("ansible.builtin.include_tasks") in ("start_servers.yml", "start_agent.yml") else step.get("ansible.builtin.include_tasks")) or ("compose" if "community.docker.docker_compose_v2" in step else None) for step in block["block"]]
            with self.subTest(role=role):
                self.assertLess(steps.index("compose"), steps.index("end_checks.yml"))


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class NoGatheredFactsTest(unittest.TestCase):
    """The mesh integration tests' plays gather no facts, so the macOS condition must not depend on any the caller gathered."""

    def run_play(self, *extra: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as work:
            return subprocess.run([ANSIBLE_PLAYBOOK, str(HOST_OS), *extra], capture_output=True, text=True, check=False, env=ansible_env(), cwd=work)

    def test_the_play_uses_the_condition_main_yml_uses(self) -> None:
        play = yaml.safe_load(HOST_OS.read_text())[0]
        self.assertIn(DARWIN_ONLY, play["tasks"][1]["ansible.builtin.debug"]["msg"])

    def test_the_condition_evaluates_after_the_role_reads_the_operating_system(self) -> None:
        result = self.run_play()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"mac={platform.system() == 'Darwin'}", result.stdout)

    def test_the_same_play_fails_without_those_tasks(self) -> None:
        # What every mesh scenario hit before the role read the operating system itself.
        result = self.run_play("-e", "read_host_os=false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no attribute 'system'", result.stdout + result.stderr)


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

    def test_a_profile_in_a_home_of_its_own_gets_that_home_in_the_agent(self) -> None:
        plist = plistlib.loads(render_plist(docker_context="colima-example", github_runner_cluster_runtime_colima_home="/Users/example/.colima-example"))
        self.assertEqual(plist["EnvironmentVariables"], {"PATH": DEFAULTS["github_runner_cluster_runtime_path"], "COLIMA_HOME": "/Users/example/.colima-example"})

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

    def test_the_login_agent_names_no_user_and_sets_no_home(self) -> None:
        # An agent runs as the user whose session loads it, and launchd ignores UserName for one; nothing of the daemon's keys leaks into it.
        self.assertNotIn("UserName", self.plist)
        self.assertNotIn("HOME", self.plist["EnvironmentVariables"])

    def test_the_node_profile_gets_a_daemon_that_runs_as_the_connecting_user_with_its_home(self) -> None:
        # A daemon is root's unless UserName says otherwise, which would start the VM as root; HOME is set because launchd.plist(5) does not promise one for a daemon and colima and lima take their home directory (the default COLIMA_HOME and the Lima instance directory) from $HOME.
        plist = plistlib.loads(render_plist(docker_context="colima-node", github_runner_cluster_runtime_colima_home="/Users/example/.colima-node"))
        self.assertEqual(plist["Label"], "com.exadev.github-runner.colima.node")
        self.assertEqual(plist["UserName"], "example")
        self.assertEqual(plist["EnvironmentVariables"], {"PATH": DEFAULTS["github_runner_cluster_runtime_path"], "HOME": HOME, "COLIMA_HOME": "/Users/example/.colima-node"})
        self.assertEqual(plist["ProgramArguments"], ["/usr/bin/env", "colima", "start", "--profile", "node", "--activate=false"])
        self.assertIs(plist["RunAtLoad"], True)
        self.assertNotIn("KeepAlive", plist)
        self.assertIs(plist["AbandonProcessGroup"], True)
        self.assertEqual(plist["StandardOutPath"], f"{HOME}/Library/Logs/com.exadev.github-runner.colima.node.log")

    def test_a_host_can_choose_either_kind_for_any_profile(self) -> None:
        self.assertEqual(plistlib.loads(render_plist(github_runner_cluster_runtime_autostart_at="boot"))["UserName"], "example")
        self.assertNotIn("UserName", plistlib.loads(render_plist(docker_context="colima-node", github_runner_cluster_runtime_autostart_at="login")))
        self.assertEqual(plistlib.loads(render_plist(github_runner_cluster_runtime_boot_profile="example", docker_context="colima-example"))["UserName"], "example")

    def test_a_user_name_with_markup_characters_is_escaped_not_injected(self) -> None:
        document = render_plist(docker_context="colima-node", ansible_facts={"user_id": "a&b<c>", "env": {"HOME": HOME}})
        self.assertEqual(plistlib.loads(document)["UserName"], "a&b<c>")

    @unittest.skipUnless(shutil.which("plutil"), "plutil is not installed")
    def test_plutil_accepts_the_daemon(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".plist") as handle:
            handle.write(render_plist(docker_context="colima-node"))
            handle.flush()
            result = subprocess.run(["plutil", "-lint", handle.name], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_daemons_comment_cannot_end_the_document_early_either(self) -> None:
        comment = re.search(rb"<!--(.*?)-->", render_plist(docker_context="colima-node"), re.DOTALL)
        assert comment is not None
        self.assertNotIn(b"--", comment.group(1))

    def test_the_comment_cannot_end_the_document_early(self) -> None:
        # A double hyphen inside an XML comment is not well formed.
        comment = re.search(rb"<!--(.*?)-->", self.document, re.DOTALL)
        assert comment is not None
        self.assertNotIn(b"--", comment.group(1))


# Stands in for docker, colima, launchctl, plutil and sudo, which are all this one script under five names. State is files in $RUNTIME_STUB: runtime-up (the Docker daemon answers), session (the user has a login session), loaded (the agent is loaded in it), daemon-loaded (the daemon is loaded into the system domain, which only root can change), <container>.state (what docker ps prints), colima-start-fails; every call is appended to calls.log with the Docker context it ran with. The Colima VM is the directory vm in $RUNTIME_STUB: its files are under vm/files, /proc/sys/net/bridge exists under vm/files only while br_netfilter is loaded, and a sysctl is the file vm/sysctl/<key> (a bridge key reads 1 once the module is loaded, as the kernel's default, unless the file says otherwise).
STUB = """#!{python}
import os, sys
stub = os.environ["RUNTIME_STUB"]
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
if name == "sudo":
    # Does what sudo does for the role, which is to run the command as root: it marks the command with RUNTIME_ROOT, which the stand-in launchctl needs to change the system domain, and keeps its own calls out of calls.log.
    with open(os.path.join(stub, "sudo.log"), "a") as sudo_log:
        sudo_log.write(" ".join(args) + "\\n")
    while args[0] != "/bin/sh":
        args = args[1:]
    os.environ["RUNTIME_ROOT"] = "1"
    os.execv(args[0], args)
with open(os.path.join(stub, "calls.log"), "a") as log:
    log.write(os.environ.get("DOCKER_CONTEXT", "-") + " " + name + " " + " ".join(args) + "\\n")
with open(os.path.join(stub, "colima-home.log"), "a") as homes:
    homes.write(name + " " + " ".join(args) + " " + os.environ.get("COLIMA_HOME", "-") + "\\n")
def exists(file):
    return os.path.exists(os.path.join(stub, file))
def touch(file):
    open(os.path.join(stub, file), "w").close()
def need_root():
    if os.environ.get("RUNTIME_ROOT") != "1":
        sys.exit("launchctl: Operation not permitted while changing the system domain as a user")
if name == "docker" and args[:2] == ["context", "inspect"]:
    # Read from the context file alone, so it answers with the daemon down.
    if not exists("context-host"):
        sys.exit("context inspect: no context-host file")
    print(open(os.path.join(stub, "context-host")).read(), end="")
elif name == "docker":
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
elif name == "colima" and args[0] == "ssh":
    if not exists("runtime-up"):
        sys.exit("colima: the VM is not running")
    command = args[args.index("--") + 1:]
    sudo = command[0] == "sudo"
    if sudo:
        command = command[1:]
    vm = os.path.join(stub, "vm")
    def vm_file(path):
        return os.path.join(vm, "files", path.lstrip("/"))
    def sysctl_file(key):
        return os.path.join(vm, "sysctl", key)
    if command[0] == "cat":
        if not os.path.exists(vm_file(command[1])):
            sys.exit("cat: " + command[1] + ": No such file or directory")
        print(open(vm_file(command[1])).read(), end="")
    elif command[0] == "test" and command[1] == "-d":
        sys.exit(0 if os.path.isdir(vm_file(command[2])) else 1)
    elif command[0] == "sysctl" and command[1] == "-n":
        if command[2].startswith("net.bridge.") and not os.path.isdir(vm_file("/proc/sys/net/bridge")):
            sys.exit("sysctl: cannot stat /proc/sys/" + command[2].replace(".", "/") + ": No such file or directory")
        print(open(sysctl_file(command[2])).read().strip() if os.path.exists(sysctl_file(command[2])) else "1")
    elif sudo and command[0] == "sysctl" and command[1] == "-w":
        key, value = command[2].split("=")
        if key.startswith("net.bridge.") and not os.path.isdir(vm_file("/proc/sys/net/bridge")):
            sys.exit("sysctl: cannot stat /proc/sys/" + key.replace(".", "/") + ": No such file or directory")
        os.makedirs(os.path.dirname(sysctl_file(key)), exist_ok=True)
        open(sysctl_file(key), "w").write(value)
    elif sudo and command[0] == "modprobe":
        if command[-1] != "br_netfilter":
            sys.exit("modprobe: FATAL: Module " + command[-1] + " not found")
        os.makedirs(vm_file("/proc/sys/net/bridge"), exist_ok=True)
    elif command[0] == "dpkg-query":
        installed = open(os.path.join(vm, "packages")).read().split() if os.path.exists(os.path.join(vm, "packages")) else []
        if command[-1] not in installed:
            sys.exit("dpkg-query: no packages found matching " + command[-1])
        print("install ok installed", end="")
    elif sudo and command[0] == "apt-get" and command[1] == "update":
        os.makedirs(vm, exist_ok=True)
        open(os.path.join(vm, "package-index"), "w").write("updated\\n")
    elif sudo and command[0] == "apt-get" and command[1] == "install":
        # A fresh VM has an empty package index: an install with no update before it fails as apt does.
        if not os.path.exists(os.path.join(vm, "package-index")):
            sys.exit("E: Unable to locate package " + command[-1])
        os.makedirs(vm, exist_ok=True)
        open(os.path.join(vm, "packages"), "a").write(command[-1] + "\\n")
    elif command[0] == "systemctl" and command[1] == "is-active":
        active = open(os.path.join(vm, "active-units")).read().split() if os.path.exists(os.path.join(vm, "active-units")) else []
        print("active" if command[2] in active else "inactive")
        sys.exit(0 if command[2] in active else 3)
    elif sudo and command[0] == "systemctl" and command[1] == "daemon-reload":
        pass
    elif sudo and command[0] == "systemctl" and command[1:3] == ["enable", "--now"]:
        os.makedirs(vm, exist_ok=True)
        open(os.path.join(vm, "active-units"), "a").write(command[3] + "\\n")
    elif sudo and command[0] == "chmod":
        os.chmod(vm_file(command[2]), int(command[1], 8))
    elif sudo and command[0] == "tee":
        os.makedirs(os.path.dirname(vm_file(command[1])), exist_ok=True)
        content = sys.stdin.read()
        open(vm_file(command[1]), "w").write(content)
        print(content, end="")
    else:
        sys.exit("unexpected colima ssh call: " + " ".join(command))
elif name == "colima":
    if args[0] != "start" or args[-1] != "--activate=false":
        sys.exit("unexpected colima call: " + " ".join(args))
    if exists("colima-start-fails"):
        sys.exit("colima: the VM did not start")
    touch("runtime-up")
elif name == "launchctl":
    system = args[1] == "system" or args[1].startswith("system/")
    if args[0] == "print":
        sys.exit(0 if exists("daemon-loaded" if system else "loaded" if args[1].count("/") == 2 else "session") else 113)
    elif args[0] == "bootstrap":
        if not os.path.exists(args[2]):
            sys.exit("Bootstrap failed: no such plist")
        if system:
            need_root()
        touch("daemon-loaded" if system else "loaded")
    elif args[0] == "bootout":
        if system:
            need_root()
        os.remove(os.path.join(stub, "daemon-loaded" if system else "loaded"))
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
        for name in ("docker", "colima", "launchctl", "plutil", "sudo"):
            tool = self.bin / name
            tool.write_text(STUB.format(python=sys.executable))
            tool.chmod(0o755)
        self.plist = self.home / "Library" / "LaunchAgents" / "com.exadev.github-runner.colima.default.plist"
        # Stands in for /Library/LaunchDaemons, which exists on every Mac; the role's own path is narrowed to it below.
        self.daemons = self.work / "LaunchDaemons"
        self.daemons.mkdir()
        # What `docker ps` prints for the node's container once the runtime answers; the end checks need it running.
        (self.stub / "github-runner-k3s.state").write_text("running")

    def state(self, *flags: str) -> None:
        for flag in flags:
            (self.stub / flag).write_text("")

    def run_role(self, *extra: str, tasks: str = "runtime,end_checks", context: str = "colima", context_host: str | None = None, system: str = "Darwin", **variables: Any) -> subprocess.CompletedProcess[str]:
        # The endpoint Colima gives its profile's context: the profile's socket in the default home, unless a test says where else.
        profile = context.removeprefix("colima-") if context.startswith("colima-") else "default"
        (self.stub / "context-host").write_text(context_host if context_host is not None else f"unix://{self.home}/.colima/{profile}/docker.sock")
        host = {
            "runtime_system": system,
            "runtime_uid": UID,
            "runtime_home": str(self.home),
            "runtime_tasks": tasks,
            "github_runner_cluster_docker_cli_context": context,
            "github_runner_cluster_docker_cli_environment": {"DOCKER_CONTEXT": context} if context else {},
            "github_runner_cluster_docker_role": "server",
            "github_runner_cluster_runtime_path": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            # The tests are not root, so the daemon's property list is owned by the user running them and sits beside the stand-in daemon directory.
            "runtime_user": "example",
            "github_runner_cluster_runtime_daemon_plist": f"{self.daemons}/{{{{ github_runner_cluster_runtime_launchd_label }}}}.plist",
            "github_runner_cluster_runtime_daemon_owner": pwd.getpwuid(os.getuid()).pw_name,
            "github_runner_cluster_runtime_daemon_group": grp.getgrgid(os.getgid()).gr_name,
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

    def colima_homes(self) -> list[str]:
        """The COLIMA_HOME (`-` for none) of every colima call, as `<args> <home>` lines."""
        log = self.stub / "colima-home.log"
        return [line.removeprefix("colima ") for line in log.read_text().splitlines() if line.startswith("colima ")] if log.exists() else []

    def assert_colima_ran_in(self, home: str) -> None:
        """Every colima call (the start and the VM's kernel settings alike) ran with `home`, and the start was among them."""
        homes = self.colima_homes()
        self.assertTrue(any(line.startswith("start ") for line in homes), homes)
        self.assertEqual([line for line in homes if not line.endswith(f" {home}")], [])

    def changes(self) -> list[str]:
        return [call for call in self.calls() if re.match(r"\S+ (colima start|launchctl (bootstrap|bootout))", call)]

    def vm_changes(self) -> list[str]:
        """The commands run as root in the Colima VM, in order, without the colima ssh prefix."""
        return [command for command in self.vm_calls() if not self.is_route_guard(command)]

    def vm_calls(self) -> list[str]:
        return [match.group(1) for call in self.calls() if (match := re.match(r"\S+ colima ssh --profile \S+ -- (sudo .*)", call))]

    @staticmethod
    def is_route_guard(command: str) -> bool:
        return "route-guard" in command or "systemctl" in command or "iputils-ping" in command or "apt-get update" in command

    def route_guard_changes(self) -> list[str]:
        """The commands that install the route guard in the VM, in order."""
        return [command for command in self.vm_calls() if self.is_route_guard(command)]

    def vm_file(self, path: str) -> Path:
        return self.stub / "vm" / "files" / path.lstrip("/")

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

    def test_a_profile_in_a_home_of_its_own_is_started_and_loaded_with_that_home(self) -> None:
        # The bridged node profile: context colima-node, profile node, in ~/.colima-node. Without COLIMA_HOME colima would start a new profile of that name in ~/.colima. A login agent is chosen here, since the node profile is started at boot by default.
        self.state("session")
        node_home = f"{self.home}/.colima-node"
        result = self.run_role(context="colima-node", context_host=f"unix://{node_home}/node/docker.sock", github_runner_cluster_runtime_autostart_at="login")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_colima_ran_in(node_home)
        agent = plistlib.loads(self.home.joinpath("Library/LaunchAgents/com.exadev.github-runner.colima.node.plist").read_bytes())
        self.assertEqual(agent["EnvironmentVariables"]["COLIMA_HOME"], node_home)
        self.assertEqual(agent["ProgramArguments"][3:6], ["--profile", "node", "--activate=false"])
        self.assertEqual(list(self.daemons.iterdir()), [])

    LABEL = "com.exadev.github-runner.colima.node"

    @property
    def node_home(self) -> str:
        return f"{self.home}/.colima-node"

    @property
    def node_daemon(self) -> Path:
        return self.daemons / f"{self.LABEL}.plist"

    @property
    def node_agent(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{self.LABEL}.plist"

    def run_node(self, *extra: str, **variables: Any) -> subprocess.CompletedProcess[str]:
        """Run the role for the bridged node profile (context colima-node, in ~/.colima-node), which is started at boot."""
        return self.run_role(*extra, context="colima-node", context_host=f"unix://{self.node_home}/node/docker.sock", **variables)

    def v2_19_agent(self) -> None:
        """A host the previous release set up: the node profile's login agent installed, and loaded when the user has a session."""
        self.node_agent.parent.mkdir(parents=True)
        self.node_agent.write_text("<plist/>")
        if (self.stub / "session").exists():
            self.state("loaded")

    def system_calls(self) -> list[str]:
        """The launchctl calls that change the system domain, which the stand-in refuses without root."""
        return [call for call in self.calls() if re.search(r"launchctl (bootstrap|bootout) system", call)]

    def test_the_node_profile_is_started_by_a_daemon_at_boot_with_nobody_logged_in(self) -> None:
        # The state of a Mac that has rebooted to the login window: no session, so no gui domain to load an agent into.
        result = self.run_node()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), ["colima-node colima start --profile node --activate=false", f"colima-node launchctl bootstrap system {self.node_daemon}"])
        daemon = plistlib.loads(self.node_daemon.read_bytes())
        self.assertEqual((daemon["Label"], daemon["UserName"], daemon["RunAtLoad"]), (self.LABEL, "example", True))
        self.assertEqual(daemon["EnvironmentVariables"], {"PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(self.home), "COLIMA_HOME": self.node_home})
        self.assertEqual(self.node_daemon.stat().st_mode & 0o777, 0o644)
        self.assertTrue((self.stub / "daemon-loaded").exists())
        self.assertFalse(self.node_agent.exists())
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())
        self.assertNotIn("loads at the next login", result.stdout)

    def test_the_daemon_is_written_and_loaded_as_root_and_nothing_else_is(self) -> None:
        self.run_node()
        sudo = (self.stub / "sudo.log").read_text().splitlines()
        self.assertTrue(any(f"{self.LABEL}.plist" in line or "ansible" in line for line in sudo), sudo)
        # The stand-in launchctl refuses the system domain without root, and the run passed, so the load and any unload went through sudo; the read-only calls and the colima start did not need it.
        self.assertEqual(len(self.system_calls()), 1)

    def test_the_daemon_is_also_installed_for_a_user_with_a_login_session_and_the_agent_never_is(self) -> None:
        self.state("session")
        result = self.run_node()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes()[-1], f"colima-node launchctl bootstrap system {self.node_daemon}")
        self.assertFalse((self.stub / "loaded").exists())
        self.assertEqual([call for call in self.calls() if "gui/" in call], [])

    def test_a_running_node_profile_is_not_started_again_when_the_daemon_is_installed(self) -> None:
        self.state("runtime-up")
        result = self.run_node()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [f"colima-node launchctl bootstrap system {self.node_daemon}"])

    def test_a_host_the_previous_release_set_up_moves_from_the_agent_to_the_daemon(self) -> None:
        self.state("session", "runtime-up")
        self.v2_19_agent()
        result = self.run_node()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [f"colima-node launchctl bootstrap system {self.node_daemon}", f"colima-node launchctl bootout gui/{UID}/{self.LABEL}"])
        self.assertFalse(self.node_agent.exists())
        self.assertFalse((self.stub / "loaded").exists())
        self.assertTrue((self.stub / "daemon-loaded").exists())
        self.assertTrue(self.node_daemon.exists())

    def test_an_agent_that_was_never_loaded_because_nobody_logged_in_is_only_deleted(self) -> None:
        # What m1 looked like after its reboot: the agent file on disk, the login window up.
        self.state("runtime-up")
        self.v2_19_agent()
        result = self.run_node()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.node_agent.exists())
        self.assertEqual([call for call in self.calls() if "gui/" in call and "launchctl print gui/" not in call], [])
        self.assertEqual(self.changes(), [f"colima-node launchctl bootstrap system {self.node_daemon}"])

    def test_the_agent_of_another_profile_is_left_alone(self) -> None:
        self.state("session", "runtime-up")
        other = self.home / "Library" / "LaunchAgents" / "com.exadev.github-runner.colima.default.plist"
        other.parent.mkdir(parents=True)
        other.write_text("<plist/>")
        self.run_node()
        self.assertEqual(other.read_text(), "<plist/>")

    def test_a_second_run_with_the_daemon_in_place_changes_nothing_and_loads_nothing(self) -> None:
        self.state("runtime-up")
        self.run_node()
        before = self.calls()
        sudo_before = (self.stub / "sudo.log").read_text()
        result = self.run_node()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual([call for call in self.calls()[len(before):] if not re.match(r"\S+ (docker (info|ps|context inspect)|launchctl print |colima ssh --profile \S+ -- (cat|test|sysctl -n|systemctl is-active|dpkg-query) )", call)], [])
        # The template runs as root to compare ownership; it is the only thing that does, and it changes nothing.
        self.assertEqual(self.system_calls(), [call for call in before if "launchctl bootstrap system" in call])
        self.assertTrue((self.stub / "sudo.log").read_text().startswith(sudo_before))

    def test_a_changed_daemon_is_unloaded_before_it_is_loaded_again(self) -> None:
        self.state("runtime-up")
        self.run_node()
        self.node_daemon.write_text(self.node_daemon.read_text().replace("start", "stop"))
        before = len(self.changes())
        result = self.run_node()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes()[before:], [f"colima-node launchctl bootout system/{self.LABEL}", f"colima-node launchctl bootstrap system {self.node_daemon}"])
        self.assertIn("<string>start</string>", self.node_daemon.read_text())

    def test_a_daemon_that_is_in_place_but_not_loaded_is_loaded(self) -> None:
        self.state("runtime-up")
        self.run_node()
        (self.stub / "daemon-loaded").unlink()
        result = self.run_node()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes()[-1], f"colima-node launchctl bootstrap system {self.node_daemon}")

    def test_check_mode_installs_and_loads_nothing_and_removes_nothing(self) -> None:
        self.state("session", "runtime-up")
        self.v2_19_agent()
        result = self.run_node("--check", "--diff")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertFalse(self.node_daemon.exists())
        self.assertTrue(self.node_agent.exists())
        self.assertIn("UserName", result.stdout, "the diff shows the daemon's property list")
        self.assertGreater(self.recap(result)["changed"], 0)

    def test_a_host_set_back_to_login_retires_the_daemon(self) -> None:
        self.state("session", "runtime-up")
        self.run_node()
        result = self.run_node(github_runner_cluster_runtime_autostart_at="login")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes()[-2:], [f"colima-node launchctl bootstrap gui/{UID} {self.node_agent}", f"colima-node launchctl bootout system/{self.LABEL}"])
        self.assertFalse(self.node_daemon.exists())
        self.assertFalse((self.stub / "daemon-loaded").exists())
        self.assertTrue(self.node_agent.exists())

    def test_a_host_can_ask_for_a_daemon_for_any_profile(self) -> None:
        self.state("runtime-up")
        result = self.run_role(github_runner_cluster_runtime_autostart_at="boot")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(plistlib.loads((self.daemons / "com.exadev.github-runner.colima.default.plist").read_bytes())["UserName"], "example")
        self.assertFalse(self.plist.exists())

    def test_a_value_that_is_not_a_choice_is_refused_before_anything_changes(self) -> None:
        self.state("session")
        result = self.run_node(github_runner_cluster_runtime_autostart_at="sometimes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be auto, login or boot, not sometimes", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_the_default_profile_keeps_its_login_agent_and_never_touches_the_system_domain(self) -> None:
        self.state("session")
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), ["colima colima start --profile default --activate=false", f"colima launchctl bootstrap gui/{UID} {self.plist}"])
        self.assertEqual(list(self.daemons.iterdir()), [])
        self.assertFalse((self.stub / "sudo.log").exists())
        # The VM's own systemd units live under /etc/systemd/system, which is not launchd's system domain.
        self.assertEqual([call for call in self.calls() if "system" in call and "colima ssh" not in call], [])

    def test_autostart_off_leaves_an_installed_daemon_and_agent_as_they_are(self) -> None:
        self.state("runtime-up")
        self.node_daemon.write_text("<plist/>")
        self.v2_19_agent()
        result = self.run_node(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.node_daemon.read_text(), self.node_agent.read_text()), ("<plist/>", "<plist/>"))
        self.assertEqual(self.changes(), [])

    def test_the_variable_wins_over_the_context_and_the_context_is_not_read(self) -> None:
        self.state("session")
        result = self.run_role(context="colima-node", github_runner_cluster_colima_home="/elsewhere")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_colima_ran_in("/elsewhere")
        self.assertFalse([call for call in self.calls() if " docker context " in call])

    def test_a_profile_in_the_default_home_runs_with_no_variable(self) -> None:
        self.state("session")
        self.run_role()
        self.assert_colima_ran_in("-")
        self.assertNotIn("COLIMA_HOME", plistlib.loads(self.plist.read_bytes())["EnvironmentVariables"])

    def test_a_context_that_is_not_the_profiles_socket_fails_before_anything_changes(self) -> None:
        self.state("session")
        result = self.run_role(context="colima-node", context_host="tcp://127.0.0.1:2375")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("set github_runner_cluster_colima_home", result.stdout)
        self.assertEqual(self.changes(), [])

    def test_a_stopped_profile_in_a_home_of_its_own_is_started_under_check_mode_reading_only(self) -> None:
        self.state("session")
        result = self.run_role("--check", context="colima-node", context_host=f"unix://{self.home}/.colima-node/node/docker.sock")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])

    def test_docker_desktop_and_other_runtimes_read_no_context(self) -> None:
        for context in ("desktop-linux", "orbstack"):
            with self.subTest(context=context):
                self.run_role(context=context)
                self.assertFalse([call for call in self.calls() if " docker context " in call])

    def assert_a_second_run_changes_nothing(self, context: str, context_host: str | None) -> None:
        self.state("session")
        self.run_role(context=context, context_host=context_host)
        before = self.calls()
        result = self.run_role(context=context, context_host=context_host)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual([call for call in self.calls()[len(before) :] if not re.match(r"\S+ (docker (info|ps|context inspect)|launchctl print|colima ssh --profile \S+ -- (cat|test|sysctl -n|systemctl is-active|dpkg-query) )", call)], [])
        self.assertNotIn("bootstrap", " ".join(self.calls()[len(before) :]))

    def test_a_second_run_changes_nothing(self) -> None:
        self.assert_a_second_run_changes_nothing("colima", None)

    def test_a_second_run_changes_nothing_for_a_profile_in_a_home_of_its_own(self) -> None:
        self.assert_a_second_run_changes_nothing("colima-node", f"unix://{self.home}/.colima-node/node/docker.sock")

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
        result = self.run_role(system="Linux")
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

    def test_a_colima_vm_without_the_module_gets_the_files_that_load_it_and_the_module(self) -> None:
        self.state("runtime-up")
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.vm_changes(), [f"sudo tee {MODULES_FILE}", f"sudo tee {SYSCTL_FILE}", "sudo modprobe -- br_netfilter"])
        self.assertEqual(self.vm_file(MODULES_FILE).read_text().splitlines()[-1], "br_netfilter")
        self.assertEqual(self.vm_file(SYSCTL_FILE).read_text().splitlines()[1:], ["net.bridge.bridge-nf-call-iptables = 1", "net.bridge.bridge-nf-call-ip6tables = 1"])
        self.assertTrue(self.vm_file("/proc/sys/net/bridge").is_dir())

    def test_a_second_run_changes_nothing_in_the_vm(self) -> None:
        self.state("runtime-up")
        self.run_role(github_runner_cluster_runtime_autostart=False)
        before = self.calls()
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual([call for call in self.calls()[len(before) :] if " sudo " in call], [])

    def test_the_route_guard_is_installed_in_the_vm_with_its_timer_and_started(self) -> None:
        script, unit = DEFAULTS["github_runner_cluster_colima_route_guard_script"], DEFAULTS["github_runner_cluster_colima_route_guard_unit"]
        self.state("runtime-up")
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            self.route_guard_changes(),
            ["sudo apt-get update", "sudo apt-get install -y --no-install-recommends iputils-ping", f"sudo tee {script}", f"sudo tee /etc/systemd/system/{unit}.service", f"sudo tee /etc/systemd/system/{unit}.timer", f"sudo chmod 0755 {script}", "sudo systemctl daemon-reload", f"sudo systemctl enable --now {unit}.timer"],
        )
        self.assertEqual(self.vm_file(script).read_text().strip(), (ROLE / "files" / "route-guard.sh").read_text().strip())
        self.assertEqual(self.vm_file(script).stat().st_mode & 0o777, 0o755)
        timer = self.vm_file(f"/etc/systemd/system/{unit}.timer").read_text()
        self.assertIn(f"OnBootSec={DEFAULTS['github_runner_cluster_colima_route_guard_interval_seconds']}s", timer)
        # systemd's default accuracy of a minute would let a tick slip far past the interval.
        self.assertIn("AccuracySec=1s", timer)
        self.assertIn(f"ExecStart={script}", self.vm_file(f"/etc/systemd/system/{unit}.service").read_text())

    def test_a_second_run_leaves_the_route_guard_alone(self) -> None:
        self.state("runtime-up")
        self.run_role(github_runner_cluster_runtime_autostart=False)
        before = len(self.route_guard_changes())
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.route_guard_changes()[before:], [])

    def test_a_stopped_route_guard_timer_is_started_again_without_rewriting_the_files(self) -> None:
        unit = DEFAULTS["github_runner_cluster_colima_route_guard_unit"]
        self.state("runtime-up")
        self.run_role(github_runner_cluster_runtime_autostart=False)
        (self.stub / "vm" / "active-units").write_text("")
        before = len(self.route_guard_changes())
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.route_guard_changes()[before:], [f"sudo systemctl enable --now {unit}.timer"])

    def test_a_changed_route_guard_script_is_rewritten_and_systemd_reloaded(self) -> None:
        script = DEFAULTS["github_runner_cluster_colima_route_guard_script"]
        self.state("runtime-up")
        self.run_role(github_runner_cluster_runtime_autostart=False)
        self.vm_file(script).write_text("#!/bin/sh\nexit 0\n")
        before = len(self.route_guard_changes())
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.route_guard_changes()[before:], [f"sudo tee {script}", f"sudo chmod 0755 {script}", "sudo systemctl daemon-reload"])

    def test_the_module_is_loaded_again_when_a_restarted_vm_has_lost_it_and_the_files_are_left_alone(self) -> None:
        self.state("runtime-up")
        self.run_role(github_runner_cluster_runtime_autostart=False)
        shutil.rmtree(self.vm_file("/proc"))
        before = len(self.vm_changes())
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.vm_changes()[before:], ["sudo modprobe -- br_netfilter"])

    def test_a_loaded_module_is_not_loaded_again_but_the_files_are_still_written(self) -> None:
        self.state("runtime-up")
        self.vm_file("/proc/sys/net/bridge").mkdir(parents=True)
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.vm_changes(), [f"sudo tee {MODULES_FILE}", f"sudo tee {SYSCTL_FILE}"])

    def test_the_file_a_person_wrote_by_hand_is_replaced_by_the_managed_one(self) -> None:
        self.state("runtime-up")
        self.vm_file(MODULES_FILE).parent.mkdir(parents=True)
        self.vm_file(MODULES_FILE).write_text("br_netfilter\n")
        self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertIn(f"sudo tee {MODULES_FILE}", self.vm_changes())
        self.assertIn("Managed by the exadev.github_runner collection", self.vm_file(MODULES_FILE).read_text())

    def test_a_bridge_sysctl_that_is_off_is_set_after_the_module_is_loaded(self) -> None:
        self.state("runtime-up")
        (self.stub / "vm" / "sysctl").mkdir(parents=True)
        (self.stub / "vm" / "sysctl" / "net.bridge.bridge-nf-call-iptables").write_text("0")
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.vm_changes()[2:], ["sudo modprobe -- br_netfilter", "sudo sysctl -w net.bridge.bridge-nf-call-iptables=1"])

    def test_the_vm_of_the_hosts_profile_is_the_one_prepared(self) -> None:
        self.state("runtime-up")
        self.run_role(context="colima-example", github_runner_cluster_runtime_autostart=False)
        ssh_calls = [call for call in self.calls() if " colima ssh " in call]
        self.assertTrue(ssh_calls)
        self.assertTrue(all(call.startswith("colima-example colima ssh --profile example -- ") for call in ssh_calls), ssh_calls)

    def test_the_vm_is_prepared_with_the_agent_installed_as_well(self) -> None:
        self.state("session")
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), ["colima colima start --profile default --activate=false", f"colima launchctl bootstrap gui/{UID} {self.plist}"])
        self.assertEqual(self.vm_changes()[-1], "sudo modprobe -- br_netfilter")

    def test_check_mode_reports_what_the_vm_needs_and_changes_none_of_it(self) -> None:
        self.state("runtime-up")
        result = self.run_role("--check", github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.vm_changes(), [])
        self.assertFalse((self.stub / "vm").exists())
        for reported in (f"write {MODULES_FILE} in the VM", f"write {SYSCTL_FILE} in the VM", "modprobe br_netfilter in the VM"):
            self.assertIn(reported, result.stdout)

    def test_a_vm_that_would_only_be_started_by_a_check_run_is_not_read(self) -> None:
        self.state("session")
        result = self.run_role("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls() if " colima ssh " in call], [])

    def test_a_runtime_that_is_not_colima_leaves_the_kernel_alone(self) -> None:
        self.state("runtime-up")
        for context in ("desktop-linux", "orbstack"):
            with self.subTest(context=context):
                result = self.run_role(context=context, github_runner_cluster_runtime_autostart=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual([call for call in self.calls() if " colima " in call], [])

    def test_a_stopped_runtime_that_is_not_started_fails_before_the_vm_is_touched(self) -> None:
        self.state("session")
        self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(self.vm_changes(), [])
        self.assertEqual([call for call in self.calls() if " colima " in call], [])

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
        result = self.run_role(github_runner_cluster_runtime_autostart=False, github_runner_cluster_docker_role="agent")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("The k3s container github-runner-k3s-agent on node1 is", result.stdout)
        (self.stub / "github-runner-k3s-agent.state").write_text("running")
        self.assertEqual(self.run_role(github_runner_cluster_runtime_autostart=False, github_runner_cluster_docker_role="agent").returncode, 0)

    def test_a_container_renamed_by_an_extra_compose_file_is_the_one_checked(self) -> None:
        self.state("runtime-up")
        (self.stub / "example-k3s.state").write_text("running")
        result = self.run_role(github_runner_cluster_runtime_autostart=False, github_runner_cluster_container_name="example-k3s")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
