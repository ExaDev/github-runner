"""Offline device stream and guest agent tests."""

import socket
import unittest

import guest_agent
import guest_transport


class GuestAgentTests(unittest.TestCase):
    def test_one_readiness_roundtrip_over_socket_pair(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        left.setblocking(False)
        right.sendall(b'{"id":"1","operation":"ready","payload":{}}\n')
        guest_agent.serve(left.fileno(), 1)
        self.assertEqual(guest_transport.receive(right.makefile("rb")), {"id": "1", "result": {"ready": True}})

    def test_scenario_handler_validates_before_dispatch(self):
        calls = []
        handler = guest_agent.scenario_handler(lambda *args: calls.append(args) or {"result": "ok"})
        self.assertEqual(handler({"scenario": "fenced", "metric": 12, "timeout": 5}), {"result": "ok"})
        self.assertEqual(calls, [("fenced", 12, 5)])
        for payload in ({"scenario": "exec", "metric": 1, "timeout": 1}, {"scenario": "fenced", "metric": True, "timeout": 1}, {"scenario": "fenced", "metric": 1, "timeout": float("nan")}):
            with self.assertRaises(ValueError):
                handler(payload)
        self.assertEqual(len(calls), 1)

    def test_device_timeout_fails_without_read(self):
        called = []
        stream = guest_agent.DeviceStream(1, 2, clock=lambda: 0, wait=lambda *args: ([], [], []), read=lambda *args: called.append(args))
        with self.assertRaises(TimeoutError):
            stream.readline(10)
        self.assertEqual(called, [])

    def test_partial_write_obeys_single_deadline(self):
        now = [0]
        def wait(readable, writable, errors, timeout):
            now[0] += 1
            return readable, writable, []
        stream = guest_agent.DeviceStream(1, 2, clock=lambda: now[0], wait=wait, write=lambda fd, data: 1)
        with self.assertRaises(TimeoutError):
            stream.write(b"abc")


if __name__ == "__main__":
    unittest.main()
