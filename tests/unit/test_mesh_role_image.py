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


if __name__ == "__main__":
    unittest.main()
