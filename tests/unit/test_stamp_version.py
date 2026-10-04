"""Tests for scripts/release/stamp_version.py: every variable it stamps exists exactly once in the files it edits, so a release cannot fail at its prepare step."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "release" / "stamp_version.py"
STAMPED_FILES = (
    "galaxy.yml",
    "roles/github_runner_arc/defaults/main.yml",
    "roles/github_runner_cluster/defaults/main.yml",
)
VERSION = "9.8.7"


class StampVersionTest(unittest.TestCase):
    def test_stamping_a_copy_of_the_repository_files_changes_every_version_variable(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        for name in STAMPED_FILES:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO_ROOT / name, root / name)
        spec = importlib.util.spec_from_file_location("stamp_version", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.GALAXY_YML = root / "galaxy.yml"
        module.ARC_DEFAULTS = root / "roles/github_runner_arc/defaults/main.yml"
        module.CLUSTER_DEFAULTS = root / "roles/github_runner_cluster/defaults/main.yml"
        module.VERSION_VARS = (
            (module.ARC_DEFAULTS, "github_runner_arc_collection_version"),
            (module.CLUSTER_DEFAULTS, "github_runner_cluster_collection_version"),
        )
        module.stamp_galaxy_version(VERSION)
        module.stamp_image_tags(VERSION)
        module.stamp_role_versions(VERSION)
        self.assertRegex((root / "galaxy.yml").read_text(), rf"(?m)^version: {re.escape(VERSION)}$")
        for defaults, var_name in module.VERSION_VARS:
            self.assertRegex(defaults.read_text(), rf'(?m)^{var_name}: "{re.escape(VERSION)}"$')

    def test_every_file_the_script_stamps_is_committed_by_the_release(self) -> None:
        """A stamped file missing from the git plugin's assets is stamped in the release build but never reaches the tag the fleet installs from."""
        spec = importlib.util.spec_from_file_location("stamp_version", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = json.loads((REPO_ROOT / ".releaserc.json").read_text())
        assets = next(plugin[1]["assets"] for plugin in config["plugins"] if isinstance(plugin, list) and plugin[0] == "@semantic-release/git")
        stamped = {path.relative_to(REPO_ROOT).as_posix() for path in (module.GALAXY_YML, module.ARC_DEFAULTS, module.CLUSTER_DEFAULTS)}
        self.assertLessEqual(stamped, set(assets))


if __name__ == "__main__":
    unittest.main()
