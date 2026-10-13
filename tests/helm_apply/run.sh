#!/usr/bin/env bash
# Integration test for how the github_runner_arc role upgrades a scale-set release once something else has changed its maxRunners, as the autoscaler's kubectl patch does. Runs the role's own Helm version read and scale-set helm task (copied verbatim from roles/github_runner_arc/tasks into a task file) with Helm 4 and with Helm 3:
#
# - Helm 4: a release the role installed, then patched by kubectl, is upgraded to a different maxRunners. A plain helm upgrade without --force-conflicts is shown to fail on that field first, so the test reproduces the conflict it guards against.
# - Helm 4: a release first installed with client-side apply, then patched, is upgraded by the role, which needs server_side true and not auto.
# - Helm 3: the role installs and upgrades a patched release. kubernetes.core.helm refuses server_side in any form, and force_conflicts only when it is true, below Helm 4, so this phase fails if server_side or a true force_conflicts reaches the module. A false force_conflicts is accepted there, so this phase cannot show that the role omits it rather than passing false; only tests/unit/test_helm_apply.py's render test guards that.
#
# Every phase runs twice: with the kubernetes.core the environment provides, and with the oldest version galaxy.yml accepts, installed in KUBERNETES_CORE_FLOOR.
#
# No controller is installed, only the CRDs, so nothing reconciles the scale sets and no GitHub credential is needed.
#
# Usage: HELM3=<helm 3 binary> HELM4=<helm 4 binary> KUBERNETES_CORE_FLOOR=<collections directory> tests/helm_apply/run.sh. Needs Docker, kind, kubectl, python3 with PyYAML, and ansible-playbook (ANSIBLE_PLAYBOOK overrides which) with the kubernetes.core collection (the version galaxy.yml accepts) and the kubernetes Python package; KUBERNETES_CORE_FLOOR is a collections directory holding kubernetes.core at exactly the lowest version galaxy.yml accepts. Creates a kind cluster named grtest-helm-apply and removes it on exit; GRTEST_KEEP=1 keeps it.
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
kubernetes_core_floor="${KUBERNETES_CORE_FLOOR:?KUBERNETES_CORE_FLOOR must name a collections directory holding the lowest kubernetes.core galaxy.yml accepts}"
default_collections="${ANSIBLE_COLLECTIONS_PATH:-$HOME/.ansible/collections:/usr/share/ansible/collections}"
# Without this, a phase whose collections path holds no kubernetes.core would fall back to one in Python's own site-packages (the full ansible package ships one) and test that version instead of failing; a kubernetes.core in a configured path already wins over site-packages.
export ANSIBLE_LOCALHOST_WARNING=false ANSIBLE_INVENTORY_UNPARSED_WARNING=false ANSIBLE_COLLECTIONS_SCAN_SYS_PATH=false

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

# Runs the role's tasks with the Helm of the given major on PATH, for one release at one maxRunners, resolving kubernetes.core from the calling run_phases' collections path; prints the play's output.
role_upgrade() {
  local major="$1" release="$2" namespace="$3" max_runners="$4"
  printf '%s' "{\"helm_tasks_file\": \"$work/helm-tasks.yml\", \"release\": \"$release\", \"namespace\": \"$namespace\", \"chart_version\": \"$chart_version\", \"kubeconfig\": \"$KUBECONFIG\", \"scale_set_values\": $(scale_set_values "$max_runners")}" > "$work/extra.json"
  PATH="$work/helm$major:$PATH" ANSIBLE_COLLECTIONS_PATH="$repo_root/playbooks/collections:$collections" \
    "$ansible_playbook" "$repo_root/tests/helm_apply/upgrade.yml" -e "@$work/extra.json" 2>&1
}

# The kubernetes.core version a collections path resolves to, as ansible-galaxy reports it.
kubernetes_core_version() {
  ANSIBLE_COLLECTIONS_PATH="$1" ansible-galaxy collection list kubernetes.core 2>/dev/null | awk '$1 == "kubernetes.core" {print $2; exit}'
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

# Runs every phase with the role's tasks resolving kubernetes.core from the given collections path; tag keeps each run's namespaces and logs apart.
run_phases() {
  local tag="$1" collections="$2" version
  local ns4="grtest-helm4-$tag" ns4_csa="grtest-helm4-csa-$tag" ns3="grtest-helm3-$tag"
  version="$(kubernetes_core_version "$collections")"
  [ -n "$version" ] || fail "no kubernetes.core in $collections"
  log "[kubernetes.core $version] Helm 4: the role installs a release, kubectl patches its maxRunners"
  kubectl create namespace "$ns4" >/dev/null
  role_upgrade 4 grtest-runners "$ns4" 3 > "$work/helm4-install-$tag.log" || fail "the role could not install the release with Helm 4: $(tail -20 "$work/helm4-install-$tag.log")"
  [ "$(live_max_runners grtest-runners "$ns4")" = 3 ] || fail "maxRunners after install is not 3"
  "$work/helm4/helm" get metadata grtest-runners -n "$ns4" | grep -q "APPLY_METHOD: server-side apply" || fail "Helm 4 did not install the release server-side"
  autoscaler_patch grtest-runners "$ns4" 5

  log "[kubernetes.core $version] Helm 4: a plain upgrade without --force-conflicts fails on maxRunners"
  if output="$(printf '%s' "$(scale_set_values 2)" | "$work/helm4/helm" upgrade grtest-runners "$charts/gha-runner-scale-set" --version "$chart_version" -n "$ns4" -f - 2>&1)"; then
    fail "a plain Helm 4 upgrade succeeded against the patched maxRunners, so this test no longer reproduces the conflict: $output"
  fi
  echo "$output" | grep -q "conflict" || fail "the plain upgrade failed, but not on a conflict: $output"

  log "[kubernetes.core $version] Helm 4: the role upgrades the patched release to a different maxRunners"
  role_upgrade 4 grtest-runners "$ns4" 2 > "$work/helm4-upgrade-$tag.log" || fail "the role's upgrade failed with Helm 4: $(tail -20 "$work/helm4-upgrade-$tag.log")"
  [ "$(live_max_runners grtest-runners "$ns4")" = 2 ] || fail "maxRunners after the role's upgrade is $(live_max_runners grtest-runners "$ns4"), not 2"

  log "[kubernetes.core $version] Helm 4: a release first installed with client-side apply, then patched, is upgraded by the role"
  kubectl create namespace "$ns4_csa" >/dev/null
  printf '%s' "$(scale_set_values 3)" | "$work/helm4/helm" install grtest-runners "$charts/gha-runner-scale-set" --version "$chart_version" -n "$ns4_csa" --server-side=false -f - >/dev/null
  autoscaler_patch grtest-runners "$ns4_csa" 5
  role_upgrade 4 grtest-runners "$ns4_csa" 2 > "$work/helm4-csa-$tag.log" || fail "the role could not upgrade a client-side release with Helm 4: $(tail -20 "$work/helm4-csa-$tag.log")"
  [ "$(live_max_runners grtest-runners "$ns4_csa")" = 2 ] || fail "maxRunners after upgrading the client-side release is not 2"
  "$work/helm4/helm" get metadata grtest-runners -n "$ns4_csa" | grep -q "APPLY_METHOD: server-side apply" || fail "the client-side release did not move to server-side apply"

  log "[kubernetes.core $version] Helm 3: the role installs and upgrades a patched release, passing neither option"
  kubectl create namespace "$ns3" >/dev/null
  role_upgrade 3 grtest-runners "$ns3" 3 > "$work/helm3-install-$tag.log" || fail "the role could not install the release with Helm 3: $(tail -20 "$work/helm3-install-$tag.log")"
  autoscaler_patch grtest-runners "$ns3" 5
  role_upgrade 3 grtest-runners "$ns3" 2 > "$work/helm3-upgrade-$tag.log" || fail "the role's upgrade failed with Helm 3: $(tail -20 "$work/helm3-upgrade-$tag.log")"
  [ "$(live_max_runners grtest-runners "$ns3")" = 2 ] || fail "maxRunners after the Helm 3 upgrade is not 2"
}

floor="$(python3 - "$repo_root/galaxy.yml" <<'PY'
import re, sys
import yaml
spec = yaml.safe_load(open(sys.argv[1]))["dependencies"]["kubernetes.core"]
match = re.fullmatch(r">=\s*(\d+\.\d+\.\d+)", str(spec).strip())
assert match, f"galaxy.yml's kubernetes.core requirement {spec!r} is not a plain lower bound"
print(match.group(1))
PY
)"
[ "$(kubernetes_core_version "$kubernetes_core_floor")" = "$floor" ] || fail "KUBERNETES_CORE_FLOOR holds kubernetes.core $(kubernetes_core_version "$kubernetes_core_floor"), not galaxy.yml's lowest accepted $floor"

run_phases current "$default_collections"
run_phases floor "$kubernetes_core_floor"

log "PASS"
