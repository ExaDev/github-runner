"""Tests the github_runner_linux_host role: its defaults, argument specs and README, and its tasks (tests/linux_host/run.yml) against stand-ins for systemctl, nmcli, iw, modprobe, sysctl, apt-get, dpkg-query, uname and shutdown on PATH, with every file the role writes or reads redirected, so that no real target, drop-in, radio, module, package or reboot is touched."""

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
ROLE = ROOT / "roles" / "github_runner_linux_host"
RUN = ROOT / "tests" / "linux_host" / "run.yml"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
SPECS: dict[str, Any] = yaml.safe_load((ROLE / "meta" / "argument_specs.yml").read_text())["argument_specs"]["main"]["options"]
README = (ROLE / "README.md").read_text()
TARGETS = ["sleep.target", "suspend.target", "hibernate.target", "hybrid-sleep.target"]
STEPS = [
    "github_runner_linux_host_keep_awake",
    "github_runner_linux_host_wifi_powersave_off",
    "github_runner_linux_host_k3s_prerequisites",
    "github_runner_linux_host_battery_charge_limit",
    "github_runner_linux_host_headless",
    "github_runner_linux_host_disable_unneeded_services",
    "github_runner_linux_host_unattended_upgrades",
    "github_runner_linux_host_upgrade",
]
SYSCTLS = {"net.ipv4.ip_forward": "1", "net.bridge.bridge-nf-call-iptables": "1", "net.bridge.bridge-nf-call-ip6tables": "1"}


class DefaultsTest(unittest.TestCase):
    def test_every_step_is_off_by_default_and_has_a_variable_of_its_own(self) -> None:
        for step in STEPS:
            with self.subTest(step=step):
                self.assertIs(DEFAULTS[step], False)
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        gated = [next(condition for condition in task["when"] if "| bool" in condition).split(" ")[0] for task in tasks if "when" in task and isinstance(task["when"], list)]
        self.assertEqual(gated, STEPS)

    def test_the_k3s_prerequisites_are_the_modules_and_sysctls_k3s_needs(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_linux_host_kernel_modules"], ["overlay", "br_netfilter"])
        self.assertEqual({key: str(value) for key, value in DEFAULTS["github_runner_linux_host_sysctl_settings"].items()}, SYSCTLS)

    def test_the_battery_is_limited_to_eighty_percent_of_bat0_unless_told_otherwise(self) -> None:
        self.assertEqual((DEFAULTS["github_runner_linux_host_battery_charge_limit_percent"], DEFAULTS["github_runner_linux_host_battery_device"]), (80, "BAT0"))

    def test_the_services_a_dedicated_node_does_not_need_are_listed_and_the_ones_it_does_are_not(self) -> None:
        services = DEFAULTS["github_runner_linux_host_unneeded_services"]
        self.assertEqual({service.split(".")[0] for service in services}, {"cups", "cups-browsed", "bluetooth", "ModemManager"})
        self.assertLess(services.index("cups.socket"), services.index("cups.service"))
        self.assertLess(services.index("cups.path"), services.index("cups.service"))
        for kept in ("avahi", "NetworkManager"):
            self.assertFalse([service for service in services if kept in service], kept)

    def test_the_upgrade_is_off_by_default_reboots_when_due_and_refuses_a_running_node(self) -> None:
        self.assertIs(DEFAULTS["github_runner_linux_host_upgrade_reboot"], True)
        self.assertIs(DEFAULTS["github_runner_linux_host_upgrade_reboot_running_node"], False)
        self.assertEqual(DEFAULTS["github_runner_linux_host_node_services"], ["k3s-agent.service", "k3s.service"])

    def test_every_sleep_the_host_can_enter_is_masked_and_every_lid_setting_is_set(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_linux_host_sleep_targets"], TARGETS)
        self.assertEqual(DEFAULTS["github_runner_linux_host_lid_switch_settings"], ["HandleLidSwitch", "HandleLidSwitchExternalPower", "HandleLidSwitchDocked"])

    def test_the_display_manager_is_worked_out_from_the_host_unless_a_unit_is_named(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_linux_host_display_manager_unit"], "")
        self.assertEqual(DEFAULTS["github_runner_linux_host_display_manager_candidates"], ["display-manager.service", "gdm.service", "gdm3.service", "lightdm.service", "sddm.service"])

    def test_the_networkmanager_drop_in_sorts_after_ubuntus_default(self) -> None:
        self.assertGreater(Path(DEFAULTS["github_runner_linux_host_networkmanager_dropin"]).name, "default-wifi-powersave-on.conf")

    def test_the_argument_specs_declare_each_default_and_the_readme_describes_each(self) -> None:
        self.assertEqual(set(DEFAULTS), set(SPECS))
        for name, default in DEFAULTS.items():
            with self.subTest(name=name):
                self.assertEqual(SPECS[name]["default"], default)
                self.assertIn(f"`{name}`", README)

    def test_the_site_playbook_runs_the_role_before_the_fleet_play_on_the_cluster_group(self) -> None:
        plays = yaml.safe_load((ROOT / "playbooks" / "site.yml").read_text())
        names = [play["name"] for play in plays]
        host_play = next(play for play in plays if "exadev.github_runner.github_runner_linux_host" in play.get("roles", []))
        self.assertLess(plays.index(host_play), names.index("GitHub Actions runner fleet"))
        self.assertEqual(host_play["hosts"], "{{ github_runner_cluster_group | default('github_runner_cluster') }}")
        self.assertIs(host_play["gather_facts"], False)


def stand_in(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Harness(unittest.TestCase):
    """Runs the role against stand-ins; the tests are in RoleTest and BaselineRoleTest."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="linux-host-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stubs = self.tmp / "stubs"
        self.state = self.stubs / "state"
        self.state.mkdir(parents=True)
        self.bin = self.stubs / "bin"
        self.bin.mkdir()
        self.host = self.tmp / "host"
        self.logind = self.host / "etc" / "systemd" / "logind.conf.d" / "90-github-runner-keep-awake.conf"
        self.networkmanager = self.host / "etc" / "NetworkManager" / "conf.d" / "zz-github-runner-wifi-powersave.conf"
        self.modules_file = self.host / "etc" / "modules-load.d" / "github-runner-k3s.conf"
        self.sysctl_file = self.host / "etc" / "sysctl.d" / "90-github-runner-k3s.conf"
        self.sys_module = self.host / "sys" / "module"
        self.power_supply = self.host / "sys" / "class" / "power_supply"
        self.battery_unit = self.host / "etc" / "systemd" / "system" / "github-runner-battery-charge-limit.service"
        self.apt_file = self.host / "etc" / "apt" / "apt.conf.d" / "52github-runner-unattended-upgrades"
        self.reboot_required = self.host / "var" / "run" / "reboot-required"
        # Directories every host has already: the role puts files in them and creates no others of the system's.
        for path in (self.sys_module, self.power_supply, self.reboot_required.parent, self.battery_unit.parent, self.modules_file.parent, self.sysctl_file.parent, self.apt_file.parent):
            path.mkdir(parents=True, exist_ok=True)
        self.make_stand_ins()
        (self.state / "devices").write_text("wlp2s0:wifi\ntailscale0:tun\nlo:loopback\np2p-dev-wlp2s0:wifi-p2p\n")
        (self.state / "power-save-wlp2s0").touch()

    def make_stand_ins(self) -> None:
        log = 'echo "$(basename "$0") $*" >> "$STUB/calls.log"\n'
        bin_dir = self.bin
        stand_in(bin_dir / "systemctl", log + r"""state="$STUB/state"
unit_state() { if [ -e "$state/masked-$1" ]; then echo masked; elif [ -e "$state/enabled-$1" ]; then cat "$state/enabled-$1"; else echo disabled; fi; }
# alias-NAME holds the unit NAME resolves to, as the symlink an enabled display manager leaves; a unit is loaded when systemd has reason to: it is active, enabled or marked loaded.
resolve() { if [ -e "$state/alias-$1" ]; then cat "$state/alias-$1"; else echo "$1"; fi; }
is_loaded() { [ -e "$state/loaded-$1" ] || [ -e "$state/active-$1" ] || [ -e "$state/enabled-$1" ]; }
case "$1" in
  is-enabled) out=$(unit_state "$2"); echo "$out"; case "$out" in enabled|alias|static) exit 0 ;; *) exit 1 ;; esac ;;
  is-active) if [ -e "$state/active-$2" ]; then echo active; exit 0; fi; echo inactive; exit 3 ;;
  get-default) if [ -e "$state/default-target" ]; then cat "$state/default-target"; else echo graphical.target; fi ;;
  set-default) printf '%s\n' "$2" > "$state/default-target" ;;
  mask) touch "$state/masked-$2" ;;
  enable) printf '%s\n' enabled > "$state/enabled-$2" ;;
  disable)
    if [ -e "$state/alias-$2" ]; then printf '%s\n' disabled > "$state/enabled-$(cat "$state/alias-$2")"; rm -f "$state/alias-$2"
    else printf '%s\n' disabled > "$state/enabled-$2"; for link in "$state"/alias-*; do [ -e "$link" ] && [ "$(cat "$link")" = "$2" ] && rm -f "$link"; done; true; fi ;;
  show)
    for name; do :; done
    id=$(resolve "$name")
    if is_loaded "$id"; then load=loaded; file_state=$(unit_state "$id"); else load=not-found; file_state=""; fi
    if [ -e "$state/active-$id" ]; then active=active; else active=inactive; fi
    printf 'Id=%s\nLoadState=%s\nActiveState=%s\nUnitFileState=%s\n' "$id" "$load" "$active" "$file_state" ;;
  stop)
    id=$(resolve "$2")
    if ! is_loaded "$id"; then echo "Failed to stop $2: Unit $2 not loaded." >&2; exit 5; fi
    rm -f "$state/active-$id" ;;
esac
""")
        stand_in(bin_dir / "nmcli", log + r"""case "$*" in
  *"device status"*) [ -e "$STUB/state/no-networkmanager" ] && { echo "nmcli: not running" >&2; exit 8; }; cat "$STUB/state/devices" ;;
esac
""")
        stand_in(bin_dir / "iw", log + r"""if [ -e "$STUB/state/no-iw" ]; then echo "iw: command not found" >&2; exit 127; fi
device="$2"
case "$3" in
  get) if [ -e "$STUB/state/power-save-$device" ]; then echo "Power save: on"; else echo "Power save: off"; fi ;;
  set) rm -f "$STUB/state/power-save-$device" ;;
esac
""")
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
        stand_in(bin_dir / "apt-get", log + r"""state="$STUB/state"
simulate=no; for argument in "$@"; do [ "$argument" = --simulate ] && simulate=yes; done
case " $* " in
  *" install "*unattended-upgrades*) [ "$simulate" = yes ] || touch "$state/package-unattended-upgrades" ;;
  *" dist-upgrade"*)
    if [ -e "$state/upgradable" ]; then
      echo "3 upgraded, 1 newly installed, 0 to remove and 0 not upgraded."
      if [ "$simulate" = no ]; then
        rm -f "$state/upgradable"
        if [ -e "$state/upgrade-needs-reboot" ]; then touch "$REBOOT_REQUIRED"; fi
      fi
    else echo "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded."; fi ;;
  *" autoremove"*)
    if [ -e "$state/removable" ]; then
      echo "0 upgraded, 0 newly installed, 2 to remove and 0 not upgraded."
      if [ "$simulate" = no ]; then rm -f "$state/removable"; fi
    else echo "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded."; fi ;;
esac
""")
        stand_in(bin_dir / "dpkg-query", r"""case "$*" in
  *unattended-upgrades*) if [ -e "$STUB/state/package-unattended-upgrades" ]; then printf 'ii '; else exit 1; fi ;;
esac
""")
        stand_in(bin_dir / "uname", r"""echo 6.14.0-test
""")
        stand_in(bin_dir / "shutdown", log)

    def run_role(self, *args: str, check: bool = False, system: str = "Linux", os_family: str = "Debian", **variables: Any) -> subprocess.CompletedProcess[str]:
        inventory = self.tmp / "inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"hosts": {"host": {
            "ansible_connection": "local",
            "ansible_become": False,
            "github_runner_linux_host_logind_dropin": str(self.logind),
            "github_runner_linux_host_networkmanager_dropin": str(self.networkmanager),
            "github_runner_linux_host_modules_load_file": str(self.modules_file),
            "github_runner_linux_host_sysctl_file": str(self.sysctl_file),
            "github_runner_linux_host_sys_module_dir": str(self.sys_module),
            "github_runner_linux_host_power_supply_dir": str(self.power_supply),
            "github_runner_linux_host_battery_unit": str(self.battery_unit),
            "github_runner_linux_host_unattended_upgrades_file": str(self.apt_file),
            "github_runner_linux_host_reboot_required_file": str(self.reboot_required),
            "github_runner_linux_host_reboot_timeout": 5,
            **variables,
        }}}}))
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "STUB": str(self.stubs),
            "SYS_MODULE": str(self.sys_module),
            "REBOOT_REQUIRED": str(self.reboot_required),
            "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"),
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_LOCALHOST_WARNING": "false",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
            "ANSIBLE_RETRY_FILES_ENABLED": "false",
        }
        command = [ANSIBLE_PLAYBOOK, "-i", str(inventory), str(RUN), "-e", json.dumps({"host_system": system, "host_os_family": os_family}), *args]
        if check:
            command.append("--check")
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=300, check=False, cwd=ROOT / "ansible")

    def calls(self) -> list[str]:
        log = self.stubs / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def changes(self) -> list[str]:
        return [call for call in self.calls() if re.match(r"^(systemctl (mask|kill|enable|disable|stop|set-default)|nmcli general|iw dev \S+ set|modprobe|sysctl -w|apt-get|shutdown)", call)]

    def changed(self, result: subprocess.CompletedProcess[str]) -> int:
        return int(re.search(r"changed=(\d+)", result.stdout).group(1))  # type: ignore[union-attr]


class RoleTest(Harness):
    def test_with_both_steps_off_the_role_reads_nothing_and_changes_nothing(self) -> None:
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.logind.exists() or self.networkmanager.exists())

    def test_keep_awake_masks_every_sleep_target_and_has_logind_ignore_the_lid(self) -> None:
        result = self.run_role(github_runner_linux_host_keep_awake=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [f"systemctl mask {target}" for target in TARGETS] + ["systemctl kill --signal=HUP --kill-whom=main systemd-logind.service"])
        text = self.logind.read_text()
        self.assertIn("[Login]\n", text)
        for setting in ("HandleLidSwitch", "HandleLidSwitchExternalPower", "HandleLidSwitchDocked"):
            self.assertIn(f"\n{setting}=ignore\n", text)
        self.assertEqual(stat.S_IMODE(self.logind.stat().st_mode), 0o644)
        self.assertFalse(self.networkmanager.exists())

    def test_a_second_run_changes_nothing_and_does_not_reload_logind(self) -> None:
        self.assertEqual(self.run_role(github_runner_linux_host_keep_awake=True).returncode, 0)
        before = len(self.calls())
        result = self.run_role(github_runner_linux_host_keep_awake=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changed(result), 0, result.stdout)
        self.assertEqual([call for call in self.calls()[before:] if "is-enabled" not in call], [])

    def test_only_the_targets_that_are_not_masked_are_masked(self) -> None:
        (self.state / "masked-suspend.target").touch()
        (self.state / "masked-sleep.target").touch()
        self.assertEqual(self.run_role(github_runner_linux_host_keep_awake=True).returncode, 0)
        self.assertEqual([call for call in self.changes() if "mask " in call], ["systemctl mask hibernate.target", "systemctl mask hybrid-sleep.target"])

    def test_a_check_run_reports_each_change_and_makes_none(self) -> None:
        result = self.run_role(check=True, github_runner_linux_host_keep_awake=True, github_runner_linux_host_wifi_powersave_off=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertFalse(self.logind.exists() or self.networkmanager.exists())
        for target in TARGETS:
            self.assertIn(f"systemctl mask {target}", result.stdout)
        self.assertIn("iw dev wlp2s0 set power_save off", result.stdout)

    def test_wifi_power_save_is_disabled_in_networkmanager_and_on_the_radio(self) -> None:
        result = self.run_role(github_runner_linux_host_wifi_powersave_off=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[connection]\nwifi.powersave = 2\n", self.networkmanager.read_text())
        self.assertEqual(self.changes(), ["nmcli general reload conf", "iw dev wlp2s0 set power_save off"])
        self.assertFalse((self.state / "power-save-wlp2s0").exists())
        self.assertFalse(self.logind.exists())

    def test_a_second_wifi_run_changes_nothing(self) -> None:
        self.assertEqual(self.run_role(github_runner_linux_host_wifi_powersave_off=True).returncode, 0)
        before = len(self.calls())
        result = self.run_role(github_runner_linux_host_wifi_powersave_off=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changed(result), 0, result.stdout)
        self.assertEqual([call for call in self.calls()[before:] if re.match(r"^(nmcli general|iw dev \S+ set)", call)], [])

    def test_a_host_without_a_wifi_device_is_left_alone(self) -> None:
        (self.state / "devices").write_text("enp3s0:ethernet\nlo:loopback\n")
        result = self.run_role(github_runner_linux_host_wifi_powersave_off=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.networkmanager.exists())
        self.assertEqual(self.changes(), [])

    def test_a_host_without_networkmanager_is_refused_since_the_setting_was_asked_for(self) -> None:
        (self.state / "no-networkmanager").touch()
        result = self.run_role(github_runner_linux_host_wifi_powersave_off=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nmcli did not answer", re.sub(r"\s+", " ", result.stdout))
        self.assertFalse(self.networkmanager.exists())

    def test_a_host_without_iw_gets_the_drop_in_and_is_told_the_radio_was_not_changed(self) -> None:
        (self.state / "no-iw").touch()
        result = self.run_role(github_runner_linux_host_wifi_powersave_off=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.networkmanager.is_file())
        self.assertIn("the drop-in applies when NetworkManager next activates its connection", re.sub(r"\s+", " ", result.stdout))

    def test_a_host_that_is_not_linux_is_left_alone(self) -> None:
        result = self.run_role(system="Darwin", github_runner_linux_host_keep_awake=True, github_runner_linux_host_wifi_powersave_off=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.logind.exists() or self.networkmanager.exists())

    def test_a_value_of_the_wrong_type_is_refused_by_the_argument_specs(self) -> None:
        result = self.run_role(github_runner_linux_host_keep_awake="sometimes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("github_runner_linux_host_keep_awake", result.stdout)
        self.assertEqual(self.calls(), [])


class BaselineRoleTest(Harness):
    """The opt-in steps beyond keeping the host awake and its Wi-Fi from power saving."""

    def enable(self, name: str, state: str = "enabled", active: bool = False) -> None:
        (self.state / f"enabled-{name}").write_text(state + "\n")
        if active:
            (self.state / f"active-{name}").touch()

    def display_manager(self, unit: str, alias: str | None = "display-manager.service", active: bool = True, state: str = "enabled") -> None:
        """Installs a display manager as systemd leaves it: the unit itself, and the alias link of an enabled one."""
        self.enable(unit, state, active=active)
        if alias is not None:
            (self.state / f"alias-{alias}").write_text(unit + "\n")

    def stopped_and_disabled(self, unit: str) -> bool:
        return not (self.state / f"active-{unit}").exists() and (self.state / f"enabled-{unit}").read_text().strip() == "disabled"

    # k3s prerequisites

    def test_the_k3s_prerequisites_are_loaded_and_set_now_with_the_modules_first_and_written_for_boot(self) -> None:
        result = self.run_role(github_runner_linux_host_k3s_prerequisites=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.modules_file.read_text().split("\n")[1:3], ["overlay", "br_netfilter"])
        sysctl_lines = [line for line in self.sysctl_file.read_text().splitlines() if not line.startswith("#")]
        self.assertEqual(sysctl_lines, [f"{key} = {value}" for key, value in SYSCTLS.items()])
        self.assertEqual(self.changes(), ["modprobe -- overlay", "modprobe -- br_netfilter"] + [f"sysctl -w {key}={value}" for key, value in SYSCTLS.items()])
        for key, value in SYSCTLS.items():
            self.assertEqual((self.state / "sysctl" / key).read_text().strip(), value)

    def test_a_second_run_of_the_k3s_prerequisites_changes_nothing(self) -> None:
        self.assertEqual(self.run_role(github_runner_linux_host_k3s_prerequisites=True).returncode, 0)
        before = len(self.calls())
        result = self.run_role(github_runner_linux_host_k3s_prerequisites=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changed(result), 0, result.stdout)
        self.assertEqual([call for call in self.calls()[before:] if re.match(r"^(modprobe|sysctl -w)", call)], [])

    def test_only_the_modules_and_sysctls_that_need_it_are_loaded_and_set(self) -> None:
        (self.sys_module / "overlay").touch()
        (self.state / "sysctl").mkdir()
        (self.state / "sysctl" / "net.ipv4.ip_forward").write_text("1\n")
        self.assertEqual(self.run_role(github_runner_linux_host_k3s_prerequisites=True).returncode, 0)
        self.assertEqual(self.changes(), ["modprobe -- br_netfilter", "sysctl -w net.bridge.bridge-nf-call-iptables=1", "sysctl -w net.bridge.bridge-nf-call-ip6tables=1"])

    def test_a_check_run_of_the_k3s_prerequisites_reports_each_change_and_makes_none(self) -> None:
        result = self.run_role(check=True, github_runner_linux_host_k3s_prerequisites=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertIn("modprobe br_netfilter", result.stdout)
        self.assertIn("sysctl -w net.bridge.bridge-nf-call-iptables=1", result.stdout)

    # battery

    def battery(self, value: str | None = "100") -> Path:
        attribute = self.power_supply / "BAT0" / "charge_control_end_threshold"
        attribute.parent.mkdir(parents=True, exist_ok=True)
        if value is not None:
            attribute.write_text(value + "\n")
        return attribute

    def test_the_battery_is_limited_now_and_by_a_unit_that_writes_the_limit_at_boot(self) -> None:
        attribute = self.battery()
        result = self.run_role(github_runner_linux_host_battery_charge_limit=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(attribute.read_text().strip(), "80")
        unit = self.battery_unit.read_text()
        self.assertIn("Type=oneshot\nRemainAfterExit=yes\n", unit)
        self.assertIn(f"ConditionPathExists={attribute}\n", unit)
        self.assertIn("After=systemd-udev-trigger.service\n", unit)
        self.assertIn(f"ExecStart=/bin/sh -c 'echo 80 > {attribute}'\n", unit)
        self.assertIn("WantedBy=multi-user.target\n", unit)
        self.assertEqual([call for call in self.calls() if call.startswith("systemctl") and "is-" not in call], ["systemctl daemon-reload", "systemctl enable github-runner-battery-charge-limit.service"])

    def test_a_configured_limit_and_device_are_used(self) -> None:
        attribute = self.battery("100")
        (self.power_supply / "BAT1").mkdir()
        other = self.power_supply / "BAT1" / "charge_control_end_threshold"
        other.write_text("100\n")
        self.assertEqual(self.run_role(github_runner_linux_host_battery_charge_limit=True, github_runner_linux_host_battery_charge_limit_percent=60, github_runner_linux_host_battery_device="BAT1").returncode, 0)
        self.assertEqual((other.read_text().strip(), attribute.read_text().strip()), ("60", "100"))
        self.assertIn("charge to 60 percent", self.battery_unit.read_text())

    def test_a_battery_at_the_limit_with_the_unit_enabled_changes_nothing(self) -> None:
        self.battery()
        self.assertEqual(self.run_role(github_runner_linux_host_battery_charge_limit=True).returncode, 0)
        before = len(self.calls())
        result = self.run_role(github_runner_linux_host_battery_charge_limit=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changed(result), 0, result.stdout)
        self.assertEqual([call for call in self.calls()[before:] if "is-enabled" not in call and not call.startswith("cat")], [])

    def test_a_host_without_the_attribute_is_skipped_and_says_so(self) -> None:
        result = self.run_role(github_runner_linux_host_battery_charge_limit=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("charge_control_end_threshold does not exist", re.sub(r"\s+", " ", result.stdout))
        self.assertFalse(self.battery_unit.exists())
        self.assertEqual(self.changes(), [])

    def test_a_limit_that_is_not_a_percentage_is_refused_before_anything_is_changed(self) -> None:
        attribute = self.battery()
        for percent in (0, 101, -5):
            with self.subTest(percent=percent):
                result = self.run_role(github_runner_linux_host_battery_charge_limit=True, github_runner_linux_host_battery_charge_limit_percent=percent)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be a whole number from 1 to 100", re.sub(r"\s+", " ", result.stdout))
                self.assertEqual(attribute.read_text().strip(), "100")
                self.assertFalse(self.battery_unit.exists())

    def test_a_check_run_of_the_battery_limit_changes_nothing(self) -> None:
        attribute = self.battery()
        result = self.run_role(check=True, github_runner_linux_host_battery_charge_limit=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(attribute.read_text().strip(), "100")
        self.assertFalse(self.battery_unit.exists())
        self.assertIn(f"write 80 to {attribute} (it is 100)", result.stdout)
        self.assertIn("systemctl enable github-runner-battery-charge-limit.service", result.stdout)

    # headless

    def test_a_graphical_host_boots_to_multi_user_and_loses_its_display_manager(self) -> None:
        self.display_manager("gdm3.service")
        result = self.run_role(github_runner_linux_host_headless=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), ["systemctl set-default multi-user.target", "systemctl stop gdm3.service", "systemctl disable gdm3.service"])
        self.assertEqual((self.state / "default-target").read_text().strip(), "multi-user.target")
        self.assertTrue(self.stopped_and_disabled("gdm3.service"))
        self.assertFalse((self.state / "alias-display-manager.service").exists())

    def test_the_alias_that_is_loaded_names_the_display_manager_whatever_unit_it_resolves_to(self) -> None:
        self.display_manager("lightdm.service")
        self.enable("gdm.service", active=True)
        result = self.run_role(github_runner_linux_host_headless=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.changes() if "set-default" not in call], ["systemctl stop lightdm.service", "systemctl disable lightdm.service"])

    def test_an_alias_that_is_not_loaded_leaves_the_display_managers_own_unit_to_be_stopped(self) -> None:
        # Ubuntu 26.04 LTS after an in-place upgrade: gdm.service runs and display-manager.service is not loaded, so stopping the alias exits 5.
        self.display_manager("gdm.service", alias=None)
        result = self.run_role(github_runner_linux_host_headless=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.changes() if "set-default" not in call], ["systemctl stop gdm.service", "systemctl disable gdm.service"])
        self.assertTrue(self.stopped_and_disabled("gdm.service"))

    def test_the_first_candidate_that_is_active_is_the_display_manager_even_after_an_enabled_one(self) -> None:
        self.display_manager("gdm3.service", alias=None, active=False)
        self.display_manager("sddm.service", alias=None)
        result = self.run_role(github_runner_linux_host_headless=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.changes() if "set-default" not in call], ["systemctl stop sddm.service", "systemctl disable sddm.service"])

    def test_a_display_manager_an_earlier_run_stopped_but_left_enabled_is_disabled_and_not_stopped(self) -> None:
        self.display_manager("gdm.service", alias=None, active=False)
        result = self.run_role(github_runner_linux_host_headless=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.changes() if "set-default" not in call], ["systemctl disable gdm.service"])

    def test_a_host_with_no_display_manager_is_told_so_and_does_not_fail(self) -> None:
        result = self.run_role(github_runner_linux_host_headless=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), ["systemctl set-default multi-user.target"])
        self.assertIn("nothing to stop or disable", result.stdout)
        self.assertNotIn("systemctl stop", "\n".join(self.calls()))

    def test_a_display_manager_that_is_loaded_and_neither_active_nor_enabled_is_left_alone(self) -> None:
        (self.state / "loaded-gdm.service").touch()
        (self.state / "default-target").write_text("multi-user.target\n")
        result = self.run_role(github_runner_linux_host_headless=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])

    def test_a_unit_the_host_names_is_used_as_it_is_and_no_candidate_is_read(self) -> None:
        self.display_manager("custom-dm.service", alias=None)
        self.display_manager("gdm.service")
        result = self.run_role(github_runner_linux_host_headless=True, github_runner_linux_host_display_manager_unit="custom-dm.service")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.changes() if "set-default" not in call], ["systemctl stop custom-dm.service", "systemctl disable custom-dm.service"])
        self.assertEqual([call for call in self.calls() if call.startswith("systemctl show")], ["systemctl show --property=Id,LoadState,ActiveState,UnitFileState custom-dm.service"])
        self.assertTrue((self.state / "active-gdm.service").exists())

    def test_a_host_that_names_the_alias_has_it_stopped_before_it_is_disabled(self) -> None:
        self.display_manager("gdm3.service")
        result = self.run_role(github_runner_linux_host_headless=True, github_runner_linux_host_display_manager_unit="display-manager.service")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.changes() if "set-default" not in call], ["systemctl stop display-manager.service", "systemctl disable display-manager.service"])
        self.assertTrue(self.stopped_and_disabled("gdm3.service"))

    def test_a_unit_the_host_names_that_is_not_loaded_is_left_alone(self) -> None:
        self.display_manager("gdm.service", alias=None)
        result = self.run_role(github_runner_linux_host_headless=True, github_runner_linux_host_display_manager_unit="custom-dm.service")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.changes() if "set-default" not in call], [])

    def test_a_headless_host_is_left_as_it_is(self) -> None:
        (self.state / "default-target").write_text("multi-user.target\n")
        result = self.run_role(github_runner_linux_host_headless=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertEqual(self.changed(result), 0, result.stdout)

    def test_a_second_headless_run_changes_nothing(self) -> None:
        self.display_manager("gdm3.service")
        self.assertEqual(self.run_role(github_runner_linux_host_headless=True).returncode, 0)
        before = len(self.calls())
        result = self.run_role(github_runner_linux_host_headless=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changed(result), 0, result.stdout)
        self.assertEqual([call for call in self.calls()[before:] if re.match(r"^systemctl (set-default|disable|stop)", call)], [])

    def test_a_check_run_of_the_headless_step_reports_each_change_and_makes_none(self) -> None:
        self.display_manager("gdm.service", alias=None)
        result = self.run_role(check=True, github_runner_linux_host_headless=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])
        for command in ("set-default multi-user.target", "stop gdm.service", "disable gdm.service"):
            self.assertIn(f"systemctl {command}", result.stdout)

    # unneeded services

    def test_the_enabled_and_running_unneeded_services_are_disabled_and_stopped_and_the_others_left(self) -> None:
        self.enable("cups.socket", active=True)
        self.enable("cups.service", active=True)
        self.enable("bluetooth.service")
        self.enable("ModemManager.service", active=True)
        self.enable("cups-browsed.service", "disabled")
        self.enable("avahi-daemon.service", active=True)
        result = self.run_role(github_runner_linux_host_disable_unneeded_services=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [
            "systemctl disable cups.socket", "systemctl disable cups.service", "systemctl disable bluetooth.service", "systemctl disable ModemManager.service",
            "systemctl stop cups.socket", "systemctl stop cups.service", "systemctl stop ModemManager.service",
        ])
        self.assertTrue((self.state / "active-avahi-daemon.service").exists())

    def test_a_second_run_leaves_the_services_alone(self) -> None:
        self.enable("bluetooth.service", active=True)
        self.assertEqual(self.run_role(github_runner_linux_host_disable_unneeded_services=True).returncode, 0)
        before = len(self.calls())
        result = self.run_role(github_runner_linux_host_disable_unneeded_services=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changed(result), 0, result.stdout)
        self.assertEqual([call for call in self.calls()[before:] if re.match(r"^systemctl (disable|stop)", call)], [])

    def test_a_check_run_of_the_services_reports_each_change_and_makes_none(self) -> None:
        self.enable("bluetooth.service", active=True)
        result = self.run_role(check=True, github_runner_linux_host_disable_unneeded_services=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertIn("systemctl disable bluetooth.service", result.stdout)
        self.assertIn("systemctl stop bluetooth.service", result.stdout)

    # unattended upgrades

    def test_unattended_upgrades_is_installed_and_set_to_install_security_updates_without_rebooting(self) -> None:
        result = self.run_role(github_runner_linux_host_unattended_upgrades=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), ["apt-get install --yes --no-install-recommends unattended-upgrades"])
        text = self.apt_file.read_text()
        for line in ('APT::Periodic::Update-Package-Lists "1";', 'APT::Periodic::Unattended-Upgrade "1";', 'Unattended-Upgrade::Allowed-Origins:: "${distro_id}:${distro_codename}-security";', 'Unattended-Upgrade::Automatic-Reboot "false";'):
            self.assertIn(line + "\n", text)
        self.assertNotIn('Automatic-Reboot "true"', text)

    def test_a_second_unattended_upgrades_run_changes_nothing(self) -> None:
        self.assertEqual(self.run_role(github_runner_linux_host_unattended_upgrades=True).returncode, 0)
        before = len(self.calls())
        result = self.run_role(github_runner_linux_host_unattended_upgrades=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changed(result), 0, result.stdout)
        self.assertEqual([call for call in self.calls()[before:] if call.startswith("apt-get")], [])

    def test_a_check_run_of_unattended_upgrades_changes_nothing(self) -> None:
        result = self.run_role(check=True, github_runner_linux_host_unattended_upgrades=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), [])
        self.assertFalse(self.apt_file.exists())
        self.assertIn("apt-get install unattended-upgrades", result.stdout)

    def test_the_apt_steps_are_refused_on_a_host_without_apt(self) -> None:
        for step in ("github_runner_linux_host_unattended_upgrades", "github_runner_linux_host_upgrade"):
            with self.subTest(step=step):
                result = self.run_role(os_family="RedHat", **{step: True})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("needs apt", re.sub(r"\s+", " ", result.stdout))
                self.assertEqual(self.changes(), [])

    # upgrade

    def pending_upgrade(self, reboot: bool = False, removable: bool = True) -> None:
        (self.state / "upgradable").touch()
        (self.state / "active-multi-user.target").touch()
        if reboot:
            (self.state / "upgrade-needs-reboot").touch()
        if removable:
            (self.state / "removable").touch()

    def apt_calls(self) -> list[str]:
        return [call.removeprefix("apt-get ").replace("--yes ", "") for call in self.calls() if call.startswith("apt-get")]

    def test_the_upgrade_updates_upgrades_and_removes_what_it_superseded_in_that_order(self) -> None:
        self.pending_upgrade()
        result = self.run_role(github_runner_linux_host_upgrade=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.apt_calls(), ["update", "dist-upgrade", "autoremove --purge"])
        self.assertEqual(self.changed(result), 2, result.stdout)
        self.assertFalse([call for call in self.calls() if call.startswith("shutdown")])

    def test_an_upgrade_with_nothing_to_do_changes_nothing(self) -> None:
        result = self.run_role(github_runner_linux_host_upgrade=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changed(result), 0, result.stdout)

    def test_a_node_service_that_is_active_stops_a_reboot_and_the_autoremove_waits(self) -> None:
        self.pending_upgrade(reboot=True)
        (self.state / "active-k3s-agent.service").touch()
        result = self.run_role(github_runner_linux_host_upgrade=True)
        self.assertNotEqual(result.returncode, 0)
        text = re.sub(r"\s+", " ", result.stdout)
        self.assertIn("is a running node of the cluster (k3s-agent.service active)", text)
        self.assertIn("The packages are upgraded and nothing was rebooted", text)
        self.assertEqual(self.apt_calls(), ["update", "dist-upgrade"])
        self.assertFalse([call for call in self.calls() if call.startswith("shutdown")])

    def test_a_reboot_that_is_not_wanted_is_reported_and_not_made(self) -> None:
        self.pending_upgrade(reboot=True)
        (self.state / "active-k3s-agent.service").touch()
        result = self.run_role(github_runner_linux_host_upgrade=True, github_runner_linux_host_upgrade_reboot=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("github_runner_linux_host_upgrade_reboot is false: the host was not rebooted", re.sub(r"\s+", " ", result.stdout))
        self.assertFalse([call for call in self.calls() if call.startswith("shutdown")])
        self.assertEqual(self.apt_calls(), ["update", "dist-upgrade", "autoremove --purge"])

    # ansible.builtin.reboot refuses a local connection ("would reboot the control node"), so these tests cannot make the reboot: they
    # check that the run gets as far as the Reboot task, and that nothing before it refused, and read the task's arguments.
    REBOOT_REFUSED = "Running ansible.builtin.reboot with local connection would reboot the control node"

    def test_a_host_that_is_not_a_node_reaches_the_reboot_when_the_upgrade_needs_one(self) -> None:
        self.pending_upgrade(reboot=True)
        result = self.run_role(github_runner_linux_host_upgrade=True)
        self.assertIn(self.REBOOT_REFUSED, result.stdout)
        self.assertNotIn("is a running node of the cluster (", re.sub(r"\s+", " ", result.stdout))
        self.assertEqual(self.apt_calls(), ["update", "dist-upgrade"])

    def test_a_running_node_reaches_the_reboot_only_when_the_run_says_so(self) -> None:
        self.pending_upgrade(reboot=True)
        (self.state / "active-k3s-agent.service").touch()
        result = self.run_role(github_runner_linux_host_upgrade=True, github_runner_linux_host_upgrade_reboot_running_node=True)
        self.assertIn(self.REBOOT_REFUSED, result.stdout)
        self.assertNotIn("is a running node of the cluster (", re.sub(r"\s+", " ", result.stdout))

    def test_the_reboot_task_waits_for_the_timeout_and_checks_the_host_reached_multi_user_target(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "upgrade.yml").read_text())
        [block] = [task for task in tasks if task.get("name", "") == "Reboot the host and check it came back"]
        [reboot] = [task for task in block["block"] if "ansible.builtin.reboot" in task]
        self.assertIs(reboot["become"], True)
        arguments = reboot["ansible.builtin.reboot"]
        self.assertEqual(arguments["reboot_timeout"], "{{ github_runner_linux_host_reboot_timeout | int }}")
        self.assertEqual(arguments["test_command"], "systemctl is-active multi-user.target")
        names = [task["name"] for task in tasks]
        self.assertLess(names.index(block["name"]), names.index("Remove the packages and kernels the upgrade left unused"))

    def test_the_services_that_make_a_host_a_node_are_the_ones_named(self) -> None:
        self.pending_upgrade(reboot=True)
        (self.state / "active-docker.service").touch()
        self.assertNotEqual(self.run_role(github_runner_linux_host_upgrade=True, github_runner_linux_host_node_services=["docker.service"]).returncode, 0)
        self.assertEqual([call for call in self.calls() if call.startswith("shutdown")], [])

    def test_a_check_run_of_the_upgrade_simulates_and_reports_the_reboot(self) -> None:
        self.pending_upgrade(reboot=True)
        self.reboot_required.touch()
        result = self.run_role(check=True, github_runner_linux_host_upgrade=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.apt_calls(), ["--simulate dist-upgrade", "--simulate autoremove --purge"])
        self.assertFalse([call for call in self.calls() if call.startswith("shutdown")])
        self.assertIn(f"reboot host: {self.reboot_required} exists", result.stdout)
        self.assertTrue((self.state / "upgradable").exists())


if __name__ == "__main__":
    unittest.main()
