#!/usr/bin/env bash
# A stand-in for systemctl for the systemd actuator's tests, keeping one unit, the k3s server, in the directory FAKE_HOST: `active` (present while it runs), `invocation` (the id of its current run, a number that rises with each start), `journal-<id>` (what that run logged), `ticks` (the fake clock that fake-date.sh reads), and `lease-renewed` and `node-ready` (what kubectl reports about the node). A start or restart does what systemd does with the real unit's ExecStartPre: it runs FAKE_PRESTART, the command the test gives it (the real write-regrade-level.sh with the unit's three paths), then logs that etcd is serving and has the kubelet renew its lease a second later. A stop removes the unit's runtime directory, FAKE_RUNTIME_DIR, as RuntimeDirectory= does. Each start also records how many processes were running whose command starts with FAKE_SHIM_PREFIX, so the test can tell what ran across it. Every call is recorded in FAKE_HOST/calls. The FAKE_* variables make a restart go wrong: FAKE_RESTART_FAILS, FAKE_NO_ETCD (etcd never serves), FAKE_NODE_STALE (the kubelet never renews its lease), FAKE_NODE_NOT_READY.
set -u

host=${FAKE_HOST:?FAKE_HOST must be set}
echo "$*" >> "$host/calls"

# The fake clock, advanced by $1 seconds, as an RFC 3339 time.
clock() {
  local ticks
  ticks=$(($(cat "$host/ticks" 2> /dev/null || echo 0) + $1))
  echo "$ticks" > "$host/ticks"
  printf '2026-01-01T00:%02d:%02dZ\n' $((ticks / 60)) $((ticks % 60))
}

start() {
  local invocation processes
  [ -z "${FAKE_RESTART_FAILS:-}" ] || exit 1
  # The listing is read whole first, so the search does not count itself.
  processes=$(ps -e -o args=)
  echo "running shims: $(grep -c "^${FAKE_SHIM_PREFIX:?FAKE_SHIM_PREFIX must be set}" <<< "$processes" || true)" >> "$host/calls"
  sh -c "${FAKE_PRESTART:?FAKE_PRESTART must be set}" || exit 1
  invocation=$(($(cat "$host/invocation" 2> /dev/null || echo 0) + 1))
  echo "$invocation" > "$host/invocation"
  touch "$host/active"
  : > "$host/journal-$invocation"
  clock 10 > /dev/null
  [ -n "${FAKE_NO_ETCD:-}" ] || echo "ETCD server is now running" >> "$host/journal-$invocation"
  echo "k3s is up and running" >> "$host/journal-$invocation"
  [ -n "${FAKE_NODE_STALE:-}" ] || clock 1 > "$host/lease-renewed"
  if [ -z "${FAKE_NODE_NOT_READY:-}" ]; then echo True > "$host/node-ready"; else echo Unknown > "$host/node-ready"; fi
}

case "${1:-} ${2:-}" in
  "is-active --quiet") [ -f "$host/active" ] ;;
  "restart k3s" | "start k3s") start ;;
  "stop k3s")
    rm -f "$host/active"
    rm -rf "${FAKE_RUNTIME_DIR:?FAKE_RUNTIME_DIR must be set}"
    ;;
  "show --property")
    [ "${3:-} ${4:-} ${5:-}" = "InvocationID --value k3s" ] || { echo "unexpected systemctl call: $*" >&2; exit 2; }
    cat "$host/invocation" 2> /dev/null || echo
    ;;
  *)
    echo "unexpected systemctl call: $*" >&2
    exit 2
    ;;
esac
