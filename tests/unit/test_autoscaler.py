"""Tests for scripts/autoscaler.sh, one poll of the usage-driven autoscaler, run in dry-run mode against a scripted stand-in for kubectl with the real bash and jq."""

from __future__ import annotations

import json
import os
import re
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
        # The running count is no longer read from this object's status (ARC 0.15 leaves it out), so a read of it is an unexpected call.
        if "spec.maxRunners" in joined:
            release = args[2]
            sys.stdout.write(str(state["max_runners"].get(release, state["default_max_runners"])))
        elif "limits.memory" in joined:
            sys.stdout.write("7Gi")
        else:
            sys.stderr.write("unexpected kubectl call: %s\\n" % args)
            sys.exit(2)
        sys.exit(0)
    if args[:2] == ["get", "ephemeralrunners"]:
        if state["ephemeral_fails"]:
            sys.exit(1)
        namespace = args[args.index("-n") + 1]
        sys.stdout.write(json.dumps({"items": [{"status": {"phase": phase}} for phase in state["ephemeral"].get(namespace, [])]}))
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
        sys.stdout.write("node 100m 5%% 1000Mi %d%%\\n" % state["node_mem_pct"])
        sys.exit(0)
    if args[:2] == ["get", "events"]:
        namespace = args[args.index("-n") + 1]
        sys.stdout.write(json.dumps({"items": [{"message": m} for m in state["events"].get(namespace, [])]}))
        sys.exit(0)
    stored_path = os.environ["FAKE_STATE"] + ".configmap"
    stored = json.load(open(stored_path)) if os.path.exists(stored_path) else {}
    if args[:2] == ["get", "configmap"]:
        key = args[-1].split("data.")[-1].rstrip("}")
        fixtures = {"job-memory": state["job_memory"], "job-peak": state["job_peak"], **state["configmap"]}
        sys.stdout.write(stored.get(key, fixtures.get(key, "0")))
        sys.exit(0)
    if args[:2] == ["patch", "configmap"]:
        # The patch arrives as a file, never as an argument: a patch can be larger than one argument may be.
        if "-p" in args:
            sys.stderr.write("the patch was passed as an argument\\n")
            sys.exit(7)
        data = json.load(open(args[args.index("--patch-file") + 1]))["data"]
        for key, out in (("job-memory", "FAKE_JOB_MEMORY_OUT"), ("job-peak", "FAKE_JOB_PEAK_OUT")):
            if key in data:
                if state["job_memory_patch_fails"]:
                    sys.exit(1)
                open(os.environ[out], "w").write(data[key])
        stored.update(data)
        json.dump(stored, open(stored_path, "w"))
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
        namespaces: tuple[str, ...] = NAMESPACES,
        max_runners: dict[str, int] | None = None,
        ephemeral: dict[str, list[str]] | None = None,
        ephemeral_fails: bool = False,
        node_mem_pct: int = 20,
        configmap: dict[str, str] | None = None,
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
                    "default_max_runners": MAX_RUNNERS,
                    "max_runners": {f"release-{ns}": count for ns, count in (max_runners or {}).items()},
                    "ephemeral": ephemeral if ephemeral is not None else {ns: ["Running"] * (running or 0) for ns in namespaces},
                    "ephemeral_fails": ephemeral_fails,
                    "node_mem_pct": node_mem_pct,
                    "configmap": configmap or {},
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
            "AUTOSCALER_TARGETS": " ".join(f"{ns}/release-{ns}" for ns in namespaces),
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

    def test_a_scale_set_with_no_ephemeral_runners_counts_as_zero_runners(self) -> None:
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

    def stored(self, key: str) -> str | None:
        """What the script last wrote to a key of the shared state ConfigMap, or None when it never wrote it."""
        path = self.dir / "state.json.configmap"
        return json.loads(path.read_text()).get(key) if path.exists() else None

    @staticmethod
    def patched_maximums(result: subprocess.CompletedProcess[str]) -> dict[str, int]:
        return {namespace: int(maximum) for namespace, maximum in re.findall(r"would patch (\S+)/release-\S+'s maxRunners to (\d+)", result.stdout)}

    def test_the_running_count_is_the_ephemeral_runners_that_have_not_finished(self) -> None:
        phases = ["Running", "Pending", "", "Succeeded", "Failed"]
        result = self.poll({}, ephemeral={"ns-a": phases, "ns-b": []})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("R_total=3 ", result.stdout)

    def test_an_unreadable_ephemeral_runner_list_stops_the_poll_instead_of_counting_zero(self) -> None:
        result = self.poll({}, ephemeral_fails=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not count", result.stderr)
        self.assertNotIn("R_total", result.stdout)

    # Both targets hold three slots under a ceiling of six, so the pool is healthy and full: the state in which a saturated target used to wait while another sat idle.
    FULL_POOL = {"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}

    def test_one_slot_moves_to_a_saturated_target_once_the_imbalance_is_confirmed(self) -> None:
        runners = {"ns-a": ["Running"] * 3, "ns-b": []}
        first = self.poll(self.FULL_POOL, ceiling=6, ephemeral=runners)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn("would patch", first.stdout)
        self.assertEqual(self.stored("rebalance-confirm-count"), "1")
        second = self.poll(self.FULL_POOL, ceiling=6, ephemeral=runners)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("rebalance:", second.stdout)
        moved = self.patched_maximums(second)
        self.assertEqual(moved, {"ns-a": MAX_RUNNERS + 1, "ns-b": MAX_RUNNERS - 1})
        self.assertEqual(sum(moved.values()), MAX_RUNNERS * len(NAMESPACES))
        self.assertEqual(self.stored("rebalance-confirm-count"), "0")

    def test_nothing_moves_while_every_target_is_busy(self) -> None:
        runners = {"ns-a": ["Running"] * 3, "ns-b": ["Running"] * 3}
        for _ in range(2):
            result = self.poll(self.FULL_POOL, ceiling=6, ephemeral=runners)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("would patch", result.stdout)
        self.assertEqual(self.stored("rebalance-confirm-count"), "0")

    def test_a_confirmation_in_progress_is_dropped_when_the_imbalance_ends(self) -> None:
        runners = {"ns-a": ["Running"] * 3, "ns-b": ["Running"] * 3}
        result = self.poll(self.FULL_POOL, ceiling=6, ephemeral=runners, configmap={"rebalance-confirm-count": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.stored("rebalance-confirm-count"), "0")

    def test_a_donor_with_one_spare_slot_ends_at_its_running_count(self) -> None:
        runners = {"ns-a": ["Running"] * 3, "ns-b": ["Running"] * 2}
        poll = {"pods": self.FULL_POOL, "ceiling": 6, "ephemeral": runners, "configmap": {"rebalance-confirm-count": "1"}}
        result = self.poll(**poll)
        self.assertEqual(self.patched_maximums(result), {"ns-a": MAX_RUNNERS + 1, "ns-b": 2})

    def test_a_donor_with_no_spare_slot_gives_nothing(self) -> None:
        runners = {"ns-a": ["Running"] * 3, "ns-b": ["Running"] * 2}
        result = self.poll(self.FULL_POOL, ceiling=5, ephemeral=runners, max_runners={"ns-a": 3, "ns-b": 2}, configmap={"rebalance-confirm-count": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("would patch", result.stdout)

    def test_a_target_is_never_rebalanced_below_one_runner(self) -> None:
        result = self.poll(self.FULL_POOL, ceiling=4, ephemeral={"ns-a": ["Running"] * 3, "ns-b": []}, max_runners={"ns-a": 3, "ns-b": 1}, configmap={"rebalance-confirm-count": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("would patch", result.stdout)

    def test_the_slot_comes_from_the_target_with_the_most_spare_slots(self) -> None:
        three = ("ns-a", "ns-b", "ns-c")
        runners = {"ns-a": ["Running"] * 3, "ns-b": ["Running"] * 2, "ns-c": []}
        pods = {ns: [SCHEDULED_POD] for ns in three}
        result = self.poll(pods, ceiling=9, namespaces=three, ephemeral=runners, configmap={"rebalance-confirm-count": "1"})
        self.assertEqual(self.patched_maximums(result), {"ns-a": MAX_RUNNERS + 1, "ns-c": MAX_RUNNERS - 1})

    def test_no_slot_moves_under_memory_pressure(self) -> None:
        runners = {"ns-a": ["Running"] * 3, "ns-b": []}
        result = self.poll(self.FULL_POOL, ceiling=6, ephemeral=runners, node_mem_pct=95, configmap={"rebalance-confirm-count": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pressure=true", result.stdout)
        self.assertNotIn("rebalance:", result.stdout)

    def test_no_slot_moves_while_a_runner_pod_cannot_be_scheduled(self) -> None:
        runners = {"ns-a": ["Running"] * 3, "ns-b": []}
        result = self.poll({"ns-a": [UNSCHEDULABLE_POD], "ns-b": [SCHEDULED_POD]}, ceiling=6, ephemeral=runners, configmap={"rebalance-confirm-count": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("rebalance:", result.stdout)

    def test_no_slot_moves_while_the_pool_is_above_its_ceiling(self) -> None:
        runners = {"ns-a": ["Running"] * 3, "ns-b": []}
        result = self.poll(self.FULL_POOL, ceiling=4, ephemeral=runners, configmap={"rebalance-confirm-count": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("above the ceiling", result.stdout)
        self.assertNotIn("rebalance:", result.stdout)

    def test_no_slot_moves_while_the_pool_can_still_grow(self) -> None:
        runners = {"ns-a": ["Running"] * 3, "ns-b": []}
        result = self.poll(self.FULL_POOL, ceiling=8, ephemeral=runners, configmap={"rebalance-confirm-count": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("rebalance:", result.stdout)

    def test_a_record_larger_than_one_argument_may_be_is_still_stored(self) -> None:
        # More bytes than the kernel accepts for all arguments together (and far more than one argument may hold), so it can reach kubectl only through a file.
        entry_bytes = 4096
        count = min(2000, os.sysconf("SC_ARG_MAX") * 2 // entry_bytes + 1)
        stored = {
            f"org/repo|wf|job-{n}": {"repo": "org/repo", "workflow": "wf", "job": f"job-{n}-" + "x" * entry_bytes, "peak_mib": 1, "samples": 1, "last_seen": 4102444800}
            for n in range(count)
        }
        result = self.poll({}, top={"ns-a": {"runner-1": 600}}, runners={"runner-1": JOB_A}, job_memory=json.dumps(stored))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("could not record", result.stderr)
        kept = self.recorded()
        self.assertEqual(len(kept), count + 1)
        self.assertIn("org/repo|.github/workflows/ci.yml@refs/heads/main|Build", kept)


if __name__ == "__main__":
    unittest.main()
