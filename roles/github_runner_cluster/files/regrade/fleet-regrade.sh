#!/usr/bin/env bash
# Runs fleet-regrade in the regrade service of the cluster role's Compose file: the platform-neutral core, then the Docker actuator, both from the directory the Compose file mounts beside this script, so a collection upgrade reaches the service when it restarts without the image being built again. The settings are in fleet-regrade.env in the same directory, which the role templates.
set -euo pipefail

lib=$(dirname "$(readlink -f "$0")")
FLEET_REGRADE_ENV=${FLEET_REGRADE_ENV:-$lib/fleet-regrade.env}

# shellcheck source=/dev/null
. "$lib/fleet-regrade-core.sh"
# shellcheck source=/dev/null
. "$lib/fleet-regrade-docker.sh"

regrade_main "$@"
