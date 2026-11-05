"""Safety checks for first-install-only macOS runner provisioning."""

from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_macos_actions_runner"


class MacOSActionsRunnerCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        self.entries = {task["name"]: task for task in self.tasks}
        enabled = self.entries["Configure the enabled macOS runner"]
        self.install = {task["name"]: task for task in enabled["block"]}

    def test_empty_root_owned_directory_is_populated_without_removal(self) -> None:
        self.assertTrue(self.entries["Inspect the existing runner directory contents"]["become"])
        find = self.entries["Inspect the existing runner directory contents"]["ansible.builtin.find"]
        self.assertTrue(find["hidden"])
        self.assertEqual(find["file_type"], "any")
        self.assertIn("matched == 0", str(self.entries["Refuse a non-empty runner directory"]))
        self.assertTrue(self.install["Install the pinned Apple Silicon runner package"]["become"])
        self.assertNotIn("state: absent", str(self.install["Create the runner directory"]))
        self.assertNotIn("Remove the prior runner installation", self.install)

    def test_registration_is_checked_before_changes_and_matching_install_is_untouched(self) -> None:
        names = list(self.entries)
        self.assertLess(names.index("Refuse an upgrade or unrecognised registration"), names.index("Configure the enabled macOS runner"))
        self.assertLess(names.index("Leave an existing matching registration untouched"), names.index("Configure the enabled macOS runner"))
        assertion = str(self.entries["Refuse an upgrade or unrecognised registration"])
        self.assertIn("existing_version.rc == 0", assertion)
        self.assertIn("existing_version.stdout | trim ==", assertion)
        argv = self.install["Configure the macOS runner"]["ansible.builtin.command"]["argv"]
        self.assertNotIn("--replace", argv)

    def test_busy_or_unknown_service_and_disable_fail_before_changes(self) -> None:
        self.assertIn("service.rc in [0, 113]", str(self.entries["Refuse unknown launchd service state"]))
        self.assertIn("service.rc == 113", str(self.entries["Refuse an existing launchd service"]))
        disabled = str(self.entries["Refuse disabling an existing runner"])
        self.assertIn("directory.stat.exists", disabled)
        self.assertIn("registration.stat.exists", disabled)
        self.assertIn("service.rc == 113", disabled)
        self.assertNotIn("bootout", str(self.tasks))
        handlers = yaml.safe_load((ROLE / "handlers" / "main.yml").read_text())
        self.assertNotIn("bootout", str(handlers))

    def test_first_install_check_mode_does_not_install_or_register(self) -> None:
        for name in ("Download the pinned Apple Silicon runner package", "Install the pinned Apple Silicon runner package", "Configure the macOS runner"):
            self.assertIn("not ansible_check_mode", self.install[name]["when"])
        self.assertTrue(self.install["Register the runner through the GitHub App"]["check_mode"] is False)

    def test_launchd_start_is_a_real_handler(self) -> None:
        handlers = yaml.safe_load((ROLE / "handlers" / "main.yml").read_text())
        listeners = [handler for handler in handlers if handler.get("listen") == "Restart macOS Actions runner"]
        self.assertEqual(len(listeners), 1)
        self.assertIn("ansible.builtin.command", listeners[0])


if __name__ == "__main__":
    unittest.main()
