"""Tests for scripts/fleet-alert.sh, one pass of the fleet-down notifier, run against a scripted stand-in for curl with the real bash and jq."""

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

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fleet-alert.sh"

API = "https://api.example.test"
GIST_ID = "0123456789abcdef"
ISSUE_REPO = "example-org/example-repo"
ISSUE_TOKEN = "ghs_exampleissuetoken"
WEBHOOK_URL = "https://hooks.example.test/services/secret-path"
AFTER_SECONDS = 900
MARKER = "<!-- github-runner-fleet-alert -->"

# A stand-in for curl that reads the config the script pipes to `curl -K -` and answers from files: the gist from FAKE_GIST (refused when FAKE_GIST_FAIL is set), the repository's open issues from FAKE_ISSUES served a hundred to a page as GitHub does, and any POST or PATCH with a 2xx unless FAKE_DELIVERY_FAIL is set. Every call is appended to FAKE_LOG with its method, URL, Authorization header and body, and its argv, which must carry no URL or token.
FAKE_CURL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, re, sys
    config = sys.stdin.read()
    def values(key):
        return [m.group(1) for m in re.finditer(r'^' + key + r' = "(.*)"$', config, re.M)]
    url = values("url")[0]
    method = values("request")[0]
    headers = values("header")
    auth = next((h for h in headers if h.startswith("Authorization: ")), None)
    data = values("data-binary")
    body = open(data[0].removeprefix("@")).read() if data else None
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({"argv": sys.argv[1:], "method": method, "url": url, "auth": auth, "body": body}) + "\\n")
    api = os.environ["FAKE_API"]
    if url.startswith(api + "/gists/"):
        if os.environ.get("FAKE_GIST_FAIL"):
            sys.exit(22)
        sys.stdout.write(open(os.environ["FAKE_GIST"]).read())
    elif method == "GET" and "/issues?" in url:
        page = int(re.search(r"[?&]page=(\\d+)", url).group(1))
        sys.stdout.write(json.dumps(json.load(open(os.environ["FAKE_ISSUES"]))[(page - 1) * 100:page * 100]))
    elif os.environ.get("FAKE_DELIVERY_FAIL"):
        sys.exit(22)
    else:
        sys.stdout.write("{}")
    """
)


def stamp(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def health(
    *,
    heartbeat_age: int = 0,
    unhealthy_for: int | None = None,
    reasons: tuple[str, ...] = ("scale set arc-runners-a/runners-a has no running listener",),
    not_ready: tuple[tuple[str, bool, int], ...] = (),
    stepped_down: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The document scripts/heartbeat.sh publishes: when it was written, whether the fleet is vouched for and since when not, the nodes not Ready as (name, is a server, seconds not Ready), and, of those, the names regrading has stepped down on purpose."""
    return {
        "healthy": unhealthy_for is None,
        "reasons": [] if unhealthy_for is None else list(reasons),
        "unhealthy_since": None if unhealthy_for is None else stamp(unhealthy_for),
        "not_ready_nodes": [{"node": name, "control_plane": server, "regrade_level": "etcd-only" if name in stepped_down else None, "since": stamp(seconds)} for name, server, seconds in not_ready],
        "node_pressure": [],
        "deployed": {"platform": None, "nodes": {}},
        "at": stamp(heartbeat_age),
    }


def open_issue(number: int, body: str = MARKER) -> dict[str, Any]:
    return {"number": number, "title": "Runner fleet is down", "body": body}


class FleetAlertTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for tool in ("bash", "jq"):
            if shutil.which(tool) is None:
                raise unittest.SkipTest(f"{tool} is not installed")

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"
        curl.write_text(FAKE_CURL)
        curl.chmod(0o755)
        self.bin = bin_dir
        self.log = self.root / "curl.jsonl"
        self.state = self.root / "state.json"

    def alert(self, document: dict[str, Any] | None, issues: list[dict[str, Any]] | None = None, **env: str) -> subprocess.CompletedProcess[str]:
        gist = self.root / "gist.json"
        content = {} if document is None else {"fleet-health.json": {"content": json.dumps(document)}}
        gist.write_text(json.dumps({"files": content}))
        issues_file = self.root / "issues.json"
        issues_file.write_text(json.dumps(issues or []))
        environment = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_API": API,
            "FAKE_LOG": str(self.log),
            "FAKE_GIST": str(gist),
            "FAKE_ISSUES": str(issues_file),
            "FLEET_ALERT_API_URL": API,
            "FLEET_ALERT_GIST_ID": GIST_ID,
            "FLEET_ALERT_AFTER_SECONDS": str(AFTER_SECONDS),
            **env,
        }
        return subprocess.run(["bash", str(SCRIPT)], env=environment, capture_output=True, text=True, check=False)

    def issue_env(self) -> dict[str, str]:
        return {"FLEET_ALERT_ISSUE_REPO": ISSUE_REPO, "FLEET_ALERT_ISSUE_TOKEN": ISSUE_TOKEN}

    def webhook_env(self) -> dict[str, str]:
        return {"FLEET_ALERT_WEBHOOK_URL": WEBHOOK_URL, "FLEET_ALERT_STATE_FILE": str(self.state)}

    def calls(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def writes(self) -> list[dict[str, Any]]:
        return [call for call in self.calls() if call["method"] != "GET"]

    # What counts as an outage.

    def test_a_healthy_fleet_delivers_nothing(self) -> None:
        result = self.alert(health(), **self.issue_env(), **self.webhook_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.writes(), [])

    def test_a_stale_heartbeat_is_an_outage(self) -> None:
        result = self.alert(health(heartbeat_age=AFTER_SECONDS + 1), **self.issue_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the heartbeat has not published since", result.stdout)
        self.assertEqual([call["method"] for call in self.writes()], ["POST"])

    def test_a_heartbeat_inside_the_window_is_not_stale(self) -> None:
        result = self.alert(health(heartbeat_age=AFTER_SECONDS - 60), **self.issue_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.writes(), [])

    def test_a_server_not_ready_for_the_window_is_an_outage(self) -> None:
        result = self.alert(health(not_ready=(("server-a", True, AFTER_SECONDS + 1),)), **self.issue_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("server node server-a is not Ready", result.stdout)
        self.assertEqual(len(self.writes()), 1)

    def test_a_server_not_ready_for_less_than_the_window_is_not(self) -> None:
        result = self.alert(health(not_ready=(("server-a", True, AFTER_SECONDS - 60),)), **self.issue_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.writes(), [])

    def test_a_server_regrading_stepped_down_is_not_an_outage_however_long_it_stays(self) -> None:
        result = self.alert(health(not_ready=(("server-a", True, AFTER_SECONDS * 10),), stepped_down=("server-a",)), **self.issue_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.writes(), [])
        self.assertIn("no outage", result.stdout)

    def test_only_the_stepped_down_server_is_spared(self) -> None:
        result = self.alert(health(not_ready=(("server-a", True, AFTER_SECONDS + 1), ("server-b", True, AFTER_SECONDS + 1)), stepped_down=("server-a",)), **self.issue_env())
        self.assertIn("server node server-b is not Ready", result.stdout)
        self.assertNotIn("server node server-a is not Ready", result.stdout)

    def test_a_heartbeat_that_predates_the_level_field_still_alerts(self) -> None:
        document = health(not_ready=(("server-a", True, AFTER_SECONDS + 1),))
        for entry in document["not_ready_nodes"]:
            del entry["regrade_level"]
        result = self.alert(document, **self.issue_env())
        self.assertIn("server node server-a is not Ready", result.stdout)

    def test_a_worker_not_ready_for_the_window_is_not_an_outage(self) -> None:
        result = self.alert(health(not_ready=(("agent-a", False, AFTER_SECONDS * 10),)), **self.issue_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.writes(), [])

    def test_a_fleet_not_vouched_for_over_the_window_is_an_outage_and_names_why(self) -> None:
        result = self.alert(health(unhealthy_for=AFTER_SECONDS + 1), **self.issue_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("has no running listener", result.stdout)
        self.assertIn("has no running listener", json.loads(self.writes()[0]["body"])["body"])

    def test_a_fleet_not_vouched_for_over_less_than_the_window_is_not(self) -> None:
        result = self.alert(health(unhealthy_for=AFTER_SECONDS - 60), **self.issue_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.writes(), [])

    # Issue delivery.

    def test_an_outage_opens_one_issue_carrying_the_marker(self) -> None:
        self.alert(health(unhealthy_for=AFTER_SECONDS + 1), **self.issue_env())
        (write,) = self.writes()
        self.assertEqual((write["method"], write["url"]), ("POST", f"{API}/repos/{ISSUE_REPO}/issues"))
        body = json.loads(write["body"])
        self.assertEqual(body["title"], "Runner fleet is down")
        self.assertIn(MARKER, body["body"])

    def test_an_outage_already_announced_does_not_open_another_issue(self) -> None:
        result = self.alert(health(unhealthy_for=AFTER_SECONDS + 1), issues=[open_issue(7)], **self.issue_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.writes(), [])

    def test_an_open_issue_without_the_marker_and_a_pull_request_are_not_the_alert(self) -> None:
        issues = [open_issue(3, body="an unrelated report"), {**open_issue(4), "pull_request": {}}]
        self.alert(health(unhealthy_for=AFTER_SECONDS + 1), issues=issues, **self.issue_env())
        self.assertEqual([call["method"] for call in self.writes()], ["POST"])

    def test_recovery_comments_on_and_closes_the_open_issue(self) -> None:
        result = self.alert(health(), issues=[open_issue(7)], **self.issue_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        comment, close = self.writes()
        self.assertEqual((comment["method"], comment["url"]), ("POST", f"{API}/repos/{ISSUE_REPO}/issues/7/comments"))
        self.assertIn("recovered", json.loads(comment["body"])["body"])
        self.assertEqual((close["method"], close["url"]), ("PATCH", f"{API}/repos/{ISSUE_REPO}/issues/7"))
        self.assertEqual(json.loads(close["body"]), {"state": "closed", "state_reason": "completed"})

    def test_recovery_with_no_open_issue_does_nothing(self) -> None:
        self.alert(health(), **self.issue_env())
        self.assertEqual(self.writes(), [])

    def test_the_marker_is_found_beyond_the_first_page_of_issues(self) -> None:
        issues = [open_issue(number, body="other") for number in range(100)] + [open_issue(500)]
        result = self.alert(health(), issues=issues, **self.issue_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["url"] for call in self.writes()][-1], f"{API}/repos/{ISSUE_REPO}/issues/500")

    def test_the_issue_token_is_sent_to_the_issue_api_and_never_on_a_command_line(self) -> None:
        self.alert(health(unhealthy_for=AFTER_SECONDS + 1), **self.issue_env())
        issue_calls = [call for call in self.calls() if "/repos/" in call["url"]]
        self.assertTrue(issue_calls)
        for call in issue_calls:
            self.assertEqual(call["auth"], f"Authorization: Bearer {ISSUE_TOKEN}")
        for call in self.calls():
            self.assertEqual(call["argv"], ["-K", "-"])

    def test_the_gist_is_read_without_a_token_unless_one_is_given(self) -> None:
        self.alert(health(), **self.issue_env())
        self.assertIsNone(self.calls()[0]["auth"])
        self.log.unlink()
        self.alert(health(), FLEET_ALERT_GIST_TOKEN="gist-read-token", **self.issue_env())
        self.assertEqual(self.calls()[0]["auth"], "Authorization: Bearer gist-read-token")

    # Webhook delivery.

    def test_an_outage_posts_the_webhook_once_and_a_second_pass_stays_quiet(self) -> None:
        document = health(unhealthy_for=AFTER_SECONDS + 1)
        first = self.alert(document, **self.webhook_env())
        self.assertEqual(first.returncode, 0, first.stderr)
        (write,) = self.writes()
        self.assertEqual((write["method"], write["url"]), ("POST", WEBHOOK_URL))
        self.assertIsNone(write["auth"])
        payload = json.loads(write["body"])
        self.assertEqual(payload["event"], "firing")
        self.assertIn("The runner fleet is down", payload["text"])
        self.assertEqual(payload["conditions"][0]["kind"], "fleet_not_vouched")
        second = self.alert(document, **self.webhook_env())
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.writes()), 1)

    def test_recovery_posts_a_resolved_webhook_once(self) -> None:
        self.alert(health(unhealthy_for=AFTER_SECONDS + 1), **self.webhook_env())
        self.alert(health(), **self.webhook_env())
        self.alert(health(), **self.webhook_env())
        events = [json.loads(call["body"])["event"] for call in self.writes()]
        self.assertEqual(events, ["firing", "resolved"])

    def test_a_healthy_fleet_that_never_alerted_sends_no_resolved_webhook(self) -> None:
        self.alert(health(), **self.webhook_env())
        self.assertEqual(self.writes(), [])

    def test_a_failed_webhook_is_not_recorded_so_the_next_pass_retries(self) -> None:
        document = health(unhealthy_for=AFTER_SECONDS + 1)
        failed = self.alert(document, FAKE_DELIVERY_FAIL="1", **self.webhook_env())
        self.assertEqual(failed.returncode, 1)
        self.assertFalse(self.state.exists())
        retried = self.alert(document, **self.webhook_env())
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(json.loads(self.state.read_text()), {"firing": True})

    def test_a_failed_issue_delivery_does_not_stop_the_webhook(self) -> None:
        document = health(unhealthy_for=AFTER_SECONDS + 1)
        result = self.alert(document, FAKE_DELIVERY_FAIL="1", **self.issue_env(), **self.webhook_env())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len([call for call in self.writes() if call["url"] == WEBHOOK_URL]), 1)

    def test_both_deliveries_can_be_configured_together(self) -> None:
        result = self.alert(health(unhealthy_for=AFTER_SECONDS + 1), **self.issue_env(), **self.webhook_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(call["url"] for call in self.writes()), sorted([WEBHOOK_URL, f"{API}/repos/{ISSUE_REPO}/issues"]))

    def test_with_no_delivery_configured_it_reports_and_sends_nothing(self) -> None:
        result = self.alert(health(unhealthy_for=AFTER_SECONDS + 1))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("firing:", result.stdout)
        self.assertIn("nothing sent", result.stdout)
        self.assertEqual(self.writes(), [])

    # Refusing what it cannot judge.

    def test_an_unreadable_gist_fails(self) -> None:
        result = self.alert(health(), FAKE_GIST_FAIL="1", **self.issue_env())
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not read gist", result.stderr)
        self.assertEqual(self.writes(), [])

    def test_a_gist_without_the_health_file_fails(self) -> None:
        result = self.alert(None, **self.issue_env())
        self.assertEqual(result.returncode, 1)
        self.assertIn("no readable fleet-health.json", result.stderr)

    def test_a_heartbeat_that_predates_the_onset_fields_fails_naming_the_remedy(self) -> None:
        old = health(unhealthy_for=AFTER_SECONDS + 1)
        del old["not_ready_nodes"]
        del old["unhealthy_since"]
        result = self.alert(old, **self.issue_env())
        self.assertEqual(result.returncode, 1)
        self.assertIn("update the heartbeat image", result.stderr)
        self.assertEqual(self.writes(), [])

    def test_the_settings_are_validated_before_anything_is_read(self) -> None:
        cases = {
            "FLEET_ALERT_AFTER_SECONDS": ("ten minutes", "whole number"),
            "FLEET_ALERT_ISSUE_REPO": ("not-a-repo", "owner/repo"),
            "FLEET_ALERT_WEBHOOK_URL": ("ftp://hooks.example.test", "http(s) URL"),
        }
        for name, (value, message) in cases.items():
            with self.subTest(name):
                result = self.alert(health(), **{name: value}, FLEET_ALERT_ISSUE_TOKEN=ISSUE_TOKEN, FLEET_ALERT_STATE_FILE=str(self.state))
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
        self.assertEqual(self.calls(), [])

    def test_an_issue_repository_needs_a_token_and_a_webhook_needs_a_state_file(self) -> None:
        no_token = self.alert(health(), FLEET_ALERT_ISSUE_REPO=ISSUE_REPO)
        self.assertEqual(no_token.returncode, 1)
        self.assertIn("FLEET_ALERT_ISSUE_TOKEN", no_token.stderr)
        no_state = self.alert(health(), FLEET_ALERT_WEBHOOK_URL=WEBHOOK_URL)
        self.assertEqual(no_state.returncode, 1)
        self.assertIn("FLEET_ALERT_STATE_FILE", no_state.stderr)

    def test_a_gist_id_is_required(self) -> None:
        result = self.alert(health(), FLEET_ALERT_GIST_ID="")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FLEET_ALERT_GIST_ID", result.stderr)


if __name__ == "__main__":
    unittest.main()
