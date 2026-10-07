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
# Where each job's observed peak memory is kept, in the same ConfigMap. A ConfigMap holds at most 1 MiB and an entry is under 512 bytes, so capping the entries at 2048 stays at half the limit; entries unseen for 30 days are dropped, since a job that has not run for that long no longer informs sizing.
JOB_MEMORY_KEY="job-memory"
# The peaks the runners' own job-completed hook measured (runner-hooks/job-completed.sh), kept apart from the sampled ones above because they are keyed by the job's id, not its display name, and are exact, not a lower bound.
JOB_PEAK_KEY="job-peak"
# The ConfigMap key counting consecutive polls in which one pooled target was saturated while another held spare slots (see rebalance_one), kept apart from the raise path's own `raise-confirm-count`.
REBALANCE_CONFIRM_KEY="rebalance-confirm-count"
# A pooled target is never rebalanced below this many runners, so a quiet one can always start a job.
REBALANCE_MIN_MAX=1
JOB_MEMORY_MAX_ENTRIES=2048
JOB_MEMORY_RETENTION_SECONDS=$(( 30 * 24 * 60 * 60 ))

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

# The value is passed through stdin and the patch through a file, never as a command-line argument: a stored record can reach hundreds of KiB, and a single argument over the kernel's limit (128 KiB on Linux, MAX_ARG_STRLEN) fails the exec with "Argument list too long". printf is a shell builtin, so it is not subject to that limit either.
configmap_set() {
  local patch_file status=0
  patch_file="$(mktemp)" || return 1
  printf '%s' "$2" | jq -Rs --arg k "$1" '{data: {($k): .}}' > "$patch_file" \
    && kubectl patch configmap "$CONFIGMAP" -n "$STATE_NAMESPACE" --type merge --patch-file "$patch_file" || status=$?
  rm -f "$patch_file"
  return "$status"
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
    --argjson pod_limit_mib "$WH_MIB" \
    --argjson targets "$(targets_json)" \
    '{last_run: $last_run, current_max_runners: $current_max_runners, headroom_gi: ($headroom_gi | tonumber? // null), mode: $mode, pod_limit_mib: $pod_limit_mib, targets: $targets}')"
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

# Moves one slot from the pooled target with the most spare slots to a target that has used all of its own, without changing the combined total, so a pool at its ceiling is not stuck with an idle target's share while another queues. Sets NEW_MAX and returns 0 when a move applies, 1 when none does. A target counts as saturated when its running count has reached its own maxRunners; a donor must keep at least its running count and at least REBALANCE_MIN_MAX runners after giving one up. Ties go to the first target, so a poll's choice is stable.
rebalance_one() {
  local i saturated=-1 donor=-1 donor_slack=0 slack
  for ((i = 0; i < ${#TARGETS[@]}; i++)); do
    NEW_MAX[i]=${MAX[$i]}
    if [ "$saturated" -lt 0 ] && [ "${R[$i]}" -ge "${MAX[$i]}" ]; then
      saturated=$i
    fi
  done
  [ "$saturated" -ge 0 ] || return 1
  for ((i = 0; i < ${#TARGETS[@]}; i++)); do
    [ "$i" -ne "$saturated" ] || continue
    slack=$(( MAX[i] - R[i] ))
    [ "$(( MAX[i] - 1 ))" -ge "$REBALANCE_MIN_MAX" ] || continue
    if [ "$slack" -gt "$donor_slack" ]; then
      donor_slack=$slack
      donor=$i
    fi
  done
  [ "$donor" -ge 0 ] || return 1
  NEW_MAX[donor]=$(( MAX[donor] - 1 ))
  NEW_MAX[saturated]=$(( MAX[saturated] + 1 ))
  REBALANCE_FROM=$donor
  REBALANCE_TO=$saturated
}

# Merges {repo, workflow, job, mib} entries into the JSON object kept under a ConfigMap key, one entry per repository, workflow ref and job, holding the highest figure seen and when it was last seen. $3 is true when each merge counts as an observation (the sampled record, where one job is seen many times); the measured record is re-read from Events that stay around, so counting there would count the same job again. Entries unseen for the retention are dropped, and the oldest go first past the cap.
store_job_entries() {
  local key="$1" jobs="$2" count="$3" existing updated
  existing="$(configmap_get "$key")"
  [ -n "$existing" ] || existing="{}"
  updated="$(jq -c --slurpfile jobs <(printf '%s' "$jobs") --argjson count "$count" --argjson now "$(date +%s)" --argjson retention "$JOB_MEMORY_RETENTION_SECONDS" --argjson cap "$JOB_MEMORY_MAX_ENTRIES" '
    reduce $jobs[0][] as $o (.;
      ($o.repo + "|" + $o.workflow + "|" + $o.job) as $k
      | .[$k] = ({repo: $o.repo, workflow: $o.workflow, job: $o.job, peak_mib: ([$o.mib, (.[$k].peak_mib // 0)] | max), last_seen: $now}
        + (if $count then {samples: ((.[$k].samples // 0) + 1)} else {} end)))
    | with_entries(select(.value.last_seen >= ($now - $retention)))
    | to_entries | sort_by(-.value.last_seen) | .[0:$cap] | from_entries' <<< "$existing")"
  configmap_set "$key" "$updated"
}

# Records, per job, the highest memory a runner pod running it has been seen at. This is a lower bound, not the true peak: it is sampled once per poll from the metrics server, so a spike shorter than the poll can pass unseen. It exists to show how far below a pod's request jobs actually sit, which decides whether an accurate in-pod measurement is worth building. The pod running a job is an EphemeralRunner of the same name, whose status names the job's repository, workflow and display name. $1 is a JSON array of {namespace, pod, mib} observations.
record_job_peaks() {
  local observations="$1" jobs="[]" obs namespace pod mib identity
  while IFS= read -r obs; do
    namespace="$(jq -r .namespace <<< "$obs")"
    pod="$(jq -r .pod <<< "$obs")"
    mib="$(jq -r .mib <<< "$obs")"
    identity="$(kubectl get ephemeralrunner "$pod" -n "$namespace" -o json 2>/dev/null \
      | jq -c --argjson mib "$mib" '{repo: .status.jobRepositoryName, workflow: .status.jobWorkflowRef, job: .status.jobDisplayName, mib: $mib} | select(.repo and .workflow and .job)')" || continue
    [ -n "$identity" ] && jobs="$(jq -c --argjson one "$identity" '. + [$one]' <<< "$jobs")"
  done < <(jq -c '.[]' <<< "$observations")
  [ "$jobs" != "[]" ] || return 0
  store_job_entries "$JOB_MEMORY_KEY" "$jobs" true
}

# The runners' own job-completed hook posts each job's cgroup peak as an Event in the pod's namespace (runner-hooks/job-completed.sh). The Events outlive the pod for the cluster's event TTL, so reading them each poll catches every job, and keeping the maximum per job makes re-reading one harmless and a forged low figure unable to lower a job's size. A message that is not the expected JSON is ignored.
record_measured_peaks() {
  local namespace jobs="[]" found
  for namespace in "${NAMESPACES[@]}"; do
    found="$(kubectl get events -n "$namespace" --field-selector reason=JobMemoryPeak -o json 2>/dev/null \
      | jq -c '[.items[] | (.message | fromjson?) // empty
        | select((.repo | type) == "string" and (.workflow | type) == "string" and (.job | type) == "string" and (.peak_bytes | type) == "number")
        | {repo, workflow, job, mib: (((.peak_bytes + 1048575) / 1048576) | floor)}]')" || return 1
    jobs="$(jq -c --argjson found "$found" '. + $found' <<< "$jobs")"
  done
  [ "$jobs" != "[]" ] || return 0
  store_job_entries "$JOB_PEAK_KEY" "$jobs" false
}

# For a failure before every target's current state is known: fail_safe patches from that state, so with none or part of it, the only honest outcome is to stop loudly (the API server is unreachable, so nothing could be patched anyway).
die() {
  echo "ERROR: $1" >&2
  exit 1
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
UNSCHEDULABLE_TOTAL=0

# Phase 1: every target's own running count, maxRunners and unschedulable runner pods. fail_safe needs all of these to lower anything, so a failure here dies instead.
for i in "${!TARGETS[@]}"; do
  namespace=${NAMESPACES[$i]}
  release=${RELEASES[$i]}
  target=${TARGETS[$i]}

  # R: how many runner slots the target holds right now, counted from its EphemeralRunners (an EphemeralRunner is one runner pod, from creation until it finishes), not from the AutoscalingRunnerSet's status: that object's status carries only `phase` and `observedGeneration` in the ARC release this role pins (0.15), so a count read from it is always empty. A runner that has finished (`Succeeded`) or given up (`Failed`) no longer holds a slot; a `Pending` one does, because its pod already counts against the pool. The role gives every scale-set profile its own namespace, so each target namespace holds exactly this one scale set's runners. A failed read stops the poll: a guessed zero would let every rule below that depends on a running count (never lower below it, slack per target) act on a number that is not real.
  r="$(kubectl get ephemeralrunners -n "$namespace" -o json 2>/dev/null \
    | jq '[.items[] | select((.status.phase // "") != "Succeeded" and (.status.phase // "") != "Failed")] | length')" \
    || die "could not count ${target}'s ephemeral runners"
  case "$r" in ''|*[!0-9]*) die "${target}'s ephemeral runner count was not a plain integer ('$r')" ;; esac
  R[i]=$r

  max="$(kubectl get autoscalingrunnerset "$release" -n "$namespace" -o jsonpath='{.spec.maxRunners}' 2>/dev/null)" \
    || die "could not read ${target}'s current spec.maxRunners"
  case "$max" in ''|*[!0-9]*) die "${target}'s spec.maxRunners was not a plain integer ('$max')" ;; esac
  MAX[i]=$max

  # Runner pods the scheduler has found no node for. The memory budget below sums every node's capacity, so it cannot see that a node is cordoned or tainted (disk pressure, for one) or that a pod's request fits no single node; a pod stuck Unschedulable is the direct evidence that the budgeted capacity is not real.
  unschedulable="$(kubectl get pods -n "$namespace" -l actions-ephemeral-runner=True -o json 2>/dev/null \
    | jq '[.items[] | select(any(.status.conditions[]?; .type == "PodScheduled" and .status == "False" and .reason == "Unschedulable"))] | length')" \
    || die "could not list ${target}'s unschedulable runner pods"
  case "$unschedulable" in ''|*[!0-9]*) die "${target}'s unschedulable runner pod count was not a plain integer ('$unschedulable')" ;; esac
  UNSCHEDULABLE_TOTAL=$(( UNSCHEDULABLE_TOTAL + unschedulable ))
done

# Phase 2: memory measurements. Any failure fails safe, which can now lower every target because phase 1 read them all.
USAGE_MIB=0
JOB_OBSERVATIONS="[]"
WH_MIB=0 # the largest pooled target's own pod memory limit, used as the headroom threshold: raising or lowering must leave room for whichever pooled target's next pod would be the largest.
for i in "${!TARGETS[@]}"; do
  namespace=${NAMESPACES[$i]}
  release=${RELEASES[$i]}
  target=${TARGETS[$i]}

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
    while read -r pod_name _cpu mem _rest; do
      pod_mib="$(to_mib "$mem")" || fail_safe "could not parse a runner pod's memory usage in ${namespace} ('$mem')"
      USAGE_MIB=$(( USAGE_MIB + pod_mib ))
      JOB_OBSERVATIONS="$(jq -c --arg namespace "$namespace" --arg pod "$pod_name" --argjson mib "$pod_mib" '. + [{namespace: $namespace, pod: $pod, mib: $mib}]' <<< "$JOB_OBSERVATIONS")"
    done <<< "$top_output"
  fi
done

# Capacity decisions never depend on this record, so a failure to keep it is reported and the poll carries on.
record_job_peaks "$JOB_OBSERVATIONS" || echo "WARN: could not record the jobs' observed memory" >&2
record_measured_peaks || echo "WARN: could not record the jobs' measured peak memory" >&2

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

echo "R_total=${R_TOTAL} maxRunners_total=${MAX_TOTAL} Wh=${WH_MIB}MiB usage=${USAGE_MIB}MiB headroom=${HEADROOM_MIB}MiB mem_available=${mem_available_pct}% pressure=${pressure} unschedulable=${UNSCHEDULABLE_TOTAL} targets=${TARGETS[*]}"

# The ceiling is a hard cap on the pooled total, so a total above it (each profile's own static maxRunners can sum to more, and every Helm upgrade restores those) is lowered to it even when nothing else is wrong.
over_ceiling=false
[ "$MAX_TOTAL" -gt "$MAX_CEILING" ] && over_ceiling=true

if [ "$pressure" = "true" ] || [ "$HEADROOM_MIB" -lt "$WH_MIB" ] || [ "$UNSCHEDULABLE_TOTAL" -gt 0 ] || [ "$over_ceiling" = "true" ]; then
  # Lower immediately, no delay or averaging — lowering never disrupts in-flight jobs (ARC only gates new claims), so there is no cost to being trigger-happy in this direction. Can drop below FLOOR (even to 0, spread across targets by lower_to) if pressure is severe enough.
  configmap_set "raise-confirm-count" "0"
  configmap_set "$REBALANCE_CONFIRM_KEY" "0"
  target_total=$FLOOR
  # Only the ceiling is exceeded: the pool is otherwise healthy, so it comes down to the ceiling, not to the floor.
  if [ "$pressure" != "true" ] && [ "$HEADROOM_MIB" -ge "$WH_MIB" ] && [ "$UNSCHEDULABLE_TOTAL" -eq 0 ]; then
    target_total=$MAX_CEILING
  fi
  [ "$pressure" = "true" ] && target_total=1
  [ "$UNSCHEDULABLE_TOTAL" -gt 0 ] && target_total=0 # clamped to R_TOTAL below: stop asking for runners no node can host, never orphan a running job
  [ "$target_total" -lt "$R_TOTAL" ] && target_total=$R_TOTAL
  if [ "$target_total" -lt "$MAX_TOTAL" ]; then
    reason="lower: headroom=${HEADROOM_MIB}MiB < Wh=${WH_MIB}MiB"
    [ "$over_ceiling" = "true" ] && reason="lower: pooled maxRunners ${MAX_TOTAL} is above the ceiling ${MAX_CEILING}"
    [ "$pressure" = "true" ] && reason="lower: host pressure (mem_available=${mem_available_pct}%)"
    [ "$UNSCHEDULABLE_TOTAL" -gt 0 ] && reason="lower: ${UNSCHEDULABLE_TOTAL} runner pod(s) unschedulable, so the budgeted capacity is not all schedulable"
    lower_to "$target_total"
    apply_new_max "$reason" "$HEADROOM_MIB"
  else
    write_status "$MAX_TOTAL" "steady: already at or below the safe target" "$HEADROOM_MIB"
  fi
elif [ "$HEADROOM_MIB" -ge "$WH_MIB" ] && [ "$MAX_TOTAL" -lt "$MAX_CEILING" ]; then
  configmap_set "$REBALANCE_CONFIRM_KEY" "0"
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
  # Here the pool is healthy and holds as many runners as it may (below the ceiling with enough headroom is the raise branch above; pressure, an unschedulable pod and a total over the ceiling are the lower branch). Slots are fixed per target, so a target that has used all of its own while another sits idle would queue for capacity the pool has. Move one slot at a time, and only once the same imbalance has been seen for as many polls as a raise needs, so a burst that ends within a poll or two moves nothing.
  configmap_set "raise-confirm-count" "0"
  if rebalance_one; then
    rebalance_count="$(configmap_get "$REBALANCE_CONFIRM_KEY")"
    case "$rebalance_count" in ''|*[!0-9]*) rebalance_count=0 ;; esac
    rebalance_count=$(( rebalance_count + 1 ))
    if [ "$rebalance_count" -ge "$RAISE_CONFIRM_POLLS" ]; then
      configmap_set "$REBALANCE_CONFIRM_KEY" "0"
      apply_new_max "rebalance: ${TARGETS[$REBALANCE_TO]} has used all ${MAX[$REBALANCE_TO]} of its runners while ${TARGETS[$REBALANCE_FROM]} runs ${R[$REBALANCE_FROM]} of ${MAX[$REBALANCE_FROM]}; moving one slot" "$HEADROOM_MIB"
    else
      configmap_set "$REBALANCE_CONFIRM_KEY" "$rebalance_count"
      write_status "$MAX_TOTAL" "steady: ${TARGETS[$REBALANCE_TO]} is saturated, imbalance confirmed for ${rebalance_count}/${RAISE_CONFIRM_POLLS} polls" "$HEADROOM_MIB"
    fi
  else
    configmap_set "$REBALANCE_CONFIRM_KEY" "0"
    write_status "$MAX_TOTAL" "steady" "$HEADROOM_MIB"
  fi
fi
