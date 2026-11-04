"""Filters the github_runner_cluster role uses to decide which role, server or agent, a host runs, whether it runs natively or under Docker Compose, and whether the role controller holds the regrade Lease for a voter swap, during which no run of the role may change a node."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ansible.errors import AnsibleFilterError

NODE_ROLES = ("server", "agent")


def _reason(result: dict[str, Any]) -> str:
    """The last line of what a failed command or task said, or its exit status when it said nothing."""
    lines = [line.strip() for line in str(result.get("stderr") or result.get("msg") or "").splitlines() if line.strip()]
    return lines[-1] if lines else f"exit status {result.get('rc')}"


def cluster_host_role(recorded: Any, legacy_agent: Any, eligible: Any, inventory_role: Any, running: Any = ()) -> dict[str, str]:
    """Decide the role a host runs now and the role it is to run after this run.

    :param recorded: what the host's role file holds (empty when it has none). :param legacy_agent: whether the host runs, or is set up to run, the agent from before the role file existed, which counts as the agent role when the file is absent and nothing runs (a native host's k3s-agent unit; never for a Docker host, whose role file is new). :param eligible: whether the host is marked github_runner_cluster_voter_eligible. :param inventory_role: the role the topology gives the host. :param running: the roles whose unit or container runs now. :returns: ``current`` (``server``, ``agent``, or empty on a first install) and ``desired``. :raises AnsibleFilterError: if the role file holds anything but a role, or the inventory role or a running role is not one.

    The role file is the host's own record of its role and outlives restarts, reboots and runs of the role. A switch writes the new role to it before it stops the old unit or container, so while exactly one of them runs, its role is the one the host runs now, and a switch that stopped part-way (a member removal that did not finish) is taken up again by the next run. With neither running, the file says. For a host marked eligible the inventory gives only the role it starts with: once the file names a role, that role is kept, since whatever moves votes between hosts changes it at run time and a run of the role must not undo that. For any other host the inventory decides.
    """
    text = "" if recorded is None else str(recorded).strip()
    if text and text not in NODE_ROLES:
        raise AnsibleFilterError(f"The role file holds {text!r}; it must be 'server' or 'agent'. Write the role the host runs into it, or remove it on a host that runs neither.")
    if inventory_role not in NODE_ROLES:
        raise AnsibleFilterError(f"{inventory_role!r} is not a node role (server or agent).")
    active = [str(role) for role in running]
    if any(role not in NODE_ROLES for role in active):
        raise AnsibleFilterError(f"{active!r} are not node roles (server or agent).")
    if len(active) == 1:
        current = active[0]
    else:
        current = text or ("agent" if bool(legacy_agent) else "")
    desired = text if text and bool(eligible) else str(inventory_role)
    return {"current": current, "desired": desired}


def _epoch(stamp: str) -> float:
    """Epoch seconds of a Lease time (RFC 3339 with a fraction, in UTC, as the API server writes it)."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def cluster_role_lease_guard(reads: Any, holder: Any, swap_id_annotation: Any) -> dict[str, str]:
    """Say whether the role controller holds the regrade Lease, which it holds for the whole of a voter swap: while it does, a run of the role must change no node, so that it never undoes or races the swap.

    :param reads: the loop results of reading the Lease through each server of the inventory (tasks/role_lease_guard.yml), each with its host as ``item`` and ``stdout`` either "stopped" or "running", the reading host's clock as epoch seconds, then the Lease as ``kubectl get lease -o json`` prints it (nothing when there is none). :param holder: the holderIdentity the role controller takes the Lease as. :param swap_id_annotation: the annotation on the Lease that names the swap. :returns: ``state``, one of ``held`` (by the controller, and unexpired), ``free`` (no server runs, or the Lease is absent, has run out, is unheld or held by anything else, such as a regrading step) and ``unknown`` (no server answered and one could not be read), with ``message``, a sentence saying why. :raises AnsibleFilterError: if a server answered with something that is not a Lease.

    One answer decides, since every server's API reads the same Lease. A Lease lasts until its renew time (its acquire time when it was never renewed) plus its duration, and is expired from that moment, the rule lease_state applies in files/regrade/fleet-regrade-core.sh; the moment is compared with the clock of the host that read it, the clock the controller renews by.
    """
    if not isinstance(reads, list):
        raise AnsibleFilterError("The reads must be a list of loop results.")
    unread: list[str] = []
    for result in reads:
        if not isinstance(result, dict):
            raise AnsibleFilterError(f"{result!r} is not a loop result.")
        host = str(result.get("item", "?"))
        if result.get("unreachable") or result.get("rc") != 0:
            unread.append(f"{host} could not be read ({_reason(result)})")
            continue
        state, _, rest = str(result.get("stdout", "")).partition("\n")
        if state.strip() == "stopped":
            continue
        clock, _, text = rest.partition("\n")
        if state.strip() != "running" or not clock.strip().isdigit():
            raise AnsibleFilterError(f"{host} gave an answer that is not a read of the Lease: {result.get('stdout')!r}")
        if not text.strip():
            return {"state": "free", "message": f"there is no Lease (read through {host})"}
        try:
            lease = json.loads(text)
        except ValueError as exc:
            raise AnsibleFilterError(f"{host} gave a Lease that is not JSON: {exc}") from exc
        if not isinstance(lease, dict):
            raise AnsibleFilterError(f"{host} gave a Lease that is not an object: {text!r}")
        metadata = lease.get("metadata") or {}
        spec = lease.get("spec") or {}
        name = f"{metadata.get('namespace', '?')}/{metadata.get('name', '?')}"
        renewed = spec.get("renewTime") or spec.get("acquireTime")
        current = str(spec.get("holderIdentity") or "")
        if current != str(holder) or renewed is None:
            return {"state": "free", "message": f"the Lease {name} is not held by {holder} (read through {host})"}
        expires = _epoch(str(renewed)) + int(spec.get("leaseDurationSeconds") or 0)
        if expires <= int(clock):
            return {"state": "free", "message": f"the Lease {name} that {holder} held has run out (read through {host})"}
        swap = str((metadata.get("annotations") or {}).get(str(swap_id_annotation)) or "")
        until = datetime.fromtimestamp(expires).astimezone().isoformat(timespec="seconds")
        return {"state": "held", "message": f"the role controller ({holder}) holds the Lease {name} for the voter swap {swap or '(unnamed)'}, read through {host}, until {until} unless it renews it first"}
    if unread:
        return {"state": "unknown", "message": "whether the role controller holds the Lease cannot be told: " + "; ".join(unread)}
    return {"state": "free", "message": "no server of the inventory runs, so there is no Lease"}


class FilterModule:
    """Registers the role filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "cluster_host_role": cluster_host_role,
            "cluster_role_lease_guard": cluster_role_lease_guard,
        }
