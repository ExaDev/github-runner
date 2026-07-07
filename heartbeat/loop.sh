#!/usr/bin/env bash
# Container entrypoint for the `heartbeat` service. The service uses host
# networking, so kubectl (with the host kubeconfig k3s generates) reaches the
# k3s API at 127.0.0.1:6443 directly - no URL rewrite, no TLS-SAN change.
# Runs heartbeat.sh on a fixed interval forever; `restart: unless-stopped` on
# the service means no external scheduler is needed.
set -euo pipefail

KUBECONFIG="/kubeconfig/kubeconfig.yaml"
export KUBECONFIG
HEARTBEAT_INTERVAL_SECONDS="${HEARTBEAT_INTERVAL_SECONDS:-180}"

# Wait for k3s to have written the host kubeconfig (it's generated at startup).
echo "Waiting for k3s kubeconfig at ${KUBECONFIG}..."
until [ -f "$KUBECONFIG" ]; do
  sleep 2
done
echo "kubeconfig ready; starting heartbeat loop (k3s API at 127.0.0.1:6443 via host networking)."

while true; do
  /app/scripts/heartbeat.sh || true
  sleep "$HEARTBEAT_INTERVAL_SECONDS"
done
