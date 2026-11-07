#!/usr/bin/python3
"""Lifecycle for a disposable fixture's explicitly addressed private bus."""

import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path("/run/nm-isolated-test")
SOCKET = ROOT / "bus"
ADDRESS = f"unix:path={SOCKET}"


def check_environment():
    """Refuse to launch outside the disposable VM asserted by the operator."""
    sentinel = ROOT / "disposable-vm"
    if os.geteuid() != 0 or sentinel.is_symlink() or not sentinel.is_file():
        raise RuntimeError("refusing: root and disposable VM sentinel required")
    if subprocess.run(["systemd-detect-virt", "--vm", "--quiet"], check=False).returncode:
        raise RuntimeError("refusing: disposable VM required")
    if ROOT.is_symlink():
        raise RuntimeError("refusing: test directory is symlinked")


def wait_ready(process, timeout, *, run=subprocess.run, clock=time.monotonic, pause=time.sleep):
    """Wait for a successful D-Bus Ping on the explicit private address."""
    deadline = clock() + timeout
    while True:
        if process.poll() is not None:
            raise RuntimeError("private bus exited before readiness")
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("private bus readiness deadline expired")
        try:
            result = run(["dbus-send", f"--bus={ADDRESS}", "--print-reply", f"--reply-timeout={max(1, int(remaining * 1000))}", "--dest=org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.Peer.Ping"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=remaining, check=False)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("private bus readiness deadline expired") from error
        if result.returncode == 0:
            if process.poll() is not None:
                raise RuntimeError("private bus exited during readiness")
            return
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("private bus readiness deadline expired")
        pause(min(0.05, remaining))


@contextmanager
def private_bus(*, check=check_environment, popen=subprocess.Popen, ready=wait_ready, readiness_timeout=5, shutdown_timeout=5):
    """Launch a private bus and yield only after its explicit address replies."""
    if shutdown_timeout <= 0 or readiness_timeout <= 0:
        raise ValueError("timeouts must be positive")
    check()
    if SOCKET.is_symlink() or SOCKET.exists():
        raise RuntimeError("refusing: test bus path is occupied or symlinked")
    config = Path(__file__).with_name("bus.conf")
    process = popen(["dbus-daemon", f"--config-file={config}", "--nofork"], stdin=subprocess.DEVNULL)
    try:
        ready(process, readiness_timeout)
        yield ADDRESS, process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=shutdown_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
