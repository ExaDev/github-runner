"""Tests the github_runner_linux_host role: its defaults, argument specs and README, and its tasks (tests/linux_host/run.yml) against stand-ins for systemctl, nmcli and iw on PATH, with the drop-ins redirected, so that no real target, drop-in or radio is touched."""

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


class DefaultsTest(unittest.TestCase):
    def test_both_steps_are_off_by_default(self) -> None:
        self.assertIs(DEFAULTS["github_runner_linux_host_keep_awake"], False)
        self.assertIs(DEFAULTS["github_runner_linux_host_wifi_powersave_off"], False)

    def test_every_sleep_the_host_can_enter_is_masked_and_every_lid_setting_is_set(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_linux_host_sleep_targets"], TARGETS)
        self.assertEqual(DEFAULTS["github_runner_linux_host_lid_switch_settings"], ["HandleLidSwitch", "HandleLidSwitchExternalPower", "HandleLidSwitchDocked"])

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


class RoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="linux-host-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stubs = self.tmp / "stubs"
        self.state = self.stubs / "state"
        self.state.mkdir(parents=True)
        (self.stubs / "bin").mkdir()
        self.logind = self.tmp / "etc" / "systemd" / "logind.conf.d" / "90-github-runner-keep-awake.conf"
        self.networkmanager = self.tmp / "etc" / "NetworkManager" / "conf.d" / "zz-github-runner-wifi-powersave.conf"
        log = 'echo "$(basename "$0") $*" >> "$STUB/calls.log"\n'
        bin_dir = self.stubs / "bin"
        stand_in(bin_dir / "systemctl", log + r"""case "$1" in
  is-enabled) if [ -e "$STUB/state/masked-$2" ]; then echo masked; exit 1; fi; echo static; exit 0 ;;
  mask) touch "$STUB/state/masked-$2" ;;
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
        (self.state / "devices").write_text("wlp2s0:wifi\ntailscale0:tun\nlo:loopback\np2p-dev-wlp2s0:wifi-p2p\n")
        (self.state / "power-save-wlp2s0").touch()

    def run_role(self, *args: str, check: bool = False, system: str = "Linux", **variables: Any) -> subprocess.CompletedProcess[str]:
        inventory = self.tmp / "inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"hosts": {"host": {
            "ansible_connection": "local",
            "ansible_become": False,
            "github_runner_linux_host_logind_dropin": str(self.logind),
            "github_runner_linux_host_networkmanager_dropin": str(self.networkmanager),
            **variables,
        }}}}))
        env = {
            **os.environ,
            "PATH": f"{self.stubs / 'bin'}:{os.environ['PATH']}",
            "STUB": str(self.stubs),
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
        return [call for call in self.calls() if re.match(r"^(systemctl (mask|kill)|nmcli general|iw dev \S+ set)", call)]

    def changed(self, result: subprocess.CompletedProcess[str]) -> int:
        return int(re.search(r"changed=(\d+)", result.stdout).group(1))  # type: ignore[union-attr]

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


if __name__ == "__main__":
    unittest.main()
