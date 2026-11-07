"""Shared by the tests of the role controller (test_fleet_role.py) and its probe (test_fleet_role_probe.py): a stand-in for kubectl that keeps nodes and ConfigMaps in a JSON file, and builders for the nodes, probe samples and probe entries the controller reads. The scripts run with the real bash, jq, awk and curl; only the cluster's API is a stand-in."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
REGRADE_CORE = ROOT / "roles" / "github_runner_cluster" / "files" / "regrade" / "fleet-regrade-core.sh"
NODE_LINK = ROOT / "roles" / "github_runner_cluster" / "files" / "node-link.sh"

NAMESPACE = "github-runner-platform"
PROBE_CONFIGMAP = "fleet-role-probe"
STATUS_CONFIGMAP = "fleet-role-status"
ACTUATOR_CONFIGMAP = "fleet-role-actuators"
# The swap's settings, distinct so that a test that names one is told apart from another by its figure; the controller derives every wait from them.
OPERATION_TIMEOUT_SECONDS = 300
RUNNER_WAIT_SECONDS = 600
LEASE_SECONDS = 900
ELIGIBLE_LABEL = "github-runner.exadev.com/voter-eligible"
PINNED_LABEL = "github-runner.exadev.com/voter-pinned"
LINK_LABEL = "github-runner.exadev.com/link"
LEVEL_LABEL = "github-runner.exadev.com/regrade-level"
VOTER_LABEL = "node-role.kubernetes.io/etcd"
# The Node condition k3s sets True on a voting etcd member and not on a learner (setEtcdStatusCondition in pkg/etcd/etcd.go).
VOTER_CONDITION = "EtcdIsVoter"

# The settings the tests give the controller; the thresholds below derive from these the way the controller derives them, so a test names the rule it holds the program to and not a bare figure.
ELECTION_TIMEOUT_MS = 1000
SAMPLE_SECONDS = 15
TARGET = 3
MAX_SERVERS = 5
PRESSURE_THRESHOLD = 10
AVAILABLE_FLOOR_PERCENT = 15
TAIL_PERCENTILE = 99
WINDOW_SAMPLES = 100 // (100 - TAIL_PERCENTILE)
WINDOW_SECONDS = SAMPLE_SECONDS * WINDOW_SAMPLES
UNFIT_RTT_MS = ELECTION_TIMEOUT_MS / 10
FIT_RTT_MS = ELECTION_TIMEOUT_MS / 20
LEARNER_STALL_SECONDS = 300
COOLDOWN_SECONDS = LEARNER_STALL_SECONDS + WINDOW_SECONDS
WAL_LIMIT_MS = 10
COMMIT_LIMIT_MS = 25
STALE_AFTER_SAMPLES = 3
ETCD_QUOTA_BYTES = 2 * 1024**3
# Plenty of memory on every host unless a test says otherwise: well above the quota, and a share of the total far above the regrade floor.
TOTAL_KB = 16 * 1024 * 1024
AVAILABLE_KB = 8 * 1024 * 1024

# Answers the kubectl calls the probe and the controller make from a JSON file (FAKE_STORE) holding {"nodes": [...], "configmaps": {"namespace/name": {"data": {...}}}, "leases": {"namespace/name": {...}}, "snapshots": [...], "runner_pods": {"node": count}}, keeps what a write changes (a merge patch of a ConfigMap, the labels, annotations, taints and cordon of a Node, a Lease created or replaced) in the file, and records every call in FAKE_CALLS. A ConfigMap, Node or Lease the store does not hold is NotFound, as the cluster answers; FAKE_DRAIN_FAILS (a node name, or 1 for every node) makes a drain fail as one that cannot evict does, and FAKE_FAIL_CALLS (a comma-separated list of the first two words of a call) makes those calls fail.
FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    raw = sys.argv[1:]
    args = []
    skip = False
    for i, a in enumerate(raw):
        if skip:
            skip = False
        elif a == "--kubeconfig":
            skip = True
        elif a.startswith("--request-timeout"):
            pass
        else:
            args.append(a)
    with open(os.environ["FAKE_CALLS"], "a") as log:
        log.write(json.dumps(args) + "\\n")
    store = json.load(open(os.environ["FAKE_STORE"]))
    store.setdefault("leases", {})
    store.setdefault("snapshots", [])
    store.setdefault("runner_pods", {})
    def save():
        json.dump(store, open(os.environ["FAKE_STORE"], "w"))
    def flag(*names):
        for name in names:
            if name in args:
                return args[args.index(name) + 1]
        return None
    def not_found(kind, name):
        sys.stderr.write('Error from server (NotFound): %s "%s" not found\\n' % (kind, name))
        sys.exit(1)
    def positional():
        out, skip = [], False
        for a in args:
            if skip:
                skip = False
            elif a in ("-n", "--namespace", "-o", "--output", "-l", "--selector", "--field-selector", "--patch-file", "--type", "--timeout", "-f"):
                skip = True
            elif not a.startswith("-"):
                out.append(a)
        return out
    def node_named(name):
        match = [n for n in store["nodes"] if n["metadata"]["name"] == name]
        if not match:
            not_found("nodes", name)
        return match[0]
    def fail_if_asked():
        asked = os.environ.get("FAKE_FAIL_CALLS", "").split(",")
        if " ".join(args[:2]) in asked:
            sys.stderr.write("Error from server: the call was made to fail\\n")
            sys.exit(1)
    fail_if_asked()
    verb = args[0]
    pos = positional()
    if args[:2] == ["get", "nodes"]:
        selector = flag("-l", "--selector")
        items = store["nodes"]
        if selector is not None:
            key, _, value = selector.partition("=")
            items = [n for n in items if n["metadata"].get("labels", {}).get(key) == value]
        print(json.dumps({"items": items}))
        sys.exit(0)
    if args[:2] == ["get", "node"]:
        print(json.dumps(node_named(args[2])))
        sys.exit(0)
    if args[:2] == ["get", "configmap"]:
        key = flag("-n", "--namespace") + "/" + args[2]
        if key not in store["configmaps"]:
            not_found("configmaps", args[2])
        print(json.dumps({"metadata": {"name": args[2]}, "data": store["configmaps"][key]["data"]}))
        sys.exit(0)
    if args[:2] == ["get", "lease"]:
        key = flag("-n", "--namespace") + "/" + args[2]
        if key not in store["leases"]:
            not_found("leases.coordination.k8s.io", args[2])
        lease = store["leases"][key]
        output = flag("-o", "--output")
        if output and output.startswith("jsonpath="):
            print(lease.get("spec", {}).get("renewTime", ""), end="")
        else:
            print(json.dumps(lease))
        sys.exit(0)
    if args[:2] == ["get", "etcdsnapshotfiles.k3s.cattle.io"]:
        if os.environ.get("FAKE_NO_SNAPSHOTS"):
            sys.stderr.write("error: the server doesn't have a resource type etcdsnapshotfiles\\n")
            sys.exit(1)
        print(json.dumps({"items": store["snapshots"]}))
        sys.exit(0)
    if args[:2] == ["get", "pods"]:
        node = [p for p in flag("--field-selector").split(",") if p.startswith("spec.nodeName=")][0].split("=", 1)[1]
        print(json.dumps({"items": [{} for _ in range(store["runner_pods"].get(node, 0))]}))
        sys.exit(0)
    if args[:2] == ["patch", "configmap"] and os.environ.get("FAKE_FAIL_PATCHES"):
        # Refuses the next patches as an API server on a poor path does: the count is kept in the file the variable names.
        remaining = int(open(os.environ["FAKE_FAIL_PATCHES"]).read() or "0")
        if remaining > 0:
            open(os.environ["FAKE_FAIL_PATCHES"], "w").write(str(remaining - 1))
            sys.stderr.write("Unable to connect to the server: net/http: TLS handshake timeout\\n")
            sys.exit(1)
    if args[:2] == ["patch", "configmap"]:
        key = flag("-n", "--namespace") + "/" + args[2]
        patch = json.load(open(flag("--patch-file")))
        data = store["configmaps"][key]["data"]
        for name, value in patch["data"].items():
            if value is None:
                data.pop(name, None)
            else:
                data[name] = value
        save()
        sys.exit(0)
    if verb in ("create", "replace") and args[1:3] == ["-f", "-"]:
        lease = json.load(sys.stdin)
        key = lease["metadata"]["namespace"] + "/" + lease["metadata"]["name"]
        current = store["leases"].get(key)
        if verb == "create":
            if current is not None:
                sys.stderr.write('Error from server (AlreadyExists): leases "%s" already exists\\n' % lease["metadata"]["name"])
                sys.exit(1)
            lease["metadata"]["resourceVersion"] = "1"
        else:
            if current is None or lease["metadata"].get("resourceVersion") != current["metadata"]["resourceVersion"]:
                sys.stderr.write('Error from server (Conflict): the object has been modified\\n')
                sys.exit(1)
            lease["metadata"]["resourceVersion"] = str(int(current["metadata"]["resourceVersion"]) + 1)
        store["leases"][key] = lease
        save()
        sys.exit(0)
    if verb in ("annotate", "label"):
        node = node_named(pos[2])
        section = "annotations" if verb == "annotate" else "labels"
        overwrite = "--overwrite" in args
        target = node["metadata"].setdefault(section, {})
        for item in pos[3:]:
            if item.endswith("-") and "=" not in item:
                target.pop(item[:-1], None)
            else:
                key, _, value = item.partition("=")
                if key in target and not overwrite:
                    sys.stderr.write("error: '%s' already has a value (%s), and --overwrite is false\\n" % (key, target[key]))
                    sys.exit(1)
                target[key] = value
        save()
        sys.exit(0)
    if verb in ("cordon", "uncordon"):
        node_named(pos[1])["spec"] = {**node_named(pos[1]).get("spec", {}), "unschedulable": verb == "cordon"}
        save()
        sys.exit(0)
    if verb == "taint":
        node = node_named(pos[2])
        spec = node.setdefault("spec", {})
        taints = spec.setdefault("taints", [])
        item = pos[3]
        if item.endswith("-"):
            key = item[:-1].split(":")[0].split("=")[0]
            spec["taints"] = [t for t in taints if t["key"] != key]
        else:
            key, _, rest = item.partition("=")
            value, _, effect = rest.partition(":")
            spec["taints"] = [t for t in taints if t["key"] != key] + [{"key": key, "value": value, "effect": effect}]
        save()
        sys.exit(0)
    if verb == "drain":
        failing = os.environ.get("FAKE_DRAIN_FAILS", "")
        if failing in ("1", pos[1]):
            sys.stderr.write("error: unable to drain node: cannot evict pod\\n")
            sys.exit(1)
        node = node_named(pos[1])
        node["spec"] = {**node.get("spec", {}), "unschedulable": True}
        save()
        sys.exit(0)
    sys.stderr.write("unexpected kubectl call: %s\\n" % args)
    sys.exit(2)
    """
)


# One argument to a process is limited to 128 KiB on Linux (MAX_ARG_STRLEN, one page short of that for the terminating NUL), whatever the total; macOS allows a mebibyte, so a script that passes a large document as an argument passes here and fails on a Linux runner. This stands in for jq on the PATH and refuses such an argument as the kernel's exec does, before running the real jq.
ARGUMENT_LIMIT_BYTES = 128 * 1024 - 1
JQ_SHIM = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    export LC_ALL=C
    for argument in "$@"; do
      if [ "${#argument}" -gt %d ]; then
        echo "jq: Argument list too long" >&2
        exit 126
      fi
    done
    exec %s "$@"
    """
)


class Cluster:
    """A scratch directory holding the stand-in kubectl, its store and its call log, with the helpers that run a script against it."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        kubectl = self.bin / "kubectl"
        kubectl.write_text(FAKE_KUBECTL)
        kubectl.chmod(0o755)
        jq = self.bin / "jq"
        jq.write_text(JQ_SHIM % (ARGUMENT_LIMIT_BYTES, shutil.which("jq")))
        jq.chmod(0o755)
        self.store_path = self.dir / "store.json"
        self.calls = self.dir / "calls.log"
        self.calls.touch()
        self.nodes: list[dict[str, Any]] = []
        self.configmaps: dict[str, dict[str, Any]] = {
            f"{NAMESPACE}/{PROBE_CONFIGMAP}": {"data": {}},
            f"{NAMESPACE}/{STATUS_CONFIGMAP}": {"data": {"status.json": ""}},
            f"{NAMESPACE}/{ACTUATOR_CONFIGMAP}": {"data": {}},
        }
        self.leases: dict[str, Any] = {}
        self.snapshots: list[dict[str, Any]] = []
        self.runner_pods: dict[str, int] = {}

    def cleanup(self) -> None:
        self.tmp.cleanup()

    def write_store(self) -> None:
        self.store_path.write_text(json.dumps({"nodes": self.nodes, "configmaps": self.configmaps, "leases": self.leases, "snapshots": self.snapshots, "runner_pods": self.runner_pods}))

    def read_store(self) -> dict[str, Any]:
        return json.loads(self.store_path.read_text())

    def probes(self) -> dict[str, Any]:
        data = self.configmaps[f"{NAMESPACE}/{PROBE_CONFIGMAP}"]["data"]
        return {name: json.loads(entry) for name, entry in data.items()}

    def set_probes(self, entries: dict[str, Any]) -> None:
        self.configmaps[f"{NAMESPACE}/{PROBE_CONFIGMAP}"]["data"] = {name: json.dumps(entry) for name, entry in entries.items()}

    def set_status(self, status: dict[str, Any] | None) -> None:
        self.configmaps[f"{NAMESPACE}/{STATUS_CONFIGMAP}"]["data"] = {"status.json": "" if status is None else json.dumps(status)}

    def status(self) -> dict[str, Any]:
        return json.loads(self.read_store()["configmaps"][f"{NAMESPACE}/{STATUS_CONFIGMAP}"]["data"]["status.json"])

    def env(self, **extra: str) -> dict[str, str]:
        env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "FAKE_STORE": str(self.store_path), "FAKE_CALLS": str(self.calls)}
        env.update(extra)
        return env

    def run(self, script: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        self.write_store()
        completed = subprocess.run([str(SCRIPTS / script), *args], env=self.env(**env), capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
        if self.store_path.exists():
            store = self.read_store()
            self.nodes = store["nodes"]
            self.configmaps = store["configmaps"]
            self.leases = store["leases"]
        return completed


CONTROLLER_ENV = {
    "FLEET_ROLE_VOTERS": str(TARGET),
    "FLEET_ROLE_MAX_SERVERS": str(MAX_SERVERS),
    "FLEET_ROLE_ELECTION_TIMEOUT_MS": str(ELECTION_TIMEOUT_MS),
    "FLEET_ROLE_SAMPLE_SECONDS": str(SAMPLE_SECONDS),
    "FLEET_ROLE_PRESSURE_THRESHOLD": str(PRESSURE_THRESHOLD),
    "FLEET_ROLE_AVAILABLE_FLOOR_PERCENT": str(AVAILABLE_FLOOR_PERCENT),
    "FLEET_ROLE_ELIGIBLE_LABEL": ELIGIBLE_LABEL,
    "FLEET_ROLE_PINNED_LABEL": PINNED_LABEL,
    "FLEET_ROLE_LEVEL_LABEL": LEVEL_LABEL,
    "FLEET_ROLE_STATE_NAMESPACE": NAMESPACE,
    "FLEET_ROLE_OPERATION_TIMEOUT_SECONDS": str(OPERATION_TIMEOUT_SECONDS),
    "FLEET_ROLE_RUNNER_WAIT_SECONDS": str(RUNNER_WAIT_SECONDS),
    "FLEET_ROLE_LEASE_SECONDS": str(LEASE_SECONDS),
    "FLEET_ROLE_ACTUATOR_CONFIGMAP": ACTUATOR_CONFIGMAP,
    "FLEET_REGRADE_CORE": str(REGRADE_CORE),
}


def controller_config(*, expected_voters: int = TARGET) -> dict[str, Any]:
    """The settings object scripts/fleet-role.sh gives the decision program (scripts/fleet-role.jq), for a test that runs the program alone."""
    return {
        "now": int(time.time()),
        "target": TARGET,
        "expected_voters": expected_voters,
        "max_servers": MAX_SERVERS,
        "election_timeout_ms": ELECTION_TIMEOUT_MS,
        "sample_seconds": SAMPLE_SECONDS,
        "pressure_threshold": PRESSURE_THRESHOLD,
        "available_floor_percent": AVAILABLE_FLOOR_PERCENT,
        "default_quota_bytes": ETCD_QUOTA_BYTES,
        "voter_label": VOTER_LABEL,
        "eligible_label": ELIGIBLE_LABEL,
        "pinned_label": PINNED_LABEL,
        "level_label": LEVEL_LABEL,
    }


def transition_time(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def node(
    name: str,
    *,
    voter: bool = False,
    learner: bool = False,
    eligible: bool = False,
    pinned: bool = False,
    cordoned: bool = False,
    ready: bool = True,
    ready_for: float = 24 * 3600,
    level: str | None = None,
    address: str | None = None,
    link: str | None = None,
) -> dict[str, Any]:
    """A Node as `kubectl get nodes -o json` lists it; a voter carries the etcd label and the EtcdIsVoter condition, a learner the label alone (k3s labels a member when it joins and sets the condition when it votes), ready_for is how long ago its Ready condition last changed, and link the medium the cluster role labelled it with, none when it has no such label."""
    labels: dict[str, str] = {}
    if voter or learner:
        labels[VOTER_LABEL] = "true"
    if eligible:
        labels[ELIGIBLE_LABEL] = "true"
    if pinned:
        labels[PINNED_LABEL] = "true"
    if level is not None:
        labels[LEVEL_LABEL] = level
    if link is not None:
        labels[LINK_LABEL] = link
    conditions = [{"type": "Ready", "status": "True" if ready else "Unknown", "lastTransitionTime": transition_time(ready_for)}]
    if voter:
        conditions.append({"type": VOTER_CONDITION, "status": "True", "lastTransitionTime": transition_time(ready_for)})
    return {
        "metadata": {"name": name, "labels": labels},
        "spec": {"unschedulable": True} if cordoned else {},
        "status": {
            "conditions": conditions,
            "addresses": [{"type": "InternalIP", "address": address or "127.0.0.1"}, {"type": "Hostname", "address": name}],
        },
    }


def histogram(milliseconds: float, count: int = 100) -> dict[str, int]:
    """Observations that all fell in the one bucket of etcd's WAL and commit histograms (bounds double from a millisecond) holding the given latency; the value is the bucket's upper bound as Prometheus writes it."""
    bound = 0.001
    while bound * 1000 < milliseconds:
        bound *= 2
    return {repr(round(bound, 6)): count}


def etcd_gain(
    *,
    wal_ms: float = 4,
    commit_ms: float = 8,
    rtt: dict[str, dict[str, int]] | None = None,
    has_leader: bool = True,
    is_learner: bool = False,
    known_members: int = TARGET,
    active_peers: int = TARGET - 1,
    quota_bytes: int = ETCD_QUOTA_BYTES,
    leader_changes: int = 0,
) -> dict[str, Any]:
    """The etcd part of a sample: what the histograms gained since the last sample, and the gauges."""
    return {
        "has_leader": has_leader,
        "is_learner": is_learner,
        "known_members": known_members,
        "active_peers": active_peers,
        "quota_bytes": quota_bytes,
        "leader_changes": leader_changes,
        "wal": histogram(wal_ms, 1),
        "commit": histogram(commit_ms, 1),
        "rtt": rtt or {},
    }


def sample(
    at: float,
    *,
    tcp: dict[str, float] | None = None,
    etcd: dict[str, Any] | None = None,
    restarted: bool = False,
    psi: float | None = 0.0,
    avail_kb: int | None = AVAILABLE_KB,
    total_kb: int | None = TOTAL_KB,
    link: str = "wired",
    link_source: str = "interface",
    fsync_ms: float | None = None,
) -> dict[str, Any]:
    return {
        "t": int(at),
        "tcp": tcp or {},
        "etcd": etcd,
        "restarted": restarted,
        "psi": psi,
        "avail_kb": avail_kb,
        "total_kb": total_kb,
        "link": link,
        "link_source": link_source,
        "iface": "eth0",
        "fsync_ms": fsync_ms,
    }


def entry(samples: list[dict[str, Any]], etcd_id: str | None = None) -> dict[str, Any]:
    return {"etcd_id": etcd_id, "last": None, "fsync_at": None, "samples": samples}


def times(count: int, now: float | None = None) -> list[float]:
    """The times of the last count samples, one sample period apart, the newest now."""
    end = time.time() if now is None else now
    return [end - (count - 1 - i) * SAMPLE_SECONDS for i in range(count)]
