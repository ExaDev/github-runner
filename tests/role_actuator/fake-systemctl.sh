#!/usr/bin/env bash
# A stand-in for systemctl for fleet-role-actuator's tests, keeping a native host's units in the directory FAKE_HOST. The k3s server unit is tests/regrade/fake-systemctl.sh's (FAKE_REGRADE_SYSTEMCTL, which this file runs for it): `active` while it runs, its invocations and journal, and its ExecStartPre (FAKE_PRESTART) run at every start. Every other unit is `active-<unit>` while it runs and `enabled-<unit>` while it is enabled. A start of either k3s unit first runs its ExecCondition, the real files/native/fleet-role-is.sh (FAKE_ROLE_IS) with the role file FAKE_ROLE_FILE and the server directory FAKE_SERVER_DIR as the units pass them, and is skipped, as systemd skips it, when that fails; a start that would leave both k3s units running is recorded in FAKE_HOST/both-running. Each call is recorded in FAKE_HOST/units, and each start as `<unit> started` or `<unit> skipped by its condition`.
set -u

host=${FAKE_HOST:?FAKE_HOST must be set}
echo "$*" >> "$host/units"

active() {
  if [ "$1" = k3s ]; then [ -f "$host/active" ]; else [ -f "$host/active-$1" ]; fi
}

# What systemd does for a start of the unit $1: nothing for one already running, its condition, then the unit.
start() {
  local unit=$1 other
  active "$unit" && return 0
  case $unit in
    k3s)
      if ! "${FAKE_ROLE_IS:?}" server "${FAKE_ROLE_FILE:?}" "${FAKE_SERVER_DIR:?}" 2> /dev/null; then
        echo "$unit skipped by its condition" >> "$host/units"
        return 0
      fi
      other=k3s-agent
      ;;
    k3s-agent)
      if ! "$FAKE_ROLE_IS" agent "$FAKE_ROLE_FILE" 2> /dev/null; then
        echo "$unit skipped by its condition" >> "$host/units"
        return 0
      fi
      other=k3s
      ;;
    *) other='' ;;
  esac
  if [ -n "$other" ] && active "$other"; then echo "$unit started while $other runs" >> "$host/both-running"; fi
  if [ "$unit" = k3s ]; then
    "${FAKE_REGRADE_SYSTEMCTL:?}" start k3s || return
  else
    touch "$host/active-$unit"
  fi
  echo "$unit started" >> "$host/units"
}

stop() {
  if [ "$1" = k3s ]; then
    active k3s || return 0
    "$FAKE_REGRADE_SYSTEMCTL" stop k3s
  else
    rm -f "$host/active-$1"
  fi
}

command=${1:-}
shift
now=''
quiet=''
units=()
for argument in "$@"; do
  case $argument in
    --now) now=1 ;;
    --quiet) quiet=1 ;;
    *) units+=("$argument") ;;
  esac
done
unit=${units[0]:-}

case $command in
  is-active)
    if active "$unit"; then [ -n "$quiet" ] || echo active; exit 0; fi
    [ -n "$quiet" ] || echo inactive
    exit 3
    ;;
  is-enabled) [ -f "$host/enabled-$unit" ] ;;
  enable)
    touch "$host/enabled-$unit"
    [ -z "$now" ] || start "$unit"
    ;;
  disable)
    rm -f "$host/enabled-$unit"
    [ -z "$now" ] || stop "$unit"
    ;;
  start) start "$unit" ;;
  stop) stop "$unit" ;;
  restart)
    if [ "$unit" = k3s ]; then
      # systemd runs the condition of a restart's start too: a server whose role file no longer names it stops and stays down.
      stop k3s
      start k3s
    else
      stop "$unit"
      start "$unit"
    fi
    ;;
  daemon-reload) ;;
  show) exec "$FAKE_REGRADE_SYSTEMCTL" show "$@" ;;
  *)
    echo "unexpected systemctl call: $command $*" >&2
    exit 2
    ;;
esac
