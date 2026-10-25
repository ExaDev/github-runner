"""Tests for the Helm values a profile is installed with, rendered by ansible-playbook from the role's own defaults and vars for a profile arc_profiles expands: what github_runner_arc_image_pull_secret_source decides about the pull Secret (whether one is in use, and whether the values reference it), and the githubConfigUrl of an organisation or repository scope."""

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
          vars:
            profile: "{{{{ ([org] | exadev.github_runner.arc_profiles).profiles[0] }}}}"
    """
)

ORG = {"name": "ExampleOrg", "app_id": 1, "image": "ghcr.io/actions/actions-runner:2.338.0", "app_secret_name": "example-github-app", "scale_set_profiles": [{"release_name": "example-release", "scale_set_labels": ["example-pool"], "max_runners": 1}]}


def render(source: str, org: dict[str, Any] = ORG) -> dict[str, Any]:
    """Return the role's pull Secret decisions and Helm values for one source, with org's first profile."""
    with tempfile.TemporaryDirectory() as work:
        play = Path(work) / "play.yml"
        play.write_text(PLAYBOOK)
        output = Path(work) / "out.json"
        extra = Path(work) / "extra.json"
        # Extra variables, since the defaults file loaded by the play outranks play variables.
        extra.write_text(json.dumps({"output": str(output), "org": org, "github_runner_arc_image_pull_secret_source": source}))
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
                "runnerScaleSetName=example-release",
                "template.spec.containers[0].image=ghcr.io/actions/actions-runner:2.338.0",
                "scaleSetLabels[0]=example-pool",
            ],
        )

    def test_a_repository_entry_registers_with_the_repositorys_url(self) -> None:
        rendered = render("none", {**ORG, "name": "example-user", "repository": "example-repo"})
        self.assertEqual(set_values(rendered)[0], "githubConfigUrl=https://github.com/example-user/example-repo")


if __name__ == "__main__":
    unittest.main()
