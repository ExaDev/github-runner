#!/usr/bin/env bash
# A stand-in for kubectl for fleet-role-actuator's tests, keeping a cluster in the directory FAKE_DIR: node.json (this host's Node, as `kubectl get node -o json` prints it) and configmap.json (the actuators' report ConfigMap, absent when there is none), beside what tests/regrade/fake-kubectl.sh keeps (lease.json, unready), to which every other call goes (FAKE_REGRADE_KUBECTL), so the Lease and the API servers' readiness are answered as they are for the regrade core. A patch merges into configmap.json as the API server does for --type merge. Every call is recorded, as that stand-in records them, in FAKE_DIR/calls. FAKE_PATCH_FAILS, or the file FAKE_DIR/patch-fails (which a test can create and remove while the actuator runs), makes every patch fail.
set -u

dir=${FAKE_DIR:?FAKE_DIR must be set}
original=("$@")
args=()
while [ $# -gt 0 ]; do
  case $1 in
    --kubeconfig | --server) shift 2 ;;
    --request-timeout=*) shift ;;
    *) args+=("$1"); shift ;;
  esac
done

case "${args[0]:-} ${args[1]:-}" in
  "get node")
    echo "${args[*]}" >> "$dir/calls"
    cat "$dir/node.json"
    ;;
  "get configmap")
    echo "${args[*]}" >> "$dir/calls"
    if [ -f "$dir/configmap.json" ]; then
      cat "$dir/configmap.json"
    else
      echo "Error from server (NotFound): configmaps \"${args[2]}\" not found" >&2
      exit 1
    fi
    ;;
  "patch configmap")
    echo "${args[*]}" >> "$dir/calls"
    [ -z "${FAKE_PATCH_FAILS:-}" ] && [ ! -e "$dir/patch-fails" ] || { echo "Unable to connect to the server" >&2; exit 1; }
    [ -f "$dir/configmap.json" ] || { echo "Error from server (NotFound): configmaps \"${args[2]}\" not found" >&2; exit 1; }
    patch=''
    for index in "${!args[@]}"; do
      if [ "${args[$index]}" = --patch ]; then patch=${args[$((index + 1))]}; fi
    done
    jq --argjson patch "$patch" '.data = ((.data // {}) + ($patch.data // {}))' "$dir/configmap.json" > "$dir/configmap.json.new"
    mv "$dir/configmap.json.new" "$dir/configmap.json"
    echo "configmap/${args[2]} patched"
    ;;
  *) exec "${FAKE_REGRADE_KUBECTL:?}" "${original[@]}" ;;
esac
