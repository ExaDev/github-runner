#!/usr/bin/python3
"""Private-bus-only PolicyKit fixture. Never point it at a system bus."""

import argparse
import os
import subprocess
from pathlib import Path


class HeldAuthorisation:
    """Hold non-root authorisation callbacks until explicitly released."""

    def __init__(self):
        self.pending = {}
        self.allow_later = False

    def hold(self, subject, cancellation_id, success, failure):
        kind, details = subject
        if kind != "unix-process" or "uid" not in details or int(details["uid"]) == 0:
            failure("a non-root unix-process subject is required")
            return
        if not cancellation_id or cancellation_id in self.pending:
            failure("invalid or duplicate cancellation id")
            return
        if self.allow_later:
            success((True, False, {}))
        else:
            self.pending[cancellation_id] = (success, failure)

    def release(self, cancellation_id, allowed):
        success, _ = self.pending.pop(cancellation_id)
        success((bool(allowed), False, {}))

    def cancel(self, cancellation_id):
        _, failure = self.pending.pop(cancellation_id)
        failure("cancelled")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-bus-address", required=True)
    args = parser.parse_args()
    sentinel = Path("/run/nm-isolated-test/disposable-vm")
    if os.geteuid() != 0 or sentinel.is_symlink() or not sentinel.is_file():
        raise SystemExit("refusing: root and disposable VM sentinel required")
    if subprocess.run(["systemd-detect-virt", "--vm", "--quiet"], check=False).returncode:
        raise SystemExit("refusing: disposable VM required")
    expected_prefix = "unix:path=/run/nm-isolated-test/"
    if not args.private_bus_address.startswith(expected_prefix) or "," in args.private_bus_address or ";" in args.private_bus_address or ".." in args.private_bus_address:
        raise SystemExit("refusing: dedicated test bus path required")
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    bus = dbus.bus.BusConnection(args.private_bus_address)
    name = dbus.service.BusName("org.freedesktop.PolicyKit1", bus, do_not_queue=True)
    held = HeldAuthorisation()

    class Authority(dbus.service.Object):
        @dbus.service.method("org.freedesktop.PolicyKit1.Authority", in_signature="(sa{sv})sa{ss}us", out_signature="(bba{ss})", async_callbacks=("reply", "error"))
        def CheckAuthorization(self, subject, action, details, flags, cancellation_id, reply, error):
            held.hold(subject, str(cancellation_id), reply, lambda message: error(dbus.exceptions.DBusException(message)))

        @dbus.service.method("org.freedesktop.PolicyKit1.Authority", in_signature="s", out_signature="")
        def CancelCheckAuthorization(self, cancellation_id):
            held.cancel(str(cancellation_id))

        @dbus.service.method("test.nm.IsolatedControl", in_signature="b", out_signature="")
        def AllowLater(self, allowed):
            held.allow_later = bool(allowed)

        @dbus.service.method("test.nm.IsolatedControl", in_signature="sb", out_signature="")
        def Release(self, cancellation_id, allowed):
            held.release(str(cancellation_id), allowed)

        @dbus.service.method("test.nm.IsolatedControl", in_signature="", out_signature="as")
        def Pending(self):
            return list(held.pending)

    authority = Authority(name, "/org/freedesktop/PolicyKit1/Authority")
    GLib.MainLoop().run()
    return authority


if __name__ == "__main__":
    main()
