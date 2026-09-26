#!/usr/bin/env bash
# Container entrypoint for the node recovery Deployment (see roles/github_runner_arc/templates/node-recovery.yaml.j2). Runs scripts/node-recovery.sh every NODE_RECOVERY_POLL_SECONDS forever; a failed pass is logged and retried on the next one. kubectl finds the in-cluster config from the pod's mounted ServiceAccount token.
set -euo pipefail

NODE_RECOVERY_POLL_SECONDS="${NODE_RECOVERY_POLL_SECONDS:-15}"

while true; do
  /app/scripts/node-recovery.sh || true
  sleep "$NODE_RECOVERY_POLL_SECONDS"
done
