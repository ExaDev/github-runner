"""Tests for the ARC role's GitHub App JWT script."""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "roles" / "github_runner_arc" / "files" / "generate_app_jwt.py"
_spec = importlib.util.spec_from_file_location("generate_app_jwt", SCRIPT)
assert _spec is not None and _spec.loader is not None
generate_app_jwt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generate_app_jwt)


def b64url_decode(text: str) -> bytes:
    """Decode unpadded base64url."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class GenerateAppJwtTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.key = Path(cls.directory.name) / "key.pem"
        cls.public = Path(cls.directory.name) / "key.pub"
        subprocess.run(["openssl", "genrsa", "-out", str(cls.key), "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", str(cls.key), "-pubout", "-out", str(cls.public)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.directory.cleanup()

    def test_claims_and_header(self) -> None:
        token = generate_app_jwt.build_jwt("12345", str(self.key), 1_000_000)
        header, claims, _ = token.split(".")
        self.assertEqual(json.loads(b64url_decode(header)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(b64url_decode(claims)), {"iat": 1_000_000 - 60, "exp": 1_000_000 + 540, "iss": "12345"})
        self.assertNotIn("=", token)

    def test_signature_verifies_with_the_public_key(self) -> None:
        token = generate_app_jwt.build_jwt("1", str(self.key), 1_000_000)
        signing_input, _, signature = token.rpartition(".")
        with tempfile.NamedTemporaryFile() as signature_file:
            signature_file.write(b64url_decode(signature))
            signature_file.flush()
            result = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(self.public), "-signature", signature_file.name], input=signing_input.encode(), capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_command_line_prints_only_the_token_and_checks_its_arguments(self) -> None:
        result = subprocess.run([sys.executable, str(SCRIPT), "7", str(self.key)], capture_output=True, text=True, check=True)
        self.assertEqual(len(result.stdout.strip().split(".")), 3)
        self.assertEqual(result.stderr, "")
        usage = subprocess.run([sys.executable, str(SCRIPT), "7"], capture_output=True, text=True)
        self.assertEqual(usage.returncode, 2)
        self.assertIn("usage", usage.stderr)

    def test_a_missing_key_fails(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            generate_app_jwt.build_jwt("1", str(Path(self.directory.name) / "absent.pem"), 1)


if __name__ == "__main__":
    unittest.main()
