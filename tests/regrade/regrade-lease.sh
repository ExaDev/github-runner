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
  rm -f "$lease" "$FAKE_DIR/calls" "$FAKE_DIR/servers" "$FAKE_DIR/unready"
  unset FAKE_LEASE_READ_ERROR FAKE_NODES_ERROR FAKE_CREATE_RACE FAKE_REPLACE_CONFLICT KUBE_SERVERS KUBE_SERVER
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

# A holder other than the node, with annotations, as the role controller takes the Lease for a swap.
reset_cluster
acquire_lease fleet-role '{"example.test/id":"swap-1","example.test/nodes":"a,b"}' > /dev/null
check "a named holder takes the lease in its own name" test "$(holder)" = fleet-role
check "the annotations are written with the lease" test "$(jq -c '.metadata.annotations' "$lease")" = '{"example.test/id":"swap-1","example.test/nodes":"a,b"}'
acquires_for() { acquire_lease "$1" > /dev/null 2>&1; }
check_not "another holder is refused while the named holder has it" acquires_for testnode
check "the named holder takes its own lease again" acquires_for fleet-role
check "taking it again with no annotations leaves the lease as that call wrote it" test "$(jq -c '.metadata.annotations' "$lease")" = null
release_lease fleet-role
check "the named holder's release clears the holder" test "$(holder)" = ""
check "and the annotations" test "$(jq -c '.metadata.annotations' "$lease")" = null

reset_cluster
acquire_lease fleet-role '{"example.test/id":"swap-1"}' > /dev/null
release_lease
check "a release for this node leaves another holder's lease alone" test "$(holder)" = fleet-role
check "and its annotations" test "$(jq -c '.metadata.annotations' "$lease")" = '{"example.test/id":"swap-1"}'

# Renewing: the holder keeps the Lease past its duration by writing a later renew time, and nobody else may.
renews() { renew_lease "$1" > /dev/null 2>&1; }
# Writes a Lease held by $1, acquired at epoch $2, last renewed at epoch $3, carrying an annotation.
write_annotated_lease() {
  build_lease "$1" "$2" "$3" 7 | jq '.metadata.annotations = {"example.test/nodes": "node-a node-b"}' > "$lease"
}

reset_cluster
write_lease fleet-role "$((NOW - 500))"
check "renew: the holder renews its own lease" renews fleet-role
check "renew: the holder is unchanged" test "$(holder)" = fleet-role
check "renew: the renew time is now" test "$(jq -r '.spec.renewTime' "$lease")" = "$(micro_time "$NOW")"
check "renew: the lease is replaced, not created" test "$(jq -r '.metadata.resourceVersion' "$lease")" = 8

reset_cluster
write_annotated_lease fleet-role "$((NOW - 800))" "$((NOW - 500))"
renews fleet-role || true
check "renew: the acquire time is kept" test "$(jq -r '.spec.acquireTime' "$lease")" = "$(micro_time "$((NOW - 800))")"
check "renew: the duration is kept" test "$(jq -r '.spec.leaseDurationSeconds' "$lease")" = 600
check "renew: annotations survive" test "$(jq -r '.metadata.annotations["example.test/nodes"]' "$lease")" = "node-a node-b"

reset_cluster
write_lease fleet-role "$((NOW - 599))"
renews fleet-role || true
past_original_expiry() { [ "$(lease_state "$(cat "$lease")" fleet-role "$((NOW + 599))")" = mine ]; }
check "renew: the lease then outlasts the expiry it had before" past_original_expiry

reset_cluster
write_annotated_lease other-node "$((NOW - 800))" "$((NOW - 100))"
cp "$lease" "$work/before.json"
check_not "renew: refused when another holds it" renews fleet-role
check "renew: a refusal leaves the lease as it was" cmp -s "$lease" "$work/before.json"

reset_cluster
write_lease '' "$((NOW - 100))"
check_not "renew: refused when the lease was released" renews fleet-role

reset_cluster
check_not "renew: refused when there is no lease" renews fleet-role

reset_cluster
write_lease fleet-role "$((NOW - 100))"
export FAKE_REPLACE_CONFLICT=1
check_not "renew: refused when the lease changed while it was being renewed" renews fleet-role

reset_cluster
write_lease fleet-role "$((NOW - 100))"
export FAKE_LEASE_READ_ERROR=1
check_not "renew: refused when the lease cannot be read" renews fleet-role
unset FAKE_LEASE_READ_ERROR

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

# Coordinated: an API server that answers first, then the peers, then the lease, then the command, then the release.
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

# Which API server a step goes through: the first of the candidates whose /readyz answers, never the kubeconfig's own (this node's) while there are candidates, chosen again for the release and whenever a step waits for the API.
first_api=https://first.example.test:6443
second_api=https://second.example.test:6443
unready() { printf '%s\n' "$@" > "$FAKE_DIR/unready"; }
# The servers the calls other than the readiness probes went to, each once, in the order first used.
used_servers() { grep -v ' get --raw=/readyz$' "$FAKE_DIR/servers" | cut -d' ' -f1 | awk '!seen[$0]++' | tr '\n' ' '; }
to_own_server() { grep -q '^default ' "$FAKE_DIR/servers"; }

reset_cluster
KUBE_SERVERS="$first_api $second_api"
run_coordinated step_recorder
check "candidates: the step runs through the first that answers" test "$ran" = " step"
check "candidates: every call goes to it" test "$(used_servers)" = "$first_api "
check_not "candidates: nothing goes to this node's own server" to_own_server

reset_cluster
KUBE_SERVERS="$first_api $second_api"
unready "$first_api"
run_coordinated step_recorder
check "a candidate that does not answer is passed over" test "$ran" = " step"
check "and every call goes to the next" test "$(used_servers)" = "$second_api "
check "the lease is given back through it" test "$(holder)" = ""

reset_cluster
KUBE_SERVERS="$first_api $second_api"
unready "$first_api" "$second_api"
ran=''
refused_output=$(coordinated step_recorder 2>&1) && refused_status=0 || refused_status=$?
check_not "no candidate answers: the step is refused" test "$refused_status" = 0
check "and the command does not run" test "$ran" = ""
check "and no lease is taken" test ! -e "$lease"
check "and the refusal names the candidates" grep -Fq "no API server answers (tried: $first_api $second_api)" <<< "$refused_output"
check_not "and this node's own server is never tried in their place" to_own_server

reset_cluster
KUBE_SERVERS="$first_api $second_api"
first_goes_away() { ran="$ran step"; unready "$first_api"; }
run_coordinated first_goes_away
check "a server that goes away during a step: the release goes through the next" test "$(holder)" = ""
check "and the calls after it went there" test "$(used_servers)" = "$first_api $second_api "

reset_cluster
run_coordinated step_recorder
check "no candidates: the kubeconfig's own server is used" test "$(used_servers)" = "default "

reset_cluster
unready default
run_coordinated step_recorder || true
check "no candidates and the kubeconfig's own does not answer: the step is refused" test "$ran" = ""

reset_cluster
KUBE_SERVERS="$first_api $second_api"
unready "$first_api"
OPERATION_TIMEOUT_SECONDS=1 wait_for_api
# shellcheck disable=SC2153 # KUBE_SERVER is the core's, set by select_api_server.
check "waiting for the API settles on a candidate that answers" test "$KUBE_SERVER" = "$second_api"
unready "$first_api" "$second_api"
waits_out() { (OPERATION_TIMEOUT_SECONDS=1 wait_for_api) > /dev/null 2>&1; }
check_not "waiting for the API gives up when none answers" waits_out
gave_up_text() {
  local output
  output=$(OPERATION_TIMEOUT_SECONDS=1 wait_for_api 2>&1) && return 1
  grep -Fq "No API server became ready within 1s (tried: $first_api $second_api)" <<< "$output"
}
check "and says which it tried" gave_up_text

# Two nodes never step at once: while one holds the lease, a second asking as another node is refused.
reset_cluster
acquire_lease > /dev/null
second_node() { (NODE_NAME=second-node; acquire_lease > /dev/null 2>&1); }
check_not "a second node is refused while the first holds the lease" second_node

finish
