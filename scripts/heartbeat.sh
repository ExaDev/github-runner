#!/usr/bin/env bash
# Runs inside the `heartbeat` in-cluster Deployment (see heartbeat/loop.sh for the ~3-min loop). Checks the k3s cluster and ARC controller are healthy and, if so, refreshes a single secret gist with a unix timestamp ("healthy until"). A consumer such as a runner fallback action reads that gist to decide between self-hosted and GitHub-hosted runners.
#
# Environment (provided by the Deployment - see roles/github_runner_arc/tasks/install_platform.yml):
# - kubectl needs no KUBECONFIG here: running as a pod with a mounted ServiceAccount token, client-go auto-detects in-cluster config.
# - HEARTBEAT_GH_TOKEN: a GitHub PAT with `gist` scope (write the gist), from a Secret
# - HEARTBEAT_GIST_ID: the secret gist id to refresh
# - HEALTH_GIST_FILE: the gist file the health reason (healthy, why not, nodes under pressure) is written to every tick (default fleet-health.json)
# - HEARTBEAT_STATE_NAMESPACE: namespace holding the autoscaler's own status ConfigMap (see scripts/autoscaler.sh)
# - AUTOSCALER_STATUS_CONFIGMAP: name of that ConfigMap
# - HEARTBEAT_CONTROLLER_NAMESPACE: namespace of the ARC controller's Deployment, when it is not the default actions-runner-controller
# - HEARTBEAT_UNSCHEDULABLE_AFTER_SECONDS: how long a runner pod may stay unschedulable before the heartbeat stops refreshing (default: one heartbeat interval)
set -euo pipefail

HEARTBEAT_GH_TOKEN="${HEARTBEAT_GH_TOKEN:?HEARTBEAT_GH_TOKEN must be set (a PAT with gist scope)}"
HEARTBEAT_GIST_ID="${HEARTBEAT_GIST_ID:?HEARTBEAT_GIST_ID must be set}"
GIST_FILE="${GIST_FILE:-arc-healthy-until}"
HEALTH_GIST_FILE="${HEALTH_GIST_FILE:-fleet-health.json}"
HEARTBEAT_STATE_NAMESPACE="${HEARTBEAT_STATE_NAMESPACE:-github-runner-platform}"
AUTOSCALER_STATUS_CONFIGMAP="${AUTOSCALER_STATUS_CONFIGMAP:-autoscaler-status}"
AUTOSCALER_STATUS_GIST_FILE="${AUTOSCALER_STATUS_GIST_FILE:-autoscaler-status.json}"
# How far in the future to set the timestamp: comfortably longer than the loop interval, so a single missed/slow tick doesn't look like an outage, but short enough that a real outage is detected promptly.
HEARTBEAT_WINDOW_SECONDS="${HEARTBEAT_WINDOW_SECONDS:-600}"
HEARTBEAT_CONTROLLER_NAMESPACE="${HEARTBEAT_CONTROLLER_NAMESPACE:-actions-runner-controller}"
# How long a runner pod may sit unschedulable before the fleet counts as unable to take work: one heartbeat interval (the loop's own, see heartbeat/loop.sh), so the condition has been seen on at least two consecutive ticks and a pod that is merely being placed never trips it.
HEARTBEAT_UNSCHEDULABLE_AFTER_SECONDS="${HEARTBEAT_UNSCHEDULABLE_AFTER_SECONDS:-${HEARTBEAT_INTERVAL_SECONDS:-180}}"

healthy=true
reasons=()

if ! kubectl get nodes --no-headers 2>/dev/null | grep -q Ready; then
  reasons+=("no node reports Ready")
  healthy=false
fi

if ! kubectl get deployment -n "$HEARTBEAT_CONTROLLER_NAMESPACE" -l app.kubernetes.io/name=gha-rs-controller \
     -o jsonpath='{.items[0].status.readyReplicas}' 2>/dev/null | grep -q '^[1-9]'; then
  reasons+=("the ARC controller has no ready replica")
  healthy=false
fi

# A fleet with Ready nodes and a running controller can still have nowhere to put a runner (every schedulable node full, the rest cordoned or tainted for disk pressure, say). Jobs sent to it then queue behind the runners already busy, so stop vouching for it and let runner-fallback-action route to GitHub-hosted runners until the pods schedule again.
now="$(date +%s)"
if ! unschedulable="$(kubectl get pods -A -l actions-ephemeral-runner=True -o json 2>/dev/null \
    | jq --argjson now "$now" --argjson after "$HEARTBEAT_UNSCHEDULABLE_AFTER_SECONDS" \
      '[.items[] | select(any(.status.conditions[]?; .type == "PodScheduled" and .status == "False" and .reason == "Unschedulable" and ($now - (.lastTransitionTime | fromdateiso8601)) >= $after))] | length')"; then
  reasons+=("could not list runner pods")
  healthy=false
elif [ "$unschedulable" -gt 0 ]; then
  echo "${unschedulable} runner pod(s) unschedulable for at least ${HEARTBEAT_UNSCHEDULABLE_AFTER_SECONDS}s" >&2
  reasons+=("${unschedulable} runner pod(s) unschedulable for at least ${HEARTBEAT_UNSCHEDULABLE_AFTER_SECONDS}s")
  healthy=false
fi

# Nodes reporting a pressure condition (disk, memory, PIDs) are named in the published reason without making the fleet unhealthy: the other nodes still serve jobs, but the lost capacity would otherwise go unseen until jobs queued (a node sat under disk pressure for days with nothing reporting it).
node_pressure="$(kubectl get nodes -o json 2>/dev/null \
  | jq -c '[.items[] | {node: .metadata.name, conditions: [.status.conditions[]? | select((.type | endswith("Pressure")) and .status == "True") | .type]} | select(.conditions | length > 0)]' || echo '[]')"

# The reason file is written on every tick, healthy or not, so the gist always says why the fleet is or is not being vouched for. GitHub deletes a gist file whose content is empty, hence the JSON object rather than an empty string for the healthy case.
reasons_json="$(printf '%s\n' "${reasons[@]}" | jq -R . | jq -sc 'map(select(length > 0))')"
health_json="$(jq -nc --argjson healthy "$healthy" --argjson reasons "$reasons_json" --argjson node_pressure "$node_pressure" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{healthy: $healthy, reasons: $reasons, node_pressure: $node_pressure, at: $at}')"
files_json="$(jq -n --arg content "$health_json" --arg file "$HEALTH_GIST_FILE" '{($file): {content: $content}}')"

if [ "$healthy" = "true" ]; then
  healthy_until=$(($(date +%s) + HEARTBEAT_WINDOW_SECONDS))
  # PATCH the gist's timestamp file, plus the autoscaler's own status ConfigMap when one exists (it won't on a fresh cluster before the autoscaler's first poll) - zero new credentials, reusing this service's existing gist-write access rather than giving the autoscaler its own.
  files_json="$(jq --arg content "$healthy_until" --arg file "$GIST_FILE" '. + {($file): {content: $content}}' <<< "$files_json")"
  status_json="$(kubectl get configmap "$AUTOSCALER_STATUS_CONFIGMAP" -n "$HEARTBEAT_STATE_NAMESPACE" \
    -o jsonpath='{.data.status\.json}' 2>/dev/null || true)"
  if [ -n "$status_json" ]; then
    files_json="$(jq --arg file "$AUTOSCALER_STATUS_GIST_FILE" --arg status "$status_json" \
      '. + {($file): {content: $status}}' <<< "$files_json")"
  fi
fi

body="$(jq -n --argjson files "$files_json" '{files: $files}')"
curl -fsS -X PATCH \
  -H "Authorization: Bearer ${HEARTBEAT_GH_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -d "$body" \
  "https://api.github.com/gists/${HEARTBEAT_GIST_ID}" >/dev/null

if [ "$healthy" != "true" ]; then
  echo "Unhealthy (${reasons[*]}) - heartbeat gist reason updated, timestamp not refreshed" >&2
  exit 1
fi
echo "Healthy - gist ${HEARTBEAT_GIST_ID} refreshed to ${healthy_until}"
