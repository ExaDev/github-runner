#!/usr/bin/env bash
# Run on a schedule (see launchd/com.exadev.github-runner.heartbeat.plist).
# Checks whether the local k3s cluster and ARC controller are healthy, and
# if so, pushes a fresh "healthy until" timestamp to an ExaDev org-level
# Actions variable. This exists because ARC's autoscaling means there is
# normally NO pre-existing "online runner" to check the way the old
# myoung34-based design's exadev/runner-fallback-action did (query
# /orgs/{org}/actions/runners for an online match) - with minRunners: 0,
# pods only exist while a job is actually running. runner-fallback-action's
# check is therefore redesigned around infrastructure health (is this
# heartbeat still fresh?) rather than "is a runner online right now" -
# see exadev/runner-fallback-action's docs/spec.md addendum.
set -euo pipefail
cd "$(dirname "$0")/.."

export KUBECONFIG
KUBECONFIG="$(pwd)/kubeconfig/kubeconfig.yaml"

# How far in the future to set the healthy-until timestamp: comfortably
# longer than the launchd interval below, so a single missed/slow tick
# doesn't spuriously look like an outage, but short enough that a real
# outage is detected promptly.
HEARTBEAT_WINDOW_SECONDS=600

healthy=true

if ! kubectl get nodes --no-headers 2>/dev/null | grep -q Ready; then
  healthy=false
fi

if ! kubectl get deployment -n actions-runner-controller -l app.kubernetes.io/name=gha-rs-controller \
     -o jsonpath='{.items[0].status.readyReplicas}' 2>/dev/null | grep -q '^[1-9]'; then
  healthy=false
fi

if [ "$healthy" != "true" ]; then
  echo "Unhealthy - not updating ARC_HEALTHY_UNTIL" >&2
  exit 1
fi

healthy_until=$(($(date +%s) + HEARTBEAT_WINDOW_SECONDS))
gh variable set ARC_HEALTHY_UNTIL --org ExaDev --body "$healthy_until"
echo "Healthy - ARC_HEALTHY_UNTIL set to ${healthy_until}"
