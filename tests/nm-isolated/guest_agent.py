"""Guest virtio-serial readiness agent; no commands or services are started."""

import argparse
import os
import select
import math
import time

import guest_transport
import guest_runner

DEVICE = "/dev/virtio-ports/test.nm.fixture"


class DeviceStream:
    """Deadline-bound, unbuffered access to a nonblocking virtio serial fd."""

    def __init__(self, fd, timeout, *, clock=time.monotonic, wait=select.select, read=os.read, write=os.write):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.fd, self.clock, self.wait, self.read, self.write_fd = fd, clock, wait, read, write
        self.deadline = clock() + timeout

    def _wait(self, writing):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("guest transport deadline expired")
        readable, writable, _ = self.wait([] if writing else [self.fd], [self.fd] if writing else [], [], remaining)
        if not (writable if writing else readable):
            raise TimeoutError("guest transport deadline expired")

    def readline(self, limit):
        result = bytearray()
        while len(result) < limit:
            self._wait(False)
            try:
                part = self.read(self.fd, 1)
            except BlockingIOError:
                continue
            if not part:
                break
            result.extend(part)
            if part == b"\n":
                break
        return bytes(result)

    def write(self, data):
        remaining = memoryview(data)
        while remaining:
            self._wait(True)
            try:
                count = self.write_fd(self.fd, remaining)
            except BlockingIOError:
                continue
            if count == 0:
                raise ConnectionError("guest transport closed during write")
            remaining = remaining[count:]

    def flush(self):
        pass


def scenario_handler(run_scenario):
    """Build a restricted scenario handler around an injected runner."""
    def handle(payload):
        if set(payload) != {"scenario", "metric", "timeout"} or payload["scenario"] not in ("no-fence", "fenced") or type(payload["metric"]) is not int or type(payload["timeout"]) not in (int, float) or not math.isfinite(payload["timeout"]) or not 0 < payload["timeout"] <= 300:
            raise ValueError("invalid scenario request")
        return run_scenario(payload["scenario"], payload["metric"], payload["timeout"])
    return handle


def serve(fd, timeout, *, run_scenario=None):
    """Serve readiness and, when supplied, one scenario request."""
    guest_transport.serve_one(DeviceStream(fd, timeout), {"ready": guest_transport.ready})
    if run_scenario is not None:
        guest_transport.serve_one(DeviceStream(fd, timeout), {"scenario": scenario_handler(run_scenario)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, required=True)
    args = parser.parse_args()
    fd = os.open(DEVICE, os.O_RDWR | os.O_NONBLOCK)
    try:
        serve(fd, args.timeout, run_scenario=guest_runner.make_runner(guest_runner.guarded_adapter_factory))
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
