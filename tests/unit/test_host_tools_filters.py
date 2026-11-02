"""Tests the filters of the github_runner_host_tools role in plugins/filter/host_tools.py: the Homebrew prefix a host has, the shell startup lines that bring Homebrew and mise into a shell, and mise's configuration rendered to TOML against golden strings."""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

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
                {"name": "homebrew", "pattern": "^[^#]*brew shellenv", "line": '[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"'},
                {"name": "mise", "pattern": "^[^#]*mise activate", "line": '[ -x /opt/homebrew/bin/mise ] && eval "$(/opt/homebrew/bin/mise activate zsh)"'},
            ],
        )

    def test_mise_is_activated_for_the_shell_it_is_in(self) -> None:
        [mise] = host_tools.host_tools_shell_integrations("/usr/local", "bash", False, True)
        self.assertTrue(mise["line"].endswith('activate bash)"'))

    def test_a_tool_that_is_not_wanted_has_no_line(self) -> None:
        self.assertEqual([item["name"] for item in host_tools.host_tools_shell_integrations("/usr/local", "bash", True, False)], ["homebrew"])
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


class MiseTomlFilterTest(unittest.TestCase):
    def test_tools_and_settings_render_as_two_tables(self) -> None:
        config = {"tools": {"node": "22", "python": ["3.13", "3.12"]}, "settings": {"experimental": True, "jobs": 4, "ratio": 0.5, "http_timeout": "30s"}}
        self.assertEqual(
            host_tools.host_tools_mise_toml(config),
            '[tools]\nnode = "22"\npython = ["3.13", "3.12"]\n\n[settings]\nexperimental = true\njobs = 4\nratio = 0.5\nhttp_timeout = "30s"\n',
        )

    def test_the_tables_come_out_in_one_order_whatever_the_mapping_has(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"settings": {"experimental": False}, "tools": {"node": "lts"}}), '[tools]\nnode = "lts"\n\n[settings]\nexperimental = false\n')

    def test_a_key_that_is_not_bare_is_quoted(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"tools": {"npm:typescript": "latest", "cargo:ripgrep": ["14"]}}), '[tools]\n"npm:typescript" = "latest"\n"cargo:ripgrep" = ["14"]\n')

    def test_strings_are_escaped(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"settings": {"note": 'say "hi"\\\n\t\x01'}}), '[settings]\nnote = "say \\"hi\\"\\\\\\n\\t\\u0001"\n')

    def test_a_setting_can_be_a_list_of_scalars(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"settings": {"disable_tools": ["node", "python"], "levels": [1, 2]}}), '[settings]\ndisable_tools = ["node", "python"]\nlevels = [1, 2]\n')

    def test_an_empty_table_is_left_out(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"tools": {}, "settings": {"experimental": True}}), "[settings]\nexperimental = true\n")

    def test_a_boolean_is_not_written_as_a_number(self) -> None:
        self.assertEqual(host_tools.host_tools_mise_toml({"settings": {"on": True, "one": 1}}), "[settings]\non = true\none = 1\n")

    def test_what_it_cannot_render_is_refused(self) -> None:
        cases = {
            "a table the role does not render": ({"env": {"A": "b"}}, "env"),
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
                host_tools.host_tools_mise_toml(config)


if __name__ == "__main__":
    unittest.main()
