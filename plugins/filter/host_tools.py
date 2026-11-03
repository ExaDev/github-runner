"""Filters the github_runner_host_tools role uses: the Homebrew prefix a host has, the shell startup lines that bring Homebrew and mise into a shell or set environment variables in it, whether a managed block leads its startup file, mise's configuration rendered to TOML, the managed lines of an npmrc file and a YAML file merged with settings."""

from __future__ import annotations

import math
import re
import shlex
from typing import Any

import yaml
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


# The name of an environment variable the shell accepts in an assignment.
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The npmrc key whose values are a list, written as one key[]=value line for each. It has a variable of its own, so the scalar settings refuse it.
NPM_LIST_KEY = "min-release-age-exclude"

# An npmrc key, and a package name or pattern in an exclusion list.
NPM_KEY = re.compile(r"^[A-Za-z0-9_@:./-]+$")
NPM_PACKAGE = re.compile(r"^[A-Za-z0-9_@~./*-]+$")

# An npmrc value written bare. The ini format npm reads treats ; and # as the start of a comment and a leading quote as quoting, and the value is trimmed, so a string with any of them would not read back as given.
NPM_VALUE = re.compile(r"^[^\x00-\x1f#;\"'\\]*$")


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


def host_tools_shell_integrations(prefix: Any, shell: Any, homebrew: Any, mise: Any) -> list[dict[str, Any]]:
    """Return the startup lines that bring Homebrew and mise into a shell.

    Each line is guarded by a test that the binary is executable, so a startup file that outlives the tool does no harm. ``pattern`` is an extended regular expression that matches a line of a startup file that already does the same, outside a comment, ``name`` tells the integrations apart, and ``non_interactive`` is false because Homebrew and mise are wanted in an interactive shell only (the line goes where the file's own interactive setup is, never ahead of an early return for non-interactive shells).

    :param prefix: the Homebrew prefix, whose ``bin`` holds ``brew`` and ``mise``. :param shell: the shell's name, ``zsh`` or ``bash``, which mise's activation is generated for. :param homebrew: whether Homebrew is wanted in the shell. :param mise: whether mise is wanted in the shell. :returns: ``[{"name", "pattern", "line", "non_interactive"}, ...]``, Homebrew first, with a dictionary for each tool that is wanted. :raises AnsibleFilterError: the prefix is not an absolute path or the shell is not one the role supports.
    """
    if not isinstance(prefix, str) or not prefix.startswith("/"):
        raise AnsibleFilterError(f"The Homebrew prefix must be an absolute path, not {prefix!r}.")
    if shell not in ("zsh", "bash"):
        raise AnsibleFilterError(f"The shell must be zsh or bash, not {shell!r}.")
    brew = shlex.quote(f"{prefix}/bin/brew")
    mise_binary = shlex.quote(f"{prefix}/bin/mise")
    integrations: list[dict[str, Any]] = []
    if homebrew:
        integrations.append({"name": "homebrew", "pattern": "^[^#]*brew shellenv", "line": f'[ -x {brew} ] && eval "$({brew} shellenv)"', "non_interactive": False})
    if mise:
        integrations.append({"name": "mise", "pattern": "^[^#]*mise activate", "line": f'[ -x {mise_binary} ] && eval "$({mise_binary} activate {shell})"', "non_interactive": False})
    return integrations


def _env_value(name: str, value: Any) -> str:
    """Return the text of an environment variable's value: a string as it is, a boolean as true or false, a number as its digits.

    :raises AnsibleFilterError: the value is none of those, or is a string of more than one line or with a NUL, which a startup file line cannot hold.
    """
    # bool is a subclass of int, so it has to be tested first.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if any(char in value for char in "\n\r\x00"):
            raise AnsibleFilterError(f"The value of {name} must be one line without a NUL.")
        return str(value)
    raise AnsibleFilterError(f"The value of {name} must be a string, a number or a boolean, not {type(value).__name__}.")


def _env_name(name: Any) -> str:
    """Return the name of an environment variable.

    :raises AnsibleFilterError: the name is not one the shell accepts in an assignment.
    """
    if not isinstance(name, str) or not ENV_NAME.match(name):
        raise AnsibleFilterError(f"{name!r} is not a name a shell accepts for an environment variable.")
    return name


def _export_line(name: str, word: str) -> dict[str, Any]:
    """Return the startup line that exports a variable set to a shell word, with the pattern that finds an assignment of it already in a startup file."""
    return {"name": f"env-{name}", "pattern": f"^[[:space:]]*(export[[:space:]]+)?{name}=", "line": f"export {name}={word}", "non_interactive": True}


def _home_relative_value(name: str, value: Any) -> str:
    """Return the shell word that expands to the user's home directory followed by a relative path: ``"$HOME/<path>"`` with the path escaped for double quotes.

    Inside double quotes the shell expands ``$`` and the backtick and gives the backslash and the double quote a meaning, and nothing else, in sh, bash and zsh alike, so a backslash before each of those four makes the path reach the variable exactly as given. (``!`` is special only to an interactive shell reading a typed line, never to one reading a startup file.)

    :raises AnsibleFilterError: the value is not a string, is empty, is absolute, has a ``..`` component, or has a newline or a NUL, which a startup file line cannot hold.
    """
    if not isinstance(value, str):
        raise AnsibleFilterError(f"The home-relative path of {name} must be a string, not {type(value).__name__}.")
    if any(char in value for char in "\n\r\x00"):
        raise AnsibleFilterError(f"The home-relative path of {name} must be one line without a NUL.")
    if not value:
        raise AnsibleFilterError(f"The home-relative path of {name} must not be empty.")
    if value.startswith("/"):
        raise AnsibleFilterError(f"The path of {name} must be relative to the user's home directory, not absolute ({value!r}); a fixed absolute path belongs in github_runner_host_tools_shell_env.")
    if ".." in value.split("/"):
        raise AnsibleFilterError(f"The path of {name} must stay inside the user's home directory, so it must not have a '..' component ({value!r}).")
    return '"$HOME/' + re.sub(r'([\\$`"])', r"\\\1", value) + '"'


def host_tools_shell_env(env: Any, home_relative: Any) -> list[dict[str, Any]]:
    """Return the startup lines that set environment variables in a shell, in the shape host_tools_shell_integrations returns, with ``non_interactive`` true: a variable is for every shell the user's commands run in, and a non-interactive bash returns from ``~/.bashrc`` at the file's interactive guard, so the caller writes these lines ahead of everything else in the file.

    A value in ``env`` is quoted for the shell with ``shlex.quote``, so it reaches the variable exactly as given whatever it holds (spaces, quotes, ``$``, backticks), in zsh and in bash alike. A value in ``home_relative`` is a path below the user's home directory and is written as ``"$HOME/<path>"``, so the shell works out the home directory when the file is read, which the role cannot know when it runs without gathering facts. ``pattern`` is an extended regular expression that matches a line of a startup file that already assigns the variable, outside a comment, whatever its value, so a value the user chose is never overridden, and ``name`` tells the lines apart.

    :param env: a mapping of variable names to string, number or boolean values. :param home_relative: a mapping of variable names to paths relative to the user's home directory. :returns: ``[{"name", "pattern", "line", "non_interactive"}, ...]``, the variables of ``env`` in its order and then those of ``home_relative`` in its. :raises AnsibleFilterError: an argument is not a mapping, a name is not one the shell accepts or is in both mappings, a value of ``env`` is not a single-line string, a number or a boolean, or a path of ``home_relative`` is not a non-empty relative path without a ``..`` component.
    """
    if not isinstance(env, dict):
        raise AnsibleFilterError(f"The shell environment must be a mapping, not {type(env).__name__}.")
    if not isinstance(home_relative, dict):
        raise AnsibleFilterError(f"The home-relative shell environment must be a mapping, not {type(home_relative).__name__}.")
    both = [name for name in env if name in home_relative]
    if both:
        raise AnsibleFilterError(f"{', '.join(map(str, both))} is set in both the shell environment and the home-relative one; give each variable once.")
    lines: list[dict[str, Any]] = []
    for name, value in env.items():
        lines.append(_export_line(name, shlex.quote(_env_value(_env_name(name), value))))
    for name, value in home_relative.items():
        lines.append(_export_line(name, _home_relative_value(_env_name(name), value)))
    return lines


def host_tools_block_leads(existing: Any, marker: Any, stem: Any) -> bool:
    """Return whether a managed block comes before everything in a startup file that could return early.

    The role writes the blocks a non-interactive shell must read at the top of the file, because a non-interactive bash reads ``~/.bashrc`` only as far as the interactive guard (``case $- in *i*) ;; *) return;; esac``). A block leads the file when every line above its BEGIN marker is blank, a comment, or inside a block of the role's own, which never returns. The test is the file's own order, not a guess at what the guard looks like, so a guard written any other way is handled alike: any code above the block counts as the guard.

    :param existing: the text of the startup file. :param marker: the block's marker line with ``{mark}`` where BEGIN and END go. :param stem: the start of the marker line of every block the role writes in this file, with ``{mark}`` where BEGIN and END go (the marker without the block's name). :returns: whether nothing but blank lines, comments and the role's blocks come above the block. :raises AnsibleFilterError: an argument is not a string, or the file has no block with the marker.
    """
    if not (isinstance(existing, str) and isinstance(marker, str) and isinstance(stem, str)):
        raise AnsibleFilterError("The startup file's text, the block's marker and the marker stem must be strings.")
    begin, own_begin, own_end = marker.replace("{mark}", "BEGIN"), stem.replace("{mark}", "BEGIN"), stem.replace("{mark}", "END")
    inside = False
    for line in existing.splitlines():
        text = line.rstrip()
        if text == begin:
            return True
        if inside:
            inside = not text.startswith(own_end)
        elif text.startswith(own_begin):
            inside = True
        elif text.strip() and not text.lstrip().startswith("#"):
            return False
    raise AnsibleFilterError(f"The startup file has no block that starts with {begin!r}.")


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


def _npm_value(key: str, value: Any) -> str:
    """Return the text of an npmrc value: a string as it is, a boolean as true or false, a number as its digits.

    :raises AnsibleFilterError: the value is none of those, or is a string that would not read back as given (a comment character, a quote, a backslash, a control character or surrounding space).
    """
    # bool is a subclass of int, so it has to be tested first.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AnsibleFilterError(f"{key} must be a finite number, not {value!r}.")
        return repr(value)
    if isinstance(value, str):
        if not NPM_VALUE.match(value) or value != value.strip():
            raise AnsibleFilterError(f"{key} must not contain a quote, a backslash, a control character, # or ;, or start or end with a space, since npm would not read it back as given.")
        return str(value)
    raise AnsibleFilterError(f"{key} must be a string, a number or a boolean, not {type(value).__name__}.")


def _outside_block(existing: str, marker: str) -> list[str]:
    """Return the lines of a text that are not between the BEGIN and END marker lines of the role's block, which are dropped with it. A BEGIN with no END after it drops nothing, since there is no block to speak of."""
    lines = existing.splitlines()
    begin, end = marker.replace("{mark}", "BEGIN"), marker.replace("{mark}", "END")
    stripped = [line.rstrip() for line in lines]
    if begin not in stripped or end not in stripped[stripped.index(begin) :]:
        return lines
    first = stripped.index(begin)
    last = first + stripped[first:].index(end)
    return lines[:first] + lines[last + 1 :]


def host_tools_npmrc(settings: Any, excludes: Any, existing: Any, marker: Any) -> dict[str, list[str]]:
    """Return the lines the role's block of an npmrc file holds, and the settings it leaves out because the file already sets them.

    A key the file sets outside the block is the user's own: it is left out of the lines and reported in ``shadowed``, so the role never overrides it (appended after it, a line of the block would win, because npm reads the last of two assignments), and an exclusion the file lists outside the block is left out silently, since listing it twice changes nothing. The lines are only key names and the values the caller gave: nothing of the file's other lines is returned or put in an error.

    :param settings: a mapping of npmrc keys to string, number or boolean values. :param excludes: a list of package names or patterns, each written as ``min-release-age-exclude[]=NAME``. :param existing: the text of the file, empty when it does not exist yet. :param marker: the block's marker line with ``{mark}`` where BEGIN and END go, which tells the role's block apart from the user's lines. :returns: ``{"lines": [...], "shadowed": [...]}``, the lines in the mapping's order and then the exclusions, without repeats. :raises AnsibleFilterError: ``settings`` is not a mapping or ``excludes`` not a list, a key or package name has a character an npmrc cannot hold, a key is the exclusion list's own, or a value is not a string, number or boolean that reads back as given.
    """
    if not isinstance(settings, dict):
        raise AnsibleFilterError(f"The npm settings must be a mapping, not {type(settings).__name__}.")
    if not isinstance(excludes, list):
        raise AnsibleFilterError(f"The npm exclusions must be a list, not {type(excludes).__name__}.")
    if not isinstance(existing, str) or not isinstance(marker, str):
        raise AnsibleFilterError("The npmrc text and the block's marker must be strings.")
    outside = _outside_block(existing, marker)
    lines: list[str] = []
    shadowed: list[str] = []
    for key, value in settings.items():
        if not isinstance(key, str) or not NPM_KEY.match(key):
            raise AnsibleFilterError(f"{key!r} is not a key an npmrc file can hold.")
        if key == NPM_LIST_KEY:
            raise AnsibleFilterError(f"{key} is a list, so it has a variable of its own and is not one of the npm settings.")
        text = _npm_value(key, value)
        if any(re.match(rf"^\s*{re.escape(key)}\s*(=|$)", line) for line in outside):
            shadowed.append(key)
        else:
            lines.append(f"{key}={text}")
    listed = {match.group(1).strip().strip("\"'") for line in outside if (match := re.match(rf"^\s*{re.escape(NPM_LIST_KEY)}\[\]\s*=\s*(.*)$", line))}
    for name in excludes:
        if not isinstance(name, str) or not NPM_PACKAGE.match(name):
            raise AnsibleFilterError(f"{name!r} is not a package name or pattern an npmrc exclusion can hold.")
        line = f"{NPM_LIST_KEY}[]={name}"
        if name not in listed and line not in lines:
            lines.append(line)
    return {"lines": lines, "shadowed": shadowed}


class _IndentedDumper(yaml.SafeDumper):
    """A YAML dumper that indents a list under its key, as hand-written configuration files do."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def _plain(value: Any, where: str) -> Any:
    """Return a copy of a YAML value made of the plain types a dump writes: strings, booleans, numbers, lists and mappings with string keys.

    :raises AnsibleFilterError: the value holds anything else, a null included, or a number that is not finite.
    """
    # bool is a subclass of int, so it has to be tested first.
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AnsibleFilterError(f"{where} must be a finite number, not {value!r}.")
        return float(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_plain(item, f"An item of {where}") for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise AnsibleFilterError(f"{where} must have string keys.")
        return {str(key): _plain(item, f"{where}.{key}") for key, item in value.items()}
    raise AnsibleFilterError(f"{where} must be a string, a number, a boolean, a list or a mapping, not {type(value).__name__}.")


def _same(left: Any, right: Any) -> bool:
    """Return whether two YAML values are equal and of the same types all the way down, which ``==`` is not (``True == 1`` and ``1 == 1.0``)."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return bool(left == right)


def _block_end(lines: list[str], start: int) -> int:
    """Return the index after the last line of the top-level entry that starts at ``start``: the lines indented under it and the list items written at its own indent, without the blank and comment lines after them, which belong to what follows."""
    end = start + 1
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip() or line.startswith("#"):
            continue
        if line[0] in " \t" or line.startswith("- ") or line.rstrip() == "-":
            end = index + 1
            continue
        break
    return end


def host_tools_yaml_merge(existing: Any, settings: Any) -> str:
    """Return a YAML file's text with top-level keys set to the given values, and everything else in the file as it was.

    Only the entries of the keys whose value differs are rewritten, in place, or appended when the file has no such key; every other line, comments and blank lines included, is kept byte for byte, and a key that already holds the value is not touched, so the result equals the input when there is nothing to change. The result is parsed and compared with what the merge should hold, and a file whose layout the merge cannot edit safely (a flow-style mapping at the top, several documents) is refused rather than written wrong. A comment on a rewritten key's own line goes with its old value.

    :param existing: the file's text, empty when it does not exist yet. :param settings: a mapping of top-level keys to string, number, boolean, list or mapping values. :returns: the new text. :raises AnsibleFilterError: ``existing`` is not YAML or not a mapping at the top, ``settings`` holds a value that is none of the plain types, or the merge could not be made without changing something else.
    """
    if not isinstance(existing, str):
        raise AnsibleFilterError(f"The existing YAML must be text, not {type(existing).__name__}.")
    if not isinstance(settings, dict):
        raise AnsibleFilterError(f"The YAML settings must be a mapping, not {type(settings).__name__}.")
    wanted = _plain(settings, "The YAML settings")
    try:
        current = yaml.safe_load(existing)
    except yaml.YAMLError as error:
        raise AnsibleFilterError(f"The existing file is not valid YAML, so nothing was merged into it ({getattr(error, 'problem', type(error).__name__)}).") from error
    if current is None:
        current = {}
    if not isinstance(current, dict):
        raise AnsibleFilterError("The existing file is not a mapping at the top, so nothing was merged into it.")
    lines = existing.splitlines(keepends=True)
    expected = dict(current)
    for key, value in wanted.items():
        if key in current and _same(current[key], value):
            continue
        rendered = yaml.dump({key: value}, Dumper=_IndentedDumper, default_flow_style=False, sort_keys=False, allow_unicode=True, width=10**9).splitlines(keepends=True)
        starts = [index for index, line in enumerate(lines) if re.match(rf"""^(?:{re.escape(key)}|"{re.escape(key)}"|'{re.escape(key)}')[ \t]*:(?=[ \t\r\n]|$)""", line)]
        if starts:
            lines[starts[-1] : _block_end(lines, starts[-1])] = rendered
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += "\n"
            lines.extend(rendered)
        expected[key] = value
    merged = "".join(lines)
    try:
        reread = yaml.safe_load(merged)
    except yaml.YAMLError:
        reread = None
    if not _same(reread if reread is not None else {}, expected):
        raise AnsibleFilterError("The file's layout is one the merge cannot edit safely, so nothing was merged into it.")
    return merged


class FilterModule:
    """Registers the host tools filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "host_tools_homebrew_prefix": host_tools_homebrew_prefix,
            "host_tools_shell_integrations": host_tools_shell_integrations,
            "host_tools_shell_env": host_tools_shell_env,
            "host_tools_block_leads": host_tools_block_leads,
            "host_tools_mise_toml": host_tools_mise_toml,
            "host_tools_npmrc": host_tools_npmrc,
            "host_tools_yaml_merge": host_tools_yaml_merge,
        }
