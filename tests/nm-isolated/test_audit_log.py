"""Synthetic tests for NetworkManager audit text and capture cursors."""

import io
import unittest

from audit_log import CaptureReader, parse_line
from audit_observer import AuditObserver


SUCCESS = b'<info> audit: op="connection-update" uuid="id" name="fixture" pid=123 uid=1000 result="success"\n'
FAILURE = b'<info> audit: op="connection-update" uuid="id" name="fixture" pid=123 uid=1000 result="fail" reason="Update failed because profile changed in the meantime and the version-id mismatches"\n'


class AuditLogTests(unittest.TestCase):
    def test_source_shaped_success_and_failure(self):
        self.assertEqual(parse_line(SUCCESS.decode(), ":1.4")["pid"], 123)
        self.assertEqual(parse_line(FAILURE.decode(), ":1.4")["reason"], "Update failed because profile changed in the meantime and the version-id mismatches")
        self.assertIsNone(parse_line("unrelated\n", ":1.4"))

    def test_duplicate_and_malformed_rejected(self):
        for line in ('audit: pid=1 pid=2\n', 'audit: op="unfinished\n', 'audit: pid=abc\n'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_line(line, ":1.4")

    def test_cursor_skips_prior_lines_and_retains_partial_line(self):
        stream = io.BytesIO(SUCCESS)
        reader = CaptureReader(stream, ":1.4")
        cursor = reader.mark()
        stream.write(FAILURE[:-1])
        records, unchanged = reader.read_after(cursor, timeout=1)
        self.assertEqual(records, [])
        self.assertEqual(unchanged, cursor)
        stream.seek(0, 2)
        stream.write(b"\n")
        records, end = reader.read_after(cursor, timeout=1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["result"], "fail")
        self.assertGreater(end, cursor)
        stream.truncate(cursor - 1)
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            reader.read_after(cursor, timeout=1)

    def test_observer_detects_ambiguity_and_unrelated(self):
        stream = io.BytesIO(SUCCESS + SUCCESS)
        now = [0.0]
        def pause(value):
            now[0] += value
        observer = AuditObserver(CaptureReader(stream, ":1.4"), ":1.4", 123, "id", 0, clock=lambda: now[0], pause=pause)
        self.assertEqual(observer.wait("request", timeout=0.1), "inconclusive")
        stream = io.BytesIO(SUCCESS)
        now[0] = 0
        observer = AuditObserver(CaptureReader(stream, ":1.4"), ":1.4", 124, "id", 0, clock=lambda: now[0], pause=pause)
        self.assertEqual(observer.wait("request", timeout=0.1), "inconclusive")


if __name__ == "__main__":
    unittest.main()
