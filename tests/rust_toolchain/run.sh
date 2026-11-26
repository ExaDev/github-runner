#!/usr/bin/env bash
# Integration test for the Rust tool image (tool-images/rust). Builds the image for this machine's architecture, copies its /toolcache into a volume at the tool cache exactly as the role's init container does (the same command, as the runner's uid, into a world-writable directory like an emptyDir), renders the job-started hook data for a profile declaring it with path: rust/<version>/bin through the role's own tasks, and then, in the reference build runner image (tool-images/build: the stock actions-runner image with a C compiler) as the runner user, runs the role's job-started dispatcher, applies the GITHUB_PATH it wrote as the runner does, and runs checks.sh: the tools report the pinned releases, the small crate in crate/ passes rustfmt, clippy, its test and cargo-deny, and cargo-zigbuild builds it for every target in the image's RUST_TARGETS. Every executable is then checked on this machine by inspect_binary.py (an Apple one loads no framework, a Linux one is static, each is for its target's processor), and the Linux one for this machine's processor is run.
#
# Usage: tests/rust_toolchain/run.sh. Needs Docker, python3 and ansible-playbook (ANSIBLE_PLAYBOOK overrides which); reaches the network for the toolchain, the runner image, its distribution's packages and crates.io. Leaves the built image (grtest/toolcache-rust:test) in place so a rerun reuses its build cache; removes its volume and files on exit.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
here="${repo_root}/tests/rust_toolchain"
ansible_playbook="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
dockerfile="${repo_root}/tool-images/rust/Dockerfile"
image=grtest/toolcache-rust:test
runner_image=grtest/runner-build:test
tool_cache=/opt/hostedtoolcache
hooks_dir=/etc/github-runner/hooks
runner_uid=1001
volume="grtest-toolcache-rust-$$"
work="$(mktemp -d "${TMPDIR:-/tmp}/grtest-rust-toolchain.XXXXXX")"

log() { echo "==> $*"; }
fail() {
  echo "FAIL: $*" >&2
  exit 1
}
cleanup() {
  docker volume rm -f "$volume" >/dev/null 2>&1 || true
  rm -rf "${work:?}"
}
trap cleanup EXIT

# The default of one of the Dockerfile's ARG pins, without the quotes a list is written in.
pin() {
  local value
  value="$(sed -n "s/^ARG $1=//p" "$dockerfile" | tr -d '"')"
  [ -n "$value" ] || fail "${dockerfile} pins no $1"
  echo "$value"
}
rust_version="$(pin RUST_VERSION)"
targets="$(pin RUST_TARGETS)"

log "Building ${image} from tool-images/rust"
docker build -t "$image" "${repo_root}/tool-images/rust"
log "Building ${runner_image} from tool-images/build"
docker build -q -t "$runner_image" "${repo_root}/tool-images/build" >/dev/null

log "Copying its tool cache into ${tool_cache} as the init container does"
docker volume create "$volume" >/dev/null
# An emptyDir is world-writable, so the init container, running as the runner's uid, can write to it; a fresh Docker volume is root's, so it is opened up first.
docker run --rm -v "${volume}:${tool_cache}" --entrypoint /bin/sh "$image" -c "chmod 0777 ${tool_cache}"
docker run --rm --user "${runner_uid}:${runner_uid}" -v "${volume}:${tool_cache}" --entrypoint /bin/sh "$image" -c 'cp -R /toolcache/. "$1"/' copy-toolcache "$tool_cache"

log "Rendering the job-started hook data through the role"
hooks="${work}/hooks"
mkdir -p "$hooks"
python3 - "${repo_root}/plugins/filter/arc.py" "${work}/extra.json" "$image" "rust/${rust_version}/bin" "${work}/rendered.json" <<'EOF'
import importlib.util, json, sys
plugin, extra, image, path, output = sys.argv[1:]
spec = importlib.util.spec_from_file_location("arc", plugin)
arc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arc)
result = arc.arc_profiles([{"name": "Example", "image": "unused:1", "scale_set_profiles": [{"max_runners": 1, "capabilities": ["rust"]}]}], require_app_id=False)
assert result["errors"] == [], result["errors"]
catalogue = {"rust": {"image": image, "path": path}}
errors = arc.arc_capability_errors(result["profiles"], catalogue)
assert errors == [], errors
with open(extra, "w") as out:
    json.dump({"profile": result["profiles"][0], "github_runner_arc_capabilities": catalogue, "render_output": output}, out)
EOF
ANSIBLE_COLLECTIONS_PATH="${repo_root}/playbooks/collections" ANSIBLE_LOCALHOST_WARNING=false ANSIBLE_INVENTORY_UNPARSED_WARNING=false \
  "$ansible_playbook" "${repo_root}/tests/capabilities/render.yml" -e "@${work}/extra.json" >/dev/null
# The ConfigMap's files, laid out as the role's volume mounts them.
python3 - "${work}/rendered.json" "$hooks" "$tool_cache" <<'EOF'
import json, os, sys
rendered, hooks, tool_cache = sys.argv[1:]
values = json.load(open(rendered))["values"]
data = json.load(open(rendered))["job_started"]
mounts = {mount["name"]: mount["mountPath"] for container in values["template"]["spec"]["containers"] if container["name"] == "runner" for mount in container["volumeMounts"]}
assert tool_cache in mounts.values(), mounts
for key, content in data.items():
    with open(os.path.join(hooks, key), "w") as out:
        out.write(content)
EOF
[ -f "${hooks}/tool-paths" ] || fail "the role rendered no tool-paths"

# The container writes the executables here as the runner's uid, which is not this user's.
output="${work}/output"
mkdir -p "$output"
chmod 0777 "$output"

log "Running the checks in ${runner_image} with the toolchain on PATH"
docker run --rm --user "${runner_uid}:${runner_uid}" \
  -v "${volume}:${tool_cache}" -v "${hooks}:${hooks_dir}:ro" \
  -v "${here}/crate:/crate:ro" -v "${here}/checks.sh:/checks.sh:ro" -v "${output}:/output" \
  -e "ACTIONS_RUNNER_HOOK_JOB_STARTED=${hooks_dir}/job-started.sh" -e "RUNNER_TOOL_CACHE=${tool_cache}" \
  -e "RUST_VERSION=${rust_version}" -e "RUST_TARGETS=${targets}" \
  -e "ZIG_RELEASE=$(pin ZIG_RELEASE)" -e "CARGO_ZIGBUILD_RELEASE=$(pin CARGO_ZIGBUILD_RELEASE)" -e "CARGO_DENY_RELEASE=$(pin CARGO_DENY_RELEASE)" \
  --entrypoint bash "$runner_image" /checks.sh | tee "${work}/checks.log"
grep -qx RUST-TOOLCHAIN-OK "${work}/checks.log" || fail "the checks did not reach their end"

log "Checking each executable"
failed=0
for target in $targets; do
  executables=("${output}/${target}"/*)
  [ "${#executables[@]}" -eq 1 ] && [ -f "${executables[0]}" ] || fail "expected one executable for ${target}, found ${executables[*]}"
  python3 "${here}/inspect_binary.py" "$target" "${executables[0]}" || failed=1
done
[ "$failed" -eq 0 ] || fail "an executable is not what its target calls for"

native="$(uname -m)"
[ "$native" != arm64 ] || native=aarch64
log "Running the ${native} Linux executable"
ran="$(docker run --rm -v "${output}/${native}-unknown-linux-musl:/run-native:ro" --entrypoint /run-native/cross-check "$runner_image")"
[ "$ran" = "answer 42" ] || fail "the native musl executable printed '${ran}'"
log "PASS: the Rust tool image builds every target"
