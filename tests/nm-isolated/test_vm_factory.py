"""Injected QEMU factory composition tests, without launching processes."""

import unittest
from unittest.mock import patch
from contextlib import contextmanager

import vm_factory


class Socket:
    def __init__(self, events):
        self.events = events
    def close(self):
        self.events.append("socket-close")


class FactoryTests(unittest.TestCase):
    def test_fresh_workspace_readiness_and_cleanup(self):
        events = []
        paths = iter(["/one", "/two"])
        @contextmanager
        def start(base, workspace, **kwargs):
            events.append(("start", workspace, kwargs["shutdown_timeout"]))
            try:
                yield workspace, "overlay", object()
            finally:
                events.append(("stop", workspace))
        def connect(workspace, process, timeout):
            events.append(("connect", workspace, timeout))
            return Socket(events)
        owner = vm_factory.make_owner("/base", lambda: next(paths), qemu="/qemu", qemu_img="/img", attempts=1, timeout=5, vm_start=start, connect_ready=connect, clock=lambda: 0)
        owner.run(lambda socket: None)
        owner.run(lambda socket: None)
        self.assertEqual(events, [("start", "/one", 5), ("connect", "/one", 5), "socket-close", ("stop", "/one"), ("start", "/two", 5), ("connect", "/two", 5), "socket-close", ("stop", "/two")])

    def test_scenario_request_carries_timeout_and_returns_result(self):
        with patch.object(vm_factory.guest_transport, "DeadlineStream", return_value="stream") as stream, patch.object(vm_factory.guest_transport, "request", return_value={"result": "ok"}) as request:
            self.assertEqual(vm_factory.request_scenario("socket", "fenced", 12, 3), {"result": "ok"})
            stream.assert_called_once_with("socket", 3)
            request.assert_called_once_with("stream", "scenario", "scenario", {"scenario": "fenced", "metric": 12, "timeout": 3})

    def test_deadline_prevents_connect_after_start(self):
        now = [0]
        @contextmanager
        def start(*args, **kwargs):
            now[0] = 2
            yield "one", "overlay", object()
        calls = []
        owner = vm_factory.make_owner("/base", lambda: "/work", qemu="/qemu", qemu_img="/img", attempts=1, timeout=2, vm_start=start, connect_ready=lambda *args: calls.append(args), clock=lambda: now[0])
        with self.assertRaises(RuntimeError):
            owner.run(lambda socket: None)
        self.assertEqual(calls, [])

    def test_teardown_failure_does_not_retry(self):
        calls = []
        @contextmanager
        def start(*args, **kwargs):
            calls.append("start")
            yield "one", "overlay", object()
            raise RuntimeError("teardown")
        owner = vm_factory.make_owner("/base", lambda: "/work", qemu="/qemu", qemu_img="/img", attempts=2, timeout=5, vm_start=start, connect_ready=lambda *args: Socket([]), clock=lambda: 0)
        with self.assertRaisesRegex(RuntimeError, "teardown failed"):
            owner.run(lambda socket: None)
        self.assertEqual(calls, ["start"])


if __name__ == "__main__":
    unittest.main()
