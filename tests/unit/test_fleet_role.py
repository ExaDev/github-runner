"""Tests for scripts/fleet-role.sh and scripts/fleet-role.jq, one pass of the role controller, run against a scripted stand-in for kubectl with the real bash and jq. The probes' entries are built the way scripts/fleet-role-probe.sh writes them (tests/unit/test_fleet_role_probe.py holds the probe to a real etcd's metrics); here every threshold and decision rule is held to its boundary."""

from __future__ import annotations

import json
import os
import subprocess
import time
import unittest
from dataclasses import dataclass, field
from typing import Any

from fleet_role_support import (
    AVAILABLE_FLOOR_PERCENT,
    ARGUMENT_LIMIT_BYTES,
    AVAILABLE_KB,
    COMMIT_LIMIT_MS,
    CONTROLLER_ENV,
    COOLDOWN_SECONDS,
    ELECTION_TIMEOUT_MS,
    ETCD_QUOTA_BYTES,
    FIT_RTT_MS,
    MAX_SERVERS,
    PRESSURE_THRESHOLD,
    REGRADE_CORE,
    SCRIPTS,
    SAMPLE_SECONDS,
    STALE_AFTER_SAMPLES,
    TARGET,
    TOTAL_KB,
    controller_config,
    UNFIT_RTT_MS,
    WAL_LIMIT_MS,
    WINDOW_SAMPLES,
    WINDOW_SECONDS,
    Cluster,
    entry,
    etcd_gain,
    histogram,
    node,
    sample,
    times,
)

Series = float | list[float]


@dataclass
class Host:
    """What one host's probe has reported over the window, and the Node it is."""

    name: str
    voter: bool = False
    # Carries the etcd label without voting: a member k3s has added and the leader has not yet promoted.
    learner: bool = False
    eligible: bool = False
    pinned: bool = False
    ready: bool = True
    ready_for: float = 24 * 3600
    level: str | None = None
    link: str = "wired"
    link_source: str = "interface"
    # Milliseconds to every voter, or per peer; a list is one reading per sample.
    tcp: Series | dict[str, Series] = 5.0
    # What etcd's own round trip histogram holds per peer, in milliseconds, for a voter; none leaves the histograms out.
    etcd_rtt: float | dict[str, float] | None = None
    # Per-sample gains of a voter's histograms, in milliseconds, or the buckets themselves when given as dicts.
    wal: Series | list[dict[str, int]] = 4.0
    commit: Series | list[dict[str, int]] = 8.0
    fsync_ms: float | None = 3.0
    psi: float | None = 0.0
    avail_kb: int = AVAILABLE_KB
    total_kb: int = TOTAL_KB
    samples: int = WINDOW_SAMPLES
    restarted: bool = False
    stale_by: float = 0
    etcd: dict[str, Any] = field(default_factory=dict)


def series_value(value: Series | dict[str, Series], peer: str, index: int) -> float:
    if isinstance(value, dict):
        value = value[peer]
    return value[index] if isinstance(value, list) else value


def gain(value: Series | list[dict[str, int]], index: int) -> dict[str, int]:
    item = value[index] if isinstance(value, list) else value
    return item if isinstance(item, dict) else histogram(item, 1)


def etcd_id(name: str) -> str:
    return f"{name}-etcd-id"


def populate(cluster: Cluster, hosts: list[Host]) -> None:
    """Gives the cluster these Nodes and, for each host, a probe entry of its last `samples` samples ending now (less stale_by)."""
    voters = [h.name for h in hosts if h.voter]
    cluster.nodes = [
        node(h.name, voter=h.voter, learner=h.learner, eligible=h.eligible, pinned=h.pinned, ready=h.ready, ready_for=h.ready_for, level=h.level) for h in hosts
    ]
    entries = {}
    for h in hosts:
        peers = [v for v in voters if v != h.name]
        stamps = times(h.samples, time.time() - h.stale_by)
        built = []
        for i, at in enumerate(stamps):
            etcd = None
            if h.voter:
                rtt = {}
                if h.etcd_rtt is not None:
                    rtt = {etcd_id(p): histogram(h.etcd_rtt[p] if isinstance(h.etcd_rtt, dict) else h.etcd_rtt, 1) for p in peers}
                etcd = {**etcd_gain(rtt=rtt, known_members=len(voters), active_peers=len(voters) - 1), "wal": gain(h.wal, i), "commit": gain(h.commit, i), **h.etcd}
            built.append(
                sample(
                    at,
                    tcp={p: series_value(h.tcp, p, i) for p in (voters if not h.voter else peers)},
                    etcd=etcd,
                    restarted=h.restarted and i == len(stamps) // 2,
                    psi=h.psi,
                    avail_kb=h.avail_kb,
                    total_kb=h.total_kb,
                    link=h.link,
                    link_source=h.link_source,
                    fsync_ms=None if h.voter else h.fsync_ms if i == 0 else None,
                )
            )
        entries[h.name] = entry(built, etcd_id(h.name) if h.voter else None)
    cluster.set_probes(entries)


def healthy(**changes: Any) -> list[Host]:
    """Three fit voters, a, b and c, and a fit eligible standby, d; every voter and the standby eligible."""
    hosts = [Host("a", voter=True, eligible=True), Host("b", voter=True, eligible=True), Host("c", voter=True, eligible=True), Host("d", eligible=True)]
    for host in hosts:
        for key, value in changes.get(host.name, {}).items():
            setattr(host, key, value)
    return hosts


class ControllerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster = Cluster()
        self.addCleanup(self.cluster.cleanup)

    def pass_(self, hosts: list[Host], **env: str) -> dict[str, Any]:
        """Runs one pass over these hosts and returns the status it published."""
        populate(self.cluster, hosts)
        completed = self.cluster.run("fleet-role.sh", **{**CONTROLLER_ENV, **env})
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.log = completed.stdout
        return self.cluster.status()

    def decide(self, hosts: list[Host], *, expected_voters: int) -> dict[str, Any]:
        """The decision program alone over these hosts, given the number of voters expected; the controller's own pass expects the target, and a swap that holds a new voter expects one more."""
        populate(self.cluster, hosts)
        for name, content in (("nodes", {"items": self.cluster.nodes}), ("probes", self.cluster.probes()), ("previous", None)):
            (self.cluster.dir / f"{name}.json").write_text(json.dumps(content))
        completed = subprocess.run(
            ["jq", "-n", "-L", str(SCRIPTS), "--argjson", "config", json.dumps(controller_config(expected_voters=expected_voters)),
             "--slurpfile", "nodes_file", str(self.cluster.dir / "nodes.json"), "--slurpfile", "probes_file", str(self.cluster.dir / "probes.json"),
             "--slurpfile", "previous_file", str(self.cluster.dir / "previous.json"), "-f", str(SCRIPTS / "fleet-role.jq")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def verdicts(self, status: dict[str, Any]) -> dict[str, str]:
        return {name: host["verdict"] for name, host in status["hosts"].items()}


class ThresholdTest(ControllerCase):
    def test_the_limits_derive_from_the_election_timeout_and_the_window_from_the_tail(self) -> None:
        status = self.pass_(healthy())
        config = status["config"]
        self.assertEqual(config["unfit_rtt_ms"], ELECTION_TIMEOUT_MS / 10)
        self.assertEqual(config["fit_rtt_ms"], ELECTION_TIMEOUT_MS / 20)
        self.assertEqual(config["window_samples"], 100)
        self.assertEqual(config["window_seconds"], WINDOW_SECONDS)
        self.assertEqual(config["cooldown_seconds"], COOLDOWN_SECONDS)
        self.assertEqual((config["wal_fsync_limit_ms"], config["backend_commit_limit_ms"]), (WAL_LIMIT_MS, COMMIT_LIMIT_MS))

    def test_healthy_voters_and_a_healthy_standby_are_all_fit_and_nothing_would_change(self) -> None:
        status = self.pass_(healthy())
        self.assertEqual(self.verdicts(status), {"a": "fit", "b": "fit", "c": "fit", "d": "fit"})
        self.assertEqual(status["decision"]["action"], "none")
        self.assertEqual(status["decision"]["because"], ["no voter is unfit"])
        self.assertEqual(status["blocks"], [])
        self.assertTrue(status["dry_run"])

    def test_a_voter_is_unfit_only_above_a_tenth_of_the_election_timeout(self) -> None:
        for rtt, verdict in ((UNFIT_RTT_MS, "fit"), (UNFIT_RTT_MS + 0.001, "unfit")):
            with self.subTest(rtt=rtt):
                status = self.pass_(healthy(a={"tcp": rtt}))
                self.assertEqual(status["hosts"]["a"]["verdict"], verdict)

    def test_a_candidate_is_fit_only_at_or_below_a_twentieth_of_the_election_timeout(self) -> None:
        for rtt, verdict in ((FIT_RTT_MS, "fit"), (FIT_RTT_MS + 0.001, "unfit")):
            with self.subTest(rtt=rtt):
                status = self.pass_(healthy(d={"tcp": rtt}))
                self.assertEqual(status["hosts"]["d"]["verdict"], verdict)

    def test_between_the_two_limits_a_voter_is_kept_and_a_candidate_is_not_promoted(self) -> None:
        between = (UNFIT_RTT_MS + FIT_RTT_MS) / 2
        status = self.pass_(healthy(a={"tcp": between}, d={"tcp": between}))
        self.assertEqual(self.verdicts(status), {"a": "fit", "b": "fit", "c": "fit", "d": "unfit"})
        self.assertEqual(status["decision"]["action"], "none")

    def test_the_round_trip_figure_is_the_tail_of_the_window_so_one_outlier_never_decides(self) -> None:
        one_outlier = [5.0] * (WINDOW_SAMPLES - 1) + [ELECTION_TIMEOUT_MS]
        two_outliers = [5.0] * (WINDOW_SAMPLES - 2) + [ELECTION_TIMEOUT_MS] * 2
        self.assertEqual(self.pass_(healthy(a={"tcp": one_outlier}))["hosts"]["a"]["verdict"], "fit")
        status = self.pass_(healthy(a={"tcp": two_outliers}))
        self.assertEqual(status["hosts"]["a"]["verdict"], "unfit")
        self.assertEqual(status["hosts"]["a"]["figures"]["rtt_p99_ms"], ELECTION_TIMEOUT_MS)

    def test_a_window_one_sample_short_is_unknown_not_fit(self) -> None:
        status = self.pass_(healthy(a={"samples": WINDOW_SAMPLES - 1}))
        self.assertEqual(status["hosts"]["a"]["verdict"], "unknown")
        self.assertIn(f"{WINDOW_SAMPLES - 1} of {WINDOW_SAMPLES} samples since the last membership change", status["hosts"]["a"]["reasons"])

    def test_a_probe_report_older_than_three_samples_is_no_report(self) -> None:
        for stale_by, verdict in (((STALE_AFTER_SAMPLES * SAMPLE_SECONDS) - 5, "fit"), ((STALE_AFTER_SAMPLES * SAMPLE_SECONDS) + 5, "unknown")):
            with self.subTest(stale_by=stale_by):
                status = self.pass_(healthy(a={"stale_by": stale_by}))
                self.assertEqual(status["hosts"]["a"]["verdict"], verdict)

    def test_a_node_with_no_probe_entry_is_unknown(self) -> None:
        hosts = healthy()
        populate(self.cluster, hosts)
        entries = self.cluster.probes()
        del entries["d"]
        self.cluster.set_probes(entries)
        completed = self.cluster.run("fleet-role.sh", **CONTROLLER_ENV)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.cluster.status()["hosts"]["d"]["reasons"][0], "no probe report")

    def test_etcds_own_round_trip_counts_beside_the_tcp_connect(self) -> None:
        status = self.pass_(healthy(a={"tcp": 5.0, "etcd_rtt": {"b": 5.0, "c": 150.0}}))
        self.assertEqual(status["hosts"]["a"]["verdict"], "unfit")
        self.assertEqual(status["hosts"]["a"]["figures"]["rtt_peer"], "c")
        self.assertTrue(status["hosts"]["a"]["figures"]["rtt_p99_ms"] > UNFIT_RTT_MS)

    def test_the_worst_peer_decides(self) -> None:
        status = self.pass_(healthy(b={"tcp": {"a": 5.0, "c": UNFIT_RTT_MS + 20}}))
        self.assertEqual(status["hosts"]["b"]["verdict"], "unfit")
        self.assertEqual(status["hosts"]["b"]["figures"]["rtt_peer"], "c")


class DiskTest(ControllerCase):
    def gains(self, in_limit: int, past: int, limit_seconds: float, over_seconds: float) -> list[dict[str, int]]:
        """One gain per sample: in_limit of them in a bucket whose upper bound is the limit, past in a bucket above it."""
        return [{str(limit_seconds): 1} for _ in range(in_limit)] + [{str(over_seconds): 1} for _ in range(past)]

    def test_a_voters_wal_fsync_tail_must_be_under_ten_milliseconds(self) -> None:
        for in_limit, past, bound, expected in (
            (99, 1, 0.01, "unfit"),  # the tail interpolates to the bucket's bound, ten milliseconds, which is not under ten
            (99, 1, 0.009, "fit"),
        ):
            with self.subTest(bound=bound):
                status = self.pass_(healthy(a={"wal": self.gains(in_limit, past, bound, 0.02)}))
                self.assertEqual(status["hosts"]["a"]["verdict"], expected)

    def test_a_voters_backend_commit_tail_must_be_under_twenty_five_milliseconds(self) -> None:
        for bound, expected in ((0.025, "unfit"), (0.024, "fit")):
            with self.subTest(bound=bound):
                status = self.pass_(healthy(a={"commit": self.gains(99, 1, bound, 0.05)}))
                self.assertEqual(status["hosts"]["a"]["verdict"], expected)

    def test_a_standbys_fdatasync_tail_must_be_under_the_wal_limit(self) -> None:
        for fsync, expected in ((WAL_LIMIT_MS - 0.01, "fit"), (WAL_LIMIT_MS, "unfit")):
            with self.subTest(fsync=fsync):
                status = self.pass_(healthy(d={"fsync_ms": fsync}))
                self.assertEqual(status["hosts"]["d"]["verdict"], expected)

    def test_a_standby_without_an_fdatasync_reading_is_unknown(self) -> None:
        status = self.pass_(healthy(d={"fsync_ms": None}))
        self.assertEqual(status["hosts"]["d"]["verdict"], "unknown")
        self.assertIn("no fdatasync reading yet", status["hosts"]["d"]["reasons"])

    def test_a_voter_with_no_etcd_disk_observations_is_unknown(self) -> None:
        status = self.pass_(healthy(a={"wal": [{}] * WINDOW_SAMPLES}))
        self.assertEqual(status["hosts"]["a"]["verdict"], "unknown")


class MemoryAndStabilityTest(ControllerCase):
    def test_memory_over_the_regrade_limit_needs_both_pressure_and_low_available(self) -> None:
        total = 1_000_000
        floor = total * AVAILABLE_FLOOR_PERCENT // 100
        self.assertEqual(floor * 100, total * AVAILABLE_FLOOR_PERCENT, "a total whose floor is a whole number of kilobytes")
        cases = (
            ("pressure at the threshold and available just under the floor", PRESSURE_THRESHOLD, floor - 1, "unfit"),
            ("pressure under the threshold", PRESSURE_THRESHOLD - 0.01, floor - 1, "fit"),
            ("available at the floor", PRESSURE_THRESHOLD, floor, "fit"),
        )
        for label, psi, avail, expected in cases:
            with self.subTest(label):
                # A quota of one byte on every voter, so that only the regrade limit can fail the host.
                quota = {"etcd": {"quota_bytes": 1}}
                status = self.pass_(healthy(a={"psi": psi, "avail_kb": avail, "total_kb": total, **quota}, b=quota, c=quota))
                self.assertEqual(status["hosts"]["a"]["verdict"], expected)

    def test_a_host_without_pressure_readings_is_judged_by_available_memory_and_says_its_pressure_is_unknown(self) -> None:
        # A kernel without pressure stall information: every sample has MemAvailable and no pressure.
        status = self.pass_(healthy(a={"psi": None, "avail_kb": TOTAL_KB * AVAILABLE_FLOOR_PERCENT // 100 - 1}))
        host = status["hosts"]["a"]
        self.assertEqual((host["verdict"], host["unknown_signals"], host["figures"]["psi_avg300_max"]), ("fit", ["memory pressure"], None))
        self.assertIn("a (voter, link wired): fit, all readings within their limits; memory pressure unknown", self.log)
        self.assertEqual(status["decision"]["action"], "none")
        status = self.pass_(healthy(a={"psi": None, "avail_kb": ETCD_QUOTA_BYTES // 1024 - 1, "total_kb": TOTAL_KB}))
        self.assertEqual(status["hosts"]["a"]["verdict"], "unfit", "MemAvailable under etcd's quota still fails a host without pressure readings")

    def test_a_host_with_pressure_readings_has_no_unknown_signal_and_reports_its_worst(self) -> None:
        status = self.pass_(healthy(a={"psi": 2.5}))
        self.assertEqual((status["hosts"]["a"]["unknown_signals"], status["hosts"]["a"]["figures"]["psi_avg300_max"]), ([], 2.5))
        self.assertNotIn("unknown", self.log.split("a (voter, link wired)")[1].splitlines()[0])

    def test_available_memory_must_be_at_least_etcds_storage_quota(self) -> None:
        quota_kb = ETCD_QUOTA_BYTES // 1024
        for avail, expected in ((quota_kb, "fit"), (quota_kb - 1, "unfit")):
            with self.subTest(avail=avail):
                status = self.pass_(healthy(a={"avail_kb": avail, "total_kb": avail * 1000}))
                self.assertEqual(status["hosts"]["a"]["verdict"], expected)

    def test_the_quota_is_the_largest_a_voter_reports(self) -> None:
        quota = 4 * 1024**3
        status = self.pass_(healthy(b={"etcd": {"quota_bytes": quota}}, a={"avail_kb": 3 * 1024**2, "total_kb": 3 * 1024**5}))
        self.assertEqual(status["config"]["quota_bytes"], quota)
        self.assertEqual(status["hosts"]["a"]["verdict"], "unfit")

    def test_one_sample_over_the_limit_anywhere_in_the_window_is_enough(self) -> None:
        populate(self.cluster, healthy())
        entries = self.cluster.probes()
        # Under the floor share of the total but above etcd's quota, so only the regrade limit can fail it.
        entries["a"]["samples"][10].update(psi=PRESSURE_THRESHOLD + 5, avail_kb=TOTAL_KB * AVAILABLE_FLOOR_PERCENT // 100 - 1)
        self.cluster.set_probes(entries)
        completed = self.cluster.run("fleet-role.sh", **CONTROLLER_ENV)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        host = self.cluster.status()["hosts"]["a"]
        self.assertEqual(host["verdict"], "unfit")
        self.assertIn("memory was over the regrade limit in 1 of 100 samples", host["reasons"])

    def test_a_node_that_was_not_ready_within_the_window_is_unfit_and_one_that_was_not_is_fit(self) -> None:
        for ready_for, expected in ((WINDOW_SECONDS - 60, "unfit"), (WINDOW_SECONDS + 60, "fit")):
            with self.subTest(ready_for=ready_for):
                status = self.pass_(healthy(d={"ready_for": ready_for}))
                self.assertEqual(status["hosts"]["d"]["verdict"], expected)

    def test_a_node_that_is_not_ready_is_unfit(self) -> None:
        status = self.pass_(healthy(d={"ready": False}))
        self.assertEqual(status["hosts"]["d"]["verdict"], "unfit")
        self.assertIn("the node is not Ready", status["hosts"]["d"]["reasons"])

    def test_a_k3s_restart_in_the_window_is_unfit(self) -> None:
        status = self.pass_(healthy(a={"restarted": True}, d={"restarted": True}))
        self.assertEqual(status["hosts"]["a"]["verdict"], "unfit")
        self.assertEqual(status["hosts"]["d"]["verdict"], "unfit")
        self.assertEqual(status["hosts"]["a"]["figures"]["restarts"], 1)

    def test_leader_changes_are_reported(self) -> None:
        status = self.pass_(healthy(a={"etcd": {"leader_changes": 2}}))
        self.assertEqual(status["hosts"]["a"]["figures"]["leader_changes"], 2 * WINDOW_SAMPLES)


class RankingTest(ControllerCase):
    def test_an_unfit_voter_is_swapped_for_the_fit_standby(self) -> None:
        status = self.pass_(healthy(a={"tcp": UNFIT_RTT_MS + 50}))
        self.assertEqual(status["decision"]["action"], "swap")
        self.assertEqual(status["decision"]["swap"], {"out": "a", "in": "d"})
        self.assertIn("DRY RUN: would swap out a for d", self.log)

    def test_wired_ranks_before_wifi_before_unknown_whatever_the_round_trip(self) -> None:
        hosts = healthy(a={"tcp": UNFIT_RTT_MS + 50, "link": "wifi"})
        hosts = [h for h in hosts if h.name != "d"]
        hosts += [Host("e", eligible=True, link="wifi", tcp=1.0), Host("f", eligible=True, link="wired", tcp=FIT_RTT_MS), Host("g", eligible=True, link="unknown", tcp=1.0)]
        status = self.pass_(hosts)
        self.assertEqual(status["ranking"], ["b", "c", "f", "e", "g"])
        self.assertEqual(status["decision"]["swap"], {"out": "a", "in": "f"})

    def test_within_a_link_type_the_lower_round_trip_ranks_first_then_the_lower_fsync_then_the_name(self) -> None:
        cases = (
            ("round trip", {"e": {"tcp": 10.0}, "f": {"tcp": 20.0}}, "e"),
            ("fsync", {"e": {"fsync_ms": 4.0}, "f": {"fsync_ms": 2.0}}, "f"),
            ("name", {"e": {}, "f": {}}, "e"),
        )
        for label, overrides, expected in cases:
            with self.subTest(label):
                base = {"tcp": 5.0, "fsync_ms": 3.0}
                hosts = healthy(a={"tcp": UNFIT_RTT_MS + 50, "link": "wifi"}, d={"tcp": FIT_RTT_MS + 20})
                hosts += [Host("e", eligible=True, **{**base, **overrides["e"]}), Host("f", eligible=True, **{**base, **overrides["f"]})]
                self.assertEqual(self.pass_(hosts)["decision"]["swap"], {"out": "a", "in": expected})

    def test_a_candidate_must_rank_above_the_voter_it_replaces(self) -> None:
        # The unfit voter is wired; the only fit standby is on a link the cluster cannot see, which ranks below wired.
        hosts = healthy(a={"tcp": UNFIT_RTT_MS + 50}, d={"link": "unknown"})
        status = self.pass_(hosts)
        self.assertEqual(status["decision"]["action"], "none")
        self.assertEqual(status["decision"]["because"][0], "voter a is unfit and no eligible standby is fit and ranks above it")

    def test_the_worst_ranked_unfit_voter_goes_out_first(self) -> None:
        hosts = healthy(a={"tcp": UNFIT_RTT_MS + 50}, b={"tcp": UNFIT_RTT_MS + 400})
        status = self.pass_(hosts)
        self.assertEqual(status["decision"]["swap"], {"out": "b", "in": "d"})

    def test_a_pinned_voter_is_never_swapped_out(self) -> None:
        status = self.pass_(healthy(a={"tcp": UNFIT_RTT_MS + 50, "pinned": True}))
        self.assertEqual(status["decision"]["action"], "none")
        self.assertEqual(status["decision"]["because"], ["voter a is the most at fault and is pinned or not marked eligible, so it is not replaced"])

    def test_a_voter_not_marked_eligible_is_never_swapped_out(self) -> None:
        status = self.pass_(healthy(a={"tcp": UNFIT_RTT_MS + 50, "eligible": False}))
        self.assertEqual(status["decision"]["action"], "none")
        self.assertEqual(status["decision"]["because"], ["voter a is the most at fault and is pinned or not marked eligible, so it is not replaced"])

    SLOW = UNFIT_RTT_MS + 200

    def slow_voter_c(self, **changes: dict[str, Any]) -> list[Host]:
        """Voter c has a slow link to every other host, as a voter on a poor path does: both ends of each of its links measure it slow, so a and b are unfit too, by the rule that a voter is unfit when its round trip to any other voter is."""
        hosts = healthy(
            a={"tcp": {"b": 5.0, "c": self.SLOW + 300}},  # the worst single reading is a's, but c is the end slow to every other voter
            b={"tcp": {"a": 5.0, "c": self.SLOW}},
            c={"tcp": {"a": self.SLOW, "b": self.SLOW}},
            d={"tcp": {"a": 5.0, "b": 5.0, "c": self.SLOW}},
        )
        for host in hosts:
            for key, value in changes.get(host.name, {}).items():
                setattr(host, key, value)
        return hosts

    def test_a_slow_link_makes_both_its_ends_unfit_and_the_voter_at_fault_is_replaced(self) -> None:
        status = self.pass_(self.slow_voter_c())
        self.assertEqual(self.verdicts(status), {"a": "unfit", "b": "unfit", "c": "unfit", "d": "unfit"})
        self.assertEqual(status["decision"]["swap"], {"out": "c", "in": "d"}, "c is slow to a and b, which reach each other fast")
        self.assertEqual(status["attribution"], {"c": ["a", "b", "c", "d"]})

    def test_a_candidate_is_judged_against_the_voters_that_would_remain(self) -> None:
        # d measures c slow, as everyone does, and is unfit against the full set; without c it is fit, which is what the swap needs.
        status = self.pass_(self.slow_voter_c())
        self.assertEqual(status["hosts"]["d"]["verdict"], "unfit")
        self.assertEqual(status["decision"]["action"], "swap")
        status = self.pass_(self.slow_voter_c(d={"tcp": {"a": self.SLOW, "b": 5.0, "c": self.SLOW}}))
        self.assertEqual(status["decision"]["action"], "none", "d is slow to a voter that stays")
        self.assertEqual(status["decision"]["because"], ["voter c is unfit and no eligible standby is fit and ranks above it"])

    def test_nothing_is_swapped_when_the_voter_at_fault_is_pinned_even_though_its_peers_are_unfit_and_replaceable(self) -> None:
        status = self.pass_(self.slow_voter_c(c={"pinned": True}))
        self.assertEqual(self.verdicts(status)["a"], "unfit")
        self.assertEqual(status["decision"]["action"], "none")
        self.assertEqual(status["decision"]["because"], ["voter c is the most at fault and is pinned or not marked eligible, so it is not replaced"])

    def test_when_every_link_is_slow_the_voter_whose_other_links_are_slow_too_is_at_fault(self) -> None:
        # As on a live fleet: a is on a poor path, slow to everyone, and b to c is only just over the limit. Every voter is unfit and every link is slow, so counting slow links ties, and b, which measures the worst single tail, ranks worst; the slowness follows a.
        just_over = UNFIT_RTT_MS + 10
        hosts = healthy(
            a={"tcp": {"b": self.SLOW, "c": self.SLOW}},
            b={"tcp": {"a": self.SLOW + 500, "c": just_over}},
            c={"tcp": {"a": self.SLOW, "b": just_over}},
            d={"tcp": {"a": self.SLOW, "b": 5.0, "c": 5.0}},
        )
        status = self.pass_(hosts)
        self.assertEqual(self.verdicts(status), {"a": "unfit", "b": "unfit", "c": "unfit", "d": "unfit"})
        self.assertEqual({name: host["attributed_to"] for name, host in status["hosts"].items()}, {"a": ["a"], "b": ["a"], "c": ["a"], "d": ["a"]})
        self.assertEqual(status["attribution"], {"a": ["a", "b", "c", "d"]})
        self.assertEqual(status["decision"]["swap"], {"out": "a", "in": "d"})
        self.assertIn("b (voter, link wired): unfit, round trip p99 to a", self.log)
        self.assertIn("(attributed to a)", self.log)

    def test_a_hosts_own_fault_is_attributed_to_it_and_a_link_fault_to_the_end_at_fault(self) -> None:
        status = self.pass_(self.slow_voter_c(a={"wal": WAL_LIMIT_MS + 5}))
        self.assertEqual(status["hosts"]["a"]["attributed_to"], ["a", "c"], "a's disk is its own, its slow link is c's")
        self.assertEqual(status["hosts"]["b"]["attributed_to"], ["c"])
        self.assertEqual(status["attribution"], {"a": ["a"], "c": ["a", "b", "c", "d"]})

    def test_a_standby_slow_to_every_voter_is_at_fault_itself(self) -> None:
        status = self.pass_(healthy(d={"tcp": FIT_RTT_MS + 20}))
        self.assertEqual((status["hosts"]["d"]["verdict"], status["hosts"]["d"]["attributed_to"]), ("unfit", ["d"]))
        self.assertEqual(status["attribution"], {"d": ["d"]})

    def test_a_standby_slow_to_a_voter_it_alone_reaches_slowly_shares_the_fault_with_it(self) -> None:
        # d is fast to b and c, and a is fast to b and c: nothing says which end of the one slow link is at fault.
        status = self.pass_(healthy(d={"tcp": {"a": FIT_RTT_MS + 20, "b": 5.0, "c": 5.0}}))
        self.assertEqual((status["hosts"]["d"]["verdict"], status["hosts"]["d"]["attributed_to"]), ("unfit", ["a", "d"]))
        self.assertEqual(status["hosts"]["a"]["attributed_to"], [], "a's own verdict is fit")

    def test_a_slow_link_between_voters_whose_other_links_are_fast_is_attributed_to_both(self) -> None:
        # Only the link between a and b is slow: a is fast to c, and so is b, so nothing says which end is at fault.
        status = self.pass_(healthy(a={"tcp": {"b": UNFIT_RTT_MS + 50, "c": 5.0}}))
        self.assertEqual(status["hosts"]["a"]["attributed_to"], ["a", "b"])
        self.assertEqual(status["hosts"]["b"]["attributed_to"], [], "b's own verdict is fit")

    def test_a_voter_slow_to_every_other_voter_is_at_fault_though_a_standby_reaches_it_fast(self) -> None:
        # a measures both its voter links slow, and b and c reach each other fast; d's one-sided reading of a does not clear it.
        status = self.pass_(healthy(a={"tcp": UNFIT_RTT_MS + 50}))
        self.assertEqual(status["hosts"]["a"]["attributed_to"], ["a"])
        self.assertEqual(status["attribution"], {"a": ["a"]})

    def test_a_verdict_that_is_not_unfit_is_attributed_to_no_one(self) -> None:
        status = self.pass_(healthy(d={"samples": WINDOW_SAMPLES - 1}))
        self.assertEqual({name: host["attributed_to"] for name, host in status["hosts"].items()}, {"a": [], "b": [], "c": [], "d": []})
        self.assertEqual(status["attribution"], {})

    def test_an_unfit_standby_is_not_a_candidate(self) -> None:
        status = self.pass_(healthy(a={"tcp": UNFIT_RTT_MS + 50}, d={"fsync_ms": 50.0}))
        self.assertEqual(status["decision"]["action"], "none")


class GateTest(ControllerCase):
    """The swap is wanted in each of these (voter a is unfit and d is fit); something about the cluster as a whole says not now."""

    def refused(self, **changes: dict[str, Any]) -> dict[str, Any]:
        status = self.pass_(healthy(a={"tcp": UNFIT_RTT_MS + 50, **changes.get("a", {})}, **{k: v for k, v in changes.items() if k != "a"}))
        self.assertEqual(status["decision"]["action"], "none", status["decision"])
        self.assertEqual(status["decision"]["wanted"], {"out": "a", "in": "d"})
        return status

    def test_no_swap_while_a_voter_is_below_full(self) -> None:
        status = self.refused(b={"level": "etcd-only"})
        self.assertEqual(status["decision"]["because"], ["voter b is at regrade level etcd-only, below full"])

    def test_no_swap_while_a_voter_is_not_ready(self) -> None:
        status = self.refused(c={"ready": False})
        self.assertIn("voter c is not Ready", status["decision"]["because"])

    def test_no_swap_while_a_voters_probe_has_stopped_reporting_as_when_it_is_paused(self) -> None:
        stale = STALE_AFTER_SAMPLES * SAMPLE_SECONDS
        status = self.refused(b={"stale_by": stale + 30})
        self.assertIn(f"voter b has no probe report in the last {stale} s", status["decision"]["because"])
        self.assertEqual(status["hosts"]["b"]["verdict"], "unknown")

    def test_no_swap_while_a_voters_etcd_metrics_could_not_be_read(self) -> None:
        populate(self.cluster, healthy(a={"tcp": UNFIT_RTT_MS + 50}))
        entries = self.cluster.probes()
        entries["b"]["samples"][-1]["etcd"] = None
        self.cluster.set_probes(entries)
        completed = self.cluster.run("fleet-role.sh", **CONTROLLER_ENV)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("etcd's state on voter b is unknown", self.cluster.status()["decision"]["because"])

    def test_no_swap_while_etcd_reports_no_leader(self) -> None:
        status = self.refused(b={"etcd": {"has_leader": False}})
        self.assertIn("voter b reports no leader", status["decision"]["because"])

    def test_no_swap_while_a_voter_is_a_learner(self) -> None:
        status = self.refused(b={"etcd": {"is_learner": True}})
        self.assertIn("voter b is a learner", status["decision"]["because"])

    def test_no_swap_while_a_voter_is_not_connected_to_every_other(self) -> None:
        status = self.refused(b={"etcd": {"active_peers": TARGET - 2}})
        self.assertIn(f"voter b is connected to {TARGET - 2} of its {TARGET - 1} peers", status["decision"]["because"])

    def test_no_swap_while_a_voter_knows_a_member_that_is_not_a_voter(self) -> None:
        # A learner joining, or a member added and not yet started, is a member etcd knows and no Node is labelled for.
        status = self.refused(b={"etcd": {"known_members": TARGET + 1}})
        self.assertIn(f"voter b knows {TARGET + 1} members, not the {TARGET} voters: a learner or a member that has not started", status["decision"]["because"])

    def test_no_swap_while_the_voter_count_is_not_the_target(self) -> None:
        hosts = healthy(a={"tcp": UNFIT_RTT_MS + 50})
        hosts.append(Host("e", voter=True, eligible=True))
        status = self.pass_(hosts)
        self.assertEqual(status["decision"]["action"], "none")
        self.assertIn(f"there are 4 voters, not the target {TARGET}", status["decision"]["because"])

    def test_no_swap_while_a_node_is_labelled_a_voter_and_is_not_one(self) -> None:
        # k3s labels a member the moment it joins as a learner, so the label alone does not make a vote.
        hosts = healthy(a={"tcp": UNFIT_RTT_MS + 50})
        hosts.append(Host("e", learner=True, eligible=True))
        status = self.pass_(hosts)
        self.assertEqual(status["decision"]["action"], "none")
        self.assertIn("node e is a member but not a voter: it carries the etcd label without EtcdIsVoter=True", status["decision"]["because"])

    def test_a_learner_by_label_is_not_counted_among_the_voters(self) -> None:
        hosts = healthy()
        hosts.append(Host("e", learner=True, eligible=True))
        status = self.pass_(hosts)
        self.assertEqual(status["membership"]["voters"], ["a", "b", "c"])
        self.assertEqual(status["hosts"]["e"]["role"], "standby")
        self.assertNotIn(f"there are {TARGET + 1} voters, not the target {TARGET}", status["blocks"])
        self.assertIn("node e is a member but not a voter: it carries the etcd label without EtcdIsVoter=True", status["blocks"])

    def test_a_node_whose_voter_condition_is_not_true_is_not_a_voter(self) -> None:
        populate(self.cluster, healthy())
        self.cluster.nodes[0]["status"]["conditions"] = [c for c in self.cluster.nodes[0]["status"]["conditions"] if c["type"] != "EtcdIsVoter"] + [
            {"type": "EtcdIsVoter", "status": "False"}
        ]
        completed = self.cluster.run("fleet-role.sh", **CONTROLLER_ENV)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = self.cluster.status()
        self.assertEqual(status["membership"]["voters"], ["b", "c"])
        self.assertIn("node a is a member but not a voter: it carries the etcd label without EtcdIsVoter=True", status["blocks"])

    def test_the_expected_voter_count_is_the_target_unless_a_swap_holds_a_new_voter(self) -> None:
        four = healthy()
        four[3].voter = True
        status = self.decide(four, expected_voters=TARGET)
        self.assertIn(f"there are {TARGET + 1} voters, not the target {TARGET}", status["blocks"])

    def test_one_more_than_the_target_is_expected_while_a_swap_holds_a_new_voter(self) -> None:
        four = healthy()
        four[3].voter = True
        status = self.decide(four, expected_voters=TARGET + 1)
        self.assertEqual(status["blocks"], [])

    def test_the_target_alone_is_not_the_expected_count_while_a_swap_holds_a_new_voter(self) -> None:
        status = self.decide(healthy(), expected_voters=TARGET + 1)
        self.assertEqual(status["blocks"], [f"there are {TARGET} voters, not the expected {TARGET + 1} (the target {TARGET} and the new voter a swap holds)"])

    def test_no_swap_during_the_cooldown_after_a_membership_change_and_a_swap_after_it(self) -> None:
        for since_change, expected in ((COOLDOWN_SECONDS - 60, "none"), (COOLDOWN_SECONDS + 60, "swap")):
            with self.subTest(since_change=since_change):
                hosts = healthy(a={"tcp": UNFIT_RTT_MS + 50})
                changed_at = int(time.time()) - since_change
                self.cluster.set_status({"membership": {"voters": ["a", "b", "c"], "changed_at": changed_at, "cooldown_until": changed_at + COOLDOWN_SECONDS}})
                # Only samples since the change count, and a window's worth have arrived since in either case (the cooldown outlasts the window).
                for host in hosts:
                    host.samples = WINDOW_SAMPLES
                status = self.pass_(hosts)
                self.assertEqual(status["decision"]["action"], expected, status["decision"])
                if expected == "none":
                    self.assertTrue(any("the cooldown lasts until" in reason for reason in status["decision"]["because"]))

    def test_the_cooldown_outlasts_the_window_by_the_learner_stall_time(self) -> None:
        self.assertEqual(COOLDOWN_SECONDS - WINDOW_SECONDS, 300)
        self.assertGreater(COOLDOWN_SECONDS, WINDOW_SECONDS)

    def test_readings_from_before_a_membership_change_are_not_counted(self) -> None:
        changed_at = int(time.time()) - (WINDOW_SECONDS // 2)
        self.cluster.set_status({"membership": {"voters": ["a", "b", "c"], "changed_at": changed_at, "cooldown_until": changed_at + COOLDOWN_SECONDS}})
        status = self.pass_(healthy())
        self.assertEqual(status["hosts"]["a"]["verdict"], "unknown")
        self.assertEqual(status["hosts"]["a"]["samples"], WINDOW_SAMPLES // 2 + 1)

    def test_fewer_eligible_hosts_than_the_target_is_reported_and_refuses_a_swap(self) -> None:
        status = self.pass_(healthy(a={"tcp": UNFIT_RTT_MS + 50}, b={"eligible": False}, c={"eligible": False}))
        self.assertEqual(status["decision"]["action"], "none")
        self.assertIn(f"2 hosts are eligible, fewer than the target {TARGET}", status["decision"]["because"])


class SizeTest(ControllerCase):
    # The probes' entries together can fill a ConfigMap, a mebibyte, and no one argument to a process may pass 128 KiB on Linux (the stand-in jq of tests/unit/fleet_role_support.py refuses one here).
    CONFIGMAP_LIMIT_BYTES = 1024 * 1024

    def test_inputs_the_size_of_a_full_configmap_are_read_whole(self) -> None:
        standbys = [Host(f"standby-with-a-long-node-name-{i:02d}", eligible=True) for i in range(14)]
        hosts = healthy() + standbys
        populate(self.cluster, hosts)
        size = sum(len(json.dumps(entry_)) for entry_ in self.cluster.probes().values())
        self.assertGreater(size, 3 * ARGUMENT_LIMIT_BYTES)
        self.assertLessEqual(size, self.CONFIGMAP_LIMIT_BYTES)
        completed = self.cluster.run("fleet-role.sh", **CONTROLLER_ENV)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        status = self.cluster.status()
        self.assertEqual(len(status["hosts"]), len(hosts))
        self.assertEqual(status["hosts"]["a"]["verdict"], "fit")


class MembershipTest(ControllerCase):
    def test_the_first_pass_records_the_voters_and_no_change(self) -> None:
        status = self.pass_(healthy())
        self.assertEqual(status["membership"], {"voters": ["a", "b", "c"], "changed_at": None, "cooldown_until": None})

    def test_a_pass_that_sees_the_same_voters_keeps_the_time_of_the_last_change(self) -> None:
        self.cluster.set_status({"membership": {"voters": ["a", "b", "c"], "changed_at": 1700000000, "cooldown_until": 1700000000 + COOLDOWN_SECONDS}})
        status = self.pass_(healthy())
        self.assertEqual(status["membership"]["changed_at"], 1700000000)

    def test_a_pass_that_sees_other_voters_records_the_change_now_and_the_cooldown(self) -> None:
        self.cluster.set_status({"membership": {"voters": ["a", "b", "x"], "changed_at": None, "cooldown_until": None}})
        before = int(time.time())
        status = self.pass_(healthy())
        self.assertGreaterEqual(status["membership"]["changed_at"], before)
        self.assertEqual(status["membership"]["cooldown_until"], status["membership"]["changed_at"] + COOLDOWN_SECONDS)


class ReportingTest(ControllerCase):
    def test_with_no_host_eligible_it_only_reports_the_voters(self) -> None:
        hosts = [Host("a", voter=True), Host("b", voter=True), Host("c", voter=True, tcp=UNFIT_RTT_MS + 50), Host("d")]
        status = self.pass_(hosts)
        self.assertEqual(sorted(status["hosts"]), ["a", "b", "c"])
        self.assertEqual(status["hosts"]["c"]["verdict"], "unfit")
        self.assertEqual(status["decision"]["action"], "none")
        self.assertIn("no host is marked eligible", status["decision"]["because"])

    def test_each_host_says_why_with_the_numbers(self) -> None:
        status = self.pass_(healthy(a={"tcp": UNFIT_RTT_MS + 50, "psi": PRESSURE_THRESHOLD, "avail_kb": TOTAL_KB * AVAILABLE_FLOOR_PERCENT // 100 - 1}))
        reasons = "; ".join(status["hosts"]["a"]["reasons"])
        self.assertIn("round trip p99 to", reasons)
        self.assertIn(f"over {UNFIT_RTT_MS:g} ms", reasons)
        self.assertIn(f"{ELECTION_TIMEOUT_MS} ms election timeout", reasons)
        self.assertIn("memory was over the regrade limit in 100 of 100 samples", reasons)
        self.assertIn("a (voter, link wired): unfit", self.log)

    def test_the_link_type_is_reported_for_every_host(self) -> None:
        status = self.pass_(healthy(a={"link": "wifi"}, d={"link": "unknown"}))
        self.assertEqual({name: host["link"] for name, host in status["hosts"].items()}, {"a": "wifi", "b": "wired", "c": "wired", "d": "unknown"})

    def test_where_each_hosts_link_was_read_is_reported(self) -> None:
        status = self.pass_(healthy(a={"link": "wifi", "link_source": "label"}))
        self.assertEqual({name: host["link_source"] for name, host in status["hosts"].items()}, {"a": "label", "b": "interface", "c": "interface", "d": "interface"})
        self.assertIn("a (voter, link wifi): fit", self.log)

    def test_a_pass_writes_only_its_own_status_and_changes_nothing_in_the_cluster(self) -> None:
        self.pass_(healthy(a={"tcp": UNFIT_RTT_MS + 50}))
        calls = [json.loads(line) for line in self.cluster.calls.read_text().splitlines()]
        writes = [call for call in calls if call[0] not in ("get",)]
        self.assertEqual(len(writes), 1, writes)
        self.assertEqual(writes[0][:3], ["patch", "configmap", "fleet-role-status"])


class SettingsTest(ControllerCase):
    def refuses(self, message: str, **env: str) -> None:
        populate(self.cluster, healthy())
        completed = self.cluster.run("fleet-role.sh", **{**CONTROLLER_ENV, **env})
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(message, completed.stderr)
        self.assertEqual([c for c in self.cluster.calls.read_text().splitlines() if "patch" in c], [], "nothing is published from refused settings")

    def test_an_even_target_is_refused(self) -> None:
        self.refuses("must be odd", FLEET_ROLE_VOTERS="4")

    def test_a_target_under_three_is_refused(self) -> None:
        self.refuses("must be at least 3", FLEET_ROLE_VOTERS="1")

    def test_a_target_without_a_place_of_headroom_below_the_maximum_is_refused(self) -> None:
        self.refuses("one place of headroom", FLEET_ROLE_VOTERS=str(MAX_SERVERS))
        self.refuses("one place of headroom", FLEET_ROLE_VOTERS="3", FLEET_ROLE_MAX_SERVERS="3")

    def test_the_largest_target_that_leaves_headroom_is_accepted(self) -> None:
        status = self.pass_(healthy(), FLEET_ROLE_VOTERS="5", FLEET_ROLE_MAX_SERVERS="6")
        self.assertEqual(status["config"]["target"], 5)

    def test_a_pass_that_is_not_a_dry_run_is_refused(self) -> None:
        self.refuses("FLEET_ROLE_DRY_RUN must be true", FLEET_ROLE_DRY_RUN="false")

    def test_the_election_timeout_is_required_so_that_the_controller_and_the_probe_cannot_disagree_on_a_default(self) -> None:
        populate(self.cluster, healthy())
        env = {k: v for k, v in CONTROLLER_ENV.items() if k != "FLEET_ROLE_ELECTION_TIMEOUT_MS"}
        completed = self.cluster.run("fleet-role.sh", **env)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("FLEET_ROLE_ELECTION_TIMEOUT_MS must be set", completed.stderr)


class RegradeCoreAgreementTest(unittest.TestCase):
    """The memory limit the controller applies is the regrade core's own is_over, checked over a grid of readings on both sides of each limit."""

    def test_the_controllers_memory_limit_agrees_with_the_regrade_cores_is_over(self) -> None:
        total = 1000
        settings = {"pressure_threshold": PRESSURE_THRESHOLD, "available_floor_percent": AVAILABLE_FLOOR_PERCENT}
        for psi in (0, PRESSURE_THRESHOLD - 0.5, PRESSURE_THRESHOLD, PRESSURE_THRESHOLD + 0.5, 100):
            for available in (0, 149, 150, 151, 1000):
                core = subprocess.run(
                    ["bash", "-c", f'. "{REGRADE_CORE}"; PRESSURE_THRESHOLD={PRESSURE_THRESHOLD} AVAILABLE_FLOOR_PERCENT={AVAILABLE_FLOOR_PERCENT}; is_over {psi} {available} {total}'],
                    capture_output=True,
                    text=True,
                    check=False,
                    env={**os.environ, "FLEET_REGRADE_ENV": "/nonexistent"},
                ).returncode == 0
                ours = subprocess.run(
                    ["jq", "-n", "-e", "-L", str(SCRIPTS), "--argjson", "c", json.dumps(settings), "--argjson", "reading", json.dumps({"psi": psi, "avail_kb": available, "total_kb": total}),
                     'include "fleet-role-stats"; $reading | over_regrade_limit($c)'],
                    capture_output=True,
                    text=True,
                    check=False,
                ).returncode == 0
                self.assertEqual(ours, core, (psi, available))


if __name__ == "__main__":
    unittest.main()
