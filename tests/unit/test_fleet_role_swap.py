"""Tests for the voter swap of scripts/fleet-role.sh and scripts/fleet-role-swap.jq: the controller's state machine, run against a scripted stand-in for kubectl that keeps Nodes, ConfigMaps and Leases and carries out what a pass writes, with the real bash and jq. Each test sets the cluster at one step of a swap (the Nodes' labels and annotations, the regrade Lease, the actuators' reports, the probes) and runs a pass, since the step a swap is at is read from those and nothing else."""

from __future__ import annotations

import json
import re
import subprocess
import time
import unittest
from typing import Any

from fleet_role_support import (
    ACTUATOR_CONFIGMAP,
    CONTROLLER_ENV,
    COOLDOWN_SECONDS,
    LEASE_SECONDS,
    LEVEL_LABEL,
    NAMESPACE,
    OPERATION_TIMEOUT_SECONDS,
    ROOT,
    RUNNER_WAIT_SECONDS,
    SAMPLE_SECONDS,
    SCRIPTS,
    STATUS_CONFIGMAP,
    TARGET,
    UNFIT_RTT_MS,
    VOTER_LABEL,
    transition_time,
)
from test_fleet_role import ControllerCase, Host, healthy, populate

DOMAIN = "github-runner.exadev.com/"
DESIRED = DOMAIN + "desired-role"
SWAP_ANNOTATION = DOMAIN + "role-swap"
STEP_ANNOTATION = DOMAIN + "role-step"
JOIN_ANNOTATION = DOMAIN + "role-join-node"
SNAPSHOT_ANNOTATION = DOMAIN + "snapshot-request"
MEMBER_GONE = DOMAIN + "role-member-gone"
LEASE_ID = DOMAIN + "swap-id"
LEASE_NODES = DOMAIN + "swap-nodes"
REMOVE = "etcd.k3s.cattle.io/remove"
REMOVED = "etcd.k3s.cattle.io/removed-node-name"
CONTROL_PLANE = "node-role.kubernetes.io/control-plane"
MASTER = "node-role.kubernetes.io/master"
LEASE_KEY = "kube-system/fleet-regrade"
LEARNER_STALL_SECONDS = 300
OUT = "c"
IN = "d"
SNAPSHOT_FILE = "fleet-role-snapshot-a-1"


def iso(seconds_ago: float = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - seconds_ago)) + ".000000Z"


def swap_id(out: str = OUT, in_: str = IN, seconds_ago: float = 0) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() - seconds_ago)) + f"-{out}-{in_}"


def lease(swap: str | None, *, holder: str = "fleet-role", renewed_ago: float = 0, nodes: str = f"{OUT},{IN}") -> dict[str, Any]:
    annotations = {} if swap is None else {LEASE_ID: swap, LEASE_NODES: nodes}
    return {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {"name": "fleet-regrade", "namespace": "kube-system", "resourceVersion": "7", "annotations": annotations},
        "spec": {"holderIdentity": holder, "leaseDurationSeconds": LEASE_SECONDS, "acquireTime": iso(renewed_ago + 5), "renewTime": iso(renewed_ago)},
    }


def report(swap: str, step: str, phase: str = "done", **fields: Any) -> str:
    return json.dumps({"swap": swap, "step": step, "phase": phase, "role": "agent", "started_at": "", "at": iso(), "message": "", "snapshot": "", **fields})


class SwapCase(ControllerCase):
    """A cluster of voters a, b and c and an eligible standby d, healthy; helpers that put it at a step of the swap of c for d."""

    def setUp(self) -> None:
        super().setUp()
        self.hosts: list[Host] = healthy()
        self.id = swap_id()
        self.edits: dict[str, dict[str, Any]] = {}
        self.completed: subprocess.CompletedProcess[str] | None = None

    # --- Building the cluster ---

    def voters(self, *names: str) -> None:
        for host in self.hosts:
            host.voter = host.name in names

    def edit(self, name: str, *, labels: dict[str, str | None] | None = None, annotations: dict[str, str | None] | None = None, cordoned: bool | None = None, taint: bool | None = None) -> None:
        change = self.edits.setdefault(name, {"labels": {}, "annotations": {}})
        change["labels"].update(labels or {})
        change["annotations"].update(annotations or {})
        if cordoned is not None:
            change["cordoned"] = cordoned
        if taint is not None:
            change["taint"] = taint

    def apply_edits(self) -> None:
        for item in self.cluster.nodes:
            change = self.edits.get(item["metadata"]["name"])
            if change is None:
                continue
            for section in ("labels", "annotations"):
                target = item["metadata"].setdefault(section, {})
                for key, value in change[section].items():
                    if value is None:
                        target.pop(key, None)
                    else:
                        target[key] = value
            if "cordoned" in change:
                item.setdefault("spec", {})["unschedulable"] = change["cordoned"]
            if "taint" in change:
                item.setdefault("spec", {})["taints"] = [{"key": CONTROL_PLANE, "value": "true", "effect": "NoSchedule"}] if change["taint"] else []

    def hold(self, swap: str | None = None, **changes: Any) -> None:
        self.cluster.leases[LEASE_KEY] = lease(self.id if swap is None else swap, **changes)

    def report(self, node: str, swap: str, step: str, phase: str = "done", **fields: Any) -> None:
        self.cluster.configmaps[f"{NAMESPACE}/{ACTUATOR_CONFIGMAP}"]["data"][node] = report(swap, step, phase, **fields)

    def kubelet_lease(self, node: str, seconds_ago: float = 0) -> None:
        self.cluster.leases[f"kube-node-lease/{node}"] = {"spec": {"renewTime": iso(seconds_ago)}}

    def previous(self, step: str, since_ago: float = 0, **fields: Any) -> None:
        """The status the last pass published: the swap at this step since this long ago."""
        now = int(time.time())
        record = {"id": self.id, "out": OUT, "in": IN, "step": step, "started_at": now - 60, "step_since": now - int(since_ago), "deadline": now, "manual": True, "snapshot": None, "outgoing_pending": False, **fields}
        self.cluster.set_status({"swap": record, "membership": {"voters": [h.name for h in self.hosts if h.voter], "changed_at": None, "cooldown_until": None}})

    # --- Running ---

    def run_pass(self, *, repopulate: bool = True, **env: str) -> subprocess.CompletedProcess[str]:
        if repopulate:
            populate(self.cluster, self.hosts)
            self.apply_edits()
        self.completed = self.cluster.run("fleet-role.sh", **{**CONTROLLER_ENV, **env})
        return self.completed

    def ok_pass(self, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        completed = self.run_pass(**kwargs)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return completed

    def command(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        populate(self.cluster, self.hosts)
        self.apply_edits()
        return self.cluster.run("fleet-role.sh", *args, **{**CONTROLLER_ENV, **env})

    # --- Reading the result ---

    def node(self, name: str) -> dict[str, Any]:
        [found] = [n for n in self.cluster.nodes if n["metadata"]["name"] == name]
        return found

    def annotations(self, name: str) -> dict[str, str]:
        return self.node(name)["metadata"].get("annotations", {})

    def labels(self, name: str) -> dict[str, str]:
        return self.node(name)["metadata"].get("labels", {})

    def cordoned(self, name: str) -> bool:
        return bool(self.node(name).get("spec", {}).get("unschedulable"))

    def taints(self, name: str) -> list[dict[str, str]]:
        return self.node(name).get("spec", {}).get("taints", [])

    def writes(self) -> list[list[str]]:
        return [call for call in map(json.loads, self.cluster.calls.read_text().splitlines()) if call[0] not in ("get",)]

    def changes(self) -> list[list[str]]:
        """What a pass changed in the cluster: every write but the renewal of the Lease it holds and the publishing of its own status."""
        return [w for w in self.writes() if w[0] != "replace" and w[:3] != ["patch", "configmap", "fleet-role-status"]]

    def held_by(self) -> str:
        return self.cluster.leases[LEASE_KEY]["spec"]["holderIdentity"]

    def swap(self) -> dict[str, Any] | None:
        return self.cluster.status()["swap"]

    def last_swap(self) -> dict[str, Any] | None:
        return self.cluster.status()["last_swap"]

    def request_data(self) -> str | None:
        return self.cluster.configmaps[f"{NAMESPACE}/{STATUS_CONFIGMAP}"]["data"].get("request.json")

    # --- Standard positions in a swap ---

    def at_snapshot_taken(self) -> None:
        """The Lease is held, a snapshot was asked of a and is ready."""
        self.hold()
        self.edit("a", annotations={SNAPSHOT_ANNOTATION: self.id})
        self.report("a", self.id, "snapshot", snapshot=SNAPSHOT_FILE)
        self.cluster.snapshots = [{"spec": {"snapshotName": SNAPSHOT_FILE}, "status": {"readyToUse": True}}]
        self.previous("drain")

    def at_promotion_asked(self, **fields: Any) -> None:
        """The candidate is cordoned and has been asked to promote."""
        self.at_snapshot_taken()
        self.edit(IN, labels={DESIRED: "server"}, annotations={SWAP_ANNOTATION: self.id, STEP_ANNOTATION: "promote", JOIN_ANNOTATION: "a"}, cordoned=True)
        self.previous("promote", **fields)

    def at_candidate_voting(self) -> None:
        """The candidate votes and is Ready on a heartbeat after its server started; it is still cordoned."""
        self.at_promotion_asked()
        self.voters("a", "b", "c", "d")
        started = iso(120)
        self.report(IN, self.id, "promote", started_at=started)
        self.kubelet_lease(IN)

    def at_candidate_uncordoned(self) -> None:
        self.at_candidate_voting()
        self.edit(IN, cordoned=False)
        self.previous("gates")

    def at_step_down_asked(self) -> None:
        self.at_candidate_uncordoned()
        self.edit(OUT, labels={LEVEL_LABEL: "etcd-only", DESIRED: "agent"}, annotations={SWAP_ANNOTATION: self.id, STEP_ANNOTATION: "step-down"}, cordoned=True, taint=True)
        self.previous("step-down")

    def at_removal_asked(self) -> None:
        self.at_step_down_asked()
        self.report(OUT, self.id, "step-down")
        self.edit(OUT, annotations={REMOVE: "true"})
        self.previous("remove")

    def at_member_removed(self) -> None:
        self.at_removal_asked()
        self.edit(OUT, annotations={REMOVED: "c-1234", REMOVE: None})
        self.previous("finish")

    def at_demotion_finished(self) -> None:
        self.at_member_removed()
        self.report(OUT, self.id, "finish-demote", started_at=iso(60))


class StartTest(SwapCase):
    def test_a_manual_request_is_accepted_when_every_gate_holds_and_recorded_for_the_next_pass(self) -> None:
        completed = self.command("swap", OUT, IN)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(self.request_data() or "null")["out"], OUT)
        self.assertEqual(json.loads(self.request_data() or "null")["in"], IN)
        self.assertEqual(self.writes()[-1][:3], ["patch", "configmap", "fleet-role-status"])
        self.assertNotIn("fleet-regrade", json.dumps(self.cluster.leases), "accepting a request takes no Lease")

    def test_the_next_pass_takes_the_lease_for_the_swap_naming_its_nodes_and_clears_the_request(self) -> None:
        self.command("swap", OUT, IN)
        self.ok_pass(repopulate=False)
        taken = self.cluster.leases[LEASE_KEY]
        self.assertEqual(taken["spec"]["holderIdentity"], "fleet-role")
        self.assertEqual(taken["spec"]["leaseDurationSeconds"], LEASE_SECONDS)
        self.assertRegex(taken["metadata"]["annotations"][LEASE_ID], rf"^\d{{8}}T\d{{6}}Z-{OUT}-{IN}$")
        self.assertEqual(taken["metadata"]["annotations"][LEASE_NODES], f"{OUT},{IN}")
        self.assertEqual((self.swap() or {})["step"], "snapshot")
        self.assertTrue((self.swap() or {})["manual"])
        self.assertIsNone(self.request_data())

    def test_dry_run_carries_out_a_manual_request_and_reports_an_automatic_decision_only(self) -> None:
        self.hosts = healthy(c={"tcp": UNFIT_RTT_MS + 50})
        self.ok_pass(FLEET_ROLE_DRY_RUN="true")
        self.assertNotIn(LEASE_KEY, self.cluster.leases)
        self.assertIn("DRY RUN: would swap out c for d", self.completed.stdout if self.completed else "")
        self.assertEqual([w for w in self.writes() if w[0] != "patch"], [])
        self.command("swap", OUT, IN, FLEET_ROLE_DRY_RUN="true")
        self.ok_pass(repopulate=False, FLEET_ROLE_DRY_RUN="true")
        self.assertIn(LEASE_KEY, self.cluster.leases)

    def test_with_the_dry_run_off_the_controller_starts_the_swap_it_decides_on(self) -> None:
        self.hosts = healthy(c={"tcp": UNFIT_RTT_MS + 50})
        self.ok_pass(FLEET_ROLE_DRY_RUN="false")
        self.assertEqual(self.held_by(), "fleet-role")
        self.assertRegex(self.cluster.leases[LEASE_KEY]["metadata"]["annotations"][LEASE_ID], rf"-{OUT}-{IN}$")
        self.assertFalse((self.swap() or {})["manual"])
        self.assertFalse(self.cluster.status()["dry_run"])

    def test_no_second_swap_is_decided_inside_the_cooldown_after_a_membership_change(self) -> None:
        self.hosts = healthy(c={"tcp": UNFIT_RTT_MS + 50})
        changed_at = int(time.time()) - (COOLDOWN_SECONDS - 60)
        self.cluster.set_status({"membership": {"voters": ["a", "b", "c"], "changed_at": changed_at, "cooldown_until": changed_at + COOLDOWN_SECONDS}})
        self.ok_pass(FLEET_ROLE_DRY_RUN="false")
        self.assertNotIn(LEASE_KEY, self.cluster.leases)
        self.assertIsNone(self.swap())

    def test_a_manual_request_is_carried_out_inside_the_cooldown_and_whatever_the_verdicts_say(self) -> None:
        changed_at = int(time.time()) - 60
        self.cluster.set_status({"membership": {"voters": ["a", "b", "c"], "changed_at": changed_at, "cooldown_until": changed_at + COOLDOWN_SECONDS}})
        self.assertEqual(self.command("swap", OUT, IN).returncode, 0)

    def test_a_manual_request_is_refused_for_a_pinned_voter_naming_it(self) -> None:
        self.hosts = healthy(c={"pinned": True})
        completed = self.command("swap", OUT, IN)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("voter c is pinned", completed.stderr)
        self.assertIsNone(self.request_data())

    def test_a_manual_request_is_refused_for_nodes_that_are_unknown_not_eligible_or_the_wrong_kind(self) -> None:
        self.hosts = healthy(d={"eligible": False})
        cases = {
            ("c", "d"): "d is not marked eligible",
            ("c", "zz"): "no node is named zz",
            ("zz", "d"): "no node is named zz",
            ("d", "c"): "d is not a voter",
            ("a", "b"): "b is already a member of etcd",
            ("c", "c"): "a swap needs two nodes",
        }
        for (out, in_), message in cases.items():
            with self.subTest(out=out, in_=in_):
                completed = self.command("swap", out, in_)
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn(message, completed.stderr)
        self.assertEqual(self.command("swap", OUT).returncode, 2)

    def test_a_manual_request_is_refused_with_each_reason_while_a_voter_is_paused(self) -> None:
        self.hosts = healthy(b={"stale_by": 10 * SAMPLE_SECONDS})
        completed = self.command("swap", OUT, IN)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("voter b has no probe report", completed.stderr)
        self.assertIsNone(self.request_data())
        self.assertNotIn(LEASE_KEY, self.cluster.leases)
        self.assertEqual(self.labels(IN).get(DESIRED), None)

    def test_a_manual_request_is_refused_while_the_regrade_lease_is_held_by_another_and_while_a_swap_runs(self) -> None:
        self.cluster.leases[LEASE_KEY] = lease(None, holder="other-node")
        completed = self.command("swap", OUT, IN)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("the regrade Lease is held by other-node", completed.stderr)
        self.hold()
        completed = self.command("swap", OUT, IN)
        self.assertEqual(completed.returncode, 1)
        self.assertIn(f"a swap is in progress ({self.id})", completed.stderr)

    def test_a_request_that_stopped_being_valid_before_the_pass_is_dropped_and_recorded(self) -> None:
        self.command("swap", OUT, IN)
        self.hosts = healthy(b={"stale_by": 10 * SAMPLE_SECONDS})
        self.ok_pass()
        self.assertIsNone(self.request_data())
        self.assertNotIn(LEASE_KEY, self.cluster.leases)
        self.assertEqual((self.last_swap() or {})["outcome"], "abandoned")
        self.assertIn("voter b has no probe report", " ".join((self.last_swap() or {})["reasons"]))

    def test_an_expired_lease_of_another_holder_is_taken_and_a_released_one_too(self) -> None:
        for existing in (lease(None, holder="regrade-node", renewed_ago=LEASE_SECONDS + 5), lease(None, holder="", renewed_ago=10)):
            with self.subTest(holder=existing["spec"]["holderIdentity"]):
                self.cluster.leases[LEASE_KEY] = existing
                self.command("swap", OUT, IN)
                self.ok_pass(repopulate=False)
                self.assertEqual(self.held_by(), "fleet-role")
                self.cluster.leases.clear()


class SnapshotTest(SwapCase):
    def test_a_snapshot_is_asked_of_a_remaining_voter_and_never_of_the_outgoing_one(self) -> None:
        self.hold()
        self.previous("snapshot")
        self.ok_pass()
        asked = [name for name in "abcd" if SNAPSHOT_ANNOTATION in self.annotations(name)]
        self.assertEqual(asked, ["a"])
        self.assertEqual(self.annotations("a")[SNAPSHOT_ANNOTATION], self.id)
        self.assertEqual(self.swap()["step"], "snapshot")

    def test_a_snapshot_is_never_asked_of_the_outgoing_voter_even_when_it_sorts_first(self) -> None:
        self.id = swap_id("a", IN)
        self.hold(nodes=f"a,{IN}")
        self.previous("snapshot", out="a")
        self.ok_pass()
        self.assertEqual([name for name in "abcd" if SNAPSHOT_ANNOTATION in self.annotations(name)], ["b"])

    def test_the_swap_waits_while_the_snapshot_is_not_ready_even_when_the_actuator_says_done(self) -> None:
        self.at_snapshot_taken()
        self.cluster.snapshots = [{"spec": {"snapshotName": SNAPSHOT_FILE}, "status": {"readyToUse": False}}]
        self.previous("snapshot")
        self.ok_pass()
        self.assertEqual(self.changes(), [], "nothing but the status is written")
        self.assertFalse(self.cordoned(IN))

    def test_a_ready_snapshot_lets_the_candidate_be_cordoned(self) -> None:
        self.at_snapshot_taken()
        self.previous("snapshot")
        self.ok_pass()
        self.assertTrue(self.cordoned(IN))
        self.assertEqual(self.swap()["snapshot"], SNAPSHOT_FILE)

    def test_a_snapshot_that_is_not_ready_in_the_operation_timeout_abandons_the_swap_and_releases_the_lease(self) -> None:
        self.hold()
        self.edit("a", annotations={SNAPSHOT_ANNOTATION: self.id})
        self.previous("snapshot", since_ago=OPERATION_TIMEOUT_SECONDS + 5)
        self.ok_pass()
        self.assertEqual(self.held_by(), "")
        self.assertEqual(self.last_swap()["outcome"], "abandoned")
        self.assertIn(f"no snapshot was ready within {OPERATION_TIMEOUT_SECONDS} s", self.last_swap()["reasons"][0])
        self.assertNotIn(SNAPSHOT_ANNOTATION, self.annotations("a"))
        self.assertIsNone(self.swap())

    def test_one_second_inside_the_operation_timeout_the_swap_still_waits(self) -> None:
        self.hold()
        self.edit("a", annotations={SNAPSHOT_ANNOTATION: self.id})
        self.previous("snapshot", since_ago=OPERATION_TIMEOUT_SECONDS - 10)
        self.ok_pass()
        self.assertEqual(self.held_by(), "fleet-role")
        self.assertEqual(self.swap()["step"], "snapshot")

    def test_a_snapshot_that_fails_on_one_voter_is_asked_of_the_next_and_when_none_can_the_swap_is_abandoned(self) -> None:
        self.hold()
        self.edit("a", annotations={SNAPSHOT_ANNOTATION: self.id})
        self.report("a", self.id, "snapshot", "failed", message="k3s etcd-snapshot save failed")
        self.previous("snapshot")
        self.ok_pass()
        self.assertEqual(self.annotations("b")[SNAPSHOT_ANNOTATION], self.id)
        self.report("b", self.id, "snapshot", "refused", message="not a running server")
        self.ok_pass(repopulate=False)
        self.assertEqual(self.last_swap()["outcome"], "abandoned")
        self.assertIn("no voter could take a snapshot", self.last_swap()["reasons"][0])
        self.assertEqual(self.held_by(), "")

    def test_the_swap_does_not_start_to_empty_the_candidate_while_a_gate_fails(self) -> None:
        self.at_snapshot_taken()
        self.hosts = healthy(b={"stale_by": 10 * SAMPLE_SECONDS})
        self.previous("snapshot")
        self.ok_pass()
        self.assertFalse(self.cordoned(IN))
        self.assertEqual(self.held_by(), "fleet-role")


class CandidateTest(SwapCase):
    def test_the_candidate_is_cordoned_and_while_runner_pods_run_on_it_the_swap_waits(self) -> None:
        self.at_snapshot_taken()
        self.edit(IN, cordoned=True)
        self.cluster.runner_pods[IN] = 2
        self.previous("drain", since_ago=RUNNER_WAIT_SECONDS - 60)
        self.ok_pass()
        self.assertEqual(self.changes(), [])
        self.assertNotIn(DESIRED, self.labels(IN))
        self.assertEqual(self.swap()["step"], "drain")

    def test_past_the_runner_wait_the_swap_is_abandoned_with_the_candidate_uncordoned_the_lease_released_and_no_cooldown(self) -> None:
        self.at_snapshot_taken()
        self.edit(IN, cordoned=True)
        self.cluster.runner_pods[IN] = 2
        self.previous("drain", since_ago=RUNNER_WAIT_SECONDS + 5)
        self.ok_pass()
        self.assertFalse(self.cordoned(IN))
        self.assertEqual(self.held_by(), "")
        self.assertEqual(self.last_swap()["outcome"], "abandoned")
        self.assertIn(f"did not finish within {RUNNER_WAIT_SECONDS} s", self.last_swap()["reasons"][0])
        self.assertEqual(self.cluster.status()["membership"]["voters"], ["a", "b", "c"])
        self.assertNotIn(SNAPSHOT_ANNOTATION, self.annotations("a"))

    def test_with_no_runner_pods_left_the_candidate_is_drained_and_asked_to_promote_joining_a_current_voter(self) -> None:
        self.at_snapshot_taken()
        self.edit(IN, cordoned=True)
        self.previous("drain")
        self.ok_pass()
        drains = [w for w in self.writes() if w[0] == "drain"]
        self.assertEqual(len(drains), 1)
        self.assertIn(f"--timeout={OPERATION_TIMEOUT_SECONDS}s", drains[0])
        self.assertEqual(self.labels(IN)[DESIRED], "server")
        self.assertEqual(self.annotations(IN)[SWAP_ANNOTATION], self.id)
        self.assertEqual(self.annotations(IN)[STEP_ANNOTATION], "promote")
        self.assertEqual(self.annotations(IN)[JOIN_ANNOTATION], "a")

    def test_a_drain_that_fails_asks_for_nothing_and_exits_non_zero_so_the_next_pass_repeats_it(self) -> None:
        self.at_snapshot_taken()
        self.edit(IN, cordoned=True)
        self.previous("drain")
        completed = self.run_pass(FAKE_DRAIN_FAILS=IN)
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn(DESIRED, self.labels(IN))
        self.assertEqual(self.swap()["id"], self.id, "the swap stays at its step")
        self.assertEqual(self.held_by(), "fleet-role")

    def test_a_drain_that_never_succeeds_abandons_the_swap_after_the_runner_wait_and_the_operation_timeout(self) -> None:
        self.at_snapshot_taken()
        self.edit(IN, cordoned=True)
        self.previous("drain", since_ago=RUNNER_WAIT_SECONDS + OPERATION_TIMEOUT_SECONDS + 5)
        self.ok_pass()
        self.assertEqual(self.last_swap()["outcome"], "abandoned")
        self.assertIn("could not be drained", self.last_swap()["reasons"][0])
        self.assertFalse(self.cordoned(IN))

    def test_the_promotion_waits_for_a_voter_that_is_ready_on_a_heartbeat_after_the_server_started(self) -> None:
        self.at_candidate_voting()
        self.kubelet_lease(IN, seconds_ago=3600)
        self.previous("promote")
        self.ok_pass()
        self.assertTrue(self.cordoned(IN))
        self.assertEqual(self.changes(), [])

    def test_a_voting_ready_candidate_with_a_fresh_heartbeat_is_uncordoned(self) -> None:
        self.at_candidate_voting()
        self.previous("promote")
        self.ok_pass()
        self.assertFalse(self.cordoned(IN))

    def test_a_candidate_that_is_not_a_voter_by_the_deadline_is_rolled_back_asking_k3s_to_remove_its_learner_when_it_joined(self) -> None:
        self.at_promotion_asked()
        self.edit(IN, labels={VOTER_LABEL: "true"}, annotations={"etcd.k3s.cattle.io/node-name": "d-1"})
        self.previous("promote", since_ago=LEARNER_STALL_SECONDS + OPERATION_TIMEOUT_SECONDS + 5)
        self.ok_pass()
        self.assertEqual(self.annotations(IN)[REMOVE], "true")
        self.assertEqual(self.labels(IN)[DESIRED], "agent")
        self.assertEqual(self.annotations(IN)[STEP_ANNOTATION], "rollback")
        self.assertEqual(self.annotations(IN)[SWAP_ANNOTATION], self.id)

    def test_a_candidate_that_never_joined_is_rolled_back_without_asking_for_a_removal(self) -> None:
        self.at_promotion_asked()
        self.previous("promote", since_ago=LEARNER_STALL_SECONDS + OPERATION_TIMEOUT_SECONDS + 5)
        self.ok_pass()
        self.assertNotIn(REMOVE, self.annotations(IN))
        self.assertEqual(self.annotations(IN)[STEP_ANNOTATION], "rollback")

    def test_one_second_inside_the_deadline_the_promotion_is_still_waited_for(self) -> None:
        self.at_promotion_asked()
        self.previous("promote", since_ago=LEARNER_STALL_SECONDS + OPERATION_TIMEOUT_SECONDS - 20)
        self.ok_pass()
        self.assertEqual(self.annotations(IN)[STEP_ANNOTATION], "promote")

    def test_an_actuator_that_reports_the_promotion_failed_starts_the_rollback_at_once(self) -> None:
        self.at_promotion_asked()
        self.report(IN, self.id, "promote", "failed", message="the server unit did not start")
        self.ok_pass()
        self.assertEqual(self.annotations(IN)[STEP_ANNOTATION], "rollback")

    def test_an_actuator_that_refuses_the_promotion_abandons_the_swap_since_nothing_changed_on_the_host(self) -> None:
        self.at_promotion_asked()
        self.report(IN, self.id, "promote", "refused", message="no server configuration")
        self.ok_pass()
        self.assertEqual(self.last_swap()["outcome"], "abandoned")
        self.assertIn("no server configuration", self.last_swap()["reasons"][0])
        self.assertFalse(self.cordoned(IN))
        self.assertEqual(self.held_by(), "")
        self.assertNotIn(SWAP_ANNOTATION, self.annotations(IN))


class RollbackTest(SwapCase):
    def at_rollback_done(self, **edits: Any) -> None:
        self.at_promotion_asked()
        self.edit(IN, labels={VOTER_LABEL: "true", CONTROL_PLANE: "true", MASTER: "true", DESIRED: "agent"},
                  annotations={STEP_ANNOTATION: "rollback", "etcd.k3s.cattle.io/node-name": "d-1", "etcd.k3s.cattle.io/node-address": "100.64.0.4", REMOVE: "true"}, taint=True)
        self.report(IN, self.id, "rollback")
        self.previous("rollback")

    def test_a_rollback_that_the_actuator_has_not_finished_is_waited_for(self) -> None:
        self.at_rollback_done()
        self.report(IN, self.id, "rollback", "running")
        self.ok_pass()
        self.assertEqual(self.changes(), [])

    def test_a_finished_rollback_waits_until_every_voter_lists_the_target_members_and_no_more(self) -> None:
        self.at_rollback_done()
        for host in self.hosts:
            host.etcd = {"known_members": TARGET + 1}
        self.ok_pass()
        self.assertEqual(self.changes(), [])
        self.assertIn("waiting for every voter to list 3 members", " ".join(self.cluster.status()["waiting"]))

    def test_a_finished_rollback_with_membership_as_it_was_clears_the_servers_labels_annotations_and_taint_and_uncordons(self) -> None:
        self.at_rollback_done()
        self.voters("a", "b", "c")
        self.ok_pass()
        self.assertFalse(self.cordoned(IN))
        for key in (VOTER_LABEL, CONTROL_PLANE, MASTER):
            self.assertNotIn(key, self.labels(IN))
        self.assertEqual([k for k in self.annotations(IN) if k.startswith("etcd.k3s.cattle.io/")], [])
        self.assertEqual(self.taints(IN), [])
        self.assertEqual(self.held_by(), "fleet-role", "the lease is held until the swap ends")

    def test_after_the_clean_up_the_rollback_ends_with_the_lease_released_and_the_swap_annotations_cleared(self) -> None:
        self.at_rollback_done()
        self.voters("a", "b", "c")
        self.ok_pass()
        self.ok_pass(repopulate=False)
        self.assertEqual(self.last_swap()["outcome"], "rolled-back")
        self.assertEqual(self.held_by(), "")
        self.assertEqual([k for k in self.annotations(IN) if k.startswith(DOMAIN)], [])
        self.assertEqual(self.labels(IN)[DESIRED], "agent")
        self.assertIsNone(self.swap())

    def test_a_rollback_the_actuator_reports_failed_is_held_for_the_owner_and_says_why(self) -> None:
        self.at_rollback_done()
        self.report(IN, self.id, "rollback", "failed", message="the datastore could not be moved aside")
        self.ok_pass()
        self.assertEqual(self.held_by(), "fleet-role")
        self.assertIn("the datastore could not be moved aside", self.completed.stdout if self.completed else "")


class OutgoingTest(SwapCase):
    def test_with_the_candidate_voting_and_the_gates_holding_the_outgoing_voter_is_taken_out_starting_with_its_cordon_and_taint(self) -> None:
        self.at_candidate_uncordoned()
        self.cluster.runner_pods[OUT] = 1
        self.ok_pass()
        self.assertTrue(self.cordoned(OUT))
        self.assertEqual([t["key"] for t in self.taints(OUT)], [CONTROL_PLANE])
        self.assertNotIn(STEP_ANNOTATION, self.annotations(OUT), "the runner pod is waited for before the drain")

    def test_past_the_runner_wait_the_outgoing_voter_is_drained_labelled_etcd_only_and_asked_to_step_down(self) -> None:
        self.at_candidate_uncordoned()
        self.edit(OUT, cordoned=True, taint=True)
        self.cluster.runner_pods[OUT] = 1
        self.previous("evict", since_ago=RUNNER_WAIT_SECONDS + 5)
        self.ok_pass()
        self.assertEqual([w[1] for w in self.writes() if w[0] == "drain"], [OUT])
        self.assertEqual(self.labels(OUT)[LEVEL_LABEL], "etcd-only")
        self.assertEqual(self.labels(OUT)[DESIRED], "agent")
        self.assertEqual(self.annotations(OUT)[STEP_ANNOTATION], "step-down")
        self.assertEqual(self.annotations(OUT)[SWAP_ANNOTATION], self.id)

    def test_the_gates_are_held_with_four_voters_and_the_outgoing_voter_is_not_touched_while_one_fails(self) -> None:
        self.at_candidate_uncordoned()
        self.hosts[0].etcd = {"active_peers": 1}
        self.ok_pass()
        self.assertFalse(self.cordoned(OUT))
        self.assertEqual(self.changes(), [])

    def test_a_down_outgoing_voter_is_cordoned_and_marked_without_a_drain_and_the_gates_ignore_its_health(self) -> None:
        self.at_candidate_uncordoned()
        self.hosts[2].ready = False
        self.hosts[2].stale_by = 10 * SAMPLE_SECONDS
        for host in self.hosts[:2] + self.hosts[3:]:
            host.etcd = {"active_peers": 2}
        self.ok_pass()
        self.assertEqual([w for w in self.writes() if w[0] == "drain"], [])
        self.assertEqual(self.annotations(OUT)[STEP_ANNOTATION], "step-down")
        self.assertEqual(self.labels(OUT)[LEVEL_LABEL], "etcd-only")

    def test_the_removal_is_asked_once_the_actuator_reports_the_step_down_done(self) -> None:
        self.at_step_down_asked()
        self.report(OUT, self.id, "step-down")
        self.hosts[2].level = "etcd-only"
        self.ok_pass()
        self.assertEqual(self.annotations(OUT)[REMOVE], "true")

    def test_a_stepped_down_member_whose_node_no_longer_says_it_votes_is_still_counted_as_a_member_and_removed(self) -> None:
        # A server at etcd-only runs no kubelet, so its Node stops saying EtcdIsVoter while etcd still lists the member: the gates count it, ignore its health and let the removal go on.
        self.at_step_down_asked()
        self.report(OUT, self.id, "step-down")
        self.hosts[2].voter = False
        self.hosts[2].learner = True
        self.hosts[2].level = "etcd-only"
        self.hosts[2].ready = False
        self.hosts[2].stale_by = 10 * SAMPLE_SECONDS
        for host in self.hosts[:2] + self.hosts[3:]:
            host.etcd = {"known_members": TARGET + 1, "active_peers": TARGET - 1}
        self.ok_pass()
        self.assertEqual(self.annotations(OUT)[REMOVE], "true")
        self.assertEqual(self.cluster.status()["gates"], [])

    def test_the_removal_waits_for_the_step_down_while_the_outgoing_voter_is_ready_and_has_not_reported(self) -> None:
        self.at_step_down_asked()
        self.hosts[2].level = "etcd-only"
        self.previous("step-down", since_ago=OPERATION_TIMEOUT_SECONDS + 5)
        self.ok_pass()
        self.assertNotIn(REMOVE, self.annotations(OUT))

    def test_a_step_down_reported_failed_on_a_node_that_stays_ready_is_held_for_the_owner(self) -> None:
        self.at_step_down_asked()
        self.hosts[2].level = "etcd-only"
        self.report(OUT, self.id, "step-down", "failed", message="the server did not come back at etcd-only")
        self.ok_pass()
        self.assertNotIn(REMOVE, self.annotations(OUT))
        self.assertIn("held for the owner", self.completed.stdout if self.completed else "")

    def test_an_outgoing_voter_that_is_down_and_has_not_reported_in_the_operation_timeout_is_removed_while_the_others_are_healthy(self) -> None:
        self.at_step_down_asked()
        self.hosts[2].ready = False
        self.hosts[2].stale_by = 10 * SAMPLE_SECONDS
        self.hosts[2].level = "etcd-only"
        for host in self.hosts[:2] + self.hosts[3:]:
            host.etcd = {"active_peers": 2}
        self.previous("step-down", since_ago=OPERATION_TIMEOUT_SECONDS + 5)
        self.ok_pass()
        self.assertEqual(self.annotations(OUT)[REMOVE], "true")

    def test_a_down_outgoing_voter_is_not_removed_before_the_operation_timeout_has_passed(self) -> None:
        self.at_step_down_asked()
        self.hosts[2].ready = False
        self.hosts[2].stale_by = 10 * SAMPLE_SECONDS
        for host in self.hosts[:2] + self.hosts[3:]:
            host.etcd = {"active_peers": 2}
        self.previous("step-down", since_ago=OPERATION_TIMEOUT_SECONDS - 30)
        self.ok_pass()
        self.assertNotIn(REMOVE, self.annotations(OUT))

    def test_the_removal_is_held_while_a_remaining_voter_is_unhealthy(self) -> None:
        self.at_step_down_asked()
        self.report(OUT, self.id, "step-down")
        self.hosts[2].level = "etcd-only"
        self.hosts[1].stale_by = 10 * SAMPLE_SECONDS
        self.ok_pass()
        self.assertNotIn(REMOVE, self.annotations(OUT))
        self.assertIn("not removing c while the remaining voters are not all healthy", self.completed.stdout if self.completed else "")

    def test_a_removal_k3s_rejected_and_cleared_is_asked_for_again_while_the_others_are_healthy_and_not_otherwise(self) -> None:
        self.at_step_down_asked()
        self.report(OUT, self.id, "step-down")
        self.hosts[2].level = "etcd-only"
        self.previous("remove", since_ago=OPERATION_TIMEOUT_SECONDS + 5)
        self.hosts[1].stale_by = 10 * SAMPLE_SECONDS
        self.ok_pass()
        self.assertNotIn(REMOVE, self.annotations(OUT))
        self.hosts[1].stale_by = 0
        self.ok_pass()
        self.assertEqual(self.annotations(OUT)[REMOVE], "true")

    def test_a_removal_that_is_asked_for_and_not_yet_done_is_waited_for_without_another_write(self) -> None:
        self.at_removal_asked()
        self.hosts[2].level = "etcd-only"
        self.ok_pass()
        self.assertEqual(self.changes(), [])

    def test_once_k3s_has_recorded_the_removal_the_controller_waits_for_the_actuators_finish(self) -> None:
        self.at_member_removed()
        self.ok_pass()
        self.assertEqual(self.changes(), [])
        self.assertTrue(self.labels(OUT).get(VOTER_LABEL))

    def test_a_demotion_the_actuator_finished_is_cleaned_up_the_role_labels_etcd_annotations_level_label_and_taint(self) -> None:
        self.at_demotion_finished()
        self.edit(OUT, labels={MASTER: "true", CONTROL_PLANE: "true"}, annotations={"etcd.k3s.cattle.io/node-name": "c-1", "etcd.k3s.cattle.io/node-address": "100.64.0.3"})
        self.ok_pass()
        for key in (VOTER_LABEL, MASTER, CONTROL_PLANE, LEVEL_LABEL):
            self.assertNotIn(key, self.labels(OUT))
        self.assertEqual([k for k in self.annotations(OUT) if k.startswith("etcd.k3s.cattle.io/")], [])
        self.assertEqual(self.taints(OUT), [])
        self.assertTrue(self.cordoned(OUT), "it is uncordoned only once it is Ready as an agent")
        self.assertEqual(self.labels(OUT)[DESIRED], "agent", "the record of the role the host runs stays")
        self.assertEqual(self.annotations(OUT)[SWAP_ANNOTATION], self.id)

    def test_the_outgoing_node_is_uncordoned_once_ready_on_a_heartbeat_after_the_agent_started(self) -> None:
        self.at_demotion_finished()
        self.edit(OUT, labels={VOTER_LABEL: None, MASTER: None, CONTROL_PLANE: None, LEVEL_LABEL: None}, annotations={REMOVED: None}, taint=False)
        self.cluster.leases.pop("kube-system/fleet-regrade", None)
        self.hold()
        self.kubelet_lease(OUT, seconds_ago=3600)
        self.hosts[2].voter = False
        self.previous("cleanup")
        self.ok_pass()
        self.assertTrue(self.cordoned(OUT))
        self.kubelet_lease(OUT)
        self.ok_pass(repopulate=False)
        self.assertFalse(self.cordoned(OUT))

    def test_the_swap_ends_completed_the_annotations_cleared_the_lease_released(self) -> None:
        self.at_demotion_finished()
        self.edit(OUT, labels={VOTER_LABEL: None, MASTER: None, CONTROL_PLANE: None, LEVEL_LABEL: None}, annotations={REMOVED: None, MEMBER_GONE: self.id}, cordoned=False, taint=False)
        self.hosts[2].voter = False
        self.previous("cleanup")
        self.ok_pass()
        self.assertEqual(self.last_swap()["outcome"], "completed")
        self.assertEqual(self.held_by(), "")
        for name in "abcd":
            self.assertEqual([k for k in self.annotations(name) if k.startswith(DOMAIN)], [], name)
        self.assertIsNone(self.swap())
        self.assertNotIn(LEVEL_LABEL, self.labels(OUT))

    def test_a_host_that_does_not_come_back_in_the_operation_timeout_is_left_to_node_recovery_with_the_swap_recorded_pending(self) -> None:
        self.at_demotion_finished()
        self.edit(OUT, labels={VOTER_LABEL: None, MASTER: None, CONTROL_PLANE: None, LEVEL_LABEL: None}, annotations={REMOVED: None}, taint=False)
        self.hosts[2].voter = False
        self.hosts[2].ready = False
        self.previous("cleanup", since_ago=OPERATION_TIMEOUT_SECONDS + 5)
        self.ok_pass()
        self.assertTrue(self.swap()["outgoing_pending"])
        self.ok_pass(repopulate=False)
        self.assertEqual(self.last_swap()["outcome"], "completed")
        self.assertTrue(self.last_swap()["outgoing_pending"])
        self.assertEqual(self.held_by(), "")
        self.assertFalse(self.cordoned(OUT), "a node that is down is not left cordoned for when it returns")

    def test_an_actuator_that_does_not_finish_the_demotion_in_the_operation_timeout_does_not_hold_the_swap_for_ever(self) -> None:
        self.at_member_removed()
        self.previous("finish", since_ago=OPERATION_TIMEOUT_SECONDS + 5)
        self.ok_pass()
        self.assertNotIn(VOTER_LABEL, self.labels(OUT))
        self.assertTrue(self.swap()["outgoing_pending"])

    def at_removed_with_its_server_still_running(self) -> None:
        """The outgoing voter's step-down failed while it was not Ready and k3s removed its member, but the server its role file still names runs on: it has set the etcd label again, and the removed-node-name annotation is gone (k3s clears it when the server joins again under a new name, and the controller's clean-up takes it off at the finish's deadline). The voters that remain each report that etcd lists three members."""
        self.at_member_removed()
        self.report(OUT, self.id, "step-down", "failed", message="the server did not come back at etcd-only")
        self.edit(OUT, annotations={REMOVED: None})
        self.hosts[2].voter = False
        self.hosts[2].learner = True
        self.hosts[2].level = "etcd-only"
        self.hosts[2].ready = False
        self.hosts[2].stale_by = 10 * SAMPLE_SECONDS

    def test_a_member_etcd_no_longer_lists_is_finished_not_removed_again_though_its_node_says_it_is_one(self) -> None:
        self.at_removed_with_its_server_still_running()
        self.ok_pass()
        self.assertNotIn(REMOVE, self.annotations(OUT))
        status = self.cluster.status()
        self.assertEqual(status["gates"], [])
        self.assertEqual(status["membership"]["departed"], [OUT])
        self.assertEqual(self.swap()["step"], "finish")
        # The actuator is told, under this swap, that etcd lists none of its members, and the next pass waits for its finish with nothing more written.
        self.assertEqual(self.annotations(OUT)[MEMBER_GONE], self.id)
        self.ok_pass(repopulate=False)
        self.assertEqual(len([w for w in self.changes() if f"{MEMBER_GONE}={self.id}" in w]), 1)
        self.assertIn(f"waiting for {OUT}'s actuator to finish its demotion", self.completed.stdout if self.completed else "")

    def test_a_member_k3s_recorded_removed_is_marked_gone_once_every_remaining_voter_lists_only_the_voters(self) -> None:
        # k3s's record on the Node can name another member than the datastore's, so the actuator is told what etcd itself shows.
        self.at_member_removed()
        self.hosts[2].voter = False
        self.hosts[0].etcd = {"known_members": TARGET + 1}
        self.ok_pass()
        self.assertNotIn(MEMBER_GONE, self.annotations(OUT), "a remaining voter that lists the outgoing member is no proof")
        for host in self.hosts[:2] + self.hosts[3:]:
            host.etcd = {"known_members": TARGET, "active_peers": TARGET - 1}
        self.ok_pass()
        self.assertEqual(self.annotations(OUT)[MEMBER_GONE], self.id)

    def assert_still_counted_as_a_member(self) -> None:
        self.ok_pass()
        status = self.cluster.status()
        self.assertEqual(status["membership"]["departed"], [])
        self.assertNotEqual(status["gates"], [])
        self.assertNotIn(REMOVE, self.annotations(OUT), "nothing is removed while a remaining voter is not healthy")
        self.assertNotEqual(self.swap()["step"], "finish")

    def test_a_member_is_counted_as_one_while_a_remaining_voter_still_lists_it(self) -> None:
        self.at_removed_with_its_server_still_running()
        self.hosts[0].etcd = {"known_members": TARGET + 1}
        self.assert_still_counted_as_a_member()

    def test_a_member_is_counted_as_one_while_a_remaining_voter_has_not_reported(self) -> None:
        self.at_removed_with_its_server_still_running()
        self.hosts[0].stale_by = 10 * SAMPLE_SECONDS
        self.assert_still_counted_as_a_member()

    def test_a_departed_member_whose_server_never_finishes_ends_the_swap_completed_and_left_to_node_recovery(self) -> None:
        self.at_removed_with_its_server_still_running()
        self.edit(OUT, annotations={MEMBER_GONE: self.id})
        self.previous("finish", since_ago=OPERATION_TIMEOUT_SECONDS + 5)
        self.ok_pass()
        self.assertTrue(self.swap()["outgoing_pending"])
        # The server sets its etcd label again after the clean-up, so the Node is never clean; the swap ends rather than cleaning it for ever.
        self.previous("finish", since_ago=OPERATION_TIMEOUT_SECONDS + 5, outgoing_pending=True)
        self.ok_pass()
        self.assertEqual(self.last_swap()["outcome"], "completed")
        self.assertTrue(self.last_swap()["outgoing_pending"])
        self.assertEqual(self.held_by(), "")
        self.assertIsNone(self.swap())

    def test_the_removed_member_is_no_voter_so_the_membership_the_controller_publishes_has_three_voters(self) -> None:
        self.at_member_removed()
        self.ok_pass()
        self.assertEqual(self.cluster.status()["membership"]["voters"], ["a", "b", "d"])


class ResumeTest(SwapCase):
    """A controller pod that is rescheduled has the Lease, the Nodes and the reports and no status of its own, and takes the step the swap is at."""

    positions = (
        ("at_snapshot_taken", "the candidate is cordoned", lambda self: self.assertTrue(self.cordoned(IN))),
        ("at_promotion_asked", None, None),
        ("at_candidate_voting", "the candidate is uncordoned", lambda self: self.assertFalse(self.cordoned(IN))),
        ("at_candidate_uncordoned", "the outgoing voter is cordoned", lambda self: self.assertTrue(self.cordoned(OUT))),
        ("at_step_down_asked", None, None),
        ("at_removal_asked", None, None),
        ("at_demotion_finished", "the outgoing node is cleaned", lambda self: self.assertNotIn(VOTER_LABEL, self.labels(OUT))),
    )

    def test_a_pass_with_no_status_takes_the_step_the_nodes_and_reports_show_at_every_step(self) -> None:
        for builder, description, check in self.positions:
            with self.subTest(step=builder):
                self.setUp()
                getattr(self, builder)()
                self.cluster.set_status(None)
                self.ok_pass()
                self.assertEqual(self.held_by(), "fleet-role")
                self.assertEqual(self.cluster.status()["swap"]["id"], self.id)
                if check is not None:
                    check(self)

    def test_the_swap_is_known_from_its_annotations_alone_when_the_lease_and_the_status_are_both_gone(self) -> None:
        self.at_step_down_asked()
        self.cluster.set_status(None)
        self.cluster.leases.clear()
        self.ok_pass()
        self.assertEqual(self.held_by(), "fleet-role")
        self.assertEqual(self.cluster.leases[LEASE_KEY]["metadata"]["annotations"][LEASE_ID], self.id)
        self.assertEqual(self.cluster.leases[LEASE_KEY]["metadata"]["annotations"][LEASE_NODES], f"{OUT},{IN}")

    def test_the_lease_is_taken_again_with_the_swaps_annotations_when_it_expired_and_nobody_took_it_the_pass_renews_it(self) -> None:
        self.at_step_down_asked()
        self.hold(renewed_ago=LEASE_SECONDS + 100)
        self.ok_pass()
        renewed = self.cluster.leases[LEASE_KEY]
        self.assertEqual(renewed["spec"]["holderIdentity"], "fleet-role")
        self.assertEqual(renewed["metadata"]["annotations"][LEASE_ID], self.id)
        self.assertLess(time.time() - time.mktime(time.strptime(renewed["spec"]["renewTime"][:19], "%Y-%m-%dT%H:%M:%S")) + time.timezone, 30)

    def test_a_swap_that_ended_with_the_annotations_cleared_before_the_lease_was_released_is_ended_not_started_again(self) -> None:
        self.at_demotion_finished()
        self.edit(OUT, labels={VOTER_LABEL: None, MASTER: None, CONTROL_PLANE: None, LEVEL_LABEL: None}, annotations={REMOVED: None, SWAP_ANNOTATION: None, STEP_ANNOTATION: None}, cordoned=False, taint=False)
        self.edit(IN, annotations={SWAP_ANNOTATION: None, STEP_ANNOTATION: None, JOIN_ANNOTATION: None})
        self.hosts[2].voter = False
        self.cluster.set_status(None)
        self.ok_pass()
        self.assertEqual(self.held_by(), "")
        self.assertEqual(self.cluster.status()["last_swap"]["outcome"], "completed")
        self.assertEqual([w for w in self.writes() if f"{SNAPSHOT_ANNOTATION}=" in " ".join(w)], [], "no snapshot is asked for again")


class LeaseTest(SwapCase):
    def test_every_pass_renews_the_lease_the_swap_holds_and_keeps_its_annotations(self) -> None:
        self.at_step_down_asked()
        self.hold(renewed_ago=LEASE_SECONDS // 2)
        before = self.cluster.leases[LEASE_KEY]["spec"]["renewTime"]
        self.ok_pass()
        after = self.cluster.leases[LEASE_KEY]
        self.assertGreater(after["spec"]["renewTime"], before)
        self.assertEqual(after["metadata"]["annotations"][LEASE_ID], self.id)
        self.assertEqual(after["spec"]["acquireTime"], self.cluster.leases[LEASE_KEY]["spec"]["acquireTime"])

    def test_a_pass_with_no_swap_renews_and_takes_nothing(self) -> None:
        self.ok_pass()
        self.assertNotIn(LEASE_KEY, self.cluster.leases)
        self.assertEqual([w for w in self.writes() if w[0] in ("create", "replace")], [])

    def test_a_lease_that_regrade_took_mid_swap_is_never_acted_under_and_never_renewed(self) -> None:
        self.at_step_down_asked()
        self.cluster.leases[LEASE_KEY] = lease(None, holder="some-server")
        self.cluster.leases[LEASE_KEY]["metadata"]["annotations"] = {}
        self.hosts[2].level = "etcd-only"
        self.report(OUT, self.id, "step-down")
        self.ok_pass()
        self.assertEqual(self.held_by(), "some-server")
        self.assertEqual(self.changes(), [], "only the status is written")
        self.assertNotIn(REMOVE, self.annotations(OUT))
        self.assertEqual(self.cluster.status()["swap"]["id"], self.id)
        self.assertIn("the regrade Lease is held by some-server", self.completed.stdout if self.completed else "")

    def test_when_the_lease_that_regrade_took_has_expired_the_swap_takes_it_again_and_goes_on(self) -> None:
        self.at_removal_asked()
        self.cluster.leases[LEASE_KEY] = lease(None, holder="some-server", renewed_ago=LEASE_SECONDS + 10)
        self.hosts[2].level = "etcd-only"
        self.ok_pass()
        self.assertEqual(self.held_by(), "fleet-role")
        self.assertEqual(self.cluster.leases[LEASE_KEY]["metadata"]["annotations"][LEASE_ID], self.id)

    def test_an_even_count_is_finished_before_any_new_decision_whatever_the_dry_run_says(self) -> None:
        self.at_candidate_uncordoned()
        self.hosts[0].tcp = UNFIT_RTT_MS + 50
        self.ok_pass(FLEET_ROLE_DRY_RUN="true")
        self.assertTrue(self.cordoned(OUT), "the swap in progress goes on, with four voters, in a dry run")
        self.assertEqual(self.cluster.status()["swap"]["id"], self.id)
        self.assertNotIn("DRY RUN", self.completed.stdout if self.completed else "")

    def test_an_action_that_fails_is_repeated_and_leaves_the_swap_where_it_was(self) -> None:
        self.at_candidate_uncordoned()
        completed = self.run_pass(FAKE_FAIL_CALLS=f"cordon {OUT}")
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(self.cordoned(OUT))
        self.assertEqual(self.held_by(), "fleet-role")
        self.assertEqual(self.cluster.status()["swap"]["id"], self.id)
        self.ok_pass(repopulate=False)
        self.assertTrue(self.cordoned(OUT))


class StatusTest(SwapCase):
    def test_the_status_command_says_what_the_swap_is_doing(self) -> None:
        self.at_step_down_asked()
        self.ok_pass()
        completed = self.cluster.run("fleet-role.sh", "status", **CONTROLLER_ENV)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(f"swap {self.id}: c out, d in, step ", completed.stdout)
        self.assertIn("no request pending", completed.stdout)

    def test_the_status_carries_the_swap_the_last_swap_the_request_and_the_real_dry_run(self) -> None:
        self.ok_pass(FLEET_ROLE_DRY_RUN="false")
        status = self.cluster.status()
        for key in ("swap", "last_swap", "request", "dry_run"):
            self.assertIn(key, status)
        self.assertFalse(status["dry_run"])

    def test_the_swap_record_names_its_step_start_and_deadline_from_the_settings(self) -> None:
        self.at_step_down_asked()
        self.previous("step-down", since_ago=30)
        self.ok_pass()
        record = self.swap()
        self.assertEqual(record["step"], "step-down")
        self.assertEqual(record["deadline"] - record["step_since"], OPERATION_TIMEOUT_SECONDS)
        self.assertEqual(record["started_at"], int(time.mktime(time.strptime(self.id[:16], "%Y%m%dT%H%M%SZ"))) - time.timezone)
        self.assertEqual(set(record) >= {"id", "out", "in", "step", "started_at", "deadline", "manual", "snapshot"}, True)

    def test_the_step_since_is_kept_while_the_step_is_unchanged_and_reset_when_it_changes(self) -> None:
        self.at_step_down_asked()
        self.previous("step-down", since_ago=100)
        before = json.loads(self.cluster.configmaps[f"{NAMESPACE}/{STATUS_CONFIGMAP}"]["data"]["status.json"])["swap"]["step_since"]
        self.ok_pass()
        self.assertEqual(self.swap()["step_since"], before)
        self.previous("remove", since_ago=100)
        self.ok_pass(repopulate=False)
        self.assertGreater(self.swap()["step_since"], before)


class InterfaceTest(unittest.TestCase):
    def test_the_names_the_controller_writes_are_the_ones_the_actuator_reads(self) -> None:
        interface = json.loads(subprocess.run(["jq", "-n", "-c", "-L", str(SCRIPTS), 'include "fleet-role-swap"; interface'], capture_output=True, text=True, check=True).stdout)
        actuator = (ROOT / "roles" / "github_runner_cluster" / "files" / "role" / "fleet-role-actuator.sh").read_text()
        constants = dict(re.findall(r"^([A-Z_]+_(?:LABEL|ANNOTATION))=(\S+)$", actuator, re.MULTILINE))
        self.assertEqual(interface["desired_role_label"], constants["DESIRED_ROLE_LABEL"])
        self.assertEqual(interface["swap_annotation"], constants["SWAP_ANNOTATION"])
        self.assertEqual(interface["step_annotation"], constants["STEP_ANNOTATION"])
        self.assertEqual(interface["join_node_annotation"], constants["JOIN_NODE_ANNOTATION"])
        self.assertEqual(interface["snapshot_annotation"], constants["SNAPSHOT_ANNOTATION"])
        self.assertEqual(interface["member_gone_annotation"], constants["MEMBER_GONE_ANNOTATION"])
        self.assertEqual(interface["etcd_label"], constants["ETCD_LABEL"])
        self.assertEqual(interface["removed_annotation"], re.search(r"^REMOVED_ANNOTATION=(\S+)$", actuator, re.MULTILINE).group(1))  # type: ignore[union-attr]
        cluster = (ROOT / "roles" / "github_runner_cluster" / "defaults" / "main.yml").read_text()
        self.assertIn(f"github_runner_cluster_role_lease_holder: {interface['lease_holder']}", cluster)
        self.assertIn(f"github_runner_cluster_role_lease_swap_id_annotation: {interface['lease_swap_id_annotation']}", cluster)

    def test_the_actuators_valid_swap_id_shape_is_the_one_the_controller_makes(self) -> None:
        result = subprocess.run(["jq", "-n", "-r", "-L", str(SCRIPTS), 'include "fleet-role-swap"; swap_id("a-b"; "c"; 1767225600)'], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(result, "20260101T000000Z-a-b-c")
        self.assertRegex(result, r"^[0-9]{8}T[0-9]{6}Z-[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")


if __name__ == "__main__":
    unittest.main()
