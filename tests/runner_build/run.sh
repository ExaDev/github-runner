#!/usr/bin/env bash
# Builds the reference build runner image (tool-images/build) twice for this machine's architecture, once with its default ids and once with RUNNER_UID and RUNNER_GID set to an id the stock image does not use, and checks each: the runner user and its primary group have the ids the build was given, no file in the image is owned by the stock ids once they changed, and the runner can still write its home, run its own scripts, use sudo and reach the docker group, which is what a runner pod needs of the image.
#
# Usage: tests/runner_build/run.sh. Needs Docker; reaches the runner image's registry and its distribution's packages. Leaves the built images (grtest/runner-build:default, grtest/runner-build:other-ids) in place so a rerun reuses the build cache.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
context="${repo_root}/tool-images/build"
stock_uid="$(sed -n 's/^ARG RUNNER_UID=//p' "${context}/Dockerfile")"
stock_gid="$(sed -n 's/^ARG RUNNER_GID=//p' "${context}/Dockerfile")"
# The group the stock image gives the docker socket, which the runner must keep.
docker_gid=123

log() { printf '==> %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# An id below the system range's top that the stock image leaves unused: the first one counting down from 999 that neither its passwd nor its group file names, so the test never depends on a number the base image might take in a later release.
free_id() {
  docker run --rm --entrypoint sh "$1" -c 'id=999; while grep -q "^[^:]*:[^:]*:${id}:" /etc/passwd /etc/group; do id=$((id - 1)); done; echo "$id"'
}

# Runs the checks against one built image: $1 the image, $2 and $3 the uid and gid it must give the runner, $4 and $5 the ids no file may keep (empty when they did not change).
check_image() {
  local image=$1 uid=$2 gid=$3 old_uid=$4 old_gid=$5
  docker run --rm --entrypoint bash "$image" -c '
    set -euo pipefail
    uid=$1 gid=$2 old_uid=$3 old_gid=$4 docker_gid=$5
    [ "$(id -u)" = "$uid" ] || { echo "runs as uid $(id -u), not $uid" >&2; exit 1; }
    [ "$(id -g)" = "$gid" ] || { echo "runs as gid $(id -g), not $gid" >&2; exit 1; }
    [ "$(id -un)" = runner ] || { echo "uid $uid is $(id -un), not runner" >&2; exit 1; }
    [ "$(getent group runner | cut -d: -f3)" = "$gid" ] || { echo "group runner is not gid $gid" >&2; exit 1; }
    id -G | tr " " "\n" | grep -qx "$docker_gid" || { echo "runner lost the docker group ($docker_gid)" >&2; exit 1; }
    if [ -n "$old_uid" ]; then
      left=$(sudo -n find / -xdev \( -uid "$old_uid" -o -gid "$old_gid" \) | head -n 5)
      [ -z "$left" ] || { echo "files still owned by the stock ids: $left" >&2; exit 1; }
    fi
    [ "$(stat -c %u /home/runner/run.sh)" = "$uid" ] || { echo "run.sh is not owned by the runner" >&2; exit 1; }
    touch /home/runner/.write-check && rm /home/runner/.write-check
    /home/runner/bin/Runner.Listener --version >/dev/null
    sudo -n true
    cc --version >/dev/null
  ' check "$uid" "$gid" "$old_uid" "$old_gid" "$docker_gid"
}

log "tool-images/build with its default ids (${stock_uid}:${stock_gid})"
docker build -q -t grtest/runner-build:default "$context" >/dev/null
check_image grtest/runner-build:default "$stock_uid" "$stock_gid" "" ""

other="$(free_id grtest/runner-build:default)"
[ "$other" != "$stock_uid" ] && [ "$other" != "$stock_gid" ] || fail "no free id below 1000 in the image"
log "tool-images/build with RUNNER_UID and RUNNER_GID ${other}"
docker build -q --build-arg "RUNNER_UID=${other}" --build-arg "RUNNER_GID=${other}" -t grtest/runner-build:other-ids "$context" >/dev/null
check_image grtest/runner-build:other-ids "$other" "$other" "$stock_uid" "$stock_gid"

# The image refuses an id it already uses rather than giving the runner a shared one: 0 is root's.
log "tool-images/build refuses RUNNER_UID 0"
if docker build -q --build-arg RUNNER_UID=0 -t grtest/runner-build:refused "$context" >/dev/null 2>&1; then
  fail "the build accepted RUNNER_UID 0, an id the image already uses"
fi

log "all checks passed"
