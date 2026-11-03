#!/usr/bin/env bash
# The role controller's per-node probe (see roles/github_runner_arc/templates/fleet-role.yaml.j2): the `fleet-role-probe` DaemonSet runs this on every node that is an etcd voter or marked eligible and has a kubelet, with host networking, so it measures the path the node's own etcd and kubelet use. Every REPORT_SECONDS it takes one sample and appends it to this node's entry in the shared probe ConfigMap, which scripts/fleet-role.sh reads. An entry keeps the last window of samples, so it survives this pod being replaced.
#
# Only a node the controller judges is measured: a voter, or a host the owner marks eligible (ELIGIBLE_LABEL, with the value true). The DaemonSet schedules a pod on no other node, and a pod that finds its node is neither (a label removed while it runs) records nothing and removes the entry it has from when it was one, as does a pod that is stopped on a node that is neither any more or is gone, so the ConfigMap, which holds at most a mebibyte, grows with the hosts that decide the cluster's control plane and not with the fleet.
#
# A sample holds, per node: the TCP connect time to every voter's etcd peer port (a connect that does not complete within the election timeout is recorded as the election timeout, so loss shows as tail latency); on a voter, what etcd's own metrics gained since the last sample (the WAL fsync and backend commit histograms, the round trip to each peer, leader changes) and whether it has a leader, is a learner and is connected to its peers; on a node that is not a voter, an fdatasync probe in the k3s data directory once per window, with the write size etcd's own fio check uses; the memory pressure and MemAvailable the regrade core reads; and the medium of the node's link: the value of its LINK_LABEL Node label when it has one, which the cluster role reads on the host itself (the medium of the interface a Mac's Colima VM is bridged to, or of the interface a Linux host's default route leaves by), and otherwise the medium of the interface this pod's default route leaves by. The label decides when the node has it because a node in a container or a VM sees only its own virtual interface: a k3s container's veth, which reads unknown, or a VM's virtio NIC, which reads wired whatever the Mac beneath it uses.
#
# `fleet-role-probe.sh check link|connect <address>|memory|etcd|fsync` takes one of those readings alone, prints it and exits, with no cluster access (connect needs ELECTION_TIMEOUT_MS; the others need nothing but the settings below), so that each can be compared with a reading taken by hand on the host it runs on.
#
# Env:
# - NODE_NAME: the node this pod runs on (the downward API's spec.nodeName). Required.
# - NAMESPACE: the namespace of the ConfigMap. Required.
# - ELECTION_TIMEOUT_MS: the cluster's etcd election timeout, which is how long a peer connect may take before it counts as lost. Required.
# - ELIGIBLE_LABEL: the Node label, with the value true, that marks a host the controller may promote or demote. Required.
# - LINK_LABEL: the Node label the cluster role publishes the host's link medium in, wired, wifi or unknown. Required.
# - PROBE_CONFIGMAP: the shared ConfigMap, created by the role, that every probe patches its own key of (default fleet-role-probe).
# - REPORT_SECONDS: seconds between samples (default 15, the node recovery watcher's poll).
# - REPORT_ONCE: when "true", take one sample and exit, for tests.
# - VOTER_LABEL: the Node label that marks an etcd member (default node-role.kubernetes.io/etcd=true).
# - ETCD_METRICS_URL: where a voter's etcd serves its metrics (default http://127.0.0.1:2381/metrics, the listener k3s keeps).
# - ETCD_PEER_PORT: etcd's peer port (default 2380).
# - FSYNC_DIR: a directory on the filesystem k3s keeps its data on, which the fdatasync probe writes in (default /var/lib/rancher/k3s/fleet-role-probe).
# - NODE_LINK: the link reader the cluster role runs on the host too (roles/github_runner_cluster/files/node-link.sh), whose default_route_interface and link_type read this pod's own (default /app/node-link.sh). SYS_CLASS_NET and PROC_NET_ROUTE, which it reads, are where the host's /sys/class/net and /proc/net/route are (the defaults).
# - FLEET_REGRADE_CORE: the regrade core (roles/github_runner_cluster/files/regrade/fleet-regrade-core.sh), whose own psi_avg300 and meminfo_kb read the memory signals (default /app/fleet-regrade-core.sh). PSI_FILE and MEMINFO_FILE, which it reads, default here to /proc/pressure/memory and /proc/meminfo, which a pod's own procfs serves for the whole node as it does /proc/meminfo, so nothing is mounted for them. A kernel without pressure stall information (built or booted without it) has no /proc/pressure or, on older kernels, has the files and refuses to read them (EOPNOTSUPP); the sample then has no pressure reading and the memory figures from /proc/meminfo alone, which the controller judges by MemAvailable alone and reports with the pressure unknown.
set -euo pipefail

PROBE_CONFIGMAP="${PROBE_CONFIGMAP:-fleet-role-probe}"
REPORT_SECONDS="${REPORT_SECONDS:-15}"
REPORT_ONCE="${REPORT_ONCE:-false}"
VOTER_LABEL="${VOTER_LABEL:-node-role.kubernetes.io/etcd=true}"
ETCD_METRICS_URL="${ETCD_METRICS_URL:-http://127.0.0.1:2381/metrics}"
ETCD_PEER_PORT="${ETCD_PEER_PORT:-2380}"
FSYNC_DIR="${FSYNC_DIR:-/var/lib/rancher/k3s/fleet-role-probe}"
NODE_LINK="${NODE_LINK:-/app/node-link.sh}"
FLEET_REGRADE_CORE="${FLEET_REGRADE_CORE:-/app/fleet-regrade-core.sh}"
PSI_FILE="${PSI_FILE:-/proc/pressure/memory}"
MEMINFO_FILE="${MEMINFO_FILE:-/proc/meminfo}"
# The core reads its settings from an environment file when there is one; the probe has none, and must not pick up another by the core's default path.
# shellcheck disable=SC2034 # read by the sourced core
FLEET_REGRADE_ENV=/nonexistent
MODULES="$(dirname "$0")"

# The size of one write of etcd's own disk check (fio --bs=2300), the size of a typical WAL entry.
readonly etcd_fio_write_bytes=2300

# shellcheck source=/dev/null
. "$FLEET_REGRADE_CORE"
# A pod is a Linux process whatever the host beneath its node runs, so the reader reads Linux's files.
# shellcheck disable=SC2034 # read by the sourced reader
NODE_LINK_SYSTEM=Linux
# shellcheck source=/dev/null
. "$NODE_LINK"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; }

# The memory pressure the regrade core reads, or nothing on a kernel without pressure stall information: one built or booted without it has no PSI_FILE or, on older kernels, has it and refuses to read it (EOPNOTSUPP), so a read that fails gives the same answer as a file that is not there.
pressure_reading() {
  [ -e "$PSI_FILE" ] || return 0
  psi_avg300 2> /dev/null || true
}

# The tail percentile and the samples a window holds are defined once, in the module the controller reads them from too.
read -r tail_percentile window_samples < <(jq -nr -L "$MODULES" 'include "fleet-role-stats"; "\(tail_percentile) \(window_samples)"')
window_seconds=$((REPORT_SECONDS * window_samples))

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

# The nodes JSON's voters, as a JSON array of name and InternalIP.
voters_of() {
  jq -c --arg label "${VOTER_LABEL%%=*}" --arg value "${VOTER_LABEL#*=}" '[.items[] | select((.metadata.labels // {})[$label] == $value) | {name: .metadata.name, ip: ([.status.addresses[]? | select(.type == "InternalIP") | .address] | .[0])}]' <<< "$1"
}

# Whether this node is among the voters (a JSON array from voters_of).
is_voter_in() {
  jq --arg node "$NODE_NAME" 'any(.[]; .name == $node)' <<< "$1"
}

# Whether this node is marked eligible in the nodes JSON. A node the JSON does not list is not.
is_eligible_in() {
  jq --arg node "$NODE_NAME" --arg label "$ELIGIBLE_LABEL" 'any(.items[]; .metadata.name == $node and (.metadata.labels // {})[$label] == "true")' <<< "$1"
}

# The value of this node's LINK_LABEL in the nodes JSON, empty when the node has no such label.
labelled_link_in() {
  jq -r --arg node "$NODE_NAME" --arg label "$LINK_LABEL" '[.items[] | select(.metadata.name == $node) | (.metadata.labels // {})[$label] | strings] | first // ""' <<< "$1"
}

# Removes this node's entry from the ConfigMap. A key set to null in a merge patch is removed.
remove_entry() {
  local patch
  patch="$(mktemp)"
  jq -n --arg node "$NODE_NAME" '{data: {($node): null}}' > "$patch"
  kubectl patch configmap "$PROBE_CONFIGMAP" -n "$NAMESPACE" --type merge --patch-file "$patch" > /dev/null || { rm -f "$patch"; return 1; }
  rm -f "$patch"
}

# What the pod does when it is stopped: if its node is neither a voter nor eligible any more (the DaemonSet stops the pod of a node that has lost both labels) or is gone, its entry is removed, so the ConfigMap holds only the hosts that decide the control plane; a pod stopped for any other reason (a new image, the node shutting down) leaves the entry for the pod that follows it to extend. The API can fail here, and a pod that is going away has nothing to retry it with.
retire() {
  local nodes_json
  trap - TERM INT
  if nodes_json="$(kubectl get nodes -o json)" && [ "$(is_voter_in "$(voters_of "$nodes_json")")" != true ] && [ "$(is_eligible_in "$nodes_json")" != true ]; then
    remove_entry || log "could not remove this node's entry"
  fi
  exit 0
}

# Takes one sample and publishes it. $1 is "true" on the first pass of this process, which for a node that is not a voter is the only sign this probe has of k3s having restarted (a voter sees it in etcd's counters going backwards).
report() {
  local first=$1 now entry voters is_voter eligible tcp='{}' peer_name peer_ip ms etcd='null' restarted=false cumulative='null' etcd_id='null'
  local iface link link_source fsync='null' fsync_at patch metrics reading change psi=''
  now="$(date +%s)"
  # This node's entry is read from the ConfigMap once, when the process starts, and kept from then on: the probe is the only writer of its key, and two API calls a sample (the nodes, then the patch) are all a probe on a poor path can be asked to make in a period.
  if [ "$entry_loaded" != true ]; then
    entry="$(kubectl get configmap "$PROBE_CONFIGMAP" -n "$NAMESPACE" -o json | jq -c --arg node "$NODE_NAME" '(.data[$node] // "null") | fromjson')" || return 1
    entry_loaded=true
  else
    entry="$kept_entry"
  fi
  nodes_json="$(kubectl get nodes -o json)" || return 1
  voters="$(voters_of "$nodes_json")"
  is_voter="$(is_voter_in "$voters")"
  eligible="$(is_eligible_in "$nodes_json")"
  if [ "$is_voter" != true ] && [ "$eligible" != true ]; then
    if [ "$entry" != null ]; then
      remove_entry
      kept_entry=null
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
  link="$(labelled_link_in "$nodes_json")"
  link_source=label
  if [ -z "$link" ]; then
    link="$(link_type "$iface")"
    link_source=interface
  fi
  fsync_at="$(jq -c '.fsync_at' <<< "$entry")"
  if [ "$is_voter" != true ] && { [ "$fsync_at" = null ] || [ $((now - fsync_at)) -ge "$window_seconds" ]; }; then
    fsync="$(fsync_ms)"
    fsync_at="$now"
  fi

  psi="$(pressure_reading)"

  # The entry goes in on standard input, never as an argument: it holds a window of samples, and one argument is limited to a fraction of the argument space (128 KiB on Linux).
  new_entry="$(jq -c -L "$MODULES" --argjson now "$now" --argjson tcp "$tcp" --argjson etcd "$etcd" \
    --argjson restarted "$restarted" --argjson cumulative "$cumulative" --argjson etcd_id "$etcd_id" --argjson fsync "$fsync" --argjson fsync_at "$fsync_at" \
    --arg psi "$psi" --arg avail "$(meminfo_kb MemAvailable)" --arg total "$(meminfo_kb MemTotal)" --arg iface "$iface" --arg link "$link" --arg link_source "$link_source" '
    include "fleet-role-stats";
    def number: if . == "" then null else tonumber end;
    . as $entry
    | {t: $now, tcp: $tcp, etcd: $etcd, restarted: $restarted, psi: ($psi | number), avail_kb: ($avail | number), total_kb: ($total | number),
     link: $link, link_source: $link_source, iface: (if $iface == "" then null else $iface end), fsync_ms: $fsync} as $sample
    | {etcd_id: $etcd_id, last: $cumulative, fsync_at: $fsync_at, samples: ((($entry.samples // []) + [$sample]) | .[-window_samples:])}' <<< "$entry")" || return 1
  patch="$(mktemp)"
  jq --arg node "$NODE_NAME" '{data: {($node): tojson}}' <<< "$new_entry" > "$patch"
  kubectl patch configmap "$PROBE_CONFIGMAP" -n "$NAMESPACE" --type merge --patch-file "$patch" > /dev/null || { rm -f "$patch"; return 1; }
  rm -f "$patch"
  # Kept only once published, so a patch that failed is read again from the ConfigMap and not taken as written.
  kept_entry="$new_entry"
}

# One reading alone, for comparing with a reading taken by hand.
if [ "${1:-}" = check ]; then
  case "${2:-}" in
    link)
      iface="$(default_route_interface)"
      jq -nc --arg interface "$iface" --arg link "$(link_type "$iface")" '{interface: $interface, link: $link}'
      ;;
    connect) connect_ms "${3:?usage: check connect <address>}" ;;
    memory)
      # No pressure reading on a kernel without pressure stall information, as in a sample.
      psi="$(pressure_reading)"
      jq -nc --arg psi "$psi" --arg avail "$(meminfo_kb MemAvailable)" --arg total "$(meminfo_kb MemTotal)" '{psi_avg300: (if $psi == "" then null else $psi | tonumber end), mem_available_kb: ($avail | tonumber), mem_total_kb: ($total | tonumber)}'
      ;;
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
: "${LINK_LABEL:?LINK_LABEL is required}"

trap retire TERM INT

# This node's entry as the ConfigMap holds it, read once and then kept (see report).
entry_loaded=false
kept_entry=null
first=true
while true; do
  # A sample the API would not take is not recorded, and the next period tries again: a probe on a poor path is the case the controller exists for, and a process that exited on it would be started again as a restart. "First" stays until a sample is recorded, since it is the only sign a node that is not a voter has of its k3s having restarted.
  if report "$first"; then
    first=false
  else
    [ "$REPORT_ONCE" != true ] || exit 1
    log "could not record a sample; trying again in ${REPORT_SECONDS}s"
  fi
  [ "$REPORT_ONCE" = true ] && exit 0
  # In the background and waited for, so that a signal is handled at once and not when the sleep ends.
  sleep "$REPORT_SECONDS" &
  wait $! || true
done
