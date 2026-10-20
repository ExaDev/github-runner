"""Runs the shell tests of fleet-regrade (tests/regrade) so that the unit-test job covers them: each script prints one line per assertion and exits non-zero when any failed."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / "roles" / "github_runner_cluster" / "files" / "regrade"
TESTS = ROOT / "tests" / "regrade"
CORE = FILES / "fleet-regrade-core.sh"


def run_script(script: str, *args: Path) -> subprocess.CompletedProcess[str]:
    """Run one test script under bash from its own directory, which the scripts resolve their helpers from."""
    return subprocess.run(["bash", str(TESTS / script), *map(str, args)], capture_output=True, text=True, check=False, cwd=TESTS)


class CoreTest(unittest.TestCase):
    def test_signals_decision_and_order_of_a_step(self) -> None:
        result = run_script("regrade-core.sh", CORE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all passed", result.stdout)

    def test_lease_and_peer_coordination(self) -> None:
        result = run_script("regrade-lease.sh", CORE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
