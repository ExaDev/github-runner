"""Source-only lifecycle for a disposable QEMU fixture VM.

The caller supplies an independently trusted base image and a private, empty working directory. A guest cannot establish whether its host is disposable.
"""

import json
import os
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

# An exec publishes the child's argument area within microseconds of switching its executable; the budget is a hundred times that, so a healthy launch never fails and a stuck child still does.
EMPTY_CMDLINE_INTERVAL = 0.001
EMPTY_CMDLINE_READS = 100


def verify_process(pid, executable, command, *, readlink=os.readlink, read_bytes=None, sleep=time.sleep, empty_reads=EMPTY_CMDLINE_READS, empty_interval=EMPTY_CMDLINE_INTERVAL):
    """Check Linux procfs executable and complete argv against the launched child.

    During exec the kernel switches the executable before it publishes the new argument area, so a healthy child can briefly read as empty. Only an empty read is retried, a bounded number of times; any non-empty difference fails at once.
    """
    if read_bytes is None:
        read_bytes = lambda name: Path(name).read_bytes()
    if readlink(f"/proc/{pid}/exe") != os.path.realpath(executable):
        raise RuntimeError("QEMU executable identity mismatch")
    raw = read_bytes(f"/proc/{pid}/cmdline")
    for _ in range(empty_reads):
        if raw:
            break
        sleep(empty_interval)
        raw = read_bytes(f"/proc/{pid}/cmdline")
    actual = raw.split(b"\0")
    expected = [os.fsencode(arg) for arg in command] + [b""]
    if actual != expected:
        raise RuntimeError("QEMU command identity mismatch")


def validate_base(base, qemu_img, *, run=subprocess.run):
    """Accept only an owned, read-only standalone local qcow2 image."""
    base = Path(base)
    if not base.is_absolute() or base != base.resolve() or ":" in str(base):
        raise ValueError("base must be a canonical local path")
    info = base.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o222:
        raise ValueError("base image must be owned and have no write permissions")
    result = run([qemu_img, "info", "--output=json", "--backing-chain", "--", str(base)], check=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        chain = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError("invalid qemu-img JSON") from None
    if not isinstance(chain, list) or len(chain) != 1 or not isinstance(chain[0], dict):
        raise ValueError("base must have no backing chain")
    image = chain[0]
    if image.get("format") != "qcow2" or image.get("filename") != str(base):
        raise ValueError("base image format or filename mismatch")
    if any(key in image for key in ("backing-filename", "full-backing-filename", "backing-filename-format", "data-file")):
        raise ValueError("external backing or data file is forbidden")
    specific = image.get("format-specific")
    if not isinstance(specific, dict) or specific.get("type") != "qcow2" or not isinstance(specific.get("data"), dict):
        raise ValueError("missing qcow2 format details")
    if any(key in specific["data"] for key in ("data-file", "data-file-raw")):
        raise ValueError("external data file is forbidden")


@contextmanager
def disposable_vm(base_image, directory, *, qemu, qemu_img, run=subprocess.run, popen=subprocess.Popen, verify=verify_process, shutdown_timeout=5):
    """Create an overlay and launch a hardware-accelerated VM with no host shares or network devices.

    KVM is required and QEMU refuses to start without it: software emulation boots a guest too slowly for the fixture's bounded waits, so there is no fallback to it.

    The caller owns and retains the workspace and overlay. Paths must not contain commas because QEMU's drive option treats them as delimiters.
    """
    base, work = Path(base_image), Path(directory)
    if shutdown_timeout <= 0:
        raise ValueError("shutdown timeout must be positive")
    if not base.is_absolute() or not work.is_absolute() or not base.is_file() or base.is_symlink() or not work.is_dir() or work.is_symlink():
        raise ValueError("absolute base image and private workspace required")
    if any("," in str(item) for item in (base, work)) or any(work.iterdir()):
        raise ValueError("workspace must be empty and paths must not contain commas")
    if work != work.resolve() or work.stat().st_uid != os.geteuid() or work.stat().st_mode & 0o077:
        raise ValueError("workspace must be canonical, owned and private")
    if not os.path.isabs(qemu) or not os.path.isabs(qemu_img):
        raise ValueError("absolute executable paths required")
    validate_base(base, qemu_img, run=run)
    name = "nm-isolated-" + uuid.uuid4().hex
    overlay = work / (name + ".qcow2")
    run([qemu_img, "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(overlay)], check=True, stdin=subprocess.DEVNULL)
    serial_socket = work / "guest.sock"
    command = [qemu, "-name", name, "-accel", "kvm", "-cpu", "host", "-m", "2048", "-smp", "2", "-nodefaults", "-no-user-config", "-display", "none", "-monitor", "none", "-serial", "none", "-nic", "none", "-drive", f"file={overlay},if=virtio,format=qcow2", "-chardev", f"socket,id=fixture,path={serial_socket},server=on,wait=off", "-device", "virtio-serial-pci", "-device", "virtserialport,chardev=fixture,name=test.nm.fixture"]
    process = popen(command, stdin=subprocess.DEVNULL)
    try:
        verify(process.pid, qemu, command)
        yield name, overlay, process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=shutdown_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
