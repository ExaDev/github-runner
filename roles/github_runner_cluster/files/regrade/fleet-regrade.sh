#!/usr/bin/env bash
# Runs fleet-regrade in the regrade service of the cluster role's Compose file: the platform-neutral core, then the Docker actuator, both from the directory the Compose file mounts beside this script, so a collection upgrade reaches the service when it restarts without the image being built again. The settings are in fleet-regrade.env in the same directory, which the role templates.
set -euo pipefail

lib=$(dirname "$(readlink -f "$0")")
FLEET_REGRADE_ENV=${FLEET_REGRADE_ENV:-$lib/fleet-regrade.env}

# shellcheck source=/dev/null
. "$lib/fleet-regrade-core.sh"
# shellcheck source=/dev/null
. "$lib/fleet-regrade-docker.sh"

# Memory pressure is what the core decides on. A kernel without it (built without CONFIG_PSI, or booted with psi=0) would fail on every sample, so the service stops at once and says why.
case ${1:-} in
  watch | status)
    if [ ! -r "${PSI_FILE:-/proc/pressure/memory}" ]; then
      echo "The kernel of this Docker host exposes no memory pressure (${PSI_FILE:-/proc/pressure/memory} is not readable): it needs CONFIG_PSI and no psi=0 on its command line. Regrading cannot decide without it." >&2
      exit 1
    fi
    ;;
esac

regrade_main "$@"
