"""Standalone, host-locked transaction for a connectivity drop-in.

The caller supplies a private, persistent host state directory and a reload callback. Recovery rolls back every transaction that did not durably commit.
"""

import base64
import ctypes
import sys
import fcntl
import json
import math
import multiprocessing
import os
import signal
import uuid
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _has_metadata(fd: int) -> bool:
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        size = libc.flistxattr(fd, None, 0, 0)
        if size < 0:
            raise OSError(ctypes.get_errno(), "flistxattr failed")
        return size > 0
    return bool(os.listxattr(fd))


_directory_has_metadata = _has_metadata


def _snapshot(path: Path) -> Optional[dict]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("drop-in must be a regular file")
        if info.st_nlink != 1:
            raise ValueError("drop-in must not have multiple hard links")
        if info.st_uid != os.geteuid() or info.st_gid != os.getegid():
            raise ValueError("drop-in ownership cannot be preserved")
        if _has_metadata(fd):
            raise ValueError("drop-in extended metadata cannot be preserved")
        with os.fdopen(fd, "rb", closefd=False) as source:
            content = source.read()
        after = os.fstat(fd)
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RuntimeError("drop-in changed during snapshot")
        return {"bytes": base64.b64encode(content).decode("ascii"), "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
    finally:
        os.close(fd)


def _write(path: Path, content: bytes, mode: int, *, check_metadata: bool = False) -> None:
    fd, name = tempfile.mkstemp(prefix=".transaction-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fchown(output.fileno(), os.geteuid(), os.getegid())
            os.fchmod(output.fileno(), mode)
            if check_metadata and _has_metadata(output.fileno()):
                raise ValueError("replacement inherited unsupported extended metadata")
            os.fsync(output.fileno())
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class ConnectivityTransaction:
    """Manage one durable transaction per host state directory.

    The state directory and drop-in parent must already exist. Every writer to the drop-in must use the same state directory and lock, and the caller must prevent non-participating writers for the transaction's entire lifetime. The exclusive_writer argument is an assertion, not enforcement. The content check detects prior changes but cannot prevent a writer racing the check and replacement. Rollback restores bytes, mode, owner and group, or absence; inode identity and timestamps are not restored. Multiple hard links, other ownership, and extended attributes, including Linux POSIX ACLs, are refused. Reload callbacks must be repeatable because recovery can invoke them again after a crash.
    """

    def __init__(self, state_dir: Path, reload: Callable[[], None], *, exclusive_writer: bool) -> None:
        if not exclusive_writer:
            raise ValueError("exclusive control of all drop-in writers is required")
        self.state_dir = Path(state_dir)
        self.reload = reload
        self.state_path = self.state_dir / "connectivity-transaction.json"
        self.lock_path = self.state_dir / "connectivity-transaction.lock"

    @contextmanager
    def _locked(self, *, blocking: bool = True) -> Iterator[int]:
        directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise PermissionError("state directory must be owned by the effective user and have mode 0700")
            if _directory_has_metadata(directory):
                raise PermissionError("state directory must not have extended metadata")
            fd = os.open("connectivity-transaction.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                lock_info = os.fstat(fd)
                if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.geteuid() or stat.S_IMODE(lock_info.st_mode) != 0o600 or lock_info.st_nlink != 1:
                    raise PermissionError("transaction lock must be a private regular file")
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                yield fd
            finally:
                os.close(fd)
        finally:
            os.close(directory)

    def _read(self) -> dict:
        with self.state_path.open("r", encoding="utf-8") as source:
            return json.load(source)

    def _save(self, state: dict) -> None:
        _write(self.state_path, json.dumps(state, sort_keys=True).encode("utf-8"), 0o600)

    def _reload_until(self, timeout: float, lock_fd: int) -> None:
        if timeout <= 0:
            raise TimeoutError("watchdog deadline elapsed before reload")
        def reload_without_lock() -> None:
            os.close(lock_fd)
            self.reload()

        context = multiprocessing.get_context("fork")
        process = context.Process(target=reload_without_lock)
        process.start()
        process.join(timeout)
        if process.is_alive():
            process.terminate()
            process.join(1)
            if process.is_alive():
                process.kill()
                process.join()
            raise TimeoutError("reload exceeded watchdog deadline")
        if process.exitcode != 0:
            raise RuntimeError("reload failed")

    def begin(self, transaction_id: str, path: Path, content: bytes, mode: int = 0o644, *, deadline: Optional[float] = None, scheduler=None, now=None, boot_id=None) -> None:
        """Prepare, optionally arm a scheduler, and apply the drop-in.

        A scheduler must durably arm independently of this process and verify that arm. The caller supplies a boot-scoped, suspend-aware monotonic clock and a boot identifier. A different boot expires the transaction immediately. Scheduled reloads run in a child process and must not depend on changes to the parent's memory.
        """
        if any(value is None for value in (deadline, scheduler, now, boot_id)) and any(value is not None for value in (deadline, scheduler, now, boot_id)):
            raise ValueError("deadline, scheduler, clock and boot identifier must be supplied together")
        if deadline is not None and (not math.isfinite(deadline) or not isinstance(boot_id, str) or not boot_id):
            raise ValueError("finite deadline and non-empty boot identifier required")
        if scheduler is not None and deadline <= now():
            raise ValueError("deadline must be in the future")
        path = Path(path)
        if not path.is_absolute() or not transaction_id:
            raise ValueError("an absolute drop-in path and transaction id are required")
        if not isinstance(mode, int) or isinstance(mode, bool) or mode < 0 or mode != stat.S_IMODE(mode):
            raise ValueError("mode must contain only permission and special bits")
        if not mode & stat.S_IRUSR:
            raise ValueError("mode must grant the owner read permission for recovery")
        with self._locked() as lock_fd:
            if self.state_path.exists() and self._read()["phase"] not in ("committed", "rolled_back"):
                raise RuntimeError("a transaction is already active")
            snapshot = _snapshot(path)
            applied = {"bytes": base64.b64encode(content).decode("ascii"), "mode": mode, "uid": os.geteuid(), "gid": os.getegid()}
            generation = uuid.uuid4().hex
            state = {"id": transaction_id, "generation": generation, "path": str(path), "snapshot": snapshot, "applied": applied, "phase": "prepared", "deadline": deadline, "boot_id": boot_id}
            self._save(state)
        if scheduler is not None:
            scheduler.arm(transaction_id, generation, deadline)
            if not scheduler.is_armed(transaction_id, generation, deadline):
                raise RuntimeError("watchdog arm could not be verified")
        with self._locked() as lock_fd:
            state = self._read()
            if state["id"] != transaction_id or state["generation"] != generation or state["phase"] != "prepared":
                raise RuntimeError("transaction changed while arming watchdog")
            if scheduler is not None and now() >= deadline:
                raise RuntimeError("watchdog deadline elapsed before application")
            if _snapshot(path) != snapshot:
                raise RuntimeError("drop-in changed outside the transaction")
            _write(path, content, mode, check_metadata=True)
            if scheduler is None:
                self.reload()
            else:
                self._reload_until(deadline - now(), lock_fd)
            state["phase"] = "pending"
            self._save(state)

    def commit(self, transaction_id: str, *, now=None, boot_id=None) -> bool:
        """Commit a pending transaction, or return false if rollback won."""
        with self._locked():
            state = self._read()
            self._check_id(state, transaction_id)
            if state["phase"] == "committed":
                return True
            if state["phase"] != "pending":
                return False
            if state["deadline"] is not None:
                if now is None or not boot_id:
                    raise ValueError("a shared clock and boot identifier are required for scheduled commit")
                if state["boot_id"] != boot_id or now() >= state["deadline"]:
                    return False
            state["phase"] = "committed"
            self._save(state)
            return True

    def expire(self, transaction_id: str, generation: str, now: float, boot_id: str) -> bool:
        """Roll back a matching scheduled transaction at or after its deadline."""
        with self._locked():
            state = self._read()
            if state["id"] != transaction_id or state["generation"] != generation:
                return False
            deadline = state["deadline"]
            if deadline is None or (state["boot_id"] == boot_id and now < deadline):
                return False
            return self._rollback(state)

    def rollback(self, transaction_id: str) -> bool:
        """Restore the snapshot unless commit already won."""
        with self._locked():
            state = self._read()
            self._check_id(state, transaction_id)
            return self._rollback(state)

    def recover(self) -> str:
        """Roll back an interrupted transaction and return its durable phase."""
        with self._locked():
            if not self.state_path.exists():
                return "empty"
            state = self._read()
            self._rollback(state)
            return state["phase"]

    @staticmethod
    def _check_id(state: dict, transaction_id: str) -> None:
        if state["id"] != transaction_id:
            raise ValueError("transaction id does not match")

    def _rollback(self, state: dict) -> bool:
        if state["phase"] == "committed":
            return False
        if state["phase"] == "rolled_back":
            return True
        if state["phase"] not in ("prepared", "pending", "rolling_back"):
            raise ValueError("unknown transaction phase")
        path = Path(state["path"])
        snapshot = state["snapshot"]
        current = _snapshot(path)
        if current != state["applied"] and current != snapshot:
            raise RuntimeError("drop-in changed outside the transaction")
        state["phase"] = "rolling_back"
        self._save(state)
        if snapshot is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            _sync_directory(path.parent)
        else:
            _write(path, base64.b64decode(snapshot["bytes"], validate=True), snapshot["mode"], check_metadata=True)
        self.reload()
        state["phase"] = "rolled_back"
        self._save(state)
        return True
