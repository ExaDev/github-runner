"""Filters the github_runner_cluster role's mesh providers share: parsing a HuJSON policy and checking it approves the pod routes."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ansible.errors import AnsibleFilterError


def hujson_to_data(text: str) -> Any:
    """Parse HuJSON (JSON with comments and trailing commas), the format Tailscale and Headscale policies use.

    :param text: the policy source. :returns: the parsed value. :raises AnsibleFilterError: if the text is not valid HuJSON.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == '"':
            end = index + 1
            while end < length and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            out.append(text[index : end + 1])
            index = end + 1
        elif text.startswith("//", index):
            newline = text.find("\n", index)
            index = length if newline == -1 else newline
        elif text.startswith("/*", index):
            close = text.find("*/", index + 2)
            if close == -1:
                raise AnsibleFilterError("Unterminated /* comment in HuJSON policy.")
            index = close + 2
        elif char in "]}":
            # A trailing comma is the last non-whitespace character emitted before a closing bracket.
            position = len(out) - 1
            while position >= 0 and out[position].isspace():
                position -= 1
            if position >= 0 and out[position] == ",":
                del out[position]
            out.append(char)
            index += 1
        else:
            out.append(char)
            index += 1
    try:
        return json.loads("".join(out))
    except json.JSONDecodeError as exc:
        raise AnsibleFilterError(f"The mesh policy is not valid HuJSON: {exc}") from exc


def _covers(approver_prefix: str, route: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    """Return whether an autoApprovers route prefix contains route."""
    try:
        approved = ipaddress.ip_network(approver_prefix, strict=False)
    except ValueError:
        return False
    return approved.version == route.version and route.subnet_of(approved)  # type: ignore[arg-type]


def mesh_policy_gaps(policy: Any, pod_cidr: str, tag: str) -> list[str]:
    """List what a tailnet policy is missing for the cluster's nodes to register and route pod traffic.

    :param policy: the parsed policy. :param pod_cidr: the cluster's pod CIDR, whose per-node slices every node advertises as a route. :param tag: the tag every node registers with. :returns: ``tagOwners`` and/or ``autoApprovers`` for each missing piece, empty when nothing is missing. :raises AnsibleFilterError: if pod_cidr is not a network.

    A route is approved when an autoApprovers.routes prefix equal to or containing the pod CIDR lists the tag, since every node's slice of the pod CIDR then falls inside it.
    """
    try:
        route = ipaddress.ip_network(pod_cidr, strict=False)
    except ValueError as exc:
        raise AnsibleFilterError(f"github_runner_cluster_pod_cidr is not a network: {exc}") from exc
    if not isinstance(policy, Mapping):
        return ["tagOwners", "autoApprovers"]
    gaps: list[str] = []
    tag_owners = policy.get("tagOwners")
    if not isinstance(tag_owners, Mapping) or tag not in tag_owners:
        gaps.append("tagOwners")
    auto_approvers = policy.get("autoApprovers")
    routes = auto_approvers.get("routes") if isinstance(auto_approvers, Mapping) else None
    approved = isinstance(routes, Mapping) and any(_covers(prefix, route) and isinstance(approvers, Sequence) and tag in approvers for prefix, approvers in routes.items())
    if not approved:
        gaps.append("autoApprovers")
    return gaps


def mesh_policy_snippet(gaps: Sequence[str], pod_cidr: str, tag: str, tag_owner: str) -> str:
    """Render the JSON to merge into a policy to close the gaps mesh_policy_gaps reported.

    :param gaps: the gap names. :param pod_cidr: the cluster's pod CIDR. :param tag: the node tag. :param tag_owner: who may assign the tag (a user such as ``admin@`` or a group). :returns: an indented JSON object holding only the missing sections.
    """
    snippet: dict[str, Any] = {}
    if "tagOwners" in gaps:
        snippet["tagOwners"] = {tag: [tag_owner] if tag_owner else []}
    if "autoApprovers" in gaps:
        snippet["autoApprovers"] = {"routes": {pod_cidr: [tag]}}
    return json.dumps(snippet, indent=2)


class FilterModule:
    """Registers the mesh filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "from_hujson": hujson_to_data,
            "mesh_policy_gaps": mesh_policy_gaps,
            "mesh_policy_snippet": mesh_policy_snippet,
        }
