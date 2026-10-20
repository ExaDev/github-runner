"""Runs the shell tests of fleet-regrade (tests/regrade) so that the unit-test job covers them: each script prints one line per assertion and exits non-zero when any failed."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / "roles" / "github_runner_cluster" / "files" / "regrade"
TESTS = ROOT / "tests" / "regrade"
CORE = FILES / "fleet-regrade-core.sh"
DOCKER_ACTUATOR = FILES / "fleet-regrade-docker.sh"
SERVICE = FILES / "fleet-regrade.sh"


def run_script(script: str, *args: Path) -> subprocess.CompletedProcess[str]:
    """Run one test script under bash from its own directory, which the scripts resolve their helpers from."""
    return subprocess.run(["bash", str(TESTS / script), *map(str, args)], capture_output=True, text=True, check=False, cwd=TESTS)


class CoreTest(unittest.TestCase):
    def test_signals_decision_and_order_of_a_step(self) -> None:
        result = run_script("regrade-core.sh", CORE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all passed", result.stdout)

    def test_stepping_up_by_itself(self) -> None:
        result = run_script("regrade-recover.sh", CORE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all passed", result.stdout)

    def test_lease_and_peer_coordination(self) -> None:
        result = run_script("regrade-lease.sh", CORE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all passed", result.stdout)


class DockerActuatorTest(unittest.TestCase):
    def test_levels_current_level_and_the_restart_between_them(self) -> None:
        result = run_script("regrade-docker.sh", CORE, DOCKER_ACTUATOR)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all passed", result.stdout)


class ServiceTest(unittest.TestCase):
    """The wrapper the regrade service runs: it must refuse to decide without the kernel's memory pressure, before it touches docker."""

    def run_service(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as work:
            stub = Path(work) / "bin" / "docker"
            stub.parent.mkdir()
            stub.write_text("#!/bin/sh\necho \"docker called: $*\" >&2\nexit 1\n")
            stub.chmod(0o755)
            full_env = {**os.environ, "PATH": f"{stub.parent}:{os.environ['PATH']}", "FLEET_REGRADE_ENV": str(Path(work) / "none"), **env}
            return subprocess.run(["bash", str(SERVICE), *args], capture_output=True, text=True, check=False, env=full_env)

    def test_watching_without_memory_pressure_stops_with_the_reason(self) -> None:
        for command in ("watch", "status"):
            with self.subTest(command=command):
                result = self.run_service(command, PSI_FILE="/nonexistent/pressure")
                self.assertEqual(result.returncode, 1)
                self.assertIn("exposes no memory pressure", result.stderr)
                self.assertIn("CONFIG_PSI", result.stderr)
                self.assertNotIn("docker called", result.stderr)

    def test_stepping_by_hand_does_not_need_the_pressure_file(self) -> None:
        # up and down never read it: they reach docker, which the stub refuses, rather than stopping at the pressure check.
        result = self.run_service("down", PSI_FILE="/nonexistent/pressure")
        self.assertNotIn("exposes no memory pressure", result.stderr)

    def test_a_readable_pressure_file_passes_the_check(self) -> None:
        with tempfile.NamedTemporaryFile("w") as psi:
            psi.write("some avg10=0.00 avg60=0.00 avg300=0.00 total=1\n")
            psi.flush()
            result = self.run_service("status", PSI_FILE=psi.name)
        self.assertNotIn("exposes no memory pressure", result.stderr)


if __name__ == "__main__":
    unittest.main()
