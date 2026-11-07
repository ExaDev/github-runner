"""Tests for the independently running host watchdog."""

import multiprocessing
import socket
import threading
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULES = Path(__file__).resolve().parents[2] / "plugins/modules"
sys.path.insert(0, str(MODULES))
import connectivity_transaction as transaction
import connectivity_watchdog as watchdog


def acknowledge_then_wait(state_dir, ready):
    root = Path(state_dir)
    manager = transaction.ConnectivityTransaction(root, lambda: None, exclusive_writer=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(root / "watchdog.sock"))
        listener.listen()
        listener.settimeout(3)
        ready.set()
        watchdog.serve_once(listener, manager, "boot", time.monotonic)
        time.sleep(30)


def run_watchdog(state_dir):
    manager = transaction.ConnectivityTransaction(Path(state_dir), lambda: None, exclusive_writer=True)
    while watchdog.scan(manager, time.monotonic(), "boot") not in ("rolled_back", "committed"):
        time.sleep(0.01)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "dropin"
        self.target.write_bytes(b"old")
        self.manager = transaction.ConnectivityTransaction(self.root, lambda: None, exclusive_writer=True)
        if sys.platform == "darwin":
            directory_patcher = patch.object(transaction, "_directory_has_metadata", return_value=False)
            directory_patcher.start()
            self.addCleanup(directory_patcher.stop)
            patcher = patch.object(transaction, "_has_metadata", return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)

    def prepare(self, deadline):
        class Scheduler:
            def arm(self, *args):
                pass

            def is_armed(self, *args):
                return True

        self.manager.begin("one", self.target, b"new", deadline=deadline, scheduler=Scheduler(), now=time.monotonic, boot_id="boot")

    def test_independent_worker_restores_after_expiry(self):
        self.prepare(time.monotonic() + 0.3)
        process = multiprocessing.get_context("fork").Process(target=run_watchdog, args=(str(self.root),))
        process.start()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join()
            self.fail("watchdog did not expire transaction")
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(self.target.read_bytes(), b"old")
        self.assertEqual(self.manager._read()["phase"], "rolled_back")

    def test_restart_finishes_interrupted_rollback(self):
        self.prepare(time.monotonic() + 60)
        state = self.manager._read()
        state["phase"] = "rolling_back"
        self.manager._save(state)
        self.assertEqual(watchdog.scan(self.manager, time.monotonic(), "boot"), "rolled_back")
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_restart_expires_prepared_state_and_preserves_committed(self):
        self.prepare(time.monotonic() + 60)
        state = self.manager._read()
        state["phase"] = "prepared"
        self.manager._save(state)
        self.assertEqual(watchdog.scan(self.manager, state["deadline"], "boot"), "rolled_back")
        self.manager.begin("two", self.target, b"committed")
        self.assertTrue(self.manager.commit("two"))
        self.assertEqual(watchdog.scan(self.manager, time.monotonic(), "boot"), "committed")
        self.assertEqual(self.target.read_bytes(), b"committed")

    def test_reboot_expires_and_blocks_commit_even_before_old_deadline(self):
        self.prepare(time.monotonic() + 60)
        self.assertFalse(self.manager.commit("one", now=time.monotonic, boot_id="new-boot"))
        self.assertEqual(watchdog.scan(self.manager, 0, "new-boot"), "rolled_back")
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_same_boot_does_not_expire_before_monotonic_deadline(self):
        self.prepare(time.monotonic() + 60)
        with patch.object(watchdog.time, "time", return_value=10**12):
            self.assertEqual(watchdog.scan(self.manager, time.monotonic(), "boot"), "pending")
        self.assertEqual(self.target.read_bytes(), b"new")

    def test_worker_challenge_checks_generation_and_liveness(self):
        self.prepare(time.monotonic() + 60)
        state = self.manager._read()
        state["phase"] = "prepared"
        self.manager._save(state)
        address = str(self.root / "watchdog.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(address)
        listener.listen()
        listener.settimeout(2)
        self.addCleanup(listener.close)

        def probe(generation):
            worker = threading.Thread(target=watchdog.serve_once, args=(listener, self.manager, "boot", time.monotonic))
            worker.start()
            result = watchdog.probe_worker(self.root, "one", generation, state["deadline"])
            worker.join(3)
            self.assertFalse(worker.is_alive())
            return result

        self.assertTrue(probe(state["generation"]))
        self.assertFalse(probe("stale"))
        listener.close()
        self.assertFalse(watchdog.probe_worker(self.root, "one", state["generation"], state["deadline"]))

    def test_worker_death_after_acknowledgement_leaves_state_for_restart(self):
        self.prepare(time.monotonic() + 60)
        state = self.manager._read()
        state["phase"] = "prepared"
        self.manager._save(state)
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        worker = context.Process(target=acknowledge_then_wait, args=(str(self.root), ready))
        worker.start()
        try:
            self.assertTrue(ready.wait(3))
            self.assertTrue(watchdog.probe_worker(self.root, "one", state["generation"], state["deadline"]))
            worker.kill()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(watchdog.probe_worker(self.root, "one", state["generation"], state["deadline"]))
            self.assertEqual(self.target.read_bytes(), b"new")
            self.assertEqual(watchdog.scan(self.manager, state["deadline"], "boot"), "rolled_back")
            self.assertEqual(self.target.read_bytes(), b"old")
        finally:
            if worker.is_alive():
                worker.kill()
                worker.join(3)

    def test_worker_cannot_acknowledge_hung_scan(self):
        self.prepare(time.monotonic() + 60)
        state = self.manager._read()
        state["phase"] = "prepared"
        self.manager._save(state)
        request = {"id": "one", "generation": state["generation"], "deadline": state["deadline"], "challenge": "fresh"}
        with self.manager._locked():
            result = watchdog.answer_probe(self.manager, request, time.monotonic(), "boot")
        self.assertEqual(result, {"challenge": "fresh", "ready": False})

    def test_active_socket_without_worker_is_not_ready(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.root / "watchdog.sock"))
        listener.listen()
        self.addCleanup(listener.close)
        with patch.object(watchdog, "HANDSHAKE_TIMEOUT_SECONDS", 0.05):
            self.assertFalse(watchdog.probe_worker(self.root, "one", "generation", 10))

    def test_scheduler_handshake_does_not_hold_transaction_lock(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.root / "watchdog.sock"))
        listener.listen()
        listener.settimeout(2)
        self.addCleanup(listener.close)
        def serve():
            for _ in range(2):
                watchdog.serve_once(listener, self.manager, "boot", time.monotonic)
        class ProbeScheduler(watchdog.ServiceScheduler):
            def arm(self, transaction_id, generation, deadline):
                if not self.is_armed(transaction_id, generation, deadline):
                    raise RuntimeError("worker not ready")
        worker = threading.Thread(target=serve)
        worker.start()
        try:
            self.manager.begin("one", self.target, b"new", deadline=time.monotonic() + 10, scheduler=ProbeScheduler(self.root), now=time.monotonic, boot_id="boot")
        finally:
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.target.read_bytes(), b"new")

    def test_expiry_during_arm_prevents_application(self):
        manager = self.manager
        class ExpiringScheduler:
            def arm(self, transaction_id, generation, deadline):
                self.assert_expired = manager.expire(transaction_id, generation, deadline, "boot")
            def is_armed(self, transaction_id, generation, deadline):
                return True
        scheduler = ExpiringScheduler()
        with self.assertRaisesRegex(RuntimeError, "changed while arming"):
            manager.begin("one", self.target, b"new", deadline=time.monotonic() + 10, scheduler=scheduler, now=time.monotonic, boot_id="boot")
        self.assertTrue(scheduler.assert_expired)
        self.assertEqual(self.target.read_bytes(), b"old")

    def test_replaced_generation_during_arm_prevents_application(self):
        manager = self.manager
        class ReplacingScheduler:
            def arm(self, transaction_id, generation, deadline):
                manager.expire(transaction_id, generation, deadline, "boot")
                manager.begin("two", self_target, b"replacement")
            def is_armed(self, transaction_id, generation, deadline):
                return True
        self_target = self.target
        with self.assertRaisesRegex(RuntimeError, "changed while arming"):
            manager.begin("one", self.target, b"new", deadline=time.monotonic() + 10, scheduler=ReplacingScheduler(), now=time.monotonic, boot_id="boot")
        self.assertEqual(self.target.read_bytes(), b"replacement")

    def test_real_scheduler_refuses_even_after_worker_acknowledges(self):
        self.prepare(time.monotonic() + 60)
        state = self.manager._read()
        with patch.object(watchdog, "probe_worker", return_value=True):
            scheduler = watchdog.ServiceScheduler(self.root)
            self.assertTrue(scheduler.is_armed("one", state["generation"], state["deadline"]))
            with self.assertRaisesRegex(RuntimeError, "arm disabled"):
                scheduler.arm("one", state["generation"], state["deadline"])
        self.assertEqual(self.target.read_bytes(), b"new")

    def test_scheduler_refuses_unverified_readiness(self):
        scheduler = watchdog.ServiceScheduler(self.root)
        self.assertFalse(scheduler.is_armed("one", "generation", 12))
        with self.assertRaisesRegex(RuntimeError, "arm disabled"):
            scheduler.arm("one", "generation", 12)

    def test_rollback_reload_has_a_timeout_and_remains_recoverable(self):
        self.prepare(time.monotonic() + 60)
        manager = transaction.ConnectivityTransaction(self.root, watchdog.reload_networkmanager, exclusive_writer=True)
        with patch.object(watchdog.subprocess, "run", side_effect=watchdog.subprocess.TimeoutExpired("nmcli", watchdog.RELOAD_TIMEOUT_SECONDS)) as run:
            with self.assertRaises(watchdog.subprocess.TimeoutExpired):
                watchdog.scan(manager, time.monotonic() + 120, "boot")
            self.assertEqual(run.call_args.kwargs["timeout"], watchdog.RELOAD_TIMEOUT_SECONDS)
        self.assertEqual(self.manager._read()["phase"], "rolling_back")
        self.assertEqual(watchdog.scan(self.manager, time.monotonic(), "boot"), "rolled_back")


if __name__ == "__main__":
    unittest.main()
