#!/usr/bin/env bash
# Prints the lowest kubernetes.core version galaxy.yml accepts, read from its plain ">=X.Y.Z" requirement, for tests/helm_apply/run.sh and the Helm apply workflow, so both take the floor from one place. Exits non-zero with a message when the requirement is not such a bound.
#
# Usage: tests/helm_apply/kubernetes-core-floor.sh. Needs python3 with PyYAML (PYTHON overrides which python).
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
"${PYTHON:-python3}" - "$repo_root/galaxy.yml" <<'PY'
import re, sys
import yaml
spec = str(yaml.safe_load(open(sys.argv[1]))["dependencies"]["kubernetes.core"]).strip()
match = re.fullmatch(r">=\s*(\d+\.\d+\.\d+)", spec)
if match is None:
    sys.exit(f"galaxy.yml's kubernetes.core requirement {spec!r} is not a plain >= lower bound, so the floor version cannot be read from it")
print(match.group(1))
PY
