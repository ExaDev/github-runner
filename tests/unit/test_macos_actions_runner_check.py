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

    def test_existing_destination_and_registration_are_refused_before_install(self) -> None:
        names = list(self.entries)
        for name in ("Refuse any existing runner path", "Refuse an existing runner registration"):
            self.assertLess(names.index(name), names.index("Configure the enabled macOS runner"))
        self.assertIn("not github_runner_macos_actions_runner_directory.stat.exists", str(self.entries["Refuse any existing runner path"]))
        self.assertNotIn("--replace", self.install["Configure the macOS runner"]["ansible.builtin.command"]["argv"])

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

    def test_first_install_check_mode_skips_staging(self) -> None:
        stage = self.install["Stage and publish the first runner installation"]
        self.assertEqual(stage["when"], "not ansible_check_mode")
        self.assertIn("not ansible_check_mode", self.install["Configure the macOS runner"]["when"])

    def test_launchd_start_is_a_real_handler(self) -> None:
        handlers = yaml.safe_load((ROLE / "handlers" / "main.yml").read_text())
        listeners = [handler for handler in handlers if handler.get("listen") == "Restart macOS Actions runner"]
        self.assertEqual(len(listeners), 1)
        self.assertIn("ansible.builtin.command", listeners[0])


if __name__ == "__main__":
    unittest.main()
