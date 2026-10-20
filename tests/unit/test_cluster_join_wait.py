"""Tests for the wait the node entrypoint puts after k3s's own `tailscale up`: the wrapper install_tailscale_up_wait writes, run against a stand-in for the real tailscale and for nslookup, and where the entrypoint installs it for a joining server and an agent."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_cluster"
ENTRYPOINT = ROLE / "files" / "k3s" / "node-entrypoint.sh"
COMPOSE = ROLE / "files" / "docker-compose.yml"
HOST = "bootstrap.example.ts.net"

# Stands in for the real tailscale: logs each call's arguments, exits with $STUB/up-status for `up` (0 when absent), and answers `ip -4 <host>` only from the call numbered in $STUB/peer-after onwards, as a backend that has just reloaded a network map with no peers does until the control server sends them again.
TAILSCALE_STUB = """#!/bin/sh
echo "$*" >> "$STUB/calls"
if [ "$1" = up ]; then
  exit "$(cat "$STUB/up-status" 2>/dev/null || echo 0)"
fi
if [ "$1" = ip ]; then
  n=$(($(cat "$STUB/ip-count" 2>/dev/null || echo 0) + 1))
  echo "$n" > "$STUB/ip-count"
  [ "$n" -ge "$(cat "$STUB/peer-after")" ] || exit 1
  echo 100.64.0.1
fi
"""

NSLOOKUP_STUB = """#!/bin/sh
echo "nslookup $*" >> "$STUB/calls"
"""


def function_source(name: str) -> str:
    """The definition of a shell function in the entrypoint, from its first line to the closing brace at the start of a line."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", ENTRYPOINT.read_text(), re.MULTILINE | re.DOTALL)
    assert match is not None, f"{name} is not defined in node-entrypoint.sh"
    return match.group(0)


class WrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.stub = self.work / "stub"
        self.stub.mkdir()
        (self.work / "bin").mkdir()
        self.real = self.work / "bin" / "tailscale"
        self.real.write_text(TAILSCALE_STUB)
        self.real.chmod(0o755)
        nslookup = self.work / "bin" / "nslookup"
        nslookup.write_text(NSLOOKUP_STUB)
        nslookup.chmod(0o755)
        (self.stub / "peer-after").write_text("3")
        self.wrapper_dir = self.work / "run" / "github-runner-node" / "bin"

    def install(self, host: str = HOST) -> subprocess.CompletedProcess[str]:
        script = "\n".join(["set -u", function_source("install_tailscale_up_wait"), f'install_tailscale_up_wait "{host}" "{self.wrapper_dir}" "{self.real}"'])
        return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=False)

    def run_wrapper(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "STUB": str(self.stub), "PATH": f"{self.work / 'bin'}{os.pathsep}{os.environ['PATH']}"}
        return subprocess.run([str(self.wrapper_dir / "tailscale"), *args], capture_output=True, text=True, check=False, env=env, timeout=60)

    def calls(self) -> list[str]:
        return (self.stub / "calls").read_text().splitlines()

    def test_up_runs_unchanged_then_waits_until_the_join_host_is_a_peer_and_resolves(self) -> None:
        self.assertEqual(self.install().returncode, 0)
        result = self.run_wrapper("up", "--authkey", "example", "--timeout=30s", "--reset")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls(),
            ["up --authkey example --timeout=30s --reset", f"ip -4 {HOST}", f"ip -4 {HOST}", f"ip -4 {HOST}", f"nslookup {HOST} 100.100.100.100"],
        )

    def test_any_other_command_runs_unchanged_and_waits_for_nothing(self) -> None:
        self.install()
        result = self.run_wrapper("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["status --json"])

    def test_a_failed_up_returns_its_status_without_waiting(self) -> None:
        self.install()
        (self.stub / "up-status").write_text("3")
        result = self.run_wrapper("up", "--reset")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.calls(), ["up --reset"])

    def test_a_node_that_joins_nothing_gets_no_wrapper(self) -> None:
        self.assertEqual(self.install(host="").returncode, 0)
        self.assertFalse(self.wrapper_dir.exists())


class JoinHostTest(unittest.TestCase):
    def join_host(self, url: str) -> str:
        script = "\n".join([function_source("join_host_of"), f'join_host_of "{url}"'])
        return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=True).stdout

    def test_the_host_is_taken_from_the_server_url(self) -> None:
        self.assertEqual(self.join_host(f"https://{HOST}:6443"), HOST)
        self.assertEqual(self.join_host(""), "")


class EntrypointTest(unittest.TestCase):
    def case_body(self, role: str) -> str:
        match = re.search(rf"^  {role}\)\n(.*?)^    ;;\n", ENTRYPOINT.read_text(), re.MULTILINE | re.DOTALL)
        assert match is not None, f"no {role} case in node-entrypoint.sh"
        return match.group(1)

    def test_a_joining_server_waits_before_k3s_and_starts_k3s_with_the_wrapper_first_on_its_path(self) -> None:
        body = self.case_body("server")
        self.assertIn('server_join_host=$(join_host_of "${K3S_JOIN_SERVER_URL:-}")', body)
        self.assertLess(body.index('wait_for_mesh_host "$server_join_host"'), body.index("exec /bin/k3s server"))
        self.assertLess(body.index('install_tailscale_up_wait "$server_join_host" "$tailscale_up_wait_dir" /usr/local/bin/tailscale'), body.index("exec /bin/k3s server"))
        self.assertIn('PATH="$tailscale_up_wait_dir:$PATH" exec /bin/k3s server', body)

    def test_an_agent_waits_for_the_server_it_dials(self) -> None:
        body = self.case_body("agent")
        self.assertIn('agent_join_host=$(join_host_of "${K3S_URL:-}")', body)
        self.assertIn('install_tailscale_up_wait "$agent_join_host" "$tailscale_up_wait_dir" /usr/local/bin/tailscale', body)
        self.assertIn('PATH="$tailscale_up_wait_dir:$PATH" exec /bin/k3s agent', body)

    def test_the_wrapper_lives_on_a_tmpfs_of_both_services_so_every_start_writes_it_afresh(self) -> None:
        directory = re.search(r"^tailscale_up_wait_dir=(\S+)$", ENTRYPOINT.read_text(), re.MULTILINE)
        assert directory is not None
        services = yaml.safe_load(COMPOSE.read_text())["services"]
        for name in ("k3s", "k3s-agent"):
            with self.subTest(service=name):
                self.assertTrue(any(directory.group(1).startswith(mount.rstrip("/") + "/") for mount in services[name]["tmpfs"]))


if __name__ == "__main__":
    unittest.main()
