"""Tests for the permissions of the kubeconfig a k3s server writes to the host: the mode node-entrypoint.sh passes to k3s, and the directory the cluster role creates for it."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_cluster"
ENTRYPOINT = ROLE / "files" / "k3s" / "node-entrypoint.sh"


def function_source(name: str) -> str:
    """The definition of a shell function in the entrypoint, from its first line to the closing brace at the start of a line."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", ENTRYPOINT.read_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, f"{name} is not defined in node-entrypoint.sh"
    return match.group(0)


def kubeconfig_modes(builder: str) -> list[str]:
    """Return every --write-kubeconfig-mode value in the k3s server arguments the builder sets."""
    script = "\n".join(
        [
            "set -u",
            'vpn_auth="name=tailscale,joinKey=example"',
            'mesh_ip="100.64.0.1"',
            'regrade_flags=""',
            'api_port_flags=""',
            'etcd_timing_flags=""',
            function_source(builder),
            builder,
            'printf "%s\\n" "$server_args"',
        ]
    )
    clean = {key: value for key, value in os.environ.items() if not key.startswith("K3S_")}
    result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=True, env=clean)
    return [arg.removeprefix("--write-kubeconfig-mode=") for arg in result.stdout.split() if arg.startswith("--write-kubeconfig-mode=")]


class KubeconfigModeTest(unittest.TestCase):
    def test_every_server_writes_a_kubeconfig_the_host_user_can_read_and_only_its_owner_can_change(self) -> None:
        for builder in ("mesh_server_args", "direct_server_args"):
            with self.subTest(builder=builder):
                modes = kubeconfig_modes(builder)
                self.assertEqual(len(modes), 1, modes)
                # k3s parses the value as octal and passes it to chmod, so it must be permission bits alone.
                mode = int(modes[0], 8)
                self.assertEqual(mode & ~0o777, 0, f"{modes[0]} sets more than permission bits")
                # Where Docker runs the container as root the file is root's, and the role reads it as the connecting user through the bits for others.
                self.assertTrue(mode & stat.S_IRUSR and mode & stat.S_IROTH, f"{modes[0]} is not readable by its owner and by others")
                self.assertEqual(mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH), 0, f"{modes[0]} lets someone other than its owner write it, or someone execute it")


class KubeconfigDirectoryTest(unittest.TestCase):
    def test_only_the_connecting_user_can_enter_the_kubeconfig_directory(self) -> None:
        tasks: list[dict[str, Any]] = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        task = next(task for task in tasks if task.get("name") == "Create this host's Compose project directory")
        modes = {item["path"]: item["mode"] for item in task["loop"]}
        self.assertEqual(task["ansible.builtin.file"]["path"], "{{ github_runner_cluster_dir }}/{{ item.path }}")
        self.assertEqual(task["ansible.builtin.file"]["mode"], "{{ item.mode }}")
        self.assertEqual(modes["kubeconfig"], "0700")


if __name__ == "__main__":
    unittest.main()
