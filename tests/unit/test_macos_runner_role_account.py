"""Regression coverage for the native macOS runner's non-interactive account."""

from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_macos_actions_runner"


class RunnerRoleAccountTest(unittest.TestCase):
    def test_role_account_has_macos_required_name_and_uid(self) -> None:
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        self.assertTrue(defaults["github_runner_macos_actions_runner_user"].startswith("_"))
        self.assertIn(defaults["github_runner_macos_actions_runner_uid"], range(450, 500))

    def test_creation_is_verified_instead_of_trusting_sysadminctl_exit(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        block = next(task["block"] for task in tasks if task["name"] == "Configure the enabled macOS runner")
        entries = {task["name"]: task for task in block}
        argv = entries["Create the unprivileged runner account"]["ansible.builtin.command"]["argv"]
        self.assertIn("-roleAccount", argv)
        self.assertNotIn("-password", argv)
        self.assertIn("github_runner_macos_actions_runner_verified_account.stdout", str(entries["Require the runner account's expected identity"]["ansible.builtin.assert"]))


if __name__ == "__main__":
    unittest.main()
