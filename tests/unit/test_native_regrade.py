"""Tests the github_runner_cluster role's memory-pressure regrading of a native server (tests/native/run.yml) against the stand-ins in native_stand_ins.py: the role's refusals, the fleet-regrade unit and the settings it runs with, the server unit's ExecStartPre that records the level, a server turn at etcd-only, and turning regrading off. The systemd actuator itself is tested in tests/regrade/regrade-systemd.sh, run by test_regrade_shell.py."""

from __future__ import annotations

import re
import subprocess
import unittest
from typing import Any

from native_stand_ins import DOMAIN, ROLE, NativeRoleHarness

ON = {"github_runner_cluster_regrade": True}


class NativeRegradeTest(NativeRoleHarness):
    """A native server that regrades, and one that is refused it."""

    def run_server(self, check: bool = False, **laptop: Any) -> subprocess.CompletedProcess[str]:
        """Run laptop as a native server beside the Docker servers server1 and server2, all three voters Ready."""
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\nlaptop,True,true\n")
        return self.run_three(laptop=laptop, check=check)

    def settings(self) -> dict[str, str]:
        text = self.file("github_runner_cluster_native_lib_dir", "regrade/fleet-regrade.env").read_text()
        script = "set -eu\n. /dev/stdin <<'ENV'\n" + text + "ENV\nenv -0\n"
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True, env={})
        return dict(entry.split("=", 1) for entry in result.stdout.split("\0") if "=" in entry)

    def regrade_unit(self) -> str:
        return self.file("github_runner_cluster_native_unit_dir", "fleet-regrade.service").read_text()

    def started_level(self) -> str:
        return self.file("github_runner_cluster_native_run_dir", "regrade-level").read_text().strip()

    def test_a_native_server_runs_the_fleet_regrade_unit_with_the_systemd_actuator(self) -> None:
        result = self.run_server(**ON)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lib = self.paths["github_runner_cluster_native_lib_dir"] / "regrade"
        for name in ("fleet-regrade.sh", "fleet-regrade-core.sh", "fleet-regrade-k3s.sh", "fleet-regrade-systemd.sh"):
            self.assertEqual((lib / name).read_text(), (ROLE / "files" / "regrade" / name).read_text(), name)
        settings = self.settings()
        self.assertEqual(settings["FLEET_REGRADE_ACTUATOR"], "systemd")
        self.assertEqual(settings["KUBECTL"], f"{self.paths['github_runner_cluster_native_bin_dir']}/k3s kubectl")
        self.assertEqual(settings["KUBECONFIG_PATH"], f"{self.paths['github_runner_cluster_native_config_dir']}/k3s.yaml")
        self.assertEqual(settings["K3S_SERVICE"], "k3s")
        self.assertEqual(settings["K3S_DATA_DIR"], str(self.paths["github_runner_cluster_native_data_dir"]))
        self.assertEqual(settings["LEVEL_FILE"], str(self.file("github_runner_cluster_native_data_dir", "regrade-level")))
        self.assertEqual(settings["STARTED_FILE"], str(self.file("github_runner_cluster_native_run_dir", "regrade-level")))
        self.assertEqual(settings["NODE_NAME"], "laptop")
        self.assertNotIn("NODE_CONTAINER", settings)
        # Every other server, never this one's own API.
        self.assertEqual(settings["KUBE_SERVERS"].split(" "), [f"https://server1.{DOMAIN}:6443", f"https://server2.{DOMAIN}:6443"])
        unit = self.regrade_unit()
        self.assertIn(f"ExecStart={lib}/fleet-regrade.sh watch", unit)
        self.assertIn(f'Environment="FLEET_REGRADE_ENV={lib}/fleet-regrade.env"', unit)
        self.assertNotRegex(unit, r"(?m)^(BindsTo|PartOf|Requires)=", "the watcher restarts k3s and must outlive it")
        link = self.file("github_runner_cluster_native_bin_dir", "fleet-regrade")
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (lib / "fleet-regrade.sh").resolve())
        self.assertIn("systemctl enable fleet-regrade", self.calls())
        self.assertIn("systemctl start fleet-regrade", self.calls())
        self.assertLess(self.calls().index("systemctl start k3s"), self.calls().index("systemctl start fleet-regrade"))

    def test_a_rerun_leaves_the_unit_running_and_a_changed_setting_restarts_it(self) -> None:
        self.assertEqual(self.run_server(**ON).returncode, 0)
        before = len(self.calls())
        self.assertEqual(self.run_server(**ON).returncode, 0)
        self.assertEqual([call for call in self.unit_calls(before) if "fleet-regrade" in call], [])
        before = len(self.calls())
        result = self.run_server(**ON, github_runner_cluster_regrade_pressure_threshold=25)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.unit_calls(before) if "fleet-regrade" in call], ["systemctl restart fleet-regrade"])
        self.assertEqual(self.settings()["PRESSURE_THRESHOLD"], "25")

    def test_the_server_unit_records_the_level_before_every_start_whether_regrading_is_on_or_not(self) -> None:
        self.assertEqual(self.run_server().returncode, 0)
        off = self.file("github_runner_cluster_native_unit_dir", "k3s.service").read_text()
        lib, config = self.paths["github_runner_cluster_native_lib_dir"], self.paths["github_runner_cluster_native_config_dir"]
        line = f"ExecStartPre={lib}/write-regrade-level {self.file('github_runner_cluster_native_data_dir', 'regrade-level')} {config}/server.yaml.d/50-regrade.yaml {self.file('github_runner_cluster_native_run_dir', 'regrade-level')}"
        self.assertIn(line, off.splitlines())
        self.assertEqual(self.file("github_runner_cluster_native_lib_dir", "write-regrade-level").read_text(), (ROLE / "files" / "native" / "write-regrade-level.sh").read_text())
        # The start the stand-in made ran it: a server never stepped down starts at full, with a drop-in that sets nothing.
        self.assertEqual(self.started_level(), "full")
        self.assertEqual([line for line in self.file("github_runner_cluster_native_config_dir", "server.yaml.d/50-regrade.yaml").read_text().splitlines() if not line.startswith("#")], [])
        self.assertEqual(self.run_server(**ON).returncode, 0)
        self.assertEqual(self.file("github_runner_cluster_native_unit_dir", "k3s.service").read_text(), off)

    def test_regrading_is_refused_where_it_could_not_work_before_anything_changes(self) -> None:
        cases: list[tuple[str, dict[str, Any], str]] = [
            ("an agent", {"github_runner_cluster_node_role": "agent"}, "is an agent"),
            ("a kernel without memory pressure", {"github_runner_cluster_native_psi_file": str(self.root / "proc" / "none")}, "exposes no memory pressure"),
        ]
        for name, laptop, message in cases:
            with self.subTest(name):
                result = self.run_server(**ON, **laptop)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, re.sub(r"\s+", " ", result.stdout))
                self.assertEqual(self.mutations(), [])

    def test_the_only_server_is_refused(self) -> None:
        result = self.run_role(host_vars={"github_runner_cluster_bootstrap": True, **ON}, only_the_node=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is the only server", re.sub(r"\s+", " ", result.stdout))
        self.assertEqual(self.mutations(), [])

    def test_an_eligible_host_that_runs_as_an_agent_is_refused_whatever_the_inventory_says(self) -> None:
        # The role the host is to run is the one recorded on it, which whatever moves votes between hosts sets at run time.
        self.running_as("agent")
        result = self.run_server(**ON, github_runner_cluster_voter_eligible=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is an agent", re.sub(r"\s+", " ", result.stdout))

    def test_turning_regrading_off_at_full_removes_the_unit_and_leaves_the_server_running(self) -> None:
        self.assertEqual(self.run_server(**ON).returncode, 0)
        before = len(self.calls())
        result = self.run_server()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.unit_calls(before), ["systemctl disable --now fleet-regrade"])
        self.assertFalse(self.file("github_runner_cluster_native_unit_dir", "fleet-regrade.service").exists())
        self.assertFalse((self.paths["github_runner_cluster_native_lib_dir"] / "regrade").exists())
        self.assertFalse(self.file("github_runner_cluster_native_bin_dir", "fleet-regrade").is_symlink())
        self.assertTrue((self.state / "active-k3s").exists())
        before = len(self.calls())
        self.assertEqual(self.run_server().returncode, 0)
        self.assertEqual(self.unit_calls(before), [])

    def test_turning_regrading_off_below_full_is_refused_and_removes_nothing(self) -> None:
        self.assertEqual(self.run_server(**ON).returncode, 0)
        self.file("github_runner_cluster_native_run_dir", "regrade-level").write_text("etcd-only\n")
        before = len(self.calls())
        result = self.run_server()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is running at the level etcd-only, below full", re.sub(r"\s+", " ", result.stdout))
        self.assertEqual(self.unit_calls(before), [])
        self.assertTrue(self.file("github_runner_cluster_native_unit_dir", "fleet-regrade.service").exists())

    def test_a_server_restarted_at_etcd_only_is_waited_for_by_its_etcd_not_its_node(self) -> None:
        self.assertEqual(self.run_server(**ON).returncode, 0)
        self.file("github_runner_cluster_native_data_dir", "regrade-level").write_text("etcd-only\n")
        # At etcd-only there is no kubelet, so the node is never Ready again.
        (self.state / "node-ready").write_text("Unknown,True")
        before = len(self.calls())
        result = self.run_server(**ON, github_runner_cluster_node_taints=["dedicated=ci:NoSchedule"], github_runner_cluster_native_ready_timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("systemctl restart k3s", self.unit_calls(before))
        self.assertEqual(self.started_level(), "etcd-only")
        self.assertIn("journalctl --no-pager --output cat _SYSTEMD_INVOCATION_ID=2", self.calls()[before:])
        self.assertFalse(any(call.startswith("kubectl get lease --namespace kube-node-lease") for call in self.calls()[before:]))

    def test_a_server_restarted_at_etcd_only_whose_etcd_does_not_come_back_fails_the_turn(self) -> None:
        self.assertEqual(self.run_server(**ON).returncode, 0)
        self.file("github_runner_cluster_native_data_dir", "regrade-level").write_text("etcd-only\n")
        (self.state / "no-etcd").touch()
        result = self.run_server(**ON, github_runner_cluster_node_taints=["dedicated=ci:NoSchedule"], github_runner_cluster_native_ready_timeout=5)
        self.assertNotEqual(result.returncode, 0)

    def test_a_demoted_server_forgets_its_level(self) -> None:
        level = self.file("github_runner_cluster_native_data_dir", "regrade-level")
        level.parent.mkdir(parents=True, exist_ok=True)
        level.write_text("control-plane\n")
        result = self.demote()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(level.exists())


if __name__ == "__main__":
    unittest.main()
