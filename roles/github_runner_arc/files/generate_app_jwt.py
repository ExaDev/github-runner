#!/usr/bin/env python3
"""Print a short-lived RS256 JSON Web Token that authenticates as a GitHub App.

Usage: generate_app_jwt.py <app id> <private key file>

The claims follow GitHub's documented App authentication: iss is the App id, iat is backdated a minute to absorb clock drift, and exp stays inside GitHub's ten-minute limit. The key is read from a file rather than an argument so it never appears in a process listing, and `openssl dgst -sign` does the RSA signature so that no Python crypto package needs installing next to Ansible. Only the token goes to stdout.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time

# GitHub rejects an App JWT whose exp is more than ten minutes after iat; these keep well inside that.
ISSUED_AT_BACKDATE_SECONDS = 60
LIFETIME_SECONDS = 540


def b64url(data: bytes) -> str:
    """Encode bytes as unpadded base64url, the encoding JWTs use (RFC 7515)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_jwt(app_id: str, key_path: str, now: int) -> str:
    """Return a signed App JWT for app_id, signed with the PEM key at key_path, issued relative to now."""
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iat": now - ISSUED_AT_BACKDATE_SECONDS, "exp": now + LIFETIME_SECONDS, "iss": str(app_id)}
    signing_input = ".".join(b64url(json.dumps(part, separators=(",", ":")).encode("utf-8")) for part in (header, claims))
    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_path, "-binary"],
        input=signing_input.encode("utf-8"),
        capture_output=True,
        check=True,
    ).stdout
    return f"{signing_input}.{b64url(signature)}"


def main(argv: list[str]) -> int:
    """Print the JWT for the App id and key file named in argv."""
    if len(argv) != 3:
        print("usage: generate_app_jwt.py <app id> <private key file>", file=sys.stderr)
        return 2
    print(build_jwt(argv[1], argv[2], int(time.time())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
