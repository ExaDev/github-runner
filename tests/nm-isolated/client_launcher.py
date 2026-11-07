"""Guarded non-root fixture client process launcher.

The production isolation check deliberately refuses until the VM boundary is verified. Process and identity dependencies can be injected for offline tests.
"""

import os
import pwd
import select
import subprocess
import time
from pathlib import Path

import networkmanager
import private_bus


class Child:
    def __init__(self, process):
        self.process = process

    @property
    def pid(self):
        return self.process.pid

    def read_request_id(self, *, timeout):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        line = bytearray()
        while len(line) <= 256:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("held client did not report a request")
            readable, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not readable:
                raise TimeoutError("held client did not report a request")
            part = os.read(self.process.stdout.fileno(), 1)
            if not part:
                raise RuntimeError("held client exited before request id")
            if part == b"\n":
                return line.decode("utf-8")
            line.extend(part)
        raise RuntimeError("invalid held client response")

    def wait(self, *, timeout):
        return self.process.wait(timeout=timeout)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()


class Launcher:
    """Start the fixed client as the named non-root guest account."""

    def __init__(self, profile_path, *, guard=networkmanager.verified_isolation, lookup=pwd.getpwnam, popen=subprocess.Popen):
        self.profile_path = profile_path
        self.guard = guard
        self.lookup = lookup
        self.popen = popen

    def start(self, *, metric, unfenced, disconnect_after_held, timeout):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.guard()
        account = self.lookup("nm-isolated")
        if account.pw_uid == 0 or account.pw_gid == 0:
            raise RuntimeError("fixture account must be non-root")
        client = Path(__file__).with_name("update2_client.py")
        command = ["/usr/bin/python3", str(client), "--private-bus-address", private_bus.ADDRESS, "--profile-path", self.profile_path, "--timeout", str(timeout)]
        if metric is not None:
            command.extend(["--metric", str(metric)])
        if unfenced:
            command.append("--unfenced")
        if disconnect_after_held:
            command.append("--disconnect-after-held")
        process = self.popen(command, user=account.pw_uid, group=account.pw_gid, extra_groups=(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
        return Child(process)
