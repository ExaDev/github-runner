# Tests for stepping up by itself in the core of fleet-regrade (files/regrade/fleet-regrade-core.sh): a node that has been calm for long enough goes back up one level at a time, through the same Lease and peer check as a step down, and never without STEP_UP=automatic. Run as `bash tests/regrade/regrade-recover.sh <path to fleet-regrade-core.sh>`. The actuator is a fake, every path the core reads is a fixture, and the two functions that act on a cluster are recorders, so nothing here touches a cluster or the running system.

# The core calls the functions defined here by name, and reads the variables set here, so shellcheck sees them as unused.

# shellcheck disable=SC2329,SC2034
set -euo pipefail

core=${1:?usage: regrade-recover.sh <path to fleet-regrade-core.sh>}
here=$(cd "$(dirname "$0")" && pwd)
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

mkdir -p "$work/bin" "$work/cluster"
cp "$here/fake-kubectl.sh" "$work/bin/kubectl"
export FAKE_DIR=$work/cluster
export FLEET_REGRADE_ENV=$work/env PSI_FILE=$work/psi MEMINFO_FILE=$work/meminfo UPTIME_FILE=$work/uptime

# shellcheck source=/dev/null
. "$here/testlib.sh"
# shellcheck source=/dev/null
. "$core"
now_epoch() { echo 1000000000; }

nodes_json() {
  printf '{"items":[{"metadata":{"name":"peer-a"},"status":{"conditions":[{"type":"Ready","status":"%s"}]}},{"metadata":{"name":"testnode"},"status":{"conditions":[{"type":"Ready","status":"True"}]}}]}\n' "$1" > "$FAKE_DIR/nodes.json"
}
nodes_json True

total=1000000 # kB; the floor is 15 percent of this, 150000 kB

# The readings: calm is pressure under 10 and MemAvailable at or above 150000; over is pressure at or above 10 and MemAvailable under 150000; between is a reading that is neither, in each of the two ways.
calm_pressure=2.00 calm_available=900000
over_pressure=20.00 over_available=90000
check "calm: pressure under the threshold and memory at the floor" is_calm "$calm_pressure" 150000 "$total"
check_not "not calm: pressure at the threshold" is_calm 10 900000 "$total"
check "calm: pressure just under the threshold" is_calm 9.99 900000 "$total"
check_not "not calm: memory just under the floor" is_calm 2 149999 "$total"
check "calm: memory exactly at the floor" is_calm 2 150000 "$total"
check_not "high pressure with plenty of memory is neither over nor calm" bash -c ". '$core' 2>/dev/null; is_over 50 900000 $total || is_calm 50 900000 $total"
check_not "low pressure with little memory is neither over nor calm" bash -c ". '$core' 2>/dev/null; is_over 2 90000 $total || is_calm 2 90000 $total"

applied="$work/applied"
levels() { printf '%s\n' full control-plane etcd-only; }
current_level() { cat "$work/level"; }
apply_level() { echo "$1" > "$work/level"; echo "$1" >> "$applied"; }
# What the core did to the cluster's workloads, in order; a file, since a scenario runs in a subshell.
calls="$work/calls"
shed_workloads() { echo shed >> "$calls"; }
restore_workloads() { echo restore >> "$calls"; }
wait_for_api() { echo wait >> "$calls"; }

# Feeds the core one sample every SAMPLE_SECONDS, the reading for each given by the function named in $1 from the sample number, and returns what it did as "<sample>:<level reached> ...".
run_scenario() {
  local reading=$1 samples=$2 sample uptime before after
  : > "$applied"
  : > "$calls"
  WATCH_ABOVE_SINCE=''
  WATCH_CALM_SINCE=''
  WATCH_LAST_STEP=''
  WATCH_WARNED=''
  echo "${3:-full}" > "$work/level"
  for sample in $(seq 1 "$samples"); do
    uptime=$((sample * 30))
    printf '%s 0.1\n' "$uptime" > "$UPTIME_FILE"
    "$reading" "$sample"
    before=$(wc -l < "$applied" | tr -d ' ')
    watch_sample > /dev/null
    after=$(wc -l < "$applied" | tr -d ' ')
    if [ "$after" -gt "$before" ]; then
      printf '%s:%s ' "$sample" "$(level_position "$(tail -n 1 "$applied")")"
    fi
  done
}
set_reading() {
  printf 'some avg10=0 avg60=0 avg300=%s total=1\n' "$1" > "$PSI_FILE"
  printf 'MemTotal: %s kB\nMemAvailable: %s kB\n' "$total" "$2" > "$MEMINFO_FILE"
}
load_levels

# Over from the second sample to the 70th, then calm. The steps worked out by hand from the settings: the streak starts at 60 s and the first step down is at 180 s (sample 6); a new streak starts at 210 s and the second needs the cooldown, so it is at 1080 s (sample 36). Calm starts at sample 71 (2130 s) and has held for the sustain period at 2250 s, sample 75; the cooldown since the step at 1080 s ended long ago, so that is the first step up. The streak starts afresh at sample 76 (2280 s) and has held for the sustain period at 2400 s, but the cooldown since the step at 2250 s runs to 3150 s, sample 105, which is the second step up.
rise_and_fall() {
  if [ "$1" -eq 1 ]; then set_reading "$calm_pressure" "$calm_available"
  elif [ "$1" -le 70 ]; then set_reading "$over_pressure" "$over_available"
  else set_reading "$calm_pressure" "$calm_available"; fi
}

STEP_UP=automatic
steps=$(run_scenario rise_and_fall 110)
check "down twice under pressure, then up twice once calm, at the worked-out moments" test "$steps" = "6:1 36:2 75:1 105:0 "
check "stepping up to the first level waits for the API and restores the workloads" test "$(tr '\n' ' ' < "$calls")" = "shed shed wait restore "

STEP_UP=manual
steps=$(run_scenario rise_and_fall 110)
check "manual: the same readings never step up" test "$steps" = "6:1 36:2 "
unset STEP_UP
steps=$(run_scenario rise_and_fall 110)
check "unset STEP_UP is manual, as the core was before it existed" test "$steps" = "6:1 36:2 "
STEP_UP=automatic

# The cooldown after a step down holds a step up back: calm straight after the step still waits for it. Over at samples 2 to 6 (one step down, at sample 6, 180 s), calm from sample 7 (210 s); sustained at 330 s; the cooldown ends at 1080 s (sample 36).
quick_recovery() {
  if [ "$1" -ge 2 ] && [ "$1" -le 6 ]; then set_reading "$over_pressure" "$over_available"; else set_reading "$calm_pressure" "$calm_available"; fi
}
steps=$(run_scenario quick_recovery 60)
check "the cooldown after a step down holds the step up back" test "$steps" = "6:1 36:0 "

# A reading between calm and over ends the calm streak, and so cannot step up, but does not step down either.
noisy_recovery() {
  if [ "$1" -eq 1 ]; then set_reading "$calm_pressure" "$calm_available"
  elif [ "$1" -le 6 ]; then set_reading "$over_pressure" "$over_available"
  elif [ $(($1 % 3)) -eq 0 ]; then set_reading 50 "$calm_available"
  else set_reading "$calm_pressure" "$calm_available"; fi
}
steps=$(run_scenario noisy_recovery 80)
check "a reading in between keeps ending the calm streak: no step up, and no step down" test "$steps" = "6:1 "

# The first level has nowhere to go up to, and calm there is not logged as a streak towards anything.
always_calm() { set_reading "$calm_pressure" "$calm_available"; }
steps=$(run_scenario always_calm 100)
check "calm at the first level does nothing" test "$steps" = ""

# A node that starts at the last level steps up through the middle one. Calm from the first sample (30 s) has held for the sustain period at 150 s, sample 5; the cooldown after that step runs to 1050 s, sample 35.
steps=$(run_scenario always_calm 100 etcd-only)
check "from the last level it steps up one at a time" test "$steps" = "5:1 35:0 "

# A step up ends the streak that caused it: the next one needs a fresh calm period of its own, not only the cooldown. That shows only when the sustain period is longer than the cooldown, so the settings are changed for this run: calm from 30 s has held for 600 s at 630 s (sample 21), the first step; the new streak starts at 660 s and holds at 1260 s (sample 42), where without the reset the cooldown, 200 s, would have allowed the second at 840 s (sample 28).
SUSTAIN_SECONDS=600
COOLDOWN_SECONDS=200
steps=$(run_scenario always_calm 60 etcd-only)
SUSTAIN_SECONDS=120
COOLDOWN_SECONDS=900
check "a step up starts a fresh calm streak" test "$steps" = "21:1 42:0 "

# The coordination reaches the step up as it does the step down.
nodes_json False
steps=$(run_scenario always_calm 100 etcd-only)
check "a peer down: the watch loop never steps up" test "$steps" = ""
nodes_json True
build_lease other-node 1000000000 1000000000 3 > "$FAKE_DIR/lease.json"
steps=$(run_scenario always_calm 100 etcd-only)
check "the lease held elsewhere: the watch loop never steps up" test "$steps" = ""
rm -f "$FAKE_DIR/lease.json"

# A level the actuator cannot name is left alone, and a failed step up still starts the cooldown, so it is tried again after it (samples 5, 35, 65 and 95 of 120) and not at every sample.
echo unknown > "$work/level"
: > "$applied"
watch_sample > /dev/null
check "an unknown level is not stepped" test ! -s "$applied"
apply_level() { echo "$1" >> "$applied"; return 1; }
steps=$(run_scenario always_calm 120 etcd-only)
check "a step up that fails is retried only after the cooldown" test "$(wc -l < "$applied" | tr -d ' ')" = 4
apply_level() { echo "$1" > "$work/level"; echo "$1" >> "$applied"; }

# The log says what the watcher is waiting for.
level_log() {
  echo etcd-only > "$work/level"
  printf '%s 0.1\n' 30 > "$UPTIME_FILE"
  set_reading "$calm_pressure" "$calm_available"
  WATCH_CALM_SINCE=''
  watch_sample
}
check "calm is logged at a level that can go up" test "$(level_log | grep -c 'level etcd-only: calm')" = 1
manual_log() { STEP_UP=manual level_log | grep -q 'level etcd-only: calm'; }
check_not "and not when stepping up is by hand" manual_log

finish
