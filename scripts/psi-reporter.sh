#!/usr/bin/env bash
# Publishes this node's own pressure stall information for the autoscaler. Runs in the `psi-reporter` DaemonSet, one pod per node a runner pod may use, with the host's /proc/pressure mounted read-only.
#
# The reading comes from the node's own /proc/pressure, not from the kubelet's summary API: on a cgroup v1 node that API reports every pressure field as 0 whatever the node is doing, so a reader built on it would find no pressure on exactly the nodes that have some.
#
# Env:
# - NODE_NAME: the node this pod runs on (the downward API's spec.nodeName). Required.
# - NAMESPACE: the namespace of the ConfigMap. Required.
# - NODE_PRESSURE_CONFIGMAP: the shared ConfigMap, pre-created empty, that every reporter patches its own key of (default: autoscaler-node-pressure).
# - PRESSURE_DIR: where the host's /proc/pressure is mounted (default: /host-pressure).
# - REPORT_SECONDS: seconds between reports (default: 15). The autoscaler treats a report older than a few polls as no report.
# - REPORT_ONCE: when "true", report once and exit, for tests.
#
# The value stored under the node's name is a JSON object: at (epoch seconds), and for each of io, cpu and memory the avg10 of its "some" and, where the kernel reports it, "full" lines, as <resource>_<kind>_avg10. A line the kernel does not report (the root cpu "full" before kernel 5.13) is left out, not reported as 0.
set -euo pipefail

: "${NODE_NAME:?NODE_NAME is required}"
: "${NAMESPACE:?NAMESPACE is required}"
NODE_PRESSURE_CONFIGMAP="${NODE_PRESSURE_CONFIGMAP:-autoscaler-node-pressure}"
PRESSURE_DIR="${PRESSURE_DIR:-/host-pressure}"
REPORT_SECONDS="${REPORT_SECONDS:-15}"
REPORT_ONCE="${REPORT_ONCE:-false}"

# The avg10 figures of one resource's file as `<resource>_<kind>_avg10: <number>` JSON members, one per line the file has.
members() {
  local resource=$1 kind avg10
  [ -r "${PRESSURE_DIR}/${resource}" ] || return 0
  while read -r kind rest; do
    avg10="$(sed -n 's/.*avg10=\([0-9.]*\).*/\1/p' <<< "$rest")"
    [ -n "$avg10" ] && printf '"%s_%s_avg10": %s\n' "$resource" "$kind" "$avg10"
  done < "${PRESSURE_DIR}/${resource}"
}

report() {
  local entry patch
  entry="$( { printf '"at": %s\n' "$(date +%s)"; members io; members cpu; members memory; } | paste -sd, - )"
  patch="$(mktemp)"
  # The node's name is the key and the entry a JSON string, so one reporter's patch never touches another's key. The patch goes in a file: it is read back as a whole, and an argument has a size limit.
  jq -n --arg node "$NODE_NAME" --arg entry "{${entry}}" '{data: {($node): $entry}}' > "$patch"
  kubectl patch configmap "$NODE_PRESSURE_CONFIGMAP" -n "$NAMESPACE" --type merge --patch-file "$patch"
  rm -f "$patch"
}

while true; do
  report
  [ "$REPORT_ONCE" = "true" ] && exit 0
  sleep "$REPORT_SECONDS"
done
