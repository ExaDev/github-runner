#!/usr/bin/env bash
# fleet-role-actuator: switches this host between a k3s server and a k3s agent, for the role controller's voter swap (scripts/fleet-role.sh), as its Node asks. It does only what has to happen on the host: the controller does everything on the API (the cordon, the wait for runner pods, the drain, the taint, the regrading level label, the removal of the etcd member, the clean-up of the Node's labels and annotations, the uncordon), and the actuator's own credential can read Nodes and the regrade Lease and patch its report, nothing more.
#
# THE NODE. Only the controller sets these, on this host's Node; all are under github-runner.exadev.com/. The label desired-role (server or agent) is the role the host is to run. The annotation role-swap names the swap (<UTC %Y%m%dT%H%M%SZ>-<outgoing node>-<incoming node>), role-step the step this host is to take in it (promote, step-down or rollback), role-join-node the node of the voter a promotion joins, snapshot-request a swap whose snapshot this host, a voter that stays, is to take, and role-member-gone the swap under which every remaining voter reports that etcd no longer lists this host's member.
#
# THE REPORT. Each step's outcome goes to the ConfigMap ACTUATOR_CONFIGMAP in ACTUATOR_NAMESPACE, under this node's name, as JSON: {swap, step, phase, role, started_at, at, message, snapshot}, phase being running, done, failed or refused, role what the role file names afterwards, started_at the host's time just before a unit was started (what the controller waits for a later heartbeat than), at the time of the report and snapshot the names of the files a snapshot created. A report the API does not take is made again by the watch, which runs a step again until its outcome is reported; every step can be run again.
#
# AUTHORITY. Every step that could add a member (promote), and every other step a swap asks for (step-down, rollback, snapshot), is taken only while the regrade Lease (LEASE_NAMESPACE/LEASE_NAME) is held, unexpired, by ROLE_LEASE_HOLDER with the swap's id in its SWAP_ID_ANNOTATION: the controller holds it for the whole swap, and a regrade step or a run of the role cannot hold it meanwhile. finish-demote needs no Lease: it runs only once the role file names the agent (or, naming the server, the datastore holds k3s's tombstone, which a host down during its step-down and removal has in place of the role file, or the Node asks for a step-down that failed before the role file was written, after which the controller removes a host that is not Ready) and the server's etcd member is gone (a tombstone in the datastore, the Node's etcd.k3s.cattle.io/removed-node-name naming the datastore's member, the Node's role-member-gone naming the swap it is in, or no etcd label on the Node), and completing the demotion of a member that is no longer one can never cost quorum, so a host that was down while it was removed finishes on its return.
#
# COMMANDS. watch runs for ever, reading this host's Node every POLL_SECONDS and taking the step it asks for, a snapshot it asks for, and the finish of a demotion whose member is gone. status prints the host's role, units and datastore and its last report. promote ID, step-down ID, rollback ID, snapshot ID and finish-demote take one step by hand and report it as the watch does. promote-local [JOIN_URL] and demote-local are the host-local part of a switch with no Lease and no report, for the role's own promotion and demotion (tasks/native/server_turn.yml and demote.yml), so that Ansible and the actuator switch a host the same way.
#
# EXIT STATUS. 0: done. 1: failed; the host is left with at most one role's unit running and a role file naming that role. 2: refused, with nothing changed: the Lease is not held by ROLE_LEASE_HOLDER for this swap (or cannot be read), the host is not provisioned to be a server, the role file names something that is not a role, the step is not one this actuator takes, or the step's own precondition does not hold (each says which). 3: not yet (finish-demote before the member is gone).
#
# The platform's file, fleet-role-actuator-$FLEET_ROLE_ACTUATOR.sh (systemd for a native host, docker for a host under the role's Compose project), says how each part is done on the host (fleet-role-actuator-systemd.sh's header lists the functions), after fleet-regrade-core.sh (kubectl_, select_api_server, lease_state, now_epoch, log), fleet-regrade-k3s.sh (write_level, restart_into, current_level, POLL_SECONDS) and the regrading actuator of the same platform, all from this script's directory. The settings are in fleet-role-actuator.env beside it, which the role templates (templates/role-actuator.env.j2). It needs bash, jq and kubectl besides what the platform needs.
set -euo pipefail

lib=$(dirname "$(readlink -f "$0")")
# The core reads its settings from FLEET_REGRADE_ENV as it is sourced, so it is pointed at the actuator's.
FLEET_REGRADE_ENV=${FLEET_ROLE_ACTUATOR_ENV:-$lib/fleet-role-actuator.env}

EXIT_DONE=0
EXIT_FAILED=1
EXIT_REFUSED=2
EXIT_NOT_YET=3

# shellcheck source=/dev/null
. "$lib/fleet-regrade-core.sh"
case ${FLEET_ROLE_ACTUATOR:-} in
  systemd | docker) ;;
  *)
    echo "FLEET_ROLE_ACTUATOR is '${FLEET_ROLE_ACTUATOR:-}' in $FLEET_REGRADE_ENV; it must be systemd or docker." >&2
    exit "$EXIT_REFUSED"
    ;;
esac
# shellcheck source=/dev/null
. "$lib/fleet-regrade-k3s.sh"
# shellcheck source=/dev/null
. "$lib/fleet-regrade-$FLEET_ROLE_ACTUATOR.sh"
# shellcheck source=/dev/null
. "$lib/fleet-role-actuator-$FLEET_ROLE_ACTUATOR.sh"

: "${NODE_NAME:?NODE_NAME must be set}" "${ROLE_LEASE_HOLDER:?ROLE_LEASE_HOLDER must be set}" "${SWAP_ID_ANNOTATION:?SWAP_ID_ANNOTATION must be set}"
: "${ACTUATOR_NAMESPACE:?ACTUATOR_NAMESPACE must be set}" "${ACTUATOR_CONFIGMAP:?ACTUATOR_CONFIGMAP must be set}" "${JOIN_URLS_FILE:?JOIN_URLS_FILE must be set}"

# The Node's interface, fixed here: the controller writes these and the actuator reads them.
DESIRED_ROLE_LABEL=github-runner.exadev.com/desired-role
SWAP_ANNOTATION=github-runner.exadev.com/role-swap
STEP_ANNOTATION=github-runner.exadev.com/role-step
JOIN_NODE_ANNOTATION=github-runner.exadev.com/role-join-node
SNAPSHOT_ANNOTATION=github-runner.exadev.com/snapshot-request
MEMBER_GONE_ANNOTATION=github-runner.exadev.com/role-member-gone
# What k3s writes: the etcd role label it puts on a member's Node, and the annotation its member controller records once etcd has removed the member named in it (pkg/etcd/member_controller.go).
ETCD_LABEL=node-role.kubernetes.io/etcd
REMOVED_ANNOTATION=etcd.k3s.cattle.io/removed-node-name

# What the step in hand says and sets, for its report: why it ended as it did, the host's time just before it started a unit, and the snapshot files it created.
MESSAGE=''
STARTED_AT=''
SNAPSHOT=''
# The swap and step run_step is taking, which report() reports against.
CURRENT_SWAP=''
CURRENT_STEP=''
# This host's Node, as JSON, read by the watch pass or the step that needs it.
NODE_JSON='{}'

# Succeeds when $1 has the shape of a swap id: the UTC time to the second, then the two node names, which are DNS labels.
valid_swap_id() {
  [[ $1 =~ ^[0-9]{8}T[0-9]{6}Z-[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]
}

read_node() {
  NODE_JSON=$(kubectl_ get node "$NODE_NAME" --output json)
}

node_annotation() {
  jq -r --arg key "$1" '.metadata.annotations[$key] // ""' <<< "$NODE_JSON"
}

node_label() {
  jq -r --arg key "$1" '.metadata.labels[$key] // ""' <<< "$NODE_JSON"
}

# This node's last report, as JSON, or {} when it has none; fails when the ConfigMap cannot be read.
own_entry() {
  local configmap
  configmap=$(kubectl_ get configmap "$ACTUATOR_CONFIGMAP" --namespace "$ACTUATOR_NAMESPACE" --output json) || return 1
  jq -r --arg key "$NODE_NAME" '.data[$key] // "{}"' <<< "$configmap"
}

# Succeeds when the report $1 is for swap $2 and step $3 and its phase is one of the rest, so the watch takes that step no more.
settled() {
  local entry=$1 swap=$2 step=$3
  shift 3
  jq -e --arg swap "$swap" --arg step "$step" --args '.swap == $swap and .step == $step and (.phase as $phase | $ARGS.positional | index($phase) != null)' "$@" <<< "$entry" > /dev/null
}

# Reports the step in hand with the phase $1, and logs it. A report that says what the last one said, but for its time, is not made again, so a step that waits does not write on every pass.
report() {
  local phase=$1 entry current
  log "${CURRENT_STEP} ${CURRENT_SWAP:-(no swap)}: ${phase}${MESSAGE:+: $MESSAGE}"
  entry=$(jq -cn --arg swap "$CURRENT_SWAP" --arg step "$CURRENT_STEP" --arg phase "$phase" --arg role "$(read_role)" --arg started "$STARTED_AT" \
    --arg at "$(host_now)" --arg message "$MESSAGE" --arg snapshot "$SNAPSHOT" \
    '{swap: $swap, step: $step, phase: $phase, role: $role, started_at: $started, at: $at, message: $message, snapshot: $snapshot}')
  if select_api_server && current=$(own_entry) && [ "$(jq -c 'del(.at)' <<< "$current")" = "$(jq -c 'del(.at)' <<< "$entry")" ]; then
    return 0
  fi
  if ! { select_api_server && kubectl_ patch configmap "$ACTUATOR_CONFIGMAP" --namespace "$ACTUATOR_NAMESPACE" --type merge \
    --patch "$(jq -cn --arg key "$NODE_NAME" --arg value "$entry" '{data: {($key): $value}}')" > /dev/null; }; then
    log "the outcome could not be reported in $ACTUATOR_NAMESPACE/$ACTUATOR_CONFIGMAP; a watching actuator takes the step again and reports it"
  fi
}

# Succeeds when the regrade Lease is held, unexpired, by ROLE_LEASE_HOLDER for the swap $1; otherwise says why in MESSAGE. A Lease that cannot be read authorises nothing, since a step must not start on a guess.
authorised() {
  local swap=$1 lease state holder held_for
  if ! select_api_server; then
    MESSAGE="no API server answers (tried: $(api_candidates_text)), so whether the regrade Lease is held for swap $swap cannot be told"
    return 1
  fi
  if ! lease=$(kubectl_ get lease "$LEASE_NAME" --namespace "$LEASE_NAMESPACE" --output json 2>&1); then
    MESSAGE="the regrade Lease cannot be read: $lease"
    return 1
  fi
  state=$(lease_state "$lease" "$ROLE_LEASE_HOLDER" "$(now_epoch)")
  if [ "$state" != mine ]; then
    holder=$(jq -r '.spec.holderIdentity // ""' <<< "$lease")
    case $state in
      free) MESSAGE="the regrade Lease is free, not held by $ROLE_LEASE_HOLDER" ;;
      expired) MESSAGE="the regrade Lease held by ${holder} has expired" ;;
      *) MESSAGE="the regrade Lease is held by ${holder}, not $ROLE_LEASE_HOLDER" ;;
    esac
    return 1
  fi
  held_for=$(jq -r --arg key "$SWAP_ID_ANNOTATION" '.metadata.annotations[$key] // ""' <<< "$lease")
  if [ "$held_for" != "$swap" ]; then
    MESSAGE="the regrade Lease is held for swap '${held_for}', not $swap"
    return 1
  fi
}

# The URL a promotion joins for the node $1, from the file the role writes with one line per server and eligible host, "<node name> <URL>".
join_url_of() {
  awk -v node="$1" '$1 == node { print $2; exit }' "$JOIN_URLS_FILE"
}

# The host-local part of a promotion: the role file first, so that neither unit's Restart=always can start the agent again from here on, then the join drop-ins (or none, which leaves the role's own join configuration to decide), then the agent unit stopped and the server unit started.
promote_local() {
  local url=${1:-}
  write_role server || { MESSAGE="the role file could not be written"; return "$EXIT_FAILED"; }
  if [ -n "$url" ]; then
    join_set "$url" || { MESSAGE="the join drop-ins could not be written"; return "$EXIT_FAILED"; }
  else
    join_clear || { MESSAGE="the join drop-ins could not be removed"; return "$EXIT_FAILED"; }
  fi
  start_server_unit || { MESSAGE="the server unit did not start"; return "$EXIT_FAILED"; }
  MESSAGE="the server unit started${url:+, joining $url}"
}

# The host-local part of finishing a demotion or a rollback: the role file names the agent (it already does after a step-down), the server unit is stopped, its regrading level forgotten, its datastore moved aside and its join drop-ins removed, and the agent unit started. The order is the role's demotion's: nothing of the server is touched while its unit could still be running.
demote_local() {
  write_role agent || { MESSAGE="the role file could not be written"; return "$EXIT_FAILED"; }
  stop_server_unit || { MESSAGE="the server unit did not stop"; return "$EXIT_FAILED"; }
  forget_level || { MESSAGE="the regrading level could not be removed"; return "$EXIT_FAILED"; }
  move_datastore_aside || { MESSAGE="the datastore could not be moved aside"; return "$EXIT_FAILED"; }
  join_clear || { MESSAGE="the join drop-ins could not be removed"; return "$EXIT_FAILED"; }
  start_agent_unit || { MESSAGE="the agent unit did not start"; return "$EXIT_FAILED"; }
  MESSAGE="the agent unit started, with the server's datastore moved aside"
}

# Runs $@ while holding the host's switch lock, and returns its status.
locked() {
  local status=0
  lock_switch || { MESSAGE="another role switch on this host held its lock for longer than ${OPERATION_TIMEOUT_SECONDS}s"; return "$EXIT_FAILED"; }
  "$@" || status=$?
  unlock_switch
  return "$status"
}

cmd_promote() {
  local swap=$1 join_node url
  authorised "$swap" || return "$EXIT_REFUSED"
  if [ "$(read_role)" = server ] && server_active; then
    STARTED_AT=$(last_start)
    MESSAGE="already runs as a server"
    return "$EXIT_DONE"
  fi
  provisioned || { MESSAGE="this host has no server configuration, so the role has not provisioned it to be a server"; return "$EXIT_REFUSED"; }
  if datastore_present; then
    MESSAGE="this host holds a datastore no demotion moved aside, and k3s would start that old member's etcd instead of joining as a new one"
    return "$EXIT_REFUSED"
  fi
  read_node || { MESSAGE="this host's Node cannot be read"; return "$EXIT_REFUSED"; }
  join_node=$(node_annotation "$JOIN_NODE_ANNOTATION")
  url=$(join_url_of "$join_node")
  [ -n "$url" ] || { MESSAGE="the node to join, '${join_node}', is not one $JOIN_URLS_FILE gives a URL for"; return "$EXIT_REFUSED"; }
  report running
  locked promote_local "$url"
}

# Named apart from the core's own step_down, cmd_watch and cmd_status, as watch_node and print_status are, so that sourcing the core never puts its functions in the place of the actuator's or the reverse.
# At etcd-only first, through regrading's own levels, so that the server keeps its vote and nothing else while the controller asks for its removal; then the role file names the agent, before the removal, so that a restart of the server unit after it (Restart=always, a reboot) cannot rejoin the cluster as a new learner; then the regrade watcher stops, which could otherwise step the server back up. A server whose role file already names the agent has stepped down already and is not restarted, since its unit's condition would now keep it down.
step_down_local() {
  local role
  role=$(read_role)
  case $role in
    agent)
      stop_regrade || { MESSAGE="the regrade watcher did not stop"; return "$EXIT_FAILED"; }
      MESSAGE="the role file already names the agent"
      return "$EXIT_DONE"
      ;;
    server) ;;
    *)
      MESSAGE="the role file names '${role}', which is not a role"
      return "$EXIT_REFUSED"
      ;;
  esac
  server_active || { MESSAGE="the server unit is not running, so there is no member here to step down"; return "$EXIT_FAILED"; }
  if [ "$(current_level)" != etcd-only ]; then
    enter_etcd_only || { MESSAGE="the server did not come back at etcd-only within ${OPERATION_TIMEOUT_SECONDS}s"; return "$EXIT_FAILED"; }
  fi
  write_role agent || { MESSAGE="the role file could not be written"; return "$EXIT_FAILED"; }
  stop_regrade || { MESSAGE="the regrade watcher did not stop"; return "$EXIT_FAILED"; }
  MESSAGE="at etcd-only, with the role file naming the agent"
}

cmd_step_down() {
  authorised "$1" || return "$EXIT_REFUSED"
  report running
  locked step_down_local
}

# Succeeds once this host's etcd member is gone, and otherwise says why not in MESSAGE. The tombstone is read first, since it needs no API: k3s writes it when etcd tells the member it was removed, which a member that was down when it was removed never hears, so the Node is asked then.
member_gone() {
  local member
  if ! datastore_present; then
    MESSAGE="no datastore is left"
    return 0
  fi
  if datastore_tombstoned; then
    MESSAGE="the datastore holds k3s's tombstone"
    return 0
  fi
  member=$(datastore_member)
  if ! { select_api_server && read_node; }; then
    MESSAGE="no API server answers and the datastore holds no tombstone, so whether the member was removed cannot be told yet"
    return 1
  fi
  if [ -n "$member" ] && [ "$(node_annotation "$REMOVED_ANNOTATION")" = "$member" ]; then
    MESSAGE="k3s recorded the removal of member $member"
    return 0
  fi
  # The controller's word, for the swap the Node is in, that etcd lists none of this host's members, from every remaining voter's own report: k3s's record names only the member it last removed and is cleared whenever the Node's etcd name annotation changes (pkg/etcd/member_controller.go), so it can disagree with the datastore's member while etcd lists neither.
  if [ -n "$(node_annotation "$SWAP_ANNOTATION")" ] && [ "$(node_annotation "$MEMBER_GONE_ANNOTATION")" = "$(node_annotation "$SWAP_ANNOTATION")" ]; then
    MESSAGE="the controller reports that etcd no longer lists this host's member, for swap $(node_annotation "$SWAP_ANNOTATION")"
    return 0
  fi
  if [ -z "$(node_label "$ETCD_LABEL")" ]; then
    MESSAGE="the Node carries no etcd label"
    return 0
  fi
  MESSAGE="etcd member ${member:-of this host} has not been removed yet"
  return 1
}

# Succeeds when this host's Node, as last read, asks it to step down to an agent: a swap's step-down, with the agent as the desired role. The controller removes the member once that step-down is done or, while the Node is not Ready, once it has failed or run past its time, so a server can be removed with its role file still naming the server.
asked_to_step_down() {
  [ -n "$(node_annotation "$SWAP_ANNOTATION")" ] && [ "$(node_annotation "$STEP_ANNOTATION")" = step-down ] && [ "$(node_label "$DESIRED_ROLE_LABEL")" = agent ]
}

# Succeeds when a host whose role file names the server is to be finished as an agent once its member is gone: its datastore holds k3s's tombstone (a host that was down while it was stepped down and removed never wrote the agent's role, and its units idle on the tombstone), or its Node asks it to step down (a step-down that failed leaves the server's role file, and the server unit's restarts would join etcd again as a new member, under a new name). member_gone still decides whether the member is gone, and finishing the demotion of a member that is gone can never cost quorum. $1 is "read" for the Node to be read first, anything else when the caller has just read it.
server_to_finish() {
  datastore_tombstoned && return 0
  if [ "$1" = read ]; then { select_api_server && read_node; } || return 1; fi
  asked_to_step_down
}

cmd_finish_demote() {
  local role
  role=$(read_role)
  if [ "$role" != agent ] && ! { [ "$role" = server ] && server_to_finish read; }; then
    MESSAGE="the role file names '${role}', not agent, the datastore holds no tombstone and the Node asks for no step-down, so no demotion of this host is under way"
    return "$EXIT_REFUSED"
  fi
  member_gone || return "$EXIT_NOT_YET"
  report running
  locked demote_local
}

cmd_rollback() {
  authorised "$1" || return "$EXIT_REFUSED"
  report running
  locked demote_local
}

take_named_snapshot() {
  SNAPSHOT=$(take_snapshot "fleet-role-$1" | paste -s -d , -) || { MESSAGE="k3s etcd-snapshot save failed"; return "$EXIT_FAILED"; }
  [ -n "$SNAPSHOT" ] || { MESSAGE="k3s etcd-snapshot save reported no snapshot file"; return "$EXIT_FAILED"; }
  MESSAGE="snapshot taken"
}

cmd_snapshot() {
  authorised "$1" || return "$EXIT_REFUSED"
  if [ "$(read_role)" != server ] || ! server_active; then
    MESSAGE="this host is not a running server, so it has no etcd to take a snapshot of"
    return "$EXIT_REFUSED"
  fi
  report running
  locked take_named_snapshot "$1"
}

# Takes the step $1 of the swap $2, reports how it ended unless it is not yet due, and returns its exit status.
run_step() {
  local step=$1 swap=$2 status=0
  CURRENT_STEP=$step
  CURRENT_SWAP=$swap
  MESSAGE=''
  STARTED_AT=''
  SNAPSHOT=''
  case $step in
    promote | step-down | rollback | snapshot)
      if ! valid_swap_id "$swap"; then
        MESSAGE="'${swap}' is not a swap id (<UTC %Y%m%dT%H%M%SZ>-<outgoing node>-<incoming node>)"
        status=$EXIT_REFUSED
      fi
      ;;
    finish-demote) ;;
    *)
      MESSAGE="'${step}' is not a step this actuator takes"
      status=$EXIT_REFUSED
      ;;
  esac
  if [ "$status" -eq 0 ]; then
    "cmd_${step//-/_}" "$swap" || status=$?
  fi
  case $status in
    "$EXIT_DONE") report "done" ;;
    "$EXIT_FAILED") report failed ;;
    "$EXIT_REFUSED") report refused ;;
    "$EXIT_NOT_YET") say "${step} ${swap:-(no swap)}: not yet: ${MESSAGE}" ;;
  esac
  return "$status"
}

# Logs $1 unless it is what the last call logged, so a watch that waits says so once.
LAST_SAID=''
say() {
  [ "$1" = "$LAST_SAID" ] || log "$1"
  LAST_SAID=$1
}

# The role a step asks the host to run, which the Node's desired-role label must agree with: a label and an annotation that disagree are a controller's mistake, and the step is refused rather than guessed at.
role_for_step() {
  case $1 in
    promote) echo server ;;
    step-down | rollback) echo agent ;;
  esac
}

# Succeeds when the step $3 of swap $2 needs no taking by the watch, given this node's report $1: it reported done, failed or refused, or it is a step-down and the finish of that demotion is reported, in any phase (the finish only follows a step-down, and its own report replaces the step-down's, so without this a finish that has begun would send the watch back to the step-down, which reports done over the finish and is then followed by the finish again, for ever).
step_taken() {
  local entry=$1 swap=$2 step=$3
  settled "$entry" "$swap" "$step" "done" failed refused && return 0
  [ "$step" = step-down ] && settled "$entry" "$swap" finish-demote running "done" failed
}

# Succeeds when the watch is to take (or retake) the finish of the demotion whose swap is $2, given this node's report $1 and its Node as the pass read it: the role file names the agent, or names the server and server_to_finish holds, the finish is not reported done, and something of the server is left, or the Node still carries this swap's step-down (so the controller is waiting for the finish and has not seen it reported). A finish whose report the API did not take leaves nothing of the server behind, so only the report is made again; the finish is idempotent, and member_gone still decides whether the member is gone.
finish_due() {
  local entry=$1 swap=$2 role
  role=$(read_role)
  [ "$role" = agent ] || { [ "$role" = server ] && server_to_finish read-already; } || return 1
  settled "$entry" "$swap" finish-demote "done" && return 1
  if server_active || datastore_present; then return 0; fi
  [ -n "$swap" ] && [ "$(node_annotation "$STEP_ANNOTATION")" = step-down ]
}

# One look at the Node: a snapshot it asks for, the step it asks for, then the finish of a demotion whose role file names the agent while something of the server is left or the finish is unreported. A step reported done, failed or refused for the swap is not taken again (the controller decides what follows a failure), except that a step whose report the API did not take is taken again, every step being idempotent and the report being made again when it ends; a finish is taken until it is reported done, since it can never cost quorum.
watch_pass() {
  local entry request swap step desired
  if ! select_api_server; then
    say "no API server answers (tried: $(api_candidates_text)); waiting"
    return 1
  fi
  read_node || return 1
  entry=$(own_entry) || return 1
  request=$(node_annotation "$SNAPSHOT_ANNOTATION")
  if [ -n "$request" ] && ! settled "$entry" "$request" snapshot "done" failed refused; then
    run_step snapshot "$request" || true
    read_node || return 1
    entry=$(own_entry) || return 1
  fi
  swap=$(node_annotation "$SWAP_ANNOTATION")
  step=$(node_annotation "$STEP_ANNOTATION")
  if [ -n "$swap" ] && [ -n "$step" ] && ! step_taken "$entry" "$swap" "$step"; then
    desired=$(node_label "$DESIRED_ROLE_LABEL")
    if [ -n "$(role_for_step "$step")" ] && [ "$desired" != "$(role_for_step "$step")" ]; then
      CURRENT_STEP=$step CURRENT_SWAP=$swap STARTED_AT='' SNAPSHOT=''
      MESSAGE="the Node's $DESIRED_ROLE_LABEL is '${desired}', which a $step does not go with"
      report refused
    else
      run_step "$step" "$swap" || true
    fi
    entry=$(own_entry) || return 1
  fi
  if finish_due "$entry" "$swap"; then
    run_step finish-demote "$swap" || true
  fi
}

watch_node() {
  log "watching node $NODE_NAME every ${POLL_SECONDS}s"
  while true; do
    watch_pass || true
    sleep "$POLL_SECONDS"
  done
}

print_status() {
  local entry
  log "role file: $(read_role)"
  log "server unit: $(server_active && echo running || echo stopped); agent unit: $(agent_active && echo running || echo stopped)"
  log "datastore: $(datastore_present && echo "in place, member $(datastore_member)$(datastore_tombstoned && echo ', tombstoned')" || echo none); moved aside: $(datastore_moved_aside && echo yes || echo no)"
  log "joins: $(join_current)"
  if select_api_server && entry=$(own_entry); then
    log "last report: $entry"
  else
    log "last report: no API server answers (tried: $(api_candidates_text))"
  fi
}

usage() {
  echo "usage: fleet-role-actuator watch|status|promote ID|step-down ID|rollback ID|snapshot ID|finish-demote|promote-local [JOIN_URL]|demote-local" >&2
  exit "$EXIT_REFUSED"
}

actuator_main() {
  local status=0
  case ${1:-} in
    watch) watch_node ;;
    status) print_status ;;
    promote | step-down | rollback | snapshot)
      [ $# -eq 2 ] || usage
      run_step "$1" "$2" || status=$?
      ;;
    finish-demote)
      [ $# -eq 1 ] || usage
      run_step finish-demote "" || status=$?
      ;;
    promote-local)
      [ $# -le 2 ] || usage
      locked promote_local "${2:-}" || status=$?
      log "promote-local: ${MESSAGE}"
      ;;
    demote-local)
      [ $# -eq 1 ] || usage
      locked demote_local || status=$?
      log "demote-local: ${MESSAGE}"
      ;;
    *) usage ;;
  esac
  return "$status"
}

actuator_main "$@"
