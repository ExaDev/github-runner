"""Tests that the autoscaler image carries every script the role runs from it, and that a release builds that image from the tag it stamps into the role's defaults, so a pruner or reporter always runs the script of the collection version that installed it."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_arc"
DOCKERFILE = ROOT / "autoscaler" / "Dockerfile"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
IMAGE_REPOSITORY = "ghcr.io/exadev/github-runner-autoscaler"


def scripts_run_from_the_image() -> set[str]:
    """The names of the scripts under /app/scripts that the role's tasks and templates run."""
    found: set[str] = set()
    for directory in ("tasks", "templates"):
        for path in (ROLE / directory).iterdir():
            found.update(re.findall(r"/app/scripts/([A-Za-z0-9._-]+\.sh)", path.read_text()))
    return found


class ImageContentsTest(unittest.TestCase):
    def test_the_role_runs_at_least_the_pruner_and_the_pressure_reporter_from_the_image(self) -> None:
        self.assertLessEqual({"node-cache-pruner.sh", "psi-reporter.sh"}, scripts_run_from_the_image())

    def test_every_file_autoscaler_sh_reads_from_its_own_directory_is_copied_beside_it(self) -> None:
        script = (ROOT / "scripts" / "autoscaler.sh").read_text()
        beside = re.findall(r'"\$\(dirname "\$0"\)/([A-Za-z0-9._-]+)"', script)
        self.assertIn("autoscaler-ceiling.jq", beside)
        dockerfile = DOCKERFILE.read_text()
        for name in beside:
            with self.subTest(name):
                self.assertTrue((ROOT / "scripts" / name).is_file(), name)
                self.assertIn(f"COPY scripts/{name} /app/scripts/{name}", dockerfile)

    def test_every_script_the_role_runs_from_the_image_is_copied_into_it_and_made_executable(self) -> None:
        dockerfile = DOCKERFILE.read_text()
        for script in scripts_run_from_the_image():
            with self.subTest(script):
                self.assertTrue((ROOT / "scripts" / script).is_file(), script)
                self.assertIn(f"COPY scripts/{script} /app/scripts/{script}", dockerfile)
                chmod = re.search(r"RUN chmod \+x ([^\n]+)", dockerfile)
                assert chmod is not None
                self.assertIn(f"/app/scripts/{script}", chmod.group(1).split())

    def test_the_image_installs_bash_on_an_alpine_whose_bash_is_at_least_4_4(self) -> None:
        # The scripts are written to run on any bash (an empty array expansion is guarded), and still ask for bash 4.4 or later of the image. Alpine's bash package by release, from its package index (https://dl-cdn.alpinelinux.org/alpine/v3.6/main/x86_64/APKINDEX.tar.gz, v3.7 and v3.20 likewise): 3.6 has 4.3.48, 3.7 has 4.4.19, and the image's 3.20 has 5.2.26. So 3.7 is the first Alpine release that qualifies.
        dockerfile = DOCKERFILE.read_text()
        base = re.search(r"^FROM alpine:(\d+)\.(\d+)", dockerfile, flags=re.MULTILINE)
        assert base is not None
        self.assertGreaterEqual((int(base.group(1)), int(base.group(2))), (3, 7))
        self.assertRegex(dockerfile, r"apk add --no-cache[^\n]*\bbash\b")

    def test_the_dockerfiles_header_says_the_release_workflow_builds_the_image(self) -> None:
        header = DOCKERFILE.read_text().split("FROM ")[0]
        self.assertIn("release workflow", header)
        self.assertNotIn("manual way", header)


class ReleaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow: dict[str, Any] = yaml.safe_load(RELEASE.read_text())
        cls.build = cls.workflow["jobs"]["build-images"]

    def test_the_autoscaler_image_is_built_and_pushed_on_every_release_under_the_repository_the_defaults_name(self) -> None:
        matrix = {entry["name"]: entry for entry in self.build["strategy"]["matrix"]["include"]}
        self.assertEqual(matrix["autoscaler"]["image"], IMAGE_REPOSITORY)
        self.assertEqual(matrix["autoscaler"]["dockerfile"], "autoscaler/Dockerfile")
        self.assertEqual(self.build["if"], "needs.release.outputs.published == 'true'")
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        self.assertTrue(defaults["github_runner_arc_autoscaler_image"].startswith(IMAGE_REPOSITORY + ":"))

    def test_the_images_are_tagged_with_the_released_version_the_defaults_are_stamped_with(self) -> None:
        build = next(step for step in self.build["steps"] if step.get("uses", "").startswith("docker/build-push-action"))
        self.assertEqual(build["with"]["context"], ".")
        meta = next(step for step in self.build["steps"] if step.get("id") == "meta")
        self.assertIn("type=semver,pattern={{version}},value=v${{ needs.release.outputs.version }}", meta["with"]["tags"])

    def test_the_images_are_built_from_the_tag_the_release_pushed_not_from_whatever_main_has_become(self) -> None:
        checkout = next(step for step in self.build["steps"] if step.get("uses", "").startswith("actions/checkout"))
        self.assertEqual(checkout["with"]["ref"], "v${{ needs.release.outputs.version }}")

    def test_the_stamp_covers_the_autoscaler_image_default(self) -> None:
        stamp = (ROOT / "scripts" / "release" / "stamp_version.py").read_text()
        self.assertIn('"github_runner_arc_autoscaler_image"', stamp)


if __name__ == "__main__":
    unittest.main()
