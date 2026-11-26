"""Tests for what the cluster role prepares inside the Colima VM, its end checks and its watchdog job, run through the role against stand-ins (runtime_autostart_support.py)."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import unittest

import yaml
from runtime_autostart_support import DEFAULTS, I386_ENTRY, I386_OFF, MODULES_FILE, ROLE, SYSCTL_FILE, UID, RoleFixture, failed_task


class ColimaVmTest(RoleFixture):
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

    def test_an_x86_vm_switches_the_i386_handler_off_now_and_after_every_start(self) -> None:
        config = self.x86_vm()
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.i386_writes(), [f"sudo tee {I386_ENTRY}"])
        self.assertEqual(self.vm_file(I386_ENTRY).read_text().strip(), "0")
        settings = yaml.safe_load(config.read_text())
        self.assertEqual(settings["provision"], [{"mode": "after-boot", "script": I386_OFF}])
        # Only the provision list changes: Colima's binfmt setting stays on, so the foreign-architecture handler is kept.
        self.assertEqual({key: value for key, value in settings.items() if key != "provision"}, {"cpu": 8, "arch": "x86_64", "binfmt": True})

    def test_the_provision_script_switches_off_only_the_i386_entry_and_tolerates_its_absence(self) -> None:
        self.assertEqual(I386_ENTRY, "/proc/sys/fs/binfmt_misc/qemu-i386")
        self.assertEqual(I386_OFF, f"test ! -e {I386_ENTRY} || echo 0 | sudo tee {I386_ENTRY} > /dev/null")
        for present in (True, False):
            with self.subTest(present=present):
                entry = self.work / f"entry-{present}"
                if present:
                    entry.write_text("enabled\n")
                script = I386_OFF.replace(I386_ENTRY, str(entry)).replace("sudo ", "")
                self.assertEqual(subprocess.run(["sh", "-c", script], check=False).returncode, 0)
                self.assertEqual(entry.read_text().strip() if present else entry.exists(), "0" if present else False)

    def test_a_second_run_on_an_x86_vm_changes_nothing(self) -> None:
        config = self.x86_vm()
        self.run_role(github_runner_cluster_runtime_autostart=False)
        before, written = len(self.vm_changes()), config.read_text()
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual(self.vm_changes()[before:], [])
        self.assertEqual(config.read_text(), written)

    def test_an_x86_vm_keeps_the_provision_scripts_it_has(self) -> None:
        own = {"mode": "ready", "script": "echo ready"}
        config = self.x86_vm(provision=[own])
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(yaml.safe_load(config.read_text())["provision"], [own, {"mode": "after-boot", "script": I386_OFF}])

    def test_an_x86_vm_with_the_handler_already_off_or_absent_is_not_written_to(self) -> None:
        for handler in ("disabled", None):
            with self.subTest(handler=handler):
                shutil.rmtree(self.stub / "vm", ignore_errors=True)
                self.x86_vm(provision=[{"mode": "after-boot", "script": I386_OFF}], handler=handler)
                result = self.run_role(github_runner_cluster_runtime_autostart=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.i386_writes(), [])

    def test_a_check_run_on_an_x86_vm_reports_the_handler_and_writes_nothing(self) -> None:
        config = self.x86_vm()
        written = config.read_text()
        result = self.run_role("--check", github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"echo 0 > {I386_ENTRY} in the VM", result.stdout)
        self.assertEqual(self.i386_writes(), [])
        self.assertEqual(self.vm_file(I386_ENTRY).read_text().splitlines()[0], "enabled")
        self.assertEqual(config.read_text(), written)

    def test_an_aarch64_vm_keeps_its_i386_handler_and_its_profile(self) -> None:
        self.state("runtime-up")
        result = self.run_role(github_runner_cluster_runtime_autostart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.i386_writes(), [])
        self.assertFalse((self.home / ".colima").exists())

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

    def test_the_watchdog_is_installed_beside_the_agent_and_loaded_into_the_login_session(self) -> None:
        self.state("session", "runtime-up")
        result = self.run_role(github_runner_cluster_runtime_watchdog=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        job = self.home / "Library" / "LaunchAgents" / "com.exadev.github-runner.colima-watchdog.default.plist"
        self.assertEqual(plistlib.loads(job.read_bytes())["StartInterval"], DEFAULTS["github_runner_cluster_runtime_watchdog_interval"])
        directory = self.home / ".local" / "share" / "exadev-github-runner" / "colima-watchdog-default"
        for name in ("colima-watchdog.sh", "run_bounded.pl"):
            self.assertTrue(os.access(directory / name, os.X_OK), name)
        self.assertTrue((directory / "state").is_dir())
        self.assertEqual(self.watchdog_calls(), [f"launchctl bootstrap gui/{UID} {job}"])

    def test_the_node_profile_gets_the_watchdog_as_a_daemon(self) -> None:
        self.state("runtime-up")
        result = self.run_role(context="colima-node", github_runner_cluster_runtime_watchdog=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        job = self.daemons / "com.exadev.github-runner.colima-watchdog.node.plist"
        self.assertEqual(plistlib.loads(job.read_bytes())["UserName"], "example")
        self.assertEqual(self.watchdog_calls(), [f"launchctl bootstrap system {job}"])

    def test_a_second_run_changes_nothing_and_a_changed_job_is_unloaded_before_it_is_loaded_again(self) -> None:
        self.state("session", "runtime-up")
        self.assertEqual(self.run_role(github_runner_cluster_runtime_watchdog=True).returncode, 0)
        before = self.watchdog_calls()
        self.assertEqual(self.run_role(github_runner_cluster_runtime_watchdog=True).returncode, 0)
        self.assertEqual(self.watchdog_calls(), before)
        result = self.run_role(github_runner_cluster_runtime_watchdog=True, github_runner_cluster_runtime_watchdog_interval=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call.split()[1] for call in self.watchdog_calls()[len(before):]], ["bootout", "bootstrap"])

    def test_off_installs_nothing(self) -> None:
        self.state("session", "runtime-up")
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.watchdog_calls(), [])
        self.assertFalse((self.home / "Library" / "LaunchAgents" / "com.exadev.github-runner.colima-watchdog.default.plist").exists())

    def test_turning_it_off_unloads_and_removes_an_installed_job(self) -> None:
        self.state("session", "runtime-up")
        self.assertEqual(self.run_role(github_runner_cluster_runtime_watchdog=True).returncode, 0)
        job = self.home / "Library" / "LaunchAgents" / "com.exadev.github-runner.colima-watchdog.default.plist"
        self.assertEqual(self.run_role().returncode, 0)
        self.assertFalse(job.exists())
        self.assertEqual(self.watchdog_calls()[-1], f"launchctl bootout gui/{UID}/com.exadev.github-runner.colima-watchdog.default")


if __name__ == "__main__":
    unittest.main()
