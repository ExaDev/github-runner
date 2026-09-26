"""Tests for the github_runner_arc role's checks of the Secrets it does not write (tasks/check_existing_secret_contents.yml), driven with lookup results shaped as kubernetes.core.k8s_info returns them, so no cluster is needed. The checks must name what is wrong without ever printing a Secret's data, even at high verbosity."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nexample-key-material-that-must-never-be-printed\n-----END RSA PRIVATE KEY-----\n"

PLAYBOOK = textwrap.dedent(
    """\
    - name: Check existing Secrets as the role does
      hosts: localhost
      connection: local
      gather_facts: false
      vars: {play_vars}
      tasks:
        - name: Expand the orgs as validation does
          ansible.builtin.set_fact:
            github_runner_arc_local: "{{{{ github_runner_arc_orgs | exadev.github_runner.arc_profiles(require_app_id=false) }}}}"

        - name: Check the looked-up Secrets
          ansible.builtin.include_role:
            name: exadev.github_runner.github_runner_arc
            tasks_from: check_existing_secret_contents.yml
    """
)


def encode(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def app_secret(data: dict[str, str] | None) -> dict[str, Any]:
    """Return a lookup result for the App Secret with data, or for no Secret at all when data is None."""
    resources = [] if data is None else [{"kind": "Secret", "data": {key: encode(value) for key, value in data.items()}}]
    return {"item": ["example-runners", "example-github-app"], "resources": resources}


FULL = {"github_app_id": "123", "github_app_installation_id": "456", "github_app_private_key": PRIVATE_KEY}


def run(result: dict[str, Any], app_id: Any = None, manage_secrets: bool = False) -> subprocess.CompletedProcess[str]:
    """Run the checks against one App Secret lookup result, at -vvv, with the org's app_id set when app_id is not None."""
    org: dict[str, Any] = {"name": "example-org", "image": "ghcr.io/example/runner:1", "app_secret_name": "example-github-app", "scale_set_profiles": [{"namespace": "example-runners", "max_runners": 1}]}
    if app_id is not None:
        org["app_id"] = app_id
    play_vars = {"github_runner_arc_orgs": [org], "github_runner_arc_manage_secrets": manage_secrets, "github_runner_arc_existing_secrets": {"results": [result]}}
    with tempfile.TemporaryDirectory() as directory:
        play = Path(directory) / "play.yml"
        play.write_text(PLAYBOOK.format(play_vars=json.dumps(play_vars)))
        env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(REPO / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_HOME": directory, "ANSIBLE_LOCAL_TEMP": directory}
        return subprocess.run(["ansible-playbook", "-vvv", str(play)], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)


@unittest.skipIf(shutil.which("ansible-playbook") is None, "ansible-playbook is not installed")
class ExistingSecretsTest(unittest.TestCase):
    def assert_nothing_leaked(self, result: subprocess.CompletedProcess[str]) -> None:
        output = result.stdout + result.stderr
        for secret in ("example-key-material-that-must-never-be-printed", encode(PRIVATE_KEY)):
            self.assertNotIn(secret, output)

    def test_a_complete_app_secret_passes_without_an_app_id(self) -> None:
        result = run(app_secret(FULL))
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])
        self.assert_nothing_leaked(result)

    def test_an_app_id_matching_the_secret_passes(self) -> None:
        result = run(app_secret(FULL), app_id=123)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])

    def test_an_app_id_disagreeing_with_the_secret_fails(self) -> None:
        result = run(app_secret(FULL), app_id=999)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("App Secret example-runners/example-github-app of example-org holds github_app_id 123, but the org sets app_id 999. Correct or remove the org's app_id.", result.stdout)
        self.assert_nothing_leaked(result)

    def test_a_secret_missing_keys_fails_naming_them(self) -> None:
        result = run(app_secret({"github_app_id": "123", "github_app_private_key": PRIVATE_KEY}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no github_app_installation_id; it needs github_app_id, github_app_installation_id and github_app_private_key.", result.stdout)
        self.assert_nothing_leaked(result)

    def test_a_missing_secret_fails(self) -> None:
        result = run(app_secret(None))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Secret example-github-app does not exist in namespace example-runners.", result.stdout)

    def test_a_host_writing_its_app_secrets_does_not_check_their_contents(self) -> None:
        result = run(app_secret({"github_app_id": "123"}), app_id=999, manage_secrets=True)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:])


if __name__ == "__main__":
    unittest.main()
