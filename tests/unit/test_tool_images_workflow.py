"""Tests that the publish workflow can publish several versions of one tool image without them colliding, and that the Node.js reference image installs nothing at build time."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-tool-images.yml"
TOOL_IMAGES = ROOT / "tool-images"
KINDS = {"toolcache", "sysroot", "runner"}
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
VERSION_ARGUMENT = re.compile(r"^ARG ([A-Z_]*_VERSION)=(.+)$", flags=re.MULTILINE)
PACKAGE_MANAGERS = re.compile(r"\b(apk|apt|apt-get|dpkg|npm|npx|pnpm|yarn|pip|pip3|pipx|gem|cargo|go install)\b")


def default_version(name: str) -> str:
    """The version a tool image's Dockerfile builds when no explicit one is named."""
    pins = VERSION_ARGUMENT.findall((TOOL_IMAGES / name / "Dockerfile").read_text())
    assert len(pins) == 1, (name, pins)
    return pins[0][1]


class MatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text())
        cls.build: list[dict[str, Any]] = cls.workflow["jobs"]["build"]["strategy"]["matrix"]["image"]
        cls.merge: list[dict[str, Any]] = cls.workflow["jobs"]["merge"]["strategy"]["matrix"]["image"]

    def test_the_merge_job_joins_exactly_the_images_the_build_job_builds(self) -> None:
        self.assertEqual(self.build, self.merge)

    def test_every_entry_names_a_kind_and_a_directory_with_a_dockerfile(self) -> None:
        for entry in self.build:
            with self.subTest(entry=entry):
                self.assertIn(entry["kind"], KINDS)
                self.assertTrue((TOOL_IMAGES / entry["name"] / "Dockerfile").is_file())

    def test_an_explicit_version_is_a_string_of_three_numbers(self) -> None:
        for entry in self.build:
            if "version" in entry:
                with self.subTest(entry=entry):
                    self.assertIsInstance(entry["version"], str)
                    self.assertRegex(entry["version"], SEMVER)

    def test_no_two_entries_publish_the_same_tag_of_the_same_repository(self) -> None:
        tags = [(entry["kind"], entry["name"], entry.get("version", default_version(entry["name"]))) for entry in self.build]
        self.assertEqual(len(tags), len(set(tags)), tags)

    def test_a_tool_with_an_explicit_version_also_has_the_entry_for_its_default(self) -> None:
        # The default entry keeps the version the Dockerfile and its local builds name published; dropping it would strand every pin of it.
        for entry in self.build:
            if "version" in entry:
                with self.subTest(entry=entry):
                    self.assertIn({"name": entry["name"], "kind": entry["kind"]}, self.build)


class StepsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text())
        cls.build_steps: list[dict[str, Any]] = workflow["jobs"]["build"]["steps"]
        cls.merge_steps: list[dict[str, Any]] = workflow["jobs"]["merge"]["steps"]

    @staticmethod
    def uses(steps: list[dict[str, Any]], action: str) -> dict[str, Any]:
        return next(step for step in steps if step.get("uses", "").startswith(action))

    def test_the_version_a_matrix_entry_names_reaches_the_build_through_the_dockerfiles_own_argument(self) -> None:
        build = self.uses(self.build_steps, "docker/build-push-action")
        self.assertEqual(build["with"]["build-args"].strip(), "${{ steps.image.outputs.argument }}=${{ steps.image.outputs.version }}")
        naming = next(step for step in self.build_steps if step.get("id") == "image")
        self.assertEqual(naming["env"]["PINNED_VERSION"], "${{ matrix.image.version }}")

    def test_both_jobs_resolve_the_version_from_the_entry_before_the_dockerfile(self) -> None:
        for steps in (self.build_steps, self.merge_steps):
            naming = next(step for step in steps if step.get("id") == "image")
            self.assertEqual(naming["env"]["PINNED_VERSION"], "${{ matrix.image.version }}")
            self.assertIn("${PINNED_VERSION:-", naming["run"])

    def test_the_digest_artifacts_of_two_versions_of_one_image_do_not_share_a_name_or_a_download_pattern(self) -> None:
        upload = self.uses(self.build_steps, "actions/upload-artifact")
        download = self.uses(self.merge_steps, "actions/download-artifact")
        stem = "digests-${{ matrix.image.kind }}-${{ matrix.image.name }}-${{ steps.image.outputs.version }}-"
        self.assertEqual(upload["with"]["name"], stem + "${{ matrix.platform.arch }}")
        self.assertEqual(download["with"]["pattern"], stem + "*")


class NodeImageTest(unittest.TestCase):
    dockerfile = (TOOL_IMAGES / "node" / "Dockerfile").read_text()

    def instructions(self) -> list[str]:
        """The Dockerfile's instructions with continuation lines joined and comments dropped."""
        joined = re.sub(r"\\\n\s*", " ", self.dockerfile)
        return [line for line in joined.splitlines() if line.strip() and not line.lstrip().startswith("#")]

    def test_no_build_step_runs_a_package_manager(self) -> None:
        for instruction in self.instructions():
            if instruction.startswith("RUN "):
                with self.subTest(instruction=instruction):
                    self.assertIsNone(PACKAGE_MANAGERS.search(instruction))

    def test_the_archive_is_checked_against_the_releases_own_checksums_before_it_is_unpacked(self) -> None:
        run = next(instruction for instruction in self.instructions() if "SHASUMS256.txt" in instruction)
        self.assertLess(run.index("sha256sum -c"), run.index("tar -x"))

    def test_both_architectures_are_laid_out_under_the_names_setup_node_looks_up(self) -> None:
        self.assertIn("amd64) arch=x64", self.dockerfile)
        self.assertIn("arm64) arch=arm64", self.dockerfile)
        self.assertIn('destination="/toolcache/node/${NODE_VERSION}/${arch}"', self.dockerfile)
        self.assertIn('touch "${destination}.complete"', self.dockerfile)


if __name__ == "__main__":
    unittest.main()
