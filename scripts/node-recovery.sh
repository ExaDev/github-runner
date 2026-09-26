#!/usr/bin/env bash
# One pass of the node recovery watcher (see roles/github_runner_arc/templates/node-recovery.yaml.j2 and node-recovery/loop.sh, which runs this on an interval).
#
# A node whose kubelet has stopped reporting (Ready condition Unknown) keeps its pods forever: the control plane marks them for deletion once their unreachable toleration runs out, but only the kubelet can confirm a deletion, so they stay Terminating. A replacement that has to wait for the old pod to go, such as an ARC listener or a StatefulSet pod, then never starts. Kubernetes' answer is non-graceful node shutdown: the node.kubernetes.io/out-of-service=nodeshutdown:NoExecute taint makes the control plane evict every pod on the node at once and force-delete the ones already terminating there.
#
# This pass applies that taint to each node whose Ready condition has been Unknown for at least NODE_RECOVERY_AFTER_SECONDS, and removes it again once the node reports Ready. It only removes a taint it applied itself, recognised by the NODE_RECOVERY_ANNOTATION annotation it writes with it, so an operator's own out-of-service taint is left alone. A node reporting Ready False is never tainted: its kubelet is alive and still finishes its pods' deletion itself. The node this pass runs on (NODE_RECOVERY_SELF_NODE) is never tainted either. Each change is one JSON patch that first tests the node's resourceVersion, so a node whose status changed after it was read (for example because it came back) is left for the next pass instead of being tainted on stale information. It never deletes a Node object, and needs only get, list and patch on nodes.
set -euo pipefail

after="${NODE_RECOVERY_AFTER_SECONDS:?NODE_RECOVERY_AFTER_SECONDS must be set}"
annotation="${NODE_RECOVERY_ANNOTATION:-github-runner.exadev/out-of-service-applied-at}"
self_node="${NODE_RECOVERY_SELF_NODE:-}"
taint_key="node.kubernetes.io/out-of-service"
taint_value="nodeshutdown"

if ! [[ "$after" =~ ^[0-9]+$ ]]; then
  echo "NODE_RECOVERY_AFTER_SECONDS must be a whole number of seconds, not '${after}'" >&2
  exit 1
fi

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

nodes="$(kubectl get nodes -o json)"
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# One JSON object per node that needs a change: its name, the action, how long it has been Unknown, and the patch to send.
plan="$(jq -c \
  --argjson after "$after" \
  --arg annotation "$annotation" \
  --arg self "$self_node" \
  --arg key "$taint_key" \
  --arg value "$taint_value" \
  --arg now "$now" '
  ($now | fromdateiso8601) as $now_s
  | .items[]
  | .metadata.name as $name
  | ((.status.conditions // []) | map(select(.type == "Ready")) | first) as $ready
  | (.spec.taints // []) as $taints
  | ([$taints[] | select(.key == $key)] | length > 0) as $tainted
  | (.metadata.annotations // {}) as $annotations
  | ($annotations | has($annotation)) as $ours
  | {op: "test", path: "/metadata/resourceVersion", value: .metadata.resourceVersion} as $test
  | if $name == $self or $ready == null then empty
    elif $ready.status == "Unknown" and ($tainted | not)
         and ($now_s - ($ready.lastTransitionTime | fromdateiso8601)) >= $after then
      {
        name: $name,
        action: "taint",
        unknown_for: ($now_s - ($ready.lastTransitionTime | fromdateiso8601)),
        patch: [
          $test,
          {op: "add", path: "/metadata/annotations", value: ($annotations + {($annotation): $now})},
          {op: "add", path: "/spec/taints", value: ($taints + [{key: $key, value: $value, effect: "NoExecute", timeAdded: $now}])}
        ]
      }
    elif $ready.status == "True" and $ours then
      {
        name: $name,
        action: "clear",
        patch: [
          $test,
          {op: "add", path: "/metadata/annotations", value: ($annotations | del(.[$annotation]))},
          {op: "add", path: "/spec/taints", value: [$taints[] | select(.key != $key)]}
        ]
      }
    else empty
    end
' <<< "$nodes")"

exit_code=0
while IFS= read -r change; do
  [ -n "$change" ] || continue
  name="$(jq -r '.name' <<< "$change")"
  action="$(jq -r '.action' <<< "$change")"
  patch="$(jq -c '.patch' <<< "$change")"
  if kubectl patch node "$name" --type=json -p "$patch" >/dev/null; then
    if [ "$action" = taint ]; then
      log "Tainted ${name} ${taint_key}=${taint_value}:NoExecute: not reporting for $(jq -r '.unknown_for' <<< "$change")s, so its pods are evicted and force-deleted"
    else
      log "Removed ${taint_key} from ${name}: it reports Ready again"
    fi
  else
    log "Could not ${action} ${name}; retrying on the next pass" >&2
    exit_code=1
  fi
done <<< "$plan"

exit "$exit_code"
