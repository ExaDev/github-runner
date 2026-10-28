#!/usr/bin/env bash
# Integration test for scale-set profile capabilities: builds the reference Node.js tool image from tool-images/node and the reference libpq system-library image from tool-images/libpq, renders a profile carrying them (the tool with a path, so the job-started hook puts it on PATH; the library with an extra variable) and a hook capability through the role's own tasks, renders the gha-runner-scale-set chart with those values, and checks the AutoscalingRunnerSet against the ARC CRDs with a server-side dry run in a kind cluster. It then starts a pod from the chart's own runner pod template in that cluster, with the runner's command replaced by what the runner does at job start (run the ACTIONS_RUNNER_HOOK_JOB_STARTED hook with GITHUB_PATH and GITHUB_ENV) followed by a step that asserts the cached Node.js is on PATH and that @actions/tool-cache's find, the lookup actions/setup-node makes before downloading, returns the copy the init container put in the tool cache, and that libpq's programs run from the sysroot its init container filled, with the search paths and the extra variable the hook set, and that the runner user can write to the node-cache directory its init container prepared, under a pod-level non-root securityContext.
#
# It then does the same for a profile with capability_store on, which only a real pod shows: the images are pushed to a registry in the test (the store names an entry for its image's digest, which an image loaded into kind has none of), and two pods run against the node's capability store (prepared as root with CHOWN and DAC_OVERRIDE over a store root the runner user already owns), checking that the entries are published once and the second pod copies nothing, that both pods see the same content through a read-only mount and a read-only sysroot subPath, that a job can change the tool cache's own tree but not the store, and that the node cache pruner, run from the rendered manifests in a pod of its own, keeps every entry a running pod lists, removes an entry nothing lists and removes the pods' entries once they have finished.
#
# Usage: tests/capabilities/run.sh. Needs Docker, kind, kubectl, helm, python3 with PyYAML, and ansible-playbook (ANSIBLE_PLAYBOOK overrides which); reaches the network for the chart, the runner image, the registry image and the npm package. GRTEST_SCALESET_CHART_VERSION pins the scale-set chart and the controller chart its CRDs come from (default: the latest). Creates a kind cluster named grtest-capabilities and a registry container named grtest-capabilities-registry, and removes both on exit unless GRTEST_KEEP=1.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ansible_playbook="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
cluster=grtest-capabilities
registry_container=grtest-capabilities-registry
namespace=arc-runners-example
tool_image=grtest/toolcache-node:test
other_tool_image=grtest/toolcache-node:other-test
library_image=grtest/sysroot-libpq:test
# The node cache pruner's own image, built from the autoscaler image's Dockerfile with only the files it copies, and the registry the store test's images are pushed to (Docker Hub's, a version tag: the registry's own releases are not pinned by digest anywhere else here either).
pruner_image=grtest/autoscaler:test
registry_image=docker.io/library/registry:2.8.3
# A node-cache capability's init image, pinned by digest as the role requires.
busybox_image=docker.io/library/busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e
sysroot=/opt/sysroot
runner_image=ghcr.io/actions/actions-runner:latest
charts=oci://ghcr.io/actions/actions-runner-controller-charts
chart_version="${GRTEST_SCALESET_CHART_VERSION:-}"
tool_cache_package="@actions/tool-cache@4.0.0"
node_version="$(sed -n 's/^ARG NODE_VERSION=//p' "$repo_root/tool-images/node/Dockerfile")"
# The first further Node.js version the publish workflow's matrix names: the same Dockerfile built for it, so the test covers a second version of the tool image beside the Dockerfile's default.
other_node_version="$(python3 -c 'import sys, yaml; print(next(entry["version"] for entry in yaml.safe_load(open(sys.argv[1]))["jobs"]["build"]["strategy"]["matrix"]["image"] if entry["name"] == "node" and "version" in entry))' "$repo_root/.github/workflows/publish-tool-images.yml")"
work="$(mktemp -d "${TMPDIR:-/tmp}/grtest-capabilities.XXXXXX")"
export KUBECONFIG="$work/kubeconfig"

log() { echo "==> $*"; }
# The number of lines in "$1", without padding: BSD wc (macOS) pads its count with spaces, which a string comparison does not ignore.
line_count() { printf '%s\n' "$1" | wc -l | tr -d '[:space:]'; }
fail() {
  echo "FAIL: $*" >&2
  kubectl -n "$namespace" get pods -o wide >&2 || true
  kubectl -n "$namespace" describe pods >&2 || true
  for pod in $(kubectl -n "$namespace" get pods -o name 2>/dev/null); do kubectl -n "$namespace" logs "$pod" --all-containers >&2 || true; done
  exit 1
}
cleanup() {
  if [ "${GRTEST_KEEP:-0}" != 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || true
    docker rm -f "$registry_container" >/dev/null 2>&1 || true
    rm -rf "$work"
  fi
}
trap cleanup EXIT

[ -n "$node_version" ] || fail "could not read NODE_VERSION from tool-images/node/Dockerfile"
[ -n "$other_node_version" ] || fail "could not read a further Node.js version from the publish workflow's matrix"
version_flag=()
[ -z "$chart_version" ] || version_flag=(--version "$chart_version")

log "Building the reference Node.js ${node_version} tool image"
docker build -q -t "$tool_image" "$repo_root/tool-images/node" >/dev/null
log "Building the same image for Node.js ${other_node_version}"
docker build -q --build-arg "NODE_VERSION=${other_node_version}" -t "$other_tool_image" "$repo_root/tool-images/node" >/dev/null
log "Building the reference libpq system-library image"
docker build -q -t "$library_image" "$repo_root/tool-images/libpq" >/dev/null

log "Building the node cache pruner's image"
# The repository root is the context: the Dockerfile copies only the files it names, so the image is the same whichever other files the context holds, and a COPY line added to it needs no change here.
docker build -q -f "$repo_root/autoscaler/Dockerfile" -t "$pruner_image" "$repo_root" >/dev/null

log "Creating kind cluster ${cluster} and its registry"
# A registry on the host's loopback that the nodes reach by its container name over the kind network, as kind's local registry guide has it (https://kind.sigs.k8s.io/docs/user/local-registry/): a pod's image reference names localhost:<port>, and each node's containerd maps that host to the container.
docker rm -f "$registry_container" >/dev/null 2>&1 || true
docker pull -q "$registry_image" >/dev/null
docker run -d --name "$registry_container" -p 127.0.0.1::5000 "$registry_image" >/dev/null
kind create cluster --name "$cluster" --kubeconfig "$KUBECONFIG" --wait 120s >/dev/null
docker network connect kind "$registry_container"
# The published port is read after the container joins the kind network: joining it republishes the port, which can change the host port.
registry_port="$(docker port "$registry_container" 5000/tcp | sed -n 's/.*:\([0-9]*\)$/\1/p' | head -n 1)"
[ -n "$registry_port" ] || fail "could not read the registry's published port"
for node in $(kind get nodes --name "$cluster"); do
  docker exec "$node" mkdir -p "/etc/containerd/certs.d/localhost:${registry_port}"
  printf '[host."http://%s:5000"]\n' "$registry_container" | docker exec -i "$node" tee "/etc/containerd/certs.d/localhost:${registry_port}/hosts.toml" >/dev/null
done
kind load docker-image "$tool_image" "$other_tool_image" "$library_image" "$pruner_image" --name "$cluster" >/dev/null

# What the store names its entries for: each image's digest in the registry, which the push returns.
push_image() {
  local reference="localhost:${registry_port}/$1" digest output
  docker tag "$1" "$reference"
  output="$(docker push "$reference" 2>&1)" || { echo "$output" >&2; docker ps -a >&2; docker logs "$registry_container" >&2 || true; fail "the push of $1 failed"; }
  digest="$(sed -n 's/.*digest: \(sha256:[0-9a-f]\{64\}\).*/\1/p' <<<"$output" | tail -n 1)"
  [ -n "$digest" ] || { echo "$output" >&2; fail "the push of $1 returned no digest"; }
  echo "${reference%:*}@${digest}"
}
log "Pushing the tool and library images to the registry"
tool_reference="$(push_image "$tool_image")"
library_reference="$(push_image "$library_image")"

# What a profile's capabilities render to, for a release named $1 (the files are $work/$1.*): $2 is "true" for a profile with capability_store on, $3 the capabilities it names and $4 and $5 the tool and library images they pull from (a store entry is named for an image's digest, so a store profile takes the registry's references).
render_release() {
  local name=$1 store=$2 capabilities=$3 tool=$4 library=$5
  log "Rendering the ${name} profile's values through the role"
  # The profile as arc_profiles expands it, and a catalogue with the tool image (put on PATH through its bin directory), the library image (with PGSYSCONFDIR, where libpq looks for its service file, as its extra variable) and a hook capability that records it ran.
  python3 - "$repo_root/plugins/filter/arc.py" "$work/$name.extra.json" "$tool" "$node_version" "$work/$name.rendered.json" "$library" "$sysroot" "$busybox_image" "$capabilities" "$store" "$other_tool_image" <<'EOF'
import importlib.util, json, sys
plugin, extra, image, version, output, library_image, sysroot, busybox_image, capabilities, store, other_image = sys.argv[1:]
spec = importlib.util.spec_from_file_location("arc", plugin)
arc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arc)
result = arc.arc_profiles([{"name": "Example", "image": "unused:1", "scale_set_profiles": [{"max_runners": 1, "capabilities": capabilities.split(","), "capability_store": store == "true"}]}], require_app_id=False)
assert result["errors"] == [], result["errors"]
catalogue = {
    "node": {"image": image, "path": f"node/{version}/bin"},
    "node-other": {"image": other_image},
    "libpq": {"sysroot_image": library_image, "env": {"PGSYSCONFDIR": f"{sysroot}/etc"}},
    "pnpm-store": {"node_cache": {"init_image": busybox_image, "dirs": {"pnpm_config_store_dir": "pnpm"}}},
    "marker": {"job_started_hook": 'echo "CAPABILITY_HOOK_RAN=yes" >> "$GITHUB_ENV"\n'},
}
with open(extra, "w") as out:
    json.dump({"profile": result["profiles"][0], "github_runner_arc_capabilities": catalogue, "render_output": output}, out)
EOF
  ANSIBLE_COLLECTIONS_PATH="$repo_root/playbooks/collections" ANSIBLE_LOCALHOST_WARNING=false ANSIBLE_INVENTORY_UNPARSED_WARNING=false \
    "$ansible_playbook" "$repo_root/tests/capabilities/render.yml" -e "@$work/$name.extra.json" >/dev/null
  python3 -c 'import json, sys, yaml; rendered = json.load(open(sys.argv[1])); yaml.safe_dump(rendered["values"], open(sys.argv[2], "w")); yaml.safe_dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "github-runner-job-started"}, "data": rendered["job_started"]}, open(sys.argv[3], "w"))' \
    "$work/$name.rendered.json" "$work/$name.values.yaml" "$work/$name.configmap.yaml"

  log "Rendering the gha-runner-scale-set chart with the ${name} profile's values"
  # The same --set values the role passes to every release (tasks/install_scale_set_profile.yml). The chart looks the controller's service account up in a live cluster, which helm template has none of, so it is named here.
  helm template example-runners "$charts/gha-runner-scale-set" "${version_flag[@]}" --namespace "$namespace" -f "$work/$name.values.yaml" \
    --set githubConfigUrl=https://github.com/example --set githubConfigSecret=example-github-app \
    --set "template.spec.containers[0].image=$runner_image" \
    --set controllerServiceAccount.name=arc-gha-rs-controller --set controllerServiceAccount.namespace=actions-runner-controller \
    >"$work/$name.chart.yaml" 2>"$work/helm.log" || { cat "$work/helm.log" >&2; fail "helm template failed"; }
  python3 - "$work/$name.chart.yaml" "$work/$name.autoscalingrunnerset.yaml" <<'EOF'
import sys, yaml
# Helm 4 prints its "Pulled: ... Digest: ..." notice for an OCI chart on standard output too, which parses as a mapping with no kind.
documents = [document for document in yaml.safe_load_all(open(sys.argv[1])) if isinstance(document, dict) and "kind" in document]
sets = [document for document in documents if document["kind"] == "AutoscalingRunnerSet"]
assert len(sets) == 1, [document["kind"] for document in documents]
yaml.safe_dump(sets[0], open(sys.argv[2], "w"))
EOF
}

render_release plain false node,node-other,libpq,marker,pnpm-store "$tool_image" "$library_image"
render_release store true node,libpq,marker "$tool_reference" "$library_reference"

log "Checking the AutoscalingRunnerSets against the ARC CRDs"
helm pull "$charts/gha-runner-scale-set-controller" "${version_flag[@]}" --untar --untardir "$work/controller" >/dev/null 2>&1
kubectl apply --server-side -f "$work/controller/gha-runner-scale-set-controller/crds/" >/dev/null
kubectl wait --for condition=established --timeout=60s crd/autoscalingrunnersets.actions.github.com >/dev/null
kubectl create namespace "$namespace" >/dev/null
for name in plain store; do
  kubectl apply --dry-run=server -f "$work/$name.autoscalingrunnerset.yaml" >/dev/null || fail "the API server rejected the rendered ${name} AutoscalingRunnerSet"
done

kubectl -n "$namespace" apply -f "$work/plain.configmap.yaml" >/dev/null
# What the runner user must find in the node cache once the init container has run: the directory the profile names, writable, and the cache's root writable too, since a job recreates a subdirectory the pruner has set aside directly under it.
cat >"$work/cache-step.sh" <<'EOF'
set -euo pipefail
echo "uid $(id -u); the store directory is $pnpm_config_store_dir"
[ "$(id -u)" = 1001 ]
[ "$pnpm_config_store_dir" = /node-cache/pnpm ]
stat -c 'owner %u mode %a %n' /node-cache "$pnpm_config_store_dir"
touch "$pnpm_config_store_dir/probe"
[ "$(stat -c %u "$pnpm_config_store_dir/probe")" = 1001 ]
mkdir /node-cache/recreated-by-a-job
stat -c 'owner %u mode %a %n' /node-cache/recreated-by-a-job
[ "$(stat -c %u /node-cache/recreated-by-a-job)" = 1001 ]
echo "node cache writable by the runner user"
EOF
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
other_arch_dirs="\$(find "\$RUNNER_TOOL_CACHE/node/${other_node_version}" -mindepth 1 -maxdepth 1 -type d)"
[ "\$(printf '%s\\n' "\$other_arch_dirs" | wc -l)" = 1 ]
[ -e "\$other_arch_dirs.complete" ]
[ "\$("\$other_arch_dirs/bin/node" --version)" = "v${other_node_version}" ]
echo "node ${other_node_version} sits in the tool cache beside it: \$other_arch_dirs"
echo "psql on PATH: \$(command -v psql) \$(psql --version)"
[ "\$(command -v psql)" = "${sysroot}/bin/psql" ]
[ "\$(pg_config --includedir)" = "${sysroot}/include" ]
[ "\${LD_LIBRARY_PATH%%:*}" = "${sysroot}/lib" ]
[ "\$PGSYSCONFDIR" = "${sysroot}/etc" ]
echo "system library ran from ${sysroot}"
$(cat "$work/cache-step.sh")
cd "\$(mktemp -d)"
npm install --silent --no-audit --no-fund "${tool_cache_package}" >/dev/null
node --input-type=module -e "import * as tc from '@actions/tool-cache'; const found = tc.find('node', '${node_version%%.*}', process.arch); if (!found) { console.error('not found in ' + process.env.RUNNER_TOOL_CACHE); process.exit(1); } console.log('tool-cache find: ' + found); const other = tc.find('node', '${other_node_version%%.*}', process.arch); if (!other || !other.includes('/${other_node_version}/')) { console.error('node ${other_node_version} not found in ' + process.env.RUNNER_TOOL_CACHE + ': ' + other); process.exit(1); } console.log('tool-cache find: ' + other);"
echo CAPABILITIES-OK
EOF
cat >"$work/make-pod.py" <<'EOF'
import sys, yaml
# Usage: make-pod.py AUTOSCALINGRUNNERSET STEP_SCRIPT POD_NAME OUTPUT
template = yaml.safe_load(open(sys.argv[1]))["spec"]["template"]
spec = template["spec"]
# The chart's service account and pull Secret belong to a real release, which this test does not install.
spec.pop("serviceAccountName", None)
spec.pop("imagePullSecrets", None)
spec["restartPolicy"] = "Never"
# A pod-level non-root context, as a cluster may impose on every pod: the init containers must set their own to do their work.
spec["securityContext"] = {"runAsNonRoot": True, "runAsUser": 1001, "runAsGroup": 1001}
runner = next(container for container in spec["containers"] if container["name"] == "runner")
runner["command"] = ["bash", "-c", open(sys.argv[2]).read()]
# The annotations too: the capability store's pruner reads the entries a pod uses from the pod's own.
metadata = {"name": sys.argv[3], "labels": template.get("metadata", {}).get("labels", {}), "annotations": template.get("metadata", {}).get("annotations", {})}
yaml.safe_dump({"apiVersion": "v1", "kind": "Pod", "metadata": metadata, "spec": spec}, open(sys.argv[4], "w"))
EOF
# The runner image is large, so the first pull dominates; a pod that has not finished within the deadline is reported with its events.
wait_for_pod() {
  local deadline phase
  deadline=$(($(date +%s) + 900))
  until phase="$(kubectl -n "$namespace" get pod "$1" -o jsonpath='{.status.phase}')" && [ "$phase" = Succeeded ] || [ "$phase" = Failed ]; do
    [ "$(date +%s)" -lt "$deadline" ] || fail "the pod $1 did not finish"
    sleep 5
  done
  # A pod that failed in an init container has no runner log to print; fail dumps every container's.
  kubectl -n "$namespace" logs "$1" -c runner || fail "the pod $1 failed before the step ran"
  [ "$phase" = Succeeded ] || fail "the step of pod $1 failed"
}
# Waits until a pod that is still running has printed the line $2 on its runner container, for a pod that holds on after its checks so that the pruner can see it running.
wait_for_log() {
  local deadline phase
  deadline=$(($(date +%s) + 900))
  until kubectl -n "$namespace" logs "$1" -c runner 2>/dev/null | grep -qx "$2"; do
    phase="$(kubectl -n "$namespace" get pod "$1" -o jsonpath='{.status.phase}')"
    if [ "$phase" = Failed ] || [ "$phase" = Succeeded ]; then
      fail "the pod $1 ended before it printed $2"
    fi
    [ "$(date +%s)" -lt "$deadline" ] || fail "the pod $1 did not print $2"
    sleep 5
  done
}

# The state a node is really in: the directory already given to the runner user, with no subdirectory in it, which is what the pruner leaves after it empties the cache and what an earlier pod leaves behind. The init container must still create the subdirectory there, as root over a directory it no longer owns.
cache_host_path="$(python3 -c 'import sys, yaml; spec = yaml.safe_load(open(sys.argv[1]))["spec"]["template"]["spec"]; print(next(volume["hostPath"]["path"] for volume in spec["volumes"] if volume["name"] == "node-cache"))' "$work/plain.autoscalingrunnerset.yaml")"
log "Starting a pod on a node cache already owned by the runner user, with no subdirectory"
docker exec "${cluster}-control-plane" sh -c 'mkdir -p "$1" && chown 1001:1001 "$1"' sh "$cache_host_path"
{ cat "$work/cache-step.sh"; echo "echo CACHE-OK"; } >"$work/seeded-step.sh"
python3 "$work/make-pod.py" "$work/plain.autoscalingrunnerset.yaml" "$work/seeded-step.sh" seeded "$work/seeded-pod.yaml"
kubectl -n "$namespace" apply -f "$work/seeded-pod.yaml" >/dev/null
wait_for_pod seeded
kubectl -n "$namespace" logs seeded -c runner | grep -qx CACHE-OK || fail "the step of pod seeded did not reach its end"
kubectl -n "$namespace" delete pod seeded --wait >/dev/null
docker exec "${cluster}-control-plane" rm -rf "$cache_host_path"

log "Starting a pod from the chart's runner pod template"
python3 "$work/make-pod.py" "$work/plain.autoscalingrunnerset.yaml" "$work/step.sh" runner "$work/pod.yaml"
kubectl -n "$namespace" apply -f "$work/pod.yaml" >/dev/null
wait_for_pod runner

kubectl -n "$namespace" logs runner -c runner | grep -qx CAPABILITIES-OK || fail "the step did not reach its end"
init_state="$(kubectl -n "$namespace" get pod runner -o jsonpath='{.status.initContainerStatuses[?(@.name=="capability-node")].state.terminated.exitCode}')"
[ "$init_state" = 0 ] || fail "the capability-node init container did not succeed (exit code '${init_state}')"
init_state="$(kubectl -n "$namespace" get pod runner -o jsonpath='{.status.initContainerStatuses[?(@.name=="capability-node-other")].state.terminated.exitCode}')"
[ "$init_state" = 0 ] || fail "the capability-node-other init container did not succeed (exit code '${init_state}')"
init_state="$(kubectl -n "$namespace" get pod runner -o jsonpath='{.status.initContainerStatuses[?(@.name=="capability-libpq")].state.terminated.exitCode}')"
[ "$init_state" = 0 ] || fail "the capability-libpq init container did not succeed (exit code '${init_state}')"
init_state="$(kubectl -n "$namespace" get pod runner -o jsonpath='{.status.initContainerStatuses[?(@.name=="capability-pnpm-store")].state.terminated.exitCode}')"
[ "$init_state" = 0 ] || fail "the capability-pnpm-store init container did not succeed (exit code '${init_state}')"

# The capability store, with what only a real pod shows. The step every store pod runs: the hook puts the tool and the library on their paths, then the checks of what a job may and may not change. The values it needs come first, as assignments; the body is quoted so that nothing in it is expanded here.
store_host_path="$(python3 -c 'import sys, yaml; spec = yaml.safe_load(open(sys.argv[1]))["spec"]["template"]["spec"]; print(next(volume["hostPath"]["path"] for volume in spec["volumes"] if volume["name"] == "capability-store"))' "$work/store.autoscalingrunnerset.yaml")"
store_mount=/opt/capability-store
node="${cluster}-control-plane"
# $1 is "true" for the pod that also asks @actions/tool-cache for the tool, which needs npm and the network.
write_store_step() {
  {
    printf 'node_version=%s\nsysroot=%s\nstore=%s\nuse_tool_cache=%s\ntool_cache_package=%s\n' "$node_version" "$sysroot" "$store_mount" "$1" "$tool_cache_package"
    cat <<'EOF'
set -euo pipefail
export GITHUB_PATH="$(mktemp)" GITHUB_ENV="$(mktemp)"
bash -e "$ACTIONS_RUNNER_HOOK_JOB_STARTED"
while IFS= read -r directory; do PATH="$directory:$PATH"; done <"$GITHUB_PATH"
export PATH
while IFS= read -r line; do export "${line?}"; done <"$GITHUB_ENV"
grep -qx CAPABILITY_HOOK_RAN=yes "$GITHUB_ENV"
[ "$(node --version)" = "v${node_version}" ]
[ "$(command -v psql)" = "${sysroot}/bin/psql" ]
[ "$(pg_config --includedir)" = "${sysroot}/include" ]
[ "${LD_LIBRARY_PATH%%:*}" = "${sysroot}/lib" ]
[ "$PGSYSCONFDIR" = "${sysroot}/etc" ]
echo "the tool and the library resolve from the store"

# The tool's tree in the pod's own tool cache: real directories holding a link to each store file.
arch_dirs="$(find "$RUNNER_TOOL_CACHE/node/$node_version" -mindepth 1 -maxdepth 1 -type d)"
[ "$(printf '%s\n' "$arch_dirs" | wc -l)" = 1 ]
arch_dir="$arch_dirs"
[ -e "$arch_dir.complete" ]
target="$(readlink "$arch_dir/README.md")"
case "$target" in "$store"/*) ;; *) echo "README.md links to '$target', not into $store"; exit 1 ;; esac
key="${target#"$store"/}"
key="${key%%/*}"
[ -d "$store/$key" ]
[ "$(stat -c %u "$store/$key")" = 1001 ]
listing() { (cd "$store/$key" && find . -printf '%p %s %l\n' | LC_ALL=C sort | sha256sum | cut -d' ' -f1); }
before="$(listing)"
echo "STORE-LISTING-BEFORE $before"

# What a job may do in the tool's tree: add a file, a directory and a package, and replace a file (a setup action installing beside or over the cached tool, npm install -g).
touch "$arch_dir/job-file"
mkdir "$arch_dir/job-dir"
touch "$arch_dir/job-dir/inner"
mkdir -p "$arch_dir/lib/node_modules/job-package"
rm "$arch_dir/LICENSE"
echo replaced >"$arch_dir/LICENSE"
[ "$(cat "$arch_dir/LICENSE")" = replaced ]

# What it may not: change a store file in place through its link, or anything in the store or the sysroot.
errors="$(mktemp)"
refused() {
  if "$@" 2>"$errors"; then echo "this was allowed, and must not be: $*"; exit 1; fi
  grep -q "Read-only file system" "$errors" || { echo "refused for another reason: $*"; cat "$errors"; exit 1; }
}
refused sh -c 'echo changed >>"$1"' sh "$arch_dir/README.md"
refused rm "$target"
refused touch "$store/$key/job-file"
refused mkdir "$store/$key/job-dir"
refused rm -rf "$store/$key"
# The runner container sees only the entries its own pod uses, never the store's root: the store holds the system libraries' entries and the merged sysroot as well, and none of them is under the store path here, and nothing can be added beside the tool's entry.
[ "$(ls -A "$store")" = "$key" ] || { echo "the store path holds '$(ls -A "$store")', not only the tool's entry '$key'"; exit 1; }
if mkdir "$store/job-entry" 2>"$errors"; then echo "a directory could be added beside the tool's entry, and must not be"; exit 1; fi
refused touch "$sysroot/job-file"
refused rm "$sysroot/bin/psql"
after="$(listing)"
echo "STORE-LISTING-AFTER $after"
[ "$before" = "$after" ]

if [ "$use_tool_cache" = true ]; then
  cd "$(mktemp -d)"
  npm install --silent --no-audit --no-fund "${tool_cache_package}" >/dev/null
  node --input-type=module -e "import * as tc from '@actions/tool-cache'; const found = tc.find('node', '${node_version%%.*}', process.arch); if (!found) { console.error('not found in ' + process.env.RUNNER_TOOL_CACHE); process.exit(1); } console.log('tool-cache find: ' + found);"
fi

# Hold on until the test has run the pruner against this running pod.
echo STORE-STEP-DONE
until [ -e /tmp/release ]; do sleep 1; done
echo STORE-OK
EOF
  } >"$work/store-step-$2.sh"
  python3 "$work/make-pod.py" "$work/store.autoscalingrunnerset.yaml" "$work/store-step-$2.sh" "$2" "$work/store-pod-$2.yaml"
}

# The state of the store's root, its incoming directory and every entry's directory: name, inode and modification time. A pod that copies or publishes anything changes it; a pod that finds every entry already there leaves it as it was.
store_state() {
  docker exec "$node" sh -c 'stat -c "%n %i %y" "$1" "$1/.incoming" "$1"/*' sh "$store_host_path"
}
store_entries() {
  docker exec "$node" ls -A "$store_host_path" | grep -vxE '\.incoming|\.trash' | LC_ALL=C sort || true
}

log "Starting a pod on a capability store already owned by the runner user, with no incoming directory"
# The state a node is really in after an earlier pod or the pruner: the store's root given to the runner user, with no incoming directory in it. The preparation must still create it, as root over a directory it no longer owns, which needs DAC_OVERRIDE.
docker exec "$node" sh -c 'mkdir -p "$1" && chown 1001:1001 "$1"' sh "$store_host_path"
write_store_step true store-a
kubectl -n "$namespace" apply -f "$work/store-pod-store-a.yaml" >/dev/null
wait_for_log store-a STORE-STEP-DONE

expected_entries="$(kubectl -n "$namespace" get pod store-a -o jsonpath='{.metadata.annotations.github-runner\.exadev\.io/capability-store-keys}' | tr ' ' '\n' | LC_ALL=C sort)"
[ "$(line_count "$expected_entries")" = 3 ] || fail "the pod's annotation does not list the tool, the library and the merged sysroot: '$expected_entries'"
[ "$(store_entries)" = "$expected_entries" ] || fail "the store holds '$(store_entries)', not exactly the entries the pod's annotation lists: '$expected_entries'"
[ -z "$(docker exec "$node" ls -A "$store_host_path/.incoming")" ] || fail "a copy was left in the incoming directory after the pod published it"
[ "$(docker exec "$node" stat -c %u "$store_host_path/.incoming")" = 1001 ] || fail "the incoming directory is not the runner user's"

log "Starting a second pod against the filled store"
state_before="$(store_state)"
write_store_step false store-b
kubectl -n "$namespace" apply -f "$work/store-pod-store-b.yaml" >/dev/null
wait_for_log store-b STORE-STEP-DONE
[ "$(store_state)" = "$state_before" ] || fail "the second pod changed the store, which it should only have read: $(diff <(echo "$state_before") <(store_state))"
listings="$(for pod in store-a store-b; do kubectl -n "$namespace" logs "$pod" -c runner | sed -n 's/^STORE-LISTING-[A-Z]* //p'; done | sort -u)"
[ "$(line_count "$listings")" = 1 ] || fail "the two pods saw different content in the entry: $listings"
[ "$(kubectl -n "$namespace" get pod store-b -o jsonpath='{.metadata.annotations.github-runner\.exadev\.io/capability-store-keys}' | tr ' ' '\n' | LC_ALL=C sort)" = "$expected_entries" ] || fail "the two pods list different store entries"
kubectl -n "$namespace" logs store-a -c runner | grep -q '^tool-cache find: ' || fail "@actions/tool-cache did not find the tool in the pod's tool cache"

log "Rendering the node cache pruner's manifests from the role's template"
python3 - "$work/plain.extra.json" "$work/pruner-extra.json" "$work/pruner.yaml" <<'EOF'
import json, sys
# The fleet is this test's one profile, with the capability store on, and its namespace is where the store's references are read from.
profile = json.load(open(sys.argv[1]))["profile"]
profile = {"namespace": profile["namespace"], "release": profile["release"], "target": f"{profile['namespace']}/{profile['release']}"}
json.dump({"github_runner_arc_fleet": {"profiles": [profile]}, "github_runner_arc_node_cache_profiles": [profile], "github_runner_arc_capability_store_namespaces": [profile["namespace"]], "github_runner_arc_node_label_key": "example/ci", "github_runner_arc_node_label_value": "true", "pruner_output": sys.argv[3]}, open(sys.argv[2], "w"))
EOF
ANSIBLE_COLLECTIONS_PATH="$repo_root/playbooks/collections" ANSIBLE_LOCALHOST_WARNING=false ANSIBLE_INVENTORY_UNPARSED_WARNING=false \
  "$ansible_playbook" "$repo_root/tests/node_cache_pruner/render.yml" -e "@$work/pruner-extra.json" >/dev/null
# The rendered Roles, bindings and service account are applied as they are. The DaemonSet becomes a pod that checks once and ends (PRUNE_ONCE), on the image built here, without the placement that picks the fleet's nodes (the kind node carries no such label).
python3 - "$work/pruner.yaml" "$pruner_image" "$work/pruner-rbac.yaml" "$work/pruner-pod.yaml" <<'EOF'
import sys, yaml
documents = [document for document in yaml.safe_load_all(open(sys.argv[1])) if document]
daemon_set = next(document for document in documents if document["kind"] == "DaemonSet")
yaml.safe_dump_all([document for document in documents if document["kind"] != "DaemonSet"], open(sys.argv[3], "w"))
spec = daemon_set["spec"]["template"]["spec"]
for field in ("nodeSelector", "tolerations", "priorityClassName", "imagePullSecrets"):
    spec.pop(field, None)
container = spec["containers"][0]
container["image"] = sys.argv[2]
container["imagePullPolicy"] = "IfNotPresent"
container["env"].append({"name": "PRUNE_ONCE", "value": "true"})
spec["restartPolicy"] = "Never"
yaml.safe_dump({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "pruner", "namespace": daemon_set["metadata"]["namespace"], "labels": daemon_set["spec"]["template"]["metadata"]["labels"]}, "spec": spec}, open(sys.argv[4], "w"))
EOF
platform_namespace="$(python3 -c 'import sys, yaml; print(next(document["metadata"]["namespace"] for document in yaml.safe_load_all(open(sys.argv[1])) if document and document["kind"] == "DaemonSet"))' "$work/pruner.yaml")"
kubectl create namespace "$platform_namespace" >/dev/null
kubectl apply -f "$work/pruner-rbac.yaml" >/dev/null
# One round of the pruner, from the rendered pod; prints its log and leaves it in $work/pruner.log.
prune_once() {
  local deadline phase
  kubectl -n "$platform_namespace" delete pod pruner --ignore-not-found --wait >/dev/null
  kubectl apply -f "$work/pruner-pod.yaml" >/dev/null
  deadline=$(($(date +%s) + 300))
  until phase="$(kubectl -n "$platform_namespace" get pod pruner -o jsonpath='{.status.phase}')" && [ "$phase" = Succeeded ] || [ "$phase" = Failed ]; do
    [ "$(date +%s)" -lt "$deadline" ] || fail "the pruner did not finish"
    sleep 3
  done
  kubectl -n "$platform_namespace" logs pruner | tee "$work/pruner.log"
  [ "$phase" = Succeeded ] || fail "the pruner failed"
}

log "Pruning the store while both pods run, with an entry and a copy that nothing lists beside them"
orphan=$(printf 'f%.0s' $(seq 64))
docker exec "$node" sh -c 'mkdir -p "$1/$2" "$1/.incoming/$2.00000000-0000-0000-0000-000000000000.capability-node" && touch "$1/$2/file"' sh "$store_host_path" "$orphan"
prune_once
grep -q "removed ${orphan}," "$work/pruner.log" || fail "the pruner did not remove the entry nothing lists"
[ "$(store_entries)" = "$expected_entries" ] || fail "the pruner left '$(store_entries)', not the entries the running pods list: '$expected_entries'"
[ -z "$(docker exec "$node" ls -A "$store_host_path/.incoming")" ] || fail "the pruner left a copy no pod owns"
if grep -E "removed ($(printf '%s\n' "$expected_entries" | paste -sd'|' -))," "$work/pruner.log"; then fail "the pruner removed an entry a running pod lists"; fi
kubectl -n "$namespace" get pod store-a store-b -o jsonpath='{range .items[*]}{.metadata.name} {.status.phase}{"\n"}{end}' | grep -vq Running && fail "a pod stopped running during the pruner's round"

log "Letting both pods finish, then pruning again"
for pod in store-a store-b; do
  kubectl -n "$namespace" exec "$pod" -c runner -- touch /tmp/release
  wait_for_pod "$pod"
  kubectl -n "$namespace" logs "$pod" -c runner | grep -qx STORE-OK || fail "the step of pod $pod did not reach its end"
done
prune_once
[ -z "$(store_entries)" ] || fail "the pruner left '$(store_entries)' after every pod that listed it had finished"

init_state="$(kubectl -n "$namespace" get pod store-a -o jsonpath='{.status.initContainerStatuses[*].state.terminated.exitCode}')"
[ "$(printf '%s\n' "$init_state" | tr ' ' '\n' | sort -u)" = 0 ] || fail "an init container of the store pod did not succeed (exit codes '${init_state}')"
log "PASS: the init containers populated the tool cache and the sysroot, the hook ran, put the tool on PATH and the library on its search paths, and @actions/tool-cache found the tool, and the runner user could write to the node cache; with the store on, the entries were published once and read by a second pod that copied nothing, a job changed the tool cache's tree but nothing in the store or the sysroot, and the pruner kept the entries running pods list and removed the rest"
