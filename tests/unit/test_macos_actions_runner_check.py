"""Check-mode regression tests for a macOS runner on a host with no runner account."""

from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_macos_actions_runner"


class MacOSActionsRunnerCheckTest(unittest.TestCase):
    def test_first_install_does_not_become_the_not_yet_created_user(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        enabled = next(task for task in tasks if task["name"] == "Configure the enabled macOS runner")
        entries = {task["name"]: task for task in enabled["block"]}
        self.assertIn("not ansible_check_mode or github_runner_macos_actions_runner_account.rc == 0", entries["Check the installed runner version"]["when"])
        for name in ("Download the pinned Apple Silicon runner package", "Install the pinned Apple Silicon runner package", "Configure the macOS runner"):
            self.assertIn("not ansible_check_mode", entries[name]["when"])

    def test_launchd_restart_listeners_are_real_handlers(self) -> None:
        handlers = yaml.safe_load((ROLE / "handlers" / "main.yml").read_text())
        listeners = [handler for handler in handlers if handler.get("listen") == "Restart macOS Actions runner"]
        self.assertEqual(len(listeners), 2)
        self.assertTrue(all("ansible.builtin.command" in handler for handler in listeners))


if __name__ == "__main__":
    unittest.main()
