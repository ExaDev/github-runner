"""Filters the github_runner_cluster role's native node uses: which k3s release asset a host's architecture takes, the release and package versions the node's pins name, and which tailnet the host's Tailscale client is logged in to."""

from __future__ import annotations

import json
import re
from typing import Any

from ansible.errors import AnsibleFilterError

# The k3s release assets for each architecture Ansible reports as ansible_facts['architecture'], and the file in the same release that lists their SHA-256 sums.
K3S_ASSETS = {
    "x86_64": {"asset": "k3s", "checksums": "sha256sum-amd64.txt"},
    "aarch64": {"asset": "k3s-arm64", "checksums": "sha256sum-arm64.txt"},
}
# rancher/k3s image tag, with k3s's "+" written "-", such as v1.34.1-k3s1.
K3S_TAG = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-k3s(?P<build>[0-9]+)$")
# tailscale/tailscale image tag, a release such as v1.102.4.
TAILSCALE_TAG = re.compile(r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$")
# The backend states in which a Tailscale client has not yet settled on whether it is logged in: tailscaled reads its persisted login for a moment after it starts.
SETTLING_STATES = ("NoState", "Starting")
LOGGED_OUT_STATES = ("NeedsLogin",)


def cluster_native_k3s_asset(architecture: Any) -> dict[str, str]:
    """Return the k3s release asset and sum file for a host architecture.

    :param architecture: ansible_facts['architecture'], such as x86_64 or aarch64. :returns: ``asset`` (the binary's file name in the release) and ``checksums`` (the release's file of SHA-256 sums for that architecture). :raises AnsibleFilterError: if the architecture has no k3s release binary the collection installs.
    """
    if not isinstance(architecture, str) or architecture not in K3S_ASSETS:
        raise AnsibleFilterError(f"{architecture!r} is not an architecture a native k3s node supports; use one of {', '.join(sorted(K3S_ASSETS))}.")
    return dict(K3S_ASSETS[architecture])


def cluster_native_k3s_release(k3s_tag: Any) -> str:
    """Return the k3s release tag (with "+") that a rancher/k3s image tag (with "-") names.

    :param k3s_tag: github_runner_cluster_k3s_version, such as v1.34.1-k3s1. :returns: the GitHub release tag, such as v1.34.1+k3s1, which is also what ``k3s --version`` prints. :raises AnsibleFilterError: if the tag is not a pinned k3s version.
    """
    found = K3S_TAG.match(str(k3s_tag).strip())
    if found is None:
        raise AnsibleFilterError(f"{k3s_tag!r} is not a pinned rancher/k3s tag such as v1.34.1-k3s1.")
    return f"v{found.group('version')}+k3s{found.group('build')}"


def cluster_native_tailscale_package_version(tailscale_tag: Any) -> str:
    """Return the Tailscale package version that a tailscale/tailscale image tag names.

    :param tailscale_tag: github_runner_cluster_tailscale_version, such as v1.102.4. :returns: the version Tailscale's package repository publishes, such as 1.102.4. :raises AnsibleFilterError: if the tag is not a pinned release.
    """
    found = TAILSCALE_TAG.match(str(tailscale_tag).strip())
    if found is None:
        raise AnsibleFilterError(f"{tailscale_tag!r} is not a pinned tailscale/tailscale tag such as v1.102.4.")
    return found.group("version")


def cluster_native_tailnet_login(status: Any, tailnet_domain: Any) -> dict[str, str]:
    """Say which tailnet a Tailscale client is logged in to, from the output of ``tailscale status --json``.

    :param status: the JSON text ``tailscale status --json`` printed. :param tailnet_domain: the cluster's tailnet MagicDNS domain, such as example.ts.net (github_runner_cluster_tailnet_domain). :returns: ``state``, one of ``settling`` (the client has not yet read its persisted login, so ask again), ``logged_out`` (no tailnet), ``cluster`` (logged in to the cluster's tailnet) and ``other`` (logged in to another one), with ``backend`` (the client's BackendState) and ``domain`` (the tailnet it is logged in to, empty when it is none). :raises AnsibleFilterError: if the status is not JSON or the domain is empty.
    """
    expected = str(tailnet_domain).strip().strip(".").lower()
    if not expected:
        raise AnsibleFilterError("The tailnet domain must be set to tell whether the client is logged in to the cluster's tailnet.")
    try:
        parsed = json.loads(status)
    except (TypeError, ValueError) as exc:
        raise AnsibleFilterError(f"The Tailscale status is not JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("BackendState"), str):
        raise AnsibleFilterError("The Tailscale status has no BackendState.")
    backend = parsed["BackendState"]
    tailnet = parsed.get("CurrentTailnet")
    domain = str(tailnet.get("MagicDNSSuffix") or "").strip(".").lower() if isinstance(tailnet, dict) else ""
    if backend in SETTLING_STATES:
        state = "settling"
    elif backend in LOGGED_OUT_STATES or not domain:
        state = "logged_out"
    elif domain == expected:
        state = "cluster"
    else:
        state = "other"
    return {"state": state, "backend": backend, "domain": domain}


class FilterModule:
    """Registers the native node's filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "cluster_native_k3s_asset": cluster_native_k3s_asset,
            "cluster_native_k3s_release": cluster_native_k3s_release,
            "cluster_native_tailscale_package_version": cluster_native_tailscale_package_version,
            "cluster_native_tailnet_login": cluster_native_tailnet_login,
        }
