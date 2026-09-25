"""Filters the github_runner_cluster role's mesh providers share: parsing a HuJSON policy, checking it approves the pod routes, and judging a Headscale join key."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ansible.errors import AnsibleFilterError

HEADSCALE_KEY_PREFIX = "hskey-auth-"
MASKED_KEY_SUFFIX = "-***"


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


def _parse_time(value: Any) -> datetime | None:
    """Parse an RFC 3339 timestamp from the Headscale API, returning None for an absent or zero one."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    # Headscale emits nanosecond precision, which fromisoformat only accepts up to microseconds.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())
        zone = tail[len(digits) :]
        text = f"{head}.{digits[:6]}{zone}"
    parsed = datetime.fromisoformat(text)
    if parsed.year <= 1:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _identifies(listed_key: str, join_key: str) -> bool:
    """Return whether a key as the API lists it (masked to its prefix for current keys) is join_key."""
    if listed_key.endswith(MASKED_KEY_SUFFIX) and listed_key.startswith(HEADSCALE_KEY_PREFIX):
        return join_key.startswith(listed_key[: -len(MASKED_KEY_SUFFIX)] + "-")
    return listed_key == join_key


def headscale_join_key_problem(keys: Sequence[Mapping[str, Any]], join_key: str, tag: str, now: Any, renew_days: Any) -> str:
    """Judge whether a known Headscale join key can keep being used for new nodes.

    :param keys: the ``preAuthKeys`` list from GET /api/v1/preauthkey. :param join_key: the key the cluster would use, possibly empty. :param tag: the tag the key must apply. :param now: the current time, a datetime or RFC 3339 string. :param renew_days: how many days before expiry a key is treated as due for replacement. :returns: an empty string when the key is usable, otherwise why it is not. :raises AnsibleFilterError: if renew_days is not an integer.
    """
    try:
        margin = timedelta(days=int(renew_days))
    except (TypeError, ValueError) as exc:
        raise AnsibleFilterError(f"github_runner_cluster_headscale_join_key_renew_days must be an integer: {exc}") from exc
    if not join_key:
        return "no join key is known"
    current = now if isinstance(now, datetime) else _parse_time(now)
    if current is None:
        raise AnsibleFilterError(f"Cannot parse the current time {now!r}.")
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    match = next((key for key in keys if _identifies(str(key.get("key", "")), join_key)), None)
    if match is None:
        return "the join key is not one of this Headscale server's pre-auth keys"
    if not match.get("reusable"):
        return "the join key is not reusable"
    if match.get("ephemeral"):
        return "the join key is ephemeral"
    if tag not in (match.get("aclTags") or []):
        return f"the join key does not apply {tag}"
    expiry = _parse_time(match.get("expiration"))
    if expiry is not None and expiry - current < margin:
        return f"the join key expires at {expiry.isoformat()}, within {margin.days} days"
    return ""


def headscale_expiry(now: Any, days: Any) -> str:
    """Return the RFC 3339 time days after now, for a new key's expiration."""
    current = now if isinstance(now, datetime) else _parse_time(now)
    if current is None:
        raise AnsibleFilterError(f"Cannot parse the current time {now!r}.")
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current + timedelta(days=int(days))).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FilterModule:
    """Registers the mesh filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "from_hujson": hujson_to_data,
            "mesh_policy_gaps": mesh_policy_gaps,
            "mesh_policy_snippet": mesh_policy_snippet,
            "headscale_join_key_problem": headscale_join_key_problem,
            "headscale_expiry": headscale_expiry,
        }
