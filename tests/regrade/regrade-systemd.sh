# Tests for the systemd actuator of fleet-regrade (files/regrade/fleet-regrade-systemd.sh, over the part both k3s actuators share, fleet-regrade-k3s.sh) and the server unit's ExecStartPre that it relies on (files/native/write-regrade-level.sh): which level the node is at, how a level is entered and left in each direction, the waits after a restart, and the pods stopped at etcd-only. Run as `bash tests/regrade/regrade-systemd.sh <path to fleet-regrade-core.sh> <path to fleet-regrade-k3s.sh> <path to fleet-regrade-systemd.sh> <path to write-regrade-level.sh>`. systemctl, journalctl, date and k3s are stand-ins that keep one unit in a directory (tests/regrade/fake-systemctl.sh and the files beside it) and kubectl is the lease test's (tests/regrade/fake-kubectl.sh), so nothing here touches a unit or a cluster; the pods' containerd shims are real processes of this test's own, started from a stand-in data directory, and the processes it stops are only those. The core calls the functions defined here by name, and reads the variables set here, so shellcheck sees them as unused.

# shellcheck disable=SC2329,SC2034
set -euo pipefail

usage="usage: regrade-systemd.sh <core> <k3s actuator> <actuator> <write-regrade-level>"
core=${1:?$usage}
k3s_actuator=${2:?$usage}
actuator=${3:?$usage}
prestart=${4:?$usage}
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)

# The test's own shims, and the containers they run, stopped on the way out whatever the result.
cleanup() {
  local pids
  pids=$(cat "$work/spawned" 2> /dev/null || true)
  # shellcheck disable=SC2046,SC2086 # One process id per word, on purpose.
  [ -z "$pids" ] || kill -KILL $(process_tree $pids) 2> /dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

mkdir -p "$work/bin" "$work/host" "$work/cluster" "$work/data" "$work/config/server.yaml.d" "$work/run"
cp "$here/fake-systemctl.sh" "$work/bin/systemctl"
cp "$here/fake-journalctl.sh" "$work/bin/journalctl"
cp "$here/fake-date.sh" "$work/bin/date"
cp "$here/fake-k3s.sh" "$work/bin/k3s"
cp "$here/fake-kubectl.sh" "$work/bin/kubectl"
export PATH="$work/bin:$PATH" FAKE_HOST=$work/host FAKE_DIR=$work/cluster FAKE_RUNTIME_DIR=$work/run FAKE_SHIM_PREFIX=$work/data/data/

level_file=$work/data/regrade-level
dropin=$work/config/server.yaml.d/50-regrade.yaml
started_file=$work/run/regrade-level
# What the unit's ExecStartPre runs, as templates/native/k3s.service.j2 writes it.
export FAKE_PRESTART="$prestart $level_file $dropin $started_file"

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
FLEET_REGRADE_ACTUATOR=systemd
K3S_SERVICE=k3s
K3S_DATA_DIR=$work/data
LEVEL_FILE=$level_file
STARTED_FILE=$started_file
KUBECTL="k3s kubectl"
KUBECONFIG_PATH=/etc/rancher/k3s/k3s.yaml
KUBE_SERVERS="https://other.example.test:6443 https://another.example.test:6443"
LEVEL_LABEL=example.test/regrade-level
ENV
export FLEET_REGRADE_ENV=$work/env

# shellcheck source=/dev/null
. "$here/testlib.sh"
# shellcheck source=/dev/null
. "$core"
# shellcheck source=/dev/null
. "$k3s_actuator"
# shellcheck source=/dev/null
. "$actuator"
POLL_SECONDS=1
now_epoch() { echo 1000000000; }

# A server unit that is running at a level, started through its real ExecStartPre, with its etcd up and its node Ready on a heartbeat after its start.
unit_at() {
  rm -rf "${FAKE_HOST:?}"/* "${FAKE_DIR:?}"/* "$work/run"
  rm -f "$level_file" "$dropin"
  unset FAKE_RESTART_FAILS FAKE_NO_ETCD FAKE_NODE_STALE FAKE_NODE_NOT_READY
  echo "$1" > "$level_file"
  systemctl start k3s
  : > "$FAKE_HOST/calls"
  printf '{"items":[{"metadata":{"name":"peer-a"},"status":{"conditions":[{"type":"Ready","status":"True"}]}},{"metadata":{"name":"testnode"},"status":{"conditions":[{"type":"Ready","status":"True"}]}}]}\n' > "$FAKE_DIR/nodes.json"
}
restarts() { grep -c '^restart k3s$' "$FAKE_HOST/calls" || true; }
# Whether the first call matching $1 in the file $3 (the unit's calls by default) was made before the last one matching $2.
call_before() {
  local first last calls=${3:-$FAKE_HOST/calls}
  first=$(grep -n -- "$1" "$calls" | head -n 1 | cut -d: -f1)
  last=$(grep -n -- "$2" "$calls" | tail -n 1 | cut -d: -f1)
  [ -n "$first" ] && [ -n "$last" ] && [ "$first" -lt "$last" ]
}
requested() { cat "$level_file"; }
started_at_level() { cat "$started_file"; }
# The keys the drop-in sets, sorted, one space after each; comments are no keys.
dropin_keys() { grep -v '^#' "$dropin" | cut -d: -f1 | sort | tr '\n' ' '; }
etcd_only_keys="disable-agent disable-apiserver disable-controller-manager disable-scheduler "
no_leftovers() { [ -z "$(find "$work/data" "$work/config" "$work/run" -name '*.new' -o -name '*.tmp')" ]; }

check "the levels, most capable first" test "$(levels | tr '\n' ' ')" = "full control-plane etcd-only "

# The unit's ExecStartPre: what k3s is configured to start as, and the record of it.
unit_at full
rm "$level_file"
systemctl restart k3s
check "a server never stepped down starts at full" test "$(started_at_level)" = full
check "with a drop-in that sets nothing" test "$(dropin_keys)" = ""
for level in full control-plane; do
  unit_at "$level"
  check "$level starts k3s whole" test "$(dropin_keys)" = ""
  check "$level is recorded as started" test "$(started_at_level)" = "$level"
done
unit_at etcd-only
check "etcd-only turns off all but etcd" test "$(dropin_keys)" = "$etcd_only_keys"
check "the drop-in is valid YAML that turns each one on" test "$(grep -c ': true$' "$dropin")" = 4
check "etcd-only is recorded as started" test "$(started_at_level)" = etcd-only
unit_at full
echo unheard-of > "$level_file"
systemctl restart k3s
check "a level that is no level starts k3s whole" test "$(dropin_keys)" = ""
check "and is recorded as read, for the actuator to call unknown" test "$(current_level)" = unknown
check "the start writes each file in one rename" no_leftovers

# Which level the node is at is what the unit's start recorded for the running k3s.
for level in full control-plane etcd-only; do
  unit_at "$level"
  check "current_level reports $level" test "$(current_level)" = "$level"
done
unit_at full
rm "$started_file" "$level_file"
check "current_level is full for a server started from before regrading" test "$(current_level)" = full
unit_at full
rm "$started_file"
check "current_level is unknown with a requested level but no record of what started" test "$(current_level)" = unknown
unit_at etcd-only
systemctl stop k3s
check "current_level is unknown while the unit is not running" test "$(current_level)" = unknown
unit_at etcd-only
# A unit that is starting again is not active until its ExecStartPre has run, and its runtime directory, kept across a restart, still holds the record of the run before.
rm "$FAKE_HOST/active"
check "current_level is unknown while the unit is not active, whatever record its runtime directory still holds" test "$(current_level)" = unknown
unit_at full
echo etcd-only > "$level_file"
check "current_level reads what runs, not what was last requested" test "$(current_level)" = full

# Between the levels that run the same k3s nothing restarts: the level is recorded as requested and as running.
unit_at full
apply_level control-plane
check "full to control-plane restarts nothing" test "$(restarts)" = 0
check "full to control-plane is recorded as requested" test "$(requested)" = control-plane
check "full to control-plane is recorded as running" test "$(started_at_level)" = control-plane
apply_level full
check "control-plane to full restarts nothing" test "$(restarts)" = 0
check "control-plane to full is recorded" test "$(requested)-$(started_at_level)" = full-full
check "the records are written in one rename" no_leftovers

# The Node is labelled with the level it is taken to, before the level is entered, and unlabelled on the way back to the first.
unit_at full
apply_level control-plane
check "stepping down labels the Node with the level" grep -Fq "label node testnode example.test/regrade-level=control-plane --overwrite" "$FAKE_DIR/calls"
apply_level full
check "returning to the first level takes the label off" grep -Fq "label node testnode example.test/regrade-level-" "$FAKE_DIR/calls"
label_fails() { (kubectl_() { return 1; }; apply_level control-plane > /dev/null 2>&1); }
check_not "a label that cannot be put on fails the step" label_fails
check "and nothing was recorded" test "$(requested)" = full

# Entering etcd-only restarts the unit once the level is recorded for its ExecStartPre to read, and waits for etcd in the new run.
unit_at control-plane
apply_level etcd-only
check "control-plane to etcd-only restarts the unit once" test "$(restarts)" = 1
check "the level is recorded before the restart" test "$(requested)" = etcd-only
check "the new run started at etcd-only" test "$(started_at_level)" = etcd-only
check "with all but etcd turned off" test "$(dropin_keys)" = "$etcd_only_keys"
check "current_level follows" test "$(current_level)" = etcd-only
check "etcd is waited for in the new run's journal" grep -Fqx -- "--no-pager --output cat _SYSTEMD_INVOCATION_ID=2" "$FAKE_HOST/calls"
check "after the restart" call_before "^restart k3s$" "_SYSTEMD_INVOCATION_ID=2"

# Leaving it restarts the unit and also waits for the node to report Ready on a heartbeat from the new run.
for level in control-plane full; do
  unit_at etcd-only
  apply_level "$level"
  check "etcd-only to $level restarts the unit once" test "$(restarts)" = 1
  check "the new run started at $level" test "$(started_at_level)" = "$level"
  check "with k3s whole again" test "$(dropin_keys)" = ""
done
check "the node's heartbeat is read after the restart" call_before "^restart k3s$" "^kubectl get lease --namespace kube-node-lease testnode"

# The level stays across a restart by hand and across a stop and a start, since the requested level is read at every start.
unit_at control-plane
apply_level etcd-only
systemctl restart k3s
check "a restart by hand keeps etcd-only" test "$(current_level)-$(dropin_keys)" = "etcd-only-$etcd_only_keys"
systemctl stop k3s
systemctl start k3s
check "a stop and a start keep etcd-only" test "$(current_level)-$(dropin_keys)" = "etcd-only-$etcd_only_keys"

# A restart that did not bring the server back fails the step, and says what is running.
unit_at full
export FAKE_NO_ETCD=1
no_etcd() { apply_level etcd-only > "$work/out" 2>&1; }
check_not "etcd that never serves fails the step" no_etcd
check "and the failure says so" grep -Fq "etcd did not come back within 2s" "$work/out"
check "and the node is left at the level it restarted at" test "$(current_level)" = etcd-only

# Waiting for etcd counts only the run since the restart: the line the run before it logged does not satisfy it.
unit_at control-plane
check "the run before the restart did log that etcd was serving" grep -Fq "ETCD server is now running" "$FAKE_HOST/journal-1"
export FAKE_NO_ETCD=1
stale_journal() { apply_level etcd-only > "$work/out" 2>&1; }
check_not "etcd's line from the run before the restart does not count" stale_journal

unit_at etcd-only
export FAKE_NODE_STALE=1
node_stale() { apply_level full > "$work/out" 2>&1; }
check_not "a kubelet that never renews its lease fails the step" node_stale
check "and the failure says so" grep -Fq "the node did not report Ready within 2s" "$work/out"

unit_at etcd-only
export FAKE_NODE_NOT_READY=1
node_not_ready() { apply_level full > "$work/out" 2>&1; }
check_not "a node that is not Ready fails the step" node_not_ready

unit_at full
export FAKE_RESTART_FAILS=1
restart_fails() { apply_level etcd-only > "$work/out" 2>&1; }
check_not "a restart systemd refuses fails the step" restart_fails

# A node that is not running at a known level is not touched.
unit_at full
systemctl stop k3s
refused() { apply_level etcd-only > "$work/out" 2>&1; }
check_not "a level cannot be applied to a server whose unit is not running" refused
check "and nothing is recorded" test "$(requested)" = full
check "and the failure says why" grep -Fq "not running at a known level" "$work/out"

# The pods that outlive k3s's agent: shims started from the k3s data directory's shim binary, each running a container process, and one shim of the same name from elsewhere, which is not k3s's and is left alone.
mkdir -p "$work/data/data/0123abcd/bin" "$work/elsewhere/bin"
ln -sf "$(command -v bash)" "$work/data/data/0123abcd/bin/containerd-shim-runc-v2"
ln -sf "$(command -v bash)" "$work/elsewhere/bin/containerd-shim-runc-v2"
# Starts a shim from $1 whose container is a sleep of $2 seconds, a duration the test uses as that process's name, and returns once the container runs, which the shim says by writing the container's pid to a file named for it. The shim is left to the system's init, as a real one is, and its pid is kept for the clean-up.
spawn_shim() {
  local ready=$work/ready-$2 shim
  rm -f "$ready"
  shim=$( ("$1" -c "sleep $2 & echo \$! > $ready; wait" > /dev/null 2>&1 < /dev/null & echo $!))
  echo "$shim" >> "$work/spawned"
  until [ -s "$ready" ]; do
    kill -0 "$shim" 2> /dev/null || die "the stand-in shim for $2 exited before its container started"
    sleep 1
  done
}
# Read whole before it is searched: grep -q stops reading at the first match, which under pipefail would fail the listing.
running() {
  local processes
  processes=$(ps -e -o args=)
  grep -Eq "^(/bin/)?sleep $1\$" <<< "$processes"
}
spawn_pods() {
  spawn_shim "$work/data/data/0123abcd/bin/containerd-shim-runc-v2" 7001
  spawn_shim "$work/data/data/0123abcd/bin/containerd-shim-runc-v2" 7002
  spawn_shim "$work/elsewhere/bin/containerd-shim-runc-v2" 7003
}
spawn_pods
check "the stand-in shims are found as k3s's" test "$(pod_shims | wc -l | tr -d ' ')" = 2
unit_at full
apply_level control-plane
check "a step that keeps the agent stops no pod" running 7001
unit_at control-plane
apply_level etcd-only > "$work/out"
check "entering etcd-only stops k3s's shims" test -z "$(pod_shims)"
check_not "and the container of the first" running 7001
check_not "and the container of the second" running 7002
check "and leaves a shim that is not k3s's alone" running 7003
check "and says which it stopped" grep -Fq "stopping the pods left without a kubelet at etcd-only" "$work/out"
check "after the restart that left the agent off, across which they still ran" grep -Fqx "running shims: 2" "$FAKE_HOST/calls"
spawn_shim "$work/data/data/0123abcd/bin/containerd-shim-runc-v2" 7004
apply_level control-plane
check "leaving etcd-only stops nothing" running 7004
apply_level full
# A shim that does not go fails the step.
unit_at control-plane
kill_ignored() { (kill() { :; }; apply_level etcd-only > "$work/out" 2>&1); }
check_not "a shim that outlives the signal fails the step" kill_ignored
check "and the failure names it" grep -Fq "containerd shims still running after 2s" "$work/out"

# Through the core: down and up by hand, with the coordination real (the lease and the peers' readiness), the workloads shed before a level is applied and restored only on reaching the first level, and every call to the cluster through another server.
unit_at full
cmd_down > /dev/null
check "down by hand reaches control-plane" test "$(current_level)" = control-plane
cmd_down > /dev/null
check "down by hand reaches etcd-only" test "$(current_level)" = etcd-only
check "the drain came before the restart that switched the API server off" call_before "^kubectl drain " "^restart k3s$"
cmd_up > /dev/null
check "up by hand leaves etcd-only for control-plane" test "$(current_level)" = control-plane
check_not "which does not yet restore the workloads" grep -Fq "uncordon" "$FAKE_DIR/calls"
cmd_up > /dev/null
check "up by hand reaches full" test "$(current_level)" = full
check "which restores the workloads" test "$(grep -c 'uncordon testnode' "$FAKE_DIR/calls")" = 1
check_not "and nothing went to this server's own API" grep -q '^default ' "$FAKE_DIR/servers"

finish
