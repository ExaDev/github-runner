"""Tests for scripts/heartbeat.sh, one tick of the fleet heartbeat, run against scripted stand-ins for kubectl and curl with the real bash and jq."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "heartbeat.sh"
UNSCHEDULABLE_AFTER_SECONDS = 180

# Answers the reads heartbeat.sh makes from FAKE_STATE (a JSON file): whether a node is Ready, whether the controller has a ready replica, and the runner pods.
FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    state = json.load(open(os.environ["FAKE_STATE"]))
    if args[:2] == ["get", "nodes"]:
        sys.stdout.write("node1 Ready\\n" if state["node_ready"] else "node1 NotReady\\n")
        sys.exit(0)
    if args[:2] == ["get", "deployment"]:
        sys.stdout.write(str(state["controller_replicas"]))
        sys.exit(0)
    if args[:2] == ["get", "pods"]:
        sys.stdout.write(json.dumps({"items": state["pods"]}))
        sys.exit(0)
    if args[:2] == ["get", "configmap"]:
        sys.exit(1)
    sys.stderr.write("unexpected kubectl call: %s\\n" % args)
    sys.exit(2)
    """
)

# Records that the gist was written; heartbeat.sh discards curl's output, so a file is the only way to see the call.
FAKE_CURL = textwrap.dedent(
    """\
    #!/bin/sh
    touch "$FAKE_CURL_CALLED"
    """
)


def unschedulable_pod(seconds_ago: int) -> dict[str, object]:
    since = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"status": {"conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "lastTransitionTime": since}]}}


class HeartbeatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        for name, body in (("kubectl", FAKE_KUBECTL), ("curl", FAKE_CURL)):
            path = self.dir / name
            path.write_text(body)
            path.chmod(0o755)

    def tick(self, pods: list[dict[str, object]] | None = None, node_ready: bool = True, controller_replicas: int = 1) -> tuple[subprocess.CompletedProcess[str], bool]:
        state = self.dir / "state.json"
        state.write_text(json.dumps({"node_ready": node_ready, "controller_replicas": controller_replicas, "pods": pods or []}))
        called = self.dir / "curl-called"
        env = {
            **os.environ,
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_STATE": str(state),
            "FAKE_CURL_CALLED": str(called),
            "HEARTBEAT_GH_TOKEN": "test-token",
            "HEARTBEAT_GIST_ID": "test-gist",
            "HEARTBEAT_UNSCHEDULABLE_AFTER_SECONDS": str(UNSCHEDULABLE_AFTER_SECONDS),
        }
        result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)
        return result, called.exists()

    def test_a_healthy_fleet_refreshes_the_gist(self) -> None:
        result, wrote = self.tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(wrote)

    def test_a_runner_pod_unschedulable_for_the_threshold_withholds_the_refresh(self) -> None:
        result, wrote = self.tick(pods=[unschedulable_pod(UNSCHEDULABLE_AFTER_SECONDS + 1)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("unschedulable", result.stderr)
        self.assertFalse(wrote)

    def test_a_runner_pod_only_just_unschedulable_does_not_withhold_the_refresh(self) -> None:
        result, wrote = self.tick(pods=[unschedulable_pod(0)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(wrote)

    def test_a_controller_without_a_ready_replica_withholds_the_refresh(self) -> None:
        result, wrote = self.tick(controller_replicas=0)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(wrote)


if __name__ == "__main__":
    unittest.main()
