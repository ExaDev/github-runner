# The role controller's decision (see scripts/fleet-role.sh, which gathers the inputs and publishes the result): which hosts are fit to hold an etcd vote, and the swap the controller would make. Pure: nothing here reads the cluster or the clock.
#
# Inputs, as jq named arguments:
# - $config: the controller's settings (see scripts/fleet-role.sh for each), with `now` the time of the pass in epoch seconds, `expected_voters` the number of voters the cluster should hold at this moment (the target, or one more while a swap holds a new voter beside the old) and `ignored_voters`, optional, the voters a swap in progress is taking out, which the gates do not hold to their health (a voter stepped down to etcd-only, or one that is down), and whose connections the others may lack.
# - $nodes_file: the output of `kubectl get nodes -o json`, as --slurpfile gives it (a one-element array).
# - $probes_file: each node's probe entry (scripts/fleet-role-probe.sh), by node name, likewise.
# - $previous_file: the status this program published on the last pass, or null on the first, likewise.
#
# Every threshold is derived, and every constant below is a fact about etcd or k3s with its source. The figures are read over a window of tail_window samples per peer pair; a host with fewer is "unknown", never fit or unfit.

include "fleet-role-stats";

# etcd's tuning guide: the election timeout must be at least ten times the round trip between members, so a voter whose tail round trip passes a tenth of it is unfit.
def unfit_rtt_divisor: 10;

# etcd's tuning guide: the heartbeat interval is best at about 0.5 to 1.5 times the round trip. A candidate is fit only at the 0.5 end, where the heartbeat is twice the round trip, a twentieth of the election timeout since the election timeout is ten heartbeats. The gap between this and the unfit limit is the hysteresis, so a host does not alternate between the two on one noisy window.
def fit_rtt_divisor: 20;

# etcd's FAQ: a disk is fast enough when the WAL fsync tail is under 10 ms and the backend commit tail under 25 ms.
def wal_fsync_limit_ms: 10;
def backend_commit_limit_ms: 25;

# A probe report older than this many samples counts as no report, as the autoscaler treats a node pressure report older than three of its polls.
def stale_after_samples: 3;

# Linux's byte count of one kibibyte, MemAvailable being reported in kB that are kibibytes.
def kib: 1024;

# The Node condition k3s sets True on a member of etcd that votes (setEtcdStatusCondition in pkg/etcd/etcd.go). k3s labels a server as an etcd node when it joins, as a learner that does not vote until the leader promotes it, and does not remove the label when a member is removed, so the label alone is no evidence of a vote.
def voter_condition: "EtcdIsVoter";

def link_rank: {"wired": 0, "wifi": 1, "unknown": 2};

def round2: if . == null then null else (. * 100 | round) / 100 end;

def ready_condition: ((.status.conditions // []) | map(select(.type == "Ready")) | .[0]);

def condition_true($type): ((.status.conditions // []) | map(select(.type == $type)) | .[0].status) == "True";

# The pair figure of one observer towards one peer over a window: the worse of the TCP connect tail and etcd's own round trip tail (only a voter has the latter, keyed by the peer's etcd id), in milliseconds, with how many TCP readings it rests on.
def pair($window; $peer; $peer_id):
  ([$window[] | .tcp[$peer] | select(. != null)]) as $tcp
  | (if $peer_id == null then null else ([$window[] | .etcd.rtt[$peer_id] | select(. != null)] | sum_buckets | histogram_quantile(tail_percentile)) end) as $etcd
  | ($tcp | nearest_rank(tail_percentile)) as $tcp_tail
  | {peer: $peer, readings: ($tcp | length),
     tcp_ms: $tcp_tail,
     etcd_ms: (if $etcd == null then null else $etcd * 1000 end),
     ms: ([$tcp_tail, (if $etcd == null then null else $etcd * 1000 end)] | map(select(. != null)) | max)};

$config as $c
| $nodes_file[0] as $nodes
| $probes_file[0] as $probes
| $previous_file[0] as $previous
| $c.now as $now
| ($c.sample_seconds * window_samples) as $window_seconds
| ($c.election_timeout_ms / unfit_rtt_divisor) as $unfit_rtt_ms
| ($c.election_timeout_ms / fit_rtt_divisor) as $fit_rtt_ms
| (learner_stall_seconds + $window_seconds) as $cooldown_seconds
| ($c.sample_seconds * stale_after_samples) as $stale_seconds

# The nodes, with what the decision reads off each.
| [$nodes.items[]
   | (.metadata.labels // {}) as $labels
   | ($labels[$c.voter_label] == "true") as $labelled
   | ready_condition as $ready
   | {name: .metadata.name,
      ready: ($ready.status == "True"),
      ready_since: ($ready.lastTransitionTime | fromdateiso8601? // null),
      labelled: $labelled,
      voter: ($labelled and condition_true(voter_condition) and ((.metadata.annotations // {})[removed_annotation] // "") == ""),
      eligible: ($labels[$c.eligible_label] == "true"),
      pinned: ($labels[$c.pinned_label] == "true"),
      removed: (((.metadata.annotations // {})[removed_annotation] // "") != ""),
      regrade_level: $labels[$c.level_label]}] as $all
| ($all | map(select(.voter)) | sort_by(.name)) as $voters
| ($voters | map(.name)) as $voter_names

# The membership, and when it last changed: a voter set that differs from the last pass's. The first pass has nothing to compare with and treats the membership as settled, so its first window is not held back by a change it did not see.
| (if $previous == null then null
   elif $previous.membership.voters != $voter_names then $now
   else $previous.membership.changed_at end) as $changed_at
| (if $changed_at == null then null else $changed_at + $cooldown_seconds end) as $cooldown_until

# The storage quota a voter's etcd reports, the largest of them, or etcd's own default when no voter reports one.
| ([$voters[] | $probes[.name].samples[-1].etcd.quota_bytes | select(. != null)] | max // $c.default_quota_bytes) as $quota_bytes

| def window($name): ($probes[$name].samples // []) | map(select(.t >= ($changed_at // 0))) | .[-window_samples:];

  def measure($node):
    $node.name as $name
    | $probes[$name] as $entry
    | window($name) as $w
    | ($entry.samples[-1].t // null) as $last_at
    | (if $node.voter then ($voter_names | map(select(. != $name))) else $voter_names end) as $peers
    | [$peers[] as $peer | pair($w; $peer; $probes[$peer].etcd_id)] as $pairs
    | ($pairs | map(.ms) | map(select(. != null)) | max) as $rtt_ms
    | ($pairs | max_by(.ms // 0) | .peer) as $rtt_peer
    | ($w | map(.etcd) | map(select(. != null))) as $etcd
    | ([$etcd[].wal] | sum_buckets | histogram_quantile(tail_percentile)) as $wal
    | ([$etcd[].commit] | sum_buckets | histogram_quantile(tail_percentile)) as $commit
    | ([$w[] | .fsync_ms | select(. != null)] | max) as $fsync
    | ($w | map(select(.psi != null and over_regrade_limit($c))) | length) as $over
    | ($w | map(.psi | select(. != null))) as $pressures
    | ($w | map(.avail_kb | select(. != null)) | min) as $min_avail_kb
    | ($w | map(select(.restarted == true)) | length) as $restarts
    | ($node.ready | not) as $not_ready
    | ($node.ready_since != null and $node.ready_since >= ($now - $window_seconds)) as $spell
    | {
        node: $node,
        reported_at: $last_at,
        fresh: ($last_at != null and $last_at >= ($now - $stale_seconds)),
        samples: ($w | length),
        link: ($w[-1].link // "unknown"),
        link_source: ($w[-1].link_source // null),
        interface: ($w[-1].iface // null),
        pairs: $pairs,
        rtt_ms: $rtt_ms,
        rtt_peer: $rtt_peer,
        pairs_complete: ($pairs | all(.readings >= window_samples)),
        wal_p99_ms: (if $wal == null then null else $wal * 1000 end),
        commit_p99_ms: (if $commit == null then null else $commit * 1000 end),
        fsync_p99_ms: $fsync,
        over_limit_samples: $over,
        pressure_max: ($pressures | max),
        pressure_samples: ($pressures | length),
        min_available_bytes: (if $min_avail_kb == null then null else $min_avail_kb * kib end),
        restarts: $restarts,
        not_ready: $not_ready,
        not_ready_spell: $spell,
        leader_changes: ($etcd | map(.leader_changes) | add // 0),
        etcd: ($entry.samples[-1].etcd // null)
      };

  # What makes a measured host unfit, as sentences carrying their numbers; empty when nothing does. A voter is judged by the unfit round trip limit and the disk it serves etcd from, a candidate by the stricter fit limit and a disk probe. The round trip is a link's, and another node may be at fault for it; every other fault is the host's own.
  def rtt_faults($m):
    $m.node as $node
    | [
        (if $m.rtt_ms != null and $m.rtt_ms > (if $node.voter then $unfit_rtt_ms else $fit_rtt_ms end)
         then "round trip p99 to \($m.rtt_peer) is \($m.rtt_ms | round2) ms, over \(if $node.voter then $unfit_rtt_ms else $fit_rtt_ms end) ms (a \(if $node.voter then "tenth" else "twentieth" end) of the \($c.election_timeout_ms) ms election timeout)"
         else empty end)
      ];

  def own_faults($m):
    $m.node as $node
    | [
        (if $node.voter then
           (if $m.wal_p99_ms != null and $m.wal_p99_ms >= wal_fsync_limit_ms then "WAL fsync p99 is \($m.wal_p99_ms | round2) ms, not under \(wal_fsync_limit_ms) ms" else empty end),
           (if $m.commit_p99_ms != null and $m.commit_p99_ms >= backend_commit_limit_ms then "backend commit p99 is \($m.commit_p99_ms | round2) ms, not under \(backend_commit_limit_ms) ms" else empty end)
         else
           (if $m.fsync_p99_ms != null and $m.fsync_p99_ms >= wal_fsync_limit_ms then "fdatasync p99 is \($m.fsync_p99_ms | round2) ms, not under \(wal_fsync_limit_ms) ms" else empty end)
         end),
        (if $m.over_limit_samples > 0 then "memory was over the regrade limit in \($m.over_limit_samples) of \($m.samples) samples" else empty end),
        (if $m.min_available_bytes != null and $m.min_available_bytes < $quota_bytes then "MemAvailable fell to \(($m.min_available_bytes / 1048576) | round) MiB, under etcd's \(($quota_bytes / 1048576) | round) MiB storage quota" else empty end),
        (if $m.not_ready then "the node is not Ready" elif $m.not_ready_spell then "the node was not Ready within the last \($window_seconds) s" else empty end),
        (if $m.restarts > 0 then "k3s restarted \($m.restarts) time(s) in the window" else empty end)
      ];

  def faults($m): rtt_faults($m) + own_faults($m);

  # The signals no sample of the window could read but that do not withhold a verdict, by name: memory pressure, on a kernel without pressure stall information, where the regrade limit (which needs both pressure and a low MemAvailable) cannot be reached and MemAvailable is judged against the storage quota alone.
  def unknown_signals($m): if $m.samples > 0 and $m.pressure_samples == 0 then ["memory pressure"] else [] end;

  # What is missing for a verdict, as sentences; empty when the readings are all there.
  def gaps($m):
    $m.node as $node
    | [
        (if $m.reported_at == null then "no probe report" elif ($m.fresh | not) then "the probe's last report is \($now - $m.reported_at) s old" else empty end),
        (if $m.samples < window_samples then "\($m.samples) of \(window_samples) samples since the last membership change" else empty end),
        (if ($m.pairs | length) == 0 then "no voter to measure against"
         elif ($m.pairs_complete | not) then "fewer than \(window_samples) round trip readings to some voter" else empty end),
        (if $node.voter and ($m.wal_p99_ms == null or $m.commit_p99_ms == null) then "no etcd disk readings" else empty end),
        (if ($node.voter | not) and $m.fsync_p99_ms == null then "no fdatasync reading yet" else empty end)
      ];

  def disk_ms($m): $m.node.voter as $voter | if $voter then $m.wal_p99_ms else $m.fsync_p99_ms end;

  def rank($m): [(link_rank[$m.link] // link_rank.unknown), ($m.rtt_ms // infinite), (disk_ms($m) // infinite), $m.node.name];

  # A measurement as if $peer were not a voter: a candidate is judged against the voters that would remain, since a slow link to the very voter it would replace says nothing against it.
  def without($peer):
    .pairs |= map(select(.peer != $peer))
    | (.pairs | map(.ms) | map(select(. != null)) | max) as $rtt
    | .rtt_ms = $rtt
    | .rtt_peer = (.pairs | max_by(.ms // 0) | .peer)
    | .pairs_complete = (.pairs | all(.readings >= window_samples));

  def host($node):
    measure($node) as $m
    | gaps($m) as $gaps
    | faults($m) as $faults
    | {name: $node.name,
       role: (if $node.voter then "voter" else "standby" end),
       eligible: $node.eligible,
       pinned: $node.pinned,
       verdict: (if ($gaps | length) > 0 then "unknown" elif ($faults | length) > 0 then "unfit" else "fit" end),
       reasons: (if ($gaps | length) > 0 then $gaps else $faults end),
       link: $m.link,
       link_source: $m.link_source,
       samples: $m.samples,
       unknown_signals: unknown_signals($m),
       figures: {rtt_p99_ms: ($m.rtt_ms | round2), rtt_peer: $m.rtt_peer,
                 wal_fsync_p99_ms: ($m.wal_p99_ms | round2), backend_commit_p99_ms: ($m.commit_p99_ms | round2), fdatasync_p99_ms: ($m.fsync_p99_ms | round2),
                 psi_avg300_max: ($m.pressure_max | round2),
                 min_available_mib: (if $m.min_available_bytes == null then null else ($m.min_available_bytes / 1048576 | round) end),
                 leader_changes: $m.leader_changes, restarts: $m.restarts},
       rank: rank($m),
       m: $m};

  # Hosts reported: every voter, and every eligible host that is not one.
  ([$all[] | select(.voter or .eligible)] | sort_by(.name) | map(host(.))) as $measured

# Every link a probe measured, between two hosts, as the worse of the two ends' tails (a standby measures its links to the voters alone, so those have one end's).
| ([$measured[] | .name as $a | .m.pairs[] | select(.ms != null) | {ends: ([$a, .peer] | sort), ms}]
   | group_by(.ends) | map({ends: .[0].ends, ms: (map(.ms) | max)})) as $links

# The best of a host's links to the voters other than $excluded, in milliseconds: how fast its path is where the link in question plays no part. Links to the voters alone, since every host measures those (a voter's to a standby is the standby's one-sided reading). Null when it has no other.
| def elsewhere($host; $excluded):
    [$links[] | select((.ends | index($host)) != null and (.ends | index($excluded)) == null)
     | select(.ends | map(select(. != $host)) | .[0] as $other | $voter_names | index($other) != null) | .ms] | min;

  # The end at fault for a slow link between $host and $peer: the one whose other links are slower too, since a node on a poor path is slow to every peer and a node on a good one is fast to its others. Both ends when their other links are as fast as each other's, or one has none to compare.
  def at_fault($host; $peer):
    elsewhere($host; $peer) as $own
    | elsewhere($peer; $host) as $theirs
    | if $own == null or $theirs == null or $own == $theirs then [$host, $peer] | sort
      elif $theirs > $own then [$peer]
      else [$host] end;

  # The nodes an unfit verdict is attributed to: for its round trip, the end at fault for the slow link to its worst peer; for any other fault (its own disk, memory, readiness or restarts), the host itself. Empty for a verdict that is not unfit.
  def attributed($h):
    if $h.verdict != "unfit" then []
    else (if (rtt_faults($h.m) | length) > 0 then at_fault($h.name; $h.m.rtt_peer) else [] end) + (if (own_faults($h.m) | length) > 0 then [$h.name] else [] end) | unique end;

  ($measured | map(. + {attributed_to: attributed(.)})) as $hosts
| ([$hosts[] | select(.eligible)] | length) as $eligible_count
| ($all | map(select(.voter))) as $voter_nodes

# Why no swap may start now whatever the hosts' fitness: the epic's conditions on the cluster as a whole, as `gates`, which hold for a swap by command as well as by decision, and the cooldown after a membership change, which only a decision waits out. A voter a swap in progress is taking out (`ignored_voters`) is held to none of the gates on its own health, and its peers may lack their connection to it.
# How many members each voter's fresh probe report says etcd lists, by voter, null for a voter with no fresh report or no reading: a rollback is settled once every voter lists the target and no more.
| ($voter_names | map({key: ., value: (if ($probes[.].samples[-1].t // 0) >= ($now - $stale_seconds) then $probes[.].samples[-1].etcd.known_members else null end)}) | from_entries) as $known_members
| ($c.ignored_voters // []) as $ignored
| ($voter_names | map(select(. as $v | $ignored | index($v) | not))) as $judged
| ([
    (if ($voters | length) != $c.expected_voters then "there are \($voters | length) voters, not the \(if $c.expected_voters == $c.target then "target \($c.target)" else "expected \($c.expected_voters) (the target \($c.target) and the new voter a swap holds)" end)" else empty end),
    ($all[] | select(.labelled and (.voter | not)) | if .removed then "node \(.name) is a member k3s has removed: it still carries the etcd label" else "node \(.name) is a member but not a voter: it carries the etcd label without \(voter_condition)=True" end),
    ($voter_nodes[] | select(.name as $n | $ignored | index($n) | not) | select(.regrade_level != null) | "voter \(.name) is at regrade level \(.regrade_level), below full"),
    ($voter_nodes[] | select(.name as $n | $ignored | index($n) | not) | select(.ready | not) | "voter \(.name) is not Ready"),
    ($judged[] as $v | $probes[$v].samples[-1] as $last | $last.etcd as $e
       | ($voters | length) as $members
       | (if ($ignored | index($v)) == null then ($ignored | map(select(. as $i | $voter_names | index($i))) | length) else 0 end) as $absent
       | if $last == null or $last.t < ($now - $stale_seconds) then "voter \($v) has no probe report in the last \($stale_seconds) s"
         elif $e == null then "etcd's state on voter \($v) is unknown"
         elif ($e.has_leader | not) then "voter \($v) reports no leader"
         elif $e.is_learner then "voter \($v) is a learner"
         elif $e.known_members != $members then "voter \($v) knows \($e.known_members) members, not the \($members) voters: a learner or a member that has not started"
         elif $e.active_peers > ($members - 1) or $e.active_peers < ($members - 1 - $absent) then "voter \($v) is connected to \($e.active_peers) of its \($members - 1) peers"
         else empty end)
  ]) as $gates
| ([
    (if $cooldown_until != null and $now < $cooldown_until then "the membership changed at \($changed_at), and the cooldown lasts until \($cooldown_until)" else empty end)
  ]) as $cooldown_blocks
| ($gates + $cooldown_blocks) as $blocks

# Which hosts' unfit verdicts each node accounts for, by node.
| ([$hosts[] | .name as $h | .attributed_to[] | {node: ., host: $h}] | group_by(.node) | map({key: .[0].node, value: map(.host)}) | from_entries) as $attribution

# The swap. A slow link makes both of its ends unfit, since a voter is unfit when its round trip to any other voter is, so the voter to replace is the one at fault: the unfit voter the most hosts' unfit verdicts are attributed to, the worst-ranked among those with as many. It goes out only if it is eligible and not pinned; when it is not, replacing a voter that shares a slow link with it would fix nothing, so nothing is swapped. The best fit eligible standby that ranks above it comes in, judged against the voters that would remain.
| ([$hosts[] | select(.role == "voter" and .verdict == "unfit") | . + {accounts_for: ($attribution[.name] // [] | length)}] | sort_by(.rank) | reverse | sort_by(-.accounts_for) | first) as $culprit
| (if $culprit != null and $culprit.eligible and ($culprit.pinned | not) then $culprit else null end) as $out
| ([$hosts[] | select(.role == "standby" and .eligible)
    | (if $out == null then .m else (.m | without($out.name)) end) as $m
    | select((gaps($m) | length) == 0 and (faults($m) | length) == 0)
    | . + {rank: rank($m)}] | sort_by(.rank)) as $fit_candidates
| ($fit_candidates | map(select($out != null and .rank < $out.rank)) | first) as $in
| (if $eligible_count == 0 then ["no host is marked eligible"]
   elif $eligible_count < $c.target then ["\($eligible_count) hosts are eligible, fewer than the target \($c.target)"]
   else [] end) as $shortfall
| (if $culprit == null then
     {action: "none", swap: null, because: (["no voter is unfit"] + $shortfall)}
   elif $out == null then
     {action: "none", swap: null, because: (["voter \($culprit.name) is the most at fault and is pinned or not marked eligible, so it is not replaced"] + $shortfall)}
   elif $in == null then
     {action: "none", swap: null, because: (["voter \($out.name) is unfit and no eligible standby is fit and ranks above it"] + $shortfall)}
   elif ($blocks | length) > 0 or ($shortfall | length) > 0 then
     {action: "none", swap: null, because: ($blocks + $shortfall), wanted: {out: $out.name, in: $in.name}}
   else
     {action: "swap", swap: {out: $out.name, in: $in.name}, because: ["voter \($out.name) is unfit (\($out.reasons | join("; "))) and \($in.name) is fit against the voters that would remain and ranks above it"]}
   end) as $decision

| {
    config: {target: $c.target, max_servers: $c.max_servers, election_timeout_ms: $c.election_timeout_ms,
             unfit_rtt_ms: $unfit_rtt_ms, fit_rtt_ms: $fit_rtt_ms, wal_fsync_limit_ms: wal_fsync_limit_ms, backend_commit_limit_ms: backend_commit_limit_ms,
             window_samples: window_samples, sample_seconds: $c.sample_seconds, window_seconds: $window_seconds, cooldown_seconds: $cooldown_seconds,
             quota_bytes: $quota_bytes},
    membership: {voters: $voter_names, known_members: $known_members, changed_at: $changed_at, cooldown_until: $cooldown_until},
    hosts: ($hosts | map({key: .name, value: del(.name, .rank, .m)}) | from_entries),
    attribution: $attribution,
    ranking: ($hosts | map(select(.verdict == "fit")) | sort_by(.rank) | map(.name)),
    gates: $gates,
    blocks: $blocks,
    decision: $decision
  }
