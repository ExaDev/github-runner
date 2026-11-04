"""Tests the role actuator (roles/github_runner_cluster/files/role): its commands with the systemd platform against stand-ins that keep a native host's units and a cluster in directories (tests/role_actuator, over tests/regrade's), and the role's setup of it on a native host (tasks/role_actuator.yml) against the stand-ins in native_stand_ins.py. The role's own promotion and demotion, which run the actuator's promote-local and demote-local, are tested in test_native_server.py and test_native_regrade.py."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from native_stand_ins import ACTUATOR_TOKEN, DOMAIN, ROLE, ROOT, NativeRoleHarness

FILES = ROLE / "files"
FAKES = ROOT / "tests" / "role_actuator"
REGRADE_FAKES = ROOT / "tests" / "regrade"
NODE = "testnode"
SWAP = "20260101T000000Z-server3-testnode"
HOLDER = "fleet-role"
SWAP_ID_ANNOTATION = "github-runner.exadev.com/swap-id"
NAMESPACE = "github-runner-platform"
CONFIGMAP = "fleet-role-actuators"
JOIN_URL = f"https://server1.{DOMAIN}:6443"
# The exit statuses the actuator documents.
DONE, FAILED, REFUSED, NOT_YET = 0, 1, 2, 3
# Long enough that the Lease a test holds never runs out during it.
LEASE_SECONDS = 600
# How often the watcher reads its Node: POLL_SECONDS in fleet-regrade-k3s.sh, which the actuator sources.
_POLL = re.search(r"^POLL_SECONDS=(\d+)$", (FILES / "regrade" / "fleet-regrade-k3s.sh").read_text(), re.MULTILINE)
if _POLL is None:
    raise AssertionError("fleet-regrade-k3s.sh sets no POLL_SECONDS")
WATCH_POLL_SECONDS = int(_POLL.group(1))


class ActuatorHarness(unittest.TestCase):
    """Runs the actuator's commands on a stand-in native host. Holds no tests of its own."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="role-actuator-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.lib = self.tmp / "lib"
        self.bin = self.tmp / "bin"
        self.host = self.tmp / "host"
        self.cluster = self.tmp / "cluster"
        self.data = self.tmp / "data"
        self.config = self.tmp / "config"
        self.units = self.tmp / "units"
        self.run_dir = self.tmp / "run"
        for path in (self.lib, self.bin, self.host, self.cluster, self.data, self.config / "server.yaml.d", self.units):
            path.mkdir(parents=True)
        # The scripts as the role places them, side by side (tasks/native/config.yml).
        for source in ("role/fleet-role-actuator.sh", "role/fleet-role-actuator-systemd.sh", "regrade/fleet-regrade-core.sh", "regrade/fleet-regrade-k3s.sh", "regrade/fleet-regrade-systemd.sh"):
            shutil.copy(FILES / source, self.lib / Path(source).name)
        for name, source in {"systemctl": FAKES / "fake-systemctl.sh", "kubectl": FAKES / "fake-kubectl.sh", "k3s": FAKES / "fake-k3s.sh", "journalctl": REGRADE_FAKES / "fake-journalctl.sh", "date": REGRADE_FAKES / "fake-date.sh"}.items():
            shutil.copy(source, self.bin / name)
            (self.bin / name).chmod(0o755)
        self.level_file = self.data / "regrade-level"
        self.started_file = self.run_dir / "regrade-level"
        self.role_file = self.data / "fleet-role"
        settings = {
            "FLEET_ROLE_ACTUATOR": "systemd",
            "NODE_NAME": NODE,
            "ROLE_FILE": self.role_file,
            "K3S_SERVICE": "k3s",
            "K3S_AGENT_SERVICE": "k3s-agent",
            "REGRADE_SERVICE": "fleet-regrade",
            "K3S_DATA_DIR": self.data,
            "CONFIG_DIR": self.config,
            "UNIT_DIR": self.units,
            "LEVEL_FILE": self.level_file,
            "STARTED_FILE": self.started_file,
            "OPERATION_TIMEOUT_SECONDS": 2,
            "API_PORT": 6443,
            "LEASE_NAMESPACE": "kube-system",
            "LEASE_NAME": "fleet-regrade",
            "ROLE_LEASE_HOLDER": HOLDER,
            "SWAP_ID_ANNOTATION": SWAP_ID_ANNOTATION,
            "ACTUATOR_NAMESPACE": NAMESPACE,
            "ACTUATOR_CONFIGMAP": CONFIGMAP,
            "JOIN_URLS_FILE": self.lib / "join-urls",
            "KUBECTL": '"k3s kubectl"',
            "KUBECONFIG_PATH": self.config / "fleet-role-actuator.kubeconfig",
            "KUBE_SERVERS": f'"https://server1.{DOMAIN}:6443 https://server2.{DOMAIN}:6443"',
        }
        (self.lib / "fleet-role-actuator.env").write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
        (self.lib / "join-urls").write_text(f"server1 {JOIN_URL}\nserver2 https://server2.{DOMAIN}:6443\n")
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_HOST": str(self.host),
            "FAKE_DIR": str(self.cluster),
            "FAKE_REGRADE_SYSTEMCTL": str(REGRADE_FAKES / "fake-systemctl.sh"),
            "FAKE_REGRADE_KUBECTL": str(REGRADE_FAKES / "fake-kubectl.sh"),
            "FAKE_ROLE_IS": str(FILES / "native" / "fleet-role-is.sh"),
            "FAKE_ROLE_FILE": str(self.role_file),
            "FAKE_SERVER_DIR": str(self.data / "server"),
            "FAKE_RUNTIME_DIR": str(self.run_dir),
            "FAKE_SHIM_PREFIX": str(self.data / "data") + "/",
            # What the server unit's ExecStartPre runs, as templates/native/k3s.service.j2 writes it.
            "FAKE_PRESTART": f"{FILES / 'native' / 'write-regrade-level.sh'} {self.level_file} {self.config / 'server.yaml.d' / '50-regrade.yaml'} {self.started_file}",
        }
        (self.config / "server.yaml").write_text("token-file: token\n")
        (self.cluster / "configmap.json").write_text(json.dumps({"metadata": {"name": CONFIGMAP}, "data": {}}))
        self.set_node()

    # --- The host ---

    def running_agent(self) -> None:
        self.role_file.write_text("agent")
        (self.host / "active-k3s-agent").touch()
        (self.host / "enabled-k3s-agent").touch()

    def running_server(self, level: str = "full") -> None:
        """A server running at ``level`` through its real ExecStartPre, with a datastore whose member is named, as k3s leaves one."""
        self.role_file.write_text("server")
        self.level_file.write_text(level + "\n")
        (self.data / "server" / "db" / "etcd").mkdir(parents=True)
        (self.data / "server" / "db" / "etcd" / "name").write_text(f"{NODE}-0a1b2c3d")
        (self.data / "server" / "db" / "etcd" / "member").mkdir()
        (self.host / "enabled-k3s").touch()
        self.systemctl("start", "k3s")

    def systemctl(self, *args: str) -> None:
        subprocess.run([str(self.bin / "systemctl"), *args], env=self.env, check=True, capture_output=True)

    def active(self, unit: str) -> bool:
        return (self.host / ("active" if unit == "k3s" else f"active-{unit}")).exists()

    def assert_never_both_running(self) -> None:
        both = self.host / "both-running"
        self.assertFalse(both.exists(), both.read_text() if both.exists() else "")

    # --- The cluster ---

    def set_node(self, labels: dict[str, str] | None = None, annotations: dict[str, str] | None = None) -> None:
        (self.cluster / "node.json").write_text(json.dumps({"metadata": {"name": NODE, "labels": labels or {}, "annotations": annotations or {}}}))

    def hold_lease(self, holder: str = HOLDER, swap: str = SWAP, renewed: float | None = None) -> None:
        stamp = datetime.fromtimestamp(time.time() if renewed is None else renewed, UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        lease = {"metadata": {"name": "fleet-regrade", "namespace": "kube-system", "resourceVersion": "1", "annotations": {SWAP_ID_ANNOTATION: swap}}, "spec": {"holderIdentity": holder, "leaseDurationSeconds": LEASE_SECONDS, "acquireTime": stamp, "renewTime": stamp}}
        (self.cluster / "lease.json").write_text(json.dumps(lease))

    def no_api(self) -> None:
        (self.cluster / "unready").write_text(f"https://server1.{DOMAIN}:6443\nhttps://server2.{DOMAIN}:6443\n")

    def report(self) -> dict[str, Any]:
        data = json.loads((self.cluster / "configmap.json").read_text())["data"]
        return json.loads(data[NODE]) if NODE in data else {}

    def patches(self) -> int:
        calls = self.cluster / "calls"
        return sum(1 for line in calls.read_text().splitlines() if line.startswith("patch configmap")) if calls.exists() else 0

    # --- Running it ---

    def actuator(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.lib / "fleet-role-actuator.sh"), *args], env={**self.env, **env}, capture_output=True, text=True, timeout=120, check=False)

    def assert_status(self, result: subprocess.CompletedProcess[str], status: int) -> None:
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)


class PromoteTest(ActuatorHarness):
    def test_a_promotion_joins_the_named_voter_and_starts_the_server_unit_in_place_of_the_agent(self) -> None:
        self.running_agent()
        self.hold_lease()
        self.set_node({"github-runner.exadev.com/desired-role": "server"}, {"github-runner.exadev.com/role-join-node": "server1"})
        result = self.actuator("promote", SWAP)
        self.assert_status(result, DONE)
        self.assertEqual(self.role_file.read_text(), "server")
        self.assertEqual(yaml.safe_load((self.config / "server.yaml.d" / "20-fleet-role-join.yaml").read_text()), {"cluster-init": False, "server": JOIN_URL})
        dropin = (self.units / "k3s.service.d" / "20-fleet-role-join.conf").read_text()
        self.assertIn(f'Environment="GITHUB_RUNNER_JOIN_HOST=server1.{DOMAIN}"', dropin.splitlines())
        self.assertTrue(self.active("k3s"))
        self.assertFalse(self.active("k3s-agent"))
        self.assertFalse((self.host / "enabled-k3s-agent").exists())
        units = (self.host / "units").read_text().splitlines()
        # The agent stops before the server starts, and systemd reloads the unit's new drop-in before the start.
        self.assertLess(units.index("disable --now k3s-agent"), units.index("k3s started"))
        self.assertLess(units.index("daemon-reload"), units.index("k3s started"))
        self.assert_never_both_running()
        report = self.report()
        self.assertEqual({key: report[key] for key in ("swap", "step", "phase", "role")}, {"swap": SWAP, "step": "promote", "phase": "done", "role": "server"})
        self.assertRegex(report["started_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_a_promotion_taken_again_starts_nothing_and_reports_the_start_it_found(self) -> None:
        self.running_agent()
        self.hold_lease()
        self.set_node({"github-runner.exadev.com/desired-role": "server"}, {"github-runner.exadev.com/role-join-node": "server1"})
        self.assert_status(self.actuator("promote", SWAP), DONE)
        first = self.report()["started_at"]
        before = (self.host / "units").read_text()
        self.assert_status(self.actuator("promote", SWAP), DONE)
        self.assertNotIn("start", (self.host / "units").read_text()[len(before):].replace("is-active", ""))
        self.assertEqual(self.report()["started_at"], first)

    def test_a_promotion_without_the_lease_for_its_swap_is_refused_and_changes_nothing(self) -> None:
        cases = {
            "no lease": (None, "the regrade Lease cannot be read"),
            "free": ({"holder": ""}, "the regrade Lease is free, not held by fleet-role"),
            "another holder": ({"holder": "server2"}, "the regrade Lease is held by server2, not fleet-role"),
            "expired": ({"renewed": time.time() - 2 * LEASE_SECONDS}, "the regrade Lease held by fleet-role has expired"),
            "a stale swap id": ({"swap": "20250101T000000Z-server2-testnode"}, f"the regrade Lease is held for swap '20250101T000000Z-server2-testnode', not {SWAP}"),
        }
        for case, (lease, message) in cases.items():
            with self.subTest(case=case):
                self.setUp()
                self.running_agent()
                if lease is not None:
                    self.hold_lease(**lease)
                self.set_node({"github-runner.exadev.com/desired-role": "server"}, {"github-runner.exadev.com/role-join-node": "server1"})
                result = self.actuator("promote", SWAP)
                self.assert_status(result, REFUSED)
                self.assertIn(message, self.report()["message"])
                self.assertEqual(self.report()["phase"], "refused")
                self.assertEqual(self.role_file.read_text(), "agent")
                self.assertTrue(self.active("k3s-agent"))
                self.assertFalse((self.units / "k3s.service.d").exists())
                self.assertEqual([line for line in (self.host / "units").read_text().splitlines() if not line.startswith("is-active")] if (self.host / "units").exists() else [], [])

    def test_a_promotion_of_a_host_that_cannot_join_cleanly_is_refused(self) -> None:
        cases = {
            "not provisioned": ("no-config", "has no server configuration"),
            "a datastore no demotion moved aside": ("datastore", "holds a datastore no demotion moved aside"),
            "a join node with no URL": ("unknown-join", "the node to join, 'server9', is not one"),
            "a step that is not a swap's": ("bad-id", "'not-a-swap' is not a swap id"),
        }
        for case, (fault, message) in cases.items():
            with self.subTest(case=case):
                self.setUp()
                self.running_agent()
                self.hold_lease(swap="not-a-swap" if fault == "bad-id" else SWAP)
                self.set_node({"github-runner.exadev.com/desired-role": "server"}, {"github-runner.exadev.com/role-join-node": "server9" if fault == "unknown-join" else "server1"})
                if fault == "no-config":
                    (self.config / "server.yaml").unlink()
                if fault == "datastore":
                    (self.data / "server" / "db").mkdir(parents=True)
                result = self.actuator("promote", "not-a-swap" if fault == "bad-id" else SWAP)
                self.assert_status(result, REFUSED)
                self.assertIn(message, result.stdout)
                self.assertEqual(self.role_file.read_text(), "agent")
                self.assertTrue(self.active("k3s-agent"))

    def test_a_promotion_whose_server_does_not_start_fails_with_one_role_running_at_most(self) -> None:
        self.running_agent()
        self.hold_lease()
        self.set_node({"github-runner.exadev.com/desired-role": "server"}, {"github-runner.exadev.com/role-join-node": "server1"})
        result = self.actuator("promote", SWAP, FAKE_RESTART_FAILS="1")
        self.assert_status(result, FAILED)
        self.assertEqual(self.report()["phase"], "failed")
        self.assertEqual(self.report()["message"], "the server unit did not start")
        self.assertEqual(self.role_file.read_text(), "server")
        self.assertFalse(self.active("k3s-agent"))
        self.assertFalse(self.active("k3s"))


class StepDownTest(ActuatorHarness):
    def test_a_step_down_runs_the_server_at_etcd_only_then_names_the_agent_and_stops_regrading(self) -> None:
        self.running_server()
        (self.host / "active-fleet-regrade").touch()
        self.hold_lease()
        result = self.actuator("step-down", SWAP)
        self.assert_status(result, DONE)
        # Restarted while the role file still named the server, so its condition let it start, at etcd-only.
        self.assertTrue(self.active("k3s"))
        self.assertEqual(self.started_file.read_text().strip(), "etcd-only")
        self.assertIn("disable-apiserver: true", (self.config / "server.yaml.d" / "50-regrade.yaml").read_text())
        self.assertNotIn("k3s skipped by its condition", (self.host / "units").read_text())
        self.assertEqual(self.role_file.read_text(), "agent")
        self.assertFalse(self.active("fleet-regrade"))
        self.assertEqual({key: self.report()[key] for key in ("step", "phase", "role")}, {"step": "step-down", "phase": "done", "role": "agent"})
        # The member is the controller's to remove: the server keeps running at etcd-only with its datastore.
        self.assertTrue((self.data / "server" / "db").is_dir())

    def test_a_step_down_whose_etcd_does_not_come_back_fails_with_the_role_file_still_naming_the_server(self) -> None:
        self.running_server()
        self.hold_lease()
        result = self.actuator("step-down", SWAP, FAKE_NO_ETCD="1")
        self.assert_status(result, FAILED)
        self.assertEqual(self.role_file.read_text(), "server")
        self.assertEqual(self.report()["phase"], "failed")

    def test_a_step_down_without_the_lease_is_refused(self) -> None:
        self.running_server()
        result = self.actuator("step-down", SWAP)
        self.assert_status(result, REFUSED)
        self.assertEqual(self.role_file.read_text(), "server")
        self.assertEqual(self.started_file.read_text().strip(), "full")

    def test_a_role_file_that_names_no_role_is_refused(self) -> None:
        self.running_server()
        self.role_file.write_text("voter")
        self.hold_lease()
        result = self.actuator("step-down", SWAP)
        self.assert_status(result, REFUSED)
        self.assertIn("the role file names 'voter', which is not a role", result.stdout)


class FinishDemoteTest(ActuatorHarness):
    def stepped_down(self) -> None:
        """A server the actuator has stepped down: at etcd-only, its role file naming the agent, a level recorded and a swap's join drop-ins in place."""
        self.running_server(level="etcd-only")
        self.role_file.write_text("agent")
        (self.config / "server.yaml.d" / "20-fleet-role-join.yaml").write_text("server: x\n")
        (self.units / "k3s.service.d").mkdir()
        (self.units / "k3s.service.d" / "20-fleet-role-join.conf").write_text("[Service]\n")

    def assert_finished(self) -> None:
        self.assertFalse(self.active("k3s"))
        self.assertFalse((self.host / "enabled-k3s").exists())
        self.assertTrue(self.active("k3s-agent"))
        self.assertFalse((self.data / "server" / "db").exists())
        self.assertEqual((self.data / "server" / "db.demoted" / "etcd" / "name").read_text(), f"{NODE}-0a1b2c3d")
        self.assertFalse(self.level_file.exists())
        self.assertFalse((self.config / "server.yaml.d" / "20-fleet-role-join.yaml").exists())
        self.assertFalse((self.units / "k3s.service.d" / "20-fleet-role-join.conf").exists())
        self.assert_never_both_running()

    def test_a_demotion_is_finished_without_the_lease_once_k3s_records_the_members_removal(self) -> None:
        self.stepped_down()
        self.set_node({"node-role.kubernetes.io/etcd": "true"}, {"etcd.k3s.cattle.io/removed-node-name": f"{NODE}-0a1b2c3d"})
        result = self.actuator("finish-demote")
        self.assert_status(result, DONE)
        self.assert_finished()
        report = self.report()
        self.assertEqual({key: report[key] for key in ("step", "phase", "role")}, {"step": "finish-demote", "phase": "done", "role": "agent"})
        self.assertTrue(report["started_at"])

    def test_a_tombstone_counts_as_removed_with_no_api_to_ask(self) -> None:
        self.stepped_down()
        (self.data / "server" / "db" / "etcd" / "tombstone").write_text("")
        self.no_api()
        self.assert_status(self.actuator("finish-demote"), DONE)
        self.assert_finished()

    def test_a_node_with_no_etcd_label_counts_as_removed(self) -> None:
        self.stepped_down()
        self.set_node({}, {})
        self.assert_status(self.actuator("finish-demote"), DONE)
        self.assert_finished()

    def test_a_demotion_is_not_finished_while_the_member_stays_and_nothing_is_reported(self) -> None:
        self.stepped_down()
        cases = {
            "a member still listed": lambda: self.set_node({"node-role.kubernetes.io/etcd": "true"}, {"etcd.k3s.cattle.io/removed-node-name": "another-member"}),
            "no API to ask": self.no_api,
        }
        for case, arrange in cases.items():
            with self.subTest(case=case):
                arrange()
                result = self.actuator("finish-demote")
                self.assert_status(result, NOT_YET)
                self.assertTrue(self.active("k3s"))
                self.assertTrue((self.data / "server" / "db").is_dir())
                self.assertEqual(self.patches(), 0)

    def test_finishing_keeps_only_the_newest_datastore_copy(self) -> None:
        self.stepped_down()
        older = self.data / "server" / "db.demoted" / "etcd" / "name"
        older.parent.mkdir(parents=True)
        older.write_text("from-an-earlier-demotion")
        (self.data / "server" / "db" / "etcd" / "tombstone").write_text("")
        self.assert_status(self.actuator("finish-demote"), DONE)
        self.assertEqual(older.read_text(), f"{NODE}-0a1b2c3d")
        self.assertEqual(sorted(path.name for path in (self.data / "server").iterdir()), ["db.demoted"])

    def test_finishing_a_host_whose_role_file_names_the_server_is_refused(self) -> None:
        self.running_server()
        (self.data / "server" / "db" / "etcd" / "tombstone").write_text("")
        result = self.actuator("finish-demote")
        self.assert_status(result, REFUSED)
        self.assertTrue((self.data / "server" / "db").is_dir())


class RollbackAndSnapshotTest(ActuatorHarness):
    def test_a_rollback_takes_a_learner_back_to_the_agent(self) -> None:
        self.running_server()
        (self.config / "server.yaml.d" / "20-fleet-role-join.yaml").write_text("server: x\n")
        self.hold_lease()
        self.assert_status(self.actuator("rollback", SWAP), DONE)
        self.assertEqual(self.role_file.read_text(), "agent")
        self.assertFalse(self.active("k3s"))
        self.assertTrue(self.active("k3s-agent"))
        self.assertTrue((self.data / "server" / "db.demoted").is_dir())
        self.assertFalse((self.config / "server.yaml.d" / "20-fleet-role-join.yaml").exists())
        self.assertEqual(self.report()["step"], "rollback")

    def test_a_rollback_without_the_lease_is_refused(self) -> None:
        self.running_server()
        self.assert_status(self.actuator("rollback", SWAP), REFUSED)
        self.assertTrue(self.active("k3s"))
        self.assertEqual(self.role_file.read_text(), "server")

    def test_a_voter_takes_the_snapshot_named_for_the_swap_and_reports_its_file(self) -> None:
        self.running_server()
        self.hold_lease()
        self.assert_status(self.actuator("snapshot", SWAP), DONE)
        self.assertEqual((self.host / "snapshots").read_text().split(), ["--data-dir", str(self.data), "--etcd-server", "https://127.0.0.1:6443", "--name", f"fleet-role-{SWAP}"])
        self.assertEqual(self.report()["snapshot"], f"fleet-role-{SWAP}-testnode-1767225600")
        self.assertEqual(self.report()["phase"], "done")

    def test_a_snapshot_the_server_refuses_fails_and_one_asked_of_an_agent_is_refused(self) -> None:
        self.running_server()
        self.hold_lease()
        result = self.actuator("snapshot", SWAP, FAKE_SNAPSHOT_FAILS="1")
        self.assert_status(result, FAILED)
        self.assertIn("Internal Server Error", result.stderr)
        self.assertEqual(self.report()["snapshot"], "")
        self.setUp()
        self.running_agent()
        self.hold_lease()
        self.assert_status(self.actuator("snapshot", SWAP), REFUSED)
        self.assertFalse((self.host / "snapshots").exists())


class CommandLineTest(ActuatorHarness):
    def test_status_prints_the_role_the_units_the_datastore_and_the_last_report(self) -> None:
        self.running_server()
        self.hold_lease()
        self.assert_status(self.actuator("snapshot", SWAP), DONE)
        result = self.actuator("status")
        self.assert_status(result, DONE)
        self.assertIn("role file: server", result.stdout)
        self.assertIn("server unit: running; agent unit: stopped", result.stdout)
        self.assertIn(f"datastore: in place, member {NODE}-0a1b2c3d; moved aside: no", result.stdout)
        self.assertIn("joins: the join configuration the role wrote", result.stdout)
        self.assertIn('"step":"snapshot"', result.stdout)

    def test_a_command_it_does_not_have_and_a_missing_argument_are_refused(self) -> None:
        for args in (("leave",), ("promote",), ("finish-demote", SWAP), ()):
            with self.subTest(args=args):
                result = self.actuator(*args)
                self.assert_status(result, REFUSED)
                self.assertIn("usage: fleet-role-actuator", result.stderr)

    def test_a_switch_waits_for_the_hosts_lock_and_fails_when_another_holds_it_too_long(self) -> None:
        self.running_agent()
        with (self.data / "fleet-role.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.actuator("promote-local")
        self.assert_status(result, FAILED)
        self.assertIn("another role switch on this host held its lock for longer than 2s", result.stdout)
        self.assertEqual(self.role_file.read_text(), "agent")
        self.assertTrue(self.active("k3s-agent"))

    def test_the_local_promotion_without_a_join_url_leaves_the_roles_own_join_configuration_to_decide(self) -> None:
        self.running_agent()
        (self.config / "server.yaml.d" / "20-fleet-role-join.yaml").write_text("server: x\n")
        (self.units / "k3s.service.d").mkdir()
        (self.units / "k3s.service.d" / "20-fleet-role-join.conf").write_text("[Service]\n")
        self.assert_status(self.actuator("promote-local"), DONE)
        self.assertFalse((self.config / "server.yaml.d" / "20-fleet-role-join.yaml").exists())
        self.assertFalse((self.units / "k3s.service.d" / "20-fleet-role-join.conf").exists())
        self.assertTrue(self.active("k3s"))
        # No report: the role's own switch is not a swap's step.
        self.assertEqual(self.patches(), 0)


class WatchTest(ActuatorHarness):
    """The watcher, run as its unit runs it, against a Node the test changes as the controller would."""

    def watch(self) -> subprocess.Popen[str]:
        process = subprocess.Popen(["bash", str(self.lib / "fleet-role-actuator.sh"), "watch"], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def stop() -> None:
            process.kill()
            process.wait()
            assert process.stdout is not None
            process.stdout.close()

        self.addCleanup(stop)
        return process

    def wait_for_report(self, step: str, phase: str) -> dict[str, Any]:
        # The watcher looks every POLL_SECONDS (2, fleet-regrade-k3s.sh); a step takes a few looks at most against the stand-ins.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            report = self.report()
            if report.get("step") == step and report.get("phase") == phase:
                return report
            time.sleep(0.2)
        self.fail(f"no {step} {phase} report; last: {self.report()}")

    def test_the_watcher_takes_a_swap_through_promotion_step_down_and_finish_once_each(self) -> None:
        self.running_agent()
        self.hold_lease()
        self.watch()
        self.set_node({"github-runner.exadev.com/desired-role": "server"}, {"github-runner.exadev.com/role-swap": SWAP, "github-runner.exadev.com/role-step": "promote", "github-runner.exadev.com/role-join-node": "server1"})
        self.wait_for_report("promote", "done")
        self.assertTrue(self.active("k3s"))
        # k3s leaves a datastore once it has joined.
        (self.data / "server" / "db" / "etcd").mkdir(parents=True)
        (self.data / "server" / "db" / "etcd" / "name").write_text(f"{NODE}-0a1b2c3d")
        self.set_node({"github-runner.exadev.com/desired-role": "agent", "node-role.kubernetes.io/etcd": "true"}, {"github-runner.exadev.com/role-swap": SWAP, "github-runner.exadev.com/role-step": "step-down"})
        self.wait_for_report("step-down", "done")
        self.assertEqual(self.started_file.read_text().strip(), "etcd-only")
        # Nothing more happens until the member is gone: the report stays the step-down's.
        time.sleep(5)
        self.assertEqual(self.report()["step"], "step-down")
        self.set_node({"github-runner.exadev.com/desired-role": "agent", "node-role.kubernetes.io/etcd": "true"}, {"github-runner.exadev.com/role-swap": SWAP, "github-runner.exadev.com/role-step": "step-down", "etcd.k3s.cattle.io/removed-node-name": f"{NODE}-0a1b2c3d"})
        self.wait_for_report("finish-demote", "done")
        self.assertTrue(self.active("k3s-agent"))
        self.assertFalse(self.active("k3s"))
        self.assertTrue((self.data / "server" / "db.demoted").is_dir())
        self.assert_never_both_running()
        units = (self.host / "units").read_text().splitlines()
        self.assertEqual([line for line in units if line.endswith(" started")], ["k3s started", "k3s started", "k3s-agent started"])

    def test_the_watcher_leaves_a_refused_step_to_the_controller_once_its_reason_is_gone(self) -> None:
        self.running_agent()
        self.watch()
        self.set_node({"github-runner.exadev.com/desired-role": "server"}, {"github-runner.exadev.com/role-swap": SWAP, "github-runner.exadev.com/role-step": "promote", "github-runner.exadev.com/role-join-node": "server1"})
        self.wait_for_report("promote", "refused")
        # The Lease the step lacked is now held, but what follows a refusal is the controller's to decide: a new swap or step, never the watcher taking the old one again.
        self.hold_lease()
        time.sleep(3 * WATCH_POLL_SECONDS)
        self.assertEqual(self.report()["phase"], "refused")
        self.assertEqual(self.role_file.read_text(), "agent")
        self.assertTrue(self.active("k3s-agent"))
        self.assertFalse(self.active("k3s"))

    def test_the_watcher_refuses_a_step_its_desired_role_does_not_go_with(self) -> None:
        self.running_agent()
        self.hold_lease()
        self.watch()
        self.set_node({"github-runner.exadev.com/desired-role": "agent"}, {"github-runner.exadev.com/role-swap": SWAP, "github-runner.exadev.com/role-step": "promote", "github-runner.exadev.com/role-join-node": "server1"})
        report = self.wait_for_report("promote", "refused")
        self.assertIn("desired-role is 'agent', which a promote does not go with", report["message"])
        self.assertTrue(self.active("k3s-agent"))
        self.assertEqual(self.role_file.read_text(), "agent")


class RoleSetupTest(NativeRoleHarness):
    """The role's setup of the actuator on a native host, with the role controller on and every host eligible."""

    ON = {"github_runner_cluster_role_controller": True, "github_runner_cluster_voter_eligible": True}

    def run_eligible(self, *args: str, check: bool = False, **laptop: Any) -> subprocess.CompletedProcess[str]:
        """Run laptop as an eligible native agent beside the Docker servers server1 and server2, every host eligible."""
        return self.run_role(*args, host_vars={"github_runner_cluster_node_role": "agent", **self.ON, **laptop}, check=check, second_server=True, group_vars=self.ON)

    def lib(self, name: str) -> Path:
        return self.paths["github_runner_cluster_native_lib_dir"] / "role" / name

    def test_an_eligible_host_runs_the_watcher_with_its_own_credential_and_settings(self) -> None:
        result = self.run_eligible()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        unit = self.file("github_runner_cluster_native_unit_dir", "fleet-role-actuator.service").read_text()
        self.assertIn(f"ExecStart={self.lib('fleet-role-actuator.sh')} watch", unit)
        self.assertIn(f'Environment="FLEET_ROLE_ACTUATOR_ENV={self.lib("fleet-role-actuator.env")}"', unit)
        self.assertIn("systemctl start fleet-role-actuator", self.calls())
        settings = dict(line.split("=", 1) for line in self.lib("fleet-role-actuator.env").read_text().splitlines() if line and not line.startswith("#"))
        self.assertEqual(settings["NODE_NAME"], "laptop")
        self.assertEqual(settings["KUBE_SERVERS"], f'"https://server1.{DOMAIN}:6443 https://server2.{DOMAIN}:6443"')
        self.assertEqual(settings["ACTUATOR_NAMESPACE"], "github-runner-platform")
        self.assertEqual(self.lib("join-urls").read_text(), f"server1 https://server1.{DOMAIN}:6443\nserver2 https://server2.{DOMAIN}:6443\n")
        for name in ("fleet-role-actuator.sh", "fleet-role-actuator-systemd.sh"):
            self.assertEqual(self.lib(name).read_text(), (FILES / "role" / name).read_text())
        for name in ("fleet-regrade-core.sh", "fleet-regrade-k3s.sh", "fleet-regrade-systemd.sh"):
            self.assertEqual(self.lib(name).read_text(), (FILES / "regrade" / name).read_text())
        kubeconfig_path = self.file("github_runner_cluster_native_config_dir", "fleet-role-actuator.kubeconfig")
        self.assertEqual(stat.S_IMODE(kubeconfig_path.stat().st_mode), 0o600)
        kubeconfig = yaml.safe_load(kubeconfig_path.read_text())
        self.assertEqual(kubeconfig["users"], [{"name": "fleet-role-actuator", "user": {"token": ACTUATOR_TOKEN}}])
        self.assertEqual(kubeconfig["clusters"][0]["cluster"], {"server": f"https://server1.{DOMAIN}:6443", "certificate-authority-data": base64.b64encode(b"cluster-ca").decode()})
        # The credential was applied, and the ConfigMap created, through a Docker server's container, since this host's own k3s may be the one a swap stops.
        self.assertIn("docker exec --interactive github-runner-k3s kubectl apply --filename -", self.calls())
        self.assertIn("docker exec github-runner-k3s kubectl create configmap fleet-role-actuators --namespace github-runner-platform", self.calls())

    def test_the_token_stays_out_of_everything_the_role_prints(self) -> None:
        result = self.run_eligible("-vvv")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        encoded = base64.b64encode(ACTUATOR_TOKEN.encode()).decode()
        for text in (result.stdout, result.stderr, (self.stubs / "calls.log").read_text()):
            self.assertNotIn(ACTUATOR_TOKEN, text)
            self.assertNotIn(encoded, text)

    def test_the_credential_reads_nodes_and_the_lease_and_patches_its_report_and_nothing_more(self) -> None:
        self.assertEqual(self.run_eligible().returncode, 0)
        documents = [document for document in yaml.safe_load_all((self.state / "applied").read_text()) if document]
        kinds = {(document["kind"], document["metadata"].get("namespace", "")): document for document in documents}
        self.assertEqual(sorted(kinds), sorted([("Namespace", ""), ("ServiceAccount", "github-runner-platform"), ("Secret", "github-runner-platform"), ("ClusterRole", ""), ("ClusterRoleBinding", ""), ("Role", "github-runner-platform"), ("RoleBinding", "github-runner-platform"), ("Role", "kube-system"), ("RoleBinding", "kube-system")]))
        self.assertEqual(kinds[("ClusterRole", "")]["rules"], [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list", "watch"]}])
        self.assertEqual(kinds[("Role", "github-runner-platform")]["rules"], [{"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["fleet-role-actuators"], "verbs": ["get", "patch"]}])
        self.assertEqual(kinds[("Role", "kube-system")]["rules"], [{"apiGroups": ["coordination.k8s.io"], "resources": ["leases"], "resourceNames": ["fleet-regrade"], "verbs": ["get"]}])
        secret = kinds[("Secret", "github-runner-platform")]
        self.assertEqual((secret["type"], secret["metadata"]["annotations"]), ("kubernetes.io/service-account-token", {"kubernetes.io/service-account.name": "fleet-role-actuator"}))
        for binding in ("ClusterRoleBinding", "RoleBinding"):
            for document in documents:
                if document["kind"] == binding:
                    self.assertEqual(document["subjects"], [{"kind": "ServiceAccount", "name": "fleet-role-actuator", "namespace": "github-runner-platform"}])

    def test_a_host_the_controller_may_not_switch_keeps_the_scripts_and_runs_no_watcher(self) -> None:
        self.assertEqual(self.run_eligible().returncode, 0)
        result = self.run_role(host_vars={"github_runner_cluster_node_role": "agent"}, second_server=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("systemctl disable --now fleet-role-actuator", self.calls())
        self.assertFalse(self.file("github_runner_cluster_native_unit_dir", "fleet-role-actuator.service").exists())
        self.assertFalse(self.file("github_runner_cluster_native_config_dir", "fleet-role-actuator.kubeconfig").exists())
        self.assertFalse(self.lib("join-urls").exists())
        self.assertTrue(self.lib("fleet-role-actuator.sh").exists())

    def test_a_check_run_reports_the_watcher_and_sets_up_nothing(self) -> None:
        result = self.run_eligible(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("apply the role actuator's credential (fleet-role-actuator in github-runner-platform)", re.sub(r"\s+", " ", result.stdout))
        self.assertFalse((self.state / "applied").exists())
        self.assertFalse(self.file("github_runner_cluster_native_config_dir", "fleet-role-actuator.kubeconfig").exists())

    def test_the_regrade_unit_starts_only_on_a_server_whose_datastore_holds_no_tombstone(self) -> None:
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\nlaptop,True,true\n")
        result = self.run_role(host_vars={"github_runner_cluster_node_role": "server", "github_runner_cluster_regrade": True}, second_server=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        unit = self.file("github_runner_cluster_native_unit_dir", "fleet-regrade.service").read_text()
        lib, data = self.paths["github_runner_cluster_native_lib_dir"], self.paths["github_runner_cluster_native_data_dir"]
        self.assertIn(f"ExecCondition={lib}/fleet-role-is server {data}/fleet-role {data}/server", unit.splitlines())
        # The same condition as the server unit's own.
        server_unit = self.file("github_runner_cluster_native_unit_dir", "k3s.service").read_text()
        self.assertIn(f"ExecCondition={lib}/fleet-role-is server {data}/fleet-role {data}/server", server_unit.splitlines())

    def test_the_actuators_namespace_is_the_arc_roles_platform_namespace(self) -> None:
        arc = yaml.safe_load((ROOT / "roles" / "github_runner_arc" / "defaults" / "main.yml").read_text())
        cluster = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        self.assertEqual(cluster["github_runner_cluster_role_actuator_namespace"], "{{ github_runner_arc_platform_namespace | default('" + arc["github_runner_arc_platform_namespace"] + "') }}")


if __name__ == "__main__":
    unittest.main()
