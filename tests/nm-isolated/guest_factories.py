"""Private bus, PolicyKit stub and daemon factories for the guarded guest owner."""

import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import networkmanager
import private_bus


@contextmanager
def bus_factory(*, timeout):
    with private_bus.private_bus(readiness_timeout=timeout, shutdown_timeout=timeout) as bus:
        yield bus


def authority_ready(process, address, timeout, *, run=subprocess.run, clock=time.monotonic, pause=time.sleep):
    deadline = clock() + timeout
    while True:
        if process.poll() is not None:
            raise RuntimeError("authority exited before readiness")
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("authority readiness deadline expired")
        result = run(["dbus-send", f"--bus={address}", "--print-reply", f"--reply-timeout={max(1, int(remaining * 1000))}", "--dest=org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority", "test.nm.IsolatedControl.Pending"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=remaining, check=False)
        if result.returncode == 0:
            if process.poll() is not None:
                raise RuntimeError("authority exited during readiness")
            return
        remaining = deadline - clock()
        if remaining > 0:
            pause(min(0.05, remaining))


@contextmanager
def authority_factory(*, bus, timeout, popen=subprocess.Popen, ready=authority_ready):
    address, bus_process = bus
    if address != private_bus.ADDRESS or bus_process.poll() is not None:
        raise RuntimeError("private bus is not running")
    script = Path(__file__).with_name("polkit_stub.py")
    process = popen(["/usr/bin/python3", str(script), "--private-bus-address", address], stdin=subprocess.DEVNULL)
    try:
        ready(process, address, timeout)
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)


@contextmanager
def profile_factory(*, bus, daemon, timeout, dbus_module=None):
    """Create and delete one unsaved, inactive Ethernet test profile."""
    address, bus_process = bus
    if address != private_bus.ADDRESS or bus_process.poll() is not None:
        raise RuntimeError("private bus is not running")
    if dbus_module is None:
        import dbus as dbus_module
        import dbus.bus
    connection = dbus_module.bus.BusConnection(address)
    try:
        settings = dbus_module.Interface(connection.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/Settings", introspect=False), "org.freedesktop.NetworkManager.Settings")
        identifier = str(uuid.uuid4())
        data = dbus_module.Dictionary({
            "connection": dbus_module.Dictionary({"id": "nm-isolated-" + identifier, "uuid": identifier, "type": "802-3-ethernet", "autoconnect": dbus_module.Boolean(False)}, signature="sv"),
            "802-3-ethernet": dbus_module.Dictionary({}, signature="sv"),
            "ipv4": dbus_module.Dictionary({"method": "disabled", "route-metric": dbus_module.Int64(600)}, signature="sv"),
            "ipv6": dbus_module.Dictionary({"method": "disabled"}, signature="sv"),
        }, signature="sa{sv}")
        path = settings.AddConnectionUnsaved(data, timeout=timeout)
        try:
            yield str(path), identifier
        finally:
            profile = dbus_module.Interface(connection.get_object("org.freedesktop.NetworkManager", path, introspect=False), "org.freedesktop.NetworkManager.Settings.Connection")
            profile.Delete(timeout=timeout)
    finally:
        connection.close()


def daemon_ready(process, address, timeout, *, connect=None, clock=time.monotonic, pause=time.sleep):
    if address != private_bus.ADDRESS or timeout <= 0:
        raise ValueError("private bus and positive timeout required")
    if connect is None:
        import dbus
        import dbus.bus
        def connect(address):
            connection = dbus.bus.BusConnection(address)
            def interfaces():
                bus = dbus.Interface(connection.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus", introspect=False), "org.freedesktop.DBus")
                settings = dbus.Interface(connection.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/Settings", introspect=False), "org.freedesktop.NetworkManager.Settings")
                return connection, bus, settings
            return interfaces()
    deadline = clock() + timeout
    connection, bus, settings = connect(address)
    try:
        def remaining():
            value = deadline - clock()
            if value <= 0:
                raise TimeoutError("daemon readiness deadline expired")
            return value
        while True:
            if process.poll() is not None:
                raise RuntimeError("daemon exited before readiness")
            try:
                owner = str(bus.GetNameOwner("org.freedesktop.NetworkManager", timeout=remaining()))
            except Exception as error:
                if getattr(error, "get_dbus_name", lambda: None)() != "org.freedesktop.DBus.Error.NameHasNoOwner":
                    raise
                pause(min(0.05, remaining()))
                continue
            if not owner.startswith(":") or int(bus.GetConnectionUnixProcessID(owner, timeout=remaining())) != process.pid:
                raise RuntimeError("NetworkManager name owned by unexpected process")
            settings.ListConnections(timeout=remaining())
            if process.poll() is not None or str(bus.GetNameOwner("org.freedesktop.NetworkManager", timeout=remaining())) != owner:
                raise RuntimeError("daemon owner changed during readiness")
            return
    finally:
        connection.close()


@contextmanager
def daemon_factory(*, bus, timeout, daemon=networkmanager.networkmanager, ready=daemon_ready):
    @contextmanager
    def existing_bus():
        yield bus
    with daemon(bus_factory=existing_bus, shutdown_timeout=timeout) as result:
        address, process, _ = result
        ready(process, address, timeout)
        yield result
