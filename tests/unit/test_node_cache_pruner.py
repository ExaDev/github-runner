"""Tests for scripts/node-cache-pruner.sh: a cache over its limit is set aside store by store and deleted only while no unfinished runner pod that mounts it runs on the node, with stand-ins for kubectl and du that answer from scripted state and record what they are asked."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "node-cache-pruner.sh"
HOST_ROOT = "/var/lib/example/node-cache"

# `get pods` answers JSON for the pods in FAKE_PODS (a JSON list of {name, namespace, node, phase, volumes: [hostPath paths], from_call?}) that match its -n namespace and its --field-selector terms (spec.nodeName=<node> and status.phase!=<phase>, as kubectl evaluates them), counting that namespace's calls from 0 and leaving out a pod whose from_call is later. It fails for a namespace in FAKE_FAIL_PODS, answers a Forbidden error for one in FAKE_FORBIDDEN, and text that is not JSON for one in FAKE_BAD_JSON. Every call is recorded, with a snapshot of the cache root at that moment.
FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    state = os.environ["FAKE_STATE_DIR"]
    open(os.path.join(state, "calls.txt"), "a").write(" ".join(args) + "\\n")
    if args[:2] != ["get", "pods"]:
        sys.stderr.write("unexpected kubectl call: %s\\n" % args)
        sys.exit(2)
    namespace = args[args.index("-n") + 1]
    listed = lambda name: [item for item in os.environ.get(name, "").split(",") if item]
    if namespace in listed("FAKE_FAIL_PODS"):
        sys.stderr.write("the server is unavailable\\n")
        sys.exit(1)
    after = os.path.join(state, "calls-" + namespace)
    seen = int(open(after).read()) if os.path.exists(after) else 0
    if os.environ.get("FAKE_FAIL_AFTER") and seen >= int(os.environ["FAKE_FAIL_AFTER"]):
        sys.stderr.write("the server is unavailable\\n")
        sys.exit(1)
    if os.environ.get("FAKE_FORBID_AFTER") and seen >= int(os.environ["FAKE_FORBID_AFTER"]):
        sys.stderr.write('Error from server (Forbidden): pods is forbidden\\n')
        sys.exit(1)
    if namespace in listed("FAKE_FORBIDDEN"):
        sys.stderr.write('Error from server (Forbidden): pods is forbidden: User cannot list resource "pods" in the namespace\\n')
        sys.exit(1)
    counter = os.path.join(state, "calls-" + namespace)
    count = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(count + 1))
    root = os.environ["CACHE_ROOT"]
    snapshot = []
    for base, names, files in os.walk(root):
        snapshot.extend(os.path.relpath(os.path.join(base, name), root) for name in names + files)
    open(os.path.join(state, "snapshot-%s-%d" % (namespace, count)), "w").write(repr(sorted(snapshot)))
    if namespace in listed("FAKE_BAD_JSON"):
        print("not json")
        sys.exit(0)
    selector = args[args.index("--field-selector") + 1] if "--field-selector" in args else ""
    def matches(pod):
        for term in selector.split(","):
            if term.startswith("spec.nodeName=") and pod["node"] != term.split("=", 1)[1]:
                return False
            if term.startswith("status.phase!=") and pod["phase"] == term.split("!=", 1)[1]:
                return False
        return True
    items = []
    for pod in json.loads(os.environ.get("FAKE_PODS", "[]")):
        if pod["namespace"] == namespace and matches(pod) and count >= pod.get("from_call", 0):
            items.append({"metadata": {"name": pod["name"]}, "spec": {"volumes": [{"name": "v%d" % index, "hostPath": {"path": path}} for index, path in enumerate(pod["volumes"])]}})
    print(json.dumps({"items": items}))
    """
)

# du that fails for a path containing FAKE_DU_PATH (no output), or reads part of a tree there (FAKE_DU=partial, a size and a failure status), and otherwise is the real one.
FAKE_DU = textwrap.dedent(
    """\
    #!/bin/sh
    case "$2" in
      *"$FAKE_DU_PATH"*) if [ -n "$FAKE_DU_PATH" ]; then
        case "$FAKE_DU" in
          fail) echo "du: cannot access" >&2; exit 1;;
          partial) printf '%s\\t%s\\n' "$FAKE_DU_KIB" "$2"; echo "du: cannot access" >&2; exit 1;;
        esac
      fi;;
    esac
    exec "$REAL_DU" "$@"
    """
)

# The limit, in KiB, every case runs against, and the hard limit the script derives from it with HARD_LIMIT_FACTOR 2.
LIMIT_KIB = 64
OVER_KIB = 100
OVER_HARD_KIB = 200
UNDER_KIB = 16


def pod(profile: str, node: str = "node-a", phase: str = "Running", mounts: str | None = None, from_call: int = 0, name: str | None = None) -> dict[str, Any]:
    """A runner pod of the profile (<namespace>/<release>): on node, in phase, mounting the profile's directory unless mounts names another path (an empty string mounts none), appearing from the given list on."""
    namespace, release = profile.split("/")
    path = f"{HOST_ROOT}/{profile}" if mounts is None else mounts
    return {"name": name or f"runner-{release}-{node}-{phase.lower()}", "namespace": namespace, "node": node, "phase": phase, "volumes": [path] if path else [], "from_call": from_call}


class Run:
    """One run of the script: the cache root, what it printed, and what kubectl was asked."""

    def __init__(self, root: Path, state: Path, output: str) -> None:
        self.root = root
        self.state = state
        self.output = output

    def calls(self) -> list[str]:
        return (self.state / "calls.txt").read_text().splitlines()

    def profile(self, name: str) -> Path:
        return self.root / name

    def snapshot(self, namespace: str, call: int) -> list[str]:
        snapshot: list[str] = eval((self.state / f"snapshot-{namespace}-{call}").read_text())
        return snapshot


class PrunerTest(unittest.TestCase):
    def setup_work(self) -> tuple[Path, Path, Path]:
        work = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["chmod", "-R", "u+rwx", str(work)]) and shutil.rmtree(work, True))
        root = work / "root"
        root.mkdir()
        bin_dir = work / "bin"
        bin_dir.mkdir()
        for name, content in (("kubectl", FAKE_KUBECTL), ("du", FAKE_DU)):
            (bin_dir / name).write_text(content)
            (bin_dir / name).chmod(0o755)
        state = work / "state"
        state.mkdir()
        (state / "calls.txt").write_text("")
        return root, bin_dir, state

    def environment(self, root: Path, bin_dir: Path, state: Path, pods: list[dict[str, Any]] | None, wanted: list[str] | None, extra: dict[str, str]) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "REAL_DU": shutil.which("du") or "du",
            "NODE_NAME": "node-a",
            "HOST_CACHE_ROOT": HOST_ROOT,
            "CACHE_ROOT": str(root),
            "LIMIT_KIB": str(LIMIT_KIB),
            "PRUNE_ONCE": "true",
            "FAKE_STATE_DIR": str(state),
            "FAKE_PODS": json.dumps(pods or []),
            **({"WANTED_CACHES": " ".join(wanted)} if wanted is not None else {}),
            **extra,
        }

    def run_pruner(self, caches: dict[str, Any], pods: list[dict[str, Any]] | None = None, wanted: list[str] | None = None, **env: str) -> Run:
        """Run one check over caches (<namespace>/<release> mapped to the KiB of one file under a pnpm store and a dot-file beside it, or to a list of store names that hold that size each), with the given pods in the cluster."""
        root, bin_dir, state = self.setup_work()
        for name, size in caches.items():
            stores, size_kib = (size, OVER_KIB) if isinstance(size, list) else (["pnpm"], size)
            for store in stores:
                (root / name / store).mkdir(parents=True)
                (root / name / store / "blob").write_bytes(b"x" * (size_kib * 1024 // len(stores)))
            (root / name / ".hidden").write_text("kept with the rest")
        completed = subprocess.run(["bash", str(SCRIPT)], check=True, env=self.environment(root, bin_dir, state, pods, wanted, env), capture_output=True, text=True)
        return Run(root, state, completed.stdout)

    def test_a_cache_over_the_limit_with_no_runner_pod_mounting_it_is_emptied_and_its_directory_kept(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, wanted=["arc-a/main"])
        self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])
        self.assertEqual(list((run.root / ".trash").iterdir()), [])
        self.assertIn("emptied", run.output)

    def test_a_cache_over_the_limit_is_left_alone_while_a_runner_pod_mounting_it_runs_on_the_node(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, pods=[pod("arc-a/main")], wanted=["arc-a/main"])
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertIn("left alone", run.output)

    def test_a_pod_mounting_another_profiles_cache_in_the_same_namespace_does_not_protect_it(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, pods=[pod("arc-a/main", mounts=f"{HOST_ROOT}/arc-a/another")], wanted=["arc-a/main"])
        self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])

    def test_a_pod_that_does_not_mount_the_cache_does_not_protect_it(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, pods=[pod("arc-a/main", mounts="")], wanted=["arc-a/main"])
        self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])

    def test_a_pod_of_another_namespace_does_not_protect_a_cache(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB, "arc-b/main": OVER_KIB}, pods=[pod("arc-b/main")], wanted=["arc-a/main", "arc-b/main"])
        self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])
        self.assertTrue((run.profile("arc-b/main") / "pnpm" / "blob").exists())

    def test_finished_pods_are_left_out_of_the_list_by_the_field_selector(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, wanted=["arc-a/main"])
        self.assertEqual(run.calls()[0], "get pods -n arc-a --field-selector spec.nodeName=node-a,status.phase!=Succeeded,status.phase!=Failed -o json")

    def test_a_finished_pod_does_not_protect_a_cache(self) -> None:
        for phase in ("Succeeded", "Failed"):
            with self.subTest(phase):
                run = self.run_pruner({"arc-a/main": OVER_KIB}, pods=[pod("arc-a/main", phase=phase)], wanted=["arc-a/main"])
                self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])

    def test_a_pending_pod_protects_a_cache(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, pods=[pod("arc-a/main", phase="Pending")], wanted=["arc-a/main"])
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())

    def test_a_pod_on_another_node_does_not_protect_a_cache(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, pods=[pod("arc-a/main", node="node-b")], wanted=["arc-a/main"])
        self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])

    def test_a_namespace_the_pruner_may_not_list_is_reported_on_every_round_and_left_alone(self) -> None:
        # A profile removed from the fleet has no Role, so the pods cannot be listed; the other profiles are unaffected.
        run = self.run_pruner({"arc-gone/old": OVER_KIB, "arc-a/main": OVER_KIB}, wanted=["arc-a/main"], FAKE_FORBIDDEN="arc-gone")
        self.assertTrue((run.profile("arc-gone/old") / "pnpm" / "blob").exists())
        self.assertIn("NODE_CACHE_UNPRUNABLE arc-gone/old: the runner pods on this node could not be listed", run.output)
        self.assertNotIn("NODE_CACHE_LIST_FAILED", run.output)
        self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])

    def test_a_partly_set_aside_cache_is_reported_as_partly_emptied_not_emptied(self) -> None:
        run = self.run_pruner({"arc-a/main": ["a-store", "b-store"]}, pods=[pod("arc-a/main", from_call=3)], wanted=["arc-a/main"])
        self.assertIn("part of it was set aside and deleted, the rest is left in place", run.output)
        self.assertNotIn(": emptied", run.output)

    def test_a_cache_nothing_of_which_could_be_set_aside_is_reported_as_untouched(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, pods=[pod("arc-a/main", from_call=1)], wanted=["arc-a/main"])
        self.assertIn("nothing could be set aside; left as it is", run.output)
        self.assertNotIn(": emptied", run.output)

    def test_a_busy_unwanted_directory_is_reported_as_such_not_as_over_the_limit(self) -> None:
        run = self.run_pruner({"arc-gone/old": UNDER_KIB}, pods=[pod("arc-gone/old")], wanted=[])
        self.assertIn("no longer wanted and holds", run.output)
        self.assertNotIn("over the limit", run.output)

    def test_a_pod_that_comes_before_an_unwanted_directory_is_removed_keeps_the_directory(self) -> None:
        # Calls: the check, one before each of the two entries (.hidden, pnpm), then one before the directory is removed.
        run = self.run_pruner({"arc-gone/old": UNDER_KIB}, pods=[pod("arc-gone/old", from_call=3)], wanted=[])
        self.assertTrue((run.root / "arc-gone" / "old").is_dir())
        self.assertEqual(list((run.root / "arc-gone" / "old").iterdir()), [])
        self.assertEqual(run.output.count("arc-gone/old"), 1, run.output)
        self.assertIn("deleted, but its directory is kept because a runner pod may mount it now", run.output)
        self.assertNotIn("directory removed", run.output)

    def test_a_cache_under_the_limit_is_left_alone_and_no_pod_is_listed(self) -> None:
        run = self.run_pruner({"arc-a/main": UNDER_KIB}, wanted=["arc-a/main"])
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertEqual(run.calls(), [])
        self.assertEqual(run.output, "")

    def test_each_profile_is_asked_about_in_its_own_namespace_on_this_node(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB, "arc-b/other": OVER_KIB}, wanted=["arc-a/main", "arc-b/other"])
        namespaces = {call.split()[3] for call in run.calls()}
        self.assertEqual(namespaces, {"arc-a", "arc-b"})

    def test_a_root_with_no_caches_is_not_an_error(self) -> None:
        self.assertEqual(self.run_pruner({}).output, "")

    def test_a_failed_size_reading_skips_only_that_profile(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB, "arc-b/main": OVER_KIB}, wanted=["arc-a/main", "arc-b/main"], FAKE_DU="fail", FAKE_DU_PATH="arc-a/main")
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertEqual(list(run.profile("arc-b/main").iterdir()), [])
        self.assertIn("arc-a/main: its size could not be read", run.output)

    def test_a_partial_size_reading_is_used_as_read(self) -> None:
        run = self.run_pruner({"arc-a/main": UNDER_KIB}, wanted=["arc-a/main"], FAKE_DU="partial", FAKE_DU_PATH="arc-a/main", FAKE_DU_KIB=str(OVER_KIB))
        self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])

    def test_a_failed_pod_list_means_no_pruning_for_that_profile_only(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB, "arc-b/main": OVER_KIB}, wanted=["arc-a/main", "arc-b/main"], FAKE_FAIL_PODS="arc-a")
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertEqual(list(run.profile("arc-b/main").iterdir()), [])
        self.assertIn("NODE_CACHE_LIST_FAILED arc-a/main: the runner pods on this node could not be listed", run.output)
        self.assertNotIn("NODE_CACHE_UNPRUNABLE", run.output)

    def test_an_answer_that_is_not_a_pod_list_means_no_pruning_and_is_not_called_unprunable(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, wanted=["arc-a/main"], FAKE_BAD_JSON="arc-a")
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertIn("NODE_CACHE_LIST_FAILED arc-a/main", run.output)
        self.assertNotIn("NODE_CACHE_UNPRUNABLE", run.output)

    def test_one_profiles_forbidden_list_does_not_colour_the_next_profiles_transient_failure(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB, "arc-b/main": OVER_KIB}, wanted=["arc-a/main", "arc-b/main"], FAKE_FORBIDDEN="arc-a", FAKE_FAIL_PODS="arc-b")
        self.assertIn("NODE_CACHE_UNPRUNABLE arc-a/main", run.output)
        self.assertIn("NODE_CACHE_LIST_FAILED arc-b/main", run.output)
        self.assertNotIn("NODE_CACHE_UNPRUNABLE arc-b/main", run.output)

    def test_a_list_that_fails_part_way_through_the_moving_is_named_by_why_it_failed(self) -> None:
        # The first list (before the entries move) succeeds; the next ones are refused.
        for variable, prefix, other in (("FAKE_FAIL_AFTER", "NODE_CACHE_LIST_FAILED", "NODE_CACHE_UNPRUNABLE"), ("FAKE_FORBID_AFTER", "NODE_CACHE_UNPRUNABLE", "NODE_CACHE_LIST_FAILED")):
            with self.subTest(variable):
                run = self.run_pruner({"arc-a/main": ["a-store", "b-store"]}, wanted=["arc-a/main"], **{variable: "1"})
                self.assertIn(f"{prefix} arc-a/main: the runner pods could not be listed part way through", run.output)
                self.assertNotIn(other, run.output)
                self.assertTrue((run.profile("arc-a/main") / "a-store" / "blob").exists())

    def test_a_pod_that_appears_before_the_first_rename_leaves_the_whole_cache_in_place(self) -> None:
        run = self.run_pruner({"arc-a/main": ["a-store", "b-store"]}, pods=[pod("arc-a/main", from_call=1)], wanted=["arc-a/main"])
        self.assertTrue((run.profile("arc-a/main") / "a-store" / "blob").exists())
        self.assertTrue((run.profile("arc-a/main") / "b-store" / "blob").exists())
        self.assertIn("started while the cache was being set aside", run.output)

    def test_a_pod_that_appears_part_way_leaves_the_stores_not_yet_moved_whole(self) -> None:
        # Entries are moved in name order: .hidden, a-store, b-store. The pod appears at the third list, before a-store moves.
        run = self.run_pruner({"arc-a/main": ["a-store", "b-store"]}, pods=[pod("arc-a/main", from_call=3)], wanted=["arc-a/main"])
        remaining = sorted(path.name for path in run.profile("arc-a/main").iterdir())
        self.assertEqual(remaining, ["b-store"])
        self.assertTrue((run.profile("arc-a/main") / "b-store" / "blob").exists())
        self.assertEqual(list((run.root / ".trash").iterdir()), [])

    def test_the_cache_moves_one_entry_at_a_time_and_nothing_is_deleted_until_the_moving_is_done(self) -> None:
        run = self.run_pruner({"arc-a/main": ["a-store", "b-store"]}, wanted=["arc-a/main"])
        # The list before the last rename (the fourth call: one at the check, then one per entry) sees the earlier entries in the trash, whole, and the last still in place.
        before_last = run.snapshot("arc-a", 3)
        self.assertIn("arc-a/main/b-store", before_last)
        self.assertTrue(any(path.endswith("a-store/blob") and path.startswith(".trash/") for path in before_last), before_last)
        self.assertFalse(any(path.endswith("b-store/blob") and path.startswith(".trash/") for path in before_last), before_last)

    def test_a_trash_left_by_an_earlier_round_is_deleted(self) -> None:
        root, bin_dir, state = self.setup_work()
        (root / "arc-a" / "main").mkdir(parents=True)
        (root / ".trash" / "set-aside.stale").mkdir(parents=True)
        (root / ".trash" / "set-aside.stale" / "blob").write_text("x")
        subprocess.run(["bash", str(SCRIPT)], check=True, env=self.environment(root, bin_dir, state, None, ["arc-a/main"], {}), capture_output=True, text=True)
        self.assertEqual(list((root / ".trash").iterdir()), [])

    def test_a_set_aside_copy_that_cannot_be_deleted_is_reported_and_cleared_by_the_next_round(self) -> None:
        root, bin_dir, state = self.setup_work()
        (root / "arc-a" / "main" / "pnpm" / "locked").mkdir(parents=True)
        (root / "arc-a" / "main" / "pnpm" / "locked" / "blob").write_text("x")
        (root / "arc-a" / "main" / "pnpm" / "big").write_bytes(b"x" * OVER_KIB * 1024)
        (root / "arc-a" / "main" / "pnpm" / "locked").chmod(0o000)
        env = self.environment(root, bin_dir, state, None, ["arc-a/main"], {})
        first = subprocess.run(["bash", str(SCRIPT)], check=True, env=env, capture_output=True, text=True)
        self.assertIn("could not be deleted; the next round retries", first.stdout)
        self.assertEqual(list((root / "arc-a" / "main").iterdir()), [])
        self.assertEqual(len(list((root / ".trash").iterdir())), 1)
        subprocess.run(["chmod", "-R", "u+rwx", str(root)], check=True)
        subprocess.run(["bash", str(SCRIPT)], check=True, env=env, capture_output=True, text=True)
        self.assertEqual(list((root / ".trash").iterdir()), [])

    def test_set_aside_copies_left_by_failed_deletes_do_not_collide_across_profiles(self) -> None:
        # Both profiles' copies are undeletable, so both stay in the trash; whichever is set aside second must not share a directory with the first, and profile names that join alike (a/b-c, a-b/c) are the easiest to make share one.
        root, bin_dir, state = self.setup_work()
        for name in ("a/b-c", "a-b/c"):
            (root / name / "pnpm").mkdir(parents=True)
            (root / name / "pnpm" / "big").write_bytes(b"x" * OVER_KIB * 1024)
            (root / name / "pnpm" / "locked").mkdir()
            (root / name / "pnpm" / "locked" / "blob").write_text("x")
            (root / name / "pnpm" / "locked").chmod(0o000)
        completed = subprocess.run(["bash", str(SCRIPT)], check=True, env=self.environment(root, bin_dir, state, None, ["a/b-c", "a-b/c"], {}), capture_output=True, text=True)
        self.assertEqual(list((root / "a-b" / "c").iterdir()), [], completed.stdout)
        self.assertEqual(list((root / "a" / "b-c").iterdir()), [], completed.stdout)

    def test_a_busy_cache_over_the_hard_limit_is_reported_with_a_stable_prefix_and_nothing_else_is_done(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_HARD_KIB}, pods=[pod("arc-a/main")], wanted=["arc-a/main"])
        self.assertIn("NODE_CACHE_OVER_HARD_LIMIT arc-a/main size_kib=", run.output)
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertEqual({call.split()[0] + " " + call.split()[1] for call in run.calls()}, {"get pods"})

    def test_a_busy_cache_between_the_limit_and_the_hard_limit_is_not_reported_as_over_it(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, pods=[pod("arc-a/main")], wanted=["arc-a/main"])
        self.assertNotIn("NODE_CACHE_OVER_HARD_LIMIT", run.output)

    def test_a_directory_that_is_no_longer_wanted_is_removed_when_no_pod_mounts_it(self) -> None:
        run = self.run_pruner({"arc-a/main": UNDER_KIB, "arc-gone/old": UNDER_KIB}, wanted=["arc-a/main"])
        self.assertFalse((run.root / "arc-gone").exists())
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertEqual(run.output.count("arc-gone/old"), 1, run.output)
        self.assertIn("arc-gone/old is no longer wanted and held", run.output)
        self.assertIn("deleted, and its directory removed", run.output)
        self.assertNotIn("kept", run.output)

    def test_an_unwanted_directory_with_nothing_in_it_is_removed(self) -> None:
        root, bin_dir, state = self.setup_work()
        (root / "arc-gone" / "old").mkdir(parents=True)
        subprocess.run(["bash", str(SCRIPT)], check=True, env=self.environment(root, bin_dir, state, None, [], {}), capture_output=True, text=True)
        self.assertFalse((root / "arc-gone").exists())

    def test_removing_one_unwanted_profile_does_not_stop_the_removal_of_the_next_in_its_namespace(self) -> None:
        # The namespace directory cannot be removed while another profile still sits in it, which must not end the round.
        run = self.run_pruner({"arc-gone/old1": UNDER_KIB, "arc-gone/old2": UNDER_KIB, "arc-a/main": UNDER_KIB}, wanted=["arc-a/main"])
        self.assertFalse((run.root / "arc-gone").exists())

    def test_a_directory_that_is_no_longer_wanted_is_kept_while_a_pod_mounts_it(self) -> None:
        run = self.run_pruner({"arc-gone/old": UNDER_KIB}, pods=[pod("arc-gone/old")], wanted=[])
        self.assertTrue((run.profile("arc-gone/old") / "pnpm" / "blob").exists())

    def test_without_a_wanted_list_no_directory_is_removed_for_being_unwanted(self) -> None:
        run = self.run_pruner({"arc-a/main": UNDER_KIB})
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())


if __name__ == "__main__":
    unittest.main()
