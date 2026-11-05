"""Exercise mesh teardown with a Compose project holding an orphan."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class MeshTeardownTest(unittest.TestCase):
    def test_removes_orphans_before_deleting_work(self):
        source = (ROOT / "tests/mesh/run.sh").read_text()
        teardown = source.split("teardown() {\n", 1)[1].split("\n}\n", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            work = base / "work"
            project = work / "grtest-node1"
            project.mkdir(parents=True)
            (project / "docker-compose.yml").write_text("services: {}\n")
            orphan = base / "orphan"
            orphan.touch()
            docker = base / "docker"
            docker.write_text(
                '#!/bin/bash\n'
                'if [ "$1" = compose ]; then\n'
                '  case " $* " in *" --remove-orphans "*) rm "$ORPHAN" ;; esac\n'
                'fi\n'
            )
            docker.chmod(0o755)
            script = (
                'set -euo pipefail\n'
                f'work="$1"; node_count=1; native_host=grtest-native; network=grtest-mesh\n'
                'node_name() { printf "grtest-node%s" "$1"; }\n'
                'vm() { printf "grtest-vm%s" "$1"; }\n'
                'teardown() {\n' + teardown + '\n}\n'
                'teardown\n'
            )
            env = os.environ.copy()
            env["PATH"] = f'{base}{os.pathsep}{env["PATH"]}'
            env["ORPHAN"] = str(orphan)
            subprocess.run(["bash", "-c", script, "test", str(work)], env=env, check=True)
            self.assertFalse(orphan.exists())
            self.assertFalse(work.exists())


if __name__ == "__main__":
    unittest.main()
