"""Image selection for the disposable mesh role-controller scenarios."""

import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SELECTOR = ROOT / "tests/mesh/role-image.sh"


class MeshRoleImageTest(unittest.TestCase):
    def select(self, image: str | None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("GRTEST_ROLE_IMAGE", None)
        if image is not None:
            env["GRTEST_ROLE_IMAGE"] = image
        return subprocess.run(
            ["bash", "-c", 'set -eu; . "$1"; grtest_select_role_image; printf "%s\\n%s\\n" "$role_controller_image" "$role_image_published"', "bash", str(SELECTOR)],
            env=env, text=True, capture_output=True, check=False,
        )

    def test_default_builds_the_local_image(self) -> None:
        result = self.select(None)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "grtest/fleet-role:test\n0\n")

    def test_explicit_tag_or_digest_is_passed_through(self) -> None:
        for image in ("ghcr.io/example/fleet-role:1.2.3", "ghcr.io/example/fleet-role@sha256:" + "a" * 64):
            with self.subTest(image=image):
                result = self.select(image)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, f"{image}\n1\n")

    def test_empty_explicit_image_is_rejected(self) -> None:
        result = self.select("")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be empty", result.stderr)


class CanaryInputTest(unittest.TestCase):
    def validate(self, image: str, scenario: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(ROOT / "tests/mesh/validate-canary.sh"), image, scenario], text=True, capture_output=True, check=False)

    def test_accepts_only_image_exercising_scenarios(self) -> None:
        image = "ghcr.io/example/fleet-role@sha256:" + "a" * 64
        for scenario in ("role_controller", "role_controller_swap", "role_swap", "role_swap_paused", "role_swap_blocked_peer", "role_swap_refused"):
            with self.subTest(scenario=scenario):
                self.assertEqual(self.validate(image, scenario).returncode, 0)
        self.assertNotEqual(self.validate(image, "hosted").returncode, 0)

    def test_rejects_mutable_and_malformed_references(self) -> None:
        for image in ("", "ghcr.io/example/fleet-role:latest", "ghcr.io/example/fleet-role@sha256:abc", "ghcr.io/example/fleet-role@sha256:" + "g" * 64):
            with self.subTest(image=image):
                self.assertNotEqual(self.validate(image, "role_controller").returncode, 0)


if __name__ == "__main__":
    unittest.main()
