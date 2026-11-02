"""Filters the github_runner_host_tools role uses: the Homebrew prefix a host has, the shell startup lines that bring Homebrew and mise into a shell, and mise's configuration rendered to TOML."""

from __future__ import annotations

import math
import re
import shlex
from typing import Any

from ansible.errors import AnsibleFilterError

# The prefixes Homebrew supports, which are where its bottles are built to be installed: a different prefix means compiling from source and is unsupported. macOS has one for each architecture; Linux has one whatever the architecture.
MACOS_PREFIXES = {"arm64": "/opt/homebrew", "x86_64": "/usr/local"}
LINUX_PREFIX = "/home/linuxbrew/.linuxbrew"

# The tables of a mise configuration file the role renders, in the order they are written.
MISE_TABLES = ("tools", "settings")

# A TOML bare key, which needs no quotes.
BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

# TOML basic strings escape these by name; every other control character is written as a \u escape.
STRING_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def host_tools_homebrew_prefix(system: Any, machine: Any) -> str:
    """Return the Homebrew prefix of a host.

    :param system: the host's ``system`` fact, ``Darwin`` or ``Linux``. :param machine: the host's ``machine`` fact, such as ``arm64`` or ``x86_64``. Only macOS has a prefix for each architecture. :returns: ``/opt/homebrew`` for Apple silicon, ``/usr/local`` for Intel and ``/home/linuxbrew/.linuxbrew`` for Linux. :raises AnsibleFilterError: Homebrew has no prefix for the system, or for the architecture on macOS.
    """
    if system == "Linux":
        return LINUX_PREFIX
    if system == "Darwin":
        if machine not in MACOS_PREFIXES:
            raise AnsibleFilterError(f"Homebrew supports macOS on {' and '.join(MACOS_PREFIXES)}, not {machine!r}.")
        return MACOS_PREFIXES[machine]
    raise AnsibleFilterError(f"Homebrew supports Darwin and Linux, not {system!r}.")


def host_tools_shell_integrations(prefix: Any, shell: Any, homebrew: Any, mise: Any) -> list[dict[str, str]]:
    """Return the startup lines that bring Homebrew and mise into a shell.

    Each line is guarded by a test that the binary is executable, so a startup file that outlives the tool does no harm. ``pattern`` is an extended regular expression that matches a line of a startup file that already does the same, outside a comment, and ``name`` tells the integrations apart.

    :param prefix: the Homebrew prefix, whose ``bin`` holds ``brew`` and ``mise``. :param shell: the shell's name, ``zsh`` or ``bash``, which mise's activation is generated for. :param homebrew: whether Homebrew is wanted in the shell. :param mise: whether mise is wanted in the shell. :returns: ``[{"name", "pattern", "line"}, ...]``, Homebrew first, with a dictionary for each tool that is wanted. :raises AnsibleFilterError: the prefix is not an absolute path or the shell is not one the role supports.
    """
    if not isinstance(prefix, str) or not prefix.startswith("/"):
        raise AnsibleFilterError(f"The Homebrew prefix must be an absolute path, not {prefix!r}.")
    if shell not in ("zsh", "bash"):
        raise AnsibleFilterError(f"The shell must be zsh or bash, not {shell!r}.")
    brew = shlex.quote(f"{prefix}/bin/brew")
    mise_binary = shlex.quote(f"{prefix}/bin/mise")
    integrations: list[dict[str, str]] = []
    if homebrew:
        integrations.append({"name": "homebrew", "pattern": "^[^#]*brew shellenv", "line": f'[ -x {brew} ] && eval "$({brew} shellenv)"'})
    if mise:
        integrations.append({"name": "mise", "pattern": "^[^#]*mise activate", "line": f'[ -x {mise_binary} ] && eval "$({mise_binary} activate {shell})"'})
    return integrations


def _key(name: Any) -> str:
    """Return a TOML key: bare when it can be, a quoted string when it has characters a bare key cannot."""
    if not isinstance(name, str) or not name:
        raise AnsibleFilterError(f"A key must be a non-empty string, not {name!r}.")
    return name if BARE_KEY.match(name) else _string(name)


def _string(text: str) -> str:
    """Return a TOML basic string."""
    escaped = "".join(STRING_ESCAPES.get(char) or (f"\\u{ord(char):04x}" if ord(char) < 0x20 or ord(char) == 0x7F else char) for char in text)
    return f'"{escaped}"'


def _scalar(value: Any, where: str) -> str:
    """Return a TOML string, boolean, integer or float.

    :raises AnsibleFilterError: the value is none of those, or is a float TOML would read as something else (not a number, or infinite).
    """
    # bool is a subclass of int, so it has to be tested first.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AnsibleFilterError(f"{where} must be a finite number, not {value!r}.")
        return repr(value)
    raise AnsibleFilterError(f"{where} must be a string, a number or a boolean, not {type(value).__name__}.")


def _table(name: str, entries: Any) -> list[str]:
    """Return the lines of one table, empty when it has no entries."""
    if not isinstance(entries, dict):
        raise AnsibleFilterError(f"{name} must be a mapping, not {type(entries).__name__}.")
    lines: list[str] = []
    for key, value in entries.items():
        where = f"{name}.{key}"
        if isinstance(value, list):
            if name == "tools" and not all(isinstance(item, str) for item in value):
                raise AnsibleFilterError(f"{where} must be a version string or a list of them.")
            rendered = "[" + ", ".join(_scalar(item, f"An item of {where}") for item in value) + "]"
        elif name == "tools":
            if not isinstance(value, str):
                raise AnsibleFilterError(f"{where} must be a version string or a list of them, not {type(value).__name__}.")
            rendered = _string(value)
        else:
            rendered = _scalar(value, where)
        lines.append(f"{_key(key)} = {rendered}")
    return [f"[{name}]", *lines] if lines else []


def host_tools_mise_toml(config: Any) -> str:
    """Return a mise configuration file for a mapping of its tables.

    :param config: a mapping with the tables ``tools`` (a tool name to a version string or a list of them) and ``settings`` (a setting name to a string, number or boolean, or a list of those). A table that is empty is left out. :returns: the TOML text, each table after a blank line and the whole ending in a newline, in the order tools then settings whatever the order of the mapping. :raises AnsibleFilterError: ``config`` is not a mapping, has a table the role does not render, a value of a kind its table does not take, or renders to nothing.
    """
    if not isinstance(config, dict):
        raise AnsibleFilterError(f"The mise configuration must be a mapping, not {type(config).__name__}.")
    unknown = [name for name in config if name not in MISE_TABLES]
    if unknown:
        raise AnsibleFilterError(f"The mise configuration has the tables {', '.join(map(repr, unknown))}; the role renders only {' and '.join(MISE_TABLES)}.")
    blocks = [lines for name in MISE_TABLES if name in config and (lines := _table(name, config[name]))]
    if not blocks:
        raise AnsibleFilterError("The mise configuration has no entry in any table, so there is nothing to render.")
    return "\n\n".join("\n".join(lines) for lines in blocks) + "\n"


class FilterModule:
    """Registers the host tools filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "host_tools_homebrew_prefix": host_tools_homebrew_prefix,
            "host_tools_shell_integrations": host_tools_shell_integrations,
            "host_tools_mise_toml": host_tools_mise_toml,
        }
