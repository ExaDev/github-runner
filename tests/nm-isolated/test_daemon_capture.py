"""Offline daemon output capture tests with in-memory pipes."""

import io
import os
import unittest

from daemon_capture import Capture, CaptureReader


class CaptureTests(unittest.TestCase):
    def test_capture_feeds_audit_cursor_reader(self):
        data = b'audit: op="connection-update" uuid="id" pid=123 result="success"\n'
        capture = Capture(io.BufferedReader(io.BytesIO(data)), len(data))
        capture.join(1)
        reader = CaptureReader(capture, ":1.1")
        records, cursor = reader.read_after(0, timeout=1)
        self.assertEqual(records[0]["pid"], 123)
        self.assertEqual(cursor, len(data))
        self.assertEqual(reader.mark(), len(data))

    def test_overflow_is_reported_and_pipe_is_drained(self):
        pipe = io.BytesIO(b"x" * 10000)
        capture = Capture(io.BufferedReader(pipe), 20)
        with self.assertRaisesRegex(RuntimeError, "capture failed"):
            capture.join(1)
        self.assertEqual(pipe.tell(), 10000)

    def test_short_pipe_write_is_visible_before_eof(self):
        read_fd, write_fd = os.pipe()
        pipe = os.fdopen(read_fd, "rb")
        capture = Capture(pipe, 100)
        try:
            os.write(write_fd, b"short\n")
            capture.thread.join(0.05)
            self.assertTrue(capture.thread.is_alive())
            self.assertEqual(capture.snapshot(), b"short\n")
        finally:
            os.close(write_fd)
            capture.join(1)
            pipe.close()

    def test_invalid_limit_fails(self):
        with self.assertRaises(ValueError):
            Capture(io.BytesIO(), 0)


if __name__ == "__main__":
    unittest.main()
