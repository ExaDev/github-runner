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
    if args[:2] == ["get", "nodes"] and "-o" in args:
        sys.stdout.write(json.dumps({"items": state.get("nodes", [])}))
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

# A call with a body is the gist update, whose body is recorded because heartbeat.sh discards curl's output. A call without one is the read of the gist's last document, answered from FAKE_PREVIOUS (a file holding the health document the previous tick published) or refused when the test has none.
FAKE_CURL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    if "-d" in args:
        open(os.environ["FAKE_CURL_BODY"], "w").write(args[args.index("-d") + 1])
        sys.exit(0)
    previous = os.environ.get("FAKE_PREVIOUS")
    if not previous or not os.path.exists(previous):
        sys.exit(22)
    sys.stdout.write(json.dumps({"files": {"fleet-health.json": {"content": open(previous).read()}}}))
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
        controller_replicas: int = 1,
        scale_sets: tuple[str, ...] = ("runners-a", "runners-b"),
        listeners: dict[str, int] | None = None,
        configmap: dict[str, str] | None = None,
        nodes: list[dict[str, object]] | None = None,
        previous: dict[str, object] | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        state = self.dir / "state.json"
        state.write_text(
            json.dumps(
                {
                    "controller_replicas": controller_replicas,
                    "pods": pods or [],
                    "scale_sets": list(scale_sets),
                    "listeners": {"runners-a": 1, "runners-b": 1} if listeners is None else listeners,
                    "configmap": configmap or {},
                    "nodes": [self.node("node1", None)] if nodes is None else nodes,
                }
            )
        )
        body = self.dir / "curl-body.json"
        previous_file = self.dir / "previous-health.json"
        if previous is not None:
            previous_file.write_text(json.dumps(previous))
        env = {
            **os.environ,
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_STATE": str(state),
            "FAKE_CURL_BODY": str(body),
            "FAKE_PREVIOUS": str(previous_file),
            "HEARTBEAT_GH_TOKEN": "test-token",
            "HEARTBEAT_GIST_ID": "test-gist",
            "HEARTBEAT_UNSCHEDULABLE_AFTER_SECONDS": str(UNSCHEDULABLE_AFTER_SECONDS),
            "HEARTBEAT_EXPECTED_SCALE_SETS": " ".join(EXPECTED),
            **(env or {}),
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

    def test_a_fleet_of_nothing_but_not_ready_nodes_withholds_the_refresh(self) -> None:
        result, files = self.tick(nodes=[self.node("a", None, ready=False), self.node("b", None, ready=False)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("no node reports Ready", result.stderr)
        self.assertWithheld(files)

    def test_one_not_ready_node_among_ready_ones_does_not_withhold_the_refresh(self) -> None:
        result, files = self.tick(nodes=[self.node("a", None), self.node("b", None, ready=False)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(TIMESTAMP_FILE, files)

    def test_a_cluster_with_no_nodes_withholds_the_refresh(self) -> None:
        result, files = self.tick(nodes=[])
        self.assertEqual(result.returncode, 1)
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

    def health(self, **kwargs: object) -> dict[str, object]:
        _, files = self.tick(**kwargs)  # type: ignore[arg-type]
        health: dict[str, object] = json.loads(files[HEALTH_FILE])
        return health

    def test_a_healthy_fleet_has_no_unhealthy_onset_and_no_not_ready_nodes(self) -> None:
        health = self.health()
        self.assertIsNone(health["unhealthy_since"])
        self.assertEqual(health["not_ready_nodes"], [])

    def test_a_not_ready_server_is_published_as_one_with_the_time_it_was_first_seen(self) -> None:
        nodes = [self.node("a", None), self.node("b", None, ready=False, control_plane=True), self.node("c", None, ready=False)]
        health = self.health(nodes=nodes)
        self.assertEqual([(n["node"], n["control_plane"]) for n in health["not_ready_nodes"]], [("b", True), ("c", False)])  # type: ignore[attr-defined, union-attr]
        self.assertTrue(all(n["since"] == health["at"] for n in health["not_ready_nodes"]))  # type: ignore[attr-defined, union-attr]

    def test_a_server_regrading_has_stepped_down_is_published_with_its_level(self) -> None:
        nodes = [self.node("a", None), self.node("b", None, ready=False, control_plane=True, regrade_level="etcd-only"), self.node("c", None, ready=False, control_plane=True)]
        health = self.health(nodes=nodes)
        self.assertEqual([(n["node"], n["regrade_level"]) for n in health["not_ready_nodes"]], [("b", "etcd-only"), ("c", None)])  # type: ignore[attr-defined, union-attr]

    def test_a_ready_server_at_a_level_below_full_is_not_listed(self) -> None:
        # control-plane is cordoned and drained but still Ready, so there is nothing to report.
        health = self.health(nodes=[self.node("a", None), self.node("b", None, control_plane=True, regrade_level="control-plane")])
        self.assertEqual(health["not_ready_nodes"], [])

    def test_the_label_the_level_is_read_from_can_be_changed(self) -> None:
        node = self.node("b", None, ready=False, control_plane=True)
        node["metadata"]["labels"]["example.test/level"] = "etcd-only"  # type: ignore[index]
        _, files = self.tick(nodes=[self.node("a", None), node], env={"HEARTBEAT_REGRADE_LEVEL_LABEL": "example.test/level"})
        self.assertEqual(json.loads(files[HEALTH_FILE])["not_ready_nodes"][0]["regrade_level"], "etcd-only")

    def test_a_node_still_not_ready_keeps_the_onset_the_last_tick_published(self) -> None:
        previous = {"healthy": True, "unhealthy_since": None, "not_ready_nodes": [{"node": "b", "control_plane": True, "since": "2000-01-01T00:00:00Z"}]}
        nodes = [self.node("a", None), self.node("b", None, ready=False, control_plane=True), self.node("c", None, ready=False)]
        health = self.health(nodes=nodes, previous=previous)
        since = {n["node"]: n["since"] for n in health["not_ready_nodes"]}  # type: ignore[attr-defined, union-attr]
        self.assertEqual(since["b"], "2000-01-01T00:00:00Z")
        self.assertEqual(since["c"], health["at"])

    def test_an_unhealthy_fleet_publishes_when_it_stopped_being_healthy(self) -> None:
        health = self.health(controller_replicas=0)
        self.assertEqual(health["unhealthy_since"], health["at"])

    def test_a_fleet_still_unhealthy_keeps_the_onset_the_last_tick_published(self) -> None:
        previous = {"healthy": False, "unhealthy_since": "2000-01-01T00:00:00Z", "not_ready_nodes": []}
        health = self.health(controller_replicas=0, previous=previous)
        self.assertEqual(health["unhealthy_since"], "2000-01-01T00:00:00Z")

    def test_a_fleet_that_was_healthy_last_tick_starts_a_new_onset(self) -> None:
        previous = {"healthy": True, "unhealthy_since": "2000-01-01T00:00:00Z", "not_ready_nodes": []}
        health = self.health(controller_replicas=0, previous=previous)
        self.assertEqual(health["unhealthy_since"], health["at"])

    def test_a_failed_read_of_the_last_document_restarts_every_onset_at_now(self) -> None:
        health = self.health(controller_replicas=0, nodes=[self.node("b", None, ready=False, control_plane=True), self.node("a", None)])
        self.assertEqual(health["unhealthy_since"], health["at"])
        self.assertEqual(health["not_ready_nodes"][0]["since"], health["at"])  # type: ignore[index]


    ANNOTATION = "github-runner.exadev.com/deployed-version"

    def node(self, name: str, version: str | None, ready: bool = True, control_plane: bool = False, regrade_level: str | None = None) -> dict[str, object]:
        annotations = {} if version is None else {self.ANNOTATION: version}
        labels = {"node-role.kubernetes.io/control-plane": "true"} if control_plane else {}
        if regrade_level is not None:
            labels["github-runner.exadev.com/regrade-level"] = regrade_level
        return {
            "metadata": {"name": name, "annotations": annotations, "labels": labels},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
        }

    def deployed(self, **kwargs: object) -> dict[str, object]:
        result, files = self.tick(**kwargs)  # type: ignore[arg-type]
        self.assertEqual(result.returncode, 0, result.stderr)
        deployed: dict[str, object] = json.loads(files[HEALTH_FILE])["deployed"]
        return deployed

    def test_each_scope_reports_the_version_it_last_recorded(self) -> None:
        deployed = self.deployed(configmap={"arc": "1.17.0"}, nodes=[self.node("mini", "1.17.0"), self.node("m3", "1.16.0")])
        self.assertEqual(deployed, {"platform": "1.17.0", "nodes": {"mini": "1.17.0", "m3": "1.16.0"}})

    def test_a_scope_with_no_stamp_is_null_so_it_shows_as_behind(self) -> None:
        deployed = self.deployed(nodes=[self.node("mini", None)])
        self.assertEqual(deployed, {"platform": None, "nodes": {"mini": None}})

    def test_the_deployed_state_is_published_even_when_the_fleet_is_unhealthy(self) -> None:
        result, files = self.tick(controller_replicas=0, configmap={"arc": "1.17.0"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(files[HEALTH_FILE])["deployed"]["platform"], "1.17.0")


if __name__ == "__main__":
    unittest.main()
