"""Tests for the cluster role's node network mode: the Compose project docker compose renders from docker-compose.yml and, with github_runner_cluster_network_mode host, docker-compose.host-network.yml, and the role's derived list of Compose files (tests/cluster_network/compose_files.yml, run with ansible-playbook)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / "roles" / "github_runner_cluster" / "files"
BASE = FILES / "docker-compose.yml"
HOST_NETWORK = FILES / "docker-compose.host-network.yml"
COMPOSE_FILES_PLAYBOOK = ROOT / "tests" / "cluster_network" / "compose_files.yml"
API_PORT = 6443
# What .env supplies; docker-compose.yml refuses to render without the required ones.
ENV = {"K3S_TOKEN": "example-token", "K3S_TLS_SAN_LIST": "node1.example.internal", "K3S_NODE_NAME": "node1", "K3S_AGENT_NODE_NAME": "node2", "K3S_VERSION": "v1.34.1-k3s1", "TAILSCALE_VERSION": "v1.102.4"}


def has_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode == 0


# One project directory for every render, so that bind-mount paths, which Compose resolves against it, compare equal between renders.
PROJECT_DIR = Path(tempfile.gettempdir()) / "github-runner-cluster-network-test"


def render(*files: Path) -> dict[str, dict[str, Any]]:
    """Render the Compose project from the given files, in merge order, with both profiles, and return its services."""
    args = ["docker", "compose", "--project-directory", str(PROJECT_DIR), "--project-name", "example"]
    for path in files:
        args += ["-f", str(path)]
    args += ["--profile", "server", "--profile", "agent", "config", "--format", "json"]
    env = {key: value for key, value in os.environ.items() if not key.startswith(("DOCKER_", "COMPOSE_"))} | ENV
    result = subprocess.run(args, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        raise AssertionError(f"docker compose config failed: {result.stderr}")
    services: dict[str, dict[str, Any]] = json.loads(result.stdout)["services"]
    return services


def published(service: dict[str, Any]) -> set[int]:
    return {int(port["published"]) for port in service.get("ports", [])}


@unittest.skipUnless(has_compose(), "docker compose is not installed")
class RenderedComposeTest(unittest.TestCase):
    def test_bridge_publishes_the_api_server_and_sets_no_network_mode(self) -> None:
        services = render(BASE)
        self.assertEqual(published(services["k3s"]), {API_PORT})
        for name in ("k3s", "k3s-agent"):
            with self.subTest(service=name):
                self.assertNotIn("network_mode", services[name])

    def test_host_puts_both_node_services_on_the_host_network_and_publishes_nothing(self) -> None:
        services = render(BASE, HOST_NETWORK)
        for name in ("k3s", "k3s-agent"):
            with self.subTest(service=name):
                self.assertEqual(services[name]["network_mode"], "host")
                self.assertEqual(published(services[name]), set())
                self.assertNotIn("networks", services[name])

    def test_host_changes_nothing_else_about_the_node_services(self) -> None:
        bridge, host = render(BASE), render(BASE, HOST_NETWORK)
        for name in ("k3s", "k3s-agent"):
            with self.subTest(service=name):
                for key in ("hostname", "environment", "volumes", "entrypoint", "privileged", "container_name"):
                    self.assertEqual(host[name].get(key), bridge[name].get(key), key)
        self.assertEqual(host["docker-prune"], bridge["docker-prune"])

    def test_host_empties_a_port_list_an_earlier_override_published(self) -> None:
        # As the headscale_in_cluster provider's override does on its bootstrap server; the host network file comes after it in the list.
        with tempfile.TemporaryDirectory() as work:
            mesh = Path(work) / "docker-compose.mesh.yml"
            mesh.write_text('services:\n  k3s:\n    ports:\n      - "8080:8080"\n')
            self.assertEqual(published(render(BASE, mesh)["k3s"]), {API_PORT, 8080})
            self.assertEqual(published(render(BASE, mesh, HOST_NETWORK)["k3s"]), set())


@unittest.skipIf(shutil.which(os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")) is None, "ansible-playbook is not installed")
class ComposeFileListTest(unittest.TestCase):
    def file_list(self, **role_vars: Any) -> list[str]:
        with tempfile.TemporaryDirectory() as work:
            output = Path(work) / "files.json"
            extra = Path(work) / "extra.json"
            extra.write_text(json.dumps({"compose_files_output": str(output), **role_vars}))
            env = {**os.environ, "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false"}
            subprocess.run([os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(COMPOSE_FILES_PLAYBOOK), "-e", f"@{extra}"], check=True, env=env, cwd=work, capture_output=True, text=True)
            files: list[str] = json.loads(output.read_text())
        return files

    def test_bridge_is_the_default_and_adds_no_file(self) -> None:
        self.assertEqual(self.file_list(), ["docker-compose.yml"])
        self.assertEqual(self.file_list(github_runner_cluster_network_mode="bridge"), ["docker-compose.yml"])

    def test_host_adds_the_host_network_file(self) -> None:
        self.assertEqual(self.file_list(github_runner_cluster_network_mode="host"), ["docker-compose.yml", "docker-compose.host-network.yml"])

    def test_the_host_network_file_comes_after_every_other_override(self) -> None:
        files = self.file_list(
            github_runner_cluster_network_mode="host",
            github_runner_cluster_mesh="headscale_in_cluster",
            github_runner_cluster_effective_bootstrap=True,
            github_runner_cluster_compose_files=["compose.extra.yml"],
        )
        self.assertEqual(files, ["docker-compose.yml", "docker-compose.mesh.yml", "compose.extra.yml", "docker-compose.host-network.yml"])

    def test_the_role_copies_the_host_network_file_to_the_host(self) -> None:
        tasks = (ROOT / "roles" / "github_runner_cluster" / "tasks" / "main.yml").read_text()
        self.assertIn("{src: docker-compose.host-network.yml, mode: \"0644\"}", tasks)


if __name__ == "__main__":
    unittest.main()
