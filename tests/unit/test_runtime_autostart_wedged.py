"""Tests for how the cluster role starts a stopped Colima profile and recovers one listed as running whose runtime does not answer, run through the role against stand-ins (runtime_autostart_support.py)."""

from __future__ import annotations

import os
import plistlib
import re
import time
import unittest

from runtime_autostart_support import UID, RoleFixture


class WedgedRuntimeTest(RoleFixture):
    def test_a_stopped_colima_is_started_and_the_agent_installed_and_loaded(self) -> None:
        self.state("session")
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.changes(), ["colima colima start --profile default --activate=false".replace("colima colima", "colima colima"), f"colima launchctl bootstrap gui/{UID} {self.plist}"])
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["ProgramArguments"][3:5], ["--profile", "default"])
        self.assertEqual(self.plist.stat().st_mode & 0o777, 0o644)
        self.assertTrue((self.stub / "loaded").exists())

    def test_a_vm_whose_runtime_stays_silent_and_whose_guest_and_agent_confirm_it_is_stopped_cleanly_and_started_again(self) -> None:
        self.listed_running()
        result = self.run_role(**self.FAST)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.stops(), ["colima colima stop --profile default"])
        self.assertEqual(self.starts(), ["colima colima start --profile default --activate=false"])
        self.assertLess(self.calls().index(self.stops()[0]), self.calls().index(self.starts()[0]))
        # The first probe and every further try.
        self.assertGreaterEqual(len(self.info_calls()), 4)
        self.assertTrue((self.stub / "runtime-up").exists())

    def test_a_clean_stop_that_fails_is_followed_by_a_forced_one(self) -> None:
        self.listed_running()
        self.state("clean-stop-fails")
        result = self.run_role(**self.FAST)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.stops(), ["colima colima stop --profile default", "colima colima stop --profile default --force"])
        self.assertEqual(len(self.starts()), 1)

    def test_only_the_hosts_own_profile_is_stopped_while_another_listed_profile_runs(self) -> None:
        self.listed_running()
        self.run_role(**self.FAST)
        self.assertEqual([call for call in self.stops() if "--profile other" in call], [])
        self.assertTrue(self.stops())

    def test_a_daemon_that_answers_on_a_later_try_is_never_stopped(self) -> None:
        # The first probe and one further try fail, as on a loaded host or while dockerd restarts, and the next answers.
        self.listed_running()
        (self.stub / "docker-answers-after").write_text("2")
        result = self.run_role(**self.FAST)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.stops(), [])
        self.assertEqual(self.starts(), [])

    def test_a_guest_that_answers_a_command_is_left_alone_although_docker_is_silent(self) -> None:
        self.listed_running()
        self.state("guest-answers")
        self.assert_left_alone(self.run_role(**self.FAST), "a command in the guest succeeded")

    def test_a_vm_whose_agent_started_moments_ago_is_left_alone_however_old_its_pid_file_is(self) -> None:
        # The pid file is a week old, from a boot long gone, and the process it names started thirty seconds ago.
        self.listed_running(agent_age=30)
        self.assert_left_alone(self.run_role(**self.FAST), "for less than 600 seconds")

    def test_a_pid_file_naming_no_running_process_leaves_the_age_unknown_and_nothing_is_stopped(self) -> None:
        # An old pid file whose process is gone, as after a reboot: its date says the agent is old, and that must not count.
        self.listed_running(stale_file=True)
        self.assert_left_alone(self.run_role(**self.FAST), "could not be read")

    def test_a_pid_file_naming_another_program_leaves_the_age_unknown(self) -> None:
        self.listed_running(command="/usr/sbin/cron -f")
        self.assert_left_alone(self.run_role(**self.FAST), "could not be read")

    def test_a_pid_file_naming_a_program_whose_path_merely_contains_hostagent_leaves_the_age_unknown(self) -> None:
        self.listed_running(command="/opt/hostagent/bin/cron -f")
        self.assert_left_alone(self.run_role(**self.FAST), "could not be read")

    def test_a_report_of_an_old_agent_does_not_say_ago_twice(self) -> None:
        self.listed_running(command="/usr/local/bin/limactl hostagent x", agent_age=7200)
        self.state("guest-answers")
        result = self.run_role(**self.FAST)
        self.assertIn("for more than 600 seconds;", result.stdout)
        self.assertNotIn("seconds ago", result.stdout)

    # The Docker context, which the stop reads for its socket check on its own.

    def test_a_host_that_names_its_colima_home_and_has_no_readable_context_still_runs(self) -> None:
        # The README's route for a host whose context does not say where the profile is (DOCKER_HOST, say): the variable gives the home and the context cannot be read. Nothing is listed as running, so the play starts the profile as before.
        for label, variables in (("an empty context", {"context": ""}), ("an unknown context", {})):
            with self.subTest(label=label):
                self.forget()
                self.state("session")
                if not variables:
                    self.state("context-unknown")
                result = self.run_role(github_runner_cluster_colima_home="/elsewhere", github_runner_cluster_runtime="colima", **variables)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_colima_ran_in("/elsewhere")
                self.assertEqual(self.stops(), [])

    def test_a_context_that_cannot_be_read_never_leads_to_a_stop_and_the_report_says_so(self) -> None:
        for label, variables in (("an empty context", {"context": ""}), ("an unknown context", {"unknown": True})):
            with self.subTest(label=label):
                self.forget()
                self.listed_running()
                if variables.get("unknown"):
                    self.state("context-unknown")
                result = self.run_role(github_runner_cluster_colima_home=f"{self.home}/.colima", github_runner_cluster_runtime="colima", **{k: v for k, v in variables.items() if k != "unknown"}, **self.FAST)
                self.assert_left_alone(result, "so its socket is not proven to belong to the profile")

    def test_a_context_whose_endpoint_is_another_runtime_never_leads_to_a_stop(self) -> None:
        for endpoint in ("tcp://remote.example.invalid:2375", "unix:///var/run/docker.sock"):
            with self.subTest(endpoint=endpoint):
                self.forget()
                self.listed_running()
                result = self.run_role(context_host=endpoint, github_runner_cluster_colima_home=f"{self.home}/.colima", **self.FAST)
                self.assert_left_alone(result, "does not point at the socket of the profile")

    # The supervisor failing is not a failed probe.

    def test_a_supervisor_that_cannot_run_the_command_fails_the_run_naming_it_and_is_not_evidence(self) -> None:
        for code in (127, 255):
            with self.subTest(code=code):
                self.forget()
                self.listed_running()
                (self.stub / "docker-exit").write_text(str(code))
                result = self.run_role(**self.FAST)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("run_bounded.pl", result.stdout)
                self.assertEqual(self.stops(), [])

    def test_a_host_without_perl_fails_the_run_naming_the_supervisor(self) -> None:
        self.listed_running()
        result = self.run_role(github_runner_cluster_runtime_path=str(self.bin), **self.FAST)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("run_bounded.pl", result.stdout)
        self.assertEqual(self.stops(), [])

    def test_a_perl_that_is_not_executable_fails_the_run_naming_the_supervisor(self) -> None:
        # The command module reports this with the OS error number as its rc (13), not 2: the message identifies it.
        self.listed_running()
        perl_dir = self.work / "no-perl"
        perl_dir.mkdir()
        (perl_dir / "perl").write_text("#!/bin/sh\n")
        (perl_dir / "perl").chmod(0o644)
        result = self.run_role(github_runner_cluster_runtime_path=f"{perl_dir}{os.pathsep}{self.bin}", **self.FAST)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("run_bounded.pl", result.stdout)
        self.assertEqual(self.stops(), [])

    def test_a_tool_that_fails_by_itself_still_counts_as_a_failed_probe(self) -> None:
        # docker exiting 1 (cannot connect) is the daemon's answer, not the supervisor's.
        self.listed_running()
        (self.stub / "docker-exit").write_text("1")
        result = self.run_role(**self.FAST)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.stops()), 1)

    def test_an_agent_that_has_run_for_days_counts_as_old(self) -> None:
        self.listed_running(agent_age=3 * 86400 + 3600 + 61)
        result = self.run_role(**self.FAST)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.stops()), 1)

    def test_an_agent_whose_age_cannot_be_read_does_not_count_as_old(self) -> None:
        self.listed_running(agent_age=None)
        self.assert_left_alone(self.run_role(**self.FAST), "could not be read")

    def test_a_colima_home_that_is_not_where_the_context_points_never_leads_to_a_stop(self) -> None:
        # The variable names another Colima home than the one the Docker context's socket is in: the probes measured one runtime and a stop would act on another's profile. (A context whose endpoint is no Colima socket at all fails earlier, in the filter that reads the home from it.)
        self.listed_running()
        result = self.run_role(github_runner_cluster_colima_home="/elsewhere", **self.FAST)
        self.assert_left_alone(result, "does not point at the socket of the profile")

    def test_with_the_stop_switched_off_a_confirmed_wedge_fails_naming_the_switch(self) -> None:
        self.listed_running()
        self.assert_left_alone(self.run_role(github_runner_cluster_runtime_force_stop_wedged=False, **self.FAST), "github_runner_cluster_runtime_force_stop_wedged is off")

    def test_the_stop_switch_is_independent_of_autostart(self) -> None:
        self.listed_running()
        result = self.run_role(github_runner_cluster_runtime_force_stop_wedged=False, github_runner_cluster_runtime_autostart=True, **self.FAST)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.stops(), [])

    def test_a_second_run_after_the_recovery_stops_and_starts_nothing(self) -> None:
        self.listed_running()
        self.run_role(**self.FAST)
        before = len(self.calls())
        result = self.run_role(**self.FAST)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls()[before:] if re.match(r"\S+ colima (stop|start)", call)], [])

    def test_a_stopped_profile_is_started_without_being_stopped_first(self) -> None:
        self.state("session")
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.stops(), [])

    def test_a_running_runtime_is_neither_listed_nor_stopped(self) -> None:
        self.state("session", "runtime-up", "colima-listed-running")
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.stops(), [])
        self.assertEqual([call for call in self.calls() if re.match(r"\S+ colima list", call)], [])

    def test_probes_that_never_reply_and_ignore_sigalrm_are_ended_and_the_vm_is_stopped_and_started(self) -> None:
        # docker info and the guest command never reply and ignore SIGALRM, as Go programs do; the clean stop never ends and ignores TERM too.
        self.listed_running()
        self.state("docker-hangs", "guest-hangs", "clean-stop-hangs", "ignore-term")
        began = time.monotonic()
        result = self.run_role(**self.HANG)
        elapsed = time.monotonic() - began
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.stops(), ["colima colima stop --profile default", "colima colima stop --profile default --force"])
        self.assertEqual(len(self.starts()), 1)
        # Unbounded, each stand-in sleeps for 300 seconds.
        self.assertLess(elapsed, 120)
        self.assertGreaterEqual(len(self.hung_pids()), 5)
        self.assert_all_ended()

    def test_a_hung_docker_with_a_guest_that_answers_fails_the_run_within_the_bound_and_stops_nothing(self) -> None:
        self.listed_running()
        self.state("docker-hangs", "guest-answers")
        began = time.monotonic()
        result = self.run_role(**self.HANG)
        self.assertLess(time.monotonic() - began, 120)
        self.assert_left_alone(result, "a command in the guest succeeded")
        self.assert_all_ended()

    def test_a_hung_first_probe_counts_as_the_runtime_not_answering(self) -> None:
        # A stopped profile whose Docker socket accepts and never replies: the start follows, as for a socket that refuses.
        self.state("session", "docker-hangs")
        began = time.monotonic()
        result = self.run_role(**self.HANG)
        self.assertLess(time.monotonic() - began, 120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.starts()), 1)
        self.assert_all_ended()

    def test_check_mode_reads_everything_stops_nothing_and_says_what_a_real_run_would_do(self) -> None:
        self.listed_running()
        result = self.run_role("--check", **self.FAST)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.stops(), [])
        self.assertTrue((self.stub / "colima-listed-running").exists())
        self.assertIn("A real run would stop the profile", result.stdout)

    def test_check_mode_on_a_vm_that_is_not_shown_to_be_wedged_says_nothing_would_be_stopped(self) -> None:
        self.listed_running()
        self.state("guest-answers")
        result = self.run_role("--check", **self.FAST)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.stops(), [])
        self.assertIn("A real run would stop nothing and fail here", result.stdout)

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


if __name__ == "__main__":
    unittest.main()
