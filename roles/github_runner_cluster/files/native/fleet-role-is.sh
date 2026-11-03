#!/bin/sh
# The ExecCondition= of both native k3s units (templates/native/k3s.service.j2 and k3s-agent.service.j2): succeeds only when the role file names the role the unit runs, so the server and the agent unit can never run together, and Restart=always can never start a unit whose role the host no longer has (a demoted server, say). systemd skips the rest of a unit's start, without marking it failed, when its ExecCondition= exits 1 to 254 (systemd.service(5), ExecCondition=, systemd 243 and later), and Restart=always does not restart a unit whose start was skipped that way (service_shall_restart in src/core/service.c at v255, the systemd of Ubuntu 24.04: `case SERVICE_RESTART_ALWAYS: return s->result != SERVICE_SKIP_CONDITION;`).
#
# Usage: fleet-role-is.sh ROLE ROLE_FILE. ROLE is server or agent; ROLE_FILE holds one of them, written by the cluster role (tasks/native/role.yml) and, later, by whatever changes the host's role. A missing or unreadable file names no role, so neither unit starts until something has decided one.
set -u

role=${1:?usage: fleet-role-is.sh ROLE ROLE_FILE}
file=${2:?usage: fleet-role-is.sh ROLE ROLE_FILE}

recorded=$(cat "$file" 2>/dev/null) || recorded=""
if [ "$recorded" = "$role" ]; then
  exit 0
fi
echo "fleet-role-is: ${file} names '${recorded:-no role}', not ${role}, so this unit does not start" >&2
exit 1
