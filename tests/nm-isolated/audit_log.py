"""Parse NetworkManager 1.54.3 audit log text from an injected capture stream."""

import re

FIELD = re.compile(r'([a-z][a-z0-9_-]*)=(?:"([^"\n]*)"|([0-9]+))(?: |$)')


def parse_line(line, daemon_instance):
    """Parse the audit suffix; reject malformed or duplicate fields."""
    marker = "audit: "
    if marker not in line:
        return None
    text = line.split(marker, 1)[1].rstrip("\n")
    values = {}
    position = 0
    while position < len(text):
        match = FIELD.match(text, position)
        if match is None or match.group(1) in values:
            raise ValueError("malformed NetworkManager audit event")
        values[match.group(1)] = match.group(2) if match.group(2) is not None else int(match.group(3))
        position = match.end()
    values["daemon_instance"] = daemon_instance
    return values


class CaptureReader:
    """Read complete lines after byte cursors from one daemon's captured output.

    The stream must be exclusive to the identified daemon process. It must not be truncated or replaced; those conditions raise instead of silently skipping.
    """

    def __init__(self, stream, daemon_instance):
        self.stream = stream
        self.daemon_instance = daemon_instance

    def mark(self):
        self.stream.seek(0, 2)
        return self.stream.tell()

    def read_after(self, cursor, *, timeout):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.stream.seek(0, 2)
        if self.stream.tell() < cursor:
            raise RuntimeError("audit capture was truncated")
        self.stream.seek(cursor)
        records = []
        while True:
            position = self.stream.tell()
            line = self.stream.readline()
            if not line or not line.endswith(b"\n"):
                return records, position
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("invalid audit capture encoding") from None
            record = parse_line(text, self.daemon_instance)
            if record is not None:
                records.append(record)
