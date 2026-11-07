"""Injected QEMU lifecycle tests; no image utility or VM is launched."""

import json
import os
import types
import tempfile
import unittest
from pathlib import Path

import qemu_vm


class Process:
    pid = 123

    def __init__(self):
        self.events = []

    def poll(self):
        return None

    def terminate(self):
        self.events.append("terminate")

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))


class QemuTests(unittest.TestCase):
    def test_launch_command_overlay_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = str(Path(temp_root).resolve())
            base = Path(root) / "base.qcow2"
            base.touch()
            base.chmod(0o400)
            work = Path(root) / "work"
            work.mkdir(mode=0o700)
            calls, checks, process = [], [], Process()
            def run(*args, **kwargs):
                calls.append((args[0], kwargs))
                return types.SimpleNamespace(stdout=json.dumps([{"filename": str(base), "format": "qcow2", "format-specific": {"type": "qcow2", "data": {}}}]))
            def popen(command, **kwargs):
                calls.append((command, kwargs))
                return process
            def verify(pid, executable, command):
                checks.append((pid, executable, command))
            with qemu_vm.disposable_vm(base, work, qemu="/usr/bin/qemu", qemu_img="/usr/bin/qemu-img", run=run, popen=popen, verify=verify) as (name, overlay, child):
                self.assertTrue(name.startswith("nm-isolated-"))
                self.assertEqual(overlay.parent, work)
                self.assertIs(child, process)
                self.assertEqual(calls[0][0], ["/usr/bin/qemu-img", "info", "--output=json", "--backing-chain", "--", str(base)])
                self.assertEqual(calls[1][0], ["/usr/bin/qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(overlay)])
                command = calls[2][0]
                self.assertIn(f"socket,id=fixture,path={work / 'guest.sock'},server=on,wait=off", command)
                self.assertIn("virtserialport,chardev=fixture,name=test.nm.fixture", command)
                self.assertIn("-nodefaults", command)
                self.assertEqual(command[command.index("-accel") + 1], "kvm")
                self.assertIn("-no-user-config", command)
                self.assertEqual(command[command.index("-nic") + 1], "none")
                self.assertFalse(any(option in command for option in ("-virtfs", "-fsdev", "-netdev")))
                self.assertEqual(checks, [(123, "/usr/bin/qemu", command)])
            self.assertEqual(process.events, ["terminate", ("wait", 5)])

    def test_rejects_writable_remote_and_backed_images(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = str(Path(temp_root).resolve())
            base = Path(root) / "base"
            base.touch()
            calls = []
            def run(*args, **kwargs):
                calls.append(args)
                return types.SimpleNamespace(stdout=json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "write permissions"):
                qemu_vm.validate_base(base, "/img", run=run)
            self.assertEqual(calls, [])
            base.chmod(0o400)
            with self.assertRaisesRegex(ValueError, "local path"):
                qemu_vm.validate_base("/remote:http", "/img", run=run)
            payload = [{"filename": str(base), "format": "qcow2", "format-specific": {"type": "qcow2", "data": {}}}, {"filename": "/other"}]
            with self.assertRaisesRegex(ValueError, "backing chain"):
                qemu_vm.validate_base(base, "/img", run=run)
            payload = [{"filename": str(base), "format": "qcow2", "backing-filename": "https://example.com/disk", "format-specific": {"type": "qcow2", "data": {}}}]
            with self.assertRaisesRegex(ValueError, "backing"):
                qemu_vm.validate_base(base, "/img", run=run)
            payload = [{"filename": str(base), "format": "qcow2", "format-specific": {"type": "qcow2", "data": {"data-file": "/other"}}}]
            with self.assertRaisesRegex(ValueError, "data file"):
                qemu_vm.validate_base(base, "/img", run=run)

    def test_process_identity_matches_complete_arguments(self):
        args = ["/usr/bin/qemu", "-nic", "none"]
        qemu_vm.verify_process(4, args[0], args, readlink=lambda name: os.path.realpath(args[0]), read_bytes=lambda name: b"/usr/bin/qemu\0-nic\0none\0")
        with self.assertRaisesRegex(RuntimeError, "command identity"):
            qemu_vm.verify_process(4, args[0], args, readlink=lambda name: os.path.realpath(args[0]), read_bytes=lambda name: b"/usr/bin/qemu\0-nic\0user\0")
        with self.assertRaisesRegex(RuntimeError, "executable identity"):
            qemu_vm.verify_process(4, args[0], args, readlink=lambda name: "/other", read_bytes=lambda name: b"")

    def test_an_empty_argument_area_during_exec_is_retried_then_matches(self):
        args = ["/usr/bin/qemu", "-nic", "none"]
        reads = iter([b"", b"", b"/usr/bin/qemu\0-nic\0none\0"])
        sleeps = []
        qemu_vm.verify_process(4, args[0], args, readlink=lambda name: os.path.realpath(args[0]), read_bytes=lambda name: next(reads), sleep=sleeps.append)
        self.assertEqual(len(sleeps), 2)

    def test_an_argument_area_that_stays_empty_fails_after_the_bound(self):
        args = ["/usr/bin/qemu", "-nic", "none"]
        sleeps = []
        with self.assertRaisesRegex(RuntimeError, "command identity"):
            qemu_vm.verify_process(4, args[0], args, readlink=lambda name: os.path.realpath(args[0]), read_bytes=lambda name: b"", sleep=sleeps.append, empty_reads=5)
        self.assertEqual(len(sleeps), 5)

    def test_a_non_empty_wrong_command_is_never_retried(self):
        args = ["/usr/bin/qemu", "-nic", "none"]
        sleeps = []
        with self.assertRaisesRegex(RuntimeError, "command identity"):
            qemu_vm.verify_process(4, args[0], args, readlink=lambda name: os.path.realpath(args[0]), read_bytes=lambda name: b"/usr/bin/qemu\0-nic\0user\0", sleep=sleeps.append)
        self.assertEqual(sleeps, [])

    def test_identity_failure_reaps_child(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = str(Path(temp_root).resolve())
            base = Path(root) / "base"
            base.touch()
            base.chmod(0o400)
            work = Path(root) / "work"
            work.mkdir(mode=0o700)
            process = Process()
            def refuse(*args):
                raise RuntimeError("identity")
            def run(*args, **kwargs):
                return types.SimpleNamespace(stdout=json.dumps([{"filename": str(base), "format": "qcow2", "format-specific": {"type": "qcow2", "data": {}}}]))
            with self.assertRaisesRegex(RuntimeError, "identity"):
                with qemu_vm.disposable_vm(base, work, qemu="/qemu", qemu_img="/img", run=run, popen=lambda *args, **kwargs: process, verify=refuse):
                    self.fail("must not enter")
            self.assertEqual(process.events, ["terminate", ("wait", 5)])


if __name__ == "__main__":
    unittest.main()
