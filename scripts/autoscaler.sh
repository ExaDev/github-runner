#!/usr/bin/env bash
# Usage-driven maxRunners autoscaler. Runs inside the `autoscaler` in-cluster Deployment (see autoscaler/loop.sh for the poll loop). Never inspects job identity or the GitHub Actions queue: it only watches real, current memory usage and pressure, and adjusts spec.maxRunners on the single AutoscalingRunnerSet (exadev-runners, namespace arc-runners-exadev) accordingly. See the project README's Autoscaler section for the full algorithm and rationale.
#
# Environment (provided by the Deployment - see roles/github_runner_arc/tasks/install_platform.yml):
# - kubectl needs no KUBECONFIG here: running as a pod with a mounted ServiceAccount token, client-go auto-detects in-cluster config.
# - AUTOSCALER_DRY_RUN: "true" computes and logs only, never patches
# - AUTOSCALER_USABLE_BUDGET_GI: proven-safe memory budget for all runner pods combined (see values/exadev-runners-values.yaml)
# - AUTOSCALER_MAX_CEILING: hard operator cap on maxRunners, independent of the live arithmetic
# - AUTOSCALER_FLOOR: the static floor every Helm upgrade reverts to
# - AUTOSCALER_RAISE_CONFIRM_POLLS: consecutive polls of confirmed headroom before raising
# - AUTOSCALER_MEM_AVAILABLE_PRESSURE_PCT: MemAvailable-of-MemTotal percentage treated as real pressure, evaluated as the worst case across all nodes (see the kubectl top nodes block below) - no swap-pressure equivalent, see that block's own comment for why
# - AUTOSCALER_NAMESPACE, AUTOSCALER_RELEASE_NAME: the AutoscalingRunnerSet to manage
# - AUTOSCALER_STATE_NAMESPACE, AUTOSCALER_STATUS_CONFIGMAP: the ConfigMap this pod's own status/raise-confirm-count state lives in (shared with scripts/heartbeat.sh, which reads status.json back out to republish in the heartbeat gist)
set -euo pipefail

NAMESPACE="${AUTOSCALER_NAMESPACE:-arc-runners-exadev}"
RELEASE_NAME="${AUTOSCALER_RELEASE_NAME:-exadev-runners}"
DRY_RUN="${AUTOSCALER_DRY_RUN:-true}"
USABLE_BUDGET_GI="${AUTOSCALER_USABLE_BUDGET_GI:-24}"
MAX_CEILING="${AUTOSCALER_MAX_CEILING:-7}"
FLOOR="${AUTOSCALER_FLOOR:-3}"
RAISE_CONFIRM_POLLS="${AUTOSCALER_RAISE_CONFIRM_POLLS:-2}"
MEM_AVAILABLE_PRESSURE_PCT="${AUTOSCALER_MEM_AVAILABLE_PRESSURE_PCT:-15}"
STATE_NAMESPACE="${AUTOSCALER_STATE_NAMESPACE:-github-runner-platform}"
CONFIGMAP="${AUTOSCALER_STATUS_CONFIGMAP:-autoscaler-status}"

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

write_status() {
  local current_max="$1" mode="$2" headroom_mib="${3:-null}" headroom_gi="null"
  if [ "$headroom_mib" != "null" ]; then
    headroom_gi="$(awk -v m="$headroom_mib" 'BEGIN{printf "%.1f", m/1024}')"
  fi
  local status_json
  status_json="$(jq -n \
    --arg last_run "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --argjson current_max_runners "$current_max" \
    --arg headroom_gi "$headroom_gi" \
    --arg mode "$mode" \
    '{last_run: $last_run, current_max_runners: $current_max_runners, headroom_gi: ($headroom_gi | tonumber? // null), mode: $mode}')"
  configmap_set "status.json" "$status_json"
}

patch_max_runners() {
  local target="$1" reason="$2" headroom_mib="${3:-null}"
  if [ "$DRY_RUN" = "true" ]; then
    echo "DRY RUN: would patch maxRunners to ${target} (${reason})"
  else
    kubectl patch autoscalingrunnerset "$RELEASE_NAME" -n "$NAMESPACE" \
      --type merge -p "{\"spec\":{\"maxRunners\": ${target}}}"
    echo "Patched maxRunners to ${target} (${reason})"
  fi
  write_status "$target" "$reason" "$headroom_mib"
}

fail_safe() {
  echo "FAIL-SAFE: $1 — patching maxRunners down to the static floor (${FLOOR})" >&2
  configmap_set "raise-confirm-count" "0"
  patch_max_runners "$FLOOR" "fail-safe: $1"
  exit 0
}

# ---- Gather signals ---------------------------------------------------------

# R: the authoritative currently-running count, read from the AutoscalingRunnerSet CRD's own status (not a pod-label guess). arc-runners-exadev holds exactly one scale set by design — see the project's decision not to differentiate runner pools — so no scale-set-specific label selector is even needed for anything below.
R="$(kubectl get autoscalingrunnerset "$RELEASE_NAME" -n "$NAMESPACE" -o jsonpath='{.status.currentRunners}' 2>/dev/null)" \
  || fail_safe "could not read ${RELEASE_NAME}'s status.currentRunners"
case "$R" in ''|*[!0-9]*) fail_safe "status.currentRunners was not a plain integer ('$R')" ;; esac

# Wh: the pod's own hard memory limit, read from the live deployed spec, not re-parsed from values/*.yaml, so this always matches what is actually running even after a manual --set override.
WH_RAW="$(kubectl get autoscalingrunnerset "$RELEASE_NAME" -n "$NAMESPACE" \
  -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}' 2>/dev/null)" \
  || fail_safe "could not read the runner pod's own memory limit"
[ -n "$WH_RAW" ] || fail_safe "the runner pod's memory limit was empty"
WH_MIB="$(to_mib "$WH_RAW")" || fail_safe "could not parse the runner pod's memory limit ('$WH_RAW')"

# Actual current memory usage of running runner pods, summed — this, not a static per-pod request/limit, is the entire point of "usage-driven". Requires metrics-server; k3s bundles it by default unless explicitly disabled with --disable metrics-server (this repo's docker-compose.yml only disables traefik).
TOP_OUTPUT="$(kubectl top pod -n "$NAMESPACE" --no-headers 2>/dev/null)" \
  || fail_safe "kubectl top pod failed (metrics-server unreachable?)"
USAGE_MIB=0
if [ -n "$TOP_OUTPUT" ]; then
  while read -r _name _cpu mem _rest; do
    pod_mib="$(to_mib "$mem")" || fail_safe "could not parse a runner pod's memory usage ('$mem')"
    USAGE_MIB=$(( USAGE_MIB + pod_mib ))
  done <<< "$TOP_OUTPUT"
fi

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

CURRENT_MAX="$(kubectl get autoscalingrunnerset "$RELEASE_NAME" -n "$NAMESPACE" -o jsonpath='{.spec.maxRunners}' 2>/dev/null)" \
  || fail_safe "could not read ${RELEASE_NAME}'s current spec.maxRunners"
case "$CURRENT_MAX" in ''|*[!0-9]*) fail_safe "spec.maxRunners was not a plain integer ('$CURRENT_MAX')" ;; esac

echo "R=${R} maxRunners=${CURRENT_MAX} Wh=${WH_MIB}MiB usage=${USAGE_MIB}MiB headroom=${HEADROOM_MIB}MiB mem_available=${mem_available_pct}% pressure=${pressure}"

if [ "$pressure" = "true" ] || [ "$HEADROOM_MIB" -lt "$WH_MIB" ]; then
  # Lower immediately, no delay or averaging — lowering never disrupts in-flight jobs (ARC only gates new claims), so there is no cost to being trigger-happy in this direction. Can drop below FLOOR (even to 0) if pressure is severe enough, but never below R: a currently-running job must never be orphaned by its own scale set shrinking under it.
  configmap_set "raise-confirm-count" "0"
  target=$FLOOR
  [ "$pressure" = "true" ] && target=1
  [ "$target" -lt "$R" ] && target=$R
  if [ "$target" -lt "$CURRENT_MAX" ]; then
    reason="lower: headroom=${HEADROOM_MIB}MiB < Wh=${WH_MIB}MiB"
    [ "$pressure" = "true" ] && reason="lower: host pressure (mem_available=${mem_available_pct}%)"
    patch_max_runners "$target" "$reason" "$HEADROOM_MIB"
  else
    write_status "$CURRENT_MAX" "steady: already at or below the safe target" "$HEADROOM_MIB"
  fi
elif [ "$HEADROOM_MIB" -ge "$WH_MIB" ] && [ "$CURRENT_MAX" -lt "$MAX_CEILING" ]; then
  confirm_count="$(configmap_get 'raise-confirm-count')"
  [ -n "$confirm_count" ] || confirm_count=0
  confirm_count=$(( confirm_count + 1 ))
  configmap_set "raise-confirm-count" "$confirm_count"
  if [ "$confirm_count" -ge "$RAISE_CONFIRM_POLLS" ]; then
    configmap_set "raise-confirm-count" "0"
    target=$(( CURRENT_MAX + 1 ))
    patch_max_runners "$target" "raise: headroom=${HEADROOM_MIB}MiB confirmed for ${confirm_count} polls" "$HEADROOM_MIB"
  else
    write_status "$CURRENT_MAX" "watching: headroom confirmed for ${confirm_count}/${RAISE_CONFIRM_POLLS} polls" "$HEADROOM_MIB"
  fi
else
  configmap_set "raise-confirm-count" "0"
  write_status "$CURRENT_MAX" "steady" "$HEADROOM_MIB"
fi
