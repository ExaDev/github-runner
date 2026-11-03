"""Tests the github_runner_cluster role's native node as a k3s server (tests/native/run.yml) against the stand-ins in native_stand_ins.py: bootstrapping and joining with the Docker servers' flags, the restart guard on the other voters, a switch between the server and agent units in both directions, and a former member never starting a second cluster."""

from __future__ import annotations

import re
import stat
import unittest

import yaml
from native_stand_ins import DOMAIN, ROLE, TAILSCALE_VERSION, NativeRoleHarness


class NativeServerTest(NativeRoleHarness):
    """A native host run as a k3s server, and switched between its server and agent units."""

    def test_a_native_bootstrap_server_starts_a_new_cluster_with_the_docker_servers_flags(self) -> None:
        result = self.run_role(host_vars={"github_runner_cluster_bootstrap": True, "github_runner_cluster_etcd_heartbeat_interval": 500, "github_runner_cluster_etcd_election_timeout": 5000}, only_the_node=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config_dir = "github_runner_cluster_native_config_dir"
        server = yaml.safe_load(self.file(config_dir, "server.yaml").read_text())
        self.assertEqual(server, {
            "token-file": str(self.file(config_dir, "token")),
            "vpn-auth-file": str(self.file(config_dir, "vpn-auth")),
            "node-name": "laptop",
            "disable": ["traefik"],
            "disable-network-policy": True,
            "write-kubeconfig-mode": "0600",
            "tls-san": [f"laptop.{DOMAIN}"],
            "etcd-arg": ["heartbeat-interval=500", "election-timeout=5000"],
            "resolv-conf": "/run/github-runner-k3s/resolv.conf",
        })
        self.assertEqual(yaml.safe_load(self.file(config_dir, "server.yaml.d/10-join.yaml").read_text()), {"cluster-init": True})
        for name in ("server.yaml", "server.yaml.d/10-join.yaml"):
            self.assertEqual(stat.S_IMODE(self.file(config_dir, name).stat().st_mode), 0o600, name)
        self.assertEqual(self.role_file(), "server")
        self.assertEqual(self.unit_calls(), ["systemctl enable --now tailscaled", "systemctl enable k3s", "systemctl start k3s"])
        self.assertEqual(self.starts(), ["start k3s server"])
        # The node is waited for through its own server, as root.
        self.assertTrue(any(call.startswith("kubectl get node laptop") for call in self.calls()))
        self.assertTrue(any(call.startswith("kubectl get lease") for call in self.calls()))
        self.assertFalse(any(call.startswith("docker") for call in self.calls()))

    def test_the_server_unit_runs_k3s_server_under_its_role_condition_with_the_mesh_address(self) -> None:
        self.assertEqual(self.run_role(host_vars={"github_runner_cluster_bootstrap": True}, only_the_node=True).returncode, 0)
        text = self.file("github_runner_cluster_native_unit_dir", "k3s.service").read_text()
        service = [line for line in text.split("[Service]")[1].split("[Install]")[0].splitlines() if line and not line.startswith("#")]
        lib, config = self.paths["github_runner_cluster_native_lib_dir"], self.paths["github_runner_cluster_native_config_dir"]
        self.assertIn(f"ExecCondition={lib}/fleet-role-is server {self.paths['github_runner_cluster_native_data_dir']}/fleet-role", service)
        self.assertIn(f"ExecStartPre={lib}/write-node-ip {config}/server.yaml.d/00-node-ip.yaml {config}/vpn-auth", service)
        self.assertIn(f"ExecStart={self.paths['github_runner_cluster_native_bin_dir']}/k3s server --config {config}/server.yaml", service)
        # Everything else is the agent unit's: one runs at a time, under the same limits.
        agent = [line for line in self.file("github_runner_cluster_native_unit_dir", "k3s-agent.service").read_text().split("[Service]")[1].split("[Install]")[0].splitlines() if line and not line.startswith("#")]
        self.assertEqual([line for line in service if not line.startswith(("ExecCondition=", "ExecStart"))], [line for line in agent if not line.startswith(("ExecCondition=", "ExecStart"))])
        for helper in ("write-node-ip", "fleet-role-is"):
            self.assertEqual(self.file("github_runner_cluster_native_lib_dir", helper).read_text(), (ROLE / "files" / "native" / f"{helper}.sh").read_text())

    def test_a_native_server_joins_a_cluster_of_docker_servers_as_they_do(self) -> None:
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\n")
        result = self.run_three()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config_dir = "github_runner_cluster_native_config_dir"
        self.assertEqual(yaml.safe_load(self.file(config_dir, "server.yaml.d/10-join.yaml").read_text()), {"server": f"https://server1.{DOMAIN}:6443"})
        # The test inventory lists laptop first (yaml.safe_dump sorts the hosts), and the SANs are in inventory order.
        self.assertEqual(yaml.safe_load(self.file(config_dir, "server.yaml").read_text())["tls-san"], [f"laptop.{DOMAIN}", f"server1.{DOMAIN}", f"server2.{DOMAIN}"])
        unit = self.file("github_runner_cluster_native_unit_dir", "k3s.service").read_text()
        self.assertIn(f'Environment="GITHUB_RUNNER_JOIN_HOST=server1.{DOMAIN}"', unit)
        # The voters were read through the bootstrap server's container before the server started.
        calls = self.calls()
        read = next(index for index, call in enumerate(calls) if call.startswith("docker exec github-runner-k3s kubectl get nodes") and "etcd}" in call)
        self.assertLess(read, calls.index("systemctl start k3s"))
        self.assertEqual(self.starts(), ["start k3s server"])

    def test_a_native_server_does_not_join_while_another_voter_is_not_ready(self) -> None:
        (self.state / "voters").write_text("server1,True,true\nserver2,False,true\n")
        result = self.run_three()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("voter server2 is not Ready (its Ready condition is False)", re.sub(r"\s+", " ", result.stdout))
        self.assertEqual(self.starts(), [])
        self.assertNotIn("systemctl start k3s", self.calls())

    def test_an_agent_is_promoted_in_place_once_its_node_is_empty(self) -> None:
        self.running_as("agent")
        password = self.root / "etc" / "rancher" / "node" / "password"
        password.parent.mkdir(parents=True)
        password.write_text("node-password")
        before = len(self.calls())
        result = self.run_three()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()[before:]
        order = [
            "kubectl cordon laptop",
            next(call for call in calls if call.startswith("kubectl get pods --all-namespaces --selector actions-ephemeral-runner=True --field-selector spec.nodeName=laptop,status.phase!=Succeeded,status.phase!=Failed")),
            next(call for call in calls if call.startswith("kubectl drain laptop --ignore-daemonsets --delete-emptydir-data")),
            "systemctl disable --now k3s-agent",
            "systemctl start k3s",
            "kubectl uncordon laptop",
        ]
        self.assertEqual([call for call in calls if call in order], order)
        self.assertEqual(self.role_file(), "server")
        self.assertEqual(self.starts()[-1], "start k3s server")
        self.assertFalse((self.state / "active-k3s-agent").exists())
        self.assertFalse((self.state / "enabled-k3s-agent").exists())
        self.assertEqual(password.read_text(), "node-password")
        # The cordon and drain went through another server, and the uncordon through this one, which now serves the API.
        self.assertIn("docker exec github-runner-k3s kubectl cordon laptop", calls)
        self.assertNotIn("docker exec github-runner-k3s kubectl uncordon laptop", calls)

    def test_a_promotion_whose_runner_pods_do_not_finish_is_abandoned_with_the_node_uncordoned(self) -> None:
        self.running_as("agent")
        (self.state / "runner-pods").write_text("pod/runner-abc12\n")
        before = len(self.calls())
        result = self.run_three(laptop={"github_runner_cluster_native_runner_wait_timeout": 1})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("its node could not be emptied", re.sub(r"\s+", " ", result.stdout))
        calls = self.calls()[before:]
        self.assertIn("kubectl uncordon laptop", calls)
        self.assertNotIn("systemctl disable --now k3s-agent", calls)
        self.assertEqual(self.role_file(), "agent")
        self.assertTrue((self.state / "active-k3s-agent").exists())

    def test_a_running_server_restarts_only_while_every_other_voter_is_ready_and_a_refused_restart_is_made_later(self) -> None:
        self.assertEqual(self.run_three().returncode, 0)
        (self.state / "voters").write_text("server1,True,true\nserver2,Unknown,true\nlaptop,True,true\n")
        taints = {"github_runner_cluster_node_taints": ["dedicated=ci:NoSchedule"]}
        before = len(self.calls())
        refused = self.run_three(laptop=taints)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Not going ahead with the restart of k3s on laptop: voter server2 is not Ready", re.sub(r"\s+", " ", refused.stdout))
        self.assertEqual(self.unit_calls(before), [])
        # The voters were read through this server's own k3s, which is running.
        self.assertTrue(any(call.startswith("kubectl get nodes") and "etcd}" in call for call in self.calls()[before:]))
        self.assertFalse(any(call.startswith("docker exec") and "etcd}" in call for call in self.calls()[before:]))
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\nlaptop,True,true\n")
        before = len(self.calls())
        result = self.run_three(laptop=taints)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.unit_calls(before), ["systemctl restart k3s"])
        before = len(self.calls())
        self.assertEqual(self.run_three(laptop=taints).returncode, 0)
        self.assertEqual(self.unit_calls(before), [])

    def test_a_check_run_reports_a_promotion_and_makes_none(self) -> None:
        self.assertEqual(self.run_three(laptop={"github_runner_cluster_node_role": "agent"}).returncode, 0)
        before = len(self.calls())
        result = self.run_three(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("promote k3s on laptop", result.stdout)
        self.assertEqual([call for call in self.mutations(before) if not call.startswith(("apt-get", "systemctl enable --now tailscaled"))], [])
        self.assertEqual(self.role_file(), "agent")

    def test_a_demotion_that_would_leave_two_voters_is_refused(self) -> None:
        self.assert_demotion_refused("server1,True,true\nserver2,True,true\nlaptop,True,true\n", "only 2 other voters (server1, server2) would remain, fewer than the 3")

    def test_a_demotion_while_another_voter_is_not_ready_is_refused(self) -> None:
        self.assert_demotion_refused("server1,True,true\nserver2,True,true\nserver3,False,true\nlaptop,True,true\n", "voter server3 is not Ready")

    def test_a_server_is_demoted_once_its_member_is_removed_with_its_datastore_moved_aside_and_its_identity_kept(self) -> None:
        before = len(self.calls())
        result = self.demote()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = self.paths["github_runner_cluster_native_data_dir"]
        self.assertEqual(self.role_file(), "agent")
        self.assertFalse((data / "server" / "db").exists())
        moved = data / "server" / "db.demoted"
        self.assertEqual((moved / "etcd/member/wal/0000000000000000-0000000000000000.wal").read_text(), "wal-bytes")
        self.assertEqual((moved / "snapshots/etcd-snapshot-laptop-1767225600").read_text(), "snapshot")
        # The newest copy replaces the one an earlier demotion left.
        self.assertEqual((moved / "etcd" / "name").read_text(), "laptop-0a1b2c3d")
        for kept in ("server/tls/server-ca.crt", "server/token", "agent/etc/containerd/config.toml"):
            self.assertTrue((data / kept).is_file(), kept)
        calls = self.calls()[before:]
        order = [
            "kubectl cordon laptop",
            "kubectl annotate --overwrite node laptop etcd.k3s.cattle.io/remove=true",
            "systemctl disable --now k3s",
            "kubectl label node laptop node-role.kubernetes.io/control-plane- node-role.kubernetes.io/etcd- node-role.kubernetes.io/master-",
            "kubectl annotate node laptop etcd.k3s.cattle.io/remove- etcd.k3s.cattle.io/removed-node-name- etcd.k3s.cattle.io/node-name- etcd.k3s.cattle.io/node-address-",
            "systemctl enable --now k3s-agent",
            "kubectl uncordon laptop",
        ]
        self.assertEqual([call for call in calls if call in order], order)
        # Everything went through another server's API, since this one stops.
        for call in order:
            if call.startswith("kubectl"):
                self.assertIn("docker exec github-runner-k3s " + call, calls)
        self.assertEqual(self.starts()[-1], "enable k3s-agent agent")
        self.assertFalse((self.state / "active-k3s").exists())

    def test_a_server_whose_member_is_not_removed_is_not_stopped(self) -> None:
        (self.state / "member-stays").touch()
        result = self.demote_quickly()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.state / "active-k3s").exists())
        self.assertTrue(self.wal.exists())
        # The role file already names the agent, so the server is not started again by Restart=always or a reboot; a rerun finishes the demotion.
        self.assertEqual(self.role_file(), "agent")
        (self.state / "member-stays").unlink()
        result = self.run_three(laptop={"github_runner_cluster_node_role": "agent"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.state / "active-k3s").exists())
        self.assertFalse(self.wal.exists())

    def test_a_former_member_never_starts_a_second_cluster(self) -> None:
        moved = self.paths["github_runner_cluster_native_data_dir"] / "server" / "db.demoted" / "etcd"
        moved.mkdir(parents=True)
        (moved / "name").write_text("laptop-0a1b2c3d")
        alone = self.run_role(host_vars={"github_runner_cluster_bootstrap": True}, only_the_node=True)
        self.assertNotEqual(alone.returncode, 0)
        self.assertIn("with no server to join", re.sub(r"\s+", " ", alone.stdout))
        self.assertEqual(self.starts(), [])
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\n")
        result = self.run_three(laptop={"github_runner_cluster_bootstrap": True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "server.yaml.d/10-join.yaml").read_text()), {"server": f"https://server1.{DOMAIN}:6443"})

    def test_a_bootstrap_host_with_no_datastore_joins_a_cluster_another_server_runs(self) -> None:
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\n")
        result = self.run_three(laptop={"github_runner_cluster_bootstrap": True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "server.yaml.d/10-join.yaml").read_text()), {"server": f"https://server1.{DOMAIN}:6443"})

    def test_a_bootstrap_host_with_no_datastore_starts_a_cluster_only_when_every_other_server_is_stopped(self) -> None:
        (self.state / "docker-stopped").touch()
        result = self.run_three(laptop={"github_runner_cluster_bootstrap": True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "server.yaml.d/10-join.yaml").read_text()), {"cluster-init": True})

    def test_a_bootstrap_host_that_cannot_tell_whether_a_cluster_exists_is_refused(self) -> None:
        (self.state / "nodes-unreadable").touch()
        result = self.run_three(laptop={"github_runner_cluster_bootstrap": True}, github_runner_cluster_allow_k3s_version_jump=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("would start a new cluster (cluster-init) unless another server already runs one, and that cannot be told", re.sub(r"\s+", " ", result.stdout))
        self.assertEqual(self.mutations(), [])

    def test_an_agent_from_before_the_role_file_keeps_running_as_an_agent(self) -> None:
        unit = self.file("github_runner_cluster_native_unit_dir", "k3s-agent.service")
        unit.write_text("[Service]\nExecStart=/usr/local/bin/k3s agent\n")
        old = self.file("github_runner_cluster_native_config_dir", "config.yaml")
        old.parent.mkdir(parents=True)
        old.write_text("server: https://server1.example.ts.net:6443\n")
        for name in ("enabled-k3s-agent", "active-k3s-agent"):
            (self.state / name).touch()
        self.set_package(TAILSCALE_VERSION)
        self.set_tailscale("Running", DOMAIN)
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.role_file(), "agent")
        self.assertFalse(old.exists())
        self.assertEqual(self.starts(), ["restart k3s-agent agent"])

    def test_an_eligible_agent_is_staged_as_a_standby_server_and_keeps_its_role(self) -> None:
        result = self.run_role(host_vars={"github_runner_cluster_voter_eligible": True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.role_file(), "agent")
        server = yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "server.yaml").read_text())
        self.assertEqual(server["tls-san"], [f"laptop.{DOMAIN}", f"server1.{DOMAIN}"])
        self.assertNotIn("systemctl start k3s", self.calls())
        # The inventory gives an eligible host only the role it starts with.
        before = len(self.calls())
        result = self.run_three(laptop={"github_runner_cluster_voter_eligible": True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.role_file(), "agent")
        self.assertEqual(self.unit_calls(before), [])

    def test_a_native_bootstrap_host_is_refused_under_headscale_in_cluster(self) -> None:
        result = self.run_role(host_vars={"github_runner_cluster_bootstrap": True, "github_runner_cluster_mesh": "headscale_in_cluster"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("whose bootstrap server runs the Headscale server as a static pod", re.sub(r"\s+", " ", result.stdout))
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
