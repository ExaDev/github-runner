"""Filters the github_runner_cluster role's native node uses: which k3s release asset a host's architecture takes, the release and package versions the node's pins name, which tailnet the host's Tailscale client is logged in to, which role the host runs and whether it bootstraps a cluster, and whether the cluster's other voters are Ready enough for a server to start, restart or join."""

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


NODE_ROLES = ("server", "agent")


def _reason(result: dict[str, Any]) -> str:
    """The last line of what a failed command or task said, or its exit status when it said nothing."""
    lines = [line.strip() for line in str(result.get("stderr") or result.get("msg") or "").splitlines() if line.strip()]
    return lines[-1] if lines else f"exit status {result.get('rc')}"


def cluster_native_role(recorded: Any, agent_installed: Any, eligible: Any, inventory_role: Any, running: Any = ()) -> dict[str, str]:
    """Decide the role a native host runs now and the role it is to run after this run.

    :param recorded: what the host's role file holds (empty when it has none). :param agent_installed: whether the host has the k3s-agent unit, which a host set up before the role file existed has. :param eligible: whether the host is marked github_runner_cluster_voter_eligible. :param inventory_role: the role the topology gives the host. :param running: the roles whose unit is active now. :returns: ``current`` (``server``, ``agent``, or empty on a first install) and ``desired``. :raises AnsibleFilterError: if the role file holds anything but a role, or the inventory role or a running role is not one.

    The role file is the host's own record of its role and outlives restarts, reboots and runs of the role. A switch writes the new role to it before it stops the old unit, so while exactly one unit runs, that unit's role is the one the host runs now, and a switch that stopped part-way (a member removal that did not finish) is taken up again by the next run. With neither unit running, the file says; a host from before the file existed ran only the agent, so its unit counts as the agent role. For a host marked eligible the inventory gives only the role it starts with: once the file names a role, that role is kept, since whatever moves votes between hosts changes it at run time and a run of the role must not undo that. For any other host the inventory decides.
    """
    text = "" if recorded is None else str(recorded).strip()
    if text and text not in NODE_ROLES:
        raise AnsibleFilterError(f"The role file holds {text!r}; it must be 'server' or 'agent'. Write the role the host runs into it, or remove it on a host that runs neither unit.")
    if inventory_role not in NODE_ROLES:
        raise AnsibleFilterError(f"{inventory_role!r} is not a node role (server or agent).")
    active = [str(role) for role in running]
    if any(role not in NODE_ROLES for role in active):
        raise AnsibleFilterError(f"{active!r} are not node roles (server or agent).")
    if len(active) == 1:
        current = active[0]
    else:
        current = text or ("agent" if bool(agent_installed) else "")
    desired = text if text and bool(eligible) else str(inventory_role)
    return {"current": current, "desired": desired}


def cluster_native_cluster_init(reads: Any, this_host: Any) -> dict[str, Any]:
    """Say whether a native bootstrap host with no datastore of its own may start a new cluster: only when no other server of the inventory runs one.

    :param reads: the loop results of reading the cluster through each server of the inventory (tasks/cluster_nodes_read.yml's github_runner_cluster_running_reads), each with its host as ``item`` and ``stdout`` "stopped" or "running" followed by node lines. :param this_host: this host's inventory name, whose own read says nothing about another cluster. :returns: ``cluster_init`` (true only when every other server read as stopped, or there is none), ``running`` (the hosts that answered as running a server) and ``unread`` (one line per server that could not be read, which leaves the question open, so the caller refuses).
    """
    if not isinstance(reads, list):
        raise AnsibleFilterError("The reads must be a list of loop results.")
    running: list[str] = []
    unread: list[str] = []
    for result in reads:
        if not isinstance(result, dict):
            raise AnsibleFilterError(f"{result!r} is not a loop result.")
        host = str(result.get("item", "?"))
        if host == str(this_host):
            continue
        lines = str(result.get("stdout", "")).split()
        if result.get("unreachable") or result.get("rc") != 0:
            unread.append(f"{host} could not be read ({_reason(result)})")
        elif lines[:1] == ["running"]:
            running.append(host)
        elif lines[:1] != ["stopped"]:
            unread.append(f"{host} gave an answer this check cannot read")
    return {"cluster_init": not running and not unread, "running": running, "unread": unread}


def cluster_native_voter_blockers(read: Any, node_name: Any, min_others: Any = 0) -> list[str]:
    """Say why a native server must not start, restart or join now: every other etcd voter the cluster lists must be Ready, so the cluster keeps its quorum while this one is away, and a learner is only ever added beside a healthy membership.

    The voters are read from the cluster, each node's etcd role label, never from the inventory, because a host's role can change at run time. A cluster that lists no other voter (a first bootstrap, or a single server restarting) has no quorum to keep.

    :param read: the result of ``kubectl get nodes`` printing ``<node>,<Ready status>,<etcd role label>`` lines (commas, since either of the last two may be empty), as Ansible registers it. :param node_name: this host's node name, which is left out. :param min_others: the fewest other voters there must be, for a change that takes this server's vote away (a demotion); 0 for one that keeps it. :returns: one sentence per other voter that is not Ready, one more when there are fewer other voters than ``min_others``, or one sentence saying the nodes could not be listed, or an empty list.
    """
    if not isinstance(read, dict):
        raise AnsibleFilterError(f"{read!r} is not a task result.")
    if read.get("unreachable") or read.get("rc") != 0:
        return [f"the cluster's nodes could not be listed ({_reason(read)}), so whether the other voters are Ready cannot be told"]
    problems: list[str] = []
    others: list[str] = []
    for line in str(read.get("stdout", "")).splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 3 or fields[0] == str(node_name) or fields[2] != "true":
            continue
        others.append(fields[0])
        if fields[1] != "True":
            problems.append(f"voter {fields[0]} is not Ready (its Ready condition is {fields[1] or 'unknown'})")
    if len(others) < int(min_others):
        problems.append(f"only {len(others)} other voters ({', '.join(others) or 'none'}) would remain, fewer than the {int(min_others)} a change that takes this server's vote away leaves")
    return problems


class FilterModule:
    """Registers the native node's filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "cluster_native_k3s_asset": cluster_native_k3s_asset,
            "cluster_native_k3s_release": cluster_native_k3s_release,
            "cluster_native_tailscale_package_version": cluster_native_tailscale_package_version,
            "cluster_native_tailnet_login": cluster_native_tailnet_login,
            "cluster_native_role": cluster_native_role,
            "cluster_native_cluster_init": cluster_native_cluster_init,
            "cluster_native_voter_blockers": cluster_native_voter_blockers,
        }
