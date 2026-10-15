#!/usr/bin/env bash
# Keeps each profile's node cache (see the role README's Capabilities section) under its size limit. Runs in the `node-cache-pruner` DaemonSet, one pod per node a runner pod may use, with the node's cache root mounted read-write.
#
# The cache root holds one directory per profile, <namespace>/<release>, and a runner pod mounts only its own profile's directory. A package manager's store is linked from by every job that installed from it, so removing part of one under a running job breaks that job. The pruner therefore never trims a store and never deletes inside a directory a pod may be using. When a profile's cache is over the limit and no runner pod that mounts it runs on this node, it moves each entry of the cache, one rename each, into a unique trash directory beside the profiles (on the same filesystem, outside every pod's mount), checks again for pods before every rename, and deletes the trash only afterwards. Each store is therefore seen whole or not at all by a pod that starts at any point; a pod that starts part way through finds the stores not yet moved whole, and the rest empty. The next job rebuilds what it needs.
#
# A cache over the limit while a runner pod mounts it cannot be emptied, so a node that is never idle would keep growing. The pruner does not change the node to force that. A cache over the hard limit (LIMIT times HARD_LIMIT_FACTOR) is reported on every round with the stable prefix NODE_CACHE_OVER_HARD_LIMIT, for whatever collects the pruner's logs to alert on; kubelet's disk-pressure eviction still protects the node itself. Draining the node, or lowering its runner count, lets the next round empty the cache.
#
# What remains is a check-then-act window: a pod can be bound, and its init container can mount the directory, between the last list and the rename that follows it. The window is the time between two commands, a runner pod needs seconds to start its job, and a pod that did start meets an empty cache it can refill (the cache's root is owned by the runner's user), not a cache being unlinked.
#
# A profile directory that is not in WANTED_CACHES belongs to a capability that has been removed from a profile still in the fleet, or to a profile that has left it, and is emptied and removed the same way. While the pruner is installed the role keeps the read-only grant to list pods of every profile it has installed, including one that has since left the fleet, so such a directory can always be checked; the grant is removed only with the pruner. A list that is refused all the same (the namespace was deleted together with its Role) is reported on every round as NODE_CACHE_UNPRUNABLE and the directory is left for the operator to delete (with the namespace gone there are no pods, so it is safe to).
#
# Every step of one profile's check may fail without stopping the others: a failed or partial size reading skips that profile for the round, and a failed pod list means no pruning, since an unknown set of pods may be using the cache: NODE_CACHE_UNPRUNABLE when the server refused the list as Forbidden (there is no Role for the pruner in that namespace), NODE_CACHE_LIST_FAILED for any other failure (the API server was unreachable, the answer was not a pod list), which the next round retries.
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
LIST_ERRORS="$(mktemp)"
trap 'rm -f "$LIST_ERRORS"' EXIT

log() { echo "$*"; }

# The size of a directory in KiB. du reports the total it could read even when it fails on a file that vanished under it, so its output is used whatever its status; nothing numeric means no reading.
size_kib() {
  local size
  size="$(du -sk "$1" 2>/dev/null | cut -f1)"
  [[ "$size" =~ ^[0-9]+$ ]] || return 1
  echo "$size"
}

# The names of the pods that mount a profile's cache and run on this node, one per line, or a failure when they cannot be listed, in which case the errors are in LIST_ERRORS.
pods_using() {
  local namespace=$1 release=$2
  : > "$LIST_ERRORS"
  kubectl get pods -n "$namespace" --field-selector "spec.nodeName=${NODE_NAME},status.phase!=Succeeded,status.phase!=Failed" -o json 2>>"$LIST_ERRORS" \
    | jq -r --arg path "${HOST_CACHE_ROOT%/}/${namespace}/${release}" '.items[] | select(any(.spec.volumes[]?; .hostPath.path == $path)) | .metadata.name' 2>>"$LIST_ERRORS"
}

# The log prefix for a list that failed: NODE_CACHE_UNPRUNABLE when kubectl reports the server's refusal in its own form, "Error from server (Forbidden)", as the first thing on a line, NODE_CACHE_LIST_FAILED for anything else (a wrapped or quoted mention of forbidden in some other error does not count).
list_failure_prefix() {
  if grep -q '^Error from server (Forbidden)' "$LIST_ERRORS" 2>/dev/null; then echo NODE_CACHE_UNPRUNABLE; else echo NODE_CACHE_LIST_FAILED; fi
}

is_wanted() {
  local entry
  [ -n "${WANTED_CACHES+x}" ] || return 0
  for entry in $WANTED_CACHES; do
    [ "$entry" = "$1" ] && return 0
  done
  return 1
}

# Moves a profile's cache out of the directory its pods mount, one rename per entry, and deletes it. The pods are listed again directly before every rename, and a pod that mounts the cache ends the moving, leaving what has not moved in place. Sets SET_ASIDE to "all" when every entry moved (or there was none), "some" when only a part did, and "none" when something was to be moved and nothing was.
set_aside_and_delete() {
  local directory=$1 namespace=$2 release=$3 size=$4 aside entry pods moved=0 entries
  SET_ASIDE=none
  mkdir -p "$TRASH_DIR" && aside="$(mktemp -d "${TRASH_DIR}/set-aside.XXXXXX")" || return 1
  shopt -s dotglob nullglob
  entries=("$directory"/*)
  for entry in "${entries[@]}"; do
    if ! pods="$(pods_using "$namespace" "$release")"; then
      log "$(list_failure_prefix) ${namespace}/${release} size_kib=${size}: the runner pods could not be listed part way through; the rest is left in place"
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
  if [ "$moved" -eq "${#entries[@]}" ]; then SET_ASIDE=all; elif [ "$moved" -gt 0 ]; then SET_ASIDE=some; fi
  # What was moved is out of every pod's reach, so deleting it cannot disturb a job. A deletion that fails is left for the next round, which clears the trash first.
  rm -rf "$aside" || log "node cache ${namespace}/${release}: the set-aside copy could not be deleted; the next round retries"
}

# Removes the directory of a profile whose cache has been emptied, and its namespace's directory when that is empty too, unless a pod has come to mount it since: a pod that mounted a directory the kubelet created for it would lose it. The pods are listed once more first. Returns 1 when the directory was kept.
remove_retired_directory() {
  local directory=$1 namespace=$2 release=$3 pods
  if ! pods="$(pods_using "$namespace" "$release")" || [ -n "$pods" ]; then
    return 1
  fi
  rmdir "$directory" 2>/dev/null
  rmdir "$(dirname "$directory")" 2>/dev/null
  return 0
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
    log "$(list_failure_prefix) ${label} size_kib=${size}: the runner pods on this node could not be listed; not pruned"
    if [ "$retired" = false ] && [ "$size" -gt "$HARD_LIMIT_KIB" ]; then
      log "NODE_CACHE_OVER_HARD_LIMIT ${label} size_kib=${size} hard_limit_kib=${HARD_LIMIT_KIB} node=${NODE_NAME}: whether a runner pod keeps it busy is unknown, since the pods could not be listed"
    fi
    return 0
  fi
  if [ -n "$pods" ]; then
    if [ "$retired" = true ]; then
      log "node cache ${label} is no longer wanted and holds ${size} KiB, but a runner pod mounts it on this node: left alone"
    elif [ "$size" -gt "$HARD_LIMIT_KIB" ]; then
      log "NODE_CACHE_OVER_HARD_LIMIT ${label} size_kib=${size} hard_limit_kib=${HARD_LIMIT_KIB} node=${NODE_NAME}: a runner pod keeps it busy, so it cannot be emptied"
    else
      log "node cache ${label} holds ${size} KiB, over the limit of ${LIMIT_KIB} KiB, but a runner pod is running on this node: left alone"
    fi
    return 0
  fi
  set_aside_and_delete "$directory" "$namespace" "$release" "$size"
  case "$SET_ASIDE" in
    all)
      if [ "$retired" = true ]; then
        if remove_retired_directory "$directory" "$namespace" "$release"; then
          log "node cache ${label} is no longer wanted and held ${size} KiB: deleted, and its directory removed"
        else
          log "node cache ${label} is no longer wanted and held ${size} KiB: deleted, but its directory is kept because a runner pod may mount it now"
        fi
      else
        log "node cache ${label} held ${size} KiB, over the limit of ${LIMIT_KIB} KiB: emptied"
      fi
      ;;
    some) log "node cache ${label} held ${size} KiB: part of it was set aside and deleted, the rest is left in place" ;;
    *) log "node cache ${label}: nothing could be set aside; left as it is" ;;
  esac
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
