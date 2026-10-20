"""Tests for running a second cluster beside another on the same Docker hosts: the container names and API port the Compose files take from .env, the override that moves k3s's listen port, the entrypoint flag, the role's defaults and .env templates, and the check in tasks/instance_check.yml (its filter in plugins/filter/cluster_instance.py, and the task file run through the role against a stand-in for docker)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("cluster_instance", ROOT / "plugins" / "filter" / "cluster_instance.py")
assert _spec is not None and _spec.loader is not None
cluster_instance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cluster_instance)
AnsibleFilterError = cluster_instance.AnsibleFilterError
ROLE = ROOT / "roles" / "github_runner_cluster"
COMPOSE = ROLE / "files" / "docker-compose.yml"
API_PORT_COMPOSE = ROLE / "files" / "docker-compose.api-port.yml"
ENTRYPOINT = ROLE / "files" / "k3s" / "node-entrypoint.sh"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())

# The values every node ran under before they were configurable, written out here rather than read from the defaults, so that a changed default fails these tests instead of moving with them.
SERVER_CONTAINER = "github-runner-k3s"
AGENT_CONTAINER = "github-runner-k3s-agent"
K3S_PORT = 6443


def ansible_env() -> dict[str, str]:
    return {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_RETRY_FILES_ENABLED": "false"}


def container_line(name: str, project: str | None = None, directory: str | None = None, ports: dict[str, Any] | None = None) -> str:
    """One container as tasks/instance_check.yml has docker inspect print it."""
    labels = {key: value for key, value in (("com.docker.compose.project", project), ("com.docker.compose.project.working_dir", directory)) if value is not None}
    return json.dumps({"name": f"/{name}", "labels": labels or None, "ports": ports})


def published(port: int, protocol: str = "tcp") -> dict[str, Any]:
    """Port bindings that publish ``port`` on every address of the host, as Docker records "port:port"."""
    return {f"{port}/{protocol}": [{"HostIp": "", "HostPort": str(port)}, {"HostIp": "::", "HostPort": str(port)}]}


class ConflictFilterTest(unittest.TestCase):
    def conflicts(self, *lines: str, port: Any = K3S_PORT, directory: str = "/home/u/.github-runner") -> list[str]:
        return cluster_instance.cluster_instance_conflicts(list(lines), "github-runner", [SERVER_CONTAINER, AGENT_CONTAINER], port, directory)

    def test_an_empty_daemon_has_no_conflict(self) -> None:
        self.assertEqual(self.conflicts(), [])
        self.assertEqual(self.conflicts(""), [])

    def test_this_project_s_own_containers_never_conflict(self) -> None:
        own = container_line(SERVER_CONTAINER, "github-runner", "/home/u/.github-runner", published(K3S_PORT))
        prune = container_line("github-runner-docker-prune-1", "github-runner", "/home/u/.github-runner")
        self.assertEqual(self.conflicts(own, prune), [])

    def test_a_container_name_another_project_holds_is_named_with_its_owner(self) -> None:
        found = self.conflicts(container_line(SERVER_CONTAINER, "other", "/srv/other"))
        self.assertEqual(found, [f"a container named {SERVER_CONTAINER} already exists and belongs to the Compose project other"])
        found = self.conflicts(container_line(AGENT_CONTAINER))
        self.assertEqual(found, [f"a container named {AGENT_CONTAINER} already exists and belongs to no Compose project"])

    def test_the_published_port_of_another_project_conflicts_whatever_the_address_or_protocol(self) -> None:
        for bindings in (published(K3S_PORT), {f"{K3S_PORT}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(K3S_PORT)}]}, {"80/udp": [{"HostIp": "", "HostPort": str(K3S_PORT)}]}):
            with self.subTest(bindings=bindings):
                found = self.conflicts(container_line("old-k3s", "old", "/srv/old", bindings))
                self.assertEqual(found, [f"the container old-k3s, of the Compose project old, already publishes host port {K3S_PORT}"])

    def test_another_port_or_an_unpublished_container_port_does_not_conflict(self) -> None:
        self.assertEqual(self.conflicts(container_line("web", "web", "/srv/web", published(3000))), [])
        self.assertEqual(self.conflicts(container_line("web", "web", "/srv/web", {f"{K3S_PORT}/tcp": None})), [])
        self.assertEqual(self.conflicts(container_line("web", "web", "/srv/web", {f"{K3S_PORT}/tcp": [{"HostIp": "", "HostPort": ""}]})), [])

    def test_an_agent_publishes_no_port_so_none_is_compared(self) -> None:
        self.assertEqual(self.conflicts(container_line("old-k3s", "old", "/srv/old", published(K3S_PORT)), port=""), [])

    def test_another_project_in_the_same_directory_conflicts(self) -> None:
        found = self.conflicts(container_line("x-1", "x", "/home/u/.github-runner"))
        self.assertEqual(found, ["the container x-1, of the Compose project x, runs from the same project directory /home/u/.github-runner"])

    def test_every_collision_of_one_container_is_reported(self) -> None:
        found = self.conflicts(container_line(SERVER_CONTAINER, "old", "/home/u/.github-runner", published(K3S_PORT)))
        self.assertEqual(len(found), 3, found)

    def test_malformed_input_is_refused(self) -> None:
        for lines in ("not a list", ["not json"], [json.dumps({"labels": {}})], [json.dumps({"name": "/a", "labels": ["x"]})]):
            with self.subTest(lines=lines), self.assertRaises(AnsibleFilterError):
                cluster_instance.cluster_instance_conflicts(lines, "p", ["a"], K3S_PORT, "/d")
        for project, names, directory in (("", ["a"], "/d"), ("p", "a", "/d"), ("p", [""], "/d"), ("p", ["a"], "")):
            with self.subTest(project=project, names=names, directory=directory), self.assertRaises(AnsibleFilterError):
                cluster_instance.cluster_instance_conflicts([], project, names, K3S_PORT, directory)


class DefaultsTest(unittest.TestCase):
    def test_the_defaults_are_the_names_and_port_every_node_already_runs_under(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_container_name"], SERVER_CONTAINER)
        self.assertEqual(DEFAULTS["github_runner_cluster_agent_container_name"], AGENT_CONTAINER)
        self.assertEqual(DEFAULTS["github_runner_cluster_api_port"], K3S_PORT)
        self.assertEqual(DEFAULTS["github_runner_cluster_k3s_default_api_port"], K3S_PORT)

    def test_the_node_container_is_the_agent_s_own_variable_on_an_agent(self) -> None:
        template = DEFAULTS["github_runner_cluster_node_container"]
        self.assertIn("github_runner_cluster_agent_container_name", template)
        self.assertNotIn(AGENT_CONTAINER, template)

    def test_the_role_copies_the_api_port_override_to_the_host(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        copy = next(task for task in tasks if task.get("name") == "Place the collection's Compose file and k3s build context in it")
        self.assertIn("docker-compose.api-port.yml", [item["src"] for item in copy["loop"]])

    def test_the_check_runs_before_the_role_writes_to_the_host(self) -> None:
        names = [task.get("name", "") for task in yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())]
        check = next(index for index, name in enumerate(names) if "container names, API port and directory" in name)
        self.assertLess(check, names.index("Create this host's Compose project directory"))
        self.assertLess(check, names.index("Keep this node's password across container recreation"))
        self.assertGreater(check, names.index("Decide this host's part in the cluster"))

    def test_every_new_variable_is_in_the_readme(self) -> None:
        readme = (ROLE / "README.md").read_text()
        for name in ("github_runner_cluster_container_name", "github_runner_cluster_agent_container_name", "github_runner_cluster_api_port"):
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", readme)
        self.assertIn("## Several clusters on one host", readme)


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class RenderTest(unittest.TestCase):
    """Renders the .env templates and the derived Compose file list with the role's defaults."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)

    def render(self, *extra: str) -> tuple[list[str], list[str], list[str]]:
        env, agent, files = self.work / "env", self.work / "env-agent", self.work / "files.json"
        args = [ANSIBLE_PLAYBOOK, str(ROOT / "tests" / "cluster_versions" / "render_env.yml"), "-e", f"env_output={env}", "-e", f"env_agent_output={agent}", "-e", f"compose_files_output={files}", "-e", "github_runner_cluster_dir=/srv/github-runner", *extra]
        result = subprocess.run(args, capture_output=True, text=True, check=False, env=ansible_env(), cwd=self.work)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        compose_files: list[str] = json.loads(files.read_text())
        return env.read_text().splitlines(), agent.read_text().splitlines(), compose_files

    def test_the_defaults_write_the_existing_names_and_port_and_add_no_compose_file(self) -> None:
        server, agent, files = self.render()
        self.assertIn(f"K3S_CONTAINER_NAME={SERVER_CONTAINER}", server)
        self.assertIn(f"K3S_API_PORT={K3S_PORT}", server)
        self.assertIn(f"K3S_AGENT_CONTAINER_NAME={AGENT_CONTAINER}", agent)
        self.assertIn(f"K3S_CONTAINER_NAME={SERVER_CONTAINER}", agent)
        self.assertIn(f"K3S_API_PORT={K3S_PORT}", agent)
        self.assertEqual(files, ["docker-compose.yml"])
        self.assertIn("COMPOSE_FILE=/srv/github-runner/docker-compose.yml", server)

    def test_a_second_cluster_s_values_reach_both_templates_and_the_port_adds_the_override(self) -> None:
        server, agent, files = self.render("-e", "github_runner_cluster_container_name=second-k3s", "-e", "github_runner_cluster_agent_container_name=second-k3s-agent", "-e", "github_runner_cluster_api_port=6543")
        self.assertIn("K3S_CONTAINER_NAME=second-k3s", server)
        self.assertIn("K3S_API_PORT=6543", server)
        self.assertIn("K3S_AGENT_CONTAINER_NAME=second-k3s-agent", agent)
        self.assertEqual(files, ["docker-compose.yml", "docker-compose.api-port.yml"])
        self.assertIn("COMPOSE_FILE=/srv/github-runner/docker-compose.yml:/srv/github-runner/docker-compose.api-port.yml", server)

    def test_the_default_port_written_as_a_string_adds_no_override(self) -> None:
        _, _, files = self.render("-e", '{"github_runner_cluster_api_port": "6443"}')
        self.assertEqual(files, ["docker-compose.yml"])


def has_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode == 0


@unittest.skipUnless(has_compose(), "docker compose is not installed")
class ComposeRenderTest(unittest.TestCase):
    """Renders the Compose files with docker compose config, which needs no daemon."""

    def compose(self, *arguments: str, files: tuple[Path, ...] = (COMPOSE,), **env: str) -> str:
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("DOCKER_", "COMPOSE_", "K3S_", "TAILSCALE_"))}
        base = {"K3S_TOKEN": "t", "K3S_TLS_SAN_LIST": "n", "K3S_VERSION": "v1.34.1-k3s1", "TAILSCALE_VERSION": "v1.102.4"}
        file_args = [argument for path in files for argument in ("-f", str(path))]
        args = ["docker", "compose", "--project-directory", tempfile.gettempdir(), "--project-name", "example", *file_args, "--profile", "server", "--profile", "agent", *arguments]
        result = subprocess.run(args, capture_output=True, text=True, check=False, env=clean | base | env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def services(self, files: tuple[Path, ...] = (COMPOSE,), **env: str) -> dict[str, Any]:
        services: dict[str, Any] = json.loads(self.compose("config", "--format", "json", files=files, **env))["services"]
        return services

    def hashes(self, files: tuple[Path, ...] = (COMPOSE,), **env: str) -> dict[str, str]:
        return dict(line.split() for line in self.compose("config", "--hash", "*", files=files, **env).splitlines())

    def test_with_nothing_set_the_containers_and_port_are_the_ones_every_node_runs_under(self) -> None:
        services = self.services()
        self.assertEqual(services["k3s"]["container_name"], SERVER_CONTAINER)
        self.assertEqual(services["k3s-agent"]["container_name"], AGENT_CONTAINER)
        self.assertEqual([(port["published"], port["target"]) for port in services["k3s"]["ports"]], [(str(K3S_PORT), K3S_PORT)])
        self.assertNotIn("K3S_API_PORT", services["k3s"]["environment"])

    def test_the_values_the_role_writes_by_default_render_the_same_configuration_hash_as_none(self) -> None:
        # Compose recreates a container whose configuration hash changes, so a default .env that rendered any differently would restart every running server on upgrade.
        written = {"K3S_CONTAINER_NAME": SERVER_CONTAINER, "K3S_AGENT_CONTAINER_NAME": AGENT_CONTAINER, "K3S_API_PORT": str(K3S_PORT)}
        self.assertEqual(self.hashes(**written), self.hashes())

    def test_a_second_cluster_gets_its_own_names_and_publishes_and_listens_on_its_own_port(self) -> None:
        services = self.services(files=(COMPOSE, API_PORT_COMPOSE), K3S_CONTAINER_NAME="second-k3s", K3S_AGENT_CONTAINER_NAME="second-k3s-agent", K3S_API_PORT="6543")
        self.assertEqual(services["k3s"]["container_name"], "second-k3s")
        self.assertEqual(services["k3s-agent"]["container_name"], "second-k3s-agent")
        self.assertEqual([(port["published"], port["target"]) for port in services["k3s"]["ports"]], [("6543", 6543)])
        self.assertEqual(services["k3s"]["environment"]["K3S_API_PORT"], "6543")
        self.assertNotIn("K3S_API_PORT", services["k3s-agent"]["environment"])
        self.assertNotEqual(self.hashes(files=(COMPOSE, API_PORT_COMPOSE), K3S_API_PORT="6543")["k3s"], self.hashes()["k3s"])

    def test_the_override_refuses_to_render_without_a_port(self) -> None:
        clean = {key: value for key, value in os.environ.items() if not key.startswith(("DOCKER_", "COMPOSE_", "K3S_", "TAILSCALE_"))}
        base = {"K3S_TOKEN": "t", "K3S_TLS_SAN_LIST": "n", "K3S_VERSION": "v1.34.1-k3s1", "TAILSCALE_VERSION": "v1.102.4"}
        args = ["docker", "compose", "--project-directory", tempfile.gettempdir(), "--project-name", "example", "-f", str(COMPOSE), "-f", str(API_PORT_COMPOSE), "--profile", "server", "config"]
        result = subprocess.run(args, capture_output=True, text=True, check=False, env=clean | base)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("K3S_API_PORT must be set", result.stderr)


def function_source(name: str) -> str:
    """The definition of a shell function in the entrypoint, from its first line to the closing brace at the start of a line."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", ENTRYPOINT.read_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, f"{name} is not defined in node-entrypoint.sh"
    return match.group(0)


class EntrypointTest(unittest.TestCase):
    """Runs the entrypoint's argument-list functions in a shell, as test_cluster_etcd_timing.py does."""

    def server_args(self, builder: str, **env: str) -> list[str]:
        script = "\n".join(
            [
                "set -u",
                'vpn_auth="name=tailscale,joinKey=example"',
                'mesh_ip="100.64.0.1"',
                'regrade_flags=""',
                function_source("set_etcd_timing_flags"),
                function_source("set_api_port_flags"),
                function_source(builder),
                "set_etcd_timing_flags",
                "set_api_port_flags",
                builder,
                'printf "%s\\n" "$server_args"',
            ]
        )
        clean = {key: value for key, value in os.environ.items() if not key.startswith("K3S_")}
        result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=False, env=clean | env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_the_script_calls_the_function_that_sets_the_flag(self) -> None:
        self.assertRegex(ENTRYPOINT.read_text(), re.compile(r"^set_api_port_flags$", re.MULTILINE))

    def test_a_port_makes_k3s_listen_on_it_on_the_mesh_and_off_it(self) -> None:
        for builder in ("mesh_server_args", "direct_server_args"):
            with self.subTest(builder=builder):
                args = self.server_args(builder, K3S_API_PORT="6543", K3S_JOIN_SERVER_URL="https://bootstrap.example.internal:6543")
                self.assertEqual([arg for arg in args if arg.startswith("--https-listen-port")], ["--https-listen-port=6543"])

    def test_no_port_leaves_k3s_on_its_own_default(self) -> None:
        for builder in ("mesh_server_args", "direct_server_args"):
            for env in ({}, {"K3S_API_PORT": ""}):
                with self.subTest(builder=builder, env=env):
                    self.assertEqual([arg for arg in self.server_args(builder, **env) if "listen-port" in arg], [])

    def test_the_agent_command_line_does_not_take_it(self) -> None:
        agent_line = next(line for line in ENTRYPOINT.read_text().splitlines() if "k3s agent" in line)
        self.assertNotIn("listen-port", agent_line)


# Stands in for the docker CLI for tasks/instance_check.yml: `ps --all --quiet` prints one id per line of $DOCKER_STUB/containers, and `inspect --format ... <ids>` prints those lines, as the task's format has docker print them.
DOCKER_STUB = """#!{python}
import os, sys
stub = os.environ["DOCKER_STUB"]
args = sys.argv[1:]
with open(os.path.join(stub, "calls.log"), "a") as log:
    log.write(" ".join(args) + "\\n")
path = os.path.join(stub, "containers")
lines = open(path).read().splitlines() if os.path.exists(path) else []
if args == ["ps", "--all", "--quiet"]:
    print("\\n".join("c%d" % index for index in range(len(lines))))
elif args[0] == "inspect" and args[1] == "--format":
    ids = args[3:]
    if ids != ["c%d" % index for index in range(len(lines))]:
        sys.exit("unexpected ids: " + " ".join(ids))
    print("\\n".join(lines))
else:
    sys.exit("unexpected docker call: " + " ".join(args))
"""


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class RoleTest(unittest.TestCase):
    """Runs tasks/instance_check.yml through the role (tests/cluster_versions/checks.yml) against the stand-in docker."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.stub = self.work / "stub"
        self.stub.mkdir()
        (self.work / "bin").mkdir()
        docker = self.work / "bin" / "docker"
        docker.write_text(DOCKER_STUB.replace("{python}", sys.executable))
        docker.chmod(0o755)

    def containers(self, *lines: str) -> None:
        (self.stub / "containers").write_text("\n".join(lines) + "\n")

    def run_check(self, *extra: str, role: str = "server", group_vars: dict[str, Any] | None = None, hosts: dict[str, dict[str, Any]] | None = None) -> subprocess.CompletedProcess[str]:
        node = {"github_runner_cluster_effective_node_role": role, "github_runner_cluster_docker_cli_environment": {}, "github_runner_cluster_dir": "/srv/second"}
        group = {"vars": {"github_runner_cluster_compose_project": "second", **(group_vars or {})}, "hosts": {name: node | values for name, values in (hosts or {"node1": {}}).items()}}
        inventory = self.work / "inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"children": {"github_runner_cluster": group}}}))
        args = [ANSIBLE_PLAYBOOK, "-i", str(inventory), str(ROOT / "tests" / "cluster_versions" / "checks.yml"), "-e", "check_tasks=instance_check", "--limit", "node1", *extra]
        env = ansible_env() | {"PATH": f"{self.work / 'bin'}{os.pathsep}{os.environ['PATH']}", "DOCKER_STUB": str(self.stub)}
        return subprocess.run(args, capture_output=True, text=True, check=False, env=env, cwd=self.work)

    def first_cluster(self) -> list[str]:
        """The containers a cluster on the defaults runs on a server host."""
        return [
            container_line(SERVER_CONTAINER, "github-runner", "/srv/github-runner", published(K3S_PORT)),
            container_line("github-runner-docker-prune-1", "github-runner", "/srv/github-runner"),
        ]

    def test_an_empty_daemon_passes(self) -> None:
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.stub / "calls.log").read_text().splitlines(), ["ps --all --quiet"])

    def test_a_second_cluster_with_its_own_names_port_and_directory_passes_beside_the_first(self) -> None:
        self.containers(*self.first_cluster())
        result = self.run_check(group_vars={"github_runner_cluster_container_name": "second-k3s", "github_runner_cluster_agent_container_name": "second-k3s-agent", "github_runner_cluster_api_port": 6543})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_second_cluster_on_the_default_names_and_port_is_refused_with_every_collision(self) -> None:
        self.containers(*self.first_cluster())
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout.replace("\n", " ")
        self.assertIn(f"a container named {SERVER_CONTAINER} already exists and belongs to the Compose project github-runner", output)
        self.assertIn(f"the container {SERVER_CONTAINER}, of the Compose project github-runner, already publishes host port {K3S_PORT}", output)
        self.assertIn("Nothing was changed.", output)

    def test_the_check_runs_under_check_mode_too(self) -> None:
        self.containers(*self.first_cluster())
        result = self.run_check("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already publishes host port", result.stdout)

    def test_an_agent_is_not_refused_for_a_port_it_does_not_publish(self) -> None:
        self.containers(*self.first_cluster())
        result = self.run_check(role="agent", group_vars={"github_runner_cluster_container_name": "second-k3s", "github_runner_cluster_agent_container_name": "second-k3s-agent"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_first_cluster_s_own_run_passes_beside_a_second(self) -> None:
        second = container_line("second-k3s", "second", "/srv/second", published(6543))
        self.containers(*self.first_cluster(), second)
        result = self.run_check(group_vars={"github_runner_cluster_compose_project": "github-runner"}, hosts={"node1": {"github_runner_cluster_dir": "/srv/github-runner"}})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_directory_another_project_runs_from_is_refused(self) -> None:
        self.containers(*self.first_cluster())
        result = self.run_check(group_vars={"github_runner_cluster_container_name": "second-k3s", "github_runner_cluster_agent_container_name": "second-k3s-agent", "github_runner_cluster_api_port": 6543}, hosts={"node1": {"github_runner_cluster_dir": "/srv/github-runner"}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runs from the same project directory /srv/github-runner", result.stdout.replace("\n", " "))

    def test_a_port_that_is_not_a_port_number_is_refused_before_docker_is_read(self) -> None:
        for port in ("0", "65536", "abc", "-1", "6443x"):
            with self.subTest(port=port):
                result = self.run_check("-e", json.dumps({"github_runner_cluster_api_port": port}))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be a whole number from 1 to 65535", result.stdout.replace("\n", " "))
        self.assertFalse((self.stub / "calls.log").exists())

    def test_hosts_of_the_group_with_different_ports_are_refused(self) -> None:
        result = self.run_check(hosts={"node1": {"github_runner_cluster_api_port": 6543}, "node2": {}, "node3": {"github_runner_cluster_api_port": 6543}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("node1: 6543; node2: 6443; node3: 6543", result.stdout.replace("\n", " "))

    def test_the_same_port_set_once_for_the_group_passes(self) -> None:
        result = self.run_check(group_vars={"github_runner_cluster_api_port": 6543}, hosts={"node1": {}, "node2": {}, "node3": {}})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_container_names_docker_refuses_or_that_are_the_same_are_refused(self) -> None:
        for names in ({"github_runner_cluster_container_name": "-bad"}, {"github_runner_cluster_agent_container_name": "has space"}, {"github_runner_cluster_container_name": "same", "github_runner_cluster_agent_container_name": "same"}):
            with self.subTest(names=names):
                result = self.run_check(group_vars=names)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be a Docker container name", result.stdout.replace("\n", " "))


if __name__ == "__main__":
    unittest.main()
