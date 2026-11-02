"""Filter the github_runner_cluster role uses to tell which container runtime a macOS host's Docker context belongs to, and so whether the role can start that runtime itself."""

from __future__ import annotations

import posixpath
import re
from typing import Any

from ansible.errors import AnsibleFilterError

RUNTIME_CHOICES = ("auto", "colima", "docker_desktop", "other")
# Colima names the Docker context of its default profile "colima" and any other profile's "colima-<profile>".
COLIMA_CONTEXT = "colima"
COLIMA_DEFAULT_PROFILE = "default"
# Colima's default home, which it falls back to when COLIMA_HOME is not set, relative to the user's home directory.
COLIMA_DEFAULT_HOME = ".colima"
# The Docker socket Colima creates in a profile's directory, <COLIMA_HOME>/<profile>/docker.sock, and so the endpoint of the Docker context it creates for the profile.
COLIMA_SOCKET = "docker.sock"
UNIX_SCHEME = "unix://"
# Docker Desktop's own context for its Linux engine.
DOCKER_DESKTOP_CONTEXTS = ("desktop-linux",)
# Lima's rule for an instance name, which is what a Colima profile becomes. It also keeps the value free of every character that would need escaping in a launchd property list or on a command line.
PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9]+([._-][A-Za-z0-9]+)*$")


def _text(name: str, value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AnsibleFilterError(f"{name} must be a string, not {type(value).__name__}: {value!r}.")
    return value.strip()


def _colima_profile_of(context: str) -> str | None:
    """Return the Colima profile a Docker context belongs to, or None when it is not a Colima context."""
    if context == COLIMA_CONTEXT:
        return COLIMA_DEFAULT_PROFILE
    prefix = COLIMA_CONTEXT + "-"
    if context.startswith(prefix) and len(context) > len(prefix):
        return context[len(prefix) :]
    return None


def cluster_container_runtime(context: Any, requested: Any = "auto", colima_profile: Any = "") -> dict[str, str]:
    """Work out which container runtime a macOS host's Docker daemon belongs to.

    The role calls it only for a macOS host, the only kind it starts a runtime on. ``auto`` takes the runtime from the Docker context the role drives: a Colima context (``colima`` or ``colima-<profile>``) is Colima and ``desktop-linux`` is Docker Desktop, and anything else (including no context because ``DOCKER_HOST`` is set) is ``other``.

    :param context: the Docker context the role drives, or an empty value for none. :param requested: ``auto``, ``colima``, ``docker_desktop`` or ``other``. :param colima_profile: the Colima profile to start, or an empty value to take it from the context (``default`` when the context names none).
    :returns: ``{"kind": "colima" | "docker_desktop" | "other", "colima_profile": <profile, empty unless kind is colima>}``.
    :raises AnsibleFilterError: if a value is not a string, ``requested`` is not one of the choices, the profile is not a valid Colima profile name, or it names another profile than the Colima context the role drives.
    """
    docker_context = _text("The Docker context", context)
    choice = _text("The container runtime", requested) or "auto"
    profile = _text("The Colima profile", colima_profile)
    if choice not in RUNTIME_CHOICES:
        raise AnsibleFilterError(f"The container runtime must be one of {', '.join(RUNTIME_CHOICES)}, not {choice!r}.")
    if profile and not PROFILE_PATTERN.match(profile):
        raise AnsibleFilterError(f"The Colima profile {profile!r} is not a valid profile name: use letters and digits, separated by single dots, hyphens or underscores.")
    context_profile = _colima_profile_of(docker_context)
    if choice == "auto":
        if context_profile is not None:
            choice = "colima"
        elif docker_context in DOCKER_DESKTOP_CONTEXTS:
            choice = "docker_desktop"
        else:
            choice = "other"
    if choice != "colima":
        return {"kind": choice, "colima_profile": ""}
    if profile and context_profile is not None and profile != context_profile:
        raise AnsibleFilterError(f"The Colima profile {profile!r} is not the one the Docker context {docker_context!r} belongs to ({context_profile!r}); the role would start one VM and drive another.")
    return {"kind": "colima", "colima_profile": profile or context_profile or COLIMA_DEFAULT_PROFILE}


def cluster_colima_home(endpoint: Any, profile: Any, user_home: Any) -> str:
    """Return the ``COLIMA_HOME`` a Colima profile needs, read from the endpoint of the Docker context Colima made for it.

    Colima puts a profile's Docker socket at ``<COLIMA_HOME>/<profile>/docker.sock`` and points the profile's context at it, so the context says where the profile lives. A command that starts the profile has to run with that ``COLIMA_HOME``, because Colima falls back to ``~/.colima`` when the variable is missing and would then start a different profile of the same name there.

    :param endpoint: The Docker host of the profile's context, ``unix://<COLIMA_HOME>/<profile>/docker.sock``.
    :param profile: The Colima profile the context belongs to.
    :param user_home: The home directory of the user the profile belongs to.
    :returns: The directory, or an empty string when it is ``<user_home>/.colima``, the home Colima uses when ``COLIMA_HOME`` is not set, so a profile in the default home keeps running with no variable.
    :raises AnsibleFilterError: a value is not a string, the profile is not a valid profile name, or the endpoint is not the socket of that profile in an absolute directory.
    """
    host = _text("The Docker context's endpoint", endpoint)
    profile_name = _text("The Colima profile", profile)
    home = _text("The user's home directory", user_home)
    if not PROFILE_PATTERN.match(profile_name):
        raise AnsibleFilterError(f"The Colima profile {profile_name!r} is not a valid profile name: use letters and digits, separated by single dots, hyphens or underscores.")
    path = host.removeprefix(UNIX_SCHEME) if host.startswith(UNIX_SCHEME) else ""
    profile_directory, socket = posixpath.split(path)
    colima_home, directory = posixpath.split(profile_directory)
    if socket != COLIMA_SOCKET or directory != profile_name or not posixpath.isabs(colima_home):
        raise AnsibleFilterError(f"The Docker context's endpoint {host!r} is not the socket of the Colima profile {profile_name!r} (unix://<COLIMA_HOME>/{profile_name}/{COLIMA_SOCKET}), so the role cannot tell where that profile lives: set github_runner_cluster_colima_home.")
    if colima_home == posixpath.join(home, COLIMA_DEFAULT_HOME):
        return ""
    return colima_home


class FilterModule:
    """Registers the container runtime filter with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {"cluster_container_runtime": cluster_container_runtime, "cluster_colima_home": cluster_colima_home}
