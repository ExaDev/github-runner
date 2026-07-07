#!/usr/bin/env bash
# Container entrypoint for the `heartbeat` service. Builds a kubeconfig that
# reaches k3s over the compose network (the host kubeconfig points at
# 127.0.0.1, which is this container, not k3s), then runs heartbeat.sh on a
# fixed interval forever. `restart: unless-stopped` on the service means no
# external scheduler is needed.
set -euo pipefail

SRC_KUBECONFIG="/kubeconfig/kubeconfig.yaml"
KUBECONFIG="/tmp/kubeconfig.yaml"
export KUBECONFIG
HEARTBEAT_INTERVAL_SECONDS="${HEARTBEAT_INTERVAL_SECONDS:-180}"

# Wait for k3s to have written the host kubeconfig (it's generated at startup).
echo "Waiting for k3s kubeconfig at ${SRC_KUBECONFIG}..."
until [ -f "$SRC_KUBECONFIG" ]; do
  sleep 2
done

# Rewrite the server address so kubectl (running here, in the compose network)
# reaches the k3s service by name. Match any server URL and point it at k3s.
# k3s is started with --tls-san k3s so its API cert is valid for "k3s".
sed -E 's#server: https://[^[:space:]]+#server: https://k3s:6443#' \
  "$SRC_KUBECONFIG" > "$KUBECONFIG"
chmod 600 "$KUBECONFIG"
echo "kubeconfig ready (server -> https://k3s:6443); starting heartbeat loop."

while true; do
  /app/scripts/heartbeat.sh || true
  sleep "$HEARTBEAT_INTERVAL_SECONDS"
done
