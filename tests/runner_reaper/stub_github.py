"""A stand-in for the parts of GitHub's API the runner reaper reads, run inside the test cluster by tests/runner_reaper/run.sh.

POST /app/installations/<id>/access_tokens answers 201 with a token, unless the installation id is the refused one, when it answers 422 as GitHub does for a permission the installation lacks. GET /repos/<repo>/actions/runs/<id> and its /jobs answer from RUNS below, with timestamps relative to the moment of the request; any other path answers 404. Each request is logged as one JSON line on stdout, so the test reads what the job asked for from the pod's log.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

REFUSED_INSTALLATION = "999"
REPOSITORY = "example-org/example-repo"
PORT = 8080
DAY = 86400

# Run id: (run status, conclusion, seconds since the run was last updated, jobs as (id, runner name, status, seconds since it started)).
RUNS = {
    201: ("in_progress", None, 0, [(9201, "hung-runner", "in_progress", 10 * DAY)]),
    202: ("completed", "cancelled", DAY, [(9202, "cancelled-runner", "completed", 2 * DAY)]),
    203: ("in_progress", None, 0, [(9203, "busy-runner", "in_progress", 60)]),
}


def ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 (the name http.server calls)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        segments = self.path.strip("/").split("/")
        installation = segments[2] if len(segments) == 4 and segments[3] == "access_tokens" else ""
        print(json.dumps({"method": "POST", "path": self.path, "body": body}), flush=True)
        if not installation:
            self.reply(404, {"message": "Not Found"})
        elif installation == REFUSED_INSTALLATION:
            self.reply(422, {"message": "The permissions requested are not granted to this installation."})
        else:
            self.reply(201, {"token": "ghs_stubreapertoken", "expires_at": ago(-3600)})

    def do_GET(self) -> None:  # noqa: N802 (the name http.server calls)
        print(json.dumps({"method": "GET", "path": self.path, "authorization": self.headers.get("Authorization", "")}), flush=True)
        path = urlsplit(self.path).path
        prefix = f"/repos/{REPOSITORY}/actions/runs/"
        segments = path.removeprefix(prefix).split("/") if path.startswith(prefix) else []
        run = RUNS.get(int(segments[0])) if segments and segments[0].isdigit() else None
        if run is None:
            self.reply(404, {"message": "Not Found"})
            return
        status, conclusion, updated, jobs = run
        if segments[1:] == []:
            self.reply(200, {"id": int(segments[0]), "status": status, "conclusion": conclusion, "updated_at": ago(updated)})
        elif segments[1:] == ["jobs"]:
            listed = [
                {"id": job_id, "runner_name": runner, "status": job_status, "conclusion": conclusion if job_status == "completed" else None,
                 "started_at": ago(started), "completed_at": ago(updated) if job_status == "completed" else None}
                for job_id, runner, job_status, started in jobs
            ]
            self.reply(200, {"total_count": len(listed), "jobs": listed})
        else:
            self.reply(404, {"message": "Not Found"})

    def reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: object) -> None:
        return


if __name__ == "__main__":
    HTTPServer(("", PORT), Handler).serve_forever()
