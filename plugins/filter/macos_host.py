"""Filter the github_runner_macos_host role uses to compare the AC Power settings pmset reports with the ones it wants."""

from __future__ import annotations

from typing import Any

from ansible.errors import AnsibleFilterError

# The header pmset -g custom prints above the AC Power profile's settings; the next unindented line ends the profile.
AC_HEADER = "AC Power:"


def _ac_settings(report: str) -> dict[str, str]:
    """Return the setting names and values in the AC Power section of ``pmset -g custom``'s output.

    :raises AnsibleFilterError: the output has no AC Power section, so it is not what ``pmset -g custom`` prints.
    """
    settings: dict[str, str] = {}
    in_section = False
    found = False
    for line in report.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            in_section = line.strip() == AC_HEADER
            found = found or in_section
            continue
        if in_section:
            # The value is the last word and the name the rest, since a name can be several words ("Sleep On Power Button 1").
            parts = line.split()
            if len(parts) >= 2:
                settings[" ".join(parts[:-1])] = parts[-1]
    if not found:
        raise AnsibleFilterError(f"The output has no {AC_HEADER!r} section, so it is not the output of pmset -g custom: {report!r}.")
    return settings


def macos_host_pmset_ac_changes(report: Any, wanted: Any) -> list[str]:
    """Return the ``pmset -c`` arguments that bring the AC Power profile to the wanted values.

    A setting whose AC Power value already equals the wanted one gives no arguments, so a Mac already in line gives an empty list. A wanted setting the report does not list is hardware the Mac lacks (a desktop has no battery profile settings, a Mac without Power Nap has no ``powernap``), and gives none either.

    :param report: the output of ``pmset -g custom``.
    :param wanted: a mapping of setting names to the values wanted, as numbers or strings.
    :returns: ``[name, value, ...]`` in the order of ``wanted``, to follow ``pmset -c``.
    :raises AnsibleFilterError: ``report`` is not a string with an AC Power section, or ``wanted`` is not a mapping.
    """
    if not isinstance(report, str):
        raise AnsibleFilterError(f"The pmset report must be a string, not {type(report).__name__}.")
    if not isinstance(wanted, dict):
        raise AnsibleFilterError(f"The wanted settings must be a mapping, not {type(wanted).__name__}.")
    current = _ac_settings(report)
    arguments: list[str] = []
    for name, value in wanted.items():
        if name in current and current[name] != str(value):
            arguments.extend([str(name), str(value)])
    return arguments


class FilterModule:
    """Registers the macOS host filter with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {"macos_host_pmset_ac_changes": macos_host_pmset_ac_changes}
