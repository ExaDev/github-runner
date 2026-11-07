"""Read fixture profile state and daemon identity over an injected private bus.

The lifecycle adapter composes injected client, authority, observer and daemon ports. Real implementations of those ports are not supplied.
"""

import time
from dataclasses import dataclass

import update2_client
from audit_observer import AuditObserver

BUS_SERVICE = "org.freedesktop.DBus"
BUS_PATH = "/org/freedesktop/DBus"
BUS_INTERFACE = "org.freedesktop.DBus"


@dataclass(frozen=True)
class Profile:
    metric: int
    version: int


class AuditScenarioObserver:
    """Bind a pre-submission capture cursor and unique client identity to one request."""

    def __init__(self, capture, daemon_instance, pid, profile_uuid, *, clock=time.monotonic, pause=time.sleep):
        if not daemon_instance or (pid is not None and pid <= 0) or not profile_uuid:
            raise ValueError("daemon instance, client PID and profile UUID required")
        self.capture = capture
        self.daemon_instance = daemon_instance
        self.pid = pid
        self.profile_uuid = profile_uuid
        self.clock = clock
        self.pause = pause
        self.pending = {}

    def mark(self):
        return self.capture.mark()

    def register(self, request, cursor, pid):
        if request in self.pending or pid <= 0 or (self.pid is not None and pid != self.pid):
            raise ValueError("duplicate request or unexpected client PID")
        self.pending[request] = AuditObserver(self.capture, self.daemon_instance, pid, self.profile_uuid, cursor, clock=self.clock, pause=self.pause)

    def wait(self, request, *, timeout):
        return self.pending[request].wait(request, timeout=timeout)


class ScenarioAdapter:
    """Compose scenario ports with a single deadline for the full scenario run.

    The observer independently records the daemon's outcome after client disconnect. A disconnected client's D-Bus reply cannot establish success or rejection.
    """

    def __init__(self, reader, clients, authority, observer, daemon, timeout, clock=time.monotonic):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.reader, self.clients, self.authority, self.observer, self.daemon = reader, clients, authority, observer, daemon
        self.clock = clock
        self.deadline = clock() + timeout
        self.cleanup_timeout = timeout
        self.retired = False

    def remaining(self):
        if self.retired:
            raise RuntimeError("scenario authority is retired")
        value = self.deadline - self.clock()
        if value <= 0:
            raise TimeoutError("scenario deadline expired")
        return value

    def profile(self):
        return self.reader.profile(timeout=self.remaining())

    def owner(self):
        return self.reader.owner(timeout=self.remaining())

    def submit_held(self, *, metric, fenced):
        cursor = self.observer.mark()
        try:
            request = self.clients.submit_held(metric=metric, fenced=fenced, timeout=self.remaining())
            self.observer.register(request, cursor, self.clients.pid(request))
            return request
        except BaseException:
            self.retire()
            raise

    def disconnect(self, request):
        self.clients.disconnect(request, timeout=self.remaining())

    def allow_later(self):
        self.authority.allow_later(timeout=self.remaining())

    def hold_later(self):
        try:
            self.authority.hold_later(timeout=self.cleanup_timeout)
        except BaseException:
            self.retire()
            raise

    def retire(self):
        self.retired = True
        self.authority.retire()

    def update_separately(self, *, metric, fenced):
        self.clients.update_separately(metric=metric, fenced=fenced, timeout=self.remaining())

    def release(self, request):
        self.authority.release(request, timeout=self.remaining())

    def await_completion(self, request):
        outcome = self.observer.wait(request, timeout=self.remaining())
        if outcome != "applied":
            raise AssertionError(f"expected applied outcome, got {outcome}")

    def await_rejection(self, request):
        outcome = self.observer.wait(request, timeout=self.remaining())
        if outcome != "version-mismatch":
            raise AssertionError(f"expected version mismatch, got {outcome}")

    def restart(self):
        self.daemon.restart(timeout=self.remaining())


class ScenarioBus:
    """Bounded D-Bus profile and owner reads on a caller-supplied connection."""

    def __init__(self, bus, dbus, profile_path, timeout):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.bus = bus
        self.dbus = dbus
        self.profile_path = profile_path
        self.timeout = timeout

    def profile(self, *, timeout=None):
        timeout = self.timeout if timeout is None else min(timeout, self.timeout)
        obj = self.bus.get_object(update2_client.SERVICE, self.profile_path, introspect=False)
        properties = self.dbus.Interface(obj, update2_client.PROPERTIES)
        connection = self.dbus.Interface(obj, update2_client.CONNECTION)
        deadline = time.monotonic() + timeout
        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("profile read deadline expired")
            return value
        before = int(properties.Get(update2_client.CONNECTION, "VersionId", timeout=remaining()))
        settings = connection.GetSettings(timeout=remaining())
        after = int(properties.Get(update2_client.CONNECTION, "VersionId", timeout=remaining()))
        if before <= 0 or before != after:
            raise RuntimeError("profile changed during state read")
        return Profile(int(settings["ipv4"]["route-metric"]), after)

    def owner(self, *, timeout=None):
        timeout = self.timeout if timeout is None else min(timeout, self.timeout)
        obj = self.bus.get_object(BUS_SERVICE, BUS_PATH, introspect=False)
        bus = self.dbus.Interface(obj, BUS_INTERFACE)
        owner = str(bus.GetNameOwner(update2_client.SERVICE, timeout=timeout))
        if not owner.startswith(":"):
            raise RuntimeError("daemon owner is not a unique bus name")
        return owner
