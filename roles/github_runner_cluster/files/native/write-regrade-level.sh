#!/bin/sh
# Puts a native server at the regrading level last requested for it, from the server unit's ExecStartPre (templates/native/k3s.service.j2), before every start: what the k3s container's entrypoint does for a Docker server (set_regrade_flags in files/k3s/node-entrypoint.sh). The level is the one the systemd actuator of fleet-regrade (files/regrade/fleet-regrade-systemd.sh) wrote to LEVEL_FILE; a server that has never been stepped down has none and starts at full.
#
# Only etcd-only changes how k3s starts: it turns off the API server, controller manager, scheduler and agent, so the node keeps its etcd member, and its vote, and little else. full and control-plane run the same k3s and differ in the cordon and taint the regrade service puts on the Node. The level reaches k3s as the server configuration's drop-in DROPIN_FILE, which k3s reads after server.yaml and the drop-ins named before it (dotDFiles and readConfigFile in pkg/configfilearg/parser.go of k3s), and which holds those four keys at etcd-only and none at any other level. The level k3s is about to start at is then recorded in STARTED_FILE, which the actuator reads as the level the node is at, since k3s hides its arguments from /proc. A level that is none of the three starts k3s whole and is recorded as it was read, which the actuator reports as unknown, so it never steps a node from a level it cannot name.
#
# Usage: write-regrade-level.sh LEVEL_FILE DROPIN_FILE STARTED_FILE. Each file is replaced in one rename, so neither k3s nor the actuator ever reads half of one.
set -eu

level_file=${1:?usage: write-regrade-level.sh LEVEL_FILE DROPIN_FILE STARTED_FILE}
dropin=${2:?usage: write-regrade-level.sh LEVEL_FILE DROPIN_FILE STARTED_FILE}
started=${3:?usage: write-regrade-level.sh LEVEL_FILE DROPIN_FILE STARTED_FILE}

level=full
if [ -r "$level_file" ]; then
  level=$(head -n 1 "$level_file")
fi

umask 077
mkdir -p "$(dirname "$dropin")" "$(dirname "$started")"
{
  printf '# Written by write-regrade-level.sh before every start of the server unit, from %s: the regrading level %s.\n' "$level_file" "$level"
  if [ "$level" = etcd-only ]; then
    printf 'disable-apiserver: true\ndisable-controller-manager: true\ndisable-scheduler: true\ndisable-agent: true\n'
  fi
} > "${dropin}.tmp"
mv "${dropin}.tmp" "$dropin"
printf '%s\n' "$level" > "${started}.tmp"
mv "${started}.tmp" "$started"
