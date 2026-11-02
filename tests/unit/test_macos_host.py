"""Tests for the github_runner_macos_host role: the filter that compares pmset's AC Power settings with the wanted ones, the variable defaults against the argument specs and the README, how site.yml wires the role, and the role's tasks (tests/macos_host/run.yml) run against stand-ins for pmset, brew, tailscaled, tailscale and sudo, so no Mac setting, package, daemon or privilege is ever touched."""

from __future__ import annotations

import grp
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_macos_host"
RUN = ROOT / "tests" / "macos_host" / "run.yml"
PLUGIN = ROOT / "plugins" / "filter" / "macos_host.py"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
SPECS: dict[str, Any] = yaml.safe_load((ROLE / "meta" / "argument_specs.yml").read_text())["argument_specs"]["main"]["options"]
PLIST_NAME = "com.tailscale.tailscaled.plist"
AUTH_KEY = "tskey-auth-example-0123456789"

_spec = importlib.util.spec_from_file_location("macos_host_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
macos_host = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(macos_host)
changes = macos_host.macos_host_pmset_ac_changes
sleep_disabled = macos_host.macos_host_pmset_sleep_disabled

# What pmset -g custom prints on a MacBook Air that sleeps on AC power, and the wanted values the role defaults to.
BATTERY = """Battery Power:
 Sleep On Power Button 1
 lowpowermode         0
 standby              1
 ttyskeepawake        1
 hibernatemode        3
 powernap             1
 hibernatefile        /var/vm/sleepimage
 displaysleep         2
 womp                 0
 networkoversleep     0
 sleep                1
 tcpkeepalive         1
 disksleep            10
"""
AC_SETTINGS = {"standby": "1", "ttyskeepawake": "1", "hibernatemode": "3", "powernap": "1", "hibernatefile": "/var/vm/sleepimage", "displaysleep": "10", "womp": "1", "networkoversleep": "0", "sleep": "1", "tcpkeepalive": "1", "disksleep": "10"}
WANTED = {"sleep": 0, "displaysleep": 0, "disksleep": 0, "womp": 1, "standby": 1, "powernap": 1, "tcpkeepalive": 1}


def report(**overrides: str | None) -> str:
    """Render pmset -g custom's output, with the AC Power values overridden (None leaves a setting out)."""
    settings = {**AC_SETTINGS, **overrides}
    lines = [f" {name:<20} {value}" for name, value in settings.items() if value is not None]
    return BATTERY + "AC Power:\n Sleep On Power Button 1\n" + "\n".join(lines) + "\n"


def system_report(*lines: str) -> str:
    """Render pmset -g's output, with the given lines among the settings in force."""
    return "System-wide power settings:\nCurrently in use:\n standby              1\n" + "".join(f" {line}\n" for line in lines) + " womp                 1\n"


class FilterTest(unittest.TestCase):
    def test_a_mac_that_sleeps_on_ac_power_gets_the_settings_that_differ_in_the_wanted_order(self) -> None:
        self.assertEqual(changes(report(), WANTED), ["sleep", "0", "displaysleep", "0", "disksleep", "0"])

    def test_a_mac_already_in_line_gets_nothing(self) -> None:
        self.assertEqual(changes(report(sleep="0", displaysleep="0", disksleep="0"), WANTED), [])

    def test_only_the_ac_power_section_is_compared(self) -> None:
        # The battery section says sleep 1 and womp 0 in every report here, and must not count either way.
        self.assertEqual(changes(report(sleep="0", displaysleep="0", disksleep="0", womp="0"), WANTED), ["womp", "1"])

    def test_the_ac_power_section_can_come_first(self) -> None:
        ac_first = "AC Power:\n sleep 5\nBattery Power:\n sleep 0\n"
        self.assertEqual(changes(ac_first, {"sleep": 0}), ["sleep", "0"])

    def test_a_setting_the_mac_does_not_report_is_skipped(self) -> None:
        self.assertEqual(changes(report(powernap=None, sleep="0", displaysleep="0", disksleep="0"), WANTED), [])

    def test_a_setting_is_compared_as_text_whatever_type_it_is_wanted_as(self) -> None:
        self.assertEqual(changes(report(sleep="0", displaysleep="0", disksleep="0"), {"womp": "1", "sleep": 0, "standby": 1}), [])
        self.assertEqual(changes(report(), {"sleep": "0"}), ["sleep", "0"])

    def test_a_name_of_several_words_does_not_break_the_section(self) -> None:
        self.assertEqual(changes(report(), {"Sleep On Power Button": 0}), ["Sleep On Power Button", "0"])

    def test_output_that_is_not_pmsets_is_refused(self) -> None:
        for text in ("", "pmset: command not found\n", BATTERY):
            with self.subTest(text=text), self.assertRaisesRegex(Exception, "no 'AC Power:' section"):
                changes(text, WANTED)

    def test_a_report_or_wanted_of_the_wrong_type_is_refused(self) -> None:
        with self.assertRaisesRegex(Exception, "report must be a string"):
            changes(None, WANTED)
        with self.assertRaisesRegex(Exception, "wanted settings must be a mapping"):
            changes(report(), ["sleep"])

    def test_the_filter_is_registered_under_the_name_the_role_uses(self) -> None:
        self.assertEqual(set(macos_host.FilterModule().filters()), {"macos_host_pmset_ac_changes", "macos_host_pmset_sleep_disabled"})
        self.assertIn("exadev.github_runner.macos_host_pmset_ac_changes", (ROLE / "tasks" / "power.yml").read_text())


class SleepDisabledFilterTest(unittest.TestCase):
    def test_a_mac_that_reports_sleep_disabled_as_one_is_disabled(self) -> None:
        self.assertIs(sleep_disabled(system_report("SleepDisabled        1")), True)

    def test_a_mac_that_reports_it_as_zero_is_not(self) -> None:
        self.assertIs(sleep_disabled(system_report("SleepDisabled        0")), False)

    def test_a_mac_that_does_not_list_the_setting_is_not(self) -> None:
        self.assertIs(sleep_disabled(system_report()), False)

    def test_the_separator_and_indentation_do_not_matter(self) -> None:
        for line in ("SleepDisabled\t\t1", "SleepDisabled 1", "  SleepDisabled      1  ", "\tSleepDisabled\t1"):
            with self.subTest(line=line):
                self.assertIs(sleep_disabled("System-wide power settings:\nCurrently in use:\n" + line + "\n"), True)

    def test_the_setting_is_found_wherever_it_is_listed(self) -> None:
        self.assertIs(sleep_disabled(system_report("hibernatemode        3", "SleepDisabled        1", "disksleep            10")), True)

    def test_another_setting_or_value_is_not_it(self) -> None:
        for line in ("SleepDisabledX       1", "NotSleepDisabled     1", "SleepDisabled        10", "SleepDisabled        1 0", "sleep                1", "Sleep On Power Button 1"):
            with self.subTest(line=line):
                self.assertIs(sleep_disabled(system_report(line)), False)

    def test_output_that_is_not_pmsets_is_refused(self) -> None:
        for text in ("", "pmset: command not found\n", "SleepDisabled 1\n"):
            with self.subTest(text=text), self.assertRaisesRegex(Exception, "no 'Currently in use:' section"):
                sleep_disabled(text)

    def test_a_report_of_the_wrong_type_is_refused(self) -> None:
        with self.assertRaisesRegex(Exception, "report must be a string"):
            sleep_disabled(None)


class DefaultsTest(unittest.TestCase):
    def test_the_power_defaults_are_the_values_the_always_on_macs_run_with(self) -> None:
        wanted = {name: DEFAULTS[f"github_runner_macos_host_power_ac_{name}"] for name in WANTED}
        self.assertEqual(wanted, WANTED)

    def test_the_role_wants_exactly_the_settings_it_has_variables_for(self) -> None:
        power_tasks = (ROLE / "tasks" / "power.yml").read_text()
        names = set(re.findall(r"github_runner_macos_host_power_ac_(\w+)", power_tasks))
        self.assertEqual(names, set(WANTED))
        self.assertEqual(set(re.findall(r"'(\w+)': github_runner_macos_host_power_ac_", power_tasks)), set(WANTED))

    def test_passwordless_sudo_is_off_by_default_and_is_for_the_connecting_user(self) -> None:
        self.assertIs(DEFAULTS["github_runner_macos_host_sudoers_nopasswd"], False)
        self.assertEqual(DEFAULTS["github_runner_macos_host_sudoers_user"], "")
        self.assertEqual(DEFAULTS["github_runner_macos_host_sudoers_dir"], "/etc/sudoers.d")

    def test_the_role_never_joins_a_tailnet_by_default(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_macos_host_tailscale_auth_key"], "")
        self.assertEqual(DEFAULTS["github_runner_macos_host_tailscale_up_args"], [])

    def test_every_default_is_in_the_argument_specs_with_the_same_value_and_in_the_readme(self) -> None:
        readme = (ROLE / "README.md").read_text()
        self.assertEqual(set(DEFAULTS), set(SPECS))
        for name, value in DEFAULTS.items():
            with self.subTest(name=name):
                self.assertEqual(SPECS[name]["default"], value)
                self.assertIn(f"`{name}`", readme)

    def test_the_auth_key_is_never_logged(self) -> None:
        self.assertIs(SPECS["github_runner_macos_host_tailscale_auth_key"]["no_log"], True)
        tasks = yaml.safe_load((ROLE / "tasks" / "tailscale.yml").read_text())
        [join] = [task for task in tasks if task.get("name") == "Join the tailnet"]
        [login] = [task for task in join["block"] if task.get("name") == "Log in with the auth key"]
        self.assertIs(login["no_log"], True)
        self.assertTrue(login["ansible.builtin.command"]["stdin"].startswith("{{ github_runner_macos_host_tailscale_auth_key"))
        self.assertNotIn("auth_key", json.dumps(login["ansible.builtin.command"]["argv"]))


def sudoers_task() -> dict[str, Any]:
    tasks = yaml.safe_load((ROLE / "tasks" / "sudoers.yml").read_text())
    [task] = [task for task in tasks if "ansible.builtin.copy" in task]
    return task


def render(template: str, **variables: Any) -> str:
    return jinja2.Environment(autoescape=False, keep_trailing_newline=True).from_string(template).render(**variables)


class SudoersFileTest(unittest.TestCase):
    VARIABLES = {"github_runner_macos_host_effective_sudoers_user": "example", "github_runner_macos_host_sudoers_dir": "/etc/sudoers.d"}

    def test_the_file_is_the_users_grant_owned_by_root_and_readable_only_by_root_and_wheel(self) -> None:
        task = sudoers_task()
        copy = task["ansible.builtin.copy"]
        self.assertEqual(render(copy["content"], **self.VARIABLES), "example ALL=(ALL) NOPASSWD: ALL\n")
        self.assertEqual(render(copy["dest"], **self.VARIABLES), "/etc/sudoers.d/example")
        self.assertEqual((copy["owner"], copy["group"], copy["mode"]), ("root", "wheel", "0440"))
        self.assertIs(task["become"], True)

    def test_visudo_checks_it_before_it_replaces_anything(self) -> None:
        self.assertEqual(sudoers_task()["ansible.builtin.copy"]["validate"], "/usr/sbin/visudo -cf %s")

    @unittest.skipUnless(Path("/usr/sbin/visudo").exists(), "visudo is not installed")
    def test_visudo_accepts_the_rendered_content(self) -> None:
        content = render(sudoers_task()["ansible.builtin.copy"]["content"], **self.VARIABLES)
        with tempfile.NamedTemporaryFile("w", suffix="-sudoers") as handle:
            handle.write(content)
            handle.flush()
            result = subprocess.run(["/usr/sbin/visudo", "-cf", handle.name], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(Path("/usr/sbin/visudo").exists(), "visudo is not installed")
    def test_visudo_refuses_a_line_that_is_not_a_rule(self) -> None:
        # Proves the check above could have failed.
        with tempfile.NamedTemporaryFile("w", suffix="-sudoers") as handle:
            handle.write("example ALL=(ALL) NOPASSWD:\n")
            handle.flush()
            result = subprocess.run(["/usr/sbin/visudo", "-cf", handle.name], capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)

    def test_the_user_is_checked_before_it_becomes_a_file_name_and_a_sudoers_word(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "sudoers.yml").read_text())
        [check] = [task for task in tasks if "ansible.builtin.assert" in task]
        [condition] = check["ansible.builtin.assert"]["that"] if isinstance(check["ansible.builtin.assert"]["that"], list) else [check["ansible.builtin.assert"]["that"]]
        pattern = re.search(r"match\('(.*)'\)", condition)
        assert pattern is not None
        for good in ("joe", "_svc", "a-b_c1"):
            with self.subTest(good=good):
                self.assertRegex(good, pattern.group(1))
        for bad in ("Joe", "joe.x", "joe~", "a b", "joe\nroot", "root ALL=(ALL) NOPASSWD: ALL #", "../x", ""):
            with self.subTest(bad=bad):
                self.assertNotRegex(bad, pattern.group(1))


class WiringTest(unittest.TestCase):
    def plays(self) -> list[dict[str, Any]]:
        loaded: list[dict[str, Any]] = yaml.safe_load((ROOT / "playbooks" / "site.yml").read_text())
        return loaded

    def test_the_baseline_play_runs_before_the_fleet_play_and_after_the_validation(self) -> None:
        names = [play["name"] for play in self.plays()]
        self.assertLess(names.index("Validate the ARC configuration"), names.index("Set up the Macs' host baseline"))
        self.assertLess(names.index("Set up the Macs' host baseline"), names.index("GitHub Actions runner fleet"))

    def test_the_baseline_play_runs_the_role_on_the_cluster_hosts(self) -> None:
        [play] = [play for play in self.plays() if play["name"] == "Set up the Macs' host baseline"]
        self.assertEqual(play["roles"], ["exadev.github_runner.github_runner_macos_host"])
        self.assertEqual(play["hosts"], [play for play in self.plays() if play["name"] == "GitHub Actions runner fleet"][0]["hosts"])

    def test_become_is_used_only_inside_the_role_and_only_on_the_tasks_that_change_the_host(self) -> None:
        for path in sorted(ROOT.joinpath("roles").glob("*/tasks/**/*.yml")) + sorted(ROOT.joinpath("playbooks").glob("*.yml")):
            if ROLE in path.parents:
                continue
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertNotRegex(path.read_text(), r"^\s*become(_user|_method)?:", "become outside github_runner_macos_host")
        for path in sorted(ROLE.glob("tasks/*.yml")):
            text = path.read_text()
            with self.subTest(path=path.name):
                self.assertEqual(len(re.findall(r"^\s*become: true", text, re.MULTILINE)), {"power.yml": 1, "sudoers.yml": 1, "tailscale.yml": 2}.get(path.name, 0))

    def test_every_task_that_runs_on_the_host_is_guarded_to_macos(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        for task in tasks:
            if "ansible.builtin.include_tasks" in task:
                self.assertIn("ansible_facts['system'] == 'Darwin'", task["when"])

    def test_bootstrap_does_not_ask_for_sudo(self) -> None:
        text = (ROOT / "bootstrap.sh").read_text()
        self.assertIn("github_runner_macos_host_power: false", text)
        self.assertIn("github_runner_macos_host_tailscale: false", text)


# Stands in for pmset, brew, tailscaled, tailscale and sudo, which are all this one script under five names. State is files in $MACOS_HOST_STUB: pmset.ac (one "name value" line per AC Power setting), brew.installed, daemon.up and login.state (what tailscale status reports), LaunchDaemons/ (where tailscaled install-system-daemon writes its definition); every call is appended to calls.log as "<tool> root=<0|1> <arguments>". The sudo stand-in does what sudo does for the role, which is to run the command as root: it marks the command with MACOS_HOST_ROOT, and a stand-in that changes the host refuses to without it, as the real one does for a user who is not root. A key given to tailscale up is logged only as a hash of it.
STUB = """#!{python}
import hashlib, json, os, sys
stub = os.environ["MACOS_HOST_STUB"]
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
root = os.environ.get("MACOS_HOST_ROOT") == "1"
def path(file):
    return os.path.join(stub, file)
def read(file, default=""):
    return open(path(file)).read() if os.path.exists(path(file)) else default
def write(file, text=""):
    os.makedirs(os.path.dirname(path(file)), exist_ok=True)
    open(path(file), "w").write(text)
def log(text):
    open(path("calls.log"), "a").write(text + "\\n")
def need_root():
    if not root:
        sys.exit(name + ": must be run as root")
if name == "sudo":
    log("sudo " + " ".join(args))
    while args[0] != "/bin/sh":
        args = args[1:]
    os.environ["MACOS_HOST_ROOT"] = "1"
    os.execv(args[0], args)
if name == "pmset":
    log("pmset root=" + str(int(root)) + " " + " ".join(args))
    if args == ["-g", "custom"]:
        settings = dict(line.split(" ", 1) for line in read("pmset.ac").splitlines())
        print("Battery Power:\\n sleep 1\\n womp 0\\nAC Power:\\n Sleep On Power Button 1")
        for key, value in settings.items():
            print(" %-20s %s" % (key, value))
    elif args[0] == "-c":
        need_root()
        settings = dict(line.split(" ", 1) for line in read("pmset.ac").splitlines())
        pairs = args[1:]
        settings.update(zip(pairs[::2], pairs[1::2]))
        write("pmset.ac", "".join(key + " " + value + "\\n" for key, value in settings.items()))
    else:
        sys.exit("unexpected pmset call: " + " ".join(args))
elif name == "brew":
    log("brew root=" + str(int(root)) + " " + " ".join(args))
    if root:
        sys.exit("Running Homebrew as root is extremely dangerous and no longer supported.")
    if args[:2] == ["info", "--json=v2"] and args[2:3] == ["tailscale"]:
        installed = [{{"version": "1.0"}}] if os.path.exists(path("brew.installed")) else []
        print(json.dumps({{"formulae": [{{"name": "tailscale", "full_name": "tailscale", "tap": "homebrew/core", "installed": installed, "outdated": False}}], "casks": []}}))
    elif args[:2] == ["install", "tailscale"]:
        write("brew.installed")
        bindir = os.path.dirname(os.path.abspath(sys.argv[0]))
        for tool in ("tailscaled", "tailscale"):
            target = os.path.join(bindir, tool)
            open(target, "w").write(open(sys.argv[0]).read())
            os.chmod(target, 0o755)
    else:
        sys.exit("unexpected brew call: " + " ".join(args))
elif name == "tailscaled":
    log("tailscaled root=" + str(int(root)) + " " + " ".join(args))
    if args != ["install-system-daemon"]:
        sys.exit("unexpected tailscaled call: " + " ".join(args))
    need_root()
    write("LaunchDaemons/com.tailscale.tailscaled.plist", "<plist/>")
    write("daemon.up")
    if not os.path.exists(path("login.state")):
        write("login.state", "NeedsLogin")
elif name == "tailscale":
    log("tailscale root=" + str(int(root)) + " " + " ".join(args))
    if not os.path.exists(path("daemon.up")):
        sys.exit("failed to connect to local tailscaled")
    if args[:1] == ["status"]:
        print(json.dumps({{"BackendState": read("login.state")}}))
    elif args[:1] == ["up"]:
        need_root()
        key = sys.stdin.read()
        log("tailscale key=" + hashlib.sha256(key.encode()).hexdigest()[:8] + " " + " ".join(argument for argument in args[1:]))
        write("login.state", "Running")
    else:
        sys.exit("unexpected tailscale call: " + " ".join(args))
else:
    sys.exit("unexpected stand-in: " + name)
"""


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:8]


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class RoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.stub = self.work / "stub"
        self.prefix = self.work / "prefix"
        self.bin = self.prefix / "bin"
        self.bin.mkdir(parents=True)
        self.stub.mkdir()
        # The stand-ins for the tools every Mac has. tailscaled and tailscale appear in the bin directory when the stand-in brew installs the formula.
        for name in ("pmset", "brew", "sudo"):
            tool = self.bin / name
            tool.write_text(STUB.format(python=sys.executable))
            tool.chmod(0o755)
        self.plist = self.stub / "LaunchDaemons" / PLIST_NAME
        self.set_power(sleep="1", displaysleep="10", disksleep="10")

    def set_power(self, **overrides: str) -> None:
        settings = {"sleep": "0", "displaysleep": "0", "disksleep": "0", "womp": "1", "standby": "1", "powernap": "1", "tcpkeepalive": "1", **overrides}
        (self.stub / "pmset.ac").write_text("".join(f"{name} {value}\n" for name, value in settings.items()))

    def power(self) -> dict[str, str]:
        return dict(line.split(" ", 1) for line in (self.stub / "pmset.ac").read_text().splitlines())

    def hand_built_daemon(self) -> None:
        self.plist.parent.mkdir()
        self.plist.write_text("<plist/>")

    def run_role(self, *extra: str, system: str = "Darwin", machine: str = "arm64", **variables: Any) -> subprocess.CompletedProcess[str]:
        host = {
            "host_system": system,
            "host_machine": machine,
            "github_runner_macos_host_homebrew_prefix": str(self.prefix),
            "github_runner_macos_host_tailscale_plist": str(self.plist),
            **variables,
        }
        inventory = self.work / "inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"hosts": {"node1": host}}}))
        env = {
            **os.environ,
            "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"),
            "ANSIBLE_LOCALHOST_WARNING": "false",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
            "ANSIBLE_RETRY_FILES_ENABLED": "false",
            "MACOS_HOST_STUB": str(self.stub),
            # The stand-ins come first on the playbook's own PATH as well, so nothing the role runs can reach this machine's real pmset, Homebrew or sudo.
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
        }
        return subprocess.run([ANSIBLE_PLAYBOOK, "-i", str(inventory), str(RUN), *extra], capture_output=True, text=True, check=False, env=env, cwd=self.work)

    def calls(self) -> list[str]:
        """Every call a stand-in logged except sudo's own, which sudo_calls gives."""
        return [call for call in self.logged() if not call.startswith("sudo ")]

    def sudo_calls(self) -> list[str]:
        return [call for call in self.logged() if call.startswith("sudo ")]

    def logged(self) -> list[str]:
        log = self.stub / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def writes(self, *tools: str) -> list[str]:
        """The calls that change the host, without the read-only ones the role makes to find out."""
        wanted = tools or ("pmset", "brew", "tailscaled", "tailscale")
        read_only = re.compile(r"(pmset root=0 -g custom|brew root=0 info .*|tailscale root=0 status .*)$")
        return [call for call in self.calls() if call.split(" ")[0] in wanted and not read_only.match(call)]

    def recap(self, result: subprocess.CompletedProcess[str]) -> dict[str, int]:
        match = re.search(r"node1\s+: ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", result.stdout)
        assert match is not None, result.stdout + result.stderr
        return dict(zip(("ok", "changed", "unreachable", "failed"), map(int, match.groups())))

    def ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # Passwordless sudo.

    def sudoers(self, name: str = "joe") -> Path:
        return self.stub / "sudoers.d" / name

    def sudoers_vars(self) -> dict[str, Any]:
        return {"github_runner_macos_host_sudoers_nopasswd": True, "github_runner_macos_host_sudoers_dir": str(self.stub / "sudoers.d"), "github_runner_macos_host_power": False, "github_runner_macos_host_tailscale": False}

    def test_it_is_off_unless_asked_for(self) -> None:
        result = self.run_role(github_runner_macos_host_sudoers_dir=str(self.stub / "sudoers.d"), github_runner_macos_host_tailscale=False, github_runner_macos_host_power=False)
        self.ok(result)
        self.assertEqual(self.sudo_calls(), [])
        self.assertFalse(self.sudoers().exists())

    def test_check_mode_on_a_host_without_the_file_reports_it_without_any_privilege(self) -> None:
        result = self.run_role("--check", "--diff", **self.sudoers_vars())
        self.ok(result)
        self.assertEqual(self.sudo_calls(), [])
        self.assertFalse(self.sudoers().exists())
        self.assertEqual(self.recap(result)["changed"], 1, result.stdout)
        self.assertIn("joe ALL=(ALL) NOPASSWD: ALL", result.stdout)

    @unittest.skipUnless("wheel" in {group.gr_name for group in grp.getgrall()}, "this machine has no wheel group, which the copy looks up even under --check")
    def test_check_mode_on_a_host_with_the_file_compares_it_as_root(self) -> None:
        self.sudoers().parent.mkdir()
        self.sudoers().write_text("joe ALL=(ALL) NOPASSWD: ALL\n")
        result = self.run_role("--check", **self.sudoers_vars())
        self.ok(result)
        self.assertGreaterEqual(len(self.sudo_calls()), 1)

    def test_the_grant_is_for_the_named_user_when_one_is_given(self) -> None:
        result = self.run_role("--check", github_runner_macos_host_sudoers_user="example", **self.sudoers_vars())
        self.ok(result)
        self.assertIn("sudoers.d/example would be written: example ALL=(ALL) NOPASSWD: ALL", result.stdout)

    def test_a_user_that_is_not_a_plain_login_name_is_refused_before_anything_is_written(self) -> None:
        result = self.run_role(github_runner_macos_host_sudoers_user="joe.bad", **self.sudoers_vars())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a lower-case login name", result.stdout)
        self.assertEqual(self.sudo_calls(), [])

    # The AC power settings.

    def test_a_mac_that_sleeps_gets_only_the_settings_that_differ_as_root(self) -> None:
        result = self.run_role(github_runner_macos_host_tailscale=False)
        self.ok(result)
        self.assertEqual(self.power()["sleep"], "0")
        self.assertEqual((self.power()["displaysleep"], self.power()["disksleep"]), ("0", "0"))
        self.assertEqual([call for call in self.writes("pmset")], ["pmset root=1 -c sleep 0 displaysleep 0 disksleep 0"])
        self.assertEqual(self.recap(result)["changed"], 1)

    def test_a_second_run_changes_nothing_and_does_not_ask_for_root(self) -> None:
        self.run_role(github_runner_macos_host_tailscale=False)
        before = self.calls()
        result = self.run_role(github_runner_macos_host_tailscale=False)
        self.ok(result)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual(self.calls()[len(before) :], ["pmset root=0 -g custom"])
        self.assertEqual(len(self.sudo_calls()), 1)

    def test_a_mac_already_in_line_is_only_read(self) -> None:
        self.set_power()
        result = self.run_role(github_runner_macos_host_tailscale=False)
        self.ok(result)
        self.assertEqual(self.calls(), ["pmset root=0 -g custom"])
        self.assertEqual(self.sudo_calls(), [])
        self.assertEqual(self.recap(result)["changed"], 0)

    def test_the_wanted_values_are_the_role_variables(self) -> None:
        self.set_power()
        result = self.run_role(github_runner_macos_host_tailscale=False, github_runner_macos_host_power_ac_sleep=15, github_runner_macos_host_power_ac_womp=0)
        self.ok(result)
        self.assertEqual(self.writes("pmset"), ["pmset root=1 -c sleep 15 womp 0"])
        self.assertEqual((self.power()["sleep"], self.power()["womp"]), ("15", "0"))

    def test_check_mode_changes_nothing_and_reports_the_change(self) -> None:
        result = self.run_role("--check", github_runner_macos_host_tailscale=False)
        self.ok(result)
        self.assertEqual(self.power()["sleep"], "1")
        self.assertEqual(self.calls(), ["pmset root=0 -g custom"])
        self.assertEqual(self.sudo_calls(), [], "a dry run needs no privilege")
        self.assertEqual(self.recap(result)["changed"], 1, result.stdout)
        self.assertIn("pmset -c sleep 0 displaysleep 0 disksleep 0", result.stdout)

    def test_check_mode_on_a_mac_already_in_line_reports_nothing(self) -> None:
        self.set_power()
        result = self.run_role("--check", github_runner_macos_host_tailscale=False)
        self.ok(result)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)

    def test_the_power_settings_can_be_turned_off(self) -> None:
        result = self.run_role(github_runner_macos_host_power=False, github_runner_macos_host_tailscale=False)
        self.ok(result)
        self.assertEqual(self.calls(), [])

    def test_a_setting_that_is_not_an_allowed_value_fails_before_anything_changes(self) -> None:
        result = self.run_role(github_runner_macos_host_power_ac_womp=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("github_runner_macos_host_power_ac_womp", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_another_operating_system_is_left_alone(self) -> None:
        result = self.run_role(system="Linux", machine="x86_64")
        self.ok(result)
        self.assertEqual(self.calls(), [])

    # The Tailscale system daemon.

    def test_a_host_without_the_daemon_gets_the_formula_and_the_daemon_and_joins_nothing(self) -> None:
        self.set_power()
        result = self.run_role()
        self.ok(result)
        self.assertEqual(self.writes(), ["brew root=0 install tailscale", "tailscaled root=1 install-system-daemon"])
        self.assertTrue(self.plist.exists())
        self.assertEqual((self.stub / "login.state").read_text(), "NeedsLogin")
        self.assertFalse([call for call in self.calls() if call.startswith("tailscale ")])
        self.assertEqual(self.recap(result)["changed"], 2)

    def test_the_daemon_is_installed_from_the_prefixs_formula_and_a_second_run_changes_nothing(self) -> None:
        self.set_power()
        self.run_role()
        before = self.calls()
        result = self.run_role()
        self.ok(result)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual(self.calls()[len(before) :], ["pmset root=0 -g custom"])

    def test_a_host_that_already_has_the_daemon_is_left_entirely_alone(self) -> None:
        self.set_power()
        self.hand_built_daemon()
        result = self.run_role()
        self.ok(result)
        self.assertEqual(self.calls(), ["pmset root=0 -g custom"])
        self.assertEqual(self.recap(result)["changed"], 0)

    def test_a_hand_built_daemon_stays_alone_even_with_an_auth_key_and_no_formula(self) -> None:
        self.set_power()
        self.hand_built_daemon()
        result = self.run_role(github_runner_macos_host_tailscale_auth_key=AUTH_KEY)
        self.ok(result)
        self.assertEqual(self.calls(), ["pmset root=0 -g custom"])
        self.assertEqual(self.sudo_calls(), [])

    def test_the_tailscale_part_can_be_turned_off(self) -> None:
        self.set_power()
        result = self.run_role(github_runner_macos_host_tailscale=False)
        self.ok(result)
        self.assertEqual(self.calls(), ["pmset root=0 -g custom"])

    def test_check_mode_reports_the_daemon_install_and_installs_nothing(self) -> None:
        self.set_power()
        result = self.run_role("--check")
        self.ok(result)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.sudo_calls(), [], "a dry run needs no privilege")
        self.assertFalse(self.plist.exists())
        self.assertFalse((self.stub / "brew.installed").exists())
        self.assertEqual(self.recap(result)["changed"], 2, result.stdout)

    def test_check_mode_with_an_auth_key_on_a_host_without_the_daemon_joins_nothing(self) -> None:
        self.set_power()
        result = self.run_role("--check", github_runner_macos_host_tailscale_auth_key=AUTH_KEY)
        self.ok(result)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.sudo_calls(), [])
        self.assertNotIn(AUTH_KEY, result.stdout + result.stderr)

    def test_the_prefix_follows_the_machine_unless_it_is_set(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "tailscale.yml").read_text())
        expression = tasks[0]["ansible.builtin.set_fact"]["github_runner_macos_host_effective_prefix"]
        environment = jinja2.Environment(autoescape=False)
        for machine, prefix_set, expected in (("arm64", "", "/opt/homebrew"), ("x86_64", "", "/usr/local"), ("x86_64", "/example", "/example")):
            with self.subTest(machine=machine, prefix_set=prefix_set):
                rendered = environment.from_string(expression).render(ansible_facts={"machine": machine}, github_runner_macos_host_homebrew_prefix=prefix_set)
                self.assertEqual(rendered.strip(), expected)

    # Joining a tailnet.

    def test_an_auth_key_joins_once_with_the_key_on_standard_input_and_nowhere_else(self) -> None:
        self.set_power()
        result = self.run_role(github_runner_macos_host_tailscale_auth_key=AUTH_KEY, github_runner_macos_host_tailscale_up_args=["--hostname=example"])
        self.ok(result)
        self.assertEqual((self.stub / "login.state").read_text(), "Running")
        up = [call for call in self.calls() if call.startswith("tailscale root=1 up")]
        self.assertEqual(up, ["tailscale root=1 up --auth-key=file:/dev/stdin --hostname=example"])
        self.assertIn(f"tailscale key={key_hash(AUTH_KEY)} --auth-key=file:/dev/stdin --hostname=example", self.calls())
        self.assertNotIn(AUTH_KEY, "\n".join(self.calls()) + result.stdout + result.stderr)
        self.assertEqual(self.recap(result)["changed"], 3)

    def test_a_second_run_with_the_key_does_not_log_in_again(self) -> None:
        self.set_power()
        self.run_role(github_runner_macos_host_tailscale_auth_key=AUTH_KEY)
        before = self.calls()
        result = self.run_role(github_runner_macos_host_tailscale_auth_key=AUTH_KEY)
        self.ok(result)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual([call for call in self.calls()[len(before) :] if " up " in call], [])

    def test_a_join_that_failed_is_retried_by_the_next_run(self) -> None:
        self.set_power()
        self.run_role()
        self.assertEqual((self.stub / "login.state").read_text(), "NeedsLogin")
        result = self.run_role(github_runner_macos_host_tailscale_auth_key=AUTH_KEY)
        self.ok(result)
        self.assertEqual((self.stub / "login.state").read_text(), "Running")

    def test_a_daemon_that_is_already_logged_in_is_not_logged_in_again(self) -> None:
        self.set_power()
        self.run_role()
        (self.stub / "login.state").write_text("Running")
        result = self.run_role(github_runner_macos_host_tailscale_auth_key=AUTH_KEY)
        self.ok(result)
        self.assertEqual([call for call in self.calls() if " up " in call], [])


if __name__ == "__main__":
    unittest.main()
