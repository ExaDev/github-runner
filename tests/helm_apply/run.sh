#!/usr/bin/env bash
# Integration test for how the github_runner_arc role upgrades a scale-set release once something else has changed its maxRunners, as the autoscaler's kubectl patch does. Runs the role's own Helm version read and scale-set helm task (copied verbatim from roles/github_runner_arc/tasks into a task file) with Helm 4 and with Helm 3:
#
# - Helm 4: a release the role installed, then patched by kubectl, is upgraded to a different maxRunners. A plain helm upgrade without --force-conflicts is shown to fail on that field first, so the test reproduces the conflict it guards against.
# - Helm 4: a release first installed with client-side apply, then patched, is upgraded by the role, which needs server_side true and not auto.
# - Helm 3: the role installs and upgrades a patched release, which fails if either option reaches the module there.
#
# No controller is installed, only the CRDs, so nothing reconciles the scale sets and no GitHub credential is needed.
#
# Usage: HELM3=<helm 3 binary> HELM4=<helm 4 binary> tests/helm_apply/run.sh. Needs Docker, kind, kubectl, python3 with PyYAML, and ansible-playbook (ANSIBLE_PLAYBOOK overrides which) with the kubernetes.core collection (6.5.0 or later) and the kubernetes Python package. Creates a kind cluster named grtest-helm-apply and removes it on exit; GRTEST_KEEP=1 keeps it.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ansible_playbook="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
# Indexed by Helm major version.
helm_binary=()
helm_binary[3]="${HELM3:?HELM3 must name a Helm 3 binary}"
helm_binary[4]="${HELM4:?HELM4 must name a Helm 4 binary}"
cluster=grtest-helm-apply
charts=oci://ghcr.io/actions/actions-runner-controller-charts
# The chart pair the fleet runs; any version with the AutoscalingRunnerSet maxRunners field would do.
chart_version=0.15.0
controller_namespace=grtest-arc-systems
work="$(mktemp -d "${TMPDIR:-/tmp}/grtest-helm-apply.XXXXXX")"
export KUBECONFIG="$work/kubeconfig"
export ANSIBLE_COLLECTIONS_PATH="$repo_root/playbooks/collections:${ANSIBLE_COLLECTIONS_PATH:-$HOME/.ansible/collections:/usr/share/ansible/collections}"
export ANSIBLE_LOCALHOST_WARNING=false ANSIBLE_INVENTORY_UNPARSED_WARNING=false

log() { echo "==> $*"; }
fail() {
  echo "FAIL: $*" >&2
  kubectl get autoscalingrunnersets -A -o wide >&2 || true
  exit 1
}
cleanup() {
  if [ "${GRTEST_KEEP:-0}" != 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || true
    rm -rf "$work"
  fi
}
trap cleanup EXIT

# One directory per Helm major holding a helm that is that binary, so a step chooses its Helm through PATH, the way the module and the version read find it.
for major in 3 4; do
  mkdir -p "$work/helm$major"
  ln -s "${helm_binary[$major]}" "$work/helm$major/helm"
  "$work/helm$major/helm" version --short | grep -q "^v$major\." || fail "HELM$major is not a Helm $major binary"
done

log "Copying the role's version read and scale-set helm task"
python3 - "$repo_root" "$work/helm-tasks.yml" <<'PY'
import pathlib, sys
import yaml

root, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])

def walk(tasks):
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from walk(task.get(key))

main = list(walk(yaml.safe_load((root / "roles/github_runner_arc/tasks/main.yml").read_text())))
version = [task for task in main if task.get("register") == "github_runner_arc_helm_version" or "github_runner_arc_helm_server_side" in task.get("ansible.builtin.set_fact", {})]
helm = [task for task in walk(yaml.safe_load((root / "roles/github_runner_arc/tasks/install_scale_set_profile.yml").read_text())) if "kubernetes.core.helm" in task]
assert len(version) == 2, f"expected the version read and the fact in main.yml, found {len(version)}"
assert len(helm) == 1, f"expected one helm task in install_scale_set_profile.yml, found {len(helm)}"
out.write_text(yaml.safe_dump(version + helm, sort_keys=False))
PY

# Runs the role's tasks with the Helm of the given major on PATH, for one release at one maxRunners; prints the play's output.
role_upgrade() {
  local major="$1" release="$2" namespace="$3" max_runners="$4"
  printf '%s' "{\"helm_tasks_file\": \"$work/helm-tasks.yml\", \"release\": \"$release\", \"namespace\": \"$namespace\", \"chart_version\": \"$chart_version\", \"kubeconfig\": \"$KUBECONFIG\", \"scale_set_values\": $(scale_set_values "$max_runners")}" > "$work/extra.json"
  PATH="$work/helm$major:$PATH" "$ansible_playbook" "$repo_root/tests/helm_apply/upgrade.yml" -e "@$work/extra.json" 2>&1
}

# The scale set's Helm values: no GitHub credential is used, since no controller runs, and the controller's service account is named because there is no controller to look it up from.
scale_set_values() {
  printf '%s' "{\"maxRunners\": $1, \"minRunners\": 0, \"githubConfigUrl\": \"https://github.com/example\", \"githubConfigSecret\": {\"github_token\": \"unused\"}, \"controllerServiceAccount\": {\"name\": \"arc-gha-rs-controller\", \"namespace\": \"$controller_namespace\"}}"
}

live_max_runners() { kubectl get autoscalingrunnerset "$1" -n "$2" -o jsonpath='{.spec.maxRunners}'; }

# Changes maxRunners the way the autoscaler does, which makes kubectl-patch that field's manager.
autoscaler_patch() { kubectl patch autoscalingrunnerset "$1" -n "$2" --type merge -p "{\"spec\":{\"maxRunners\":$3}}" >/dev/null; }

log "Creating the kind cluster and applying the ARC CRDs (no controller)"
kind create cluster --name "$cluster" --kubeconfig "$KUBECONFIG" --wait 120s >/dev/null
"$work/helm4/helm" show crds "$charts/gha-runner-scale-set-controller" --version "$chart_version" | kubectl apply --server-side -f - >/dev/null

log "Helm 4: the role installs a release, kubectl patches its maxRunners"
kubectl create namespace grtest-helm4 >/dev/null
role_upgrade 4 grtest-runners grtest-helm4 3 > "$work/helm4-install.log" || fail "the role could not install the release with Helm 4: $(tail -20 "$work/helm4-install.log")"
[ "$(live_max_runners grtest-runners grtest-helm4)" = 3 ] || fail "maxRunners after install is not 3"
"$work/helm4/helm" get metadata grtest-runners -n grtest-helm4 | grep -q "APPLY_METHOD: server-side apply" || fail "Helm 4 did not install the release server-side"
autoscaler_patch grtest-runners grtest-helm4 5

log "Helm 4: a plain upgrade without --force-conflicts fails on maxRunners"
if output="$(printf '%s' "$(scale_set_values 2)" | "$work/helm4/helm" upgrade grtest-runners "$charts/gha-runner-scale-set" --version "$chart_version" -n grtest-helm4 -f - 2>&1)"; then
  fail "a plain Helm 4 upgrade succeeded against the patched maxRunners, so this test no longer reproduces the conflict: $output"
fi
echo "$output" | grep -q "conflict" || fail "the plain upgrade failed, but not on a conflict: $output"

log "Helm 4: the role upgrades the patched release to a different maxRunners"
role_upgrade 4 grtest-runners grtest-helm4 2 > "$work/helm4-upgrade.log" || fail "the role's upgrade failed with Helm 4: $(tail -20 "$work/helm4-upgrade.log")"
[ "$(live_max_runners grtest-runners grtest-helm4)" = 2 ] || fail "maxRunners after the role's upgrade is $(live_max_runners grtest-runners grtest-helm4), not 2"

log "Helm 4: a release first installed with client-side apply, then patched, is upgraded by the role"
kubectl create namespace grtest-helm4-csa >/dev/null
printf '%s' "$(scale_set_values 3)" | "$work/helm4/helm" install grtest-runners "$charts/gha-runner-scale-set" --version "$chart_version" -n grtest-helm4-csa --server-side=false -f - >/dev/null
autoscaler_patch grtest-runners grtest-helm4-csa 5
role_upgrade 4 grtest-runners grtest-helm4-csa 2 > "$work/helm4-csa.log" || fail "the role could not upgrade a client-side release with Helm 4: $(tail -20 "$work/helm4-csa.log")"
[ "$(live_max_runners grtest-runners grtest-helm4-csa)" = 2 ] || fail "maxRunners after upgrading the client-side release is not 2"
"$work/helm4/helm" get metadata grtest-runners -n grtest-helm4-csa | grep -q "APPLY_METHOD: server-side apply" || fail "the client-side release did not move to server-side apply"

log "Helm 3: the role installs and upgrades a patched release, passing neither option"
kubectl create namespace grtest-helm3 >/dev/null
role_upgrade 3 grtest-runners grtest-helm3 3 > "$work/helm3-install.log" || fail "the role could not install the release with Helm 3: $(tail -20 "$work/helm3-install.log")"
autoscaler_patch grtest-runners grtest-helm3 5
role_upgrade 3 grtest-runners grtest-helm3 2 > "$work/helm3-upgrade.log" || fail "the role's upgrade failed with Helm 3: $(tail -20 "$work/helm3-upgrade.log")"
[ "$(live_max_runners grtest-runners grtest-helm3)" = 2 ] || fail "maxRunners after the Helm 3 upgrade is not 2"

log "PASS"
