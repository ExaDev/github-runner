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
EXPECTED = ("arc-runners-a/runners-a", "arc-runners-b/runners-b")

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
    if args[:2] == ["get", "autoscalingrunnerset"]:
        sys.exit(0 if args[2] in state["scale_sets"] else 1)
    if args[:2] == ["get", "pods"] and "-A" not in args:
        selector = args[args.index("-l") + 1]
        release = next(part.split("=")[1] for part in selector.split(",") if part.startswith("actions.github.com/scale-set-name"))
        sys.stdout.write("Running\\n" * state["listeners"].get(release, 0))
        sys.exit(0)
    if args[:2] == ["get", "pods"]:
        sys.stdout.write(json.dumps({"items": state["pods"]}))
        sys.exit(0)
    if args[:2] == ["get", "configmap"]:
        key = args[-1].split("data.")[-1].rstrip("}").replace("\\.", ".")
        value = state["configmap"].get(key)
        if value is None:
            sys.exit(1)
        sys.stdout.write(value)
        sys.exit(0)
    sys.stderr.write("unexpected kubectl call: %s\\n" % args)
    sys.exit(2)
    """
)

# Records the body of the gist update; heartbeat.sh discards curl's output, so a file is the only way to see the call.
FAKE_CURL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, sys
    args = sys.argv[1:]
    open(os.environ["FAKE_CURL_BODY"], "w").write(args[args.index("-d") + 1])
    """
)

TIMESTAMP_FILE = "arc-healthy-until"
HEALTH_FILE = "fleet-health.json"


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

    def tick(
        self,
        pods: list[dict[str, object]] | None = None,
        node_ready: bool = True,
        controller_replicas: int = 1,
        scale_sets: tuple[str, ...] = ("runners-a", "runners-b"),
        listeners: dict[str, int] | None = None,
        configmap: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        state = self.dir / "state.json"
        state.write_text(
            json.dumps(
                {
                    "node_ready": node_ready,
                    "controller_replicas": controller_replicas,
                    "pods": pods or [],
                    "scale_sets": list(scale_sets),
                    "listeners": {"runners-a": 1, "runners-b": 1} if listeners is None else listeners,
                    "configmap": configmap or {},
                }
            )
        )
        body = self.dir / "curl-body.json"
        env = {
            **os.environ,
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_STATE": str(state),
            "FAKE_CURL_BODY": str(body),
            "HEARTBEAT_GH_TOKEN": "test-token",
            "HEARTBEAT_GIST_ID": "test-gist",
            "HEARTBEAT_UNSCHEDULABLE_AFTER_SECONDS": str(UNSCHEDULABLE_AFTER_SECONDS),
            "HEARTBEAT_EXPECTED_SCALE_SETS": " ".join(EXPECTED),
        }
        result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)
        files = {name: entry["content"] for name, entry in json.loads(body.read_text())["files"].items()} if body.exists() else {}
        return result, files

    def assertWithheld(self, files: dict[str, str]) -> None:
        """The refresh is withheld: no timestamp is written, but the health file says why."""
        self.assertNotIn(TIMESTAMP_FILE, files)
        health = json.loads(files[HEALTH_FILE])
        self.assertFalse(health["healthy"])
        self.assertTrue(health["reasons"])

    def test_a_healthy_fleet_refreshes_the_gist(self) -> None:
        result, files = self.tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(TIMESTAMP_FILE, files)
        self.assertTrue(json.loads(files[HEALTH_FILE])["healthy"])

    def test_a_runner_pod_unschedulable_for_the_threshold_withholds_the_refresh(self) -> None:
        result, files = self.tick(pods=[unschedulable_pod(UNSCHEDULABLE_AFTER_SECONDS + 1)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("unschedulable", result.stderr)
        self.assertWithheld(files)

    def test_a_runner_pod_only_just_unschedulable_does_not_withhold_the_refresh(self) -> None:
        result, files = self.tick(pods=[unschedulable_pod(0)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(TIMESTAMP_FILE, files)

    def test_a_missing_scale_set_withholds_the_refresh(self) -> None:
        result, files = self.tick(scale_sets=("runners-a",))
        self.assertEqual(result.returncode, 1)
        self.assertIn("arc-runners-b/runners-b does not exist", result.stderr)
        self.assertWithheld(files)

    def test_a_scale_set_without_a_running_listener_withholds_the_refresh(self) -> None:
        result, files = self.tick(listeners={"runners-a": 1, "runners-b": 0})
        self.assertEqual(result.returncode, 1)
        self.assertIn("arc-runners-b/runners-b has no running listener", result.stderr)
        self.assertWithheld(files)

    def test_a_controller_without_a_ready_replica_withholds_the_refresh(self) -> None:
        result, files = self.tick(controller_replicas=0)
        self.assertEqual(result.returncode, 1)
        self.assertWithheld(files)

    @staticmethod
    def job_peaks(*peaks: int) -> str:
        return json.dumps(
            {
                f"secret-org/private-repo|secret-org/private-repo/.github/workflows/ci.yml@refs/heads/main|job-{i}": {
                    "repo": "secret-org/private-repo",
                    "workflow": "ci.yml",
                    "job": f"job-{i}",
                    "peak_mib": peak,
                    "last_seen": 1,
                }
                for i, peak in enumerate(peaks)
            }
        )

    def test_the_distribution_of_measured_peaks_is_published_without_any_names(self) -> None:
        configmap = {"job-peak": self.job_peaks(100, 200, 300, 4000), "status.json": json.dumps({"pod_limit_mib": 7168})}
        result, files = self.tick(configmap=configmap)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(files["job-peak-summary.json"])
        self.assertEqual(
            summary,
            {"jobs": 4, "repos": 1, "first_seen": "1970-01-01T00:00:01Z", "last_seen": "1970-01-01T00:00:01Z", "p50_mib": 200, "p95_mib": 4000, "max_mib": 4000, "pod_limit_mib": 7168},
        )
        self.assertNotIn("secret-org", files["job-peak-summary.json"])
        self.assertNotIn("private-repo", files["job-peak-summary.json"])

    def test_no_summary_is_published_before_any_peak_has_been_measured(self) -> None:
        result, files = self.tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("job-peak-summary.json", files)

    def test_the_summary_is_published_even_when_the_fleet_is_unhealthy(self) -> None:
        result, files = self.tick(controller_replicas=0, configmap={"job-peak": self.job_peaks(150)})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(files["job-peak-summary.json"])["jobs"], 1)

    def test_the_summary_counts_distinct_repositories_and_the_period_covered(self) -> None:
        entries = {
            "a|w|j1": {"repo": "org/a", "workflow": "w", "job": "j1", "peak_mib": 100, "last_seen": 1000},
            "a|w|j2": {"repo": "org/a", "workflow": "w", "job": "j2", "peak_mib": 200, "last_seen": 3000},
            "b|w|j1": {"repo": "org/b", "workflow": "w", "job": "j1", "peak_mib": 300, "last_seen": 2000},
        }
        result, files = self.tick(configmap={"job-peak": json.dumps(entries)})
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(files["job-peak-summary.json"])
        self.assertEqual((summary["jobs"], summary["repos"]), (3, 2))
        self.assertEqual((summary["first_seen"], summary["last_seen"]), ("1970-01-01T00:16:40Z", "1970-01-01T00:50:00Z"))
        self.assertNotIn("org/a", files["job-peak-summary.json"])


if __name__ == "__main__":
    unittest.main()
