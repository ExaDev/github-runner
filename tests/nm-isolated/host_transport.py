"""Connect to a fixture VM's private virtio-serial socket and check readiness."""

import errno
import socket
import time
from pathlib import Path

import guest_transport


def connect_ready(workspace, process, timeout, *, socket_factory=socket.socket, clock=time.monotonic, pause=time.sleep):
    """Connect and perform one readiness exchange within one deadline.

    The returned socket belongs to the caller. A ready reply confirms only that the guest agent responded, not that the VM or guest is safely isolated.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    path = str(Path(workspace) / "guest.sock")
    deadline = clock() + timeout
    while True:
        if process.poll() is not None:
            raise RuntimeError("QEMU exited before guest readiness")
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("guest readiness deadline expired")
        sock = socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(remaining)
            sock.connect(path)
        except OSError as error:
            sock.close()
            if error.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                raise
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError("guest readiness deadline expired") from error
            pause(min(0.05, remaining))
            continue
        try:
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError("guest readiness deadline expired")
            stream = guest_transport.DeadlineStream(sock, remaining, clock=clock)
            result = guest_transport.request(stream, "readiness", "ready", {})
            if result != {"ready": True}:
                raise RuntimeError("guest did not report readiness")
            if process.poll() is not None:
                raise RuntimeError("QEMU exited during guest readiness")
            return sock
        except BaseException:
            sock.close()
            raise
