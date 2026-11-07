"""Exercise Darwin exclusive runner promotion on disposable local directories."""

import concurrent.futures
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROLE = Path(__file__).resolve().parents[2] / "roles" / "github_runner_macos_actions_runner"
SPEC = importlib.util.spec_from_file_location("runner_promotion", ROLE / "files" / "promote.py")
assert SPEC is not None and SPEC.loader is not None
PROMOTION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROMOTION)


@unittest.skipUnless(sys.platform == "darwin", "Darwin renamex_np is required")
class PromotionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="runner-promotion-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / "runner"

    def stage(self, name: str) -> Path:
        directory = self.root / name
        directory.mkdir()
        (directory / "content").write_text(name)
        return directory

    def test_absent_destination_is_installed(self) -> None:
        source = self.stage("first")
        PROMOTION.promote(str(source), str(self.destination))
        self.assertEqual((self.destination / "content").read_text(), "first")
        self.assertFalse(source.exists())

    def test_existing_empty_and_nonempty_destinations_are_unchanged(self) -> None:
        for existing in (False, True):
            with self.subTest(nonempty=existing):
                self.destination.mkdir()
                if existing:
                    (self.destination / ".runner").write_text("registered")
                source = self.stage("stage")
                with self.assertRaises(OSError):
                    PROMOTION.promote(str(source), str(self.destination))
                self.assertEqual(list(self.destination.iterdir()), [self.destination / ".runner"] if existing else [])
                self.assertTrue(source.exists())
                (source / "content").unlink()
                source.rmdir()
                if existing:
                    (self.destination / ".runner").unlink()
                self.destination.rmdir()

    def test_concurrent_installers_have_one_winner(self) -> None:
        stages = [self.stage("first"), self.stage("second")]

        def attempt(source: Path) -> bool:
            try:
                PROMOTION.promote(str(source), str(self.destination))
                return True
            except FileExistsError:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, stages))
        self.assertEqual(sum(results), 1)
        self.assertIn((self.destination / "content").read_text(), {"first", "second"})
        self.assertEqual(sum(source.exists() for source in stages), 1)

    def test_interrupted_staging_does_not_create_destination(self) -> None:
        self.stage("interrupted")
        self.assertFalse(self.destination.exists())
        source = self.stage("retry")
        PROMOTION.promote(str(source), str(self.destination))
        self.assertEqual((self.destination / "content").read_text(), "retry")
        self.assertTrue((self.root / "interrupted" / "content").exists())


if __name__ == "__main__":
    unittest.main()
