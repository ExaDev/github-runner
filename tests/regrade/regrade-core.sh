# Tests for the platform-neutral core of fleet-regrade (modules/fleet-regrade-core.sh): the signals, the decision, the order of a step, and that the core decides the same whatever actuator it drives. Run as `bash tests/regrade-core.sh <path to fleet-regrade-core.sh>`; the flake's checks do. The actuator is a fake in these tests (several, in fact), every path the core reads is a fixture, and the two functions that act on a cluster are recorders, so nothing here touches a cluster or the running system.
# The core calls the functions defined here (the fake actuator and the recorders) by name, and reads the variables set here, so shellcheck sees them as unused.
# shellcheck disable=SC2329,SC2034
set -euo pipefail

core=${1:?usage: regrade-core.sh <path to fleet-regrade-core.sh>}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

cat > "$work/env" <<ENV
PRESSURE_THRESHOLD=10
AVAILABLE_FLOOR_PERCENT=15
SAMPLE_SECONDS=30
SUSTAIN_SECONDS=120
COOLDOWN_SECONDS=900
OPERATION_TIMEOUT_SECONDS=300
NODE_NAME=testnode
CONTROL_PLANE_TAINT=node-role.kubernetes.io/control-plane=true:NoSchedule
KUBECONFIG_PATH=$work/kubeconfig
KUBECTL=$work/bin/kubectl
LEASE_DURATION_SECONDS=600
ENV

# A cluster whose other etcd-voting nodes are all Ready, and no lease: a step is allowed. The coordination itself is tested in regrade-lease.sh.
mkdir -p "$work/bin" "$work/cluster"
cp "$(dirname "$0")/fake-kubectl.sh" "$work/bin/kubectl"
export FAKE_DIR=$work/cluster
printf '{"items":[{"metadata":{"name":"peer-a"},"status":{"conditions":[{"type":"Ready","status":"True"}]}},{"metadata":{"name":"testnode"},"status":{"conditions":[{"type":"Ready","status":"True"}]}}]}\n' > "$FAKE_DIR/nodes.json"

export FLEET_REGRADE_ENV=$work/env PSI_FILE=$work/psi MEMINFO_FILE=$work/meminfo UPTIME_FILE=$work/uptime

# shellcheck source=/dev/null
. "$(dirname "$0")/testlib.sh"
# shellcheck source=/dev/null
. "$core"
now_epoch() { echo 1000000000; }

total=1000000 # kB; the floor is 15 percent of this, 150000 kB

printf 'some avg10=0.00 avg60=0.00 avg300=12.50 total=1\nfull avg10=0.00 avg60=0.00 avg300=3.00 total=1\n' > "$work/psi"
printf 'MemTotal:       %s kB\nMemAvailable:   %s kB\n' "$total" 100000 > "$work/meminfo"
check "reads the some avg300 line, not full" test "$(psi_avg300)" = 12.50
check "reads MemAvailable" test "$(meminfo_kb MemAvailable)" = 100000

check "over: pressure at threshold and memory below the floor" is_over 10 100000 "$total"
check_not "not over: pressure just under the threshold" is_over 9.99 100000 "$total"
check_not "not over: pressure high but MemAvailable above the floor" is_over 50 150000 "$total"
check "over: MemAvailable just under the floor" is_over 50 149999 "$total"

check_not "no step with no streak" may_step 1000 '' ''
check_not "no step before the sustain period" may_step 1119 1000 ''
check "step once sustained, with no earlier step" may_step 1120 1000 ''
check_not "no step inside the cooldown" may_step 2000 1000 1200
check "step once the cooldown has passed" may_step 2100 1000 1200

# Three fake actuators that satisfy the contract in different ways: A keeps its level in a file under the usual names, B keeps it in memory under other names, C has only two levels. Each records what it was asked to apply.
applied="$work/applied"
actuator_a() {
  echo full > "$work/level-a"
  levels() { printf '%s\n' full control-plane etcd-only; }
  current_level() { cat "$work/level-a"; }
  apply_level() { echo "$1" > "$work/level-a"; echo "$1" >> "$applied"; }
}
actuator_b() {
  B_LEVEL=alpha
  levels() { printf '%s\n' alpha beta gamma; }
  current_level() { echo "$B_LEVEL"; }
  apply_level() { B_LEVEL=$1; echo "$1" >> "$applied"; }
}
actuator_c() {
  C_LEVEL=one
  levels() { printf '%s\n' one two; }
  current_level() { echo "$C_LEVEL"; }
  apply_level() { C_LEVEL=$1; echo "$1" >> "$applied"; }
}

# Workloads and the API server are the cluster side of the core, replaced by recorders.
calls=()
shed_workloads() { calls+=(shed); }
restore_workloads() { calls+=(restore); }
wait_for_api() { calls+=(wait); }

actuator_a
load_levels
check "the levels are the actuator's, in order" test "${LEVELS[*]}" = "full control-plane etcd-only"
check "the first step down" test "$(next_level_down full)" = control-plane
check "the second step down" test "$(next_level_down control-plane)" = etcd-only
check_not "nowhere to go from the last level" next_level_down etcd-only
check "the first step up" test "$(next_level_up etcd-only)" = control-plane
check "the second step up" test "$(next_level_up control-plane)" = full
check_not "nowhere to go from the first level" next_level_up full
check_not "a level the actuator does not have has no next" next_level_down unheard-of
actuator_b
load_levels
check "another actuator's levels are used as they are" test "$(next_level_down alpha)" = beta

# Order of a step: workloads are shed before anything is applied, and a failed drain stops the step.
actuator_a
: > "$applied"
calls=()
# The fake actuator records into the same list as the cluster recorders, to show the order of a step.
apply_level() { calls+=("apply:$1"); }
step_down full control-plane "test reading" > /dev/null
check "a step sheds, then applies" test "${calls[*]}" = "shed apply:control-plane"

calls=()
step_down control-plane etcd-only "test reading" > /dev/null
check "the step that switches the API server off sheds again first" test "${calls[*]}" = "shed apply:etcd-only"

calls=()
shed_workloads() { calls+=(shed-failed); return 1; }
failing_step() { step_down control-plane etcd-only "test reading" > /dev/null; }
check_not "a failed drain fails the step" failing_step
check "a failed drain applies nothing" test "${calls[*]}" = "shed-failed"
shed_workloads() { calls+=(shed); }

failing_apply() { step_down full control-plane "test reading" > /dev/null; }
apply_level() { calls+=("apply-failed:$1"); return 1; }
calls=()
check_not "a level that cannot be applied fails the step" failing_apply

# Stepping up applies the level above and restores the node's workloads only on reaching the first level.
actuator_a
: > "$applied"
calls=()
echo etcd-only > "$work/level-a"
cmd_up > /dev/null
check "up from the last level applies the level above" test "$(cat "$applied")" = control-plane
check "up to a middle level does not restore workloads" test "${calls[*]}" = ""
cmd_up > /dev/null
check "up to the first level waits for the API and restores workloads" test "${calls[*]}" = "wait restore"
up_at_top() { (cmd_up > /dev/null 2>&1); }
check_not "up at the first level is refused" up_at_top
: > "$applied"
calls=()
cmd_down > /dev/null
check "down by hand sheds, then applies the level below" test "${calls[*]}-$(cat "$applied")" = "shed-control-plane"
cmd_down > /dev/null
down_at_bottom() { (cmd_down > /dev/null 2>&1); }
check_not "down at the last level is refused" down_at_bottom
unknown_level() { (current_level() { echo unknown; }; cmd_down > /dev/null 2>&1); }
check_not "down from an unknown level is refused" unknown_level

# The core decides the same whatever it drives. The same readings are fed to each actuator, one sample every SAMPLE_SECONDS: one under the limit, then over it. Over for the sustain period gives the first step; the second needs both a fresh streak and the cooldown since the first; and there is nothing after the last level. The expected steps are worked out by hand from the settings above: the streak starts at the second sample (60 s), the first step is at 180 s, a new streak starts at 210 s, and 1080 s is the first moment 900 s have passed since the first step.
run_scenario() {
  local sample uptime
  : > "$applied"
  calls=()
  WATCH_ABOVE_SINCE=''
  WATCH_LAST_STEP=''
  WATCH_WARNED=''
  for sample in $(seq 1 60); do
    uptime=$((sample * 30))
    printf '%s 0.1\n' "$uptime" > "$UPTIME_FILE"
    if [ "$sample" -eq 1 ]; then
      printf 'some avg10=0 avg60=0 avg300=2.00 total=1\n' > "$PSI_FILE"
      printf 'MemTotal: %s kB\nMemAvailable: 900000 kB\n' "$total" > "$MEMINFO_FILE"
    else
      printf 'some avg10=0 avg60=0 avg300=20.00 total=1\n' > "$PSI_FILE"
      printf 'MemTotal: %s kB\nMemAvailable: 90000 kB\n' "$total" > "$MEMINFO_FILE"
    fi
    before=$(wc -l < "$applied" | tr -d ' ')
    watch_sample > /dev/null
    after=$(wc -l < "$applied" | tr -d ' ')
    if [ "$after" -gt "$before" ]; then
      printf '%s:%s ' "$sample" "$(position_of_last_applied)"
    fi
  done
}
position_of_last_applied() {
  level_position "$(tail -n 1 "$applied")"
}
shed_workloads() { calls+=(shed); }

actuator_a; load_levels; steps_a=$(run_scenario)
actuator_b; load_levels; steps_b=$(run_scenario)
actuator_c; load_levels; steps_c=$(run_scenario)
check "actuator A steps at the worked-out moments" test "$steps_a" = "6:1 36:2 "
check "actuator B, with other names and another way of keeping its level, decides identically" test "$steps_b" = "$steps_a"
check "actuator C, with only two levels, steps once at the same moment and then has nowhere to go" test "$steps_c" = "6:1 "

# A step ends the streak that caused it: the next step needs a fresh sustained period of its own, not only the cooldown. That shows only when the sustain period is longer than the cooldown, so the settings are changed for this run: the streak starts at 60 s, the first step is at 660 s, a new streak starts at 690 s and the second step is at 1290 s, where without the reset it would come at 870 s.
SUSTAIN_SECONDS=600
COOLDOWN_SECONDS=200
actuator_a; load_levels; steps_long_sustain=$(run_scenario)
SUSTAIN_SECONDS=120
COOLDOWN_SECONDS=900
check "a step starts a fresh streak" test "$steps_long_sustain" = "22:1 43:2 "

# The coordination reaches the watch loop: with another etcd-voting node down, the same readings that stepped before lead to no step at all, and with the lease held by another node likewise.
printf '{"items":[{"metadata":{"name":"peer-a"},"status":{"conditions":[{"type":"Ready","status":"False"}]}},{"metadata":{"name":"testnode"},"status":{"conditions":[{"type":"Ready","status":"True"}]}}]}\n' > "$FAKE_DIR/nodes.json"
actuator_a; load_levels; steps_peer_down=$(run_scenario)
check "a peer down: the watch loop never steps" test "$steps_peer_down" = ""
printf '{"items":[{"metadata":{"name":"peer-a"},"status":{"conditions":[{"type":"Ready","status":"True"}]}},{"metadata":{"name":"testnode"},"status":{"conditions":[{"type":"Ready","status":"True"}]}}]}\n' > "$FAKE_DIR/nodes.json"
build_lease other-node 1000000000 1000000000 3 > "$FAKE_DIR/lease.json"
actuator_a; load_levels; steps_lease_held=$(run_scenario)
check "the lease held elsewhere: the watch loop never steps" test "$steps_lease_held" = ""
rm -f "$FAKE_DIR/lease.json"

# A system that is none of the levels is left alone.
actuator_a
echo unknown > "$work/level-a"
: > "$applied"
watch_sample > /dev/null
check "an unknown level is not stepped" test ! -s "$applied"

finish
