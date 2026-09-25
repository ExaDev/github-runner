"""Tests for the 1Password secrets adapter's filters."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "filter" / "onepassword.py"
_spec = importlib.util.spec_from_file_location("onepassword", PLUGIN)
assert _spec is not None and _spec.loader is not None
onepassword = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(onepassword)


class FillOrgKeysTest(unittest.TestCase):
    def test_a_reference_is_replaced_by_the_key_read_for_it(self) -> None:
        orgs = [{"name": "A", "app_id": 1, "private_key_op_reference": "op://v/a/key"}]
        self.assertEqual(onepassword.onepassword_fill_org_keys(orgs, {"A": "pem-a"}), [{"name": "A", "app_id": 1, "private_key": "pem-a"}])

    def test_an_org_without_a_reference_is_left_alone(self) -> None:
        orgs = [{"name": "A", "private_key": "own"}, {"name": "B", "private_key_op_reference": "op://v/b/key"}]
        self.assertEqual(onepassword.onepassword_fill_org_keys(orgs, {"A": "ignored", "B": "pem-b"}), [{"name": "A", "private_key": "own"}, {"name": "B", "private_key": "pem-b"}])

    def test_the_input_is_not_changed(self) -> None:
        orgs = [{"name": "A", "private_key_op_reference": "op://v/a/key"}]
        onepassword.onepassword_fill_org_keys(orgs, {"A": "pem-a"})
        self.assertEqual(orgs, [{"name": "A", "private_key_op_reference": "op://v/a/key"}])

    def test_a_reference_with_no_key_read_is_an_error(self) -> None:
        with self.assertRaisesRegex(onepassword.AnsibleFilterError, "for org A"):
            onepassword.onepassword_fill_org_keys([{"name": "A", "private_key_op_reference": "op://v/a/key"}], {})

    def test_a_non_list_is_an_error(self) -> None:
        with self.assertRaises(onepassword.AnsibleFilterError):
            onepassword.onepassword_fill_org_keys("nope", {})  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
