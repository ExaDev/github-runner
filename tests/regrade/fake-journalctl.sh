#!/usr/bin/env bash
# A stand-in for journalctl for the systemd actuator's tests: prints what one run of the unit logged, from the directory FAKE_HOST that fake-systemctl.sh keeps, for the only query the actuator makes, a run's lines by its invocation id with no metadata.
set -u

host=${FAKE_HOST:?FAKE_HOST must be set}
echo "$*" >> "$host/calls"
[ "${1:-} ${2:-} ${3:-}" = "--no-pager --output cat" ] || { echo "unexpected journalctl call: $*" >&2; exit 2; }
case ${4:-} in
  _SYSTEMD_INVOCATION_ID=?*) cat "$host/journal-${4#_SYSTEMD_INVOCATION_ID=}" 2> /dev/null || true ;;
  *) echo "unexpected journalctl call: $*" >&2; exit 2 ;;
esac
