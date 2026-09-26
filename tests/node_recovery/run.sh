#!/usr/bin/env bash
# Integration test for the github_runner_arc role's node recovery watcher: renders the role's manifests, applies them to a three-node kind cluster with the watcher image built from this checkout, and stops one worker's container to stand in for a node that goes down. A single-replica StatefulSet stands in for an ARC listener: like the listener, its controller waits for the old pod to go before it creates a new one, so a pod stuck Terminating on a stopped node stalls it the same way.
#
# First, with the watcher scaled to zero, it shows the failure: the stand-in's pod on the stopped node stays Terminating and is never replaced. Then the watcher taints that node out of service and the pod is replaced on a healthy node; the worker is started again and the taint is removed. Finally, with the watcher running throughout and the stand-in on default tolerations, it stops the worker again and asserts the pod is replaced within the node monitor grace period plus the threshold plus a margin, well before Kubernetes' own five-minute eviction, and not before the threshold. Also checks the watcher's permissions, that it never deletes a Node and that it leaves healthy nodes alone.
#
# Usage: tests/node_recovery/run.sh. Needs Docker, kind, kubectl, jq and ansible-playbook (ANSIBLE_PLAYBOOK overrides which). Creates a kind cluster named grtest-recovery and removes it on exit unless GRTEST_KEEP=1.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ansible_playbook="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
cluster=grtest-recovery
platform_namespace=github-runner-platform
namespace=grtest-listeners
image=grtest/node-recovery:test
pod_image=busybox:1.36
watcher_node="${cluster}-worker"
doomed_node="${cluster}-worker2"
after_seconds=30
poll_seconds=5
# kube-controller-manager's default node-monitor-grace-period: 50s from Kubernetes 1.32, 40s before.
grace_seconds=50
# The pod garbage collector's sweep interval, how long the StatefulSet takes to schedule and start the replacement, and slack for a busy CI runner.
margin_seconds=60
recovery_bound=$((grace_seconds + after_seconds + poll_seconds + margin_seconds))
# How long the failure is watched for, with the watcher off, before it is taken as not recovering on its own.
stuck_watch_seconds=60
out_of_service=node.kubernetes.io/out-of-service
annotation=github-runner.exadev/out-of-service-applied-at
work="$(mktemp -d "${TMPDIR:-/tmp}/grtest-recovery.XXXXXX")"
export KUBECONFIG="$work/kubeconfig"

log() { echo "==> $*"; }
fail() {
  echo "FAIL: $*" >&2
  kubectl get nodes -o wide >&2 || true
  kubectl get nodes -o json | jq -r '.items[] | "\(.metadata.name) taints=\(.spec.taints // [] | map(.key) | join(",")) annotations=\(.metadata.annotations // {} | keys | join(","))"' >&2 || true
  kubectl get pods -A -o wide >&2 || true
  kubectl -n "$platform_namespace" logs deploy/node-recovery --tail=40 >&2 || true
  exit 1
}
cleanup() {
  if [ "${GRTEST_KEEP:-0}" != 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || true
    rm -rf "$work"
  fi
}
trap cleanup EXIT

# Polls a condition command every two seconds until it succeeds or the timeout passes.
wait_for() {
  local timeout="$1" description="$2"
  shift 2
  local deadline=$(( $(date +%s) + timeout ))
  until "$@"; do
    [ "$(date +%s)" -lt "$deadline" ] || fail "timed out after ${timeout}s waiting for ${description}"
    sleep 2
  done
}

node_ready() { kubectl get node "$1" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'; }
node_is() { [ "$(node_ready "$1")" = "$2" ]; }
has_taint() { kubectl get node "$1" -o json | jq -e --arg key "$out_of_service" '[.spec.taints // [] | .[] | select(.key == $key)] | length > 0' >/dev/null; }
lacks_taint() { ! has_taint "$1"; }
lacks_annotation() { kubectl get node "$1" -o json | jq -e --arg a "$annotation" '(.metadata.annotations // {}) | has($a) | not' >/dev/null; }
pod_json() { kubectl -n "$namespace" get pod "$1" -o json 2>/dev/null; }
pod_field() { pod_json "$1" | jq -r "$2"; }
pod_terminating() { [ "$(pod_field "$1" '.metadata.deletionTimestamp // ""')" != "" ]; }
# Whether the pod exists as a different pod from the given uid, running on a node other than the doomed one.
pod_replaced() {
  local json
  json="$(pod_json "$1")" || return 1
  [ "$(jq -r '.metadata.uid' <<< "$json")" != "$2" ] \
    && [ "$(jq -r '.spec.nodeName // ""' <<< "$json")" != "$doomed_node" ] \
    && [ "$(jq -r '.status.phase' <<< "$json")" = Running ] \
    && [ "$(jq -r '.metadata.deletionTimestamp // ""' <<< "$json")" = "" ]
}

# A single-replica StatefulSet preferring the doomed node, standing in for an ARC listener. Extra tolerations are passed as a JSON list.
listener() {
  local name="$1" tolerations="$2"
  kubectl apply -f - >/dev/null <<EOF
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: ${name}
  namespace: ${namespace}
spec:
  replicas: 1
  serviceName: ${name}
  selector:
    matchLabels: {app: ${name}}
  template:
    metadata:
      labels: {app: ${name}}
    spec:
      terminationGracePeriodSeconds: 5
      tolerations: ${tolerations}
      affinity:
        nodeAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              preference:
                matchExpressions:
                  - {key: kubernetes.io/hostname, operator: In, values: [${doomed_node}]}
      containers:
        - name: listener
          image: ${pod_image}
          imagePullPolicy: IfNotPresent
          command: [sleep, "86400"]
EOF
  wait_for 120 "${name}-0 to run on ${doomed_node}" pod_running_on "${name}-0" "$doomed_node"
}
pod_running_on() { [ "$(pod_field "$1" '.status.phase + " " + (.spec.nodeName // "")' 2>/dev/null)" = "Running $2" ]; }

stop_doomed() {
  docker stop "$doomed_node" >/dev/null
  stopped_at="$(date +%s)"
}

log "Creating kind cluster $cluster"
cat > "$work/kind.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
  - role: worker
  - role: worker
EOF
kind create cluster --name "$cluster" --config "$work/kind.yaml" --kubeconfig "$KUBECONFIG" --wait 180s >/dev/null
server_version="$(kubectl version -o json | jq -r '.serverVersion.gitVersion')"
log "Server version ${server_version}"
doomed_uid="$(kubectl get node "$doomed_node" -o jsonpath='{.metadata.uid}')"

log "Building and loading the watcher image and the stand-in's image"
docker build -q -f "$repo_root/node-recovery/Dockerfile" -t "$image" "$repo_root" >/dev/null
docker pull -q "$pod_image" >/dev/null
kind load docker-image "$image" "$pod_image" --name "$cluster" >/dev/null

log "Rendering and applying the role's node recovery manifests"
ANSIBLE_COLLECTIONS_PATH="$repo_root/playbooks/collections${ANSIBLE_COLLECTIONS_PATH:+:$ANSIBLE_COLLECTIONS_PATH}" \
  "$ansible_playbook" "$repo_root/tests/node_recovery/render.yml" \
  -e github_runner_arc_node_recovery_image="$image" \
  -e github_runner_arc_node_recovery_after_seconds="$after_seconds" \
  -e github_runner_arc_node_recovery_poll_seconds="$poll_seconds" \
  -e recovery_output="$work/recovery.yaml" >/dev/null
kubectl create namespace "$platform_namespace" >/dev/null
kubectl create namespace "$namespace" >/dev/null
kubectl apply -f "$work/recovery.yaml" >/dev/null
# The test keeps the watcher off the node it stops, so each phase measures the taint rather than the watcher's own rescheduling; everything else is as the role renders it.
kubectl -n "$platform_namespace" patch deployment node-recovery --type=merge \
  -p "{\"spec\": {\"template\": {\"spec\": {\"nodeSelector\": {\"kubernetes.io/hostname\": \"${watcher_node}\"}}}}}" >/dev/null
kubectl -n "$platform_namespace" rollout status deployment/node-recovery --timeout=120s >/dev/null

log "Checking the watcher's permissions"
as="system:serviceaccount:${platform_namespace}:node-recovery"
check_can() {
  local expected="$1"
  shift
  local answer
  answer="$(kubectl auth can-i --as="$as" "$@" 2>/dev/null || true)"
  [ "$answer" = "$expected" ] || fail "can-i $* answered '$answer', expected '$expected'"
}
check_can yes get nodes
check_can yes list nodes
check_can yes patch nodes
check_can no delete nodes
check_can no create nodes
check_can no delete pods --all-namespaces
check_can no get secrets --all-namespaces

log "Phase 1: with the watcher off, a pod on a stopped node stays Terminating and is never replaced"
kubectl -n "$platform_namespace" scale deployment node-recovery --replicas=0 >/dev/null
wait_for 60 "the watcher to stop" bash -c "[ -z \"\$(kubectl -n $platform_namespace get pods -l app.kubernetes.io/name=node-recovery -o name)\" ]"
# A short toleration gets the pod marked for deletion soon after the node is marked unreachable, the state a listener reaches after the default five minutes, without waiting them out.
listener stuck '[{"key": "node.kubernetes.io/unreachable", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 5}, {"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 5}]'
stuck_uid="$(pod_field stuck-0 '.metadata.uid')"
stop_doomed
wait_for $((grace_seconds + margin_seconds)) "${doomed_node} to be marked Unknown" node_is "$doomed_node" Unknown
wait_for 60 "stuck-0 to be marked for deletion" pod_terminating stuck-0
log "stuck-0 is Terminating; watching it for ${stuck_watch_seconds}s"
sleep "$stuck_watch_seconds"
[ "$(pod_field stuck-0 '.metadata.uid')" = "$stuck_uid" ] || fail "stuck-0 was replaced without the watcher, so the test does not reproduce the failure"
pod_terminating stuck-0 || fail "stuck-0 is no longer Terminating"
log "stuck-0 is still Terminating on ${doomed_node} and has not been replaced"

log "Phase 1: starting the watcher clears it"
started_at="$(date +%s)"
kubectl -n "$platform_namespace" scale deployment node-recovery --replicas=1 >/dev/null
wait_for $((poll_seconds + margin_seconds)) "${doomed_node} to be tainted out of service" has_taint "$doomed_node"
wait_for $((poll_seconds + margin_seconds)) "stuck-0 to be replaced on a healthy node" pod_replaced stuck-0 "$stuck_uid"
log "stuck-0 replaced on $(pod_field stuck-0 '.spec.nodeName') $(( $(date +%s) - started_at ))s after the watcher started"

log "Phase 2: the taint is removed once the node is back"
docker start "$doomed_node" >/dev/null
wait_for 240 "${doomed_node} to report Ready" node_is "$doomed_node" True
wait_for $((poll_seconds * 4 + 10)) "the taint to be removed from ${doomed_node}" lacks_taint "$doomed_node"
wait_for 20 "the watcher's annotation to be removed from ${doomed_node}" lacks_annotation "$doomed_node"
[ "$(kubectl get node "$doomed_node" -o jsonpath='{.metadata.uid}')" = "$doomed_uid" ] || fail "the Node object of ${doomed_node} was replaced"
log "Taint and annotation removed; the Node object is the original one"

log "Phase 3: with the watcher running and default tolerations, a stopped node is recovered within ${recovery_bound}s"
listener bounded '[]'
bounded_uid="$(pod_field bounded-0 '.metadata.uid')"
stop_doomed
wait_for "$recovery_bound" "bounded-0 to be replaced on a healthy node" pod_replaced bounded-0 "$bounded_uid"
elapsed=$(( $(date +%s) - stopped_at ))
log "bounded-0 replaced on $(pod_field bounded-0 '.spec.nodeName') ${elapsed}s after ${doomed_node} stopped (bound ${recovery_bound}s; Kubernetes alone would only mark it for deletion after ${grace_seconds}s + 300s)"
node_json="$(kubectl get node "$doomed_node" -o json)"
unknown_since="$(jq -r '.status.conditions[] | select(.type == "Ready") | .lastTransitionTime' <<< "$node_json")"
tainted_at="$(jq -r --arg a "$annotation" '.metadata.annotations[$a]' <<< "$node_json")"
waited=$(( $(jq -rn --arg t "$tainted_at" '$t | fromdateiso8601') - $(jq -rn --arg t "$unknown_since" '$t | fromdateiso8601') ))
[ "$waited" -ge "$after_seconds" ] || fail "the watcher tainted ${doomed_node} ${waited}s after it went Unknown, before the ${after_seconds}s threshold"
log "The watcher waited ${waited}s after ${doomed_node} went Unknown before tainting it (threshold ${after_seconds}s)"

log "Checking the healthy nodes were left alone"
for healthy in "${cluster}-control-plane" "$watcher_node"; do
  lacks_taint "$healthy" || fail "${healthy} was tainted out of service"
  lacks_annotation "$healthy" || fail "${healthy} carries the watcher's annotation"
done
[ "$(kubectl get node "$doomed_node" -o jsonpath='{.metadata.uid}')" = "$doomed_uid" ] || fail "the Node object of ${doomed_node} was replaced"

log "PASS"
