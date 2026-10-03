"""Tests for runner-hooks/job-completed.sh, run with a stand-in curl and a fake cgroup and service account directory, with the real bash and jq."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "runner-hooks" / "job-completed.sh"
PEAK_BYTES = 734003200
NAMESPACE = "arc-runners-example"

# Records the request it receives and answers with FAKE_CURL_STATUS, or fails outright when FAKE_CURL_FAILS is set.
FAKE_CURL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    if os.environ.get("FAKE_CURL_FAILS"):
        sys.exit(7)
    with open(os.environ["FAKE_CURL_REQUEST"], "w") as out:
        json.dump({"url": args[-1], "body": json.loads(args[args.index("-d") + 1]), "headers": [args[i + 1] for i, a in enumerate(args) if a == "-H"]}, out)
    sys.stdout.write(os.environ.get("FAKE_CURL_STATUS", "201"))
    """
)


class JobCompletedHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        curl = self.dir / "curl"
        curl.write_text(FAKE_CURL)
        curl.chmod(0o755)
        self.cgroup = self.dir / "cgroup"
        self.account = self.dir / "account"
        self.cgroup.mkdir()
        self.account.mkdir()
        (self.account / "namespace").write_text(NAMESPACE)
        (self.account / "token").write_text("example-token")
        self.request = self.dir / "request.json"

    def run_hook(self, memory_peak: str | None = str(PEAK_BYTES), **extra: str) -> subprocess.CompletedProcess[str]:
        if memory_peak is not None:
            (self.cgroup / "memory.peak").write_text(memory_peak + "\n")
        env = {
            **os.environ,
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}",
            "JOB_PEAK_CGROUP_DIR": str(self.cgroup),
            "JOB_PEAK_SERVICEACCOUNT_DIR": str(self.account),
            "JOB_PEAK_API_URL": "https://api.example",
            "FAKE_CURL_REQUEST": str(self.request),
            "HOSTNAME": "runner-pod-1",
            "GITHUB_REPOSITORY": "org/repo",
            "GITHUB_WORKFLOW_REF": "org/repo/.github/workflows/ci.yml@refs/heads/main",
            "GITHUB_JOB": "build",
            **extra,
        }
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)

    def test_the_peak_is_posted_as_an_event_in_the_pods_own_namespace(self) -> None:
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        request = json.loads(self.request.read_text())
        self.assertEqual(request["url"], f"https://api.example/api/v1/namespaces/{NAMESPACE}/events")
        self.assertIn("Authorization: Bearer example-token", request["headers"])
        event = request["body"]
        self.assertEqual(event["reason"], "JobMemoryPeak")
        self.assertEqual(event["involvedObject"], {"kind": "Pod", "name": "runner-pod-1", "namespace": NAMESPACE})
        self.assertEqual(
            json.loads(event["message"]),
            {"repo": "org/repo", "workflow": "org/repo/.github/workflows/ci.yml@refs/heads/main", "job": "build", "peak_bytes": PEAK_BYTES},
        )

    def test_a_missing_memory_peak_records_nothing_and_does_not_fail_the_job(self) -> None:
        result = self.run_hook(memory_peak=None)
        self.assertEqual(result.returncode, 0)
        self.assertIn("no memory.peak", result.stderr)
        self.assertFalse(self.request.exists())

    def test_a_non_numeric_memory_peak_records_nothing_and_does_not_fail_the_job(self) -> None:
        result = self.run_hook(memory_peak="max")
        self.assertEqual(result.returncode, 0)
        self.assertIn("not a number", result.stderr)
        self.assertFalse(self.request.exists())

    def test_a_refusal_from_the_api_server_does_not_fail_the_job(self) -> None:
        result = self.run_hook(FAKE_CURL_STATUS="403")
        self.assertEqual(result.returncode, 0)
        self.assertIn("answered 403", result.stderr)

    def test_an_unreachable_api_server_does_not_fail_the_job(self) -> None:
        result = self.run_hook(FAKE_CURL_FAILS="1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("no response", result.stderr)


if __name__ == "__main__":
    unittest.main()
