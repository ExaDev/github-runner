"""Tests the github_runner_host_tools role: the filters that pick the Homebrew prefix, write the shell startup lines and render mise's configuration, the package-install security settings for npm and pnpm the role writes, the defaults against the argument specs and README, how site.yml wires the role, and the role's tasks (tests/host_tools/run.yml) against stand-ins for brew, sudo, apt-get, dpkg-query, getent, dscl and Homebrew's installer on PATH, so no package, shell startup file outside a temporary home or privilege is ever touched and nothing is downloaded."""

from __future__ import annotations

import functools
import hashlib
import http.server
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_host_tools"
RUN = ROOT / "tests" / "host_tools" / "run.yml"
PLUGIN = ROOT / "plugins" / "filter" / "host_tools.py"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
SPECS: dict[str, Any] = yaml.safe_load((ROLE / "meta" / "argument_specs.yml").read_text())["argument_specs"]["main"]["options"]
VARS: dict[str, Any] = yaml.safe_load((ROLE / "vars" / "main.yml").read_text())

_spec = importlib.util.spec_from_file_location("host_tools_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
host_tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host_tools)

# A stand-in commit and the installer it names, so that the checksum the role is given is one the tests compute.
COMMIT = "a" * 40
BREW_AT = "${HOST_TOOLS_PREFIX}/bin/brew"

INSTALLER = (
    "#!/bin/bash\n"
    'echo "installer NONINTERACTIVE=${NONINTERACTIVE-unset} root=${HOST_TOOLS_ROOT:-0}" >> "${HOST_TOOLS_STUB}/calls.log"\n'
    'mkdir -p "${HOST_TOOLS_PREFIX}/bin"\n'
    f'cp "${{HOST_TOOLS_BREW}}" "{BREW_AT}"\n'
    f'chmod 755 "{BREW_AT}"\n'
)

# One stand-in for every command the role runs, told apart by the name it is called by. State lives in files under $HOST_TOOLS_STUB: mise.state (absent, outdated or current), brew.update (what brew update prints), packages (the apt packages installed), shell (the login shell) and calls.log. sudo marks the process it runs as root, as the real one does, and the tools that need root refuse without it while brew refuses with it, as the real ones do.
STUB = r"""#!{python}
import json, os, sys

stub = os.environ["HOST_TOOLS_STUB"]
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
root = os.environ.get("HOST_TOOLS_ROOT") == "1"


def path(file):
    return os.path.join(stub, file)


def read(file, default=""):
    return open(path(file)).read() if os.path.exists(path(file)) else default


def write(file, text=""):
    os.makedirs(os.path.dirname(path(file)), exist_ok=True)
    open(path(file), "w").write(text)


def log(text):
    open(path("calls.log"), "a").write(text + "\n")


def need_root():
    if not root:
        sys.exit(name + ": must be run as root")


if name == "sudo":
    log("sudo " + " ".join(args))
    while args[0] != "/bin/sh":
        args = args[1:]
    os.environ["HOST_TOOLS_ROOT"] = "1"
    os.execv(args[0], args)
elif name == "brew":
    log("brew root=" + str(int(root)) + " " + " ".join(args))
    if root:
        sys.exit("Running Homebrew as root is extremely dangerous and no longer supported.")
    state = read("mise.state", "absent").strip()
    if args == ["update"]:
        print(read("brew.update", "Already up-to-date."))
    elif args[:2] == ["info", "--json=v2"] and args[2:] == ["mise"]:
        detail = {"name": "mise", "full_name": "mise", "tap": "homebrew/core", "installed": [{"version": "1.0"}] if state != "absent" else [], "outdated": state == "outdated"}
        print(json.dumps({"formulae": [detail], "casks": []}))
    elif args in (["install", "mise"], ["upgrade", "mise"]):
        write("mise.state", "current")
        prefix_bin = os.path.dirname(os.path.abspath(sys.argv[0]))
        target = os.path.join(prefix_bin, "mise")
        open(target, "w").write("#!/bin/sh\n")
        os.chmod(target, 0o755)
    else:
        sys.exit("unexpected brew call: " + " ".join(args))
elif name == "dpkg-query":
    log("dpkg-query " + " ".join(args))
    installed = read("packages").split()
    wanted = args[2:]
    for package in wanted:
        if package in installed:
            print(package + " ii ")
    sys.exit(0 if all(package in installed for package in wanted) else 1)
elif name == "apt-get":
    log("apt-get root=" + str(int(root)) + " " + " ".join(args))
    need_root()
    if args == ["update"]:
        pass
    elif args[:3] == ["install", "--yes", "--no-install-recommends"]:
        write("packages", read("packages") + "\n".join(args[3:]) + "\n")
    else:
        sys.exit("unexpected apt-get call: " + " ".join(args))
elif name == "getent":
    log("getent " + " ".join(args))
    print(args[1] + ":x:1000:1000:Example:/home/" + args[1] + ":" + read("shell", "/bin/bash").strip())
elif name == "dscl":
    log("dscl " + " ".join(args))
    print("UserShell: " + read("shell", "/bin/zsh").strip())
else:
    sys.exit("unexpected stand-in: " + name)
"""

STAND_INS = ("sudo", "getent", "dscl", "dpkg-query", "apt-get")
READ_ONLY = re.compile(r"(brew root=0 (info .*|update)|dpkg-query .*|getent .*|dscl .*|sudo .*)$")


# A made-up credential, so the tests can check that no run prints the file it sits in.
NPMRC_TOKEN = "made-up-token-0000"
NPMRC_USER = f"registry=https://registry.example.test/\n//registry.example.test/:_authToken={NPMRC_TOKEN}\n"
# The test inventory is written with sorted keys, so the block lists these in that order.
NPM_SETTINGS = {"ignore-scripts": True, "min-release-age": 7}
NPM_MARKS = ("# BEGIN ANSIBLE MANAGED BLOCK github_runner_host_tools npm\n", "# END ANSIBLE MANAGED BLOCK github_runner_host_tools npm\n")
NPM_BLOCK_SETTINGS = NPM_MARKS[0] + "ignore-scripts=true\nmin-release-age=7\n" + NPM_MARKS[1]
NPM_BLOCK = NPM_MARKS[0] + "ignore-scripts=true\nmin-release-age=7\nmin-release-age-exclude[]=example-a\nmin-release-age-exclude[]=@example/*\n" + NPM_MARKS[1]
PNPM_CONFIG = {"Darwin": "Library/Preferences/pnpm/config.yaml", "Linux": ".config/pnpm/config.yaml"}
PNPM_SETTINGS = {"minimumReleaseAge": 10080, "saveExact": True}
PNPM_USER = "# Mine.\nstoreDir: /somewhere/store   # the store\n\n# The age, in minutes.\nminimumReleaseAge: 1440\n\n# Exclusions.\nminimumReleaseAgeExclude:\n- old-one\n\nregistry: https://registry.example.test/\n"
PNPM_MERGED = "# Mine.\nstoreDir: /somewhere/store   # the store\n\n# The age, in minutes.\nminimumReleaseAge: 10080\n\n# Exclusions.\nminimumReleaseAgeExclude:\n  - example-a\n\nregistry: https://registry.example.test/\nsaveExact: true\n"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Serves a directory without writing a line to the test output for every request."""

    def log_message(self, format: str, *args: Any) -> None:
        pass


def homebrew_block(prefix: Path) -> str:
    return f'# BEGIN ANSIBLE MANAGED BLOCK github_runner_host_tools homebrew\n[ -x {prefix}/bin/brew ] && eval "$({prefix}/bin/brew shellenv)"\n# END ANSIBLE MANAGED BLOCK github_runner_host_tools homebrew\n'


def mise_block(prefix: Path, shell: str) -> str:
    return f'# BEGIN ANSIBLE MANAGED BLOCK github_runner_host_tools mise\n[ -x {prefix}/bin/mise ] && eval "$({prefix}/bin/mise activate {shell})"\n# END ANSIBLE MANAGED BLOCK github_runner_host_tools mise\n'


class FilterRegistrationTest(unittest.TestCase):
    def test_the_filters_are_registered_under_the_names_the_role_uses(self) -> None:
        self.assertEqual(set(host_tools.FilterModule().filters()), {"host_tools_homebrew_prefix", "host_tools_shell_integrations", "host_tools_mise_toml", "host_tools_npmrc", "host_tools_yaml_merge"})
        used = set(re.findall(r"exadev\.github_runner\.(host_tools_\w+)", "".join(path.read_text() for path in (ROLE / "tasks").glob("*.yml"))))
        self.assertEqual(used, set(host_tools.FilterModule().filters()))


class DefaultsTest(unittest.TestCase):
    def test_every_variable_is_in_the_specs_with_its_default_and_documented_in_the_readme(self) -> None:
        readme = (ROLE / "README.md").read_text()
        self.assertEqual(set(DEFAULTS), set(SPECS))
        for name, value in DEFAULTS.items():
            with self.subTest(name=name):
                self.assertEqual(SPECS[name]["default"], value)
                self.assertIn(f"`{name}`", readme)

    def test_every_variable_is_read_by_a_task(self) -> None:
        text = "".join(path.read_text() for path in (ROLE / "tasks").glob("*.yml"))
        for name in (*DEFAULTS, *VARS):
            with self.subTest(name=name):
                self.assertIn(name, text)

    def test_the_tools_and_the_shell_integration_are_on_and_the_mise_defaults_are_empty(self) -> None:
        for name in ("homebrew", "mise", "shell_integration"):
            self.assertIs(DEFAULTS[f"github_runner_host_tools_{name}"], True)
        self.assertEqual(DEFAULTS["github_runner_host_tools_mise_config"], {})
        self.assertEqual(DEFAULTS["github_runner_host_tools_homebrew_prefix"], "")

    def test_the_installer_is_pinned_to_a_commit_and_a_checksum_of_the_right_shape(self) -> None:
        self.assertRegex(DEFAULTS["github_runner_host_tools_homebrew_install_commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(DEFAULTS["github_runner_host_tools_homebrew_install_sha256"], r"^[0-9a-f]{64}$")

    def test_the_installer_comes_from_the_homebrew_install_repository_over_https(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_host_tools_homebrew_install_base_url"], "https://raw.githubusercontent.com/Homebrew/install")

    def test_the_shells_the_role_supports_are_zsh_and_bash_with_the_files_it_reads_and_writes(self) -> None:
        files = VARS["github_runner_host_tools_shell_files"]
        self.assertEqual(files["zsh"], {"read": [".zshenv", ".zshrc", ".zprofile"], "write": ".zshenv"})
        self.assertEqual(files["bash"], {"read": [".bashrc", ".bash_profile", ".profile"], "write": ".bashrc"})
        for shell in files.values():
            self.assertIn(shell["write"], shell["read"])

    def test_the_linux_prerequisites_are_the_ones_homebrew_documents(self) -> None:
        self.assertEqual(VARS["github_runner_host_tools_apt_prerequisites"], ["build-essential", "procps", "curl", "file", "git"])


def all_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every task in a task list, with the tasks inside blocks."""
    found: list[dict[str, Any]] = []
    for task in tasks:
        found.append(task)
        for key in ("block", "rescue", "always"):
            found.extend(all_tasks(task.get(key, [])))
    return found


class TasksTest(unittest.TestCase):
    def tasks(self) -> list[tuple[str, dict[str, Any]]]:
        return [(path.name, task) for path in sorted((ROLE / "tasks").glob("*.yml")) for task in all_tasks(yaml.safe_load(path.read_text()))]

    def test_the_only_formula_the_role_names_to_homebrew_is_mise(self) -> None:
        homebrew = [(name, task["community.general.homebrew"]) for name, task in self.tasks() if "community.general.homebrew" in task]
        self.assertGreaterEqual(len(homebrew), 1)
        for name, module in homebrew:
            with self.subTest(file=name, module=module):
                packages = module.get("name", [])
                self.assertIn(packages if isinstance(packages, list) else [packages], ([], ["mise"]))
                self.assertNotIn("upgrade_all", module)
                self.assertNotIn("upgrade", module)
                self.assertIs(module["update_homebrew"], True)
                self.assertEqual(module.get("state", "present"), "latest" if packages else "present")

    def test_mise_is_kept_at_its_latest_release_by_the_same_call_that_updates_homebrew(self) -> None:
        [module] = [task["community.general.homebrew"] for name, task in self.tasks() if task.get("name") == "Update Homebrew and bring mise to its latest release"]
        self.assertEqual((module["name"], module["state"], module["update_homebrew"]), ("mise", "latest", True))

    def test_no_task_shells_out_to_brew_or_runs_a_shell_command(self) -> None:
        for name, task in self.tasks():
            with self.subTest(file=name, task=task.get("name")):
                self.assertNotIn("ansible.builtin.shell", task)
                argv = task.get("ansible.builtin.command", {}).get("argv", [])
                self.assertNotIn("brew", argv if isinstance(argv, list) else [])

    def test_the_installer_is_downloaded_with_a_checksum_and_run_by_file_never_piped_into_a_shell(self) -> None:
        tasks = [task for name, task in self.tasks() if name == "install_homebrew.yml"]
        [download] = [task["ansible.builtin.get_url"] for task in tasks if "ansible.builtin.get_url" in task]
        self.assertEqual(download["checksum"], "sha256:{{ github_runner_host_tools_homebrew_install_sha256 }}")
        self.assertTrue(download["url"].startswith("{{ github_runner_host_tools_homebrew_install_base_url }}/{{ github_runner_host_tools_homebrew_install_commit }}/"))
        [run] = [task for task in tasks if task.get("name") == "Run the installer"]
        self.assertEqual(run["environment"]["NONINTERACTIVE"], "1")
        self.assertEqual(run["ansible.builtin.command"]["creates"], "{{ github_runner_host_tools_effective_prefix }}/bin/brew")
        self.assertIsInstance(run["ansible.builtin.command"]["argv"], list)
        for name, task in self.tasks():
            self.assertNotIn("shell", {key.split(".")[-1] for key in task if key.startswith("ansible.builtin.")}, name)

    def test_the_installer_is_never_run_as_root(self) -> None:
        for name, task in self.tasks():
            if task.get("name") in ("Download the installer", "Run the installer", "Make a directory for the installer"):
                with self.subTest(task=task["name"]):
                    self.assertNotIn("become", task)

    def test_the_shell_integration_never_sets_a_mode_on_a_startup_file(self) -> None:
        [block] = [task["ansible.builtin.blockinfile"] for name, task in self.tasks() if "ansible.builtin.blockinfile" in task and name == "shell_integration_line.yml"]
        self.assertNotIn("mode", block)
        self.assertIs(block["create"], True)

    def test_the_npm_settings_block_never_sets_a_mode_and_a_missing_npmrc_is_created_private(self) -> None:
        [block] = [task["ansible.builtin.blockinfile"] for name, task in self.tasks() if "ansible.builtin.blockinfile" in task and name == "npm_settings.yml"]
        self.assertNotIn("mode", block)
        [created] = [task for name, task in self.tasks() if name == "npm_settings.yml" and task.get("name") == "Create ~/.npmrc when it does not exist"]
        self.assertEqual(created["ansible.builtin.copy"]["mode"], "0600")
        self.assertIs(created["ansible.builtin.copy"]["force"], False)
        self.assertIn("not github_runner_host_tools_npmrc.stat.exists", created["when"])

    def test_the_npmrc_is_never_shown_or_logged(self) -> None:
        [read] = [task for name, task in self.tasks() if name == "npm_settings.yml" and "ansible.builtin.slurp" in task]
        self.assertIs(read["no_log"], True)

    def test_the_pnpm_config_is_written_without_a_mode_and_through_a_symlink(self) -> None:
        [write] = [task["ansible.builtin.copy"] for name, task in self.tasks() if name == "pnpm_settings.yml" and "ansible.builtin.copy" in task]
        self.assertNotIn("mode", write)
        self.assertIs(write["follow"], True)

    def test_the_pnpm_config_paths_are_the_ones_pnpm_documents(self) -> None:
        self.assertEqual(VARS["github_runner_host_tools_pnpm_config_files"], {"Darwin": "Library/Preferences/pnpm/config.yaml", "Linux": ".config/pnpm/config.yaml"})

    def test_the_package_install_settings_write_nothing_by_default(self) -> None:
        for name in ("npm_settings", "npm_min_release_age_exclude", "pnpm_settings", "pnpm_minimum_release_age_exclude"):
            with self.subTest(name=name):
                self.assertEqual(DEFAULTS[f"github_runner_host_tools_{name}"], {} if name.endswith("_settings") else [])

    def test_the_mise_configuration_goes_to_conf_d_and_never_to_config_toml(self) -> None:
        paths = [str(module.get("dest") or module.get("path")) for _, task in self.tasks() for key, module in task.items() if key.startswith("ansible.builtin.") and isinstance(module, dict) and (module.get("dest") or module.get("path"))]
        mise_paths = [path for path in paths if "/.config/mise" in path]
        self.assertIn("{{ ansible_facts['user_dir'] }}/.config/mise/conf.d/00-fleet.toml", mise_paths)
        for path in mise_paths:
            with self.subTest(path=path):
                self.assertIn("/conf.d", path)
                self.assertFalse(path.endswith("config.toml") and "00-fleet" not in path)
        for name, task in self.tasks():
            self.assertNotIn("absent", str(task.get("ansible.builtin.file", {}).get("state", "")) if "mise" in str(task) else "", name)

    def test_every_task_that_runs_the_installer_or_changes_the_host_is_guarded_by_check_mode(self) -> None:
        for name, task in self.tasks():
            if task.get("name") in ("Refresh the package index", "Install the missing packages"):
                self.assertIn("not ansible_check_mode", task["when"], name)
        [download] = [task for name, task in self.tasks() if task.get("name") == "Download and run the installer"]
        self.assertEqual(download["when"], "not ansible_check_mode")


class WiringTest(unittest.TestCase):
    def plays(self) -> list[dict[str, Any]]:
        loaded = yaml.safe_load((ROOT / "playbooks" / "site.yml").read_text())
        assert isinstance(loaded, list)
        return loaded

    def test_the_play_runs_after_the_validation_and_before_both_host_baseline_plays(self) -> None:
        names = [play["name"] for play in self.plays()]
        play = names.index("Install Homebrew and mise")
        self.assertLess(names.index("Validate the ARC configuration"), play)
        self.assertLess(play, names.index("Set up the Macs' host baseline"))
        self.assertLess(play, names.index("Set up the Linux hosts' host baseline"))

    def test_it_runs_the_role_for_the_same_hosts_as_the_baseline_plays(self) -> None:
        [play] = [play for play in self.plays() if play["name"] == "Install Homebrew and mise"]
        self.assertEqual(play["roles"], ["exadev.github_runner.github_runner_host_tools"])
        [baseline] = [play for play in self.plays() if play["name"] == "Set up the Macs' host baseline"]
        self.assertEqual(play["hosts"], baseline["hosts"])
        self.assertIs(play["gather_facts"], False)

    def test_the_role_is_listed_in_the_collection_readme(self) -> None:
        self.assertIn("`github_runner_host_tools`", (ROOT / "README.md").read_text())

    def test_bootstrap_turns_the_tools_off_because_it_runs_without_sudo(self) -> None:
        text = (ROOT / "bootstrap.sh").read_text()
        self.assertIn("github_runner_host_tools_homebrew: false", text)
        self.assertIn("github_runner_host_tools_mise: false", text)


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook not installed")
class RoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.stub = self.work / "stub"
        self.prefix = self.work / "prefix"
        self.home = self.work / "home"
        self.tmp = self.work / "tmp"
        self.tools = self.work / "tools"
        self.served = self.work / "served"
        for directory in (self.stub, self.prefix / "bin", self.home, self.tmp, self.tools, self.served / COMMIT):
            directory.mkdir(parents=True)
        for name in (*STAND_INS, "brew"):
            self.write_executable(self.tools / name, STUB.replace("{python}", sys.executable))
        self.installer = self.served / COMMIT / "install.sh"
        self.installer.write_text(INSTALLER)
        # The installer is served over HTTP from the loopback address, the one scheme get_url documents, so the test downloads exactly as a real run does and nothing leaves the machine.
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(self.served)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def write_executable(self, path: Path, text: str) -> None:
        path.write_text(text)
        path.chmod(0o755)

    def have_homebrew(self) -> None:
        shutil.copy(self.tools / "brew", self.prefix / "bin" / "brew")

    def run_role(self, *extra: str, system: str = "Darwin", machine: str = "arm64", os_family: str = "Darwin", user: str = "example", **variables: Any) -> subprocess.CompletedProcess[str]:
        host = {
            "host_system": system,
            "host_machine": machine,
            "host_os_family": os_family,
            "host_user": user,
            "host_home": str(self.home),
            "github_runner_host_tools_homebrew_prefix": str(self.prefix),
            "github_runner_host_tools_homebrew_install_base_url": f"http://127.0.0.1:{self.server.server_address[1]}",
            "github_runner_host_tools_homebrew_install_commit": COMMIT,
            "github_runner_host_tools_homebrew_install_sha256": hashlib.sha256(self.installer.read_bytes()).hexdigest(),
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
            "HOST_TOOLS_STUB": str(self.stub),
            "HOST_TOOLS_PREFIX": str(self.prefix),
            "HOST_TOOLS_BREW": str(self.tools / "brew"),
            # The stand-ins come first on the playbook's own PATH, so nothing the role runs can reach the machine's real Homebrew, sudo or apt; the installer and the temporary directory it is downloaded to stay in the test's own directory.
            "PATH": f"{self.tools}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(self.tmp),
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }
        return subprocess.run([ANSIBLE_PLAYBOOK, "-i", str(inventory), str(RUN), *extra], capture_output=True, text=True, check=False, env=env, cwd=self.work)

    def logged(self) -> list[str]:
        log = self.stub / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def changes(self) -> list[str]:
        """The calls that change the host, without the reads the role makes to find out and the sudo that wraps them."""
        return [call for call in self.logged() if not READ_ONLY.match(call)]

    def host_calls(self) -> list[str]:
        """The calls to the commands that change or install anything, which --check must not make."""
        return [call for call in self.logged() if call.startswith(("brew", "installer", "apt-get", "sudo"))]

    def recap(self, result: subprocess.CompletedProcess[str]) -> dict[str, int]:
        match = re.search(r"node1\s+: ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", result.stdout)
        assert match is not None, result.stdout + result.stderr
        return dict(zip(("ok", "changed", "unreachable", "failed"), map(int, match.groups())))

    def ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def squashed(self, result: subprocess.CompletedProcess[str]) -> str:
        return re.sub(r"\s+", " ", result.stdout + result.stderr)

    # Homebrew.

    def test_a_mac_without_homebrew_gets_the_pinned_installer_run_non_interactively_as_the_user_then_mise(self) -> None:
        result = self.run_role(github_runner_host_tools_shell_integration=False)
        self.ok(result)
        self.assertEqual(self.changes(), ["installer NONINTERACTIVE=1 root=0", "brew root=0 install mise"])
        self.assertTrue((self.prefix / "bin" / "brew").exists())
        self.assertTrue((self.prefix / "bin" / "mise").exists())
        self.assertEqual(list(self.tmp.iterdir()), [], "the installer's directory was left behind")

    def test_a_second_run_changes_nothing_but_the_brew_update_it_always_makes(self) -> None:
        self.ok(self.run_role(github_runner_host_tools_shell_integration=False))
        before = len(self.logged())
        result = self.run_role(github_runner_host_tools_shell_integration=False)
        self.ok(result)
        self.assertEqual(self.logged()[before:], ["brew root=0 update", "brew root=0 info --json=v2 mise"])
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)

    def test_homebrew_is_updated_on_every_run_and_a_real_update_is_a_change(self) -> None:
        self.have_homebrew()
        (self.stub / "mise.state").write_text("current")
        (self.stub / "brew.update").write_text("Updated 1 tap (homebrew/core).")
        result = self.run_role(github_runner_host_tools_shell_integration=False)
        self.ok(result)
        self.assertIn("brew root=0 update", self.logged())
        self.assertEqual(self.recap(result)["changed"], 1, result.stdout)

    def test_an_existing_homebrew_is_not_installed_over(self) -> None:
        self.have_homebrew()
        self.ok(self.run_role(github_runner_host_tools_shell_integration=False))
        self.assertFalse([call for call in self.logged() if call.startswith("installer")])
        self.assertFalse([call for call in self.logged() if call.startswith("sudo")])

    def test_mise_is_upgraded_when_it_is_outdated_and_nothing_else_ever_is(self) -> None:
        self.have_homebrew()
        (self.stub / "mise.state").write_text("outdated")
        self.ok(self.run_role(github_runner_host_tools_shell_integration=False))
        self.assertEqual([call for call in self.logged() if re.match(r"brew root=0 (install|upgrade)", call)], ["brew root=0 upgrade mise"])
        self.assertEqual([call for call in self.logged() if call.startswith("brew")], ["brew root=0 update", "brew root=0 info --json=v2 mise", "brew root=0 upgrade mise"])

    def test_a_download_that_does_not_match_the_checksum_is_refused_and_nothing_is_left_behind(self) -> None:
        result = self.run_role(github_runner_host_tools_homebrew_install_sha256="0" * 64, github_runner_host_tools_shell_integration=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum", result.stdout.lower())
        self.assertEqual(self.logged(), [])
        self.assertFalse((self.prefix / "bin" / "brew").exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_a_commit_or_checksum_that_is_not_a_hash_is_refused_before_anything_runs(self) -> None:
        for variable, value in (("github_runner_host_tools_homebrew_install_commit", "main"), ("github_runner_host_tools_homebrew_install_sha256", "ABC")):
            with self.subTest(variable=variable):
                result = self.run_role(**{variable: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("hexadecimal", self.squashed(result))
                self.assertEqual(self.logged(), [])

    def test_the_role_refuses_to_run_as_root_with_a_clear_message_and_changes_nothing(self) -> None:
        result = self.run_role(user="root")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Homebrew refuses to run as root", self.squashed(result))
        self.assertEqual(self.logged(), [])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_check_mode_downloads_and_runs_nothing_and_reports_what_it_would_do(self) -> None:
        result = self.run_role("--check")
        self.ok(result)
        self.assertEqual(self.host_calls(), [])
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertFalse((self.prefix / "bin" / "brew").exists())
        self.assertIn("install.sh at " + COMMIT, self.squashed(result))
        self.assertGreaterEqual(self.recap(result)["changed"], 1, result.stdout)

    def test_check_mode_on_a_host_with_homebrew_reports_the_update_without_running_it(self) -> None:
        self.have_homebrew()
        result = self.run_role("--check", github_runner_host_tools_shell_integration=False)
        self.ok(result)
        self.assertEqual(self.logged(), [])
        self.assertEqual(self.recap(result)["changed"], 1, result.stdout)

    def test_mise_can_be_left_out_and_homebrew_is_then_only_updated(self) -> None:
        self.have_homebrew()
        self.ok(self.run_role(github_runner_host_tools_mise=False, github_runner_host_tools_shell_integration=False))
        self.assertEqual(self.logged(), ["brew root=0 update"])

    def test_mise_without_homebrew_is_refused(self) -> None:
        result = self.run_role(github_runner_host_tools_homebrew=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed through Homebrew", self.squashed(result))
        self.assertEqual(self.logged(), [])

    def test_with_both_tools_off_nothing_is_touched(self) -> None:
        result = self.run_role(github_runner_host_tools_homebrew=False, github_runner_host_tools_mise=False)
        self.ok(result)
        self.assertEqual(self.logged(), [])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_a_host_the_role_does_not_support_is_skipped_with_a_message(self) -> None:
        for system, family in (("Linux", "RedHat"), ("FreeBSD", "FreeBSD")):
            with self.subTest(family=family):
                result = self.run_role(system=system, os_family=family, machine="amd64")
                self.ok(result)
                self.assertIn("supports macOS and Debian-family Linux only", self.squashed(result))
                self.assertEqual(self.logged(), [])
                self.assertEqual(list(self.home.iterdir()), [])
                self.assertEqual(self.recap(result)["changed"], 0)

    # Linux.

    def test_a_debian_host_without_homebrew_gets_the_prerequisites_by_apt_as_root_then_homebrew_as_the_user(self) -> None:
        result = self.run_role(system="Linux", os_family="Debian", machine="x86_64", github_runner_host_tools_shell_integration=False)
        self.ok(result)
        self.assertEqual(
            self.changes(),
            [
                "apt-get root=1 update",
                "apt-get root=1 install --yes --no-install-recommends build-essential procps curl file git",
                "installer NONINTERACTIVE=1 root=0",
                "brew root=0 install mise",
            ],
        )

    def test_only_the_prerequisites_that_are_missing_are_installed(self) -> None:
        (self.stub / "packages").write_text("curl\ngit\nfile\n")
        self.ok(self.run_role(system="Linux", os_family="Debian", machine="x86_64", github_runner_host_tools_shell_integration=False))
        self.assertIn("apt-get root=1 install --yes --no-install-recommends build-essential procps", self.logged())

    def test_a_debian_host_that_has_homebrew_needs_no_apt_and_no_privilege(self) -> None:
        self.have_homebrew()
        self.ok(self.run_role(system="Linux", os_family="Debian", machine="x86_64", github_runner_host_tools_shell_integration=False))
        self.assertEqual([call for call in self.logged() if call.startswith(("apt-get", "dpkg-query", "sudo"))], [])

    def test_check_mode_on_a_debian_host_reports_the_packages_without_installing_them(self) -> None:
        result = self.run_role("--check", system="Linux", os_family="Debian", machine="x86_64")
        self.ok(result)
        self.assertEqual(self.host_calls(), [])
        self.assertIn("apt-get install build-essential procps curl file git", self.squashed(result))

    # Shell startup files.

    def test_zsh_gets_both_blocks_in_zshenv_and_a_second_run_changes_nothing(self) -> None:
        self.have_homebrew()
        (self.stub / "shell").write_text("/bin/zsh")
        self.ok(self.run_role())
        self.assertEqual((self.home / ".zshenv").read_text(), homebrew_block(self.prefix) + mise_block(self.prefix, "zsh"))
        (self.stub / "mise.state").write_text("current")
        result = self.run_role()
        self.ok(result)
        self.assertEqual((self.home / ".zshenv").read_text(), homebrew_block(self.prefix) + mise_block(self.prefix, "zsh"))
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)

    def test_bash_gets_both_blocks_in_bashrc_with_the_shell_it_is_activated_for(self) -> None:
        self.have_homebrew()
        self.ok(self.run_role(system="Linux", os_family="Debian", machine="x86_64"))
        self.assertEqual((self.home / ".bashrc").read_text(), homebrew_block(self.prefix) + mise_block(self.prefix, "bash"))
        self.assertFalse((self.home / ".zshenv").exists())

    def test_the_login_shell_comes_from_the_account_entry_not_from_the_os(self) -> None:
        self.have_homebrew()
        (self.stub / "shell").write_text("/bin/bash")
        self.ok(self.run_role())
        self.assertEqual((self.home / ".bashrc").read_text(), homebrew_block(self.prefix) + mise_block(self.prefix, "bash"))
        self.assertIn("dscl . -read /Users/example UserShell", self.logged())

    def test_a_line_a_startup_file_has_already_is_not_added_again_in_any_file_of_that_shell(self) -> None:
        self.have_homebrew()
        (self.stub / "shell").write_text("/bin/zsh")
        (self.home / ".zprofile").write_text('eval "$(/opt/homebrew/bin/brew shellenv)"\n')
        (self.home / ".zshrc").write_text('eval "$(mise activate zsh)"\n')
        self.ok(self.run_role())
        self.assertFalse((self.home / ".zshenv").exists())
        self.assertEqual((self.home / ".zprofile").read_text(), 'eval "$(/opt/homebrew/bin/brew shellenv)"\n')

    def test_only_the_missing_tool_is_added(self) -> None:
        self.have_homebrew()
        (self.stub / "shell").write_text("/bin/zsh")
        (self.home / ".zshrc").write_text('eval "$(/opt/homebrew/bin/brew shellenv)"\n')
        self.ok(self.run_role())
        self.assertEqual((self.home / ".zshenv").read_text(), mise_block(self.prefix, "zsh"))

    def test_a_commented_out_line_does_not_count(self) -> None:
        self.have_homebrew()
        (self.stub / "shell").write_text("/bin/zsh")
        (self.home / ".zshrc").write_text('# eval "$(/opt/homebrew/bin/brew shellenv)"\n#eval "$(mise activate zsh)"\n')
        self.ok(self.run_role())
        self.assertEqual((self.home / ".zshenv").read_text(), homebrew_block(self.prefix) + mise_block(self.prefix, "zsh"))

    def test_a_startup_file_of_the_other_shell_does_not_count(self) -> None:
        self.have_homebrew()
        (self.stub / "shell").write_text("/bin/zsh")
        (self.home / ".bashrc").write_text('eval "$(/opt/homebrew/bin/brew shellenv)"\neval "$(mise activate bash)"\n')
        self.ok(self.run_role())
        self.assertEqual((self.home / ".zshenv").read_text(), homebrew_block(self.prefix) + mise_block(self.prefix, "zsh"))

    def test_an_existing_file_keeps_its_mode_and_its_content_and_gains_the_blocks(self) -> None:
        self.have_homebrew()
        (self.stub / "shell").write_text("/bin/zsh")
        startup = self.home / ".zshenv"
        startup.write_text("export EXAMPLE=1\n")
        startup.chmod(0o600)
        self.ok(self.run_role())
        self.assertEqual(stat.S_IMODE(startup.stat().st_mode), 0o600)
        self.assertEqual(startup.read_text(), "export EXAMPLE=1\n" + homebrew_block(self.prefix) + mise_block(self.prefix, "zsh"))

    def test_a_startup_file_that_is_a_symlink_stays_a_symlink_and_its_target_is_edited(self) -> None:
        self.have_homebrew()
        (self.stub / "shell").write_text("/bin/zsh")
        target = self.work / "dotfiles-zshenv"
        target.write_text("export EXAMPLE=1\n")
        (self.home / ".zshenv").symlink_to(target)
        self.ok(self.run_role())
        self.assertTrue((self.home / ".zshenv").is_symlink())
        self.assertIn(homebrew_block(self.prefix), target.read_text())

    def test_the_role_updates_its_own_block_when_the_prefix_changes(self) -> None:
        self.have_homebrew()
        (self.stub / "shell").write_text("/bin/zsh")
        self.ok(self.run_role(github_runner_host_tools_mise=False))
        other = self.work / "other"
        (other / "bin").mkdir(parents=True)
        shutil.copy(self.tools / "brew", other / "bin" / "brew")
        self.ok(self.run_role(github_runner_host_tools_mise=False, github_runner_host_tools_homebrew_prefix=str(other)))
        self.assertEqual((self.home / ".zshenv").read_text(), homebrew_block(other))

    def test_a_shell_the_role_does_not_set_up_is_left_alone_with_a_message(self) -> None:
        self.have_homebrew()
        (self.stub / "shell").write_text("/usr/bin/fish")
        result = self.run_role()
        self.ok(result)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertIn("so no startup file was changed", self.squashed(result))

    def test_the_shell_integration_can_be_turned_off(self) -> None:
        self.have_homebrew()
        self.ok(self.run_role(github_runner_host_tools_shell_integration=False))
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertFalse([call for call in self.logged() if call.startswith(("getent", "dscl"))])

    def test_check_mode_reports_the_blocks_without_writing_them(self) -> None:
        self.have_homebrew()
        result = self.run_role("--check", "--diff")
        self.ok(result)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertIn("brew shellenv", result.stdout)

    # mise configuration.

    def fleet_file(self) -> Path:
        return self.home / ".config" / "mise" / "conf.d" / "00-fleet.toml"

    def test_an_empty_mise_configuration_writes_nothing_and_leaves_an_existing_file_alone(self) -> None:
        self.have_homebrew()
        self.fleet_file().parent.mkdir(parents=True)
        self.fleet_file().write_text("# the host's own\n")
        self.ok(self.run_role(github_runner_host_tools_shell_integration=False))
        self.assertEqual(self.fleet_file().read_text(), "# the host's own\n")
        self.assertEqual(sorted(path.name for path in self.fleet_file().parent.parent.iterdir()), ["conf.d"])

    def test_a_mise_configuration_is_rendered_to_conf_d_and_a_second_run_changes_nothing(self) -> None:
        self.have_homebrew()
        (self.stub / "mise.state").write_text("current")
        config = {"tools": {"node": "22", "python": ["3.13", "3.12"]}, "settings": {"experimental": True}}
        self.ok(self.run_role(github_runner_host_tools_shell_integration=False, github_runner_host_tools_mise_config=config))
        self.assertEqual(
            self.fleet_file().read_text(),
            '# Managed by Ansible (exadev.github_runner.github_runner_host_tools). Edits to this file are overwritten.\n[tools]\nnode = "22"\npython = ["3.13", "3.12"]\n\n[settings]\nexperimental = true\n',
        )
        result = self.run_role(github_runner_host_tools_shell_integration=False, github_runner_host_tools_mise_config=config)
        self.ok(result)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)

    def test_the_hosts_own_config_toml_is_never_written_or_removed(self) -> None:
        self.have_homebrew()
        own = self.home / ".config" / "mise" / "config.toml"
        own.parent.mkdir(parents=True)
        own.write_text('[tools]\nnode = "20"\n')
        self.ok(self.run_role(github_runner_host_tools_shell_integration=False, github_runner_host_tools_mise_config={"tools": {"node": "22"}}))
        self.assertEqual(own.read_text(), '[tools]\nnode = "20"\n')
        self.ok(self.run_role(github_runner_host_tools_shell_integration=False))
        self.assertEqual(own.read_text(), '[tools]\nnode = "20"\n')
        self.assertTrue(self.fleet_file().exists())

    def test_check_mode_reports_the_mise_configuration_without_a_directory_to_write_it_in(self) -> None:
        self.have_homebrew()
        result = self.run_role("--check", github_runner_host_tools_shell_integration=False, github_runner_host_tools_mise_config={"tools": {"node": "22"}})
        self.ok(result)
        self.assertFalse((self.home / ".config").exists())
        self.assertIn("00-fleet.toml would be written", result.stdout)

    def test_a_mise_configuration_the_filter_cannot_render_fails_the_run(self) -> None:
        self.have_homebrew()
        result = self.run_role(github_runner_host_tools_shell_integration=False, github_runner_host_tools_mise_config={"env": {"A": "b"}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("renders only tools and settings", self.squashed(result))
        self.assertFalse(self.fleet_file().exists())


    # Package-install security settings. The tests turn Homebrew and mise off, so they need none of the stand-ins and a host with no tool installed is enough.

    def run_settings(self, *extra: str, **variables: Any) -> subprocess.CompletedProcess[str]:
        return self.run_role(*extra, github_runner_host_tools_homebrew=False, github_runner_host_tools_mise=False, **variables)

    def npmrc(self) -> Path:
        return self.home / ".npmrc"

    def pnpm_config(self, system: str = "Darwin") -> Path:
        return self.home / PNPM_CONFIG[system]

    def mode(self, path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    def test_no_package_install_variable_set_leaves_every_file_alone_and_unread(self) -> None:
        self.npmrc().write_text(NPMRC_USER)
        self.npmrc().chmod(0o640)
        self.pnpm_config().parent.mkdir(parents=True)
        self.pnpm_config().write_text(PNPM_USER)
        before = (self.npmrc().stat().st_mtime_ns, self.pnpm_config().stat().st_mtime_ns)
        result = self.run_settings()
        self.ok(result)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual(self.npmrc().read_text(), NPMRC_USER)
        self.assertEqual(self.mode(self.npmrc()), 0o640)
        self.assertEqual(self.pnpm_config().read_text(), PNPM_USER)
        self.assertEqual((self.npmrc().stat().st_mtime_ns, self.pnpm_config().stat().st_mtime_ns), before)
        self.assertNotIn(NPMRC_TOKEN, self.squashed(result))

    def test_no_package_install_variable_set_creates_no_file_on_a_clean_home(self) -> None:
        self.ok(self.run_settings())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_npm_settings_go_into_one_block_after_the_users_own_lines_and_a_second_run_changes_nothing(self) -> None:
        self.npmrc().write_text(NPMRC_USER)
        variables = {"github_runner_host_tools_npm_settings": NPM_SETTINGS, "github_runner_host_tools_npm_min_release_age_exclude": ["example-a", "@example/*"]}
        result = self.run_settings(**variables)
        self.ok(result)
        self.assertEqual(self.npmrc().read_text(), NPMRC_USER + NPM_BLOCK)
        self.assertNotIn(NPMRC_TOKEN, self.squashed(result))
        result = self.run_settings(**variables)
        self.ok(result)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual(self.npmrc().read_text(), NPMRC_USER + NPM_BLOCK)

    def test_a_new_npmrc_is_private_and_an_existing_ones_mode_is_kept(self) -> None:
        self.ok(self.run_settings(github_runner_host_tools_npm_settings=NPM_SETTINGS))
        self.assertEqual(self.mode(self.npmrc()), 0o600)
        self.assertEqual(self.npmrc().read_text(), "# BEGIN ANSIBLE MANAGED BLOCK github_runner_host_tools npm\nignore-scripts=true\nmin-release-age=7\n# END ANSIBLE MANAGED BLOCK github_runner_host_tools npm\n")
        for mode in (0o644, 0o640, 0o400):
            with self.subTest(mode=oct(mode)):
                self.npmrc().chmod(0o600)
                self.npmrc().write_text(NPMRC_USER)
                self.npmrc().chmod(mode)
                self.ok(self.run_settings(github_runner_host_tools_npm_settings=NPM_SETTINGS))
                self.assertEqual(self.mode(self.npmrc()), mode)
                self.assertEqual(self.npmrc().read_text(), NPMRC_USER + NPM_BLOCK_SETTINGS)

    def test_dropping_an_entry_removes_its_line_and_dropping_all_removes_the_block(self) -> None:
        self.npmrc().write_text(NPMRC_USER)
        self.ok(self.run_settings(github_runner_host_tools_npm_settings=NPM_SETTINGS, github_runner_host_tools_npm_min_release_age_exclude=["example-a", "example-b"]))
        self.assertIn("min-release-age-exclude[]=example-b\n", self.npmrc().read_text())
        self.ok(self.run_settings(github_runner_host_tools_npm_settings={"min-release-age": 7}, github_runner_host_tools_npm_min_release_age_exclude=["example-a"]))
        self.assertEqual(self.npmrc().read_text(), NPMRC_USER + "# BEGIN ANSIBLE MANAGED BLOCK github_runner_host_tools npm\nmin-release-age=7\nmin-release-age-exclude[]=example-a\n# END ANSIBLE MANAGED BLOCK github_runner_host_tools npm\n")
        result = self.run_settings()
        self.ok(result)
        self.assertEqual(self.npmrc().read_text(), NPMRC_USER)
        self.assertEqual(self.recap(result)["changed"], 1, result.stdout)
        result = self.run_settings()
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)

    def test_a_changed_value_is_rewritten_in_the_block(self) -> None:
        self.ok(self.run_settings(github_runner_host_tools_npm_settings={"min-release-age": 3}))
        self.ok(self.run_settings(github_runner_host_tools_npm_settings={"min-release-age": 7}))
        self.assertEqual(self.npmrc().read_text().count("min-release-age"), 3)
        self.assertIn("\nmin-release-age=7\n", self.npmrc().read_text())

    def test_a_key_the_user_sets_outside_the_block_is_kept_and_reported_and_the_role_does_not_write_it(self) -> None:
        self.npmrc().write_text(NPMRC_USER + "min-release-age=30\n# ignore-scripts=false\n")
        variables = {"github_runner_host_tools_npm_settings": NPM_SETTINGS}
        result = self.run_settings(**variables)
        self.ok(result)
        self.assertEqual(self.npmrc().read_text(), NPMRC_USER + "min-release-age=30\n# ignore-scripts=false\n# BEGIN ANSIBLE MANAGED BLOCK github_runner_host_tools npm\nignore-scripts=true\n# END ANSIBLE MANAGED BLOCK github_runner_host_tools npm\n")
        self.assertIn("already sets min-release-age outside the block", self.squashed(result))
        self.assertNotIn(NPMRC_TOKEN, self.squashed(result))
        result = self.run_settings(**variables)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)

    def test_a_key_the_user_adds_later_takes_it_out_of_the_block(self) -> None:
        self.ok(self.run_settings(github_runner_host_tools_npm_settings=NPM_SETTINGS))
        self.npmrc().write_text("min-release-age=30\n" + self.npmrc().read_text())
        self.ok(self.run_settings(github_runner_host_tools_npm_settings=NPM_SETTINGS))
        self.assertEqual(self.npmrc().read_text().count("min-release-age"), 1)
        self.assertEqual(self.npmrc().read_text().splitlines()[0], "min-release-age=30")

    def test_every_setting_the_user_already_has_leaves_no_block_and_no_change(self) -> None:
        self.npmrc().write_text("min-release-age=30\nignore-scripts=false\n")
        result = self.run_settings(github_runner_host_tools_npm_settings=NPM_SETTINGS)
        self.ok(result)
        self.assertEqual(self.npmrc().read_text(), "min-release-age=30\nignore-scripts=false\n")
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)

    def test_an_exclusion_the_user_lists_already_is_not_added_again(self) -> None:
        self.npmrc().write_text("min-release-age-exclude[]=example-a\n")
        self.ok(self.run_settings(github_runner_host_tools_npm_min_release_age_exclude=["example-a", "example-b"]))
        self.assertEqual(self.npmrc().read_text(), "min-release-age-exclude[]=example-a\n# BEGIN ANSIBLE MANAGED BLOCK github_runner_host_tools npm\nmin-release-age-exclude[]=example-b\n# END ANSIBLE MANAGED BLOCK github_runner_host_tools npm\n")

    def test_a_file_with_no_final_newline_keeps_its_last_line(self) -> None:
        self.npmrc().write_text("registry=https://registry.example.test/")
        self.ok(self.run_settings(github_runner_host_tools_npm_settings={"fund": False}))
        self.assertEqual(self.npmrc().read_text().splitlines()[0], "registry=https://registry.example.test/")
        self.assertEqual(self.npmrc().read_text().count("fund=false"), 1)

    def test_a_symlinked_npmrc_stays_a_link_and_its_target_is_edited(self) -> None:
        target = self.work / "dotfiles-npmrc"
        target.write_text(NPMRC_USER)
        target.chmod(0o640)
        self.npmrc().symlink_to(target)
        self.ok(self.run_settings(github_runner_host_tools_npm_settings=NPM_SETTINGS))
        self.assertTrue(self.npmrc().is_symlink())
        self.assertEqual(target.read_text(), NPMRC_USER + NPM_BLOCK_SETTINGS)
        self.assertEqual(self.mode(target), 0o640)

    def test_check_mode_reports_the_npm_block_and_writes_nothing(self) -> None:
        result = self.run_settings("--check", github_runner_host_tools_npm_settings=NPM_SETTINGS)
        self.ok(result)
        self.assertFalse(self.npmrc().exists())
        self.assertGreaterEqual(self.recap(result)["changed"], 1, result.stdout)
        self.npmrc().write_text(NPMRC_USER)
        result = self.run_settings("--check", github_runner_host_tools_npm_settings=NPM_SETTINGS)
        self.ok(result)
        self.assertEqual(self.npmrc().read_text(), NPMRC_USER)
        self.assertGreaterEqual(self.recap(result)["changed"], 1, result.stdout)

    def test_a_value_npm_would_misread_is_refused_and_leaves_no_file(self) -> None:
        result = self.run_settings(github_runner_host_tools_npm_settings={"fund": "a;b"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("would not read it back", self.squashed(result))
        self.assertFalse(self.npmrc().exists())

    def test_a_value_npm_would_misread_is_refused_and_the_existing_file_is_unchanged(self) -> None:
        self.npmrc().write_text(NPMRC_USER)
        result = self.run_settings(github_runner_host_tools_npm_settings={"fund": "a;b"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.npmrc().read_text(), NPMRC_USER)
        self.assertNotIn(NPMRC_TOKEN, self.squashed(result))

    def test_a_host_the_role_does_not_support_gets_no_package_install_settings(self) -> None:
        result = self.run_settings(system="FreeBSD", os_family="FreeBSD", github_runner_host_tools_npm_settings=NPM_SETTINGS, github_runner_host_tools_pnpm_settings={"saveExact": True})
        self.ok(result)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_pnpm_settings_make_a_new_config_file_at_the_macos_path(self) -> None:
        self.ok(self.run_settings(github_runner_host_tools_pnpm_settings=PNPM_SETTINGS, github_runner_host_tools_pnpm_minimum_release_age_exclude=["example-a", "@example/*"]))
        self.assertEqual(self.pnpm_config().read_text(), "minimumReleaseAge: 10080\nsaveExact: true\nminimumReleaseAgeExclude:\n  - example-a\n  - '@example/*'\n")
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), ["Library"])

    def test_pnpm_settings_make_a_new_config_file_at_the_linux_path(self) -> None:
        self.ok(self.run_settings(system="Linux", os_family="Debian", machine="x86_64", github_runner_host_tools_pnpm_settings=PNPM_SETTINGS))
        self.assertEqual(self.pnpm_config("Linux").read_text(), "minimumReleaseAge: 10080\nsaveExact: true\n")
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [".config"])

    def test_pnpm_settings_are_merged_keeping_the_other_keys_and_comments_and_the_mode_and_a_second_run_changes_nothing(self) -> None:
        self.pnpm_config().parent.mkdir(parents=True)
        self.pnpm_config().write_text(PNPM_USER)
        self.pnpm_config().chmod(0o640)
        variables = {"github_runner_host_tools_pnpm_settings": PNPM_SETTINGS, "github_runner_host_tools_pnpm_minimum_release_age_exclude": ["example-a"]}
        self.ok(self.run_settings(**variables))
        self.assertEqual(self.pnpm_config().read_text(), PNPM_MERGED)
        self.assertEqual(self.mode(self.pnpm_config()), 0o640)
        result = self.run_settings(**variables)
        self.ok(result)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual(self.pnpm_config().read_text(), PNPM_MERGED)

    def test_a_pnpm_config_that_holds_the_values_already_is_not_rewritten(self) -> None:
        self.pnpm_config().parent.mkdir(parents=True)
        self.pnpm_config().write_text("# mine\nminimumReleaseAge:    10080\nsaveExact: yes\n")
        before = self.pnpm_config().stat().st_mtime_ns
        result = self.run_settings(github_runner_host_tools_pnpm_settings={"minimumReleaseAge": 10080})
        self.ok(result)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual(self.pnpm_config().stat().st_mtime_ns, before)

    def test_a_symlinked_pnpm_config_stays_a_link_and_its_target_is_edited(self) -> None:
        target = self.work / "dotfiles-pnpm.yaml"
        target.write_text(PNPM_USER)
        self.pnpm_config().parent.mkdir(parents=True)
        self.pnpm_config().symlink_to(target)
        self.ok(self.run_settings(github_runner_host_tools_pnpm_settings=PNPM_SETTINGS))
        self.assertTrue(self.pnpm_config().is_symlink())
        self.assertIn("minimumReleaseAge: 10080\n", target.read_text())
        self.assertIn("# Mine.\n", target.read_text())

    def test_the_pnpm_exclusion_list_given_twice_is_refused_before_the_file_is_changed(self) -> None:
        result = self.run_settings(github_runner_host_tools_pnpm_settings={"minimumReleaseAgeExclude": ["a"]}, github_runner_host_tools_pnpm_minimum_release_age_exclude=["b"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Use one of them", self.squashed(result))
        self.assertEqual(list(self.home.iterdir()), [])

    def test_a_pnpm_config_that_is_not_yaml_is_refused_and_left_as_it_is(self) -> None:
        self.pnpm_config().parent.mkdir(parents=True)
        self.pnpm_config().write_text("a: [1\n")
        result = self.run_settings(github_runner_host_tools_pnpm_settings=PNPM_SETTINGS)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not valid YAML", self.squashed(result))
        self.assertEqual(self.pnpm_config().read_text(), "a: [1\n")

    def test_check_mode_reports_the_pnpm_settings_and_writes_nothing(self) -> None:
        result = self.run_settings("--check", "--diff", github_runner_host_tools_pnpm_settings=PNPM_SETTINGS)
        self.ok(result)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertIn("config.yaml would be written", self.squashed(result))
        self.assertGreaterEqual(self.recap(result)["changed"], 1, result.stdout)
        self.pnpm_config().parent.mkdir(parents=True)
        self.pnpm_config().write_text(PNPM_USER)
        result = self.run_settings("--check", github_runner_host_tools_pnpm_settings=PNPM_SETTINGS)
        self.ok(result)
        self.assertEqual(self.pnpm_config().read_text(), PNPM_USER)
        self.assertGreaterEqual(self.recap(result)["changed"], 1, result.stdout)

    def test_the_npm_and_pnpm_settings_do_not_need_homebrew(self) -> None:
        result = self.run_settings(github_runner_host_tools_npm_settings=NPM_SETTINGS, github_runner_host_tools_pnpm_settings=PNPM_SETTINGS)
        self.ok(result)
        self.assertEqual(self.logged(), [])
        self.assertTrue(self.npmrc().exists())
        self.assertTrue(self.pnpm_config().exists())



if __name__ == "__main__":
    unittest.main()
