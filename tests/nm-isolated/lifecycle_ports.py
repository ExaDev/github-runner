"""Fixture scenario lifecycle ports using injected child-process and bus interfaces.

The child launcher must enforce the disposable VM guard and non-root identity. No default launcher is provided; these ports do not establish isolation.
"""


class Clients:
    def __init__(self, launcher):
        self.launcher = launcher
        self.requests = {}

    def submit_held(self, *, metric, fenced, timeout):
        child = self.launcher.start(metric=metric, unfenced=not fenced, disconnect_after_held=True, timeout=timeout)
        try:
            request = child.read_request_id(timeout=timeout)
            if not request or request in self.requests:
                raise RuntimeError("invalid or duplicate held request id")
            self.requests[request] = child
            return request
        except BaseException:
            child.close()
            raise

    def close(self):
        errors = []
        for child in self.requests.values():
            try:
                child.close()
            except Exception as error:
                errors.append(error)
        self.requests.clear()
        if errors:
            raise RuntimeError("client cleanup failed") from errors[0]

    def pid(self, request):
        return self.requests[request].pid

    def disconnect(self, request, *, timeout):
        """Confirm the child has disconnected and exited successfully."""
        child = self.requests[request]
        if child.wait(timeout=timeout) != 0:
            raise RuntimeError("held client did not disconnect successfully")

    def update_separately(self, *, metric, fenced, timeout):
        child = self.launcher.start(metric=metric, unfenced=not fenced, disconnect_after_held=False, timeout=timeout)
        try:
            if child.wait(timeout=timeout) != 0:
                raise RuntimeError("separate writer failed")
        finally:
            child.close()


class Authority:
    def __init__(self, control, lifetime):
        lifetime.check()
        self.control = control
        self.lifetime = lifetime

    def retire(self):
        self.lifetime.retire()

    def check(self):
        self.lifetime.check()

    def allow_later(self, *, timeout):
        self.check()
        self.control.AllowLater(True, timeout=timeout)

    def hold_later(self, *, timeout):
        self.check()
        try:
            self.control.AllowLater(False, timeout=timeout)
        except BaseException:
            self.retire()
            raise

    def release(self, request, *, timeout):
        self.check()
        self.control.Release(request, True, timeout=timeout)


class Daemon:
    def restart(self, *, timeout):
        raise RuntimeError("daemon restart is inconclusive: no verified lifecycle")
