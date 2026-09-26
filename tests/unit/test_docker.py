"""Tests for the filter that points the docker CLI at the Docker context the cluster role pins."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "filter" / "docker.py"
_spec = importlib.util.spec_from_file_location("docker_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
docker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(docker)


class DockerCliEnvironmentTest(unittest.TestCase):
    def test_a_pinned_context_sets_docker_context(self) -> None:
        self.assertEqual(docker.docker_cli_environment("colima"), {"DOCKER_CONTEXT": "colima"})

    def test_surrounding_whitespace_is_not_part_of_the_name(self) -> None:
        self.assertEqual(docker.docker_cli_environment("  colima\n"), {"DOCKER_CONTEXT": "colima"})

    def test_no_pinned_context_leaves_the_environment_alone(self) -> None:
        # Empty, so the CLI keeps following the host's current context or DOCKER_HOST.
        for value in ("", "   ", None):
            with self.subTest(value=value):
                self.assertEqual(docker.docker_cli_environment(value), {})

    def test_a_value_that_is_not_a_name_is_an_error(self) -> None:
        with self.assertRaises(docker.AnsibleFilterError):
            docker.docker_cli_environment(["colima"])

    def test_the_filter_is_registered(self) -> None:
        self.assertIs(docker.FilterModule().filters()["docker_cli_environment"], docker.docker_cli_environment)


if __name__ == "__main__":
    unittest.main()
