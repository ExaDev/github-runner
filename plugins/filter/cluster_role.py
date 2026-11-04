"""Filters the github_runner_cluster role uses to decide which role, server or agent, a host runs, whether it runs natively or under Docker Compose."""

from __future__ import annotations

from typing import Any

from ansible.errors import AnsibleFilterError

NODE_ROLES = ("server", "agent")


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


class FilterModule:
    """Registers the role filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "cluster_host_role": cluster_host_role,
        }
