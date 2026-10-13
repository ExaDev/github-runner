"""Filters the github_runner_cluster role uses to check the versions every node's k3s image is built from: that each is a pinned tag, that every host in a cluster group names the same k3s minor version, and that the version keeps to k3s's upgrade path and Kubernetes' version skew policy from what the cluster runs."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ansible.errors import AnsibleFilterError

# A rancher/k3s image tag: k3s's own version with "+" written as "-", for example v1.34.1-k3s1.
K3S_TAG = re.compile(r"^v(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)-k3s(?P<build>[0-9]+)$")
# A tailscale/tailscale image tag for a release, for example v1.102.4.
TAILSCALE_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")


def cluster_version_errors(k3s_version: Any, tailscale_version: Any) -> list[str]:
    """Say what is wrong with the image versions a node is to be built from, or return an empty list.

    :param k3s_version: the rancher/k3s tag, for example v1.34.1-k3s1.
    :param tailscale_version: the tailscale/tailscale tag, for example v1.102.4.
    :returns: one sentence per problem.
    """
    errors: list[str] = []
    if not isinstance(k3s_version, str) or K3S_TAG.match(k3s_version.strip()) is None:
        errors.append(
            f"github_runner_cluster_k3s_version is {k3s_version!r}; it must be a pinned rancher/k3s tag such as v1.34.1-k3s1"
            " (k3s's version with '+' written as '-'), never latest, so that every node is built from the same k3s."
        )
    if not isinstance(tailscale_version, str) or TAILSCALE_TAG.match(tailscale_version.strip()) is None:
        errors.append(f"github_runner_cluster_tailscale_version is {tailscale_version!r}; it must be a pinned tailscale/tailscale release tag such as v1.102.4, never latest.")
    return errors


def cluster_k3s_minor_skew(hosts: Any, hostvars: Any, default: Any) -> list[str]:
    """Return every host's k3s version when the hosts of a cluster group do not all name the same k3s minor version, or an empty list when they do.

    :param hosts: the cluster group's host names.
    :param hostvars: Ansible's hostvars, which hold each host's inventory and extra variables but never role defaults; a host without github_runner_cluster_k3s_version of its own uses ``default``.
    :param default: the version that applies where a host sets none: the role's own default (github_runner_cluster_k3s_tested_version), which is the same whichever host runs the check. Never the running host's github_runner_cluster_k3s_version, which would make a host that sets another minor see that minor everywhere and pass.
    :returns: ``["host: version", ...]`` for every host when more than one minor version is named, otherwise ``[]``. A version that is not a k3s tag counts as a minor of its own.
    """
    if not isinstance(hosts, Sequence) or isinstance(hosts, str) or not isinstance(hostvars, Mapping):
        raise AnsibleFilterError("The hosts must be a list and hostvars a mapping.")
    versions: dict[str, str] = {}
    for host in hosts:
        own = hostvars.get(host, {}).get("github_runner_cluster_k3s_version") if isinstance(hostvars.get(host, {}), Mapping) else None
        versions[str(host)] = str(own if own is not None else default).strip()
    minors = set()
    for version in versions.values():
        match = K3S_TAG.match(version)
        minors.add((match.group("major"), match.group("minor")) if match else version)
    if len(minors) <= 1:
        return []
    return [f"{host}: {version}" for host, version in versions.items()]


# A version k3s reports: `k3s --version` prints "k3s version v1.34.1+k3s1 (24fc436e)" and a Node's kubeletVersion is "v1.34.1+k3s1".
RUNNING_VERSION = re.compile(r"\bv(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)\+k3s(?P<build>[0-9]+)\b")


def _release(match: re.Match[str]) -> tuple[int, int, int, int]:
    """A k3s release as comparable numbers: major, minor, patch and k3s's own build."""
    return (int(match.group("major")), int(match.group("minor")), int(match.group("patch")), int(match.group("build")))


# How ansible-core reports a task cut off by its timeout keyword: from 2.18 the result has a "timedout" key; before that only its message says so.
TIMEOUT_MESSAGES = ("failed to execute in the expected time frame", "Timed out after")


def _server_read(result: Any) -> tuple[str, str, list[str]]:
    """Classify one server's read of the cluster (an Ansible loop result of tasks/running_version.yml's read).

    :returns: the host, its state ("answered", "stopped" or "unread") and, for "answered", the node lines; for "unread", one line saying why.
    """
    if not isinstance(result, Mapping):
        raise AnsibleFilterError(f"{result!r} is not a loop result.")
    host = str(result.get("item", "?"))
    message = str(result.get("msg", "") or "")
    if result.get("unreachable"):
        return host, "unread", [f"{host} could not be reached ({message.strip().splitlines()[0] if message.strip() else 'no reason given'})"]
    if result.get("timedout") or any(text in message for text in TIMEOUT_MESSAGES):
        return host, "unread", [f"{host} did not answer within github_runner_cluster_cluster_read_timeout"]
    lines = [line.strip() for line in str(result.get("stdout", "")).splitlines() if line.strip()]
    if result.get("rc") != 0:
        reason = [line.strip() for line in str(result.get("stderr", "") or message).splitlines() if line.strip()]
        return host, "unread", [f"{host} could not be read ({reason[-1] if reason else 'exit status ' + str(result.get('rc'))})"]
    if lines[:1] == ["stopped"]:
        return host, "stopped", []
    if lines[:1] == ["running"]:
        return host, "answered", lines[1:]
    return host, "unread", [f"{host} gave an answer this check cannot read ({' '.join(lines)!r})"]


def cluster_k3s_version_change_blockers(k3s_version: Any, node_name: Any, own_report: Any, server_reads: Any, node_role: Any = "server", builds: Any = True) -> list[str]:
    """Say why building ``k3s_version`` would move a node outside k3s's upgrade path and Kubernetes' version skew policy, or return an empty list.

    The rules, from k3s (no downgrade, the cluster upgraded one minor at a time, servers before agents) and Kubernetes' version skew policy (API servers within one minor of each other, a kubelet never newer than any API server, older ones supported):

    - this node is never built older than it runs, release by release (patch and k3s build too);
    - no node is built more than one minor above any node of the cluster;
    - a server is never built an older minor than any other node runs, server or agent;
    - an agent is never built a newer minor than any server runs. This one applies only when the run builds the agent's image (``builds``): changing the pin builds nothing, so writing a new pin to an agent before its servers are rebuilt, as the documented upgrade does, is allowed.

    Patch releases on other nodes are not compared: a node may be a patch behind or ahead of its peers.

    :param k3s_version: the rancher/k3s tag this host's image is built from, for example v1.34.1-k3s1; it must already have passed ``cluster_version_errors``.
    :param node_name: this host's node name: its container, and its own entry in the node list, are this node.
    :param own_report: what ``k3s --version`` printed in this node's running container (for example "k3s version v1.34.1+k3s1 (24fc436e)"), or an empty string when it does not run.
    :param server_reads: the loop results of reading the cluster through each server of the inventory (tasks/running_version.yml), each with its host as ``item`` and ``stdout`` either "stopped" or "running" followed by "<node> <kubeletVersion> <control-plane label>" lines (the label "true" on a server, empty on an agent); an empty list when the inventory holds no server.
    :param node_role: this host's role, "server" or "agent".
    :param builds: whether this run builds the node's image (it is missing), which is when an agent's kubelet would take the pinned version.
    :returns: one sentence per rule broken. A version that cannot be read is a problem too, since the check would otherwise pass on what it never saw, and so is a cluster that some server runs but no server could be read through.
    """
    pinned = K3S_TAG.match(str(k3s_version).strip())
    if pinned is None:
        raise AnsibleFilterError(f"{k3s_version!r} is not a rancher/k3s tag.")
    if not isinstance(own_report, str) or not isinstance(server_reads, Sequence) or isinstance(server_reads, str):
        raise AnsibleFilterError("The own report must be a string and the server reads a list.")
    if node_role not in ("server", "agent"):
        raise AnsibleFilterError(f"{node_role!r} is not a node role (server or agent).")
    reads = [_server_read(result) for result in server_reads]
    answered = [lines for _, state, lines in reads if state == "answered"]
    unread = [line for _, state, lines in reads if state == "unread" for line in lines]
    problems: list[str] = []
    if unread and not answered:
        problems.append(f"The cluster could not be read through any server of the inventory, so what it runs cannot be told: {'; '.join(unread)}.")
    # (source, version, is this node, is a server)
    running: list[tuple[str, str, bool, bool]] = []
    if own_report.strip():
        running.append((f"This node's own container ({node_name})", own_report.strip(), True, node_role == "server"))
    seen: set[str] = set()
    for lines in answered:
        for line in lines:
            fields = line.split()
            if not fields or fields[0] in seen:
                continue
            seen.add(fields[0])
            running.append((f"Node {fields[0]}", fields[1] if len(fields) > 1 else "", fields[0] == str(node_name), len(fields) > 2 and fields[2] == "true"))
    target = _release(pinned)
    for source, version, is_self, is_server in running:
        found = RUNNING_VERSION.search(version)
        if found is None:
            problems.append(f"{source} reports {version!r}, which is not a k3s version this check can read.")
            continue
        current = _release(found)
        shown = found.group(0)
        if is_self and target < current:
            problems.append(f"{source} runs {shown}, newer than {k3s_version}; k3s cannot be downgraded.")
        elif target[0] != current[0] or target[1] > current[1] + 1:
            problems.append(f"{source} runs {shown}, more than one minor away from {k3s_version}; k3s upgrades the cluster one minor at a time.")
        elif node_role == "server" and not is_self and target[:2] < current[:2]:
            if is_server:
                problems.append(f"Server {source.removeprefix('Node ')} runs {shown}, a newer minor than {k3s_version}; the servers cannot go back a minor.")
            else:
                problems.append(f"Agent {source.removeprefix('Node ')} runs {shown}, a newer minor than {k3s_version}; a kubelet may not be newer than the API servers.")
        elif node_role == "agent" and is_server and bool(builds) and target[:2] > current[:2]:
            problems.append(f"Server {source.removeprefix('Node ')} runs {shown}, an older minor than {k3s_version}; an agent's kubelet may not be newer than the API servers, so upgrade the servers first.")
    return problems


class FilterModule:
    """Registers the cluster version filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "cluster_version_errors": cluster_version_errors,
            "cluster_k3s_minor_skew": cluster_k3s_minor_skew,
            "cluster_k3s_version_change_blockers": cluster_k3s_version_change_blockers,
        }
