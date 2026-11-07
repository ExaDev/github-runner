"""Injected lifecycle tests; never start guest services."""

import unittest
import types
from contextlib import contextmanager

import guest_factories
import private_bus


class Process:
    def __init__(self):
        self.events = []
    def poll(self):
        return None
    def terminate(self):
        self.events.append("terminate")
    def wait(self, *, timeout):
        self.events.append(("wait", timeout))


class FactoryTests(unittest.TestCase):
    def test_authority_uses_private_bus_and_reaps_child(self):
        bus, child, calls = Process(), Process(), []
        def popen(command, **kwargs):
            calls.append((command, kwargs))
            return child
        with guest_factories.authority_factory(bus=(private_bus.ADDRESS, bus), timeout=2, popen=popen, ready=lambda *args: None) as result:
            self.assertIs(result, child)
        self.assertIn(private_bus.ADDRESS, calls[0][0])
        self.assertEqual(child.events, ["terminate", ("wait", 2)])

    def test_readiness_uses_explicit_bus(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            return types.SimpleNamespace(returncode=0)
        guest_factories.authority_ready(Process(), private_bus.ADDRESS, 2, run=run)
        self.assertIn("--bus=" + private_bus.ADDRESS, calls[0][0])
        self.assertIn("test.nm.IsolatedControl.Pending", calls[0][0])

    def test_profile_creation_and_cleanup(self):
        calls = []
        class Connection:
            def get_object(self, service, path, **kwargs):
                calls.append(("object", service, str(path)))
                return self
            def AddConnectionUnsaved(self, data, *, timeout):
                calls.append(("add", data, timeout))
                return "/profile/1"
            def Delete(self, *, timeout):
                calls.append(("delete", timeout))
            def close(self):
                calls.append(("close",))
        connection = Connection()
        class DBus:
            bus = types.SimpleNamespace(BusConnection=lambda address: connection)
            Boolean = bool
            Int64 = int
            @staticmethod
            def Dictionary(value, signature):
                return value
            @staticmethod
            def Interface(obj, interface):
                return obj
        with guest_factories.profile_factory(bus=(private_bus.ADDRESS, Process()), daemon=None, timeout=2, dbus_module=DBus) as (path, identifier):
            self.assertEqual(path, "/profile/1")
            self.assertEqual(calls[1][1]["connection"]["uuid"], identifier)
            self.assertFalse(calls[1][1]["connection"]["autoconnect"])
        self.assertEqual(calls[-2:], [("delete", 2), ("close",)])

    def test_daemon_readiness_checks_pid_and_settings(self):
        calls = []
        class Conn:
            def close(self):
                calls.append("close")
        class Bus:
            def GetNameOwner(self, name, *, timeout):
                calls.append("owner")
                return ":1.4"
            def GetConnectionUnixProcessID(self, owner, *, timeout):
                calls.append("pid")
                return 42
        class Settings:
            def ListConnections(self, *, timeout):
                calls.append("settings")
                return []
        process = Process()
        process.pid = 42
        guest_factories.daemon_ready(process, private_bus.ADDRESS, 2, connect=lambda address: (Conn(), Bus(), Settings()))
        self.assertEqual(calls, ["owner", "pid", "settings", "owner", "close"])
        process.pid = 43
        calls.clear()
        with self.assertRaisesRegex(RuntimeError, "unexpected process"):
            guest_factories.daemon_ready(process, private_bus.ADDRESS, 2, connect=lambda address: (Conn(), Bus(), Settings()))
        self.assertNotIn("settings", calls)
        process.pid = 42
        class Unready(Settings):
            def ListConnections(self, *, timeout):
                raise TimeoutError("interface unavailable")
        with self.assertRaises(TimeoutError):
            guest_factories.daemon_ready(process, private_bus.ADDRESS, 2, connect=lambda address: (Conn(), Bus(), Unready()))

    def test_daemon_receives_existing_bus(self):
        bus = (private_bus.ADDRESS, Process())
        @contextmanager
        def daemon(*, bus_factory, shutdown_timeout):
            with bus_factory() as received:
                self.assertIs(received, bus)
                self.assertEqual(shutdown_timeout, 2)
                yield (private_bus.ADDRESS, received[1], "capture")
        calls = []
        with guest_factories.daemon_factory(bus=bus, timeout=2, daemon=daemon, ready=lambda *args: calls.append(args)) as result:
            self.assertEqual(result[2], "capture")
            self.assertEqual(calls, [(bus[1], private_bus.ADDRESS, 2)])

    def test_wrong_bus_refused_before_spawn(self):
        calls = []
        with self.assertRaisesRegex(RuntimeError, "private bus"):
            with guest_factories.authority_factory(bus=("wrong", Process()), timeout=2, popen=lambda *args, **kwargs: calls.append(args)):
                self.fail("must not start")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
