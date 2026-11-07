"""Offline request construction tests; no D-Bus connection is made."""

import unittest

import update2_client as client


class TypedInt(int):
    pass


class Int64(TypedInt):
    pass


class UInt32(TypedInt):
    pass


class UInt64(TypedInt):
    pass


class Dictionary(dict):
    def __init__(self, values, signature):
        super().__init__(values)
        self.signature = signature


class FakeDBus:
    Int64 = Int64
    UInt32 = UInt32
    UInt64 = UInt64
    Dictionary = Dictionary

    @staticmethod
    def Interface(obj, name):
        return obj.interfaces[name]


class FakeConnection:
    def __init__(self):
        self.settings = {"connection": {"id": "fixture"}, "ipv4": {"route-metric": Int64(600)}}
        self.calls = []

    def GetSettings(self, *, timeout):
        return self.settings

    def Update2(self, *args, **kwargs):
        self.calls.append(args)
        self.handlers = kwargs
        return {"result": "accepted"}


class FakeProperties:
    def __init__(self, version):
        self.version = version
        self.calls = []

    def Get(self, *args, timeout):
        self.calls.append(args)
        return self.version


class FakeBus:
    def __init__(self, version=7):
        self.connection = FakeConnection()
        self.properties = FakeProperties(version)
        self.interfaces = {client.CONNECTION: self.connection, client.PROPERTIES: self.properties}
        self.calls = []

    def get_object(self, *args, **kwargs):
        self.calls.append(args)
        return self

    def close(self):
        self.closed = True


class FakeControl:
    def __init__(self):
        self.pending = []

    def Pending(self, timeout):
        return self.pending


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value

    def tick(self):
        self.value += 1


class Update2Tests(unittest.TestCase):
    def test_metric_update_builds_typed_fenced_request(self):
        bus = FakeBus()
        result = client.update_profile(bus, FakeDBus, "/org/freedesktop/NetworkManager/Settings/1", 20101)
        self.assertEqual(result, {"result": "accepted"})
        self.assertEqual(bus.calls, [(client.SERVICE, "/org/freedesktop/NetworkManager/Settings/1")])
        self.assertEqual(bus.properties.calls, [(client.CONNECTION, "VersionId")])
        settings, flags, args = bus.connection.calls[0]
        self.assertEqual(settings.signature, "sa{sv}")
        self.assertEqual(settings["ipv4"].signature, "sv")
        self.assertEqual(settings["connection"], {"id": "fixture"})
        self.assertIsInstance(settings["ipv4"]["route-metric"], Int64)
        self.assertEqual(settings["ipv4"]["route-metric"], 20101)
        self.assertEqual(bus.connection.settings["ipv4"]["route-metric"], 600)
        self.assertIsInstance(flags, UInt32)
        self.assertEqual(flags, 2)
        self.assertEqual(args.signature, "sv")
        self.assertIsInstance(args["version-id"], UInt64)
        self.assertEqual(args["version-id"], 7)

    def test_omitted_metric_sends_identical_settings(self):
        bus = FakeBus()
        client.update_profile(bus, FakeDBus, "/profile")
        self.assertEqual(bus.connection.calls[0][0], bus.connection.settings)

    def test_submission_uses_one_decreasing_deadline(self):
        bus = FakeBus()
        now = [0]
        def get(*args, timeout):
            self.assertEqual(timeout, 3)
            now[0] += 1
            return 7
        def settings(*, timeout):
            self.assertEqual(timeout, 2)
            now[0] += 1
            return bus.connection.settings
        bus.properties.Get = get
        bus.connection.GetSettings = settings
        client.update_profile(bus, FakeDBus, "/profile", timeout=3, clock=lambda: now[0])
        self.assertEqual(bus.connection.handlers["timeout"], 1)

    def test_explicit_unfenced_control_omits_version_id(self):
        bus = FakeBus(version=0)
        client.update_profile(bus, FakeDBus, "/profile", 10, unfenced=True)
        self.assertEqual(bus.properties.calls, [])
        self.assertEqual(bus.connection.calls[0][2], {})
        self.assertEqual(bus.connection.calls[0][2].signature, "sv")

    def test_zero_version_refuses_unfenced_update(self):
        bus = FakeBus(version=0)
        with self.assertRaises(ValueError):
            client.update_profile(bus, FakeDBus, "/profile")
        self.assertEqual(bus.connection.calls, [])

    def test_disconnect_only_after_authority_holds_request(self):
        bus, control, clock = FakeBus(), FakeControl(), Clock()
        def pump():
            self.assertFalse(getattr(bus, "closed", False))
            self.assertEqual(len(bus.connection.calls), 1)
            control.pending = ["held-update"]
            clock.tick()
        result = client.submit_and_disconnect(bus, control, FakeDBus, "/profile", 10, 3, pump, clock)
        self.assertEqual(result, "held-update")
        self.assertTrue(bus.closed)
        self.assertEqual(set(bus.connection.handlers), {"reply_handler", "error_handler", "timeout"})

    def test_timeout_does_not_disconnect(self):
        bus, control, clock = FakeBus(), FakeControl(), Clock()
        with self.assertRaises(TimeoutError):
            client.submit_and_disconnect(bus, control, FakeDBus, "/profile", 10, 2, clock.tick, clock)
        self.assertFalse(getattr(bus, "closed", False))

    def test_completion_before_hold_does_not_disconnect(self):
        bus, control, clock = FakeBus(), FakeControl(), Clock()
        def pump():
            bus.connection.handlers["error_handler"](RuntimeError("denied"))
            clock.tick()
        with self.assertRaisesRegex(RuntimeError, "completed before"):
            client.submit_and_disconnect(bus, control, FakeDBus, "/profile", 10, 3, pump, clock)
        self.assertFalse(getattr(bus, "closed", False))

    def test_existing_or_ambiguous_requests_are_refused(self):
        bus, control, clock = FakeBus(), FakeControl(), Clock()
        control.pending = ["existing"]
        with self.assertRaisesRegex(RuntimeError, "no pending"):
            client.submit_and_disconnect(bus, control, FakeDBus, "/profile", 10, 3, clock.tick, clock)
        self.assertEqual(bus.connection.calls, [])
        control.pending = []
        def pump():
            control.pending = ["one", "two"]
            clock.tick()
        with self.assertRaisesRegex(RuntimeError, "multiple"):
            client.submit_and_disconnect(bus, control, FakeDBus, "/profile", 10, 3, pump, clock)
        self.assertFalse(getattr(bus, "closed", False))

    def test_bus_errors_propagate(self):
        bus = FakeBus()
        def fail(*args, **kwargs):
            raise RuntimeError("update failed")
        bus.connection.Update2 = fail
        with self.assertRaisesRegex(RuntimeError, "update failed"):
            client.update_profile(bus, FakeDBus, "/profile", 10)


if __name__ == "__main__":
    unittest.main()
