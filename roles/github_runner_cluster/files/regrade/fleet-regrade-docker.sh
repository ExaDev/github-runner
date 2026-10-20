# The Docker actuator for fleet-regrade, run by the regrade service of the cluster role's Compose file. The core, fleet-regrade-core.sh, is placed ahead of this file (concatenated, or sourced by the wrapper beside it); this file supplies the three functions of the actuator contract that the core describes, and runs it.
#
# The levels are ways of running the one k3s container of this server. full and control-plane run the same k3s and differ only in the cordon and taint the core puts on the Node, so entering either from the other restarts nothing. etcd-only runs k3s with its API server, controller manager, scheduler and agent turned off: the server keeps its etcd member, and its vote, and nothing else, which it needs the container's entrypoint to start differently, so entering or leaving it restarts the container.
#
# A level is recorded in the container, where the entrypoint (files/k3s/node-entrypoint.sh, which uses the same two paths) reads it at every start. LEVEL_FILE is on the k3s data volume: it survives the container being recreated and the host restarting, so a node stepped down stays stepped down until it is stepped up, and an Ansible run, which changes nothing the container is created from, cannot undo a step. STARTED_FILE is on the container's tmpfs: it exists only while the container runs and holds the level its k3s started at, which is what current_level reports, since k3s hides its arguments from /proc and a level cannot be read back from the process. The container is restarted, never created again, so the node password, kept in the container's writable layer or in a volume, is the one the cluster already holds.
#
# This differs from the NixOS actuator in one respect the core's contract allows: a reboot does not return the node to the first level. A server that restarts at etcd-only comes back at etcd-only, with the cordon the core put on its Node still in place, so the cluster's view and the node's agree.
#
# The Node carries a label, LEVEL_LABEL, with the level as its value, from the first step down until the node is back at the first level. Whatever judges nodes by their readiness can leave a stepped-down server out: at etcd-only it has no kubelet, so it is not Ready by design.
#
# It needs docker and the variables the core reads from the environment file, plus NODE_CONTAINER, the name of this server's k3s container, and LEVEL_LABEL. kubectl is the one in that container (KUBECTL in the environment file is `docker exec` of it), and KUBE_SERVER must name another server's API: the container's own, at etcd-only, answers `apiserver disabled` on some servers.

LEVEL_FILE=${LEVEL_FILE:-/var/lib/rancher/k3s/regrade-level}
STARTED_FILE=${STARTED_FILE:-/run/regrade-level}

# How often a restarted container is asked whether it is back, in seconds: the core's own pace for waiting on the API server.
POLL_SECONDS=2

# The line k3s logs once its embedded etcd is serving, at every level and on every start. Waiting for it is how a restart is known to have returned the server's vote, which the core's lease relies on: it is released as soon as apply_level returns, and another server that stepped before this one's etcd was back would stack on top of the outage.
ETCD_READY_LOG='ETCD server is now running'

levels() {
  printf '%s\n' full control-plane etcd-only
}

# Runs a command in the server's container.
in_node() {
  docker exec -i "$NODE_CONTAINER" "$@"
}

# The level the running k3s started at, as its entrypoint recorded it; "unknown" while the container is not running, or when the record is none of the levels (for example one written by another version, or a restart in progress). A running container with neither a record nor a requested level is one whose entrypoint is from before regrading, which could only start k3s whole, so it is at full; its next restart picks up the new entrypoint.
current_level() {
  local level
  # shellcheck disable=SC2016 # The expansions are for the shell inside the container, which is given the two paths as arguments.
  level=$(in_node sh -c 'if [ -r "$1" ]; then cat "$1"; elif [ ! -e "$2" ]; then echo full; fi' sh "$STARTED_FILE" "$LEVEL_FILE" 2> /dev/null) || {
    echo unknown
    return 0
  }
  if levels | grep -Fqx -- "$level"; then
    echo "$level"
  else
    echo unknown
  fi
}

# Puts the label for the level $1 on the Node, or takes it off for the first level. Done before the level is entered, so the label is never missing from a node that has stepped down; the cluster API is reached through another server, which is up at every level.
label_node() {
  if [ "$1" = "$(levels | head -n 1)" ]; then
    kubectl_ label node "$NODE_NAME" "${LEVEL_LABEL}-"
  else
    kubectl_ label node "$NODE_NAME" "${LEVEL_LABEL}=$1" --overwrite
  fi
}

# Replaces the file at $1, inside the container, with the level $2, atomically, so a reader never sees half a name.
write_level() {
  # shellcheck disable=SC2016 # The expansions are for the shell inside the container, which is given the level and the path as arguments.
  in_node sh -c 'umask 077 && printf "%s\n" "$1" > "$2.new" && mv "$2.new" "$2"' sh "$2" "$1"
}

# How k3s runs at a level: as a server (every level but etcd-only), or as an etcd member alone. Only a change between the two changes the arguments k3s starts with, so only that needs a restart.
k3s_mode() {
  if [ "$1" = etcd-only ]; then
    echo etcd
  else
    echo server
  fi
}

# Whether the node has registered again after the container started at $1: it reports Ready and its kubelet has renewed the node lease since. Ready alone is not proof, since the API keeps the old container's Ready condition until the node controller's grace period runs out. Both are UTC, so their first nineteen characters, to the second, order as text.
node_ready_since() {
  local started=$1 ready renewed
  ready=$(kubectl_ get node "$NODE_NAME" --output 'jsonpath={.status.conditions[?(@.type=="Ready")].status}' 2> /dev/null) || return 1
  renewed=$(kubectl_ get lease --namespace kube-node-lease "$NODE_NAME" --output 'jsonpath={.spec.renewTime}' 2> /dev/null) || return 1
  [ "$ready" = True ] && [[ ${renewed:0:19} > "${started:0:19}" ]]
}

# Restarts the container and returns once the server is back as the level $1 runs it: etcd serving again, and, when the level runs the agent, the node Ready on a heartbeat from the new container. Fails when that takes longer than the operation timeout, which leaves the node as it is, for current_level to report.
restart_node() {
  local to=$1 started deadline
  docker restart --time "$OPERATION_TIMEOUT_SECONDS" "$NODE_CONTAINER" > /dev/null || return 1
  started=$(docker inspect --format '{{.State.StartedAt}}' "$NODE_CONTAINER") || return 1
  deadline=$((SECONDS + OPERATION_TIMEOUT_SECONDS))
  until grep -Fq "$ETCD_READY_LOG" <<< "$(docker logs --since "$started" "$NODE_CONTAINER" 2>&1)"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      log "etcd did not come back within ${OPERATION_TIMEOUT_SECONDS}s"
      return 1
    fi
    sleep "$POLL_SECONDS"
  done
  [ "$(k3s_mode "$to")" = server ] || return 0
  until node_ready_since "$started"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      log "the node did not report Ready within ${OPERATION_TIMEOUT_SECONDS}s"
      return 1
    fi
    sleep "$POLL_SECONDS"
  done
}

apply_level() {
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
    restart_node "$to"
  fi
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  regrade_main "$@"
fi
