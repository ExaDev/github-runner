"""Offline fresh-VM composition tests with injected contexts."""

import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from vm_scenario_owner import VMScenarioOwner


class Tests(unittest.TestCase):
    def test_failure_retries_only_after_first_vm_exit(self):
        events = []
        @contextmanager
        def vm_factory(*, timeout):
            name = f"vm-{len([x for x in events if x.startswith('start')])}"
            events.append("start " + name)
            try:
                yield SimpleNamespace(identity=name)
            finally:
                events.append("stop " + name)
        @contextmanager
        def connect(vm, *, timeout):
            yield vm
        def scenario(vm):
            if vm.identity == "vm-0":
                raise ValueError("first")
            return vm.identity
        owner = VMScenarioOwner(vm_factory, connect, attempts=2, timeout=5)
        self.assertEqual(owner.run(scenario), "vm-1")
        self.assertEqual(events, ["start vm-0", "stop vm-0", "start vm-1", "stop vm-1"])

    def test_duplicate_identity_is_refused(self):
        @contextmanager
        def vm_factory(*, timeout):
            yield SimpleNamespace(identity="same")
        @contextmanager
        def connect(vm, *, timeout):
            yield vm
        owner = VMScenarioOwner(vm_factory, connect, attempts=1, timeout=5)
        owner.run(lambda vm: None)
        with self.assertRaisesRegex(RuntimeError, "all fresh"):
            owner.run(lambda vm: None)

    def test_teardown_failure_does_not_retry(self):
        calls = []
        @contextmanager
        def vm_factory(*, timeout):
            calls.append("start")
            yield SimpleNamespace(identity="one")
            raise RuntimeError("teardown")
        @contextmanager
        def connect(vm, *, timeout):
            yield vm
        owner = VMScenarioOwner(vm_factory, connect, attempts=2, timeout=5)
        with self.assertRaisesRegex(RuntimeError, "teardown failed"):
            owner.run(lambda vm: None)
        self.assertEqual(calls, ["start"])


if __name__ == "__main__":
    unittest.main()
