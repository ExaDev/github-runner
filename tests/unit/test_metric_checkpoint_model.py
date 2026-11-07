"""Executable checkpoint model, not evidence of real NetworkManager behaviour.

Assumptions: a checkpoint atomically snapshots all modelled persistent profiles; one checkpoint at a time owns them; expiry restores the snapshot; a committed checkpoint leaves changes persistent. The injected clock is monotonic. NetworkManager daemon restarts, actual D-Bus ordering and profile scope are not modelled.
"""

import importlib.util
import json
import threading
import unittest
from pathlib import Path
from dataclasses import dataclass


PLANNER_PATH = Path(__file__).resolve().parents[2] / "plugins/modules/networkmanager_metric_plan.py"
spec = importlib.util.spec_from_file_location("networkmanager_metric_plan", PLANNER_PATH)
assert spec is not None and spec.loader is not None
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)
transaction_spec = importlib.util.spec_from_file_location("networkmanager_metric_transaction", PLANNER_PATH.parent / "networkmanager_metric_transaction.py")
assert transaction_spec is not None and transaction_spec.loader is not None
metric_transaction = importlib.util.module_from_spec(transaction_spec)
transaction_spec.loader.exec_module(metric_transaction)


class VersionFenceModel:
    """Offline assumptions: daemon checks version after authorisation; every accepted update bumps it, including identical values; owner identifies one daemon lifetime. Unversioned writers are not fenced."""

    def __init__(self):
        self.owner = ":1.1"
        self.version = 2
        self.metric = 600

    def snapshot(self):
        return self.owner, self.version

    def update(self, owner, version, metric):
        if owner != self.owner or (version != 0 and version != self.version):
            return False
        self.metric = metric
        self.version += 1
        return True

    def restart(self):
        self.owner = ":1.2"
        self.version = 2


@dataclass(frozen=True)
class Profile:
    metric: int
    default_route: bool = True


class CheckpointModel:
    def __init__(self, profiles: dict[str, Profile]) -> None:
        self.profiles = profiles.copy()
        self._lock = threading.Lock()
        self._snapshot: dict[str, Profile] | None = None
        self._token: object | None = None
        self._deadline = 0.0

    def _expire(self, now: float) -> None:
        if self._token is not None and now >= self._deadline:
            assert self._snapshot is not None
            self.profiles = self._snapshot
            self._snapshot = None
            self._token = None

    def begin(self, now: float, deadline: float) -> object:
        with self._lock:
            self._expire(now)
            if self._token is not None:
                raise RuntimeError("checkpoint already active")
            if deadline <= now:
                raise ValueError("deadline must be in the future")
            self._snapshot = self.profiles.copy()
            self._token = object()
            self._deadline = deadline
            return self._token

    def set_metric(self, token: object, name: str, metric: int, now: float) -> None:
        with self._lock:
            self._expire(now)
            if token is not self._token or self._token is None:
                raise RuntimeError("checkpoint is not active")
            previous = self.profiles[name]
            self.profiles[name] = Profile(metric, previous.default_route)

    def commit(self, token: object, now: float) -> bool:
        with self._lock:
            self._expire(now)
            if token is not self._token or self._token is None:
                return False
            self._token = None
            self._snapshot = None
            return True

    def expire(self, now: float) -> None:
        with self._lock:
            self._expire(now)


class MetricCheckpointModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = {"ethernet": Profile(100), "wifi": Profile(600)}
        self.model = CheckpointModel(self.original)

    def profiles(self):
        common = {"active": True, "default_route": True, "explicit_route_metric": False, "address_family": "ipv4", "table": 254, "gateway": "192.0.2.1"}
        return [{**common, "kind": "ethernet", "uuid": "ethernet"}, {**common, "kind": "wifi", "uuid": "wifi"}]

    def rules(self):
        def rule(priority, action, table, mark=0, mask=0, invert=False):
            return {"priority": priority, "mark": mark, "mask": mask, "invert": invert, "action": action, "table": table}
        return [rule(0, "lookup", 255), rule(5210, "lookup", 254, 0x80000, 0xff0000), rule(5230, "lookup", 253, 0x80000, 0xff0000), rule(5250, "unreachable", 0, 0x80000, 0xff0000), rule(5270, "lookup", 52), rule(32766, "lookup", 254)]

    def candidate(self, profiles):
        return planner.plan(profiles, self.rules(), 0, {255: "miss", 52: "miss", 254: "main-default", 253: "miss"})

    def test_planned_metrics_rollback_with_checkpoint_model(self) -> None:
        candidates = self.candidate(self.profiles())
        self.assertEqual(candidates, [{"uuid": "ethernet", "metric": 100}, {"uuid": "wifi", "metric": 20101}])
        token = self.model.begin(0, 10)
        for candidate in candidates:
            self.model.set_metric(token, candidate["uuid"], candidate["metric"], 1)
        self.assertTrue(self.model.profiles["wifi"].default_route)
        self.model.expire(10)
        self.assertEqual(self.model.profiles, self.original)

    def test_planner_rejects_unestablished_assumptions(self) -> None:
        for field, value in (("active", False), ("default_route", False), ("explicit_route_metric", True), ("address_family", "ipv6"), ("table", 100), ("gateway", ""), ("uuid", "")):
            with self.subTest(field=field):
                profiles = self.profiles()
                profiles[1][field] = value
                with self.assertRaises(ValueError):
                    self.candidate(profiles)
        for field, value in (("gateway", "192.0.2.2"), ("uuid", "ethernet"), ("kind", "ethernet")):
            profiles = self.profiles()
            profiles[1][field] = value
            with self.assertRaises(ValueError):
                self.candidate(profiles)
        with self.assertRaises(ValueError):
            self.candidate(self.profiles()[:1])

    def test_read_only_observation_collects_narrow_profile_fields_and_policy_rules(self) -> None:
        calls = []
        rules = [{"priority": 5210, "fwmark": "0x80000", "fwmask": "0xff0000", "table": "main"}, {"priority": 5230, "fwmark": "0x80000", "fwmask": "0xff0000", "table": "default"}, {"priority": 5250, "fwmark": "0x80000", "fwmask": "0xff0000", "action": "unreachable"}, {"priority": 5270, "table": 52}]
        routes = [{"dst": "default", "gateway": "192.0.2.1", "dev": "eth0", "metric": 100, "table": "main"}, {"dst": "default", "gateway": "192.0.2.1", "dev": "wlan0", "metric": 600, "table": "main"}]
        def run(argv):
            calls.append(argv)
            if argv[:3] == ["ip", "-j", "-4"]:
                return json.dumps(rules if "rule" in argv else routes)
            if argv[1] == "--terse":
                return "profile-one:eth0\n"
            return {"connection.uuid": argv[-1], "connection.type": "802-3-ethernet", "ipv4.route-metric": "100", "ipv4.route-table": "0", "ipv4.never-default": "no", "ipv4.routes": ""}[argv[2]]
        result = planner.collect_observations(run, ["profile-one"])
        self.assertEqual(result["rules"], rules)
        self.assertEqual(result["routes"], routes)
        self.assertEqual(result["profiles"][0]["ipv4.route-metric"], "100")
        self.assertEqual(result["profiles"][0]["device"], "eth0")
        self.assertEqual(len(calls), 9)
        self.assertTrue(all("password" not in " ".join(call) and "secrets" not in " ".join(call) for call in calls))
        self.assertTrue(all(call[0] in ("ip", "nmcli") for call in calls))

    def test_observation_refuses_invalid_inputs_and_invalid_json(self) -> None:
        with self.assertRaises(ValueError):
            planner.collect_observations(lambda argv: "", ["--bad"])
        with self.assertRaises(ValueError):
            planner.collect_observations(lambda argv: "{}", [])

    def test_active_association_mismatch_is_rejected(self) -> None:
        def run(argv):
            if argv[:3] == ["ip", "-j", "-4"]:
                return "[]"
            if argv[1] == "--terse":
                return "other:eth0\n"
            return "requested" if argv[2] == "connection.uuid" else "value"
        with self.assertRaisesRegex(ValueError, "not uniquely active"):
            planner.collect_observations(run, ["requested"])

    def test_destination_outcomes_change_with_routes(self) -> None:
        routes = [{"dst": "default", "dev": "eth0", "metric": 100}, {"dst": "10.42.0.0/24", "table": "52", "dev": "tailscale0"}]
        self.assertEqual(planner.destination_tables(routes, "203.0.113.1"), {"main": "main-default"})
        self.assertEqual(planner.destination_tables(routes, "10.42.0.2"), {"main": "main-default", "52": "other-route"})
        routes.append({"dst": "203.0.113.0/24", "dev": "wlan0"})
        self.assertEqual(planner.destination_tables(routes, "203.0.113.1"), {"main": "other-route"})
        routes[-1]["multipath"] = []
        with self.assertRaises(ValueError):
            planner.destination_tables(routes, "203.0.113.1")

    def test_evaluate_observed_rules_and_device_defaults(self) -> None:
        observations = {"profiles": [{"device": "eth0"}, {"device": "wlan0"}], "rules": [{"priority": 0, "src": "all", "table": "local"}, {"priority": 5270, "src": "all", "table": "52"}, {"priority": 32766, "src": "all", "table": "main"}], "routes": [{"dst": "default", "dev": "eth0", "gateway": "192.0.2.1"}, {"dst": "default", "dev": "wlan0", "gateway": "192.0.2.1"}, {"dst": "10.42.0.0/24", "dev": "tailscale0", "table": "52"}]}
        self.assertTrue(planner.evaluate_observations(observations, "203.0.113.1", 0))
        self.assertFalse(planner.evaluate_observations(observations, "10.42.0.1", 0))
        observations["profiles"][1]["device"] = "missing"
        with self.assertRaises(ValueError):
            planner.evaluate_observations(observations, "203.0.113.1", 0)

    def test_changed_snapshots_are_rejected(self) -> None:
        from unittest.mock import patch
        for field in ("rules", "routes"):
            first = {"profiles": [], "rules": [], "routes": []}
            second = {"profiles": [], "rules": [], "routes": []}
            second[field] = [{"changed": True}]
            with patch.object(planner, "collect_observations", side_effect=[first, second]):
                with self.assertRaisesRegex(ValueError, "changed"):
                    planner.collect_consistent(lambda argv: "", [])

    def test_policy_rules_select_per_flow_table(self) -> None:
        tables = {255: "miss", 52: "miss", 254: "main-default", 253: "miss"}
        self.assertTrue(planner.controls_destination(self.rules(), 0, tables))
        self.assertTrue(planner.controls_destination(self.rules(), 0x80000, tables))
        self.assertFalse(planner.controls_destination(self.rules(), 0, {**tables, 52: "other-route"}))
        self.assertFalse(planner.controls_destination(self.rules(), 0x80000, {**tables, 254: "miss"}))
        self.assertFalse(planner.controls_destination(self.rules(), 0, {**tables, 254: "other-route"}))
        with self.assertRaises(ValueError):
            planner.controls_destination(self.rules(), 0, {255: "miss"})
        rules = self.rules()
        rules[0]["source"] = "192.0.2.0/24"
        with self.assertRaises(ValueError):
            planner.controls_destination(rules, 0, tables)

    def test_adapter_is_bound_to_one_daemon_process_by_its_unique_name(self) -> None:
        from unittest.mock import MagicMock
        dbus = MagicMock()
        dbus.Interface.side_effect = lambda obj, name: obj
        live = {":1.5"}
        reached = []

        class Gone(Exception):
            pass

        class Bus:
            def get_name_owner(self, service):
                return ":1.5"

            def get_object(self, name, path):
                if name not in live:
                    raise Gone("ServiceUnknown: " + name)
                reached.append(name)
                return MagicMock()

        adapter = metric_transaction.NetworkManagerAdapter(Bus(), dbus)
        adapter.destroy_checkpoint("/checkpoint")
        self.assertEqual(reached, [":1.5"])
        # The daemon restarts: the well-known name now belongs to a new process, and the old unique name is gone.
        live.clear()
        live.add(":1.34")
        with self.assertRaisesRegex(Gone, "ServiceUnknown: :1.5"):
            adapter.destroy_checkpoint("/checkpoint")
        for owner in ("org.freedesktop.NetworkManager", "", None, 5):
            bus = MagicMock()
            bus.get_name_owner.return_value = owner
            with self.assertRaisesRegex(ValueError, "unique bus name"):
                metric_transaction.NetworkManagerAdapter(bus, dbus)

    def test_dbus_adapter_profile_checkpoint_and_errors(self) -> None:
        from unittest.mock import MagicMock
        dbus = MagicMock()
        dbus.Interface.side_effect = lambda obj, name: obj
        dbus.UInt32.side_effect = int
        dbus.UInt64.side_effect = lambda value, **kwargs: int(value)
        dbus.Int64.side_effect = lambda value, **kwargs: value
        dbus.Array.side_effect = lambda value, **kwargs: value
        dbus.Dictionary.side_effect = lambda value, **kwargs: value
        bus = MagicMock()
        bus.get_name_owner.return_value = ":1.5"
        manager, settings, connection, device = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        paths = {"/org/freedesktop/NetworkManager": manager, "/org/freedesktop/NetworkManager/Settings": settings, "/profile": connection, "/device": device}
        bus.get_object.side_effect = lambda service, path: paths[path]
        settings.GetConnectionByUuid.return_value = "/profile"
        connection.Get.return_value = 17
        connection.GetSettings.return_value = {"connection": {"id": "fixture"}, "ipv4": {"route-metric": -1}}
        manager.CheckpointCreate.return_value = "/checkpoint"
        manager.CheckpointRollback.return_value = {"/device": 0}
        adapter = metric_transaction.NetworkManagerAdapter(bus, dbus)
        self.assertEqual(adapter.create_checkpoint(["/device"], 30, 0), "/checkpoint")
        manager.CheckpointCreate.assert_called_once_with(["/device"], 30, 0)
        adapter.set_metric("fixture-uuid", 20101)
        self.assertEqual(connection.Update2.call_args.args[0]["ipv4"]["route-metric"], 20101)
        self.assertEqual(connection.Update2.call_args.args[0]["connection"]["id"], "fixture")
        self.assertEqual(connection.Update2.call_args.args[1:], (1, {"version-id": 17}))
        connection.Update2.side_effect = RuntimeError("version mismatch")
        with self.assertRaisesRegex(RuntimeError, "version mismatch"):
            adapter.set_metric("fixture-uuid", 100)
        device.GetAppliedConnection.return_value = ({"connection": {"uuid": "fixture-uuid"}, "ipv4": {"route-metric": -1}}, 42)
        applied, version = adapter.inspect_device("/device", "fixture-uuid")
        with self.assertRaises(ValueError):
            adapter.inspect_device("/device", "other")
        adapter.reapply("/device", "fixture-uuid", 20101, applied, version)
        device.Reapply.assert_called_once_with(applied, 42, 1)
        adapter.rollback_checkpoint("/checkpoint", ["/device"])
        manager.CheckpointRollback.return_value = {}
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            adapter.rollback_checkpoint("/checkpoint", ["/device"])
        manager.CheckpointRollback.return_value = {"/device": 3}
        with self.assertRaisesRegex(RuntimeError, "incomplete or failed"):
            adapter.rollback_checkpoint("/checkpoint", ["/device"])
        with self.assertRaises(ValueError):
            adapter.create_checkpoint(["/device"], 0, 0)
        manager.CheckpointCreate.side_effect = RuntimeError("overlapping checkpoint")
        with self.assertRaisesRegex(RuntimeError, "overlapping"):
            adapter.create_checkpoint(["/device"], 30, 0)

    def test_protocol_retains_original_and_rollback_failures(self) -> None:
        from unittest.mock import MagicMock
        adapter = MagicMock()
        adapter.inspect_device.side_effect = ValueError("wrong profile")
        adapter.rollback_checkpoint.side_effect = RuntimeError("rollback incomplete")
        profiles = [{"device": "/a", "uuid": "a", "metric": 100}, {"device": "/b", "uuid": "b", "metric": 20101}]
        with self.assertRaises(ExceptionGroup) as raised:
            metric_transaction._execute_for_test(adapter, profiles, 30, lambda: True)
        self.assertEqual([str(e) for e in raised.exception.exceptions], ["wrong profile", "rollback incomplete"])
        adapter.set_metric.assert_not_called()
        adapter.rollback_checkpoint.assert_called_once_with(adapter.create_checkpoint.return_value, ["/a", "/b"])

    def test_version_fence_rejects_delayed_authorisation(self) -> None:
        model = VersionFenceModel()
        owner, version = model.snapshot()
        self.assertTrue(model.update(owner, version, 600))
        self.assertEqual(model.version, version + 1)
        self.assertFalse(model.update(owner, version, 20101))
        self.assertEqual(model.metric, 600)

    def test_version_reuse_after_restart_requires_owner_identity(self) -> None:
        model = VersionFenceModel()
        owner, version = model.snapshot()
        model.restart()
        self.assertEqual(model.version, version)
        self.assertFalse(model.update(owner, version, 20101))
        self.assertTrue(model.update(model.owner, version, 20101))

    def test_unversioned_writer_bypasses_version_fence(self) -> None:
        model = VersionFenceModel()
        owner, version = model.snapshot()
        self.assertTrue(model.update(owner, version, 600))
        self.assertTrue(model.update(owner, 0, 20101))
        self.assertEqual(model.metric, 20101)

    def test_late_daemon_write_can_follow_offline_recovery(self) -> None:
        """Model a request accepted before writer death but executed after recovery."""
        durable = {"phase": "prepared", "before": self.original.copy()}
        profile_store = self.original.copy()
        pending_daemon_write = ("wifi", Profile(20101))
        durable["phase"] = "rolling_back"
        profile_store.update(durable["before"])
        durable["phase"] = "rolled_back"
        name, value = pending_daemon_write
        profile_store[name] = value
        self.assertEqual(durable["phase"], "rolled_back")
        self.assertNotEqual(profile_store, durable["before"])
        self.assertEqual(profile_store["wifi"].metric, 20101)

    def test_guarded_transaction_never_calls_adapter(self) -> None:
        class Forbidden:
            def __getattribute__(self, name):
                raise AssertionError("host adapter was touched")
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            metric_transaction.execute(Forbidden(), [], 10, lambda: True)

    def test_checkpoint_protocol_competitor_and_timeout(self) -> None:
        model = self.model
        now = [0]
        class Adapter:
            def create_checkpoint(self, devices, timeout, flags):
                self.assertion = (devices, timeout, flags)
                return model.begin(now[0], now[0] + timeout)
            def inspect_device(self, device, uuid):
                return {}, 1
            def set_metric(self, uuid, metric):
                model.set_metric(self.token, uuid, metric, now[0])
            def reapply(self, device, uuid, metric, applied, version):
                pass
            def destroy_checkpoint(self, checkpoint):
                if not model.commit(checkpoint, now[0]):
                    raise RuntimeError("checkpoint expired")
            def rollback_checkpoint(self, checkpoint, devices):
                model.expire(now[0])
        profiles = [{"device": "eth0", "uuid": "ethernet", "metric": 100}, {"device": "wlan0", "uuid": "wifi", "metric": 20101}]
        existing = model.begin(0, 10)
        with self.assertRaisesRegex(RuntimeError, "already active"):
            metric_transaction._execute_for_test(Adapter(), profiles, 10, lambda: True)
        self.assertTrue(model.commit(existing, 1))
        adapter = Adapter()
        create = adapter.create_checkpoint
        def create_with_token(devices, timeout, flags):
            adapter.token = create(devices, timeout, flags)
            return adapter.token
        adapter.create_checkpoint = create_with_token
        def expire_before_commit():
            now[0] = 10
            return True
        with self.assertRaisesRegex(RuntimeError, "expired"):
            metric_transaction._execute_for_test(adapter, profiles, 10, expire_before_commit)
        self.assertEqual(model.profiles, self.original)
        self.assertEqual(adapter.assertion, (["eth0", "wlan0"], 10, 0))

    def test_expiry_restores_both_persistent_profiles(self) -> None:
        token = self.model.begin(0, 10)
        self.model.set_metric(token, "ethernet", 100, 1)
        self.model.set_metric(token, "wifi", 20101, 2)
        self.model.expire(9)
        self.assertEqual(self.model.profiles["wifi"].metric, 20101)
        self.model.expire(10)
        self.assertEqual(self.model.profiles, self.original)
        self.assertFalse(self.model.commit(token, 10))

    def test_commit_preserves_metrics_and_wifi_default_route(self) -> None:
        token = self.model.begin(0, 10)
        self.model.set_metric(token, "wifi", 20101, 1)
        self.assertTrue(self.model.commit(token, 2))
        self.model.expire(10)
        self.assertEqual(self.model.profiles["wifi"], Profile(20101))
        self.assertLess(self.model.profiles["ethernet"].metric + 20000, self.model.profiles["wifi"].metric)

    def test_competing_transactions_have_one_owner(self) -> None:
        barrier = threading.Barrier(3)
        outcomes: list[object] = []
        def begin() -> None:
            barrier.wait()
            try:
                outcomes.append(self.model.begin(0, 10))
            except RuntimeError:
                outcomes.append(None)
        workers = [threading.Thread(target=begin) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sum(value is not None for value in outcomes), 1)

    def test_expired_owner_cannot_modify_or_commit_new_checkpoint(self) -> None:
        old = self.model.begin(0, 10)
        self.model.set_metric(old, "wifi", 20101, 1)
        new = self.model.begin(10, 20)
        with self.assertRaises(RuntimeError):
            self.model.set_metric(old, "wifi", 1, 11)
        self.assertFalse(self.model.commit(old, 11))
        self.model.set_metric(new, "wifi", 21000, 11)
        self.assertTrue(self.model.commit(new, 12))
        self.assertEqual(self.model.profiles["wifi"].metric, 21000)


if __name__ == "__main__":
    unittest.main()
