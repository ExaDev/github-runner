"""Tests for scripts/renew-pull-secret.sh, the image pull Secret renewal job, run against scripted stand-ins for curl and kubectl with the real bash, jq and openssl."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "renew-pull-secret.sh"

# A stand-in for curl that answers from a scenario file and records each call's arguments and config file. It understands only the options the script uses.
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
    head = False
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
        if arg == "--head":
            head = True
        elif not arg.startswith("-"):
            url = arg
        i += 1
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({"argv": args, "config": config, "data": data, "url": url, "head": head}) + "\\n")
    if "/access_tokens" in url:
        status, body = scenario["mint"]
    elif "/token?" in url:
        status, body = scenario["registry_token"]
    else:
        status, body = scenario["manifest"]
    if output and output != "/dev/null":
        with open(output, "w") as handle:
            handle.write(json.dumps(body))
    sys.stdout.write(str(status))
    """
)

FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    manifest = sys.stdin.read()
    with open(os.environ["FAKE_APPLIED"], "a") as handle:
        handle.write(json.dumps({"argv": sys.argv[1:], "manifest": manifest}) + "\\n")
    sys.exit(int(os.environ.get("FAKE_KUBECTL_EXIT", "0")))
    """
)

TOKEN = "ghs_exampleinstallationtoken0123456789"
REGISTRY_BEARER = "registry-bearer-example"
EXPIRES = "2030-01-01T00:00:00Z"


class RenewPullSecretTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for tool in ("bash", "jq", "openssl"):
            if shutil.which(tool) is None:
                raise unittest.SkipTest(f"{tool} is not installed")
        cls.keys = tempfile.TemporaryDirectory()
        cls.key = Path(cls.keys.name) / "key.pem"
        cls.public = Path(cls.keys.name) / "key.pub"
        subprocess.run(["openssl", "genrsa", "-out", str(cls.key), "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", str(cls.key), "-pubout", "-out", str(cls.public)], check=True, capture_output=True)

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
        self.app = root / "app"
        self.app.mkdir()
        (self.app / "github_app_id").write_text("12345")
        (self.app / "github_app_installation_id").write_text("67890\n")
        (self.app / "github_app_private_key").write_text(self.key.read_text())
        self.log = root / "curl.jsonl"
        self.applied = root / "applied.jsonl"
        self.scenario_path = root / "scenario.json"
        self.scenario = {
            "mint": [201, {"token": TOKEN, "expires_at": EXPIRES}],
            "registry_token": [200, {"token": REGISTRY_BEARER}],
            "manifest": [200, {}],
        }

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_script(self, images: list[dict[str, str]] | None = None, kubectl_exit: int = 0) -> subprocess.CompletedProcess[str]:
        self.scenario_path.write_text(json.dumps(self.scenario))
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_SCENARIO": str(self.scenario_path),
            "FAKE_LOG": str(self.log),
            "FAKE_APPLIED": str(self.applied),
            "FAKE_KUBECTL_EXIT": str(kubectl_exit),
            "GITHUB_APP_DIR": str(self.app),
            "PULL_SECRET_NAME": "example-pull",
            "PULL_SECRET_NAMESPACE": "example-runners",
            "PULL_REGISTRY": "ghcr.io",
            "VERIFY_IMAGES": json.dumps([{"repository": "example/runner", "reference": "1.0"}] if images is None else images),
            "EXPIRY_ANNOTATION": "example.com/token-expires-at",
            "FIELD_MANAGER": "example-renewer",
        }
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def applies(self) -> list[dict]:
        return [json.loads(line) for line in self.applied.read_text().splitlines()] if self.applied.exists() else []

    def test_writes_the_verified_token_as_a_dockerconfigjson_secret(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        [apply] = self.applies()
        self.assertEqual(apply["argv"], ["apply", "--server-side", "--force-conflicts", "--field-manager=example-renewer", "-f", "-"])
        secret = json.loads(apply["manifest"])
        self.assertEqual(secret["type"], "kubernetes.io/dockerconfigjson")
        self.assertEqual(secret["metadata"], {"name": "example-pull", "namespace": "example-runners", "annotations": {"example.com/token-expires-at": EXPIRES}})
        config = json.loads(base64.b64decode(secret["data"][".dockerconfigjson"]))
        auth = config["auths"]["ghcr.io"]
        self.assertEqual(auth["username"], "x-access-token")
        self.assertEqual(auth["password"], TOKEN)
        self.assertEqual(base64.b64decode(auth["auth"]).decode(), f"x-access-token:{TOKEN}")

    def test_mints_a_packages_read_token_with_a_signed_app_jwt(self) -> None:
        self.run_script()
        mint = self.calls()[0]
        self.assertEqual(mint["url"], "https://api.github.com/app/installations/67890/access_tokens")
        self.assertEqual(json.loads(mint["data"]), {"permissions": {"packages": "read"}})
        jwt = mint["config"].split("Bearer ", 1)[1].rstrip('"\n')
        header, claims, signature = jwt.split(".")

        def decode(part: str) -> bytes:
            return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

        self.assertEqual(json.loads(decode(header)), {"alg": "RS256", "typ": "JWT"})
        body = json.loads(decode(claims))
        self.assertEqual(body["iss"], "12345")
        self.assertEqual(body["exp"] - body["iat"], 600)
        with tempfile.NamedTemporaryFile() as signature_file:
            signature_file.write(decode(signature))
            signature_file.flush()
            verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(self.public), "-signature", signature_file.name], input=f"{header}.{claims}".encode(), capture_output=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_verifies_each_image_with_the_registry_handshake(self) -> None:
        images = [{"repository": "example/runner", "reference": "1.0"}, {"repository": "example/other", "reference": "sha256:abc"}]
        self.run_script(images)
        calls = self.calls()[1:]
        self.assertEqual([call["url"] for call in calls], [
            "https://ghcr.io/token?scope=repository:example/runner:pull&service=ghcr.io",
            "https://ghcr.io/v2/example/runner/manifests/1.0",
            "https://ghcr.io/token?scope=repository:example/other:pull&service=ghcr.io",
            "https://ghcr.io/v2/example/other/manifests/sha256:abc",
        ])
        self.assertIn(f'user = "x-access-token:{TOKEN}"', calls[0]["config"])
        self.assertIn(f"Bearer {REGISTRY_BEARER}", calls[1]["config"])
        self.assertTrue(calls[1]["head"])

    def test_no_credential_reaches_an_argument_or_the_output(self) -> None:
        result = self.run_script()
        mint_jwt = self.calls()[0]["config"].split("Bearer ", 1)[1].rstrip('"\n')
        for secret in (TOKEN, REGISTRY_BEARER, mint_jwt):
            for call in self.calls():
                self.assertNotIn(secret, " ".join(call["argv"]))
            for apply in self.applies():
                self.assertNotIn(secret, " ".join(apply["argv"]))
            self.assertNotIn(secret, result.stdout + result.stderr)

    def assert_refused_without_writing(self, result: subprocess.CompletedProcess[str], reason: str) -> None:
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(reason, result.stderr)
        self.assertIn("leaving example-runners/example-pull unchanged", result.stderr)
        self.assertEqual(self.applies(), [])
        self.assertNotIn(TOKEN, result.stderr)

    def test_a_refused_mint_writes_nothing(self) -> None:
        self.scenario["mint"] = [422, {"message": "The permissions requested are not granted to this installation."}]
        result = self.run_script()
        self.assert_refused_without_writing(result, "HTTP 422: The permissions requested are not granted")
        self.assertEqual(len(self.calls()), 1)

    def test_a_refused_registry_exchange_writes_nothing(self) -> None:
        self.scenario["registry_token"] = [401, {}]
        self.assert_refused_without_writing(self.run_script(), "refused the token for ghcr.io/example/runner:1.0 (HTTP 401)")

    def test_a_refused_manifest_read_writes_nothing(self) -> None:
        self.scenario["manifest"] = [403, {}]
        self.assert_refused_without_writing(self.run_script(), "cannot read ghcr.io/example/runner:1.0 (reading the manifest returned HTTP 403)")

    def test_a_missing_app_field_fails_before_any_request(self) -> None:
        (self.app / "github_app_installation_id").unlink()
        result = self.run_script()
        self.assert_refused_without_writing(result, "the App Secret has no github_app_installation_id")
        self.assertEqual(self.calls(), [])

    def test_a_non_numeric_app_id_fails_before_any_request(self) -> None:
        (self.app / "github_app_id").write_text('1","x":"y')
        self.assert_refused_without_writing(self.run_script(), "github_app_id is not a number")
        self.assertEqual(self.calls(), [])

    def test_a_failed_write_is_reported(self) -> None:
        result = self.run_script(kubectl_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not write the Secret", result.stderr)

    def test_with_no_images_to_verify_it_still_writes(self) -> None:
        result = self.run_script(images=[])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(len(self.applies()), 1)


if __name__ == "__main__":
    unittest.main()
