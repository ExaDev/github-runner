#!/usr/bin/env bash
# Integration test for the autoscaler's ceiling (scripts/autoscaler.sh, which runs scripts/autoscaler-ceiling.jq) on a real cluster. Two scale sets are installed from the chart the fleet runs, with no controller, so nothing reconciles them and no GitHub credential is needed. The autoscaler polls as its own service account would, through kubectl impersonation, holding exactly the RBAC the role's install_platform.yml grants (rendered from that file), and patches the scale sets for real:
#
# - the ceiling it works out equals the role's install-time measurement (plugins/filter/arc.py's arc_measured_max_runners) of the same nodes and pods, and a pool above it is lowered to it;
# - a pod of another workload taking a runner's worth of cpu lowers the ceiling and the pool, and once it is gone the pool is raised back after the confirmation polls;
# - a runner template the ceiling cannot be worked out from keeps the last ceiling and never raises the pool;
# - a cordoned node is not counted, so the pool comes down to its running count.
#
# The runner pod's cpu request is chosen from the node's free cpu so that cpu decides the ceiling, whatever size of node the cluster has.
#
# Usage: tests/autoscaler_ceiling/run.sh. Needs Docker, kind, kubectl, helm, jq, and python3 (PYTHON overrides which) with PyYAML, Jinja2 and ansible-core (plugins/filter/arc.py imports it). Creates a kind cluster named grtest-ceiling with metrics-server and removes it on exit; GRTEST_KEEP=1 keeps it. With GRTEST_KUBECONFIG and GRTEST_DISPOSABLE=1 it uses that cluster instead, which must have exactly one node and a working metrics API (k3s's own metrics-server, for one); the test labels and cordons that node, so it refuses a cluster not declared disposable.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
python="${PYTHON:-python3}"
cluster=grtest-ceiling
charts=oci://ghcr.io/actions/actions-runner-controller-charts
# The chart pair the fleet runs; any version with the AutoscalingRunnerSet maxRunners field would do.
chart_version=0.15.0
# metrics-server for kind, which ships none, pinned by digest: the release's own components.yaml.
metrics_server_version=v0.7.2
metrics_server_sha256=f103539a54ed72efe66616afc74a8bfaed651703cb3918797599046af5617441
platform_namespace=grtest-ceiling-platform
pooled_namespaces=(grtest-ceiling-a grtest-ceiling-b)
other_namespace=grtest-ceiling-other
eligibility_label=grtest.example/eligible
initial_max_runners=3
# The cpu request is the node's free cpu divided by this, so that cpu alone places this many runners on the node.
runners_by_cpu=4
# A memory request small enough that memory never decides the ceiling on any node this test runs on.
runner_memory=64Mi
work="$(mktemp -d "${TMPDIR:-/tmp}/grtest-ceiling.XXXXXX")"

log() { echo "==> $*"; }
cluster_created=0
fail() {
  echo "FAIL: $*" >&2
  if [ -s "$work/poll.log" ]; then
    echo "--- last poll" >&2
    cat "$work/poll.log" >&2
  fi
  exit 1
}
cleanup() {
  if [ "${GRTEST_KEEP:-0}" != 1 ]; then
    if [ "$cluster_created" = 1 ]; then
      kind delete cluster --name "$cluster" >/dev/null 2>&1 || true
    elif [ -n "${GRTEST_KUBECONFIG:-}" ]; then
      kubectl uncordon "${node:-}" >/dev/null 2>&1 || true
      # The chart puts the controller's cleanup finalizer on what it installs, and no controller runs here to remove it, so the namespaces would never finish terminating.
      for namespace in "${pooled_namespaces[@]}"; do
        kubectl get autoscalingrunnersets,roles,rolebindings,serviceaccounts,secrets -n "$namespace" -o name 2>/dev/null | while read -r object; do
          kubectl patch "$object" -n "$namespace" --type merge -p '{"metadata":{"finalizers":null}}' >/dev/null 2>&1 || true
        done
      done
      kubectl delete namespace "$platform_namespace" "${pooled_namespaces[@]}" "$other_namespace" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    fi
    rm -rf "$work"
  fi
}
trap cleanup EXIT

if [ -n "${GRTEST_KUBECONFIG:-}" ]; then
  [ "${GRTEST_DISPOSABLE:-0}" = 1 ] || fail "GRTEST_KUBECONFIG names a cluster this test will label and cordon a node of; set GRTEST_DISPOSABLE=1 to say it is disposable"
  export KUBECONFIG="$GRTEST_KUBECONFIG"
else
  export KUBECONFIG="$work/kubeconfig"
  log "Creating the kind cluster and installing metrics-server"
  kind create cluster --name "$cluster" --kubeconfig "$KUBECONFIG" --wait 120s >/dev/null
  cluster_created=1
  curl -fsSLo "$work/metrics-server.yaml" "https://github.com/kubernetes-sigs/metrics-server/releases/download/${metrics_server_version}/components.yaml"
  echo "${metrics_server_sha256}  $work/metrics-server.yaml" | sha256sum -c - >/dev/null || fail "metrics-server's components.yaml does not match its pinned digest"
  kubectl apply -f "$work/metrics-server.yaml" >/dev/null
  # kind's kubelets serve certificates the cluster's CA did not sign.
  kubectl patch deployment metrics-server -n kube-system --type json -p '[{"op": "add", "path": "/spec/template/spec/containers/0/args/-", "value": "--kubelet-insecure-tls"}]' >/dev/null
fi

mapfile -t nodes < <(kubectl get nodes -o name)
[ "${#nodes[@]}" -eq 1 ] || fail "the cluster has ${#nodes[@]} nodes; this test needs exactly one"
node="${nodes[0]#node/}"

log "Waiting for the metrics API"
for _ in $(seq 1 60); do
  kubectl top nodes --no-headers >/dev/null 2>&1 && break
  sleep 5
done
kubectl top nodes --no-headers >/dev/null || fail "the metrics API never answered"

kubectl label node "$node" "${eligibility_label}=true" --overwrite >/dev/null
helm show crds "$charts/gha-runner-scale-set-controller" --version "$chart_version" | kubectl apply --server-side -f - >/dev/null

# The role's install-time measurement of the node as it is now, with a runner pod of the given cpu request, and the node's free cpu in millicores: a reference worked out in Python's exact decimals, apart from the program under test.
reference() {
  kubectl get nodes -o json > "$work/nodes.json"
  kubectl get pods --all-namespaces -o json > "$work/pods.json"
  "$python" - "$repo_root" "$work" "$1" "$eligibility_label" "${pooled_namespaces[@]}" <<'PY'
import importlib.util, json, pathlib, sys
from decimal import Decimal

root, work, cpu, label, pooled = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5:]
spec = importlib.util.spec_from_file_location("arc_filters", root / "plugins/filter/arc.py")
arc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arc)
nodes = json.loads((work / "nodes.json").read_text())["items"]
pods = json.loads((work / "pods.json").read_text())["items"]
sizing = {"measured": True, "pod_cpu_request": str(arc.arc_quantity(cpu)), "pod_memory_request_gib": "0.0625"}
measured = arc.arc_measured_max_runners(sizing, nodes, pods, {label: "true"}, pooled)
assert not measured["errors"], measured["errors"]
node = measured["nodes"][0]
free = (Decimal(str(node["allocatable_cpu"])) - Decimal(str(node["requested_cpu"]))) * 1000
print(measured["max_runners"], int(free))
PY
}

read -r _ free_millicores < <(reference 1)
runner_cpu="$(( free_millicores / runners_by_cpu ))m"
read -r expected _ < <(reference "$runner_cpu")
[ "$expected" -eq "$runners_by_cpu" ] || fail "a ${runner_cpu} runner should place ${runners_by_cpu} on the node by cpu, but the reference places ${expected}"
log "The node has ${free_millicores}m of cpu free; a ${runner_cpu} runner places ${expected}"

log "Installing two scale sets with no controller"
for namespace in "${pooled_namespaces[@]}"; do
  kubectl create namespace "$namespace" >/dev/null
  jq -n --arg cpu "$runner_cpu" --arg memory "$runner_memory" --arg label "$eligibility_label" --argjson max "$initial_max_runners" '{
    maxRunners: $max, minRunners: 0, githubConfigUrl: "https://github.com/example", githubConfigSecret: {github_token: "unused"},
    controllerServiceAccount: {name: "arc-gha-rs-controller", namespace: "grtest-arc-systems"},
    template: {spec: {nodeSelector: {($label): "true"}, containers: [{name: "runner", image: "ghcr.io/actions/actions-runner:latest", command: ["/home/runner/run.sh"],
      resources: {requests: {cpu: $cpu, memory: $memory}, limits: {cpu: $cpu, memory: $memory}}}]}}
  }' > "$work/values-$namespace.json"
  helm install grtest-runners "$charts/gha-runner-scale-set" --version "$chart_version" -n "$namespace" -f "$work/values-$namespace.json" >/dev/null
done

log "Granting the autoscaler's service account exactly the role's RBAC"
kubectl create namespace "$platform_namespace" >/dev/null
kubectl create namespace "$other_namespace" >/dev/null
kubectl create serviceaccount autoscaler -n "$platform_namespace" >/dev/null
kubectl create configmap autoscaler-status -n "$platform_namespace" >/dev/null
"$python" - "$repo_root" "$platform_namespace" "${pooled_namespaces[@]}" > "$work/rbac.yaml" <<'PY'
import pathlib, sys
import jinja2, yaml

root, platform, pooled = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
tasks = yaml.safe_load((root / "roles/github_runner_arc/tasks/install_platform.yml").read_text())
blocks = [task for task in tasks if task.get("name") == "Platform: autoscaler RBAC"]
assert len(blocks) == 1, f"expected one autoscaler RBAC block in install_platform.yml, found {len(blocks)}"
environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
documents = []
for task in blocks[0]["block"]:
    template = environment.from_string(yaml.safe_dump(task["kubernetes.core.k8s"]["definition"]))
    items = [{"namespace": namespace, "release": "grtest-runners", "label": namespace} for namespace in pooled] if "loop" in task else [None]
    for item in items:
        documents.append(yaml.safe_load(template.render(github_runner_arc_platform_namespace=platform, item=item)))
print(yaml.safe_dump_all(documents))
PY
kubectl apply -f "$work/rbac.yaml" >/dev/null

# The autoscaler's kubectl, as its service account.
mkdir -p "$work/bin"
real_kubectl="$(command -v kubectl)"
printf '#!/bin/sh\nexec %s --as=system:serviceaccount:%s:autoscaler "$@"\n' "$real_kubectl" "$platform_namespace" > "$work/bin/kubectl"
chmod +x "$work/bin/kubectl"

targets=""
for namespace in "${pooled_namespaces[@]}"; do targets="$targets $namespace/grtest-runners"; done

# One poll, for real (not dry run), its output kept for the checks and for a failure's report.
poll() {
  PATH="$work/bin:$PATH" \
  AUTOSCALER_TARGETS="${targets# }" \
  AUTOSCALER_DRY_RUN=false \
  AUTOSCALER_USABLE_BUDGET_GI=1024 \
  AUTOSCALER_MAX_CEILING="" \
  AUTOSCALER_FLOOR=1 \
  AUTOSCALER_NODE_SELECTOR="${eligibility_label}=true" \
  AUTOSCALER_STATE_NAMESPACE="$platform_namespace" \
  AUTOSCALER_STATUS_CONFIGMAP=autoscaler-status \
    "$repo_root/scripts/autoscaler.sh" > "$work/poll.log" 2>&1 || fail "the poll exited non-zero"
}
pooled_total() {
  local namespace total=0
  for namespace in "${pooled_namespaces[@]}"; do
    total=$(( total + $(kubectl get autoscalingrunnerset grtest-runners -n "$namespace" -o jsonpath='{.spec.maxRunners}') ))
  done
  echo "$total"
}
status() { kubectl get configmap autoscaler-status -n "$platform_namespace" -o jsonpath='{.data.status\.json}'; }
expect_log() { grep -qF -- "$1" "$work/poll.log" || fail "the poll did not print '$1'"; }
# What the poll recorded as its decision, which a poll that changes nothing reports only there.
expect_mode() { [ "$(status | jq -r .mode)" = "$1" ] || fail "the poll recorded '$(status | jq -r .mode)', not '$1'"; }
expect_total() { [ "$(pooled_total)" -eq "$1" ] || fail "the pooled maxRunners total is $(pooled_total), not $1"; }

log "A pool above the ceiling comes down to it"
poll
expect_log "ceiling=${expected} measured_ceiling=true"
expect_log "above the ceiling ${expected}"
expect_total "$expected"
[ "$(status | jq -c --arg node "$node" '[.ceiling, .ceiling_measured, (.ceiling_nodes[] | select(.name == $node) | .runners)]')" = "[${expected},true,${expected}]" ] || fail "the status does not record the ceiling: $(status)"

log "Another workload's pod taking a runner's cpu lowers the ceiling and the pool"
kubectl run consumer -n "$other_namespace" --image=registry.k8s.io/pause:3.10 --restart=Never --overrides "{\"spec\": {\"containers\": [{\"name\": \"consumer\", \"image\": \"registry.k8s.io/pause:3.10\", \"resources\": {\"requests\": {\"cpu\": \"${runner_cpu}\"}}}]}}" >/dev/null
kubectl wait --for=condition=Ready pod/consumer -n "$other_namespace" --timeout=180s >/dev/null || fail "the consumer pod never started"
read -r lowered _ < <(reference "$runner_cpu")
[ "$lowered" -eq $(( expected - 1 )) ] || fail "the reference places ${lowered} beside the consumer, not $(( expected - 1 ))"
poll
expect_log "ceiling=${lowered} measured_ceiling=true"
expect_total "$lowered"

log "Once it is gone the pool is raised back after the confirmation polls"
kubectl delete pod consumer -n "$other_namespace" --wait=true >/dev/null
poll
expect_mode "watching: headroom confirmed for 1/2 polls"
expect_total "$lowered"
poll
expect_log "raise:"
expect_total "$expected"

log "A template the ceiling cannot be worked out from keeps the last ceiling and never raises"
# No cpu request or limit, so how many fit cannot be worked out; the memory limit stays, since without it the poll fails safe before the ceiling is read.
for namespace in "${pooled_namespaces[@]}"; do
  kubectl patch autoscalingrunnerset grtest-runners -n "$namespace" --type json -p '[{"op": "remove", "path": "/spec/template/spec/containers/0/resources/requests/cpu"}, {"op": "remove", "path": "/spec/template/spec/containers/0/resources/limits/cpu"}, {"op": "replace", "path": "/spec/maxRunners", "value": 1}]' >/dev/null
done
for _ in 1 2 3; do
  poll
  expect_log "ERROR: could not work out the ceiling from the nodes; keeping the last one (${expected}) and not raising"
  expect_log "ceiling=${expected} measured_ceiling=false"
  expect_mode "steady: the ceiling could not be worked out from the nodes, so the pool is not raised"
  expect_total 2
done
grep -qF "the runner pod requests no cpu or no memory" "$work/poll.log" || fail "the poll did not say why the ceiling could not be worked out"
for namespace in "${pooled_namespaces[@]}"; do
  kubectl patch autoscalingrunnerset grtest-runners -n "$namespace" --type json -p "[{\"op\": \"add\", \"path\": \"/spec/template/spec/containers/0/resources/requests/cpu\", \"value\": \"${runner_cpu}\"}, {\"op\": \"add\", \"path\": \"/spec/template/spec/containers/0/resources/limits/cpu\", \"value\": \"${runner_cpu}\"}]" >/dev/null
done
poll
expect_log "ceiling=${expected} measured_ceiling=true"

log "A cordoned node is not counted, so the pool comes down to its running count"
kubectl cordon "$node" >/dev/null
poll
expect_log "ceiling=0 measured_ceiling=true"
expect_total 0
[ "$(status | jq -r --arg node "$node" '.ceiling_nodes[] | select(.name == $node) | .not_counted')" = "is cordoned" ] || fail "the status does not say why the node is not counted: $(status)"
kubectl uncordon "$node" >/dev/null

log "PASS"
