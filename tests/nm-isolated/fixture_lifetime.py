"""Shared fail-closed lifetime for one fixture authority and bus."""


class FixtureLifetime:
    """Retire all aliases and invoke the fixture owner's bounded teardown once.

    Teardown must stop both authority and bus and report failure; this object cannot independently prove a supplied teardown actually did so.
    """

    def __init__(self, teardown, timeout):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.teardown = teardown
        self.timeout = timeout
        self.retired = False
        self.teardown_error = None

    def check(self):
        if self.retired:
            raise RuntimeError("fixture lifetime is retired")

    def retire(self):
        if self.retired:
            return
        self.retired = True
        try:
            self.teardown(timeout=self.timeout)
        except BaseException as error:
            self.teardown_error = error
            raise
