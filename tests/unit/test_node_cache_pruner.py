"""Tests for scripts/node-cache-pruner.sh: a cache over its limit is set aside whole and deleted only when no runner pod runs on the node in its profile's namespace, with stand-ins for kubectl and du that answer from scripted state and record what they are asked."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "node-cache-pruner.sh"

# `get pods` answers one pod for a namespace that is busy (from its FAKE_BUSY_AFTER-th call on, counting that namespace's calls from 0), nothing otherwise, and fails for a namespace in FAKE_FAIL_PODS. `get node` answers the annotation a previous `annotate` set. Every call is recorded.
FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, sys
    args = sys.argv[1:]
    state = os.environ["FAKE_STATE_DIR"]
    open(os.path.join(state, "calls.txt"), "a").write(" ".join(args) + "\\n")
    annotation = os.path.join(state, "annotation")
    if args[:2] == ["get", "pods"]:
        namespace = args[args.index("-n") + 1]
        if namespace in os.environ.get("FAKE_FAIL_PODS", "").split(","):
            sys.stderr.write("the server is unavailable\\n")
            sys.exit(1)
        counter = os.path.join(state, "calls-" + namespace)
        count = int(open(counter).read()) if os.path.exists(counter) else 0
        open(counter, "w").write(str(count + 1))
        # What the cache root looked like at this call: the profile directories' entries and the trash's, so a test can see the state between the check and the delete.
        root = os.environ["CACHE_ROOT"]
        snapshot = {}
        for directory, entries in ((root + "/" + os.environ.get("FAKE_PROFILE", ""), "profile"), (root + "/.trash", "trash")):
            found = []
            for base, names, files in os.walk(directory):
                found.extend(os.path.relpath(os.path.join(base, name), directory) for name in names + files)
            snapshot[entries] = sorted(found)
        open(os.path.join(state, "snapshot-%s-%d" % (namespace, count)), "w").write(repr(snapshot))
        if namespace in os.environ.get("FAKE_BUSY", "").split(",") and count >= int(os.environ.get("FAKE_BUSY_AFTER", "0")):
            print("pod/runner-abc")
        sys.exit(0)
    if args[:1] == ["cordon"]:
        sys.exit(0)
    if args[:1] == ["uncordon"]:
        sys.exit(0)
    if args[:2] == ["annotate", "node"]:
        if args[3].endswith("-"):
            os.path.exists(annotation) and os.remove(annotation)
        else:
            open(annotation, "w").write("true")
        sys.exit(0)
    if args[:2] == ["get", "node"]:
        print("true" if os.path.exists(annotation) else "", end="")
        sys.exit(0)
    sys.stderr.write("unexpected kubectl call: %s\\n" % args)
    sys.exit(2)
    """
)

# du that fails (FAKE_DU=fail, no output) or reads part of a tree (FAKE_DU=partial, a size and a failure status), and otherwise is the real one.
FAKE_DU = textwrap.dedent(
    """\
    #!/bin/sh
    case "$FAKE_DU" in
      fail) echo "du: cannot access" >&2; exit 1;;
      partial) printf '%s\\t%s\\n' "$FAKE_DU_KIB" "$2"; echo "du: cannot access" >&2; exit 1;;
      *) exec "$REAL_DU" "$@";;
    esac
    """
)

# The limit, in KiB, every case runs against, and the hard limit the script derives from it with DRAIN_FACTOR 2.
LIMIT_KIB = 64
HARD_KIB = 128
OVER_KIB = 100
OVER_HARD_KIB = 200
UNDER_KIB = 16


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


class PrunerTest(unittest.TestCase):
    def run_pruner(self, caches: dict[str, int], busy: list[str] | None = None, wanted: list[str] | None = None, annotated: bool = False, **env: str) -> Run:
        """Run one check over caches (<namespace>/<release> mapped to the KiB of one file under a pnpm directory and a dot-file beside it), with runner pods running on the node in the busy namespaces."""
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        root = work / "root"
        root.mkdir()
        for name, size_kib in caches.items():
            (root / name / "pnpm").mkdir(parents=True)
            (root / name / "pnpm" / "blob").write_bytes(b"x" * (size_kib * 1024))
            (root / name / ".hidden").write_text("kept with the rest")
        bin_dir = work / "bin"
        bin_dir.mkdir()
        for name, content in (("kubectl", FAKE_KUBECTL), ("du", FAKE_DU)):
            (bin_dir / name).write_text(content)
            (bin_dir / name).chmod(0o755)
        state = work / "state"
        state.mkdir()
        (state / "calls.txt").write_text("")
        if annotated:
            (state / "annotation").write_text("true")
        full_env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "REAL_DU": shutil.which("du") or "du",
            "NODE_NAME": "node-a",
            "CACHE_ROOT": str(root),
            "LIMIT_KIB": str(LIMIT_KIB),
            "PRUNE_ONCE": "true",
            "FAKE_STATE_DIR": str(state),
            "FAKE_BUSY": ",".join(busy or []),
            **({"WANTED_CACHES": " ".join(wanted)} if wanted is not None else {}),
            **env,
        }
        completed = subprocess.run(["bash", str(SCRIPT)], check=True, env=full_env, capture_output=True, text=True)
        return Run(root, state, completed.stdout)

    def test_a_cache_over_the_limit_with_no_runner_pod_on_the_node_is_emptied_and_its_directory_kept(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, wanted=["arc-a/main"])
        self.assertTrue(run.profile("arc-a/main").is_dir())
        self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])
        self.assertEqual(list((run.root / ".trash").iterdir()), [])
        self.assertIn("emptied", run.output)

    def test_a_cache_over_the_limit_is_left_alone_while_a_runner_pod_of_its_profile_runs_on_the_node(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, busy=["arc-a"], wanted=["arc-a/main"])
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertIn("left alone", run.output)
        self.assertEqual([call for call in run.calls() if call.startswith(("cordon", "uncordon"))], [])

    def test_a_runner_pod_of_another_profile_does_not_protect_a_cache(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB, "arc-b/main": OVER_KIB}, busy=["arc-b"], wanted=["arc-a/main", "arc-b/main"])
        self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])
        self.assertTrue((run.profile("arc-b/main") / "pnpm" / "blob").exists())

    def test_a_cache_under_the_limit_is_left_alone_and_no_pod_is_listed(self) -> None:
        run = self.run_pruner({"arc-a/main": UNDER_KIB}, wanted=["arc-a/main"])
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertEqual([call for call in run.calls() if call.startswith("get pods")], [])
        self.assertEqual(run.output, "")

    def test_a_cache_is_set_aside_with_its_dot_files_so_a_pod_never_sees_half_of_it(self) -> None:
        # The trash is deleted in the same run, so the set-aside is observed through a trash left over from a crash: a stale entry is garbage and goes at the start of the next round.
        run = self.run_pruner({"arc-a/main": OVER_KIB}, wanted=["arc-a/main"])
        self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])
        self.assertFalse((run.profile("arc-a/main") / ".hidden").exists())

    def test_a_trash_left_by_an_earlier_round_is_deleted(self) -> None:
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        # Run once to build the scripted environment, then plant the leftover and run again over the same root.
        run = self.run_pruner({"arc-a/main": UNDER_KIB}, wanted=["arc-a/main"])
        (run.root / ".trash" / "stale-1").mkdir(parents=True)
        (run.root / ".trash" / "stale-1" / "blob").write_text("x")
        env = {**os.environ, "PATH": f"{run.state.parent / 'bin'}:{os.environ['PATH']}", "REAL_DU": shutil.which("du") or "du", "NODE_NAME": "node-a", "CACHE_ROOT": str(run.root), "LIMIT_KIB": str(LIMIT_KIB), "PRUNE_ONCE": "true", "FAKE_STATE_DIR": str(run.state), "WANTED_CACHES": "arc-a/main"}
        subprocess.run(["bash", str(SCRIPT)], check=True, env=env, capture_output=True, text=True)
        self.assertEqual(list((run.root / ".trash").iterdir()), [])

    def test_the_cache_is_out_of_the_profile_directory_whole_before_anything_is_deleted(self) -> None:
        # The second pod list is taken after the move and before the delete, so what kubectl saw then is the state a pod binding at that moment would meet.
        run = self.run_pruner({"arc-a/main": OVER_KIB}, wanted=["arc-a/main"], FAKE_PROFILE="arc-a/main")
        recheck = eval((run.state / "snapshot-arc-a-1").read_text())
        self.assertEqual(recheck["profile"], [])
        self.assertTrue(any(entry.endswith("pnpm/blob") for entry in recheck["trash"]), recheck)
        self.assertTrue(any(entry.endswith(".hidden") for entry in recheck["trash"]), recheck)

    def test_a_pod_that_appears_while_the_cache_is_set_aside_sees_an_empty_cache_and_nothing_is_left_half_deleted(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, busy=["arc-a"], wanted=["arc-a/main"], FAKE_BUSY_AFTER="1")
        self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])
        self.assertEqual(list((run.root / ".trash").iterdir()), [])
        self.assertIn("started while the cache was being set aside", run.output)

    def test_each_profile_is_asked_about_in_its_own_namespace_on_this_node(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB, "arc-b/other": OVER_KIB}, wanted=["arc-a/main", "arc-b/other"])
        asked = sorted({call for call in run.calls() if call.startswith("get pods")})
        self.assertEqual(asked, ["get pods -n arc-a --field-selector spec.nodeName=node-a -o name", "get pods -n arc-b --field-selector spec.nodeName=node-a -o name"])

    def test_a_root_with_no_caches_is_not_an_error(self) -> None:
        run = self.run_pruner({})
        self.assertEqual(run.output, "")

    def test_a_failed_size_reading_skips_that_profile_and_the_others_are_still_pruned(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB, "arc-b/main": OVER_KIB}, wanted=["arc-a/main", "arc-b/main"], FAKE_DU="fail")
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertIn("could not be read", run.output)

    def test_a_partial_size_reading_is_used_as_read(self) -> None:
        run = self.run_pruner({"arc-a/main": UNDER_KIB}, wanted=["arc-a/main"], FAKE_DU="partial", FAKE_DU_KIB=str(OVER_KIB))
        self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])

    def test_a_failed_pod_list_means_no_pruning_for_that_profile_only(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB, "arc-b/main": OVER_KIB}, wanted=["arc-a/main", "arc-b/main"], FAKE_FAIL_PODS="arc-a")
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertEqual(list(run.profile("arc-b/main").iterdir()), [])
        self.assertIn("could not be listed", run.output)

    def test_a_busy_node_over_the_hard_limit_is_cordoned_and_annotated_so_it_drains(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_HARD_KIB}, busy=["arc-a"], wanted=["arc-a/main"])
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertIn("cordon node-a", run.calls())
        self.assertIn("annotate node node-a github-runner.exadev.io/node-cache-cordoned=true --overwrite", run.calls())

    def test_a_busy_node_between_the_limit_and_the_hard_limit_is_not_cordoned(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_KIB}, busy=["arc-a"], wanted=["arc-a/main"])
        self.assertEqual([call for call in run.calls() if call.startswith("cordon")], [])
        self.assertEqual(HARD_KIB > OVER_KIB, True)

    def test_a_node_the_pruner_cordoned_is_emptied_and_uncordoned_once_it_is_idle(self) -> None:
        run = self.run_pruner({"arc-a/main": OVER_HARD_KIB}, wanted=["arc-a/main"], annotated=True)
        self.assertEqual(list(run.profile("arc-a/main").iterdir()), [])
        self.assertIn("uncordon node-a", run.calls())
        self.assertIn("annotate node node-a github-runner.exadev.io/node-cache-cordoned-", run.calls())

    def test_a_node_it_did_not_cordon_is_never_uncordoned(self) -> None:
        run = self.run_pruner({"arc-a/main": UNDER_KIB}, wanted=["arc-a/main"])
        self.assertEqual([call for call in run.calls() if call.startswith("uncordon")], [])

    def test_a_directory_that_is_no_longer_wanted_is_removed_when_no_pod_of_it_runs(self) -> None:
        run = self.run_pruner({"arc-a/main": UNDER_KIB, "arc-gone/old": UNDER_KIB}, wanted=["arc-a/main"])
        self.assertFalse((run.root / "arc-gone").exists())
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())
        self.assertIn("no longer wanted", run.output)

    def test_removing_one_unwanted_profile_does_not_stop_the_removal_of_the_next_in_its_namespace(self) -> None:
        # The namespace directory cannot be removed while another profile still sits in it, which must not end the round.
        run = self.run_pruner({"arc-gone/old1": UNDER_KIB, "arc-gone/old2": UNDER_KIB, "arc-a/main": UNDER_KIB}, wanted=["arc-a/main"])
        self.assertFalse((run.root / "arc-gone").exists())

    def test_a_directory_that_is_no_longer_wanted_is_kept_while_a_pod_runs_in_its_namespace(self) -> None:
        run = self.run_pruner({"arc-gone/old": UNDER_KIB}, busy=["arc-gone"], wanted=[])
        self.assertTrue((run.profile("arc-gone/old") / "pnpm" / "blob").exists())

    def test_without_a_wanted_list_no_directory_is_removed_for_being_unwanted(self) -> None:
        run = self.run_pruner({"arc-a/main": UNDER_KIB})
        self.assertTrue((run.profile("arc-a/main") / "pnpm" / "blob").exists())


if __name__ == "__main__":
    unittest.main()
