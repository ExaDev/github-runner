#!/usr/bin/env bash
# One pass of the role controller (see roles/github_runner_arc/templates/fleet-role.yaml.j2 and fleet-role/loop.sh, which runs this on an interval). It decides which of the hosts the owner marks eligible are fit to hold an etcd vote, from what scripts/fleet-role-probe.sh publishes, and which swap of an unfit voter for a fit standby it would make. It changes nothing in the cluster: its only write is its own status ConfigMap, which the heartbeat republishes. The decision is scripts/fleet-role.jq; this script gathers its inputs and publishes the result.
#
# It runs unchanged with no host marked eligible, and then only reports the voters' fitness.
#
# Environment (provided by the Deployment, see the template):
# - FLEET_ROLE_DRY_RUN: must be "true", the default. The controller has no way to change a role; the setting exists so that a Deployment that names it keeps working when it can.
# - FLEET_ROLE_VOTERS: the number of voters the cluster is held at. Odd, at least 3, and at most FLEET_ROLE_MAX_SERVERS minus one, which leaves a swap one place of headroom (the new voter joins before the old one leaves). Required.
# - FLEET_ROLE_MAX_SERVERS: the most servers the cluster may have (github_runner_cluster_max_servers). Required.
# - FLEET_ROLE_ELECTION_TIMEOUT_MS: the cluster's etcd election timeout in milliseconds, k3s's own default of 1000 where the cluster configures none (the role works that out). Required.
# - FLEET_ROLE_SAMPLE_SECONDS: the probe's sample period. Required; the window the figures are read over is this times the samples a window holds.
# - FLEET_ROLE_PRESSURE_THRESHOLD, FLEET_ROLE_AVAILABLE_FLOOR_PERCENT: the regrade core's limit (github_runner_cluster_regrade_pressure_threshold and github_runner_cluster_regrade_available_floor_percent). Required.
# - FLEET_ROLE_ETCD_QUOTA_BYTES: etcd's storage quota, used for a host's memory floor when no voter reports one; etcd's own default when unset.
# - FLEET_ROLE_ELIGIBLE_LABEL, FLEET_ROLE_PINNED_LABEL: the Node labels, with the value true, that mark a host the controller may promote or demote and one it must never demote. Required.
# - FLEET_ROLE_LEVEL_LABEL: the label regrading puts on a server below full, with the level as its value. Required.
# - FLEET_ROLE_STATE_NAMESPACE, FLEET_ROLE_PROBE_CONFIGMAP, FLEET_ROLE_STATUS_CONFIGMAP: where the probes publish and this controller keeps its status; both ConfigMaps are created by the role.
set -euo pipefail

# etcd's default storage quota, 2 GiB, in bytes (--quota-backend-bytes).
readonly etcd_default_quota_bytes=2147483648
# etcd needs a majority, so fewer than three voters tolerate no failure.
readonly min_voters=3

dry_run="${FLEET_ROLE_DRY_RUN:-true}"
target="${FLEET_ROLE_VOTERS:?FLEET_ROLE_VOTERS must be set}"
max_servers="${FLEET_ROLE_MAX_SERVERS:?FLEET_ROLE_MAX_SERVERS must be set}"
election_timeout_ms="${FLEET_ROLE_ELECTION_TIMEOUT_MS:?FLEET_ROLE_ELECTION_TIMEOUT_MS must be set}"
sample_seconds="${FLEET_ROLE_SAMPLE_SECONDS:?FLEET_ROLE_SAMPLE_SECONDS must be set}"
pressure_threshold="${FLEET_ROLE_PRESSURE_THRESHOLD:?FLEET_ROLE_PRESSURE_THRESHOLD must be set}"
available_floor_percent="${FLEET_ROLE_AVAILABLE_FLOOR_PERCENT:?FLEET_ROLE_AVAILABLE_FLOOR_PERCENT must be set}"
default_quota_bytes="${FLEET_ROLE_ETCD_QUOTA_BYTES:-$etcd_default_quota_bytes}"
eligible_label="${FLEET_ROLE_ELIGIBLE_LABEL:?FLEET_ROLE_ELIGIBLE_LABEL must be set}"
pinned_label="${FLEET_ROLE_PINNED_LABEL:?FLEET_ROLE_PINNED_LABEL must be set}"
level_label="${FLEET_ROLE_LEVEL_LABEL:?FLEET_ROLE_LEVEL_LABEL must be set}"
namespace="${FLEET_ROLE_STATE_NAMESPACE:-github-runner-platform}"
probe_configmap="${FLEET_ROLE_PROBE_CONFIGMAP:-fleet-role-probe}"
status_configmap="${FLEET_ROLE_STATUS_CONFIGMAP:-fleet-role-status}"
modules="$(dirname "$0")"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
fail() { echo "$*" >&2; exit 1; }

[ "$dry_run" = true ] || fail "FLEET_ROLE_DRY_RUN must be true: this controller reports the swap it would make and has no way to make one (got '${dry_run}')"
for setting in target max_servers election_timeout_ms sample_seconds default_quota_bytes; do
  [[ "${!setting}" =~ ^[1-9][0-9]*$ ]] || fail "${setting} must be a whole number, at least 1, not '${!setting}'"
done
for setting in pressure_threshold available_floor_percent; do
  [[ "${!setting}" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "${setting} must be a number, not '${!setting}'"
done
[ $((target % 2)) -eq 1 ] || fail "FLEET_ROLE_VOTERS must be odd, not ${target}: an even count tolerates no more failures than the odd count below it"
[ "$target" -ge "$min_voters" ] || fail "FLEET_ROLE_VOTERS must be at least ${min_voters}, not ${target}"
[ "$target" -le $((max_servers - 1)) ] || fail "FLEET_ROLE_VOTERS (${target}) must leave a swap one place of headroom below FLEET_ROLE_MAX_SERVERS (${max_servers})"

now="$(date +%s)"
# The inputs go to jq in files, never as arguments: a probe entry holds a window of samples, the entries together can fill a ConfigMap, and one argument is limited to a fraction of the argument space (128 KiB on Linux), which a fleet's nodes and probes pass.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
kubectl get nodes -o json > "$work/nodes.json"
# Each node's entry is a JSON document in its own key.
kubectl get configmap "$probe_configmap" -n "$namespace" -o json | jq -c '(.data // {}) | map_values(fromjson)' > "$work/probes.json"
kubectl get configmap "$status_configmap" -n "$namespace" -o json | jq -c '(.data["status.json"] // "") | if . == "" then null else fromjson end' > "$work/previous.json"

config="$(jq -nc \
  --argjson now "$now" --argjson target "$target" --argjson max_servers "$max_servers" \
  --argjson election_timeout_ms "$election_timeout_ms" --argjson sample_seconds "$sample_seconds" \
  --argjson pressure_threshold "$pressure_threshold" --argjson available_floor_percent "$available_floor_percent" \
  --argjson default_quota_bytes "$default_quota_bytes" \
  --arg eligible_label "$eligible_label" --arg pinned_label "$pinned_label" --arg level_label "$level_label" \
  '{now: $now, target: $target, max_servers: $max_servers, election_timeout_ms: $election_timeout_ms, sample_seconds: $sample_seconds,
    pressure_threshold: $pressure_threshold, available_floor_percent: $available_floor_percent, default_quota_bytes: $default_quota_bytes,
    voter_label: "node-role.kubernetes.io/etcd", eligible_label: $eligible_label, pinned_label: $pinned_label, level_label: $level_label}')"

result="$(jq -n -L "$modules" --argjson config "$config" --slurpfile nodes_file "$work/nodes.json" --slurpfile probes_file "$work/probes.json" --slurpfile previous_file "$work/previous.json" -f "$modules/fleet-role.jq")"
status="$(jq -c --arg last_run "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson dry_run true '. + {last_run: $last_run, dry_run: $dry_run}' <<< "$result")"

# The patch goes in a file: it is read back as a whole, and an argument has a size limit.
jq -n --arg status "$status" '{data: {"status.json": $status}}' > "$work/patch.json"
kubectl patch configmap "$status_configmap" -n "$namespace" --type merge --patch-file "$work/patch.json" > /dev/null

# What each host's verdict rests on, then the decision, in the log.
while IFS= read -r line; do log "$line"; done < <(jq -r '
  .hosts | to_entries[]
  | "\(.key) (\(.value.role)\(if .value.pinned then ", pinned" else "" end)\(if .value.eligible then "" else ", not eligible" end)): \(.value.verdict), \(.value.reasons | if length == 0 then "all readings within their limits" else join("; ") end)"' <<< "$result")
while IFS= read -r line; do log "$line"; done < <(jq -r '
  if .decision.action == "swap" then "DRY RUN: would swap out \(.decision.swap.out) for \(.decision.swap.in): \(.decision.because | join("; "))"
  else "DRY RUN: would not act: \(.decision.because | join("; "))\(if .decision.wanted == null and (.blocks | length) > 0 then " (also: \(.blocks | join("; ")))" else "" end)" end' <<< "$result")
