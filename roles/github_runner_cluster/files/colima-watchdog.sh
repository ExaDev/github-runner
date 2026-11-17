#!/bin/bash
# Restarts a Colima VM whose container runtime has stopped answering. One launchd tick runs it once (templates/colima-watchdog-launchd.plist.j2), and a tick that finds the runtime healthy does nothing and prints nothing.
#
# A VZ-backed VM can stop with no crash report while `colima list` still says Running: the Docker socket then never answers and the node's containers die with it. tasks/runtime_wedged.yml deals with that, but only when the playbook runs, so a VM that wedges between runs stays down. This script applies the same evidence continuously: it acts only when every one of these holds, so a loaded host, a restarting daemon and a VM that is still booting are left alone.
#
# 1. `docker info` failed on WATCHDOG_FAILURES consecutive runs (the count is kept in the state directory, and any answer resets it);
# 2. Colima lists the profile as Running and all of these hold: a command in the guest (`colima ssh -- true`) fails within WATCHDOG_PROBE_TIMEOUT seconds; the VM's host agent (limactl hostagent, named by the pid file in the profile's Lima directory) has been running for more than WATCHDOG_BOOT_GRACE seconds, so a VM that was just restarted is never restarted again at once; and the Docker context the probe used points at this profile's socket, so a failing probe of another runtime never restarts this VM;
# 3. the restart log allows it: at most WATCHDOG_MAX_RESTARTS in the last hour, and the gap since the previous one is at least WATCHDOG_MIN_GAP seconds doubled for every further restart in that hour.
#
# A profile Colima lists as Stopped is left alone: the launchd job that starts it at boot or login owns that case, and a stop that the role's own resize or an operator made on purpose must not be undone from here. A restart is `colima stop` (then `colima stop --force` when that fails or times out) and `colima start --activate=false`, each bounded by run_bounded.pl, which sits beside this script. A restart stops every container on the host, which then come back with the VM (restart: unless-stopped). Every decision that is not "healthy" is one line on standard output, which launchd writes to the job's log.
#
# Settings come from the environment: WATCHDOG_PROFILE and WATCHDOG_STATE_DIR (required), COLIMA_HOME, DOCKER_CONTEXT, and the WATCHDOG_* numbers below, each with the default shown. Exit status: 0 healthy or nothing to do yet, 1 a restart was needed and refused or failed, 2 misuse or a supervisor that could not run.
set -u

: "${WATCHDOG_PROFILE:?WATCHDOG_PROFILE is required}"
: "${WATCHDOG_STATE_DIR:?WATCHDOG_STATE_DIR is required}"
FAILURES="${WATCHDOG_FAILURES:-3}"
PROBE_TIMEOUT="${WATCHDOG_PROBE_TIMEOUT:-20}"
KILL_GRACE="${WATCHDOG_KILL_GRACE:-5}"
BOOT_GRACE="${WATCHDOG_BOOT_GRACE:-600}"
CLEAN_STOP_TIMEOUT="${WATCHDOG_CLEAN_STOP_TIMEOUT:-120}"
START_TIMEOUT="${WATCHDOG_START_TIMEOUT:-900}"
MAX_RESTARTS="${WATCHDOG_MAX_RESTARTS:-3}"
MIN_GAP="${WATCHDOG_MIN_GAP:-600}"
WINDOW=3600

here="$(cd "$(dirname "$0")" && pwd)"
bounded="$here/run_bounded.pl"
failures_file="$WATCHDOG_STATE_DIR/failures"
restarts_file="$WATCHDOG_STATE_DIR/restarts"

log() {
  printf '%s colima-watchdog[%s]: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$WATCHDOG_PROFILE" "$*"
}

# Runs a command with a deadline (seconds) that holds whatever the command does with signals.
bounded_run() {
  local deadline="$1"
  shift
  perl "$bounded" "$deadline" "$KILL_GRACE" "$@"
}

# True when a status from bounded_run says the supervisor, not the command, failed (127 could not start, 255 misused).
supervisor_failed() {
  [ "$1" -eq 127 ] || [ "$1" -eq 255 ]
}

mkdir -p "$WATCHDOG_STATE_DIR" || exit 2
touch "$restarts_file" || exit 2

bounded_run "$PROBE_TIMEOUT" docker info --format '{{.ServerVersion}}' >/dev/null 2>&1
probe=$?
if supervisor_failed "$probe"; then
  log "run_bounded.pl could not run docker info (exit $probe); this says nothing about the VM"
  exit 2
fi
if [ "$probe" -eq 0 ]; then
  rm -f "$failures_file"
  exit 0
fi

count=0
if [ -f "$failures_file" ]; then
  count="$(cat "$failures_file")"
fi
case "$count" in '' | *[!0-9]*) count=0 ;; esac
count=$((count + 1))
printf '%s\n' "$count" >"$failures_file"
if [ "$count" -lt "$FAILURES" ]; then
  log "docker info failed ($count of $FAILURES consecutive runs before acting)"
  exit 0
fi

# What Colima says about the profile (one JSON object per line): Running, Stopped, or another word.
status="$(bounded_run "$PROBE_TIMEOUT" colima list --json 2>/dev/null | grep -F "\"name\":\"$WATCHDOG_PROFILE\"" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p' | head -n 1)"

colima_home="${COLIMA_HOME:-$HOME/.colima}"
case "$status" in
  Running)
    bounded_run "$PROBE_TIMEOUT" colima ssh --profile "$WATCHDOG_PROFILE" -- true >/dev/null 2>&1
    guest=$?
    if supervisor_failed "$guest"; then
      log "run_bounded.pl could not run a command in the guest (exit $guest); this says nothing about the VM"
      exit 2
    fi
    if [ "$guest" -eq 0 ]; then
      log "docker info failed on $count consecutive runs but a command in the guest works; leaving the VM alone"
      exit 0
    fi

    instance="colima"
    [ "$WATCHDOG_PROFILE" = "default" ] || instance="colima-$WATCHDOG_PROFILE"
    pid="$(tr -d '[:space:]' <"$colima_home/_lima/$instance/ha.pid" 2>/dev/null)"
    case "$pid" in '' | *[!0-9]*) pid=0 ;; esac
    # ps prints [[days-]hours:]minutes:seconds and then the command. A dead pid, another program or an unreadable time leaves the age unknown (-1), which never counts as old.
    age=-1
    read -r etime cmdline <<EOF
$(ps -o etime= -o command= -p "$pid" 2>/dev/null)
EOF
    program="${cmdline%% *}"
    case "$cmdline" in
      *" hostagent"*) is_agent=1 ;;
      *) is_agent=0 ;;
    esac
    if [ -n "${etime:-}" ] && [ "${program##*/}" = "limactl" ] && [ "$is_agent" -eq 1 ]; then
      days=0
      clock="$etime"
      case "$etime" in *-*) days="${etime%%-*}" clock="${etime#*-}" ;; esac
      seconds=0
      valid=1
      old_ifs="$IFS"
      IFS=:
      for part in $clock; do
        case "$part" in '' | *[!0-9]*) valid=0 ;; esac
        [ "$valid" -eq 1 ] && seconds=$((seconds * 60 + 10#$part))
      done
      IFS="$old_ifs"
      case "$days" in '' | *[!0-9]*) valid=0 ;; esac
      [ "$valid" -eq 1 ] && age=$((10#$days * 86400 + seconds))
    fi
    if [ "$age" -le "$BOOT_GRACE" ]; then
      log "docker info failed on $count consecutive runs and the guest does not answer, but the VM's host agent has run for $([ "$age" -lt 0 ] && echo 'a time that could not be read' || echo "$age seconds") (not more than $BOOT_GRACE); leaving the VM alone"
      exit 0
    fi

    context="${DOCKER_CONTEXT:-$(docker context show 2>/dev/null)}"
    endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}' "$context" 2>/dev/null)"
    if [ "$endpoint" != "unix://$colima_home/$WATCHDOG_PROFILE/docker.sock" ]; then
      log "docker info failed on $count consecutive runs, but the Docker context '$context' does not point at the socket of this profile; leaving the VM alone"
      exit 0
    fi
    report="docker info failed on $count consecutive runs, the guest does not answer and the VM's host agent has run for $age seconds"
    ;;
  *)
    log "docker info failed on $count consecutive runs and Colima lists the profile as '${status:-not listed}'; only a profile listed as Running is restarted"
    exit 0
    ;;
esac

now="$(date +%s)"
recent=0
last=0
kept="$(mktemp "$WATCHDOG_STATE_DIR/restarts.XXXXXX")" || exit 2
while read -r stamp; do
  case "$stamp" in '' | *[!0-9]*) continue ;; esac
  if [ $((now - stamp)) -lt "$WINDOW" ]; then
    printf '%s\n' "$stamp" >>"$kept"
    recent=$((recent + 1))
    [ "$stamp" -gt "$last" ] && last="$stamp"
  fi
done <"$restarts_file"
mv "$kept" "$restarts_file"

if [ "$recent" -ge "$MAX_RESTARTS" ]; then
  log "$report; not restarting, because $recent restarts already ran in the last hour (the cap is $MAX_RESTARTS). Recover by hand: colima stop --profile $WATCHDOG_PROFILE --force && colima start --profile $WATCHDOG_PROFILE"
  exit 1
fi
if [ "$recent" -gt 0 ]; then
  gap=$((MIN_GAP << (recent - 1)))
  if [ $((now - last)) -lt "$gap" ]; then
    log "$report; not restarting yet, because the previous restart was $((now - last)) seconds ago and $gap are required after $recent in the last hour"
    exit 0
  fi
fi

# Recorded before the restart, so that a restart that fails still counts against the cap.
printf '%s\n' "$now" >>"$restarts_file"
rm -f "$failures_file"

log "$report; restarting: colima stop, then start"
bounded_run "$CLEAN_STOP_TIMEOUT" colima stop --profile "$WATCHDOG_PROFILE" >/dev/null 2>&1
stop=$?
if supervisor_failed "$stop"; then
  log "run_bounded.pl could not run colima stop (exit $stop)"
  exit 2
fi
if [ "$stop" -ne 0 ]; then
  log "colima stop exited $stop; stopping without a graceful shutdown"
  colima stop --profile "$WATCHDOG_PROFILE" --force >/dev/null 2>&1
fi

bounded_run "$START_TIMEOUT" colima start --profile "$WATCHDOG_PROFILE" --activate=false >/dev/null 2>&1
start=$?
if [ "$start" -ne 0 ]; then
  log "colima start exited $start; the next run decides again, within the restart cap"
  exit 1
fi
log "restarted"
exit 0
