"""Injected fixture ownership and child teardown tests."""

import subprocess
import unittest

from fixture_owner import FixtureOwner


class Child:
    def __init__(self, name, events, fail=False):
        self.name, self.events, self.fail = name, events, fail
        self.running = True

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.events.append((self.name, "terminate"))
        if not self.fail:
            self.running = False

    def kill(self):
        self.events.append((self.name, "kill"))
        self.running = False

    def wait(self, *, timeout):
        self.events.append((self.name, "wait"))
        if self.running:
            raise subprocess.TimeoutExpired(self.name, timeout)
        return 0


class OwnerTests(unittest.TestCase):
    def test_aliases_share_one_owner_and_teardown(self):
        events, control = [], object()
        owner = FixtureOwner(control, Child("authority", events), Child("bus", events), timeout=2, guard=lambda: None)
        alias = owner.authority()
        with self.assertRaisesRegex(RuntimeError, "already owned"):
            FixtureOwner(control, Child("a", events), Child("b", events), timeout=2, guard=lambda: None)
        owner.retire()
        self.assertEqual(events, [("authority", "terminate"), ("authority", "wait"), ("bus", "terminate"), ("bus", "wait")])
        with self.assertRaisesRegex(RuntimeError, "retired"):
            alias.allow_later(timeout=1)
        with self.assertRaisesRegex(RuntimeError, "retired"):
            owner.authority()

    def test_teardown_failure_still_stops_bus_and_blocks_reuse(self):
        events, control = [], object()
        owner = FixtureOwner(control, Child("authority", events, fail=True), Child("bus", events), timeout=2, guard=lambda: None)
        with self.assertRaisesRegex(RuntimeError, "teardown failed"):
            owner.retire()
        self.assertIn(("authority", "kill"), events)
        self.assertIn(("bus", "terminate"), events)
        with self.assertRaisesRegex(RuntimeError, "already owned"):
            FixtureOwner(control, Child("a", events), Child("b", events), timeout=2, guard=lambda: None)

    def test_guard_failure_claims_nothing(self):
        control = object()
        def refuse():
            raise RuntimeError("guard")
        with self.assertRaisesRegex(RuntimeError, "guard"):
            FixtureOwner(control, Child("a", []), Child("b", []), timeout=2, guard=refuse)
        owner = FixtureOwner(control, Child("a", []), Child("b", []), timeout=2, guard=lambda: None)
        owner.retire()


if __name__ == "__main__":
    unittest.main()
