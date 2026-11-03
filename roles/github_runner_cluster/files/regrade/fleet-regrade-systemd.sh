# The systemd actuator for fleet-regrade, run by the fleet-regrade unit of a native server (github_runner_cluster_node_mode: native), whose k3s runs on the host as the systemd unit K3S_SERVICE. The core, fleet-regrade-core.sh, and the part both k3s actuators share, fleet-regrade-k3s.sh, are placed ahead of this file (sourced, in that order, by the wrapper beside it, fleet-regrade.sh, as FLEET_REGRADE_ACTUATOR=systemd in the environment file selects); that file says what the levels are and how they are recorded, and this one says where: on the host, where the unit's ExecStartPre (files/native/write-regrade-level.sh, given the same two paths by templates/native/k3s.service.j2) reads the level at every start and renders the server configuration's drop-in from it.
#
# LEVEL_FILE is on k3s's data directory, so it survives the unit restarting and the host rebooting. STARTED_FILE is in the unit's runtime directory, which systemd keeps across a restart of the unit (RuntimeDirectoryPreserve=restart) and removes when the unit stops, so it exists only while k3s runs.
#
# The unit's KillMode=process stops only k3s itself when it restarts: the containerd shims of the node's pods keep running, as they do across any restart of a native node. k3s at etcd-only starts no agent, and so no kubelet and no containerd, to stop them or take them up again, and the pods that survived the drain (DaemonSet pods) would keep running and keep their memory, which defeats the step. Entering etcd-only therefore stops the shims that are left, with everything they run, as k3s's own k3s-killall.sh does; leaving it starts the agent, whose kubelet starts the pods the node should run.
#
# It needs systemctl, journalctl and ps, and the variables fleet-regrade-k3s.sh needs, plus K3S_SERVICE and K3S_DATA_DIR. kubectl is k3s's own (KUBECTL in the environment file is `k3s kubectl`) with the admin kubeconfig k3s wrote while it ran whole, and KUBE_SERVERS must name only other servers' APIs: this server has none at etcd-only.

K3S_DATA_DIR=${K3S_DATA_DIR:-/var/lib/rancher/k3s}
LEVEL_FILE=${LEVEL_FILE:-$K3S_DATA_DIR/regrade-level}
STARTED_FILE=${STARTED_FILE:-/run/github-runner-k3s/regrade-level}

# Runs a command on the host while the server's unit is active, and fails without running it otherwise, as `docker exec` does for a container that is not running. A unit that is still starting is not active yet (Type=exec reports activating until its ExecStartPre commands have run), so a level is never read from a record its start has not written.
on_server() {
  systemctl is-active --quiet "$K3S_SERVICE" || return 1
  "$@"
}

# The time is read just before the restart, by the host's own clock, which the kubelet stamps its heartbeat with: a heartbeat later than it is from the new k3s. systemctl returns once the unit's ExecStartPre commands have run and k3s has been started, or fails when they could not run.
restart_server() {
  # shellcheck disable=SC2034 # Read by restart_into, in fleet-regrade-k3s.sh.
  RESTARTED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  systemctl restart "$K3S_SERVICE"
}

# Only what the unit's current run logged counts, read by its invocation id, which systemd gives every start of a unit: the line from the run before the restart has another. The journal is read whole before it is searched, since grep -q would stop reading at the first match and, under pipefail, fail a reader cut off by it.
etcd_serving() {
  local invocation
  invocation=$(systemctl show --property InvocationID --value "$K3S_SERVICE") || return 1
  [ -n "$invocation" ] || return 1
  grep -Fq "$ETCD_READY_LOG" <<< "$(journalctl --no-pager --output cat "_SYSTEMD_INVOCATION_ID=$invocation")"
}

# The process ids of the containerd shims k3s started for the node's pods, found as k3s-killall.sh (the script k3s's install.sh writes) finds them: a process whose command is one of the shim binaries k3s unpacks under its data directory, data/<hash>/bin/containerd-shim*.
pod_shims() {
  ps -e -o pid= -o args= | awk -v data="$K3S_DATA_DIR/data/" '
    index($2, data) == 1 && substr($2, length(data) + 1) ~ /^[^\/]+\/bin\/containerd-shim[^\/]*$/ { print $1 }'
}

# The process ids given as arguments and every process descended from them, as k3s-killall.sh's pstree walks the tree: a shim's children are the containers of its pod.
process_tree() {
  ps -e -o pid= -o ppid= | awk -v roots="$*" '
    BEGIN { count = split(roots, list, " "); for (i = 1; i <= count; i++) keep[list[i]] = 1 }
    { parent[$1] = $2 }
    END {
      do {
        grew = 0
        for (pid in parent) if (!(pid in keep) && (parent[pid] in keep)) { keep[pid] = 1; grew = 1 }
      } while (grew)
      for (pid in keep) print pid
    }'
}

# Stops the shims that outlived k3s's agent, with everything they run, by SIGKILL as k3s-killall.sh does, and returns once none is left, failing when that takes longer than the operation timeout. The signals are not checked one by one: a process that exits between the listing and the signal is gone either way, and what decides the result is whether any shim is left.
stop_pod_shims() {
  local shims deadline
  shims=$(pod_shims)
  [ -n "$shims" ] || return 0
  log "stopping the pods left without a kubelet at etcd-only: containerd shims $(echo "$shims" | tr '\n' ' ')"
  # shellcheck disable=SC2046,SC2086 # One process id per word, on purpose.
  kill -KILL $(process_tree $shims) 2> /dev/null || true
  deadline=$((SECONDS + OPERATION_TIMEOUT_SECONDS))
  until [ -z "$(pod_shims)" ]; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      log "containerd shims still running after ${OPERATION_TIMEOUT_SECONDS}s: $(pod_shims | tr '\n' ' ')"
      return 1
    fi
    sleep "$POLL_SECONDS"
  done
}

apply_level() {
  k3s_apply_level "$1" || return 1
  [ "$1" != etcd-only ] || stop_pod_shims
}
