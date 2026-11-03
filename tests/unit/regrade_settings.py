"""Reads a regrade settings file (templates/regrade.env.j2) back with a real shell, for the tests of the role's regrading (test_cluster_regrade.py and test_native_regrade.py): what a shell that sources the file, as fleet-regrade.sh does, makes of each variable it sets."""

from __future__ import annotations

import subprocess


def sourced(text: str) -> dict[str, str]:
    """Every variable the settings file sets, as bash makes of it after sourcing the file under ``set -eu``, in an environment that holds nothing else.

    Raises ``subprocess.CalledProcessError`` when the file does not source cleanly.
    """
    # set -a exports every assignment the file makes, so env -0 lists them; the variables bash itself exports into an empty environment are left out.
    script = "set -eu\nset -a\n. /dev/stdin <<'ENV'\n" + text + "ENV\nset +a\nenv -0\n"
    shell = subprocess.run(["bash", "-c", "env -0"], capture_output=True, text=True, check=True, env={}).stdout
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True, env={})
    own = {entry.split("=", 1)[0] for entry in shell.split("\0") if "=" in entry}
    return {name: value for name, value in (entry.split("=", 1) for entry in result.stdout.split("\0") if "=" in entry) if name not in own}
