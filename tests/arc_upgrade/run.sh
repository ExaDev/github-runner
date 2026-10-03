#!/usr/bin/env bash
# Integration test for the github_runner_arc role's CRD handling when ARC is upgraded to a newer chart: installs the controller chart at the old version, which installs its CRDs, shows that the newer scale-set chart's AutoscalingRunnerSet is then rejected by the API server for a field the old CRD does not declare (the failure that once removed every scale set), applies the CRDs the way the role does, and shows the same object is accepted and the newer controller runs.
#
# A scale set needs no GitHub credentials to be validated: its manifests are rendered with helm template and sent with a server-side dry run, which runs the API server's schema validation without creating anything.
#
# Usage: tests/arc_upgrade/run.sh. Needs Docker, kind, kubectl, helm and ansible-playbook (ANSIBLE_PLAYBOOK overrides which) with the kubernetes.core collection and the kubernetes Python package. Creates a kind cluster named grtest-arc-upgrade and removes it on exit; GRTEST_KEEP=1 keeps it.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ansible_playbook="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
cluster=grtest-arc-upgrade
charts=oci://ghcr.io/actions/actions-runner-controller-charts
# The newest chart that predates the schema change, and the first that needs it: the old controller's CRDs reject the new scale-set chart, which is what the test exercises. Update both when a newer pair is worth covering.
old_version=0.14.2
new_version=0.15.0
controller_namespace=grtest-arc-systems
runner_namespace=grtest-arc-runners
work="$(mktemp -d "${TMPDIR:-/tmp}/grtest-arc-upgrade.XXXXXX")"
export KUBECONFIG="$work/kubeconfig"

log() { echo "==> $*"; }
fail() {
  echo "FAIL: $*" >&2
  kubectl get crd -o name >&2 || true
  kubectl -n "$controller_namespace" get pods >&2 || true
  exit 1
}
cleanup() {
  if [ "${GRTEST_KEEP:-0}" != 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || true
    rm -rf "$work"
  fi
}
trap cleanup EXIT

# Renders the scale-set chart at a version and sends it to the API server for validation only; prints the server's answer and returns its status. The controller's service account is named because helm template cannot look it up in the cluster.
dry_run_scale_set() {
  local manifests
  manifests="$(helm template grtest-runners "$charts/gha-runner-scale-set" --version "$1" --namespace "$runner_namespace" \
    --set githubConfigUrl=https://github.com/example --set githubConfigSecret.github_token=unused \
    --set controllerServiceAccount.name=arc-gha-rs-controller --set controllerServiceAccount.namespace="$controller_namespace" 2>&1)" \
    || { echo "helm template failed: $manifests"; exit 1; }
  printf '%s\n' "$manifests" | kubectl apply --server-side --dry-run=server --force-conflicts -f - 2>&1
}

log "Creating the kind cluster"
kind create cluster --name "$cluster" --kubeconfig "$KUBECONFIG" --wait 120s >/dev/null
kubectl create namespace "$runner_namespace" >/dev/null

log "Installing the controller chart at $old_version, which installs its CRDs"
helm install arc "$charts/gha-runner-scale-set-controller" --version "$old_version" \
  --namespace "$controller_namespace" --create-namespace --wait --timeout 180s >/dev/null

log "Checking that the $new_version scale-set chart is rejected against the $old_version CRDs"
if answer="$(dry_run_scale_set "$new_version")"; then
  fail "the $new_version scale-set chart was accepted by the $old_version CRDs, so this test no longer reproduces the failure; choose a newer version pair. Output: $answer"
fi
echo "$answer" | grep -q "not declared in schema" || fail "the scale-set chart was rejected, but not for an undeclared field: $answer"

log "Applying the controller chart's CRDs at $new_version with the role's task"
"$ansible_playbook" "$repo_root/tests/arc_upgrade/apply-crds.yml" -e "chart_version=$new_version" -e "kubeconfig=$KUBECONFIG" >/dev/null

log "Checking that the same scale-set chart is now accepted"
answer="$(dry_run_scale_set "$new_version")" || fail "the $new_version scale-set chart is still rejected after the CRDs were applied: $answer"

log "Upgrading the controller to $new_version"
helm upgrade arc "$charts/gha-runner-scale-set-controller" --version "$new_version" \
  --namespace "$controller_namespace" --wait --timeout 180s >/dev/null

log "Applying the CRDs again, which must change nothing"
"$ansible_playbook" "$repo_root/tests/arc_upgrade/apply-crds.yml" -e "chart_version=$new_version" -e "kubeconfig=$KUBECONFIG" 2>&1 | grep -E "ok=|changed=" | tee "$work/second-run.txt"
grep -q "changed=0" "$work/second-run.txt" || fail "applying the CRDs a second time reported changes"

log "PASS"
