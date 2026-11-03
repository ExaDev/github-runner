# The Docker actuator for fleet-regrade, run by the regrade service of the cluster role's Compose file, for a server whose k3s runs in a container. The core, fleet-regrade-core.sh, and the part both k3s actuators share, fleet-regrade-k3s.sh, are placed ahead of this file (sourced, in that order, by the wrapper beside it, fleet-regrade.sh, as FLEET_REGRADE_ACTUATOR=docker in the environment file selects); that file says what the levels are and how they are recorded, and this one says where: in the k3s container, whose entrypoint (files/k3s/node-entrypoint.sh, which uses the same two paths) reads the level at every start.
#
# LEVEL_FILE is on the k3s data volume, so it survives the container being recreated as well as restarted. STARTED_FILE is on the container's tmpfs, so it exists only while the container runs. A level that needs k3s to start differently restarts the container, which is never created again, so the node password, kept in the container's writable layer or in a volume, is the one the cluster already holds.
#
# It needs docker and the variables fleet-regrade-k3s.sh needs, plus NODE_CONTAINER, the name of this server's k3s container. kubectl is the one in that container (KUBECTL in the environment file is `docker exec` of it), and KUBE_SERVERS must name only other servers' APIs: the container's own, at etcd-only, answers `apiserver disabled` on some servers.

LEVEL_FILE=${LEVEL_FILE:-/var/lib/rancher/k3s/regrade-level}
STARTED_FILE=${STARTED_FILE:-/run/regrade-level}

# Runs a command in the server's container, which fails while the container is not running.
on_server() {
  docker exec -i "$NODE_CONTAINER" "$@"
}

# The container is given the operation timeout to stop in, and its start time, as Docker records it, is the time of the restart.
restart_server() {
  docker restart --time "$OPERATION_TIMEOUT_SECONDS" "$NODE_CONTAINER" > /dev/null || return 1
  RESTARTED_AT=$(docker inspect --format '{{.State.StartedAt}}' "$NODE_CONTAINER")
}

# Only what the container logged since it started counts: the line from before the restart does not.
etcd_serving() {
  grep -Fq "$ETCD_READY_LOG" <<< "$(docker logs --since "$RESTARTED_AT" "$NODE_CONTAINER" 2>&1)"
}

# Restarting the container stops everything in it, its pods' processes included, so a level needs nothing beyond the shared steps.
apply_level() {
  k3s_apply_level "$1"
}
