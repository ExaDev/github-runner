#!/usr/bin/env bash
# Container entrypoint for the role controller Deployment (see roles/github_runner_arc/templates/fleet-role.yaml.j2). Runs scripts/fleet-role.sh every FLEET_ROLE_SAMPLE_SECONDS forever, the period the probes sample at; a failed pass is logged and retried on the next one. kubectl finds the in-cluster config from the pod's mounted ServiceAccount token.
set -euo pipefail

FLEET_ROLE_SAMPLE_SECONDS="${FLEET_ROLE_SAMPLE_SECONDS:?FLEET_ROLE_SAMPLE_SECONDS must be set}"

while true; do
  /app/scripts/fleet-role.sh || true
  sleep "$FLEET_ROLE_SAMPLE_SECONDS"
done
