"""Offline structured synthetic NetworkManager audit event tests."""

import unittest

from audit_observer import AuditObserver


class Reader:
    def __init__(self, events):
        self.events = events
        self.cursors = []

    def read_after(self, cursor, *, timeout):
        self.cursors.append((cursor, timeout))
        return self.events[cursor:], len(self.events)


def event(**changes):
    value = {"daemon_instance": ":1.4", "pid": 123, "uuid": "profile-uuid", "op": "connection-update", "result": "success"}
    value.update(changes)
    return value


class AuditTests(unittest.TestCase):
    def observe(self, events, cursor=0):
        now = [0.0]
        reader = Reader(events)
        def pause(value):
            now[0] += value
        observer = AuditObserver(reader, ":1.4", 123, "profile-uuid", cursor, clock=lambda: now[0], pause=pause)
        return observer.wait("held", timeout=0.1), reader

    def test_success_after_cursor(self):
        result, reader = self.observe([event(result="fail"), event()], cursor=1)
        self.assertEqual(result, "applied")
        self.assertEqual(reader.cursors[0][0], 1)

    def test_match_returns_without_spending_remaining_deadline(self):
        now = [0.0]
        observer = AuditObserver(Reader([event()]), ":1.4", 123, "profile-uuid", 0, clock=lambda: now[0], pause=lambda delay: now.__setitem__(0, now[0] + delay))
        self.assertEqual(observer.wait("held", timeout=1), "applied")
        self.assertEqual(now[0], 0)

    def test_version_mismatch_exact_reason(self):
        reason = "Update failed because profile changed in the meantime and the version-id mismatches"
        result, _ = self.observe([event(result="fail", reason=reason)])
        self.assertEqual(result, "version-mismatch")
        result, _ = self.observe([event(result="fail", reason="another failure")])
        self.assertEqual(result, "inconclusive")

    def test_unrelated_missing_and_ambiguous_are_inconclusive(self):
        cases = [[], [event(pid=124)], [event(uuid="other")], [event(daemon_instance=":1.9")], [event(op="connection-delete")], [event(), event()]]
        for records in cases:
            with self.subTest(records=records):
                self.assertEqual(self.observe(records)[0], "inconclusive")

    def test_rejects_non_positive_deadline(self):
        observer = AuditObserver(Reader([]), ":1.4", 123, "id", 0)
        with self.assertRaises(ValueError):
            observer.wait("held", timeout=0)


if __name__ == "__main__":
    unittest.main()
