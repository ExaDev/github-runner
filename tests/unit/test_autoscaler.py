"""Tests for scripts/autoscaler.sh, one poll of the usage-driven autoscaler, run in dry-run mode against a scripted stand-in for kubectl with the real bash and jq."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "autoscaler.sh"
NAMESPACES = ("ns-a", "ns-b")
RUNNING = 2
MAX_RUNNERS = 3

# Answers the reads autoscaler.sh makes from FAKE_STATE (a JSON file): per-namespace pods, whether `kubectl top pod` fails, and the memory limit; every patch is accepted.
FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    state = json.load(open(os.environ["FAKE_STATE"]))
    joined = " ".join(args)
    if args[:2] == ["get", "autoscalingrunnerset"]:
        if "status.currentRunners" in joined:
            if state["running"] is not None:
                sys.stdout.write(str(state["running"]))
        elif "spec.maxRunners" in joined:
            sys.stdout.write(str(state["max_runners"]))
        elif "limits.memory" in joined:
            sys.stdout.write("7Gi")
        sys.exit(0)
    if args[:2] == ["get", "pods"]:
        namespace = args[args.index("-n") + 1]
        sys.stdout.write(json.dumps({"items": state["pods"].get(namespace, [])}))
        sys.exit(0)
    if args[:2] == ["top", "pod"]:
        if state["top_pod_fails"]:
            sys.exit(1)
        namespace = args[args.index("-n") + 1]
        for name, mib in state["top"].get(namespace, {}).items():
            sys.stdout.write("%s 10m %dMi\\n" % (name, mib))
        sys.exit(0)
    if args[:2] == ["get", "ephemeralrunner"]:
        runner = state["runners"].get(args[2])
        if runner is None:
            sys.exit(1)
        sys.stdout.write(json.dumps({"status": runner}))
        sys.exit(0)
    if args[:2] == ["top", "nodes"]:
        sys.stdout.write("node 100m 5% 1000Mi 20%\\n")
        sys.exit(0)
    if args[:2] == ["get", "events"]:
        namespace = args[args.index("-n") + 1]
        sys.stdout.write(json.dumps({"items": [{"message": m} for m in state["events"].get(namespace, [])]}))
        sys.exit(0)
    if args[:2] == ["get", "configmap"]:
        key = args[-1].split("data.")[-1].rstrip("}")
        stored = {"job-memory": state["job_memory"], "job-peak": state["job_peak"]}
        sys.stdout.write(stored.get(key, "0"))
        sys.exit(0)
    if args[:2] == ["patch", "configmap"]:
        data = json.loads(args[args.index("-p") + 1])["data"]
        for key, out in (("job-memory", "FAKE_JOB_MEMORY_OUT"), ("job-peak", "FAKE_JOB_PEAK_OUT")):
            if key in data:
                if state["job_memory_patch_fails"]:
                    sys.exit(1)
                open(os.environ[out], "w").write(data[key])
        sys.exit(0)
    if args[:1] == ["patch"]:
        sys.exit(0)
    sys.stderr.write("unexpected kubectl call: %s\\n" % args)
    sys.exit(2)
    """
)

UNSCHEDULABLE_POD = {"status": {"conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]}}
SCHEDULED_POD = {"status": {"conditions": [{"type": "PodScheduled", "status": "True"}]}}
JOB_A = {"jobRepositoryName": "org/repo", "jobWorkflowRef": ".github/workflows/ci.yml@refs/heads/main", "jobDisplayName": "Build"}


class AutoscalerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        kubectl = self.dir / "kubectl"
        kubectl.write_text(FAKE_KUBECTL)
        kubectl.chmod(0o755)

    def poll(
        self,
        pods: dict[str, list[dict[str, object]]],
        top_pod_fails: bool = False,
        running: int | None = RUNNING,
        ceiling: int = 8,
        top: dict[str, dict[str, int]] | None = None,
        runners: dict[str, dict[str, str]] | None = None,
        job_memory: str = "",
        job_memory_patch_fails: bool = False,
        events: dict[str, list[str]] | None = None,
        job_peak: str = "",
    ) -> subprocess.CompletedProcess[str]:
        state = self.dir / "state.json"
        state.write_text(
            json.dumps(
                {
                    "running": running,
                    "max_runners": MAX_RUNNERS,
                    "pods": pods,
                    "top_pod_fails": top_pod_fails,
                    "top": top or {},
                    "runners": runners or {},
                    "job_memory": job_memory,
                    "job_peak": job_peak,
                    "events": events or {},
                    "job_memory_patch_fails": job_memory_patch_fails,
                }
            )
        )
        self.job_memory_out = self.dir / "job-memory.json"
        self.job_memory_out.unlink(missing_ok=True)
        self.job_peak_out = self.dir / "job-peak.json"
        self.job_peak_out.unlink(missing_ok=True)
        env = {
            **os.environ,
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_STATE": str(state),
            "FAKE_JOB_MEMORY_OUT": str(self.job_memory_out),
            "FAKE_JOB_PEAK_OUT": str(self.job_peak_out),
            "AUTOSCALER_TARGETS": " ".join(f"{ns}/release-{ns}" for ns in NAMESPACES),
            "AUTOSCALER_DRY_RUN": "true",
            "AUTOSCALER_USABLE_BUDGET_GI": "33",
            "AUTOSCALER_MAX_CEILING": str(ceiling),
            "AUTOSCALER_FLOOR": "3",
        }
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)

    def test_an_unschedulable_runner_pod_lowers_every_target_to_its_running_count(self) -> None:
        result = self.poll({"ns-a": [UNSCHEDULABLE_POD]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unschedulable=1", result.stdout)
        for namespace in NAMESPACES:
            self.assertIn(f"would patch {namespace}/release-{namespace}'s maxRunners to {RUNNING}", result.stdout)

    def test_scheduled_pods_leave_the_targets_alone(self) -> None:
        result = self.poll({"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unschedulable=0", result.stdout)
        self.assertNotIn("would patch", result.stdout)

    def test_a_pooled_total_above_the_ceiling_is_lowered_to_the_ceiling(self) -> None:
        ceiling = RUNNING * len(NAMESPACES)
        result = self.poll({"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}, running=0, ceiling=ceiling)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("above the ceiling", result.stdout)
        for namespace in NAMESPACES:
            self.assertIn(f"would patch {namespace}/release-{namespace}'s maxRunners to {RUNNING}", result.stdout)

    def test_a_pooled_total_at_the_ceiling_is_left_alone(self) -> None:
        result = self.poll({"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}, ceiling=MAX_RUNNERS * len(NAMESPACES))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("would patch", result.stdout)

    def test_a_scale_set_that_has_never_run_a_pod_counts_as_zero_runners(self) -> None:
        result = self.poll({"ns-a": [UNSCHEDULABLE_POD]}, running=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("R_total=0", result.stdout)
        for namespace in NAMESPACES:
            self.assertIn(f"would patch {namespace}/release-{namespace}'s maxRunners to 0", result.stdout)

    def test_a_failing_memory_measurement_fails_safe_for_every_target(self) -> None:
        result = self.poll({}, top_pod_fails=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("unbound variable", result.stderr)
        self.assertIn("FAIL-SAFE", result.stderr)
        for namespace in NAMESPACES:
            self.assertIn(f"would patch {namespace}/release-{namespace}'s maxRunners to {RUNNING}", result.stdout)

    def recorded(self) -> dict[str, dict[str, object]]:
        return json.loads(self.job_memory_out.read_text())

    def test_a_jobs_highest_observed_memory_is_kept_across_polls(self) -> None:
        runners = {"runner-1": JOB_A}
        pods: dict[str, list[dict[str, object]]] = {}
        stored = ""
        for mib in (500, 900, 300):
            self.assertEqual(self.poll(pods, top={"ns-a": {"runner-1": mib}}, runners=runners, job_memory=stored).returncode, 0)
            stored = self.job_memory_out.read_text()
        entry = self.recorded()["org/repo|.github/workflows/ci.yml@refs/heads/main|Build"]
        self.assertEqual(entry["peak_mib"], 900)
        self.assertEqual(entry["samples"], 3)

    def test_a_pod_without_job_details_is_not_recorded(self) -> None:
        result = self.poll({}, top={"ns-a": {"runner-1": 700}}, runners={"runner-1": {}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.job_memory_out.exists())

    def test_a_job_not_seen_within_the_retention_is_dropped(self) -> None:
        stale = json.dumps({"old|wf|job": {"repo": "old", "workflow": "wf", "job": "job", "peak_mib": 4000, "samples": 9, "last_seen": 1}})
        result = self.poll({}, top={"ns-a": {"runner-1": 600}}, runners={"runner-1": JOB_A}, job_memory=stale)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("old|wf|job", self.recorded())

    def test_failing_to_record_does_not_stop_the_capacity_decision(self) -> None:
        result = self.poll({"ns-a": [UNSCHEDULABLE_POD]}, top={"ns-a": {"runner-1": 600}}, runners={"runner-1": JOB_A}, job_memory_patch_fails=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not record", result.stderr)
        for namespace in NAMESPACES:
            self.assertIn(f"would patch {namespace}/release-{namespace}'s maxRunners to {RUNNING}", result.stdout)

    @staticmethod
    def event(peak_bytes: int, job: str = "build") -> str:
        return json.dumps({"repo": "org/repo", "workflow": "org/repo/.github/workflows/ci.yml@refs/heads/main", "job": job, "peak_bytes": peak_bytes})

    def measured(self) -> dict[str, dict[str, object]]:
        return json.loads(self.job_peak_out.read_text())

    def test_a_runners_own_measured_peak_is_kept_as_whole_mebibytes_rounded_up(self) -> None:
        result = self.poll({}, events={"ns-a": [self.event(3 * 1024 * 1024 + 1)]})
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.measured()["org/repo|org/repo/.github/workflows/ci.yml@refs/heads/main|build"]
        self.assertEqual(entry["peak_mib"], 4)
        self.assertNotIn("samples", entry)

    def test_a_forged_lower_peak_cannot_lower_a_stored_one(self) -> None:
        key = "org/repo|org/repo/.github/workflows/ci.yml@refs/heads/main|build"
        stored = json.dumps({key: {"repo": "org/repo", "workflow": "org/repo/.github/workflows/ci.yml@refs/heads/main", "job": "build", "peak_mib": 900, "last_seen": 4102444800}})
        result = self.poll({}, events={"ns-a": [self.event(10 * 1024 * 1024)]}, job_peak=stored)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.measured()[key]["peak_mib"], 900)

    def test_an_event_that_is_not_the_expected_message_is_ignored(self) -> None:
        events = {"ns-a": ["not json", json.dumps({"repo": "org/repo"}), json.dumps({"repo": "r", "workflow": "w", "job": "j", "peak_bytes": "big"})]}
        result = self.poll({}, events=events)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.job_peak_out.exists())


if __name__ == "__main__":
    unittest.main()
