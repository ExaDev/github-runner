# Tests for the Docker actuator of fleet-regrade (files/regrade/fleet-regrade-docker.sh): which level the node is at, and how a level is entered. Run as `bash tests/regrade/regrade-docker.sh <path to fleet-regrade-core.sh> <path to fleet-regrade-docker.sh>`. docker is a stand-in that keeps one container in a directory (tests/regrade/fake-docker.sh) and kubectl is the lease test's (tests/regrade/fake-kubectl.sh), so nothing here touches a container or a cluster. The core calls the functions defined here by name, and reads the variables set here, so shellcheck sees them as unused.

# shellcheck disable=SC2329,SC2034
set -euo pipefail

core=${1:?usage: regrade-docker.sh <core> <actuator>}
actuator=${2:?usage: regrade-docker.sh <core> <actuator>}
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/bin" "$work/node" "$work/cluster"
cp "$here/fake-docker.sh" "$work/bin/docker"
cp "$here/fake-kubectl.sh" "$work/bin/kubectl"
export PATH="$work/bin:$PATH" FAKE_NODE=$work/node FAKE_DIR=$work/cluster

cat > "$work/env" <<ENV
PRESSURE_THRESHOLD=10
AVAILABLE_FLOOR_PERCENT=15
SAMPLE_SECONDS=30
SUSTAIN_SECONDS=120
COOLDOWN_SECONDS=900
OPERATION_TIMEOUT_SECONDS=2
LEASE_DURATION_SECONDS=600
NODE_NAME=testnode
CONTROL_PLANE_TAINT=node-role.kubernetes.io/control-plane=true:NoSchedule
NODE_CONTAINER=test-k3s
KUBECTL="docker exec -i test-k3s kubectl"
KUBECONFIG_PATH=/etc/rancher/k3s/k3s.yaml
KUBE_SERVER=https://other.example.test:6443
LEVEL_LABEL=example.test/regrade-level
ENV
export FLEET_REGRADE_ENV=$work/env LEVEL_FILE=$work/node/level-file STARTED_FILE=$work/node/started-file

# shellcheck source=/dev/null
. "$here/testlib.sh"
# shellcheck source=/dev/null
. "$core"
# shellcheck source=/dev/null
. "$actuator"
POLL_SECONDS=1

# A container that is running at a level, with its etcd up and its node Ready on a heartbeat after its start.
container_at() {
  rm -f "$FAKE_NODE"/* "$FAKE_DIR"/*
  unset FAKE_RESTART_FAILS FAKE_NO_ETCD FAKE_NODE_STALE FAKE_NODE_NOT_READY
  touch "$FAKE_NODE/running"
  echo "$1" > "$STARTED_FILE"
  echo "$1" > "$LEVEL_FILE"
  echo '2026-01-01T00:00:00Z' > "$FAKE_NODE/started-at"
  echo '2026-01-01T00:00:00Z ETCD server is now running' > "$FAKE_NODE/log"
  echo '2026-01-01T00:00:05Z' > "$FAKE_NODE/lease-renewed"
  echo True > "$FAKE_NODE/node-ready"
  printf '{"items":[{"metadata":{"name":"peer-a"},"status":{"conditions":[{"type":"Ready","status":"True"}]}},{"metadata":{"name":"testnode"},"status":{"conditions":[{"type":"Ready","status":"True"}]}}]}\n' > "$FAKE_DIR/nodes.json"
}
restarts() { grep -c '^restart' "$FAKE_NODE/calls" || true; }
# Whether the first call matching $1 was made before the last one matching $2.
call_before() {
  local first last
  first=$(grep -n -- "$1" "$FAKE_NODE/calls" | head -n 1 | cut -d: -f1)
  last=$(grep -n -- "$2" "$FAKE_NODE/calls" | tail -n 1 | cut -d: -f1)
  [ -n "$first" ] && [ -n "$last" ] && [ "$first" -lt "$last" ]
}
requested() { cat "$LEVEL_FILE"; }
started_at_level() { cat "$STARTED_FILE"; }

check "the levels, most capable first" test "$(levels | tr '\n' ' ')" = "full control-plane etcd-only "

# Which level the node is at is what its entrypoint recorded for the running container.
for level in full control-plane etcd-only; do
  container_at "$level"
  check "current_level reports $level" test "$(current_level)" = "$level"
done
container_at full
rm "$STARTED_FILE" "$LEVEL_FILE"
check "current_level is full for a container whose entrypoint predates regrading" test "$(current_level)" = full
container_at full
rm "$STARTED_FILE"
check "current_level is unknown with a requested level but no record of what started" test "$(current_level)" = unknown
container_at full
echo unheard-of > "$STARTED_FILE"
check "current_level is unknown for a record that is no level" test "$(current_level)" = unknown
container_at full
rm "$FAKE_NODE/running"
check "current_level is unknown while the container is not running" test "$(current_level)" = unknown
container_at full
echo etcd-only > "$LEVEL_FILE"
check "current_level reads what runs, not what was last requested" test "$(current_level)" = full

# Between the levels that run the same k3s nothing restarts: the level is recorded as requested and as running.
container_at full
apply_level control-plane
check "full to control-plane restarts nothing" test "$(restarts)" = 0
check "full to control-plane is recorded as requested" test "$(requested)" = control-plane
check "full to control-plane is recorded as running" test "$(started_at_level)" = control-plane
apply_level full
check "control-plane to full restarts nothing" test "$(restarts)" = 0
check "control-plane to full is recorded" test "$(requested)-$(started_at_level)" = full-full

# The Node is labelled with the level it is taken to, before the level is entered, and unlabelled on the way back to the first.
container_at full
apply_level control-plane
check "stepping down labels the Node with the level" grep -Fq "label node testnode example.test/regrade-level=control-plane --overwrite" "$FAKE_DIR/calls"
apply_level full
check "returning to the first level takes the label off" grep -Fq "label node testnode example.test/regrade-level-" "$FAKE_DIR/calls"
container_at control-plane
apply_level etcd-only
check "the label is on before the container restarts" call_before "label node testnode example.test/regrade-level=etcd-only" "^restart"
container_at full
label_fails() { (kubectl_() { return 1; }; apply_level control-plane > /dev/null 2>&1); }
check_not "a label that cannot be put on fails the step" label_fails
check "and nothing was recorded" test "$(requested)" = full

# Entering etcd-only restarts the container once the level is recorded for its entrypoint to read, and waits for etcd.
container_at control-plane
apply_level etcd-only
check "control-plane to etcd-only restarts the container once" test "$(restarts)" = 1
check "the restart is given the operation timeout to stop in" grep -Fqx "restart --time 2 test-k3s" "$FAKE_NODE/calls"
check "the level is recorded before the restart" call_before "level-file" "^restart"
check "the entrypoint started at etcd-only" test "$(started_at_level)" = etcd-only
check "current_level follows" test "$(current_level)" = etcd-only

# Leaving it restarts the container and also waits for the node to report Ready on a heartbeat from the new container.
container_at etcd-only
echo '2026-01-01T00:00:05Z' > "$FAKE_NODE/lease-renewed"
apply_level control-plane
check "etcd-only to control-plane restarts the container once" test "$(restarts)" = 1
check "the entrypoint started at control-plane" test "$(started_at_level)" = control-plane
container_at etcd-only
apply_level full
check "etcd-only to full restarts the container once" test "$(restarts)" = 1
check "the entrypoint started at full" test "$(started_at_level)" = full

# A restart that did not bring the server back fails the step, and says what is running.
container_at full
export FAKE_NO_ETCD=1
no_etcd() { apply_level etcd-only > "$work/out" 2>&1; }
check_not "etcd that never serves fails the step" no_etcd
check "and the failure says so" grep -Fq "etcd did not come back within 2s" "$work/out"
check "and the node is left at the level it restarted at" test "$(current_level)" = etcd-only

container_at etcd-only
export FAKE_NODE_STALE=1
node_stale() { apply_level full > "$work/out" 2>&1; }
check_not "a kubelet that never renews its lease fails the step" node_stale
check "and the failure says so" grep -Fq "the node did not report Ready within 2s" "$work/out"

container_at etcd-only
export FAKE_NODE_NOT_READY=1
node_not_ready() { apply_level full > "$work/out" 2>&1; }
check_not "a node that is not Ready fails the step" node_not_ready

container_at full
export FAKE_RESTART_FAILS=1
restart_fails() { apply_level etcd-only > "$work/out" 2>&1; }
check_not "a restart docker refuses fails the step" restart_fails

# Waiting for etcd counts only what was logged since the container started: the line from before the restart does not satisfy it.
container_at full
export FAKE_NO_ETCD=1
echo '2026-01-01T00:00:00Z ETCD server is now running' > "$FAKE_NODE/log"
stale_log() { apply_level etcd-only > "$work/out" 2>&1; }
check_not "etcd's line from before the restart does not count" stale_log

# A node that is not running at a known level is not touched.
container_at full
rm "$FAKE_NODE/running"
refused() { apply_level etcd-only > "$work/out" 2>&1; }
check_not "a level cannot be applied to a node that is not running" refused
check "and nothing is recorded" test "$(requested)" = full
check "and the failure says why" grep -Fq "not running at a known level" "$work/out"

# Through the core: down and up by hand, with the coordination real (the lease and the peers' readiness), and the workloads shed before a level is applied and restored only on reaching the first level.
container_at full
cmd_down > /dev/null
check "down by hand reaches control-plane" test "$(current_level)" = control-plane
cmd_down > /dev/null
check "down by hand reaches etcd-only" test "$(current_level)" = etcd-only
check "the drain came before the restart that switched the API server off" call_before " drain " "^restart"
cmd_up > /dev/null
check "up by hand leaves etcd-only for control-plane" test "$(current_level)" = control-plane
check_not "which does not yet restore the workloads" grep -Fq "uncordon" "$FAKE_DIR/calls"
cmd_up > /dev/null
check "up by hand reaches full" test "$(current_level)" = full
check "which restores the workloads" test "$(grep -c 'uncordon testnode' "$FAKE_DIR/calls")" = 1

finish
