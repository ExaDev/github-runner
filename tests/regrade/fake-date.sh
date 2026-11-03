#!/usr/bin/env bash
# A stand-in for date for the systemd actuator's tests: the UTC time in RFC 3339 is the fake clock fake-systemctl.sh keeps in FAKE_HOST/ticks, so the time read before a restart and the kubelet's heartbeat after it are on one clock; any other call is the real date's.
set -u

if [ "$*" = "-u +%Y-%m-%dT%H:%M:%SZ" ]; then
  ticks=$(cat "${FAKE_HOST:?FAKE_HOST must be set}/ticks" 2> /dev/null || echo 0)
  printf '2026-01-01T00:%02d:%02dZ\n' $((ticks / 60)) $((ticks % 60))
else
  exec /bin/date "$@"
fi
