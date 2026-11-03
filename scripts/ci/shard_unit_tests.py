#!/usr/bin/env python3
"""Deal the unit-test modules of a directory out to CI shards by the work each module does, and print one shard's modules.

    shard_unit_tests.py SHARD SHARDS [DIRECTORY]

SHARD is the zero-based index of the shard asked for and SHARDS how many there are. The modules (``test_*.py`` directly in DIRECTORY, which defaults to ``tests/unit`` of this repository) are printed one path per line in the order they should run. The shards partition the modules: every module is in exactly one, and the same tree always gives the same assignment.

The work of a module is counted from its tests, not stored, in units of one Ansible task. A test's time is spent in the processes it starts, chiefly ``ansible-playbook`` runs, so a module's work is that of the process launches (``subprocess.run`` and its siblings) that each of its tests reaches, plus the one that runs the module: through the test's own body, the helpers, base-class methods and ``super()`` calls it makes, and ``setUp``, ``tearDown`` and, once per class, ``setUpClass``. Every launch costs ``TASKS_PER_PROCESS_START``, and a launch of a playbook costs in addition the tasks that playbook reaches: its own, those of each role it includes (one task file where the include names it, the average task file of a role where the name is a variable, the whole role where it names none) and those of each task file it includes by a literal name. A launch site counts once however many times a loop runs it, and of the two branches of a conditional the dearer counts. A skipped test or class does no work.

The modules are then dealt by longest-processing-time first: the heaviest module goes to whichever shard has the least work so far, ties going to the lower shard index, and equal modules in path order. Dealing to the lightest shard keeps the heaviest shard within ``mean + (1 - 1/SHARDS) * heaviest module`` of the mean (Graham's list-scheduling bound), which a dealing by position in a list cannot promise.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import os
import subprocess
import sys
import unittest
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import FunctionType, MethodType
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIRECTORY = REPO_ROOT / "tests" / "unit"

# The unit of work is one Ansible task. Starting a process costs about as much as this many tasks run: the ratio was fitted by least squares of each module's duration in CI logs against its process starts and its playbook tasks, and what the dealing needs of it is only that a playbook start is not negligible beside a task and that a start of any process is not free.
TASKS_PER_PROCESS_START = 64

# Every way the standard library starts a process that the tests use.
LAUNCHERS: tuple[object, ...] = (subprocess.run, subprocess.Popen, subprocess.check_output, subprocess.check_call, subprocess.call)

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
INCLUDE_ROLE = ("ansible.builtin.include_role", "ansible.builtin.import_role", "include_role", "import_role")
INCLUDE_TASKS = ("ansible.builtin.include_tasks", "ansible.builtin.import_tasks", "include_tasks", "import_tasks")


def count_tasks(tasks: object) -> int:
    """The tasks of a task list, those inside its blocks, rescues and always sections included."""
    if not isinstance(tasks, list):
        return 0
    total = 0
    for task in tasks:
        if isinstance(task, dict):
            total += 1 + sum(count_tasks(task.get(section)) for section in ("block", "rescue", "always"))
    return total


class PlaybookTasks:
    """How many tasks a playbook reaches, read from the YAML of the playbook and of the roles and task files it names."""

    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.playbooks: dict[Path, int] = {}

    def task_file(self, path: Path) -> int:
        return count_tasks(yaml.safe_load(path.read_text()))

    def role(self, name: str, tasks_from: object) -> int:
        """The tasks of a role as an include of it reaches them: one task file when ``tasks_from`` names it, the average task file when it is a variable, every task file when there is none."""
        directory = self.repository / "roles" / name.rsplit(".", 1)[-1] / "tasks"
        if isinstance(tasks_from, str) and "{{" not in tasks_from:
            return self.task_file(directory / f"{tasks_from.removesuffix('.yml')}.yml")
        total = sum(self.task_file(file) for file in directory.rglob("*.yml"))
        if tasks_from is None:
            return total
        return -(-total // len(list(directory.rglob("*.yml"))))

    def includes(self, tasks: object, playbook: Path) -> int:
        """The tasks that the includes among a task list reach, without those of the list itself."""
        if not isinstance(tasks, list):
            return 0
        total = 0
        for task in tasks:
            if not isinstance(task, dict):
                continue
            total += sum(self.includes(task.get(section), playbook) for section in ("block", "rescue", "always"))
            for key in INCLUDE_ROLE:
                if key in task:
                    total += self.role(task[key]["name"], task[key].get("tasks_from"))
            for key in INCLUDE_TASKS:
                if key in task:
                    target = task[key] if isinstance(task[key], str) else task[key]["file"]
                    if "{{" not in target:
                        total += self.task_file((playbook.parent / target).resolve())
        return total

    def playbook(self, path: Path) -> int:
        """The tasks of a playbook and of what it includes."""
        if path not in self.playbooks:
            total = 0
            for play in yaml.safe_load(path.read_text()):
                for section in ("pre_tasks", "tasks", "post_tasks"):
                    total += count_tasks(play.get(section)) + self.includes(play.get(section), path)
                for role in play.get("roles", []):
                    total += self.role(role if isinstance(role, str) else role["role"], None)
            self.playbooks[path] = total
        return self.playbooks[path]


def resolve(node: ast.expr, scope: Mapping[str, Any], receiver: type | None, receiver_name: str | None) -> object | None:
    """The object a name or dotted attribute chain denotes in ``scope``, or None when the expression is anything else or names nothing."""
    if isinstance(node, ast.Name):
        if receiver is not None and node.id == receiver_name:
            return receiver
        if node.id in scope:
            value: object = scope[node.id]
            return value
        return getattr(builtins, node.id, None)
    if isinstance(node, ast.Attribute):
        base = resolve(node.value, scope, receiver, receiver_name)
        return getattr(base, node.attr, None) if base is not None else None
    return None


def path_of(node: ast.expr, scope: Mapping[str, Any]) -> Path | None:
    """The path an expression of names, string constants, ``/``, ``+`` and ``str()`` or ``Path()`` of those denotes, or None for any other expression."""
    if isinstance(node, ast.Constant):
        return Path(node.value) if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        value = scope.get(node.id)
        return Path(value) if isinstance(value, (str, os.PathLike)) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = path_of(node.left, scope), path_of(node.right, scope)
        return left / right if left is not None and right is not None else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("str", "Path") and len(node.args) == 1:
        return path_of(node.args[0], scope)
    return None


@dataclass(frozen=True)
class Frame:
    """The function being read and what a name in it can mean."""

    function: FunctionType
    receiver: type | None
    receiver_name: str | None
    # Whether the function names ANSIBLE_PLAYBOOK or ansible-playbook, which makes the processes it starts playbook runs.
    runs_playbooks: bool
    # What each local name is assigned, for the argument list a process is started with.
    assignments: Mapping[str, ast.expr]


class LaunchCounter:
    """Counts the work of the process launches a function reaches, following the calls that resolve to functions defined under ``directory``."""

    def __init__(self, directory: Path, repository: Path) -> None:
        self.directory = directory.resolve()
        self.playbooks = PlaybookTasks(repository)
        self.definitions: dict[str, dict[int, FunctionNode]] = {}
        self.memo: dict[tuple[FunctionType, type | None], int] = {}
        self.active: set[tuple[FunctionType, type | None]] = set()

    def definition(self, function: FunctionType) -> FunctionNode | None:
        """The syntax tree of a function's definition when its source file is under the directory."""
        file = Path(function.__code__.co_filename)
        if not file.is_absolute() or not file.resolve().is_relative_to(self.directory):
            return None
        key = str(file)
        if key not in self.definitions:
            found: dict[int, FunctionNode] = {}
            for node in ast.walk(ast.parse(file.read_text())):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # co_firstlineno is the first decorator's line when there are decorators, and the ``def`` line otherwise.
                    found[min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])] = node
            self.definitions[key] = found
        return self.definitions[key].get(function.__code__.co_firstlineno)

    def work(self, function: object, receiver: type | None) -> int:
        """The work of one call of ``function``, as a method of ``receiver`` when that is a class (so that ``self.name`` resolves through its MRO)."""
        if isinstance(function, MethodType):
            # A bound method runs on whatever it is bound to: a class for a classmethod, an instance's class otherwise.
            receiver = function.__self__ if isinstance(function.__self__, type) else type(function.__self__)
            function = function.__func__
        if not isinstance(function, FunctionType):
            return 0
        function = inspect.unwrap(function)
        if not isinstance(function, FunctionType):
            return 0
        key = (function, receiver)
        if key in self.memo:
            return self.memo[key]
        if key in self.active:
            # A recursive helper adds its own work once, not once per level.
            return 0
        node = self.definition(function)
        if node is None:
            return 0
        self.active.add(key)
        parameters = [argument.arg for argument in node.args.posonlyargs + node.args.args]
        mentions = {name.id for name in ast.walk(node) if isinstance(name, ast.Name)} | {text.value for text in ast.walk(node) if isinstance(text, ast.Constant) and isinstance(text.value, str)}
        frame = Frame(
            function=function,
            receiver=receiver,
            receiver_name=parameters[0] if receiver is not None and parameters else None,
            runs_playbooks=any("ANSIBLE_PLAYBOOK" in mention or "ansible-playbook" in mention for mention in mentions),
            assignments={target.id: assignment.value for assignment in ast.walk(node) if isinstance(assignment, ast.Assign) for target in assignment.targets if isinstance(target, ast.Name)},
        )
        total = self.statements(node.body, frame)
        self.active.discard(key)
        self.memo[key] = total
        return total

    def statements(self, nodes: list[ast.stmt], frame: Frame) -> int:
        return sum(self.node(node, frame) for node in nodes)

    def node(self, node: ast.AST, frame: Frame) -> int:
        # A nested definition starts nothing until it is called, and calls to it do not resolve.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return 0
        if isinstance(node, ast.If):
            return self.node(node.test, frame) + max(self.statements(node.body, frame), self.statements(node.orelse, frame))
        total = sum(self.node(child, frame) for child in ast.iter_child_nodes(node))
        if isinstance(node, ast.Call):
            total += self.call(node, frame)
        return total

    def call(self, node: ast.Call, frame: Frame) -> int:
        """What the call itself adds: the work of a launch, or of the function it runs."""
        callee = node.func
        if isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Call) and isinstance(callee.value.func, ast.Name) and callee.value.func.id == "super" and frame.receiver is not None:
            return self.work_after(frame, callee.attr)
        target = resolve(callee, frame.function.__globals__, frame.receiver, frame.receiver_name)
        if any(target is launcher for launcher in LAUNCHERS):
            return TASKS_PER_PROCESS_START + (self.playbook_tasks(node, frame) if frame.runs_playbooks else 0)
        return self.work(target, frame.receiver)

    def playbook_tasks(self, launch: ast.Call, frame: Frame) -> int:
        """The tasks of the playbook a launch runs: the first element of its argument list that is the path of a YAML file, or none when no element is."""
        arguments = launch.args[0] if launch.args else None
        if isinstance(arguments, ast.Name):
            arguments = frame.assignments.get(arguments.id)
        if not isinstance(arguments, ast.List):
            return 0
        for element in arguments.elts:
            path = path_of(element, frame.function.__globals__)
            if path is not None and path.suffix == ".yml" and path.is_file():
                return self.playbooks.playbook(path)
        return 0

    def work_after(self, frame: Frame, name: str) -> int:
        """The work of ``super().name()`` called from the frame's function: the next definition of ``name`` in the receiver's MRO after the class that defines the function."""
        assert frame.receiver is not None
        owner = frame.function.__globals__.get(frame.function.__qualname__.split(".")[0])
        mro = frame.receiver.__mro__
        if owner not in mro:
            return 0
        for cls in mro[mro.index(owner) + 1 :]:
            if name in vars(cls):
                inherited = vars(cls)[name]
                return self.work(inherited.__func__ if isinstance(inherited, (classmethod, staticmethod)) else inherited, frame.receiver)
        return 0

def flatten(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    """The test cases of a suite, whatever its nesting."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def skipped(candidate: object) -> bool:
    return bool(getattr(candidate, "__unittest_skip__", False))


def module_work(counter: LaunchCounter, module: str) -> int:
    """The work of one importable test module: the start of the process that runs it, plus the work of each test it would run."""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(module)
    if loader.errors:
        raise RuntimeError(f"{module} cannot be loaded:\n" + "\n".join(str(error) for error in loader.errors))
    total = TASKS_PER_PROCESS_START
    classes: set[type] = set()
    for case in flatten(suite):
        cls = type(case)
        method = getattr(cls, case._testMethodName)
        if skipped(cls) or skipped(method):
            continue
        total += sum(counter.work(step, cls) for step in (cls.setUp, method, cls.tearDown))
        if cls not in classes:
            classes.add(cls)
            total += counter.work(cls.setUpClass, cls)
    return total


def deal(work: Mapping[str, int], shards: int) -> list[list[str]]:
    """Assign every module to one of ``shards`` shards, the heaviest first to the shard with the least work so far.

    Modules of equal work are taken in name order and ties between shards go to the lower index, so the same modules and work always give the same shards. Each shard lists its modules in name order.
    """
    if shards < 1:
        raise ValueError(f"at least one shard is needed, not {shards}")
    loads = [0] * shards
    dealt: list[list[str]] = [[] for _ in range(shards)]
    for name, amount in sorted(work.items(), key=lambda item: (-item[1], item[0])):
        index = min(range(shards), key=lambda shard: (loads[shard], shard))
        loads[index] += amount
        dealt[index].append(name)
    return [sorted(names) for names in dealt]


def modules_of(directory: Path) -> list[str]:
    """The names of the test modules directly in a directory."""
    return sorted(path.stem for path in directory.glob("test_*.py"))


def work_of(directory: Path) -> dict[str, int]:
    """The work of every test module directly in a directory, importing them the way unittest discovery does from it."""
    directory = directory.resolve()
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    counter = LaunchCounter(directory, REPO_ROOT)
    return {module: module_work(counter, module) for module in modules_of(directory)}


def main(arguments: list[str]) -> int:
    if len(arguments) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    shard, shards = int(arguments[0]), int(arguments[1])
    directory = Path(arguments[2]) if len(arguments) == 3 else DEFAULT_DIRECTORY
    if not 0 <= shard < shards:
        print(f"shard {shard} is not one of the {shards} shards", file=sys.stderr)
        return 2
    modules = deal(work_of(directory), shards)[shard]
    if not modules:
        print(f"shard {shard} of {shards} has no modules: there are fewer modules than shards", file=sys.stderr)
        return 1
    for module in modules:
        print(directory / f"{module}.py")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
