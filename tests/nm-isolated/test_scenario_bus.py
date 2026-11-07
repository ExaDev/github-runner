"""Offline injected D-Bus profile and identity tests."""

import io
import unittest

from audit_log import CaptureReader

import scenario_bus
import update2_client


class FakeInterface:
    def __init__(self):
        self.versions = iter([3, 3])
        self.calls = []
        self.name = ":1.7"

    def Get(self, interface, name, *, timeout):
        self.calls.append(("Get", interface, name, timeout))
        return next(self.versions)

    def GetSettings(self, *, timeout):
        self.calls.append(("GetSettings", timeout))
        return {"ipv4": {"route-metric": 600}}

    def GetNameOwner(self, name, *, timeout):
        self.calls.append(("GetNameOwner", name, timeout))
        return self.name


class FakeBus:
    def __init__(self):
        self.interface = FakeInterface()
        self.calls = []

    def get_object(self, service, path, *, introspect):
        self.calls.append((service, path, introspect))
        return self.interface


class FakeDBus:
    @staticmethod
    def Interface(obj, interface):
        return obj


class ScenarioBusTests(unittest.TestCase):
    def test_profile_reads_stable_version_and_metric(self):
        bus = FakeBus()
        adapter = scenario_bus.ScenarioBus(bus, FakeDBus, "/profile", 2)
        self.assertEqual(adapter.profile(), scenario_bus.Profile(600, 3))
        self.assertEqual(bus.calls, [(update2_client.SERVICE, "/profile", False)])
        self.assertEqual([call[0] for call in bus.interface.calls], ["Get", "GetSettings", "Get"])
        self.assertTrue(all(0 < call[-1] <= 2 for call in bus.interface.calls))

    def test_profile_change_is_rejected(self):
        bus = FakeBus()
        bus.interface.versions = iter([3, 4])
        with self.assertRaisesRegex(RuntimeError, "changed"):
            scenario_bus.ScenarioBus(bus, FakeDBus, "/profile", 2).profile()

    def test_lifecycle_ports_share_deadline_and_distinguish_outcomes(self):
        calls = []
        now = [0]
        class Ports:
            def mark(self):
                return 0
            def register(self, request, cursor, pid):
                pass
            def pid(self, request):
                return 123
            def submit_held(self, **kwargs):
                calls.append(("held", kwargs))
                now[0] += 1
                return "request"
            def disconnect(self, request, **kwargs):
                calls.append(("disconnect", request, kwargs))
            def allow_later(self, **kwargs):
                calls.append(("allow", kwargs))
            def update_separately(self, **kwargs):
                calls.append(("separate", kwargs))
            def release(self, request, **kwargs):
                calls.append(("release", request, kwargs))
            def wait(self, request, **kwargs):
                calls.append(("wait", request, kwargs))
                return "version-mismatch"
            def restart(self, **kwargs):
                calls.append(("restart", kwargs))
        ports = Ports()
        adapter = scenario_bus.ScenarioAdapter(None, ports, ports, ports, ports, 5, clock=lambda: now[0])
        request = adapter.submit_held(metric=42, fenced=True)
        adapter.disconnect(request)
        adapter.allow_later()
        adapter.update_separately(metric=600, fenced=True)
        adapter.release(request)
        adapter.await_rejection(request)
        adapter.restart()
        self.assertEqual([c[0] for c in calls], ["held", "disconnect", "allow", "separate", "release", "wait", "restart"])
        self.assertEqual(calls[0][1]["timeout"], 5)
        self.assertEqual(calls[-1][1]["timeout"], 4)
        with self.assertRaisesRegex(AssertionError, "expected applied"):
            adapter.await_completion(request)
        now[0] = 5
        with self.assertRaises(TimeoutError):
            adapter.restart()

    def test_audit_capture_is_marked_before_submission(self):
        line = b'audit: op="connection-update" uuid="id" pid=123 result="success"\n'
        stream = io.BytesIO(line)
        capture = CaptureReader(stream, ":1.4")
        now = [0.0]
        def pause(duration):
            now[0] += duration
        observer = scenario_bus.AuditScenarioObserver(capture, ":1.4", 123, "id", clock=lambda: now[0], pause=pause)
        class Clients:
            def pid(self, request):
                return 123
            def submit_held(self, **kwargs):
                stream.seek(0, 2)
                stream.write(line)
                return "held"
        adapter = scenario_bus.ScenarioAdapter(None, Clients(), None, observer, None, 1, clock=lambda: now[0])
        request = adapter.submit_held(metric=1, fenced=False)
        adapter.await_completion(request)
        self.assertEqual(now[0], 0)
        self.assertGreater(adapter.remaining(), 0)

    def test_audit_rejects_mismatched_launched_pid(self):
        observer = scenario_bus.AuditScenarioObserver(CaptureReader(io.BytesIO(), ":1.4"), ":1.4", 123, "id")
        with self.assertRaisesRegex(ValueError, "PID"):
            observer.register("held", 0, 124)

    def test_audit_missing_or_duplicate_is_inconclusive(self):
        line = b'audit: op="connection-update" uuid="id" pid=123 result="success"\n'
        for data in (b"", line + line):
            with self.subTest(data=data):
                now = [0.0]
                def pause(duration):
                    now[0] += duration
                observer = scenario_bus.AuditScenarioObserver(CaptureReader(io.BytesIO(data), ":1.4"), ":1.4", 123, "id", clock=lambda: now[0], pause=pause)
                observer.register("held", 0, 123)
                self.assertEqual(observer.wait("held", timeout=0.1), "inconclusive")

    def test_owner_reads_unique_name(self):
        bus = FakeBus()
        adapter = scenario_bus.ScenarioBus(bus, FakeDBus, "/profile", 2)
        self.assertEqual(adapter.owner(), ":1.7")
        self.assertEqual(bus.interface.calls, [("GetNameOwner", update2_client.SERVICE, 2)])
        bus.interface.name = "not-unique"
        with self.assertRaisesRegex(RuntimeError, "unique"):
            adapter.owner()


if __name__ == "__main__":
    unittest.main()
