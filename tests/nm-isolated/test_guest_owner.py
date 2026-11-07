"""Injected guest fixture ownership tests; no service is started."""

import unittest
from contextlib import contextmanager

import guest_owner


class GuestOwnerTests(unittest.TestCase):
    def test_lifecycle_order_and_exception_cleanup(self):
        events = []
        def factory(name):
            @contextmanager
            def start(**kwargs):
                events.append("start " + name)
                try:
                    yield name
                finally:
                    events.append("stop " + name)
            return start
        with self.assertRaisesRegex(ValueError, "scenario"):
            with guest_owner.guest_owner(timeout=2, bus_factory=factory("bus"), authority_factory=factory("authority"), daemon_factory=factory("daemon"), profile_factory=factory("profile"), adapter_factory=factory("adapter"), guard=lambda: None) as adapter:
                self.assertEqual(adapter, "adapter")
                raise ValueError("scenario")
        self.assertEqual(events, ["start bus", "start authority", "start daemon", "start profile", "start adapter", "stop adapter", "stop profile", "stop daemon", "stop authority", "stop bus"])

    def test_partial_startup_cleans_prior_resources(self):
        events = []
        @contextmanager
        def bus(**kwargs):
            events.append("start")
            try:
                yield "bus"
            finally:
                events.append("stop")
        @contextmanager
        def fail(**kwargs):
            raise RuntimeError("authority failed")
            yield
        with self.assertRaisesRegex(RuntimeError, "authority failed"):
            with guest_owner.guest_owner(timeout=2, bus_factory=bus, authority_factory=fail, daemon_factory=fail, profile_factory=fail, adapter_factory=fail, guard=lambda: None):
                self.fail("must not enter")
        self.assertEqual(events, ["start", "stop"])

    def test_guard_refuses_before_factories(self):
        calls = []
        def guard():
            raise RuntimeError("unsafe")
        def factory(**kwargs):
            calls.append(kwargs)
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            with guest_owner.guest_owner(timeout=2, bus_factory=factory, authority_factory=factory, daemon_factory=factory, profile_factory=factory, adapter_factory=factory, guard=guard):
                self.fail("must not enter")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
