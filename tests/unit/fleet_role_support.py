"""Shared by the tests of the role controller (test_fleet_role.py) and its probe (test_fleet_role_probe.py): a stand-in for kubectl that keeps nodes and ConfigMaps in a JSON file, and builders for the nodes, probe samples and probe entries the controller reads. The scripts run with the real bash, jq, awk and curl; only the cluster's API is a stand-in."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
REGRADE_CORE = ROOT / "roles" / "github_runner_cluster" / "files" / "regrade" / "fleet-regrade-core.sh"

NAMESPACE = "github-runner-platform"
PROBE_CONFIGMAP = "fleet-role-probe"
STATUS_CONFIGMAP = "fleet-role-status"
ELIGIBLE_LABEL = "github-runner.exadev.com/voter-eligible"
PINNED_LABEL = "github-runner.exadev.com/voter-pinned"
LEVEL_LABEL = "github-runner.exadev.com/regrade-level"
VOTER_LABEL = "node-role.kubernetes.io/etcd"

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

# Answers the kubectl calls the probe and the controller make from a JSON file (FAKE_STORE) holding {"nodes": [...], "configmaps": {"namespace/name": {"data": {...}}}}, merges a merge patch's data into a ConfigMap, and records every call in FAKE_CALLS. A ConfigMap the store does not hold is NotFound, as the cluster answers.
FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_CALLS"], "a") as log:
        log.write(json.dumps(args) + "\\n")
    store = json.load(open(os.environ["FAKE_STORE"]))
    def flag(name):
        return args[args.index(name) + 1] if name in args else None
    if args[:2] == ["get", "nodes"]:
        selector = flag("-l")
        items = store["nodes"]
        if selector is not None:
            key, _, value = selector.partition("=")
            items = [n for n in items if n["metadata"].get("labels", {}).get(key) == value]
        print(json.dumps({"items": items}))
        sys.exit(0)
    if args[:2] == ["get", "node"]:
        match = [n for n in store["nodes"] if n["metadata"]["name"] == args[2]]
        if not match:
            sys.stderr.write('Error from server (NotFound): nodes "%s" not found\\n' % args[2])
            sys.exit(1)
        print(json.dumps(match[0]))
        sys.exit(0)
    if args[:2] == ["get", "configmap"]:
        key = flag("-n") + "/" + args[2]
        if key not in store["configmaps"]:
            sys.stderr.write('Error from server (NotFound): configmaps "%s" not found\\n' % args[2])
            sys.exit(1)
        print(json.dumps({"metadata": {"name": args[2]}, "data": store["configmaps"][key]["data"]}))
        sys.exit(0)
    if args[:2] == ["patch", "configmap"]:
        key = flag("-n") + "/" + args[2]
        patch = json.load(open(flag("--patch-file")))
        data = store["configmaps"][key]["data"]
        for name, value in patch["data"].items():
            if value is None:
                data.pop(name, None)
            else:
                data[name] = value
        json.dump(store, open(os.environ["FAKE_STORE"], "w"))
        sys.exit(0)
    sys.stderr.write("unexpected kubectl call: %s\\n" % args)
    sys.exit(2)
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
        self.store_path = self.dir / "store.json"
        self.calls = self.dir / "calls.log"
        self.calls.touch()
        self.nodes: list[dict[str, Any]] = []
        self.configmaps: dict[str, dict[str, Any]] = {
            f"{NAMESPACE}/{PROBE_CONFIGMAP}": {"data": {}},
            f"{NAMESPACE}/{STATUS_CONFIGMAP}": {"data": {"status.json": ""}},
        }

    def cleanup(self) -> None:
        self.tmp.cleanup()

    def write_store(self) -> None:
        self.store_path.write_text(json.dumps({"nodes": self.nodes, "configmaps": self.configmaps}))

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

    def run(self, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        self.write_store()
        completed = subprocess.run([str(SCRIPTS / script)], env=self.env(**env), capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
        if self.store_path.exists():
            store = self.read_store()
            self.nodes = store["nodes"]
            self.configmaps = store["configmaps"]
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
}


def transition_time(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def node(
    name: str,
    *,
    voter: bool = False,
    eligible: bool = False,
    pinned: bool = False,
    ready: bool = True,
    ready_for: float = 24 * 3600,
    level: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    """A Node as `kubectl get nodes -o json` lists it; ready_for is how long ago its Ready condition last changed."""
    labels: dict[str, str] = {}
    if voter:
        labels[VOTER_LABEL] = "true"
    if eligible:
        labels[ELIGIBLE_LABEL] = "true"
    if pinned:
        labels[PINNED_LABEL] = "true"
    if level is not None:
        labels[LEVEL_LABEL] = level
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "Unknown", "lastTransitionTime": transition_time(ready_for)}],
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
        "iface": "eth0",
        "fsync_ms": fsync_ms,
    }


def entry(samples: list[dict[str, Any]], etcd_id: str | None = None) -> dict[str, Any]:
    return {"etcd_id": etcd_id, "last": None, "fsync_at": None, "samples": samples}


def times(count: int, now: float | None = None) -> list[float]:
    """The times of the last count samples, one sample period apart, the newest now."""
    end = time.time() if now is None else now
    return [end - (count - 1 - i) * SAMPLE_SECONDS for i in range(count)]
