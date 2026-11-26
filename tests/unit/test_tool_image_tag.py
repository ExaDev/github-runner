"""Tests of scripts/ci/tool_image_tag.sh, which names the tag a tool image is published under, and scripts/ci/check_tool_image_tag.sh, which refuses a change to what an image builds under a tag that stays the same."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAG = ROOT / "scripts" / "ci" / "tool_image_tag.sh"
CHECK = ROOT / "scripts" / "ci" / "check_tool_image_tag.sh"
BASE = "# A comment.\nFROM busybox\nARG TOOL_VERSION=1.0\nARG IMAGE_REVISION=1\nARG OTHER_RELEASE=2\n"


def tag(dockerfile: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "Dockerfile"
        path.write_text(dockerfile)
        return subprocess.run(["bash", str(TAG), str(path)], capture_output=True, text=True)


def check(old: dict[str, str], new: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """The check's result for two image directories, each given as its files' names and contents."""
    with tempfile.TemporaryDirectory() as directory:
        paths = []
        for name, files in (("old", old), ("new", new)):
            path = Path(directory) / name
            path.mkdir()
            for file, content in files.items():
                (path / file).write_text(content)
            paths.append(str(path))
        return subprocess.run(["bash", str(CHECK), *paths], capture_output=True, text=True)


class ToolImageTagTest(unittest.TestCase):
    def test_the_version_alone(self) -> None:
        self.assertEqual(tag("ARG TOOL_VERSION=1.0\n").stdout, "1.0\n")

    def test_the_revision_follows_the_version(self) -> None:
        self.assertEqual(tag(BASE).stdout, "1.0-r1\n")

    def test_anything_but_one_version_and_a_numeric_revision_fails(self) -> None:
        for dockerfile in ("FROM busybox\n", "ARG A_VERSION=1\nARG B_VERSION=2\n", "ARG A_VERSION=1\nARG IMAGE_REVISION=\n", "ARG A_VERSION=1\nARG IMAGE_REVISION=two\n"):
            with self.subTest(dockerfile=dockerfile):
                self.assertNotEqual(tag(dockerfile).returncode, 0)


class CheckToolImageTagTest(unittest.TestCase):
    def test_a_dockerfile_comment_change_needs_no_new_tag(self) -> None:
        files = {"Dockerfile": BASE, "shim.sh": "true\n"}
        self.assertEqual(check(files, {**files, "Dockerfile": BASE.replace("A comment.", "Another.") + "\n# More.\n"}).returncode, 0)

    def test_a_change_under_the_same_tag_fails(self) -> None:
        old = {"Dockerfile": BASE, "shim.sh": "true\n"}
        changes = {
            "a pin": {**old, "Dockerfile": BASE.replace("OTHER_RELEASE=2", "OTHER_RELEASE=3")},
            "another file": {**old, "shim.sh": "false\n"},
            "an added file": {**old, "more.sh": "true\n"},
            "a removed file": {"Dockerfile": BASE},
        }
        for what, new in changes.items():
            with self.subTest(what=what):
                result = check(old, new)
                self.assertEqual(result.returncode, 1)
                self.assertIn("1.0-r1", result.stderr)

    def test_a_change_under_a_new_tag_passes(self) -> None:
        old = {"Dockerfile": BASE, "shim.sh": "true\n"}
        for dockerfile in (BASE.replace("IMAGE_REVISION=1", "IMAGE_REVISION=2"), BASE.replace("TOOL_VERSION=1.0", "TOOL_VERSION=1.1")):
            self.assertEqual(check(old, {"Dockerfile": dockerfile, "shim.sh": "false\n"}).returncode, 0)


if __name__ == "__main__":
    unittest.main()
