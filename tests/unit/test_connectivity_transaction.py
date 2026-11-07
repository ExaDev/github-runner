"""Tests for the standalone connectivity transaction."""

import ctypes
import importlib.util
import sys
import multiprocessing
import os
import stat
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

MODULE = Path(__file__).resolve().parents[2] / "plugins/modules/connectivity_transaction.py"
spec = importlib.util.spec_from_file_location("connectivity_transaction", MODULE)
assert spec is not None and spec.loader is not None
transaction = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transaction)


def crash_during_reload(state_dir, target):
    def crash():
        os._exit(73)

    transaction.ConnectivityTransaction(Path(state_dir), crash, exclusive_writer=True).begin("crash", Path(target), b"new")


def begin_with_blocked_reload(state_dir, target, entered, release, finished):
    def reload():
        entered.set()
        try:
            release.wait(15)
        finally:
            finished.set()

    scheduler = FakeScheduler()
    manager = transaction.ConnectivityTransaction(Path(state_dir), reload, exclusive_writer=True)
    manager.begin("crash", Path(target), b"new", deadline=60, scheduler=scheduler, now=lambda: 0, boot_id="boot")


def recover_after_crash(state_dir, results):
    manager = transaction.ConnectivityTransaction(Path(state_dir), lambda: None, exclusive_writer=True)
    results.put(manager.recover())


def race(state_dir, operation, gate, results):
    manager = transaction.ConnectivityTransaction(Path(state_dir), lambda: None, exclusive_writer=True)
    gate.wait()
    results.put((operation, getattr(manager, operation)("one")))


class FakeScheduler:
    def __init__(self, fail=None):
        self.armed = None
        self.fail = fail

    def arm(self, transaction_id, generation, deadline):
        if self.fail == "before_arm":
            raise RuntimeError("crash before arm")
        self.armed = (transaction_id, generation, deadline)
        if self.fail == "after_arm":
            raise RuntimeError("crash after arm")

    def is_armed(self, transaction_id, generation, deadline):
        if self.fail == "verify":
            return False
        return self.armed == (transaction_id, generation, deadline)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "dropin"
        self.reloads = []
        self.manager = transaction.ConnectivityTransaction(self.root, lambda: self.reloads.append(True), exclusive_writer=True)
        if sys.platform == "darwin":
            directory_patcher = patch.object(transaction, "_directory_has_metadata", return_value=False)
            directory_patcher.start()
            self.addCleanup(directory_patcher.stop)
            patcher = patch.object(transaction, "_has_metadata", return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_restores_exact_bytes_and_mode(self):
        self.target.write_bytes(b"\x00old\xff\n")
        self.target.chmod(0o751)
        self.manager.begin("one", self.target, b"new")
        self.assertEqual(self.target.read_bytes(), b"new")
        self.assertTrue(self.manager.rollback("one"))
        self.assertEqual(self.target.read_bytes(), b"\x00old\xff\n")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o751)
        self.assertEqual(len(self.reloads), 2)
        self.assertEqual(self.manager.recover(), "rolled_back")
        self.assertEqual(len(self.reloads), 2)

    def test_restores_absence_and_commit_survives_restart(self):
        self.manager.begin("one", self.target, b"new")
        self.assertTrue(self.manager.rollback("one"))
        self.assertFalse(self.target.exists())
        self.manager.begin("two", self.target, b"new")
        self.assertTrue(self.manager.commit("two"))
        self.assertEqual(transaction.ConnectivityTransaction(self.root, lambda: self.fail("unexpected reload"), exclusive_writer=True).recover(), "committed")
        self.assertEqual(self.target.read_bytes(), b"new")
        self.assertFalse(self.manager.rollback("two"))

    def test_recovery_after_apply_reload_failure_and_rollback_reload_failure(self):
        self.target.write_bytes(b"original")
        def fail():
            raise RuntimeError("reload failed")
        failing = transaction.ConnectivityTransaction(self.root, fail, exclusive_writer=True)
        with self.assertRaisesRegex(RuntimeError, "reload failed"):
            failing.begin("one", self.target, b"new")
        self.assertEqual(self.target.read_bytes(), b"new")
        with self.assertRaisesRegex(RuntimeError, "reload failed"):
            failing.recover()
        self.assertEqual(self.target.read_bytes(), b"original")
        self.assertEqual(self.manager.recover(), "rolled_back")
        self.assertEqual(self.target.read_bytes(), b"original")

    def test_process_crash_releases_lock_and_recovery_restores_snapshot(self):
        self.target.write_bytes(b"old")
        context = multiprocessing.get_context("fork")
        process = context.Process(target=crash_during_reload, args=(str(self.root), str(self.target)))
        process.start()
        process.join(10)
        self.assertEqual(process.exitcode, 73)
        self.assertEqual(self.target.read_bytes(), b"new")
        self.assertEqual(self.manager.recover(), "rolled_back")
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_recovery_after_parent_dies_during_scheduled_reload(self):
        self.target.write_bytes(b"old")
        context = multiprocessing.get_context("fork")
        entered, release, finished = context.Event(), context.Event(), context.Event()
        results = context.Queue()
        parent = context.Process(target=begin_with_blocked_reload, args=(str(self.root), str(self.target), entered, release, finished))
        recovery = context.Process(target=recover_after_crash, args=(str(self.root), results))
        parent.start()
        try:
            self.assertTrue(entered.wait(5), "reload did not start")
            self.assertEqual(self.target.read_bytes(), b"new")
            parent.kill()
            parent.join(5)
            self.assertFalse(parent.is_alive(), "transaction parent did not exit")
            recovery.start()
            recovery.join(3)
            self.assertFalse(recovery.is_alive(), "recovery blocked on a lock inherited by the reload child")
            self.assertEqual(recovery.exitcode, 0)
            self.assertEqual(results.get(timeout=2), "rolled_back")
            self.assertEqual(self.target.read_bytes(), b"old")
            self.assertFalse(finished.is_set(), "original reload must still be running when recovery finishes")
        finally:
            release.set()
            if entered.is_set():
                finished.wait(5)
            for process in (parent, recovery):
                if process.pid is not None and process.is_alive():
                    process.kill()
                    process.join(5)
            results.close()
            results.join_thread()

    def test_commit_and_rollback_are_serialised_across_processes(self):
        self.target.write_bytes(b"old")
        self.manager.begin("one", self.target, b"new")
        context = multiprocessing.get_context("fork")
        gate = context.Event()
        results = context.Queue()
        processes = [context.Process(target=race, args=(str(self.root), operation, gate, results)) for operation in ("commit", "rollback")]
        for process in processes:
            process.start()
        gate.set()
        outcomes = dict(results.get(timeout=10) for _ in processes)
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertNotEqual(outcomes["commit"], outcomes["rollback"])
        self.assertEqual(self.target.read_bytes(), b"new" if outcomes["commit"] else b"old")
        self.assertEqual(self.manager.recover(), "committed" if outcomes["commit"] else "rolled_back")

    def test_exclusive_writer_contract_is_required(self):
        with self.assertRaisesRegex(ValueError, "exclusive control"):
            transaction.ConnectivityTransaction(self.root, lambda: None, exclusive_writer=False)

    def test_external_change_is_not_overwritten(self):
        self.target.write_bytes(b"old")
        self.manager.begin("one", self.target, b"new")
        self.target.write_bytes(b"external")
        with self.assertRaisesRegex(RuntimeError, "changed outside"):
            self.manager.rollback("one")
        self.assertEqual(self.target.read_bytes(), b"external")
        with self.assertRaisesRegex(RuntimeError, "changed outside"):
            self.manager.recover()

    def test_symlink_is_refused_without_following(self):
        other = self.root / "other"
        other.write_bytes(b"safe")
        self.target.symlink_to(other)
        with self.assertRaises(OSError):
            self.manager.begin("one", self.target, b"new")
        self.assertEqual(other.read_bytes(), b"safe")

    def test_extended_attributes_are_refused(self):
        self.target.write_bytes(b"old")
        if sys.platform == "darwin":
            libc = ctypes.CDLL(None, use_errno=True)
            result = libc.setxattr(os.fsencode(self.target), b"user.transaction_test", b"metadata", 8, 0, 0)
            self.assertEqual(result, 0)
        else:
            os.setxattr(self.target, "user.transaction_test", b"metadata")
        with patch.object(transaction, "_has_metadata", wraps=transaction._has_metadata if sys.platform != "darwin" else lambda fd: True):
            with self.assertRaisesRegex(ValueError, "extended metadata"):
                self.manager.begin("one", self.target, b"new")
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_hard_link_is_refused(self):
        self.target.write_bytes(b"old")
        other = self.root / "other"
        os.link(self.target, other)
        with self.assertRaisesRegex(ValueError, "hard links"):
            self.manager.begin("one", self.target, b"new")
        self.assertEqual(other.read_bytes(), b"old")

    def test_inherited_metadata_refuses_replacement_before_rename(self):
        self.target.write_bytes(b"old")
        with patch.object(transaction, "_has_metadata", side_effect=[False, False, True]):
            with self.assertRaisesRegex(ValueError, "replacement inherited"):
                self.manager.begin("one", self.target, b"new")
        self.assertEqual(self.target.read_bytes(), b"old")
        self.assertEqual(self.manager.recover(), "rolled_back")
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_inherited_metadata_refuses_rollback_before_rename(self):
        self.target.write_bytes(b"old")
        self.manager.begin("one", self.target, b"new")
        with patch.object(transaction, "_has_metadata", side_effect=[False, True]):
            with self.assertRaisesRegex(ValueError, "replacement inherited"):
                self.manager.rollback("one")
        self.assertEqual(self.target.read_bytes(), b"new")
        self.assertEqual(self.manager.recover(), "rolled_back")
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_invalid_modes_are_rejected_before_changes(self):
        self.target.write_bytes(b"old")
        for mode in (stat.S_IFREG | 0o644, -1, 0o10000, True, "644"):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "mode must contain"):
                    self.manager.begin("one", self.target, b"new", mode)
                self.assertEqual(self.target.read_bytes(), b"old")
                self.assertFalse(self.manager.state_path.exists())

    def test_unreadable_owner_mode_is_rejected_before_changes(self):
        self.target.write_bytes(b"old")
        for mode in (0o000, 0o077):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, "owner read permission"):
                    self.manager.begin("one", self.target, b"new", mode)
                self.assertEqual(self.target.read_bytes(), b"old")
                self.assertFalse(self.manager.state_path.exists())

    def test_special_permission_bits_survive_comparison_and_rollback(self):
        self.target.write_bytes(b"old")
        self.manager.begin("one", self.target, b"new", 0o1750)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o1750)
        self.assertTrue(self.manager.rollback("one"))
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_scheduler_boundaries_and_expiry(self):
        for failure in ("before_arm", "after_arm", "verify", "reload", None):
            with self.subTest(failure=failure):
                root = self.root / str(failure)
                root.mkdir(mode=0o700)
                target = root / "dropin"
                target.write_bytes(b"old")
                scheduler = FakeScheduler(failure)
                def reload():
                    if failure == "reload":
                        raise RuntimeError("controller lost")
                manager = transaction.ConnectivityTransaction(root, reload, exclusive_writer=True)
                if failure is None:
                    manager.begin("one", target, b"new", deadline=20, scheduler=scheduler, now=lambda: 10, boot_id="boot")
                else:
                    with self.assertRaises(RuntimeError):
                        manager.begin("one", target, b"new", deadline=20, scheduler=scheduler, now=lambda: 10, boot_id="boot")
                recovery = transaction.ConnectivityTransaction(root, lambda: None, exclusive_writer=True)
                generation = recovery._read()["generation"]
                self.assertFalse(recovery.expire("one", generation, 19, "boot"))
                if scheduler.armed is None:
                    self.assertEqual(target.read_bytes(), b"old")
                    self.assertEqual(recovery.recover(), "rolled_back")
                else:
                    self.assertTrue(recovery.expire("one", generation, 20, "boot"))
                self.assertEqual(target.read_bytes(), b"old")

    def test_expired_watchdog_cannot_rollback_committed_or_new_transaction(self):
        scheduler = FakeScheduler()
        self.manager.begin("one", self.target, b"new", deadline=20, scheduler=scheduler, now=lambda: 10, boot_id="boot")
        generation = scheduler.armed[1]
        self.assertTrue(self.manager.commit("one", now=lambda: 19, boot_id="boot"))
        self.assertFalse(self.manager.expire("one", generation, 20, "boot"))
        self.manager.begin("one", self.target, b"newer", deadline=30, scheduler=scheduler, now=lambda: 10, boot_id="boot")
        self.assertNotEqual(generation, scheduler.armed[1])
        self.assertFalse(self.manager.expire("one", generation, 30, "boot"))
        self.assertEqual(self.target.read_bytes(), b"newer")
        self.assertTrue(self.manager.expire("one", scheduler.armed[1], 30, "boot"))
        self.assertEqual(self.target.read_bytes(), b"new")

    def test_commit_at_deadline_cannot_beat_watchdog(self):
        scheduler = FakeScheduler()
        self.target.write_bytes(b"old")
        self.manager.begin("one", self.target, b"new", deadline=20, scheduler=scheduler, now=lambda: 10, boot_id="boot")
        with self.assertRaisesRegex(ValueError, "clock"):
            self.manager.commit("one")
        self.assertFalse(self.manager.commit("one", now=lambda: 20, boot_id="boot"))
        self.assertTrue(self.manager.expire("one", scheduler.armed[1], 20, "boot"))
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_hung_scheduled_reload_releases_lock_for_expiry(self):
        self.target.write_bytes(b"old")
        scheduler = FakeScheduler()
        gate = multiprocessing.get_context("fork").Event()
        manager = transaction.ConnectivityTransaction(self.root, gate.wait, exclusive_writer=True)
        with self.assertRaises(TimeoutError):
            manager.begin("one", self.target, b"new", deadline=0.1, scheduler=scheduler, now=lambda: 0, boot_id="boot")
        self.assertTrue(self.manager.expire("one", scheduler.armed[1], 0.1, "boot"))
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_state_directory_permissions_and_symlink_are_rejected(self):
        self.root.chmod(0o755)
        with self.assertRaisesRegex(PermissionError, "0700"):
            self.manager.begin("one", self.target, b"new")
        self.assertFalse(self.manager.state_path.exists())
        self.root.chmod(0o700)
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        manager = transaction.ConnectivityTransaction(link, lambda: None, exclusive_writer=True)
        with self.assertRaises(OSError):
            manager.begin("one", self.target, b"new")
        self.assertFalse(self.manager.state_path.exists())

    def test_state_directory_owner_and_metadata_are_checked(self):
        with patch.object(transaction.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(PermissionError, "owned"):
                self.manager.begin("one", self.target, b"new")
        with patch.object(transaction, "_directory_has_metadata", return_value=True):
            with self.assertRaisesRegex(PermissionError, "metadata"):
                self.manager.begin("one", self.target, b"new")
        self.assertFalse(self.manager.state_path.exists())

    def test_separate_module_refuses_before_any_writer_action(self):
        import ast
        module_path = MODULE.parent / "networkmanager_connectivity.py"
        tree = ast.parse(module_path.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
        self.assertIsInstance(function.body[0], ast.Expr)
        call = function.body[0].value
        self.assertIsInstance(call, ast.Call)
        self.assertIsInstance(call.func, ast.Attribute)
        self.assertEqual(call.func.attr, "fail_json")

    def test_nonparticipating_writer_can_replace_file_despite_lock(self):
        self.target.write_bytes(b"old")
        replacement = self.root / "replacement"
        replacement.write_bytes(b"external")
        with self.manager._locked():
            os.replace(replacement, self.target)
        self.assertEqual(self.target.read_bytes(), b"external")

    def test_lock_symlink_is_rejected(self):
        other = self.root / "other"
        other.write_text("safe")
        self.manager.lock_path.symlink_to(other)
        with self.assertRaises(OSError):
            self.manager.begin("one", self.target, b"new")
        self.assertEqual(other.read_text(), "safe")

    def test_active_transaction_and_wrong_id_are_rejected(self):
        self.manager.begin("one", self.target, b"new")
        with self.assertRaises(RuntimeError):
            self.manager.begin("two", self.target, b"other")
        with self.assertRaises(ValueError):
            self.manager.commit("two")
        with self.assertRaises(ValueError):
            self.manager.rollback("two")
        self.assertEqual(self.target.read_bytes(), b"new")


if __name__ == "__main__":
    unittest.main()
