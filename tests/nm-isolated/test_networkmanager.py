"""Offline tests of the fail-closed NetworkManager lifecycle."""

import subprocess
import unittest
from contextlib import contextmanager

import networkmanager
import private_bus


class Process:
    def __init__(self, events, name, exited=False, timeout=False):
        self.events = events
        self.name = name
        self.exited = exited
        self.timeout = timeout
        self.stdout = None

    def poll(self):
        return 1 if self.exited else None

    def terminate(self):
        self.events.append((self.name, "terminate"))

    def kill(self):
        self.events.append((self.name, "kill"))

    def wait(self, timeout=None):
        self.events.append((self.name, "wait", timeout))
        if self.timeout and timeout is not None:
            raise subprocess.TimeoutExpired(self.name, timeout)
        return 0


class FakeCapture:
    def __init__(self, pipe, limit):
        self.limit = limit

    def join(self, timeout):
        pass


class NetworkManagerTests(unittest.TestCase):
    def test_isolation_evidence_rejects_shared_namespace_and_host_mounts(self):
        def run(mountinfo, same_namespace=False):
            def link(name):
                return "mnt:[1]" if same_namespace or name == "/proc/1/ns/mnt" else "mnt:[2]"
            networkmanager.verified_isolation(vm_check=lambda: None, readlink=link, read_text=lambda name: mountinfo)
        root = "1 0 0:1 / / rw - tmpfs tmpfs rw"
        with self.assertRaisesRegex(RuntimeError, "namespace is shared"):
            run(root, same_namespace=True)
        with self.assertRaisesRegex(RuntimeError, "non-isolated"):
            run(root + "\n2 1 8:1 / /etc/NetworkManager rw - ext4 /dev/sda rw")
        with self.assertRaisesRegex(RuntimeError, "non-isolated"):
            run(root + "\n2 1 0:1 /host /etc/NetworkManager rw - tmpfs tmpfs rw")
        with self.assertRaisesRegex(RuntimeError, "namespace escape"):
            run(root)

    def test_default_verifier_refuses_before_launch(self):
        calls = []
        with self.assertRaises(RuntimeError):
            with networkmanager.networkmanager(popen=lambda *args, **kwargs: calls.append(args)):
                self.fail("must not enter")
        self.assertEqual(calls, [])

    def test_child_uses_explicit_bus_and_is_reaped_before_bus(self):
        events = []
        bus = Process(events, "bus")
        daemon = Process(events, "nm")

        @contextmanager
        def bus_factory():
            events.append("bus-start")
            try:
                yield private_bus.ADDRESS, bus
            finally:
                events.append("bus-stop")

        def popen(command, **kwargs):
            events.append("nm-start")
            self.assertEqual(command, ["NetworkManager", "--no-daemon", "--log-level=INFO", "--log-domains=ALL"])
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
            self.assertEqual(kwargs["env"]["DBUS_SYSTEM_BUS_ADDRESS"], private_bus.ADDRESS)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            return daemon

        with self.assertRaisesRegex(ValueError, "fixture failure"):
            with networkmanager.networkmanager(isolation_check=lambda: None, bus_factory=bus_factory, popen=popen, capture_factory=FakeCapture, shutdown_timeout=2) as (address, child, capture):
                self.assertEqual(address, private_bus.ADDRESS)
                self.assertIs(child, daemon)
                raise ValueError("fixture failure")
        self.assertEqual(events, ["bus-start", "nm-start", ("nm", "terminate"), ("nm", "wait", 2), "bus-stop"])

    def test_wrong_or_dead_bus_refused_without_daemon(self):
        for address, exited in [("unix:path=/run/dbus/system_bus_socket", False), (private_bus.ADDRESS, True)]:
            with self.subTest(address=address, exited=exited):
                calls = []
                @contextmanager
                def bus_factory():
                    yield address, Process([], "bus", exited=exited)
                with self.assertRaisesRegex(RuntimeError, "private bus"):
                    with networkmanager.networkmanager(isolation_check=lambda: None, bus_factory=bus_factory, popen=lambda *args, **kwargs: calls.append(args)):
                        self.fail("must not enter")
                self.assertEqual(calls, [])

    def test_unresponsive_daemon_is_killed_and_reaped(self):
        events = []
        @contextmanager
        def bus_factory():
            yield private_bus.ADDRESS, Process(events, "bus")
        daemon = Process(events, "nm", timeout=True)
        with networkmanager.networkmanager(isolation_check=lambda: None, bus_factory=bus_factory, popen=lambda *args, **kwargs: daemon, capture_factory=FakeCapture, shutdown_timeout=2):
            pass
        self.assertEqual(events, [("nm", "terminate"), ("nm", "wait", 2), ("nm", "kill"), ("nm", "wait", None)])


if __name__ == "__main__":
    unittest.main()
