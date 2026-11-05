#!/usr/bin/env bash
# Validate the published-image canary inputs before starting the mesh.
set -euo pipefail

image="${1:?image reference required}"
scenario="${2:?scenario required}"
if [[ ! "$image" =~ ^[a-z0-9][a-z0-9._:/-]*@sha256:[a-f0-9]{64}$ ]]; then
  printf '%s\n' 'Image must be a lowercase registry reference pinned by sha256 digest' >&2
  exit 1
fi
case "$scenario" in
  role_controller | role_controller_swap | role_swap | role_swap_paused | role_swap_blocked_peer | role_swap_refused) ;;
  *) printf 'Unsupported published-image scenario: %s\n' "$scenario" >&2; exit 1 ;;
esac
