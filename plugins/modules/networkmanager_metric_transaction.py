#!/usr/bin/python
"""Guarded checkpoint transaction protocol for future NetworkManager integration.

The adapter must call NetworkManager's device-scoped checkpoint API with flags zero and a non-zero timeout. This protocol is not connected to a host adapter or Ansible role.
"""


class NetworkManagerAdapter:
    """Use a python-dbus system bus; callers must supply validated device object paths.

    One adapter instance is bound to one NetworkManager process: every call is addressed to the unique bus name the daemon owned when the adapter was created, never to the well-known name. A restarted daemon has a new unique name, so a read made before the restart cannot be followed by a write after it: the call fails with an unknown-name error. This matters because a restart resets each profile's version-id, so a stale version can match a changed profile (verified on NetworkManager 1.54.3). Profiles are always found by UUID, because a restart also renumbers their object paths.
    """

    def __init__(self, bus, dbus_module):
        self.bus = bus
        self.dbus = dbus_module
        self.service = "org.freedesktop.NetworkManager"
        self.owner = bus.get_name_owner(self.service)
        if not isinstance(self.owner, str) or not self.owner.startswith(":"):
            raise ValueError("NetworkManager must own a unique bus name")

    def _interface(self, path, name):
        return self.dbus.Interface(self.bus.get_object(self.owner, path), name)

    def _manager(self):
        return self._interface("/org/freedesktop/NetworkManager", self.service)

    def create_checkpoint(self, devices, timeout, flags):
        if not devices or timeout <= 0 or flags != 0:
            raise ValueError("a non-zero timeout, devices and flags zero are required")
        return self._manager().CheckpointCreate(self.dbus.Array(devices, signature="o"), self.dbus.UInt32(timeout), self.dbus.UInt32(flags))

    def inspect_device(self, device, uuid):
        interface = self._interface(device, self.service + ".Device")
        applied, version = interface.GetAppliedConnection(self.dbus.UInt32(0))
        if applied.get("connection", {}).get("uuid") != uuid or not version:
            raise ValueError("checkpoint device does not have the expected applied profile and version")
        return applied, version

    def set_metric(self, uuid, metric):
        settings = self._interface("/org/freedesktop/NetworkManager/Settings", self.service + ".Settings")
        path = settings.GetConnectionByUuid(uuid)
        connection = self._interface(path, self.service + ".Settings.Connection")
        properties = self._interface(path, "org.freedesktop.DBus.Properties")
        version = properties.Get(self.service + ".Settings.Connection", "VersionId")
        if not version:
            raise ValueError("profile version must be non-zero")
        profile = connection.GetSettings()
        if "ipv4" not in profile:
            raise ValueError("profile has no IPv4 settings")
        profile["ipv4"]["route-metric"] = self.dbus.Int64(metric, variant_level=1)
        args = self.dbus.Dictionary({"version-id": self.dbus.UInt64(version, variant_level=1)}, signature="sv")
        connection.Update2(profile, self.dbus.UInt32(1), args)

    def reapply(self, device, uuid, metric, applied, version):
        if applied.get("connection", {}).get("uuid") != uuid or not version or "ipv4" not in applied:
            raise ValueError("applied profile identity, IPv4 settings and version are required")
        applied["ipv4"]["route-metric"] = self.dbus.Int64(metric, variant_level=1)
        interface = self._interface(device, self.service + ".Device")
        interface.Reapply(applied, self.dbus.UInt64(version), self.dbus.UInt32(1))

    def rollback_checkpoint(self, checkpoint, devices):
        results = self._manager().CheckpointRollback(checkpoint)
        if set(results) != set(devices) or any(int(result) != 0 for result in results.values()):
            raise RuntimeError("checkpoint rollback incomplete or failed: " + repr(dict(results)))
        return results

    def destroy_checkpoint(self, checkpoint):
        self._manager().CheckpointDestroy(checkpoint)


def execute(adapter, profiles, timeout, verify):
    """Apply candidate metrics behind a checkpoint, or fail before touching the adapter."""
    raise RuntimeError("metric transaction disabled until isolated NetworkManager crash and rollback validation")


def _execute_for_test(adapter, profiles, timeout, verify):
    """Exercise the protocol with an offline adapter; never use against a host."""
    if len(profiles) != 2 or len({item["device"] for item in profiles}) != 2 or timeout <= 0:
        raise ValueError("two distinct devices and a positive timeout are required")
    checkpoint = adapter.create_checkpoint([item["device"] for item in profiles], timeout, 0)
    try:
        snapshots = [adapter.inspect_device(profile["device"], profile["uuid"]) for profile in profiles]
        for profile, (applied, version) in zip(profiles, snapshots):
            adapter.set_metric(profile["uuid"], profile["metric"])
            adapter.reapply(profile["device"], profile["uuid"], profile["metric"], applied, version)
        if not verify():
            raise RuntimeError("independent route verification failed")
        adapter.destroy_checkpoint(checkpoint)
    except Exception as original:
        try:
            adapter.rollback_checkpoint(checkpoint, [profile["device"] for profile in profiles])
        except Exception as rollback_error:
            raise ExceptionGroup("metric transaction and rollback both failed", [original, rollback_error]) from None
        raise
