#!/usr/bin/env bash
# A stand-in for k3s for fleet-role-actuator's tests: `k3s kubectl` is the kubectl on the PATH (the test makes it tests/role_actuator/fake-kubectl.sh), and `k3s etcd-snapshot save` records its arguments in FAKE_HOST/snapshots and logs, as k3s logs through logrus, the one file the server created, named as k3s names an on-demand snapshot (the given name, the node and a time). FAKE_SNAPSHOT_FAILS makes the save fail as k3s does when the server refuses it.
set -u

host=${FAKE_HOST:?FAKE_HOST must be set}
case "${1:-} ${2:-}" in
  "kubectl "*)
    shift
    exec kubectl "$@"
    ;;
  "etcd-snapshot save")
    shift 2
    echo "$*" >> "$host/snapshots"
    if [ -n "${FAKE_SNAPSHOT_FAILS:-}" ]; then
      echo 'time="2026-01-01T00:00:00Z" level=fatal msg="see server log for details: Internal Server Error"' >&2
      exit 1
    fi
    name=''
    while [ $# -gt 0 ]; do
      if [ "$1" = --name ]; then name=$2; fi
      shift
    done
    echo "time=\"2026-01-01T00:00:00Z\" level=info msg=\"Snapshot ${name}-testnode-1767225600 saved.\"" >&2
    ;;
  *)
    echo "unexpected k3s call: $*" >&2
    exit 2
    ;;
esac
