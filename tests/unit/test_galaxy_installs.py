"""Checks that every ansible-galaxy install that reaches a Galaxy server runs without the server response cache.

ansible-core's _call_galaxy (ansible/galaxy/api.py) writes an empty cache entry before it sends a request, and fills it only when the request succeeds. The call is retried on a 429, 502 or 520, and the retry finds the empty entry, which is unexpired, and fails with "Missing expected 'results' in ansible-galaxy cache". --no-cache leaves the cache out, so the retry asks the server again.
"""

from __future__ import annotations

import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from ansible.errors import AnsibleError
from ansible.galaxy import api

ROOT = Path(__file__).resolve().parents[2]
INSTALL = re.compile(r"ansible-galaxy\"? collection install(?P<args>[^\n]*)")


def install_lines() -> list[tuple[str, str]]:
    """Every ansible-galaxy collection install in the workflows and shell scripts that resolves from a server: (file, arguments). An install of a local tarball reaches no server."""
    files = sorted((ROOT / ".github" / "workflows").glob("*.yml")) + sorted(ROOT.glob("*.sh"))
    found = []
    for path in files:
        for match in INSTALL.finditer(path.read_text()):
            if '"$tarball"' not in match.group("args"):
                found.append((str(path.relative_to(ROOT)), match.group("args")))
    return found


class GalaxyInstallTest(unittest.TestCase):
    def test_every_install_from_a_server_runs_without_the_response_cache(self) -> None:
        lines = install_lines()
        self.assertGreater(len(lines), 0)
        for path, args in lines:
            with self.subTest(path=path, args=args):
                self.assertIn("--no-cache", args.split())

    def call_after_one_bad_gateway(self, no_cache: bool) -> tuple[list[str], BaseException | None]:
        """Call the Galaxy API through ansible-core's own _call_galaxy, whose first request answers 502: the requests it sent, and what it raised (None when it succeeded)."""
        calls: list[str] = []

        def open_url(url: str, **_: object) -> io.BytesIO:
            calls.append(url)
            if len(calls) == 1:
                raise HTTPError(url, 502, "Bad Gateway", {}, io.BytesIO(b"{}"))  # type: ignore[arg-type]
            return io.BytesIO(b'{"name": "example"}')

        with tempfile.TemporaryDirectory() as cache, mock.patch.object(api, "open_url", open_url), mock.patch.object(api.C, "GALAXY_CACHE_DIR", cache), mock.patch("ansible.module_utils.api.time.sleep"):
            server = api.GalaxyAPI(mock.MagicMock(), "galaxy", "https://galaxy.example/api/", no_cache=no_cache)
            try:
                server._call_galaxy("https://galaxy.example/api/v3/collections/example/example/", cache=True)
            # What it raised is the result under test: a KeyError on older ansible-core, an AnsibleError wrapping it on newer.
            except (KeyError, AnsibleError) as error:
                return calls, error
        return calls, None

    def test_without_the_cache_a_retried_request_is_sent_again(self) -> None:
        calls, error = self.call_after_one_bad_gateway(no_cache=True)
        self.assertIsNone(error)
        self.assertEqual(len(calls), 2)

    def test_with_the_cache_a_retried_request_reads_the_empty_entry(self) -> None:
        # The behaviour --no-cache exists to avoid: the retry sends nothing and fails on the entry the failed request left. When an ansible-core release stops caching a failed request, this fails and the flag and this test can go.
        calls, error = self.call_after_one_bad_gateway(no_cache=False)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(error)
        self.assertIn("results", str(error))

if __name__ == "__main__":
    unittest.main()
