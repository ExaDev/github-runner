"""Unit tests for the private-bus authorisation fixture, without D-Bus."""

import importlib.util
import unittest
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "nm-isolated/polkit_stub.py"
spec = importlib.util.spec_from_file_location("isolated_polkit", path)
assert spec is not None and spec.loader is not None
stub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stub)


class FakeDaemon:
    def __init__(self):
        self.metric = 600
        self.version = 2

    def submit(self, authority, request_id, version, metric):
        def authorised(result):
            if result[0] and version == self.version:
                self.metric = metric
                self.version += 1
        authority.hold(("unix-process", {"uid": 1000}), request_id, authorised, lambda error: None)


class PolicyFixtureTests(unittest.TestCase):
    def test_no_fence_allows_delayed_request(self):
        daemon = FakeDaemon()
        authority = stub.HeldAuthorisation()
        daemon.submit(authority, "pending", daemon.version, 20101)
        authority.release("pending", True)
        self.assertEqual(daemon.metric, 20101)

    def test_identical_fence_rejects_delayed_request(self):
        daemon = FakeDaemon()
        authority = stub.HeldAuthorisation()
        daemon.submit(authority, "pending", daemon.version, 20101)
        authority.allow_later = True
        daemon.submit(authority, "fence", daemon.version, 600)
        self.assertEqual((daemon.metric, daemon.version), (600, 3))
        self.assertEqual(list(authority.pending), ["pending"])
        authority.release("pending", True)
        self.assertEqual((daemon.metric, daemon.version), (600, 3))

    def test_allow_later_does_not_release_existing_and_still_refuses_root(self):
        authority = stub.HeldAuthorisation()
        replies, errors = [], []
        authority.hold(("unix-process", {"uid": 1000}), "old", replies.append, errors.append)
        authority.allow_later = True
        authority.hold(("unix-process", {"uid": 1000}), "new", replies.append, errors.append)
        authority.hold(("unix-process", {"uid": 0}), "root", replies.append, errors.append)
        self.assertEqual(replies, [(True, False, {})])
        self.assertEqual(list(authority.pending), ["old"])
        self.assertEqual(len(errors), 1)
        authority.release("old", True)
        self.assertEqual(len(replies), 2)

    def test_refuses_root_and_cancels_pending_request(self):
        authority = stub.HeldAuthorisation()
        errors = []
        authority.hold(("unix-process", {"uid": 0}), "root", lambda result: self.fail(), errors.append)
        self.assertEqual(len(errors), 1)
        authority.hold(("unix-process", {"uid": 1000}), "pending", lambda result: self.fail(), errors.append)
        authority.cancel("pending")
        self.assertEqual(errors[-1], "cancelled")
        self.assertEqual(authority.pending, {})


if __name__ == "__main__":
    unittest.main()
