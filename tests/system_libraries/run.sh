#!/usr/bin/env bash
# Relocation test for the reference system-library capability images in tool-images/ (openmpi, highs, libpq). For each one named it builds the image for this machine's architecture, copies its /sysroot payload into a volume at /opt/sysroot exactly as the role's init container does (the same command, as the runner's uid, into a world-writable directory like an emptyDir), renders the job-started hook data for a profile carrying that capability through the role's own tasks, and then, as the runner user, runs the role's job-started dispatcher, applies the GITHUB_PATH and GITHUB_ENV it wrote as the runner does, and runs the checks in tests/system_libraries/checks/<payload>.sh twice: check_stock in the stock actions-runner image (the payload's programs and libraries resolve and run), and check_compiled in the reference build runner image (tool-images/build: that same stock image, at the version and digest it pins, with build-essential and pkg-config added), since the stock image has no compiler (a small program compiled against the payload's headers runs). The stock phase runs the build image's own base, so both phases share one runner release. The payloads are built under /sysroot and run from /opt/sysroot, where the build prefix does not exist, so every check runs relocated.
#
# Usage: tests/system_libraries/run.sh [openmpi|highs|libpq]... (default: all three). Needs Docker, python3 and ansible-playbook (ANSIBLE_PLAYBOOK overrides which); reaches the network for the sources, the runner image and its distribution's packages. Leaves the built images (grtest/sysroot-<payload>:test) in place so a rerun reuses their build cache; removes its volumes on exit.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ansible_playbook="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
build_dockerfile="${repo_root}/tool-images/build/Dockerfile"
runner_image="ghcr.io/actions/actions-runner:$(sed -n 's/^ARG RUNNER_VERSION=//p' "$build_dockerfile")@$(sed -n 's/^ARG RUNNER_DIGEST=//p' "$build_dockerfile")"
toolchain_image=grtest/runner-build:test
sysroot=/opt/sysroot
hooks_dir=/etc/github-runner/hooks
runner_uid=1001
work="$(mktemp -d "${TMPDIR:-/tmp}/grtest-system-libraries.XXXXXX")"
volumes=()

log() { echo "==> $*"; }
fail() {
  echo "FAIL: $*" >&2
  exit 1
}
cleanup() {
  for volume in "${volumes[@]}"; do
    docker volume rm -f "$volume" >/dev/null 2>&1 || true
  done
  rm -rf "$work"
}
trap cleanup EXIT

# The extra variables each reference image is declared with, as a JSON object: the env of its capability in the role README's example.
capability_env() {
  case "$1" in
    openmpi) echo "{\"OPAL_PREFIX\": \"${sysroot}\"}" ;;
    highs | libpq) echo "{}" ;;
    *) fail "unknown payload $1 (expected openmpi, highs or libpq)" ;;
  esac
}

payloads=("$@")
[ "${#payloads[@]}" -gt 0 ] || payloads=(openmpi highs libpq)

log "Building ${toolchain_image} from tool-images/build: ${runner_image} with build-essential and pkg-config"
docker build -q -t "$toolchain_image" "${repo_root}/tool-images/build" >/dev/null

# What the runner does at job start: run the hook named by ACTIONS_RUNNER_HOOK_JOB_STARTED with fresh environment files, then apply them to the job's steps (each GITHUB_PATH line goes in front of the PATH so far, each GITHUB_ENV line is set). Then the payload's checks for the phase.
cat >"${work}/step.sh" <<'EOF'
set -euo pipefail
export GITHUB_PATH="$(mktemp)" GITHUB_ENV="$(mktemp)"
bash -e "$ACTIONS_RUNNER_HOOK_JOB_STARTED"
while IFS= read -r directory; do PATH="${directory}:${PATH}"; done <"$GITHUB_PATH"
export PATH
while IFS= read -r line; do export "${line?}"; done <"$GITHUB_ENV"
# The payload was built under /sysroot; nothing may fall back to it.
[ ! -e /sysroot ]
. "/checks/${PAYLOAD}.sh"
"check_${PHASE}"
echo SYSTEM-LIBRARY-OK
EOF

for payload in "${payloads[@]}"; do
  env_json="$(capability_env "$payload")"
  image="grtest/sysroot-${payload}:test"
  volume="grtest-sysroot-${payload}-$$"
  hooks="${work}/${payload}/hooks"
  mkdir -p "$hooks"

  log "Building ${image} from tool-images/${payload}"
  docker build -q -t "$image" "${repo_root}/tool-images/${payload}" >/dev/null

  log "Copying its payload into ${sysroot} as the init container does"
  volumes+=("$volume")
  docker volume create "$volume" >/dev/null
  # An emptyDir is world-writable, so the init container, running as the runner's uid, can write to it; a fresh Docker volume is root's, so it is opened up first.
  docker run --rm -v "${volume}:${sysroot}" --entrypoint /bin/sh "$image" -c "chmod 0777 ${sysroot}"
  docker run --rm --user "${runner_uid}:${runner_uid}" -v "${volume}:${sysroot}" --entrypoint /bin/sh "$image" -c 'cp -R /sysroot/. "$1"/' copy-sysroot "$sysroot"

  log "Rendering the job-started hook data through the role"
  python3 - "${repo_root}/plugins/filter/arc.py" "${work}/${payload}/extra.json" "$payload" "$image" "$env_json" "${work}/${payload}/rendered.json" <<'EOF'
import importlib.util, json, sys
plugin, extra, name, image, env, output = sys.argv[1:]
spec = importlib.util.spec_from_file_location("arc", plugin)
arc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arc)
result = arc.arc_profiles([{"name": "Example", "image": "unused:1", "scale_set_profiles": [{"max_runners": 1, "capabilities": [name]}]}], require_app_id=False)
assert result["errors"] == [], result["errors"]
capability = {"sysroot_image": image}
if json.loads(env):
    capability["env"] = json.loads(env)
catalogue = {name: capability}
errors = arc.arc_capability_errors(result["profiles"], catalogue)
assert errors == [], errors
with open(extra, "w") as out:
    json.dump({"profile": result["profiles"][0], "github_runner_arc_capabilities": catalogue, "render_output": output}, out)
EOF
  ANSIBLE_COLLECTIONS_PATH="${repo_root}/playbooks/collections" ANSIBLE_LOCALHOST_WARNING=false ANSIBLE_INVENTORY_UNPARSED_WARNING=false \
    "$ansible_playbook" "${repo_root}/tests/capabilities/render.yml" -e "@${work}/${payload}/extra.json" >/dev/null
  # The ConfigMap's files, laid out as the role's volume mounts them.
  python3 - "${work}/${payload}/rendered.json" "$hooks" "$sysroot" <<'EOF'
import json, os, sys
rendered, hooks, sysroot = sys.argv[1:]
values = json.load(open(rendered))["values"]
data = json.load(open(rendered))["job_started"]
mounts = {mount["name"]: mount["mountPath"] for container in values["template"]["spec"]["containers"] if container["name"] == "runner" for mount in container["volumeMounts"]}
assert mounts.get("sysroot") == sysroot, mounts
for key, content in data.items():
    path = os.path.join(hooks, key if not key.endswith(".sh") or key == "job-started.sh" else os.path.join("job-started.d", key))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as out:
        out.write(content)
EOF
  [ -f "${hooks}/sysroot-env" ] || fail "the role rendered no sysroot-env for ${payload}"

  for phase in stock compiled; do
    image_for_phase="$runner_image"
    [ "$phase" = stock ] || image_for_phase="$toolchain_image"
    log "Running check_${phase} for ${payload} in ${image_for_phase} from the relocated prefix"
    docker run --rm --user "${runner_uid}:${runner_uid}" \
      -v "${volume}:${sysroot}" -v "${hooks}:${hooks_dir}:ro" \
      -v "${repo_root}/tests/system_libraries/checks:/checks:ro" \
      -v "${work}/step.sh:/step.sh:ro" \
      -e "ACTIONS_RUNNER_HOOK_JOB_STARTED=${hooks_dir}/job-started.sh" -e "PAYLOAD=${payload}" -e "PHASE=${phase}" \
      --entrypoint bash "$image_for_phase" /step.sh | tee "${work}/${payload}/${phase}.log"
    grep -qx SYSTEM-LIBRARY-OK "${work}/${payload}/${phase}.log" || fail "check_${phase} for ${payload} did not reach its end"
  done
  log "PASS: ${payload} works from ${sysroot}"
done
