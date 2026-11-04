# The Docker platform of fleet-role-actuator, for a host whose k3s runs under the cluster role's Compose project (github_runner_cluster_node_mode: docker): the server's k3s service in the container SERVER_CONTAINER (profile server) or the agent's k3s-agent service in AGENT_CONTAINER (profile agent), each idling unless the role file in the project's role directory (ROLE_DIR, fleet-role.d, which docker-compose.fleet-role.yml mounts into both) names its role (files/k3s/node-entrypoint.sh). It runs as the role-actuator service of the same project (templates/docker-compose.role-actuator.yml.j2), in a container with the Docker socket, the project directory at its own absolute path (so that the paths a Compose command passes the daemon are the host's) and the server's k3s data volume at K3S_DATA_DIR, where the server's container mounts it too. fleet-role-actuator.sh sources fleet-regrade-core.sh, fleet-regrade-k3s.sh and fleet-regrade-docker.sh ahead of this file, which supply kubectl_, the Lease reader, write_level, restart_into and current_level, and then calls only the functions of the platform contract, which fleet-role-actuator-systemd.sh's header sets out and this file supplies for containers: a "unit" there is a service's container here.
#
# THE IDENTITY. The two services keep their node password (/etc/rancher/node) and their Tailscale state (/var/lib/tailscale) in volumes of their own, so a switch copies both from the stopped container of the role the host leaves into the volumes of the role it takes, through files/node-identity.sh, before that role's container starts: the node keeps its name and node password in the cluster and its device and address on the tailnet. A side whose container was never created has nothing to copy, and its volumes still hold the identity the host started with.
#
# THE API. This container is on no tailnet, and the node containers are: kubectl runs in whichever node container is running with Tailscale up (node_kubectl, the KUBECTL the settings name), given the actuator's own kubeconfig on its standard input, so the token is on no disk of that container and in no command line. While neither runs, partway through a switch, nothing reaches the API, and the watch reports the step once one does.
#
# It needs docker with its Compose plugin, flock and date, besides what fleet-regrade-docker.sh needs, and the settings named below, which the role writes to the actuator's environment file (templates/role-actuator.env.j2).

: "${PROJECT_DIR:?PROJECT_DIR must be set}" "${COMPOSE_PROJECT:?COMPOSE_PROJECT must be set}" "${ROLE_DIR:?ROLE_DIR must be set}" "${STATE_DIR:?STATE_DIR must be set}"
: "${SERVER_CONTAINER:?SERVER_CONTAINER must be set}" "${AGENT_CONTAINER:?AGENT_CONTAINER must be set}" "${REGRADE_CONTAINER:?REGRADE_CONTAINER must be set}"

SERVER_DIR=$K3S_DATA_DIR/server
ROLE_FILE=$ROLE_DIR/role
JOIN_FILE=$ROLE_DIR/join-url
SWITCH_LOCK=$STATE_DIR/fleet-role.lock
LAST_START_FILE=$STATE_DIR/fleet-role-started-at
# lib is fleet-role-actuator.sh's own directory, where the role places node-identity.sh beside it.
# shellcheck disable=SC2154 # Set by fleet-role-actuator.sh, which sources this file.
IDENTITY=$lib/node-identity.sh
# The Tailscale daemon's socket in a node container, as files/k3s/node-entrypoint.sh starts it: present only while that container's k3s runs, never in one that idles.
TAILSCALE_SOCKET=/var/run/tailscale/tailscaled.sock

# The Compose service, profile and identity volume keys of each role, as files/docker-compose.yml names them.
service_of() { if [ "$1" = server ]; then echo k3s; else echo k3s-agent; fi; }
container_of() { if [ "$1" = server ]; then echo "$SERVER_CONTAINER"; else echo "$AGENT_CONTAINER"; fi; }
volume_of() {
  local prefix
  if [ "$1" = server ]; then prefix=k3s; else prefix=k3s-agent; fi
  echo "${prefix}-$2"
}

# Runs docker compose on the host's project with the Compose files the role runs it with (COMPOSE_FILES, names relative to the project directory; none means docker-compose.yml alone, as Compose reads it by itself) and the profile $1.
compose() {
  local profile=$1 file files=()
  shift
  # shellcheck disable=SC2086 # COMPOSE_FILES is a list of file names, split on purpose.
  for file in ${COMPOSE_FILES:-}; do files+=(--file "$file"); done
  (cd "$PROJECT_DIR" && docker compose --project-directory "$PROJECT_DIR" --project-name "$COMPOSE_PROJECT" "${files[@]}" --profile "$profile" "$@")
}

container_state() {
  docker ps --all --filter "name=^/$1\$" --format '{{.State}}'
}

# The kubectl of a node container whose k3s runs, given this actuator's kubeconfig ($2, after --kubeconfig) on its standard input; kubectl_ calls it with every other argument after that.
node_kubectl() {
  local kubeconfig container
  [ "${1:-}" = --kubeconfig ] || { echo "node_kubectl: called without --kubeconfig" >&2; return 2; }
  kubeconfig=$2
  shift 2
  for container in "$SERVER_CONTAINER" "$AGENT_CONTAINER"; do
    [ "$(container_state "$container")" = running ] || continue
    docker exec "$container" test -S "$TAILSCALE_SOCKET" 2> /dev/null || continue
    docker exec -i "$container" kubectl --kubeconfig /dev/stdin "$@" < "$kubeconfig"
    return
  done
  echo "no node container of this host runs k3s, so the cluster's API cannot be reached from it" >&2
  return 1
}

read_role() {
  cat "$ROLE_FILE" 2> /dev/null || true
}

write_role() {
  printf '%s' "$1" > "$ROLE_FILE.new" && mv "$ROLE_FILE.new" "$ROLE_FILE"
}

# The role's .env holds both services' variables on a host it may switch, the server's TLS names among them (templates/env.j2), so a server can start from it.
provisioned() {
  grep -q '^K3S_TLS_SAN_LIST=.' "$PROJECT_DIR/.env" 2> /dev/null
}

server_active() {
  [ "$(container_state "$SERVER_CONTAINER")" = running ]
}

agent_active() {
  [ "$(container_state "$AGENT_CONTAINER")" = running ]
}

datastore_present() {
  [ -d "$SERVER_DIR/db" ]
}

datastore_tombstoned() {
  [ -e "$SERVER_DIR/db/etcd/tombstone" ]
}

datastore_member() {
  cat "$SERVER_DIR/db/etcd/name" 2> /dev/null || true
}

datastore_moved_aside() {
  [ -d "$SERVER_DIR/db.demoted" ]
}

host_now() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

# The entrypoint reads join-url at every start of the server's container and joins the server it names, whatever K3S_JOIN_SERVER_URL in .env says, and waits for that server's host on the mesh.
join_set() {
  printf '%s\n' "$1" > "$JOIN_FILE.new" && mv "$JOIN_FILE.new" "$JOIN_FILE"
}

join_current() {
  local url
  url=$(cat "$JOIN_FILE" 2> /dev/null || true)
  if [ -n "$url" ]; then echo "$url"; else echo "the join configuration the role wrote"; fi
}

join_clear() {
  rm -f "$JOIN_FILE"
}

# Copies the node's identity from the role $1's stopped container into the volumes of the role $2, when the role $1 has a container.
copy_identity() {
  local from=$1 to=$2 container kind
  container=$(container_of "$from")
  [ -n "$(container_state "$container")" ] || return 0
  for kind in node:/etc/rancher/node tailscale-data:/var/lib/tailscale; do
    bash "$IDENTITY" --stopped "$container" "${kind#*:}" "${COMPOSE_PROJECT}_$(volume_of "$to" "${kind%%:*}")" "$COMPOSE_PROJECT" "$(volume_of "$to" "${kind%%:*}")" || return 1
  done
}

# A server whose container did not come up has not joined anything, so the agent is brought back at once, with the role file naming it again and the join URL gone: unlike a native host, whose own Tailscale client keeps the API in reach, a Docker host with neither node container up cannot read the Lease that a rollback asks for, and would be left with nothing running.
start_server_unit() {
  if server_active; then return 0; fi
  compose agent stop "$(service_of agent)" || return 1
  if copy_identity agent server && record_start && compose server up --detach --no-deps "$(service_of server)"; then
    return 0
  fi
  log "the server's container did not come up; starting the agent's again"
  compose server stop "$(service_of server)" || true
  if ! { write_role agent && join_clear && compose agent up --detach --no-deps "$(service_of agent)"; }; then
    log "the agent's container did not come up either"
  fi
  return 1
}

stop_server_unit() {
  compose server stop "$(service_of server)"
}

start_agent_unit() {
  if agent_active; then return 0; fi
  copy_identity server agent || return 1
  record_start || return 1
  compose agent up --detach --no-deps "$(service_of agent)"
}

record_start() {
  STARTED_AT=$(host_now)
  printf '%s\n' "$STARTED_AT" > "$LAST_START_FILE"
}

last_start() {
  cat "$LAST_START_FILE" 2> /dev/null || true
}

# The datastore is on the server's k3s data volume, mounted here at the path the server's container mounts it, and is moved only while that container is stopped. One copy only, as on a native host.
move_datastore_aside() {
  datastore_present || return 0
  rm -rf "$SERVER_DIR/db.demoted" && mv "$SERVER_DIR/db" "$SERVER_DIR/db.demoted"
}

forget_level() {
  rm -f "$LEVEL_FILE"
}

# Restarting the container stops everything in it, its pods' processes included (fleet-regrade-docker.sh's apply_level), so etcd-only needs only the level and the restart.
enter_etcd_only() {
  write_level "$LEVEL_FILE" etcd-only || return 1
  restart_into etcd-only
}

# docker stop rather than Compose's stop, since the service runs only beside a server and its profile is not in every run of the role; a stopped container is not started again by its restart: unless-stopped.
stop_regrade() {
  [ "$(container_state "$REGRADE_CONTAINER")" = running ] || return 0
  docker stop "$REGRADE_CONTAINER" > /dev/null
}

# As on a native host, through the server's own supervisor, inside its container, where k3s finds its token in its data directory.
take_snapshot() {
  local output
  output=$(docker exec "$SERVER_CONTAINER" k3s etcd-snapshot save --etcd-server "https://127.0.0.1:${API_PORT:?API_PORT must be set}" --name "$1" 2>&1) || {
    log "$output" >&2
    return 1
  }
  sed -n 's/.*Snapshot \([^ ]*\) saved\..*/\1/p' <<< "$output"
}

lock_switch() {
  exec 9> "$SWITCH_LOCK" || return 1
  flock -w "$OPERATION_TIMEOUT_SECONDS" 9
}

unlock_switch() {
  exec 9>&-
}
