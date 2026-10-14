"""Tests for scripts/node-cache-pruner.sh: a cache over its limit is emptied only when no runner pod runs on the node in its profile's namespace, with a stand-in for kubectl that answers from a scripted pod list."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "node-cache-pruner.sh"

# Answers `get pods` for a namespace in FAKE_BUSY_NAMESPACES, when asked about this node, with one pod; every other question gets no pods.
FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, sys
    args = sys.argv[1:]
    if args[:2] == ["get", "pods"] and "-n" in args and "--field-selector" in args:
        open(os.environ["FAKE_CALLS_OUT"], "a").write(" ".join(args) + "\\n")
        namespace = args[args.index("-n") + 1]
        selector = args[args.index("--field-selector") + 1]
        if namespace in os.environ.get("FAKE_BUSY_NAMESPACES", "").split(",") and selector == "spec.nodeName=" + os.environ["NODE_NAME"]:
            print("pod/runner-abc")
        sys.exit(0)
    sys.stderr.write("unexpected kubectl call: %s\\n" % args)
    sys.exit(2)
    """
)

# The limit, in KiB, every case runs against: a cache of OVER_KIB exceeds it and one of UNDER_KIB does not.
LIMIT_KIB = 64
OVER_KIB = 128
UNDER_KIB = 16


class PrunerTest(unittest.TestCase):
    def run_pruner(self, caches: dict[str, int], busy: list[str]) -> tuple[Path, str, str]:
        """Run one check over caches (<namespace>/<release> mapped to the KiB of one file under a pnpm directory), with runner pods running on the node in the busy namespaces; return the cache root, the script's output and the pod lists it asked for."""
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        root = work / "root"
        for name, size_kib in caches.items():
            (root / name / "pnpm").mkdir(parents=True)
            (root / name / "pnpm" / "blob").write_bytes(b"x" * (size_kib * 1024))
        bin_dir = work / "bin"
        bin_dir.mkdir()
        kubectl = bin_dir / "kubectl"
        kubectl.write_text(FAKE_KUBECTL)
        kubectl.chmod(0o755)
        calls = work / "calls.txt"
        calls.write_text("")
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "NODE_NAME": "node-a", "CACHE_ROOT": str(root), "LIMIT_KIB": str(LIMIT_KIB), "PRUNE_ONCE": "true", "FAKE_CALLS_OUT": str(calls), "FAKE_BUSY_NAMESPACES": ",".join(busy)}
        completed = subprocess.run(["bash", str(SCRIPT)], check=True, env=env, capture_output=True, text=True)
        return root, completed.stdout, calls.read_text()

    def test_a_cache_over_the_limit_with_no_runner_pod_on_the_node_is_emptied_and_its_directory_kept(self) -> None:
        root, output, _ = self.run_pruner({"arc-a/main": OVER_KIB}, busy=[])
        self.assertTrue((root / "arc-a" / "main").is_dir())
        self.assertEqual(list((root / "arc-a" / "main").iterdir()), [])
        self.assertIn("arc-a/main", output)
        self.assertIn("emptied", output)

    def test_a_cache_over_the_limit_is_left_alone_while_a_runner_pod_of_its_profile_runs_on_the_node(self) -> None:
        root, output, _ = self.run_pruner({"arc-a/main": OVER_KIB}, busy=["arc-a"])
        self.assertTrue((root / "arc-a" / "main" / "pnpm" / "blob").exists())
        self.assertIn("left alone", output)

    def test_a_runner_pod_of_another_profile_does_not_protect_a_cache(self) -> None:
        root, _, _ = self.run_pruner({"arc-a/main": OVER_KIB, "arc-b/main": OVER_KIB}, busy=["arc-b"])
        self.assertEqual(list((root / "arc-a" / "main").iterdir()), [])
        self.assertTrue((root / "arc-b" / "main" / "pnpm" / "blob").exists())

    def test_a_cache_under_the_limit_is_left_alone_and_no_pod_is_listed(self) -> None:
        root, output, calls = self.run_pruner({"arc-a/main": UNDER_KIB}, busy=[])
        self.assertTrue((root / "arc-a" / "main" / "pnpm" / "blob").exists())
        self.assertEqual(calls, "")
        self.assertEqual(output, "")

    def test_each_profile_is_asked_about_in_its_own_namespace_on_this_node(self) -> None:
        _, _, calls = self.run_pruner({"arc-a/main": OVER_KIB, "arc-b/other": OVER_KIB}, busy=[])
        self.assertEqual(sorted(calls.splitlines()), ["get pods -n arc-a --field-selector spec.nodeName=node-a -o name", "get pods -n arc-b --field-selector spec.nodeName=node-a -o name"])

    def test_a_root_with_no_caches_is_not_an_error(self) -> None:
        _, output, calls = self.run_pruner({}, busy=[])
        self.assertEqual((output, calls), ("", ""))


if __name__ == "__main__":
    unittest.main()
