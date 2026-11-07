"""Own fixture child processes and the authority wrapper lifetime."""

import subprocess
import threading
import time

from fixture_lifetime import FixtureLifetime
from lifecycle_ports import Authority


class FixtureOwner:
    """Claim a control object and tear down authority and bus on retirement.

    Claims are process-local and retained after retirement. The production isolation guard must run before constructing an owner around real children.
    """

    _claims = {}
    _lock = threading.Lock()

    def __init__(self, control, authority_process, bus_process, *, timeout, guard):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        guard()
        if authority_process is bus_process:
            raise ValueError("authority and bus must be distinct children")
        with self._lock:
            key = id(control)
            if key in self._claims:
                raise RuntimeError("control already owned")
            self._claims[key] = control
        self.control = control
        self.authority_process = authority_process
        self.bus_process = bus_process
        self.lifetime = FixtureLifetime(self._teardown, timeout)

    def authority(self):
        return Authority(self.control, self.lifetime)

    def retire(self):
        self.lifetime.retire()

    def _teardown(self, *, timeout):
        deadline = time.monotonic() + timeout
        errors = []
        for process in (self.authority_process, self.bus_process):
            try:
                if process.poll() is None:
                    process.terminate()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired("fixture child", timeout)
                process.wait(timeout=remaining)
            except BaseException as error:
                errors.append(error)
                try:
                    if process.poll() is None:
                        process.kill()
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        process.wait(timeout=remaining)
                    else:
                        errors.append(TimeoutError("no time left to reap fixture child"))
                except BaseException as cleanup_error:
                    errors.append(cleanup_error)
        if errors:
            raise RuntimeError("fixture teardown failed") from errors[0]
