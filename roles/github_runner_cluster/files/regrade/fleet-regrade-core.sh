# The platform-neutral core of fleet-regrade: it steps a k3s server down through levels of reduced function when memory runs short, and back up: by hand, or by itself once memory has been calm for long enough (STEP_UP, below). Nothing here knows how a level is entered; that is the actuator's job, so the same core drives a NixOS node (modules/fleet-regrade-nix.sh) and, later, a server that runs in Docker. It has no Nix store paths and no NixOS assumptions, so it can move to another repository unchanged.
#
# THE ACTUATOR CONTRACT. The file that is concatenated after this one, or sources it, defines exactly three functions and then calls `regrade_main "$@"`:
#
# levels           prints the level names, one per line, from the most capable to the most reduced. The first is the level the node starts in and is restored to; a reboot returns to it.
#
# current_level    prints the name of the level the node is running at now, read from the system itself, or "unknown" if it is none of them.
#
# apply_level NAME makes the node run at NAME, by whatever means the platform has, and returns non-zero if it could not. It is called with the node's workloads already drained when stepping down, and it is the actuator's business to restart whatever has to restart.
#
# The core owns the coordination between servers too: a step starts only while the node holds a Lease and every other etcd-voting node is Ready, so two servers never step at once and a step never starts while a peer is down.
#
# The core owns the signals (memory pressure and available memory), the decision (over the limit for long enough, one step at a time, a cooldown between steps, and, when STEP_UP is automatic, calm for long enough to go back up one level), shedding and restoring workloads with kubectl, waiting for the API server, and the command line. The sustain period is the hysteresis: a reading under the limit ends a streak, so a node has to be over for the whole period, with nothing under it, before a step down, and calm for the whole period, with nothing over it, before a step up. Calm is stricter than not being over: pressure under the threshold and MemAvailable at or above the floor, both, where over needs both to be bad, so a reading between the two states ends both streaks and a node does not step down and up on the same noise. The cooldown after a step, in either direction, is longer than the pressure window, so a node that cannot hold a higher level cycles no faster than once per cooldown and sustain period.
#
# STEP_UP is optional: `automatic` lets the watcher step up one level at a time, `manual` (the default when it is unset, which is what the core did before it existed) leaves stepping up to `fleet-regrade up`. Giving up an etcd vote is not a level, and nothing here ever does it.
#
# It needs bash, awk, jq and kubectl. Configuration comes from the environment file named by FLEET_REGRADE_ENV; the paths below default to the real system and are overridden only by tests.

FLEET_REGRADE_ENV=${FLEET_REGRADE_ENV:-/etc/fleet-regrade.env}
PSI_FILE=${PSI_FILE:-/proc/pressure/memory}
MEMINFO_FILE=${MEMINFO_FILE:-/proc/meminfo}
UPTIME_FILE=${UPTIME_FILE:-/proc/uptime}

# Where the cluster keeps its own leases, and the name that identifies this protocol among them.
LEASE_NAMESPACE=${LEASE_NAMESPACE:-kube-system}
LEASE_NAME=${LEASE_NAME:-fleet-regrade}

# The label k3s puts on the nodes that are etcd members, which are the nodes whose health a step must not stack on top of.
VOTER_LABEL=${VOTER_LABEL:-node-role.kubernetes.io/etcd=true}

# shellcheck source=/dev/null
[ -f "$FLEET_REGRADE_ENV" ] && . "$FLEET_REGRADE_ENV"

# Everything goes to stdout, which the service manager keeps as the log.
log() {
  printf '%s\n' "$*"
}

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

# The 300-second average of the share of time some task was stalled on memory, in percent.
psi_avg300() {
  awk '$1 == "some" { for (i = 2; i <= NF; i++) if ($i ~ /^avg300=/) { sub(/^avg300=/, "", $i); print $i } }' "$PSI_FILE"
}

meminfo_kb() {
  awk -v key="$1:" '$1 == key { print $2 }' "$MEMINFO_FILE"
}

uptime_seconds() {
  awk '{ print int($1) }' "$UPTIME_FILE"
}

# Succeeds when pressure is at or above the threshold and MemAvailable is below the floor, a share of MemTotal. Both are needed: pressure alone also rises on reclaim while plenty is available, and a low MemAvailable alone is normal once the page cache has filled.
is_over() {
  local pressure=$1 available_kb=$2 total_kb=$3
  awk -v p="$pressure" -v t="$PRESSURE_THRESHOLD" -v a="$available_kb" -v m="$total_kb" -v f="$AVAILABLE_FLOOR_PERCENT" \
    'BEGIN { exit !(p + 0 >= t + 0 && a * 100 < m * f) }'
}

# Succeeds when the reading is calm: pressure under the threshold and MemAvailable at or above the floor. It is not the opposite of is_over, which needs both signals to be bad: a node between the two states is neither, and a streak of either ends there.
is_calm() {
  local pressure=$1 available_kb=$2 total_kb=$3
  awk -v p="$pressure" -v t="$PRESSURE_THRESHOLD" -v a="$available_kb" -v m="$total_kb" -v f="$AVAILABLE_FLOOR_PERCENT" \
    'BEGIN { exit !(p + 0 < t + 0 && a * 100 >= m * f) }'
}

# Succeeds when the reading has held (over, to step down; calm, to step up) for the sustain period and the cooldown since the last step has passed. since and last_step are uptime seconds, empty when unset.
may_step() {
  local now=$1 since=$2 last_step=$3
  [ -n "$since" ] || return 1
  [ $((now - since)) -ge "$SUSTAIN_SECONDS" ] || return 1
  [ -z "$last_step" ] || [ $((now - last_step)) -ge "$COOLDOWN_SECONDS" ]
}

# The actuator's levels as an array, most capable first.
load_levels() {
  local level
  LEVELS=()
  while IFS= read -r level; do
    [ -z "$level" ] || LEVELS+=("$level")
  done < <(levels)
  [ "${#LEVELS[@]}" -gt 0 ] || die "The actuator reports no levels."
}

level_position() {
  local i
  for i in "${!LEVELS[@]}"; do
    if [ "${LEVELS[$i]}" = "$1" ]; then
      echo "$i"
      return 0
    fi
  done
  return 1
}

next_level_down() {
  local position
  position=$(level_position "$1") || return 1
  [ $((position + 1)) -lt "${#LEVELS[@]}" ] || return 1
  echo "${LEVELS[$((position + 1))]}"
}

next_level_up() {
  local position
  position=$(level_position "$1") || return 1
  [ "$position" -gt 0 ] || return 1
  echo "${LEVELS[$((position - 1))]}"
}

# kubectl against the cluster, as an administrator. KUBECTL may be more than one word (`k3s kubectl`). KUBE_SERVER, when set, names the API server to use instead of the one in the kubeconfig: a node that has switched its own API server off still has to reach the cluster's.
kubectl_() {
  # shellcheck disable=SC2086 # KUBECTL is a command that may take a subcommand, so it is split on purpose.
  $KUBECTL --kubeconfig "$KUBECONFIG_PATH" ${KUBE_SERVER:+--server "$KUBE_SERVER"} "$@"
}

# Taints, cordons and evicts everything but daemonsets, while the API server is still up. Both steps are idempotent, so each step down runs it again before anything is switched off. The taint is applied here and not left to the node's own configuration, which takes effect only when a node registers. --force evicts pods that no controller owns too: the node is shedding load, and a pod that blocks the drain would stop the step.
shed_workloads() {
  kubectl_ taint nodes "$NODE_NAME" "$CONTROL_PLANE_TAINT" --overwrite
  kubectl_ drain "$NODE_NAME" --ignore-daemonsets --delete-emptydir-data --force --timeout="${OPERATION_TIMEOUT_SECONDS}s"
}

restore_workloads() {
  kubectl_ taint nodes "$NODE_NAME" "${CONTROL_PLANE_TAINT%%=*}:NoSchedule-" || true
  kubectl_ uncordon "$NODE_NAME"
}

wait_for_api() {
  local deadline=$((SECONDS + OPERATION_TIMEOUT_SECONDS))
  until kubectl_ get --raw=/readyz > /dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || die "The API server did not become ready within ${OPERATION_TIMEOUT_SECONDS}s."
    sleep 2
  done
}

# The time as epoch seconds, replaced by tests.
now_epoch() {
  date +%s
}

# A Lease timestamp (RFC 3339 with microseconds) for an epoch time, built by jq so that it is the same on every platform.
micro_time() {
  jq -nr --argjson t "$1" '$t | todate | sub("Z$"; ".000000Z")'
}

# The Lease as the API server stores it: held by $1, acquired at epoch $2, last renewed at epoch $3, for LEASE_DURATION_SECONDS. A fourth argument is the resourceVersion of the Lease being replaced, which makes the replacement fail if anyone else has changed it since it was read.
build_lease() {
  jq -n --arg name "$LEASE_NAME" --arg namespace "$LEASE_NAMESPACE" --arg holder "$1" \
    --arg acquired "$(micro_time "$2")" --arg renewed "$(micro_time "$3")" \
    --argjson duration "$LEASE_DURATION_SECONDS" --arg version "${4:-}" '
    {
      apiVersion: "coordination.k8s.io/v1",
      kind: "Lease",
      metadata: ({ name: $name, namespace: $namespace } + (if $version == "" then {} else { resourceVersion: $version } end)),
      spec: { holderIdentity: $holder, leaseDurationSeconds: $duration, acquireTime: $acquired, renewTime: $renewed }
    }'
}

# What a Lease, given as JSON, means to node $2 at epoch time $3: free (nobody holds it), expired (a holder whose time has run out), mine (unexpired and held by $2) or held (unexpired and held by another). A Lease lasts until its renew time plus its duration, and is expired from that moment.
lease_state() {
  jq -r --arg me "$2" --argjson now "$3" '
    (.spec.holderIdentity // "") as $holder
    | (.spec.renewTime // .spec.acquireTime // null) as $renewed
    | (.spec.leaseDurationSeconds // 0) as $duration
    | if $holder == "" then "free"
      elif $renewed == null then "expired"
      elif (($renewed | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) + $duration) <= $now then "expired"
      elif $holder == $me then "mine"
      else "held" end' <<< "$1"
}

# Takes the Lease, or fails. Created when there is none, replaced when it is free, expired or already this node's, refused when another holds it, and refused when the API server cannot be asked, since a step must not start on a guess. Both writes fail if someone else got there first.
acquire_lease() {
  : "${LEASE_DURATION_SECONDS:?LEASE_DURATION_SECONDS must be set}"
  local current now state holder version
  now=$(now_epoch)
  if ! current=$(kubectl_ get lease "$LEASE_NAME" --namespace "$LEASE_NAMESPACE" --output json 2>&1); then
    case $current in
      *"(NotFound)"*)
        if build_lease "$NODE_NAME" "$now" "$now" | kubectl_ create -f - > /dev/null; then
          return 0
        fi
        log "cannot step: another node created the lease first"
        return 1
        ;;
      *)
        log "cannot step: the lease cannot be read: $current"
        return 1
        ;;
    esac
  fi
  state=$(lease_state "$current" "$NODE_NAME" "$now")
  if [ "$state" = held ]; then
    holder=$(jq -r '.spec.holderIdentity' <<< "$current")
    log "cannot step: the lease is held by $holder"
    return 1
  fi
  version=$(jq -r '.metadata.resourceVersion' <<< "$current")
  if build_lease "$NODE_NAME" "$now" "$now" "$version" | kubectl_ replace -f - > /dev/null; then
    return 0
  fi
  log "cannot step: the lease changed while it was being taken"
  return 1
}

# Gives the Lease back, if it is still this node's: the holder is cleared, so the next node does not wait for it to expire. If it has meanwhile passed to another node it is left alone.
release_lease() {
  local current
  current=$(kubectl_ get lease "$LEASE_NAME" --namespace "$LEASE_NAMESPACE" --output json) || return 1
  [ "$(jq -r '.spec.holderIdentity // ""' <<< "$current")" = "$NODE_NAME" ] || return 0
  jq '.spec.holderIdentity = ""' <<< "$current" | kubectl_ replace -f - > /dev/null
}

# Succeeds when every other etcd-voting node is Ready. A node whose state is unknown, that has no Ready condition, or that cannot be listed counts as not ready: a step never starts while a peer might be down.
peers_ready() {
  local nodes not_ready
  if ! nodes=$(kubectl_ get nodes --selector "$VOTER_LABEL" --output json); then
    log "cannot step: the etcd-voting nodes cannot be listed"
    return 1
  fi
  not_ready=$(jq -r --arg me "$NODE_NAME" '
    [ .items[] | select(.metadata.name != $me)
      | select(([.status.conditions[]? | select(.type == "Ready") | .status] | first // "Unknown") != "True")
      | .metadata.name ] | join(" ")' <<< "$nodes") || return 1
  if [ -n "$not_ready" ]; then
    log "cannot step: etcd-voting nodes not ready: $not_ready"
    return 1
  fi
}

# Runs a command only while this node may transition: its peers are Ready and it holds the Lease. The Lease is released afterwards whether or not the command succeeded, and the command's status is the result.
coordinated() {
  local status=0
  peers_ready || return 1
  acquire_lease || return 1
  "$@" || status=$?
  release_lease || log "the lease could not be released; it expires on its own"
  return "$status"
}

# One step down. Workloads are drained before the level is applied, and a failed drain stops the step, so nothing that needs the API server is switched off with pods still on the node.
step_down() {
  local from=$1 to=$2 reading=$3
  log "step down $from -> $to: $reading"
  if ! shed_workloads; then
    log "drain failed, staying at $from"
    return 1
  fi
  if ! apply_level "$to"; then
    log "applying $to failed, level is now $(current_level)"
    return 1
  fi
  log "now at $to"
}

# One step up, by hand or by the watcher. Returning to the first level also lifts the taint and uncordons the node, once the API server answers.
step_up() {
  local from=$1 to=$2
  log "step up $from -> $to"
  if ! apply_level "$to"; then
    log "applying $to failed, level is now $(current_level)"
    return 1
  fi
  if [ "$to" = "${LEVELS[0]}" ]; then
    wait_for_api
    restore_workloads
  fi
  log "now at $to"
}

reading_text() {
  printf 'memory some avg300=%s%% (threshold %s%%), MemAvailable %s kB of %s kB (floor %s%%)' \
    "$1" "$PRESSURE_THRESHOLD" "$2" "$3" "$AVAILABLE_FLOOR_PERCENT"
}

# One look at the signals. The streaks and the time of the last step are kept in WATCH_ABOVE_SINCE, WATCH_CALM_SINCE and WATCH_LAST_STEP, which a loop (or a test) carries from one sample to the next.
WATCH_ABOVE_SINCE=''
WATCH_CALM_SINCE=''
WATCH_LAST_STEP=''
WATCH_WARNED=''
watch_sample() {
  local level next previous pressure available total now text
  level=$(current_level)
  if [ "$level" = unknown ]; then
    [ -n "$WATCH_WARNED" ] || log "the running system is not one of the levels; not stepping"
    WATCH_WARNED=1
    return 0
  fi
  WATCH_WARNED=''
  pressure=$(psi_avg300)
  available=$(meminfo_kb MemAvailable)
  total=$(meminfo_kb MemTotal)
  now=$(uptime_seconds)
  text=$(reading_text "$pressure" "$available" "$total")
  if is_over "$pressure" "$available" "$total"; then
    if [ -z "$WATCH_ABOVE_SINCE" ]; then
      WATCH_ABOVE_SINCE=$now
      log "level $level: over the limit: $text"
    fi
  elif [ -n "$WATCH_ABOVE_SINCE" ]; then
    WATCH_ABOVE_SINCE=''
    log "level $level: back under the limit: $text"
  fi
  if is_calm "$pressure" "$available" "$total"; then
    if [ -z "$WATCH_CALM_SINCE" ]; then
      WATCH_CALM_SINCE=$now
      if [ "${STEP_UP:-manual}" = automatic ] && next_level_up "$level" > /dev/null; then
        log "level $level: calm: $text"
      fi
    fi
  else
    WATCH_CALM_SINCE=''
  fi
  if next=$(next_level_down "$level") && may_step "$now" "$WATCH_ABOVE_SINCE" "$WATCH_LAST_STEP"; then
    coordinated step_down "$level" "$next" "$text" || true
    WATCH_LAST_STEP=$(uptime_seconds)
    WATCH_ABOVE_SINCE=''
    WATCH_CALM_SINCE=''
  elif [ "${STEP_UP:-manual}" = automatic ] && previous=$(next_level_up "$level") && may_step "$now" "$WATCH_CALM_SINCE" "$WATCH_LAST_STEP"; then
    coordinated step_up "$level" "$previous" || true
    WATCH_LAST_STEP=$(uptime_seconds)
    WATCH_CALM_SINCE=''
  fi
}

cmd_watch() {
  load_levels
  while true; do
    sleep "$SAMPLE_SECONDS"
    watch_sample
  done
}

cmd_status() {
  load_levels
  log "level: $(current_level) (of: ${LEVELS[*]})"
  log "$(reading_text "$(psi_avg300)" "$(meminfo_kb MemAvailable)" "$(meminfo_kb MemTotal)")"
  log "sample ${SAMPLE_SECONDS}s, sustained for ${SUSTAIN_SECONDS}s, cooldown ${COOLDOWN_SECONDS}s, step up ${STEP_UP:-manual}"
}

# One step down by hand, for trying a level without waiting for pressure.
cmd_down() {
  local level next
  load_levels
  level=$(current_level)
  [ "$level" != unknown ] || die "The running system is not one of the levels."
  next=$(next_level_down "$level") || die "Already at $level, the most reduced level."
  coordinated step_down "$level" "$next" "by hand"
}

# One step up by hand.
cmd_up() {
  local level next
  load_levels
  level=$(current_level)
  [ "$level" != unknown ] || die "The running system is not one of the levels."
  next=$(next_level_up "$level") || die "Already at $level, the first level."
  coordinated step_up "$level" "$next"
}

regrade_main() {
  case ${1:-} in
    watch) cmd_watch ;;
    status) cmd_status ;;
    up) cmd_up ;;
    down) cmd_down ;;
    *) die "Usage: fleet-regrade watch|status|up|down" ;;
  esac
}
