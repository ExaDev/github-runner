#!/usr/bin/env bash
# A stand-in for kubectl for the regrade tests, keeping a cluster in the directory FAKE_DIR: lease.json (the Lease, absent when there is none), nodes.json (what listing the etcd-voting nodes returns) and unready (the API servers, one URL per line, `default` for the kubeconfig's own, whose /readyz does not answer). It understands only what the core asks of it, answers as the real one does for the cases that matter (a Lease that is not found, one that already exists, a replacement of a Lease that has changed), and records every call in FAKE_DIR/calls, without the options that say where and how long, and the API server each call went to (its --server, or `default`) in FAKE_DIR/servers, a line for each call. The FAKE_* variables make a call fail: FAKE_LEASE_READ_ERROR, FAKE_NODES_ERROR, FAKE_CREATE_RACE, FAKE_REPLACE_CONFLICT.
set -u

dir=${FAKE_DIR:?FAKE_DIR must be set}
server=default
args=()
while [ $# -gt 0 ]; do
  case $1 in
    --kubeconfig) shift 2 ;;
    --server) server=$2; shift 2 ;;
    --request-timeout=*) shift ;;
    *) args+=("$1"); shift ;;
  esac
done
echo "${args[*]}" >> "$dir/calls"
echo "$server ${args[*]}" >> "$dir/servers"

if [ "${args[*]}" = "get --raw=/readyz" ] && grep -Fqx -- "$server" "$dir/unready" 2> /dev/null; then
  echo "Unable to connect to the server: dial tcp: i/o timeout" >&2
  exit 1
fi

lease="$dir/lease.json"
case "${args[0]:-} ${args[1]:-}" in
  "get lease")
    if [ -n "${FAKE_LEASE_READ_ERROR:-}" ]; then
      echo "Unable to connect to the server: dial tcp: i/o timeout" >&2
      exit 1
    fi
    if [ -f "$lease" ]; then
      cat "$lease"
    else
      echo 'Error from server (NotFound): leases.coordination.k8s.io "fleet-regrade" not found' >&2
      exit 1
    fi
    ;;
  "create -f")
    body=$(cat)
    if [ -f "$lease" ] || [ -n "${FAKE_CREATE_RACE:-}" ]; then
      echo 'Error from server (AlreadyExists): leases.coordination.k8s.io "fleet-regrade" already exists' >&2
      exit 1
    fi
    jq '.metadata.resourceVersion = "1"' <<< "$body" > "$lease"
    ;;
  "replace -f")
    body=$(cat)
    current=$(jq -r '.metadata.resourceVersion' "$lease" 2> /dev/null || echo none)
    sent=$(jq -r '.metadata.resourceVersion // "none"' <<< "$body")
    if [ -n "${FAKE_REPLACE_CONFLICT:-}" ] || [ "$sent" != "$current" ]; then
      echo 'Error from server (Conflict): Operation cannot be fulfilled on leases.coordination.k8s.io "fleet-regrade": the object has been modified' >&2
      exit 1
    fi
    jq --arg v "$((current + 1))" '.metadata.resourceVersion = $v' <<< "$body" > "$lease"
    ;;
  "get nodes")
    if [ -n "${FAKE_NODES_ERROR:-}" ]; then
      echo "Unable to connect to the server" >&2
      exit 1
    fi
    cat "$dir/nodes.json"
    ;;
  *) ;;
esac
