"""Injected tests for host readiness connection orchestration."""

import errno
import unittest

import host_transport


class Process:
    def __init__(self):
        self.exited = False

    def poll(self):
        return 1 if self.exited else None


class Socket:
    def __init__(self, connect_error=None, response=b'{"id":"readiness","result":{"ready":true}}\n'):
        self.connect_error = connect_error
        self.response = bytearray(response)
        self.sent = bytearray()
        self.closed = False
        self.address = None
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, address):
        self.address = address
        if self.connect_error:
            raise OSError(self.connect_error, "not ready")

    def send(self, data):
        self.sent.extend(data)
        return len(data)

    def recv(self, size):
        result = bytes(self.response[:size])
        del self.response[:size]
        return result

    def close(self):
        self.closed = True


class HostTransportTests(unittest.TestCase):
    def test_retry_then_readiness_and_explicit_path(self):
        first, second = Socket(errno.ENOENT), Socket()
        sockets = iter([first, second])
        now = [0.0]
        def pause(duration):
            now[0] += duration
        result = host_transport.connect_ready("/private/work", Process(), 1, socket_factory=lambda *args: next(sockets), clock=lambda: now[0], pause=pause)
        self.assertIs(result, second)
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)
        self.assertEqual(second.address, "/private/work/guest.sock")
        self.assertIn(b'"operation":"ready"', second.sent)
        self.assertLess(second.timeouts[0], 1)

    def test_retry_is_bounded_and_closes_each_socket(self):
        now = [0.0]
        sockets = []
        def factory(*args):
            sock = Socket(errno.ECONNREFUSED)
            sockets.append(sock)
            return sock
        def pause(duration):
            now[0] += duration
        with self.assertRaises(TimeoutError):
            host_transport.connect_ready("/private/work", Process(), 0.1, socket_factory=factory, clock=lambda: now[0], pause=pause)
        self.assertTrue(sockets)
        self.assertTrue(all(sock.closed for sock in sockets))

    def test_failed_readiness_closes_socket(self):
        sock = Socket(response=b'{"id":"readiness","result":{"ready":false}}\n')
        with self.assertRaisesRegex(RuntimeError, "did not report"):
            host_transport.connect_ready("/private/work", Process(), 1, socket_factory=lambda *args: sock)
        self.assertTrue(sock.closed)

    def test_dead_process_never_connects(self):
        process = Process()
        process.exited = True
        calls = []
        with self.assertRaisesRegex(RuntimeError, "exited"):
            host_transport.connect_ready("/private/work", process, 1, socket_factory=lambda *args: calls.append(args))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
