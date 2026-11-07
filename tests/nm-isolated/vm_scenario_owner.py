"""Own one disposable VM per scenario using an injected QEMU context."""

import time


class VMScenarioOwner:
    """Never reuse a VM after a scenario, including after an exception.

    `vm_factory` owns VM teardown through its context manager. The caller must supply a fresh private workspace for each factory invocation.
    """

    def __init__(self, vm_factory, connect, *, attempts, timeout, clock=time.monotonic):
        if attempts <= 0 or timeout <= 0:
            raise ValueError("attempts and timeout must be positive")
        self.vm_factory = vm_factory
        self.connect = connect
        self.attempts = attempts
        self.timeout = timeout
        self.clock = clock
        self.used = set()

    def run(self, scenario):
        deadline = self.clock() + self.timeout
        last_error = None
        for _ in range(self.attempts):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError("scenario deadline expired") from last_error
            entered = False
            completed = False
            try:
                with self.vm_factory(timeout=remaining) as vm:
                    entered = True
                    if vm.identity in self.used:
                        raise RuntimeError("VM identity reused")
                    self.used.add(vm.identity)
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise TimeoutError("scenario deadline expired")
                    with self.connect(vm, timeout=remaining) as adapter:
                        result = scenario(adapter)
                    completed = True
                return result
            except Exception as error:
                if completed or (entered and error.__context__ is not None):
                    raise RuntimeError("VM or connection teardown failed; refusing retry") from error
                last_error = error
        raise RuntimeError("all fresh VM attempts failed") from last_error
