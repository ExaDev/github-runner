"""Validate a prebuilt guest's provisioning manifest and installed prerequisites.

Validation is read-only. Provisioning remains disabled until host-side disposable-VM provenance can be established; it does not start services.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

DESTINATION = "/opt/nm-isolated-test"
PACKAGES = ("dbus", "network-manager", "python3", "python3-dbus", "python3-gi", "systemd")
FILES = ("adapter_factory.py", "audit_log.py", "audit_observer.py", "bus.conf", "client_launcher.py", "daemon_capture.py", "fixture_lifetime.py", "fixture_owner.py", "guest_agent.py", "guest_factories.py", "guest_owner.py", "guest_runner.py", "guest_transport.py", "lifecycle_ports.py", "networkmanager.py", "polkit_stub.py", "preflight.sh", "private_bus.py", "scenario_bus.py", "scenarios.py", "update2_client.py")


def validate_manifest(value):
    """Reject any manifest differing from the intended fixed provisioning set."""
    expected = {"schema": 1, "platform": "debian", "networkmanager_version": "1.54.3", "packages": list(PACKAGES), "files": list(FILES), "destination": DESTINATION, "virtio_port": "test.nm.fixture"}
    if value != expected:
        raise ValueError("unexpected guest provisioning manifest")


def validate_guest(manifest, *, run=subprocess.run, is_file=Path.is_file):
    """Check installed package status, daemon version and fixture files."""
    validate_manifest(manifest)
    for package in PACKAGES:
        result = run(["dpkg-query", "-W", "-f=${Status}", package], check=True, capture_output=True, text=True)
        if result.stdout != "install ok installed":
            raise RuntimeError(f"package not installed: {package}")
    result = run(["NetworkManager", "--version"], check=True, capture_output=True, text=True)
    if result.stdout.strip() != manifest["networkmanager_version"]:
        raise RuntimeError("NetworkManager version mismatch")
    for name in FILES:
        if not is_file(Path(DESTINATION) / name):
            raise RuntimeError(f"fixture file missing: {name}")


def provision(source, manifest, expected_hashes, *, guard, run=subprocess.run, install=None):
    """Validate trusted inputs then install files via an injected installation port.

    The production guard is deliberately unavailable until VM provenance can be established by the host. The caller-supplied hashes must come from an independently trusted source.
    """
    guard()
    validate_manifest(manifest)
    if set(expected_hashes) != set(FILES):
        raise ValueError("a trusted digest is required for every fixture file")
    contents = {}
    for name in FILES:
        item = Path(source) / name
        if item.is_symlink() or not item.is_file():
            raise ValueError(f"invalid fixture source: {name}")
        data = item.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_hashes[name]:
            raise ValueError(f"fixture digest mismatch: {name}")
        contents[name] = data
    for package in PACKAGES:
        result = run(["dpkg-query", "-W", "-f=${Status}", package], check=True, capture_output=True, text=True)
        if result.stdout != "install ok installed":
            raise RuntimeError(f"package not installed: {package}")
    result = run(["NetworkManager", "--version"], check=True, capture_output=True, text=True)
    if result.stdout.strip() != manifest["networkmanager_version"]:
        raise RuntimeError("NetworkManager version mismatch")
    if install is None:
        install = install_files
    install(contents)


def install_files(contents):
    """Create a new fixture directory without overwriting existing files."""
    destination = Path(DESTINATION)
    destination.mkdir(mode=0o755)
    for name, data in contents.items():
        fd = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)


def refuse_unverified_guest():
    """Refuse writes until host-side disposable-VM provenance is enforced."""
    raise RuntimeError("refusing: disposable VM provenance is not verified")


def main():
    manifest_path = Path(__file__).with_name("guest_manifest.json")
    validate_guest(json.loads(manifest_path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
