"""Filters the github_runner_cluster role uses before it moves a node container between networks: whether recreating it would be safe for the cluster, and whether the Docker host has an address on the LAN."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ansible.errors import AnsibleFilterError

# The label ARC puts on every ephemeral runner pod (actions-runner-controller, controllers/actions.github.com/resourcebuilder.go, newEphemeralRunnerPod).
RUNNER_POD_LABEL = "actions-ephemeral-runner"
RUNNER_POD_LABEL_VALUE = "True"
# Labels k3s gives a server node (one that runs the API server and, on etcd, an etcd member).
SERVER_NODE_LABELS = ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/etcd", "node-role.kubernetes.io/master")
FINISHED_POD_PHASES = ("Succeeded", "Failed")


def _labels(item: Mapping[str, Any]) -> Mapping[str, Any]:
    labels = item.get("metadata", {}).get("labels") or {}
    return labels if isinstance(labels, Mapping) else {}


def _ready(node: Mapping[str, Any]) -> bool:
    conditions = node.get("status", {}).get("conditions") or []
    return any(condition.get("type") == "Ready" and condition.get("status") == "True" for condition in conditions)


def _is_server(node: Mapping[str, Any]) -> bool:
    labels = _labels(node)
    return any(str(labels.get(label, "")).lower() == "true" for label in SERVER_NODE_LABELS)


def _etcd_healthy(result: Any) -> bool:
    """Whether one server's etcd read (an Ansible loop result of ``kubectl get --raw /readyz/etcd`` in its container) answered "ok"."""
    if not isinstance(result, Mapping) or result.get("unreachable") or result.get("skipped"):
        return False
    return result.get("rc") == 0 and str(result.get("stdout", "")).strip() == "ok"


def cluster_restart_blockers(nodes: Any, pods: Any, node_name: Any, servers: Any, etcd_results: Any) -> list[str]:
    """Say why restarting (or recreating) this host's node now would be unsafe, or return an empty list when it would not be.

    A server node may restart only while the other servers whose node is Ready and whose own API server reports etcd healthy still form a majority of every server, so etcd keeps its quorum without this one; a cluster with this node as its only server has no quorum to lose. Whatever cannot be told counts against the restart: a server the inventory names whose node is missing or not labelled as a server, a node labelled as a server that the inventory does not name (its etcd was not read), and a server whose etcd read failed or could not reach its host. Any node may restart only while no ARC runner pod that has not finished is on it, since restarting the node ends the job that pod runs.

    :param nodes: the cluster's Node objects (``kubectl get nodes -o json``'s ``items``).
    :param pods: Pod objects (``kubectl get pods -A -o json``'s ``items``); pods on other nodes are ignored.
    :param node_name: this host's node name.
    :param servers: the node names of the cluster's servers as the inventory decides them (tasks/topology.yml), this host's included when it is one.
    :param etcd_results: the loop results of reading ``/readyz/etcd`` through each other server's container, each with the ``item`` it looped over: that server's entry of the topology's hosts as ``dict2items`` gives it (``key`` the host, ``value.node_name`` its node name); empty when this host is not a server.
    :returns: one sentence per reason not to restart, or an empty list.
    """
    if not all(isinstance(value, Sequence) and not isinstance(value, str) for value in (nodes, pods, servers, etcd_results)):
        raise AnsibleFilterError("The nodes, pods, servers and etcd results must be lists.")
    blockers: list[str] = []
    by_name = {node.get("metadata", {}).get("name"): node for node in nodes}
    if node_name not in by_name:
        blockers.append(f"This host's node, {node_name}, is not in the cluster's node list, so whether restarting it is safe cannot be told.")
        return blockers
    known = [str(name) for name in servers]
    unlabelled = sorted(name for name in known if name in by_name and not _is_server(by_name[name]))
    if unlabelled:
        blockers.append(f"The cluster does not label {', '.join(unlabelled)} as server nodes, though the inventory makes them servers, so whether etcd keeps its quorum cannot be told.")
    unknown = sorted(name for name, node in by_name.items() if _is_server(node) and name not in known)
    every_server = sorted(set(known) | set(unknown))
    if node_name in known and len(every_server) > 1:
        etcd_ok = {
            str(result["item"].get("value", {}).get("node_name")): _etcd_healthy(result)
            for result in etcd_results
            if isinstance(result, Mapping) and isinstance(result.get("item"), Mapping)
        }
        healthy = sorted(name for name in every_server if name != node_name and name in by_name and _ready(by_name[name]) and etcd_ok.get(name, False))
        majority = len(every_server) // 2 + 1
        if len(healthy) < majority:
            unhealthy = []
            for name in every_server:
                if name == node_name or name in healthy:
                    continue
                if name not in by_name:
                    unhealthy.append(f"{name} (not in the node list)")
                elif not _ready(by_name[name]):
                    unhealthy.append(f"{name} (not Ready)")
                elif name in unknown:
                    unhealthy.append(f"{name} (not in the inventory, so its etcd was not read)")
                else:
                    unhealthy.append(f"{name} (etcd not reported healthy)")
            blockers.append(
                f"Restarting {node_name} would leave {len(healthy)} of {len(every_server)} servers with a Ready node and a healthy etcd,"
                f" fewer than the {majority} etcd needs for quorum: {', '.join(unhealthy)}."
            )
    runners = sorted(
        pod.get("metadata", {}).get("namespace", "") + "/" + pod.get("metadata", {}).get("name", "")
        for pod in pods
        if pod.get("spec", {}).get("nodeName") == node_name
        and str(_labels(pod).get(RUNNER_POD_LABEL, "")) == RUNNER_POD_LABEL_VALUE
        and pod.get("status", {}).get("phase") not in FINISHED_POD_PHASES
    )
    if runners:
        blockers.append(f"Runner pods are still on {node_name} and would lose their jobs: {', '.join(runners)}. Cordon the node and wait for them to finish.")
    return blockers


# How ansible-core reports a task cut off by its timeout keyword: from 2.18 the result has a "timedout" key; before that only its message says so (as in plugins/filter/cluster_versions.py).
TIMEOUT_MESSAGES = ("failed to execute in the expected time frame", "Timed out after")


def cluster_guard_state(results: Any) -> dict[str, Any]:
    """Take the cluster's state from the first server that answered the guard's read (tasks/host_network.yml), or say why none did.

    :param results: the loop results of reading the nodes and this node's pods through each server of the inventory, each with its host as ``item`` and, on success, ``stdout`` holding ``{"nodes": ..., "pods": ...}``.
    :returns: ``{"state": {"nodes": [...], "pods": [...]}, "problem": ""}`` from the first that answered, or ``{"state": None, "problem": ...}`` saying why for each server, or that the inventory holds none.
    """
    if not isinstance(results, Sequence) or isinstance(results, str):
        raise AnsibleFilterError("The reads must be a list of loop results.")
    if not results:
        return {
            "state": None,
            "problem": "The inventory holds no server of this cluster (its servers are outside it, as for a host bootstrap.sh joins to a cluster), so the role cannot read the cluster to tell whether moving this node is safe. Move it by hand, following the role README's Networking section.",
        }
    reasons: list[str] = []
    for result in results:
        if not isinstance(result, Mapping):
            raise AnsibleFilterError(f"{result!r} is not a loop result.")
        host = str(result.get("item", "?"))
        if result.get("unreachable"):
            reasons.append(f"{host} could not be reached")
            continue
        message = str(result.get("msg", "") or "")
        if result.get("timedout") or any(text in message for text in TIMEOUT_MESSAGES):
            reasons.append(f"{host} did not answer within github_runner_cluster_cluster_read_timeout")
            continue
        if result.get("rc") != 0:
            lines = [line.strip() for line in str(result.get("stderr", "") or message).splitlines() if line.strip()]
            reasons.append(f"{host} could not be read ({lines[-1] if lines else 'exit status ' + str(result.get('rc'))})")
            continue
        try:
            state = json.loads(str(result.get("stdout", "")))
        except ValueError:
            reasons.append(f"{host} gave an answer that is not the cluster's state")
            continue
        if isinstance(state, Mapping) and isinstance(state.get("nodes"), Mapping) and isinstance(state.get("pods"), Mapping):
            return {"state": {"nodes": state["nodes"].get("items", []), "pods": state["pods"].get("items", [])}, "problem": ""}
        reasons.append(f"{host} gave an answer that is not the cluster's state")
    return {"state": None, "problem": f"The cluster could not be read through any server of the inventory, so whether moving this node is safe cannot be told: {'; '.join(reasons)}."}


def addresses_in_network(lines: Any, network: Any) -> list[str]:
    """Return the IPv4 addresses in ``ip -4 -o addr show`` output that lie in a network.

    :param lines: the command's output lines, one address per line, such as ``2: eth0    inet 192.0.2.10/24 brd ...``.
    :param network: the network in CIDR notation, for example ``192.0.2.0/24``.
    :returns: the matching addresses, without their prefix length, in the order listed.
    :raises AnsibleFilterError: if the network is not a valid IPv4 network or the lines are not a list.
    """
    try:
        wanted = ipaddress.IPv4Network(str(network).strip(), strict=False)
    except ValueError as error:
        raise AnsibleFilterError(f"{network!r} is not an IPv4 network in CIDR notation, such as 192.0.2.0/24: {error}") from error
    if not isinstance(lines, Sequence) or isinstance(lines, str):
        raise AnsibleFilterError("The address lines must be a list.")
    found: list[str] = []
    for line in lines:
        fields = str(line).split()
        if "inet" not in fields or fields.index("inet") + 1 >= len(fields):
            continue
        address = ipaddress.IPv4Interface(fields[fields.index("inet") + 1]).ip
        if address in wanted:
            found.append(str(address))
    return found


class FilterModule:
    """Registers the cluster guard filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {"cluster_restart_blockers": cluster_restart_blockers, "cluster_guard_state": cluster_guard_state, "addresses_in_network": addresses_in_network}
