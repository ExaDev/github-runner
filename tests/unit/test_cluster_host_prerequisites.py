"""Tests the cluster role's host preparation for k3s (tasks/host_prerequisites.yml): its defaults, its wiring into both node modes, and its two steps (tests/cluster_host_prerequisites/run.yml) against stand-ins for modprobe, sysctl, apparmor_parser and sudo on PATH, with every file the steps write or read redirected, so that no real module, sysctl or profile is touched."""

from __future__ import annotations

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
ROLE = ROOT / "roles" / "github_runner_cluster"
RUN = ROOT / "tests" / "cluster_host_prerequisites" / "run.yml"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
README = (ROLE / "README.md").read_text()
STEPS = [
    "github_runner_cluster_k3s_prerequisites",
    "github_runner_cluster_containerd_apparmor_stacking_fix",
]
SYSCTLS = {"net.ipv4.ip_forward": "1", "net.bridge.bridge-nf-call-iptables": "1", "net.bridge.bridge-nf-call-ip6tables": "1"}


class DefaultsTest(unittest.TestCase):
    def test_every_step_is_off_by_default_and_has_a_variable_of_its_own(self) -> None:
        for step in STEPS:
            with self.subTest(step=step):
                self.assertIs(DEFAULTS[step], False)
        tasks = yaml.safe_load((ROLE / "tasks" / "host_prerequisites.yml").read_text())
        gated = [next(condition for condition in task["when"] if "| bool" in condition).split(" ")[0] for task in tasks]
        self.assertEqual(gated, STEPS)
        for task in tasks:
            self.assertIn("ansible_facts['system'] == 'Linux'", task["when"])

    def test_the_k3s_prerequisites_are_the_modules_and_sysctls_k3s_needs(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_kernel_modules"], ["overlay", "br_netfilter"])
        self.assertEqual({key: str(value) for key, value in DEFAULTS["github_runner_cluster_sysctl_settings"].items()}, SYSCTLS)

    def test_each_switch_is_described_in_the_readme(self) -> None:
        for step in STEPS:
            with self.subTest(step=step):
                self.assertIn(f"`{step}`", README)


class WiringTest(unittest.TestCase):
    """Both node modes prepare the host once, before the node is started or changed."""

    def names(self, path: str) -> list[str]:
        return [task["name"] for task in yaml.safe_load((ROLE / "tasks" / path).read_text())]

    def includes(self, path: str) -> list[str]:
        return [task.get("ansible.builtin.include_tasks") for task in yaml.safe_load((ROLE / "tasks" / path).read_text())]

    def test_a_docker_node_prepares_the_host_after_the_platform_is_read_and_before_the_runtime_is_touched(self) -> None:
        includes = self.includes("main.yml")
        self.assertEqual(includes.count("host_prerequisites.yml"), 1)
        self.assertLess(includes.index("host_os.yml"), includes.index("host_prerequisites.yml"))
        self.assertLess(includes.index("host_prerequisites.yml"), includes.index("runtime.yml"))

    def test_a_native_node_prepares_the_host_after_the_k3s_binary_is_installed_and_before_its_unit_is_written(self) -> None:
        includes = self.includes("native/main.yml")
        self.assertEqual(includes.count("host_prerequisites.yml"), 1)
        self.assertLess(includes.index("native/k3s_binary.yml"), includes.index("host_prerequisites.yml"))
        self.assertLess(includes.index("host_prerequisites.yml"), includes.index("native/config.yml"))
        self.assertLess(includes.index("host_prerequisites.yml"), includes.index("native/service.yml"))


def stand_in(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)



class Harness(unittest.TestCase):
    """Runs the steps against stand-ins; the tests are in K3sPrerequisitesTest and ContainerdAppArmorTest."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cluster-host-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stubs = self.tmp / "stubs"
        self.state = self.stubs / "state"
        self.state.mkdir(parents=True)
        self.bin = self.stubs / "bin"
        self.bin.mkdir()
        self.host = self.tmp / "host"
        self.modules_file = self.host / "etc" / "modules-load.d" / "github-runner-k3s.conf"
        self.sysctl_file = self.host / "etc" / "sysctl.d" / "90-github-runner-k3s.conf"
        self.sys_module = self.host / "sys" / "module"
        self.apparmor_enabled = self.host / "sys" / "module" / "apparmor" / "parameters" / "enabled"
        self.apparmor_profiles = self.host / "sys" / "kernel" / "security" / "apparmor" / "profiles"
        self.apparmor_profile = self.host / "etc" / "apparmor.d" / "cri-containerd.apparmor.d"
        self.k3s_binary = self.host / "usr" / "local" / "bin" / "k3s"
        self.k3s_data = self.host / "var" / "lib" / "rancher" / "k3s"
        # Directories every host has already: the steps put files in them and create no others of the system's.
        for path in (self.sys_module, self.modules_file.parent, self.sysctl_file.parent):
            path.mkdir(parents=True, exist_ok=True)
        self.make_stand_ins()

    def make_stand_ins(self) -> None:
        log = 'echo "$(basename "$0") $*" >> "$STUB/calls.log"\n'
        bin_dir = self.bin
        stand_in(bin_dir / "modprobe", log + r"""touch "$STUB/sys-module-$2"
mkdir -p "$SYS_MODULE" && touch "$SYS_MODULE/$2"
""")
        stand_in(bin_dir / "sysctl", log + r"""dir="$STUB/state/sysctl"
mkdir -p "$dir"
case "$1" in
  -n) [ -e "$dir/$2" ] || { echo "sysctl: cannot stat /proc/sys/$2: No such file or directory" >&2; exit 255; }; cat "$dir/$2" ;;
  -w)
    key=${2%%=*}; value=${2#*=}
    case "$key" in net.bridge.*) [ -e "$SYS_MODULE/br_netfilter" ] || { echo "sysctl: cannot stat /proc/sys/$key" >&2; exit 255; } ;; esac
    printf '%s\n' "$value" > "$dir/$key" ;;
esac
""")
        # Stands in for the sudo that become runs, so that a test can see which tasks asked for privilege: it logs, then runs the command it was given as the connecting user.
        stand_in(bin_dir / "sudo", log + r"""while [ "$1" != /bin/sh ]; do shift; done
exec "$@"
""")
        # Only what the steps call: the version read, the compile without loading that validates the file, and the replace that loads it, which lists the profile the file declares among the loaded ones as the kernel does.
        stand_in(bin_dir / "apparmor_parser", log + r"""[ -e "$STUB/state/no-apparmor-parser" ] && { echo "apparmor_parser: command not found" >&2; exit 127; }
case "$1" in
  --version) echo "AppArmor parser version 5.0.2" ;;
  --skip-kernel-load) shift 2; [ -e "$STUB/state/profile-rejected" ] && { echo "AppArmor parser error for $1: syntax error" >&2; exit 1; }; true ;;
  --replace)
    name=$(sed -n 's/^profile \([^ ]*\) .*/\1/p' "$2")
    awk -v name="$name" '$1 == name { found = 1 } END { exit !found }' "$APPARMOR_PROFILES" 2>/dev/null || printf '%s (enforce)\n' "$name" >> "$APPARMOR_PROFILES" ;;
esac
""")

    def run_role(self, *args: str, check: bool = False, system: str = "Linux", **variables: Any) -> subprocess.CompletedProcess[str]:
        inventory = self.tmp / "inventory.yml"
        host = {
            "ansible_connection": "local",
            "ansible_become": False,
            "github_runner_cluster_modules_load_file": str(self.modules_file),
            "github_runner_cluster_sysctl_file": str(self.sysctl_file),
            "github_runner_cluster_sys_module_dir": str(self.sys_module),
            "github_runner_cluster_apparmor_enabled_file": str(self.apparmor_enabled),
            "github_runner_cluster_apparmor_profiles_file": str(self.apparmor_profiles),
            "github_runner_cluster_containerd_apparmor_profile_file": str(self.apparmor_profile),
            "github_runner_cluster_k3s_binary": str(self.k3s_binary),
            "github_runner_cluster_native_data_dir": str(self.k3s_data),
            **variables,
        }
        # A variable given as None is left out: ansible_become is False above so that no test needs sudo, and a test that watches which tasks ask for it passes None for it.
        inventory.write_text(yaml.safe_dump({"all": {"hosts": {"host": {name: value for name, value in host.items() if value is not None}}}}))
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "STUB": str(self.stubs),
            "SYS_MODULE": str(self.sys_module),
            "APPARMOR_PROFILES": str(self.apparmor_profiles),
            "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"),
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_LOCALHOST_WARNING": "false",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
            "ANSIBLE_RETRY_FILES_ENABLED": "false",
        }
        command = [ANSIBLE_PLAYBOOK, "-i", str(inventory), str(RUN), "-e", json.dumps({"host_system": system}), *args]
        if check:
            command.append("--check")
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=300, check=False, cwd=ROOT / "ansible")

    def calls(self) -> list[str]:
        log = self.stubs / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def changes(self) -> list[str]:
        return [call for call in self.calls() if re.match(r"^(modprobe|sysctl -w|apparmor_parser --replace)", call)]

    def changed(self, result: subprocess.CompletedProcess[str]) -> int:
        return int(re.search(r"changed=(\d+)", result.stdout).group(1))  # type: ignore[union-attr]


class K3sPrerequisitesTest(Harness):
    def test_the_k3s_prerequisites_are_loaded_and_set_now_with_the_modules_first_and_written_for_boot(self) -> None:
        result = self.run_role(github_runner_cluster_k3s_prerequisites=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.modules_file.read_text().split("\n")[1:3], ["overlay", "br_netfilter"])
        sysctl_lines = [line for line in self.sysctl_file.read_text().splitlines() if not line.startswith("#")]
        self.assertEqual(sysctl_lines, [f"{key} = {value}" for key, value in SYSCTLS.items()])
        self.assertEqual(self.changes(), ["modprobe -- overlay", "modprobe -- br_netfilter"] + [f"sysctl -w {key}={value}" for key, value in SYSCTLS.items()])
        for key, value in SYSCTLS.items():
            self.assertEqual((self.state / "sysctl" / key).read_text().strip(), value)

    def test_a_second_run_of_the_k3s_prerequisites_changes_nothing(self) -> None:
        self.assertEqual(self.run_role(github_runner_cluster_k3s_prerequisites=True).returncode, 0)
        before = len(self.calls())
        result = self.run_role(github_runner_cluster_k3s_prerequisites=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changed(result), 0, result.stdout)
        self.assertEqual([call for call in self.calls()[before:] if re.match(r"^(modprobe|sysctl -w)", call)], [])

    def test_only_the_modules_and_sysctls_that_need_it_are_loaded_and_set(self) -> None:
        (self.sys_module / "overlay").touch()
        (self.state / "sysctl").mkdir()
        (self.state / "sysctl" / "net.ipv4.ip_forward").write_text("1\n")
        self.assertEqual(self.run_role(github_runner_cluster_k3s_prerequisites=True).returncode, 0)
        self.assertEqual(self.changes(), ["modprobe -- br_netfilter", "sysctl -w net.bridge.bridge-nf-call-iptables=1", "sysctl -w net.bridge.bridge-nf-call-ip6tables=1"])

    def test_a_check_run_of_the_k3s_prerequisites_reports_each_change_and_makes_none(self) -> None:
        result = self.run_role(check=True, github_runner_cluster_k3s_prerequisites=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertIn("modprobe br_netfilter", result.stdout)
        self.assertIn("sysctl -w net.bridge.bridge-nf-call-iptables=1", result.stdout)


class ContainerdAppArmorTest(Harness):
    """The AppArmor step: the profile that lets a container's processes signal each other under a stacked label, installed and loaded once, and refused for a k3s whose containerd it was not taken from."""

    PROFILE_NAME = "cri-containerd.apparmor.d"
    RELEASE = "v1.34.1+k3s1"
    CONTAINERD = "v2.1.4-k3s2"
    APPLY = {"github_runner_cluster_containerd_apparmor_stacking_fix": True}

    def apparmor_host(self, *, enabled: str | None = "Y", loaded: bool = False, k3s: bool = True, release: str = RELEASE, unpacked: bool = True, unpacked_release: str | None = None, containerd: str = CONTAINERD) -> None:
        """A host the way Ubuntu has it: AppArmor's module parameter and securityfs list, the cluster role's k3s binary, and the binary k3s unpacked into <data dir>/data/<hash>/bin/k3s with data/current a symlink to <hash>, as k3s makes it."""
        if enabled is not None:
            self.apparmor_enabled.parent.mkdir(parents=True, exist_ok=True)
            self.apparmor_enabled.write_text(enabled + "\n")
        self.apparmor_profiles.parent.mkdir(parents=True, exist_ok=True)
        # Decoys: a profile whose name starts with the profile's, and one that differs from it only where its name has a dot, among profiles that are not it.
        lines = ["docker-default (enforce)", f"{self.PROFILE_NAME}.old (enforce)", self.PROFILE_NAME.replace(".", "-") + " (enforce)", "unconfined"]
        self.apparmor_profiles.write_text("\n".join(lines + ([f"{self.PROFILE_NAME} (enforce)"] if loaded else [])) + "\n")
        self.apparmor_profile.parent.mkdir(parents=True, exist_ok=True)
        if k3s:
            (self.state / "k3s-release").write_text(release)
            self.k3s_binary.parent.mkdir(parents=True, exist_ok=True)
            stand_in(self.k3s_binary, 'echo "k3s version $(cat "$STUB/state/k3s-release") (24fc436e)"\necho "go version go1.24.6"\n')
        if unpacked:
            self.unpack("0123abcd", unpacked_release or release, containerd, current=True)

    def unpack(self, name: str, release: str, containerd: str, *, current: bool) -> None:
        """Unpacks a multicall binary as k3s does: the linker flags it was built with sit in a binary's worth of other bytes."""
        binary = self.k3s_data / "data" / name / "bin" / "k3s"
        binary.parent.mkdir(parents=True, exist_ok=True)
        flags = f"-ldflags=\" -X github.com/k3s-io/k3s/pkg/version.Version={release}  -X github.com/containerd/containerd/v2/version.Version={containerd}  -X github.com/containerd/containerd/v2/version.Package=github.com/k3s-io/containerd/v2 -w -s\""
        binary.write_bytes(b"\x7fELF\x00\x01\x02\xff" * 64 + b"build\t" + flags.encode() + b"\n" + b"\x00\xfe" * 64)
        if current:
            link = self.k3s_data / "data" / "current"
            link.unlink(missing_ok=True)
            link.symlink_to(binary.parent.parent)

    def replaces(self) -> list[str]:
        return [call for call in self.calls() if call.startswith("apparmor_parser --replace")]

    def apply(self, *, check: bool = False, **variables: Any) -> subprocess.CompletedProcess[str]:
        return self.run_role(check=check, **self.APPLY, **variables)

    def test_the_step_is_off_by_default_and_touches_nothing(self) -> None:
        self.apparmor_host()
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.apparmor_profile.exists())

    def test_the_profile_is_validated_installed_and_loaded(self) -> None:
        self.apparmor_host()
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(stat.S_IMODE(self.apparmor_profile.stat().st_mode), 0o644)
        profile = self.apparmor_profile.read_text()
        for rule in (
            "signal (send,receive) peer=cri-containerd.apparmor.d,",
            "signal (send,receive) peer=cri-containerd.apparmor.d//&*,",
            "ptrace (trace,tracedby,read,readby) peer=cri-containerd.apparmor.d,",
            "ptrace (trace,tracedby,read,readby) peer=cri-containerd.apparmor.d//&*,",
        ):
            self.assertIn(f"\n  {rule}\n", profile)
        self.assertIn("profile cri-containerd.apparmor.d flags=(attach_disconnected,mediate_deleted) {\n", profile)
        self.assertIn(f"containerd {self.CONTAINERD}'s default CRI profile", profile)
        self.assertNotIn("{{", profile)
        self.assertEqual(self.changes(), [f"apparmor_parser --replace {self.apparmor_profile}"])
        validations = [call for call in self.calls() if call.startswith("apparmor_parser --skip-kernel-load --skip-cache ")]
        self.assertEqual(len(validations), 1)
        self.assertNotIn(str(self.apparmor_profile), validations[0], "the file is compiled before it replaces the profile, not after")
        self.assertLess(self.calls().index(validations[0]), self.calls().index(self.replaces()[0]))
        self.assertIn(f"\n{self.PROFILE_NAME} (enforce)\n", self.apparmor_profiles.read_text())

    def test_a_second_run_changes_nothing_and_loads_nothing(self) -> None:
        self.apparmor_host()
        self.assertEqual(self.apply().returncode, 0)
        before = len(self.calls())
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changed(result), 0, result.stdout)
        self.assertEqual([call for call in self.calls()[before:] if re.match(r"^apparmor_parser (--replace|--skip)", call)], [])

    def test_a_changed_file_is_put_back_and_loaded_again(self) -> None:
        self.apparmor_host()
        self.assertEqual(self.apply().returncode, 0)
        installed = self.apparmor_profile.read_text()
        self.apparmor_profile.write_text(installed.replace("  ptrace (trace,tracedby,read,readby) peer=cri-containerd.apparmor.d//&*,\n", ""))
        before = len(self.replaces())
        self.assertEqual(self.apply().returncode, 0)
        self.assertEqual(self.apparmor_profile.read_text(), installed)
        self.assertEqual(len(self.replaces()), before + 1)

    def test_a_profile_that_is_not_loaded_is_loaded_once_though_its_file_is_there(self) -> None:
        self.apparmor_host()
        self.assertEqual(self.apply().returncode, 0)
        # Only profiles that are not it, though their names look like it, are listed now.
        self.apparmor_profiles.write_text(f"docker-default (enforce)\n{self.PROFILE_NAME}.old (enforce)\n{self.PROFILE_NAME.replace('.', '-')} (enforce)\n")
        before = len(self.replaces())
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.replaces()), before + 1)
        self.assertIn(f"\n{self.PROFILE_NAME} (enforce)\n", self.apparmor_profiles.read_text())
        self.assertEqual(self.changed(self.apply()), 0)

    def test_a_check_run_reports_the_install_and_the_load_and_makes_none(self) -> None:
        self.apparmor_host()
        result = self.apply(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertFalse(self.apparmor_profile.exists())
        self.assertIn(f"apparmor_parser --replace {self.apparmor_profile}", result.stdout)
        self.assertEqual(self.apparmor_profiles.read_text().count(f"{self.PROFILE_NAME} "), 0)

    def test_a_check_run_of_a_host_that_has_the_profile_reports_nothing_to_do(self) -> None:
        self.apparmor_host()
        self.assertEqual(self.apply().returncode, 0)
        before = len(self.calls())
        result = self.apply(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changed(result), 0, result.stdout)
        self.assertEqual([call for call in self.calls()[before:] if call.startswith("apparmor_parser --replace")], [])

    def test_a_host_without_apparmor_is_left_alone_and_says_so(self) -> None:
        for enabled in (None, "N"):
            with self.subTest(enabled=enabled):
                self.apparmor_host(enabled=enabled)
                if enabled is None:
                    self.apparmor_enabled.unlink(missing_ok=True)
                result = self.apply()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("AppArmor is not enabled in its kernel", re.sub(r"\s+", " ", result.stdout))
                self.assertEqual(self.changes(), [])
                self.assertFalse(self.apparmor_profile.exists())

    def test_a_host_that_is_not_linux_is_left_alone(self) -> None:
        self.apparmor_host()
        result = self.apply(system="Darwin")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_k3s_with_another_containerd_is_refused_before_anything_is_written(self) -> None:
        self.apparmor_host(containerd="v2.1.5-k3s1")
        result = self.apply()
        self.assertNotEqual(result.returncode, 0)
        message = re.sub(r"\s+", " ", result.stdout)
        self.assertIn(f"k3s {self.RELEASE} on host carries containerd v2.1.5-k3s1", message)
        self.assertIn(f"taken from containerd {self.CONTAINERD}", message)
        self.assertEqual(self.changes(), [])
        self.assertFalse(self.apparmor_profile.exists())

    def test_a_k3s_with_another_containerd_is_refused_when_the_profile_is_already_installed(self) -> None:
        self.apparmor_host()
        self.assertEqual(self.apply().returncode, 0)
        self.unpack("4567ef01", "v1.34.3+k3s1", "v2.1.5-k3s1", current=True)
        (self.state / "k3s-release").write_text("v1.34.3+k3s1")
        result = self.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("carries containerd v2.1.5-k3s1", re.sub(r"\s+", " ", result.stdout))

    def test_the_containerd_is_read_from_the_binary_current_points_at_and_not_from_an_older_one(self) -> None:
        self.apparmor_host()
        self.unpack("00old000", "v1.33.0+k3s1", "v2.0.0-k3s1", current=False)
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.apparmor_profile.is_file())

    def test_the_containerd_is_not_checked_against_a_binary_of_another_release_than_the_host_has(self) -> None:
        # The cluster role has just replaced the k3s binary and k3s has not started at the new release, so the binary it unpacked is the previous release's.
        self.apparmor_host(release="v1.34.3+k3s1", unpacked_release=self.RELEASE, containerd="v2.1.5-k3s1")
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        message = re.sub(r"\s+", " ", result.stdout)
        self.assertIn("was not checked", message)
        self.assertIn("k3s v1.34.3+k3s1 has not started on this host yet", message)
        self.assertTrue(self.apparmor_profile.is_file())

    def test_a_host_without_k3s_gets_the_profile_and_is_told_the_containerd_was_not_checked(self) -> None:
        self.apparmor_host(k3s=False, unpacked=False)
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("k3s is not installed yet", re.sub(r"\s+", " ", result.stdout))
        self.assertTrue(self.apparmor_profile.is_file())

    def test_a_k3s_that_has_not_unpacked_yet_is_not_refused(self) -> None:
        self.apparmor_host(unpacked=False)
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("has not started on this host yet (the binary it unpacked is missing)", re.sub(r"\s+", " ", result.stdout))

    def test_a_binary_that_does_not_say_its_containerd_is_refused_rather_than_trusted(self) -> None:
        self.apparmor_host()
        (self.k3s_data / "data" / "current" / "bin" / "k3s").write_bytes(b"\x7fELF no linker flags in here\n")
        result = self.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not carry the k3s release or the containerd version", re.sub(r"\s+", " ", result.stdout))
        self.assertEqual(self.changes(), [])
        self.assertFalse(self.apparmor_profile.exists())

    def test_a_host_whose_parser_cannot_be_run_is_refused_before_anything_is_written(self) -> None:
        self.apparmor_host()
        (self.state / "no-apparmor-parser").touch()
        result = self.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("apparmor_parser cannot be run", re.sub(r"\s+", " ", result.stdout))
        self.assertFalse(self.apparmor_profile.exists())

    def test_a_profile_the_parser_rejects_never_replaces_the_file_or_is_loaded(self) -> None:
        self.apparmor_host()
        self.assertEqual(self.apply().returncode, 0)
        installed = self.apparmor_profile.read_text()
        self.apparmor_profile.write_text("# changed by hand\n")
        (self.state / "profile-rejected").touch()
        before = len(self.replaces())
        result = self.apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.apparmor_profile.read_text(), "# changed by hand\n")
        self.assertNotEqual(installed, "# changed by hand\n")
        self.assertEqual(len(self.replaces()), before)

    def test_a_host_without_apparmor_asks_for_no_privilege(self) -> None:
        self.apparmor_host(enabled="N")
        result = self.apply(ansible_become=None, ansible_become_exe=str(self.bin / "sudo"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
