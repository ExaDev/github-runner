#!/usr/bin/env bash
# Integration test for scale-set profile capabilities: builds the reference Node.js tool image from tool-images/node and the reference libpq system-library image from tool-images/libpq, renders a profile carrying them (the tool with a path, so the job-started hook puts it on PATH; the library with an extra variable) and a hook capability through the role's own tasks, renders the gha-runner-scale-set chart with those values, and checks the AutoscalingRunnerSet against the ARC CRDs with a server-side dry run in a kind cluster. It then starts a pod from the chart's own runner pod template in that cluster, with the runner's command replaced by what the runner does at job start (run the ACTIONS_RUNNER_HOOK_JOB_STARTED hook with GITHUB_PATH and GITHUB_ENV) followed by a step that asserts the cached Node.js is on PATH and that @actions/tool-cache's find, the lookup actions/setup-node makes before downloading, returns the copy the init container put in the tool cache, and that libpq's programs run from the sysroot its init container filled, with the search paths and the extra variable the hook set.
#
# Usage: tests/capabilities/run.sh. Needs Docker, kind, kubectl, helm, python3 with PyYAML, and ansible-playbook (ANSIBLE_PLAYBOOK overrides which); reaches the network for the chart, the runner image and the npm package. GRTEST_SCALESET_CHART_VERSION pins the scale-set chart and the controller chart its CRDs come from (default: the latest). Creates a kind cluster named grtest-capabilities and removes it on exit unless GRTEST_KEEP=1.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ansible_playbook="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
cluster=grtest-capabilities
namespace=arc-runners-example
tool_image=grtest/toolcache-node:test
library_image=grtest/sysroot-libpq:test
sysroot=/opt/sysroot
runner_image=ghcr.io/actions/actions-runner:latest
charts=oci://ghcr.io/actions/actions-runner-controller-charts
chart_version="${GRTEST_SCALESET_CHART_VERSION:-}"
tool_cache_package="@actions/tool-cache@4.0.0"
node_version="$(sed -n 's/^ARG NODE_VERSION=//p' "$repo_root/tool-images/node/Dockerfile")"
work="$(mktemp -d "${TMPDIR:-/tmp}/grtest-capabilities.XXXXXX")"
export KUBECONFIG="$work/kubeconfig"

log() { echo "==> $*"; }
fail() {
  echo "FAIL: $*" >&2
  kubectl -n "$namespace" get pods -o wide >&2 || true
  kubectl -n "$namespace" describe pod runner >&2 || true
  kubectl -n "$namespace" logs runner --all-containers >&2 || true
  exit 1
}
cleanup() {
  if [ "${GRTEST_KEEP:-0}" != 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || true
    rm -rf "$work"
  fi
}
trap cleanup EXIT

[ -n "$node_version" ] || fail "could not read NODE_VERSION from tool-images/node/Dockerfile"
version_flag=()
[ -z "$chart_version" ] || version_flag=(--version "$chart_version")

log "Building the reference Node.js ${node_version} tool image"
docker build -q -t "$tool_image" "$repo_root/tool-images/node" >/dev/null
log "Building the reference libpq system-library image"
docker build -q -t "$library_image" "$repo_root/tool-images/libpq" >/dev/null

log "Creating kind cluster ${cluster}"
kind create cluster --name "$cluster" --kubeconfig "$KUBECONFIG" --wait 120s >/dev/null
kind load docker-image "$tool_image" "$library_image" --name "$cluster" >/dev/null

log "Rendering the profile's values through the role"
# The profile as arc_profiles expands it, and a catalogue with the tool image (put on PATH through its bin directory), the library image (with PGSYSCONFDIR, where libpq looks for its service file, as its extra variable) and a hook capability that records it ran.
python3 - "$repo_root/plugins/filter/arc.py" "$work/extra.json" "$tool_image" "$node_version" "$work/rendered.json" "$library_image" "$sysroot" <<'EOF'
import importlib.util, json, sys
plugin, extra, image, version, output, library_image, sysroot = sys.argv[1:]
spec = importlib.util.spec_from_file_location("arc", plugin)
arc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arc)
result = arc.arc_profiles([{"name": "Example", "image": "unused:1", "scale_set_profiles": [{"max_runners": 1, "capabilities": ["node", "libpq", "marker"]}]}], require_app_id=False)
assert result["errors"] == [], result["errors"]
catalogue = {
    "node": {"image": image, "path": f"node/{version}/bin"},
    "libpq": {"sysroot_image": library_image, "env": {"PGSYSCONFDIR": f"{sysroot}/etc"}},
    "marker": {"job_started_hook": 'echo "CAPABILITY_HOOK_RAN=yes" >> "$GITHUB_ENV"\n'},
}
with open(extra, "w") as out:
    json.dump({"profile": result["profiles"][0], "github_runner_arc_capabilities": catalogue, "render_output": output}, out)
EOF
ANSIBLE_COLLECTIONS_PATH="$repo_root/playbooks/collections" ANSIBLE_LOCALHOST_WARNING=false ANSIBLE_INVENTORY_UNPARSED_WARNING=false \
  "$ansible_playbook" "$repo_root/tests/capabilities/render.yml" -e "@$work/extra.json" >/dev/null
python3 -c 'import json, sys, yaml; rendered = json.load(open(sys.argv[1])); yaml.safe_dump(rendered["values"], open(sys.argv[2], "w")); yaml.safe_dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "github-runner-job-started"}, "data": rendered["job_started"]}, open(sys.argv[3], "w"))' \
  "$work/rendered.json" "$work/values.yaml" "$work/configmap.yaml"

log "Rendering the gha-runner-scale-set chart with those values"
# The same --set values the role passes to every release (tasks/install_scale_set_profile.yml). The chart looks the controller's service account up in a live cluster, which helm template has none of, so it is named here.
helm template example-runners "$charts/gha-runner-scale-set" "${version_flag[@]}" --namespace "$namespace" -f "$work/values.yaml" \
  --set githubConfigUrl=https://github.com/example --set githubConfigSecret=example-github-app \
  --set "template.spec.containers[0].image=$runner_image" \
  --set controllerServiceAccount.name=arc-gha-rs-controller --set controllerServiceAccount.namespace=actions-runner-controller \
  >"$work/chart.yaml" 2>"$work/helm.log" || { cat "$work/helm.log" >&2; fail "helm template failed"; }
python3 - "$work/chart.yaml" "$work/autoscalingrunnerset.yaml" <<'EOF'
import sys, yaml
documents = [document for document in yaml.safe_load_all(open(sys.argv[1])) if document]
sets = [document for document in documents if document["kind"] == "AutoscalingRunnerSet"]
assert len(sets) == 1, [document["kind"] for document in documents]
yaml.safe_dump(sets[0], open(sys.argv[2], "w"))
EOF

log "Checking the AutoscalingRunnerSet against the ARC CRDs"
helm pull "$charts/gha-runner-scale-set-controller" "${version_flag[@]}" --untar --untardir "$work/controller" >/dev/null 2>&1
kubectl apply --server-side -f "$work/controller/gha-runner-scale-set-controller/crds/" >/dev/null
kubectl wait --for condition=established --timeout=60s crd/autoscalingrunnersets.actions.github.com >/dev/null
kubectl create namespace "$namespace" >/dev/null
kubectl apply --dry-run=server -f "$work/autoscalingrunnerset.yaml" >/dev/null || fail "the API server rejected the rendered AutoscalingRunnerSet"

log "Starting a pod from the chart's runner pod template"
kubectl -n "$namespace" apply -f "$work/configmap.yaml" >/dev/null
# The step: what the runner does at job start (it runs the hook named by ACTIONS_RUNNER_HOOK_JOB_STARTED with bash -e and fresh environment files, then applies them), then a job step that uses the tool and asks @actions/tool-cache for it as actions/setup-node does.
cat >"$work/step.sh" <<EOF
set -euo pipefail
export GITHUB_PATH="\$(mktemp)" GITHUB_ENV="\$(mktemp)"
bash -e "\$ACTIONS_RUNNER_HOOK_JOB_STARTED"
while IFS= read -r directory; do PATH="\$directory:\$PATH"; done <"\$GITHUB_PATH"
export PATH
while IFS= read -r line; do export "\${line?}"; done <"\$GITHUB_ENV"
grep -qx CAPABILITY_HOOK_RAN=yes "\$GITHUB_ENV"
echo "hook capability ran"
echo "node on PATH: \$(command -v node) \$(node --version)"
[ "\$(node --version)" = "v${node_version}" ]
echo "psql on PATH: \$(command -v psql) \$(psql --version)"
[ "\$(command -v psql)" = "${sysroot}/bin/psql" ]
[ "\$(pg_config --includedir)" = "${sysroot}/include" ]
[ "\${LD_LIBRARY_PATH%%:*}" = "${sysroot}/lib" ]
[ "\$PGSYSCONFDIR" = "${sysroot}/etc" ]
echo "system library ran from ${sysroot}"
cd "\$(mktemp -d)"
npm install --silent --no-audit --no-fund "${tool_cache_package}" >/dev/null
node --input-type=module -e "import * as tc from '@actions/tool-cache'; const found = tc.find('node', '${node_version%%.*}', process.arch); if (!found) { console.error('not found in ' + process.env.RUNNER_TOOL_CACHE); process.exit(1); } console.log('tool-cache find: ' + found);"
echo CAPABILITIES-OK
EOF
python3 - "$work/autoscalingrunnerset.yaml" "$work/step.sh" "$work/pod.yaml" <<'EOF'
import sys, yaml
template = yaml.safe_load(open(sys.argv[1]))["spec"]["template"]
spec = template["spec"]
# The chart's service account and pull Secret belong to a real release, which this test does not install.
spec.pop("serviceAccountName", None)
spec.pop("imagePullSecrets", None)
spec["restartPolicy"] = "Never"
runner = next(container for container in spec["containers"] if container["name"] == "runner")
runner["command"] = ["bash", "-c", open(sys.argv[2]).read()]
yaml.safe_dump({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "runner", "labels": template.get("metadata", {}).get("labels", {})}, "spec": spec}, open(sys.argv[3], "w"))
EOF
kubectl -n "$namespace" apply -f "$work/pod.yaml" >/dev/null

# The runner image is large, so the first pull dominates; a pod that has not finished within the deadline is reported with its events.
deadline=$(($(date +%s) + 900))
until phase="$(kubectl -n "$namespace" get pod runner -o jsonpath='{.status.phase}')" && [ "$phase" = Succeeded ] || [ "$phase" = Failed ]; do
  [ "$(date +%s)" -lt "$deadline" ] || fail "the pod did not finish"
  sleep 5
done
kubectl -n "$namespace" logs runner -c runner
[ "$phase" = Succeeded ] || fail "the step failed"
kubectl -n "$namespace" logs runner -c runner | grep -qx CAPABILITIES-OK || fail "the step did not reach its end"
init_state="$(kubectl -n "$namespace" get pod runner -o jsonpath='{.status.initContainerStatuses[?(@.name=="capability-node")].state.terminated.exitCode}')"
[ "$init_state" = 0 ] || fail "the capability-node init container did not succeed (exit code '${init_state}')"
init_state="$(kubectl -n "$namespace" get pod runner -o jsonpath='{.status.initContainerStatuses[?(@.name=="capability-libpq")].state.terminated.exitCode}')"
[ "$init_state" = 0 ] || fail "the capability-libpq init container did not succeed (exit code '${init_state}')"
log "PASS: the init containers populated the tool cache and the sysroot, the hook ran, put the tool on PATH and the library on its search paths, and @actions/tool-cache found the tool"
