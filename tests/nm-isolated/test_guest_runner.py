"""Offline guest scenario runner integration tests."""

import socket
import threading
import unittest
from contextlib import contextmanager

import guest_agent
import guest_runner
import guest_transport
from test_scenarios import Adapter


class GuestRunnerTests(unittest.TestCase):
    def test_no_fence_over_bounded_socket_transport(self):
        events = []
        @contextmanager
        def factory(*, timeout):
            events.append(("start", timeout))
            try:
                yield Adapter()
            finally:
                events.append("stop")
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        left.setblocking(False)
        errors = []
        def worker():
            try:
                guest_agent.serve(left.fileno(), 2, run_scenario=guest_runner.make_runner(factory))
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        ready = guest_transport.request(guest_transport.DeadlineStream(right, 2), "ready", "ready", {})
        result = guest_transport.request(guest_transport.DeadlineStream(right, 2), "scenario", "scenario", {"scenario": "no-fence", "metric": 20101, "timeout": 1})
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(ready, {"ready": True})
        self.assertEqual(result["after"], {"metric": 20101, "version": 3})
        self.assertEqual(events, [("start", 1), "stop"])

    def test_production_factory_refuses(self):
        with self.assertRaises(RuntimeError):
            guest_runner.make_runner(guest_runner.guarded_adapter_factory)("fenced", 1, 1)


if __name__ == "__main__":
    unittest.main()
