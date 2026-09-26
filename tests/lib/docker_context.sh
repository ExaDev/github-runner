#!/usr/bin/env bash
# Shared by the integration harnesses, sourced rather than run. Proves the cluster role's github_runner_cluster_docker_context pin wins over the host's current Docker context: it builds a throwaway Docker CLI configuration whose current context is a decoy pointing at a socket that does not exist, plus a context named for the pin that points at this machine's real daemon. Only the ansible-playbook runs see that configuration, through DOCKER_CONFIG; the harness's own docker commands keep the real one, whose current context is never changed. Any docker call the role made without the pin would reach the decoy and fail.

grtest_pinned_context=grtest-pinned
grtest_decoy_context=grtest-decoy

# Usage: grtest_setup_pinned_contexts <directory>. Creates the configuration there and checks the decoy really is the current context and the pinned one really reaches the daemon.
grtest_setup_pinned_contexts() {
  local config="$1" real_config="${DOCKER_CONFIG:-${HOME}/.docker}" endpoint
  endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"
  mkdir -p "$config"
  # Keep finding the CLI plugins (Compose above all) the real configuration finds, and nothing else from it: its credentials stay out.
  if [ -d "${real_config}/cli-plugins" ]; then
    ln -s "${real_config}/cli-plugins" "${config}/cli-plugins"
  fi
  python3 - "${real_config}/config.json" "${config}/config.json" <<'EOF'
import json, sys
try:
    with open(sys.argv[1]) as source:
        real = json.load(source)
except FileNotFoundError:
    real = {}
kept = {key: real[key] for key in ("cliPluginsExtraDirs",) if key in real}
with open(sys.argv[2], "w") as target:
    json.dump(kept, target)
EOF
  DOCKER_CONFIG="$config" docker context create "$grtest_pinned_context" --docker "host=${endpoint}" >/dev/null
  DOCKER_CONFIG="$config" docker context create "$grtest_decoy_context" --docker "host=unix://${config}/decoy.sock" >/dev/null
  DOCKER_CONFIG="$config" docker context use "$grtest_decoy_context" >/dev/null 2>&1
  if DOCKER_CONFIG="$config" docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    echo "The decoy context reached a daemon, so it proves nothing" >&2
    return 1
  fi
  DOCKER_CONFIG="$config" docker --context "$grtest_pinned_context" version --format '{{.Server.Version}}' >/dev/null
  DOCKER_CONFIG="$config" docker compose version >/dev/null
}

# Usage: grtest_assert_current_context_unchanged <directory>. Fails unless the throwaway configuration's current context is still the decoy, i.e. the role did not switch it.
grtest_assert_current_context_unchanged() {
  local current
  current="$(DOCKER_CONFIG="$1" docker context show)"
  if [ "$current" != "$grtest_decoy_context" ]; then
    echo "The role changed the current Docker context from ${grtest_decoy_context} to ${current}" >&2
    return 1
  fi
}
