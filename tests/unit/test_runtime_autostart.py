"""Tests for how the cluster role keeps a macOS host's container runtime running across reboots: the launchd agent and daemon template (tests/runtime_autostart/render_plist.yml), the variable defaults, the macOS-only guard in tasks/main.yml and the bounded runner. The role's runtime tasks run against stand-ins in the test_runtime_autostart_*.py modules beside this one."""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import yaml
from runtime_autostart_support import ANSIBLE_PLAYBOOK, DARWIN_ONLY, DEFAULTS, HOME, HOST_OS, MODULES_FILE, RENDER, ROLE, ROOT, SYSCTL_FILE, ansible_env


def render_plist(**role_vars: Any) -> bytes:
    """Render the launchd agent with ansible-playbook and return the property list."""
    with tempfile.TemporaryDirectory() as work:
        output = Path(work) / "agent.plist"
        extra = Path(work) / "extra.json"
        extra.write_text(json.dumps({"plist_output": str(output), **role_vars}))
        subprocess.run([ANSIBLE_PLAYBOOK, str(RENDER), "-e", f"@{extra}"], check=True, env=ansible_env(), cwd=work, capture_output=True, text=True)
        return output.read_bytes()


class DefaultsTest(unittest.TestCase):
    def test_the_autostart_is_on_and_the_runtime_is_taken_from_the_docker_context(self) -> None:
        self.assertIs(DEFAULTS["github_runner_cluster_runtime_autostart"], True)
        self.assertEqual(DEFAULTS["github_runner_cluster_runtime"], "auto")
        self.assertEqual(DEFAULTS["github_runner_cluster_colima_profile"], "")

    def test_the_path_names_both_homebrew_prefixes_and_the_system_directories(self) -> None:
        entries = DEFAULTS["github_runner_cluster_runtime_path"].split(":")
        for prefix in ("/opt/homebrew", "/usr/local"):
            self.assertIn(f"{prefix}/bin", entries)
            self.assertIn(f"{prefix}/sbin", entries)
        for system in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            self.assertIn(system, entries)

    def test_the_agent_is_per_profile_and_lives_in_the_connecting_users_launch_agents(self) -> None:
        self.assertIn("{{ github_runner_cluster_effective_runtime.colima_profile }}", DEFAULTS["github_runner_cluster_runtime_launchd_label"])
        plist = DEFAULTS["github_runner_cluster_runtime_agent_plist"]
        self.assertTrue(plist.startswith("{{ ansible_facts['env']['HOME'] }}/Library/LaunchAgents/"))
        self.assertTrue(plist.endswith("{{ github_runner_cluster_runtime_launchd_label }}.plist"))

    def test_the_node_profile_starts_at_boot_and_any_other_at_login_unless_a_host_says_so(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_runtime_autostart_at"], "auto")
        self.assertEqual(DEFAULTS["github_runner_cluster_runtime_boot_profile"], "node")

    def test_the_daemon_is_in_the_system_launch_daemons_directory_and_owned_by_root(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_runtime_daemon_plist"], "/Library/LaunchDaemons/{{ github_runner_cluster_runtime_launchd_label }}.plist")
        self.assertEqual((DEFAULTS["github_runner_cluster_runtime_daemon_owner"], DEFAULTS["github_runner_cluster_runtime_daemon_group"]), ("root", "wheel"))

    def test_the_node_container_follows_the_node_s_role(self) -> None:
        template = DEFAULTS["github_runner_cluster_node_container"]
        self.assertIn("github_runner_cluster_container_name", template)
        self.assertIn("github_runner_cluster_agent_container_name", template)

    def test_the_stop_of_a_silent_runtime_has_its_own_switch_and_bounded_probes(self) -> None:
        self.assertIs(DEFAULTS["github_runner_cluster_runtime_force_stop_wedged"], True)
        self.assertGreater(DEFAULTS["github_runner_cluster_runtime_probe_attempts"], 1)
        self.assertGreater(DEFAULTS["github_runner_cluster_runtime_probe_timeout"], 0)
        self.assertGreater(DEFAULTS["github_runner_cluster_runtime_kill_grace"], 0)
        self.assertGreater(DEFAULTS["github_runner_cluster_runtime_boot_grace"], DEFAULTS["github_runner_cluster_runtime_probe_attempts"] * (DEFAULTS["github_runner_cluster_runtime_probe_timeout"] + DEFAULTS["github_runner_cluster_runtime_probe_delay"]))

    def test_every_new_variable_is_in_the_readme(self) -> None:
        readme = (ROLE / "README.md").read_text()
        for name in ("github_runner_cluster_runtime_force_stop_wedged", "github_runner_cluster_runtime_probe_attempts", "github_runner_cluster_runtime_probe_delay", "github_runner_cluster_runtime_probe_timeout", "github_runner_cluster_runtime_boot_grace", "github_runner_cluster_runtime_kill_grace", "github_runner_cluster_runtime_clean_stop_timeout", "github_runner_cluster_runtime_autostart", "github_runner_cluster_runtime_autostart_at", "github_runner_cluster_runtime_boot_profile", "github_runner_cluster_runtime", "github_runner_cluster_colima_profile", "github_runner_cluster_runtime_path"):
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", readme)


class WatchdogDefaultsTest(unittest.TestCase):
    def test_the_watchdog_is_on_with_a_cap_and_a_backoff(self) -> None:
        self.assertIs(DEFAULTS["github_runner_cluster_runtime_watchdog"], True)
        self.assertGreaterEqual(DEFAULTS["github_runner_cluster_runtime_watchdog_failures"], 2)
        self.assertGreaterEqual(DEFAULTS["github_runner_cluster_runtime_watchdog_max_restarts"], 1)
        self.assertGreater(DEFAULTS["github_runner_cluster_runtime_watchdog_min_gap"], 0)

    def test_the_watchdog_has_a_label_per_profile_and_a_directory_under_the_home(self) -> None:
        self.assertIn("{{ github_runner_cluster_effective_runtime.colima_profile }}", DEFAULTS["github_runner_cluster_runtime_watchdog_label"])
        self.assertNotEqual(DEFAULTS["github_runner_cluster_runtime_watchdog_label"], DEFAULTS["github_runner_cluster_runtime_launchd_label"])
        self.assertTrue(DEFAULTS["github_runner_cluster_runtime_watchdog_dir"].startswith("{{ ansible_facts['env']['HOME'] }}/"))


class WatchdogPlistTest(unittest.TestCase):
    @staticmethod
    def render(**role_vars: Any) -> dict[str, Any]:
        return plistlib.loads(render_plist(plist_template="colima-watchdog-launchd.plist.j2", **role_vars))

    def test_it_runs_the_script_every_interval_and_once_at_load_without_keeping_it_alive(self) -> None:
        plist = self.render()
        self.assertEqual(plist["Label"], "com.exadev.github-runner.colima-watchdog.default")
        self.assertEqual(plist["ProgramArguments"], ["/bin/bash", f"{HOME}/.local/share/exadev-github-runner/colima-watchdog-default/colima-watchdog.sh"])
        self.assertEqual(plist["StartInterval"], DEFAULTS["github_runner_cluster_runtime_watchdog_interval"])
        self.assertIs(plist["RunAtLoad"], True)
        self.assertNotIn("KeepAlive", plist)
        # The VM a restart starts is detached from the job and must outlive it.
        self.assertIs(plist["AbandonProcessGroup"], True)
        self.assertEqual(plist["StandardOutPath"], f"{HOME}/Library/Logs/com.exadev.github-runner.colima-watchdog.default.log")

    def test_it_hands_the_script_its_profile_state_directory_and_limits(self) -> None:
        environment = self.render(github_runner_cluster_runtime_watchdog_max_restarts=2, github_runner_cluster_runtime_environment={"PATH": "/p", "DOCKER_CONTEXT": "colima-node", "COLIMA_HOME": "/c"}, docker_context="colima-node")["EnvironmentVariables"]
        self.assertEqual(environment["WATCHDOG_PROFILE"], "node")
        self.assertEqual(environment["WATCHDOG_STATE_DIR"], f"{HOME}/.local/share/exadev-github-runner/colima-watchdog-node/state")
        self.assertEqual(environment["WATCHDOG_MAX_RESTARTS"], "2")
        self.assertEqual((environment["PATH"], environment["DOCKER_CONTEXT"], environment["COLIMA_HOME"], environment["HOME"]), ("/p", "colima-node", "/c", HOME))
        for name in ("FAILURES", "PROBE_TIMEOUT", "KILL_GRACE", "BOOT_GRACE", "CLEAN_STOP_TIMEOUT", "START_TIMEOUT", "MIN_GAP"):
            self.assertRegex(environment[f"WATCHDOG_{name}"], r"^[0-9]+$")

    def test_the_boot_profile_gets_a_daemon_that_runs_as_the_connecting_user(self) -> None:
        self.assertEqual(self.render(docker_context="colima-node")["UserName"], "example")
        self.assertNotIn("UserName", self.render())


class GuardTest(unittest.TestCase):
    def test_the_vm_needs_the_module_and_both_bridge_sysctls_on(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_colima_kernel_module"], "br_netfilter")
        self.assertEqual(DEFAULTS["github_runner_cluster_colima_sysctl_settings"], {"net.bridge.bridge-nf-call-iptables": 1, "net.bridge.bridge-nf-call-ip6tables": 1})
        self.assertEqual(DEFAULTS["github_runner_cluster_colima_bridge_proc_dir"], "/proc/sys/net/bridge")
        self.assertEqual(DEFAULTS["github_runner_cluster_colima_modules_load_file"], MODULES_FILE)
        self.assertEqual(DEFAULTS["github_runner_cluster_colima_sysctl_file"], SYSCTL_FILE)

    def test_the_vm_is_prepared_last_in_the_runtime_tasks_and_only_for_colima_once_it_answers(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "runtime.yml").read_text())
        names = [task.get("ansible.builtin.include_tasks") or ("fail" if "ansible.builtin.fail" in task else None) for task in tasks]
        self.assertEqual(names[-4:], ["fail", "colima_kernel.yml", "colima_binfmt.yml", "colima_route_guard.yml"])
        for task in tasks[-3:]:
            self.assertEqual(task["when"], ["github_runner_cluster_effective_runtime.kind == 'colima'", "github_runner_cluster_runtime_up | bool"])
            self.assertNotIn("github_runner_cluster_runtime_autostart", json.dumps(task))

    def test_the_readmes_say_what_the_role_does_to_the_vm(self) -> None:
        readme = (ROLE / "README.md").read_text()
        self.assertIn("**Colima kernel.**", readme)
        self.assertIn(MODULES_FILE, readme)
        self.assertIn(SYSCTL_FILE, readme)
        self.assertIn("colima_kernel.yml", (ROOT / "docs" / "non-obvious-behaviour.md").read_text())
        self.assertIn("**Colima 32-bit programs.**", readme)
        self.assertIn("colima_binfmt.yml", readme)
        self.assertIn("colima_binfmt.yml", (ROOT / "docs" / "non-obvious-behaviour.md").read_text())

    def test_both_includes_run_on_macos_only(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        blocks = [task for task in tasks if "block" in task]
        everything = tasks + [inner for block in blocks for inner in block["block"]]
        for tasks_file in ("runtime.yml", "end_checks.yml"):
            includes = [task for task in everything if task.get("ansible.builtin.include_tasks") == tasks_file]
            with self.subTest(tasks_file=tasks_file):
                self.assertTrue(includes)
                for include in includes:
                    self.assertEqual(include["when"], DARWIN_ONLY)

    def test_the_operating_system_is_read_before_either_condition_uses_it(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        order = [task.get("ansible.builtin.include_tasks") for task in tasks]
        self.assertLess(order.index("host_os.yml"), order.index("runtime.yml"))
        self.assertNotIn("when", tasks[order.index("host_os.yml")])

    def test_the_runtime_tasks_gather_the_user_and_environment_they_use_when_the_caller_did_not(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "runtime.yml").read_text())
        gather = tasks[0]["ansible.builtin.setup"]
        self.assertEqual(gather["gather_subset"], ["!all", "!min", "user", "env"])
        self.assertEqual(tasks[0]["when"], "'user_uid' not in ansible_facts or 'env' not in ansible_facts")
        used = "".join(path.read_text() for path in sorted((ROLE / "tasks").glob("runtime*.yml"))) + (ROLE / "defaults" / "main.yml").read_text() + (ROLE / "templates" / "colima-launchd.plist.j2").read_text()
        # user_id is in the user subset gathered above, with user_uid.
        self.assertEqual(set(re.findall(r"ansible_facts\['(\w+)'\]", used)) - {"system"}, {"user_uid", "user_id", "env"})

    def test_the_runtime_is_up_before_anything_reads_the_daemon(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        order = [task.get("ansible.builtin.include_tasks") or task.get("ansible.builtin.command") for task in tasks]
        self.assertLess(order.index("runtime.yml"), order.index("versions.yml"))
        self.assertLess(order.index("runtime.yml"), order.index("running_version.yml"))
        self.assertLess(order.index("docker_context.yml"), order.index("runtime.yml"))

    def test_the_end_checks_follow_the_compose_start_on_servers_and_agents(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        for role in ("server", "agent"):
            [block] = [task for task in tasks if "block" in task and task.get("when") == f'github_runner_cluster_docker_role == "{role}"']
            # A server starts through its turn (tasks/start_servers.yml) and an agent through tasks/start_agent.yml, each of which runs the Compose start.
            steps = [("compose" if step.get("ansible.builtin.include_tasks") in ("start_servers.yml", "start_agent.yml") else step.get("ansible.builtin.include_tasks")) or ("compose" if "community.docker.docker_compose_v2" in step else None) for step in block["block"]]
            with self.subTest(role=role):
                self.assertLess(steps.index("compose"), steps.index("end_checks.yml"))


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class NoGatheredFactsTest(unittest.TestCase):
    """The mesh integration tests' plays gather no facts, so the macOS condition must not depend on any the caller gathered."""

    def run_play(self, *extra: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as work:
            return subprocess.run([ANSIBLE_PLAYBOOK, str(HOST_OS), *extra], capture_output=True, text=True, check=False, env=ansible_env(), cwd=work)

    def test_the_play_uses_the_condition_main_yml_uses(self) -> None:
        play = yaml.safe_load(HOST_OS.read_text())[0]
        self.assertIn(DARWIN_ONLY, play["tasks"][1]["ansible.builtin.debug"]["msg"])

    def test_the_condition_evaluates_after_the_role_reads_the_operating_system(self) -> None:
        result = self.run_play()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"mac={platform.system() == 'Darwin'}", result.stdout)

    def test_the_same_play_fails_without_those_tasks(self) -> None:
        # What every mesh scenario hit before the role read the operating system itself.
        result = self.run_play("-e", "read_host_os=false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no attribute 'system'", result.stdout + result.stderr)


class PlistTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = render_plist()
        cls.plist = plistlib.loads(cls.document)

    def test_it_starts_the_default_profile_without_changing_the_current_docker_context(self) -> None:
        self.assertEqual(self.plist["ProgramArguments"], ["/usr/bin/env", "colima", "start", "--profile", "default", "--activate=false"])

    def test_it_runs_once_at_login(self) -> None:
        self.assertIs(self.plist["RunAtLoad"], True)
        self.assertNotIn("KeepAlive", self.plist)

    def test_the_vm_outlives_the_job_that_started_it(self) -> None:
        # colima start returns once the VM is up, leaving lima's host agent running; launchd kills what a finished job leaves in its process group unless told to abandon it.
        self.assertIs(self.plist["AbandonProcessGroup"], True)

    def test_the_path_is_the_roles_with_both_homebrew_prefixes(self) -> None:
        self.assertEqual(self.plist["EnvironmentVariables"], {"PATH": DEFAULTS["github_runner_cluster_runtime_path"]})

    def test_the_label_names_the_profile_and_the_log_is_in_the_users_logs(self) -> None:
        self.assertEqual(self.plist["Label"], "com.exadev.github-runner.colima.default")
        self.assertEqual(self.plist["StandardOutPath"], f"{HOME}/Library/Logs/com.exadev.github-runner.colima.default.log")
        self.assertEqual(self.plist["StandardErrorPath"], self.plist["StandardOutPath"])

    def test_another_profile_gets_its_own_label_and_argument(self) -> None:
        plist = plistlib.loads(render_plist(docker_context="colima-example"))
        self.assertEqual(plist["Label"], "com.exadev.github-runner.colima.example")
        self.assertEqual(plist["ProgramArguments"][3:5], ["--profile", "example"])

    def test_a_profile_set_for_a_context_that_does_not_say_is_used(self) -> None:
        plist = plistlib.loads(render_plist(docker_context="", github_runner_cluster_runtime="colima", github_runner_cluster_colima_profile="build"))
        self.assertEqual((plist["Label"], plist["ProgramArguments"][4]), ("com.exadev.github-runner.colima.build", "build"))

    def test_a_profile_in_a_home_of_its_own_gets_that_home_in_the_agent(self) -> None:
        plist = plistlib.loads(render_plist(docker_context="colima-example", github_runner_cluster_runtime_colima_home="/Users/example/.colima-example"))
        self.assertEqual(plist["EnvironmentVariables"], {"PATH": DEFAULTS["github_runner_cluster_runtime_path"], "COLIMA_HOME": "/Users/example/.colima-example"})

    def test_a_path_with_markup_characters_is_escaped_not_injected(self) -> None:
        path = "/opt/a&b:/opt/<c>:/usr/bin"
        document = render_plist(github_runner_cluster_runtime_path=path)
        self.assertEqual(plistlib.loads(document)["EnvironmentVariables"]["PATH"], path)
        self.assertNotIn(b"a&b", document)

    @unittest.skipUnless(shutil.which("plutil"), "plutil is not installed")
    def test_plutil_accepts_it(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".plist") as handle:
            handle.write(self.document)
            handle.flush()
            result = subprocess.run(["plutil", "-lint", handle.name], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_login_agent_names_no_user_and_sets_no_home(self) -> None:
        # An agent runs as the user whose session loads it, and launchd ignores UserName for one; nothing of the daemon's keys leaks into it.
        self.assertNotIn("UserName", self.plist)
        self.assertNotIn("HOME", self.plist["EnvironmentVariables"])

    def test_the_node_profile_gets_a_daemon_that_runs_as_the_connecting_user_with_its_home(self) -> None:
        # A daemon is root's unless UserName says otherwise, which would start the VM as root; HOME is set because launchd.plist(5) does not promise one for a daemon and colima and lima take their home directory (the default COLIMA_HOME and the Lima instance directory) from $HOME.
        plist = plistlib.loads(render_plist(docker_context="colima-node", github_runner_cluster_runtime_colima_home="/Users/example/.colima-node"))
        self.assertEqual(plist["Label"], "com.exadev.github-runner.colima.node")
        self.assertEqual(plist["UserName"], "example")
        self.assertEqual(plist["EnvironmentVariables"], {"PATH": DEFAULTS["github_runner_cluster_runtime_path"], "HOME": HOME, "COLIMA_HOME": "/Users/example/.colima-node"})
        self.assertEqual(plist["ProgramArguments"], ["/usr/bin/env", "colima", "start", "--profile", "node", "--activate=false"])
        self.assertIs(plist["RunAtLoad"], True)
        self.assertNotIn("KeepAlive", plist)
        self.assertIs(plist["AbandonProcessGroup"], True)
        self.assertEqual(plist["StandardOutPath"], f"{HOME}/Library/Logs/com.exadev.github-runner.colima.node.log")

    def test_a_host_can_choose_either_kind_for_any_profile(self) -> None:
        self.assertEqual(plistlib.loads(render_plist(github_runner_cluster_runtime_autostart_at="boot"))["UserName"], "example")
        self.assertNotIn("UserName", plistlib.loads(render_plist(docker_context="colima-node", github_runner_cluster_runtime_autostart_at="login")))
        self.assertEqual(plistlib.loads(render_plist(github_runner_cluster_runtime_boot_profile="example", docker_context="colima-example"))["UserName"], "example")

    def test_a_user_name_with_markup_characters_is_escaped_not_injected(self) -> None:
        document = render_plist(docker_context="colima-node", ansible_facts={"user_id": "a&b<c>", "env": {"HOME": HOME}})
        self.assertEqual(plistlib.loads(document)["UserName"], "a&b<c>")

    @unittest.skipUnless(shutil.which("plutil"), "plutil is not installed")
    def test_plutil_accepts_the_daemon(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".plist") as handle:
            handle.write(render_plist(docker_context="colima-node"))
            handle.flush()
            result = subprocess.run(["plutil", "-lint", handle.name], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_daemons_comment_cannot_end_the_document_early_either(self) -> None:
        comment = re.search(rb"<!--(.*?)-->", render_plist(docker_context="colima-node"), re.DOTALL)
        assert comment is not None
        self.assertNotIn(b"--", comment.group(1))

    def test_the_comment_cannot_end_the_document_early(self) -> None:
        # A double hyphen inside an XML comment is not well formed.
        comment = re.search(rb"<!--(.*?)-->", self.document, re.DOTALL)
        assert comment is not None
        self.assertNotIn(b"--", comment.group(1))


class BoundedRunTest(unittest.TestCase):
    """files/run_bounded.pl, which gives probes, the guest command and the clean stop a deadline that holds whatever the command does with signals."""

    SCRIPT = ROLE / "files" / "run_bounded.pl"

    def run_bounded(self, deadline: float, grace: float, *command: str) -> tuple[subprocess.CompletedProcess[str], float]:
        began = time.monotonic()
        result = subprocess.run(["perl", str(self.SCRIPT), str(deadline), str(grace), *command], capture_output=True, text=True, check=False, timeout=60)
        return result, time.monotonic() - began

    def test_the_commands_status_and_output_pass_through(self) -> None:
        result, _ = self.run_bounded(10, 1, "sh", "-c", "echo out; echo err >&2; exit 3")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "out\n", "err\n"))

    def test_a_signal_that_ends_the_command_is_reported_as_the_shell_does(self) -> None:
        result, _ = self.run_bounded(10, 1, "sh", "-c", "kill -9 $$")
        self.assertEqual(result.returncode, 137)

    def test_a_command_that_ignores_sigalrm_is_ended_at_the_deadline(self) -> None:
        # What a Go program does with the timer a bare alarm would set.
        result, elapsed = self.run_bounded(1, 1, sys.executable, "-c", "import signal, time; signal.signal(signal.SIGALRM, signal.SIG_IGN); time.sleep(60)")
        self.assertEqual(result.returncode, 124)
        self.assertLess(elapsed, 10)

    def test_a_command_that_ignores_term_is_killed_after_the_grace(self) -> None:
        result, elapsed = self.run_bounded(1, 1, sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
        self.assertEqual(result.returncode, 124)
        self.assertGreaterEqual(elapsed, 2)
        self.assertLess(elapsed, 15)

    def test_what_the_command_started_does_not_outlive_the_deadline(self) -> None:
        marker = Path(tempfile.mkdtemp(prefix="bounded-")) / "pid"
        self.addCleanup(shutil.rmtree, marker.parent, True)
        result, _ = self.run_bounded(1, 1, "sh", "-c", f"trap '' TERM; sleep 60 & echo $! > {marker}; wait")
        self.assertEqual(result.returncode, 124)
        pid = int(marker.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        self.fail("a process the command started is still running")

    def test_a_supervisor_that_is_signalled_ends_its_command_and_what_it_started(self) -> None:
        # Ansible or a person killing the supervisor must not leave the hung command (a colima stop mid-shutdown, say) running on its own.
        for signal_name in ("TERM", "INT", "HUP"):
            with self.subTest(signal=signal_name):
                marker = Path(tempfile.mkdtemp(prefix="bounded-")) / "pids"
                self.addCleanup(shutil.rmtree, marker.parent, True)
                script = f"trap '' TERM INT HUP; sleep 60 & echo $$ $! > {marker}; wait"
                supervisor = subprocess.Popen(["perl", str(self.SCRIPT), "60", "1", "sh", "-c", script])
                for _ in range(100):
                    if marker.exists() and marker.read_text().strip():
                        break
                    time.sleep(0.05)
                pids = [int(word) for word in marker.read_text().split()]
                os.kill(supervisor.pid, getattr(signal, "SIG" + signal_name))
                self.assertEqual(supervisor.wait(timeout=30), 143)
                for pid in pids:
                    for _ in range(50):
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.1)
                    else:
                        self.fail(f"process {pid} survived the {signal_name} sent to the supervisor")

    def test_regression_guard_a_command_that_ends_before_the_deadline_reports_its_own_status(self) -> None:
        # A guard only: it passes on a supervisor with or without the final look at the child before the timeout path, because making a command end in the last poll interval is a race against the scheduler, which a test on a loaded runner cannot win reliably. The final look is read from the script instead.
        result, _ = self.run_bounded(1, 1, "sh", "-c", "sleep 0.1; exit 5")
        self.assertEqual(result.returncode, 5)
        self.assertIn("one last look", self.SCRIPT.read_text().lower())

    def test_the_deadline_is_measured_on_the_monotonic_clock(self) -> None:
        self.assertIn("CLOCK_MONOTONIC", self.SCRIPT.read_text())

    def test_a_command_that_cannot_be_started_exits_127(self) -> None:
        result, _ = self.run_bounded(5, 1, "/nonexistent/command")
        self.assertEqual(result.returncode, 127)


if __name__ == "__main__":
    unittest.main()
