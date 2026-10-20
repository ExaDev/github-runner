"""Tests for what the tailscale mesh provider's API tasks (roles/github_runner_cluster/tasks/mesh/tailscale_api.yml) send to Tailscale's API."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from typing import Any

import yaml

TASKS = Path(__file__).resolve().parents[2] / "roles" / "github_runner_cluster" / "tasks" / "mesh" / "tailscale_api.yml"


def key_request() -> dict[str, Any]:
    """The body of the request that creates the join key."""
    tasks: list[dict[str, Any]] = yaml.safe_load(TASKS.read_text())
    task = next(task for task in tasks if task.get("name", "").startswith("Create a reusable, pre-authorized Tailscale join key"))
    body: dict[str, Any] = task["ansible.builtin.uri"]["body"]
    return body


class JoinKeyRequestTest(unittest.TestCase):
    def test_the_description_uses_only_characters_the_api_accepts(self) -> None:
        # The API answers 400 "keys: description had invalid characters" for parentheses, commas and colons; letters, digits, spaces and hyphens were accepted.
        self.assertRegex(key_request()["description"], re.compile(r"\A[A-Za-z0-9 -]+\Z"))

    def test_the_key_is_reusable_preauthorised_tagged_and_not_ephemeral(self) -> None:
        create = key_request()["capabilities"]["devices"]["create"]
        self.assertEqual((create["reusable"], create["ephemeral"], create["preauthorized"]), (True, False, True))
        self.assertEqual(create["tags"], ["{{ github_runner_cluster_mesh_tag }}"])


if __name__ == "__main__":
    unittest.main()
