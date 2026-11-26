#!/usr/bin/env bash
# What tests/rust_toolchain/run.sh runs inside the runner image: the job-started hook, as the runner runs it before a job's first step, then the toolchain's checks. Reads the pins from the environment, the crate from /crate, and writes each target's executable to /output/<target>/.
set -euo pipefail

# The executables are removed by the user who ran the test, who is not this one.
trap 'chmod -R a+rwX /output/* 2>/dev/null || true' EXIT

GITHUB_PATH="$(mktemp)"
GITHUB_ENV="$(mktemp)"
export GITHUB_PATH GITHUB_ENV
bash -e "$ACTIONS_RUNNER_HOOK_JOB_STARTED"
while IFS= read -r directory; do PATH="${directory}:${PATH}"; done <"$GITHUB_PATH"
export PATH

# Fails unless the first line a tool prints names exactly the release.
reports() {
  local release="$1" line
  shift
  line="$("$@" | head -n 1)"
  echo "$line"
  case " ${line} " in
    *" ${release} "*) ;;
    *) echo "expected release ${release} from: $*" >&2; exit 1 ;;
  esac
}
for tool in rustc rustdoc cargo rustfmt cargo-clippy clippy-driver cargo-zigbuild cargo-deny zig; do
  case "$(command -v "$tool")" in
    "${RUNNER_TOOL_CACHE}"/*) ;;
    *) echo "${tool} does not come from the tool cache" >&2; exit 1 ;;
  esac
done
reports "$RUST_VERSION" rustc --version
reports "$RUST_VERSION" cargo --version
reports "$ZIG_RELEASE" zig version
reports "$CARGO_ZIGBUILD_RELEASE" cargo-zigbuild --version
reports "$CARGO_DENY_RELEASE" cargo deny --version

cp -R /crate "${HOME}/crate"
cd "${HOME}/crate"
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
cargo deny --locked check licenses bans sources

for target in $RUST_TARGETS; do
  echo "==> cargo zigbuild --target ${target}"
  cargo zigbuild --locked --release --target "$target" 2>&1 | tee "${HOME}/build.log"
  # rustc only warns when it cannot strip an executable, so the warning is what shows the toolchain's own strip tool does not run.
  if grep -q 'rust-objcopy` failed' "${HOME}/build.log"; then
    echo "the toolchain's rust-objcopy did not run for ${target}" >&2
    exit 1
  fi
  mkdir "/output/${target}"
  case "$target" in
    *-windows-*) name=cross-check.exe ;;
    *) name=cross-check ;;
  esac
  cp "target/${target}/release/${name}" "/output/${target}/${name}"
done
echo RUST-TOOLCHAIN-OK
