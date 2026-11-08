"""Structural regression tests for the native macOS runner's install, upgrade and disable lifecycle."""

from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_macos_actions_runner"


def enabled_entries() -> dict[str, dict]:
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    block = next(task["block"] for task in tasks if task["name"] == "Configure the enabled macOS runner")
    return {task["name"]: task for task in block}


class MacOSActionsRunnerLifecycleTest(unittest.TestCase):
    def test_wiping_the_prior_installation_runs_as_root(self) -> None:
        # The directory is owned by the runner account with mode 0750, so the connecting user cannot remove it.
        self.assertIs(enabled_entries()["Remove the prior runner installation"].get("become"), True)


if __name__ == "__main__":
    unittest.main()
