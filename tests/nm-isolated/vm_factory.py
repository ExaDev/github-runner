"""Compose QEMU and readiness transport for source-only fixture scenarios."""

import time
from contextlib import contextmanager
from dataclasses import dataclass

import guest_transport
import host_transport
import qemu_vm
from vm_scenario_owner import VMScenarioOwner


@dataclass(frozen=True)
class VM:
    identity: str
    workspace: object
    process: object


def request_scenario(sock, name, metric, timeout):
    """Request one scenario and return its guest-supplied result."""
    stream = guest_transport.DeadlineStream(sock, timeout)
    return guest_transport.request(stream, "scenario", "scenario", {"scenario": name, "metric": metric, "timeout": timeout})


def make_owner(base_image, workspace_factory, *, qemu, qemu_img, attempts, timeout, vm_start=qemu_vm.disposable_vm, connect_ready=host_transport.connect_ready, clock=time.monotonic):
    """Create a fresh-workspace QEMU owner returning a ready transport socket.

    The workspace factory must return an empty private directory for each call. The returned socket is not a scenario adapter. Guest provisioning and the production isolation guard remain independent prerequisites.
    """
    @contextmanager
    def start(*, timeout):
        workspace = workspace_factory()
        with vm_start(base_image, workspace, qemu=qemu, qemu_img=qemu_img, shutdown_timeout=timeout) as (name, overlay, process):
            yield VM(name, workspace, process)

    @contextmanager
    def connect(vm, *, timeout):
        sock = connect_ready(vm.workspace, vm.process, timeout)
        try:
            yield sock
        finally:
            sock.close()

    return VMScenarioOwner(start, connect, attempts=attempts, timeout=timeout, clock=clock)
