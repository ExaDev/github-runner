"""Fail-closed lifecycle composition for an isolated NetworkManager fixture."""

import os
import subprocess
from contextlib import contextmanager

import private_bus
from daemon_capture import Capture


def verified_isolation(*, vm_check=private_bus.check_environment, readlink=os.readlink, read_text=None):
    """Check isolation evidence and refuse without a trusted filesystem provenance proof."""
    vm_check()
    if read_text is None:
        def read_text(name):
            with open(name, encoding="utf-8") as stream:
                return stream.read()
    if readlink("/proc/self/ns/mnt") == readlink("/proc/1/ns/mnt"):
        raise RuntimeError("refusing: mount namespace is shared with VM init")
    mounts = read_text("/proc/self/mountinfo").splitlines()
    if not mounts:
        raise RuntimeError("refusing: mount table is empty")
    root_seen = False
    for line in mounts:
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_root, mountpoint = fields[3:5]
            filesystem = fields[separator + 1]
        except (ValueError, IndexError):
            raise RuntimeError("refusing: malformed mount table") from None
        if separator < 6 or mount_root != "/" or filesystem != "tmpfs":
            raise RuntimeError("refusing: host or non-isolated filesystem mount")
        if mountpoint == "/":
            root_seen = True
    if not root_seen:
        raise RuntimeError("refusing: isolated root mount missing")
    raise RuntimeError("refusing: tmpfs mount provenance and namespace escape cannot be verified")


@contextmanager
def networkmanager(*, isolation_check=verified_isolation, bus_factory=private_bus.private_bus, popen=subprocess.Popen, capture_factory=Capture, capture_limit=1048576, shutdown_timeout=5):
    """Compose the private bus and daemon lifecycle after isolation verification.

    The production verifier inspects isolation evidence but still refuses without mount provenance proof. Injected checks are for offline tests and must not be used to authorise a real daemon.
    """
    if shutdown_timeout <= 0 or capture_limit <= 0:
        raise ValueError("shutdown timeout and capture limit must be positive")
    isolation_check()
    with bus_factory() as (address, bus_process):
        if address != private_bus.ADDRESS or bus_process.poll() is not None:
            raise RuntimeError("refusing: private bus is not the expected running child")
        environment = os.environ.copy()
        environment["DBUS_SYSTEM_BUS_ADDRESS"] = address
        process = popen(["NetworkManager", "--no-daemon", "--log-level=INFO", "--log-domains=ALL"], env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        capture = None
        try:
            capture = capture_factory(process.stdout, capture_limit)
            yield address, process, capture
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=shutdown_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if capture is not None:
                capture.join(shutdown_timeout)
