"""Filters for the 1Password secrets adapter: filling each org's App private key from what 1Password returned for its reference."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ansible.errors import AnsibleFilterError


def onepassword_fill_org_keys(orgs: Sequence[Mapping[str, Any]], org_private_keys: Mapping[str, str]) -> list[dict[str, Any]]:
    """Give every org that names its App private key by 1Password reference the key itself.

    :param orgs: github_runner_arc_orgs entries. :param org_private_keys: the keys read from 1Password, by 1Password reference, so entries of one login (an organisation and its repositories) that name different keys each get their own, and entries naming one reference share it. :returns: the entries in the same order, each one with a private_key_op_reference replaced by private_key holding the key read for it, and every other entry unchanged. :raises AnsibleFilterError: if orgs is not a list, or no key was read for an org that has a reference.
    """
    if not isinstance(orgs, Sequence) or isinstance(orgs, (str, bytes)):
        raise AnsibleFilterError("github_runner_arc_orgs must be a list")
    filled: list[dict[str, Any]] = []
    for org in orgs:
        entry = dict(org)
        if "private_key_op_reference" in entry:
            reference = entry.pop("private_key_op_reference")
            if reference not in org_private_keys:
                raise AnsibleFilterError(f"No App private key was read from 1Password for {entry.get('name')} ({reference})")
            entry["private_key"] = org_private_keys[reference]
        filled.append(entry)
    return filled


class FilterModule:
    """Registers the 1Password adapter's filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {"onepassword_fill_org_keys": onepassword_fill_org_keys}
