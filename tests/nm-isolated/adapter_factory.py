"""Compose scenario adapter ports for an isolated guest fixture."""

from contextlib import contextmanager

import client_launcher
import daemon_capture
from fixture_lifetime import FixtureLifetime
import lifecycle_ports
import scenario_bus


@contextmanager
def adapter_factory(*, bus, authority, daemon, profile, timeout, dbus_module=None, launcher_factory=client_launcher.Launcher):
    """Bind private bus, profile and child lifetimes; caller must enforce isolation."""
    if dbus_module is None:
        import dbus as dbus_module
        import dbus.bus
    address, bus_process = bus
    daemon_address, daemon_process, capture = daemon
    if address != daemon_address:
        raise RuntimeError("daemon and fixture buses differ")
    path, profile_uuid = profile
    connection = dbus_module.bus.BusConnection(address)
    try:
        owner_name = str(dbus_module.Interface(connection.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus", introspect=False), "org.freedesktop.DBus").GetNameOwner("org.freedesktop.NetworkManager", timeout=timeout))
        control = dbus_module.Interface(connection.get_object("org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority", introspect=False), "test.nm.IsolatedControl")
        lifetime = FixtureLifetime(lambda *, timeout: None, timeout)
        reader = scenario_bus.ScenarioBus(connection, dbus_module, path, timeout)
        clients = lifecycle_ports.Clients(launcher_factory(path))
        observer = scenario_bus.AuditScenarioObserver(daemon_capture.CaptureReader(capture, owner_name), owner_name, None, profile_uuid)
        adapter = scenario_bus.ScenarioAdapter(reader, clients, lifecycle_ports.Authority(control, lifetime), observer, lifecycle_ports.Daemon(), timeout)
        try:
            yield adapter
        finally:
            clients.close()
    finally:
        connection.close()
