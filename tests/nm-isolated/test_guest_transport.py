"""Offline tests for the stream transport without devices or VM processes."""

import io
import json
import socket
import unittest

import guest_transport as transport


class Duplex:
    def __init__(self, incoming):
        self.reader = io.BytesIO(incoming)
        self.writer = io.BytesIO()

    def readline(self, limit):
        return self.reader.readline(limit)

    def write(self, data):
        return self.writer.write(data)

    def flush(self):
        pass


class TransportTests(unittest.TestCase):
    def test_dispatches_only_named_handler(self):
        stream = Duplex(b'{"id":"one","operation":"ready","payload":{}}\n')
        transport.serve_one(stream, {"ready": lambda payload: {"ready": True}})
        self.assertEqual(json.loads(stream.writer.getvalue()), {"id": "one", "result": {"ready": True}})

    def test_readiness_handler_reports_only_responsiveness(self):
        stream = Duplex(b'{"id":"one","operation":"ready","payload":{}}\n')
        transport.serve_one(stream, {"ready": transport.ready})
        self.assertEqual(json.loads(stream.writer.getvalue())["result"], {"ready": True})
        with self.assertRaisesRegex(ValueError, "empty"):
            transport.ready({"assert-isolated": True})

    def test_deadline_applies_across_partial_reads_and_writes(self):
        class FakeSocket:
            def __init__(self):
                self.now = 0
                self.timeouts = []
                self.sent = bytearray()
            def settimeout(self, timeout):
                self.timeouts.append(timeout)
            def recv(self, size):
                self.now += 1
                return b"x"
            def send(self, data):
                self.now += 1
                self.sent.extend(data[:1])
                return 1
        sock = FakeSocket()
        stream = transport.DeadlineStream(sock, 2, clock=lambda: sock.now)
        with self.assertRaises(TimeoutError):
            stream.readline(10)
        self.assertEqual(sock.timeouts, [2, 1])
        sock = FakeSocket()
        stream = transport.DeadlineStream(sock, 2, clock=lambda: sock.now)
        with self.assertRaises(TimeoutError):
            stream.write(b"abc")
        self.assertEqual(sock.sent, b"ab")

    def test_real_socket_timeout_on_read_and_full_write_buffer(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        with self.assertRaises(TimeoutError):
            transport.DeadlineStream(left, 0.02).readline(10)
        left.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        with self.assertRaises(TimeoutError):
            transport.DeadlineStream(left, 0.02).write(b"x" * (1024 * 1024))

    def test_unknown_operation_is_rejected(self):
        stream = Duplex(b'{"id":"one","operation":"exec","payload":{}}\n')
        with self.assertRaisesRegex(ValueError, "unknown operation"):
            transport.serve_one(stream, {})
        self.assertEqual(stream.writer.getvalue(), b"")

    def test_request_matches_response_id(self):
        stream = Duplex(b'{"id":"one","result":{"ready":true}}\n')
        self.assertEqual(transport.request(stream, "one", "ready", {}), {"ready": True})
        self.assertEqual(json.loads(stream.writer.getvalue()), {"id": "one", "operation": "ready", "payload": {}})
        with self.assertRaisesRegex(ValueError, "does not match"):
            transport.request(Duplex(b'{"id":"other","result":{}}\n'), "one", "ready", {})

    def test_invalid_and_oversized_frames_fail(self):
        for data in (b"[]\n", b"{", b"x" * (transport.MAX_FRAME + 1)):
            with self.subTest(length=len(data)):
                with self.assertRaises(ValueError):
                    transport.receive(io.BytesIO(data))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            transport.send(io.BytesIO(), {"data": "x" * transport.MAX_FRAME})


if __name__ == "__main__":
    unittest.main()
