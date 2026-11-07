"""Offline lifecycle port tests with injected child and authority controls."""

import unittest

import lifecycle_ports
from fixture_lifetime import FixtureLifetime


class Child:
    def __init__(self, request="held", code=0):
        self.request = request
        self.code = code
        self.calls = []

    def read_request_id(self, *, timeout):
        self.calls.append(("read", timeout))
        return self.request

    def wait(self, *, timeout):
        self.calls.append(("wait", timeout))
        return self.code

    def close(self):
        self.calls.append(("close",))


class Launcher:
    def __init__(self, *children):
        self.children = iter(children)
        self.calls = []

    def start(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.children)


class Tests(unittest.TestCase):
    def test_held_and_separate_use_distinct_children(self):
        first, second = Child(), Child()
        launcher = Launcher(first, second)
        ports = lifecycle_ports.Clients(launcher)
        request = ports.submit_held(metric=12, fenced=False, timeout=2)
        ports.disconnect(request, timeout=1)
        ports.update_separately(metric=600, fenced=True, timeout=1)
        self.assertEqual(launcher.calls, [{"metric": 12, "unfenced": True, "disconnect_after_held": True, "timeout": 2}, {"metric": 600, "unfenced": False, "disconnect_after_held": False, "timeout": 1}])
        self.assertEqual(first.calls, [("read", 2), ("wait", 1)])
        self.assertEqual(second.calls, [("wait", 1), ("close",)])

    def test_cleanup_closes_held_child_on_success_and_failure(self):
        for code in (0, 1):
            child = Child(code=code)
            ports = lifecycle_ports.Clients(Launcher(child))
            request = ports.submit_held(metric=1, fenced=True, timeout=2)
            try:
                ports.disconnect(request, timeout=1)
            except RuntimeError:
                pass
            finally:
                ports.close()
            self.assertIn(("close",), child.calls)
            self.assertEqual(ports.requests, {})

    def test_failed_child_and_duplicate_are_not_accepted(self):
        first, duplicate = Child(), Child()
        ports = lifecycle_ports.Clients(Launcher(first, duplicate))
        ports.submit_held(metric=1, fenced=True, timeout=2)
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            ports.submit_held(metric=1, fenced=True, timeout=2)
        self.assertIn(("close",), duplicate.calls)
        bad = Child(code=1)
        with self.assertRaisesRegex(RuntimeError, "writer failed"):
            lifecycle_ports.Clients(Launcher(bad)).update_separately(metric=1, fenced=True, timeout=1)
        self.assertIn(("close",), bad.calls)

    def test_reset_timeout_retires_authority(self):
        class Control:
            def AllowLater(self, value, *, timeout):
                raise TimeoutError("reset")
        authority = lifecycle_ports.Authority(Control(), FixtureLifetime(lambda **kwargs: None, 1))
        with self.assertRaises(TimeoutError):
            authority.hold_later(timeout=1)
        with self.assertRaisesRegex(RuntimeError, "retired"):
            authority.allow_later(timeout=1)

    def test_reset_failure_tears_down_pending_and_blocks_aliases(self):
        events = []
        pending = {"held"}
        def teardown(*, timeout):
            events.append(("authority-stop", timeout))
            pending.clear()
            events.append(("bus-stop", timeout))
        class Control:
            def AllowLater(self, value, *, timeout):
                raise TimeoutError("reset")
        lifetime = FixtureLifetime(teardown, 2)
        first = lifecycle_ports.Authority(Control(), lifetime)
        alias = lifecycle_ports.Authority(Control(), lifetime)
        with self.assertRaises(TimeoutError):
            first.hold_later(timeout=1)
        self.assertEqual(pending, set())
        self.assertEqual(events, [("authority-stop", 2), ("bus-stop", 2)])
        with self.assertRaisesRegex(RuntimeError, "retired"):
            alias.allow_later(timeout=1)
        with self.assertRaisesRegex(RuntimeError, "retired"):
            lifecycle_ports.Authority(Control(), lifetime)

    def test_teardown_failure_still_blocks_reuse(self):
        def teardown(*, timeout):
            raise TimeoutError("teardown")
        lifetime = FixtureLifetime(teardown, 2)
        authority = lifecycle_ports.Authority(object(), lifetime)
        with self.assertRaisesRegex(TimeoutError, "teardown"):
            authority.retire()
        self.assertIsInstance(lifetime.teardown_error, TimeoutError)
        with self.assertRaisesRegex(RuntimeError, "retired"):
            lifecycle_ports.Authority(object(), lifetime)

    def test_authority_controls_and_restart_refusal(self):
        calls = []
        class Control:
            def AllowLater(self, value, *, timeout):
                calls.append(("allow", value, timeout))
            def Release(self, request, value, *, timeout):
                calls.append(("release", request, value, timeout))
        authority = lifecycle_ports.Authority(Control(), FixtureLifetime(lambda **kwargs: None, 1))
        authority.allow_later(timeout=2)
        authority.hold_later(timeout=2)
        authority.release("held", timeout=1)
        self.assertEqual(calls, [("allow", True, 2), ("allow", False, 2), ("release", "held", True, 1)])
        with self.assertRaisesRegex(RuntimeError, "inconclusive"):
            lifecycle_ports.Daemon().restart(timeout=1)


if __name__ == "__main__":
    unittest.main()
