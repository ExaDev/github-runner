"""Tests for scripts/ci/shard_unit_tests.py: the work counted from a module's tests follows the processes they start, and the dealing puts every module in exactly one shard, the same way every time, with the heaviest shard within a bound of the mean."""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import itertools
import random
import shutil
import sys
import tempfile
import textwrap
import unittest
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
START = shard.TASKS_PER_PROCESS_START
NAMES = itertools.count()


def heaviest_bound(work: dict[str, int], shards: int) -> float:
    """Graham's bound on the heaviest of ``shards`` shards when each module goes to the lightest shard so far: the mean plus all but a shards-th part of the heaviest module."""
    return sum(work.values()) / shards + (1 - 1 / shards) * max(work.values())


def loads(work: dict[str, int], dealt: list[list[str]]) -> list[int]:
    return [sum(work[name] for name in names) for names in dealt]


def modules_went_to_the_lightest_shard(work: dict[str, int], dealt: list[list[str]]) -> list[str]:
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


class RepositoryDealTest(unittest.TestCase):
    """The dealing of this repository's own test modules."""

    work: dict[str, int]

    @classmethod
    def setUpClass(cls) -> None:
        cls.work = shard.work_of(UNIT_TESTS)

    def test_the_work_covers_every_test_module_of_the_directory(self) -> None:
        self.assertEqual(sorted(self.work), sorted(path.stem for path in UNIT_TESTS.glob("test_*.py")))

    def test_every_module_does_at_least_the_work_of_the_process_that_runs_it(self) -> None:
        self.assertEqual([name for name, amount in self.work.items() if amount < START], [])

    def test_every_module_is_in_exactly_one_shard_whatever_the_number_of_shards(self) -> None:
        for shards in range(1, 9):
            dealt = shard.deal(self.work, shards)
            self.assertEqual(sorted(itertools.chain.from_iterable(dealt)), sorted(self.work), f"{shards} shards")

    def test_the_same_tree_is_dealt_the_same_way_again(self) -> None:
        self.assertEqual(shard.deal(shard.work_of(UNIT_TESTS), 5), shard.deal(self.work, 5))

    def test_the_heaviest_shard_is_within_the_list_scheduling_bound_of_the_mean(self) -> None:
        for shards in range(1, 9):
            dealt = shard.deal(self.work, shards)
            self.assertLessEqual(max(loads(self.work, dealt)), heaviest_bound(self.work, shards), f"{shards} shards")

    def test_every_module_went_to_the_shard_that_had_the_least_work_when_it_was_dealt(self) -> None:
        for shards in range(2, 9):
            self.assertEqual(modules_went_to_the_lightest_shard(self.work, shard.deal(self.work, shards)), [], f"{shards} shards")

    def test_the_shards_work_differs_by_no_more_than_the_heaviest_module(self) -> None:
        # The shard that ends heaviest took its last module while it was the lightest, so it exceeds the lightest by less than that module.
        for shards in range(2, 9):
            totals = loads(self.work, shard.deal(self.work, shards))
            self.assertLessEqual(max(totals) - min(totals), max(self.work.values()), f"{shards} shards")


class WorkTest(unittest.TestCase):
    """The work counted from synthetic modules, each written into a repository of its own."""

    def setUp(self) -> None:
        self.repository = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.repository)
        self.directory = self.repository / "tests" / "unit"
        self.directory.mkdir(parents=True)
        self.written: list[str] = []
        sys.path.insert(0, str(self.directory))
        self.addCleanup(sys.path.remove, str(self.directory))
        self.addCleanup(self.forget)

    def forget(self) -> None:
        for name in self.written:
            sys.modules.pop(name, None)

    def write(self, source: str, name: str | None = None) -> str:
        name = name or f"synthetic_{next(NAMES)}"
        (self.directory / f"{name}.py").write_text(textwrap.dedent(source))
        self.written.append(name)
        importlib.invalidate_caches()
        return name

    def work(self, module: str) -> int:
        return shard.module_work(shard.LaunchCounter(self.directory, self.repository), module)

    def test_a_module_that_starts_nothing_costs_the_process_that_runs_it(self) -> None:
        module = self.write(
            """
            import unittest

            class Quiet(unittest.TestCase):
                def test_one(self): self.assertTrue(True)
                def test_two(self): self.assertTrue(True)
            """
        )
        self.assertEqual(self.work(module), START)

    def test_each_process_a_test_starts_costs_a_start_whichever_function_of_the_subprocess_module_starts_it(self) -> None:
        module = self.write(
            """
            import subprocess
            import unittest
            from subprocess import run

            class Starts(unittest.TestCase):
                def test_run(self): subprocess.run(["true"])
                def test_imported_name(self): run(["true"])
                def test_popen(self): subprocess.Popen(["true"]).wait()
                def test_output(self): subprocess.check_output(["true"])
                def test_check(self): subprocess.check_call(["true"])
                def test_call(self): subprocess.call(["true"])
            """
        )
        self.assertEqual(self.work(module), START * (1 + 6))

    def test_launches_through_helpers_base_classes_super_and_the_set_up_methods_are_reached(self) -> None:
        support = self.write(
            """
            import subprocess
            import unittest

            def start():
                subprocess.run(["true"])

            class Base(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    start()

                def setUp(self):
                    start()

                def tearDown(self):
                    start()

                def helper(self):
                    self.inner()

                def inner(self):
                    start()
            """
        )
        module = self.write(
            f"""
            import unittest

            import {support}
            from {support} import Base, start

            class Child({support}.Base):
                def setUp(self):
                    super().setUp()
                    start()

                def test_a(self):
                    self.helper()
                    start()

                def test_b(self):
                    {support}.start()

            class Sibling(Base):
                def test_c(self): pass
            """
        )
        # The module's own process; Child: setUpClass once; each of its two tests: both set-ups (Base's through super()), tearDown, and the body (test_a has two launches, test_b one); Sibling: setUpClass once, and for its one test setUp, tearDown and no body. The imported Base has no tests of its own beyond the TestCase machinery.
        child = 1 + 2 * (2 + 1) + (2 + 1)
        sibling = 1 + 2
        self.assertEqual(self.work(module), START * (1 + child + sibling))

    def test_a_launch_site_counts_once_however_many_times_a_loop_runs_it_and_a_conditional_counts_its_dearer_branch(self) -> None:
        module = self.write(
            """
            import subprocess
            import unittest

            class Shapes(unittest.TestCase):
                def test_loop(self):
                    for _ in range(50):
                        subprocess.run(["true"])

                def test_branches(self):
                    if self.id():
                        subprocess.run(["true"])
                    else:
                        subprocess.run(["true"])
                        subprocess.run(["true"])
            """
        )
        self.assertEqual(self.work(module), START * (1 + 1 + 2))

    def test_a_skipped_test_or_class_does_no_work_and_a_recursive_helper_counts_its_launch_once(self) -> None:
        module = self.write(
            """
            import subprocess
            import unittest

            def again(depth):
                subprocess.run(["true"])
                if depth:
                    again(depth - 1)

            class Partly(unittest.TestCase):
                @unittest.skip("not here")
                def test_skipped(self): subprocess.run(["true"])

                def test_recursive(self): again(3)

            @unittest.skip("not here")
            class Whole(unittest.TestCase):
                def setUp(self): subprocess.run(["true"])
                def test_a(self): subprocess.run(["true"])
            """
        )
        self.assertEqual(self.work(module), START * (1 + 1))

    def test_a_playbook_launch_costs_the_tasks_the_playbook_the_roles_and_the_task_files_they_include_reach(self) -> None:
        roles = self.repository / "roles" / "demo" / "tasks"
        roles.mkdir(parents=True)
        (roles / "main.yml").write_text("- name: a\n  ansible.builtin.debug: {}\n- name: b\n  block:\n    - name: c\n      ansible.builtin.debug: {}\n")
        (roles / "other.yml").write_text("- name: d\n  ansible.builtin.debug: {}\n- name: e\n  ansible.builtin.debug: {}\n")
        (roles / "outer.yml").write_text("- name: f\n  ansible.builtin.include_tasks: other.yml\n- name: g\n  ansible.builtin.include_tasks: \"{{ name }}.yml\"\n")
        (roles / "nested").mkdir()
        (roles / "nested" / "inner.yml").write_text("- name: h\n  ansible.builtin.include_tasks: outer.yml\n")
        # Tasks of main.yml: a, b and c (inside b's block), 3; of other.yml, 2; of outer.yml, its own 2 and the 2 of other.yml it includes by name (the include by a variable reaches nothing countable), 4; of nested/inner.yml, its own 1 and outer.yml's 4, found in the role's tasks directory, 5. The average of the four files, 14 / 4, rounds up to 4.
        for name, include, reached in (
            ("named", "name: exadev.demo.demo\n          tasks_from: other", 2),
            ("nested", "name: exadev.demo.demo\n          tasks_from: nested/inner", 5),
            ("variable", "name: exadev.demo.demo\n          tasks_from: \"{{ item }}\"", 4),
            ("looped", "name: exadev.demo.demo\n          tasks_from: \"{{ item }}\"\n      loop: [main, outer]", 7),
            ("whole", "name: exadev.demo.demo", 3),
        ):
            playbook = self.repository / "tests" / f"{name}.yml"
            playbook.write_text(
                "- hosts: all\n  tasks:\n    - name: own\n      ansible.builtin.debug: {}\n"
                f"    - name: include\n      ansible.builtin.include_role:\n          {include}\n"
            )
            module = self.write(
                f"""
                import subprocess
                import unittest
                from pathlib import Path

                ANSIBLE_PLAYBOOK = "ansible-playbook"
                PLAY = Path(__file__).resolve().parents[1] / "{name}.yml"

                class Runs(unittest.TestCase):
                    def test_it(self):
                        subprocess.run([ANSIBLE_PLAYBOOK, "-i", "inventory", str(PLAY)])
                """
            )
            # The play's own two tasks, those the include reaches, and the start of the process; the module's own process.
            self.assertEqual(self.work(module), START * 2 + 2 + reached, name)

    def test_a_process_that_is_not_a_playbook_run_costs_its_start_alone(self) -> None:
        playbook = self.repository / "tests" / "unrelated.yml"
        playbook.write_text("- hosts: all\n  tasks:\n    - name: own\n      ansible.builtin.debug: {}\n")
        module = self.write(
            """
            import subprocess
            import unittest
            from pathlib import Path

            PLAY = Path(__file__).resolve().parents[1] / "unrelated.yml"

            class Runs(unittest.TestCase):
                def test_it(self):
                    subprocess.run(["cat", str(PLAY)])
            """
        )
        self.assertEqual(self.work(module), START * 2)

    def test_a_module_that_cannot_be_imported_is_an_error_not_a_module_of_no_work(self) -> None:
        module = self.write("import a_module_that_does_not_exist\n")
        with self.assertRaises(RuntimeError):
            self.work(module)


class CommandLineTest(unittest.TestCase):
    def test_a_shard_prints_the_paths_of_its_modules_and_the_shards_between_them_print_every_module(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        for name in ("test_one", "test_two", "test_three"):
            (directory / f"{name}.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n    def test_it(self): pass\n")
        self.addCleanup(lambda: [sys.modules.pop(name, None) for name in ("test_one", "test_two", "test_three")])
        self.addCleanup(lambda: sys.path.remove(str(directory.resolve())))
        printed: list[str] = []
        for index in range(2):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                status = shard.main([str(index), "2", str(directory)])
            self.assertEqual(status, 0)
            printed.extend(out.getvalue().split())
        self.assertEqual(sorted(printed), sorted(str(directory / f"{name}.py") for name in ("test_one", "test_two", "test_three")))

    def test_a_shard_index_outside_the_shards_is_refused(self) -> None:
        self.assertEqual(shard.main(["5", "5"]), 2)


if __name__ == "__main__":
    unittest.main()
