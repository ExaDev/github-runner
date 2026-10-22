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

# `get autoscalingrunnersets.actions.github.com` answers JSON for the scale sets in FAKE_SCALE_SETS (a JSON list of {namespace, store_keys}) of its -n namespace, and fails for a namespace in FAKE_FAIL_SCALE_SETS. `get pods` answers JSON for the pods in FAKE_PODS (a JSON list of {name, namespace, node, phase, volumes: [hostPath paths], from_call?, uid?, store_keys?}) that match its -n namespace and its --field-selector terms (spec.nodeName=<node> and status.phase!=<phase>, as kubectl evaluates them), counting that namespace's calls from 0 and leaving out a pod whose from_call is later. It fails for a namespace in FAKE_FAIL_PODS, answers a Forbidden error for one in FAKE_FORBIDDEN, and text that is not JSON for one in FAKE_BAD_JSON. Every call is recorded, with a snapshot of the cache root at that moment.
FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    state = os.environ["FAKE_STATE_DIR"]
    open(os.path.join(state, "calls.txt"), "a").write(" ".join(args) + "\\n")
    listed = lambda name: [item for item in os.environ.get(name, "").split(",") if item]
    annotations = lambda keys: {"github-runner.exadev.io/capability-store-keys": " ".join(keys)} if keys else {}
    if args[:2] == ["get", "autoscalingrunnersets.actions.github.com"]:
        namespace = args[args.index("-n") + 1]
        if namespace in listed("FAKE_FAIL_SCALE_SETS"):
            sys.stderr.write("the server is unavailable\\n")
            sys.exit(1)
        scale_sets = [item for item in json.loads(os.environ.get("FAKE_SCALE_SETS", "[]")) if item["namespace"] == namespace]
        print(json.dumps({"items": [{"spec": {"template": {"metadata": {"annotations": annotations(item["store_keys"])}}}} for item in scale_sets]}))
        sys.exit(0)
    if args[:2] != ["get", "pods"]:
        sys.stderr.write("unexpected kubectl call: %s\\n" % args)
        sys.exit(2)
    namespace = args[args.index("-n") + 1]
    listed = lambda name: [item for item in os.environ.get(name, "").split(",") if item]
    if namespace in listed("FAKE_FAIL_PODS"):
        sys.stderr.write(os.environ.get("FAKE_ERROR_TEXT", "the server is unavailable") + "\\n")
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
            metadata = {"name": pod["name"], "uid": pod.get("uid", pod["name"]), "annotations": annotations(pod.get("store_keys", []))}
            if pod.get("deleting"):
                metadata["deletionTimestamp"] = "2026-01-01T00:00:00Z"
            items.append({"metadata": metadata, "spec": {"volumes": [{"name": "v%d" % index, "hostPath": {"path": path}} for index, path in enumerate(pod["volumes"])]}})
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

# rmdir that fails as scripted by FAKE_RMDIR_FAIL, and otherwise is the real one. "notempty" is a directory something wrote to after the pruner emptied it: the stub writes a file into it and then fails as rmdir does on a directory with something in it. Any other value is a failure for another reason, leaving the directory empty, with that text as rmdir's message.
FAKE_RMDIR = textwrap.dedent(
    """\
    #!/bin/sh
    case "$FAKE_RMDIR_FAIL" in
      "") exec "$REAL_RMDIR" "$@";;
      notempty) touch "$1/written-late"; echo "rmdir: failed to remove '$1': Directory not empty" >&2; exit 1;;
      *) echo "rmdir: failed to remove '$1': $FAKE_RMDIR_FAIL" >&2; exit 1;;
    esac
    """
)

# The limit, in KiB, every case runs against, and the hard limit the script derives from it with HARD_LIMIT_FACTOR 2.
LIMIT_KIB = 64
OVER_KIB = 100
OVER_HARD_KIB = 200
UNDER_KIB = 16


def pod(profile: str, node: str = "node-a", phase: str = "Running", mounts: str | None = None, from_call: int = 0, name: str | None = None, deleting: bool = False) -> dict[str, Any]:
    """A runner pod of the profile (<namespace>/<release>): on node, in phase, mounting the profile's directory unless mounts names another path (an empty string mounts none), appearing from the given list on."""
    namespace, release = profile.split("/")
    path = f"{HOST_ROOT}/{profile}" if mounts is None else mounts
    return {"name": name or f"runner-{release}-{node}-{phase.lower()}", "namespace": namespace, "node": node, "phase": phase, "volumes": [path] if path else [], "from_call": from_call, "deleting": deleting}


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
        self.addCleanup(lambda: subprocess.run(["chmod", "-R", "u+rwx", str(work)], check=False) and shutil.rmtree(work, True))
        root = work / "root"
        root.mkdir()
        bin_dir = work / "bin"
        bin_dir.mkdir()
        for name, content in (("kubectl", FAKE_KUBECTL), ("du", FAKE_DU), ("rmdir", FAKE_RMDIR)):
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
            "REAL_RMDIR": shutil.which("rmdir") or "rmdir",
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
        self.assertRegex(run.output, r"NODE_CACHE_UNPRUNABLE arc-gone/old size_kib=\d+: the runner pods on this node could not be listed")
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
        self.assertRegex(run.output, r"NODE_CACHE_LIST_FAILED arc-a/main size_kib=\d+: the runner pods on this node could not be listed")
        self.assertNotIn("NODE_CACHE_UNPRUNABLE", run.output)

    def test_an_answer_that_is_not_a_pod_list_means_no_pruning_and_is_not_called_unprunable(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, wanted=["arc-a/main"], FAKE_BAD_JSON="arc-a")
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertIn("NODE_CACHE_LIST_FAILED arc-a/main size_kib=", run.output)
        self.assertNotIn("NODE_CACHE_UNPRUNABLE", run.output)

    def test_one_profiles_forbidden_list_does_not_colour_the_next_profiles_transient_failure(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB, "arc-b/main": OVER_KIB}, wanted=["arc-a/main", "arc-b/main"], FAKE_FORBIDDEN="arc-a", FAKE_FAIL_PODS="arc-b")
        self.assertIn("NODE_CACHE_UNPRUNABLE arc-a/main size_kib=", run.output)
        self.assertIn("NODE_CACHE_LIST_FAILED arc-b/main size_kib=", run.output)
        self.assertNotIn("NODE_CACHE_UNPRUNABLE arc-b/main", run.output)

    def test_a_failed_list_for_a_cache_over_the_hard_limit_still_reports_that_it_is_over_it(self) -> None:
        for variable, prefix in (("FAKE_FAIL_PODS", "NODE_CACHE_LIST_FAILED"), ("FAKE_FORBIDDEN", "NODE_CACHE_UNPRUNABLE")):
            with self.subTest(variable):
                run = self.run_pruner({"arc-a/main": OVER_HARD_KIB}, wanted=["arc-a/main"], **{variable: "arc-a"})
                self.assertIn(f"{prefix} arc-a/main size_kib=", run.output)
                self.assertRegex(run.output, r"NODE_CACHE_OVER_HARD_LIMIT arc-a/main size_kib=\d+ hard_limit_kib=\d+ node=node-a: whether a runner pod keeps it busy is unknown")

    def test_a_failed_list_for_a_cache_under_the_hard_limit_does_not_claim_it_is_over_it(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, wanted=["arc-a/main"], FAKE_FAIL_PODS="arc-a")
        self.assertNotIn("NODE_CACHE_OVER_HARD_LIMIT", run.output)

    def test_an_error_that_merely_mentions_forbidden_is_a_failed_list_not_a_refusal(self) -> None:
        for message in ("Unable to connect to the server: proxy says forbidden", "error: the user Forbidden-Admin could not be found", "Error from server (Forbidden-ish): x"):
            with self.subTest(message):
                run = self.run_pruner({"arc-a/main": OVER_KIB}, wanted=["arc-a/main"], FAKE_ERROR_TEXT=message, FAKE_FAIL_PODS="arc-a")
                self.assertIn("NODE_CACHE_LIST_FAILED arc-a/main", run.output)
                self.assertNotIn("NODE_CACHE_UNPRUNABLE", run.output)

    def test_kubectls_own_refusal_form_is_a_refusal_even_with_more_text_after_it(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, wanted=["arc-a/main"], FAKE_ERROR_TEXT="Error from server (Forbidden): pods is forbidden: User \"system:serviceaccount:ns:node-cache-pruner\" cannot list resource", FAKE_FAIL_PODS="arc-a")
        self.assertIn("NODE_CACHE_UNPRUNABLE arc-a/main", run.output)

    def test_a_list_that_fails_part_way_through_the_moving_is_named_by_why_it_failed(self) -> None:
        # The first list (before the entries move) succeeds; the next ones are refused.
        for variable, prefix, other in (("FAKE_FAIL_AFTER", "NODE_CACHE_LIST_FAILED", "NODE_CACHE_UNPRUNABLE"), ("FAKE_FORBID_AFTER", "NODE_CACHE_UNPRUNABLE", "NODE_CACHE_LIST_FAILED")):
            with self.subTest(variable):
                run = self.run_pruner({"arc-a/main": ["a-store", "b-store"]}, wanted=["arc-a/main"], **{variable: "1"})
                self.assertRegex(run.output, rf"{prefix} arc-a/main size_kib=\d+: the runner pods could not be listed part way through")
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

    def test_a_failed_list_before_an_unwanted_directory_is_removed_names_why_and_does_not_blame_a_pod(self) -> None:
        # Calls: the check, one before each of the two entries (.hidden, pnpm), then the one before the directory is removed, which fails.
        for variable, prefix, other in (("FAKE_FAIL_AFTER", "NODE_CACHE_LIST_FAILED", "NODE_CACHE_UNPRUNABLE"), ("FAKE_FORBID_AFTER", "NODE_CACHE_UNPRUNABLE", "NODE_CACHE_LIST_FAILED")):
            with self.subTest(variable):
                run = self.run_pruner({"arc-gone/old": UNDER_KIB}, wanted=[], **{variable: "3"})
                self.assertTrue((run.root / "arc-gone" / "old").is_dir())
                self.assertEqual(list((run.root / "arc-gone" / "old").iterdir()), [])
                self.assertRegex(run.output, rf"{prefix} arc-gone/old size_kib=\d+: deleted, but the runner pods could not be listed, so its directory is kept")
                self.assertNotIn(other, run.output)
                self.assertNotIn("may mount it now", run.output)
                self.assertEqual(run.output.count("arc-gone/old"), 1, run.output)

    def test_an_unwanted_directory_something_was_written_to_after_it_was_emptied_is_kept_and_says_why(self) -> None:
        run = self.run_pruner({"arc-gone/old": UNDER_KIB}, wanted=[], FAKE_RMDIR_FAIL="notempty")
        self.assertTrue((run.root / "arc-gone" / "old" / "written-late").exists())
        self.assertEqual(run.output.count("arc-gone/old"), 1, run.output)
        self.assertIn("deleted, but its directory is kept because it is not empty", run.output)
        self.assertNotIn("directory removed", run.output)
        self.assertNotIn("may mount it now", run.output)
        self.assertNotIn("could not be removed", run.output)

    def test_an_unwanted_directory_that_rmdir_cannot_remove_for_another_reason_reports_rmdirs_error(self) -> None:
        run = self.run_pruner({"arc-gone/old": UNDER_KIB}, wanted=[], FAKE_RMDIR_FAIL="Device or resource busy")
        self.assertTrue((run.root / "arc-gone" / "old").is_dir())
        self.assertEqual(list((run.root / "arc-gone" / "old").iterdir()), [])
        self.assertEqual(len(run.output.splitlines()), 1, run.output)
        self.assertRegex(run.output, r"deleted, but its directory could not be removed: rmdir: failed to remove '[^']+': Device or resource busy")
        self.assertNotIn("not empty", run.output)

    def test_an_unwanted_directory_with_nothing_in_it_is_removed_by_a_pruner_running_on_bash_3(self) -> None:
        # An empty array is an unbound variable under set -u before bash 4.4; macOS ships bash 3.2, so where an old bash is present the empty-entries case runs on it.
        for candidate in ("/bin/bash", "/usr/bin/bash"):
            if not Path(candidate).exists():
                continue
            version = subprocess.run([candidate, "-c", "echo ${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"], capture_output=True, text=True, check=False).stdout.split()
            if len(version) == 2 and (int(version[0]), int(version[1])) < (4, 4):
                root, bin_dir, state = self.setup_work()
                (root / "arc-gone" / "old").mkdir(parents=True)
                completed = subprocess.run([candidate, str(SCRIPT)], env=self.environment(root, bin_dir, state, None, [], {}), capture_output=True, text=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertFalse((root / "arc-gone").exists())
                return
        self.skipTest("no bash older than 4.4 here")

    def test_the_entries_are_expanded_in_a_way_that_is_safe_for_an_empty_array_on_any_bash(self) -> None:
        text = SCRIPT.read_text()
        self.assertIn('${entries[@]+"${entries[@]}"}', text)
        self.assertNotIn('in "${entries[@]}"', text)

    def test_a_pod_being_deleted_but_still_running_counts_as_using_the_cache(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, pods=[pod("arc-a/main", deleting=True)], wanted=["arc-a/main"])
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertIn("left alone", run.output)

    def test_a_directory_that_is_no_longer_wanted_is_kept_while_a_pod_mounts_it(self) -> None:
        run = self.run_pruner({"arc-gone/old": UNDER_KIB}, pods=[pod("arc-gone/old")], wanted=[])
        self.assertTrue((run.profile("arc-gone/old") / "pnpm" / "blob").exists())

    def test_without_a_wanted_list_no_directory_is_removed_for_being_unwanted(self) -> None:
        run = self.run_pruner({"arc-a/main": UNDER_KIB})
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())


class StartupTest(unittest.TestCase):
    """The settings the pruner checks before it does anything."""

    def start(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        root = work / "root"
        (root / "arc-a" / "main" / "pnpm").mkdir(parents=True)
        (root / "arc-a" / "main" / "pnpm" / "blob").write_bytes(b"x" * OVER_KIB * 1024)
        bin_dir = work / "bin"
        bin_dir.mkdir()
        (bin_dir / "kubectl").write_text("#!/bin/sh\necho called >> \"$FAKE_CALLS\"\nexit 1\n")
        (bin_dir / "kubectl").chmod(0o755)
        self.calls = work / "calls"
        self.root = root
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "NODE_NAME": "node-a", "HOST_CACHE_ROOT": HOST_ROOT, "CACHE_ROOT": str(root), "LIMIT_KIB": str(LIMIT_KIB), "PRUNE_ONCE": "true", "FAKE_CALLS": str(self.calls), "WANTED_CACHES": "arc-a/main"}
        for name in overrides:
            env.pop(name, None)
        env.update({name: value for name, value in overrides.items() if value is not None})
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)

    def assert_refused(self, completed: subprocess.CompletedProcess[str], name: str) -> None:
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertIn(f"node-cache-pruner: {name} must be a positive integer", completed.stderr)
        self.assertFalse(self.calls.exists(), "kubectl was called")
        self.assertTrue((self.root / "arc-a" / "main" / "pnpm" / "blob").exists(), "the cache was touched")

    def test_a_limit_that_is_not_a_positive_integer_is_refused_before_anything_is_done(self) -> None:
        for value in ("0", "-1", "", "abc", "1.5", "01", " 5", "5 "):
            with self.subTest(f"LIMIT_KIB={value!r}"):
                self.assert_refused(self.start(LIMIT_KIB=value), "LIMIT_KIB")
            with self.subTest(f"LIMIT_GIB={value!r}"):
                completed = self.start(LIMIT_KIB=None, LIMIT_GIB=value)  # type: ignore[arg-type]
                self.assert_refused(completed, "LIMIT_GIB")

    def test_a_missing_limit_is_refused(self) -> None:
        completed = self.start(LIMIT_KIB=None)  # type: ignore[arg-type]
        self.assert_refused(completed, "LIMIT_GIB")

    def test_a_hard_limit_factor_that_is_not_a_positive_integer_is_refused(self) -> None:
        for value in ("0", "-2", "", "two", "1.5"):
            with self.subTest(value):
                self.assert_refused(self.start(HARD_LIMIT_FACTOR=value), "HARD_LIMIT_FACTOR")

    def test_a_check_interval_that_is_not_a_positive_integer_is_refused(self) -> None:
        for value in ("0", "-5", "", "soon"):
            with self.subTest(value):
                self.assert_refused(self.start(CHECK_SECONDS=value), "CHECK_SECONDS")

    def test_a_missing_required_variable_ends_the_pruner_with_the_same_status_and_names_it(self) -> None:
        for name in ("NODE_NAME", "HOST_CACHE_ROOT"):
            with self.subTest(name):
                completed = self.start(**{name: None})  # type: ignore[arg-type]
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn(f"node-cache-pruner: {name} is required", completed.stderr)
                self.assertFalse(self.calls.exists())

    def test_a_store_root_without_namespaces_is_refused_before_the_store_or_the_cache_is_touched(self) -> None:
        # Unset, empty and only whitespace all list no namespace, so no reference would be read and every entry would count as unused.
        for label, overrides in (("unset", {}), ("empty", {"STORE_NAMESPACES": ""}), ("blank", {"STORE_NAMESPACES": "  "})):
            with self.subTest(label):
                store = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, store, True)
                (store / KEY_A).mkdir()
                (store / ".incoming" / f"{KEY_B}.pod-1.capability-node").mkdir(parents=True)
                completed = self.start(STORE_ROOT=str(store), **overrides)
                self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
                self.assertIn("node-cache-pruner: STORE_NAMESPACES is required when STORE_ROOT is set", completed.stderr)
                self.assertFalse(self.calls.exists(), "kubectl was called")
                self.assertTrue((store / KEY_A).exists() and (store / ".incoming" / f"{KEY_B}.pod-1.capability-node").exists(), "the store was touched")
                self.assertTrue((self.root / "arc-a" / "main" / "pnpm" / "blob").exists(), "the cache was touched")

    def test_namespaces_without_a_store_root_and_neither_set_start_normally(self) -> None:
        for label, overrides in (("neither", {}), ("namespaces only", {"STORE_NAMESPACES": "arc-a"})):
            with self.subTest(label):
                completed = self.start(**overrides)
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_every_numeric_setting_accepts_its_maximum_and_refuses_one_more_and_anything_that_would_wrap(self) -> None:
        maximum = {"LIMIT_KIB": 1125899906842624, "LIMIT_GIB": 1073741824, "HARD_LIMIT_FACTOR": 1024, "CHECK_SECONDS": 86400}
        for name, largest in maximum.items():
            with self.subTest(name):
                overrides: dict[str, str | None] = {"LIMIT_KIB": None} if name == "LIMIT_GIB" else {}
                accepted = self.start(**{**overrides, name: str(largest)})  # type: ignore[arg-type]
                self.assertEqual(accepted.returncode, 0, accepted.stderr)
                for value in (largest + 1, 10**18, 10**19, 10**30):
                    refused = self.start(**{**overrides, name: str(value)})  # type: ignore[arg-type]
                    self.assertEqual(refused.returncode, 2, f"{name}={value}: {refused.stdout}{refused.stderr}")
                    self.assertIn(f"node-cache-pruner: {name} must be at most {largest}", refused.stderr)
                    self.assertTrue((self.root / "arc-a" / "main" / "pnpm" / "blob").exists())

    def test_the_value_that_made_the_hard_limit_wrap_to_zero_is_refused(self) -> None:
        completed = self.start(LIMIT_KIB=None, LIMIT_GIB="8796093022208")  # type: ignore[arg-type]
        self.assertEqual(completed.returncode, 2)
        self.assertIn("LIMIT_GIB must be at most 1073741824", completed.stderr)

    def test_the_largest_accepted_settings_together_cannot_wrap_the_hard_limit(self) -> None:
        # 2^50 KiB times 2^10 is 2^60, under 2^63 - 1: the largest limit and the largest factor give a positive hard limit.
        self.assertLess(1125899906842624 * 1024, 2**63 - 1)
        self.assertEqual(1073741824 * 1024 * 1024, 1125899906842624)

    def test_valid_settings_start_normally_and_a_gibibyte_limit_is_converted(self) -> None:
        completed = self.start(LIMIT_KIB=None, LIMIT_GIB="1", HARD_LIMIT_FACTOR="3", CHECK_SECONDS="60")  # type: ignore[arg-type]
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # 100 KiB is far under 1 GiB, so the cache is left alone and no pod is listed.
        self.assertTrue((self.root / "arc-a" / "main" / "pnpm" / "blob").exists())
        self.assertFalse(self.calls.exists())



# The shapes of the entry names the role produces: a tool cache, a merged sysroot and the dind sidecar's externals (see arc_capability_store_key and _capability_store_merged_key). A name sorts by its prefix, so the expected lists below are sorted rather than written in the order of the keys.
KEY_A = "toolcache-" + "a" * 64
KEY_B = "merged-sysroot-" + "b" * 64
KEY_C = "externals-" + "c" * 64


class CapabilityStorePrunerTest(unittest.TestCase):
    """The capability store's pruning: an entry stays while a scale set's template or an unfinished pod on this node lists it, and a copy under .incoming stays while its pod is unfinished on this node."""

    # PrunerTest's helpers, borrowed rather than inherited so its cases do not run twice.
    setup_work = PrunerTest.setup_work
    environment = PrunerTest.environment
    run_pruner = PrunerTest.run_pruner

    def run_store(self, entries: list[str], incoming: list[str] | None = None, scale_sets: list[dict[str, Any]] | None = None, pods: list[dict[str, Any]] | None = None, **env: str) -> tuple[Run, Path]:
        root, bin_dir, state = self.setup_work()
        store = root.parent / "store"
        for name in entries:
            (store / name).mkdir(parents=True)
            (store / name / "payload").write_text(name)
        for name in incoming or []:
            (store / ".incoming" / name).mkdir(parents=True)
        environment = {**self.environment(root, bin_dir, state, pods, [], {"STORE_ROOT": str(store), "STORE_NAMESPACES": "arc-a", "FAKE_SCALE_SETS": json.dumps(scale_sets or []), **env})}
        completed = subprocess.run(["bash", str(SCRIPT)], check=True, env=environment, capture_output=True, text=True)
        return Run(root, state, completed.stdout), store

    def store_pod(self, keys: list[str], node: str = "node-a", phase: str = "Running", uid: str = "pod-1", from_call: int = 0, namespace: str = "arc-a") -> dict[str, Any]:
        return {"name": f"runner-{uid}", "uid": uid, "namespace": namespace, "node": node, "phase": phase, "volumes": [], "store_keys": keys, "from_call": from_call}

    def remaining(self, store: Path) -> list[str]:
        return sorted(path.name for path in store.iterdir() if not path.name.startswith("."))

    def test_an_entry_no_template_or_pod_uses_is_removed_and_one_a_template_lists_is_kept(self) -> None:
        run, store = self.run_store([KEY_A, KEY_B], scale_sets=[{"namespace": "arc-a", "store_keys": [KEY_A]}])
        self.assertEqual(self.remaining(store), [KEY_A])
        self.assertIn(f"capability store: removed {KEY_B}, which no scale set or pod on this node uses", run.output)

    def test_an_entry_an_unfinished_pod_on_this_node_lists_is_kept_after_its_template_moved_on(self) -> None:
        _, store = self.run_store([KEY_A, KEY_B, KEY_C], scale_sets=[{"namespace": "arc-a", "store_keys": [KEY_C]}], pods=[self.store_pod([KEY_A]), self.store_pod([KEY_B], phase="Pending", uid="pod-2")])
        self.assertEqual(self.remaining(store), sorted([KEY_A, KEY_B, KEY_C]))

    def test_a_pod_in_a_namespace_that_left_the_store_profiles_keeps_its_entries_while_the_namespace_is_listed(self) -> None:
        # arc-gone has no scale set any more but a pod made from its old template runs on the node: the role lists the namespace for as long as the pruner holds a Role there.
        _, store = self.run_store([KEY_A, KEY_B], scale_sets=[{"namespace": "arc-a", "store_keys": [KEY_B]}], pods=[self.store_pod([KEY_A], namespace="arc-gone")], STORE_NAMESPACES="arc-a arc-gone")
        self.assertEqual(self.remaining(store), sorted([KEY_A, KEY_B]))

    def test_a_pod_in_a_namespace_that_is_not_listed_keeps_nothing(self) -> None:
        # What the old scan did to arc-gone: the namespace dropped out of STORE_NAMESPACES and the entry went under the running pod.
        _, store = self.run_store([KEY_A, KEY_B], scale_sets=[{"namespace": "arc-a", "store_keys": [KEY_B]}], pods=[self.store_pod([KEY_A], namespace="arc-gone")])
        self.assertEqual(self.remaining(store), [KEY_B])

    def test_a_finished_pod_or_one_on_another_node_keeps_nothing(self) -> None:
        _, store = self.run_store([KEY_A, KEY_B], pods=[self.store_pod([KEY_A], phase="Succeeded"), self.store_pod([KEY_B], node="node-b", uid="pod-2")])
        self.assertEqual(self.remaining(store), [])

    def test_a_pod_that_starts_using_an_entry_before_it_is_removed_keeps_it(self) -> None:
        # The pod appears from the second list on: the first finds the entry unused, the list taken again before the rename finds the pod.
        _, store = self.run_store([KEY_A], pods=[self.store_pod([KEY_A], from_call=1)])
        self.assertEqual(self.remaining(store), [KEY_A])

    def test_a_copy_in_progress_is_kept_while_its_pod_runs_and_removed_once_it_is_gone(self) -> None:
        _, store = self.run_store([], incoming=[f"{KEY_A}.pod-1.capability-node", f"{KEY_B}.pod-gone.capability-node"], pods=[self.store_pod([KEY_A])])
        self.assertEqual(sorted(path.name for path in (store / ".incoming").iterdir()), [f"{KEY_A}.pod-1.capability-node"])

    def test_nothing_is_removed_when_the_scale_sets_cannot_be_listed(self) -> None:
        run, store = self.run_store([KEY_A], FAKE_FAIL_SCALE_SETS="arc-a")
        self.assertEqual(self.remaining(store), [KEY_A])
        self.assertIn("NODE_CACHE_LIST_FAILED capability store: the scale sets or the pods on this node could not be listed; not pruned", run.output)

    def test_without_a_store_root_the_store_is_not_looked_at(self) -> None:
        run = self.run_pruner({})
        self.assertFalse([call for call in run.calls() if "autoscalingrunnersets" in call])


if __name__ == "__main__":
    unittest.main()
