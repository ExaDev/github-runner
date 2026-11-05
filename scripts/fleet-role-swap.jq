# The role controller's voter swap (see scripts/fleet-role.sh, which gathers the inputs and carries out the action this program names): what the next step of a swap is, from what the cluster shows, and whether a swap may start. Pure: nothing here reads the cluster or the clock, and the controller holds no state of its own beyond the status it publishes, since the step a swap is at is read from the Nodes, the actuators' reports and the regrade Lease, so a controller pod that is rescheduled resumes where the swap is.
#
# Included with `include "fleet-role-swap";` and jq -L naming this directory. `context($s)` and `next_action($s)` take one object:
# - config: `now` (epoch seconds), `target`, `holder` (the Lease holder the controller is), `dry_run`, `operation_timeout` and `runner_wait` (seconds), `eligible_label`, `pinned_label` and `level_label` (the Node labels the controller reads).
# - nodes: the Nodes, as `kubectl get nodes -o json` lists them (the `items`).
# - lease: the regrade Lease as the controller read it, as {state, holder, swap_id, swap_nodes}: state being free, expired, mine or held as fleet-regrade-core.sh's lease_state says for the controller as the holder (after any renewal), swap_nodes the annotation's comma-separated "out,in".
# - actuators: the actuators' reports by node (the ConfigMap's keys, parsed), {} when there are none.
# - node_leases: the renew time of each swap node's kube-node-lease Lease, by node, which a running kubelet renews.
# - snapshots: the ETCDSnapshotFile objects (`items`), or null when they could not be listed.
# - runner_pods: how many unfinished runner pods run on each swap node, by node.
# - decision: the decision program's output (scripts/fleet-role.jq) for this pass, given the `context` below.
# - previous: the status the controller published on the last pass, or null; request: the manual request pending, {out, in, at}, or null.
#
# Every wait derives from the settings: the operation timeout, the runner wait and k3s's learner stall time (fleet-role-stats.jq).

include "fleet-role-stats";

# The names the controller and the actuators share, fixed by the actuator (roles/github_runner_cluster/files/role/fleet-role-actuator.sh) and by k3s; tests/unit/test_fleet_role_swap.py holds the two together, and fleet-role.sh reads the names it writes from here.
def domain: "github-runner.exadev.com/";
def interface: {
  desired_role_label: (domain + "desired-role"),
  swap_annotation: (domain + "role-swap"),
  step_annotation: (domain + "role-step"),
  join_node_annotation: (domain + "role-join-node"),
  snapshot_annotation: (domain + "snapshot-request"),
  member_gone_annotation: (domain + "role-member-gone"),
  lease_swap_id_annotation: (domain + "swap-id"),
  lease_swap_nodes_annotation: (domain + "swap-nodes"),
  etcd_label: "node-role.kubernetes.io/etcd",
  control_plane_label: "node-role.kubernetes.io/control-plane",
  master_label: "node-role.kubernetes.io/master",
  etcd_annotation_prefix: "etcd.k3s.cattle.io/",
  remove_annotation: "etcd.k3s.cattle.io/remove",
  removed_annotation: removed_annotation,
  control_plane_taint: "node-role.kubernetes.io/control-plane",
  lease_holder: "fleet-role"
};

def voter_condition: "EtcdIsVoter";

# The seconds a step may take before the swap gives up on it or the controller says it is overdue, by step.
def budget($c):
  {
    snapshot: $c.operation_timeout,
    drain: ($c.runner_wait + $c.operation_timeout),
    evict: ($c.runner_wait + $c.operation_timeout),
    promote: (learner_stall_seconds + $c.operation_timeout),
    gates: $c.operation_timeout,
    "step-down": $c.operation_timeout,
    remove: $c.operation_timeout,
    finish: $c.operation_timeout,
    cleanup: $c.operation_timeout,
    rollback: $c.operation_timeout
  };

# What the decision reads off a Node.
def facts($c):
  interface as $i
  | (.metadata.labels // {}) as $labels
  | (.metadata.annotations // {}) as $annotations
  | ((.status.conditions // [])) as $conditions
  | {
      name: .metadata.name,
      ready: (($conditions | map(select(.type == "Ready")) | .[0].status) == "True"),
      labelled: ($labels[$i.etcd_label] == "true"),
      voter: ($labels[$i.etcd_label] == "true" and (($conditions | map(select(.type == voter_condition)) | .[0].status) == "True") and (($annotations[$i.removed_annotation] // "") == "")),
      removed: (($annotations[$i.removed_annotation] // "") != ""),
      remove_requested: (($annotations[$i.remove_annotation] // "") != ""),
      cordoned: (.spec.unschedulable // false),
      tainted: ((.spec.taints // []) | any(.key == $i.control_plane_taint)),
      level: $labels[$c.level_label],
      eligible: ($labels[$c.eligible_label] == "true"),
      pinned: ($labels[$c.pinned_label] == "true"),
      desired: ($labels[$i.desired_role_label] // ""),
      swap: ($annotations[$i.swap_annotation] // ""),
      step: ($annotations[$i.step_annotation] // ""),
      snapshot_request: ($annotations[$i.snapshot_annotation] // ""),
      member_gone: ($annotations[$i.member_gone_annotation] // ""),
      etcd_annotations: ($annotations | keys | map(select(startswith($i.etcd_annotation_prefix)))),
      server_labels: ([$i.etcd_label, $i.control_plane_label, $i.master_label] | map(select($labels[.] != null)))
    };

# The Nodes by name, with their facts.
def by_name($s): $s.nodes | map(facts($s.config)) | map({key: .name, value: .}) | from_entries;

def swap_id($out; $in; $now): ($now | strftime("%Y%m%dT%H%M%SZ")) + "-" + $out + "-" + $in;

# The epoch time a swap id names (<UTC %Y%m%dT%H%M%SZ>-<out>-<in>).
def id_epoch: .[0:16] | strptime("%Y%m%dT%H%M%SZ") | mktime;

# The swap in progress, as {id, out, in, source}, or null: from the Lease while the controller holds it with a swap's annotations, else from the status it last published, else from the swap annotations on the Nodes (the one id they carry, split at the pair of nodes whose names it ends with, since a node name may hold a hyphen).
def identity($s):
  $s.lease as $lease
  | ($s.previous.swap // null) as $previous
  | if $lease.holder == $s.config.holder and ($lease.swap_id // "") != "" and (($lease.swap_nodes // "") | split(",") | length) == 2 then
      ($lease.swap_nodes | split(",")) as $nodes
      | {id: $lease.swap_id, out: $nodes[0], in: $nodes[1], source: "lease"}
    elif $previous != null then
      {id: $previous.id, out: $previous.out, in: $previous.in, source: "status"}
    else
      ([$s.nodes[] | (.metadata.annotations // {})[interface.swap_annotation] // empty | select(. != "")] | unique) as $ids
      | ($s.nodes | map(.metadata.name)) as $names
      | if ($ids | length) != 1 then null
        else $ids[0] as $id
          | ([$names[] as $out | $names[] as $in | select($out != $in and ($id | endswith("-" + $out + "-" + $in)) and ($id | id_epoch | . != null)) | {id: $id, out: $out, in: $in, source: "nodes"}] | first)
        end
    end;

# What the decision program must be told about the swap in progress: the voters to expect (one more than the target while the new voter and the outgoing one both vote) and the voter the swap is taking out, which the gates do not hold to its own health.
def context($s):
  by_name($s) as $nodes
  | identity($s) as $id
  | $s.config.target as $target
  | if $id == null then {swap: null, expected_voters: $target, ignored_voters: []}
    else ($nodes[$id.out]) as $out | ($nodes[$id.in]) as $in
      | (($in != null and $in.voter) and ($out != null) and ($out.removed | not)) as $both
      | {swap: $id,
         expected_voters: (if $both then $target + 1 else $target end),
         ignored_voters: (if $both and (($out.ready | not) or $out.level != null or $out.desired == "agent") then [$id.out] else [] end)}
    end;

# The reasons a swap of $out for $in may not start, as {status, reasons}: status 2 for a name that is no node, a node that is not marked eligible or not the voter or standby it is taken for (the request is wrong), 1 for a cluster that is not ready for it (every gate), 0 for none. Fitness and the cooldown are not among them: a request is carried out whatever the verdicts say.
def check($s; $out; $in):
  by_name($s) as $nodes
  | $s.decision.gates as $gates
  | $s.lease as $lease
  | ($nodes[$out]) as $o
  | ($nodes[$in]) as $i
  | [
      (if $out == $in then "a swap needs two nodes, not \($out) twice" else empty end),
      (if $o == null then "no node is named \($out)" else empty end),
      (if $i == null then "no node is named \($in)" else empty end),
      (if $o != null and ($o.voter | not) then "\($out) is not a voter" else empty end),
      (if $i != null and ($i.labelled or $i.voter) then "\($in) is already a member of etcd" else empty end),
      (if $o != null and ($o.eligible | not) then "\($out) is not marked eligible" else empty end),
      (if $i != null and ($i.eligible | not) then "\($in) is not marked eligible" else empty end)
    ] as $usage
  | [
      (if $o != null and $o.pinned then "voter \($out) is pinned, so it is never taken out" else empty end),
      (if $i != null and ($i.ready | not) then "\($in) is not Ready" else empty end),
      (if $lease.state == "held" then "the regrade Lease is held by \($lease.holder)" else empty end),
      (if $lease.state == "mine" and ($lease.swap_id // "") != "" then "a swap is in progress (\($lease.swap_id))" else empty end),
      ($gates[])
    ] as $refusals
  | if ($usage | length) > 0 then {status: 2, reasons: $usage}
    elif ($refusals | length) > 0 then {status: 1, reasons: $refusals}
    else {status: 0, reasons: []} end;

def step_record($s; $id; $step; $extra):
  ($s.previous.swap // null) as $previous
  | (if $previous != null and $previous.id == $id.id and $previous.step == $step then $previous.step_since else $s.config.now end) as $since
  | {id: $id.id, out: $id.out, in: $id.in, step: $step,
     started_at: ($id.id | id_epoch),
     step_since: $since,
     deadline: ($since + budget($s.config)[$step]),
     manual: ($previous.manual // false),
     snapshot: ($previous.snapshot // null),
     outgoing_pending: ($previous.outgoing_pending // false)} + $extra;

# The actuator's last report on $node for the step $step of swap $id, or null.
def report($s; $node; $id; $step):
  ($s.actuators[$node] // null) as $r
  | if $r != null and $r.swap == $id and $r.step == $step then $r else null end;

def ended($outcome; $reasons; $s; $id):
  {outcome: $outcome, id: $id.id, out: $id.out, in: $id.in, at: $s.config.now, reasons: $reasons};

# An RFC 3339 time cut to whole seconds, which compares as text.
def seconds: .[0:19];

# Succeeds when $node's kubelet has renewed its node Lease at or after $since (a time as RFC 3339).
def renewed_since($s; $node; $since):
  ($s.node_leases[$node] // "") as $renewed
  | $since != null and $since != "" and $renewed != "" and ($renewed | seconds) >= ($since | seconds);

# Succeeds when etcd no longer lists the outgoing node $o's member: it is not a voter, and every voter reports, fresh, that etcd lists as many members as there are voters, so the members are the voters and nothing else (a learner or a member that has not started counts among the members a voter reports).
def gone_from_etcd($s; $o):
  ($s.decision.membership.voters) as $voters
  | ($o.voter | not) and ($voters | length) > 0
    and ($voters | all(. as $v | $s.decision.membership.known_members[$v] == ($voters | length)));

# The swap, step by step. Returns {swap, action, wait, consume_request, last_swap}: `swap` the record the status holds, `action` the one thing to do this pass (or null), `wait` why nothing is done, `last_swap` the record of a swap or a request that ends this pass if the action succeeds.
def in_progress($s; $id):
  by_name($s) as $nodes
  | ($nodes[$id.out]) as $o
  | ($nodes[$id.in]) as $i
  | $s.config as $c
  | $s.decision.gates as $gates
  | $s.lease as $lease
  | ($s.snapshots) as $snapshots
  | def wait($step; $reasons; $extra): {swap: step_record($s; $id; $step; $extra), action: null, wait: $reasons, consume_request: false, last_swap: null};
    def wait($step; $reasons): wait($step; $reasons; {});
    def do($step; $action; $extra): {swap: step_record($s; $id; $step; $extra), action: $action, wait: [], consume_request: false, last_swap: null};
    def do($step; $action): do($step; $action; {});
    def finalise($step; $outcome; $reasons; $uncordon; $pending):
      {swap: step_record($s; $id; $step; {}),
       action: {kind: "finalise", outcome: $outcome, uncordon: $uncordon},
       wait: [], consume_request: false,
       last_swap: (ended($outcome; $reasons; $s; $id) + (if $pending then {outgoing_pending: true} else {} end))};
    def settled: ($gates | length) == 0;
    def cleaned($n): ($n.labelled | not) and $n.level == null and ($n.etcd_annotations | length) == 0 and ($n.tainted | not);

  if $lease.state == "held" then
    wait($s.previous.swap.step // "snapshot"; ["the regrade Lease is held by \($lease.holder): nothing is done to the swap of \($id.out) for \($id.in) until it is released or has expired"])
  elif $lease.state != "mine" then
    do($s.previous.swap.step // "snapshot"; {kind: "take_lease", id: $id.id, out: $id.out, in: $id.in, manual: ($s.previous.swap.manual // false)})
  elif $i == null then
    finalise("gates"; "abandoned"; ["the candidate node \($id.in) is gone"]; []; false)
  elif $o == null then
    if $i.voter then finalise("cleanup"; "completed"; ["the outgoing node \($id.out) is gone"]; []; false)
    else finalise("gates"; "abandoned"; ["the outgoing node \($id.out) is gone before the candidate voted"]; [$id.in]; false) end

  # A candidate that did not become a voter is being rolled back.
  elif $i.swap == $id.id and $i.step == "rollback" then
    report($s; $id.in; $id.id; "rollback") as $r
    | if $r == null or $r.phase == "running" then wait("rollback"; ["waiting for \($id.in)'s actuator to roll the promotion back"])
      elif $r.phase == "failed" or $r.phase == "refused" then wait("rollback"; ["\($id.in)'s actuator reports the rollback \($r.phase): \($r.message)"])
      elif ($s.decision.membership.known_members | to_entries | map(.value) | all(. == $c.target)) | not then
        wait("rollback"; ["waiting for every voter to list \($c.target) members, which is etcd without the candidate"])
      elif ($i.labelled or $i.cordoned or $i.tainted or ($i.etcd_annotations | length) > 0) then
        do("rollback"; {kind: "clean_node", node: $id.in, uncordon: true})
      else finalise("rollback"; "rolled-back"; ["\($id.in) did not become a voter and is an agent again"]; []; false) end

  # The outgoing voter has been asked to step down.
  elif $o.swap == $id.id and $o.step == "step-down" then
    report($s; $id.out; $id.id; "step-down") as $down
    | report($s; $id.out; $id.id; "finish-demote") as $finish
    | if cleaned($o) then
        if ($o.cordoned | not) then finalise("cleanup"; "completed"; ["\($id.in) votes and \($id.out) is an agent"]; []; ($s.previous.swap.outgoing_pending // false))
        elif ($s.previous.swap.outgoing_pending // false) then
          finalise("cleanup"; "completed"; ["\($id.in) votes; \($id.out) did not come back as an agent in time and is left to node recovery"]; [$id.out]; true)
        elif $o.ready and renewed_since($s; $id.out; $finish.started_at) then do("cleanup"; {kind: "uncordon", node: $id.out})
        else
          step_record($s; $id; "cleanup"; {}) as $record
          | if $c.now >= $record.deadline then
              {swap: ($record + {outgoing_pending: true}), action: null, wait: ["\($id.out) did not come back as an agent within \(budget($c).cleanup) s; it is left to node recovery"], consume_request: false, last_swap: null}
            else wait("cleanup"; ["waiting for \($id.out) to be Ready as an agent"]) end
        end
      # Removed, as k3s records it on the Node or as etcd's own membership shows it, which is the proof that holds when the Node's labels and annotations have been set again by a server still running on the host, or name a member other than the one its datastore holds.
      elif $o.removed or gone_from_etcd($s; $o) then
        if $finish != null and $finish.phase == "done" then do("cleanup"; {kind: "clean_node", node: $id.out, uncordon: false})
        # The actuator cannot read etcd's membership, and k3s's record on the Node can name another member than the datastore's, so the controller tells it, under this swap, what every remaining voter reports.
        elif gone_from_etcd($s; $o) and $o.member_gone != $id.id then do("finish"; {kind: "mark_member_gone", node: $id.out, id: $id.id})
        else
          step_record($s; $id; "finish"; {}) as $record
          | if $c.now >= $record.deadline and ($s.previous.swap.outgoing_pending // false) then
              finalise("finish"; "completed"; ["\($id.in) votes and etcd no longer lists \($id.out), whose actuator did not finish its demotion within \(budget($c).finish) s; it is left to node recovery"]; []; true)
            elif $c.now >= $record.deadline then
              {swap: ($record + {outgoing_pending: true}), action: {kind: "clean_node", node: $id.out, uncordon: false}, wait: [], consume_request: false, last_swap: null}
            else wait("finish"; ["waiting for \($id.out)'s actuator to finish its demotion"]) end
        end
      elif $o.remove_requested then wait("remove"; ["waiting for k3s to remove \($id.out)'s member"])
      else
        step_record($s; $id; "step-down"; {}) as $record
        | (($down != null and $down.phase == "done")
           or ($o.ready | not) and ($down != null and ($down.phase == "failed" or $down.phase == "refused") or $c.now >= $record.deadline)) as $removable
        | if $removable then
            if settled then do("remove"; {kind: "annotate_remove", node: $id.out})
            else wait("remove"; ["not removing \($id.out) while the remaining voters are not all healthy"] + $gates) end
          elif $down != null and ($down.phase == "failed" or $down.phase == "refused") then
            wait("step-down"; ["\($id.out)'s actuator reports the step-down \($down.phase) and the node is Ready, so it is held for the owner: \($down.message)"])
          else wait("step-down"; ["waiting for \($id.out)'s actuator to step it down to etcd-only"]) end
      end

  # The candidate has been asked to become a voter.
  elif $i.swap == $id.id and $i.step == "promote" then
    report($s; $id.in; $id.id; "promote") as $r
    | step_record($s; $id; "promote"; {}) as $record
    | ($r != null and $r.phase == "done" and $i.voter and $i.ready and renewed_since($s; $id.in; $r.started_at)) as $voting
    | if $voting and $i.cordoned then do("promote"; {kind: "uncordon", node: $id.in})
      elif $voting then
        if settled then
          if ($o.ready | not) then do("step-down"; {kind: "step_down_out", node: $id.out, id: $id.id, drain: false})
          elif ($o.cordoned | not) or ($o.tainted | not) then do("evict"; {kind: "cordon_out", node: $id.out})
          else
            step_record($s; $id; "evict"; {}) as $evict
            | if ($s.runner_pods[$id.out] // 0) > 0 and $c.now < $evict.step_since + $c.runner_wait then
                wait("evict"; ["waiting for \($s.runner_pods[$id.out]) runner pod(s) on \($id.out) to finish before it is drained"])
              else do("step-down"; {kind: "step_down_out", node: $id.out, id: $id.id, drain: true}) end
          end
        else wait("gates"; ["not taking \($id.out) out while the cluster is not healthy with \($id.in) in"] + $gates) end
      elif $r != null and $r.phase == "refused" then
        finalise("promote"; "abandoned"; ["\($id.in)'s actuator refused the promotion: \($r.message)"]; [$id.in]; false)
      elif ($r != null and $r.phase == "failed") then
        do("rollback"; {kind: "begin_rollback", node: $id.in, id: $id.id, remove: (($i.etcd_annotations | length) > 0)})
      elif $c.now >= $record.deadline then
        do("rollback"; {kind: "begin_rollback", node: $id.in, id: $id.id, remove: (($i.etcd_annotations | length) > 0)})
      else wait("promote"; ["waiting for \($id.in) to be a voter and Ready on a heartbeat after its server started"]) end

  # The candidate votes and no swap annotation is left on it or on the outgoing voter: a swap that ended with the Lease still held, which is ended and never started again.
  elif $i.voter and $i.swap == "" then
    finalise("cleanup"; "completed"; ["\($id.in) votes and the swap's annotations are cleared"]; []; ($s.previous.swap.outgoing_pending // false))

  # Before anything has been done to the candidate: the snapshot, then emptying it.
  else
    step_record($s; $id; "snapshot"; {}) as $record
    | ($s.decision.membership.voters | map(select(. != $id.out))) as $keepers
    | ($keepers | map(select(. as $n | $nodes[$n].snapshot_request == $id.id))) as $asked
    | (if ($asked | length) > 0 then report($s; $asked[-1]; $id.id; "snapshot") else null end) as $taken
    | (if $taken != null and $taken.phase == "done" and $snapshots != null
       then ($taken.snapshot | split(",") | map(select(. != ""))) as $names
         | ($names | length) > 0 and ($names | all(. as $name | $snapshots | map(select(.spec.snapshotName == $name and .status.readyToUse == true)) | length > 0))
       else false end) as $ready
    | if $ready then
        step_record($s; $id; "drain"; {snapshot: $taken.snapshot}) as $drain
        | if ($i.cordoned | not) then
            if settled then do("drain"; {kind: "cordon", node: $id.in}; {snapshot: $taken.snapshot})
            elif $c.now >= $record.deadline then finalise("snapshot"; "abandoned"; ["the cluster was not healthy enough to start within \(budget($c).snapshot) s"] + $gates; []; false)
            else wait("snapshot"; ["not emptying \($id.in) while the cluster is not healthy"] + $gates) end
          elif ($s.runner_pods[$id.in] // 0) > 0 then
            if $c.now >= $drain.step_since + $c.runner_wait then finalise("drain"; "abandoned"; ["runner pods on \($id.in) did not finish within \($c.runner_wait) s"]; [$id.in]; false)
            else wait("drain"; ["waiting for \($s.runner_pods[$id.in]) runner pod(s) on \($id.in) to finish"]; {snapshot: $taken.snapshot}) end
          elif $c.now >= $drain.deadline then finalise("drain"; "abandoned"; ["\($id.in) could not be drained within \(budget($c).drain) s"]; [$id.in]; false)
          elif settled then do("drain"; {kind: "promote_in", node: $id.in, id: $id.id, join_node: $keepers[0]}; {snapshot: $taken.snapshot})
          else wait("drain"; ["not promoting \($id.in) while the cluster is not healthy"] + $gates; {snapshot: $taken.snapshot}) end
      elif $taken != null and ($taken.phase == "failed" or $taken.phase == "refused") then
        ($keepers | map(select(. as $n | $nodes[$n].snapshot_request != $id.id))) as $untried
        | if ($untried | length) > 0 then do("snapshot"; {kind: "request_snapshot", node: $untried[0], id: $id.id})
          else finalise("snapshot"; "abandoned"; ["no voter could take a snapshot: \($taken.message)"]; []; false) end
      elif $c.now >= $record.deadline then
        finalise("snapshot"; "abandoned"; ["no snapshot was ready within \(budget($c).snapshot) s"]; []; false)
      elif ($asked | length) > 0 then wait("snapshot"; ["waiting for \($asked[-1])'s snapshot to be ready"])
      elif ($gates | length) > 0 then wait("snapshot"; ["not starting while the cluster is not healthy"] + $gates)
      elif ($keepers | length) == 0 then finalise("snapshot"; "abandoned"; ["there is no voter to take a snapshot"]; []; false)
      else do("snapshot"; {kind: "request_snapshot", node: $keepers[0], id: $id.id}) end
  end;

# Whether a swap starts now, and which: a manual request that passes its checks, or, with the dry run off, the decision's swap. {swap, action, wait, consume_request, last_swap, check}.
def starting($s):
  $s.request as $request
  | $s.config as $c
  | $s.lease as $lease
  | (if $request == null then null else check($s; $request.out; $request.in) end) as $check
  | if $request != null then
      if $check.status == 0 then
        swap_id($request.out; $request.in; $c.now) as $id
        | {swap: {id: $id, out: $request.out, in: $request.in, step: "snapshot", started_at: $c.now, step_since: $c.now, deadline: ($c.now + budget($c).snapshot), manual: true, snapshot: null, outgoing_pending: false},
           action: {kind: "take_lease", id: $id, out: $request.out, in: $request.in, manual: true},
           wait: [], consume_request: true, last_swap: null, check: $check}
      else
        {swap: null, action: null, wait: ["the request to swap \($request.out) for \($request.in) is refused: \($check.reasons | join("; "))"], consume_request: true,
         last_swap: {outcome: "abandoned", id: null, out: $request.out, in: $request.in, at: $c.now, reasons: $check.reasons}, check: $check}
      end
    elif $s.decision.decision.action == "swap" then
      $s.decision.decision.swap as $swap
      | if $c.dry_run then
          {swap: null, action: null, wait: ["dry run: would swap out \($swap.out) for \($swap.in)"], consume_request: false, last_swap: null, check: null}
        elif $lease.state == "held" then
          {swap: null, action: null, wait: ["not swapping \($swap.out) for \($swap.in) while the regrade Lease is held by \($lease.holder)"], consume_request: false, last_swap: null, check: null}
        else
          swap_id($swap.out; $swap.in; $c.now) as $id
          | {swap: {id: $id, out: $swap.out, in: $swap.in, step: "snapshot", started_at: $c.now, step_since: $c.now, deadline: ($c.now + budget($c).snapshot), manual: false, snapshot: null, outgoing_pending: false},
             action: {kind: "take_lease", id: $id, out: $swap.out, in: $swap.in, manual: false},
             wait: [], consume_request: false, last_swap: null, check: null}
        end
    else
      {swap: null, action: null, wait: [], consume_request: false, last_swap: null, check: null}
    end;

def next_action($s):
  identity($s) as $id
  | if $id == null then starting($s)
    else in_progress($s; $id) + {check: (if $s.request == null then null else {status: 1, reasons: ["a swap is in progress (\($id.id))"]} end)}
         | .consume_request = ($s.request != null)
    end;
