#!/usr/bin/python
"""Serialise NetworkManager connectivity configuration and reload on one host."""

import fcntl
import os
import stat
import tempfile

from ansible.module_utils.basic import AnsibleModule

CONTENT = b"# Managed by the exadev.github_runner collection's github_runner_linux_host role. Do not edit by hand.\n[connectivity]\nenabled=false\n"
PROPERTY = ["busctl", "get-property", "org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager", "org.freedesktop.NetworkManager", "ConnectivityCheckEnabled"]


def atomic_write(path, content):
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".connectivity-")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run(module):
    module.fail_json(msg="NetworkManager connectivity changes are not ready: reload isolation and the exclusive writer boundary are not established")
    path = module.params["dropin"]
    disabled = module.params["disabled"]
    directory = os.path.dirname(path)
    marker = path + ".reload-pending"
    # The existing host-wide directory is a stable inode shared by all invocations.
    lock = os.open("/var/run", os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        exists = os.path.exists(path)
        if disabled and exists:
            with open(path, "rb") as existing:
                config_changed = existing.read() != CONTENT or stat.S_IMODE(os.stat(path).st_mode) != 0o644
        else:
            config_changed = disabled or exists
        pending = os.path.exists(marker)
        runtime_changed = False
        if disabled:
            rc, stdout, stderr = module.run_command(PROPERTY)
            if rc:
                module.fail_json(msg="Reading NetworkManager connectivity failed", rc=rc, stderr=stderr)
            if stdout.strip() not in ("b true", "b false"):
                module.fail_json(msg="Unexpected NetworkManager connectivity property", stdout=stdout)
            runtime_changed = stdout.strip() != "b false"
        changed = config_changed or pending or runtime_changed
        if module.check_mode or not changed:
            module.exit_json(changed=changed)
        os.makedirs(directory, mode=0o755, exist_ok=True)
        atomic_write(marker, b"pending\n")
        if config_changed:
            if disabled:
                atomic_write(path, CONTENT)
            else:
                os.unlink(path)
                sync_directory(directory)
        rc, stdout, stderr = module.run_command(["nmcli", "general", "reload", "conf"])
        if rc:
            module.fail_json(msg="NetworkManager configuration reload failed", rc=rc, stdout=stdout, stderr=stderr, changed=True)
        if disabled:
            rc, stdout, stderr = module.run_command(PROPERTY)
            if rc or stdout.strip() != "b false":
                module.fail_json(msg="Verify NetworkManager connectivity checking is disabled failed", rc=rc, stdout=stdout, stderr=stderr, changed=True)
        os.unlink(marker)
        sync_directory(directory)
        module.exit_json(changed=True)
    finally:
        os.close(lock)


def main():
    module = AnsibleModule(argument_spec={"dropin": {"type": "path", "required": True}, "disabled": {"type": "bool", "required": True}}, supports_check_mode=True)
    try:
        run(module)
    except OSError as exc:
        module.fail_json(msg=str(exc))


if __name__ == "__main__":
    main()
