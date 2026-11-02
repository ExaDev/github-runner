"""Tests that publish-tool-images.yml publishes each matrix entry under a tag of its own: the build and merge matrices list the same entries, a build runner entry with a runner_id passes it to the Dockerfile's RUNNER_UID and RUNNER_GID and suffixes its tag, and one entry's digest artifacts are never matched by another entry's download pattern, which would join the wrong digests under a tag."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-tool-images.yml"
BUILD_DOCKERFILE = ROOT / "tool-images" / "build" / "Dockerfile"
ARCHITECTURES = ("amd64", "arm64")


def load() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(loaded, dict)
    return loaded


def steps(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    found = workflow["jobs"][job]["steps"]
    assert isinstance(found, list)
    return found


def step_with(workflow: dict[str, Any], job: str, key: str, value: str) -> dict[str, Any]:
    for step in steps(workflow, job):
        if step.get(key) == value:
            return step
    raise AssertionError(f"{job} has no step with {key} {value}")


def version_of(workflow: dict[str, Any], job: str, entry: dict[str, Any]) -> str:
    """The tag the job's image step gives an entry, by running its shell under the entry's environment."""
    step = step_with(workflow, job, "id", "image")
    env = {"OWNER": "example", "KIND": entry["kind"], "NAME": entry["name"], "RUNNER_ID": str(entry.get("runner_id", "")), "PATH": os.environ["PATH"]}
    output = ROOT / ".publish-tool-images-test-output"
    try:
        subprocess.run(["bash", "-c", step["run"]], cwd=ROOT, env={**env, "GITHUB_OUTPUT": str(output)}, check=True)
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    finally:
        output.unlink(missing_ok=True)
    return values["version"]


def expression(template: str, entry: dict[str, Any], **extra: str) -> str:
    """Evaluates the two expression shapes the workflow uses for names and build arguments: plain ${{ matrix.image.<key> }} and friends, and ${{ matrix.image.runner_id && format('<text>{0}', matrix.image.runner_id) || '' }}."""

    def one(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        conditional = re.fullmatch(r"matrix\.image\.runner_id && format\('([^']*)', matrix\.image\.runner_id\) \|\| ''", body)
        if conditional:
            return conditional.group(1).replace("{0}", str(entry["runner_id"])) if "runner_id" in entry else ""
        if body.startswith("matrix.image."):
            return str(entry.get(body.removeprefix("matrix.image."), ""))
        if body in extra:
            return extra[body]
        raise AssertionError(f"unexpected expression {body}")

    return re.sub(r"\$\{\{(.*?)\}\}", one, template)


class PublishToolImagesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = load()
        self.build_entries = self.workflow["jobs"]["build"]["strategy"]["matrix"]["image"]
        self.merge_entries = self.workflow["jobs"]["merge"]["strategy"]["matrix"]["image"]

    def test_build_and_merge_list_the_same_entries(self) -> None:
        self.assertEqual(self.build_entries, self.merge_entries)

    def test_a_runner_id_reaches_the_dockerfile_and_the_tag(self) -> None:
        variants = [entry for entry in self.build_entries if "runner_id" in entry]
        self.assertTrue(variants, "the matrix publishes no build runner image with a runner id")
        dockerfile = BUILD_DOCKERFILE.read_text()
        self.assertIn("ARG RUNNER_UID=", dockerfile)
        self.assertIn("ARG RUNNER_GID=", dockerfile)
        build_args = step_with(self.workflow, "build", "id", "build")["with"]["build-args"]
        for entry in variants:
            self.assertEqual(entry["name"], "build", "only the build runner image takes a runner id")
            arguments = [line for line in expression(build_args, entry).splitlines() if line.strip()]
            self.assertEqual(sorted(argument.strip() for argument in arguments), [f"RUNNER_GID={entry['runner_id']}", f"RUNNER_UID={entry['runner_id']}"])
            for job in ("build", "merge"):
                self.assertTrue(version_of(self.workflow, job, entry).endswith(f"-uid{entry['runner_id']}"))
        for entry in self.build_entries:
            if "runner_id" not in entry:
                self.assertEqual([line for line in expression(build_args, entry).splitlines() if line.strip()], [], f"{entry} gets build arguments")

    def test_each_tag_downloads_only_its_own_digests(self) -> None:
        upload = step_with(self.workflow, "build", "uses", "actions/upload-artifact@v7")["with"]["name"]
        download = step_with(self.workflow, "merge", "uses", "actions/download-artifact@v8")["with"]["pattern"]
        artifacts: dict[str, tuple[str, str]] = {}
        for entry in self.build_entries:
            version = version_of(self.workflow, "build", entry)
            self.assertEqual(version, version_of(self.workflow, "merge", entry))
            for arch in ARCHITECTURES:
                name = expression(upload, entry, **{"matrix.platform.arch": arch, "steps.image.outputs.version": version})
                self.assertNotIn(name, artifacts, f"two entries upload {name}")
                artifacts[name] = (f"{entry['kind']}-{entry['name']}", version)
        for entry in self.merge_entries:
            version = version_of(self.workflow, "merge", entry)
            pattern = expression(download, entry, **{"steps.image.outputs.version": version})
            matched = sorted(name for name in artifacts if fnmatch.fnmatchcase(name, pattern))
            self.assertEqual(len(matched), len(ARCHITECTURES), f"{pattern} matches {matched}")
            self.assertEqual({artifacts[name] for name in matched}, {(f"{entry['kind']}-{entry['name']}", version)})


if __name__ == "__main__":
    unittest.main()
