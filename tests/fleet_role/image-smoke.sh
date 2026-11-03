#!/usr/bin/env bash
# Runs what the role controller's pods run, from the image as built (fleet-role/Dockerfile), with nothing that reaches a cluster: the tools and files the pods need are present and executable, the jq modules compile under the image's own jq (not the host's), the probe's check mode reads the memory signals through the regrade core the image carries and the link through the cluster role's link reader, and one pass of the controller runs end to end against a kubectl that answers an empty cluster and records the status it is asked to publish. Needs only Docker.
#
# Usage: tests/fleet_role/image-smoke.sh <image>   (the image must already be built)
set -euo pipefail

image="${1:?usage: image-smoke.sh <image>}"
work="$(mktemp -d "${TMPDIR:-/tmp}/fleet-role-image.XXXXXX")"
trap 'rm -rf "$work"' EXIT
# A directory every container user can read, since the files are mounted read-only into a container that runs as a non-root user.
chmod 755 "$work"

docker run --rm --entrypoint bash "$image" -c '
  set -euo pipefail
  for tool in jq kubectl curl fio; do command -v "$tool" > /dev/null; done
  test -x /app/loop.sh -a -x /app/scripts/fleet-role.sh -a -x /app/scripts/fleet-role-probe.sh
  test -f /app/fleet-regrade-core.sh -a -f /app/node-link.sh -a -f /app/scripts/fleet-role.jq -a -f /app/scripts/fleet-role-stats.jq'

docker run --rm --entrypoint jq "$image" -en -L /app/scripts 'include "fleet-role-stats"; {window: window_samples, tail: tail_percentile} | select(.window == 100 and .tail == 99)' > /dev/null

printf 'MemTotal: 1000 kB\nMemAvailable: 400 kB\n' > "$work/meminfo"
printf 'some avg10=0.00 avg60=0.00 avg300=1.50 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n' > "$work/pressure"
chmod 644 "$work/meminfo" "$work/pressure"
docker run --rm -v "$work/meminfo:/meminfo:ro" -v "$work/pressure:/pressure:ro" -e MEMINFO_FILE=/meminfo -e PSI_FILE=/pressure \
  --entrypoint /app/scripts/fleet-role-probe.sh "$image" check memory | jq -e '.psi_avg300 == 1.5 and .mem_available_kb == 400' > /dev/null

# With no file named, the probe reads the pod's own /proc/pressure/memory, which is the node's, so the DaemonSet mounts nothing for it: the container sees pressure exactly when this host's kernel has it (a mounted /proc/pressure would stop a pod on a kernel without it from starting) and the reading is then absent, never an error.
host_has_psi=false
[ -r /proc/pressure/memory ] && host_has_psi=true
docker run --rm --entrypoint /app/scripts/fleet-role-probe.sh "$image" check memory \
  | jq -e --argjson psi "$host_has_psi" '(.psi_avg300 != null) == $psi and .mem_total_kb > 0' > /dev/null

# The link reader the image carries reads the container's own interface: a veth, whose link it cannot see.
docker run --rm --entrypoint /app/scripts/fleet-role-probe.sh "$image" check link | jq -e '.link == "unknown" and (.interface | length > 0)' > /dev/null

# A kubectl that answers an empty cluster and keeps the patch it is given, in place of the image's own on the PATH.
mkdir "$work/out"
chmod 777 "$work/out"
cat > "$work/kubectl" << 'STUB'
#!/bin/sh
case "$1 $2" in
  "get nodes") echo '{"items":[]}' ;;
  "get configmap") case "$3" in fleet-role-probe) echo '{"data":{}}' ;; *) echo '{"data":{"status.json":""}}' ;; esac ;;
  "patch configmap")
    previous=''
    for argument in "$@"; do
      [ "$previous" = --patch-file ] && cp "$argument" /out/patch.json
      previous=$argument
    done ;;
  *) echo "unexpected kubectl call: $*" >&2; exit 2 ;;
esac
STUB
chmod 755 "$work/kubectl"
output="$(docker run --rm -v "$work/kubectl:/usr/local/bin/kubectl:ro" -v "$work/out:/out" \
  -e FLEET_ROLE_DRY_RUN=true -e FLEET_ROLE_VOTERS=3 -e FLEET_ROLE_MAX_SERVERS=5 -e FLEET_ROLE_ELECTION_TIMEOUT_MS=1000 -e FLEET_ROLE_SAMPLE_SECONDS=15 \
  -e FLEET_ROLE_PRESSURE_THRESHOLD=10 -e FLEET_ROLE_AVAILABLE_FLOOR_PERCENT=15 -e FLEET_ROLE_ELIGIBLE_LABEL=example.com/eligible \
  -e FLEET_ROLE_PINNED_LABEL=example.com/pinned -e FLEET_ROLE_LEVEL_LABEL=example.com/level \
  --entrypoint /app/scripts/fleet-role.sh "$image")"
grep -q 'no host is marked eligible' <<< "$output"
jq -e '.data["status.json"] | fromjson | .dry_run == true and .decision.action == "none"' "$work/out/patch.json" > /dev/null
echo "image smoke test passed: $image"
