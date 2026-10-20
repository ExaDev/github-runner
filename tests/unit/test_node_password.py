"""Tests for how the cluster role keeps a k3s node's password across container recreation: the Compose file's node volumes, and the seeding of the volume from the running container (tasks/node_password.yml), run through the role (tests/node_password/run.yml) against a stand-in for docker that moves a real tar stream between its docker cp processes, first on the playbook's PATH so that no real Docker daemon is reached."""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from typing import Any, ClassVar

import yaml
from cluster_stand_ins import (
    ROLE,
    RoleTestCase,
    has_compose,
    recap,
    render_compose,
    role_tasks,
)

COMPOSE = ROLE / "files" / "docker-compose.yml"
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
NODE_PASSWORD_MOUNT = "/etc/rancher/node"


class ComposeFileTest(unittest.TestCase):
    SERVICES: ClassVar[dict[str, str]] = {"k3s": "k3s-node", "k3s-agent": "k3s-agent-node"}

    def test_every_node_service_mounts_its_own_named_volume_at_the_password_directory(self) -> None:
        document = yaml.safe_load(COMPOSE.read_text())
        for service, volume in self.SERVICES.items():
            with self.subTest(service=service):
                self.assertIn(f"{volume}:{NODE_PASSWORD_MOUNT}", document["services"][service]["volumes"])
                self.assertIn(volume, document["volumes"])

    def test_the_services_do_not_share_a_password_volume(self) -> None:
        self.assertEqual(len(set(self.SERVICES.values())), len(self.SERVICES))

    def test_the_role_names_the_volumes_the_file_declares(self) -> None:
        self.assertIn("'k3s-node'", DEFAULTS["github_runner_cluster_node_password_volume_key"])
        self.assertIn("'k3s-agent-node'", DEFAULTS["github_runner_cluster_node_password_volume_key"])
        self.assertEqual(DEFAULTS["github_runner_cluster_node_password_volume"], "{{ github_runner_cluster_compose_project }}_{{ github_runner_cluster_node_password_volume_key }}")
        self.assertIn("'k3s-agent'", DEFAULTS["github_runner_cluster_node_service"])


@unittest.skipUnless(has_compose(), "docker compose is not installed")
class ComposeRenderTest(unittest.TestCase):
    def test_compose_names_each_volume_from_the_project_and_the_key_the_role_uses(self) -> None:
        result = render_compose("config", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        for service, volume in ComposeFileTest.SERVICES.items():
            with self.subTest(service=service):
                self.assertEqual(document["volumes"][volume]["name"], f"example_{volume}")
                mounts = {mount["target"]: mount for mount in document["services"][service]["volumes"] if mount["type"] == "volume"}
                self.assertEqual(mounts[NODE_PASSWORD_MOUNT]["source"], volume)



class NodePasswordSeedingTest(RoleTestCase):
    VOLUME = "node1_k3s-node"

    def seed(self, *extra: str, check: bool = False, **hostvars: Any) -> subprocess.CompletedProcess[str]:
        hosts, group_vars = self.hosts(("node1",), **hostvars)
        return self.run_tasks("node_password", hosts, group_vars, *extra, check=check)

    def test_a_running_container_without_a_volume_seeds_one_that_holds_its_password(self) -> None:
        self.container("node1-k3s", password=self.SECRET)
        result = self.seed()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.stub / "volumes" / self.VOLUME / "password").read_text(), self.SECRET)
        self.assertEqual(recap(result.stdout, "node1")["changed"], 1)
        self.nothing_on_the_host_holds_the_secret(result)

    def test_the_volume_keeps_the_password_files_mode_and_root_ownership(self) -> None:
        self.container("node1-k3s", password=self.SECRET)
        self.assertEqual(self.seed().returncode, 0)
        self.assertEqual(oct((self.stub / "volumes" / self.VOLUME / "password").stat().st_mode & 0o777), "0o600")
        self.assertEqual((self.stub / "owners.log").read_text().splitlines(), ["password 0:0 600"])
        self.assertTrue(any(call.startswith("- cp --archive ") for call in self.calls()), "the copy must ask for the archive's ownership")

    def test_the_volume_carries_the_labels_compose_puts_on_its_volumes(self) -> None:
        self.container("node1-k3s", password=self.SECRET)
        self.assertEqual(self.seed().returncode, 0)
        self.assertEqual((self.stub / "labels" / self.VOLUME).read_text().splitlines(), ["com.docker.compose.project=node1", "com.docker.compose.volume=k3s-node"])

    def test_the_throwaway_container_is_created_from_the_nodes_own_image_and_removed(self) -> None:
        self.container("node1-k3s", password=self.SECRET, image="sha256:feed")
        self.assertEqual(self.seed().returncode, 0)
        creates = [call for call in self.calls() if call.startswith("- create ")]
        self.assertEqual(creates, [f"- create --name node1-k3s-password-seed --volume {self.VOLUME}:/etc/rancher/node sha256:feed"])
        self.assertFalse((self.stub / "containers" / "node1-k3s-password-seed.state").exists())

    def test_a_second_run_changes_nothing(self) -> None:
        self.container("node1-k3s", password=self.SECRET)
        self.assertEqual(self.seed().returncode, 0)
        before = self.calls()
        result = self.seed()
        self.assertEqual(recap(result.stdout, "node1")["changed"], 0)
        self.assertEqual([call for call in self.calls()[len(before) :] if re.match(r"\S+ (create|cp|volume create)", call)], [])
        self.assertEqual((self.stub / "volumes" / self.VOLUME / "password").read_text(), self.SECRET)

    def test_a_volume_that_exists_is_left_as_it_is(self) -> None:
        self.container("node1-k3s", password=self.SECRET)
        (self.stub / "volumes" / self.VOLUME).mkdir(parents=True)
        (self.stub / "volumes" / self.VOLUME / "password").write_text("the password the node already holds")
        result = self.seed()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.stub / "volumes" / self.VOLUME / "password").read_text(), "the password the node already holds")
        self.assertEqual(self.mutating(), [])

    def test_a_volume_whose_name_only_contains_the_wanted_one_does_not_count(self) -> None:
        self.container("node1-k3s", password=self.SECRET)
        (self.stub / "volumes" / f"x{self.VOLUME}").mkdir(parents=True)
        self.assertEqual(self.seed().returncode, 0)
        self.assertEqual((self.stub / "volumes" / self.VOLUME / "password").read_text(), self.SECRET)

    def test_no_container_yet_means_nothing_to_seed(self) -> None:
        result = self.seed()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(recap(result.stdout, "node1")["changed"], 0)
        self.assertFalse((self.stub / "volumes").exists())
        self.assertEqual(self.mutating(), [])

    def test_a_stopped_container_is_seeded_too(self) -> None:
        # Its password is the one the cluster holds a Secret for, and Compose recreates a stopped container as it does a running one.
        self.container("node1-k3s", state="exited", password=self.SECRET)
        self.assertEqual(self.seed().returncode, 0)
        self.assertEqual((self.stub / "volumes" / self.VOLUME / "password").read_text(), self.SECRET)

    def test_check_mode_says_it_would_seed_and_does_nothing(self) -> None:
        self.container("node1-k3s", password=self.SECRET)
        result = self.seed(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Would create the volume {self.VOLUME} and copy /etc/rancher/node from node1-k3s into it", result.stdout)
        self.assertEqual(recap(result.stdout, "node1")["changed"], 1)
        self.assertEqual(self.mutating(), [])
        self.assertFalse((self.stub / "volumes").exists())

    def test_check_mode_with_nothing_to_seed_reports_nothing(self) -> None:
        for setup in (lambda: None, lambda: (self.container("node1-k3s", password=self.SECRET), (self.stub / "volumes" / self.VOLUME).mkdir(parents=True))):
            with self.subTest():
                setup()
                result = self.seed(check=True)
                self.assertNotIn("Would create the volume", result.stdout)
                self.assertEqual(recap(result.stdout, "node1")["changed"], 0)

    def test_an_agent_is_seeded_into_its_own_volume(self) -> None:
        hosts, group_vars = self.hosts(("node1",), ("agent1",))
        self.container("github-runner-k3s-agent", password=self.SECRET)
        result = self.run_tasks("node_password", hosts, group_vars, limit="agent1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.stub / "volumes" / "agent1_k3s-agent-node" / "password").read_text(), self.SECRET)
        self.assertEqual((self.stub / "labels" / "agent1_k3s-agent-node").read_text().splitlines(), ["com.docker.compose.project=agent1", "com.docker.compose.volume=k3s-agent-node"])

    def test_a_failed_copy_fails_the_run_keeps_no_volume_and_leaks_nothing(self) -> None:
        self.container("node1-k3s", password=self.SECRET)
        self.write("containers", "node1-k3s.cp-fails", text="")
        result = self.seed()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not copy /etc/rancher/node from node1-k3s into the volume node1_k3s-node (exit status 1: ", result.stdout)
        # Both docker cp processes' messages are shown, since the one that failed first is not always the last to speak.
        self.assertIn("Error response from daemon: Could not find the file /etc/rancher/node in container node1-k3s", result.stdout)
        self.assertIn("docker volume create", result.stdout)
        self.assertFalse((self.stub / "volumes" / self.VOLUME).exists(), "a half-seeded volume would stop every later run from seeding")
        self.assertFalse((self.stub / "containers" / "node1-k3s-password-seed.state").exists())
        self.nothing_on_the_host_holds_the_secret(result)

    def test_the_run_after_a_failed_copy_seeds_again(self) -> None:
        self.container("node1-k3s", password=self.SECRET)
        self.write("containers", "node1-k3s.cp-fails", text="")
        self.assertNotEqual(self.seed().returncode, 0)
        (self.stub / "containers" / "node1-k3s.cp-fails").unlink()
        self.assertEqual(self.seed().returncode, 0)
        self.assertEqual((self.stub / "volumes" / self.VOLUME / "password").read_text(), self.SECRET)

    def test_a_throwaway_container_left_by_an_earlier_run_is_replaced(self) -> None:
        self.container("node1-k3s", password=self.SECRET)
        self.write("containers", "node1-k3s-password-seed.state", text="created")
        result = self.seed()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.stub / "volumes" / self.VOLUME / "password").read_text(), self.SECRET)

    def test_every_docker_call_runs_in_the_pinned_context(self) -> None:
        self.container("node1-k3s", password=self.SECRET)
        result = self.seed(github_runner_cluster_docker_cli_environment={"DOCKER_CONTEXT": "ctx-node1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual({call.split()[0] for call in self.calls()}, {"ctx-node1"})

    def test_a_daemon_that_cannot_be_reached_fails_the_run(self) -> None:
        self.write("daemon-down", text="")
        result = self.seed()
        self.assertNotEqual(result.returncode, 0)


class TaskStructureTest(unittest.TestCase):
    def test_the_password_volume_is_seeded_before_anything_that_can_recreate_a_container(self) -> None:
        tasks = role_tasks("main.yml")
        includes = [task.get("ansible.builtin.include_tasks") for task in tasks]
        self.assertIn("node_password.yml", includes)
        # Before the mesh step, which can start the bootstrap server's Compose project, and after the Compose file that mounts the volume is in place.
        self.assertLess(includes.index("node_password.yml"), includes.index("mesh/main.yml"))
        placed = next(index for index, task in enumerate(tasks) if "ansible.builtin.copy" in task and task["name"].startswith("Place the collection's Compose file"))
        self.assertLess(placed, includes.index("node_password.yml"))

    def test_the_seeding_task_that_handles_the_password_never_prints_it(self) -> None:
        seed = next(task for task in role_tasks("node_password.yml") if task.get("name") == "Seed the password volume from the running container")
        self.assertIs(seed["no_log"], True)
        self.assertIn("ansible_check_mode", " ".join(seed["when"]))
        script = seed["ansible.builtin.shell"]
        self.assertNotIn(">", re.sub(r">/dev/null( 2>&1)?", "", script).replace("2>&1", ""), "the copy must not be redirected to a file")
        self.assertIn("set -o pipefail", script)


if __name__ == "__main__":
    unittest.main()
