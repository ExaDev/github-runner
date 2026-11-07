"""Injected non-root launcher tests without starting processes."""

import io
import os
import threading
import types
import unittest

import client_launcher
import private_bus


class Process:
    def __init__(self):
        self.stdout = io.BytesIO()
        self.events = []
    def poll(self):
        return None
    def terminate(self):
        self.events.append("terminate")
    def wait(self, *, timeout=None):
        self.events.append(("wait", timeout))
        return 0


class LauncherTests(unittest.TestCase):
    def test_nonroot_identity_private_bus_and_flags(self):
        calls = []
        process = Process()
        launcher = client_launcher.Launcher("/profile", guard=lambda: None, lookup=lambda name: types.SimpleNamespace(pw_uid=1001, pw_gid=1002), popen=lambda cmd, **kwargs: calls.append((cmd, kwargs)) or process)
        child = launcher.start(metric=4, unfenced=True, disconnect_after_held=True, timeout=2)
        command, options = calls[0]
        self.assertIn(private_bus.ADDRESS, command)
        self.assertIn("--unfenced", command)
        self.assertIn("--disconnect-after-held", command)
        self.assertEqual((options["user"], options["group"], options["extra_groups"]), (1001, 1002, ()))
        child.close()
        self.assertEqual(process.events, ["terminate", ("wait", 5)])
        self.assertTrue(process.stdout.closed)

    def test_fragmented_request_line(self):
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb")
        process = types.SimpleNamespace(stdout=stream)
        os.write(write_fd, b"req")
        timer = threading.Timer(0.02, lambda: os.write(write_fd, b"uest\n"))
        timer.start()
        try:
            self.assertEqual(client_launcher.Child(process).read_request_id(timeout=1), "request")
        finally:
            timer.join()
            os.close(write_fd)
            stream.close()

    def test_guard_or_root_refuses_before_launch(self):
        calls = []
        def refuse():
            raise RuntimeError("unsafe")
        launcher = client_launcher.Launcher("/profile", guard=refuse, popen=lambda *args, **kwargs: calls.append(args))
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            launcher.start(metric=1, unfenced=False, disconnect_after_held=False, timeout=1)
        launcher = client_launcher.Launcher("/profile", guard=lambda: None, lookup=lambda name: types.SimpleNamespace(pw_uid=0, pw_gid=1), popen=lambda *args, **kwargs: calls.append(args))
        with self.assertRaisesRegex(RuntimeError, "non-root"):
            launcher.start(metric=1, unfenced=False, disconnect_after_held=False, timeout=1)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
