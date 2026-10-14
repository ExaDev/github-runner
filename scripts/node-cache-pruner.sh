#!/usr/bin/env bash
# Keeps each profile's node cache (see the role README's Capabilities section) under its size limit. Runs in the `node-cache-pruner` DaemonSet, one pod per node a runner pod may use, with the node's cache root mounted read-write.
#
# The cache root holds one directory per profile, <namespace>/<release>, and a runner pod mounts only its own profile's directory. A package manager's store is linked from by every job that installed from it, so removing part of one under a running job breaks that job. The pruner therefore never trims a store and never deletes inside a directory a pod may be using. When a profile's cache is over the limit and no runner pod that mounts it runs on this node, it moves each entry of the cache, one rename each, into a unique trash directory beside the profiles (on the same filesystem, outside every pod's mount), checks again for pods before every rename, and deletes the trash only afterwards. Each store is therefore seen whole or not at all by a pod that starts at any point; a pod that starts part way through finds the stores not yet moved whole, and the rest empty. The next job rebuilds what it needs.
#
# A cache over the limit while a runner pod mounts it cannot be emptied, so a node that is never idle would keep growing. The pruner does not change the node to force that. A cache over the hard limit (LIMIT times HARD_LIMIT_FACTOR) is reported on every round with the stable prefix NODE_CACHE_OVER_HARD_LIMIT, for whatever collects the pruner's logs to alert on; kubelet's disk-pressure eviction still protects the node itself. Draining the node, or lowering its runner count, lets the next round empty the cache.
#
# A profile directory that is not in WANTED_CACHES belongs to a capability that has been removed from a profile, and is emptied and deleted the same way.
#
# Every step of one profile's check may fail without stopping the others: a failed or partial size reading skips that profile for the round, and a failed pod list means no pruning, since an unknown set of pods may be using the cache.
#
# Which pods count: those in the profile's namespace on this node, not finished (Succeeded or Failed), that have a hostPath volume at exactly the profile's directory on the host (HOST_CACHE_ROOT/<namespace>/<release>). Another profile's pods, finished pods and pods that do not mount the cache are ignored.
#
# Env:
# - NODE_NAME: the node this pod runs on (the downward API's spec.nodeName). Required.
# - HOST_CACHE_ROOT: the cache root as the node knows it, which is the path of the pods' hostPath volumes up to <namespace>/<release>. Required.
# - CACHE_ROOT: where the node's cache root is mounted in this pod (default: /host-node-cache).
# - LIMIT_GIB: the most gibibytes one profile's cache may hold, a positive integer. Required unless LIMIT_KIB is set.
# - LIMIT_KIB: the limit in kibibytes, which takes the place of LIMIT_GIB, so a test need not write gibibytes.
# - HARD_LIMIT_FACTOR: the hard limit as a multiple of the limit, an integer of at least 1 (default: 2).
# - WANTED_CACHES: the profiles that have a cache, as <namespace>/<release> separated by spaces. When it is set, any other directory under the cache root is removed. Unset leaves every directory alone.
# - CHECK_SECONDS: seconds between checks (default: 300).
# - PRUNE_ONCE: when "true", check once and exit, for tests.
set -uo pipefail

: "${NODE_NAME:?NODE_NAME is required}"
: "${HOST_CACHE_ROOT:?HOST_CACHE_ROOT is required}"
CACHE_ROOT="${CACHE_ROOT:-/host-node-cache}"
CHECK_SECONDS="${CHECK_SECONDS:-300}"
PRUNE_ONCE="${PRUNE_ONCE:-false}"
HARD_LIMIT_FACTOR="${HARD_LIMIT_FACTOR:-2}"
LIMIT_KIB="${LIMIT_KIB:-$(( ${LIMIT_GIB:?LIMIT_GIB or LIMIT_KIB is required} * 1024 * 1024 ))}"
HARD_LIMIT_KIB=$((LIMIT_KIB * HARD_LIMIT_FACTOR))
TRASH_DIR="${CACHE_ROOT}/.trash"

log() { echo "$*"; }

# The size of a directory in KiB. du reports the total it could read even when it fails on a file that vanished under it, so its output is used whatever its status; nothing numeric means no reading.
size_kib() {
  local size
  size="$(du -sk "$1" 2>/dev/null | cut -f1)"
  [[ "$size" =~ ^[0-9]+$ ]] || return 1
  echo "$size"
}

# The names of the pods that mount a profile's cache and run on this node, one per line, or a failure when they cannot be listed.
pods_using() {
  local namespace=$1 release=$2
  kubectl get pods -n "$namespace" --field-selector "spec.nodeName=${NODE_NAME},status.phase!=Succeeded,status.phase!=Failed" -o json \
    | jq -r --arg path "${HOST_CACHE_ROOT%/}/${namespace}/${release}" '.items[] | select(any(.spec.volumes[]?; .hostPath.path == $path)) | .metadata.name'
}

is_wanted() {
  local entry
  [ -n "${WANTED_CACHES+x}" ] || return 0
  for entry in $WANTED_CACHES; do
    [ "$entry" = "$1" ] && return 0
  done
  return 1
}

# Moves a profile's cache out of the directory its pods mount, one rename per entry, and deletes it. Before every rename the pods are listed again, and a pod that mounts the cache ends the moving, leaving what has not moved in place. Returns 1 when there was something to move and none of it could be.
set_aside_and_delete() {
  local directory=$1 namespace=$2 release=$3 aside entry pods moved=0 entries
  mkdir -p "$TRASH_DIR" && aside="$(mktemp -d "${TRASH_DIR}/set-aside.XXXXXX")" || return 1
  shopt -s dotglob nullglob
  entries=("$directory"/*)
  for entry in "${entries[@]}"; do
    if ! pods="$(pods_using "$namespace" "$release")"; then
      log "node cache ${namespace}/${release}: the runner pods could not be listed part way through; the rest is left in place"
      break
    fi
    if [ -n "$pods" ]; then
      log "node cache ${namespace}/${release}: a runner pod started while the cache was being set aside; the rest is left in place"
      break
    fi
    if mv "$entry" "$aside/"; then
      moved=$((moved + 1))
    else
      log "node cache ${namespace}/${release}: ${entry##*/} could not be moved; the rest is left in place"
      break
    fi
  done
  shopt -u dotglob nullglob
  # What was moved is out of every pod's reach, so deleting it cannot disturb a job. A deletion that fails is left for the next round, which clears the trash first.
  rm -rf "$aside" || log "node cache ${namespace}/${release}: the set-aside copy could not be deleted; the next round retries"
  [ "$moved" -gt 0 ] || [ "${#entries[@]}" -eq 0 ]
}

# One profile directory: <namespace>/<release> under the cache root.
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
  if ! pods="$(pods_using "$namespace" "$release")"; then
    log "node cache ${label}: the runner pods on this node could not be listed; not pruned"
    return 0
  fi
  if [ -n "$pods" ]; then
    if [ "$retired" = false ] && [ "$size" -gt "$HARD_LIMIT_KIB" ]; then
      log "NODE_CACHE_OVER_HARD_LIMIT ${label} size_kib=${size} hard_limit_kib=${HARD_LIMIT_KIB} node=${NODE_NAME}: a runner pod keeps it busy, so it cannot be emptied"
    else
      log "node cache ${label} holds ${size} KiB, over the limit of ${LIMIT_KIB} KiB, but a runner pod is running on this node: left alone"
    fi
    return 0
  fi
  if set_aside_and_delete "$directory" "$namespace" "$release"; then
    if [ "$retired" = true ]; then
      rmdir "$directory" 2>/dev/null
      rmdir "$(dirname "$directory")" 2>/dev/null
      log "node cache ${label} is no longer wanted and held ${size} KiB: removed"
    else
      log "node cache ${label} held ${size} KiB, over the limit of ${LIMIT_KIB} KiB: emptied"
    fi
  else
    log "node cache ${label}: nothing could be set aside; left as it is"
  fi
}

check() {
  local directory
  # Whatever an earlier round left in the trash was set aside from a cache already: it is garbage.
  rm -rf "${TRASH_DIR:?}"/* 2>/dev/null
  for directory in "$CACHE_ROOT"/*/*/; do
    [ -d "$directory" ] || continue
    check_profile "${directory%/}"
  done
}

while true; do
  check
  [ "$PRUNE_ONCE" = "true" ] && exit 0
  sleep "$CHECK_SECONDS"
done
