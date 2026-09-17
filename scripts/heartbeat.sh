#!/usr/bin/env bash
# Runs inside the `heartbeat` docker-compose service (see heartbeat/loop.sh for
# the ~3-min loop). Checks the local k3s cluster and ARC controller are
# healthy and, if so, refreshes a single secret gist with a unix timestamp
# ("healthy until"). ExaDev/runner-fallback-action reads that gist to decide
# self-hosted vs ubuntu-latest - see this action's docs/spec.md.
#
# Environment (provided by the compose service):
# - KUBECONFIG: a kubeconfig whose server reaches k3s (loop.sh rewrites the host kubeconfig to https://k3s:6443)
# - HEARTBEAT_GH_TOKEN: a GitHub PAT with `gist` scope (write the gist)
# - HEARTBEAT_GIST_ID: the secret gist id to refresh
# - HEARTBEAT_STATE_DIR: shared, read-only mount of the autoscaler's own state dir (see scripts/autoscaler.sh)
set -euo pipefail

HEARTBEAT_GH_TOKEN="${HEARTBEAT_GH_TOKEN:?HEARTBEAT_GH_TOKEN must be set (a PAT with gist scope)}"
HEARTBEAT_GIST_ID="${HEARTBEAT_GIST_ID:?HEARTBEAT_GIST_ID must be set}"
GIST_FILE="${GIST_FILE:-arc-healthy-until}"
HEARTBEAT_STATE_DIR="${HEARTBEAT_STATE_DIR:-/state}"
AUTOSCALER_STATUS_PATH="$HEARTBEAT_STATE_DIR/autoscaler-status.json"
AUTOSCALER_STATUS_GIST_FILE="${AUTOSCALER_STATUS_GIST_FILE:-autoscaler-status.json}"
# How far in the future to set the timestamp: comfortably longer than the
# loop interval, so a single missed/slow tick doesn't look like an outage,
# but short enough that a real outage is detected promptly.
HEARTBEAT_WINDOW_SECONDS="${HEARTBEAT_WINDOW_SECONDS:-600}"

healthy=true

if ! kubectl get nodes --no-headers 2>/dev/null | grep -q Ready; then
  healthy=false
fi

if ! kubectl get deployment -n actions-runner-controller -l app.kubernetes.io/name=gha-rs-controller \
     -o jsonpath='{.items[0].status.readyReplicas}' 2>/dev/null | grep -q '^[1-9]'; then
  healthy=false
fi

if [ "$healthy" != "true" ]; then
  echo "Unhealthy - not refreshing the heartbeat gist" >&2
  exit 1
fi

healthy_until=$(($(date +%s) + HEARTBEAT_WINDOW_SECONDS))
# PATCH the gist's timestamp file, plus the autoscaler's own status file when one exists (it won't on a fresh cluster before the autoscaler's first poll) - zero new credentials, reusing this service's existing gist-write access rather than giving the autoscaler its own.
files_json="$(jq -n --arg content "$healthy_until" --arg file "$GIST_FILE" '{($file): {content: $content}}')"
if [ -f "$AUTOSCALER_STATUS_PATH" ]; then
  files_json="$(jq --arg file "$AUTOSCALER_STATUS_GIST_FILE" --slurpfile status "$AUTOSCALER_STATUS_PATH" \
    '. + {($file): {content: ($status[0] | tostring)}}' <<< "$files_json")"
fi
body="$(jq -n --argjson files "$files_json" '{files: $files}')"

curl -fsS -X PATCH \
  -H "Authorization: Bearer ${HEARTBEAT_GH_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -d "$body" \
  "https://api.github.com/gists/${HEARTBEAT_GIST_ID}" >/dev/null
echo "Healthy - gist ${HEARTBEAT_GIST_ID} refreshed to ${healthy_until}"
