#!/usr/bin/env bash
# Usage-driven maxRunners autoscaler. Runs inside the `autoscaler` in-cluster Deployment (see autoscaler/loop.sh for the poll loop). Never inspects job identity or the GitHub Actions queue: it only watches real, current memory usage and pressure, and adjusts spec.maxRunners across every AutoscalingRunnerSet named in AUTOSCALER_TARGETS, pooling them against one combined memory budget rather than budgeting each separately. See the project README's Autoscaler section for the full algorithm and rationale.
#
# Environment (provided by the Deployment - see roles/github_runner_arc/tasks/install_platform.yml):
# - kubectl needs no KUBECONFIG here: running as a pod with a mounted ServiceAccount token, client-go auto-detects in-cluster config.
# - AUTOSCALER_DRY_RUN: "true" computes and logs only, never patches
# - AUTOSCALER_USABLE_BUDGET_GI: proven-safe memory budget for every pooled target's runner pods combined, across every node any of them can use
# - AUTOSCALER_MAX_CEILING: hard operator cap on the pooled maxRunners total, independent of the live arithmetic
# - AUTOSCALER_FLOOR: the combined floor every poll reverts the pooled total to under pressure; each Helm upgrade separately reverts its own target's maxRunners to that target's own values-file floor, which need not equal this combined figure divided evenly
# - AUTOSCALER_RAISE_CONFIRM_POLLS: consecutive polls of confirmed headroom before raising
# - AUTOSCALER_MEM_AVAILABLE_PRESSURE_PCT: MemAvailable-of-MemTotal percentage treated as real pressure, evaluated as the worst case across all nodes (see the kubectl top nodes block below) - no swap-pressure equivalent, see that block's own comment for why
# - AUTOSCALER_TARGETS: the AutoscalingRunnerSets to pool, as whitespace-separated namespace/release tokens (one per scale-set profile with autoscale: true; see plugins/filter/arc.py's own target field, which is exactly this namespace/release form)
# - AUTOSCALER_STATE_NAMESPACE, AUTOSCALER_STATUS_CONFIGMAP: the ConfigMap this pod's own status/raise-confirm-count state lives in (shared with scripts/heartbeat.sh, which reads status.json back out to republish in the heartbeat gist)
set -euo pipefail

# The targets and the pool-specific figures have no defaults: they depend on the cluster, and the role always sets them.
TARGETS_RAW="${AUTOSCALER_TARGETS:?AUTOSCALER_TARGETS must be set}"
DRY_RUN="${AUTOSCALER_DRY_RUN:-true}"
USABLE_BUDGET_GI="${AUTOSCALER_USABLE_BUDGET_GI:?AUTOSCALER_USABLE_BUDGET_GI must be set}"
MAX_CEILING="${AUTOSCALER_MAX_CEILING:?AUTOSCALER_MAX_CEILING must be set}"
FLOOR="${AUTOSCALER_FLOOR:?AUTOSCALER_FLOOR must be set}"
RAISE_CONFIRM_POLLS="${AUTOSCALER_RAISE_CONFIRM_POLLS:-2}"
MEM_AVAILABLE_PRESSURE_PCT="${AUTOSCALER_MEM_AVAILABLE_PRESSURE_PCT:-15}"
STATE_NAMESPACE="${AUTOSCALER_STATE_NAMESPACE:-github-runner-platform}"
CONFIGMAP="${AUTOSCALER_STATUS_CONFIGMAP:-autoscaler-status}"

# Splits on whitespace: one token or several, however the Deployment env's own spacing renders.
read -ra TARGETS <<< "$TARGETS_RAW"
[ "${#TARGETS[@]}" -gt 0 ] || { echo "AUTOSCALER_TARGETS named no targets" >&2; exit 1; }
NAMESPACES=()
RELEASES=()
for target in "${TARGETS[@]}"; do
  NAMESPACES+=("${target%%/*}")
  RELEASES+=("${target#*/}")
done

# Kubernetes memory quantities as reported by `kubectl get -o jsonpath` and `kubectl top pod` use binary suffixes (Ki/Mi/Gi). Convert to whole MiB so every comparison below is plain integer arithmetic — bash has no floats, and shelling out to `bc`/`python3` for this is unnecessary weight in a tiny Alpine image.
to_mib() {
  local qty="$1"
  case "$qty" in
    *Gi) echo $(( ${qty%Gi} * 1024 )) ;;
    *Mi) echo "${qty%Mi}" ;;
    *Ki) echo $(( ${qty%Ki} / 1024 )) ;;
    *)   echo "unexpected memory quantity format: '$qty'" >&2; return 1 ;;
  esac
}

# Reads and writes go through this ConfigMap (pre-created empty by install_platform.yml, so the autoscaler's own RBAC only ever needs get/patch, never create) rather than a shared bind-mounted directory - the two containers can now land on different nodes, so there's no shared filesystem to write to.
configmap_get() {
  kubectl get configmap "$CONFIGMAP" -n "$STATE_NAMESPACE" -o jsonpath="{.data.$1}" 2>/dev/null || true
}

configmap_set() {
  kubectl patch configmap "$CONFIGMAP" -n "$STATE_NAMESPACE" --type merge \
    -p "$(jq -n --arg k "$1" --arg v "$2" '{data: {($k): $v}}')"
}

# targets_json: a compact JSON array, one object per target, built from the parallel NAMESPACES/RELEASES/MAX arrays - so the status ConfigMap (and the heartbeat gist it feeds) shows each pooled target's own current maxRunners, not just the combined total.
targets_json() {
  local i out="[]"
  for ((i = 0; i < ${#TARGETS[@]}; i++)); do
    out="$(jq -c --argjson acc "$out" --arg namespace "${NAMESPACES[$i]}" --arg release "${RELEASES[$i]}" --argjson max_runners "${MAX[$i]}" --argjson current_runners "${R[$i]}" \
      '$acc + [{namespace: $namespace, release: $release, max_runners: $max_runners, current_runners: $current_runners}]' <<< null)"
  done
  echo "$out"
}

write_status() {
  local current_max_total="$1" mode="$2" headroom_mib="${3:-null}" headroom_gi="null"
  if [ "$headroom_mib" != "null" ]; then
    headroom_gi="$(awk -v m="$headroom_mib" 'BEGIN{printf "%.1f", m/1024}')"
  fi
  local status_json
  status_json="$(jq -n \
    --arg last_run "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --argjson current_max_runners "$current_max_total" \
    --arg headroom_gi "$headroom_gi" \
    --arg mode "$mode" \
    --argjson targets "$(targets_json)" \
    '{last_run: $last_run, current_max_runners: $current_max_runners, headroom_gi: ($headroom_gi | tonumber? // null), mode: $mode, targets: $targets}')"
  configmap_set "status.json" "$status_json"
}

# Patches every target whose NEW_MAX differs from its live MAX (set by the caller before calling this), skipping the rest: an unchanged target needs no API call. Updates MAX in place afterwards so targets_json (via write_status) reports what was actually applied.
apply_new_max() {
  local reason="$1" headroom_mib="${2:-null}" i total=0
  for ((i = 0; i < ${#TARGETS[@]}; i++)); do
    total=$(( total + NEW_MAX[i] ))
    [ "${NEW_MAX[$i]}" -eq "${MAX[$i]}" ] && continue
    if [ "$DRY_RUN" = "true" ]; then
      echo "DRY RUN: would patch ${TARGETS[$i]}'s maxRunners to ${NEW_MAX[$i]} (${reason})"
    else
      kubectl patch autoscalingrunnerset "${RELEASES[$i]}" -n "${NAMESPACES[$i]}" \
        --type merge -p "{\"spec\":{\"maxRunners\": ${NEW_MAX[$i]}}}"
      echo "Patched ${TARGETS[$i]}'s maxRunners to ${NEW_MAX[$i]} (${reason})"
    fi
    MAX[i]=${NEW_MAX[$i]}
  done
  write_status "$total" "$reason" "$headroom_mib"
}

# Lowers the combined total toward $1 by taking slack (a target's own MAX minus its own R) from whichever pooled target currently has the most of it, one runner at a time, so the target closest to actually using its allotment keeps the most of what it has. Never takes a target below its own R: a currently-running job must never be orphaned by its own scale set shrinking under it, so the combined result can stay above $1 when demand alone already exceeds it.
lower_to() {
  local combined_target=$1 i combined=0
  for ((i = 0; i < ${#TARGETS[@]}; i++)); do
    NEW_MAX[i]=${MAX[$i]}
    combined=$(( combined + NEW_MAX[i] ))
  done
  while [ "$combined" -gt "$combined_target" ]; do
    local best=-1 best_slack=0
    for ((i = 0; i < ${#TARGETS[@]}; i++)); do
      local slack=$(( NEW_MAX[i] - R[i] ))
      if [ "$slack" -gt "$best_slack" ]; then
        best_slack=$slack
        best=$i
      fi
    done
    [ "$best" -ge 0 ] || break # every target is already down at its own R; can't lower further without orphaning a running job
    NEW_MAX[best]=$(( NEW_MAX[best] - 1 ))
    combined=$(( combined - 1 ))
  done
}

# Raises the combined total by exactly one runner, handed to whichever pooled target currently has the least slack (its own MAX minus its own R): the one closest to fully using what it already has, so the extra capacity goes where it is actually needed rather than to whichever target happens to be first.
raise_by_one() {
  local i best=0 best_slack=$(( MAX[0] - R[0] ))
  for ((i = 1; i < ${#TARGETS[@]}; i++)); do
    local slack=$(( MAX[i] - R[i] ))
    if [ "$slack" -lt "$best_slack" ]; then
      best_slack=$slack
      best=$i
    fi
  done
  for ((i = 0; i < ${#TARGETS[@]}; i++)); do NEW_MAX[i]=${MAX[$i]}; done
  NEW_MAX[best]=$(( NEW_MAX[best] + 1 ))
}

fail_safe() {
  echo "FAIL-SAFE: $1 — lowering the pooled total toward the combined floor (${FLOOR})" >&2
  configmap_set "raise-confirm-count" "0"
  lower_to "$FLOOR"
  apply_new_max "fail-safe: $1"
  exit 0
}

# ---- Gather signals ---------------------------------------------------------

declare -a R MAX
USAGE_MIB=0
WH_MIB=0 # the largest pooled target's own pod memory limit, used as the headroom threshold: raising or lowering must leave room for whichever pooled target's next pod would be the largest.
for i in "${!TARGETS[@]}"; do
  namespace=${NAMESPACES[$i]}
  release=${RELEASES[$i]}
  target=${TARGETS[$i]}

  # R: the authoritative currently-running count, read from the AutoscalingRunnerSet CRD's own status (not a pod-label guess). The role gives every scale-set profile its own namespace, so each target namespace holds exactly this one scale set and no scale-set-specific label selector is needed for anything below.
  r="$(kubectl get autoscalingrunnerset "$release" -n "$namespace" -o jsonpath='{.status.currentRunners}' 2>/dev/null)" \
    || fail_safe "could not read ${target}'s status.currentRunners"
  case "$r" in ''|*[!0-9]*) fail_safe "${target}'s status.currentRunners was not a plain integer ('$r')" ;; esac
  R[i]=$r

  max="$(kubectl get autoscalingrunnerset "$release" -n "$namespace" -o jsonpath='{.spec.maxRunners}' 2>/dev/null)" \
    || fail_safe "could not read ${target}'s current spec.maxRunners"
  case "$max" in ''|*[!0-9]*) fail_safe "${target}'s spec.maxRunners was not a plain integer ('$max')" ;; esac
  MAX[i]=$max

  # Wh: the pod's own hard memory limit, read from the live deployed spec, not re-parsed from values/*.yaml, so this always matches what is actually running even after a manual --set override.
  wh_raw="$(kubectl get autoscalingrunnerset "$release" -n "$namespace" \
    -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}' 2>/dev/null)" \
    || fail_safe "could not read ${target}'s runner pod memory limit"
  [ -n "$wh_raw" ] || fail_safe "${target}'s runner pod memory limit was empty"
  wh_mib="$(to_mib "$wh_raw")" || fail_safe "could not parse ${target}'s runner pod memory limit ('$wh_raw')"
  [ "$wh_mib" -gt "$WH_MIB" ] && WH_MIB=$wh_mib

  # Actual current memory usage of running runner pods, summed — this, not a static per-pod request/limit, is the entire point of "usage-driven". Requires metrics-server; k3s bundles it by default unless explicitly disabled with --disable metrics-server (this repo's docker-compose.yml only disables traefik).
  top_output="$(kubectl top pod -n "$namespace" --no-headers 2>/dev/null)" \
    || fail_safe "kubectl top pod failed for ${namespace} (metrics-server unreachable?)"
  if [ -n "$top_output" ]; then
    while read -r _name _cpu mem _rest; do
      pod_mib="$(to_mib "$mem")" || fail_safe "could not parse a runner pod's memory usage in ${namespace} ('$mem')"
      USAGE_MIB=$(( USAGE_MIB + pod_mib ))
    done <<< "$top_output"
  fi
done

R_TOTAL=0
MAX_TOTAL=0
for i in "${!TARGETS[@]}"; do
  R_TOTAL=$(( R_TOTAL + R[i] ))
  MAX_TOTAL=$(( MAX_TOTAL + MAX[i] ))
done

# Cluster-wide memory-availability pressure via `kubectl top nodes` (metrics-server), not a single node's own /proc/meminfo - the autoscaler pod is genuinely unpinned now (see docker-compose.yml's own removal of host pinning), so a single-node reading would only ever reflect wherever the scheduler happened to place THIS pod, not the pool as a whole. Takes the WORST-CASE (minimum) available-memory percentage across all nodes, not an average: a pool average can look healthy while the specific node a new runner pod would actually land on is not - consistent with the algorithm's own existing bias toward lowering eagerly, since lowering never disrupts in-flight jobs. No swap-pressure signal: metrics-server's API has no swap field at all (confirmed against its type, not just "probably not"), and the only alternative (the kubelet's own Summary API) needs a materially broader RBAC grant - node/proxy, effectively "reach anything a kubelet exposes" - for a signal most kubelet versions don't even surface unless the off-by-default NodeSwap feature gate is on. Real cluster-wide swap visibility needs Prometheus/node-exporter, which this fleet doesn't run and which would be genuine scope creep to add here - dropping swap detection is a deliberate, documented trade-off, not an oversight.
TOP_NODES_OUTPUT="$(kubectl top nodes --no-headers 2>/dev/null)" \
  || fail_safe "kubectl top nodes failed (metrics-server unreachable?)"
[ -n "$TOP_NODES_OUTPUT" ] || fail_safe "kubectl top nodes returned no nodes"
mem_available_pct=100
while read -r _name _cpu _cpu_pct _mem mem_pct; do
  mem_pct="${mem_pct%\%}"
  case "$mem_pct" in ''|*[!0-9]*) fail_safe "could not parse a node's own MEMORY% from kubectl top nodes ('$mem_pct')" ;; esac
  node_available_pct=$(( 100 - mem_pct ))
  [ "$node_available_pct" -lt "$mem_available_pct" ] && mem_available_pct=$node_available_pct
done <<< "$TOP_NODES_OUTPUT"

pressure=false
if [ "$mem_available_pct" -lt "$MEM_AVAILABLE_PRESSURE_PCT" ]; then
  pressure=true
fi

# ---- Algorithm ---------------------------------------------------------

USABLE_BUDGET_MIB=$(( USABLE_BUDGET_GI * 1024 ))
HEADROOM_MIB=$(( USABLE_BUDGET_MIB - USAGE_MIB ))

echo "R_total=${R_TOTAL} maxRunners_total=${MAX_TOTAL} Wh=${WH_MIB}MiB usage=${USAGE_MIB}MiB headroom=${HEADROOM_MIB}MiB mem_available=${mem_available_pct}% pressure=${pressure} targets=${TARGETS[*]}"

if [ "$pressure" = "true" ] || [ "$HEADROOM_MIB" -lt "$WH_MIB" ]; then
  # Lower immediately, no delay or averaging — lowering never disrupts in-flight jobs (ARC only gates new claims), so there is no cost to being trigger-happy in this direction. Can drop below FLOOR (even to 0, spread across targets by lower_to) if pressure is severe enough.
  configmap_set "raise-confirm-count" "0"
  target_total=$FLOOR
  [ "$pressure" = "true" ] && target_total=1
  [ "$target_total" -lt "$R_TOTAL" ] && target_total=$R_TOTAL
  if [ "$target_total" -lt "$MAX_TOTAL" ]; then
    reason="lower: headroom=${HEADROOM_MIB}MiB < Wh=${WH_MIB}MiB"
    [ "$pressure" = "true" ] && reason="lower: host pressure (mem_available=${mem_available_pct}%)"
    lower_to "$target_total"
    apply_new_max "$reason" "$HEADROOM_MIB"
  else
    write_status "$MAX_TOTAL" "steady: already at or below the safe target" "$HEADROOM_MIB"
  fi
elif [ "$HEADROOM_MIB" -ge "$WH_MIB" ] && [ "$MAX_TOTAL" -lt "$MAX_CEILING" ]; then
  confirm_count="$(configmap_get 'raise-confirm-count')"
  [ -n "$confirm_count" ] || confirm_count=0
  confirm_count=$(( confirm_count + 1 ))
  configmap_set "raise-confirm-count" "$confirm_count"
  if [ "$confirm_count" -ge "$RAISE_CONFIRM_POLLS" ]; then
    configmap_set "raise-confirm-count" "0"
    raise_by_one
    apply_new_max "raise: headroom=${HEADROOM_MIB}MiB confirmed for ${confirm_count} polls" "$HEADROOM_MIB"
  else
    write_status "$MAX_TOTAL" "watching: headroom confirmed for ${confirm_count}/${RAISE_CONFIRM_POLLS} polls" "$HEADROOM_MIB"
  fi
else
  configmap_set "raise-confirm-count" "0"
  write_status "$MAX_TOTAL" "steady" "$HEADROOM_MIB"
fi
