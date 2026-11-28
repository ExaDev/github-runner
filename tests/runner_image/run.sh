#!/usr/bin/env bash
# Builds each runner image in this repository for this machine's architecture, the Dockerfile at the repository root and tool-images/build (the image the fleets' pods run), and checks that it carries what a workflow written for GitHub's ubuntu-24.04 image installs behind a guard: zstd (actions/cache compresses with it when it is on PATH and falls back to gzip when it is not), gh, and the system packages Playwright's Chromium needs. The Chromium check runs Playwright's own `install-deps chromium` as root against the latest playwright-core release and requires that it installs nothing, so an image's package list cannot fall behind the list Playwright resolves for its distribution release.
#
# Usage: tests/runner_image/run.sh. Needs Docker; reaches the base image's registry, the images' distribution's packages and the npm registry. Leaves the built images (grtest/runner-image:root and grtest/runner-image:build) in place so a rerun reuses the build cache.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log() { printf '==> %s\n' "$*"; }

# $1 the tag to build under, $2 the build context.
check_image() {
  local image="grtest/runner-image:$1" context=$2
  log "Building ${context}"
  docker build -q -t "$image" "$context" >/dev/null

  log "${context}: zstd and gh are on the runner's PATH"
  docker run --rm --entrypoint bash "$image" -c 'set -euo pipefail; zstd --version; gh --version | head -n 1'

  # playwright-core has no dependencies, so its tarball runs as it is with the Node.js the runner image already carries (the glibc build: the musl ones cannot run here); no package manager is needed in the image.
  log "${context}: Playwright's install-deps for Chromium installs nothing"
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
}

check_image root "$repo_root"
check_image build "${repo_root}/tool-images/build"

log "all checks passed"
