"""Outcome inference tests; no daemon or bus is started."""

import unittest

from outcome_observer import OutcomeObserver
from scenario_bus import Profile


class Reader:
    def __init__(self, profile):
        self.value = profile
        self.calls = []

    def profile(self, *, timeout):
        self.calls.append(timeout)
        return self.value


class Barrier:
    def __init__(self, finished):
        self.value = finished
        self.calls = []

    def finished(self, request, *, timeout):
        self.calls.append((request, timeout))
        return self.value


class ObserverTests(unittest.TestCase):
    def observe(self, after, finished=True):
        reader = Reader(after)
        barrier = Barrier(finished)
        observer = OutcomeObserver(reader, barrier, clock=lambda: 0)
        observer.register("request", Profile(600, 3), 20101)
        return observer, reader, barrier

    def test_applied_requires_completion_and_changed_metric_and_version(self):
        observer, reader, barrier = self.observe(Profile(20101, 4))
        self.assertEqual(observer.wait("request", timeout=2), "applied")
        self.assertEqual(barrier.calls, [("request", 2)])
        self.assertEqual(reader.calls, [2])

    def test_unchanged_is_not_applied_not_a_specific_rejection(self):
        observer, _, _ = self.observe(Profile(600, 3))
        self.assertEqual(observer.wait("request", timeout=2), "not-applied")

    def test_missing_barrier_or_ambiguous_state_is_inconclusive(self):
        observer, reader, _ = self.observe(Profile(20101, 4), finished=False)
        self.assertEqual(observer.wait("request", timeout=2), "inconclusive")
        self.assertEqual(reader.calls, [])
        for state in (Profile(600, 4), Profile(20101, 3), Profile(123, 4)):
            observer, _, _ = self.observe(state)
            self.assertEqual(observer.wait("request", timeout=2), "inconclusive")

    def test_deadline_after_barrier_fails_without_profile_read(self):
        now = [0]
        class SlowBarrier:
            def finished(self, request, *, timeout):
                now[0] = 2
                return True
        reader = Reader(Profile(600, 3))
        observer = OutcomeObserver(reader, SlowBarrier(), clock=lambda: now[0])
        observer.register("request", Profile(600, 3), 20101)
        with self.assertRaises(TimeoutError):
            observer.wait("request", timeout=2)
        self.assertEqual(reader.calls, [])


if __name__ == "__main__":
    unittest.main()
