#!/usr/bin/env bash
# The role controller's per-node probe (see roles/github_runner_arc/templates/fleet-role.yaml.j2): the `fleet-role-probe` DaemonSet runs this on every node that has a kubelet, with host networking, so it measures the path the node's own etcd and kubelet use. Every REPORT_SECONDS it takes one sample and appends it to this node's entry in the shared probe ConfigMap, which scripts/fleet-role.sh reads. An entry keeps the last window of samples, so it survives this pod being replaced.
#
# Only a node the controller judges is measured: a voter, or a host the owner marks eligible (ELIGIBLE_LABEL, with the value true). Any other node has nothing recorded, and an entry it has from when it was one is removed, so the ConfigMap, which holds at most a mebibyte, grows with the hosts that decide the cluster's control plane and not with the fleet.
#
# A sample holds, per node: the TCP connect time to every voter's etcd peer port (a connect that does not complete within the election timeout is recorded as the election timeout, so loss shows as tail latency); on a voter, what etcd's own metrics gained since the last sample (the WAL fsync and backend commit histograms, the round trip to each peer, leader changes) and whether it has a leader, is a learner and is connected to its peers; on a node that is not a voter, an fdatasync probe in the k3s data directory once per window, with the write size etcd's own fio check uses; the memory pressure and MemAvailable the regrade core reads; and the link the default route leaves by.
#
# `fleet-role-probe.sh check link|connect <address>|memory|etcd|fsync` takes one of those readings alone, prints it and exits, with no cluster access (connect needs ELECTION_TIMEOUT_MS; the others need nothing but the settings below), so that each can be compared with a reading taken by hand on the host it runs on.
#
# Env:
# - NODE_NAME: the node this pod runs on (the downward API's spec.nodeName). Required.
# - NAMESPACE: the namespace of the ConfigMap. Required.
# - ELECTION_TIMEOUT_MS: the cluster's etcd election timeout, which is how long a peer connect may take before it counts as lost. Required.
# - ELIGIBLE_LABEL: the Node label, with the value true, that marks a host the controller may promote or demote. Required.
# - PROBE_CONFIGMAP: the shared ConfigMap, created by the role, that every probe patches its own key of (default fleet-role-probe).
# - REPORT_SECONDS: seconds between samples (default 15, the node recovery watcher's poll).
# - REPORT_ONCE: when "true", take one sample and exit, for tests.
# - VOTER_LABEL: the Node label that marks an etcd member (default node-role.kubernetes.io/etcd=true).
# - ETCD_METRICS_URL: where a voter's etcd serves its metrics (default http://127.0.0.1:2381/metrics, the listener k3s keeps).
# - ETCD_PEER_PORT: etcd's peer port (default 2380).
# - FSYNC_DIR: a directory on the filesystem k3s keeps its data on, which the fdatasync probe writes in (default /var/lib/rancher/k3s/fleet-role-probe).
# - SYS_CLASS_NET, PROC_NET_ROUTE: where the host's /sys/class/net and /proc/net/route are (the defaults).
# - FLEET_REGRADE_CORE: the regrade core (roles/github_runner_cluster/files/regrade/fleet-regrade-core.sh), whose own psi_avg300 and meminfo_kb read the memory signals (default /app/fleet-regrade-core.sh). PSI_FILE and MEMINFO_FILE, which it reads, default here to the host's /proc/pressure mounted at /host-pressure and the node's /proc/meminfo.
set -euo pipefail

PROBE_CONFIGMAP="${PROBE_CONFIGMAP:-fleet-role-probe}"
REPORT_SECONDS="${REPORT_SECONDS:-15}"
REPORT_ONCE="${REPORT_ONCE:-false}"
VOTER_LABEL="${VOTER_LABEL:-node-role.kubernetes.io/etcd=true}"
ETCD_METRICS_URL="${ETCD_METRICS_URL:-http://127.0.0.1:2381/metrics}"
ETCD_PEER_PORT="${ETCD_PEER_PORT:-2380}"
FSYNC_DIR="${FSYNC_DIR:-/var/lib/rancher/k3s/fleet-role-probe}"
SYS_CLASS_NET="${SYS_CLASS_NET:-/sys/class/net}"
PROC_NET_ROUTE="${PROC_NET_ROUTE:-/proc/net/route}"
FLEET_REGRADE_CORE="${FLEET_REGRADE_CORE:-/app/fleet-regrade-core.sh}"
PSI_FILE="${PSI_FILE:-/host-pressure/memory}"
MEMINFO_FILE="${MEMINFO_FILE:-/proc/meminfo}"
# The core reads its settings from an environment file when there is one; the probe has none, and must not pick up another by the core's default path.
# shellcheck disable=SC2034 # read by the sourced core
FLEET_REGRADE_ENV=/nonexistent
MODULES="$(dirname "$0")"

# The size of one write of etcd's own disk check (fio --bs=2300), the size of a typical WAL entry.
readonly etcd_fio_write_bytes=2300

# shellcheck source=/dev/null
. "$FLEET_REGRADE_CORE"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; }

# The tail percentile and the samples a window holds are defined once, in the module the controller reads them from too.
read -r tail_percentile window_samples < <(jq -nr -L "$MODULES" 'include "fleet-role-stats"; "\(tail_percentile) \(window_samples)"')
window_seconds=$((REPORT_SECONDS * window_samples))

# The interface the default route leaves by, the one with the lowest metric among the routes that are up and have a gateway; empty when there is none. /proc/net/route is hexadecimal, tab separated, with a header line.
default_route_interface() {
  local iface destination _gateway flags _refcnt _use metric mask best='' best_metric=''
  while read -r iface destination _gateway flags _refcnt _use metric mask _; do
    [ "$destination" = 00000000 ] && [ "$mask" = 00000000 ] || continue
    (( (0x$flags & 3) == 3 )) || continue
    if [ -z "$best" ] || [ "$metric" -lt "$best_metric" ]; then
      best=$iface
      best_metric=$metric
    fi
  done < "$PROC_NET_ROUTE"
  echo "$best"
}

# wired, wifi or unknown for an interface: wifi when the kernel gives it a wireless phy, wired when it is backed by a device, unknown when it is virtual (a veth in a container, a bridge, a tunnel) or absent, since then the link beneath it cannot be seen from here. Reads the interface's own directory and resolves it with cd -P, never a find over /sys, whose symlinks (device, subsystem, phy80211) loop back on themselves.
link_type() {
  local iface=$1 dir="${SYS_CLASS_NET}/$1" real
  if [ -z "$iface" ] || [ ! -d "$dir" ]; then
    echo unknown
    return
  fi
  if [ -e "${dir}/phy80211" ] || [ -d "${dir}/wireless" ] || grep -qx 'DEVTYPE=wlan' "${dir}/uevent" 2> /dev/null; then
    echo wifi
    return
  fi
  real="$(cd -P "$dir" && pwd -P)"
  case "$real" in
    */devices/virtual/*) echo unknown ;;
    *) echo wired ;;
  esac
}

# The time curl took to connect to a peer's etcd peer port, in milliseconds. The request itself is not the point (the port speaks TLS, and curl's HTTP is refused), only the connection: curl reports a connect time of zero when it never completed, and then the reading is the election timeout.
connect_ms() {
  local host=$1 seconds
  : "${ELECTION_TIMEOUT_MS:?ELECTION_TIMEOUT_MS is required}"
  [[ "$host" != *:* ]] || host="[${host}]"
  seconds="$(curl --silent --output /dev/null --connect-timeout "$(awk -v ms="$ELECTION_TIMEOUT_MS" 'BEGIN { printf "%.3f", ms / 1000 }')" \
    --write-out '%{time_connect}' "http://${host}:${ETCD_PEER_PORT}/" 2> /dev/null || true)"
  awk -v s="$seconds" -v timeout="$ELECTION_TIMEOUT_MS" 'BEGIN { if (s + 0 == 0) printf "%d\n", timeout; else printf "%.3f\n", s * 1000 }'
}

# The tail of fdatasync latency in this directory's filesystem, in milliseconds, from etcd's own fio check (--rw=write --ioengine=sync --fdatasync=1 --bs=2300) at the fewest writes that give the tail a sample of its own.
fsync_ms() {
  fio --rw=write --ioengine=sync --fdatasync=1 --directory="$FSYNC_DIR" --size=$((etcd_fio_write_bytes * window_samples)) --bs="$etcd_fio_write_bytes" \
    --name=fleet-role-probe --unlink=1 --output-format=json \
    | jq -r --arg percentile "${tail_percentile}.000000" '.jobs[0].sync.lat_ns.percentile[$percentile] / 1000000'
}

# Takes one sample and publishes it. $1 is "true" on the first pass of this process, which for a node that is not a voter is the only sign this probe has of k3s having restarted (a voter sees it in etcd's counters going backwards).
report() {
  local first=$1 now entry voters is_voter eligible tcp='{}' peer_name peer_ip ms etcd='null' restarted=false cumulative='null' etcd_id='null'
  local iface fsync='null' fsync_at patch metrics reading change psi=''
  now="$(date +%s)"
  entry="$(kubectl get configmap "$PROBE_CONFIGMAP" -n "$NAMESPACE" -o json | jq -c --arg node "$NODE_NAME" '(.data[$node] // "null") | fromjson')"
  voters="$(kubectl get nodes -l "$VOTER_LABEL" -o json | jq -c '[.items[] | {name: .metadata.name, ip: ([.status.addresses[]? | select(.type == "InternalIP") | .address] | .[0])}]')"
  is_voter="$(jq --arg node "$NODE_NAME" 'any(.[]; .name == $node)' <<< "$voters")"
  eligible="$(kubectl get node "$NODE_NAME" -o json | jq --arg label "$ELIGIBLE_LABEL" '(.metadata.labels // {})[$label] == "true"')"
  if [ "$is_voter" != true ] && [ "$eligible" != true ]; then
    # A key set to null in a merge patch is removed.
    if [ "$entry" != null ]; then
      patch="$(mktemp)"
      jq -n --arg node "$NODE_NAME" '{data: {($node): null}}' > "$patch"
      kubectl patch configmap "$PROBE_CONFIGMAP" -n "$NAMESPACE" --type merge --patch-file "$patch" > /dev/null
      rm -f "$patch"
    fi
    return 0
  fi

  while IFS=$'\t' read -r peer_name peer_ip; do
    ms="$(connect_ms "$peer_ip")"
    tcp="$(jq -c --arg peer "$peer_name" --argjson ms "$ms" '. + {($peer): $ms}' <<< "$tcp")"
  done < <(jq -r --arg node "$NODE_NAME" '.[] | select(.name != $node) | [.name, .ip] | @tsv' <<< "$voters")

  if [ "$is_voter" = true ]; then
    if metrics="$(curl --silent --show-error --fail --max-time "$REPORT_SECONDS" "$ETCD_METRICS_URL")"; then
      reading="$(jq -Rs -L "$MODULES" 'include "fleet-role-stats"; etcd_reading' <<< "$metrics")"
      change="$(jq -c -L "$MODULES" --argjson previous "$(jq -c '.last' <<< "$entry")" --argjson now "$reading" 'include "fleet-role-stats"; etcd_sample($previous; $now)' <<< null)"
      etcd="$(jq -c '.sample' <<< "$change")"
      restarted="$(jq -c '.restarted' <<< "$change")"
      cumulative="$(jq -c '.cumulative' <<< "$change")"
      etcd_id="$(jq -c '.id' <<< "$reading")"
    else
      log "could not read ${ETCD_METRICS_URL}; this sample has no etcd reading"
      cumulative="$(jq -c '.last' <<< "$entry")"
      etcd_id="$(jq -c '.etcd_id' <<< "$entry")"
    fi
  else
    restarted="$first"
  fi

  iface="$(default_route_interface)"
  fsync_at="$(jq -c '.fsync_at' <<< "$entry")"
  if [ "$is_voter" != true ] && { [ "$fsync_at" = null ] || [ $((now - fsync_at)) -ge "$window_seconds" ]; }; then
    fsync="$(fsync_ms)"
    fsync_at="$now"
  fi

  # A kernel without pressure stall information has no such file, and then the sample has no pressure reading.
  if [ -r "$PSI_FILE" ]; then
    psi="$(psi_avg300)"
  fi

  patch="$(mktemp)"
  # The entry goes in on standard input, never as an argument: it holds a window of samples, and one argument is limited to a fraction of the argument space (128 KiB on Linux).
  jq -L "$MODULES" --arg node "$NODE_NAME" --argjson now "$now" --argjson tcp "$tcp" --argjson etcd "$etcd" \
    --argjson restarted "$restarted" --argjson cumulative "$cumulative" --argjson etcd_id "$etcd_id" --argjson fsync "$fsync" --argjson fsync_at "$fsync_at" \
    --arg psi "$psi" --arg avail "$(meminfo_kb MemAvailable)" --arg total "$(meminfo_kb MemTotal)" --arg iface "$iface" --arg link "$(link_type "$iface")" '
    include "fleet-role-stats";
    def number: if . == "" then null else tonumber end;
    . as $entry
    | {t: $now, tcp: $tcp, etcd: $etcd, restarted: $restarted, psi: ($psi | number), avail_kb: ($avail | number), total_kb: ($total | number),
     link: $link, iface: (if $iface == "" then null else $iface end), fsync_ms: $fsync} as $sample
    | {data: {($node): ({etcd_id: $etcd_id, last: $cumulative, fsync_at: $fsync_at, samples: ((($entry.samples // []) + [$sample]) | .[-window_samples:])} | tojson)}}' <<< "$entry" > "$patch"
  kubectl patch configmap "$PROBE_CONFIGMAP" -n "$NAMESPACE" --type merge --patch-file "$patch" > /dev/null
  rm -f "$patch"
}

# One reading alone, for comparing with a reading taken by hand.
if [ "${1:-}" = check ]; then
  case "${2:-}" in
    link)
      iface="$(default_route_interface)"
      jq -nc --arg interface "$iface" --arg link "$(link_type "$iface")" '{interface: $interface, link: $link}'
      ;;
    connect) connect_ms "${3:?usage: check connect <address>}" ;;
    memory) jq -nc --arg psi "$(psi_avg300)" --arg avail "$(meminfo_kb MemAvailable)" --arg total "$(meminfo_kb MemTotal)" '{psi_avg300: ($psi | tonumber), mem_available_kb: ($avail | tonumber), mem_total_kb: ($total | tonumber)}' ;;
    etcd) curl --silent --show-error --fail --max-time "$REPORT_SECONDS" "$ETCD_METRICS_URL" | jq -Rsc -L "$MODULES" 'include "fleet-role-stats"; etcd_reading | {id, has_leader, is_learner, known_members, active_peers, quota_bytes, leader_changes, wal_fsync_p99_ms: (.wal | per_bucket | histogram_quantile(tail_percentile) * 1000), backend_commit_p99_ms: (.commit | per_bucket | histogram_quantile(tail_percentile) * 1000)}' ;;
    fsync) fsync_ms ;;
    *) echo "usage: $0 check link|connect <address>|memory|etcd|fsync" >&2; exit 2 ;;
  esac
  exit 0
fi

: "${NODE_NAME:?NODE_NAME is required}"
: "${NAMESPACE:?NAMESPACE is required}"
: "${ELECTION_TIMEOUT_MS:?ELECTION_TIMEOUT_MS is required}"
: "${ELIGIBLE_LABEL:?ELIGIBLE_LABEL is required}"

first=true
while true; do
  report "$first"
  first=false
  [ "$REPORT_ONCE" = true ] && exit 0
  sleep "$REPORT_SECONDS"
done
