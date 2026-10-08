#!/usr/bin/env bash
# Integration test for the ARC role's registry cache: in a kind cluster it installs the cache's resources as the role renders them (tests/registry_cache/render.yml), for Docker Hub, ghcr.io and a private registry that needs a login (an in-cluster registry with basic auth, whose credential the cache reads from a Secret), and starts a pod from the chart's runner pod template as the role renders a Docker-in-Docker profile with the cache on (tests/capabilities/render.yml). The pod's step pulls from all three registries with docker pull and builds through a docker-container builder created as docker/setup-buildx-action creates one, without logging in anywhere, and the cache's own access logs must show each pull. The cache is then scaled to nothing and a second pod must still pull and build from the public registries, directly.
#
# Usage: tests/registry_cache/run.sh. Needs Docker, kind, kubectl, helm, python3 with PyYAML, and ansible-playbook (ANSIBLE_PLAYBOOK overrides which); reaches the network for the chart, the images and the registries. GRTEST_SCALESET_CHART_VERSION pins the scale-set chart and the controller chart its CRDs come from (default: the latest). Creates a kind cluster named grtest-registry-cache and removes it on exit unless GRTEST_KEEP=1.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ansible_playbook="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
cluster=grtest-registry-cache
namespace=arc-runners-example
cache_namespace=github-runner-registry-cache
upstream_namespace=grtest-private-registry
upstream_host="upstream.${upstream_namespace}.svc.cluster.local:5000"
runner_image=ghcr.io/actions/actions-runner:latest
registry_image="$(sed -n 's/^github_runner_arc_registry_cache_image: "\(.*\)"$/\1/p' "$repo_root/roles/github_runner_arc/defaults/main.yml")"
charts=oci://ghcr.io/actions/actions-runner-controller-charts
chart_version="${GRTEST_SCALESET_CHART_VERSION:-}"
# Images each phase pulls, distinct between the phases so the second cannot be served from what the first left in the daemon. The ghcr.io images are small public multi-architecture images.
hub_pull=busybox:1.36
hub_build=alpine:3.20
ghcr_pull=ghcr.io/linuxserver/baseimage-alpine:3.11
ghcr_build=ghcr.io/linuxserver/baseimage-alpine:3.10
private_image="${upstream_host}/grtest/private:1"
direct_hub_pull=busybox:1.35
direct_hub_build=alpine:3.19
direct_ghcr_pull=ghcr.io/linuxserver/baseimage-alpine:3.9
work="$(mktemp -d "${TMPDIR:-/tmp}/grtest-registry-cache.XXXXXX")"
export KUBECONFIG="$work/kubeconfig"
# A made-up credential for the throwaway private registry, never a real one.
private_user=grtest
private_password="grtest-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"

log() { echo "==> $*"; }
fail() {
  echo "FAIL: $*" >&2
  kubectl get pods -A -o wide >&2 || true
  for pod in runner-cached runner-direct; do
    kubectl -n "$namespace" describe pod "$pod" >&2 || true
    kubectl -n "$namespace" logs "$pod" --all-containers >&2 || true
  done
  kubectl -n "$cache_namespace" describe deployment,replicaset,pod >&2 || true
  kubectl -n "$cache_namespace" logs deployment/registry-cache --all-containers --tail=50 >&2 || true
  exit 1
}
cleanup() {
  if [ "${GRTEST_KEEP:-0}" != 1 ]; then
    kind delete cluster --name "$cluster" >/dev/null 2>&1 || true
    rm -rf "$work"
  fi
}
trap cleanup EXIT

[ -n "$registry_image" ] || fail "could not read github_runner_arc_registry_cache_image from the role's defaults"
version_flag=()
[ -z "$chart_version" ] || version_flag=(--version "$chart_version")

log "Creating kind cluster ${cluster}"
kind create cluster --name "$cluster" --kubeconfig "$KUBECONFIG" --wait 120s >/dev/null

log "Starting a private registry that needs a login, holding one image"
docker run --rm --entrypoint htpasswd httpd:2.4-alpine -Bbn "$private_user" "$private_password" >"$work/htpasswd"
kubectl create namespace "$upstream_namespace" >/dev/null
kubectl -n "$upstream_namespace" create secret generic htpasswd --from-file=htpasswd="$work/htpasswd" >/dev/null
kubectl -n "$upstream_namespace" apply -f - >/dev/null <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: {name: upstream}
spec:
  selector: {matchLabels: {app: upstream}}
  template:
    metadata: {labels: {app: upstream}}
    spec:
      containers:
        - name: registry
          image: ${registry_image}
          env:
            - {name: REGISTRY_AUTH, value: htpasswd}
            - {name: REGISTRY_AUTH_HTPASSWD_REALM, value: grtest}
            - {name: REGISTRY_AUTH_HTPASSWD_PATH, value: /auth/htpasswd}
            - {name: OTEL_TRACES_EXPORTER, value: none}
          volumeMounts: [{name: htpasswd, mountPath: /auth}]
      volumes: [{name: htpasswd, secret: {secretName: htpasswd}}]
---
apiVersion: v1
kind: Service
metadata: {name: upstream}
spec:
  selector: {app: upstream}
  ports: [{port: 5000}]
EOF
kubectl -n "$upstream_namespace" rollout status deployment/upstream --timeout=180s >/dev/null
# Pushed from inside the cluster, so the test needs nothing of the host's Docker daemon but htpasswd, and the credential reaches crane only through a Secret.
kubectl -n "$upstream_namespace" create secret generic push --from-literal=username="$private_user" --from-literal=password="$private_password" >/dev/null
# shellcheck disable=SC2016 # $username, $password (the Secret's keys, as envFrom names them) and $1 are expanded by the pod's shell, not this one.
push_script='crane auth login --insecure "$1" -u "$username" -p "$password" && crane copy --insecure busybox:1.37 "$1/grtest/private:1"'
kubectl -n "$upstream_namespace" run push --restart=Never --image=gcr.io/go-containerregistry/crane:debug \
  --overrides="$(python3 -c 'import json, sys; print(json.dumps({"spec": {"containers": [{"name": "push", "image": "gcr.io/go-containerregistry/crane:debug", "command": ["sh", "-c", sys.argv[1], "push", sys.argv[2]], "envFrom": [{"secretRef": {"name": "push"}}]}]}}))' "$push_script" "$upstream_host")" >/dev/null
deadline=$(($(date +%s) + 300))
until phase="$(kubectl -n "$upstream_namespace" get pod push -o jsonpath='{.status.phase}')" && { [ "$phase" = Succeeded ] || [ "$phase" = Failed ]; }; do
  [ "$(date +%s)" -lt "$deadline" ] || break
  sleep 5
done
[ "${phase:-}" = Succeeded ] || { kubectl -n "$upstream_namespace" logs push >&2 || true; fail "could not push the private image"; }

log "Rendering and installing the registry cache through the role"
python3 - "$repo_root/plugins/filter/arc.py" "$work" "$upstream_host" "$runner_image" <<'EOF'
import importlib.util, json, sys
plugin, work, upstream_host, runner_image = sys.argv[1:]
spec = importlib.util.spec_from_file_location("arc", plugin)
arc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arc)
fleet = arc.arc_profiles([{"name": "Example", "image": runner_image, "scale_set_profiles": [{"max_runners": 1, "container_mode": "dind"}]}], require_app_id=False)
assert fleet["errors"] == [], fleet["errors"]
settings = {
    "github_runner_arc_registry_cache_enabled": True,
    "github_runner_arc_registry_cache_storage": "ephemeral",
    "github_runner_arc_registry_cache_storage_size": "4Gi",
    "github_runner_arc_registry_cache_registries": [
        {"name": "docker-hub", "host": "docker.io"},
        {"name": "ghcr", "host": "ghcr.io"},
        {"name": "private", "host": upstream_host, "url": f"http://{upstream_host}", "credentials": {"secret_name": "private-read"}},
    ],
}
with open(f"{work}/cache-extra.json", "w") as out:
    json.dump({**settings, "github_runner_arc_fleet": fleet, "render_output": f"{work}/cache.json"}, out)
with open(f"{work}/values-extra.json", "w") as out:
    json.dump({**settings, "profile": fleet["profiles"][0], "org": {"name": "Example", "image": runner_image}, "github_runner_arc_capabilities": {}, "render_output": f"{work}/rendered.json"}, out)
EOF
export ANSIBLE_COLLECTIONS_PATH="$repo_root/playbooks/collections" ANSIBLE_LOCALHOST_WARNING=false ANSIBLE_INVENTORY_UNPARSED_WARNING=false
"$ansible_playbook" "$repo_root/tests/registry_cache/render.yml" -e "@$work/cache-extra.json" >/dev/null
"$ansible_playbook" "$repo_root/tests/capabilities/render.yml" -e "@$work/values-extra.json" >/dev/null
python3 -c 'import json, sys, yaml; yaml.safe_dump_all(json.load(open(sys.argv[1])), open(sys.argv[2], "w"))' "$work/cache.json" "$work/cache.yaml"
python3 -c 'import json, sys, yaml; rendered = json.load(open(sys.argv[1])); assert rendered["registry_cache_wired"]; yaml.safe_dump(rendered["values"], open(sys.argv[2], "w")); yaml.safe_dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "registry-cache"}, "data": rendered["registry_cache_configmap"]}, open(sys.argv[3], "w"))' \
  "$work/rendered.json" "$work/values.yaml" "$work/configmap.yaml"
# The cache runs at the platform's priority, whose class the role creates before installing anything (tasks/install_priority_class.yml).
kubectl create priorityclass "$(sed -n 's/^github_runner_arc_platform_priority_class_name: "\(.*\)"$/\1/p' "$repo_root/roles/github_runner_arc/defaults/main.yml")" --value=1000000 >/dev/null
kubectl create namespace "$cache_namespace" >/dev/null
kubectl -n "$cache_namespace" create secret generic private-read --from-literal=username="$private_user" --from-literal=password="$private_password" >/dev/null
kubectl create namespace "$namespace" >/dev/null
kubectl apply -f "$work/cache.yaml" >/dev/null
kubectl -n "$cache_namespace" rollout status deployment/registry-cache --timeout=180s >/dev/null || fail "the cache did not become ready"

log "Rendering the gha-runner-scale-set chart with the profile's values"
helm template example-runners "$charts/gha-runner-scale-set" "${version_flag[@]}" --namespace "$namespace" -f "$work/values.yaml" \
  --set githubConfigUrl=https://github.com/example --set githubConfigSecret=example-github-app \
  --set "template.spec.containers[0].image=$runner_image" \
  --set controllerServiceAccount.name=arc-gha-rs-controller --set controllerServiceAccount.namespace=actions-runner-controller \
  >"$work/chart.yaml" 2>"$work/helm.log" || { cat "$work/helm.log" >&2; fail "helm template failed"; }
python3 - "$work/chart.yaml" "$work/autoscalingrunnerset.yaml" <<'EOF'
import sys, yaml
# Helm 4 prints its "Pulled: ... Digest: ..." notice for an OCI chart on standard output too, which parses as a mapping with no kind.
documents = [document for document in yaml.safe_load_all(open(sys.argv[1])) if isinstance(document, dict) and "kind" in document]
sets = [document for document in documents if document["kind"] == "AutoscalingRunnerSet"]
assert len(sets) == 1, [document["kind"] for document in documents]
yaml.safe_dump(sets[0], open(sys.argv[2], "w"))
EOF
helm pull "$charts/gha-runner-scale-set-controller" "${version_flag[@]}" --untar --untardir "$work/controller" >/dev/null 2>&1
kubectl apply --server-side -f "$work/controller/gha-runner-scale-set-controller/crds/" >/dev/null
kubectl wait --for condition=established --timeout=60s crd/autoscalingrunnersets.actions.github.com >/dev/null
kubectl apply --dry-run=server -n "$namespace" -f "$work/autoscalingrunnerset.yaml" >/dev/null || fail "the API server rejected the rendered AutoscalingRunnerSet"
kubectl -n "$namespace" apply -f "$work/configmap.yaml" >/dev/null

# run_pod <name> <step>: starts a pod from the chart's runner pod template with the runner's command replaced by the step, and waits for it to finish.
run_pod() {
  local name="$1" step="$2"
  python3 - "$work/autoscalingrunnerset.yaml" "$step" "$name" "$work/$name.yaml" <<'EOF'
import sys, yaml
template = yaml.safe_load(open(sys.argv[1]))["spec"]["template"]
spec = template["spec"]
# The chart's service account and pull Secret belong to a real release, which this test does not install.
spec.pop("serviceAccountName", None)
spec.pop("imagePullSecrets", None)
spec["restartPolicy"] = "Never"
runner = next(container for container in spec["containers"] if container["name"] == "runner")
runner["command"] = ["bash", "-c", sys.argv[2]]
yaml.safe_dump({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": sys.argv[3]}, "spec": spec}, open(sys.argv[4], "w"))
EOF
  kubectl -n "$namespace" apply -f "$work/$name.yaml" >/dev/null
  local deadline=$(($(date +%s) + 900)) phase
  until phase="$(kubectl -n "$namespace" get pod "$name" -o jsonpath='{.status.phase}')" && { [ "$phase" = Succeeded ] || [ "$phase" = Failed ]; }; do
    [ "$(date +%s)" -lt "$deadline" ] || fail "pod $name did not finish"
    sleep 5
  done
  kubectl -n "$namespace" logs "$name" -c runner
  [ "$phase" = Succeeded ] || fail "pod $name's step failed"
}

# The builder is created the way docker/setup-buildx-action creates one when a workflow gives it no configuration (its getCreateArgs: a name, the docker-container driver, its default entitlement flags and --use, and no --config).
builder='docker buildx create --name grtest-builder --driver docker-container --buildkitd-flags "--allow-insecure-entitlement security.insecure --allow-insecure-entitlement network.host" --use >/dev/null'

log "Pulling and building through the cache, without logging in"
since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
run_pod runner-cached "set -euo pipefail
until docker info >/dev/null 2>&1; do sleep 1; done
docker pull -q ${hub_pull}
docker pull -q ${ghcr_pull}
docker pull -q ${private_image}
${builder}
printf 'FROM ${hub_build}\nRUN true\n' | docker buildx build -q - >/dev/null
printf 'FROM ${ghcr_build}\nRUN true\n' | docker buildx build -q - >/dev/null
printf 'FROM ${private_image}\nRUN true\n' | docker buildx build -q - >/dev/null
echo REGISTRY-CACHE-OK"
kubectl -n "$namespace" logs runner-cached -c runner | grep -qx REGISTRY-CACHE-OK || fail "the cached step did not reach its end"
# served <container> <repository path> <reference>: the cache container's access log has a successful manifest request for the image since the step started.
served() {
  kubectl -n "$cache_namespace" logs deployment/registry-cache -c "$1" --since-time="$since" | grep -E "http.request.uri=\"/v2/$2/manifests/$3[?\"]" | grep -q 'http.response.status=200'
}
served docker-hub library/busybox 1.36 || fail "docker pull of ${hub_pull} did not go through the cache"
served ghcr linuxserver/baseimage-alpine 3.11 || fail "docker pull of ${ghcr_pull} did not go through the cache"
served private grtest/private 1 || fail "docker pull of ${private_image} did not go through the cache"
served docker-hub library/alpine 3.20 || fail "the docker-container builder's pull of ${hub_build} did not go through the cache"
served ghcr linuxserver/baseimage-alpine 3.10 || fail "the docker-container builder's pull of ${ghcr_build} did not go through the cache"
log "Every pull went through the cache, the private image without a login"

log "Scaling the cache to nothing and pulling again"
kubectl -n "$cache_namespace" scale deployment/registry-cache --replicas=0 >/dev/null
kubectl -n "$cache_namespace" wait --for=delete pod -l app.kubernetes.io/name=registry-cache --timeout=120s >/dev/null 2>&1 || true
run_pod runner-direct "set -euo pipefail
until docker info >/dev/null 2>&1; do sleep 1; done
docker pull -q ${direct_hub_pull}
docker pull -q ${direct_ghcr_pull}
${builder}
printf 'FROM ${direct_hub_build}\nRUN true\n' | docker buildx build -q - >/dev/null
echo REGISTRY-CACHE-DOWN-OK"
kubectl -n "$namespace" logs runner-direct -c runner | grep -qx REGISTRY-CACHE-DOWN-OK || fail "the step with the cache down did not reach its end"
log "PASS: pulls and builds went through the cache, and with the cache down went to the registries directly"
