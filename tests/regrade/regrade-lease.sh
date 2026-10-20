# Tests for the cluster-wide coordination in the core of fleet-regrade (modules/fleet-regrade-core.sh): a transition starts only while the node holds the Lease and the other etcd-voting nodes are Ready. Run as `bash tests/regrade-lease.sh <path to fleet-regrade-core.sh>`; the flake's checks do. kubectl is a fake that keeps a cluster in a directory (tests/fake-kubectl.sh), and time is a variable, so nothing here touches a cluster.
# The core calls the functions defined here by name, and reads the variables set here, so shellcheck sees them as unused.
# shellcheck disable=SC2329,SC2034
set -euo pipefail

core=${1:?usage: regrade-lease.sh <path to fleet-regrade-core.sh>}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

cat > "$work/env" <<ENV
NODE_NAME=testnode
KUBECTL=$work/bin/kubectl
KUBECONFIG_PATH=$work/kubeconfig
LEASE_DURATION_SECONDS=600
ENV
mkdir -p "$work/bin" "$work/cluster"
cp "$(dirname "$0")/fake-kubectl.sh" "$work/bin/kubectl"
export FLEET_REGRADE_ENV=$work/env FAKE_DIR=$work/cluster

# shellcheck source=/dev/null
. "$(dirname "$0")/testlib.sh"
# shellcheck source=/dev/null
. "$core"

NOW=1000000000
now_epoch() { echo "$NOW"; }
lease="$FAKE_DIR/lease.json"
reset_cluster() {
  rm -f "$lease" "$FAKE_DIR/calls"
  unset FAKE_LEASE_READ_ERROR FAKE_NODES_ERROR FAKE_CREATE_RACE FAKE_REPLACE_CONFLICT
  nodes_json True True
}
# The voters: peer-a and peer-b with the given Ready condition statuses (True, False or Unknown), and this node, Ready.
nodes_json() {
  printf '{"items":[{"metadata":{"name":"peer-a"},"status":{"conditions":[{"type":"Ready","status":"%s"}]}},{"metadata":{"name":"peer-b"},"status":{"conditions":[{"type":"Ready","status":"%s"}]}},{"metadata":{"name":"testnode"},"status":{"conditions":[{"type":"Ready","status":"True"}]}}]}\n' "$1" "$2" > "$FAKE_DIR/nodes.json"
}
holder() { jq -r '.spec.holderIdentity' "$lease"; }
# Writes a Lease held by $1 and last renewed at epoch $2, with the configured duration.
write_lease() {
  build_lease "$1" "$2" "$2" 7 > "$lease"
}
acquires() { acquire_lease > /dev/null 2>&1; }

# The Lease.
reset_cluster
check "free: taken when there is no lease" acquires
check "the lease is created in the name of this node" test "$(holder)" = testnode
check "the lease carries the configured duration" test "$(jq -r '.spec.leaseDurationSeconds' "$lease")" = 600
check "the lease is renewed as of now" test "$(jq -r '.spec.renewTime' "$lease")" = "$(micro_time "$NOW")"

reset_cluster
write_lease other-node "$((NOW - 100))"
check_not "held: refused when another holds it unexpired" acquires
check "a refusal leaves the holder alone" test "$(holder)" = other-node

reset_cluster
write_lease other-node "$((NOW - 599))"
check_not "held: still refused one second before it expires" acquires

reset_cluster
write_lease other-node "$((NOW - 600))"
check "expired: taken over at the moment it expires" acquires
check "the takeover is in the name of this node" test "$(holder)" = testnode

reset_cluster
write_lease other-node "$((NOW - 100000))"
check "expired: taken over long after" acquires

reset_cluster
write_lease testnode "$((NOW - 100))"
check "mine: taken again (a step that was cut short)" acquires

reset_cluster
write_lease '' "$((NOW - 10))"
check "released: taken when nobody holds it" acquires
check "the release is replaced, not created" test "$(jq -r '.metadata.resourceVersion' "$lease")" = 8

reset_cluster
export FAKE_CREATE_RACE=1
check_not "another node creating it first refuses the step" acquires

reset_cluster
write_lease other-node "$((NOW - 1000))"
export FAKE_REPLACE_CONFLICT=1
check_not "a lease changed while it was being taken refuses the step" acquires
check "and is left as the other node had it" test "$(holder)" = other-node

reset_cluster
export FAKE_LEASE_READ_ERROR=1
check_not "a lease that cannot be read refuses the step" acquires
check "and nothing is written" test ! -e "$lease"
writes_attempted() { grep -qE "^(create|replace) " "$FAKE_DIR/calls"; }
check_not "and no write is even attempted" writes_attempted

# Releasing.
reset_cluster
acquire_lease > /dev/null
release_lease
check "release clears the holder" test "$(holder)" = ""
check "and the next node can take it at once" acquires

reset_cluster
write_lease other-node "$((NOW - 10))"
release_lease
check "release leaves a lease that has passed to another node alone" test "$(holder)" = other-node

# The other etcd-voting nodes.
reset_cluster
check "peers: all Ready" peers_ready
not_ready_peer() { (peers_ready > /dev/null 2>&1); }
nodes_json False True
check_not "peers: one NotReady refuses" not_ready_peer
nodes_json True Unknown
check_not "peers: one Unknown refuses" not_ready_peer
printf '{"items":[{"metadata":{"name":"peer-a"},"status":{"conditions":[]}},{"metadata":{"name":"testnode"},"status":{"conditions":[{"type":"Ready","status":"True"}]}}]}\n' > "$FAKE_DIR/nodes.json"
check_not "peers: no Ready condition at all refuses" not_ready_peer
printf '{"items":[{"metadata":{"name":"testnode"},"status":{"conditions":[{"type":"Ready","status":"False"}]}}]}\n' > "$FAKE_DIR/nodes.json"
check "peers: this node's own state is not a peer's" peers_ready
reset_cluster
export FAKE_NODES_ERROR=1
check_not "peers: a list that cannot be read refuses" not_ready_peer
reset_cluster
peers_ready
check "peers: only etcd-voting nodes are asked about" grep -qF -- "--selector node-role.kubernetes.io/etcd=true" "$FAKE_DIR/calls"

# Coordinated: the peers first, then the lease, then the command, then the release.
ran=''
step_recorder() { ran="$ran step"; }
failing_step() { ran="$ran failing"; return 3; }
run_coordinated() { ran=''; coordinated "$@" > /dev/null 2>&1; }

reset_cluster
run_coordinated step_recorder
check "all clear: the command runs" test "$ran" = " step"
check "and the lease is given back" test "$(holder)" = ""

reset_cluster
status=0
run_coordinated failing_step || status=$?
check "a failing command runs" test "$ran" = " failing"
check "its status is the result" test "$status" = 3
check "and the lease is still given back" test "$(holder)" = ""

reset_cluster
nodes_json False True
run_coordinated step_recorder || true
check "a peer down: the command does not run" test "$ran" = ""
check "a peer down: no lease is taken" test ! -e "$lease"

reset_cluster
write_lease other-node "$((NOW - 10))"
run_coordinated step_recorder || true
check "lease held elsewhere: the command does not run" test "$ran" = ""
check "lease held elsewhere: it is left as it was" test "$(holder)" = other-node

# Two nodes never step at once: while one holds the lease, a second asking as another node is refused.
reset_cluster
acquire_lease > /dev/null
second_node() { (NODE_NAME=second-node; acquire_lease > /dev/null 2>&1); }
check_not "a second node is refused while the first holds the lease" second_node

finish
