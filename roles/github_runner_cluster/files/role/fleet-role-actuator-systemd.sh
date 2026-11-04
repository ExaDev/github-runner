# The systemd platform of fleet-role-actuator, for a native host (github_runner_cluster_node_mode: native), whose k3s runs on the host as one of two systemd units, the server's K3S_SERVICE or the agent's K3S_AGENT_SERVICE, each started only while the role file ROLE_FILE names its role (files/native/fleet-role-is.sh, the units' ExecCondition). fleet-role-actuator.sh sources fleet-regrade-core.sh, fleet-regrade-k3s.sh and fleet-regrade-systemd.sh ahead of this file, which supply kubectl_, the Lease reader, write_level, restart_into, current_level and stop_pod_shims, and then defines the commands, the authority rules and the reports, which call only the functions below; a platform that runs k3s elsewhere (a container under Compose) supplies the same functions.
#
# THE PLATFORM CONTRACT. Each function returns non-zero when it could not do its part, and leaves the host as the part found it or as far as it got; none prints anything but its result, save take_snapshot, which logs what k3s said when the snapshot failed.
#
# read_role prints the role ROLE_FILE names, or nothing when there is none.
#
# write_role ROLE makes ROLE_FILE name ROLE, in one rename, so a unit's condition never reads half a name.
#
# provisioned succeeds when the host has the server's configuration a promotion starts k3s with.
#
# server_active succeeds while the server's k3s runs, and agent_active while the agent's does.
#
# datastore_present succeeds while the server's datastore (SERVER_DIR/db) is in place.
#
# datastore_tombstoned succeeds when k3s has written the tombstone that says etcd removed this server's member.
#
# datastore_member prints the name of the etcd member the datastore holds, or nothing.
#
# datastore_moved_aside succeeds while a datastore a demotion moved aside is kept.
#
# join_set URL makes the server join URL when it starts, whatever the configuration the role wrote says, and makes the unit wait for that URL's host on the mesh.
#
# join_clear takes join_set's files away again.
#
# join_current prints the URL join_set last set, or says that the join configuration the role wrote decides.
#
# start_server_unit stops and disables the agent's unit, then enables and starts the server's, setting STARTED_AT to the time just before the start.
#
# stop_server_unit stops and disables the server's unit.
#
# start_agent_unit enables and starts the agent's unit, setting STARTED_AT to the time just before the start.
#
# last_start prints the STARTED_AT of the last start either function made, so a step taken again reports the time of the start it found.
#
# host_now prints the time by the host's clock, as RFC 3339 in UTC to the second.
#
# move_datastore_aside moves SERVER_DIR/db to SERVER_DIR/db.demoted, replacing an older copy, when the datastore is in place.
#
# forget_level removes the regrading level the server was stepped to.
#
# enter_etcd_only takes the running server to the regrading level etcd-only and leaves nothing of its pods running.
#
# stop_regrade stops the fleet-regrade watcher, when it runs.
#
# take_snapshot NAME takes an on-demand etcd snapshot named NAME through the server's own supervisor and prints the names k3s gave the files it created, one per line.
#
# lock_switch / unlock_switch hold and release the host's role switch lock, so that a switch the role makes (tasks/native/demote.yml, server_turn.yml) and one the actuator makes never run together.
#
# It needs systemctl, flock and date, besides what fleet-regrade-systemd.sh needs, and the settings named below, which the role writes to the actuator's environment file (templates/role-actuator.env.j2).

: "${ROLE_FILE:?ROLE_FILE must be set}" "${CONFIG_DIR:?CONFIG_DIR must be set}" "${UNIT_DIR:?UNIT_DIR must be set}"
: "${K3S_SERVICE:?K3S_SERVICE must be set}" "${K3S_AGENT_SERVICE:?K3S_AGENT_SERVICE must be set}" "${REGRADE_SERVICE:?REGRADE_SERVICE must be set}"

SERVER_DIR=$K3S_DATA_DIR/server
# The drop-ins join_set writes. The server reads every *.yaml in its configuration's drop-in directory after server.yaml, in name order (dotDFiles in pkg/configfilearg/parser.go of k3s), so this one, named after the role's 10-join.yaml and before the 50-regrade.yaml write-regrade-level.sh writes, replaces 10-join.yaml's keys. systemd reads a unit's drop-ins after the unit file, so the Environment= of this one replaces the unit's own GITHUB_RUNNER_JOIN_HOST, which files/native/tailscale-up-wait.sh waits for.
JOIN_CONFIG=$CONFIG_DIR/server.yaml.d/20-fleet-role-join.yaml
JOIN_UNIT_DROPIN=$UNIT_DIR/$K3S_SERVICE.service.d/20-fleet-role-join.conf
SWITCH_LOCK=$K3S_DATA_DIR/fleet-role.lock
LAST_START_FILE=$K3S_DATA_DIR/fleet-role-started-at

read_role() {
  cat "$ROLE_FILE" 2> /dev/null || true
}

write_role() {
  printf '%s' "$1" > "$ROLE_FILE.new" && mv "$ROLE_FILE.new" "$ROLE_FILE"
}

provisioned() {
  [ -f "$CONFIG_DIR/server.yaml" ]
}

server_active() {
  systemctl is-active --quiet "$K3S_SERVICE"
}

agent_active() {
  systemctl is-active --quiet "$K3S_AGENT_SERVICE"
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

# The time by the host's clock, which the kubelet stamps its heartbeat with, so a heartbeat later than it is from the k3s started after it.
host_now() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

# Writes $2, and a line break, to the file $1 in one rename when the file does not already hold exactly that, and sets WROTE to 1 when it wrote and to nothing when it did not, so a caller knows whether systemd must reload.
write_if_changed() {
  local file=$1 content=$2
  WROTE=''
  [ "$(cat "$file" 2> /dev/null)" != "$content" ] || return 0
  mkdir -p "$(dirname "$file")" && printf '%s\n' "$content" > "$file.new" && mv "$file.new" "$file" || return 1
  WROTE=1
}

join_set() {
  local url=$1 host
  host=${url#*://}
  host=${host%%[:/]*}
  # cluster-init: false as well as server, since 10-join.yaml says cluster-init: true on a bootstrap host that was never a member, and a joining server must not start a cluster of its own.
  write_if_changed "$JOIN_CONFIG" "$(printf '# Written by fleet-role-actuator for a promotion: the voter this server joins, whatever 10-join.yaml says.\ncluster-init: false\nserver: "%s"' "$url")" || return 1
  write_if_changed "$JOIN_UNIT_DROPIN" "$(printf '# Written by fleet-role-actuator for a promotion: the host files/native/tailscale-up-wait.sh waits for, the host of the URL in %s.\n[Service]\nEnvironment="GITHUB_RUNNER_JOIN_HOST=%s"' "$JOIN_CONFIG" "$host")" || return 1
  [ -z "$WROTE" ] || systemctl daemon-reload
}

join_current() {
  local url
  url=$(sed -n 's/^server: "\(.*\)"$/\1/p' "$JOIN_CONFIG" 2> /dev/null || true)
  if [ -n "$url" ]; then echo "$url"; else echo "the join configuration the role wrote"; fi
}

join_clear() {
  local reload=''
  [ ! -e "$JOIN_UNIT_DROPIN" ] || reload=1
  rm -f "$JOIN_CONFIG" "$JOIN_UNIT_DROPIN" || return 1
  [ -z "$reload" ] || systemctl daemon-reload
}

start_server_unit() {
  systemctl disable --now "$K3S_AGENT_SERVICE" || return 1
  record_start || return 1
  systemctl enable --now "$K3S_SERVICE"
}

stop_server_unit() {
  systemctl disable --now "$K3S_SERVICE"
}

start_agent_unit() {
  record_start || return 1
  systemctl enable --now "$K3S_AGENT_SERVICE"
}

record_start() {
  STARTED_AT=$(host_now)
  printf '%s\n' "$STARTED_AT" > "$LAST_START_FILE"
}

last_start() {
  cat "$LAST_START_FILE" 2> /dev/null || true
}

# A copy, not a deletion: it holds the snapshots this server took (server/db/snapshots). One copy only, the one from the latest demotion, so repeated switches do not fill the disk.
move_datastore_aside() {
  datastore_present || return 0
  rm -rf "$SERVER_DIR/db.demoted" && mv "$SERVER_DIR/db" "$SERVER_DIR/db.demoted"
}

# The level is a server's, and the agent unit never reads it; a later promotion must start the server whole, not at a level it was stepped to in an earlier term as a server.
forget_level() {
  rm -f "$LEVEL_FILE"
}

# k3s at etcd-only starts no kubelet and so no containerd to stop the pods that survived the drain; their shims are stopped as fleet-regrade-systemd.sh's apply_level stops them.
enter_etcd_only() {
  write_level "$LEVEL_FILE" etcd-only || return 1
  restart_into etcd-only || return 1
  stop_pod_shims
}

stop_regrade() {
  systemctl is-active --quiet "$REGRADE_SERVICE" || return 0
  systemctl stop "$REGRADE_SERVICE"
}

# k3s etcd-snapshot save asks the server's supervisor (--etcd-server, this host's own API port) with the server's token, which it reads from its data directory when none is given (commandSetup in pkg/cli/etcdsnapshot/etcd_snapshot.go of k3s), and logs "Snapshot <name> saved." for each file the server created; the token is never on the command line.
take_snapshot() {
  local output
  output=$(k3s etcd-snapshot save --data-dir "$K3S_DATA_DIR" --etcd-server "https://127.0.0.1:${API_PORT:?API_PORT must be set}" --name "$1" 2>&1) || {
    log "$output" >&2
    return 1
  }
  sed -n 's/.*Snapshot \([^ ]*\) saved\..*/\1/p' <<< "$output"
}

# Waits for the lock for no longer than one operation, which is what a switch holding it takes at most.
lock_switch() {
  exec 9> "$SWITCH_LOCK" || return 1
  flock -w "$OPERATION_TIMEOUT_SECONDS" 9
}

unlock_switch() {
  exec 9>&-
}
