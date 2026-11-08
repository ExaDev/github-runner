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

    def test_service_restarts_after_an_upgrade_or_a_fresh_registration(self) -> None:
        # The upgrade boots the service out and the launch daemon template is unchanged, so only these tasks can bring it back.
        entries = enabled_entries()
        for name in ("Install the pinned Apple Silicon runner package", "Configure the macOS runner"):
            self.assertEqual(entries[name].get("notify"), "Restart macOS Actions runner", name)

    def test_description_does_not_call_the_persistent_runner_ephemeral(self) -> None:
        # The runner registers once and keeps its installation between jobs, so it is not ephemeral.
        specs = yaml.safe_load((ROLE / "meta" / "argument_specs.yml").read_text())
        self.assertNotIn("ephemeral", specs["argument_specs"]["main"]["short_description"].lower())


if __name__ == "__main__":
    unittest.main()
