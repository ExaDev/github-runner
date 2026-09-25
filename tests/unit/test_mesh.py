"""Tests for the github_runner_cluster role's mesh filters: HuJSON parsing and the policy check."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "filter" / "mesh.py"
_spec = importlib.util.spec_from_file_location("mesh", PLUGIN)
assert _spec is not None and _spec.loader is not None
mesh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mesh)

TAG = "tag:github-runner-node"
POD_CIDR = "10.42.0.0/16"


class HujsonTest(unittest.TestCase):
    def test_comments_and_trailing_commas_are_accepted(self) -> None:
        text = """
        // leading comment
        {
          "tagOwners": {"tag:a": ["admin@",],}, /* block
          comment */
          "hosts": {"url": "http://example.com/a//b", "quote": "a \\" // not a comment",},
        }
        """
        self.assertEqual(
            mesh.hujson_to_data(text),
            {"tagOwners": {"tag:a": ["admin@"]}, "hosts": {"url": "http://example.com/a//b", "quote": 'a " // not a comment'}},
        )

    def test_invalid_text_is_an_error(self) -> None:
        with self.assertRaises(mesh.AnsibleFilterError):
            mesh.hujson_to_data('{"a": }')


class PolicyGapsTest(unittest.TestCase):
    def complete(self) -> dict:
        return {"tagOwners": {TAG: []}, "autoApprovers": {"routes": {POD_CIDR: [TAG]}}}

    def test_a_complete_policy_has_no_gaps(self) -> None:
        self.assertEqual(mesh.mesh_policy_gaps(self.complete(), POD_CIDR, TAG), [])

    def test_a_wider_approved_prefix_covers_the_pod_cidr(self) -> None:
        policy = {"tagOwners": {TAG: ["admin@"]}, "autoApprovers": {"routes": {"10.0.0.0/8": ["group:ops", TAG]}}}
        self.assertEqual(mesh.mesh_policy_gaps(policy, POD_CIDR, TAG), [])

    def test_a_narrower_prefix_or_other_tag_does_not_approve_the_routes(self) -> None:
        for routes in ({"10.42.0.0/24": [TAG]}, {POD_CIDR: ["tag:other"]}, {"fd00::/8": [TAG]}):
            with self.subTest(routes=routes):
                policy = {"tagOwners": {TAG: []}, "autoApprovers": {"routes": routes}}
                self.assertEqual(mesh.mesh_policy_gaps(policy, POD_CIDR, TAG), ["autoApprovers"])

    def test_missing_sections_are_reported(self) -> None:
        self.assertEqual(mesh.mesh_policy_gaps({"grants": []}, POD_CIDR, TAG), ["tagOwners", "autoApprovers"])
        self.assertEqual(mesh.mesh_policy_gaps(None, POD_CIDR, TAG), ["tagOwners", "autoApprovers"])

    def test_the_snippet_holds_only_what_is_missing_and_closes_the_gaps(self) -> None:
        snippet = mesh.mesh_policy_snippet(["autoApprovers"], POD_CIDR, TAG, "admin@")
        self.assertEqual(json.loads(snippet), {"autoApprovers": {"routes": {POD_CIDR: [TAG]}}})
        both = json.loads(mesh.mesh_policy_snippet(["tagOwners", "autoApprovers"], POD_CIDR, TAG, "admin@"))
        self.assertEqual(both["tagOwners"], {TAG: ["admin@"]})
        self.assertEqual(mesh.mesh_policy_gaps(both, POD_CIDR, TAG), [])


if __name__ == "__main__":
    unittest.main()
