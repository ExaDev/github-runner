#!/usr/bin/env bash
# Container entrypoint for the `autoscaler` service. Mirrors heartbeat/loop.sh: host networking, so kubectl (with the host kubeconfig k3s generates) reaches the k3s API at 127.0.0.1:6443 directly. Runs scripts/autoscaler.sh on a fixed interval forever; `restart: unless-stopped` on the service means no external scheduler is needed.
set -euo pipefail

KUBECONFIG="/kubeconfig/kubeconfig.yaml"
export KUBECONFIG
AUTOSCALER_POLL_SECONDS="${AUTOSCALER_POLL_SECONDS:-45}"

echo "Waiting for k3s kubeconfig at ${KUBECONFIG}..."
until [ -f "$KUBECONFIG" ]; do
  sleep 2
done
echo "kubeconfig ready; starting autoscaler loop (k3s API at 127.0.0.1:6443 via host networking)."

while true; do
  /app/scripts/autoscaler.sh || true
  sleep "$AUTOSCALER_POLL_SECONDS"
done
