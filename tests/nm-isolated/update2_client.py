#!/usr/bin/python3
"""Send one version-fenced Update2 on an explicitly supplied test bus."""

import argparse
import os
import subprocess
import time
from pathlib import Path

SERVICE = "org.freedesktop.NetworkManager"
CONNECTION = "org.freedesktop.NetworkManager.Settings.Connection"
PROPERTIES = "org.freedesktop.DBus.Properties"
IN_MEMORY = 0x2


def update_profile(bus, dbus, profile_path, metric=None, reply_handler=None, error_handler=None, *, unfenced=False, timeout=10, clock=time.monotonic):
    """Submit an in-memory Update2, fenced unless explicitly disabled."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    deadline = clock() + timeout
    def remaining():
        value = deadline - clock()
        if value <= 0:
            raise TimeoutError("Update2 submission deadline expired")
        return value
    obj = bus.get_object(SERVICE, profile_path, introspect=False)
    connection = dbus.Interface(obj, CONNECTION)
    args = {}
    if not unfenced:
        properties = dbus.Interface(obj, PROPERTIES)
        version = int(properties.Get(CONNECTION, "VersionId", timeout=remaining()))
        if version <= 0:
            raise ValueError("a non-zero profile version is required")
        args["version-id"] = dbus.UInt64(version)
    settings = connection.GetSettings(timeout=remaining())
    settings = {name: dict(values) for name, values in settings.items()}
    if metric is not None:
        settings["ipv4"]["route-metric"] = dbus.Int64(metric)
    kwargs = {"timeout": remaining()}
    if reply_handler is not None and error_handler is not None:
        kwargs.update(reply_handler=reply_handler, error_handler=error_handler)
    return connection.Update2(
        dbus.Dictionary({name: dbus.Dictionary(values, signature="sv") for name, values in settings.items()}, signature="sa{sv}"),
        dbus.UInt32(IN_MEMORY),
        dbus.Dictionary(args, signature="sv"),
        **kwargs,
    )


def submit_and_disconnect(bus, control, dbus, profile_path, metric, timeout, pump, clock=time.monotonic, *, unfenced=False):
    """Disconnect only after the independent authority reports the new held request."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    deadline = clock() + timeout
    if control.Pending(timeout=timeout):
        raise RuntimeError("authority must have no pending requests before submission")
    outcome = []
    update_profile(bus, dbus, profile_path, metric, lambda result: outcome.append(("reply", result)), lambda error: outcome.append(("error", error)), unfenced=unfenced, timeout=deadline - clock(), clock=clock)
    while clock() < deadline:
        pump()
        if outcome:
            raise RuntimeError(f"Update2 completed before authorisation was held: {outcome[0][0]}")
        remaining = deadline - clock()
        if remaining <= 0:
            break
        added = {str(value) for value in control.Pending(timeout=remaining)}
        if len(added) > 1:
            raise RuntimeError("multiple new authorisation requests prevent identifying the update")
        if added:
            request_id = added.pop()
            bus.close()
            return request_id
    raise TimeoutError("Update2 was not observed at the authority before the deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-bus-address", required=True)
    parser.add_argument("--profile-path", required=True)
    parser.add_argument("--metric", type=int, help="IPv4 route metric; omit for an identical update")
    parser.add_argument("--disconnect-after-held", action="store_true", help="disconnect after the authority observes the request")
    parser.add_argument("--unfenced", action="store_true", help="omit version-id for the unfenced control")
    parser.add_argument("--timeout", type=float, default=10, help="maximum seconds to wait for held authorisation")
    args = parser.parse_args()
    sentinel = Path("/run/nm-isolated-test/disposable-vm")
    if os.geteuid() == 0 or sentinel.is_symlink() or not sentinel.is_file():
        raise SystemExit("refusing: non-root client and disposable VM sentinel required")
    if subprocess.run(["systemd-detect-virt", "--vm", "--quiet"], check=False).returncode:
        raise SystemExit("refusing: disposable VM required")
    prefix = "unix:path=/run/nm-isolated-test/"
    address = args.private_bus_address
    if not address.startswith(prefix) or any(part in address for part in (",", ";", "..")):
        raise SystemExit("refusing: dedicated test bus path required")
    import dbus
    import dbus.bus

    if args.disconnect_after_held:
        if args.timeout <= 0:
            parser.error("--timeout must be positive")
        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib

        DBusGMainLoop(set_as_default=True)
        bus = dbus.bus.BusConnection(address)
        control_bus = dbus.bus.BusConnection(address)
        control = dbus.Interface(control_bus.get_object("org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority"), "test.nm.IsolatedControl")
        context = GLib.MainContext.default()

        def pump():
            while context.pending():
                context.iteration(False)
            time.sleep(0.01)

        request_id = submit_and_disconnect(bus, control, dbus, args.profile_path, args.metric, args.timeout, pump, unfenced=args.unfenced)
        print(request_id, flush=True)
        control_bus.close()
    else:
        bus = dbus.bus.BusConnection(address)
        update_profile(bus, dbus, args.profile_path, args.metric, unfenced=args.unfenced, timeout=args.timeout)


if __name__ == "__main__":
    main()
