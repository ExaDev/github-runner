#!/usr/bin/env bash
# The role controller (see roles/github_runner_arc/templates/fleet-role.yaml.j2 and fleet-role/loop.sh, which runs a pass of this on an interval). It decides which of the hosts the owner marks eligible are fit to hold an etcd vote, from what scripts/fleet-role-probe.sh publishes, and carries out a voter swap: an eligible standby becomes a voter and an unfit voter is removed and becomes an agent, as one swap under the regrade Lease. The decision is scripts/fleet-role.jq, the swap's state machine is scripts/fleet-role-swap.jq (which reads what step a swap is at from the Nodes, the actuators' reports and the Lease), and this script gathers their inputs, carries out the one action a pass takes, and publishes the result to its status ConfigMap, which the heartbeat republishes. The host-side work of a swap (the units, the role file, the datastore) is the role actuator's (roles/github_runner_cluster/files/role/fleet-role-actuator.sh), driven through the Node's desired-role label and swap annotations; everything on the API (the snapshot request, the cordon, the runner wait, the drain, the taint, the regrading level label, the removal of the member, the clean-up of the Node) is done here.
#
# With no arguments it runs one pass: it renews the regrade Lease if a swap holds it, takes at most one action and publishes the status. `fleet-role.sh swap OUT IN` asks for OUT, a voter, to be swapped for IN, an eligible standby, as a manual request that the next pass carries out; its exit status is 0 when the request is accepted (every gate holds now), 1 when it is refused (each reason is printed) and 2 for a usage error or a node that is unknown, not marked eligible or not the voter and the standby they are taken for. `fleet-role.sh status` prints the swap in progress and its step, the last swap and a pending request, from the published status. Run it as `kubectl -n github-runner-platform exec deploy/fleet-role -- /app/scripts/fleet-role.sh swap OUT IN`.
#
# A manual request honours every gate (the voters at full, Ready, etcd with a leader and no learner, the regrade Lease free) but not the fitness verdict or the cooldown, and never takes out a pinned voter or brings in a host that is not marked eligible. With FLEET_ROLE_DRY_RUN=true only a manual request is carried out and automatic decisions are reported; false carries out automatic decisions as well. A swap already in progress is driven to its end, or rolled back, whatever the setting, so an even count of voters is never left behind. The one thing a failed pass leaves is a swap at the step it was at: every action is repeated safely by the next pass.
#
# Environment (provided by the Deployment, see the template):
# - FLEET_ROLE_DRY_RUN: true (the default) or false, as above.
# - FLEET_ROLE_VOTERS: the number of voters the cluster is held at. Odd, at least 3, and at most FLEET_ROLE_MAX_SERVERS minus one, which leaves a swap one place of headroom (the new voter joins before the old one leaves). Required.
# - FLEET_ROLE_MAX_SERVERS: the most servers the cluster may have (github_runner_cluster_max_servers). Required.
# - FLEET_ROLE_ELECTION_TIMEOUT_MS: the cluster's etcd election timeout in milliseconds, k3s's own default of 1000 where the cluster configures none (the role works that out). Required.
# - FLEET_ROLE_SAMPLE_SECONDS: the probe's sample period. Required; the window the figures are read over is this times the samples a window holds.
# - FLEET_ROLE_PRESSURE_THRESHOLD, FLEET_ROLE_AVAILABLE_FLOOR_PERCENT: the regrade core's limit (github_runner_cluster_regrade_pressure_threshold and github_runner_cluster_regrade_available_floor_percent). Required.
# - FLEET_ROLE_ETCD_QUOTA_BYTES: etcd's storage quota, used for a host's memory floor when no voter reports one; etcd's own default when unset.
# - FLEET_ROLE_ELIGIBLE_LABEL, FLEET_ROLE_PINNED_LABEL: the Node labels, with the value true, that mark a host the controller may promote or demote and one it must never demote. Required.
# - FLEET_ROLE_LEVEL_LABEL: the label regrading puts on a server below full, with the level as its value. Required.
# - FLEET_ROLE_OPERATION_TIMEOUT_SECONDS: how long a drain, the wait for a snapshot and each step of an actuator may take (github_runner_cluster_regrade_operation_timeout_seconds, which the actuators bound their own steps by). Required.
# - FLEET_ROLE_RUNNER_WAIT_SECONDS: how long a planned change waits for the runner pods on a node to finish before it is abandoned or goes on (github_runner_cluster_native_runner_wait_timeout, which the role's own switch waits). Required.
# - FLEET_ROLE_LEASE_SECONDS: how long the regrade Lease lasts if the controller stops renewing it (github_runner_cluster_regrade_lease_duration_seconds). Required.
# - FLEET_ROLE_STATE_NAMESPACE, FLEET_ROLE_PROBE_CONFIGMAP, FLEET_ROLE_STATUS_CONFIGMAP, FLEET_ROLE_ACTUATOR_CONFIGMAP: where the probes publish, this controller keeps its status and the actuators report; the first three are created by the role, the actuators' by the cluster role. FLEET_ROLE_ACTUATOR_CONFIGMAP is required.
# - FLEET_REGRADE_CORE: the regrade core (default /app/fleet-regrade-core.sh), whose Lease functions this uses.
set -euo pipefail

# etcd's default storage quota, 2 GiB, in bytes (--quota-backend-bytes).
readonly etcd_default_quota_bytes=2147483648
# etcd needs a majority, so fewer than three voters tolerate no failure.
readonly min_voters=3
# The label ARC puts on every ephemeral runner pod (plugins/filter/cluster_guard.py's RUNNER_POD_LABEL).
readonly runner_pod_selector=actions-ephemeral-runner=True
# The taint the regrade core puts on a server it has stepped down (github_runner_cluster_regrade_taint), applied to the outgoing voter first so nothing new lands on it.
readonly control_plane_taint=node-role.kubernetes.io/control-plane=true:NoSchedule
# The ConfigMap key a manual request is kept under. The controller's pass owns status.json and the request command owns this key, so the two never overwrite one another.
readonly request_key=request.json

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
operation_timeout="${FLEET_ROLE_OPERATION_TIMEOUT_SECONDS:?FLEET_ROLE_OPERATION_TIMEOUT_SECONDS must be set}"
runner_wait="${FLEET_ROLE_RUNNER_WAIT_SECONDS:?FLEET_ROLE_RUNNER_WAIT_SECONDS must be set}"
lease_seconds="${FLEET_ROLE_LEASE_SECONDS:?FLEET_ROLE_LEASE_SECONDS must be set}"
namespace="${FLEET_ROLE_STATE_NAMESPACE:-github-runner-platform}"
probe_configmap="${FLEET_ROLE_PROBE_CONFIGMAP:-fleet-role-probe}"
status_configmap="${FLEET_ROLE_STATUS_CONFIGMAP:-fleet-role-status}"
actuator_configmap="${FLEET_ROLE_ACTUATOR_CONFIGMAP:?FLEET_ROLE_ACTUATOR_CONFIGMAP must be set}"
modules="$(dirname "$0")"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
fail() { echo "$*" >&2; exit 1; }

[ "$dry_run" = true ] || [ "$dry_run" = false ] || fail "FLEET_ROLE_DRY_RUN must be true or false, not '${dry_run}'"
for setting in target max_servers election_timeout_ms sample_seconds default_quota_bytes operation_timeout runner_wait lease_seconds; do
  [[ "${!setting}" =~ ^[1-9][0-9]*$ ]] || fail "${setting} must be a whole number, at least 1, not '${!setting}'"
done
for setting in pressure_threshold available_floor_percent; do
  [[ "${!setting}" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "${setting} must be a number, not '${!setting}'"
done
[ $((target % 2)) -eq 1 ] || fail "FLEET_ROLE_VOTERS must be odd, not ${target}: an even count tolerates no more failures than the odd count below it"
[ "$target" -ge "$min_voters" ] || fail "FLEET_ROLE_VOTERS must be at least ${min_voters}, not ${target}"
[ "$target" -le $((max_servers - 1)) ] || fail "FLEET_ROLE_VOTERS (${target}) must leave a swap one place of headroom below FLEET_ROLE_MAX_SERVERS (${max_servers})"

# The regrade core's Lease functions, run as the controller: its kubectl_ is the pod's own kubectl (KUBECONFIG_PATH empty leaves the in-cluster configuration to kubectl), and the holder is named in each call.
export KUBECTL=kubectl KUBECONFIG_PATH='' LEASE_DURATION_SECONDS=$lease_seconds NODE_NAME=fleet-role FLEET_REGRADE_ENV=/nonexistent
# shellcheck source=/dev/null
. "${FLEET_REGRADE_CORE:-/app/fleet-regrade-core.sh}"

# The names the controller and the actuators share, from the one place that defines them.
read -r desired_role_label swap_annotation step_annotation join_node_annotation snapshot_annotation member_gone_annotation lease_swap_id_annotation lease_swap_nodes_annotation \
  etcd_label control_plane_label master_label etcd_annotation_prefix remove_annotation lease_holder < <(jq -nr -L "$modules" \
  'include "fleet-role-swap"; interface | [.desired_role_label, .swap_annotation, .step_annotation, .join_node_annotation, .snapshot_annotation, .member_gone_annotation, .lease_swap_id_annotation, .lease_swap_nodes_annotation, .etcd_label, .control_plane_label, .master_label, .etcd_annotation_prefix, .remove_annotation, .lease_holder] | join(" ")')

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# One kubectl call whose output is not wanted. The actions chain these with `|| return 1`, since errexit is off inside a function that is called as the condition of an `if`.
kube() { kubectl_ "$@" > /dev/null; }

# --- Reading the cluster. The inputs go to jq in files, never as arguments: a probe entry holds a window of samples, the entries together can fill a ConfigMap, and one argument is limited to a fraction of the argument space (128 KiB on Linux), which a fleet's nodes and probes pass. ---

# The ConfigMap's data key $2 as JSON, null when the ConfigMap or the key has none.
read_status_key() {
  kubectl_ get configmap "$status_configmap" --namespace "$namespace" --output json | jq -c --arg key "$1" '(.data[$key] // "") | if . == "" then null else fromjson end'
}

# The regrade Lease as the API holds it, or null when there is none; fails for any other reason it cannot be read.
read_lease() {
  local output
  if output=$(kubectl_ get lease "$LEASE_NAME" --namespace "$LEASE_NAMESPACE" --output json 2>&1); then
    echo "$output"
  else
    case $output in
      *"(NotFound)"*) echo null ;;
      *) echo "the regrade Lease cannot be read: ${output}" >&2; return 1 ;;
    esac
  fi
}

# The Lease as the swap program reads it: {state, holder, swap_id, swap_nodes}, state as lease_state says for the controller.
lease_view() {
  local lease=$1 state=free
  if [ "$lease" != null ]; then state=$(lease_state "$lease" "$lease_holder" "$now"); fi
  jq -c --arg state "$state" --arg id_key "$lease_swap_id_annotation" --arg nodes_key "$lease_swap_nodes_annotation" \
    '{state: $state, holder: (.spec.holderIdentity // ""), swap_id: (.metadata.annotations[$id_key] // ""), swap_nodes: (.metadata.annotations[$nodes_key] // "")}' <<< "$lease"
}

# The settings the programs read, as one JSON object.
make_config() {
  jq -nc \
    --argjson now "$now" --argjson target "$target" --argjson max_servers "$max_servers" \
    --argjson election_timeout_ms "$election_timeout_ms" --argjson sample_seconds "$sample_seconds" \
    --argjson pressure_threshold "$pressure_threshold" --argjson available_floor_percent "$available_floor_percent" \
    --argjson default_quota_bytes "$default_quota_bytes" --argjson operation_timeout "$operation_timeout" --argjson runner_wait "$runner_wait" \
    --argjson dry_run "$dry_run" --arg holder "$lease_holder" \
    --arg eligible_label "$eligible_label" --arg pinned_label "$pinned_label" --arg level_label "$level_label" \
    '{now: $now, target: $target, max_servers: $max_servers, election_timeout_ms: $election_timeout_ms, sample_seconds: $sample_seconds,
      pressure_threshold: $pressure_threshold, available_floor_percent: $available_floor_percent, default_quota_bytes: $default_quota_bytes,
      operation_timeout: $operation_timeout, runner_wait: $runner_wait, dry_run: $dry_run, holder: $holder,
      voter_label: "node-role.kubernetes.io/etcd", eligible_label: $eligible_label, pinned_label: $pinned_label, level_label: $level_label}'
}

# Runs a program of the swap module over the state object in the file $1.
swap_program() {
  jq -c -n -L "$modules" --slurpfile s "$1" "include \"fleet-role-swap\"; $2"
}

# Reads everything a pass or a request needs into $work: the Nodes, the probes, the status and any request, the Lease, then, with a swap known, what its steps read. Sets now, lease_json, swap_json, config, result and the files the swap program reads, and runs the decision.
read_state() {
  now="$(date +%s)"
  kubectl_ get nodes --output json > "$work/nodes.json"
  # Each node's entry is a JSON document in its own key.
  kubectl_ get configmap "$probe_configmap" --namespace "$namespace" --output json | jq -c '(.data // {}) | map_values(fromjson)' > "$work/probes.json"
  read_status_key status.json > "$work/previous.json"
  read_status_key "$request_key" > "$work/request.json"
  lease_json=$(read_lease)
  lease_view "$lease_json" > "$work/lease.json"
  config=$(make_config)
  jq -c --argjson config "$config" --slurpfile nodes "$work/nodes.json" --slurpfile lease "$work/lease.json" --slurpfile previous "$work/previous.json" \
    '{config: $config, nodes: $nodes[0].items, lease: $lease[0], previous: $previous[0]}' <<< null > "$work/context-state.json"
  # shellcheck disable=SC2016 # The jq program's own variable.
  swap_json=$(swap_program "$work/context-state.json" 'context($s[0])')
  decide
  gather_swap_inputs
}

# The decision program over the Nodes and probes, told how many voters to expect and which voter a swap is taking out.
decide() {
  local expected ignored
  expected=$(jq -r '.expected_voters' <<< "$swap_json")
  ignored=$(jq -c '.ignored_voters' <<< "$swap_json")
  decision_config=$(jq -c --argjson expected "$expected" --argjson ignored "$ignored" '. + {expected_voters: $expected, ignored_voters: $ignored}' <<< "$config")
  result="$(jq -n -L "$modules" --argjson config "$decision_config" --slurpfile nodes_file "$work/nodes.json" --slurpfile probes_file "$work/probes.json" --slurpfile previous_file "$work/previous.json" -f "$modules/fleet-role.jq")"
  echo "$result" > "$work/result.json"
}

# With a swap known: the actuators' reports, the snapshot files, each swap node's kubelet Lease and the runner pods on it, and the state file the plan reads.
gather_swap_inputs() {
  local out in
  out=$(jq -r '.swap.out // ""' <<< "$swap_json")
  in=$(jq -r '.swap.in // ""' <<< "$swap_json")
  echo '{}' > "$work/actuators.json"
  echo null > "$work/snapshots.json"
  echo '{}' > "$work/node-leases.json"
  echo '{}' > "$work/runner-pods.json"
  if [ -n "$in" ]; then
    read_actuators > "$work/actuators.json"
    # A cluster that does not publish snapshot files leaves this null, which the plan says when it waits for a snapshot.
    if ! kubectl_ get etcdsnapshotfiles.k3s.cattle.io --output json | jq -c '.items' > "$work/snapshots.json" 2> /dev/null; then echo null > "$work/snapshots.json"; fi
    local node renewed pods
    for node in "$out" "$in"; do
      renewed=$(kubectl_ get lease "$node" --namespace kube-node-lease --output 'jsonpath={.spec.renewTime}' 2> /dev/null || true)
      jq -c --arg node "$node" --arg renewed "$renewed" '. + {($node): $renewed}' "$work/node-leases.json" > "$work/node-leases.new" && mv "$work/node-leases.new" "$work/node-leases.json"
      pods=$(kubectl_ get pods --all-namespaces --selector "$runner_pod_selector" --field-selector "spec.nodeName=${node},status.phase!=Succeeded,status.phase!=Failed" --output json | jq '.items | length')
      jq -c --arg node "$node" --argjson pods "$pods" '. + {($node): $pods}' "$work/runner-pods.json" > "$work/runner-pods.new" && mv "$work/runner-pods.new" "$work/runner-pods.json"
    done
  fi
}

# The actuators' reports by node, parsed, {} when their ConfigMap does not exist yet.
read_actuators() {
  local output
  if output=$(kubectl_ get configmap "$actuator_configmap" --namespace "$namespace" --output json 2>&1); then
    jq -c '(.data // {}) | map_values(fromjson)' <<< "$output"
  else
    case $output in
      *"(NotFound)"*) echo '{}' ;;
      *) echo "the actuators' reports cannot be read: ${output}" >&2; return 1 ;;
    esac
  fi
}

# The plan: what the swap program names for this pass, over the state read. $1 is a request as JSON, or null for the one pending.
plan_for() {
  local request=${1:-}
  if [ -z "$request" ]; then request=$(cat "$work/request.json"); fi
  jq -c --argjson request "$request" --slurpfile result "$work/result.json" --slurpfile context "$work/context-state.json" \
    --slurpfile actuators "$work/actuators.json" --slurpfile snapshots "$work/snapshots.json" --slurpfile node_leases "$work/node-leases.json" --slurpfile runner_pods "$work/runner-pods.json" \
    '$context[0] + {actuators: $actuators[0], snapshots: $snapshots[0], node_leases: $node_leases[0], runner_pods: $runner_pods[0], decision: $result[0], request: $request}' <<< null > "$work/plan-state.json"
  # shellcheck disable=SC2016 # The jq program's own variable.
  swap_program "$work/plan-state.json" 'next_action($s[0])'
}

# --- Acting. One function per kind of action the plan names; each is repeated safely, so a pass that is cut short is finished by the next. ---

# Takes the Lease for the swap, with the swap's id and nodes on it, where the actuators and node recovery read them.
act_take_lease() {
  local id=$1 out=$2 in=$3 annotations
  annotations=$(jq -nc --arg id_key "$lease_swap_id_annotation" --arg id "$id" --arg nodes_key "$lease_swap_nodes_annotation" --arg nodes "${out},${in}" '{($id_key): $id, ($nodes_key): $nodes}') || return 1
  acquire_lease "$lease_holder" "$annotations"
}

act_request_snapshot() {
  kube annotate --overwrite node "$1" "${snapshot_annotation}=$2"
}

act_cordon() {
  kube cordon "$1"
}

act_uncordon() {
  kube uncordon "$1"
}

# Drains the candidate, then asks its actuator to promote it, joining the voter named: the label, then the annotations, since the actuator acts on the annotations and refuses a label that disagrees.
act_promote_in() {
  local node=$1 id=$2 join_node=$3
  kube drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout="${operation_timeout}s" || return 1
  kube label --overwrite node "$node" "${desired_role_label}=server" || return 1
  kube annotate --overwrite node "$node" "${swap_annotation}=${id}" "${step_annotation}=promote" "${join_node_annotation}=${join_node}"
}

# Asks the actuator of a candidate that did not become a voter to switch the host back, after asking k3s to remove the learner that is still listed (which never touches quorum).
act_begin_rollback() {
  local node=$1 id=$2 remove=$3
  if [ "$remove" = true ]; then kube annotate --overwrite node "$node" "${remove_annotation}=true" || return 1; fi
  kube label --overwrite node "$node" "${desired_role_label}=agent" || return 1
  kube annotate --overwrite node "$node" "${swap_annotation}=${id}" "${step_annotation}=rollback"
}

# Taints and cordons the outgoing voter, so nothing new lands on it while its runner pods finish.
act_cordon_out() {
  kube taint nodes "$1" "$control_plane_taint" --overwrite || return 1
  kube cordon "$1"
}

# Empties the outgoing voter (a node that is down cannot be drained), marks it for regrading's etcd-only level, which its actuator enters, and asks the actuator to step it down.
act_step_down_out() {
  local node=$1 id=$2 drain=$3
  act_cordon_out "$node" || return 1
  if [ "$drain" = true ]; then kube drain "$node" --ignore-daemonsets --delete-emptydir-data --force --timeout="${operation_timeout}s" || return 1; fi
  kube label --overwrite node "$node" "${level_label}=etcd-only" "${desired_role_label}=agent" || return 1
  kube annotate --overwrite node "$node" "${swap_annotation}=${id}" "${step_annotation}=step-down"
}

# Asks k3s's member controller, on another server, to remove the outgoing voter's etcd member.
act_annotate_remove() {
  kube annotate --overwrite node "$1" "${remove_annotation}=true"
}

# Tells the outgoing voter's actuator, under the swap $2, that etcd no longer lists its member, as every remaining voter reports.
act_mark_member_gone() {
  kube annotate --overwrite node "$1" "${member_gone_annotation}=$2"
}

# Removes what k3s and regrading left on a node that is no longer a server (the role labels, the etcd annotations, the level label and the taint), whichever of them it carries, and uncordons it when asked. The desired-role label stays, as the record of the role the host runs, and the swap annotations stay until the swap ends.
act_clean_node() {
  local node=$1 uncordon=$2 json labels annotations taint
  json=$(kubectl_ get node "$node" --output json) || return 1
  labels=$(jq -r --arg etcd "$etcd_label" --arg control_plane "$control_plane_label" --arg master "$master_label" --arg level "$level_label" \
    '(.metadata.labels // {}) as $present | [$etcd, $control_plane, $master, $level][] | select($present[.] != null)' <<< "$json") || return 1
  annotations=$(jq -r --arg prefix "$etcd_annotation_prefix" '(.metadata.annotations // {}) | keys[] | select(startswith($prefix))' <<< "$json") || return 1
  taint=$(jq -r --arg key "${control_plane_taint%%=*}" '[(.spec.taints // [])[] | select(.key == $key)] | length' <<< "$json") || return 1
  local label_args=() annotation_args=() key
  while IFS= read -r key; do if [ -n "$key" ]; then label_args+=("${key}-"); fi; done <<< "$labels"
  while IFS= read -r key; do if [ -n "$key" ]; then annotation_args+=("${key}-"); fi; done <<< "$annotations"
  if [ "${#label_args[@]}" -gt 0 ]; then kube label node "$node" "${label_args[@]}" || return 1; fi
  if [ "${#annotation_args[@]}" -gt 0 ]; then kube annotate node "$node" "${annotation_args[@]}" || return 1; fi
  if [ "$taint" -gt 0 ]; then kube taint nodes "$node" "${control_plane_taint%%=*}:NoSchedule-" || return 1; fi
  if [ "$uncordon" = true ]; then act_uncordon "$node"; fi
}

# Ends the swap $1: the nodes $2 (a JSON array) are uncordoned, the swap annotations come off every node that carries them, and the Lease is given back last, so a pass cut short still holds it and finishes the job. The outcome is recorded by the caller.
act_finalise() {
  local id=$1 uncordon=$2 node nodes
  for node in $(jq -r '.[]' <<< "$uncordon"); do act_uncordon "$node" || return 1; done
  nodes=$(jq -r --arg id "$id" --arg swap "$swap_annotation" --arg snapshot "$snapshot_annotation" \
    '.items[] | select((.metadata.annotations[$swap] == $id) or (.metadata.annotations[$snapshot] == $id)) | .metadata.name' "$work/nodes.json") || return 1
  for node in $nodes; do
    kube annotate node "$node" "${swap_annotation}-" "${step_annotation}-" "${join_node_annotation}-" "${snapshot_annotation}-" "${member_gone_annotation}-" || return 1
  done
  release_lease "$lease_holder"
}

# Carries out the action $1 of the plan $2, and returns its status.
carry_out() {
  local action=$1 plan=$2 kind
  kind=$(jq -r '.kind' <<< "$action")
  case $kind in
    take_lease) act_take_lease "$(jq -r '.id' <<< "$action")" "$(jq -r '.out' <<< "$action")" "$(jq -r '.in' <<< "$action")" ;;
    request_snapshot) act_request_snapshot "$(jq -r '.node' <<< "$action")" "$(jq -r '.id' <<< "$action")" ;;
    cordon) act_cordon "$(jq -r '.node' <<< "$action")" ;;
    uncordon) act_uncordon "$(jq -r '.node' <<< "$action")" ;;
    promote_in) act_promote_in "$(jq -r '.node' <<< "$action")" "$(jq -r '.id' <<< "$action")" "$(jq -r '.join_node' <<< "$action")" ;;
    begin_rollback) act_begin_rollback "$(jq -r '.node' <<< "$action")" "$(jq -r '.id' <<< "$action")" "$(jq -r '.remove' <<< "$action")" ;;
    cordon_out) act_cordon_out "$(jq -r '.node' <<< "$action")" ;;
    step_down_out) act_step_down_out "$(jq -r '.node' <<< "$action")" "$(jq -r '.id' <<< "$action")" "$(jq -r '.drain' <<< "$action")" ;;
    annotate_remove) act_annotate_remove "$(jq -r '.node' <<< "$action")" ;;
    mark_member_gone) act_mark_member_gone "$(jq -r '.node' <<< "$action")" "$(jq -r '.id' <<< "$action")" ;;
    clean_node) act_clean_node "$(jq -r '.node' <<< "$action")" "$(jq -r '.uncordon' <<< "$action")" ;;
    finalise) act_finalise "$(jq -r '.swap.id' <<< "$plan")" "$(jq -c '.uncordon' <<< "$action")" ;;
    *) fail "the plan names an action this controller does not know: ${kind}" ;;
  esac
}

# --- The commands. ---

# One pass.
run_pass() {
  local plan action action_status=0 swap last_swap request had_swap line
  # A swap holds the Lease for as long as it runs and each pass renews it, so it lasts only as long as a controller that is working. A renewal that fails is not fatal: the Lease is read again below, as the state it is really in.
  lease_json=$(read_lease)
  if [ "$lease_json" != null ] && [ "$(jq -r '.spec.holderIdentity // ""' <<< "$lease_json")" = "$lease_holder" ]; then
    renew_lease "$lease_holder" > /dev/null || log "the regrade Lease could not be renewed; it is read again"
  fi
  read_state
  plan=$(plan_for)
  had_swap=$(jq -r '.swap != null' <<< "$swap_json")
  action=$(jq -c '.action' <<< "$plan")
  swap=$(jq -c '.swap' <<< "$plan")
  last_swap=$(jq -c '.last_swap' <<< "$plan")
  request=$(cat "$work/request.json")

  # What each host's verdict rests on, then the decision, in the log.
  while IFS= read -r line; do log "$line"; done < <(jq -r '
    .hosts | to_entries[]
    | "\(.key) (\(.value.role)\(if .value.pinned then ", pinned" else "" end)\(if .value.eligible then "" else ", not eligible" end), link \(.value.link)): \(.value.verdict), \(.value.reasons | if length == 0 then "all readings within their limits" else join("; ") end)\(if (.value.attributed_to | length) > 0 then " (attributed to \(.value.attributed_to | join(" and ")))" else "" end)\(.value.unknown_signals | map("; \(.) unknown") | join(""))"' <<< "$result")
  if [ "$had_swap" = false ]; then
    if [ "$dry_run" = true ]; then
      while IFS= read -r line; do log "$line"; done < <(jq -r '
        if .decision.action == "swap" then "DRY RUN: would swap out \(.decision.swap.out) for \(.decision.swap.in): \(.decision.because | join("; "))"
        else "DRY RUN: would not act: \(.decision.because | join("; "))\(if .decision.wanted == null and (.blocks | length) > 0 then " (also: \(.blocks | join("; ")))" else "" end)" end' <<< "$result")
    else
      while IFS= read -r line; do log "$line"; done < <(jq -r '
        if .decision.action == "swap" then "decision: swap out \(.decision.swap.out) for \(.decision.swap.in): \(.decision.because | join("; "))"
        else "decision: no swap: \(.decision.because | join("; "))\(if .decision.wanted == null and (.blocks | length) > 0 then " (also: \(.blocks | join("; ")))" else "" end)" end' <<< "$result")
    fi
  fi
  while IFS= read -r line; do log "$line"; done < <(jq -r '.wait[] | "swap: \(.)"' <<< "$plan")

  if [ "$action" != null ]; then
    log "swap$(jq -r 'if .swap != null then " \(.swap.id) (\(.swap.step))" else "" end' <<< "$plan"): $(jq -r '.kind + " " + (del(.kind) | to_entries | map("\(.key)=\(.value)") | join(" "))' <<< "$action")"
    if ! carry_out "$action" "$plan"; then
      action_status=1
      log "the action failed; the next pass repeats it"
      # A swap that was not yet under way is not begun by an action that failed.
      if [ "$had_swap" = false ]; then swap=null; fi
      last_swap=null
    elif [ "$(jq -r '.kind' <<< "$action")" = finalise ]; then
      swap=null
      log "swap ended: $(jq -c '.last_swap' <<< "$plan")"
    fi
  fi
  if [ "$(jq -r '.consume_request' <<< "$plan")" = true ] && [ "$action_status" -eq 0 ]; then
    jq -n --arg key "$request_key" '{data: {($key): null}}' > "$work/patch.json"
    kubectl_ patch configmap "$status_configmap" --namespace "$namespace" --type merge --patch-file "$work/patch.json" > /dev/null
    request=null
  fi

  publish "$swap" "$last_swap" "$request" "$(jq -c '.wait' <<< "$plan")"
  return "$action_status"
}

# Publishes the status: the decision program's result, with the swap in progress and what it waits for, the last swap that ended (the one before it while none has since) and the pending request. The patch goes in a file: it is read back as a whole, and an argument has a size limit.
publish() {
  local swap=$1 last_swap=$2 request=$3 waiting=$4 status
  status="$(jq -c --arg last_run "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson dry_run "$dry_run" --argjson swap "$swap" --argjson last_swap "$last_swap" --argjson request "$request" --argjson waiting "$waiting" --slurpfile previous "$work/previous.json" \
    '. + {last_run: $last_run, dry_run: $dry_run, swap: $swap, waiting: $waiting, last_swap: ($last_swap // $previous[0].last_swap // null), request: $request}' <<< "$result")"
  jq -n --arg status "$status" '{data: {"status.json": $status}}' > "$work/status-patch.json"
  kubectl_ patch configmap "$status_configmap" --namespace "$namespace" --type merge --patch-file "$work/status-patch.json" > /dev/null
}

# Asks for the swap of $1 for $2.
request_swap() {
  local out=$1 in=$2 plan check status
  read_state
  plan=$(plan_for "$(jq -nc --arg out "$out" --arg in "$in" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '{out: $out, in: $in, at: $at}')")
  check=$(jq -c '.check' <<< "$plan")
  status=$(jq -r '.status' <<< "$check")
  if [ "$status" -eq 0 ]; then
    jq -n --arg key "$request_key" --arg request "$(jq -nc --arg out "$out" --arg in "$in" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '{out: $out, in: $in, at: $at}')" '{data: {($key): $request}}' > "$work/patch.json"
    kubectl_ patch configmap "$status_configmap" --namespace "$namespace" --type merge --patch-file "$work/patch.json" > /dev/null
    echo "accepted: ${out} will be swapped out for ${in} by the next pass"
    return 0
  fi
  echo "refused:" >&2
  jq -r '.reasons[] | "  \(.)"' <<< "$check" >&2
  return "$status"
}

# Prints the swap in progress and its step, the last swap and the request pending.
print_status() {
  local status
  status=$(read_status_key status.json)
  if [ "$status" = null ]; then echo "no status has been published yet"; return 0; fi
  jq -r '
    (if .swap == null then "no swap in progress"
     else "swap \(.swap.id): \(.swap.out) out, \(.swap.in) in, step \(.swap.step) since \(.swap.step_since | todate), deadline \(.swap.deadline | todate)\(if .swap.manual then ", by request" else "" end)\(if .swap.outgoing_pending then ", the outgoing host has not come back as an agent" else "" end)" end),
    (if .last_swap == null then "no swap has ended yet" else "last swap: \(.last_swap.outcome) (\(.last_swap.reasons | join("; ")))" end),
    (if .request == null then "no request pending" else "request pending: \(.request.out) out, \(.request.in) in" end),
    "dry run: \(.dry_run)"' <<< "$status"
}

case ${1:-} in
  "") [ $# -eq 0 ] || fail "usage: fleet-role.sh [swap OUT IN | status]"; run_pass ;;
  swap)
    [ $# -eq 3 ] || { echo "usage: fleet-role.sh swap OUT IN" >&2; exit 2; }
    request_swap "$2" "$3"
    ;;
  status)
    [ $# -eq 1 ] || { echo "usage: fleet-role.sh status" >&2; exit 2; }
    print_status
    ;;
  *) echo "usage: fleet-role.sh [swap OUT IN | status]" >&2; exit 2 ;;
esac
