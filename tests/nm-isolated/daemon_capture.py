"""Bounded, append-only capture of a fixture daemon's output pipe."""

import io
import threading


class Capture:
    """Drain the daemon pipe independently and fail closed on overflow."""

    def __init__(self, pipe, limit):
        if limit <= 0:
            raise ValueError("capture limit must be positive")
        self.pipe = pipe
        self.limit = limit
        self.data = bytearray()
        self.failure = None
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        try:
            while True:
                chunk = self.pipe.read1(4096)
                if not chunk:
                    return
                with self.lock:
                    if len(self.data) + len(chunk) > self.limit:
                        self.failure = RuntimeError("daemon capture limit exceeded")
                    elif self.failure is None:
                        self.data.extend(chunk)
        except Exception as error:
            with self.lock:
                self.failure = error

    def snapshot(self):
        with self.lock:
            if self.failure:
                raise RuntimeError("daemon capture failed") from self.failure
            return bytes(self.data)

    def join(self, timeout):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError("daemon capture did not finish")
        self.snapshot()


class CaptureReader:
    """Adapt a capture snapshot to the audit parser's byte cursor contract."""

    def __init__(self, capture, daemon_instance):
        self.capture = capture
        self.daemon_instance = daemon_instance

    def mark(self):
        return len(self.capture.snapshot())

    def read_after(self, cursor, *, timeout):
        from audit_log import CaptureReader as TextReader

        return TextReader(io.BytesIO(self.capture.snapshot()), self.daemon_instance).read_after(cursor, timeout=timeout)
