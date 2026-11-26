"""Tests of tool-images/rust/zig.sh, the zig on the Rust tool image's PATH: it passes every argument on unchanged, and adds -lunwind exactly when a link asks for LLVM's libunwind, in its arguments or in a response file."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SHIM = Path(__file__).resolve().parents[2] / "tool-images" / "rust" / "zig.sh"


class ZigShimTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "bin").mkdir()
        (self.root / "zig").mkdir()
        shutil.copy(SHIM, self.root / "bin" / "zig")
        # A stand-in for Zig that prints each argument it receives on a line of its own.
        stand_in = self.root / "zig" / "zig"
        stand_in.write_text('#!/bin/sh\nfor argument in "$@"; do printf "%s\\n" "$argument"; done\n')
        stand_in.chmod(0o755)

    def received(self, *arguments: str) -> list[str]:
        result = subprocess.run(["sh", str(self.root / "bin" / "zig"), *arguments], capture_output=True, text=True, check=True)
        return result.stdout.splitlines()

    def test_other_invocations_are_unchanged(self) -> None:
        for arguments in (["version"], ["cc", "-o", "a b", "main.o"], ["cc", "--unwindlib=libgcc"]):
            self.assertEqual(self.received(*arguments), arguments)

    def test_libunwind_is_added_once_at_the_end(self) -> None:
        arguments = ["cc", "--unwindlib=libunwind", "main.o", "--unwindlib=libunwind"]
        self.assertEqual(self.received(*arguments), [*arguments, "-lunwind"])

    def test_a_response_file_is_searched(self) -> None:
        asking, other = self.root / "asking", self.root / "other"
        asking.write_text('"-nolibc"\n"--unwindlib=libunwind"\n')
        other.write_text('"-nolibc"\n')
        self.assertEqual(self.received("cc", f"@{asking}"), ["cc", f"@{asking}", "-lunwind"])
        self.assertEqual(self.received("cc", f"@{other}"), ["cc", f"@{other}"])


if __name__ == "__main__":
    unittest.main()
