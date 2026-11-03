"""Reads a mise global configuration back with a real mise, for the tests of the github_runner_host_tools role (test_host_tools.py and test_host_tools_filters.py): what mise exports for the files in a home directory's conf.d, with everything mise reads kept inside that directory."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

MISE = shutil.which("mise")


def mise_reads(home: Path) -> tuple[dict[str, str], str]:
    """Return the variables a real mise exports for the configuration files under ``home``/.config/mise, and what it printed to stderr.

    The home directory is the one a template such as ``{{ env.HOME }}`` works out, and the data, cache and state directories are inside it, so the result depends on nothing of the machine running the test.

    :raises AssertionError: mise is not on PATH, which a test that reads a file back with it needs.
    """
    assert MISE is not None, "mise is needed to read the rendered file back; install it (mise, Homebrew or the release tarball) and put it on PATH"
    env = {
        "PATH": f"{Path(MISE).parent}{os.pathsep}/usr/bin:/bin",
        "HOME": str(home),
        "MISE_TRUSTED_CONFIG_PATHS": str(home),
        "MISE_DATA_DIR": str(home / "data"),
        "MISE_CACHE_DIR": str(home / "cache"),
        "MISE_STATE_DIR": str(home / "state"),
    }
    result = subprocess.run([MISE, "env", "--json"], capture_output=True, text=True, check=True, cwd=home, env=env)
    exported = json.loads(result.stdout)
    assert isinstance(exported, dict)
    return exported, result.stderr


def mise_env(toml: str, home: Path) -> tuple[dict[str, str], str]:
    """Write ``toml`` as ``home``/.config/mise/conf.d/00-fleet.toml and return what ``mise_reads`` returns for ``home``."""
    conf = home / ".config" / "mise" / "conf.d"
    conf.mkdir(parents=True)
    (conf / "00-fleet.toml").write_text(toml)
    return mise_reads(home)
