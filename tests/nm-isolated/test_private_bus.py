"""Private bus lifecycle tests using an injected child, never a daemon."""

import subprocess
import types
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
from pathlib import Path

import private_bus


class Child:
    def __init__(self, running=True, timeout=False):
        self.running = running
        self.timeout = timeout
        self.events = []

    def poll(self):
        self.events.append("poll")
        return None if self.running else 1

    def terminate(self):
        self.events.append("terminate")

    def kill(self):
        self.events.append("kill")

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))
        if self.timeout and timeout is not None:
            raise subprocess.TimeoutExpired("dbus-daemon", timeout)
        return 0


class PrivateBusTests(unittest.TestCase):
    def start(self, child, checks, calls):
        def check():
            checks.append("checked")
        def popen(command, **kwargs):
            calls.append((command, kwargs))
            return child
        return private_bus.private_bus(check=check, popen=popen, ready=lambda process, timeout: None, shutdown_timeout=2)

    def test_explicit_private_address_and_cleanup(self):
        child, checks, calls = Child(), [], []
        with self.start(child, checks, calls) as (address, process):
            self.assertEqual(checks, ["checked"])
            self.assertEqual(address, "unix:path=/run/nm-isolated-test/bus")
            self.assertIs(process, child)
            self.assertEqual(calls[0][0], ["dbus-daemon", "--config-file=" + str(Path(private_bus.__file__).with_name("bus.conf")), "--nofork"])
            self.assertEqual(calls[0][1], {"stdin": subprocess.DEVNULL})
        self.assertEqual(child.events, ["poll", "terminate", ("wait", 2)])

    def test_config_allows_named_nonroot_client(self):
        config = ET.parse(Path(private_bus.__file__).with_name("bus.conf")).getroot()
        self.assertEqual(config.findtext("listen"), private_bus.ADDRESS)
        policy = config.find("policy[@user='nm-isolated']")
        self.assertIsNotNone(policy)
        self.assertIsNotNone(policy.find("allow[@send_destination='org.freedesktop.NetworkManager']"))
        # A connection permission is bus-global, so dbus-daemon accepts it only in the default policy; it names exactly the two accounts and never a wildcard.
        self.assertIsNone(policy.find("allow[@user]"))
        connect = sorted(rule.get("user") for rule in config.findall("policy[@context='default']/allow[@user]"))
        self.assertEqual(connect, ["nm-isolated", "root"])
        self.assertEqual(config.findall(".//allow[@user='*']"), [])

    def test_readiness_retries_explicit_bus_and_is_bounded(self):
        calls, now = [], [0.0]
        def run(command, **kwargs):
            calls.append((command, kwargs))
            return types.SimpleNamespace(returncode=0 if len(calls) == 2 else 1)
        def pause(duration):
            now[0] += duration
        private_bus.wait_ready(Child(), 1, run=run, clock=lambda: now[0], pause=pause)
        self.assertEqual(len(calls), 2)
        self.assertIn("--bus=" + private_bus.ADDRESS, calls[0][0])
        self.assertLess(calls[1][1]["timeout"], calls[0][1]["timeout"])

    def test_readiness_failure_reaps_child(self):
        child = Child()
        def fail(process, timeout):
            raise TimeoutError("not ready")
        with self.assertRaises(TimeoutError):
            with private_bus.private_bus(check=lambda: None, popen=lambda *args, **kwargs: child, ready=fail, shutdown_timeout=2):
                self.fail("must not enter")
        self.assertEqual(child.events, ["poll", "terminate", ("wait", 2)])

    def test_cleanup_on_body_error(self):
        child = Child()
        with self.assertRaisesRegex(ValueError, "body"):
            with self.start(child, [], []):
                raise ValueError("body")
        self.assertEqual(child.events, ["poll", "terminate", ("wait", 2)])

    def test_kills_child_that_does_not_stop(self):
        child = Child(timeout=True)
        with self.start(child, [], []):
            pass
        self.assertEqual(child.events, ["poll", "terminate", ("wait", 2), "kill", ("wait", None)])

    def test_reaps_exited_child_without_signalling(self):
        child = Child(running=False)
        with self.start(child, [], []):
            pass
        self.assertEqual(child.events, ["poll", ("wait", 2)])

    def test_environment_guard_accepts_existing_bus_but_launcher_refuses_it(self):
        with patch.object(private_bus.os, "geteuid", return_value=0), patch.object(Path, "is_symlink", return_value=False), patch.object(Path, "is_file", return_value=True), patch.object(Path, "exists", return_value=True), patch.object(private_bus.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)):
            private_bus.check_environment()
            with self.assertRaisesRegex(RuntimeError, "occupied"):
                with private_bus.private_bus(check=lambda: None):
                    self.fail("must not start")

    def test_guard_failure_never_spawns(self):
        calls = []
        def refuse():
            raise RuntimeError("unsafe")
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            with private_bus.private_bus(check=refuse, popen=lambda *args, **kwargs: calls.append(args)):
                self.fail("must not enter")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
