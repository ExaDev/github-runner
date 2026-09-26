"""Tests for the GitHub App setup's first phase (playbooks/github_app_setup.yml without a code): the form it renders and the checks it makes before any code exists. Runs the real playbook, which touches neither GitHub nor a cluster in this phase."""

from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "playbooks" / "github_app_setup.yml"


def run_phase_one(extra_vars: dict) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    """Run phase 1 with extra_vars, returning the process and every task result the json callback reported."""
    with tempfile.TemporaryDirectory() as directory:
        env = {
            **os.environ,
            "ANSIBLE_STDOUT_CALLBACK": "json",
            "ANSIBLE_LOCALHOST_WARNING": "false",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
            "ANSIBLE_LOCAL_TEMP": directory,
            "ANSIBLE_HOME": directory,
        }
        result = subprocess.run(
            ["ansible-playbook", str(PLAYBOOK), "-e", json.dumps(extra_vars)],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            cwd=REPO / "ansible",
        )
    report = json.loads(result.stdout)
    tasks = [task["hosts"]["localhost"] | {"task": task["task"]["name"].rpartition(" : ")[2]} for play in report["plays"] for task in play["tasks"] if "localhost" in task["hosts"]]
    return result, tasks


def form_text(tasks: list[dict]) -> str:
    """Return the rendered form's text, read from the path phase 1 reported, and remove the file."""
    [render] = [task for task in tasks if task["task"] == "Render the form" and not task.get("skipped")]
    path = Path(render["dest"])
    try:
        return html.unescape(path.read_text())
    finally:
        path.unlink()


BASE = {"github_runner_arc_app_setup_org": "example-org", "github_runner_arc_app_setup_name": "Example runners"}


@unittest.skipIf(shutil.which("ansible-playbook") is None, "ansible-playbook is not installed")
class AppSetupPhaseOneTest(unittest.TestCase):
    def test_default_form_names_the_collection_playbook(self) -> None:
        result, tasks = run_phase_one(BASE)
        self.assertEqual(result.returncode, 0, result.stdout[-2000:])
        text = form_text(tasks)
        self.assertIn(
            """ansible-playbook playbooks/github_app_setup.yml -e github_runner_arc_app_setup_org=example-org -e '{"github_runner_arc_app_setup_name": "Example runners"}' -e github_runner_arc_app_manifest_code=<code> -e github_runner_arc_app_private_key_path=<path for the key>""",
            text,
        )
        self.assertNotIn("writes the App Secret", text)

    def test_a_wrapper_names_itself_in_the_form_and_the_instructions(self) -> None:
        result, tasks = run_phase_one(BASE | {"github_runner_arc_app_setup_command": "ansible-playbook app-setup.yml", "github_runner_arc_app_setup_write_secret": True, "github_runner_arc_app_setup_secret_namespaces": ["example-runners"]})
        self.assertEqual(result.returncode, 0, result.stdout[-2000:])
        text = form_text(tasks)
        self.assertIn("ansible-playbook app-setup.yml -e github_runner_arc_app_manifest_code=<code> -e github_runner_arc_app_private_key_path=<path for the key>", text)
        self.assertNotIn("playbooks/github_app_setup.yml", text)
        self.assertIn("It then writes the App Secret into the cluster and removes the key file.", text)
        [explain] = [task for task in tasks if task["task"] == "Explain the remaining steps"]
        self.assertIn("Then run: ansible-playbook app-setup.yml -e github_runner_arc_app_manifest_code=<code>", " ".join(explain["msg"]))

    def test_keeping_the_key_is_reflected_in_the_form(self) -> None:
        _, tasks = run_phase_one(BASE | {"github_runner_arc_app_setup_write_secret": True, "github_runner_arc_app_setup_secret_namespaces": ["example-runners"], "github_runner_arc_app_setup_keep_private_key": True})
        text = form_text(tasks)
        self.assertIn("It then writes the App Secret into the cluster.", text)
        self.assertNotIn("removes the key file", text)

    def test_secret_targets_default_to_the_orgs_profiles(self) -> None:
        orgs = [{"name": "example-org", "image": "ghcr.io/example-org/runner:1", "app_secret_name": "example-app", "scale_set_profiles": [{"suffix": "", "max_runners": 1}, {"suffix": "-big", "namespace": "big-runners", "max_runners": 1}]}]
        result, tasks = run_phase_one(BASE | {"github_runner_arc_app_setup_write_secret": True, "github_runner_arc_orgs": orgs})
        self.assertEqual(result.returncode, 0, result.stdout[-2000:])
        form_text(tasks)
        [targets] = [task for task in tasks if task["task"] == "Work out where the App Secret goes"]
        self.assertEqual(targets["ansible_facts"]["github_runner_arc_app_setup_secret_targets"], [["arc-runners-example-org", "example-app"], ["big-runners", "example-app"]])

    def test_explicit_namespaces_and_the_default_name(self) -> None:
        _, tasks = run_phase_one(BASE | {"github_runner_arc_app_setup_write_secret": True, "github_runner_arc_app_setup_secret_namespaces": ["one", "two"]})
        form_text(tasks)
        [targets] = [task for task in tasks if task["task"] == "Work out where the App Secret goes"]
        self.assertEqual(targets["ansible_facts"]["github_runner_arc_app_setup_secret_targets"], [["one", "example-org-github-app"], ["two", "example-org-github-app"]])

    def test_writing_the_secret_with_nowhere_to_write_it_fails_in_phase_one(self) -> None:
        result, tasks = run_phase_one(BASE | {"github_runner_arc_app_setup_write_secret": True})
        self.assertNotEqual(result.returncode, 0)
        [failure] = [task for task in tasks if task["task"] == "Fail when there is nowhere to write the App Secret"]
        self.assertIn("set github_runner_arc_app_setup_secret_namespaces", failure["msg"])
        self.assertFalse([task for task in tasks if task["task"] == "Render the form" and not task.get("skipped")])


if __name__ == "__main__":
    unittest.main()
