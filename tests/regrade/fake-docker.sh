#!/usr/bin/env bash
# A stand-in for docker for the Docker actuator's tests, keeping one container in the directory FAKE_NODE: `running` (present while it runs), the files the actuator reads and writes inside it (the test points LEVEL_FILE and STARTED_FILE into this directory, so `docker exec` runs a command on the local filesystem), `started-at` (what `docker inspect` prints), `log` (lines of "<time> <text>", of which `docker logs --since` prints those from the given time on), and `lease-renewed` and `node-ready` (what kubectl reports about the node). `docker restart` does what the container's entrypoint does at a start: it records the requested level as the level k3s started at, and logs that etcd is serving. kubectl calls that are not about the node or its lease go to the kubectl on the PATH, which the test makes fake-kubectl.sh. Every call is recorded in FAKE_NODE/calls. The FAKE_* variables make a restart go wrong: FAKE_RESTART_FAILS, FAKE_NO_ETCD (etcd never serves), FAKE_NODE_STALE (the kubelet never renews its lease), FAKE_NODE_NOT_READY.
set -u

node=${FAKE_NODE:?FAKE_NODE must be set}
echo "$*" >> "$node/calls"

# The fake clock, advanced by $1 seconds, as an RFC 3339 time.
clock() {
  local ticks
  ticks=$(( $(cat "$node/ticks" 2> /dev/null || echo 0) + $1 ))
  echo "$ticks" > "$node/ticks"
  printf '2026-01-01T00:%02d:%02dZ\n' $((ticks / 60)) $((ticks % 60))
}

case ${1:-} in
  exec)
    shift
    [ -f "$node/running" ] || {
      echo "Error response from daemon: container is not running" >&2
      exit 1
    }
    [ "${1:-}" = -i ] && shift
    shift # the container name
    if [ "${1:-}" = kubectl ]; then
      shift
      args=()
      while [ $# -gt 0 ]; do
        case $1 in
          --kubeconfig | --server) shift 2 ;;
          *) args+=("$1"); shift ;;
        esac
      done
      case "${args[0]:-} ${args[1]:-}" in
        "get node") cat "$node/node-ready" ;;
        "get lease") if [ "${args[2]:-}" = --namespace ]; then cat "$node/lease-renewed"; else exec kubectl "${args[@]}"; fi ;;
        *) exec kubectl "${args[@]}" ;;
      esac
    else
      exec "$@"
    fi
    ;;
  restart)
    [ -z "${FAKE_RESTART_FAILS:-}" ] || exit 1
    started=$(clock 10)
    echo "$started" > "$node/started-at"
    touch "$node/running"
    cat "$node/level-file" > "$node/started-file" 2> /dev/null || echo full > "$node/started-file"
    [ -n "${FAKE_NO_ETCD:-}" ] || echo "$started ETCD server is now running" >> "$node/log"
    echo "$started k3s is up and running" >> "$node/log"
    [ -n "${FAKE_NODE_STALE:-}" ] || clock 1 > "$node/lease-renewed"
    [ -z "${FAKE_NODE_NOT_READY:-}" ] && echo True > "$node/node-ready" || echo Unknown > "$node/node-ready"
    ;;
  inspect) cat "$node/started-at" ;;
  logs)
    since=$3
    while read -r time text; do
      [[ $time < $since ]] || echo "$text"
    done < "$node/log"
    ;;
  *)
    echo "unexpected docker call: $*" >&2
    exit 2
    ;;
esac
