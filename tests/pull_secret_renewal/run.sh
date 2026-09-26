#!/usr/bin/env bash
# Integration test for the github_runner_arc role's App-sourced pull Secret renewal: renders the role's renewal manifests, applies them to a kind cluster with the renewer image built from this checkout, and runs the CronJob's Job against a stand-in for GitHub's token endpoint (stub_github.py). Asserts the ServiceAccount can only get and patch the pull Secret, that a Job writes the minted token into it as the CronJob's non-root, read-only container, and that a refused mint fails the Job and leaves the Secret as it was. The registry check is covered by tests/unit/test_renew_pull_secret.py; here the org's image is on another registry, so the Job has nothing to verify.
#
# Usage: tests/pull_secret_renewal/run.sh. Needs Docker, kind, kubectl, openssl and ansible-playbook (ANSIBLE_PLAYBOOK overrides which). Creates a kind cluster named grtest-renewal and removes it on exit unless GRTEST_KEEP=1.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ansible_playbook="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
cluster=grtest-renewal
namespace=grtest-runners
image=grtest/pull-secret-renewer:test
stub_image=python:3.13-alpine
pull_secret=ghcr-pull
renewer="${pull_secret}-renewer"
app_secret=example-org-github-app
job_timeout=180s
work="$(mktemp -d "${TMPDIR:-/tmp}/grtest-renewal.XXXXXX")"
export KUBECONFIG="$work/kubeconfig"

log() { echo "==> $*"; }
fail() {
  echo "FAIL: $*" >&2
  kubectl -n "$namespace" get jobs,pods >&2 || true
  kubectl -n "$namespace" logs -l app.kubernetes.io/name=pull-secret-renewer --tail=40 --prefix >&2 || true
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

# The password in the pull Secret's registry auth, decoded.
pull_password() {
  kubectl -n "$namespace" get secret "$pull_secret" -o jsonpath='{.data.\.dockerconfigjson}' | base64 -d | jq -r '.auths["ghcr.io"].password'
}

# Runs one Job from the CronJob and waits for it to finish either way, printing complete or failed.
run_job() {
  local job="$1"
  kubectl -n "$namespace" create job "$job" --from="cronjob/$renewer" >/dev/null
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

log "Creating kind cluster $cluster"
kind create cluster --name "$cluster" --kubeconfig "$KUBECONFIG" --wait 120s >/dev/null

log "Building and loading the renewer image"
docker build -q -f "$repo_root/pull-secret-renewer/Dockerfile" -t "$image" "$repo_root" >/dev/null
kind load docker-image "$image" --name "$cluster" >/dev/null

log "Starting the GitHub stand-in"
kubectl create namespace "$namespace" >/dev/null
kubectl -n "$namespace" create configmap stub-github --from-file="$repo_root/tests/pull_secret_renewal/stub_github.py" >/dev/null
kubectl -n "$namespace" create deployment stub-github --image="$stub_image" --port=8080 -- python3 /stub/stub_github.py >/dev/null
kubectl -n "$namespace" patch deployment stub-github --type=json -p '[
  {"op": "add", "path": "/spec/template/spec/volumes", "value": [{"name": "stub", "configMap": {"name": "stub-github"}}]},
  {"op": "add", "path": "/spec/template/spec/containers/0/volumeMounts", "value": [{"name": "stub", "mountPath": "/stub"}]}
]' >/dev/null
kubectl -n "$namespace" expose deployment stub-github --port=8080 >/dev/null
kubectl -n "$namespace" rollout status deployment/stub-github --timeout=120s >/dev/null

log "Creating the App Secret, the first pull Secret and an unrelated Secret"
openssl genrsa -out "$work/app.pem" 2048 2>/dev/null
kubectl -n "$namespace" create secret generic "$app_secret" \
  --from-literal=github_app_id=12345 --from-literal=github_app_installation_id=67890 \
  --from-file=github_app_private_key="$work/app.pem" >/dev/null
kubectl -n "$namespace" create secret docker-registry "$pull_secret" \
  --docker-server=ghcr.io --docker-username=x-access-token --docker-password=first-token >/dev/null
kubectl -n "$namespace" create secret generic unrelated --from-literal=value=unrelated >/dev/null

log "Rendering and applying the role's renewal manifests"
ANSIBLE_COLLECTIONS_PATH="$repo_root/playbooks/collections${ANSIBLE_COLLECTIONS_PATH:+:$ANSIBLE_COLLECTIONS_PATH}" \
  "$ansible_playbook" "$repo_root/tests/pull_secret_renewal/render.yml" \
  -e renewal_namespace="$namespace" -e github_runner_arc_image_pull_secret_renewer_image="$image" -e renewal_org_image=registry.example.com/example-org/runner:1.0 \
  -e renewal_output="$work/renewal.yaml" >/dev/null
kubectl apply -f "$work/renewal.yaml" >/dev/null
# The test points the Job at the stand-in; everything else is as the role renders it.
kubectl -n "$namespace" patch cronjob "$renewer" --type=json -p "[{\"op\": \"add\", \"path\": \"/spec/jobTemplate/spec/template/spec/containers/0/env/-\", \"value\": {\"name\": \"GITHUB_API_URL\", \"value\": \"http://stub-github.${namespace}.svc:8080\"}}]" >/dev/null

log "Checking the ServiceAccount's permissions"
as="system:serviceaccount:${namespace}:${renewer}"
check_can() {
  local expected="$1"
  shift
  local answer
  answer="$(kubectl -n "$namespace" auth can-i --as="$as" "$@" 2>/dev/null || true)"
  [ "$answer" = "$expected" ] || fail "can-i $* answered '$answer', expected '$expected'"
}
check_can yes get "secret/$pull_secret"
check_can yes patch "secret/$pull_secret"
check_can no get "secret/$app_secret"
check_can no get secret/unrelated
check_can no patch secret/unrelated
check_can no create secrets
check_can no list secrets

log "Renewing: the Job should write the minted token"
[ "$(run_job renew-1)" = complete ] || fail "the first renewal Job did not complete"
[ "$(pull_password)" = ghs_stubtoken1 ] || fail "the pull Secret does not hold the minted token"
kubectl -n "$namespace" get secret "$pull_secret" -o json > "$work/secret.json"
[ "$(jq -r '.metadata.annotations["github-runner.exadev/pull-token-expires-at"]' "$work/secret.json")" = 2030-01-01T00:00:00Z ] || fail "the expiry annotation is missing or wrong"
[ "$(jq -r '.metadata.annotations["kubectl.kubernetes.io/last-applied-configuration"] // "none"' "$work/secret.json")" = none ] || fail "the Secret carries a last-applied annotation holding the token"
[ "$(base64 -d <<< "$(jq -r '.data[".dockerconfigjson"]' "$work/secret.json")" | jq -r '.auths["ghcr.io"].username')" = x-access-token ] || fail "the username is not x-access-token"
request="$(kubectl -n "$namespace" logs deploy/stub-github | head -n 1)"
[ "$(jq -c '.body' <<< "$request")" = '{"permissions":{"packages":"read"}}' ] || fail "the mint did not ask for packages: read only: $request"
[ "$(jq -r '.claims.iss' <<< "$request")" = 12345 ] || fail "the JWT was not issued by the App: $request"
[ "$(jq -r '.path' <<< "$request")" = /app/installations/67890/access_tokens ] || fail "the mint went to the wrong installation: $request"

log "Renewing again: the Job should replace the token"
[ "$(run_job renew-2)" = complete ] || fail "the second renewal Job did not complete"
[ "$(pull_password)" = ghs_stubtoken2 ] || fail "the second renewal did not replace the token"

log "Refusing the mint: the Job should fail and leave the Secret alone"
kubectl -n "$namespace" patch secret "$app_secret" --type=merge -p "{\"data\": {\"github_app_installation_id\": \"$(printf 999 | base64)\"}}" >/dev/null
# The kubelet refreshes a mounted Secret on its own sync period; a new pod reads it fresh, which is what each Job starts.
[ "$(run_job renew-refused)" = failed ] || fail "a refused mint did not fail the Job"
[ "$(pull_password)" = ghs_stubtoken2 ] || fail "a refused mint changed the pull Secret"
kubectl -n "$namespace" logs job/renew-refused 2>/dev/null | grep -q "leaving ${namespace}/${pull_secret} unchanged" || fail "the refused Job did not say it left the Secret unchanged"

log "PASS"
