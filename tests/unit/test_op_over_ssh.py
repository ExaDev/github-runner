"""Tests for roles/github_runner_secrets_onepassword/files/op-over-ssh.sh: it forwards only read commands to the named machine, quoted, and refuses the rest. Runs tests/op_over_ssh/run.sh, which uses a stub ssh and contacts no machine."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class OpOverSshTests(unittest.TestCase):
    def test_forwards_reads_quoted_and_refuses_writes(self) -> None:
        result = subprocess.run(["bash", str(REPO / "tests" / "op_over_ssh" / "run.sh")], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("op-over-ssh: ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
