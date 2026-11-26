"""Tests for the launchd daemon or agent the cluster role installs to start the runtime, the Colima home and context it runs with, and the other runtimes and check mode, run through the role against stand-ins (runtime_autostart_support.py)."""

from __future__ import annotations

import os
import plistlib
import re
import unittest

from runtime_autostart_support import UID, RoleFixture, failed_task


class LaunchdJobTest(RoleFixture):
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
        self.assertEqual([call for call in self.calls()[len(before):] if not re.match(r"\S+ (docker (info|ps|context inspect)|launchctl print |colima ssh --profile \S+ -- (cat|test|uname|sysctl -n|systemctl is-active|dpkg-query) )", call)], [])
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
        # A profile that is stopped never reaches the stop's own read of the context, so the home comes from the variable alone.
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


if __name__ == "__main__":
    unittest.main()
