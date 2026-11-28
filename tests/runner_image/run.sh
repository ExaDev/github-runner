#!/usr/bin/env bash
# Builds the runner image (the Dockerfile at the repository root) for this machine's architecture and checks that it carries what a workflow written for GitHub's ubuntu-24.04 image assumes and installs behind a guard: zstd (actions/cache compresses with it when it is on PATH and falls back to gzip when it is not), gh and yq, and the system packages Playwright's Chromium needs. The Chromium check runs Playwright's own `install-deps chromium` as root against the latest playwright-core release and requires that it installs nothing, so the image's package list cannot fall behind the list Playwright resolves for the image's distribution release.
#
# Usage: tests/runner_image/run.sh. Needs Docker; reaches the base image's registry, the image's distribution's packages and the npm registry. Leaves the built image (grtest/runner-image:test) in place so a rerun reuses the build cache.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
image=grtest/runner-image:test

log() { printf '==> %s\n' "$*"; }

log "Building the runner image"
docker build -q -t "$image" "$repo_root" >/dev/null

log "zstd, gh and yq are on the runner's PATH"
docker run --rm --entrypoint bash "$image" -c 'set -euo pipefail; zstd --version; gh --version | head -n 1; yq --version'

# playwright-core has no dependencies, so its tarball runs as it is with the Node.js the runner image already carries; no package manager is needed in the image.
log "Playwright's install-deps for Chromium installs nothing"
docker run --rm --user 0 --entrypoint bash "$image" -c '
  set -euo pipefail
  tarball="$(curl -fsSL https://registry.npmjs.org/playwright-core/latest | python3 -c "import json,sys; print(json.load(sys.stdin)[\"dist\"][\"tarball\"])")"
  work="$(mktemp -d)"
  cd "$work"
  curl -fsSL "$tarball" | tar xz
  node="$(ls -d /home/runner/externals/node*/bin/node | grep -v _alpine | sort -V | tail -n 1)"
  dpkg-query -f "\${Package}\n" -W | sort >"$work/before"
  "$node" package/cli.js install-deps chromium >"$work/install.log" 2>&1 || { cat "$work/install.log" >&2; exit 1; }
  dpkg-query -f "\${Package}\n" -W | sort >"$work/after"
  added="$(comm -13 "$work/before" "$work/after" | tr "\n" " ")"
  if [ -n "$added" ]; then
    echo "install-deps chromium still installed: $added" >&2
    exit 1
  fi
'

log "all checks passed"
