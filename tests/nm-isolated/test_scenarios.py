"""Offline scenario orchestration tests using a deterministic fake adapter."""

import unittest
from dataclasses import dataclass

import scenarios


@dataclass(frozen=True)
class Profile:
    metric: int
    version: int


class Adapter:
    def __init__(self):
        self.current = Profile(600, 2)
        self.events = []
        self.pending = None
        self.bus_owner = ":1.1"
        self.advance_fence = True
        self.ignore_fence = False
        self.allowing = False
        self.retired = False

    def profile(self):
        if self.retired:
            raise RuntimeError("retired")
        return self.current

    def retire(self):
        self.retired = True

    def submit_held(self, *, metric, fenced):
        if self.allowing:
            raise RuntimeError("request was not held")
        self.events.append(("submit", metric, fenced))
        self.pending = (metric, self.current.version if fenced else None)
        return "held"

    def disconnect(self, request):
        self.events.append(("disconnect", request))

    def allow_later(self):
        self.allowing = True
        self.events.append("allow-later")

    def hold_later(self):
        self.allowing = False
        self.events.append("hold-later")

    def update_separately(self, *, metric, fenced):
        self.events.append(("separate", metric, fenced))
        if self.advance_fence:
            self.current = Profile(metric, self.current.version + 1)

    def release(self, request):
        self.events.append(("release", request))
        metric, version = self.pending
        self.rejected = version is not None and version != self.current.version and not self.ignore_fence
        if not self.rejected:
            self.current = Profile(metric, self.current.version + 1)

    def await_completion(self, request):
        if self.rejected:
            raise AssertionError("unexpected rejection")

    def await_rejection(self, request):
        if not self.rejected:
            raise AssertionError("expected rejection")

    def owner(self):
        return self.bus_owner

    def restart(self):
        self.bus_owner = ":1.2"
        self.current = Profile(self.current.metric, 1)


class ScenarioTests(unittest.TestCase):
    def test_unfenced_delayed_write_changes_profile(self):
        adapter = Adapter()
        before, after = scenarios.no_fence(adapter, 20101)
        self.assertEqual((before, after), (Profile(600, 2), Profile(20101, 3)))
        self.assertEqual(adapter.events, [("submit", 20101, False), ("disconnect", "held"), ("release", "held")])

    def test_separate_fence_changes_version_before_release(self):
        adapter = Adapter()
        before, fence, after = scenarios.fenced(adapter, 20101)
        self.assertEqual((before, fence, after), (Profile(600, 2), Profile(600, 3), Profile(600, 3)))
        self.assertEqual(adapter.events, [("submit", 20101, True), ("disconnect", "held"), "allow-later", ("separate", 600, True), "hold-later", ("release", "held")])

    def test_no_version_advance_or_ignored_fence_fails(self):
        adapter = Adapter()
        adapter.advance_fence = False
        with self.assertRaisesRegex(AssertionError, "advance version"):
            scenarios.fenced(adapter, 20101)
        adapter = Adapter()
        adapter.ignore_fence = True
        with self.assertRaisesRegex(AssertionError, "expected rejection"):
            scenarios.fenced(adapter, 20101)

    def test_next_scenario_holds_after_success_and_writer_failure(self):
        adapter = Adapter()
        scenarios.fenced(adapter, 20101)
        scenarios.no_fence(adapter, 20102)
        adapter = Adapter()
        def fail(**kwargs):
            raise RuntimeError("writer failed")
        adapter.update_separately = fail
        with self.assertRaisesRegex(RuntimeError, "writer failed"):
            scenarios.fenced(adapter, 20101)
        self.assertFalse(adapter.allowing)
        self.assertTrue(adapter.retired)
        with self.assertRaisesRegex(RuntimeError, "retired"):
            scenarios.no_fence(adapter, 20102)

    def test_each_post_hold_failure_retires_authority(self):
        for stage in ("disconnect", "allow_later", "update_separately", "hold_later", "release", "await_rejection"):
            with self.subTest(stage=stage):
                adapter = Adapter()
                def fail(*args, **kwargs):
                    raise TimeoutError("injected")
                setattr(adapter, stage, fail)
                with self.assertRaises(TimeoutError):
                    scenarios.fenced(adapter, 20101)
                self.assertTrue(adapter.retired)
                with self.assertRaisesRegex(RuntimeError, "retired"):
                    scenarios.no_fence(adapter, 20102)

    def test_restart_requires_changed_unique_owner(self):
        adapter = Adapter()
        result = scenarios.restart(adapter)
        self.assertEqual(result, (":1.1", Profile(600, 2), ":1.2", Profile(600, 1)))
        adapter.restart = lambda: None
        with self.assertRaisesRegex(AssertionError, "owner did not change"):
            scenarios.restart(adapter)

    def test_identical_test_metric_is_refused(self):
        for scenario in (scenarios.no_fence, scenarios.fenced):
            with self.assertRaises(ValueError):
                scenario(Adapter(), 600)


if __name__ == "__main__":
    unittest.main()
