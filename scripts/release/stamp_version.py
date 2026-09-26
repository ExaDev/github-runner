#!/usr/bin/env python3
"""Stamp the collection version into galaxy.yml and the published-image tag defaults in roles/github_runner_arc/defaults/main.yml, ahead of `ansible-galaxy collection build`.

Called from .releaserc.json's @semantic-release/exec prepareCmd, with the new version as the only argument (semantic-release supplies it via ${nextRelease.version}). galaxy.yml's `version:` stays 0.0.0 in source between releases (see its own header comment) and is stamped fresh here each release; the arc-role image defaults are matched by variable name via regex, not by line number, since roles/github_runner_arc/defaults/main.yml can be edited concurrently by other work on this repo, and a line-number-based patch would silently corrupt an unrelated line.

Stamps exactly the variables named in IMAGE_VARS in that file, one per image the release workflow builds for the role. The runner image itself (ghcr.io/exadev/github-runner) has no equivalent role default to stamp: it is configured per-org, per-profile in each host's own host_vars (see roles/github_runner_arc/tasks/install_scale_set_profile.yml's `org.image`), which is fleet inventory configuration, not collection content, and is excluded from the built collection entirely (see galaxy.yml's build_ignore entry for ansible).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GALAXY_YML = REPO_ROOT / "galaxy.yml"
ARC_DEFAULTS = REPO_ROOT / "roles" / "github_runner_arc" / "defaults" / "main.yml"

# One entry per image-tag default this script stamps. Matched by variable name at the start of the line, with the existing tag (whatever it currently is, not necessarily "latest") replaced. This is what makes the regex robust against manual edits to the surrounding file, rather than tied to today's exact "latest" value or to a specific line number.
IMAGE_VARS = (
    "github_runner_arc_heartbeat_image",
    "github_runner_arc_autoscaler_image",
    "github_runner_arc_image_pull_secret_renewer_image",
)


def stamp_galaxy_version(version: str) -> None:
    text = GALAXY_YML.read_text()
    new_text, count = re.subn(
        r"^version:\s*.*$",
        f"version: {version}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise SystemExit(f"expected exactly one `version:` line in {GALAXY_YML}, found {count}")
    GALAXY_YML.write_text(new_text)


def stamp_image_tags(version: str) -> None:
    text = ARC_DEFAULTS.read_text()
    for var_name in IMAGE_VARS:
        pattern = re.compile(
            rf'^({re.escape(var_name)}:\s*"ghcr\.io/exadev/[^:"]+):[^"]+(")$',
            flags=re.MULTILINE,
        )
        new_text, count = pattern.subn(rf"\g<1>:{version}\g<2>", text)
        if count != 1:
            raise SystemExit(f"expected exactly one {var_name} line in {ARC_DEFAULTS}, found {count}")
        text = new_text
    ARC_DEFAULTS.write_text(text)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} <version>")
    version = sys.argv[1]
    stamp_galaxy_version(version)
    stamp_image_tags(version)
    print(f"Stamped version {version} into {GALAXY_YML.relative_to(REPO_ROOT)} and {ARC_DEFAULTS.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
