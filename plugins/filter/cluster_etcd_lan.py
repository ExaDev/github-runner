"""Filters the github_runner_cluster role uses to move etcd's peer traffic between k3s servers onto their LAN addresses (github_runner_cluster_etcd_lan_address and github_runner_cluster_etcd_peers_over_lan), and back: the checks on the inventory, the reading of etcd's member list and of the other servers' certificates, and the decision of which server changes its address in a run."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ansible.errors import AnsibleFilterError

# k3s names a server's etcd member after its node, a hyphen and the first eight characters of a random UUID (setName in pkg/etcd/etcd.go at v1.34.1+k3s1).
MEMBER_SUFFIX = r"-[0-9a-f]{8}"


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def cluster_etcd_lan_problems(hosts: Any, peers_over_lan: Any) -> list[str]:
    """Return what in the inventory keeps etcd's peer traffic from moving onto the servers' LAN addresses, or would break it once moved.

    :param hosts: one mapping per host of the cluster group, with ``name``, ``node_name``, ``role`` (the topology's server or agent), ``eligible`` (github_runner_cluster_voter_eligible), ``node_mode``, ``network_mode``, ``lan_cidr``, ``mesh``, ``address`` (github_runner_cluster_etcd_lan_address) and ``peers_over_lan`` (that host's github_runner_cluster_etcd_peers_over_lan). :param peers_over_lan: this host's github_runner_cluster_etcd_peers_over_lan. :returns: one sentence per problem, empty when there is none. :raises AnsibleFilterError: if ``hosts`` is not a list of mappings.

    An address is a server's own: an IPv4 address in the LAN the nodes share, on a Docker node on the host network, whose k3s then holds the address in the namespace etcd listens in. k3s registers one peer URL per etcd member and joins a new member with only the first URL of each existing one, and etcd refuses a join whose URLs differ from the members' (ValidateClusterAndAssignIDs in server/etcdserver/api/membership/cluster.go), so a member cannot carry the LAN address and the mesh address together: with peers_over_lan every server and every host that may become one (an eligible host) needs an address of its own, and every host of the group must agree on the setting. The off-mesh start of a headscale_in_cluster bootstrap server passes no node address to k3s, so its etcd address would change between its two starts.
    """
    if not isinstance(hosts, Sequence) or isinstance(hosts, str) or not all(isinstance(host, Mapping) for host in hosts):
        raise AnsibleFilterError("The hosts must be a list of mappings.")
    problems: list[str] = []
    seen: dict[str, str] = {}
    wanted = bool(peers_over_lan)
    for host in hosts:
        name = _text(host.get("name"))
        address = _text(host.get("address"))
        takes_vote = host.get("role") == "server" or bool(host.get("eligible"))
        if bool(host.get("peers_over_lan")) != wanted:
            problems.append(f"{name} sets github_runner_cluster_etcd_peers_over_lan to {bool(host.get('peers_over_lan'))}, and this host to {wanted}; set it once for the whole group.")
        if not address:
            if wanted and takes_vote:
                problems.append(f"{name} is {'a server' if host.get('role') == 'server' else 'eligible for a vote'} but sets no github_runner_cluster_etcd_lan_address, and with github_runner_cluster_etcd_peers_over_lan every etcd member must be reached on its LAN address (k3s registers one peer URL per member).")
            continue
        try:
            parsed = ipaddress.IPv4Address(address)
        except ValueError:
            problems.append(f"{name}'s github_runner_cluster_etcd_lan_address {address!r} is not an IPv4 address.")
            continue
        if not takes_vote:
            problems.append(f"{name} sets github_runner_cluster_etcd_lan_address but is neither a server nor eligible for a vote, so it runs no etcd member.")
        if host.get("node_mode") == "native":
            problems.append(f"{name} is a native node, which the LAN address is not supported on: its node address comes from files/native/write-node-ip.sh, and its etcd member cannot be updated with the role's etcdctl container. Leave the address unset there, and unmark it eligible before turning github_runner_cluster_etcd_peers_over_lan on.")
        if host.get("network_mode") != "host":
            problems.append(f"{name} sets github_runner_cluster_etcd_lan_address with github_runner_cluster_network_mode {host.get('network_mode')}. On the bridge network the node's container has only its Docker bridge address: a LAN address published through the Docker VM's NAT reaches etcd from the VM's address instead of the peer's, which fails etcd's check of the peer certificate against the connection's source, and macOS drops a user-mode VM's traffic to other Macs. Move the node to the host network of a VM with an address on the LAN first (the role README's Networking section).")
        cidr = _text(host.get("lan_cidr"))
        try:
            in_lan = bool(cidr) and parsed in ipaddress.IPv4Network(cidr, strict=False)
        except ValueError:
            in_lan = False
        if not in_lan:
            problems.append(f"{name}'s github_runner_cluster_etcd_lan_address {address} is not in github_runner_cluster_lan_cidr ({cidr or 'unset'}).")
        if address in seen:
            problems.append(f"{name} and {seen[address]} both set github_runner_cluster_etcd_lan_address {address}; each etcd member needs an address of its own.")
        seen.setdefault(address, name)
        if wanted and host.get("mesh") == "headscale_in_cluster":
            problems.append(f"{name} runs the headscale_in_cluster mesh, whose bootstrap server first starts k3s off the mesh with no node address, so its etcd address would change between its two starts; github_runner_cluster_etcd_peers_over_lan is not supported with it.")
    return list(dict.fromkeys(problems))


def cluster_etcd_members(member_list: Any) -> list[dict[str, Any]]:
    """Return the members of ``etcdctl member list -w json`` output.

    :param member_list: the command's standard output. :returns: one mapping per member: ``id`` (hexadecimal, as ``etcdctl member update`` takes it), ``name``, ``peer_hosts`` (the host of each peer URL, in order) and ``learner``. :raises AnsibleFilterError: if the output is not etcdctl's JSON member list.
    """
    try:
        data = json.loads(_text(member_list))
    except ValueError as error:
        raise AnsibleFilterError(f"etcdctl's member list is not JSON: {error}") from error
    members = data.get("members") if isinstance(data, Mapping) else None
    if not isinstance(members, list):
        raise AnsibleFilterError("etcdctl's member list has no members array.")
    found: list[dict[str, Any]] = []
    for member in members:
        if not isinstance(member, Mapping) or not isinstance(member.get("ID"), int):
            raise AnsibleFilterError(f"etcdctl's member list holds an entry without a numeric ID: {member!r}")
        hosts = []
        for url in member.get("peerURLs") or []:
            match = re.fullmatch(r"https?://\[?([^\]/]+?)\]?:[0-9]+/?", str(url))
            if match is None:
                raise AnsibleFilterError(f"etcd member {member.get('name')!r} has a peer URL etcdctl printed as {url!r}, which is not scheme://host:port.")
            hosts.append(match.group(1))
        found.append({"id": format(member["ID"], "x"), "name": _text(member.get("name")), "peer_hosts": hosts, "learner": bool(member.get("isLearner"))})
    return found


def cluster_etcd_member_of(members: Any, node_name: Any) -> dict[str, Any] | None:
    """Return the member k3s registered for a node, or None when it has none.

    :param members: the output of cluster_etcd_members. :param node_name: the node's name. :raises AnsibleFilterError: if more than one member carries the node's name, which k3s itself refuses to join with.
    """
    pattern = re.compile(re.escape(_text(node_name)) + MEMBER_SUFFIX)
    matches = [member for member in members if pattern.fullmatch(str(member.get("name", "")))]
    if len(matches) > 1:
        raise AnsibleFilterError(f"etcd lists {len(matches)} members for {node_name}: {', '.join(member['name'] for member in matches)}. Remove the stale one before changing its address.")
    return matches[0] if matches else None


def cluster_etcd_lan_switches(hosts: Any) -> list[str]:
    """Return the hosts that change their etcd address in this run: the first of those that would, in play order.

    :param hosts: one mapping per host of the play, in play order, with ``name`` and ``switch`` (empty, ``to_lan`` or ``to_mesh``). :returns: a list of at most one host name. Changing a member's address stops it until its container is recreated, so two in one run could leave the cluster without a quorum; the others change in later runs.
    """
    return [_text(host.get("name")) for host in hosts if _text(host.get("switch"))][:1]


def cluster_etcd_lan_readiness(certificates: Any, reaches: Any, addresses: Any) -> dict[str, list[str]]:
    """Return the other servers that keep a member from moving onto its LAN address.

    :param certificates: the looped results of reading each other server's etcd peer certificate's IP addresses, one per line (``item`` the host, ``rc`` and ``stdout_lines``; an unreachable host has no ``rc``).
    :param reaches: the looped results of each other server pinging the member's LAN address (``item`` and ``rc``).
    :param addresses: each other server's own LAN address, by host.
    :returns: ``unnamed``, the servers whose certificate could not be read or does not name their own LAN address, and ``unreached``, the servers that did not reach the member's address.
    """
    unnamed = [str(result["item"]) for result in certificates if result.get("rc") != 0 or _text(addresses.get(result["item"])) not in [_text(line) for line in result.get("stdout_lines") or []]]
    unreached = [str(result["item"]) for result in reaches if result.get("rc") != 0]
    return {"unnamed": unnamed, "unreached": unreached}


class FilterModule:
    """Registers the etcd LAN filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "cluster_etcd_lan_problems": cluster_etcd_lan_problems,
            "cluster_etcd_members": cluster_etcd_members,
            "cluster_etcd_member_of": cluster_etcd_member_of,
            "cluster_etcd_lan_switches": cluster_etcd_lan_switches,
            "cluster_etcd_lan_readiness": cluster_etcd_lan_readiness,
        }
