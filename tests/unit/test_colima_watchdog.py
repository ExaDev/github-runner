"""Tests for files/colima-watchdog.sh, which restarts a Colima VM whose container runtime stopped answering: when it acts, when it leaves the VM alone, and the cap and backoff on restarts. docker, colima and ps are stand-ins on PATH whose answers each test sets through STUB_* variables; run_bounded.pl is the real supervisor."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / "roles" / "github_runner_cluster" / "files"
PROFILE = "node"
SOCKET = "unix://{home}/node/docker.sock"
RUNNING = '{"name":"node","status":"Running","arch":"aarch64"}\n{"name":"default","status":"Stopped"}'
HOST_AGENT = "20:00:00 /opt/homebrew/bin/limactl hostagent --pidfile ha.pid --socket ha.sock colima-node"

DOCKER = """#!/bin/sh
case "$1 $2" in
  "info --format") exit "${STUB_DOCKER_INFO:-1}" ;;
  "context show") echo "${DOCKER_CONTEXT:-colima-node}" ;;
  "context inspect") printf '%s\\n' "$STUB_ENDPOINT" ;;
esac
"""
COLIMA = """#!/bin/sh
echo "colima $*" >> "$STUB_CALLS"
case "$1" in
  list) printf '%s\\n' "$STUB_LIST" ;;
  ssh) exit "${STUB_GUEST:-1}" ;;
  stop) case "$*" in *--force*) exit 0 ;; *) exit "${STUB_STOP:-0}" ;; esac ;;
  start) exit "${STUB_START:-0}" ;;
esac
"""
PS = """#!/bin/sh
printf '%s\\n' "$STUB_PS"
"""


class Watchdog(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        for name, body in (("docker", DOCKER), ("colima", COLIMA), ("ps", PS)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.install = self.work / "install"
        self.install.mkdir()
        for name in ("colima-watchdog.sh", "run_bounded.pl"):
            shutil.copy(FILES / name, self.install / name)
        self.state = self.work / "state"
        self.home = self.work / "colima"
        (self.home / "_lima" / "colima-node").mkdir(parents=True)
        (self.home / "_lima" / "colima-node" / "ha.pid").write_text("4242\n")
        self.calls = self.work / "calls"
        self.calls.touch()

    def run_watchdog(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.work),
            "COLIMA_HOME": str(self.home),
            "WATCHDOG_PROFILE": PROFILE,
            "WATCHDOG_STATE_DIR": str(self.state),
            "WATCHDOG_PROBE_TIMEOUT": "5",
            "WATCHDOG_KILL_GRACE": "1",
            "STUB_CALLS": str(self.calls),
            "STUB_LIST": RUNNING,
            "STUB_PS": HOST_AGENT,
            "STUB_ENDPOINT": SOCKET.format(home=self.home),
            **overrides,
        }
        return subprocess.run(["/bin/bash", str(self.install / "colima-watchdog.sh")], env=env, capture_output=True, text=True, check=False, timeout=60)

    def run_until_acting(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        """Run enough times for the failure threshold to be reached, and return the last run."""
        for _ in range(2):
            self.assertEqual(self.run_watchdog(**overrides).returncode, 0)
        return self.run_watchdog(**overrides)

    def colima_calls(self) -> list[str]:
        return [line for line in self.calls.read_text().splitlines() if not line.startswith(("colima list", "colima ssh"))]

    def restarts(self) -> list[int]:
        return [int(line) for line in (self.state / "restarts").read_text().split()]


class WhenItActs(Watchdog):
    def test_a_healthy_runtime_prints_nothing_and_resets_the_count(self) -> None:
        self.run_watchdog()
        self.assertTrue((self.state / "failures").exists())
        result = self.run_watchdog(STUB_DOCKER_INFO="0")
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertFalse((self.state / "failures").exists())

    def test_it_waits_for_the_consecutive_failures_before_looking_further(self) -> None:
        first = self.run_watchdog()
        self.assertIn("1 of 3", first.stdout)
        self.assertEqual(self.calls.read_text(), "")

    def test_a_wedged_vm_is_stopped_and_started_and_the_restart_is_logged(self) -> None:
        result = self.run_until_acting()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.colima_calls(), ["colima stop --profile node", "colima start --profile node --activate=false"])
        self.assertIn("restarting", result.stdout)
        self.assertIn("restarted", result.stdout)
        self.assertEqual(len(self.restarts()), 1)
        self.assertFalse((self.state / "failures").exists())

    def test_a_clean_stop_that_fails_is_followed_by_a_forced_one(self) -> None:
        self.run_until_acting(STUB_STOP="1")
        self.assertEqual(self.colima_calls(), ["colima stop --profile node", "colima stop --profile node --force", "colima start --profile node --activate=false"])

    def test_a_start_that_fails_exits_1_and_still_counts_against_the_cap(self) -> None:
        result = self.run_until_acting(STUB_START="1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(self.restarts()), 1)


class WhenItLeavesTheVmAlone(Watchdog):
    def assert_left_alone(self, result: subprocess.CompletedProcess[str], reason: str) -> None:
        self.assertEqual(result.returncode, 0)
        self.assertIn(reason, result.stdout)
        self.assertEqual(self.colima_calls(), [])

    def test_a_guest_that_answers(self) -> None:
        self.assert_left_alone(self.run_until_acting(STUB_GUEST="0"), "a command in the guest works")

    def test_a_host_agent_younger_than_the_boot_grace(self) -> None:
        self.assert_left_alone(self.run_until_acting(STUB_PS="05:00 /opt/homebrew/bin/limactl hostagent --pidfile ha.pid"), "300 seconds")

    def test_a_pid_that_is_not_the_host_agent(self) -> None:
        self.assert_left_alone(self.run_until_acting(STUB_PS="20:00:00 /usr/bin/other --flag"), "could not be read")

    def test_a_dead_pid(self) -> None:
        self.assert_left_alone(self.run_until_acting(STUB_PS=""), "could not be read")

    def test_a_context_that_points_at_another_profiles_socket(self) -> None:
        self.assert_left_alone(self.run_until_acting(STUB_ENDPOINT="unix:///elsewhere/default/docker.sock"), "does not point at the socket")

    def test_a_profile_colima_lists_as_stopped(self) -> None:
        self.assert_left_alone(self.run_until_acting(STUB_LIST='{"name":"node","status":"Stopped"}'), "only a profile listed as Running")

    def test_a_profile_that_is_not_listed(self) -> None:
        self.assert_left_alone(self.run_until_acting(STUB_LIST='{"name":"default","status":"Running"}'), "not listed")

    def test_a_days_old_host_agent_counts_as_old_enough(self) -> None:
        result = self.run_until_acting(STUB_PS="2-03:04:05 /opt/homebrew/bin/limactl hostagent --pidfile ha.pid")
        self.assertIn("183845 seconds", result.stdout)
        self.assertEqual(len(self.colima_calls()), 2)


class TheRestartCapAndBackoff(Watchdog):
    def seed(self, *ages_seconds: int) -> None:
        self.state.mkdir(exist_ok=True)
        now = int(time.time())
        (self.state / "restarts").write_text("".join(f"{now - age}\n" for age in ages_seconds))

    def test_a_restart_within_the_minimum_gap_of_the_previous_one_waits(self) -> None:
        self.seed(100)
        result = self.run_until_acting()
        self.assertEqual(result.returncode, 0)
        self.assertIn("not restarting yet", result.stdout)
        self.assertEqual(self.colima_calls(), [])

    def test_the_gap_doubles_for_every_further_restart_in_the_hour(self) -> None:
        self.seed(1500, 700)
        self.assertIn("not restarting yet", self.run_until_acting(WATCHDOG_MAX_RESTARTS="5").stdout)
        self.assertEqual(self.colima_calls(), [])

    def test_a_restart_after_the_gap_goes_ahead(self) -> None:
        self.seed(700)
        self.run_until_acting()
        self.assertEqual(len(self.colima_calls()), 2)
        self.assertEqual(len(self.restarts()), 2)

    def test_the_cap_refuses_a_restart_and_exits_1_with_the_commands_to_recover_by_hand(self) -> None:
        self.seed(3000, 2000, 1000)
        for _ in range(2):
            self.run_watchdog()
        result = self.run_watchdog()
        self.assertEqual(result.returncode, 1)
        self.assertIn("the cap is 3", result.stdout)
        self.assertIn("colima stop --profile node --force", result.stdout)
        self.assertEqual(self.colima_calls(), [])

    def test_restarts_older_than_an_hour_no_longer_count(self) -> None:
        self.seed(7000, 6000, 5000)
        self.run_until_acting()
        self.assertEqual(len(self.restarts()), 1)
        self.assertEqual(len(self.colima_calls()), 2)


class Misuse(Watchdog):
    def test_a_supervisor_that_cannot_run_is_no_verdict_on_the_vm(self) -> None:
        (self.install / "run_bounded.pl").write_text("exit 127\n")
        result = self.run_watchdog()
        self.assertEqual(result.returncode, 2)
        self.assertIn("says nothing about the VM", result.stdout)
        self.assertFalse((self.state / "failures").exists())

    def test_the_profile_and_state_directory_are_required(self) -> None:
        result = subprocess.run(["/bin/bash", str(self.install / "colima-watchdog.sh")], env={"PATH": os.environ["PATH"]}, capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
