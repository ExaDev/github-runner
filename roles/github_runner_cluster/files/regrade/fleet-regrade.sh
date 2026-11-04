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

# A Docker host's role directory, mounted from the Compose project by docker-compose.fleet-role.yml, records the role the host runs (files/k3s/node-entrypoint.sh reads the same file). On a host it names another role than the server, a voter swap has stepped this server down or demoted it, and a watcher that ran would step it back up: the service idles instead, as the entrypoint does, since restart: unless-stopped would start one that exited again. A native server's unit has the same guard as its ExecCondition (templates/native/fleet-regrade.service.j2). A host with no role file runs as before.
FLEET_ROLE_FILE=${FLEET_ROLE_FILE:-/etc/github-runner/role}
if [ "$FLEET_REGRADE_ACTUATOR" = docker ] && [ -e "$FLEET_ROLE_FILE" ] && [ "$(cat "$FLEET_ROLE_FILE")" != server ]; then
  message="$FLEET_ROLE_FILE names '$(cat "$FLEET_ROLE_FILE")', not server, so there is no server here to regrade"
  case ${1:-} in
    watch)
      log "${message}; idling until the service is stopped"
      trap 'exit 0' TERM INT
      while :; do
        sleep infinity &
        wait $!
      done
      ;;
    *) die "$message" ;;
  esac
fi

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
