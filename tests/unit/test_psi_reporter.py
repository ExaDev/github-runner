"""Tests for scripts/psi-reporter.sh: one report from a scripted /proc/pressure, with a stand-in for kubectl that keeps the patch it is given."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "psi-reporter.sh"

FAKE_KUBECTL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    args = sys.argv[1:]
    if args[:2] == ["patch", "configmap"] and "--patch-file" in args:
        open(os.environ["FAKE_PATCH_OUT"], "w").write(open(args[args.index("--patch-file") + 1]).read())
        open(os.environ["FAKE_ARGS_OUT"], "w").write(" ".join(args))
        sys.exit(0)
    sys.stderr.write("unexpected kubectl call: %s\\n" % args)
    sys.exit(2)
    """
)

IO = "some avg10=27.47 avg60=28.46 avg300=29.43 total=1624379039\nfull avg10=19.41 avg60=23.80 avg300=26.64 total=1491824298\n"
CPU_V1 = "some avg10=0.88 avg60=0.65 avg300=0.87 total=99315065\n"
MEMORY = "some avg10=0.08 avg60=0.04 avg300=0.03 total=26200355\nfull avg10=0.01 avg60=0.03 avg300=0.01 total=21701934\n"


class ReporterTest(unittest.TestCase):
    def report(self, files: dict[str, str], **env: str) -> tuple[dict[str, object], str]:
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            pressure = root / "pressure"
            pressure.mkdir()
            for name, content in files.items():
                (pressure / name).write_text(content)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(FAKE_KUBECTL)
            kubectl.chmod(0o755)
            patch_out, args_out = root / "patch.json", root / "args.txt"
            full_env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "NODE_NAME": "node-a", "NAMESPACE": "platform", "PRESSURE_DIR": str(pressure), "REPORT_ONCE": "true", "FAKE_PATCH_OUT": str(patch_out), "FAKE_ARGS_OUT": str(args_out), **env}
            subprocess.run(["bash", str(SCRIPT)], check=True, env=full_env, capture_output=True, text=True)
            patch: dict[str, object] = json.loads(patch_out.read_text())
            return patch, args_out.read_text()

    def test_reports_each_line_the_kernel_has_under_the_nodes_own_key(self) -> None:
        patch, args = self.report({"io": IO, "cpu": CPU_V1, "memory": MEMORY})
        data = patch["data"]
        assert isinstance(data, dict)
        self.assertEqual(list(data), ["node-a"])
        entry = json.loads(data["node-a"])
        self.assertEqual(
            {key: value for key, value in entry.items() if key != "at"},
            {"io_some_avg10": 27.47, "io_full_avg10": 19.41, "cpu_some_avg10": 0.88, "memory_some_avg10": 0.08, "memory_full_avg10": 0.01},
        )
        self.assertGreater(entry["at"], 0)
        self.assertIn("patch configmap autoscaler-node-pressure -n platform --type merge --patch-file", args)

    def test_a_line_the_kernel_does_not_report_is_left_out_not_reported_as_zero(self) -> None:
        patch, _ = self.report({"io": IO, "cpu": CPU_V1, "memory": MEMORY})
        data = patch["data"]
        assert isinstance(data, dict)
        self.assertNotIn("cpu_full_avg10", json.loads(data["node-a"]))

    def test_a_missing_pressure_file_leaves_that_resource_out(self) -> None:
        patch, _ = self.report({"io": IO})
        data = patch["data"]
        assert isinstance(data, dict)
        self.assertEqual(sorted(key for key in json.loads(data["node-a"]) if key != "at"), ["io_full_avg10", "io_some_avg10"])

    def test_the_configmap_name_and_namespace_are_configurable(self) -> None:
        _, args = self.report({"io": IO}, NODE_PRESSURE_CONFIGMAP="pressure-x", NAMESPACE="other")
        self.assertIn("patch configmap pressure-x -n other", args)

    def test_it_refuses_to_start_without_a_node_name(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            env = {key: value for key, value in os.environ.items() if key != "NODE_NAME"}
            result = subprocess.run(["bash", str(SCRIPT)], env={**env, "NAMESPACE": "x", "PRESSURE_DIR": work, "REPORT_ONCE": "true"}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("NODE_NAME is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
