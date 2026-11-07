"""Bounded JSON-lines transport for a future isolated guest serial channel.

This module does not provision or start a guest. Callers supply a connected binary stream and guest-side handlers; neither endpoint opens a host device.
"""

import json
import time

MAX_FRAME = 65536


class DeadlineStream:
    """Unbuffered socket stream with one monotonic deadline for all operations."""

    def __init__(self, sock, timeout, clock=time.monotonic):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.sock = sock
        self.clock = clock
        self.deadline = clock() + timeout

    def _remaining(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("transport deadline expired")
        self.sock.settimeout(remaining)

    def write(self, data):
        view = memoryview(data)
        while view:
            self._remaining()
            count = self.sock.send(view)
            if count == 0:
                raise ConnectionError("transport closed during write")
            view = view[count:]

    def flush(self):
        pass

    def readline(self, limit):
        data = bytearray()
        while len(data) < limit:
            self._remaining()
            chunk = self.sock.recv(1)
            if not chunk:
                break
            data.extend(chunk)
            if chunk == b"\n":
                break
        return bytes(data)


def ready(payload):
    """Report that this handler is responsive, not that isolation is proven."""
    if payload:
        raise ValueError("readiness payload must be empty")
    return {"ready": True}


def send(stream, value):
    """Send one bounded JSON object to the other endpoint."""
    encoded = json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > MAX_FRAME:
        raise ValueError("frame exceeds limit")
    stream.write(encoded)
    stream.flush()


def receive(stream):
    """Read one complete bounded JSON object, rejecting malformed frames."""
    line = stream.readline(MAX_FRAME + 1)
    if len(line) > MAX_FRAME or not line.endswith(b"\n"):
        raise ValueError("incomplete or oversized frame")
    try:
        value = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("invalid JSON frame") from None
    if not isinstance(value, dict):
        raise ValueError("frame must be an object")
    return value


def serve_one(stream, handlers):
    """Dispatch a named fixed handler; never interpret guest input as a command."""
    request = receive(stream)
    if set(request) != {"id", "operation", "payload"} or not isinstance(request["id"], str) or not isinstance(request["operation"], str) or not isinstance(request["payload"], dict):
        raise ValueError("invalid request")
    operation = request["operation"]
    if operation not in handlers:
        raise ValueError("unknown operation")
    result = handlers[operation](request["payload"])
    send(stream, {"id": request["id"], "result": result})


def request(stream, request_id, operation, payload):
    """Send a request and verify that the response corresponds to it."""
    send(stream, {"id": request_id, "operation": operation, "payload": payload})
    response = receive(stream)
    if set(response) != {"id", "result"} or response["id"] != request_id:
        raise ValueError("response does not match request")
    return response["result"]
