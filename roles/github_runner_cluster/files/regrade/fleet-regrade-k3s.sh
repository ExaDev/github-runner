# What the two k3s actuators of fleet-regrade share: the Docker actuator (fleet-regrade-docker.sh), for a server whose k3s runs in a container under Compose, and the systemd actuator (fleet-regrade-systemd.sh), for a native server whose k3s runs as a systemd unit on the host. The core, fleet-regrade-core.sh, is placed ahead of this file and the platform's actuator after it (sourced in that order by the wrapper beside them, fleet-regrade.sh); between them they supply the three functions of the actuator contract that the core describes.
#
# THE LEVELS. full and control-plane run the same k3s and differ only in the cordon and taint the core puts on the Node, so entering either from the other restarts nothing. etcd-only runs k3s with its API server, controller manager, scheduler and agent turned off: the server keeps its etcd member, and its vote, and nothing else, which needs k3s to start with other arguments, so entering or leaving it restarts k3s.
#
# THE RECORDS. A level is recorded in LEVEL_FILE, on k3s's data directory, which whatever starts k3s (the container's entrypoint, or the unit's ExecStartPre) reads at every start: it survives k3s restarting, its container being recreated and the host restarting, so a node stepped down stays stepped down until it is stepped up, and an Ansible run, which changes nothing the level is read from, cannot undo a step. A reboot does not return the node to the first level, which the core's contract allows: a server that restarts at etcd-only comes back at etcd-only, with the cordon the core put on its Node still in place, so the cluster's view and the node's agree. STARTED_FILE is on a tmpfs that exists only while k3s runs and holds the level its k3s started at, which whatever starts k3s writes and which is what current_level reports, since k3s hides its arguments from /proc and a level cannot be read back from the process. Both are replaced atomically, so a reader never sees half a name.
#
# The Node carries a label, LEVEL_LABEL, with the level as its value, from the first step down until the node is back at the first level. Whatever judges nodes by their readiness can leave a stepped-down server out: at etcd-only it has no kubelet, so it is not Ready by design.
#
# THE PLATFORM. The actuator after this file sets LEVEL_FILE and STARTED_FILE and defines four functions:
#
# on_server CMD... runs a command where this server's k3s runs, and fails without running it while k3s is not running.
#
# restart_server   restarts k3s, so that it starts at the level LEVEL_FILE names, and sets RESTARTED_AT to the time it restarted, as RFC 3339 in UTC by the clock the kubelet stamps its heartbeat with.
#
# etcd_serving     succeeds once k3s has logged ETCD_READY_LOG since the last restart_server.
#
# apply_level NAME the third function of the core's contract, which calls k3s_apply_level and does whatever else its platform needs.
#
# It needs the variables the core reads from the environment file, plus LEVEL_LABEL. kubectl reaches the cluster through another server's API (KUBE_SERVERS in the environment file), which is up at every level.

# How often a restarted k3s is asked whether it is back, in seconds: the core's own pace for waiting on the API server.
POLL_SECONDS=2

# The line k3s logs once its embedded etcd is serving, at every level and on every start. Waiting for it is how a restart is known to have returned the server's vote, which the core's lease relies on: it is released as soon as apply_level returns, and another server that stepped before this one's etcd was back would stack on top of the outage.
# shellcheck disable=SC2034 # Read by the actuator sourced after this file.
ETCD_READY_LOG='ETCD server is now running'

levels() {
  printf '%s\n' full control-plane etcd-only
}

# The level the running k3s started at, as STARTED_FILE records it; "unknown" while k3s is not running, or when the record is none of the levels (for example one written by another version, or a restart in progress). A running k3s with neither a record nor a requested level is one started from before regrading, which could only start k3s whole, so it is at full; its next restart records its level.
current_level() {
  local level
  # shellcheck disable=SC2016 # The expansions are for the shell on_server runs, which is given the two paths as arguments.
  level=$(on_server sh -c 'if [ -r "$1" ]; then cat "$1"; elif [ ! -e "$2" ]; then echo full; fi' sh "$STARTED_FILE" "$LEVEL_FILE" 2> /dev/null) || {
    echo unknown
    return 0
  }
  if levels | grep -Fqx -- "$level"; then
    echo "$level"
  else
    echo unknown
  fi
}

# Puts the label for the level $1 on the Node, or takes it off for the first level. Done before the level is entered, so the label is never missing from a node that has stepped down.
label_node() {
  if [ "$1" = "$(levels | head -n 1)" ]; then
    kubectl_ label node "$NODE_NAME" "${LEVEL_LABEL}-"
  else
    kubectl_ label node "$NODE_NAME" "${LEVEL_LABEL}=$1" --overwrite
  fi
}

# Replaces the file at $1, where k3s runs, with the level $2, atomically, so a reader never sees half a name.
write_level() {
  # shellcheck disable=SC2016 # The expansions are for the shell on_server runs, which is given the level and the path as arguments.
  on_server sh -c 'umask 077 && printf "%s\n" "$1" > "$2.new" && mv "$2.new" "$2"' sh "$2" "$1"
}

# How k3s runs at a level: as a server (every level but etcd-only), or as an etcd member alone. Only a change between the two changes the arguments k3s starts with, so only that needs a restart.
k3s_mode() {
  if [ "$1" = etcd-only ]; then
    echo etcd
  else
    echo server
  fi
}

# Whether the node has registered again after k3s restarted at $1: it reports Ready and its kubelet has renewed the node lease since. Ready alone is not proof, since the API keeps the old kubelet's Ready condition until the node controller's grace period runs out. Both are UTC, so their first nineteen characters, to the second, order as text.
node_ready_since() {
  local started=$1 ready renewed
  ready=$(kubectl_ get node "$NODE_NAME" --output 'jsonpath={.status.conditions[?(@.type=="Ready")].status}' 2> /dev/null) || return 1
  renewed=$(kubectl_ get lease --namespace kube-node-lease "$NODE_NAME" --output 'jsonpath={.spec.renewTime}' 2> /dev/null) || return 1
  [ "$ready" = True ] && [[ ${renewed:0:19} > "${started:0:19}" ]]
}

# Restarts k3s and returns once the server is back as the level $1 runs it: etcd serving again, and, when the level runs the agent, the node Ready on a heartbeat from the new k3s. Fails when that takes longer than the operation timeout, which leaves the node as it is, for current_level to report.
restart_into() {
  local to=$1 deadline
  restart_server || return 1
  deadline=$((SECONDS + OPERATION_TIMEOUT_SECONDS))
  until etcd_serving; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      log "etcd did not come back within ${OPERATION_TIMEOUT_SECONDS}s"
      return 1
    fi
    sleep "$POLL_SECONDS"
  done
  [ "$(k3s_mode "$to")" = server ] || return 0
  until node_ready_since "$RESTARTED_AT"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      log "the node did not report Ready within ${OPERATION_TIMEOUT_SECONDS}s"
      return 1
    fi
    sleep "$POLL_SECONDS"
  done
}

# Takes the node to the level $1: labels the Node, records the level for the next start, and either records it as the running level too (a change that runs the same k3s) or restarts k3s into it.
k3s_apply_level() {
  local to=$1 from
  from=$(current_level)
  if [ "$from" = unknown ]; then
    log "cannot apply $to: the node is not running at a known level"
    return 1
  fi
  label_node "$to" || return 1
  write_level "$LEVEL_FILE" "$to" || return 1
  if [ "$(k3s_mode "$from")" = "$(k3s_mode "$to")" ]; then
    write_level "$STARTED_FILE" "$to"
  else
    restart_into "$to"
  fi
}
