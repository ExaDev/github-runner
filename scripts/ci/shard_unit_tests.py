#!/usr/bin/env python3
"""Deal the unit-test modules of a directory out to CI shards by how long earlier runs measured each to take, run a shard's modules while measuring them, and carry the measurements from run to run.

    shard_unit_tests.py fetch TIMINGS
    shard_unit_tests.py plan SHARDS TIMINGS [--directory DIRECTORY]
    shard_unit_tests.py run MEASURED MODULE... [--directory DIRECTORY]
    shard_unit_tests.py merge TIMINGS MEASURED_DIRECTORY MERGED [--directory DIRECTORY]

A timings file is a JSON object from module name to the seconds its run took. DIRECTORY holds the modules (``test_*.py`` directly in it) and defaults to ``tests/unit`` of this repository.

``fetch`` writes the newest ``unit-timings`` artifact that a run of this repository (never of a fork) uploaded, or an empty object when there is none; it reads the repository from ``GITHUB_REPOSITORY`` and calls ``gh``. ``plan`` prints the shard matrix as JSON, ``{"include": [{"shard": 0, "modules": "test_a test_b"}, ...]}``. ``run`` runs each module in a process of its own, rewriting MEASURED after each one that passes, and fails after the last when any failed. ``merge`` writes the earlier timings with every ``*.json`` under MEASURED_DIRECTORY laid over them, dropping modules the directory no longer holds.

Nothing is estimated from the source and no duration is stored in the repository: what a test costs (playbook runs, loops, deliberate hangs) shows only when it runs. A module no run has measured weighs the mean of the measured ones, so with no measurements the modules are dealt by count.

The modules are dealt by longest-processing-time first: the heaviest module goes to whichever shard has the least so far, ties going to the lower shard index, and equal modules in name order. Dealing to the lightest shard keeps the heaviest shard within ``mean + (1 - 1/SHARDS) * heaviest module`` of the mean (Graham's list-scheduling bound).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIRECTORY = REPO_ROOT / "tests" / "unit"
# The artifact a run uploads its merged timings as, and the file inside it. The workflow names both.
ARTIFACT = "unit-timings"
ARTIFACT_FILE = "timings.json"


def weights(modules: Sequence[str], timings: Mapping[str, float]) -> dict[str, float]:
    """What each of ``modules`` weighs in the dealing: its measured seconds, or the mean of the measured modules when it has none (1 when no module has any)."""
    measured = [timings[name] for name in modules if name in timings]
    unmeasured = sum(measured) / len(measured) if measured else 1.0
    return {name: timings.get(name, unmeasured) for name in modules}


def deal(work: Mapping[str, float], shards: int) -> list[list[str]]:
    """Assign every module to one of ``shards`` shards, the heaviest first to the shard with the least work so far.

    Modules of equal work are taken in name order and ties between shards go to the lower index, so the same modules and work always give the same shards. Each shard lists its modules in name order.
    """
    if shards < 1:
        raise ValueError(f"at least one shard is needed, not {shards}")
    loads = [0.0] * shards
    dealt: list[list[str]] = [[] for _ in range(shards)]
    for name, amount in sorted(work.items(), key=lambda item: (-item[1], item[0])):
        index = min(range(shards), key=lambda shard: (loads[shard], shard))
        loads[index] += amount
        dealt[index].append(name)
    return [sorted(names) for names in dealt]


def modules_of(directory: Path) -> list[str]:
    """The names of the test modules directly in a directory."""
    return sorted(path.stem for path in directory.glob("test_*.py"))


def read_timings(path: Path) -> dict[str, float]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not all(isinstance(seconds, (int, float)) for seconds in value.values()):
        raise ValueError(f"{path} is not an object from module name to seconds")
    return {str(name): float(seconds) for name, seconds in value.items()}


def write_timings(path: Path, timings: Mapping[str, float]) -> None:
    path.write_text(json.dumps(dict(sorted(timings.items())), indent=2) + "\n")


def latest_artifact(artifacts: Sequence[Mapping[str, object]]) -> int | None:
    """The id of the newest unexpired artifact of ``artifacts`` (as the REST API lists them) that a run on a branch of the repository itself uploaded, or None. A fork's run lists its artifacts under the base repository, and its figures are not the repository's to trust."""
    own: list[tuple[str, int]] = []
    for artifact in artifacts:
        run = artifact["workflow_run"]
        if artifact["expired"] or not isinstance(run, Mapping) or run["head_repository_id"] != run["repository_id"]:
            continue
        created, identity = artifact["created_at"], artifact["id"]
        assert isinstance(created, str) and isinstance(identity, int)
        own.append((created, identity))
    return max(own)[1] if own else None


def gh_api(path: str) -> bytes:
    return subprocess.run(["gh", "api", path], check=True, capture_output=True).stdout


def fetch(output: Path) -> int:
    repository = os.environ["GITHUB_REPOSITORY"]
    # The API lists the newest first, so one page holds the newest of the repository's own unless a page of forks' uploads sits above it.
    listing = json.loads(gh_api(f"repos/{repository}/actions/artifacts?name={ARTIFACT}&per_page=100"))
    identity = latest_artifact(listing["artifacts"])
    if identity is None:
        print("No earlier run has uploaded timings: the modules will be dealt by count.")
        write_timings(output, {})
        return 0
    with zipfile.ZipFile(io.BytesIO(gh_api(f"repos/{repository}/actions/artifacts/{identity}/zip"))) as archive:
        output.write_bytes(archive.read(ARTIFACT_FILE))
    print(f"Read the timings of {len(read_timings(output))} module(s) from artifact {identity}.")
    return 0


def plan(shards: int, timings: Path, directory: Path) -> int:
    modules = modules_of(directory)
    if not 1 <= shards <= len(modules):
        print(f"{len(modules)} module(s) cannot fill {shards} shard(s)", file=sys.stderr)
        return 1
    dealt = deal(weights(modules, read_timings(timings)), shards)
    print(json.dumps({"include": [{"shard": index, "modules": " ".join(names)} for index, names in enumerate(dealt)]}))
    return 0


def run(output: Path, modules: Sequence[str], directory: Path) -> int:
    measured: dict[str, float] = {}
    failed: list[str] = []
    write_timings(output, measured)
    # Every module runs whatever an earlier one did, so one failure never hides another's.
    for module in modules:
        if not (directory / f"{module}.py").is_file():
            failed.append(module)
            continue
        sys.stdout.flush()
        began = time.monotonic()
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(directory), "-p", f"{module}.py", "-v"], check=False)
        if result.returncode != 0:
            # A module that failed may have stopped early, so its time says nothing about a run that passes.
            failed.append(module)
            continue
        measured[module] = round(time.monotonic() - began, 3)
        write_timings(output, measured)
    if failed:
        print(f"::error::Failed module(s): {' '.join(failed)}")
        return 1
    return 0


def merge(previous: Path, measured: Path, output: Path, directory: Path) -> int:
    timings = read_timings(previous)
    for path in sorted(measured.rglob("*.json")):
        timings.update(read_timings(path))
    present = set(modules_of(directory))
    write_timings(output, {name: seconds for name, seconds in timings.items() if name in present})
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description="Deal, run and measure the unit-test modules.")
    commands = top.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch").add_argument("timings", type=Path)
    planning = commands.add_parser("plan")
    planning.add_argument("shards", type=int)
    planning.add_argument("timings", type=Path)
    running = commands.add_parser("run")
    running.add_argument("measured", type=Path)
    running.add_argument("modules", nargs="+")
    merging = commands.add_parser("merge")
    merging.add_argument("timings", type=Path)
    merging.add_argument("measured", type=Path)
    merging.add_argument("merged", type=Path)
    for command in (planning, running, merging):
        command.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    return top


def main(arguments: Sequence[str]) -> int:
    options = parser().parse_args(arguments)
    if options.command == "fetch":
        return fetch(options.timings)
    if options.command == "plan":
        return plan(options.shards, options.timings, options.directory)
    if options.command == "run":
        return run(options.measured, options.modules, options.directory)
    return merge(options.timings, options.measured, options.merged, options.directory)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
