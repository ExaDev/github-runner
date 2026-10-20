#!/usr/bin/env bash
# One pass of the node recovery watcher (see roles/github_runner_arc/templates/node-recovery.yaml.j2 and node-recovery/loop.sh, which runs this on an interval).
#
# A node whose kubelet has stopped reporting (Ready condition Unknown) keeps its pods forever: the control plane marks them for deletion once their unreachable toleration runs out, but only the kubelet can confirm a deletion, so they stay Terminating. A replacement that has to wait for the old pod to go, such as an ARC listener or a StatefulSet pod, then never starts. Kubernetes' answer is non-graceful node shutdown: the node.kubernetes.io/out-of-service=nodeshutdown:NoExecute taint makes the control plane evict every pod on the node at once and force-delete the ones already terminating there.
#
# This pass applies that taint to each node whose Ready condition has been Unknown for at least NODE_RECOVERY_AFTER_SECONDS, and removes it again once the node reports Ready. It only removes a taint it applied itself, recognised by the NODE_RECOVERY_ANNOTATION annotation it writes with it, so an operator's own out-of-service taint is left alone. A node reporting Ready False is never tainted: its kubelet is alive and still finishes its pods' deletion itself. The node this pass runs on (NODE_RECOVERY_SELF_NODE) is never tainted either. Each change is one JSON patch that first tests the node's resourceVersion, so a node whose status changed after it was read (for example because it came back) is left for the next pass instead of being tainted on stale information. Without the prune pass below it never deletes a Node object, and needs only get, list and patch on nodes.
#
# An opt-in second pass deletes a Node object that has stayed Unknown for at least NODE_RECOVERY_PRUNE_AFTER_SECONDS (empty turns it off) and carries the label NODE_RECOVERY_PRUNE_LABEL with the value true, which a node that rejoins by itself, such as a k3s agent, sets on itself when it registers. Deleting a Node removes it, and the pods bound to it, from the cluster; an agent that comes back registers a new Node object. A k3s server's Node is never deleted, because that removes its etcd member: a node that is a control-plane or etcd member is refused even when it carries the label, and the pass fails saying so. Nothing is deleted either unless at least min_etcd_voters Ready nodes carrying the etcd role label would remain (a non-voting agent does not count), so a cluster with fewer voters than quorum needs, or with none, is left alone and the pass fails saying so. An unlabelled node, a node without a Ready condition and the node this pass runs on (NODE_RECOVERY_SELF_NODE) are never deleted. The delete is one API request carrying the node's uid and resourceVersion as preconditions, which kubectl delete cannot send, so a node that changed after it was read (for example because it came back) is left for the next pass. This pass then needs delete on nodes as well as curl, which reaches the API with the pod's ServiceAccount credentials (NODE_RECOVERY_API_URL and NODE_RECOVERY_SERVICEACCOUNT_DIR override where it looks).
set -euo pipefail

after="${NODE_RECOVERY_AFTER_SECONDS:?NODE_RECOVERY_AFTER_SECONDS must be set}"
annotation="${NODE_RECOVERY_ANNOTATION:-github-runner.exadev/out-of-service-applied-at}"
self_node="${NODE_RECOVERY_SELF_NODE:-}"
prune_after="${NODE_RECOVERY_PRUNE_AFTER_SECONDS:-}"
prune_label="${NODE_RECOVERY_PRUNE_LABEL:-github-runner.exadev.io/removable}"
# A cluster needs three voting etcd members to tolerate losing one: with two, losing either leaves one of two, which is not a majority. k3s removes a server's etcd member when its Node is deleted, so the prune pass deletes nothing unless at least this many Ready etcd members would remain.
readonly min_etcd_voters=3
taint_key="node.kubernetes.io/out-of-service"
taint_value="nodeshutdown"

if ! [[ "$after" =~ ^[0-9]+$ ]]; then
  echo "NODE_RECOVERY_AFTER_SECONDS must be a whole number of seconds, not '${after}'" >&2
  exit 1
fi

if [ -n "$prune_after" ]; then
  if ! [[ "$prune_after" =~ ^[1-9][0-9]*$ ]]; then
    echo "NODE_RECOVERY_PRUNE_AFTER_SECONDS must be empty or a whole number of seconds, at least 1, not '${prune_after}'" >&2
    exit 1
  fi
  # At or below the taint threshold, a node would be deleted in the pass that would have tainted it, before the taint had cleared its pods.
  if [ "$prune_after" -le "$after" ]; then
    echo "NODE_RECOVERY_PRUNE_AFTER_SECONDS (${prune_after}) must be greater than NODE_RECOVERY_AFTER_SECONDS (${after})" >&2
    exit 1
  fi
fi

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

nodes="$(kubectl get nodes -o json)"
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# One JSON object per node that needs a change: its name, the action, how long it has been Unknown, and the patch to send (or, for a delete, the uid and resourceVersion to send as preconditions).
plan="$(jq -c \
  --argjson after "$after" \
  --argjson min_voters "$min_etcd_voters" \
  --arg prune_after "$prune_after" \
  --arg prune_label "$prune_label" \
  --arg annotation "$annotation" \
  --arg self "$self_node" \
  --arg key "$taint_key" \
  --arg value "$taint_value" \
  --arg now "$now" '
  ($now | fromdateiso8601) as $now_s
  | (if $prune_after == "" then null else ($prune_after | tonumber) end) as $prune_s
  | [.items[]
     | select((.metadata.labels // {}) | has("node-role.kubernetes.io/etcd"))
     | select(((.status.conditions // []) | map(select(.type == "Ready")) | first).status == "True")
     | .metadata.name] as $voters
  | .items[]
  | .metadata.name as $name
  | ((.status.conditions // []) | map(select(.type == "Ready")) | first) as $ready
  | (.spec.taints // []) as $taints
  | ([$taints[] | select(.key == $key)] | length > 0) as $tainted
  | (.metadata.annotations // {}) as $annotations
  | ($annotations | has($annotation)) as $ours
  | (.metadata.labels // {}) as $labels
  | ($labels | has("node-role.kubernetes.io/control-plane") or has("node-role.kubernetes.io/etcd")) as $server
  | {op: "test", path: "/metadata/resourceVersion", value: .metadata.resourceVersion} as $test
  | if $name == $self or $ready == null then empty
    elif $prune_s != null and $ready.status == "Unknown" and $labels[$prune_label] == "true"
         and ($now_s - ($ready.lastTransitionTime | fromdateiso8601)) >= $prune_s then
      {
        name: $name,
        action: (if $server then "refuse" elif (($voters - [$name]) | length) < $min_voters then "floor" else "prune" end),
        voters: (($voters - [$name]) | length),
        unknown_for: ($now_s - ($ready.lastTransitionTime | fromdateiso8601)),
        uid: .metadata.uid,
        resource_version: .metadata.resourceVersion
      }
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

# Deletes a Node object only if its uid and resourceVersion are still the given ones. Returns 0 when it is gone or has changed since it was read, in which case the next pass decides again, and 1 when the API refused or could not be reached.
delete_node() {
  local name="$1" uid="$2" resource_version="$3" unknown_for="$4"
  local sa_dir="${NODE_RECOVERY_SERVICEACCOUNT_DIR:-/var/run/secrets/kubernetes.io/serviceaccount}"
  local host="${KUBERNETES_SERVICE_HOST:-}"
  local api_url="${NODE_RECOVERY_API_URL:-}"
  if [ -z "$api_url" ]; then
    [ -n "$host" ] || { log "Could not delete ${name}: neither NODE_RECOVERY_API_URL nor KUBERNETES_SERVICE_HOST is set" >&2; return 1; }
    # An IPv6 service address needs brackets in a URL.
    [[ "$host" != *:* ]] || host="[${host}]"
    api_url="https://${host}:${KUBERNETES_SERVICE_PORT:-443}"
  fi
  local body response status
  body="$(jq -nc --arg uid "$uid" --arg rv "$resource_version" '{kind: "DeleteOptions", apiVersion: "v1", preconditions: {uid: $uid, resourceVersion: $rv}}')"
  response="$(mktemp)"
  # The token goes to curl on stdin, so it is on no command line.
  if ! status="$(printf 'header = "Authorization: Bearer %s"\n' "$(cat "${sa_dir}/token")" \
      | curl --silent --show-error --config - --cacert "${sa_dir}/ca.crt" --request DELETE \
          --header 'Content-Type: application/json' --data "$body" \
          --output "$response" --write-out '%{http_code}' "${api_url}/api/v1/nodes/${name}")"; then
    log "Could not delete ${name}: the API did not answer; retrying on the next pass" >&2
    rm -f "$response"
    return 1
  fi
  case "$status" in
    2??)
      log "Deleted ${name}: not reporting for ${unknown_for}s, so it is gone from the cluster until it registers again"
      ;;
    404)
      log "${name} is already gone"
      ;;
    409)
      log "${name} changed since it was read; leaving it for the next pass"
      ;;
    *)
      log "Could not delete ${name}: the API answered ${status}: $(jq -r '.message // empty' "$response" 2>/dev/null); retrying on the next pass" >&2
      rm -f "$response"
      return 1
      ;;
  esac
  rm -f "$response"
}

exit_code=0
while IFS= read -r change; do
  [ -n "$change" ] || continue
  name="$(jq -r '.name' <<< "$change")"
  action="$(jq -r '.action' <<< "$change")"
  if [ "$action" = refuse ]; then
    log "Not deleting ${name}: it carries ${prune_label}=true but is a control-plane or etcd member, and deleting a server's Node removes its etcd member; remove the label" >&2
    exit_code=1
    continue
  fi
  if [ "$action" = floor ]; then
    log "Not deleting ${name}: only $(jq -r '.voters' <<< "$change") Ready etcd members would remain, fewer than the ${min_etcd_voters} a cluster needs to tolerate losing one" >&2
    exit_code=1
    continue
  fi
  if [ "$action" = prune ]; then
    delete_node "$name" "$(jq -r '.uid' <<< "$change")" "$(jq -r '.resource_version' <<< "$change")" "$(jq -r '.unknown_for' <<< "$change")" || exit_code=1
    continue
  fi
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
