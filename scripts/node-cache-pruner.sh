#!/usr/bin/env bash
# Keeps each profile's node cache (see the role README's Capabilities section) under its size limit. Runs in the `node-cache-pruner` DaemonSet, one pod per node a runner pod may use, with the node's cache root mounted read-write.
#
# A cache is empty-or-in-use: a package manager's store is linked from by every job that installed from it, and a job's dependency tree holds links into it, so removing part of it under a running job breaks that job. The pruner therefore never trims a cache. When a profile's cache exceeds the limit it empties the whole cache, and only while no runner pod of that profile runs on this node, so no job can be using it. The next job rebuilds what it needs.
#
# The cache root holds one directory per profile, <namespace>/<release>; the profile's runner pods all run in <namespace>, so a pod listed there on this node is a pod that may be using that profile's cache.
#
# Env:
# - NODE_NAME: the node this pod runs on (the downward API's spec.nodeName). Required.
# - CACHE_ROOT: where the node's cache root is mounted (default: /host-node-cache).
# - LIMIT_GIB: the most gibibytes one profile's cache may hold, a positive integer. Required unless LIMIT_KIB is set.
# - LIMIT_KIB: the limit in kibibytes, which takes the place of LIMIT_GIB, so a test need not write gibibytes.
# - CHECK_SECONDS: seconds between checks (default: 300).
# - PRUNE_ONCE: when "true", check once and exit, for tests.
set -euo pipefail

: "${NODE_NAME:?NODE_NAME is required}"
CACHE_ROOT="${CACHE_ROOT:-/host-node-cache}"
CHECK_SECONDS="${CHECK_SECONDS:-300}"
PRUNE_ONCE="${PRUNE_ONCE:-false}"
LIMIT_KIB="${LIMIT_KIB:-$(( ${LIMIT_GIB:?LIMIT_GIB or LIMIT_KIB is required} * 1024 * 1024 ))}"

# Empties one profile's cache directory when it is over the limit and no runner pod of the profile runs on this node.
prune() {
  local directory=$1 namespace=$2 release=$3 size_kib pods
  size_kib="$(du -sk "$directory" | cut -f1)"
  [ "$size_kib" -le "$LIMIT_KIB" ] && return 0
  pods="$(kubectl get pods -n "$namespace" --field-selector "spec.nodeName=${NODE_NAME}" -o name)"
  if [ -n "$pods" ]; then
    echo "node cache ${namespace}/${release} holds ${size_kib} KiB, over the limit of ${LIMIT_KIB} KiB, but a runner pod is running on this node: left alone"
    return 0
  fi
  find "$directory" -mindepth 1 -delete
  echo "node cache ${namespace}/${release} held ${size_kib} KiB, over the limit of ${LIMIT_KIB} KiB: emptied"
}

check() {
  local directory namespace release
  for directory in "$CACHE_ROOT"/*/*/; do
    [ -d "$directory" ] || continue
    directory="${directory%/}"
    release="$(basename "$directory")"
    namespace="$(basename "$(dirname "$directory")")"
    prune "$directory" "$namespace" "$release"
  done
}

while true; do
  check
  [ "$PRUNE_ONCE" = "true" ] && exit 0
  sleep "$CHECK_SECONDS"
done
