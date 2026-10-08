#!/usr/bin/env bash
# Runner job-started hook (ACTIONS_RUNNER_HOOK_JOB_STARTED) for a profile with capabilities. The github_runner_arc role mounts it, with the profile's tool paths and hook scripts, from a ConfigMap at a fixed directory (see the role README's Capabilities section), and points the runner at it the same way the runner image points at its job-completed hook. The runner runs it as the runner user before the job's first step, with the job's default environment variables and its environment files (GITHUB_PATH, GITHUB_ENV).
#
# First it puts each tool path on PATH: every line of tool-paths is <tool>/<version>, optionally followed by /<subdirectory>, and the directory added is <tool>/<version>/<arch>[/<subdirectory>] under RUNNER_TOOL_CACHE, where <arch> is the one architecture the capability's image put there (the one with a <arch>.complete marker beside it). Next it applies sysroot-env, the system-library capabilities' environment: a NAME+=directory line prepends the directory to NAME's current value (through GITHUB_PATH for PATH), and a NAME=value line sets NAME. Each is also exported here, so the scripts after it see it too. A PKG_CONFIG_PATH line also sets PKG_CONFIG_LIBDIR to that directory followed by pkg-config's own default search path, because actions/setup-python replaces PKG_CONFIG_PATH with its own directory instead of extending it, which would otherwise hide the sysroot's .pc files from every later step, whereas nothing rewrites PKG_CONFIG_LIBDIR and pkg-config searches it after PKG_CONFIG_PATH. Then it runs each script in job-started.d in name order, each with bash -e, as the runner runs a hook.
#
# Unlike the job-completed hook, a failure here fails the job: a job whose tools or setup did not arrive should stop before its first step rather than fail obscurely later.
set -euo pipefail

hooks_dir="$(dirname "${BASH_SOURCE[0]}")"

fail() {
  echo "job-started: $*" >&2
  exit 1
}

if [ -f "${hooks_dir}/tool-paths" ]; then
  [ -n "${RUNNER_TOOL_CACHE:-}" ] || fail "RUNNER_TOOL_CACHE is not set"
  [ -n "${GITHUB_PATH:-}" ] || fail "GITHUB_PATH is not set"
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    tool_version="$(cut -d/ -f1-2 <<<"$entry")"
    subdirectory="${entry#"$tool_version"}"
    markers=("${RUNNER_TOOL_CACHE}/${tool_version}"/*.complete)
    [ -e "${markers[0]}" ] || fail "${tool_version} is not in the tool cache (no ${RUNNER_TOOL_CACHE}/${tool_version}/<arch>.complete)"
    [ "${#markers[@]}" -eq 1 ] || fail "${tool_version} has more than one architecture in the tool cache: ${markers[*]}"
    directory="${markers[0]%.complete}${subdirectory}"
    [ -d "$directory" ] || fail "${directory} does not exist"
    echo "Adding ${directory} to PATH"
    echo "$directory" >>"$GITHUB_PATH"
  done <"${hooks_dir}/tool-paths"
fi

if [ -f "${hooks_dir}/sysroot-env" ]; then
  [ -n "${GITHUB_PATH:-}" ] || fail "GITHUB_PATH is not set"
  [ -n "${GITHUB_ENV:-}" ] || fail "GITHUB_ENV is not set"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    name="${line%%=*}"
    value="${line#*=}"
    if [ "${name%+}" != "$name" ]; then
      name="${name%+}"
      if [ "$name" = PATH ]; then
        echo "Adding ${value} to PATH"
        echo "$value" >>"$GITHUB_PATH"
        export PATH="${value}:${PATH}"
        continue
      fi
      current="${!name:-}"
      if [ "$name" = PKG_CONFIG_PATH ]; then
        libdir_default="${PKG_CONFIG_LIBDIR:-}"
        # Without pkg-config there is no default to keep and nothing reads the variable; a pkg-config installed later in the job finds the sysroot through PKG_CONFIG_PATH alone.
        if [ -z "$libdir_default" ] && command -v pkg-config >/dev/null; then
          libdir_default="$(pkg-config --variable=pc_path pkg-config)"
        fi
        if [ -n "$libdir_default" ]; then
          libdir="${value}:${libdir_default}"
          echo "Setting PKG_CONFIG_LIBDIR"
          echo "PKG_CONFIG_LIBDIR=${libdir}" >>"$GITHUB_ENV"
          export PKG_CONFIG_LIBDIR="$libdir"
        fi
      fi
      value="${value}${current:+:${current}}"
    fi
    echo "Setting ${name}"
    echo "${name}=${value}" >>"$GITHUB_ENV"
    export "${name}=${value}"
  done <"${hooks_dir}/sysroot-env"
fi

for script in "${hooks_dir}/job-started.d"/*.sh; do
  [ -e "$script" ] || continue
  echo "Running $(basename "$script")"
  bash -e "$script"
done
