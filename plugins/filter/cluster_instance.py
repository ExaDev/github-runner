"""Filters the github_runner_cluster role uses before it changes anything on a host, to tell whether the cluster it is about to set up there would collide with another Compose project on the same Docker daemon: a container name, the published API port or the project directory that another project already uses."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from ansible.errors import AnsibleFilterError

PROJECT_LABEL = "com.docker.compose.project"
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"


def _owner(labels: dict[str, Any]) -> str:
    """Name the Compose project a container belongs to, for a message."""
    project = labels.get(PROJECT_LABEL) or ""
    return f"the Compose project {project}" if project else "no Compose project"


def _host_ports(bindings: Any) -> set[str]:
    """Every host port a container's port bindings publish, as strings, whatever the address or protocol."""
    ports: set[str] = set()
    if not isinstance(bindings, dict):
        return ports
    for published in bindings.values():
        for binding in published or []:
            if isinstance(binding, dict) and binding.get("HostPort"):
                ports.add(str(binding["HostPort"]))
    return ports


def cluster_instance_conflicts(lines: Any, project: Any, names: Any, port: Any, directory: Any) -> list[str]:
    """Say what on a Docker daemon another Compose project already uses that this cluster's project would need.

    :param lines: one JSON object per container on the daemon, as ``docker inspect`` prints with the format in tasks/instance_check.yml: ``name`` (with Docker's leading slash), ``labels`` (the container's labels, or null) and ``ports`` (its HostConfig.PortBindings, or null). :param project: this cluster's Compose project name on the host. :param names: the container names this project creates (the server's and the agent's). :param port: the host port this project publishes, or an empty string when it publishes none (an agent). :param directory: this project's directory on the host. :returns: one sentence per container of another project (or of none) that has one of the names, publishes the port or runs from the directory; empty when there is no collision. Containers of this project itself are never a collision, since Compose recreates or keeps them. :raises AnsibleFilterError: if the arguments have the wrong types or a line is not such an object.
    """
    if not isinstance(lines, Sequence) or isinstance(lines, str) or not isinstance(project, str) or project == "":
        raise AnsibleFilterError("The lines must be a list and the project a non-empty string.")
    if not isinstance(names, Sequence) or isinstance(names, str) or not all(isinstance(name, str) and name for name in names):
        raise AnsibleFilterError("The names must be a list of non-empty strings.")
    if not isinstance(directory, str) or directory == "":
        raise AnsibleFilterError("The directory must be a non-empty string.")
    wanted_port = str(port).strip()
    conflicts: list[str] = []
    for line in lines:
        if not isinstance(line, str) or line.strip() == "":
            continue
        try:
            container = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AnsibleFilterError(f"A container line is not JSON: {exc}") from exc
        if not isinstance(container, dict) or not isinstance(container.get("name"), str):
            raise AnsibleFilterError("A container line has no name.")
        labels = container.get("labels") or {}
        if not isinstance(labels, dict):
            raise AnsibleFilterError(f"The container {container['name']} has labels that are not a mapping.")
        if labels.get(PROJECT_LABEL) == project:
            continue
        name = container["name"].lstrip("/")
        owner = _owner(labels)
        if name in names:
            conflicts.append(f"a container named {name} already exists and belongs to {owner}")
        if wanted_port and wanted_port in _host_ports(container.get("ports")):
            conflicts.append(f"the container {name}, of {owner}, already publishes host port {wanted_port}")
        if labels.get(WORKING_DIR_LABEL) == directory:
            conflicts.append(f"the container {name}, of {owner}, runs from the same project directory {directory}")
    return conflicts


class FilterModule:
    """Registers the cluster instance filter with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {"cluster_instance_conflicts": cluster_instance_conflicts}
