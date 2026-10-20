"""Tests for the cluster role's memory-pressure regrading: the defaults, the checks in tasks/regrade.yml (run through the role with tests/cluster_versions/checks.yml), the settings the service reads (templates/regrade.env.j2), the regrade service in the Compose file, the levels the node's entrypoint starts k3s at, and the tasks that run or remove the service (tasks/regrade_service.yml) against a stand-in docker. The decision logic and the Docker actuator are tested in tests/regrade and run by tests/unit/test_regrade_shell.py."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_cluster"
FILES = ROLE / "files"
ENTRYPOINT = FILES / "k3s" / "node-entrypoint.sh"
COMPOSE = FILES / "docker-compose.yml"
ACTUATOR = FILES / "regrade" / "fleet-regrade-docker.sh"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())

ENABLED = "github_runner_cluster_regrade"
THRESHOLD = "github_runner_cluster_regrade_pressure_threshold"
FLOOR = "github_runner_cluster_regrade_available_floor_percent"
COOLDOWN = "github_runner_cluster_regrade_cooldown_seconds"
LEASE = "github_runner_cluster_regrade_lease_duration_seconds"
TIMEOUT = "github_runner_cluster_regrade_operation_timeout_seconds"
WINDOW = "github_runner_cluster_regrade_pressure_window_seconds"
STEP_UP = "github_runner_cluster_regrade_step_up"


def ansible_env() -> dict[str, str]:
    return {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_RETRY_FILES_ENABLED": "false"}


def rendered(defaults: dict[str, Any], key: str, **overrides: Any) -> int:
    """A numeric role default with its Jinja expression worked out, from the other defaults and any overrides: every template is rendered again from its source against the values of the last pass until nothing changes, which settles defaults written in terms of one another."""
    from jinja2 import Environment

    environment = Environment()
    # Only the regrading settings: the other defaults use Ansible's own filters, which plain Jinja does not have.
    sources = {name: value for name, value in {**defaults, **overrides}.items() if name.startswith(ENABLED)}
    values = dict(sources)
    for _ in range(len(sources)):
        settled = {name: environment.from_string(value).render(values) if isinstance(value, str) and "{{" in value else value for name, value in sources.items()}
        if settled == values:
            break
        values = settled
    return int(values[key])


class DefaultsTest(unittest.TestCase):
    def test_regrading_is_off_by_default(self) -> None:
        self.assertIs(DEFAULTS[ENABLED], False)

    def test_a_server_that_regrades_steps_back_up_by_itself_unless_told_not_to(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_regrade_step_up"], "automatic")

    def test_every_time_follows_from_the_pressure_window_and_the_sample_period(self) -> None:
        window = DEFAULTS[WINDOW]
        self.assertEqual(window, 300, "the longest average the kernel reports is avg300")
        sample = rendered(DEFAULTS, "github_runner_cluster_regrade_sample_seconds")
        self.assertEqual(sample, window // 10)
        self.assertEqual(rendered(DEFAULTS, "github_runner_cluster_regrade_sustain_seconds"), 4 * sample)
        self.assertEqual(rendered(DEFAULTS, COOLDOWN), 3 * window)
        self.assertEqual(rendered(DEFAULTS, TIMEOUT), window)
        self.assertEqual(rendered(DEFAULTS, LEASE), 3 * window)

    def test_the_times_follow_the_window_if_it_changes(self) -> None:
        self.assertEqual(rendered(DEFAULTS, COOLDOWN, **{WINDOW: 600}), 1800)
        self.assertEqual(rendered(DEFAULTS, "github_runner_cluster_regrade_sample_seconds", **{WINDOW: 600}), 60)

    def test_the_cooldown_exceeds_the_window_and_the_lease_covers_a_whole_step(self) -> None:
        self.assertGreaterEqual(rendered(DEFAULTS, COOLDOWN), DEFAULTS[WINDOW])
        self.assertGreaterEqual(rendered(DEFAULTS, LEASE), 3 * rendered(DEFAULTS, TIMEOUT))

    def test_the_taint_is_the_standard_one_with_the_effect_the_core_removes(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_regrade_taint"], "node-role.kubernetes.io/control-plane=true:NoSchedule")

    def test_every_variable_is_in_the_readme(self) -> None:
        readme = (ROLE / "README.md").read_text()
        for name in (key for key in DEFAULTS if key.startswith(ENABLED)):
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", readme)


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class ChecksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)

    def check(self, hosts: dict[str, dict[str, Any]] | None = None, **variables: Any) -> subprocess.CompletedProcess[str]:
        """Run the topology and regrade checks on a group of hosts (three servers unless ``hosts`` says otherwise), with ``variables`` as group variables."""
        inventory = self.work / "inventory.yml"
        group = {"vars": variables, "hosts": hosts or {"node1": {}, "node2": {}, "node3": {}}}
        inventory.write_text(yaml.safe_dump({"all": {"children": {"github_runner_cluster": group}}}))
        args = [ANSIBLE_PLAYBOOK, "-i", str(inventory), str(ROOT / "tests" / "cluster_versions" / "checks.yml"), "-e", "check_tasks=topology,regrade"]
        return subprocess.run(args, capture_output=True, text=True, check=False, env=ansible_env(), cwd=self.work)

    def refused(self, message: str, hosts: dict[str, dict[str, Any]] | None = None, **variables: Any) -> subprocess.CompletedProcess[str]:
        result = self.check(hosts, **variables)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, " ".join(result.stdout.split()))
        self.assertIn("Nothing was changed", " ".join(result.stdout.split()))
        return result

    def test_off_passes_whatever_the_settings(self) -> None:
        result = self.check(**{THRESHOLD: "not a number", COOLDOWN: 1})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_on_with_the_defaults_passes_for_three_servers(self) -> None:
        result = self.check(**{ENABLED: True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_value_that_is_not_true_or_false_is_refused(self) -> None:
        for value in ("yes please", "", 2):
            with self.subTest(value=value):
                self.refused("must be true or false", **{ENABLED: value})

    def test_an_agent_is_refused(self) -> None:
        # Four hosts, so the fourth is an agent; the setting is given to every host.
        result = self.refused("node4 is an agent", {name: {} for name in ("node1", "node2", "node3", "node4")}, **{ENABLED: True})
        self.assertEqual(re.findall(r"fatal: \[(node\d)\]", result.stdout), ["node4"])

    def test_regrading_only_the_servers_of_a_group_with_an_agent_passes(self) -> None:
        hosts = {"node1": {ENABLED: True}, "node2": {ENABLED: True}, "node3": {ENABLED: True}, "node4": {}}
        result = self.check(hosts)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_cluster_with_one_server_is_refused(self) -> None:
        self.refused("is the only server", {"node1": {}}, **{ENABLED: True})

    def test_the_bootstrap_server_of_the_in_cluster_headscale_mesh_is_refused_and_the_others_are_not(self) -> None:
        result = self.refused("bootstrap server of a headscale_in_cluster mesh", **{ENABLED: True, "github_runner_cluster_mesh": "headscale_in_cluster"})
        self.assertEqual(re.findall(r"fatal: \[(node\d)\]", result.stdout), ["node1"])

    def test_a_setting_that_is_not_a_positive_whole_number_is_refused(self) -> None:
        for name in (COOLDOWN, TIMEOUT, LEASE, FLOOR, "github_runner_cluster_regrade_sample_seconds", "github_runner_cluster_regrade_sustain_seconds"):
            for value in ("abc", "1.5", 0, -3, "010"):
                with self.subTest(name=name, value=value):
                    self.refused(f"{name} is '{value}'; it must be a positive whole number", **{ENABLED: True, name: value})

    def test_stepping_up_is_automatic_or_manual(self) -> None:
        for value in ("automatic", "manual"):
            with self.subTest(value=value):
                result = self.check(**{ENABLED: True, STEP_UP: value})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for value in ("auto", "", "never", True):
            with self.subTest(value=value):
                self.refused("must be automatic or manual", **{ENABLED: True, STEP_UP: value})

    def test_the_pressure_threshold_is_a_percentage(self) -> None:
        for value in (0, 10, "12.5", 100):
            with self.subTest(value=value):
                result = self.check(**{ENABLED: True, THRESHOLD: value})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for value in ("abc", -1, 100.5, "5%"):
            with self.subTest(value=value):
                self.refused("must be a percentage", **{ENABLED: True, THRESHOLD: value})

    def test_the_floor_is_a_share_of_memory(self) -> None:
        self.refused("must be at most 100", **{ENABLED: True, FLOOR: 101})

    def test_a_cooldown_under_the_pressure_window_is_refused(self) -> None:
        self.refused("must be at least github_runner_cluster_regrade_pressure_window_seconds", **{ENABLED: True, COOLDOWN: DEFAULTS[WINDOW] - 1})
        result = self.check(**{ENABLED: True, COOLDOWN: DEFAULTS[WINDOW]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_lease_that_can_run_out_during_a_step_is_refused(self) -> None:
        timeout = rendered(DEFAULTS, TIMEOUT)
        self.refused("must be at least three times github_runner_cluster_regrade_operation_timeout_seconds", **{ENABLED: True, LEASE: 3 * timeout - 1})
        result = self.check(**{ENABLED: True, LEASE: 3 * timeout})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class SettingsFileTest(unittest.TestCase):
    def render(self, *extra: str, host: str = "node2") -> str:
        with tempfile.TemporaryDirectory() as work:
            output = Path(work) / "fleet-regrade.env"
            args = [ANSIBLE_PLAYBOOK, "-i", f"{host},", str(ROOT / "tests" / "regrade" / "render_env.yml"), "-e", f"env_output={output}", *extra]
            result = subprocess.run(args, capture_output=True, text=True, check=False, env=ansible_env(), cwd=work)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return output.read_text()

    def sourced(self, text: str, *names: str) -> dict[str, str]:
        """What a shell that sources the file, as the core does, makes of these variables."""
        script = "set -eu\n. /dev/stdin <<'ENV'\n" + text + "ENV\n" + "".join(f'printf "%s\\0" "${{{name}}}"\n' for name in names)
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(zip(names, result.stdout.split("\0"), strict=False))

    def test_the_core_reads_every_setting_it_needs(self) -> None:
        core = (FILES / "regrade" / "fleet-regrade-core.sh").read_text()
        referenced = set(re.findall(r"\$\{?([A-Z][A-Z_]+)\b", core))
        assigned = set(re.findall(r"\b([A-Z][A-Z_]+)=", core))
        # What the core takes from the environment: referenced, never assigned by the core itself, and not a bash variable. The paths and names with a default (PSI_FILE and the like) are assigned with one, so they are not needed here.
        needed = referenced - assigned - {"SECONDS", "BASH_SOURCE"}
        provided = {line.split("=", 1)[0] for line in self.render().splitlines() if re.match(r"[A-Z_]+=", line)}
        self.assertEqual(sorted(needed - provided), [])
        self.assertIn("PRESSURE_THRESHOLD", needed, "the check finds the settings at all")

    def test_the_values_are_the_role_variables(self) -> None:
        values = self.sourced(self.render(), "PRESSURE_THRESHOLD", "AVAILABLE_FLOOR_PERCENT", "SAMPLE_SECONDS", "SUSTAIN_SECONDS", "COOLDOWN_SECONDS", "OPERATION_TIMEOUT_SECONDS", "LEASE_DURATION_SECONDS", "NODE_NAME", "STEP_UP")
        self.assertEqual(
            values,
            {"PRESSURE_THRESHOLD": "10", "AVAILABLE_FLOOR_PERCENT": "15", "SAMPLE_SECONDS": "30", "SUSTAIN_SECONDS": "120", "COOLDOWN_SECONDS": "900", "OPERATION_TIMEOUT_SECONDS": "300", "LEASE_DURATION_SECONDS": "900", "NODE_NAME": "node2", "STEP_UP": "automatic"},
        )

    def test_stepping_up_by_hand_reaches_the_file(self) -> None:
        self.assertEqual(self.sourced(self.render("-e", f"{STEP_UP}=manual"), "STEP_UP"), {"STEP_UP": "manual"})

    def test_a_changed_setting_reaches_the_file(self) -> None:
        values = self.sourced(self.render("-e", f"{THRESHOLD}=25", "-e", f"{FLOOR}=20"), "PRESSURE_THRESHOLD", "AVAILABLE_FLOOR_PERCENT")
        self.assertEqual(values, {"PRESSURE_THRESHOLD": "25", "AVAILABLE_FLOOR_PERCENT": "20"})

    def test_kubectl_is_the_one_in_the_nodes_container_and_stays_one_command_for_the_core(self) -> None:
        values = self.sourced(self.render("-e", "github_runner_cluster_container_name=custom-k3s"), "KUBECTL", "NODE_CONTAINER", "KUBECONFIG_PATH", "CONTROL_PLANE_TAINT")
        self.assertEqual(values["KUBECTL"], "docker exec -i custom-k3s kubectl")
        self.assertEqual(values["NODE_CONTAINER"], "custom-k3s")
        self.assertEqual(values["KUBECONFIG_PATH"], "/etc/rancher/k3s/k3s.yaml")
        self.assertEqual(values["CONTROL_PLANE_TAINT"], "node-role.kubernetes.io/control-plane=true:NoSchedule")

    def test_the_api_it_uses_is_another_servers_never_its_own(self) -> None:
        for host, other in (("node1", "node2"), ("node2", "node1"), ("node3", "node1")):
            with self.subTest(host=host):
                values = self.sourced(self.render(host=host), "KUBE_SERVER")
                self.assertEqual(values["KUBE_SERVER"], f"https://{other}.example.internal:6443")

    def test_it_follows_the_api_port(self) -> None:
        values = self.sourced(self.render("-e", "github_runner_cluster_api_port=7443"), "KUBE_SERVER")
        self.assertTrue(values["KUBE_SERVER"].endswith(":7443"))


class ComposeServiceTest(unittest.TestCase):
    def test_the_service_has_a_profile_of_its_own_so_the_server_and_agent_profiles_never_start_it(self) -> None:
        service = yaml.safe_load(COMPOSE.read_text())["services"]["regrade"]
        self.assertEqual(service["profiles"], ["regrade"])
        for name, other in yaml.safe_load(COMPOSE.read_text())["services"].items():
            if name != "regrade":
                with self.subTest(service=name):
                    self.assertNotIn("regrade", other["profiles"])

    def test_it_is_built_from_the_directory_it_mounts_and_is_kept_alive(self) -> None:
        service = yaml.safe_load(COMPOSE.read_text())["services"]["regrade"]
        self.assertEqual(service["build"], {"context": "./regrade"})
        self.assertIn("./regrade:/usr/local/lib/fleet-regrade:ro", service["volumes"])
        self.assertEqual(service["restart"], "unless-stopped")
        self.assertLess(service["oom_score_adj"], 0)
        self.assertGreater(service["oom_score_adj"], -1000)

    def test_its_only_access_beyond_its_own_directory_is_the_docker_socket(self) -> None:
        service = yaml.safe_load(COMPOSE.read_text())["services"]["regrade"]
        self.assertEqual(sorted(service["volumes"]), sorted(["/var/run/docker.sock:/var/run/docker.sock", "./regrade:/usr/local/lib/fleet-regrade:ro"]))
        for key in ("privileged", "network_mode", "pid", "cap_add", "devices"):
            self.assertNotIn(key, service)

    def test_the_k3s_service_is_unchanged_by_it(self) -> None:
        # The level lives in a file the entrypoint reads, so enabling regrading changes nothing a container is created from.
        k3s = yaml.safe_load(COMPOSE.read_text())["services"]["k3s"]
        self.assertEqual([entry for entry in k3s["environment"] if "REGRADE" in entry.upper()], [])
        self.assertEqual([entry for entry in k3s["volumes"] if "regrade" in entry], [])

    def test_the_service_files_are_where_the_mount_and_the_image_expect_them(self) -> None:
        directory = FILES / "regrade"
        dockerfile = (directory / "Dockerfile").read_text()
        self.assertIn("/usr/local/lib/fleet-regrade/fleet-regrade.sh", dockerfile)
        for name in ("fleet-regrade.sh", "fleet-regrade-core.sh", "fleet-regrade-docker.sh"):
            self.assertTrue((directory / name).is_file(), name)
        self.assertTrue(os.access(directory / "fleet-regrade.sh", os.X_OK))
        tasks = (ROLE / "tasks" / "regrade_service.yml").read_text()
        for name in ("Dockerfile", "fleet-regrade.sh", "fleet-regrade-core.sh", "fleet-regrade-docker.sh"):
            self.assertIn(f"name: {name}", tasks)


def has_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode == 0


@unittest.skipUnless(has_compose(), "docker compose is not installed")
class ComposeRenderTest(unittest.TestCase):
    def config(self, *profiles: str) -> dict[str, Any]:
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("DOCKER_", "COMPOSE_", "K3S_", "TAILSCALE_"))}
        base = {"K3S_TOKEN": "t", "K3S_TLS_SAN_LIST": "n", "K3S_VERSION": "v1.34.1-k3s1", "TAILSCALE_VERSION": "v1.102.4"}
        args = ["docker", "compose", "--project-directory", tempfile.gettempdir(), "--project-name", "example", "-f", str(COMPOSE), *[arg for profile in profiles for arg in ("--profile", profile)], "config", "--format", "json"]
        result = subprocess.run(args, capture_output=True, text=True, check=False, env=clean | base)
        self.assertEqual(result.returncode, 0, result.stderr)
        services: dict[str, Any] = json.loads(result.stdout)["services"]
        return services

    def test_the_server_and_agent_profiles_do_not_include_it(self) -> None:
        self.assertNotIn("regrade", self.config("server", "agent"))

    def test_its_own_profile_includes_only_it(self) -> None:
        self.assertEqual(list(self.config("regrade")), ["regrade"])

    def test_the_rendered_service_reads_its_settings_from_the_mounted_directory(self) -> None:
        service = self.config("regrade")["regrade"]
        self.assertEqual(service["environment"]["FLEET_REGRADE_ENV"], "/usr/local/lib/fleet-regrade/fleet-regrade.env")
        self.assertEqual(service["oom_score_adj"], -900)


def function_source(name: str) -> str:
    """The definition of a shell function in the entrypoint, from its first line to the closing brace at the start of a line."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", ENTRYPOINT.read_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, f"{name} is not defined in node-entrypoint.sh"
    return match.group(0)


def assignments(names: list[str]) -> str:
    """The top-level assignments of the entrypoint for these variables, each on one line."""
    text = ENTRYPOINT.read_text()
    lines = []
    for name in names:
        match = re.search(rf"^{name}=.*$", text, re.MULTILINE)
        assert match is not None, f"{name} is not assigned in node-entrypoint.sh"
        lines.append(match.group(0))
    return "\n".join(lines)


class EntrypointTest(unittest.TestCase):
    """Runs the entrypoint's own functions in a shell, without Tailscale or k3s: the script starts tailscaled and execs k3s at top level, so only the assignments and functions that decide the level are taken from it."""

    ETCD_ONLY = ["--disable-apiserver", "--disable-controller-manager", "--disable-scheduler", "--disable-agent"]

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.level_file = self.work / "regrade-level"
        self.started_file = self.work / "started"

    def run_script(self, *body: str, **env: str) -> subprocess.CompletedProcess[str]:
        script = "\n".join(
            [
                "set -u",
                'vpn_auth="name=tailscale,joinKey=example"',
                'mesh_ip="100.64.0.1"',
                'etcd_timing_flags=""',
                'api_port_flags=""',
                assignments(["regrade_level_file", "regrade_started_file", "regrade_flags"]),
                function_source("set_regrade_flags"),
                function_source("mesh_server_args"),
                *body,
            ]
        )
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("K3S_", "REGRADE_"))}
        return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=False, env=clean | {"REGRADE_LEVEL_FILE": str(self.level_file), "REGRADE_STARTED_FILE": str(self.started_file)} | env)

    def server_args(self, level: str | None, **env: str) -> list[str]:
        if level is not None:
            self.level_file.write_text(level + "\n")
        result = self.run_script("set_regrade_flags", "mesh_server_args", 'printf "%s\\n" "$server_args"', **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_a_server_that_was_never_stepped_down_starts_whole_and_says_so(self) -> None:
        args = self.server_args(None)
        self.assertEqual([arg for arg in args if arg in self.ETCD_ONLY], [])
        self.assertEqual(self.started_file.read_text(), "full\n")

    def test_full_and_control_plane_run_the_same_k3s(self) -> None:
        whole = self.server_args(None)
        for level in ("full", "control-plane"):
            with self.subTest(level=level):
                self.assertEqual(self.server_args(level), whole)
                self.assertEqual(self.started_file.read_text(), f"{level}\n")

    def test_etcd_only_turns_off_everything_but_etcd_after_the_other_arguments(self) -> None:
        args = self.server_args("etcd-only", K3S_TLS_SAN_LIST="a b", K3S_JOIN_SERVER_URL="https://bootstrap.example.internal:6443")
        self.assertEqual(args[-4:], self.ETCD_ONLY)
        self.assertIn("--server", args)
        self.assertEqual(args.count("--tls-san"), 2)
        self.assertEqual(self.started_file.read_text(), "etcd-only\n")

    def test_the_bootstrap_server_keeps_cluster_init_at_etcd_only(self) -> None:
        # Verified against a real cluster: the flag is ignored once etcd is initialised, so the bootstrap server steps down like the others.
        args = self.server_args("etcd-only")
        self.assertIn("--cluster-init", args)
        self.assertEqual(args[-4:], self.ETCD_ONLY)

    def test_the_level_is_read_at_every_start_not_remembered(self) -> None:
        self.assertEqual(self.server_args("etcd-only")[-4:], self.ETCD_ONLY)
        self.assertEqual([arg for arg in self.server_args("full") if arg in self.ETCD_ONLY], [])

    def test_the_arguments_without_a_record_are_what_they_were_before(self) -> None:
        self.assertEqual(
            self.server_args(None),
            ["--disable=traefik", "--disable-network-policy", "--write-kubeconfig-mode=6443", "--vpn-auth=name=tailscale,joinKey=example", "--resolv-conf=/etc/k3s-resolv.conf", "--node-ip=100.64.0.1", "--cluster-init"],
        )

    def test_the_flags_are_empty_until_the_start_that_reads_the_level(self) -> None:
        # The supervised start of a bootstrap server with an in-cluster Headscale server builds its arguments without reading a level, and set -u must not trip on that.
        self.level_file.write_text("etcd-only\n")
        result = self.run_script("mesh_server_args", 'printf "%s\\n" "$server_args"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([arg for arg in result.stdout.split() if arg in self.ETCD_ONLY], [])

    def test_only_the_plain_server_start_reads_the_level(self) -> None:
        text = ENTRYPOINT.read_text()
        self.assertEqual(len(re.findall(r"^\s+set_regrade_flags$", text, re.MULTILINE)), 1)
        server_branch = text[text.index("  server)\n") : text.index("  agent)\n")]
        self.assertLess(server_branch.index("set_regrade_flags"), server_branch.index("mesh_server_args"))

    def test_the_entrypoint_and_the_actuator_use_the_same_two_files(self) -> None:
        actuator = ACTUATOR.read_text()
        for variable, entry in (("LEVEL_FILE", "regrade_level_file"), ("STARTED_FILE", "regrade_started_file")):
            with self.subTest(file=variable):
                in_actuator = re.search(rf"^{variable}=\$\{{{variable}:-(.+)\}}$", actuator, re.MULTILINE)
                in_entrypoint = re.search(rf"^{entry}=\$\{{\w+:-(.+)\}}$", ENTRYPOINT.read_text(), re.MULTILINE)
                assert in_actuator is not None and in_entrypoint is not None
                self.assertEqual(in_actuator.group(1), in_entrypoint.group(1))

    def test_the_level_file_is_on_the_data_volume_and_the_record_of_what_runs_is_on_a_tmpfs(self) -> None:
        compose = yaml.safe_load(COMPOSE.read_text())["services"]["k3s"]
        level = re.search(r"^regrade_level_file=\$\{\w+:-(.+)\}$", ENTRYPOINT.read_text(), re.MULTILINE)
        started = re.search(r"^regrade_started_file=\$\{\w+:-(.+)\}$", ENTRYPOINT.read_text(), re.MULTILINE)
        assert level is not None and started is not None
        self.assertTrue(level.group(1).startswith("/var/lib/rancher/k3s/"), "the data volume")
        self.assertIn("k3s-data:/var/lib/rancher/k3s", compose["volumes"])
        self.assertTrue(any(started.group(1).startswith(path + "/") for path in compose["tmpfs"]), "a tmpfs the container recreates at every start")


class RoleTasksTest(unittest.TestCase):
    def test_the_settings_are_checked_before_the_role_changes_anything(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        order = [task.get("ansible.builtin.include_tasks") or task.get("name") for task in tasks]
        self.assertLess(order.index("topology.yml"), order.index("regrade.yml"))
        self.assertLess(order.index("regrade.yml"), order.index("Create this host's Compose project directory"))

    def test_the_service_runs_on_servers_after_the_node_is_started(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        [block] = [task for task in tasks if "block" in task and task.get("when") == 'github_runner_cluster_effective_node_role == "server"']
        steps = [step.get("ansible.builtin.include_tasks") for step in block["block"]]
        self.assertLess(steps.index("start_servers.yml"), steps.index("regrade_service.yml"))
        self.assertLess(steps.index("regrade_service.yml"), steps.index("end_checks.yml"))
        [agent] = [task for task in tasks if "block" in task and task.get("when") == 'github_runner_cluster_effective_node_role == "agent"']
        self.assertNotIn("regrade_service.yml", [step.get("ansible.builtin.include_tasks") for step in agent["block"]])

    def test_the_wait_for_ready_leaves_out_a_server_regrading_has_stepped_down(self) -> None:
        text = (ROLE / "tasks" / "main.yml").read_text()
        waits = re.findall(r"kubectl wait --for=condition=Ready node (.*)$", text, re.MULTILINE)
        self.assertEqual(len(waits), 2, "the wait and its retry")
        for wait in waits:
            self.assertIn("--selector '!{{ github_runner_cluster_regrade_level_label }}'", wait)
            self.assertNotIn("--all", wait)

    def test_the_self_healing_leaves_a_stepped_down_servers_node_password_alone(self) -> None:
        text = (ROLE / "tasks" / "main.yml").read_text()
        self.assertIn("github_runner_cluster_nodes_stepped_down.stdout_lines", text)
        self.assertLess(text.index("Read which nodes memory-pressure regrading has stepped down"), text.index("Pick the non-bootstrap hosts whose node has not rejoined"))

    def test_a_server_below_full_stops_the_play_before_any_host_removes_its_service(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "regrade_service.yml").read_text())
        [stop] = [task for task in tasks if task.get("name") == "Stop regrading"]
        steps = stop["block"]
        names = [step["name"] for step in steps]
        refusal = steps[names.index("Fail if the server is stepped down")]
        self.assertIs(refusal["any_errors_fatal"], True)
        self.assertLess(names.index("Fail if the server is stepped down"), names.index("Remove the regrade service's container"))

    def test_the_actuator_labels_the_node_with_the_key_the_role_selects_on(self) -> None:
        self.assertIn("LEVEL_LABEL={{ github_runner_cluster_regrade_level_label }}", (ROLE / "templates" / "regrade.env.j2").read_text())
        self.assertIn('"${LEVEL_LABEL}=$1"', ACTUATOR.read_text())
        self.assertTrue(DEFAULTS["github_runner_cluster_regrade_level_label"].startswith(DEFAULTS["github_runner_cluster_deployed_version_annotation"].split("/")[0] + "/"))


STUB_DOCKER = textwrap.dedent(
    """\
    #!/bin/sh
    # Records every call, answers `exec ... cat` with the level in $STUB_LEVEL (failing when it is unset, as an absent file or a stopped container does), and says there is no such container to `rm` unless $STUB_HAS_SERVICE is set.
    echo "$*" >> "$STUB_CALLS"
    case "$1" in
      exec) if [ -n "${STUB_LEVEL:-}" ]; then echo "$STUB_LEVEL"; else exit 1; fi ;;
      rm) if [ -n "${STUB_HAS_SERVICE:-}" ]; then echo "$3"; else echo "Error response from daemon: No such container: $3" >&2; exit 1; fi ;;
      *) echo "unexpected docker call: $*" >&2; exit 2 ;;
    esac
    """
)


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class StopRegradingTest(unittest.TestCase):
    """The tasks that run when regrading is off, against a stand-in docker."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        stub = self.work / "bin" / "docker"
        stub.parent.mkdir()
        stub.write_text(STUB_DOCKER)
        stub.chmod(0o755)
        self.calls = self.work / "calls"

    def run_tasks(self, *extra: str, **env: str) -> subprocess.CompletedProcess[str]:
        full_env = {**ansible_env(), "PATH": f"{self.work / 'bin'}:{os.environ['PATH']}", "STUB_CALLS": str(self.calls), **env}
        args = [ANSIBLE_PLAYBOOK, str(ROOT / "tests" / "regrade" / "service.yml"), "-e", f"{ENABLED}=false", *extra]
        return subprocess.run(args, capture_output=True, text=True, check=False, env=full_env, cwd=self.work)

    def docker_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_a_server_at_full_has_its_service_removed(self) -> None:
        result = self.run_tasks(STUB_LEVEL="full", STUB_HAS_SERVICE="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.docker_calls(), ["exec github-runner-k3s cat /run/regrade-level", "rm --force github-runner-regrade-1"])
        self.assertRegex(result.stdout, r"changed=1")

    def test_a_service_that_is_not_there_is_not_a_failure_and_not_a_change(self) -> None:
        result = self.run_tasks(STUB_LEVEL="full")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"changed=0")

    def test_a_container_that_does_not_run_or_has_no_record_is_not_a_reason_to_stop(self) -> None:
        result = self.run_tasks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_server_below_full_is_not_left_without_the_service_that_steps_it_up(self) -> None:
        for level in ("control-plane", "etcd-only"):
            with self.subTest(level=level):
                self.calls.unlink(missing_ok=True)
                result = self.run_tasks(STUB_LEVEL=level, STUB_HAS_SERVICE="1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"is running at the level {level}", " ".join(result.stdout.split()))
                self.assertIn("fleet-regrade up", result.stdout)
                self.assertEqual(self.docker_calls(), ["exec github-runner-k3s cat /run/regrade-level"], "nothing is removed")

    def test_the_container_and_project_names_follow_the_variables(self) -> None:
        result = self.run_tasks("-e", "github_runner_cluster_container_name=custom-k3s", "-e", "github_runner_cluster_compose_project=custom", STUB_LEVEL="full", STUB_HAS_SERVICE="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.docker_calls(), ["exec custom-k3s cat /run/regrade-level", "rm --force custom-regrade-1"])


if __name__ == "__main__":
    unittest.main()
