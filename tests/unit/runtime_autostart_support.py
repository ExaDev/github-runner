"""What the tests of the cluster role's runtime tasks share: the paths and values they read, the stand-in for docker, colima, launchctl, plutil, sudo and ps, and the fixture that runs the role against it."""

from __future__ import annotations

import grp
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
I386_ENTRY = "/proc/sys/fs/binfmt_misc/qemu-i386"
I386_OFF = f"test ! -e {I386_ENTRY} || echo 0 | sudo tee {I386_ENTRY} > /dev/null"


def ansible_env() -> dict[str, str]:
    return {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_RETRY_FILES_ENABLED": "false"}


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
def hang():
    # A program that never answers and does not die on SIGALRM, as a Go binary does not; with ignore-term it ignores TERM as well, so only KILL ends it.
    import signal, time
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    if exists("ignore-term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(os.path.join(stub, "hung.pids"), "a") as pids:
        pids.write(str(os.getpid()) + "\\n")
    time.sleep(300)
def touch(file):
    open(os.path.join(stub, file), "w").close()
def need_root():
    if os.environ.get("RUNTIME_ROOT") != "1":
        sys.exit("launchctl: Operation not permitted while changing the system domain as a user")
if name == "docker" and args[:2] == ["context", "inspect"]:
    # Read from the context file alone, so it answers with the daemon down. An empty context name or an unknown one is an error, as docker's is.
    if args[-1] == "" or exists("context-unknown"):
        sys.exit("context " + repr(args[-1]) + ": context not found")
    if not exists("context-host"):
        sys.exit("context inspect: no context-host file")
    print(open(os.path.join(stub, "context-host")).read(), end="")
elif name == "docker":
    if args[:1] == ["info"] and exists("docker-hangs"):
        hang()
    if args[:1] == ["info"] and exists("docker-exit"):
        sys.exit(int(open(os.path.join(stub, "docker-exit")).read()))
    if not exists("runtime-up") and args[:1] == ["info"] and exists("docker-answers-after"):
        # A daemon that fails its first tries and then answers, as one that is restarting or a loaded host does.
        counter = os.path.join(stub, "docker-info-failures")
        seen = int(open(counter).read()) if os.path.exists(counter) else 0
        if seen >= int(open(os.path.join(stub, "docker-answers-after")).read()):
            touch("runtime-up")
        else:
            open(counter, "w").write(str(seen + 1))
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
    if exists("guest-hangs") and args[args.index("--") + 1:] == ["true"]:
        hang()
    if exists("guest-answers") and args[args.index("--") + 1:] == ["true"]:
        sys.exit(0)
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
    elif command == ["uname", "-m"]:
        # aarch64 unless a test says otherwise, as the VM of an Apple silicon Mac is.
        print(open(os.path.join(vm, "arch")).read().strip() if os.path.exists(os.path.join(vm, "arch")) else "aarch64")
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
elif name == "colima" and args[0] == "list":
    # One JSON object per profile, whatever profile is asked for: this host has the profile the test names and another one beside it, which is always running.
    profile = os.environ.get("RUNTIME_PROFILE", "default")
    print('{{"name":"other","status":"Running","runtime":"docker"}}')
    print('{{"name":"' + profile + '","status":"' + ("Running" if exists("colima-listed-running") else "Stopped") + '","runtime":"docker"}}')
elif name == "colima" and args[0] == "stop":
    if args[:3] != ["stop", "--profile", os.environ.get("RUNTIME_PROFILE", "default")] or args[3:] not in ([], ["--force"]):
        sys.exit("unexpected colima stop call: " + " ".join(args))
    if args[3:] == [] and exists("clean-stop-hangs"):
        hang()
    if args[3:] == [] and exists("clean-stop-fails"):
        sys.exit("colima: the VM does not stop")
    for file in ("colima-listed-running", "runtime-up", "docker-hangs", "guest-hangs", "docker-exit"):
        if exists(file):
            os.remove(os.path.join(stub, file))
elif name == "colima":
    if args[0] != "start" or args[-1] != "--activate=false":
        sys.exit("unexpected colima call: " + " ".join(args))
    if exists("colima-start-fails"):
        sys.exit("colima: the VM did not start")
    # Starting a profile Colima already lists as running does nothing, so a wedged VM stays wedged.
    if not exists("colima-listed-running"):
        touch("colima-listed-running")
        touch("runtime-up")
        # A VM that is started again answers.
        for file in ("docker-hangs", "guest-hangs", "docker-exit"):
            if exists(file):
                os.remove(os.path.join(stub, file))
elif name == "ps":
    # The host agent as ps shows it, from the file agent-ps ("<elapsed> <command>"); a pid that is not running prints nothing and fails.
    if not exists("agent-ps"):
        sys.exit(1)
    print(open(os.path.join(stub, "agent-ps")).read(), end="")
elif name == "launchctl":
    system = args[1] == "system" or args[1].startswith("system/")
    # Each job is loaded or not on its own: the watchdog's has a flag of its own beside the start job's.
    def flag(target):
        return ("watchdog-" if "watchdog" in target else "") + ("daemon-loaded" if system else "loaded")
    if args[0] == "print":
        sys.exit(0 if exists(flag(args[1]) if system or args[1].count("/") == 2 else "session") else 113)
    elif args[0] == "bootstrap":
        if not os.path.exists(args[2]):
            sys.exit("Bootstrap failed: no such plist")
        if system:
            need_root()
        touch(flag(args[2]))
    elif args[0] == "bootout":
        if system:
            need_root()
        os.remove(os.path.join(stub, flag(args[1])))
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
class RoleFixture(unittest.TestCase):
    """The stand-in host a test runs the cluster role's runtime tasks against, with the readers of what the stand-ins recorded. It holds no tests, so a module that imports it runs none of another module's."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.stub = self.work / "stub"
        self.home = self.work / "home"
        self.stub.mkdir()
        self.home.mkdir()
        self.bin = self.work / "bin"
        self.bin.mkdir()
        for name in ("docker", "colima", "launchctl", "plutil", "sudo", "ps"):
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
            "github_runner_cluster_runtime_watchdog_daemon_plist": f"{self.daemons}/{{{{ github_runner_cluster_runtime_watchdog_label }}}}.plist",
            # Off unless a test turns it on, so that the exact launchctl calls the tests of the start job assert are the start job's alone.
            "github_runner_cluster_runtime_watchdog": False,
            **variables,
        }
        inventory = self.work / "inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"hosts": {"node1": host}}}))
        # The stand-ins come first on the playbook's own PATH as well as the tasks', so a tool a task runs without the role's PATH still cannot reach this machine's real launchd, Colima or Docker.
        env = ansible_env() | {"RUNTIME_STUB": str(self.stub), "RUNTIME_PROFILE": profile, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
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

    # A profile Colima lists as running while its runtime does not answer.

    FAST = {"github_runner_cluster_runtime_probe_attempts": 3, "github_runner_cluster_runtime_probe_delay": 0, "github_runner_cluster_runtime_probe_timeout": 5}

    def forget(self) -> None:
        """Start a sub-case from a clean stub directory and a clean home."""
        for entry in self.stub.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry)
            elif entry.name not in ("context-host", "github-runner-k3s.state"):
                entry.unlink()
        shutil.rmtree(self.home / ".colima", ignore_errors=True)

    def stops(self) -> list[str]:
        return [call for call in self.calls() if re.match(r"\S+ colima stop", call)]

    def starts(self) -> list[str]:
        return [call for call in self.calls() if re.match(r"\S+ colima start", call)]

    AGENT = "/usr/local/bin/limactl hostagent --pidfile /example/ha.pid --socket /example/ha.sock"

    @staticmethod
    def etime(seconds: int) -> str:
        """ps's elapsed time, [[days-]hours:]minutes:seconds."""
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        minutes, secs = divmod(rest, 60)
        return (f"{days}-" if days else "") + (f"{hours:02d}:" if days or hours else "") + f"{minutes:02d}:{secs:02d}"

    def listed_running(self, agent_age: int | None = 7200, command: str | None = None, stale_file: bool = False) -> None:
        """A VM Colima lists as running whose Docker daemon is silent, with a live host agent that has run for `agent_age` seconds (None for no pid file at all). `stale_file` leaves a pid file that is old but names no running process, as one from before a reboot does; `command` is what the process runs."""
        self.state("session", "colima-listed-running")
        if agent_age is None:
            return
        pid = self.home / ".colima" / "_lima" / "colima" / "ha.pid"
        pid.parent.mkdir(parents=True, exist_ok=True)
        pid.write_text("4242\n")
        old = time.time() - 7 * 86400
        os.utime(pid, (old, old))
        if not stale_file:
            (self.stub / "agent-ps").write_text(f"{self.etime(agent_age)} {command or self.AGENT}\n")

    def hung_pids(self) -> list[int]:
        log = self.stub / "hung.pids"
        return [int(line) for line in log.read_text().split()] if log.exists() else []

    def assert_all_ended(self) -> None:
        for pid in self.hung_pids():
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                self.fail(f"the hung stand-in {pid} is still running")

    def info_calls(self) -> list[str]:
        return [call for call in self.calls() if re.match(r"\S+ docker info", call)]

    def assert_left_alone(self, result: subprocess.CompletedProcess[str], *words: str) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(failed_task(result.stdout), "Fail where the VM is not shown to be wedged or the stop is off")
        self.assertEqual(self.stops(), [])
        self.assertEqual(self.starts(), [])
        self.assertIn("colima stop --profile default --force", result.stdout, "the recovery by hand is named")
        for word in words:
            self.assertIn(word, result.stdout)

    HANG = {"github_runner_cluster_runtime_probe_attempts": 2, "github_runner_cluster_runtime_probe_delay": 0, "github_runner_cluster_runtime_probe_timeout": 2, "github_runner_cluster_runtime_kill_grace": 1, "github_runner_cluster_runtime_clean_stop_timeout": 2}
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

    def assert_a_second_run_changes_nothing(self, context: str, context_host: str | None) -> None:
        self.state("session")
        self.run_role(context=context, context_host=context_host)
        before = self.calls()
        result = self.run_role(context=context, context_host=context_host)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual([call for call in self.calls()[len(before) :] if not re.match(r"\S+ (docker (info|ps|context inspect)|launchctl print|colima ssh --profile \S+ -- (cat|test|uname|sysctl -n|systemctl is-active|dpkg-query) )", call)], [])
        self.assertNotIn("bootstrap", " ".join(self.calls()[len(before) :]))

    def i386_writes(self) -> list[str]:
        """The commands run as root in the VM that write its qemu-i386 entry, in order."""
        return [command for command in self.vm_calls() if I386_ENTRY in command]

    def x86_vm(self, provision: list[dict[str, str]] | None = None, handler: str | None = "enabled") -> Path:
        """An x86_64 VM whose qemu-i386 entry reads `handler` (none for no entry), and its profile's colima.yaml with `provision`."""
        self.state("runtime-up")
        (self.stub / "vm").mkdir(exist_ok=True)
        (self.stub / "vm" / "arch").write_text("x86_64\n")
        if handler is not None:
            entry = self.vm_file(I386_ENTRY)
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text(f"{handler}\ninterpreter /usr/bin/qemu-i386\nflags: POCF\n")
        config = self.home / ".colima" / "default" / "colima.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(yaml.safe_dump({"cpu": 8, "arch": "x86_64", "binfmt": True, "provision": provision}))
        return config

    # The watchdog's launchd job.

    def watchdog_calls(self) -> list[str]:
        """The launchctl calls that load or unload the watchdog, without the Docker context each is logged under. """
        return [call.split(" ", 1)[1] for call in self.calls() if re.match(r"\S+ launchctl (bootstrap|bootout)", call) and "watchdog" in call]
