"""Tests for roles/github_runner_arc/files/reap-runners.sh, one pass of the runner reaper, run against scripted stand-ins for curl (GitHub's API) and kubectl with the real bash, jq and openssl."""

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
from typing import Any

FILES = Path(__file__).resolve().parents[2] / "roles" / "github_runner_arc" / "files"
SCRIPT = FILES / "reap-runners.sh"
TOKEN_LIB = FILES / "github-app-token.sh"

NAMESPACE = "example-runners"
REPO = "example-org/example-repo"
TOKEN = "ghs_examplereapertoken0123456789"
BOUND = 3600
GRACE = 300
API = "https://api.github.com"

# A stand-in for curl that answers from a scenario file and records each call. The scenario maps an API path (with its query) to [status, body], or to "unreachable" for a connection failure; a path it does not list answers 404.
FAKE_CURL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    scenario = json.load(open(os.environ["FAKE_SCENARIO"]))
    output = None
    config = ""
    data = None
    url = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-o", "-w", "-K", "-X", "-H", "--data"):
            value = args[i + 1]
            if arg == "-o":
                output = value
            elif arg == "-K":
                config = open(value).read()
            elif arg == "--data":
                data = value
            i += 2
            continue
        if not arg.startswith("-"):
            url = arg
        i += 1
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({"argv": args, "config": config, "data": data, "url": url}) + "\\n")
    path = url.removeprefix(os.environ["FAKE_API"])
    if path.endswith("/access_tokens"):
        answer = scenario["mint"]
    else:
        answer = scenario["paths"].get(path, [404, {"message": "Not Found"}])
    if answer == "unreachable":
        sys.stderr.write("curl: (7) Failed to connect\\n")
        sys.exit(7)
    status, body = answer
    if output:
        with open(output, "w") as handle:
            handle.write(json.dumps(body))
    sys.stdout.write(str(status))
    """
)

# Answers `kubectl get ephemeralrunners...` from a file and records every `kubectl delete`; FAKE_GET_EXIT and FAKE_DELETE_EXIT set their exit statuses.
FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_KUBECTL_LOG"], "a") as log:
        log.write(json.dumps(args) + "\\n")
    if args[:2] == ["get", "ephemeralrunners.actions.github.com"]:
        code = int(os.environ.get("FAKE_GET_EXIT", "0"))
        if code == 0:
            sys.stdout.write(open(os.environ["FAKE_RUNNERS"]).read())
        sys.exit(code)
    if args[:2] == ["delete", "ephemeralrunners.actions.github.com"]:
        sys.exit(int(os.environ.get("FAKE_DELETE_EXIT", "0")))
    sys.stderr.write("unexpected kubectl call: %s\\n" % args)
    sys.exit(2)
    """
)


def timestamp(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def runner(name: str, run_id: int | None = None, repository: str = REPO, deleting: bool = False) -> dict[str, Any]:
    """An EphemeralRunner as ARC records it: a runner the listener assigned a job carries the job's run and repository in its status."""
    metadata: dict[str, Any] = {"name": name, "namespace": NAMESPACE}
    if deleting:
        metadata["deletionTimestamp"] = timestamp(10)
    status: dict[str, Any] = {"phase": "Running", "runnerName": name}
    if run_id is not None:
        status.update({"workflowRunId": run_id, "jobRepositoryName": repository, "jobRequestId": run_id * 10})
    return {"apiVersion": "actions.github.com/v1alpha1", "kind": "EphemeralRunner", "metadata": metadata, "status": status}


def run(status: str, conclusion: str | None = None, updated_seconds_ago: int = 0) -> list[Any]:
    return [200, {"status": status, "conclusion": conclusion, "updated_at": timestamp(updated_seconds_ago)}]


def job(job_id: int, runner_name: str, status: str, started_seconds_ago: int | None = None, completed_seconds_ago: int | None = None, conclusion: str | None = None) -> dict[str, Any]:
    return {
        "id": job_id,
        "runner_name": runner_name,
        "status": status,
        "conclusion": conclusion,
        "started_at": None if started_seconds_ago is None else timestamp(started_seconds_ago),
        "completed_at": None if completed_seconds_ago is None else timestamp(completed_seconds_ago),
    }


def run_path(run_id: int) -> str:
    return f"/repos/{REPO}/actions/runs/{run_id}"


def jobs_path(run_id: int, page: int = 1) -> str:
    return f"{run_path(run_id)}/jobs?filter=all&per_page=100&page={page}"


class ReapRunnersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for tool in ("bash", "jq", "openssl"):
            if shutil.which(tool) is None:
                raise unittest.SkipTest(f"{tool} is not installed")
        cls.keys = tempfile.TemporaryDirectory()
        cls.key = Path(cls.keys.name) / "key.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(cls.key), "2048"], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.keys.cleanup()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, body in (("curl", FAKE_CURL), ("kubectl", FAKE_KUBECTL)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(0o755)
        # The ConfigMap mounts the script and the token minting side by side, so the tests run them from one directory too.
        self.script = root / "reaper" / SCRIPT.name
        self.script.parent.mkdir()
        shutil.copy(SCRIPT, self.script)
        shutil.copy(TOKEN_LIB, self.script.parent / TOKEN_LIB.name)
        self.app = root / "app"
        self.app.mkdir()
        (self.app / "github_app_id").write_text("12345")
        (self.app / "github_app_installation_id").write_text("67890\n")
        (self.app / "github_app_private_key").write_text(self.key.read_text())
        self.curl_log = root / "curl.jsonl"
        self.kubectl_log = root / "kubectl.jsonl"
        self.runners_path = root / "runners.json"
        self.scenario_path = root / "scenario.json"
        self.runners: list[dict[str, Any]] = []
        self.scenario: dict[str, Any] = {"mint": [201, {"token": TOKEN, "expires_at": "2030-01-01T00:00:00Z"}], "paths": {}}

    def tearDown(self) -> None:
        self.directory.cleanup()

    def reap(self, **env_overrides: str) -> subprocess.CompletedProcess[str]:
        self.runners_path.write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": self.runners}))
        self.scenario_path.write_text(json.dumps(self.scenario))
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_SCENARIO": str(self.scenario_path),
            "FAKE_LOG": str(self.curl_log),
            "FAKE_API": API,
            "FAKE_KUBECTL_LOG": str(self.kubectl_log),
            "FAKE_RUNNERS": str(self.runners_path),
            "GITHUB_APP_DIR": str(self.app),
            "RUNNER_NAMESPACE": NAMESPACE,
            "MAX_JOB_SECONDS": str(BOUND),
            "COMPLETED_GRACE_SECONDS": str(GRACE),
            **env_overrides,
        }
        return subprocess.run(["bash", str(self.script)], env=env, capture_output=True, text=True)

    def curl_calls(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.curl_log.read_text().splitlines()] if self.curl_log.exists() else []

    def kubectl_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.kubectl_log.read_text().splitlines()] if self.kubectl_log.exists() else []

    def deleted(self) -> list[str]:
        return [call[2] for call in self.kubectl_calls() if call[0] == "delete"]

    def test_mints_a_token_restricted_to_actions_read(self) -> None:
        self.reap()
        mint = self.curl_calls()[0]
        self.assertEqual(mint["url"], f"{API}/app/installations/67890/access_tokens")
        self.assertEqual(json.loads(mint["data"]), {"permissions": {"actions": "read"}})

    def test_an_idle_runner_is_never_deleted_or_looked_up(self) -> None:
        self.runners = [runner("idle-runner")]
        result = self.reap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), [])
        self.assertEqual([call["url"] for call in self.curl_calls()][1:], [])
        self.assertIn("1 idle", result.stderr)

    def test_a_runner_whose_run_was_cancelled_is_deleted_and_logged(self) -> None:
        self.runners = [runner("stuck-runner", run_id=101)]
        self.scenario["paths"][run_path(101)] = run("completed", "cancelled", updated_seconds_ago=GRACE * 2)
        result = self.reap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), ["stuck-runner"])
        [delete] = [call for call in self.kubectl_calls() if call[0] == "delete"]
        self.assertEqual(delete, ["delete", "ephemeralrunners.actions.github.com", "stuck-runner", "-n", NAMESPACE, "--wait=false"])
        self.assertIn(f"deleted {NAMESPACE}/stuck-runner: run 101 in {REPO} is cancelled", result.stderr)

    def test_a_run_completed_inside_the_grace_keeps_its_runner(self) -> None:
        self.runners = [runner("exiting-runner", run_id=102)]
        self.scenario["paths"][run_path(102)] = run("completed", "success", updated_seconds_ago=GRACE // 2)
        result = self.reap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), [])
        self.assertIn("inside the grace", result.stderr)

    def test_a_job_running_past_the_bound_has_its_runner_deleted(self) -> None:
        self.runners = [runner("hung-runner", run_id=103)]
        self.scenario["paths"][run_path(103)] = run("in_progress")
        self.scenario["paths"][jobs_path(103)] = [200, {"total_count": 1, "jobs": [job(9001, "hung-runner", "in_progress", started_seconds_ago=BOUND * 2)]}]
        result = self.reap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), ["hung-runner"])
        self.assertIn(f"run 103 in {REPO}: job 9001 has been in_progress for", result.stderr)
        self.assertIn(f"over the bound of {BOUND}s", result.stderr)

    def test_a_job_within_the_bound_keeps_its_runner(self) -> None:
        self.runners = [runner("busy-runner", run_id=104)]
        self.scenario["paths"][run_path(104)] = run("in_progress")
        self.scenario["paths"][jobs_path(104)] = [200, {"total_count": 1, "jobs": [job(9002, "busy-runner", "in_progress", started_seconds_ago=BOUND // 2)]}]
        result = self.reap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), [])
        self.assertIn("within the bound", result.stderr)

    def test_a_completed_job_in_a_running_run_has_its_runner_deleted(self) -> None:
        self.runners = [runner("finished-runner", run_id=105)]
        self.scenario["paths"][run_path(105)] = run("in_progress")
        self.scenario["paths"][jobs_path(105)] = [200, {"total_count": 2, "jobs": [
            job(9003, "other-runner", "in_progress", started_seconds_ago=10),
            job(9004, "finished-runner", "completed", started_seconds_ago=GRACE * 4, completed_seconds_ago=GRACE * 2, conclusion="success"),
        ]}]
        result = self.reap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), ["finished-runner"])
        self.assertIn("job 9004 is success", result.stderr)

    def test_the_runners_job_is_found_on_a_later_page(self) -> None:
        self.runners = [runner("paged-runner", run_id=106)]
        self.scenario["paths"][run_path(106)] = run("in_progress")
        self.scenario["paths"][jobs_path(106, 1)] = [200, {"total_count": 101, "jobs": [job(n, f"runner-{n}", "in_progress", started_seconds_ago=10) for n in range(100)]}]
        self.scenario["paths"][jobs_path(106, 2)] = [200, {"total_count": 101, "jobs": [job(9005, "paged-runner", "in_progress", started_seconds_ago=BOUND * 2)]}]
        result = self.reap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), ["paged-runner"])

    def test_a_runner_no_job_names_is_kept_after_the_last_page(self) -> None:
        self.runners = [runner("unnamed-runner", run_id=107)]
        self.scenario["paths"][run_path(107)] = run("queued")
        self.scenario["paths"][jobs_path(107)] = [200, {"total_count": 1, "jobs": [job(9006, "someone-else", "queued")]}]
        result = self.reap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), [])
        self.assertIn("none of its jobs names this runner", result.stderr)
        self.assertEqual([call["url"] for call in self.curl_calls()][-1], API + jobs_path(107))

    def test_a_runner_already_being_deleted_is_left_alone(self) -> None:
        self.runners = [runner("leaving-runner", run_id=108, deleting=True)]
        self.scenario["paths"][run_path(108)] = run("completed", "cancelled", updated_seconds_ago=GRACE * 2)
        result = self.reap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), [])

    def test_a_refused_mint_deletes_nothing_and_names_the_permission(self) -> None:
        self.runners = [runner("stuck-runner", run_id=101)]
        self.scenario["paths"][run_path(101)] = run("completed", "cancelled", updated_seconds_ago=GRACE * 2)
        self.scenario["mint"] = [422, {"message": "The permissions requested are not granted to this installation."}]
        result = self.reap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("The App needs the actions: read permission", result.stderr)
        self.assertIn("deleted nothing", result.stderr)
        self.assertEqual(self.kubectl_calls(), [])

    def test_unreachable_github_deletes_nothing(self) -> None:
        self.runners = [runner("stuck-runner", run_id=101)]
        self.scenario["mint"] = "unreachable"
        result = self.reap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not reach the GitHub API", result.stderr)
        self.assertEqual(self.kubectl_calls(), [])

    def test_a_failed_listing_deletes_nothing(self) -> None:
        result = self.reap(FAKE_GET_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"could not list the EphemeralRunners in {NAMESPACE}; deleted nothing", result.stderr)
        self.assertEqual(self.deleted(), [])

    def test_an_unreadable_run_leaves_its_runner_and_fails_after_judging_the_rest(self) -> None:
        self.runners = [runner("unknown-runner", run_id=109), runner("offline-runner", run_id=110), runner("stuck-runner", run_id=101)]
        self.scenario["paths"][run_path(110)] = "unreachable"
        self.scenario["paths"][run_path(101)] = run("completed", "cancelled", updated_seconds_ago=GRACE * 2)
        result = self.reap()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.deleted(), ["stuck-runner"])
        self.assertIn("unknown-runner: reading run 109 in", result.stderr)
        self.assertIn("returned HTTP 404", result.stderr)
        self.assertIn("offline-runner: could not reach GitHub", result.stderr)
        self.assertIn("2 runner(s) could not be judged", result.stderr)

    def test_an_unreadable_job_list_leaves_its_runner(self) -> None:
        self.runners = [runner("hung-runner", run_id=111)]
        self.scenario["paths"][run_path(111)] = run("in_progress")
        self.scenario["paths"][jobs_path(111)] = [500, {"message": "Server Error"}]
        result = self.reap()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.deleted(), [])

    def test_a_dry_run_deletes_nothing_but_says_what_it_would(self) -> None:
        self.runners = [runner("stuck-runner", run_id=101)]
        self.scenario["paths"][run_path(101)] = run("completed", "cancelled", updated_seconds_ago=GRACE * 2)
        result = self.reap(DRY_RUN="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.deleted(), [])
        self.assertIn(f"would delete {NAMESPACE}/stuck-runner: run 101", result.stderr)

    def test_a_failed_delete_is_reported(self) -> None:
        self.runners = [runner("stuck-runner", run_id=101)]
        self.scenario["paths"][run_path(101)] = run("completed", "cancelled", updated_seconds_ago=GRACE * 2)
        result = self.reap(FAKE_DELETE_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stuck-runner: could not delete it", result.stderr)

    def test_a_bad_recorded_repository_is_never_requested(self) -> None:
        self.runners = [runner("odd-runner", run_id=112, repository="../../app/installations")]
        result = self.reap()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.curl_calls()), 1)
        self.assertEqual(self.deleted(), [])

    def test_a_bound_that_is_not_a_positive_number_fails_before_any_request(self) -> None:
        for bound in ("0", "", "1h"):
            with self.subTest(bound=bound):
                result = self.reap(MAX_JOB_SECONDS=bound)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.curl_calls(), [])

    def test_the_token_never_reaches_an_argument_or_the_output(self) -> None:
        self.runners = [runner("stuck-runner", run_id=101)]
        self.scenario["paths"][run_path(101)] = run("completed", "cancelled", updated_seconds_ago=GRACE * 2)
        result = self.reap()
        for call in self.curl_calls():
            self.assertNotIn(TOKEN, " ".join(call["argv"]))
        self.assertIn(f"Bearer {TOKEN}", self.curl_calls()[1]["config"])
        for call in self.kubectl_calls():
            self.assertNotIn(TOKEN, " ".join(call))
        self.assertNotIn(TOKEN, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
