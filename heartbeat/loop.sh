#!/usr/bin/env bash
# Container entrypoint for the `heartbeat` Deployment. Runs as an ordinary in-cluster pod (no nodeSelector, no host networking - Kubernetes' own scheduler places and reschedules it): kubectl auto-detects in-cluster config from the pod's mounted ServiceAccount token, no KUBECONFIG needed. Runs heartbeat.sh on a fixed interval forever.
set -euo pipefail

HEARTBEAT_INTERVAL_SECONDS="${HEARTBEAT_INTERVAL_SECONDS:-180}"

while true; do
  /app/scripts/heartbeat.sh || true
  sleep "$HEARTBEAT_INTERVAL_SECONDS"
done
