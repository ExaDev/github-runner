#!/usr/bin/env bash
# Keeps each profile's node cache (see the role README's Capabilities section) under its size limit. Runs in the `node-cache-pruner` DaemonSet, one pod per node a runner pod may use, with the node's cache root mounted read-write.
#
# The cache root holds one directory per profile, <namespace>/<release>, and a runner pod mounts only its own profile's directory. A package manager's store is linked from by every job that installed from it, so removing part of it under a running job breaks that job. The pruner therefore never trims a cache and never works inside a directory a pod may be using: when a profile's cache is over the limit and no runner pod of that profile runs on this node, it moves the cache's contents in one rename each into a trash directory beside the profiles (on the same filesystem, outside every pod's mount), checks again for pods, and only then deletes the trash. A pod that starts at any point sees the whole cache or an empty one, never one being unlinked. The next job rebuilds what it needs.
#
# A cache over the limit while a runner pod runs cannot be emptied, so a node that is never idle would keep growing. When a cache is over the hard limit (LIMIT times DRAIN_FACTOR) the pruner cordons the node, so no new runner pod lands there and the running ones finish, empties the cache once the node has none, and uncordons the node it cordoned (an annotation records that it did).
#
# A profile directory that is not in WANTED_CACHES belongs to a capability or a profile that has been removed, and is emptied and deleted the same way.
#
# Every step of one profile's check may fail without stopping the others: a failed or partial size reading skips that profile for the round, and a failed pod list means no pruning, since an unknown set of pods may be using the cache.
#
# Env:
# - NODE_NAME: the node this pod runs on (the downward API's spec.nodeName). Required.
# - CACHE_ROOT: where the node's cache root is mounted (default: /host-node-cache).
# - LIMIT_GIB: the most gibibytes one profile's cache may hold, a positive integer. Required unless LIMIT_KIB is set.
# - LIMIT_KIB: the limit in kibibytes, which takes the place of LIMIT_GIB, so a test need not write gibibytes.
# - DRAIN_FACTOR: the hard limit as a multiple of the limit, an integer of at least 1 (default: 2).
# - WANTED_CACHES: the profiles that have a cache, as <namespace>/<release> separated by spaces. When it is set, any other directory under the cache root is removed. Unset leaves every directory alone.
# - CHECK_SECONDS: seconds between checks (default: 300).
# - PRUNE_ONCE: when "true", check once and exit, for tests.
set -uo pipefail

: "${NODE_NAME:?NODE_NAME is required}"
CACHE_ROOT="${CACHE_ROOT:-/host-node-cache}"
CHECK_SECONDS="${CHECK_SECONDS:-300}"
PRUNE_ONCE="${PRUNE_ONCE:-false}"
DRAIN_FACTOR="${DRAIN_FACTOR:-2}"
LIMIT_KIB="${LIMIT_KIB:-$(( ${LIMIT_GIB:?LIMIT_GIB or LIMIT_KIB is required} * 1024 * 1024 ))}"
HARD_LIMIT_KIB=$((LIMIT_KIB * DRAIN_FACTOR))
TRASH_DIR="${CACHE_ROOT}/.trash"
CORDONED_ANNOTATION="github-runner.exadev.io/node-cache-cordoned"

log() { echo "$*"; }

# The size of a directory in KiB. du reports the total it could read even when it fails on a file that vanished under it, so its output is used whatever its status; nothing numeric means no reading.
size_kib() {
  local size
  size="$(du -sk "$1" 2>/dev/null | cut -f1)"
  [[ "$size" =~ ^[0-9]+$ ]] || return 1
  echo "$size"
}

# The runner pods of a namespace on this node, one name per line; a failure is the caller's to treat as unknown.
pods_here() {
  kubectl get pods -n "$1" --field-selector "spec.nodeName=${NODE_NAME}" -o name
}

is_wanted() {
  local entry
  [ -n "${WANTED_CACHES+x}" ] || return 0
  for entry in $WANTED_CACHES; do
    [ "$entry" = "$1" ] && return 0
  done
  return 1
}

cordon_node() {
  kubectl cordon "$NODE_NAME" >/dev/null && kubectl annotate node "$NODE_NAME" "${CORDONED_ANNOTATION}=true" --overwrite >/dev/null
}

uncordon_node() {
  kubectl uncordon "$NODE_NAME" >/dev/null && kubectl annotate node "$NODE_NAME" "${CORDONED_ANNOTATION}-" >/dev/null
}

# Whether this pruner cordoned the node and has not yet uncordoned it.
cordoned_by_us() {
  [ "$(kubectl get node "$NODE_NAME" -o jsonpath="{.metadata.annotations.github-runner\\.exadev\\.io/node-cache-cordoned}" 2>/dev/null)" = true ]
}

# Moves a profile's cache out of the directory its pods mount, into the trash, and deletes it there. Returns 1 when the move failed, leaving what was not moved in place.
set_aside_and_delete() {
  local directory=$1 label=$2 aside entry pods
  aside="${TRASH_DIR}/${label//\//-}-$(date +%s)"
  mkdir -p "$aside" || return 1
  shopt -s dotglob nullglob
  for entry in "$directory"/*; do
    mv "$entry" "$aside/" || { shopt -u dotglob nullglob; return 1; }
  done
  shopt -u dotglob nullglob
  # A pod bound since the check finds the profile directory empty, or already refilled by its own init container; the old contents are out of its reach, so deleting them is safe.
  if pods="$(pods_here "${label%%/*}")" && [ -n "$pods" ]; then
    log "node cache ${label}: a runner pod started while the cache was being set aside; it sees an empty cache"
  fi
  rm -rf "$aside"
}

# One profile directory: <namespace>/<release> under the cache root. Sets over_hard when it could not be emptied and is over the hard limit.
check_profile() {
  local directory=$1 namespace release label size pods retired=false
  release="$(basename "$directory")"
  namespace="$(basename "$(dirname "$directory")")"
  label="${namespace}/${release}"
  is_wanted "$label" || retired=true
  if ! size="$(size_kib "$directory")"; then
    log "node cache ${label}: its size could not be read; skipped this round"
    return 0
  fi
  if [ "$retired" = false ] && [ "$size" -le "$LIMIT_KIB" ]; then
    return 0
  fi
  if ! pods="$(pods_here "$namespace")"; then
    log "node cache ${label}: the runner pods on this node could not be listed; not pruned"
    return 0
  fi
  if [ -n "$pods" ]; then
    if [ "$retired" = false ] && [ "$size" -gt "$HARD_LIMIT_KIB" ]; then
      over_hard=true
      log "node cache ${label} holds ${size} KiB, over the hard limit of ${HARD_LIMIT_KIB} KiB, and a runner pod is running on this node: cordoning the node so it can be emptied"
    else
      log "node cache ${label} holds ${size} KiB, over the limit of ${LIMIT_KIB} KiB, but a runner pod is running on this node: left alone"
    fi
    return 0
  fi
  if set_aside_and_delete "$directory" "$label"; then
    if [ "$retired" = true ]; then
      rmdir "$directory" 2>/dev/null && rmdir "$(dirname "$directory")" 2>/dev/null
      log "node cache ${label} is no longer wanted and held ${size} KiB: removed"
    else
      log "node cache ${label} held ${size} KiB, over the limit of ${LIMIT_KIB} KiB: emptied"
    fi
  else
    log "node cache ${label}: it could not be set aside; left as it is"
  fi
}

check() {
  local directory over_hard=false
  # Whatever an earlier round left in the trash was set aside from a cache already: it is garbage.
  rm -rf "${TRASH_DIR:?}"/* 2>/dev/null
  for directory in "$CACHE_ROOT"/*/*/; do
    [ -d "$directory" ] || continue
    check_profile "${directory%/}"
  done
  if [ "$over_hard" = true ]; then
    cordon_node || log "the node could not be cordoned"
  elif cordoned_by_us; then
    uncordon_node && log "the node cache is back under its hard limit: node uncordoned" || log "the node could not be uncordoned"
  fi
}

while true; do
  check
  [ "$PRUNE_ONCE" = "true" ] && exit 0
  sleep "$CHECK_SECONDS"
done
