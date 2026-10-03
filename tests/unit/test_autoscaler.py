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
        sys.stdout.write("runner 10m 500Mi\\n")
        sys.exit(0)
    if args[:2] == ["top", "nodes"]:
        sys.stdout.write("node 100m 5% 1000Mi 20%\\n")
        sys.exit(0)
    if args[:2] == ["get", "configmap"]:
        sys.stdout.write("0")
        sys.exit(0)
    if args[:1] == ["patch"]:
        sys.exit(0)
    sys.stderr.write("unexpected kubectl call: %s\\n" % args)
    sys.exit(2)
    """
)

UNSCHEDULABLE_POD = {"status": {"conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}]}}
SCHEDULED_POD = {"status": {"conditions": [{"type": "PodScheduled", "status": "True"}]}}


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
    ) -> subprocess.CompletedProcess[str]:
        state = self.dir / "state.json"
        state.write_text(json.dumps({"running": running, "max_runners": MAX_RUNNERS, "pods": pods, "top_pod_fails": top_pod_fails}))
        env = {
            **os.environ,
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_STATE": str(state),
            "AUTOSCALER_TARGETS": " ".join(f"{ns}/release-{ns}" for ns in NAMESPACES),
            "AUTOSCALER_DRY_RUN": "true",
            "AUTOSCALER_USABLE_BUDGET_GI": "33",
            "AUTOSCALER_MAX_CEILING": "8",
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


if __name__ == "__main__":
    unittest.main()
