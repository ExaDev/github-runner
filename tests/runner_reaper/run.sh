#!/usr/bin/env bash
# Integration test for the github_runner_arc role's runner reaper: renders the role's reaper manifests, applies them to a kind cluster that has ARC's EphemeralRunner CRD (no controller, so a deleted runner goes at once), and runs the CronJob's Job in the pull Secret renewal's image built from this checkout against a stand-in for GitHub (stub_github.py). Asserts the ServiceAccount can only list and delete EphemeralRunners in its namespace; that a pass deletes a runner whose job has run past the bound and one whose run was cancelled, and leaves an idle runner and one whose job is within the bound; and that a refused token fails the Job and deletes nothing. The script's every branch is covered by tests/unit/test_reap_runners.py; this shows the role's CronJob, RBAC and ConfigMap doing it in a cluster.
#
# Usage: tests/runner_reaper/run.sh. Needs Docker, kind, kubectl, helm, jq, openssl and ansible-playbook (ANSIBLE_PLAYBOOK overrides which). Creates a kind cluster named grtest-reaper and removes it on exit unless GRTEST_KEEP=1.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ansible_playbook="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
cluster=grtest-reaper
namespace=grtest-runners
other_namespace=grtest-elsewhere
image=grtest/pull-secret-renewer:test
stub_image=python:3.13-alpine
controller_chart=oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller
reaper=runner-reaper
app_secret=example-org-github-app
# An hour: the stand-in's hung job started days ago and its busy job a minute ago, so the bound falls between them.
bound=3600
job_timeout=180s
work="$(mktemp -d "${TMPDIR:-/tmp}/grtest-reaper.XXXXXX")"
export KUBECONFIG="$work/kubeconfig"

log() { echo "==> $*"; }
fail() {
  echo "FAIL: $*" >&2
  kubectl -n "$namespace" get jobs,pods,ephemeralrunners >&2 || true
  kubectl -n "$namespace" logs -l app.kubernetes.io/name=runner-reaper --tail=40 --prefix >&2 || true
  kubectl -n "$namespace" logs deploy/stub-github --tail=40 >&2 || true
  exit 1
}
cleanup() {
  if [ "${GRTEST_KEEP:-0}" != 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || true
    rm -rf "$work"
  fi
}
trap cleanup EXIT

# Runs one Job from the CronJob and waits for it to finish either way, printing complete, failed or timeout.
run_job() {
  local job="$1"
  kubectl -n "$namespace" create job "$job" --from="cronjob/$reaper" >/dev/null
  kubectl -n "$namespace" wait --for=condition=complete "job/$job" --timeout="$job_timeout" >/dev/null 2>&1 &
  local complete=$!
  kubectl -n "$namespace" wait --for=condition=failed "job/$job" --timeout="$job_timeout" >/dev/null 2>&1 &
  local failed=$!
  while kill -0 "$complete" 2>/dev/null && kill -0 "$failed" 2>/dev/null; do
    sleep 2
  done
  if wait "$complete" 2>/dev/null; then
    kill "$failed" 2>/dev/null || true
    echo complete
  elif wait "$failed" 2>/dev/null; then
    kill "$complete" 2>/dev/null || true
    echo failed
  else
    echo timeout
  fi
}

# Creates an EphemeralRunner as ARC's controller would; with a run id it also records the job on its status, as ARC's listener does when it assigns the runner a job.
create_runner() {
  local name="$1" run_id="${2:-}"
  kubectl -n "$namespace" apply -f - >/dev/null <<YAML
apiVersion: actions.github.com/v1alpha1
kind: EphemeralRunner
metadata:
  name: ${name}
spec:
  githubConfigUrl: https://github.com/example-org
  githubConfigSecret: ${app_secret}
  runnerScaleSetId: 1
YAML
  if [ -n "$run_id" ]; then
    kubectl -n "$namespace" patch ephemeralrunner "$name" --subresource=status --type=merge \
      -p "{\"status\": {\"phase\": \"Running\", \"runnerName\": \"${name}\", \"jobRequestId\": ${run_id}0, \"workflowRunId\": ${run_id}, \"jobRepositoryName\": \"example-org/example-repo\", \"jobDisplayName\": \"build\"}}" >/dev/null
  fi
}

runner_exists() {
  kubectl -n "$namespace" get ephemeralrunner "$1" >/dev/null 2>&1
}

log "Creating kind cluster $cluster"
kind create cluster --name "$cluster" --kubeconfig "$KUBECONFIG" --wait 120s >/dev/null

log "Applying ARC's CRDs"
helm show crds "$controller_chart" 2>/dev/null | kubectl apply --server-side -f - >/dev/null
kubectl wait --for=condition=established crd/ephemeralrunners.actions.github.com --timeout=60s >/dev/null

log "Building and loading the image the reaper runs in"
docker build -q -f "$repo_root/pull-secret-renewer/Dockerfile" -t "$image" "$repo_root" >/dev/null
kind load docker-image "$image" --name "$cluster" >/dev/null

log "Starting the GitHub stand-in"
kubectl create namespace "$namespace" >/dev/null
kubectl create namespace "$other_namespace" >/dev/null
kubectl -n "$namespace" create configmap stub-github --from-file="$repo_root/tests/runner_reaper/stub_github.py" >/dev/null
kubectl -n "$namespace" create deployment stub-github --image="$stub_image" --port=8080 -- python3 /stub/stub_github.py >/dev/null
kubectl -n "$namespace" patch deployment stub-github --type=json -p '[
  {"op": "add", "path": "/spec/template/spec/volumes", "value": [{"name": "stub", "configMap": {"name": "stub-github"}}]},
  {"op": "add", "path": "/spec/template/spec/containers/0/volumeMounts", "value": [{"name": "stub", "mountPath": "/stub"}]}
]' >/dev/null
kubectl -n "$namespace" expose deployment stub-github --port=8080 >/dev/null
kubectl -n "$namespace" rollout status deployment/stub-github --timeout=120s >/dev/null

log "Creating the App Secret and the runners"
openssl genrsa -out "$work/app.pem" 2048 2>/dev/null
kubectl -n "$namespace" create secret generic "$app_secret" \
  --from-literal=github_app_id=12345 --from-literal=github_app_installation_id=67890 \
  --from-file=github_app_private_key="$work/app.pem" >/dev/null
create_runner idle-runner
create_runner hung-runner 201
create_runner cancelled-runner 202
create_runner busy-runner 203

log "Rendering and applying the role's reaper manifests"
ANSIBLE_COLLECTIONS_PATH="$repo_root/playbooks/collections${ANSIBLE_COLLECTIONS_PATH:+:$ANSIBLE_COLLECTIONS_PATH}" \
  "$ansible_playbook" "$repo_root/tests/runner_reaper/render.yml" \
  -e reaper_namespace="$namespace" -e github_runner_arc_runner_reaper_image="$image" \
  -e github_runner_arc_runner_reaper_max_job_seconds="$bound" -e reaper_output="$work/reaper.yaml" >/dev/null
kubectl apply -f "$work/reaper.yaml" >/dev/null
# The test points the Job at the stand-in; everything else is as the role renders it.
kubectl -n "$namespace" patch cronjob "$reaper" --type=json -p "[{\"op\": \"add\", \"path\": \"/spec/jobTemplate/spec/template/spec/containers/0/env/-\", \"value\": {\"name\": \"GITHUB_API_URL\", \"value\": \"http://stub-github.${namespace}.svc:8080\"}}]" >/dev/null

log "Checking the ServiceAccount's permissions"
as="system:serviceaccount:${namespace}:${reaper}"
check_can() {
  local expected="$1"
  shift
  local answer
  answer="$(kubectl auth can-i --as="$as" "$@" 2>/dev/null || true)"
  [ "$answer" = "$expected" ] || fail "can-i $* answered '$answer', expected '$expected'"
}
check_can yes list ephemeralrunners.actions.github.com -n "$namespace"
check_can yes delete ephemeralrunners.actions.github.com -n "$namespace"
check_can no patch ephemeralrunners.actions.github.com -n "$namespace"
check_can no create ephemeralrunners.actions.github.com -n "$namespace"
check_can no list ephemeralrunners.actions.github.com -n "$other_namespace"
check_can no delete ephemeralrunners.actions.github.com -n "$other_namespace"
check_can no delete pods -n "$namespace"
check_can no get "secret/$app_secret" -n "$namespace"
check_can no list secrets -n "$namespace"

log "Reaping: the hung and cancelled runners should go, the idle and busy ones stay"
[ "$(run_job reap-1)" = complete ] || fail "the reaper Job did not complete"
runner_exists hung-runner && fail "the runner whose job ran past the bound is still there"
runner_exists cancelled-runner && fail "the runner whose run was cancelled is still there"
runner_exists idle-runner || fail "the idle runner was deleted"
runner_exists busy-runner || fail "the runner whose job is within the bound was deleted"
kubectl -n "$namespace" logs job/reap-1 > "$work/reap-1.log"
grep -q "deleted ${namespace}/hung-runner: run 201 in example-org/example-repo: job 9201 has been in_progress for .*over the bound of ${bound}s" "$work/reap-1.log" \
  || fail "the log does not say why the hung runner was deleted: $(cat "$work/reap-1.log")"
grep -q "deleted ${namespace}/cancelled-runner: run 202 in example-org/example-repo is cancelled" "$work/reap-1.log" \
  || fail "the log does not say why the cancelled runner was deleted: $(cat "$work/reap-1.log")"
kubectl -n "$namespace" logs deploy/stub-github > "$work/stub.log"
[ "$(jq -sc 'map(select(.method == "POST"))[0].body' "$work/stub.log")" = '{"permissions":{"actions":"read"}}' ] || fail "the mint did not ask for actions: read only"
jq -se 'map(select(.method == "GET")) | all(.authorization == "Bearer ghs_stubreapertoken")' "$work/stub.log" >/dev/null || fail "a run was read without the minted token"
jq -se '[.[] | select(.method == "GET") | .path | capture("/runs/(?<id>[0-9]+)").id] | unique == ["201", "202", "203"]' "$work/stub.log" >/dev/null || fail "the reaper read runs other than those of the three assigned runners"

log "Refusing the token: the Job should fail and delete nothing"
create_runner second-hung-runner 201
kubectl -n "$namespace" patch secret "$app_secret" --type=merge -p "{\"data\": {\"github_app_installation_id\": \"$(printf 999 | base64)\"}}" >/dev/null
# The kubelet refreshes a mounted Secret on its own sync period; a new pod reads it fresh, which is what each Job starts.
[ "$(run_job reap-refused)" = failed ] || fail "a refused token did not fail the Job"
runner_exists second-hung-runner || fail "a pass whose token was refused deleted a runner"
kubectl -n "$namespace" logs job/reap-refused 2>/dev/null | grep -q "The App needs the actions: read permission.*deleted nothing" || fail "the refused Job did not name the permission it needs"

log "PASS"
