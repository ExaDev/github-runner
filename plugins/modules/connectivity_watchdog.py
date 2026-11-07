#!/usr/bin/env python3
"""Host-owned recovery worker for connectivity transactions.

The service is deliberately not wired into the role: a NetworkManager reload can outlive its caller and overlap a rollback reload. Installation alone does not make that transaction safe to use.
"""

import argparse
import json
import socket
import subprocess
import time
import uuid
from pathlib import Path

from connectivity_transaction import ConnectivityTransaction


POLL_INTERVAL_SECONDS = 1.0
RELOAD_TIMEOUT_SECONDS = 30
HANDSHAKE_TIMEOUT_SECONDS = 2
MAX_REQUEST_BYTES = 4096


class ServiceScheduler:
    """Keep application disabled until the host safety boundaries are enforced."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)

    def arm(self, transaction_id: str, generation: str, deadline: float) -> None:
        raise RuntimeError("watchdog arm disabled: independent liveness, reload isolation and exclusive writers are not established")

    def is_armed(self, transaction_id: str, generation: str, deadline: float) -> bool:
        return probe_worker(self.state_dir, transaction_id, generation, deadline)


def probe_worker(state_dir: Path, transaction_id: str, generation: str, deadline: float) -> bool:
    """Challenge a live worker to confirm the exact durable prepared generation."""
    challenge = uuid.uuid4().hex
    request = {"id": transaction_id, "generation": generation, "deadline": deadline, "challenge": challenge}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
            connection.connect(str(state_dir / "watchdog.sock"))
            connection.sendall(json.dumps(request).encode() + b"\n")
            response = connection.recv(MAX_REQUEST_BYTES)
        return json.loads(response) == {"challenge": challenge, "ready": True}
    except (OSError, ValueError):
        return False


def answer_probe(manager: ConnectivityTransaction, request: dict, now: float, boot_id: str) -> dict:
    """Check the generation under the transaction lock without waiting for a hung scan."""
    response = {"challenge": request.get("challenge"), "ready": False}
    try:
        with manager._locked(blocking=False):
            if not manager.state_path.exists():
                return response
            state = manager._read()
            response["ready"] = (state["phase"] == "prepared" and state["id"] == request.get("id") and state["generation"] == request.get("generation") and state["deadline"] == request.get("deadline") and state["boot_id"] == boot_id and now < state["deadline"])
    except BlockingIOError:
        pass
    return response


def serve_once(listener: socket.socket, manager: ConnectivityTransaction, boot_id: str, clock) -> None:
    """Answer one probe or return when the listener's poll interval expires."""
    try:
        connection, _ = listener.accept()
    except TimeoutError:
        return
    with connection:
        connection.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
        try:
            raw = connection.recv(MAX_REQUEST_BYTES)
            request = json.loads(raw)
            if not isinstance(request, dict):
                return
            response = answer_probe(manager, request, clock(), boot_id)
            connection.sendall(json.dumps(response).encode())
        except (OSError, ValueError):
            return


def reload_networkmanager() -> None:
    subprocess.run(["nmcli", "general", "reload", "conf"], check=True, timeout=RELOAD_TIMEOUT_SECONDS)


def scan(manager: ConnectivityTransaction, now: float, boot_id: str) -> str:
    """Expire a due generation or finish a rollback interrupted by a crash."""
    with manager._locked():
        if not manager.state_path.exists():
            return "empty"
        state = manager._read()
        if state["phase"] == "rolling_back":
            manager._rollback(state)
            return "rolled_back"
        if state["phase"] in ("committed", "rolled_back"):
            return state["phase"]
        if state["phase"] not in ("prepared", "pending"):
            raise ValueError("unknown transaction phase")
        deadline = state["deadline"]
        if deadline is None:
            raise ValueError("watchdog transaction has no deadline")
        if state["boot_id"] != boot_id or now >= deadline:
            manager._rollback(state)
            return "rolled_back"
        return state["phase"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    manager = ConnectivityTransaction(args.state_dir, reload_networkmanager, exclusive_writer=True)
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    clock = lambda: time.clock_gettime(time.CLOCK_BOOTTIME)
    socket_path = args.state_dir / "watchdog.sock"
    with manager._locked():
        socket_path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
    with listener:
        listener.listen()
        listener.settimeout(POLL_INTERVAL_SECONDS)
        while True:
            scan(manager, clock(), boot_id)
            serve_once(listener, manager, boot_id, clock)


if __name__ == "__main__":
    main()
