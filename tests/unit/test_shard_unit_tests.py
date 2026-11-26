"""Tests for scripts/ci/shard_unit_tests.py: a module weighs what a run measured it to take, the dealing puts every module in exactly one shard, the same way every time, with the heaviest shard within a bound of the mean, and the measurements are taken, carried between runs and read back."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import itertools
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "shard_unit_tests.py"
UNIT_TESTS = REPO_ROOT / "tests" / "unit"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("shard_unit_tests", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shard = load_script()


def heaviest_bound(work: Mapping[str, float], shards: int) -> float:
    """Graham's bound on the heaviest of ``shards`` shards when each module goes to the lightest shard so far: the mean plus all but a shards-th part of the heaviest module."""
    return sum(work.values()) / shards + (1 - 1 / shards) * max(work.values())


def loads(work: Mapping[str, float], dealt: list[list[str]]) -> list[float]:
    return [sum(work[name] for name in names) for names in dealt]


def modules_went_to_the_lightest_shard(work: Mapping[str, float], dealt: list[list[str]]) -> list[str]:
    """The shards that held more without their smallest module than another shard holds in all, which a module dealt to the lightest shard so far cannot leave behind: that shard was the lightest when the module went to it."""
    totals = loads(work, dealt)
    return [
        f"shard {index}"
        for index, names in enumerate(dealt)
        if names and totals[index] - min(work[name] for name in names) > min(total for other, total in enumerate(totals) if other != index)
    ]


class DealTest(unittest.TestCase):
    SKEWED = {"heavy": 1000, **{f"light_{index:02d}": 1 + index % 7 for index in range(40)}, "middling_a": 300, "middling_b": 290, "middling_c": 40}

    def test_every_module_is_in_exactly_one_shard(self) -> None:
        for shards in range(1, 9):
            dealt = shard.deal(self.SKEWED, shards)
            self.assertEqual(len(dealt), shards)
            self.assertEqual(sorted(itertools.chain.from_iterable(dealt)), sorted(self.SKEWED))

    def test_the_assignment_does_not_depend_on_the_order_the_modules_are_listed_in(self) -> None:
        expected = shard.deal(self.SKEWED, 5)
        names = list(self.SKEWED)
        generator = random.Random(len(names))
        for _ in range(len(names)):
            generator.shuffle(names)
            self.assertEqual(shard.deal({name: self.SKEWED[name] for name in names}, 5), expected)

    def test_the_heaviest_shard_is_within_the_list_scheduling_bound_of_the_mean(self) -> None:
        for shards in range(1, 9):
            dealt = shard.deal(self.SKEWED, shards)
            self.assertLessEqual(max(loads(self.SKEWED, dealt)), heaviest_bound(self.SKEWED, shards), f"{shards} shards")

    def test_every_module_went_to_the_shard_that_had_the_least_work_when_it_was_dealt(self) -> None:
        for shards in range(2, 9):
            self.assertEqual(modules_went_to_the_lightest_shard(self.SKEWED, shard.deal(self.SKEWED, shards)), [], f"{shards} shards")

    def test_equal_modules_are_dealt_in_name_order_to_the_lower_shard_first(self) -> None:
        work = {name: 5 for name in ("test_d", "test_a", "test_c", "test_b", "test_e")}
        self.assertEqual(shard.deal(work, 2), [["test_a", "test_c", "test_e"], ["test_b", "test_d"]])

    def test_a_heavy_module_gets_a_shard_to_itself_while_the_lighter_ones_share_the_rest(self) -> None:
        work = {"heavy": 100, **{f"light_{index}": 10 for index in range(10)}}
        dealt = shard.deal(work, 2)
        self.assertEqual(sorted(loads(work, dealt)), [100, 100])
        self.assertIn(["heavy"], dealt)

    def test_a_shard_count_below_one_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            shard.deal(self.SKEWED, 0)


class WeightsTest(unittest.TestCase):
    def test_a_measured_module_weighs_what_it_was_measured_to_take(self) -> None:
        self.assertEqual(shard.weights(["test_a", "test_b"], {"test_a": 300.0, "test_b": 4.5}), {"test_a": 300.0, "test_b": 4.5})

    def test_an_unmeasured_module_weighs_the_mean_of_the_measured_ones(self) -> None:
        self.assertEqual(shard.weights(["test_a", "test_b", "test_new"], {"test_a": 30.0, "test_b": 10.0})["test_new"], 20.0)

    def test_with_nothing_measured_every_module_weighs_the_same(self) -> None:
        self.assertEqual(len(set(shard.weights(["test_a", "test_b", "test_c"], {}).values())), 1)

    def test_a_measurement_of_a_module_that_is_gone_changes_nothing(self) -> None:
        self.assertEqual(shard.weights(["test_a", "test_new"], {"test_a": 6.0, "test_gone": 900.0}), {"test_a": 6.0, "test_new": 6.0})


class DirectoryCase(unittest.TestCase):
    """A directory of synthetic test modules and a scratch directory beside it."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.work)
        self.directory = self.work / "unit"
        self.directory.mkdir()

    def write(self, name: str, body: str = "def test_it(self): pass") -> None:
        (self.directory / f"{name}.py").write_text(f"import unittest\n\nclass T(unittest.TestCase):\n    {body}\n")

    def read(self, path: Path) -> dict[str, float]:
        value: dict[str, float] = json.loads(path.read_text())
        return value

    def command(self, *arguments: str) -> tuple[int, str]:
        # The processes ``run`` starts write to the file descriptors, past sys.stdout; their output is kept out of this test's log, where a synthetic module's failure would read as a real one.
        with open(self.work / "processes.log", "ab") as log, contextlib.redirect_stdout(io.StringIO()) as out:
            kept = [os.dup(1), os.dup(2)]
            try:
                os.dup2(log.fileno(), 1)
                os.dup2(log.fileno(), 2)
                status = shard.main(list(arguments))
            finally:
                for descriptor, original in zip((1, 2), kept):
                    os.dup2(original, descriptor)
                    os.close(original)
        return status, out.getvalue()


class PlanTest(DirectoryCase):
    # Measured seconds of modules whose tests look alike to anything but a clock: the heavy ones must not meet in one shard.
    MEASURED = {"test_a": 700.0, "test_b": 520.0, "test_c": 420.0, "test_d": 380.0, "test_e": 40.0, "test_f": 30.0, "test_g": 20.0, "test_h": 10.0}

    def plan(self, shards: int, timings: dict[str, float]) -> list[list[str]]:
        file = self.work / "timings.json"
        file.write_text(json.dumps(timings))
        status, out = self.command("plan", str(shards), str(file), "--directory", str(self.directory))
        self.assertEqual(status, 0)
        include = json.loads(out)["include"]
        self.assertEqual([entry["shard"] for entry in include], list(range(shards)))
        return [entry["modules"].split() for entry in include]

    def test_the_shards_are_balanced_by_the_measured_durations(self) -> None:
        for name in self.MEASURED:
            self.write(name)
        dealt = self.plan(4, self.MEASURED)
        self.assertEqual(sorted(itertools.chain.from_iterable(dealt)), sorted(self.MEASURED))
        self.assertEqual([sorted(self.MEASURED[name] for name in names)[-1] for names in dealt], [700.0, 520.0, 420.0, 380.0])
        self.assertEqual(max(loads(self.MEASURED, dealt)), 700.0)

    def test_every_module_is_planned_when_nothing_has_been_measured(self) -> None:
        for name in self.MEASURED:
            self.write(name)
        dealt = self.plan(4, {})
        self.assertEqual(sorted(itertools.chain.from_iterable(dealt)), sorted(self.MEASURED))
        self.assertEqual({len(names) for names in dealt}, {2})

    def test_fewer_modules_than_shards_is_an_error_not_an_empty_shard(self) -> None:
        self.write("test_only")
        file = self.work / "timings.json"
        file.write_text("{}")
        self.assertEqual(self.command("plan", "2", str(file), "--directory", str(self.directory))[0], 1)

    def test_this_repositorys_modules_are_each_planned_once(self) -> None:
        file = self.work / "timings.json"
        file.write_text("{}")
        status, out = self.command("plan", "5", str(file))
        self.assertEqual(status, 0)
        planned = [name for entry in json.loads(out)["include"] for name in entry["modules"].split()]
        self.assertEqual(sorted(planned), sorted(path.stem for path in UNIT_TESTS.glob("test_*.py")))


class RunTest(DirectoryCase):
    def test_each_module_that_passes_is_recorded_with_the_time_it_took(self) -> None:
        self.write("test_slow", "def test_it(self): __import__('time').sleep(0.5)")
        self.write("test_quick")
        output = self.work / "measured.json"
        status, _ = self.command("run", str(output), "test_slow", "test_quick", "--directory", str(self.directory))
        self.assertEqual(status, 0)
        measured = self.read(output)
        self.assertEqual(sorted(measured), ["test_quick", "test_slow"])
        self.assertGreaterEqual(measured["test_slow"], 0.5)

    def test_a_failing_module_fails_the_run_after_the_others_and_is_not_recorded(self) -> None:
        self.write("test_fails", "def test_it(self): self.fail()")
        self.write("test_passes")
        output = self.work / "measured.json"
        status, out = self.command("run", str(output), "test_fails", "test_passes", "--directory", str(self.directory))
        self.assertEqual(status, 1)
        self.assertEqual(sorted(self.read(output)), ["test_passes"])
        self.assertIn("test_fails", out)

    def test_what_has_been_measured_is_on_disk_before_the_next_module_runs(self) -> None:
        # A shard that is cancelled part way still hands on what it measured.
        output = self.work / "measured.json"
        self.write("test_first")
        self.write("test_second", f"def test_it(self): self.assertIn('test_first', open({str(output)!r}).read())")
        self.assertEqual(self.command("run", str(output), "test_first", "test_second", "--directory", str(self.directory))[0], 0)

    def test_a_module_that_does_not_exist_is_an_error(self) -> None:
        self.assertEqual(self.command("run", str(self.work / "measured.json"), "test_absent", "--directory", str(self.directory))[0], 1)


class MergeTest(DirectoryCase):
    def test_new_measurements_replace_old_ones_and_modules_that_are_gone_are_dropped(self) -> None:
        for name in ("test_kept", "test_changed", "test_added"):
            self.write(name)
        previous = self.work / "previous.json"
        previous.write_text(json.dumps({"test_kept": 1.0, "test_changed": 2.0, "test_gone": 3.0}))
        measured = self.work / "measured"
        (measured / "shard-0").mkdir(parents=True)
        (measured / "shard-1").mkdir()
        (measured / "shard-0" / "measured.json").write_text(json.dumps({"test_changed": 20.0}))
        (measured / "shard-1" / "measured.json").write_text(json.dumps({"test_added": 4.0}))
        output = self.work / "merged.json"
        self.assertEqual(self.command("merge", str(previous), str(measured), str(output), "--directory", str(self.directory))[0], 0)
        self.assertEqual(self.read(output), {"test_added": 4.0, "test_changed": 20.0, "test_kept": 1.0})


class LatestArtifactTest(unittest.TestCase):
    @staticmethod
    def artifact(identity: int, created: str, expired: bool = False, head: int = 1) -> dict[str, object]:
        return {"id": identity, "created_at": created, "expired": expired, "workflow_run": {"repository_id": 1, "head_repository_id": head}}

    def test_the_newest_artifact_that_has_not_expired_is_chosen(self) -> None:
        artifacts = [self.artifact(1, "2000-01-01T00:00:00Z"), self.artifact(3, "2000-01-03T00:00:00Z", expired=True), self.artifact(2, "2000-01-02T00:00:00Z")]
        self.assertEqual(shard.latest_artifact(artifacts), 2)

    def test_an_artifact_uploaded_by_a_run_of_a_fork_is_never_chosen(self) -> None:
        artifacts = [self.artifact(1, "2000-01-01T00:00:00Z"), self.artifact(2, "2000-01-02T00:00:00Z", head=7)]
        self.assertEqual(shard.latest_artifact(artifacts), 1)

    def test_there_may_be_none(self) -> None:
        self.assertIsNone(shard.latest_artifact([]))


if __name__ == "__main__":
    unittest.main()
