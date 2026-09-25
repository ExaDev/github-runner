"""Works out which hosts in a k3s cluster group are servers, which one bootstraps, and how each joins."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ansible.errors import AnsibleFilterError
from ansible.module_utils.parsing.convert_bool import boolean

NODE_ROLES = ("server", "agent")


def _text(host_vars: Mapping[str, Any], key: str) -> str:
    """Return a per-host string override, treating an unset, null or empty value as unset."""
    value = host_vars.get(key)
    return "" if value is None else str(value)


def _describe_host(name: str, host_vars: Mapping[str, Any], errors: list[str]) -> dict[str, Any]:
    """Collect one host's overrides and derived identity from its inventory variables."""
    role = _text(host_vars, "github_runner_cluster_node_role")
    if role and role not in NODE_ROLES:
        errors.append(f"{name}: github_runner_cluster_node_role must be 'server' or 'agent' (got '{role}').")
    bootstrap_override = host_vars.get("github_runner_cluster_bootstrap")
    node_name = _text(host_vars, "github_runner_cluster_node_name") or name
    domain = _text(host_vars, "github_runner_cluster_tailnet_domain")
    address = _text(host_vars, "github_runner_cluster_node_address") or (f"{node_name}.{domain}" if domain else node_name)
    return {
        "name": name,
        "role_override": role if role in NODE_ROLES else "",
        "bootstrap_override": None if bootstrap_override is None else boolean(bootstrap_override, strict=False),
        "node_name": node_name,
        "address": address,
        "server_url_override": _text(host_vars, "github_runner_cluster_server_url"),
        "tls_sans_override": list(host_vars.get("github_runner_cluster_tls_sans") or []),
    }


def _largest_odd_at_most(limit: int) -> int:
    """Return the largest odd number no greater than limit, or 0 when limit is below 1."""
    if limit < 1:
        return 0
    return limit if limit % 2 == 1 else limit - 1


def github_runner_cluster_topology(members: list[str], hostvars: Mapping[str, Mapping[str, Any]], max_servers: Any, api_port: Any) -> dict[str, Any]:
    """Decide every cluster member's part from inventory order and per-host overrides.

    :param members: the cluster group's hosts, in inventory order. :param hostvars: Ansible's hostvars, read for each member's per-host overrides. :param max_servers: the most servers to choose automatically. :param api_port: the k3s API port a joining node connects to. :returns: ``errors`` (empty when the result is valid), ``servers`` and ``bootstrap`` (host names), and ``hosts``, mapping each member to its ``role``, ``bootstrap``, ``node_name``, ``address``, ``server_url`` and ``tls_sans``. :raises AnsibleFilterError: if max_servers or api_port is not an integer.

    The automatic server count is the largest odd number no greater than both the number of hosts that are not forced to be agents and max_servers, because etcd needs a majority and an even member count tolerates no more failures than the odd count below it. The first hosts in inventory order become servers; the first server not told to join elsewhere bootstraps the cluster, and every other node joins it.
    """
    try:
        server_limit = int(max_servers)
        port = int(api_port)
    except (TypeError, ValueError) as exc:
        raise AnsibleFilterError(f"github_runner_cluster_max_servers and github_runner_cluster_api_port must be integers: {exc}") from exc

    errors: list[str] = []
    if server_limit < 1:
        errors.append(f"github_runner_cluster_max_servers must be at least 1 (got {server_limit}).")
    hosts = [_describe_host(name, hostvars[name], errors) for name in members]
    # A host told to bootstrap has to be a server, so it takes one of the server places rather than competing for one in inventory order.
    for host in hosts:
        if host["bootstrap_override"] is True and not host["role_override"]:
            host["role_override"] = "server"

    forced_agents = sum(1 for host in hosts if host["role_override"] == "agent")
    target = _largest_odd_at_most(min(len(hosts) - forced_agents, server_limit))
    server_count = sum(1 for host in hosts if host["role_override"] == "server")
    for host in hosts:
        if host["role_override"]:
            host["role"] = host["role_override"]
        elif server_count < target:
            host["role"] = "server"
            server_count += 1
        else:
            host["role"] = "agent"
    servers = [host for host in hosts if host["role"] == "server"]

    chosen = [host for host in hosts if host["bootstrap_override"] is True]
    bootstrap_host = None
    if len(chosen) > 1:
        errors.append("More than one host sets github_runner_cluster_bootstrap: true (" + ", ".join(host["name"] for host in chosen) + "); exactly one host bootstraps the cluster.")
    elif chosen:
        bootstrap_host = chosen[0]
        if bootstrap_host["role"] != "server":
            errors.append(f"{bootstrap_host['name']} sets github_runner_cluster_bootstrap: true but is an agent; only a server can bootstrap the cluster.")
        if bootstrap_host["server_url_override"]:
            errors.append(f"{bootstrap_host['name']} sets both github_runner_cluster_bootstrap: true and github_runner_cluster_server_url; a host either bootstraps the cluster or joins an existing one, not both.")
    else:
        bootstrap_host = next((host for host in servers if not host["server_url_override"] and host["bootstrap_override"] is not False), None)

    # With no bootstrap host in the group, every node joins a cluster whose other servers are outside this inventory, so the group's own server count says nothing about quorum.
    if bootstrap_host is not None:
        if not servers:
            errors.append("The cluster group has no servers; at least one host must be a server.")
        elif len(servers) % 2 == 0:
            errors.append(f"The cluster would have {len(servers)} servers ({', '.join(host['name'] for host in servers)}); etcd needs an odd number, so make one of them an agent or add another server.")

    for host in hosts:
        host["bootstrap"] = host is bootstrap_host
        if host["server_url_override"]:
            host["server_url"] = host["server_url_override"]
        elif host["bootstrap"]:
            host["server_url"] = ""
        elif bootstrap_host is not None:
            host["server_url"] = f"https://{bootstrap_host['address']}:{port}"
        else:
            host["server_url"] = ""
            errors.append(f"{host['name']} has nothing to join: no host in the cluster group bootstraps the cluster, and it sets no github_runner_cluster_server_url.")
        host["tls_sans"] = host["tls_sans_override"] or [server["address"] for server in servers]

    return {
        "errors": errors,
        "servers": [host["name"] for host in servers],
        "bootstrap": bootstrap_host["name"] if bootstrap_host is not None else "",
        "hosts": {host["name"]: {key: host[key] for key in ("role", "bootstrap", "node_name", "address", "server_url", "tls_sans")} for host in hosts},
    }


class FilterModule:
    """Registers the cluster topology filter with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {"cluster_topology": github_runner_cluster_topology}
