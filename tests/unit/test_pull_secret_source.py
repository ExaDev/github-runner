"""Tests for what github_runner_arc_image_pull_secret_source decides about the pull Secret, rendered by ansible-playbook from the role's own defaults and vars: whether one is in use, and whether the Helm values a profile is installed with reference it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_arc"

PLAYBOOK = textwrap.dedent(
    f"""\
    - name: Render the pull Secret decisions as the role does
      hosts: localhost
      connection: local
      gather_facts: false
      vars_files:
        - {ROLE / "defaults" / "main.yml"}
        - {ROLE / "vars" / "main.yml"}
      tasks:
        - name: Write them out
          ansible.builtin.copy:
            content: "{{{{ {{'in_use': github_runner_arc_pull_secret_in_use | bool, 'set_values': github_runner_arc_scale_set_set_values}} | to_json }}}}"
            dest: "{{{{ output }}}}"
            mode: "0644"
    """
)

ORG = {"name": "ExampleOrg", "image": "ghcr.io/actions/actions-runner:2.338.0"}
PROFILE = {"app_secret": "example-github-app", "scale_set_labels": ["example-pool"]}


def render(source: str) -> dict[str, Any]:
    """Return the role's pull Secret decisions for one source, with the example org and profile."""
    with tempfile.TemporaryDirectory() as work:
        play = Path(work) / "play.yml"
        play.write_text(PLAYBOOK)
        output = Path(work) / "out.json"
        extra = Path(work) / "extra.json"
        # Extra variables, since the defaults file loaded by the play outranks play variables.
        extra.write_text(json.dumps({"output": str(output), "org": ORG, "profile": PROFILE, "github_runner_arc_image_pull_secret_source": source}))
        env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_HOME": work, "ANSIBLE_LOCAL_TEMP": work}
        subprocess.run([os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(play), "-e", f"@{extra}"], check=True, env=env, cwd=work, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        rendered: dict[str, Any] = json.loads(output.read_text())
        return rendered


def set_values(rendered: dict[str, Any]) -> list[str]:
    return [entry["value"] for entry in rendered["set_values"]]


@unittest.skipIf(shutil.which(os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")) is None, "ansible-playbook is not installed")
class PullSecretSourceTest(unittest.TestCase):
    def test_a_written_pull_secret_is_referenced_by_the_runner_pods(self) -> None:
        for source in ("static", "app"):
            with self.subTest(source=source):
                rendered = render(source)
                self.assertTrue(rendered["in_use"])
                self.assertIn("template.spec.imagePullSecrets[0].name=ghcr-pull", set_values(rendered))

    def test_none_leaves_the_pull_secret_out_and_keeps_the_rest(self) -> None:
        rendered = render("none")
        self.assertFalse(rendered["in_use"])
        self.assertEqual(
            set_values(rendered),
            [
                "githubConfigUrl=https://github.com/ExampleOrg",
                "githubConfigSecret=example-github-app",
                "template.spec.containers[0].image=ghcr.io/actions/actions-runner:2.338.0",
                "scaleSetLabels[0]=example-pool",
            ],
        )


if __name__ == "__main__":
    unittest.main()
