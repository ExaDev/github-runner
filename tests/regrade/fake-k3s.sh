#!/usr/bin/env bash
# A stand-in for k3s for the systemd actuator's tests: `k3s kubectl` answers what the actuator asks about the node and its lease from the directory FAKE_HOST that fake-systemctl.sh keeps, and passes every other kubectl call to the kubectl on the PATH, which the test makes fake-kubectl.sh. Each call is recorded in FAKE_HOST/calls beside the unit's own, as `kubectl` and its arguments without the options that say where, so the test can read the order of the two.
set -u

host=${FAKE_HOST:?FAKE_HOST must be set}
[ "${1:-}" = kubectl ] || { echo "unexpected k3s call: $*" >&2; exit 2; }
shift
original=("$@")
args=()
while [ $# -gt 0 ]; do
  case $1 in
    --kubeconfig | --server) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
echo "kubectl ${args[*]}" >> "$host/calls"
case "${args[0]:-} ${args[1]:-}" in
  "get node") cat "$host/node-ready" ;;
  "get lease") if [ "${args[2]:-}" = --namespace ]; then cat "$host/lease-renewed"; else exec kubectl "${original[@]}"; fi ;;
  *) exec kubectl "${original[@]}" ;;
esac
