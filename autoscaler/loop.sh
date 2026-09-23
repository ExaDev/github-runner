#!/usr/bin/env bash
# Container entrypoint for the `autoscaler` Deployment. Mirrors heartbeat/loop.sh: runs as an ordinary in-cluster pod (no nodeSelector, no host networking), so kubectl auto-detects in-cluster config from the pod's mounted ServiceAccount token, no KUBECONFIG needed. Runs scripts/autoscaler.sh on a fixed interval forever.
set -euo pipefail

AUTOSCALER_POLL_SECONDS="${AUTOSCALER_POLL_SECONDS:-45}"

while true; do
  /app/scripts/autoscaler.sh || true
  sleep "$AUTOSCALER_POLL_SECONDS"
done
