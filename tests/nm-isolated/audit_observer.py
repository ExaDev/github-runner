"""Correlate synthetic or captured NetworkManager audit events for a test run.

The reader supplies structured records and opaque cursors; no journal or host logging service is opened by this module.
"""

import time


class AuditObserver:
    def __init__(self, reader, daemon_instance, pid, profile_uuid, cursor, *, clock=time.monotonic, pause=time.sleep):
        self.reader = reader
        self.daemon_instance = daemon_instance
        self.pid = pid
        self.profile_uuid = profile_uuid
        self.cursor = cursor
        self.clock = clock
        self.pause = pause

    def wait(self, request, *, timeout):
        """Return on one matching batch; multiple matches in that batch are inconclusive.

        The fixture must guarantee that the dedicated client sends exactly one update. Without that invariant, a later duplicate cannot be ruled out.

        Reader.read_after(cursor, timeout) returns (records, new_cursor). Records are parsed by the capture adapter, which must preserve ordering and must not fabricate a daemon identity, cursor or audit field.
        """
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = self.clock() + timeout
        matches = []
        cursor = self.cursor
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            records, cursor = self.reader.read_after(cursor, timeout=remaining)
            for record in records:
                if (record.get("daemon_instance") == self.daemon_instance
                    and record.get("pid") == self.pid
                    and record.get("uuid") == self.profile_uuid
                    and record.get("op") == "connection-update"):
                    matches.append(record)
            if len(matches) > 1:
                return "inconclusive"
            if matches:
                break
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            self.pause(min(0.05, remaining))
        if len(matches) != 1:
            return "inconclusive"
        record = matches[0]
        if record.get("result") == "success":
            return "applied"
        if record.get("result") == "fail" and record.get("reason") == "Update failed because profile changed in the meantime and the version-id mismatches":
            return "version-mismatch"
        return "inconclusive"
