#!/usr/bin/env bash
# Runs fleet-regrade: the platform-neutral core, the part both k3s actuators share, then the actuator FLEET_REGRADE_ACTUATOR names in the settings (docker, for the regrade service of the cluster role's Compose file, or systemd, for the fleet-regrade unit of a native server), all from the directory this script is in, so a collection upgrade reaches the service when it restarts, without the Compose service's image being built again. The settings are in fleet-regrade.env in the same directory, which the role templates.
set -euo pipefail

lib=$(dirname "$(readlink -f "$0")")
FLEET_REGRADE_ENV=${FLEET_REGRADE_ENV:-$lib/fleet-regrade.env}

# shellcheck source=/dev/null
. "$lib/fleet-regrade-core.sh"

case ${FLEET_REGRADE_ACTUATOR:-} in
  docker | systemd) ;;
  *) die "FLEET_REGRADE_ACTUATOR is '${FLEET_REGRADE_ACTUATOR:-}' in $FLEET_REGRADE_ENV; it must be docker or systemd." ;;
esac
# shellcheck source=/dev/null
. "$lib/fleet-regrade-k3s.sh"
# shellcheck source=/dev/null
. "$lib/fleet-regrade-$FLEET_REGRADE_ACTUATOR.sh"

# Memory pressure is what the core decides on. A kernel without it (built without CONFIG_PSI, or booted with psi=0) would fail on every sample, so the service stops at once and says why. The role refuses to turn regrading on where it can read that the kernel has none.
case ${1:-} in
  watch | status)
    if [ ! -r "$PSI_FILE" ]; then
      echo "The kernel this server runs on exposes no memory pressure ($PSI_FILE is not readable): it needs CONFIG_PSI and no psi=0 on its command line. Regrading cannot decide without it." >&2
      exit 1
    fi
    ;;
esac

regrade_main "$@"
