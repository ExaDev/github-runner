"""A stand-in for GitHub's installation token endpoint, run inside the test cluster by tests/pull_secret_renewal/run.sh.

POST /app/installations/<id>/access_tokens answers 201 with a new token each time, numbered so the test can tell which one a Secret holds, unless the installation id is the refused one, when it answers 422 as GitHub does for a permission the installation lacks. Each request is logged as one JSON line on stdout, so the test reads what the job sent from the pod's log.
"""

from __future__ import annotations

import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

REFUSED_INSTALLATION = "999"
EXPIRES_AT = "2030-01-01T00:00:00Z"
PORT = 8080


def jwt_claims(authorization: str) -> dict:
    """Return the claims of the bearer JWT in an Authorization header, or an empty dict."""
    parts = authorization.removeprefix("Bearer ").split(".")
    if len(parts) != 3:
        return {}
    return json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))


class Handler(BaseHTTPRequestHandler):
    issued = 0

    def do_POST(self) -> None:  # noqa: N802 (the name http.server calls)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        segments = self.path.strip("/").split("/")
        installation = segments[2] if len(segments) == 4 and segments[3] == "access_tokens" else ""
        print(json.dumps({"path": self.path, "body": body, "claims": jwt_claims(self.headers.get("Authorization", ""))}), flush=True)
        if not installation:
            self.reply(404, {"message": "Not Found"})
        elif installation == REFUSED_INSTALLATION:
            self.reply(422, {"message": "The permissions requested are not granted to this installation."})
        else:
            Handler.issued += 1
            self.reply(201, {"token": f"ghs_stubtoken{Handler.issued}", "expires_at": EXPIRES_AT})

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
