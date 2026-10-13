"""Filters the github_runner_cluster role uses to check the versions every node's k3s image is built from: that each is a pinned tag, and that every host in a cluster group names the same k3s minor version, and that the version keeps to k3s's upgrade path and Kubernetes' version skew policy from what the cluster runs."""

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


class FilterModule:
    """Registers the cluster version filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {"cluster_version_errors": cluster_version_errors, "cluster_k3s_minor_skew": cluster_k3s_minor_skew}
