"""Filters the github_runner_cluster role uses before it lets Docker Compose recreate a k3s server's container: whether the running container's configuration differs from the rendered one, and whether the other servers are Ready, which they must be for the recreated server's etcd member to be spared."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ansible.errors import AnsibleFilterError

# How many characters of a configuration hash a message shows: enough to tell two hashes apart, short enough to read.
SHOWN_HASH_LENGTH = 12

# How ansible-core reports a task cut off by its timeout keyword: from 2.18 the result has a "timedout" key; before that only its message says so.
TIMEOUT_MESSAGES = ("failed to execute in the expected time frame", "Timed out after")


def cluster_rendered_config_hash(output: Any, service: Any) -> str:
    """Pick one service's configuration hash out of the output of ``docker compose config --hash '*'``.

    :param output: the command's standard output, one ``<service> <hash>`` line per service in the active profiles. :param service: the Compose service name, for example k3s. :returns: the hash Compose compares with the ``com.docker.compose.config-hash`` label of a running container when it decides whether to recreate it. :raises AnsibleFilterError: if the output has no line for the service, since a hash that was not read must never count as no difference.
    """
    if not isinstance(output, str) or not isinstance(service, str) or service == "":
        raise AnsibleFilterError("The output must be a string and the service a non-empty string.")
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == service:
            return fields[1]
    raise AnsibleFilterError(f"Docker Compose printed no configuration hash for the service {service!r}.")


def cluster_recreate_reason(running_hash: Any, rendered_hash: Any) -> str:
    """Say why Docker Compose would recreate a container, or return an empty string when it would keep it.

    :param running_hash: the container's ``com.docker.compose.config-hash`` label; empty when it has none, which Compose treats as a difference. :param rendered_hash: the hash of the configuration Compose renders now (``cluster_rendered_config_hash``). :returns: one sentence naming both hashes, or an empty string when they are equal.
    """
    if not isinstance(running_hash, str) or not isinstance(rendered_hash, str) or rendered_hash.strip() == "":
        raise AnsibleFilterError("The running hash must be a string and the rendered hash a non-empty string.")
    running, rendered = running_hash.strip(), rendered_hash.strip()
    if running == rendered:
        return ""
    shown = rendered[:SHOWN_HASH_LENGTH]
    if running == "":
        return f"the running container carries no Compose configuration hash, and the rendered configuration's is {shown}"
    return f"the running container's Compose configuration hash {running[:SHOWN_HASH_LENGTH]} differs from the rendered configuration's {shown}"


def _node_status(read: Mapping[str, Any]) -> dict[str, str]:
    """The ``<node> <Ready status>`` lines of a successful node listing as a mapping from node name to status ("" when the line has none)."""
    status: dict[str, str] = {}
    for line in str(read.get("stdout", "")).splitlines():
        fields = line.split()
        if fields:
            status[fields[0]] = fields[1] if len(fields) > 1 else ""
    return status


def _read_problem(read: Mapping[str, Any]) -> str | None:
    """Say why a node listing did not succeed, or return None when it did."""
    message = str(read.get("msg", "") or "")
    if read.get("timedout") or any(text in message for text in TIMEOUT_MESSAGES):
        return "the nodes could not be listed through this node's own container within github_runner_cluster_cluster_read_timeout"
    if read.get("rc") != 0:
        reason = [line.strip() for line in str(read.get("stderr", "") or message).splitlines() if line.strip()]
        return f"the nodes could not be listed through this node's own container ({reason[-1] if reason else 'exit status ' + str(read.get('rc'))})"
    return None


def _checked_input(peers: Any, read: Any) -> tuple[Sequence[str], Mapping[str, Any]]:
    if not isinstance(peers, Sequence) or isinstance(peers, str) or not all(isinstance(peer, str) for peer in peers):
        raise AnsibleFilterError("The peers must be a list of node names.")
    if not isinstance(read, Mapping):
        raise AnsibleFilterError(f"{read!r} is not a task result.")
    return peers, read


def cluster_peers_registered(peers: Any, read: Any) -> list[str]:
    """Name the other servers whose nodes the cluster lists, which are the ones that hold quorum for this server.

    :param peers: the node names of the other servers of the cluster.
    :param read: the result of listing the nodes through this node's own container, as Ansible registers it.
    :returns: the peers that are registered, in the order given; empty when the listing failed.
    """
    peers, read = _checked_input(peers, read)
    if _read_problem(read) is not None:
        return []
    status = _node_status(read)
    return [peer for peer in peers if peer in status]


def cluster_recreate_blockers(peers: Any, read: Any) -> list[str]:
    """Say why a server must not be recreated now: the other servers must report Ready, so that the cluster keeps its etcd quorum while this one is away.

    A cluster whose other servers have not registered at all is one still being brought up (the bootstrap server restarted before anything joined it), where there is no quorum to keep; once any of them is registered, one that is missing blocks like one that is not Ready.

    :param peers: the node names of the other servers of the cluster.
    :param read: the result of listing the nodes through this node's own container (``kubectl get nodes`` printing ``<node> <Ready status>`` lines), as Ansible registers it.
    :returns: one sentence per server that is not Ready or has not registered, or a single sentence when the nodes could not be read, or an empty list when every other server is Ready (or there is no other server, or none is registered yet).
    """
    peers, read = _checked_input(peers, read)
    if len(peers) == 0:
        return []
    problem = _read_problem(read)
    if problem is not None:
        return [problem]
    status = _node_status(read)
    if not any(peer in status for peer in peers):
        return []
    problems: list[str] = []
    for peer in peers:
        if peer not in status:
            problems.append(f"server {peer} is not registered")
        elif status[peer] != "True":
            problems.append(f"server {peer} is not Ready (its Ready condition is {status[peer] or 'unknown'})")
    return problems


class FilterModule:
    """Registers the recreate filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "cluster_rendered_config_hash": cluster_rendered_config_hash,
            "cluster_recreate_reason": cluster_recreate_reason,
            "cluster_peers_registered": cluster_peers_registered,
            "cluster_recreate_blockers": cluster_recreate_blockers,
        }
