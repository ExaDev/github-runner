"""Injected validation tests, without package queries or guest setup."""

import ast
import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path

import guest_provision


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(Path(__file__).with_name("guest_manifest.json").read_text(encoding="utf-8"))

    def test_manifest_covers_transitive_local_imports(self):
        root = Path(__file__).parent
        included = set(self.manifest["files"])
        modules = {path.stem for path in root.glob("*.py")}
        queue = [name for name in included if name.endswith(".py")]
        checked = set()
        while queue:
            name = queue.pop()
            if name in checked:
                continue
            checked.add(name)
            tree = ast.parse((root / name).read_text(encoding="utf-8"), filename=name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                for module in names:
                    if module in modules:
                        dependency = module + ".py"
                        self.assertIn(dependency, included, f"{name} imports omitted {dependency}")
                        queue.append(dependency)

    def test_prebuilt_guest_prerequisites(self):
        calls, files = [], []
        def run(command, **kwargs):
            calls.append(command)
            output = "1.54.3\n" if command[0] == "NetworkManager" else "install ok installed"
            return types.SimpleNamespace(stdout=output)
        def is_file(path):
            files.append(path)
            return True
        guest_provision.validate_guest(self.manifest, run=run, is_file=is_file)
        self.assertEqual(len(calls), len(guest_provision.PACKAGES) + 1)
        self.assertEqual([p.name for p in files], list(guest_provision.FILES))
        self.assertTrue(all(p.parent == Path(guest_provision.DESTINATION) for p in files))

    def test_manifest_drift_fails_before_commands(self):
        self.manifest["files"].append("extra.py")
        calls = []
        with self.assertRaisesRegex(ValueError, "manifest"):
            guest_provision.validate_guest(self.manifest, run=lambda *args, **kwargs: calls.append(args))
        self.assertEqual(calls, [])

    def test_provision_validates_every_file_before_install(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            hashes = {}
            for name in guest_provision.FILES:
                data = name.encode()
                (source / name).write_bytes(data)
                hashes[name] = hashlib.sha256(data).hexdigest()
            installed = []
            def run(command, **kwargs):
                return types.SimpleNamespace(stdout="1.54.3" if command[0] == "NetworkManager" else "install ok installed")
            guest_provision.provision(source, self.manifest, hashes, guard=lambda: None, run=run, install=installed.append)
            self.assertEqual(installed, [{name: name.encode() for name in guest_provision.FILES}])
            (source / guest_provision.FILES[-1]).write_bytes(b"changed")
            installed.clear()
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                guest_provision.provision(source, self.manifest, hashes, guard=lambda: None, run=run, install=installed.append)
            self.assertEqual(installed, [])

    def test_production_guard_refuses_before_any_work(self):
        with self.assertRaisesRegex(RuntimeError, "provenance"):
            guest_provision.provision("/not-read", self.manifest, {}, guard=guest_provision.refuse_unverified_guest)

    def test_missing_package_version_and_file_fail(self):
        def absent(*args, **kwargs):
            return types.SimpleNamespace(stdout="deinstall ok config-files")
        with self.assertRaisesRegex(RuntimeError, "package not installed"):
            guest_provision.validate_guest(self.manifest, run=absent)
        def wrong_version(command, **kwargs):
            return types.SimpleNamespace(stdout="1.0" if command[0] == "NetworkManager" else "install ok installed")
        with self.assertRaisesRegex(RuntimeError, "version mismatch"):
            guest_provision.validate_guest(self.manifest, run=wrong_version)
        def installed(command, **kwargs):
            return types.SimpleNamespace(stdout="1.54.3" if command[0] == "NetworkManager" else "install ok installed")
        with self.assertRaisesRegex(RuntimeError, "fixture file missing"):
            guest_provision.validate_guest(self.manifest, run=installed, is_file=lambda path: False)


if __name__ == "__main__":
    unittest.main()
