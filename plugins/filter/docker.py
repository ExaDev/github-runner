"""Filters the github_runner_cluster role uses to point the docker CLI at the same daemon as its community.docker module calls."""

from __future__ import annotations

from typing import Any

from ansible.errors import AnsibleFilterError


def docker_cli_environment(context: Any) -> dict[str, str]:
    """Return the environment that makes the docker CLI use a pinned Docker context.

    DOCKER_CONTEXT selects the context for that one command, and for the Compose plugin it runs, without touching the host's current context. With no context pinned the environment is empty, so the CLI keeps following the host's current context or DOCKER_HOST.

    :param context: the pinned context name, or an empty value for none. :returns: ``{"DOCKER_CONTEXT": context}``, or an empty mapping. :raises AnsibleFilterError: if the value is not a string.
    """
    if context is None or context == "":
        return {}
    if not isinstance(context, str):
        raise AnsibleFilterError(f"A Docker context name must be a string, not {type(context).__name__}: {context!r}.")
    name = context.strip()
    if name == "":
        return {}
    return {"DOCKER_CONTEXT": name}


class FilterModule:
    """Registers the Docker filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {"docker_cli_environment": docker_cli_environment}
