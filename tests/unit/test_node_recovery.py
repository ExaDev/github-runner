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
TOKEN = "made-up-service-account-token"
ANNOTATION = "github-runner.exadev/out-of-service-applied-at"
OUT_OF_SERVICE = "node.kubernetes.io/out-of-service"
AFTER_SECONDS = 120
PRUNE_AFTER_SECONDS = 600
PRUNE_LABEL = "github-runner.exadev/removable"
REMOVABLE = {PRUNE_LABEL: "true"}

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


# Stands in for the API server's DELETE on a node: records the request, reads the cluster's current nodes (name to uid and resourceVersion) from FAKE_CURRENT, and answers 409 when the request's preconditions do not match the node as it is now, 404 when it is gone, and otherwise 200 and the node is recorded as deleted. The token arrives on stdin as curl config and must never be on the command line.
FAKE_CURL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, re, sys
    args = sys.argv[1:]
    def value(flag):
        return args[args.index(flag) + 1]
    config = sys.stdin.read()
    url = args[-1]
    body = json.loads(value("--data"))
    name = url.rsplit("/", 1)[1]
    current = json.load(open(os.environ["FAKE_CURRENT"])).get(name)
    status = 200
    if current is None:
        status = 404
    elif body["preconditions"] != {"uid": current["uid"], "resourceVersion": current["resourceVersion"]}:
        status = 409
    with open(os.environ["FAKE_DELETES"], "a") as log:
        log.write(json.dumps({"name": name, "url": url, "argv": args, "config": config, "body": body, "status": status}) + "\\n")
    with open(value("--output"), "w") as out:
        out.write(json.dumps({"message": "fake API refused", "code": status}))
    sys.stdout.write(str(status))
    sys.exit(int(os.environ.get("FAKE_CURL_EXIT", "0")))
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
    labels: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "metadata": {"name": name, "uid": f"uid-{name}", "resourceVersion": f"rv-{name}", "annotations": annotations or {}, "labels": labels or {}},
        "spec": {"taints": taints or []},
        "status": {"conditions": [{"type": "Ready", "status": ready, "lastTransitionTime": timestamp(seconds_ago)}]},
    }


UNREACHABLE = {"key": "node.kubernetes.io/unreachable", "effect": "NoExecute"}
OURS = {"key": OUT_OF_SERVICE, "value": "nodeshutdown", "effect": "NoExecute"}


class PassCase(unittest.TestCase):
    """Runs one pass of the script against the stand-ins, and has no tests of its own."""

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
        curl = bin_dir / "curl"
        curl.write_text(FAKE_CURL)
        curl.chmod(0o755)
        self.nodes_file = self.work / "nodes.json"
        self.patches_file = self.work / "patches.jsonl"
        self.current_file = self.work / "current.json"
        self.deletes_file = self.work / "deletes.jsonl"
        self.sa_dir = self.work / "serviceaccount"
        self.sa_dir.mkdir()
        (self.sa_dir / "token").write_text(TOKEN)
        (self.sa_dir / "ca.crt").write_text("ca")
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_NODES": str(self.nodes_file),
            "FAKE_PATCHES": str(self.patches_file),
            "FAKE_CURRENT": str(self.current_file),
            "FAKE_DELETES": str(self.deletes_file),
            "NODE_RECOVERY_API_URL": "https://api.example.test:6443",
            "NODE_RECOVERY_SERVICEACCOUNT_DIR": str(self.sa_dir),
            "NODE_RECOVERY_AFTER_SECONDS": str(AFTER_SECONDS),
            "NODE_RECOVERY_ANNOTATION": ANNOTATION,
            "NODE_RECOVERY_SELF_NODE": "watcher-node",
        }

    def run_pass(
        self, nodes: list[dict[str, object]], current: list[dict[str, object]] | None = None, **env: str
    ) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
        """Runs one pass over the nodes as listed. The API's own view when the delete arrives is `current`, the same nodes unless given."""
        self.nodes_file.write_text(json.dumps({"items": nodes}))
        live = nodes if current is None else current
        self.current_file.write_text(json.dumps({n["metadata"]["name"]: n["metadata"] for n in live}))
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

    def deletes(self) -> list[dict[str, object]]:
        """Every DELETE the fake API received, whether it was accepted or refused."""
        if not self.deletes_file.exists():
            return []
        return [json.loads(line) for line in self.deletes_file.read_text().splitlines()]

    def deleted(self) -> list[str]:
        """The nodes the fake API actually deleted."""
        return [d["name"] for d in self.deletes() if d["status"] == 200]

    @staticmethod
    def value_at(patch: list[dict[str, object]], path: str) -> object:
        return next(op["value"] for op in patch if op["path"] == path)



class NodeRecoveryTest(PassCase):
    """The taint pass."""

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


class NodePruneTest(PassCase):
    """The opt-in prune pass, which deletes a labelled node that stayed Unknown for NODE_RECOVERY_PRUNE_AFTER_SECONDS."""

    def run_prune(self, nodes: list[dict[str, object]], **kwargs: object) -> tuple[subprocess.CompletedProcess[str], list[dict[str, object]]]:
        env = {"NODE_RECOVERY_PRUNE_AFTER_SECONDS": str(PRUNE_AFTER_SECONDS), "NODE_RECOVERY_PRUNE_LABEL": PRUNE_LABEL}
        env.update(kwargs.pop("env", {}))  # type: ignore[arg-type]
        return self.run_pass(nodes, **kwargs, **env)  # type: ignore[arg-type]

    def test_deletes_a_labelled_node_unknown_for_longer_than_the_prune_threshold(self) -> None:
        dead = node("dead", "Unknown", PRUNE_AFTER_SECONDS + 30, taints=[UNREACHABLE, OURS], labels=REMOVABLE)
        result, patches = self.run_prune([dead])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), ["dead"])
        request = self.deletes()[0]
        self.assertEqual(request["url"], "https://api.example.test:6443/api/v1/nodes/dead")
        self.assertEqual(request["argv"][request["argv"].index("--request") + 1], "DELETE")
        self.assertEqual(request["body"]["preconditions"], {"uid": "uid-dead", "resourceVersion": "rv-dead"})
        self.assertEqual(patches, [])
        self.assertIn("Deleted dead", result.stdout)

    def test_sends_the_service_account_token_on_stdin_only(self) -> None:
        self.run_prune([node("dead", "Unknown", PRUNE_AFTER_SECONDS + 30, labels=REMOVABLE)])
        request = self.deletes()[0]
        self.assertIn(f"Bearer {TOKEN}", request["config"])
        self.assertFalse([a for a in request["argv"] if TOKEN in a], "the token is on curl's command line")
        self.assertEqual(request["argv"][request["argv"].index("--cacert") + 1], str(self.sa_dir / "ca.crt"))

    def test_prunes_in_the_pass_that_would_taint_it_instead_of_tainting_first(self) -> None:
        _, patches = self.run_prune([node("dead", "Unknown", PRUNE_AFTER_SECONDS + 30, labels=REMOVABLE)])
        self.assertEqual(patches, [])
        self.assertEqual(self.deleted(), ["dead"])

    def test_does_not_delete_an_unlabelled_node(self) -> None:
        _, patches = self.run_prune([node("server", "Unknown", PRUNE_AFTER_SECONDS * 10, taints=[OURS])])
        self.assertEqual(self.deletes(), [])
        self.assertEqual(patches, [])

    def test_does_not_delete_a_node_whose_label_is_not_true(self) -> None:
        self.run_prune([node("dead", "Unknown", PRUNE_AFTER_SECONDS * 10, labels={PRUNE_LABEL: "false"})])
        self.assertEqual(self.deletes(), [])

    def test_the_label_key_is_configurable(self) -> None:
        other = "example.com/disposable"
        self.run_prune(
            [node("dead", "Unknown", PRUNE_AFTER_SECONDS + 1, labels={other: "true"}), node("kept", "Unknown", PRUNE_AFTER_SECONDS + 1, labels=REMOVABLE)],
            env={"NODE_RECOVERY_PRUNE_LABEL": other},
        )
        self.assertEqual(self.deleted(), ["dead"])

    def test_does_not_delete_a_node_unknown_for_less_than_the_prune_threshold(self) -> None:
        _, patches = self.run_prune([node("blip", "Unknown", PRUNE_AFTER_SECONDS - 30, taints=[UNREACHABLE], labels=REMOVABLE)])
        self.assertEqual(self.deletes(), [])
        self.assertEqual([p["name"] for p in patches], ["blip"], "a node past the taint threshold but not the prune one is still tainted")

    def test_does_not_delete_a_node_whose_kubelet_reports_not_ready(self) -> None:
        self.run_prune([node("sick", "False", PRUNE_AFTER_SECONDS * 10, labels=REMOVABLE)])
        self.assertEqual(self.deletes(), [])

    def test_does_not_delete_a_ready_node(self) -> None:
        self.run_prune([node("healthy", "True", PRUNE_AFTER_SECONDS * 10, labels=REMOVABLE)])
        self.assertEqual(self.deletes(), [])

    def test_never_deletes_its_own_node(self) -> None:
        self.run_prune([node("watcher-node", "Unknown", PRUNE_AFTER_SECONDS * 10, labels=REMOVABLE)])
        self.assertEqual(self.deletes(), [])

    def test_never_deletes_a_node_without_a_ready_condition(self) -> None:
        bare = node("bare", "Unknown", PRUNE_AFTER_SECONDS * 10, labels=REMOVABLE)
        bare["status"] = {"conditions": []}
        self.run_prune([bare])
        self.assertEqual(self.deletes(), [])

    def test_refuses_a_labelled_control_plane_or_etcd_member_and_fails_the_pass(self) -> None:
        for role in ("control-plane", "etcd"):
            with self.subTest(role=role):
                self.deletes_file.unlink(missing_ok=True)
                server = node("server", "Unknown", PRUNE_AFTER_SECONDS * 10, labels={**REMOVABLE, f"node-role.kubernetes.io/{role}": "true"})
                result, _ = self.run_prune([server, node("agent", "Unknown", PRUNE_AFTER_SECONDS * 10, labels=REMOVABLE)])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Not deleting server", result.stderr)
                self.assertEqual(self.deleted(), ["agent"], "the refusal must not stop the other nodes being pruned")

    def test_leaves_a_node_that_recovered_between_the_listing_and_the_delete(self) -> None:
        listed = node("back", "Unknown", PRUNE_AFTER_SECONDS + 30, labels=REMOVABLE)
        recovered = node("back", "True", 1, labels=REMOVABLE)
        recovered["metadata"]["resourceVersion"] = "rv-back-after-recovery"  # type: ignore[index]
        result, _ = self.run_prune([listed], current=[recovered])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([d["status"] for d in self.deletes()], [409], "the delete must carry the listed resourceVersion for the API to refuse it")
        self.assertEqual(self.deleted(), [])
        self.assertIn("changed since it was read", result.stdout)

    def test_a_node_already_gone_is_not_a_failure(self) -> None:
        result, _ = self.run_prune([node("gone", "Unknown", PRUNE_AFTER_SECONDS + 30, labels=REMOVABLE)], current=[])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already gone", result.stdout)

    def test_a_delete_curl_cannot_send_fails_the_pass(self) -> None:
        result, _ = self.run_prune([node("dead", "Unknown", PRUNE_AFTER_SECONDS + 30, labels=REMOVABLE)], FAKE_CURL_EXIT="7")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retrying on the next pass", result.stderr)

    def test_the_other_passes_still_run_beside_a_prune(self) -> None:
        _, patches = self.run_prune(
            [
                node("dead", "Unknown", PRUNE_AFTER_SECONDS + 30, labels=REMOVABLE),
                node("sleeping", "Unknown", AFTER_SECONDS + 30),
                node("back", "True", 5, taints=[OURS], annotations={ANNOTATION: timestamp(600)}),
            ]
        )
        self.assertEqual(self.deleted(), ["dead"])
        self.assertEqual(sorted(p["name"] for p in patches), ["back", "sleeping"])

    def test_is_off_when_the_threshold_is_empty(self) -> None:
        _, patches = self.run_pass([node("dead", "Unknown", PRUNE_AFTER_SECONDS * 10, labels=REMOVABLE)], NODE_RECOVERY_PRUNE_AFTER_SECONDS="")
        self.assertEqual(self.deletes(), [])
        self.assertEqual([p["name"] for p in patches], ["dead"])

    def test_is_off_when_the_threshold_is_not_set(self) -> None:
        self.run_pass([node("dead", "Unknown", PRUNE_AFTER_SECONDS * 10, labels=REMOVABLE)])
        self.assertEqual(self.deletes(), [])

    def test_rejects_a_prune_threshold_not_greater_than_the_taint_threshold(self) -> None:
        for threshold in (AFTER_SECONDS - 1, AFTER_SECONDS):
            with self.subTest(threshold=threshold):
                result, patches = self.run_prune([node("dead", "Unknown", AFTER_SECONDS * 10, labels=REMOVABLE)], env={"NODE_RECOVERY_PRUNE_AFTER_SECONDS": str(threshold)})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be greater than NODE_RECOVERY_AFTER_SECONDS", result.stderr)
                self.assertEqual((self.deletes(), patches), ([], []))

    def test_rejects_a_prune_threshold_that_is_not_a_whole_number(self) -> None:
        for threshold in ("ten minutes", "0", "-5", "1.5", "0600"):
            with self.subTest(threshold=threshold):
                result, _ = self.run_prune([node("dead", "Unknown", PRUNE_AFTER_SECONDS * 10, labels=REMOVABLE)], env={"NODE_RECOVERY_PRUNE_AFTER_SECONDS": threshold})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("NODE_RECOVERY_PRUNE_AFTER_SECONDS must be empty or a whole number", result.stderr)
                self.assertEqual(self.deletes(), [])

    def test_reaches_the_api_through_the_pods_service_address_by_default(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "NODE_RECOVERY_API_URL"}
        self.env = {**env, "KUBERNETES_SERVICE_HOST": "10.43.0.1", "KUBERNETES_SERVICE_PORT": "443"}
        self.run_prune([node("dead", "Unknown", PRUNE_AFTER_SECONDS + 1, labels=REMOVABLE)])
        self.assertEqual(self.deletes()[0]["url"], "https://10.43.0.1:443/api/v1/nodes/dead")
        self.env["KUBERNETES_SERVICE_HOST"] = "fd00::1"
        self.run_prune([node("dead2", "Unknown", PRUNE_AFTER_SECONDS + 1, labels=REMOVABLE)])
        self.assertEqual(self.deletes()[1]["url"], "https://[fd00::1]:443/api/v1/nodes/dead2")


if __name__ == "__main__":
    unittest.main()
