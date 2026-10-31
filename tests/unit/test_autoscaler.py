"""Tests for scripts/autoscaler.sh, one poll of the usage-driven autoscaler, run in dry-run mode against a scripted stand-in for kubectl with the real bash and jq."""

from __future__ import annotations

import importlib.util
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "autoscaler.sh"
CEILING_PROGRAM = SCRIPT.parent / "autoscaler-ceiling.jq"
PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "filter" / "arc.py"
_spec = importlib.util.spec_from_file_location("arc_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
arc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arc)
NAMESPACES = ("ns-a", "ns-b")
RUNNING = 2
MAX_RUNNERS = 3

# Answers the reads autoscaler.sh makes from FAKE_STATE (a JSON file): per-namespace pods, whether `kubectl top pod` fails, the memory limit, each scale set's runner pod template, the nodes and every pod in the cluster; every patch is accepted.
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
        elif args[-2:] == ["-o", "json"]:
            namespace = args[args.index("-n") + 1]
            sys.stdout.write(json.dumps({"spec": {"template": {"spec": state["templates"].get(namespace, state["template"])}}}))
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
    if args[:3] == ["get", "pods", "--all-namespaces"]:
        if state["cluster_pods_fail"]:
            sys.exit(1)
        # The field selector the autoscaler passes, applied as the API server would.
        assert "--field-selector=status.phase!=Succeeded,status.phase!=Failed" in args, args
        sys.stdout.write(json.dumps({"items": [pod for pod in state["cluster_pods"] if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")]}))
        sys.exit(0)
    if args[:2] == ["get", "pods"]:
        namespace = args[args.index("-n") + 1]
        sys.stdout.write(json.dumps({"items": state["pods"].get(namespace, [])}))
        sys.exit(0)
    if args[:2] == ["top", "pod"]:
        # As the real kubectl does: a pod placed on a node with no metrics yet fails the whole namespace.
        if state["top_pod_fails"]:
            sys.exit(1)
        namespace = args[args.index("-n") + 1]
        measured = state["top"].get(namespace, {})
        for pod in state["pods"].get(namespace, []):
            name = pod.get("metadata", {}).get("name")
            if pod.get("spec", {}).get("nodeName") and name not in measured:
                sys.stderr.write("error: Metrics not available for pod %s/%s, age: 5s\\n" % (namespace, name))
                sys.exit(1)
        for name, mib in measured.items():
            sys.stdout.write("%s 10m %dMi\\n" % (name, mib))
        sys.exit(0)
    if args[:2] == ["get", "--raw"] and args[2].startswith("/apis/metrics.k8s.io/v1beta1/namespaces/"):
        # The metrics API's own list: only the pods metrics-server has sampled, and an error only when the API itself is down.
        if state["top_pod_fails"]:
            sys.stderr.write("Error from server (ServiceUnavailable): the server is currently unable to handle the request\\n")
            sys.exit(1)
        namespace = args[2].split("/")[5]
        items = [{"metadata": {"name": name, "namespace": namespace}, "containers": [{"name": "runner", "usage": {"cpu": "10m", "memory": "%dMi" % mib}}]} for name, mib in state["top"].get(namespace, {}).items()]
        sys.stdout.write(json.dumps({"kind": "PodMetricsList", "items": items}))
        sys.exit(0)
    if args[:2] == ["get", "ephemeralrunner"]:
        runner = state["runners"].get(args[2])
        if runner is None:
            sys.exit(1)
        sys.stdout.write(json.dumps({"status": runner}))
        sys.exit(0)
    if args[:2] == ["get", "nodes"] and args[-2:] == ["-o", "json"]:
        if state["nodes_fail"]:
            sys.exit(1)
        items = []
        for node in state["nodes"]:
            items.append({
                "metadata": {"name": node["name"], "labels": dict(label.split("=", 1) for label in node["labels"])},
                "spec": {"unschedulable": node.get("unschedulable", False), "taints": node.get("taints", [])},
                "status": {"allocatable": node.get("allocatable", state["allocatable"]), "conditions": [{"type": "Ready", "status": node.get("ready", "True")}]},
            })
        sys.stdout.write(json.dumps({"items": items}))
        sys.exit(0)
    if args[:2] == ["get", "nodes"]:
        # `-o name`, optionally narrowed by the same key=value selector the autoscaler passes to `top nodes`.
        selector = args[args.index("-l") + 1] if "-l" in args else None
        for node in state["nodes"]:
            if selector is None or selector in node["labels"]:
                sys.stdout.write("node/%s\\n" % node["name"])
        sys.exit(0)
    if args[:3] == ["get", "configmap", state["pressure_configmap"]] and "json" in args:
        sys.stdout.write(json.dumps({"data": state["pressure"]}))
        sys.exit(0)
    if args[:2] == ["top", "nodes"]:
        # -l takes one key=value selector, as the autoscaler passes it; a node is listed when it carries that label. Like the real kubectl, a node without metrics is printed as <unknown> beside ones with them, and when no listed node has metrics at all (none matches, or every match is down) it prints nothing and fails.
        selector = args[args.index("-l") + 1] if "-l" in args else None
        listed = [node for node in state["nodes"] if selector is None or selector in node["labels"]]
        if not state["top_nodes_lists_unknown"] and not any(node["mem"] != "<unknown>" for node in listed):
            sys.stderr.write("error: metrics not available yet\\n")
            sys.exit(1)
        for node in listed:
            sys.stdout.write("%s 100m 5%% 1000Mi %s\\n" % (node["name"], node["mem"]))
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
        # Like kubectl's jsonpath, a key that is not there prints nothing.
        sys.stdout.write(stored.get(key, fixtures.get(key, "")))
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
# A runner pod spec as the fleet's values give one: requests equal to limits.
RUNNER_TEMPLATE = {"containers": [{"name": "runner", "resources": {"requests": {"cpu": "2", "memory": "7Gi"}, "limits": {"cpu": "2", "memory": "7Gi"}}}]}
# Large enough that the ceiling worked out from it is above every bound the tests that are not about the ceiling set, so those tests see the bound as the ceiling.
ROOMY_NODE = {"cpu": "64", "memory": "512Gi", "pods": "110"}
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
        ceiling: int | None = 8,
        namespaces: tuple[str, ...] = NAMESPACES,
        max_runners: dict[str, int] | None = None,
        ephemeral: dict[str, list[str]] | None = None,
        ephemeral_fails: bool = False,
        node_mem_pct: int = 20,
        nodes: list[dict[str, object]] | None = None,
        node_selector: str | None = None,
        configmap: dict[str, str] | None = None,
        top: dict[str, dict[str, int]] | None = None,
        runners: dict[str, dict[str, str]] | None = None,
        job_memory: str = "",
        job_memory_patch_fails: bool = False,
        events: dict[str, list[str]] | None = None,
        job_peak: str = "",
        pressure: dict[str, str] | None = None,
        extra_env: dict[str, str] | None = None,
        template: dict[str, object] | None = None,
        templates: dict[str, dict[str, object]] | None = None,
        cluster_pods: list[dict[str, object]] | None = None,
        nodes_fail: bool = False,
        cluster_pods_fail: bool = False,
        top_nodes_lists_unknown: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        state = self.dir / "state.json"
        state.write_text(
            json.dumps(
                {
                    "default_max_runners": MAX_RUNNERS,
                    "max_runners": {f"release-{ns}": count for ns, count in (max_runners or {}).items()},
                    "ephemeral": ephemeral if ephemeral is not None else {ns: ["Running"] * (running or 0) for ns in namespaces},
                    "ephemeral_fails": ephemeral_fails,
                    "nodes": nodes if nodes is not None else [{"name": "node", "labels": [], "mem": f"{node_mem_pct}%"}],
                    "configmap": configmap or {},
                    "pods": pods,
                    "top_pod_fails": top_pod_fails,
                    "top": top or {},
                    "runners": runners or {},
                    "job_memory": job_memory,
                    "job_peak": job_peak,
                    "events": events or {},
                    "job_memory_patch_fails": job_memory_patch_fails,
                    "pressure_configmap": "node-pressure",
                    "pressure": pressure or {},
                    "template": template or RUNNER_TEMPLATE,
                    "templates": templates or {},
                    "allocatable": ROOMY_NODE,
                    "cluster_pods": cluster_pods or [],
                    "nodes_fail": nodes_fail,
                    "cluster_pods_fail": cluster_pods_fail,
                    "top_nodes_lists_unknown": top_nodes_lists_unknown,
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
            "AUTOSCALER_MAX_CEILING": "" if ceiling is None else str(ceiling),
            "AUTOSCALER_FLOOR": "3",
        }
        if node_selector is not None:
            env["AUTOSCALER_NODE_SELECTOR"] = node_selector
        if pressure is not None:
            env["AUTOSCALER_NODE_PRESSURE_CONFIGMAP"] = "node-pressure"
        env.update(extra_env or {})
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

    def test_a_scale_set_with_no_ephemeral_runners_counts_as_zero_runners_and_keeps_one_slot(self) -> None:
        result = self.poll({"ns-a": [UNSCHEDULABLE_POD]}, running=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("R_total=0", result.stdout)
        for namespace in NAMESPACES:
            self.assertIn(f"would patch {namespace}/release-{namespace}'s maxRunners to 1", result.stdout)

    def test_a_failing_memory_measurement_fails_safe_for_every_target(self) -> None:
        result = self.poll({}, top_pod_fails=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("unbound variable", result.stderr)
        self.assertIn("FAIL-SAFE", result.stderr)
        for namespace in NAMESPACES:
            self.assertIn(f"would patch {namespace}/release-{namespace}'s maxRunners to {RUNNING}", result.stdout)

    # Two nodes runner pods may use and a third, outside the eligibility label, whose kubelet is unreachable so metrics-server reports no figures for it.
    ELIGIBLE = "example.com/eligible=true"
    UNREACHABLE_INELIGIBLE = (
        {"name": "a", "labels": [ELIGIBLE], "mem": "20%"},
        {"name": "b", "labels": [ELIGIBLE], "mem": "30%"},
        {"name": "c", "labels": [], "mem": "<unknown>"},
    )

    def test_an_unreachable_node_outside_the_selector_does_not_fail_safe(self) -> None:
        result = self.poll({"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}, ceiling=MAX_RUNNERS * len(NAMESPACES), nodes=list(self.UNREACHABLE_INELIGIBLE), node_selector=self.ELIGIBLE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("FAIL-SAFE", result.stderr)
        self.assertIn("mem_available=70%", result.stdout)
        self.assertNotIn("would patch", result.stdout)

    def test_an_unreachable_node_is_left_out_without_a_selector(self) -> None:
        result = self.poll({"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}, ceiling=MAX_RUNNERS * len(NAMESPACES), nodes=list(self.UNREACHABLE_INELIGIBLE))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("FAIL-SAFE", result.stderr)
        self.assertIn("Skipping node c: kubectl top nodes has no memory reading for it", result.stdout)
        self.assertNotIn("Skipping node a", result.stdout)
        self.assertNotIn("Skipping node b", result.stdout)
        self.assertIn("mem_available=70% pressure=false", result.stdout)
        self.assertNotIn("would patch", result.stdout)

    def test_pressure_on_a_node_outside_the_selector_is_not_pressure(self) -> None:
        nodes = [{"name": "a", "labels": [self.ELIGIBLE], "mem": "20%"}, {"name": "c", "labels": [], "mem": "95%"}]
        result = self.poll({"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}, ceiling=MAX_RUNNERS * len(NAMESPACES), nodes=nodes, node_selector=self.ELIGIBLE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mem_available=80% pressure=false", result.stdout)
        self.assertNotIn("would patch", result.stdout)

    def test_a_selector_matching_no_node_fails_safe_with_kubectls_error(self) -> None:
        result = self.poll({"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}, ceiling=MAX_RUNNERS * len(NAMESPACES), nodes=[{"name": "c", "labels": [], "mem": "20%"}], node_selector=self.ELIGIBLE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"FAIL-SAFE: kubectl top nodes -l {self.ELIGIBLE} failed (error: metrics not available yet)", result.stderr)
        for namespace in NAMESPACES:
            self.assertIn(f"would patch {namespace}/release-{namespace}'s maxRunners to {RUNNING}", result.stdout)

    def test_an_unreachable_node_inside_the_selector_is_left_out_and_logged_by_name(self) -> None:
        nodes = [{"name": "a", "labels": [self.ELIGIBLE], "mem": "20%"}, {"name": "b", "labels": [self.ELIGIBLE], "mem": "<unknown>"}, {"name": "c", "labels": [], "mem": "10%"}]
        result = self.poll({"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}, ceiling=MAX_RUNNERS * len(NAMESPACES), nodes=nodes, node_selector=self.ELIGIBLE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("FAIL-SAFE", result.stderr)
        self.assertEqual(result.stdout.count("Skipping node"), 1)
        self.assertIn("Skipping node b: kubectl top nodes has no memory reading for it", result.stdout)
        self.assertIn("mem_available=80% pressure=false", result.stdout)
        self.assertNotIn("would patch", result.stdout)

    def test_every_node_unreachable_fails_safe_with_its_own_message(self) -> None:
        # kubectl itself fails when no listed node has metrics (see the fake), so this answer, which lists only <unknown> rows, is the guard against reading it as no pressure.
        nodes = [{"name": "a", "labels": [], "mem": "<unknown>"}, {"name": "b", "labels": [], "mem": "<unknown>"}]
        result = self.poll({"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}, ceiling=MAX_RUNNERS * len(NAMESPACES), nodes=nodes, top_nodes_lists_unknown=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("Skipping node"), 2)
        self.assertIn("FAIL-SAFE: no node reported a MEMORY% from kubectl top nodes (every row was <unknown>)", result.stderr)
        for namespace in NAMESPACES:
            self.assertIn(f"would patch {namespace}/release-{namespace}'s maxRunners to {RUNNING}", result.stdout)

    def test_a_percentage_that_is_not_a_number_still_fails_safe(self) -> None:
        nodes = [{"name": "a", "labels": [], "mem": "20%"}, {"name": "b", "labels": [], "mem": "garbage%"}]
        result = self.poll({"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}, ceiling=MAX_RUNNERS * len(NAMESPACES), nodes=nodes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FAIL-SAFE: could not parse a node's own MEMORY% from kubectl top nodes ('garbage')", result.stderr)
        for namespace in NAMESPACES:
            self.assertIn(f"would patch {namespace}/release-{namespace}'s maxRunners to {RUNNING}", result.stdout)

    def test_the_worst_reporting_node_still_gives_pressure_beside_an_unreachable_one(self) -> None:
        nodes = [{"name": "a", "labels": [], "mem": "<unknown>"}, {"name": "b", "labels": [], "mem": "95%"}, {"name": "c", "labels": [], "mem": "50%"}]
        result = self.poll({"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}, ceiling=MAX_RUNNERS * len(NAMESPACES), nodes=nodes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("FAIL-SAFE", result.stderr)
        self.assertIn("Skipping node a", result.stdout)
        self.assertIn("mem_available=5% pressure=true", result.stdout)
        self.assertIn("lower: host pressure", result.stdout)

    def recorded(self) -> dict[str, dict[str, object]]:
        return json.loads(self.job_memory_out.read_text())

    # Two nodes runner pods may use, each with a pressure report from its reporter, written as the reporter writes it.
    NODES = ({"name": "a", "labels": [ELIGIBLE], "mem": "20%"}, {"name": "b", "labels": [ELIGIBLE], "mem": "30%"})

    @staticmethod
    def report(io_full: float, age_seconds: int = 0, cpu_some: float = 1.0) -> str:
        return json.dumps({"at": int(time.time()) - age_seconds, "io_some_avg10": io_full + 1, "io_full_avg10": io_full, "cpu_some_avg10": cpu_some})

    def pressure_poll(self, pressure: dict[str, str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        pods = {"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}
        return self.poll(pods, ceiling=12, nodes=list(self.NODES), node_selector=self.ELIGIBLE, pressure=pressure, **kwargs)  # type: ignore[arg-type]

    def test_io_pressure_on_one_node_lowers_the_pool_by_one_and_names_the_node(self) -> None:
        result = self.pressure_poll({"a": self.report(19.4), "b": self.report(0.1)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("node_pressure=true", result.stdout)
        self.assertIn("lower: node pressure (a io stall 19.4% >= 10%)", result.stdout)
        # Six pooled runners, two of them running on each target: one fewer, and not the floor.
        self.assertIn("would patch ns-a/release-ns-a's maxRunners to 2", result.stdout)
        self.assertNotIn("FAIL-SAFE", result.stderr)

    def test_a_node_pressure_step_never_goes_below_the_running_count(self) -> None:
        result = self.pressure_poll({"a": self.report(40.0), "b": self.report(0.0)}, max_runners={"ns-a": RUNNING, "ns-b": RUNNING})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("would patch", result.stdout)

    def test_healthy_nodes_with_fresh_reports_change_nothing(self) -> None:
        result = self.pressure_poll({"a": self.report(0.2), "b": self.report(0.0)}, max_runners={"ns-a": 12, "ns-b": 0})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("node_pressure=false unmeasured_nodes=0", result.stdout)
        self.assertNotIn("node pressure", result.stdout)

    def test_a_node_with_no_report_or_a_stale_one_holds_the_pool_and_does_not_lower_it(self) -> None:
        for pressure in ({"a": self.report(0.0)}, {"a": self.report(0.0), "b": self.report(0.0, age_seconds=1000)}):
            result = self.pressure_poll(pressure)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("unmeasured_nodes=1", result.stdout)
            self.assertIn("node_pressure=false", result.stdout)
            self.assertNotIn("would patch", result.stdout)

    def test_a_stale_report_of_high_pressure_is_not_pressure(self) -> None:
        result = self.pressure_poll({"a": self.report(0.0), "b": self.report(60.0, age_seconds=1000)})
        self.assertIn("node_pressure=false unmeasured_nodes=1", result.stdout)

    def test_the_cpu_signal_is_off_unless_a_threshold_is_given(self) -> None:
        busy = {"a": self.report(0.0, cpu_some=90.0), "b": self.report(0.0)}
        self.assertIn("node_pressure=false", self.pressure_poll(busy).stdout)
        asked = self.pressure_poll(busy, extra_env={"AUTOSCALER_CPU_PRESSURE_PCT": "80"})
        self.assertIn("lower: node pressure (a cpu stall 90.0% >= 80%)", asked.stdout)

    def test_the_io_threshold_can_be_moved(self) -> None:
        reports = {"a": self.report(19.4), "b": self.report(0.0)}
        self.assertNotIn("node_pressure=true", self.pressure_poll(reports, extra_env={"AUTOSCALER_IO_PRESSURE_PCT": "25"}).stdout)

    def test_without_the_pressure_configmap_nothing_about_node_pressure_is_read(self) -> None:
        result = self.poll({"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}, ceiling=12, nodes=list(self.NODES), node_selector=self.ELIGIBLE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("node_pressure=false unmeasured_nodes=0", result.stdout)

    def test_a_node_outside_the_selector_with_pressure_is_not_pressure(self) -> None:
        nodes = [*self.NODES, {"name": "x", "labels": [], "mem": "20%"}]
        pods = {"ns-a": [SCHEDULED_POD], "ns-b": [SCHEDULED_POD]}
        result = self.poll(pods, ceiling=12, nodes=nodes, node_selector=self.ELIGIBLE, pressure={"a": self.report(0.0), "b": self.report(0.0), "x": self.report(80.0)}, max_runners={"ns-a": 12, "ns-b": 0})
        self.assertIn("node_pressure=false unmeasured_nodes=0", result.stdout)

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

    @staticmethod
    def runner_pod(name: str, node: str | None = "node", phase: str = "Running", limit: str | None = "7Gi") -> dict[str, object]:
        """A runner pod as the pod list gives it: placed on node (None for not yet placed), with a memory limit unless limit is None."""
        resources = {"limits": {"memory": limit}} if limit is not None else {}
        spec: dict[str, object] = {"containers": [{"name": "runner", "resources": resources}]}
        if node is not None:
            spec["nodeName"] = node
        return {"metadata": {"name": name}, "spec": spec, "status": {"phase": phase, "conditions": [{"type": "PodScheduled", "status": "True"}]}}

    def test_a_runner_pod_with_no_metrics_yet_neither_fails_safe_nor_lowers_the_pool(self) -> None:
        pods = {"ns-a": [self.runner_pod("old"), self.runner_pod("new")], "ns-b": [self.runner_pod("other")]}
        result = self.poll(pods, top={"ns-a": {"old": 900}, "ns-b": {"other": 600}}, max_runners={"ns-a": 3, "ns-b": 3}, ceiling=6)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("FAIL-SAFE", result.stderr)
        self.assertIn("usage=1500MiB", result.stdout)
        self.assertIn("unmeasured_pods=1 assumed=7168MiB", result.stdout)
        self.assertNotIn("would patch", result.stdout)

    def test_an_unmeasured_pod_is_counted_at_its_limit_before_the_pool_is_raised(self) -> None:
        # Four new 7Gi pods leave 5Gi of the 33Gi budget at their limits, less than a pod's room; one leaves 26Gi.
        four = {"ns-a": [self.runner_pod(f"new-{n}") for n in range(4)]}
        held = self.poll(four, max_runners={"ns-a": 2, "ns-b": 2}, configmap={"raise-confirm-count": "1"})
        self.assertEqual(held.returncode, 0, held.stderr)
        self.assertIn("unmeasured_pods=4 assumed=28672MiB", held.stdout)
        self.assertNotIn("would patch", held.stdout)
        self.assertIn("have no metrics yet", json.loads(self.stored("status.json") or "{}")["mode"])
        one = {"ns-a": [self.runner_pod("new")]}
        # The held poll reset the confirmation count, so this one starts it again on the way to a raise.
        self.poll(one, max_runners={"ns-a": 2, "ns-b": 2})
        self.assertEqual(json.loads(self.stored("status.json") or "{}")["mode"], "watching: headroom confirmed for 1/2 polls")

    def test_an_unmeasured_pod_with_no_limit_is_counted_at_its_scale_sets_limit(self) -> None:
        result = self.poll({"ns-a": [self.runner_pod("new", limit=None)]})
        self.assertIn("unmeasured_pods=1 assumed=7168MiB", result.stdout)

    def test_a_pod_not_yet_placed_or_already_finished_is_not_unmeasured(self) -> None:
        pods = {"ns-a": [self.runner_pod("waiting", node=None, phase="Pending"), self.runner_pod("done", phase="Succeeded"), self.runner_pod("broke", phase="Failed")]}
        result = self.poll(pods)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unmeasured_pods=0 assumed=0MiB", result.stdout)

    def test_an_unmeasured_pods_limit_in_another_unit_is_converted(self) -> None:
        result = self.poll({"ns-a": [self.runner_pod("new", limit="7516192768")]})
        self.assertIn("unmeasured_pods=1 assumed=7168MiB", result.stdout)

    def test_the_metrics_api_failing_still_fails_safe_and_says_why(self) -> None:
        result = self.poll({}, top_pod_fails=True)
        self.assertIn("FAIL-SAFE: the metrics API failed for ns-a (Error from server (ServiceUnavailable)", result.stderr)

    def test_lowering_never_takes_a_scale_set_with_nothing_running_to_zero(self) -> None:
        # The fail-safe lowers towards a floor of 3: the set running three keeps them, and the idle one keeps one slot instead of none.
        result = self.poll({}, top_pod_fails=True, ephemeral={"ns-a": ["Running"] * 3, "ns-b": []})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patched_maximums(result), {"ns-b": 1})
        for reason in ({"nodes": [{"name": "node", "labels": [], "mem": "95%"}]}, {"pods": {"ns-a": [UNSCHEDULABLE_POD]}}):
            with self.subTest(reason):
                lowered = self.poll(reason.get("pods", {}), nodes=reason.get("nodes"), ephemeral={"ns-a": ["Running"] * 3, "ns-b": []})  # type: ignore[arg-type]
                self.assertEqual(self.patched_maximums(lowered), {"ns-b": 1})

    def test_a_scale_set_left_at_zero_is_given_a_slot_back_by_rebalancing(self) -> None:
        result = self.poll(self.FULL_POOL, ceiling=6, max_runners={"ns-a": 0, "ns-b": 6}, ephemeral={"ns-a": [], "ns-b": ["Running"] * 2}, configmap={"rebalance-confirm-count": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patched_maximums(result), {"ns-a": 1, "ns-b": 5})

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


    # Two nodes runner pods may use, as a fleet's might be: a large one where cpu is the limit at the template's request (four by cpu, five by memory), and a small one where a system pod's requests leave memory for one. Five runners in all.
    ELIGIBLE_SELECTOR = {"example.com/eligible": "true"}
    LARGE = {"name": "a", "labels": [ELIGIBLE, "kubernetes.io/hostname=a"], "mem": "20%", "allocatable": {"cpu": "8", "memory": "40Gi", "pods": "110"}}
    SMALL = {"name": "b", "labels": [ELIGIBLE, "kubernetes.io/hostname=b"], "mem": "30%", "allocatable": {"cpu": "6", "memory": "8129140Ki", "pods": "110"}}
    SYSTEM_POD_ON_SMALL = {"metadata": {"name": "dns", "namespace": "kube-system"}, "spec": {"nodeName": "b", "containers": [{"name": "dns", "resources": {"requests": {"cpu": "310m", "memory": "202Mi"}}}]}, "status": {"phase": "Running"}}
    # The pooled runner pods already running on the large node: if they were counted they would leave it room for none.
    POOLED_RUNNERS_ON_LARGE = [
        {"metadata": {"name": f"runner-{n}", "namespace": "ns-a"}, "spec": {"nodeName": "a", "containers": RUNNER_TEMPLATE["containers"]}, "status": {"phase": "Running"}} for n in range(4)
    ]
    ELIGIBLE_TEMPLATE = {**RUNNER_TEMPLATE, "nodeSelector": ELIGIBLE_SELECTOR}

    def ceiling_poll(self, nodes: list[dict[str, object]] | None = None, cluster_pods: list[dict[str, object]] | None = None, **kwargs: object) -> subprocess.CompletedProcess[str]:
        """One poll with no bound on the ceiling, so the nodes alone decide it; the pool is three slots on each target with two running, unless kwargs say otherwise."""
        options: dict[str, object] = {"ceiling": None, "node_selector": self.ELIGIBLE, "template": self.ELIGIBLE_TEMPLATE, **kwargs}
        return self.poll(  # type: ignore[arg-type]
            options.pop("pods", self.FULL_POOL),
            nodes=nodes if nodes is not None else [self.LARGE, self.SMALL],
            cluster_pods=cluster_pods if cluster_pods is not None else [self.SYSTEM_POD_ON_SMALL, *self.POOLED_RUNNERS_ON_LARGE],
            **options,
        )

    def pooled_total_after(self, result: subprocess.CompletedProcess[str], before: dict[str, int] | None = None) -> int:
        """The pooled maxRunners total once the poll's patches apply, from what each target held before it."""
        after = {ns: MAX_RUNNERS for ns in NAMESPACES} | (before or {})
        after.update(self.patched_maximums(result))
        return sum(after.values())

    def test_the_ceiling_is_worked_out_from_the_nodes_and_a_pool_above_it_is_lowered_to_it(self) -> None:
        result = self.ceiling_poll()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ceiling=5 measured_ceiling=true ceiling_bound=none", result.stdout)
        self.assertIn("above the ceiling 5", result.stdout)
        self.assertEqual(self.pooled_total_after(result), 5)
        self.assertEqual(self.stored("ceiling"), "5")

    def test_the_pool_is_raised_up_to_the_ceiling_and_no_further(self) -> None:
        below = {"ns-a": 2, "ns-b": 2}
        raised = self.ceiling_poll(max_runners=below, configmap={"raise-confirm-count": "1"})
        self.assertEqual(raised.returncode, 0, raised.stderr)
        self.assertIn("raise:", raised.stdout)
        self.assertEqual(self.pooled_total_after(raised, below), 5)
        at = {"ns-a": 3, "ns-b": 2}
        held = self.ceiling_poll(max_runners=at, configmap={"raise-confirm-count": "1"})
        self.assertEqual(held.returncode, 0, held.stderr)
        self.assertNotIn("would patch", held.stdout)

    def test_a_bound_below_the_worked_out_ceiling_caps_it(self) -> None:
        result = self.ceiling_poll(ceiling=4, max_runners={"ns-a": 2, "ns-b": 2}, configmap={"raise-confirm-count": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ceiling=4 measured_ceiling=true ceiling_bound=4", result.stdout)
        self.assertNotIn("would patch", result.stdout)

    def test_a_bound_above_the_worked_out_ceiling_changes_nothing(self) -> None:
        self.assertIn("ceiling=5 measured_ceiling=true ceiling_bound=40", self.ceiling_poll(ceiling=40).stdout)

    def test_a_node_runner_pods_cannot_be_placed_on_now_is_not_counted(self) -> None:
        cases = {
            "not ready": {"ready": "False"},
            "not known to be ready": {"ready": "Unknown"},
            "cordoned": {"unschedulable": True},
            "tainted": {"taints": [{"key": "example.com/dedicated", "value": "builds", "effect": "NoSchedule"}]},
            "evicting": {"taints": [{"key": "example.com/dedicated", "effect": "NoExecute"}]},
            "outside the selector": {"labels": []},
        }
        for case, change in cases.items():
            with self.subTest(case):
                result = self.ceiling_poll(nodes=[self.LARGE, {**self.SMALL, **change}])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("ceiling=4 measured_ceiling=true", result.stdout)
                self.assertEqual(self.pooled_total_after(result), 4)

    def test_a_taint_the_runner_pods_tolerate_or_one_that_only_prefers_leaves_the_node_counted(self) -> None:
        taint = {"key": "example.com/dedicated", "value": "builds", "effect": "NoSchedule"}
        tolerant = {**self.ELIGIBLE_TEMPLATE, "tolerations": [{"key": "example.com/dedicated", "operator": "Equal", "value": "builds", "effect": "NoSchedule"}]}
        self.assertIn("ceiling=5 ", self.ceiling_poll(nodes=[self.LARGE, {**self.SMALL, "taints": [taint]}], template=tolerant).stdout)
        preferring = {**taint, "effect": "PreferNoSchedule"}
        self.assertIn("ceiling=5 ", self.ceiling_poll(nodes=[self.LARGE, {**self.SMALL, "taints": [preferring]}]).stdout)

    def test_every_pooled_template_must_be_placeable_on_a_counted_node(self) -> None:
        pinned = {**self.ELIGIBLE_TEMPLATE, "nodeSelector": {**self.ELIGIBLE_SELECTOR, "kubernetes.io/hostname": "a"}}
        self.assertIn("ceiling=4 ", self.ceiling_poll(templates={"ns-b": pinned}).stdout)

    def test_the_largest_pooled_request_is_the_runner_pod_counted(self) -> None:
        larger = {**self.ELIGIBLE_TEMPLATE, "containers": [{"name": "runner", "resources": {"requests": {"cpu": "4", "memory": "7Gi"}}}]}
        # Two on the large node by cpu, and the small one's memory still holds one.
        self.assertIn("ceiling=3 ", self.ceiling_poll(templates={"ns-b": larger}).stdout)

    def test_a_template_that_sets_only_limits_is_counted_at_those_limits(self) -> None:
        limits_only = {**self.ELIGIBLE_TEMPLATE, "containers": [{"name": "runner", "resources": {"limits": {"cpu": "2", "memory": "7Gi"}}}]}
        self.assertIn("ceiling=5 ", self.ceiling_poll(template=limits_only).stdout)

    def test_a_sidecar_in_the_template_is_counted_beside_the_runner(self) -> None:
        with_sidecar = {**self.ELIGIBLE_TEMPLATE, "initContainers": [{"name": "dind", "restartPolicy": "Always", "resources": {"requests": {"cpu": "1", "memory": "1Gi"}}}]}
        # Three cpu and 8Gi a pod: two fit on the large node by cpu, and none on the small one by memory.
        self.assertIn("ceiling=2 ", self.ceiling_poll(template=with_sidecar).stdout)

    def test_a_pod_of_a_scale_set_outside_the_pool_lowers_the_ceiling_while_the_pooled_ones_do_not(self) -> None:
        builder = {"metadata": {"name": "builder-runner", "namespace": "builder"}, "spec": {"nodeName": "a", "containers": [{"name": "runner", "resources": {"requests": {"cpu": "1", "memory": "2Gi"}}}]}, "status": {"phase": "Running"}}
        # The large node then has seven cpu for the pool, three runners' worth.
        result = self.ceiling_poll(cluster_pods=[self.SYSTEM_POD_ON_SMALL, *self.POOLED_RUNNERS_ON_LARGE, builder])
        self.assertIn("ceiling=4 ", result.stdout)

    def test_a_pod_not_yet_placed_takes_nothing_from_any_node(self) -> None:
        pending = {"metadata": {"name": "waiting", "namespace": "default"}, "spec": {"containers": [{"name": "big", "resources": {"requests": {"cpu": "6"}}}]}, "status": {"phase": "Pending"}}
        self.assertIn("ceiling=5 ", self.ceiling_poll(cluster_pods=[self.SYSTEM_POD_ON_SMALL, pending]).stdout)

    def test_a_nodes_pod_capacity_bounds_what_it_places(self) -> None:
        crowded = {**self.SMALL, "allocatable": {**self.SMALL["allocatable"], "pods": "1"}}  # type: ignore[dict-item]
        # The one pod slot is the system pod's.
        self.assertIn("ceiling=4 ", self.ceiling_poll(nodes=[self.LARGE, crowded]).stdout)

    def test_the_status_records_the_ceiling_and_what_each_node_places(self) -> None:
        outside = {"name": "c", "labels": [], "mem": "10%"}
        self.ceiling_poll(nodes=[self.LARGE, self.SMALL, outside])
        status = json.loads(self.stored("status.json") or "{}")
        self.assertEqual(status["ceiling"], 5)
        self.assertIsNone(status["ceiling_bound"])
        self.assertTrue(status["ceiling_measured"])
        self.assertEqual(status["ceiling_nodes"], [{"name": "a", "runners": 4}, {"name": "b", "runners": 1}, {"name": "c", "not_counted": "does not carry the runner pods' nodeSelector"}])

    def test_a_failed_read_keeps_the_last_ceiling_for_lowering_and_never_raises(self) -> None:
        for failure in ({"nodes_fail": True}, {"cluster_pods_fail": True}):
            with self.subTest(failure):
                lowered = self.ceiling_poll(configmap={"ceiling": "5"}, **failure)
                self.assertEqual(lowered.returncode, 0, lowered.stderr)
                self.assertIn("ERROR: could not work out the ceiling from the nodes; keeping the last one (5) and not raising", lowered.stderr)
                self.assertIn("ceiling=5 measured_ceiling=false", lowered.stdout)
                self.assertEqual(self.pooled_total_after(lowered), 5)
                below = {"ns-a": 2, "ns-b": 2}
                held = self.ceiling_poll(configmap={"ceiling": "5", "raise-confirm-count": "1"}, max_runners=below, **failure)
                self.assertEqual(held.returncode, 0, held.stderr)
                self.assertNotIn("would patch", held.stdout)
                self.assertIn("the ceiling could not be worked out", held.stdout + json.loads(self.stored("status.json") or "{}")["mode"])
                self.assertEqual(self.stored("raise-confirm-count"), "0")

    def test_with_no_ceiling_ever_worked_out_and_no_bound_a_failed_read_neither_raises_nor_lowers(self) -> None:
        result = self.ceiling_poll(nodes_fail=True, max_runners={"ns-a": 2, "ns-b": 2}, configmap={"raise-confirm-count": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ERROR: could not work out the ceiling from the nodes; not raising", result.stderr)
        self.assertIn("ceiling=none measured_ceiling=false", result.stdout)
        self.assertNotIn("would patch", result.stdout)

    def test_with_a_bound_and_no_ceiling_ever_worked_out_a_failed_read_lowers_to_the_bound(self) -> None:
        result = self.ceiling_poll(nodes_fail=True, ceiling=4)
        self.assertIn("ceiling=4 measured_ceiling=false", result.stdout)
        self.assertEqual(self.pooled_total_after(result), 4)

    def test_a_figure_that_is_not_a_quantity_fails_the_ceiling_loudly(self) -> None:
        broken = {**self.SMALL, "allocatable": {**self.SMALL["allocatable"], "memory": "lots"}}  # type: ignore[dict-item]
        result = self.ceiling_poll(nodes=[self.LARGE, broken], max_runners={"ns-a": 2, "ns-b": 2}, configmap={"raise-confirm-count": "1"})
        self.assertIn('not a Kubernetes quantity: "lots"', result.stderr)
        self.assertIn("ERROR: could not work out the ceiling", result.stderr)
        self.assertNotIn("would patch", result.stdout)

    def test_a_node_that_reports_no_pod_capacity_fails_the_ceiling_loudly(self) -> None:
        unreported = {**self.SMALL, "allocatable": {"cpu": "6", "memory": "8Gi"}}
        result = self.ceiling_poll(nodes=[self.LARGE, unreported])
        self.assertIn("node b reports no allocatable pods", result.stderr)
        self.assertIn("measured_ceiling=false", result.stdout)

    def test_a_runner_pod_with_no_request_fails_the_ceiling_loudly(self) -> None:
        result = self.ceiling_poll(template={"containers": [{"name": "runner"}]})
        self.assertIn("the runner pod requests no cpu or no memory", result.stderr)
        self.assertIn("measured_ceiling=false", result.stdout)

    def test_memory_pressure_still_lowers_the_pool_at_once_below_the_ceiling(self) -> None:
        result = self.ceiling_poll(nodes=[{**self.LARGE, "mem": "95%"}, self.SMALL])
        self.assertIn("lower: host pressure", result.stdout)
        self.assertEqual(self.pooled_total_after(result), RUNNING * len(NAMESPACES))

    def test_a_bound_that_is_not_a_whole_number_stops_the_poll(self) -> None:
        result = self.poll({}, extra_env={"AUTOSCALER_MAX_CEILING": "four"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AUTOSCALER_MAX_CEILING must be empty or a whole number", result.stderr)



class CeilingProgramTest(unittest.TestCase):
    """The autoscaler's ceiling program against the role's install-time measurement (plugins/filter/arc.py's arc_measured_max_runners), which works out the same figure in Python's exact decimals, on clusters drawn at random. The clusters hold only what both count alike: Ready, uncordoned nodes, taints other than the ones Kubernetes adds for a passing condition, no safety margin or reserve, and pods that have not finished."""

    # Enough clusters that every quantity form, the sidecar and init container rules, taints and tolerations, the pooled namespace and the pod capacity each decide some node's figure; each case is one jq run.
    CASES = 300
    SEED = 20261020
    CPU = ("100m", "250m", "0.5", "1", "1500m", "2", "3")
    MEMORY = ("64Mi", "256Mi", "500M", "1Gi", "1073741824", "2Gi", "3.5Gi")
    NODE_CPU = ("2", "4", "6", "7800m", "8", "16")
    NODE_MEMORY = ("8129140Ki", "16Gi", "32G", "40991212Ki", "64Gi")
    NODE_PODS = ("3", "8", "110")
    TAINT = {"key": "example.com/dedicated", "value": "builds", "effect": "NoSchedule"}
    TOLERATION = {"key": "example.com/dedicated", "operator": "Equal", "value": "builds", "effect": "NoSchedule"}
    POOLED = "pooled"

    @classmethod
    def container(cls, rng: random.Random, name: str, restart_policy: str | None = None) -> dict[str, object]:
        container: dict[str, object] = {"name": name, "resources": {"requests": {"cpu": rng.choice(cls.CPU), "memory": rng.choice(cls.MEMORY)}}}
        if restart_policy is not None:
            container["restartPolicy"] = restart_policy
        return container

    @classmethod
    def spec(cls, rng: random.Random) -> dict[str, object]:
        spec: dict[str, object] = {"containers": [cls.container(rng, f"app-{n}") for n in range(rng.randint(1, 2))]}
        if rng.random() < 0.4:
            spec["initContainers"] = [cls.container(rng, f"init-{n}", rng.choice((None, "Always"))) for n in range(rng.randint(1, 3))]
        if rng.random() < 0.2:
            spec["overhead"] = {"cpu": rng.choice(cls.CPU), "memory": rng.choice(cls.MEMORY)}
        return spec

    @classmethod
    def cluster(cls, rng: random.Random) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
        template = cls.spec(rng)
        template["nodeSelector"] = {"example.com/eligible": "true"}
        if rng.random() < 0.5:
            template["tolerations"] = [cls.TOLERATION]
        nodes = []
        for n in range(rng.randint(1, 4)):
            nodes.append({
                "metadata": {"name": f"node-{n}", "labels": {"example.com/eligible": "true"} if rng.random() < 0.85 else {}},
                "spec": {"taints": [cls.TAINT]} if rng.random() < 0.3 else {},
                "status": {
                    "allocatable": {"cpu": rng.choice(cls.NODE_CPU), "memory": rng.choice(cls.NODE_MEMORY), "pods": rng.choice(cls.NODE_PODS)},
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            })
        pods = []
        for n in range(rng.randint(0, 8)):
            pod_spec = cls.spec(rng)
            if rng.random() < 0.9:
                pod_spec["nodeName"] = rng.choice(nodes)["metadata"]["name"]  # type: ignore[index]
            pods.append({"metadata": {"name": f"pod-{n}", "namespace": rng.choice((cls.POOLED, "kube-system", "other"))}, "spec": pod_spec, "status": {"phase": "Running"}})
        return template, nodes, pods

    @classmethod
    def program(cls, template: dict[str, object], nodes: list[dict[str, object]], pods: list[dict[str, object]]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as work:
            for name, value in (("templates", template), ("nodes", {"items": nodes}), ("pods", {"items": pods})):
                Path(work, name).write_text(json.dumps(value))
            out = subprocess.run(
                ["jq", "-c", "--slurpfile", "templates", f"{work}/templates", "--slurpfile", "nodes", f"{work}/nodes", "--argjson", "pooled", json.dumps([cls.POOLED]), "-L", str(CEILING_PROGRAM.parent), "-f", str(CEILING_PROGRAM), f"{work}/pods"],
                capture_output=True, text=True, check=True,
            )
        result: dict[str, object] = json.loads(out.stdout)
        return result

    def reference(self, template: dict[str, object], nodes: list[dict[str, object]], pods: list[dict[str, object]]) -> int:
        cpu, memory = arc._pod_requests({"spec": template})
        sizing = {"measured": True, "pod_cpu_request": str(cpu), "pod_memory_request_gib": str(memory / arc._GIB)}
        measured = arc.arc_measured_max_runners(sizing, nodes, pods, template["nodeSelector"], [self.POOLED], template.get("tolerations", []))
        if measured["errors"] == ["no node is eligible for runner pods with the labels example.com/eligible=true"]:
            return 0
        self.assertEqual(measured["errors"], [])
        count: int = measured["max_runners"]
        return count

    @classmethod
    def setUpClass(cls) -> None:
        rng = random.Random(cls.SEED)
        cls.clusters = [cls.cluster(rng) for _ in range(cls.CASES)]
        cls.results = [cls.program(*cluster) for cluster in cls.clusters]

    def test_the_program_places_as_many_runners_as_the_install_time_measurement(self) -> None:
        for case, (cluster, result) in enumerate(zip(self.clusters, self.results)):
            with self.subTest(case=case):
                self.assertEqual(result["ceiling"], self.reference(*cluster))

    def test_the_drawn_clusters_reach_every_bound(self) -> None:
        """The comparison above means something only if the drawn clusters make cpu, memory and the pod capacity each the limit on some node, leave some node out and give some node room for none."""
        seen: set[str] = set()
        for result in self.results:
            for node in result["nodes"]:  # type: ignore[attr-defined]
                bounds = {"cpu": node["by_cpu"], "memory": node["by_memory"], "pods": node["by_pods"]}
                seen.update(name for name, value in bounds.items() if value == node["runners"] and sum(v == node["runners"] for v in bounds.values()) == 1)
                if node["runners"] == 0:
                    seen.add("none")
            if result["skipped"]:
                seen.add("skipped")
        self.assertEqual(seen, {"cpu", "memory", "pods", "none", "skipped"})

if __name__ == "__main__":
    unittest.main()
