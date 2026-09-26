"""Tests for scripts/node-recovery.sh, one pass of the node recovery watcher, run against a scripted stand-in for kubectl with the real bash and jq."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "node-recovery.sh"
ANNOTATION = "github-runner.exadev/out-of-service-applied-at"
OUT_OF_SERVICE = "node.kubernetes.io/out-of-service"
AFTER_SECONDS = 120

# Answers `kubectl get nodes -o json` from a file and records every `kubectl patch`; FAKE_PATCH_EXIT sets the patch's exit status.
FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    if args[:2] == ["get", "nodes"]:
        sys.stdout.write(open(os.environ["FAKE_NODES"]).read())
        sys.exit(0)
    if args[:2] == ["patch", "node"]:
        with open(os.environ["FAKE_PATCHES"], "a") as log:
            log.write(json.dumps({"name": args[2], "argv": args}) + "\\n")
        sys.exit(int(os.environ.get("FAKE_PATCH_EXIT", "0")))
    sys.stderr.write("unexpected kubectl call: %s\\n" % args)
    sys.exit(2)
    """
)


def timestamp(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def node(
    name: str,
    ready: str,
    seconds_ago: int,
    taints: list[dict[str, str]] | None = None,
    annotations: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "metadata": {"name": name, "resourceVersion": f"rv-{name}", "annotations": annotations or {}},
        "spec": {"taints": taints or []},
        "status": {"conditions": [{"type": "Ready", "status": ready, "lastTransitionTime": timestamp(seconds_ago)}]},
    }


UNREACHABLE = {"key": "node.kubernetes.io/unreachable", "effect": "NoExecute"}
OURS = {"key": OUT_OF_SERVICE, "value": "nodeshutdown", "effect": "NoExecute"}


class NodeRecoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for tool in ("bash", "jq"):
            if shutil.which(tool) is None:
                raise unittest.SkipTest(f"{tool} is not installed")

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        bin_dir = self.work / "bin"
        bin_dir.mkdir()
        kubectl = bin_dir / "kubectl"
        kubectl.write_text(FAKE_KUBECTL)
        kubectl.chmod(0o755)
        self.nodes_file = self.work / "nodes.json"
        self.patches_file = self.work / "patches.jsonl"
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_NODES": str(self.nodes_file),
            "FAKE_PATCHES": str(self.patches_file),
            "NODE_RECOVERY_AFTER_SECONDS": str(AFTER_SECONDS),
            "NODE_RECOVERY_ANNOTATION": ANNOTATION,
            "NODE_RECOVERY_SELF_NODE": "watcher-node",
        }

    def run_pass(self, nodes: list[dict[str, object]], **env: str) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
        self.nodes_file.write_text(json.dumps({"items": nodes}))
        result = subprocess.run(["bash", str(SCRIPT)], env={**self.env, **env}, capture_output=True, text=True, check=False)
        patches = []
        if self.patches_file.exists():
            for line in self.patches_file.read_text().splitlines():
                call = json.loads(line)
                argv = call["argv"]
                self.assertEqual(argv[3], "--type=json")
                self.assertEqual(argv[4], "-p")
                patches.append({"name": call["name"], "patch": json.loads(argv[5])})
        return result, patches

    @staticmethod
    def value_at(patch: list[dict[str, object]], path: str) -> object:
        return next(op["value"] for op in patch if op["path"] == path)

    def test_taints_a_node_unknown_for_longer_than_the_threshold(self) -> None:
        result, patches = self.run_pass([node("dead", "Unknown", AFTER_SECONDS + 30, taints=[UNREACHABLE], annotations={"keep": "me"})])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p["name"] for p in patches], ["dead"])
        patch = patches[0]["patch"]
        self.assertEqual(patch[0], {"op": "test", "path": "/metadata/resourceVersion", "value": "rv-dead"})
        taints = self.value_at(patch, "/spec/taints")
        self.assertIn(UNREACHABLE, taints)
        added = [t for t in taints if t["key"] == OUT_OF_SERVICE]
        self.assertEqual(len(added), 1)
        self.assertEqual((added[0]["value"], added[0]["effect"]), ("nodeshutdown", "NoExecute"))
        annotations = self.value_at(patch, "/metadata/annotations")
        self.assertEqual(annotations["keep"], "me")
        self.assertIn(ANNOTATION, annotations)
        self.assertIn("Tainted dead", result.stdout)

    def test_leaves_a_node_unknown_for_less_than_the_threshold(self) -> None:
        result, patches = self.run_pass([node("blip", "Unknown", AFTER_SECONDS - 30, taints=[UNREACHABLE])])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(patches, [])

    def test_leaves_a_node_whose_kubelet_reports_not_ready(self) -> None:
        _, patches = self.run_pass([node("sick", "False", AFTER_SECONDS * 10)])
        self.assertEqual(patches, [])

    def test_never_taints_its_own_node(self) -> None:
        _, patches = self.run_pass([node("watcher-node", "Unknown", AFTER_SECONDS * 10)])
        self.assertEqual(patches, [])

    def test_leaves_an_out_of_service_taint_it_did_not_apply(self) -> None:
        operator_taint = {"key": OUT_OF_SERVICE, "value": "maintenance", "effect": "NoExecute"}
        _, patches = self.run_pass(
            [
                node("operator-dead", "Unknown", AFTER_SECONDS * 10, taints=[operator_taint]),
                node("operator-back", "True", 5, taints=[operator_taint]),
            ]
        )
        self.assertEqual(patches, [])

    def test_does_not_taint_a_node_twice(self) -> None:
        _, patches = self.run_pass([node("dead", "Unknown", AFTER_SECONDS * 10, taints=[UNREACHABLE, OURS], annotations={ANNOTATION: timestamp(60)})])
        self.assertEqual(patches, [])

    def test_clears_its_taint_once_the_node_is_ready(self) -> None:
        other = {"key": "dedicated", "value": "builds", "effect": "NoSchedule"}
        result, patches = self.run_pass([node("back", "True", 5, taints=[other, OURS], annotations={ANNOTATION: timestamp(600), "keep": "me"})])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p["name"] for p in patches], ["back"])
        patch = patches[0]["patch"]
        self.assertEqual(patch[0], {"op": "test", "path": "/metadata/resourceVersion", "value": "rv-back"})
        self.assertEqual(self.value_at(patch, "/spec/taints"), [other])
        self.assertEqual(self.value_at(patch, "/metadata/annotations"), {"keep": "me"})
        self.assertIn("Removed", result.stdout)

    def test_leaves_healthy_nodes_alone(self) -> None:
        _, patches = self.run_pass([node("healthy", "True", 3600), node("fresh", "True", 1)])
        self.assertEqual(patches, [])

    def test_patches_every_node_that_needs_it(self) -> None:
        _, patches = self.run_pass(
            [
                node("dead", "Unknown", AFTER_SECONDS + 1),
                node("healthy", "True", 3600),
                node("back", "True", 5, taints=[OURS], annotations={ANNOTATION: timestamp(600)}),
            ]
        )
        self.assertEqual(sorted(p["name"] for p in patches), ["back", "dead"])

    def test_a_refused_patch_fails_the_pass(self) -> None:
        result, patches = self.run_pass([node("dead", "Unknown", AFTER_SECONDS * 10)], FAKE_PATCH_EXIT="1")
        self.assertEqual(len(patches), 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retrying on the next pass", result.stderr)

    def test_rejects_a_threshold_that_is_not_a_number(self) -> None:
        result, patches = self.run_pass([node("dead", "Unknown", AFTER_SECONDS * 10)], NODE_RECOVERY_AFTER_SECONDS="two minutes")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(patches, [])


if __name__ == "__main__":
    unittest.main()
