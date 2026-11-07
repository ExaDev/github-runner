"""Injected scenario adapter composition tests."""

import types
import unittest
from contextlib import contextmanager

import adapter_factory


class Tests(unittest.TestCase):
    def test_adapter_composition_and_connection_cleanup(self):
        events = []
        class Connection:
            def get_object(self, service, path, **kwargs):
                return self
            def GetNameOwner(self, name, *, timeout):
                return ":1.3"
            def close(self):
                events.append("close")
        connection = Connection()
        class DBus:
            bus = types.SimpleNamespace(BusConnection=lambda address: connection)
            @staticmethod
            def Interface(obj, interface):
                return obj
        with adapter_factory.adapter_factory(bus=("private", object()), authority=object(), daemon=("private", object(), object()), profile=("/profile", "uuid"), timeout=2, dbus_module=DBus, launcher_factory=lambda path: "launcher") as adapter:
            self.assertIs(adapter.authority.control, connection)
        self.assertEqual(events, ["close"])

    def test_bus_mismatch_refuses_before_connection(self):
        with self.assertRaisesRegex(RuntimeError, "buses differ"):
            with adapter_factory.adapter_factory(bus=("one", object()), authority=object(), daemon=("two", object(), object()), profile=("/p", "u"), timeout=2, dbus_module=object()):
                self.fail("must not enter")


if __name__ == "__main__":
    unittest.main()
