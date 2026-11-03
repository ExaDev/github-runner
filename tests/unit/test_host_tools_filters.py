"""Tests the filters of the github_runner_host_tools role in plugins/filter/host_tools.py: the Homebrew prefix a host has, the shell startup lines that bring Homebrew and mise into a shell or set environment variables in it, whether a managed block leads its startup file, mise's configuration rendered to TOML, the lines of the managed block of an npmrc file and a YAML file merged with settings, against golden strings."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml
from mise_reader import mise_env

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "filter" / "host_tools.py"

_spec = importlib.util.spec_from_file_location("host_tools_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
host_tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(host_tools)


class PrefixFilterTest(unittest.TestCase):
    def test_apple_silicon_has_its_own_prefix(self) -> None:
        self.assertEqual(host_tools.host_tools_homebrew_prefix("Darwin", "arm64"), "/opt/homebrew")

    def test_intel_macs_have_theirs(self) -> None:
        self.assertEqual(host_tools.host_tools_homebrew_prefix("Darwin", "x86_64"), "/usr/local")

    def test_linux_has_one_prefix_whatever_the_architecture(self) -> None:
        for machine in ("x86_64", "aarch64"):
            with self.subTest(machine=machine):
                self.assertEqual(host_tools.host_tools_homebrew_prefix("Linux", machine), "/home/linuxbrew/.linuxbrew")

    def test_an_architecture_or_system_it_does_not_know_is_refused(self) -> None:
        with self.assertRaisesRegex(Exception, "not 'ppc64'"):
            host_tools.host_tools_homebrew_prefix("Darwin", "ppc64")
        with self.assertRaisesRegex(Exception, "not 'FreeBSD'"):
            host_tools.host_tools_homebrew_prefix("FreeBSD", "amd64")


class ShellIntegrationFilterTest(unittest.TestCase):
    def test_each_tool_gets_a_guarded_line_and_a_pattern_for_an_equivalent(self) -> None:
        self.assertEqual(
            host_tools.host_tools_shell_integrations("/opt/homebrew", "zsh", True, True),
            [
                {"name": "homebrew", "pattern": "^[^#]*brew shellenv", "line": '[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"', "non_interactive": False},
                {"name": "mise", "pattern": "^[^#]*mise activate", "line": '[ -x /opt/homebrew/bin/mise ] && eval "$(/opt/homebrew/bin/mise activate zsh)"', "non_interactive": False},
            ],
        )

    def test_mise_is_activated_for_the_shell_it_is_in(self) -> None:
        [mise] = [item for item in host_tools.host_tools_shell_integrations("/usr/local", "bash", False, True) if item["name"] == "mise"]
        self.assertTrue(mise["line"].endswith('activate bash)"'))

    def test_a_tool_that_is_not_wanted_has_no_line(self) -> None:
        self.assertEqual([item["name"] for item in host_tools.host_tools_shell_integrations("/usr/local", "bash", True, False)], ["path", "homebrew"])
        self.assertEqual([item["name"] for item in host_tools.host_tools_shell_integrations("/usr/local", "zsh", True, False)], ["homebrew"])
        self.assertEqual(host_tools.host_tools_shell_integrations("/usr/local", "bash", False, False), [])

    def test_a_prefix_with_a_space_is_quoted(self) -> None:
        [brew] = host_tools.host_tools_shell_integrations("/opt/my brew", "zsh", True, False)
        self.assertEqual(brew["line"], '[ -x \'/opt/my brew/bin/brew\' ] && eval "$(\'/opt/my brew/bin/brew\' shellenv)"')

    def test_the_patterns_ignore_a_commented_line_and_find_a_real_one(self) -> None:
        brew, mise = host_tools.host_tools_shell_integrations("/usr/local", "zsh", True, True)
        for item, text in ((brew, "brew shellenv"), (mise, "mise activate")):
            with self.subTest(tool=item["name"]):
                self.assertIsNotNone(re.search(item["pattern"], f'eval "$(/x/bin/{text} zsh)"'))
                self.assertIsNone(re.search(item["pattern"], f'# eval "$(/x/bin/{text})"'))

    def test_a_relative_prefix_or_an_unknown_shell_is_refused(self) -> None:
        with self.assertRaisesRegex(Exception, "absolute"):
            host_tools.host_tools_shell_integrations("opt/homebrew", "zsh", True, True)
        with self.assertRaisesRegex(Exception, "zsh or bash"):
            host_tools.host_tools_shell_integrations("/opt/homebrew", "fish", True, True)


class BashPathFilterTest(unittest.TestCase):
    def path_item(self, prefix: str = "/home/linuxbrew/.linuxbrew", homebrew: bool = True, mise: bool = True) -> dict[str, object]:
        [item] = [item for item in host_tools.host_tools_shell_integrations(prefix, "bash", homebrew, mise) if item["name"] == "path"]
        return item

    def test_bash_gets_one_line_with_the_shims_ahead_of_homebrews_directories_for_a_non_interactive_shell(self) -> None:
        self.assertEqual(
            self.path_item(),
            {
                "name": "path",
                "pattern": "^[^#]*PATH=[^#]*/home/linuxbrew/\\.linuxbrew/bin",
                "line": 'export PATH="$HOME/.local/share/mise/shims:/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:$PATH"',
                "non_interactive": True,
            },
        )

    def test_only_the_tools_wanted_are_on_the_path(self) -> None:
        self.assertEqual(self.path_item(mise=False)["line"], 'export PATH="/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:$PATH"')
        self.assertEqual(self.path_item(homebrew=False)["line"], 'export PATH="$HOME/.local/share/mise/shims:$PATH"')

    def test_the_tools_own_lines_stay_for_interactive_shells_only(self) -> None:
        items = {item["name"]: item["non_interactive"] for item in host_tools.host_tools_shell_integrations("/usr/local", "bash", True, True)}
        self.assertEqual(items, {"path": True, "homebrew": False, "mise": False})

    def test_zsh_gets_no_path_line_because_zshenv_is_read_by_every_zsh(self) -> None:
        self.assertNotIn("path", [item["name"] for item in host_tools.host_tools_shell_integrations("/usr/local", "zsh", True, True)])

    def test_the_line_gives_a_shell_the_directories_exactly_whatever_the_prefix_and_home_hold(self) -> None:
        for prefix in ("/home/linuxbrew/.linuxbrew", "/opt/my brew", "/opt/$brew", "/opt/`id`", '/opt/"q"', "/opt/it's", "/opt/back\\slash", "/opt/a:b"):
            item = self.path_item(prefix)
            for home in ("/home/example", "/home/a b", "/home/it's", '/home/"q"', "/home/$d"):
                for shell in ("sh", "bash"):
                    with self.subTest(prefix=prefix, home=home, shell=shell):
                        result = subprocess.run([shell, "-c", str(item["line"]) + '\nprintf %s "$PATH"'], capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin", "HOME": home})
                        self.assertEqual(result.stdout, f"{home}/.local/share/mise/shims:{prefix}/bin:{prefix}/sbin:/usr/bin:/bin")
                        self.assertEqual(result.stderr, "")

    def test_the_pattern_finds_a_path_assignment_naming_the_prefix_and_nothing_else(self) -> None:
        item = self.path_item()
        pattern = str(item["pattern"])
        for text in ('export PATH="/home/linuxbrew/.linuxbrew/bin:$PATH"', "PATH=$PATH:/home/linuxbrew/.linuxbrew/bin", '  export PATH=/home/linuxbrew/.linuxbrew/bin:/usr/bin'):
            with self.subTest(text=text):
                self.assertTrue(grep_finds(pattern, text))
        for text in ('# export PATH="/home/linuxbrew/.linuxbrew/bin:$PATH"', 'eval "$(/home/linuxbrew/.linuxbrew/bin/brew shellenv)"', 'export PATH="$HOME/.local/bin:$PATH"', 'export PATH="/home/linuxbrewX.linuxbrew/bin:$PATH"', 'export PATH="/opt/homebrew/bin:$PATH"'):
            with self.subTest(text=text):
                self.assertFalse(grep_finds(pattern, text))

    def test_a_prefix_with_characters_special_in_a_regular_expression_is_matched_literally(self) -> None:
        pattern = str(self.path_item("/opt/a+b(c)[d]{e}|f.g")["pattern"])
        self.assertTrue(grep_finds(pattern, "export PATH=/opt/a+b(c)[d]{e}|f.g/bin:$PATH"))
        self.assertFalse(grep_finds(pattern, "export PATH=/opt/aab(c)[d]{e}|fxg/bin:$PATH"))


class MiseTomlFilterTest(unittest.TestCase):
    def test_tools_and_settings_render_as_two_tables(self) -> None:
        config = {"tools": {"node": "22", "python": ["3.13", "3.12"]}, "settings": {"experimental": True, "jobs": 4, "ratio": 0.5, "http_timeout": "30s"}}
        self.assertEqual(
            host_tools.host_tools_mise_toml(config, {}, {}),
            '[tools]\nnode = "22"\npython = ["3.13", "3.12"]\n\n[settings]\nexperimental = true\njobs = 4\nratio = 0.5\nhttp_timeout = "30s"\n',
        )

    def test_the_tables_come_out_in_one_order_whatever_the_mapping_has(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"settings": {"experimental": False}, "tools": {"node": "lts"}}, {}, {}), '[tools]\nnode = "lts"\n\n[settings]\nexperimental = false\n')

    def test_a_key_that_is_not_bare_is_quoted(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"tools": {"npm:typescript": "latest", "cargo:ripgrep": ["14"]}}, {}, {}), '[tools]\n"npm:typescript" = "latest"\n"cargo:ripgrep" = ["14"]\n')

    def test_strings_are_escaped(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"settings": {"note": 'say "hi"\\\n\t\x01'}}, {}, {}), '[settings]\nnote = "say \\"hi\\"\\\\\\n\\t\\u0001"\n')

    def test_a_setting_can_be_a_list_of_scalars(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"settings": {"disable_tools": ["node", "python"], "levels": [1, 2]}}, {}, {}), '[settings]\ndisable_tools = ["node", "python"]\nlevels = [1, 2]\n')

    def test_an_empty_table_is_left_out(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"tools": {}, "settings": {"experimental": True}}, {}, {}), "[settings]\nexperimental = true\n")

    def test_a_boolean_is_not_written_as_a_number(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"settings": {"on": True, "one": 1}}, {}, {}), "[settings]\non = true\none = 1\n")

    def test_what_it_cannot_render_is_refused(self) -> None:
        cases = {
            "a table the role does not render": ({"plugins": {"A": "b"}}, "renders only tools, settings, env"),
            "nothing to render": ({"tools": {}}, "nothing to render"),
            "not a mapping": (["node"], "must be a mapping"),
            "a tool given a number": ({"tools": {"node": 22}}, "tools.node"),
            "a tool given a list of numbers": ({"tools": {"node": [22]}}, "tools.node"),
            "a setting given a mapping": ({"settings": {"a": {"b": 1}}}, "settings.a"),
            "a setting that is not a finite number": ({"settings": {"a": float("inf")}}, "finite"),
            "a table that is not a mapping": ({"tools": ["node"]}, "tools must be a mapping"),
            "an empty key": ({"tools": {"": "1"}}, "non-empty"),
        }
        for label, (config, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(Exception, message):
                host_tools.host_tools_mise_toml(config, {}, {})


class MiseEnvFilterTest(unittest.TestCase):
    def read_back(self, env: dict[str, object], home_relative: dict[str, str], home: str = "home") -> tuple[dict[str, str], str]:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        return mise_env(host_tools.host_tools_mise_toml({"env": env}, home_relative, {}), root / home)

    def test_the_env_table_is_written_last_with_plain_values_then_home_relative_paths(self) -> None:
        self.assertEqual(
            host_tools.host_tools_mise_toml({"env": {"B": "x", "A": True, "N": 3, "F": 0.5}, "tools": {"node": "22"}}, {"TURBO_CACHE_DIR": ".cache/turbo"}, {}),
            '[tools]\nnode = "22"\n\n[env]\nB = "x"\nA = "true"\nN = "3"\nF = "0.5"\nTURBO_CACHE_DIR = "{{ env.HOME }}/.cache/turbo"\n',
        )

    def test_home_relative_paths_alone_are_enough_to_render(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({}, {"TURBO_CACHE_DIR": ".cache/turbo"}, {}), '[env]\nTURBO_CACHE_DIR = "{{ env.HOME }}/.cache/turbo"\n')

    def test_an_empty_env_table_is_left_out(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"env": {}, "tools": {"node": "22"}}, {}, {}), '[tools]\nnode = "22"\n')

    def test_a_dollar_sign_and_a_template_brace_are_escaped_for_mise_and_the_string_for_toml(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"env": {"A": 'a$b "c" {{d}} {%e%} {#f#} {g}'}}, {}, {}), "[env]\nA = \"a$$b \\\"c\\\" {{ '{' }}{d}} {{ '{' }}%e%} {{ '{' }}#f#} {g}\"\n")

    def test_a_home_relative_variable_is_expanded_by_mise_to_the_home_directory_it_reads_the_file_in(self) -> None:
        for home in ("home", "a home", "it's", 'say "hi"', "$d"):
            with self.subTest(home=home):
                exported, _ = self.read_back({}, {"TURBO_CACHE_DIR": ".cache/turbo"}, home)
                self.assertTrue(exported["TURBO_CACHE_DIR"].endswith(f"/{home}/.cache/turbo"), exported)

    def test_a_value_reaches_the_variable_exactly_as_given_whatever_it_holds(self) -> None:
        values = ["$HOME", "${HOME}", "$$", "a$", "$1", "${X:-y}", "$(id)", "`id`", "~", "~/x", "{{ env.HOME }}", "{{", "{%", "{#", "{", "}}", "{{x}}", "{% raw %}", "{% endraw %}", "{{ '{' }}", "a{b}c", "{ {", "$ {", "${{x}}", "it's", 'say "hi"', "back\\slash", "tab\there", "a b  c", "é", "", "-n", "true", "3", "*"]
        exported, stderr = self.read_back({f"V{index}": value for index, value in enumerate(values)}, {})
        for index, value in enumerate(values):
            with self.subTest(value=value):
                self.assertEqual(exported[f"V{index}"], value)
        self.assertNotIn("not defined", stderr)

    def test_a_number_and_a_boolean_are_exported_as_their_text(self) -> None:
        exported, _ = self.read_back({"A": True, "B": False, "C": 3, "D": 0.5}, {})
        self.assertEqual({key: exported[key] for key in "ABCD"}, {"A": "true", "B": "false", "C": "3", "D": "0.5"})

    def test_a_path_reaches_the_variable_exactly_as_given_after_the_home_directory(self) -> None:
        paths = [".cache/turbo", "a b  c", "it's", 'say "hi"', "$HOME", "${HOME}", "`id`", "$(id)", "back\\slash", "\\$", "tab\there", "*", "; echo injected", "-n", "~", "~/x", "a..b", "..x", "x..", "dir/", "./x", "é", "{{ env.HOME }}", "{{x}}", "{%x%}", "{#x#}", "{", "{{", "}}", "{% endraw %}", "$$", "a$"]
        exported, stderr = self.read_back({}, {f"P{index}": path for index, path in enumerate(paths)}, "the home")
        for index, path in enumerate(paths):
            with self.subTest(path=path):
                self.assertTrue(exported[f"P{index}"].endswith(f"/the home/{path}"), exported[f"P{index}"])
                self.assertEqual(exported[f"P{index}"].count("/the home/"), 1)
        self.assertNotIn("not defined", stderr)

    def test_what_mise_would_not_take_as_a_variable_is_refused(self) -> None:
        cases = {
            "a name with a hyphen": ({"env": {"A-B": "x"}}, {}, "not a name a shell accepts"),
            "a name starting with a digit": ({"env": {"1A": "x"}}, {}, "not a name a shell accepts"),
            "a name that is not a string": ({"env": {1: "x"}}, {}, "not a name a shell accepts"),
            "a home-relative name with a hyphen": ({}, {"A-B": "x"}, "not a name a shell accepts"),
            "a list value": ({"env": {"A": ["a"]}}, {}, "must be a string, a number or a boolean"),
            "a null value": ({"env": {"A": None}}, {}, "must be a string, a number or a boolean"),
            "a newline": ({"env": {"A": "a\nb"}}, {}, "one line"),
            "a NUL": ({"env": {"A": "a\x00b"}}, {}, "one line"),
            "an env table that is not a mapping": ({"env": ["A"]}, {}, "env must be a mapping"),
            "a home-relative mapping that is not a mapping": ({}, [".cache"], "home-relative env must be a mapping"),
            "an absolute path": ({}, {"A": "/var/cache"}, "relative to the user's home"),
            "an empty path": ({}, {"A": ""}, "must not be empty"),
            "a parent component": ({}, {"A": "a/../b"}, "'..' component"),
            "only a parent component": ({}, {"A": ".."}, "'..' component"),
            "a number as a path": ({}, {"A": 3}, "must be a string"),
            "a newline in a path": ({}, {"A": "a\nb"}, "one line"),
            "a name in both": ({"env": {"A": "x"}}, {"A": ".cache"}, "A is given more than once"),
        }
        for label, (config, home_relative, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(Exception, message):
                host_tools.host_tools_mise_toml(config, home_relative, {})

    def test_nothing_in_either_mapping_is_nothing_to_render(self) -> None:
        with self.assertRaisesRegex(Exception, "nothing to render"):
            host_tools.host_tools_mise_toml({"env": {}}, {}, {})


def grep_finds(pattern: str, text: str) -> bool:
    """Whether grep -E, which the role runs the pattern with, finds the pattern in a line."""
    return subprocess.run(["grep", "-E", "-q", "-e", pattern], input=text + "\n", capture_output=True, text=True, check=False).returncode == 0


class ShellEnvFilterTest(unittest.TestCase):
    def test_a_variable_becomes_a_quoted_export_line_and_a_pattern_that_finds_an_assignment_of_it(self) -> None:
        [item] = host_tools.host_tools_shell_env({"UV_PYTHON_DOWNLOADS": "never"}, {}, {})
        self.assertEqual(item, {"name": "env-UV_PYTHON_DOWNLOADS", "pattern": "^[[:space:]]*(export[[:space:]]+)?UV_PYTHON_DOWNLOADS=", "line": "export UV_PYTHON_DOWNLOADS=never", "non_interactive": True})

    def test_the_variables_come_out_in_the_order_of_the_mapping(self) -> None:
        self.assertEqual([item["name"] for item in host_tools.host_tools_shell_env({"B": "1", "A": "2"}, {}, {})], ["env-B", "env-A"])

    def test_an_empty_mapping_has_no_lines(self) -> None:
        self.assertEqual(host_tools.host_tools_shell_env({}, {}, {}), [])

    def test_a_value_reaches_the_variable_exactly_as_given_whatever_it_holds(self) -> None:
        values = ["it's", 'say "hi"', "$HOME", "${HOME}", "`id`", "$(id)", "a b  c", "back\\slash", "tab\there", "*", "; echo injected", "", "-n", "'", "''", "\"'\"", "é"]
        for value in values:
            with self.subTest(value=value):
                [item] = host_tools.host_tools_shell_env({"EXAMPLE": value}, {}, {})
                for shell in ("sh", "bash"):
                    result = subprocess.run([shell, "-c", item["line"] + '\nprintf %s "$EXAMPLE"'], capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent-home"})
                    self.assertEqual(result.stdout, value)
                    self.assertEqual(result.stderr, "")

    def test_a_dollar_sign_and_a_single_quote_are_quoted_and_not_expanded(self) -> None:
        self.assertEqual(host_tools.host_tools_shell_env({"A": "$HOME"}, {}, {})[0]["line"], "export A='$HOME'")
        self.assertEqual(host_tools.host_tools_shell_env({"A": "it's"}, {}, {})[0]["line"], "export A='it'\"'\"'s'")

    def test_a_boolean_is_true_or_false_and_a_number_is_its_digits(self) -> None:
        lines = {item["name"]: item["line"] for item in host_tools.host_tools_shell_env({"A": True, "B": False, "C": 3, "D": 0.5}, {}, {})}
        self.assertEqual(lines, {"env-A": "export A=true", "env-B": "export B=false", "env-C": "export C=3", "env-D": "export D=0.5"})

    def test_the_pattern_finds_an_assignment_with_or_without_export_and_not_a_comment_or_another_variable(self) -> None:
        [item] = host_tools.host_tools_shell_env({"UV_PYTHON_DOWNLOADS": "never"}, {}, {})
        for text in ("UV_PYTHON_DOWNLOADS=automatic", "export UV_PYTHON_DOWNLOADS=automatic", "  export   UV_PYTHON_DOWNLOADS='x'", "\texport UV_PYTHON_DOWNLOADS="):
            with self.subTest(text=text):
                self.assertTrue(grep_finds(item["pattern"], text))
        for text in ("# export UV_PYTHON_DOWNLOADS=never", "export OTHER_UV_PYTHON_DOWNLOADS=never", "echo UV_PYTHON_DOWNLOADS=never", "UV_PYTHON_DOWNLOADS_MORE=never"):
            with self.subTest(text=text):
                self.assertFalse(grep_finds(item["pattern"], text))

    def test_what_a_shell_would_not_take_is_refused(self) -> None:
        cases = {
            "not a mapping": (["A"], "must be a mapping"),
            "name with a hyphen": ({"A-B": "1"}, "not a name a shell accepts"),
            "name starting with a digit": ({"1A": "1"}, "not a name a shell accepts"),
            "empty name": ({"": "1"}, "not a name a shell accepts"),
            "name with a space": ({"A B": "1"}, "not a name a shell accepts"),
            "name that is not a string": ({1: "1"}, "not a name a shell accepts"),
            "value with a newline": ({"A": "a\nb"}, "one line"),
            "value with a NUL": ({"A": "a\x00b"}, "one line"),
            "list value": ({"A": ["a"]}, "must be a string, a number or a boolean"),
            "null value": ({"A": None}, "must be a string, a number or a boolean"),
        }
        for label, (env, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(Exception, message):
                host_tools.host_tools_shell_env(env, {}, {})


def shell_reads(shell: str, line: str, home: str) -> str:
    """The value of EXAMPLE after a real shell reads a startup line, with the given home directory."""
    result = subprocess.run([shell, "-c", line + '\nprintf %s "$EXAMPLE"'], capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": home})
    assert result.stderr == "", result.stderr
    return result.stdout


class HomeRelativeFilterTest(unittest.TestCase):
    def test_a_path_becomes_an_export_of_the_home_directory_and_the_path_in_double_quotes(self) -> None:
        [item] = host_tools.host_tools_shell_env({}, {"TURBO_CACHE_DIR": ".cache/turbo"}, {})
        self.assertEqual(item, {"name": "env-TURBO_CACHE_DIR", "pattern": "^[[:space:]]*(export[[:space:]]+)?TURBO_CACHE_DIR=", "line": 'export TURBO_CACHE_DIR="$HOME/.cache/turbo"', "non_interactive": True})

    def test_the_variables_of_both_mappings_come_out_the_plain_ones_first_each_in_its_own_order(self) -> None:
        items = host_tools.host_tools_shell_env({"B": "1", "A": "2"}, {"D": "d", "C": "c"}, {})
        self.assertEqual([item["name"] for item in items], ["env-B", "env-A", "env-D", "env-C"])

    def test_a_path_reaches_the_variable_as_given_in_every_shell_whatever_it_holds_and_the_home_is_expanded_once(self) -> None:
        paths = [".cache/turbo", "a b  c", "it's", 'say "hi"', "$HOME", "${HOME}", "`id`", "$(id)", "back\\slash", "\\$", "tab\there", "*", "?", "[a]", "; echo injected", "-n", "~", "~/x", "!x", "a..b", "..x", "x..", ".hidden/.also", "dir/", "./x", "é", "'", "\""]
        homes = ["/home/example", "/home/a b", "/home/it's", '/home/"q"', "/home/$d"]
        for path in paths:
            [item] = host_tools.host_tools_shell_env({}, {"EXAMPLE": path}, {})
            for home in homes:
                for shell in ("sh", "bash", "zsh"):
                    with self.subTest(path=path, home=home, shell=shell):
                        self.assertEqual(shell_reads(shell, item["line"], home), f"{home}/{path}")

    def test_the_special_characters_are_escaped_and_nothing_else_is(self) -> None:
        [item] = host_tools.host_tools_shell_env({}, {"A": 'x\\y$z`w"v !*~'}, {})
        self.assertEqual(item["line"], 'export A="$HOME/x\\\\y\\$z\\`w\\"v !*~"')

    def test_the_pattern_finds_an_assignment_of_the_variable_already_in_a_startup_file(self) -> None:
        [item] = host_tools.host_tools_shell_env({}, {"TURBO_CACHE_DIR": ".cache/turbo"}, {})
        self.assertTrue(grep_finds(item["pattern"], 'export TURBO_CACHE_DIR="$HOME/.cache/turbo"'))
        self.assertTrue(grep_finds(item["pattern"], "TURBO_CACHE_DIR=/elsewhere"))
        self.assertFalse(grep_finds(item["pattern"], "# export TURBO_CACHE_DIR=/elsewhere"))

    def test_an_empty_mapping_has_no_lines(self) -> None:
        self.assertEqual(host_tools.host_tools_shell_env({}, {}, {}), [])

    def test_what_is_not_a_path_below_the_home_directory_is_refused(self) -> None:
        cases = {
            "absolute": ({"A": "/var/cache"}, "relative to the user's home"),
            "the root": ({"A": "/"}, "relative to the user's home"),
            "empty": ({"A": ""}, "must not be empty"),
            "a parent component": ({"A": "../x"}, "'..' component"),
            "a parent component in the middle": ({"A": "a/../b"}, "'..' component"),
            "a parent component at the end": ({"A": "a/.."}, "'..' component"),
            "only a parent component": ({"A": ".."}, "'..' component"),
            "a newline": ({"A": "a\nb"}, "one line"),
            "a carriage return": ({"A": "a\rb"}, "one line"),
            "a NUL": ({"A": "a\x00b"}, "one line"),
            "a number": ({"A": 3}, "must be a string"),
            "a boolean": ({"A": True}, "must be a string"),
            "a list": ({"A": ["a"]}, "must be a string"),
            "a null": ({"A": None}, "must be a string"),
            "a name with a hyphen": ({"A-B": "x"}, "not a name a shell accepts"),
            "a name that is not a string": ({1: "x"}, "not a name a shell accepts"),
        }
        for label, (home_relative, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(Exception, message):
                host_tools.host_tools_shell_env({}, home_relative, {})

    def test_a_mapping_that_is_not_one_is_refused(self) -> None:
        with self.assertRaisesRegex(Exception, "home-relative shell environment must be a mapping"):
            host_tools.host_tools_shell_env({}, [".cache"], {})

    def test_a_variable_in_both_mappings_is_refused_and_named(self) -> None:
        with self.assertRaisesRegex(Exception, "A is given more than once"):
            host_tools.host_tools_shell_env({"A": "x"}, {"A": "y"}, {})


# The CPU counts the share tests pretend a host has, and the divisors they divide by. The counts include 1, which a divisor above 1 would round down to 0, the exact multiples and the counts one below them.
CPU_COUNTS = (1, 2, 3, 4, 5, 10, 11, 12, 63, 64)
CPU_DIVISORS = (1, 2, 3, 4, 8, 100)


def expected_share(cpus: int, divisor: int) -> int:
    return max(1, cpus // divisor)


def host_cpus() -> int:
    """The count mise's num_cpus() and getconf report on this host: the CPUs the process may run on where the OS says, else all of them."""
    return len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1


class CpuShareFilterTest(unittest.TestCase):
    def stand_in_getconf(self, cpus: int) -> Path:
        """A directory holding a getconf that reports ``cpus`` for _NPROCESSORS_ONLN and refuses anything else, so the count is the test's and not the host's."""
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        stand_in = directory / "getconf"
        stand_in.write_text(f'#!/bin/sh\n[ "$1" = _NPROCESSORS_ONLN ] || {{ echo "unexpected getconf $*" >&2; exit 2; }}\necho {cpus}\n')
        stand_in.chmod(0o755)
        return directory

    def read(self, shell: str, line: str, path: str, extra: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run([shell, "-c", f'{extra}{line}\nprintf %s "$V"'], capture_output=True, text=True, check=True, env={"PATH": path, "HOME": "/nonexistent-home"})

    def test_a_share_is_one_export_line_that_counts_the_cpus_and_divides_in_a_subshell(self) -> None:
        [item] = host_tools.host_tools_shell_env({}, {}, {"V": 2})
        self.assertEqual(item, {"name": "env-V", "pattern": "^[[:space:]]*(export[[:space:]]+)?V=", "line": 'export V="$(n=$(getconf _NPROCESSORS_ONLN); n=$((n / 2)); echo $((n > 0 ? n : 1)))"', "non_interactive": True})

    def test_the_value_is_the_cpu_count_divided_rounded_down_and_never_below_one_in_every_shell(self) -> None:
        for cpus in CPU_COUNTS:
            path = f"{self.stand_in_getconf(cpus)}:/usr/bin:/bin"
            for divisor in CPU_DIVISORS:
                [item] = host_tools.host_tools_shell_env({}, {}, {"V": divisor})
                for shell in ("sh", "bash", "zsh"):
                    with self.subTest(cpus=cpus, divisor=divisor, shell=shell):
                        result = self.read(shell, str(item["line"]), path)
                        self.assertEqual(result.stdout, str(expected_share(cpus, divisor)))
                        self.assertEqual(result.stderr, "")

    def test_the_real_getconf_gives_the_hosts_own_count(self) -> None:
        for divisor in CPU_DIVISORS:
            [item] = host_tools.host_tools_shell_env({}, {}, {"V": divisor})
            for shell in ("sh", "bash", "zsh"):
                with self.subTest(divisor=divisor, shell=shell):
                    self.assertEqual(self.read(shell, str(item["line"]), "/usr/bin:/bin").stdout, str(max(1, host_cpus() // divisor)))

    def test_no_variable_of_the_arithmetic_is_left_in_the_users_shell_and_one_they_have_is_kept(self) -> None:
        [item] = host_tools.host_tools_shell_env({}, {}, {"V": 2})
        path = f"{self.stand_in_getconf(10)}:/usr/bin:/bin"
        for shell in ("sh", "bash", "zsh"):
            with self.subTest(shell=shell):
                result = subprocess.run([shell, "-c", f'n=keep\n{item["line"]}\nprintf "%s|%s" "$V" "$n"; unset n\n{item["line"]}\nprintf "|%s" "${{n-unset}}"'], capture_output=True, text=True, check=True, env={"PATH": path, "HOME": "/nonexistent-home"})
                self.assertEqual(result.stdout, "5|keep|unset")

    def test_the_value_is_exported_to_the_commands_the_shell_runs(self) -> None:
        [item] = host_tools.host_tools_shell_env({}, {}, {"VITEST_MAX_WORKERS": 2})
        path = f"{self.stand_in_getconf(10)}:/usr/bin:/bin"
        for shell in ("bash", "zsh"):
            with self.subTest(shell=shell):
                result = subprocess.run([shell, "-c", f'{item["line"]}\nenv'], capture_output=True, text=True, check=True, env={"PATH": path, "HOME": "/nonexistent-home"})
                self.assertIn("VITEST_MAX_WORKERS=5\n", result.stdout)

    def test_the_pattern_finds_an_assignment_of_the_variable_already_in_a_startup_file(self) -> None:
        [item] = host_tools.host_tools_shell_env({}, {}, {"VITEST_MAX_WORKERS": 2})
        self.assertTrue(grep_finds(str(item["pattern"]), "export VITEST_MAX_WORKERS=4"))
        self.assertFalse(grep_finds(str(item["pattern"]), "# export VITEST_MAX_WORKERS=4"))

    def test_the_variables_come_out_plain_then_home_relative_then_shares(self) -> None:
        items = host_tools.host_tools_shell_env({"B": "x"}, {"D": "d"}, {"F": 2, "E": 3})
        self.assertEqual([item["name"] for item in items], ["env-B", "env-D", "env-F", "env-E"])

    def test_the_mise_share_is_a_template_in_the_env_table(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({}, {}, {"V": 2}), '[env]\nV = "{% set c = num_cpus() | int %}{% set n = ((c - c % 2) / 2) | int %}{% if n < 1 %}1{% else %}{{ n }}{% endif %}"\n')

    def test_the_mise_share_is_the_cpu_count_divided_rounded_down_and_never_below_one_for_counts_the_host_does_not_have(self) -> None:
        names = {divisor: f"D{divisor}" for divisor in CPU_DIVISORS}
        rendered = host_tools.host_tools_mise_toml({}, {}, {name: divisor for divisor, name in names.items()})
        self.assertEqual(rendered.count("num_cpus()"), len(names))
        for cpus in CPU_COUNTS:
            root = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, root)
            # num_cpus() returns a string, so a single-quoted Tera string (the template sits in a double-quoted TOML string) stands in for the host's count.
            exported, stderr = mise_env(rendered.replace("num_cpus()", f"'{cpus}'"), root / "home")
            with self.subTest(cpus=cpus):
                self.assertEqual({divisor: exported[name] for divisor, name in names.items()}, {divisor: str(expected_share(cpus, divisor)) for divisor in names})
                self.assertEqual(stderr, "")

    def test_the_mise_share_with_the_real_num_cpus_gives_the_hosts_own_count(self) -> None:
        names = {divisor: f"D{divisor}" for divisor in CPU_DIVISORS}
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        exported, _ = mise_env(host_tools.host_tools_mise_toml({}, {}, {name: divisor for divisor, name in names.items()}), root / "home")
        self.assertEqual({divisor: exported[name] for divisor, name in names.items()}, {divisor: str(max(1, host_cpus() // divisor)) for divisor in names})

    def test_the_shell_and_mise_agree_for_every_count_and_divisor(self) -> None:
        for cpus in (1, 3, 10, 64):
            path = f"{self.stand_in_getconf(cpus)}:/usr/bin:/bin"
            root = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, root)
            rendered = host_tools.host_tools_mise_toml({}, {}, {f"D{divisor}": divisor for divisor in CPU_DIVISORS})
            exported, _ = mise_env(rendered.replace("num_cpus()", f"'{cpus}'"), root / "home")
            for divisor in CPU_DIVISORS:
                [item] = host_tools.host_tools_shell_env({}, {}, {"V": divisor})
                with self.subTest(cpus=cpus, divisor=divisor):
                    self.assertEqual(self.read("bash", str(item["line"]), path).stdout, exported[f"D{divisor}"])

    def test_a_divisor_that_is_not_an_integer_of_at_least_one_is_refused(self) -> None:
        cases = {"zero": (0, "at least 1"), "negative": (-2, "at least 1"), "a float": (2.0, "must be an integer"), "half": (0.5, "must be an integer"), "a string": ("2", "must be an integer"), "a percentage": ("50%", "must be an integer"), "true": (True, "must be an integer"), "false": (False, "must be an integer"), "null": (None, "must be an integer"), "a list": ([2], "must be an integer")}
        for label, (divisor, message) in cases.items():
            with self.subTest(label, target="shell"), self.assertRaisesRegex(Exception, message):
                host_tools.host_tools_shell_env({}, {}, {"V": divisor})
            with self.subTest(label, target="mise"), self.assertRaisesRegex(Exception, message):
                host_tools.host_tools_mise_toml({}, {}, {"V": divisor})

    def test_a_name_a_shell_would_not_accept_is_refused(self) -> None:
        with self.assertRaisesRegex(Exception, "not a name a shell accepts"):
            host_tools.host_tools_shell_env({}, {}, {"A-B": 2})
        with self.assertRaisesRegex(Exception, "not a name a shell accepts"):
            host_tools.host_tools_mise_toml({}, {}, {"A-B": 2})

    def test_a_mapping_that_is_not_one_is_refused(self) -> None:
        with self.assertRaisesRegex(Exception, "CPU-share shell environment must be a mapping"):
            host_tools.host_tools_shell_env({}, {}, [2])
        with self.assertRaisesRegex(Exception, "CPU-share env must be a mapping"):
            host_tools.host_tools_mise_toml({}, {}, [2])

    def test_a_variable_given_in_two_mappings_is_refused_and_named_in_every_pairing(self) -> None:
        pairings = {"plain and share": (({"A": "x"}, {}, {"A": 2})), "path and share": (({}, {"A": "x"}, {"A": 2})), "plain and path": (({"A": "x"}, {"A": "y"}, {}))}
        for label, (env, home_relative, cpu_share) in pairings.items():
            with self.subTest(label, target="shell"), self.assertRaisesRegex(Exception, "A is given more than once"):
                host_tools.host_tools_shell_env(env, home_relative, cpu_share)
            with self.subTest(label, target="mise"), self.assertRaisesRegex(Exception, "A is given more than once"):
                host_tools.host_tools_mise_toml({"env": env}, home_relative, cpu_share)


STEM = "# {mark} ANSIBLE MANAGED BLOCK example "
OWN = "# BEGIN ANSIBLE MANAGED BLOCK example env-A\nexport A=1\n# END ANSIBLE MANAGED BLOCK example env-A\n"
OTHER = OWN.replace("env-A", "env-B").replace("A=1", "B=2")


class BlockLeadsFilterTest(unittest.TestCase):
    def leads(self, text: str) -> bool:
        return bool(host_tools.host_tools_block_leads(text, STEM + "env-A", STEM))

    def test_a_block_at_the_start_of_the_file_leads(self) -> None:
        self.assertTrue(self.leads(OWN + "export X=1\n"))

    def test_blank_lines_and_comments_above_the_block_do_not_matter(self) -> None:
        self.assertTrue(self.leads("#!/bin/bash\n\n  # a note\n" + OWN))

    def test_other_blocks_of_the_role_above_it_do_not_matter_whatever_they_hold(self) -> None:
        self.assertTrue(self.leads(OTHER + OWN))
        self.assertTrue(self.leads(OTHER.replace("export B=2", "[ -z \"$PS1\" ] && return") + OWN))

    def test_any_code_above_the_block_means_it_does_not_lead(self) -> None:
        for code in ("export X=1\n", "case $- in\n    *i*) ;;\n      *) return;;\nesac\n", "[ -z \"$PS1\" ] && return\n", "  return\n"):
            with self.subTest(code=code):
                self.assertFalse(self.leads("# note\n" + code + OWN))
                self.assertFalse(self.leads(OTHER + code + OWN))

    def test_the_real_ubuntu_skeleton_with_the_block_appended_does_not_lead_and_with_it_prepended_does(self) -> None:
        skeleton = (ROOT / "tests" / "host_tools" / "ubuntu-26.04-skel.bashrc").read_text()
        self.assertFalse(self.leads(skeleton + OWN))
        self.assertTrue(self.leads(OWN + skeleton))

    def test_blocks_below_it_do_not_matter(self) -> None:
        self.assertTrue(self.leads(OTHER + OWN + OTHER.replace("B", "C")))

    def test_the_block_is_matched_by_its_whole_marker_line(self) -> None:
        text = OWN.replace("env-A", "env-AB")
        with self.assertRaisesRegex(Exception, "no block"):
            host_tools.host_tools_block_leads(text, STEM + "env-A", STEM)

    def test_a_file_without_the_block_is_refused(self) -> None:
        with self.assertRaisesRegex(Exception, "no block"):
            host_tools.host_tools_block_leads("# note\n", STEM + "env-A", STEM)

    def test_what_is_not_text_is_refused(self) -> None:
        with self.assertRaisesRegex(Exception, "must be strings"):
            host_tools.host_tools_block_leads(None, STEM + "env-A", STEM)


MARKER = "# {mark} ANSIBLE MANAGED BLOCK example"


class NpmrcFilterTest(unittest.TestCase):
    def test_settings_then_exclusions_come_out_as_lines_in_the_order_given(self) -> None:
        result = host_tools.host_tools_npmrc({"min-release-age": 7, "ignore-scripts": True, "fund": False}, ["example-a", "@example/*"], "", MARKER)
        self.assertEqual(result, {"lines": ["min-release-age=7", "ignore-scripts=true", "fund=false", "min-release-age-exclude[]=example-a", "min-release-age-exclude[]=@example/*"], "shadowed": []})

    def test_a_string_a_float_and_a_value_with_a_variable_reference_are_written_as_given(self) -> None:
        result = host_tools.host_tools_npmrc({"registry": "https://registry.example.test/", "ratio": 0.5, "cache": "${HOME}/.npm-cache"}, [], "", MARKER)
        self.assertEqual(result["lines"], ["registry=https://registry.example.test/", "ratio=0.5", "cache=${HOME}/.npm-cache"])

    def test_nothing_set_has_no_lines(self) -> None:
        self.assertEqual(host_tools.host_tools_npmrc({}, [], "registry=x\n", MARKER), {"lines": [], "shadowed": []})

    def test_a_key_the_file_sets_outside_the_block_is_left_out_and_named(self) -> None:
        existing = "registry=https://registry.example.test/\nmin-release-age=30\n"
        result = host_tools.host_tools_npmrc({"min-release-age": 7, "ignore-scripts": True}, [], existing, MARKER)
        self.assertEqual(result, {"lines": ["ignore-scripts=true"], "shadowed": ["min-release-age"]})

    def test_the_users_key_is_found_with_spaces_around_the_equals_sign_indented_and_with_no_value(self) -> None:
        for line in ("min-release-age = 30", "  min-release-age=30", "min-release-age", "min-release-age ="):
            with self.subTest(line=line):
                self.assertEqual(host_tools.host_tools_npmrc({"min-release-age": 7}, [], line + "\n", MARKER)["shadowed"], ["min-release-age"])

    def test_a_comment_or_another_key_with_the_same_beginning_is_not_the_users_key(self) -> None:
        existing = "# min-release-age=30\n; min-release-age=30\nmin-release-age-exclude[]=a\nmin-release-agent=1\nfoo=min-release-age=3\n"
        result = host_tools.host_tools_npmrc({"min-release-age": 7}, [], existing, MARKER)
        self.assertEqual(result, {"lines": ["min-release-age=7"], "shadowed": []})

    def test_the_roles_own_block_is_not_the_users_key_so_a_changed_value_is_rewritten(self) -> None:
        existing = "registry=x\n# BEGIN ANSIBLE MANAGED BLOCK example\nmin-release-age=3\nmin-release-age-exclude[]=a\n# END ANSIBLE MANAGED BLOCK example\n"
        result = host_tools.host_tools_npmrc({"min-release-age": 7}, ["a"], existing, MARKER)
        self.assertEqual(result, {"lines": ["min-release-age=7", "min-release-age-exclude[]=a"], "shadowed": []})

    def test_a_key_outside_the_block_is_found_after_it_as_well(self) -> None:
        existing = "# BEGIN ANSIBLE MANAGED BLOCK example\nfund=false\n# END ANSIBLE MANAGED BLOCK example\nfund=true\n"
        self.assertEqual(host_tools.host_tools_npmrc({"fund": False}, [], existing, MARKER)["shadowed"], ["fund"])

    def test_a_begin_marker_with_no_end_is_not_a_block(self) -> None:
        existing = "# BEGIN ANSIBLE MANAGED BLOCK example\nfund=false\n"
        self.assertEqual(host_tools.host_tools_npmrc({"fund": False}, [], existing, MARKER)["shadowed"], ["fund"])

    def test_an_exclusion_the_file_lists_outside_the_block_is_not_written_again(self) -> None:
        existing = "min-release-age-exclude[]=example-a\nmin-release-age-exclude[] = \"@example/b\"\n"
        result = host_tools.host_tools_npmrc({}, ["example-a", "@example/b", "example-c"], existing, MARKER)
        self.assertEqual(result, {"lines": ["min-release-age-exclude[]=example-c"], "shadowed": []})

    def test_an_exclusion_given_twice_is_written_once(self) -> None:
        self.assertEqual(host_tools.host_tools_npmrc({}, ["a", "a"], "", MARKER)["lines"], ["min-release-age-exclude[]=a"])

    def test_other_lines_of_the_file_never_reach_the_result_or_an_error(self) -> None:
        secret = "//registry.example.test/:_authToken=made-up-token-0000"
        result = host_tools.host_tools_npmrc({"fund": False}, ["a"], secret + "\n", MARKER)
        self.assertNotIn("made-up-token-0000", repr(result))
        with self.assertRaises(Exception) as caught:
            host_tools.host_tools_npmrc({"fund": "a;b"}, [], secret + "\n", MARKER)
        self.assertNotIn("made-up-token-0000", str(caught.exception))

    def test_what_an_npmrc_would_not_read_back_as_given_is_refused(self) -> None:
        cases = {
            "settings not a mapping": ([], [], "must be a mapping"),
            "exclusions not a list": ({}, {"a": 1}, "must be a list"),
            "key with a space": ({"a b": 1}, [], "not a key"),
            "empty key": ({"": 1}, [], "not a key"),
            "key that is a list": ({"min-release-age-exclude": "a"}, [], "variable of its own"),
            "key with brackets": ({"a[]": 1}, [], "not a key"),
            "value with a comment character": ({"a": "x # y"}, [], "would not read it back"),
            "value with a semicolon": ({"a": "x;y"}, [], "would not read it back"),
            "value with a quote": ({"a": "'x'"}, [], "would not read it back"),
            "value with a backslash": ({"a": "x\\y"}, [], "would not read it back"),
            "value with a newline": ({"a": "x\ny"}, [], "would not read it back"),
            "value with surrounding space": ({"a": " x"}, [], "would not read it back"),
            "list value": ({"a": ["x"]}, [], "must be a string, a number or a boolean"),
            "null value": ({"a": None}, [], "must be a string, a number or a boolean"),
            "infinite value": ({"a": float("inf")}, [], "finite"),
            "exclusion with a space": ({}, ["a b"], "not a package name"),
            "exclusion that is not a string": ({}, [1], "not a package name"),
            "exclusion that would end the line early": ({}, ["a\nb"], "not a package name"),
        }
        for label, (settings, excludes, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(Exception, message):
                host_tools.host_tools_npmrc(settings, excludes, "", MARKER)


class YamlMergeFilterTest(unittest.TestCase):
    def merge(self, existing: str, settings: dict) -> str:
        return host_tools.host_tools_yaml_merge(existing, settings)

    def test_a_new_file_is_the_settings_alone_with_lists_indented(self) -> None:
        self.assertEqual(self.merge("", {"minimumReleaseAge": 10080, "saveExact": True, "minimumReleaseAgeExclude": ["a", "@b/c"]}), "minimumReleaseAge: 10080\nsaveExact: true\nminimumReleaseAgeExclude:\n  - a\n  - '@b/c'\n")

    def test_a_nested_mapping_is_rendered_and_read_back(self) -> None:
        result = self.merge("", {"catalog": {"react": "^19.0.0"}})
        self.assertEqual(result, "catalog:\n  react: ^19.0.0\n")
        self.assertEqual(yaml.safe_load(result), {"catalog": {"react": "^19.0.0"}})

    def test_a_string_that_looks_like_another_type_stays_a_string(self) -> None:
        result = self.merge("", {"a": "7", "b": "true", "c": "null"})
        self.assertEqual(yaml.safe_load(result), {"a": "7", "b": "true", "c": "null"})

    def test_unrelated_keys_comments_and_blank_lines_are_kept_byte_for_byte(self) -> None:
        existing = "# pnpm settings\nstoreDir: /somewhere/store   # the store\n\n# the age\nminimumReleaseAge: 1440\n\n# registries\nregistry: https://registry.example.test/\n"
        self.assertEqual(self.merge(existing, {"minimumReleaseAge": 10080}), existing.replace("minimumReleaseAge: 1440", "minimumReleaseAge: 10080"))

    def test_a_key_that_holds_the_value_is_not_touched_and_the_text_comes_back_unchanged(self) -> None:
        existing = "minimumReleaseAge:   10080   # a week\nsaveExact: yes\nlist: [a,   b]\n"
        self.assertEqual(self.merge(existing, {"minimumReleaseAge": 10080}), existing)
        self.assertEqual(self.merge(existing, {"list": ["a", "b"]}), existing)

    def test_a_value_of_another_type_counts_as_different(self) -> None:
        self.assertEqual(self.merge("a: 1\n", {"a": True}), "a: true\n")
        self.assertEqual(self.merge("a: 1\n", {"a": 1.0}), "a: 1.0\n")
        self.assertEqual(self.merge("a: '1'\n", {"a": 1}), "a: 1\n")

    def test_a_list_is_replaced_with_its_items_whatever_layout_it_had(self) -> None:
        for before in ("a:\n- x\n- y\n", "a:\n  - x\n  - y\n", "a: [x, y]\n", "a:\n  - x\n  # note\n  - y\n"):
            with self.subTest(before=before):
                self.assertEqual(self.merge(before + "z: 1\n", {"a": ["p"]}), "a:\n  - p\nz: 1\n")

    def test_comments_before_the_next_key_stay_with_it(self) -> None:
        self.assertEqual(self.merge("a:\n- x\n\n# about b\nb: 1\n", {"a": ["p"]}), "a:\n  - p\n\n# about b\nb: 1\n")

    def test_a_key_that_is_missing_is_appended_after_the_last_line_even_with_no_final_newline(self) -> None:
        self.assertEqual(self.merge("a: 1", {"b": 2}), "a: 1\nb: 2\n")
        self.assertEqual(self.merge("a: 1\n# end\n", {"b": 2}), "a: 1\n# end\nb: 2\n")

    def test_a_quoted_key_is_found(self) -> None:
        self.assertEqual(self.merge('"a": 1\n\'b\': 2\n', {"a": 3, "b": 4}), "a: 3\nb: 4\n")

    def test_a_key_that_only_begins_like_another_is_not_it(self) -> None:
        self.assertEqual(self.merge("ab: 1\nabc: 2\n", {"a": 3}), "ab: 1\nabc: 2\na: 3\n")

    def test_a_key_that_is_the_start_of_a_nested_one_is_not_a_top_level_key(self) -> None:
        self.assertEqual(self.merge("outer:\n  a: 1\n", {"a": 3}), "outer:\n  a: 1\na: 3\n")

    def test_windows_line_endings_elsewhere_in_the_file_are_kept(self) -> None:
        self.assertEqual(self.merge("a: 1\r\nb: 2\r\n", {"a": 3}), "a: 3\nb: 2\r\n")

    def test_a_document_start_marker_is_kept(self) -> None:
        self.assertEqual(self.merge("---\na: 1\n", {"b": 2}), "---\na: 1\nb: 2\n")

    def test_an_empty_or_comment_only_file_takes_the_settings(self) -> None:
        self.assertEqual(self.merge("# nothing yet\n", {"a": 1}), "# nothing yet\na: 1\n")
        self.assertEqual(self.merge("{}\n", {}), "{}\n")

    def test_no_settings_return_the_text_unchanged(self) -> None:
        self.assertEqual(self.merge("a: 1\n", {}), "a: 1\n")

    def test_a_merge_twice_is_the_merge_once(self) -> None:
        once = self.merge("# keep\nb: 1\n", {"a": ["x", "y"], "c": {"d": 1}})
        self.assertEqual(self.merge(once, {"a": ["x", "y"], "c": {"d": 1}}), once)

    def test_ansible_style_subclasses_of_the_plain_types_are_accepted(self) -> None:
        class Text(str):
            pass

        class Mapping(dict):  # type: ignore[type-arg]
            pass

        self.assertEqual(self.merge("", Mapping({Text("a"): [Text("x")], "b": Mapping({"c": Text("d")})})), "a:\n  - x\nb:\n  c: d\n")

    def test_what_cannot_be_merged_is_refused_and_nothing_is_returned(self) -> None:
        cases = {
            "existing not text": (None, {"a": 1}, "must be text"),
            "settings not a mapping": ("", [1], "must be a mapping"),
            "not YAML": ("a: [1\n", {"b": 1}, "not valid YAML"),
            "a list at the top": ("- a\n- b\n", {"b": 1}, "not a mapping at the top"),
            "a scalar at the top": ("just text\n", {"b": 1}, "not a mapping at the top"),
            "a flow mapping at the top": ("{a: 1}\n", {"b": 2}, "cannot edit safely"),
            "several documents": ("a: 1\n---\nb: 2\n", {"c": 1}, "not valid YAML"),
            "a null value": ("", {"a": None}, "must be a string, a number"),
            "a non-string key": ("", {"a": {1: 2}}, "string keys"),
            "an infinite value": ("", {"a": float("inf")}, "finite"),
            "an object": ("", {"a": object()}, "must be a string, a number"),
        }
        for label, (existing, settings, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(Exception, message):
                host_tools.host_tools_yaml_merge(existing, settings)


if __name__ == "__main__":
    unittest.main()
