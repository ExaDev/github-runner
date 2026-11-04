# Statistics, etcd metrics parsing and the facts about k3s that are shared by the role controller's probe (scripts/fleet-role-probe.sh) and the controller itself (scripts/fleet-role.jq), included with `include "fleet-role-stats";` and jq -L naming this directory.

# k3s's learnerMaxStallTime (pkg/etcd/etcd.go): a learner that makes no progress for this long is removed, so a membership change has settled this long after it, and a candidate that has not become a voter by then has failed.
def learner_stall_seconds: 300;

# The annotation k3s's member controller sets on a Node once etcd has removed the member named in it (pkg/etcd/member_controller.go). The Node keeps its etcd label and its stale EtcdIsVoter condition until a demotion cleans them up, so a Node carrying this is not a voter whatever else it says.
def removed_annotation: "etcd.k3s.cattle.io/removed-node-name";

# The tail the controller judges a host by, as a whole percentile.
def tail_percentile: 99;

# The fewest samples that give the tail percentile a sample of its own: with this many, the nearest-rank percentile is the second largest, so one outlier in the window never decides alone. 100 / (100 - 99) is exact in floating point, which 1 / (1 - 0.99) is not.
def window_samples: 100 / (100 - tail_percentile);

# The nearest-rank percentile of an array of numbers: the smallest value that at least $p percent of the array is at or below. Null for an empty array.
def nearest_rank($p): if length == 0 then null else sort | .[((length * $p / 100) | ceil) - 1] end;

# A histogram's bucket bounds as numbers, with the +Inf bucket as infinity.
def bound: if . == "+Inf" then infinite else tonumber end;

# The $p percentile of a histogram given as an object from a bucket's upper bound (a string, as Prometheus writes it) to the number of observations that fell in that bucket alone, interpolated linearly inside the bucket the rank falls in, as Prometheus's histogram_quantile does. A rank in the +Inf bucket gives the largest finite bound. Null when there are no observations.
def histogram_quantile($p):
  to_entries | map({le: (.key | bound), n: .value}) | sort_by(.le) as $buckets
  | (map(.n) | add // 0) as $total
  | if $total == 0 then null else
      ($total * $p / 100) as $rank
      | reduce $buckets[] as $b ({cum: 0, lower: 0, out: null};
          if .out != null then .
          else (.cum + $b.n) as $cum
            | if $cum >= $rank then
                {cum: $cum, lower: $b.le,
                 out: (if $b.le == infinite then .lower else .lower + ($b.le - .lower) * (($rank - .cum) / $b.n) end)}
              else {cum: $cum, lower: $b.le, out: null} end
          end)
      | .out
    end;

# The sum of several such objects, bucket by bucket.
def sum_buckets: reduce .[] as $o ({}; reduce ($o | keys[]) as $k (.; .[$k] += $o[$k]));

# A cumulative histogram (upper bound to the number of observations at or below it, as Prometheus exposes it) as the number of observations in each bucket alone.
def per_bucket:
  to_entries | map({key: .key, le: (.key | bound), n: .value}) | sort_by(.le) as $b
  | [range(0; $b | length) | {(($b[.]).key): (($b[.]).n - (if . == 0 then 0 else ($b[. - 1]).n end))}] | add // {};

# What a histogram gained between two readings of it, per bucket and without the empty ones, and whether it went backwards, which only a restart of the process that keeps it does. $previous is null on the first reading, which has nothing to subtract from.
def gained($previous; $now):
  ($now | per_bucket) as $after
  | if $previous == null then {reset: false, gained: {}}
    else ($previous | per_bucket) as $before
      | ($after | with_entries(.value -= ($before[.key] // 0))) as $delta
      | {reset: ($delta | any(.[]; . < 0)), gained: ($delta | with_entries(select(.value > 0)))}
    end;

# The labels of one line of the Prometheus text format as an object.
def labels: [scan("([A-Za-z_][A-Za-z0-9_]*)=\"([^\"]*)\"") | {(.[0]): .[1]}] | add // {};

# The etcd series of a metrics page (read raw, `jq -Rs`), as {name, labels, value}.
def etcd_series:
  split("\n")
  | map(select(startswith("etcd_"))
        | capture("^(?<name>[A-Za-z0-9_:]+)(\\{(?<labels>[^}]*)\\})?[ \t]+(?<value>[^ \t]+)$")
        | {name, labels: ((.labels // "") | labels), value: (.value | tonumber?)}
        | select(.value != null));

def histogram_of($series; $name): [$series[] | select(.name == ($name + "_bucket")) | {(.labels.le): .value}] | add // {};

# What the probe keeps of an etcd metrics page, cumulative as etcd reports it: the member's own id (the `To` label of its peers' round trip histograms is this id), whether it has a leader, whether it is a learner, the number of members it knows (itself included, and a learner or a member that has not started too, which is how a membership change in progress shows) and the number of peers it is connected to (etcd keeps one gauge per peer that counts its active streams to it, up to two, so a peer counts when its gauge is at least 1), its storage quota, the leader changes it has seen, and the three histograms the controller judges (the WAL fsync, the backend commit and, per peer id, the round trip).
def etcd_reading:
  etcd_series as $s
  | def gauge($name): [$s[] | select(.name == $name) | .value] | .[0];
    {id: ([$s[] | select(.name == "etcd_server_id") | .labels.server_id] | .[0]),
     has_leader: (gauge("etcd_server_has_leader") == 1),
     is_learner: (gauge("etcd_server_is_learner") == 1),
     known_members: ([$s[] | select(.name == "etcd_network_known_peers" and .value == 1)] | length),
     active_peers: ([$s[] | select(.name == "etcd_network_active_peers" and .value >= 1) | .labels.Remote] | unique | length),
     quota_bytes: gauge("etcd_server_quota_backend_bytes"),
     leader_changes: gauge("etcd_server_leader_changes_seen_total"),
     wal: histogram_of($s; "etcd_disk_wal_fsync_duration_seconds"),
     commit: histogram_of($s; "etcd_disk_backend_commit_duration_seconds"),
     rtt: ([$s[] | select(.name == "etcd_network_peer_round_trip_time_seconds_bucket") | {to: .labels.To, le: .labels.le, value}]
           | group_by(.to) | map({(.[0].to): (map({(.le): .value}) | add)}) | add // {})};

# The change from the cumulative readings kept after the last sample ($previous, null for the first) to the one just read ($now, an etcd_reading): the gauges as they are, each histogram as what it gained, and whether any went backwards, which means etcd restarted. A peer whose round trip histogram has not been seen before has nothing to subtract from, so it gains nothing in this sample.
def etcd_sample($previous; $now):
  gained($previous.wal; $now.wal) as $wal
  | gained($previous.commit; $now.commit) as $commit
  | ($now.rtt | to_entries | map({key, gain: gained($previous.rtt[.key]; .value)})) as $rtt
  | {sample: {has_leader: $now.has_leader, is_learner: $now.is_learner, known_members: $now.known_members, active_peers: $now.active_peers, quota_bytes: $now.quota_bytes,
              leader_changes: (if $previous == null or $now.leader_changes < $previous.leader_changes then 0 else $now.leader_changes - $previous.leader_changes end),
              wal: $wal.gained, commit: $commit.gained,
              rtt: ($rtt | map({(.key): .gain.gained}) | add // {})},
     restarted: ($previous != null and ($wal.reset or $commit.reset or any($rtt[]; .gain.reset) or $now.leader_changes < $previous.leader_changes)),
     cumulative: {wal: $now.wal, commit: $now.commit, rtt: $now.rtt, leader_changes: $now.leader_changes}};

# The part of a probe sample's memory reading the regrade core calls over the limit (fleet-regrade-core.sh, is_over): pressure at or above the threshold and MemAvailable below the floor share of MemTotal, both. tests/unit/test_fleet_role.py holds this to the core's own function. $c is the controller's settings (pressure_threshold, available_floor_percent).
def over_regrade_limit($c): .psi >= $c.pressure_threshold and (.avail_kb * 100) < (.total_kb * $c.available_floor_percent);
