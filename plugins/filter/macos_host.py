"""Filters the github_runner_macos_host role uses to compare what pmset reports with what it wants: the AC Power settings, and whether sleep is disabled system-wide."""

from __future__ import annotations

from typing import Any

from ansible.errors import AnsibleFilterError

# The header pmset -g custom prints above the AC Power profile's settings; the next unindented line ends the profile.
AC_HEADER = "AC Power:"
# The header pmset -g prints above the settings in force, among them SleepDisabled once disablesleep is on.
IN_USE_HEADER = "Currently in use:"
# The setting pmset -a disablesleep 1 turns on. Unlike the AC Power settings it is system-wide, so pmset -g custom does not list it.
SLEEP_DISABLED = "SleepDisabled"


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


def macos_host_pmset_sleep_disabled(report: Any) -> bool:
    """Return whether ``pmset -g`` reports sleep as disabled, which is what ``pmset -a disablesleep 1`` sets and the only thing that stops a MacBook with its lid closed from sleeping (``sleep 0`` does not cover clamshell sleep).

    A report with no ``SleepDisabled`` line gives ``False`` like one that says ``SleepDisabled 0``, since a Mac that never had the setting turned on does not list it.

    :param report: the output of ``pmset -g``.
    :returns: ``True`` when ``SleepDisabled`` is ``1``.
    :raises AnsibleFilterError: ``report`` is not a string with a ``Currently in use:`` section, so it is not what ``pmset -g`` prints.
    """
    if not isinstance(report, str):
        raise AnsibleFilterError(f"The pmset report must be a string, not {type(report).__name__}.")
    lines = [line.split() for line in report.splitlines()]
    if not any(" ".join(words) == IN_USE_HEADER for words in lines):
        raise AnsibleFilterError(f"The output has no {IN_USE_HEADER!r} section, so it is not the output of pmset -g: {report!r}.")
    return any(len(words) == 2 and words[0] == SLEEP_DISABLED and words[1] == "1" for words in lines)


class FilterModule:
    """Registers the macOS host filter with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "macos_host_pmset_ac_changes": macos_host_pmset_ac_changes,
            "macos_host_pmset_sleep_disabled": macos_host_pmset_sleep_disabled,
        }
