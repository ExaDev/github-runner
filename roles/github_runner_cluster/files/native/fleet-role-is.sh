#!/bin/sh
# The ExecCondition= of both native k3s units (templates/native/k3s.service.j2 and k3s-agent.service.j2): succeeds only when the role file names the role the unit runs, so the server and the agent unit can never run together, and Restart=always can never start a unit whose role the host no longer has (a demoted server, say). systemd skips the rest of a unit's start, without marking it failed, when its ExecCondition= exits 1 to 254 (systemd.service(5), ExecCondition=, systemd 243 and later), and Restart=always does not restart a unit whose start was skipped that way (service_shall_restart in src/core/service.c at v255, the systemd of Ubuntu 24.04: `case SERVICE_RESTART_ALWAYS: return s->result != SERVICE_SKIP_CONDITION;`).
#
# The server unit also does not start while k3s's datastore holds a tombstone. k3s writes SERVER_DIR/db/etcd/tombstone when etcd tells it this member was removed from the cluster (pkg/daemons/executor/etcd.go at v1.34.1+k3s1), and at its next start moves the datastore aside and joins again as a new learner (Register in pkg/etcd/etcd.go), so a server removed while the role file still names it a server (removed while it was down, or before anything switched the file) would become a member again behind the back of whatever removed it. Finishing the demotion moves server/db aside, tombstone and all.
#
# Usage: fleet-role-is.sh server ROLE_FILE SERVER_DIR, or fleet-role-is.sh agent ROLE_FILE. ROLE_FILE holds server or agent, written by the cluster role (tasks/native/role.yml) and by whatever changes the host's role. A missing or unreadable file names no role, so neither unit starts until something has decided one. SERVER_DIR is k3s's server directory (its data directory's server).
set -u

role=${1:?usage: fleet-role-is.sh server ROLE_FILE SERVER_DIR | fleet-role-is.sh agent ROLE_FILE}
file=${2:?usage: fleet-role-is.sh server ROLE_FILE SERVER_DIR | fleet-role-is.sh agent ROLE_FILE}

recorded=$(cat "$file" 2>/dev/null) || recorded=""
if [ "$recorded" != "$role" ]; then
  echo "fleet-role-is: ${file} names '${recorded:-no role}', not ${role}, so this unit does not start" >&2
  exit 1
fi
if [ "$role" = server ]; then
  server_dir=${3:?usage: fleet-role-is.sh server ROLE_FILE SERVER_DIR}
  if [ -e "${server_dir}/db/etcd/tombstone" ]; then
    echo "fleet-role-is: ${server_dir}/db/etcd/tombstone says this server's etcd member was removed from the cluster, and k3s started now would join it again as a new member, so this unit does not start" >&2
    exit 1
  fi
fi
exit 0
