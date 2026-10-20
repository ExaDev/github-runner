#!/usr/bin/env bash
# Mesh integration test for the github_runner_cluster role: stands up three k3s nodes in Docker on one machine, each in its own copy of the Compose project, and has the role join them through Headscale. Scenarios: hosted      headscale_hosted: the role runs Headscale under Compose beside the first node. in_cluster  headscale_in_cluster: the role runs Headscale inside the cluster, on the bootstrap server. existing    headscale_existing: the policy check fails, with the entry to add, against a server whose policy lacks autoApprovers, and passes once it has one. Every scenario uses automatic server selection (three hosts, no overrides, so three servers). compose_faulty  the role refuses a Docker Compose release that recreates the containers it has just built, before writing anything (run it under such a release; it is not in the default set). The cluster scenarios assert that etcd has three members, that every node is Ready, and that pods on different nodes reach each other over the mesh.
#
# With GRTEST_PIN_CONTEXT=1, the cluster scenarios run the role, and the in_cluster recovery playbook, with github_runner_cluster_docker_context pinned while the current Docker context they see points at nothing (tests/lib/docker_context.sh), so any docker call that ignored the pin fails the run; afterwards the current context must be unchanged. context_checks  the role refuses a pinned context that does not exist, and a pinned context alongside DOCKER_HOST, before writing anything (not in the default set).
#
# Usage: tests/mesh/run.sh <scenario>... (default: all three). Needs Docker with Compose, and Python 3 with ansible-core (ANSIBLE_PLAYBOOK overrides which ansible-playbook runs). Creates only Docker objects named grtest-*, and removes them again on exit unless GRTEST_KEEP=1.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=tests/lib/docker_context.sh
. "${repo_root}/tests/lib/docker_context.sh"
pin_context="${GRTEST_PIN_CONTEXT:-0}"
ansible_playbook="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
network=grtest-mesh
subnet_prefix=172.31.250
headscale_ip="${subnet_prefix}.10"
node_count=3
etcd_image=gcr.io/etcd-development/etcd:v3.5.21
busybox_image=busybox:1.37
# Tries per pod pair, 5 seconds apart plus each try's own timeout: about two minutes for the mesh to converge after a restart.
reach_attempts=8
# etcd's timing for every server, in milliseconds: a heartbeat interval and an election timeout of ten times it, above k3s's own defaults and so told apart from them, and above etcd's required five times.
etcd_heartbeat_ms=500
etcd_election_ms=$((etcd_heartbeat_ms * 10))
work="${GRTEST_WORK:-$(mktemp -d "${TMPDIR:-/tmp}/grtest-mesh.XXXXXX")}"
k3s_token="grtest-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
# The Docker CLI configuration the ansible-playbook runs see when the context is pinned; see tests/lib/docker_context.sh.
docker_config="${work}/docker-config"

log() { echo "==> $*"; }
fail() {
  echo "FAIL: $*" >&2
  diagnose >&2 || true
  exit 1
}

# What a failed run needs to be understood without the environment, which is removed on exit: each node's own view of the mesh and the tail of its log.
diagnose() {
  local index
  for index in $(seq 1 "$node_count"); do
    docker inspect "$(container "$index")" >/dev/null 2>&1 || continue
    echo "----- $(container "$index"): tailscale status"
    docker exec "$(container "$index")" tailscale status </dev/null 2>&1 | head -n 20 || true
    echo "----- $(container "$index"): last log lines"
    docker logs --tail 60 "$(container "$index")" 2>&1 | cut -c1-400 || true
  done
  if docker inspect grtest-headscale >/dev/null 2>&1; then
    echo "----- grtest-headscale: nodes and routes"
    docker exec grtest-headscale headscale nodes list </dev/null 2>&1 || true
    docker exec grtest-headscale headscale nodes list-routes </dev/null 2>&1 || true
  fi
}

# The control node reaches a container's published port on the loopback address on macOS, where Docker's container addresses are not routable from the host; on Linux it reaches the container directly.
if [ "$(uname -s)" = Darwin ]; then
  api_on_host=true
else
  api_on_host=false
fi

node_name() { echo "grtest-node$1"; }
node_ip() { echo "${subnet_prefix}.$((10 + $1))"; }
container() { echo "grtest-node$1-k3s"; }

teardown() {
  for index in $(seq 1 "$node_count"); do
    if [ -f "${work}/$(node_name "$index")/docker-compose.yml" ]; then
      docker compose --project-directory "${work}/$(node_name "$index")" -f "${work}/$(node_name "$index")/docker-compose.yml" --profile server down -v --rmi local >/dev/null 2>&1 || true
    fi
  done
  if [ -f "${work}/headscale/docker-compose.yml" ]; then
    docker compose --project-directory "${work}/headscale" -f "${work}/headscale/docker-compose.yml" down -v >/dev/null 2>&1 || true
  fi
  docker rm -f grtest-headscale grtest-hs-existing grtest-etcdctl >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -rf "$work"
}

# Runs ansible-playbook from ansible/, with the throwaway Docker configuration when the context is pinned.
ansible_run() {
  local config=()
  if [ "$pin_context" = 1 ]; then config=("DOCKER_CONFIG=${docker_config}"); fi
  (cd "${repo_root}/ansible" && env "${config[@]}" ANSIBLE_COLLECTIONS_PATH="${repo_root}/playbooks/collections:${ANSIBLE_COLLECTIONS_PATH:-}" "$ansible_playbook" "$@")
}

cleanup() {
  if [ "${GRTEST_KEEP:-}" = 1 ]; then
    log "Keeping the test environment in ${work} (GRTEST_KEEP=1)"
    return
  fi
  log "Removing the test environment"
  teardown
}
trap cleanup EXIT

reset_environment() {
  teardown
  mkdir -p "$work"
  docker network create --subnet "${subnet_prefix}.0/24" "$network" >/dev/null
  if [ "$pin_context" = 1 ]; then
    log "Pinning the Docker context ${grtest_pinned_context}, with the current context pointing at nothing"
    grtest_setup_pinned_contexts "$docker_config"
  fi
}

# Each node gets its own cluster directory, which the role fills with the Compose file and k3s build context, plus an override that drops the host port every node would otherwise publish and puts it on the shared test network at a fixed address. Its container is named by github_runner_cluster_container_name in the inventory.
prepare_nodes() {
  local scenario="$1" index dir
  for index in $(seq 1 "$node_count"); do
    dir="${work}/$(node_name "$index")"
    mkdir -p "$dir"
    {
      echo "services:"
      echo "  k3s:"
      if [ "$scenario" = in_cluster ] && [ "$index" = 1 ] && [ "$api_on_host" = true ]; then
        echo "    ports: !override [\"127.0.0.1:18080:8080\"]"
      else
        echo "    ports: !reset []"
      fi
      echo "    networks:"
      echo "      ${network}:"
      echo "        ipv4_address: $(node_ip "$index")"
      echo "networks:"
      echo "  ${network}:"
      echo "    external: true"
    } > "${dir}/compose.grtest.yml"
  done
}

write_inventory() {
  local scenario="$1" url api_url index
  case "$scenario" in
    hosted) url="http://${headscale_ip}:8080" ;;
    in_cluster) url="http://$(node_ip 1):8080" ;;
  esac
  if [ "$api_on_host" = true ]; then api_url="http://127.0.0.1:18080"; else api_url="$url"; fi
  cat > "${work}/headscale-grtest.yml" <<EOF
services:
  headscale:
    ports: !override ["127.0.0.1:18080:8080"]
    networks:
      ${network}:
        ipv4_address: ${headscale_ip}
networks:
  ${network}:
    external: true
EOF
  {
    echo "all:"
    echo "  vars:"
    echo "    ansible_connection: local"
    echo "    ansible_python_interpreter: $(command -v python3)"
    echo "  children:"
    echo "    github_runner_cluster:"
    echo "      vars:"
    echo "        github_runner_cluster_mesh: headscale_${scenario}"
    echo "        github_runner_cluster_tailnet_domain: mesh.grtest.internal"
    echo "        github_runner_cluster_headscale_url: ${url}"
    echo "        github_runner_cluster_headscale_api_url: ${api_url}"
    echo "        github_runner_cluster_headscale_tls: none"
    echo "        github_runner_cluster_headscale_dir: ${work}/headscale"
    echo "        github_runner_cluster_headscale_container_name: grtest-headscale"
    echo "        github_runner_cluster_headscale_compose_files: [${work}/headscale-grtest.yml]"
    echo "        github_runner_cluster_compose_files: [compose.grtest.yml]"
    echo "        github_runner_cluster_k3s_token: ${k3s_token}"
    echo "        github_runner_cluster_etcd_heartbeat_interval: ${etcd_heartbeat_ms}"
    echo "        github_runner_cluster_etcd_election_timeout: ${etcd_election_ms}"
    if [ "$pin_context" = 1 ]; then
      echo "        github_runner_cluster_docker_context: ${grtest_pinned_context}"
    fi
    # Litestream has its own test (tests/litestream); this one exercises the in-cluster bootstrap on node-local state, which the role only allows once acknowledged.
    if [ "$scenario" = in_cluster ]; then
      echo "        github_runner_cluster_headscale_accept_node_local_state: true"
    fi
    echo "      hosts:"
    for index in $(seq 1 "$node_count"); do
      echo "        $(node_name "$index"):"
      echo "          github_runner_cluster_dir: ${work}/$(node_name "$index")"
      echo "          github_runner_cluster_compose_project: $(node_name "$index")"
      echo "          github_runner_cluster_container_name: $(container "$index")"
    done
  } > "${work}/inventory.yml"
}

run_role() {
  log "Running the cluster role"
  # Returns the playbook's own status, which a caller testing it (compose_faulty) relies on, errexit being off there. Any arguments go to ansible-playbook.
  ansible_run -i "${work}/inventory.yml" "${repo_root}/tests/mesh/cluster.yml" "$@" || return
  if [ "$pin_context" = 1 ]; then grtest_assert_current_context_unchanged "$docker_config" || fail "the role changed the current Docker context"; fi
}

kubectl_on() { docker exec -i "$(container "$1")" kubectl "${@:2}"; }

assert_cluster() {
  local expected_members nodes members index other ip route
  log "Waiting for every node to be Ready"
  local deadline=$((SECONDS + 600))
  until [ "$(kubectl_on 1 get nodes --no-headers 2>/dev/null | grep -c ' Ready ')" = "$node_count" ]; do
    [ "$SECONDS" -lt "$deadline" ] || { kubectl_on 1 get nodes -o wide || true; fail "not every node became Ready"; }
    sleep 5
  done
  kubectl_on 1 get nodes -o wide

  # The entrypoint's wrapper around k3s's own `tailscale up` logs this line after it has waited for the join host; its absence means k3s never ran the wrapper (as when it sat on a noexec tmpfs, where exec.LookPath passes over it), whether or not this control server needed the wait.
  log "Checking each joining server's k3s ran the wait after its own tailscale up"
  for index in $(seq 2 "$node_count"); do
    # grep without -q reads the whole log: with -q it would stop at the first match and, under pipefail, the docker logs it cut off would fail the check.
    docker logs "$(container "$index")" 2>&1 | grep -F "github-runner-node: $(node_name 1).mesh.grtest.internal is a peer and resolves after tailscale up" >/dev/null \
      || fail "$(container "$index") never logged the wait after k3s's tailscale up"
  done

  log "Checking the servers were chosen automatically (all three) and etcd has three members"
  nodes=$(kubectl_on 1 get nodes -l node-role.kubernetes.io/etcd=true --no-headers | wc -l | tr -d ' ')
  [ "$nodes" = "$node_count" ] || fail "expected ${node_count} etcd nodes, found ${nodes}"
  docker rm -f grtest-etcdctl >/dev/null 2>&1 || true
  members=$(docker run --rm --name grtest-etcdctl --network "container:$(container 1)" --volumes-from "$(container 1)" --entrypoint etcdctl "$etcd_image" \
    --endpoints https://127.0.0.1:2379 --cacert /var/lib/rancher/k3s/server/tls/etcd/server-ca.crt \
    --cert /var/lib/rancher/k3s/server/tls/etcd/client.crt --key /var/lib/rancher/k3s/server/tls/etcd/client.key \
    member list -w simple)
  echo "$members"
  expected_members=$(echo "$members" | grep -c ', started, ')
  [ "$expected_members" = "$node_count" ] || fail "expected ${node_count} started etcd members, found ${expected_members}"
  # Every member's peer address must be its mesh (100.64.0.0/10) address, not a container bridge address.
  if echo "$members" | grep -v 'https://100\.' | grep -q ', started, '; then fail "an etcd member advertises a peer address outside the mesh"; fi

  log "Checking pods on different nodes reach each other over the mesh"
  kubectl_on 1 apply -f - <<EOF
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: grtest-echo
  namespace: default
spec:
  selector:
    matchLabels: {app: grtest-echo}
  template:
    metadata:
      labels: {app: grtest-echo}
    spec:
      containers:
        - name: echo
          image: ${busybox_image}
          command: ["sh", "-c", "hostname > /www/index.html && httpd -f -p 8080 -h /www"]
          volumeMounts: [{name: www, mountPath: /www}]
      volumes: [{name: www, emptyDir: {}}]
EOF
  kubectl_on 1 rollout status daemonset/grtest-echo --timeout=300s
  kubectl_on 1 get pods -l app=grtest-echo -o wide
  local pods
  pods=$(kubectl_on 1 get pods -l app=grtest-echo -o jsonpath='{range .items[*]}{.metadata.name},{.spec.nodeName},{.status.podIP}{"\n"}{end}')
  [ "$(echo "$pods" | wc -l | tr -d ' ')" = "$node_count" ] || fail "expected one echo pod per node"
  local pairs=0
  while IFS=, read -r pod node _; do
    while IFS=, read -r _ other ip; do
      [ "$node" = "$other" ] && continue
      reaches "$pod" "$ip" || fail "pod ${pod} on ${node} cannot reach ${ip} on ${other}"
      echo "  ${node} -> ${other} (${ip}) ok"
      pairs=$((pairs + 1))
    done <<< "$pods"
  done <<< "$pods"
  [ "$pairs" = $((node_count * (node_count - 1))) ] || fail "checked ${pairs} node pairs, expected $((node_count * (node_count - 1)))"
  # The route to another node's pods must go through the mesh interface, not the Docker bridge the test nodes share.
  for index in $(seq 1 "$node_count"); do
    while IFS=, read -r _ other ip; do
      [ "$other" = "$(node_name "$index")" ] && continue
      route=$(docker exec "$(container "$index")" ip route get "$ip" </dev/null)
      echo "$route" | grep -q 'tailscale0' || fail "$(node_name "$index") routes ${ip} outside the mesh: ${route}"
    done <<< "$pods"
  done
  log "Pod-to-pod traffic crosses the mesh"
}

# Every server's etcd must have been started with the timing the inventory sets: k3s writes the --etcd-arg values into the config file it starts etcd from.
assert_etcd_timing() {
  local index config
  for index in $(seq 1 "$node_count"); do
    config=$(docker exec "$(container "$index")" cat /var/lib/rancher/k3s/server/db/etcd/config </dev/null) || fail "$(node_name "$index") has no etcd config file"
    echo "$config" | grep -Eq "^heartbeat-interval:.*\\b${etcd_heartbeat_ms}\\b" || { echo "$config"; fail "$(node_name "$index") does not run etcd with a ${etcd_heartbeat_ms} ms heartbeat interval"; }
    echo "$config" | grep -Eq "^election-timeout:.*\\b${etcd_election_ms}\\b" || { echo "$config"; fail "$(node_name "$index") does not run etcd with a ${etcd_election_ms} ms election timeout"; }
  done
}

# The echo pod's container restart count and readiness on a node, as "<count> <ready>".
echo_pod_state() {
  kubectl_on 1 get pods -l app=grtest-echo --field-selector "spec.nodeName=$(node_name "$1")" \
    -o jsonpath='{.items[0].status.containerStatuses[0].restartCount} {.items[0].status.containerStatuses[0].ready}' 2>/dev/null
}

# Restarts a node's container and returns once the cluster has seen it. The API keeps reporting the node and its pods as Ready until the node controller's grace period runs out, so waiting for Ready right after the restart passes at once on the old state, and the pod-to-pod checks then run before the restarted node's pod network exists again. The restart kills the containers of every pod on the node, so the node's echo pod is restarted by its kubelet: the wait is for that pod's restart count to rise and the pod to be ready again, and the checks after it read the pod addresses afresh.
restart_joined_server() {
  local index="$1" before after
  before=$(echo_pod_state "$index") || fail "cannot read the echo pod on $(node_name "$index") before restarting it"
  docker restart "$(container "$index")" >/dev/null
  local deadline=$((SECONDS + 600))
  while :; do
    after=$(echo_pod_state "$index" || true)
    if [ -n "$after" ] && [ "${after% *}" -gt "${before% *}" ] && [ "${after#* }" = true ]; then break; fi
    [ "$SECONDS" -lt "$deadline" ] || { kubectl_on 1 get pods -l app=grtest-echo -o wide || true; fail "the echo pod on $(node_name "$index") did not restart and become ready again after its node restarted (was: ${before}, now: ${after:-unreadable})"; }
    sleep 5
  done
  log "$(node_name "$index") is back: its echo pod restarted (restart count ${before% *} -> ${after% *})"
}

# Whether a pod reaches another pod's echo server, allowing the mesh time to converge: after a node restarts, its peers take a while to learn its new endpoints and routes again.
reaches() {
  local _
  for _ in $(seq 1 "$reach_attempts"); do
    kubectl_on 1 exec "$1" -- wget -q -T 10 -O - "http://$2:8080/" </dev/null 2>/dev/null | grep -q . && return 0
    sleep 5
  done
  return 1
}

started_at() {
  local index
  for index in $(seq 1 "$node_count"); do docker inspect --format '{{.State.StartedAt}}' "$(container "$index")"; done | tr '\n' ' '
}

started_of() { docker inspect --format '{{.State.StartedAt}}' "$(container "$1")"; }

# The containers the role says it would recreate, in container-name order, one space after each; empty when none. Under --check the role's check before its Compose step reports a server whose Compose configuration hash differs from the running container's, so an empty answer on a cluster the role has just started also shows the hash it renders is the one Compose put on the container. The message is joined onto one line first, since the playbook's output format may fold it.
would_recreate() {
  local output
  output=$(ansible_run -i "${work}/inventory.yml" "${repo_root}/tests/mesh/recreate_check.yml" --check "$@" 2>&1) || { echo "$output" >&2; return 1; }
  { echo "$output" | tr '\n' ' ' | tr -s ' ' | grep -o 'Compose would recreate grtest-node[0-9]*-k3s' || true; } | sed 's/^Compose would recreate //' | sort -u | tr '\n' ' '
}

# Returns once the echo pod on a node has restarted since its count was read and is ready again. Recreating a node's container kills every pod container on it, and the API keeps reporting the old pods as running and ready until the kubelet has restarted them, so the pod-to-pod checks would otherwise run against addresses that no longer answer.
wait_for_echo_pod_restart() {
  local index="$1" before="$2" now deadline=$((SECONDS + 600))
  while :; do
    now=$(echo_pod_state "$index" || true)
    if [ -n "$now" ] && [ "${now% *}" -gt "$before" ] && [ "${now#* }" = true ]; then break; fi
    [ "$SECONDS" -lt "$deadline" ] || { kubectl_on 1 get pods -l app=grtest-echo -o wide || true; fail "the echo pod on $(node_name "$index") did not restart and become ready again (was ${before}, now: ${now:-unreadable})"; }
    sleep 5
  done
}

# A joined server's container is recreated because its Compose configuration changed. Its node must rejoin without anyone deleting its node-password Secret, because /etc/rancher/node is a volume that outlives the container, and no other node's container may restart. The change is a label, in a Compose file named only through github_runner_cluster_compose_files; the other nodes get an empty one, so their configuration is unchanged.
recreate_a_joined_server() {
  local target=2 index drift password_before password_after secret_before secret_after echo_restarts_before
  local -a started_before=() started_after=()
  drift='{"github_runner_cluster_compose_files": ["compose.grtest.yml", "compose.grtest-drift.yml"]}'
  log "A cluster the role has just started must have nothing to recreate"
  [ -z "$(would_recreate)" ] || fail "the role would recreate a container it has just started: $(would_recreate)"
  for index in $(seq 1 "$node_count"); do
    if [ "$index" = "$target" ]; then
      printf 'services:\n  k3s:\n    labels:\n      grtest.drift: "1"\n' > "${work}/$(node_name "$index")/compose.grtest-drift.yml"
    else
      printf 'services: {}\n' > "${work}/$(node_name "$index")/compose.grtest-drift.yml"
    fi
    started_before+=("$(started_of "$index")")
  done
  log "Changing $(node_name "$target")'s Compose configuration: the role must say it would recreate that container, and only that one"
  [ "$(would_recreate -e "$drift")" = "$(container "$target") " ] || fail "expected the role to report $(container "$target") alone, it reported: $(would_recreate -e "$drift")"
  secret_before=$(kubectl_on 1 get secret -n kube-system "$(node_name "$target").node-password.k3s" -o jsonpath='{.metadata.uid}')
  [ -n "$secret_before" ] || fail "$(node_name "$target") has no node-password Secret"
  password_before=$(docker exec "$(container "$target")" cat /etc/rancher/node/password </dev/null | cksum)
  echo_restarts_before=$(echo_pod_state "$target" || true)
  [ -n "$echo_restarts_before" ] || fail "cannot read the echo pod on $(node_name "$target") before recreating its server"
  run_role -e "$drift"
  wait_for_echo_pod_restart "$target" "${echo_restarts_before% *}"
  for index in $(seq 1 "$node_count"); do started_after+=("$(started_of "$index")"); done
  for index in $(seq 1 "$node_count"); do
    if [ "$index" = "$target" ]; then
      [ "${started_before[$((index - 1))]}" != "${started_after[$((index - 1))]}" ] || fail "the role did not recreate $(container "$index")"
    else
      [ "${started_before[$((index - 1))]}" = "${started_after[$((index - 1))]}" ] || fail "the role restarted $(container "$index"), which it had no reason to"
    fi
  done
  assert_cluster
  secret_after=$(kubectl_on 1 get secret -n kube-system "$(node_name "$target").node-password.k3s" -o jsonpath='{.metadata.uid}')
  [ "$secret_before" = "$secret_after" ] || fail "$(node_name "$target")'s node-password Secret was deleted and made again"
  password_after=$(docker exec "$(container "$target")" cat /etc/rancher/node/password </dev/null | cksum)
  [ "$password_before" = "$password_after" ] || fail "the recreated container generated a new node password"
  [ -z "$(would_recreate -e "$drift")" ] || fail "the role would still recreate: $(would_recreate -e "$drift")"
  log "$(container "$target") was recreated, kept its node password and rejoined"
}

scenario_cluster() {
  local scenario="$1"
  reset_environment
  prepare_nodes "$scenario"
  write_inventory "$scenario"
  run_role
  assert_cluster
  log "Checking every server's etcd runs with the timing the inventory sets"
  assert_etcd_timing
  log "Rerunning the role, which must not restart any node"
  local before after
  before=$(started_at)
  run_role
  after=$(started_at)
  [ "$before" = "$after" ] || fail "rerunning the role restarted a node container: ${before} -> ${after}"
  assert_cluster
  assert_etcd_timing
  if [ "$scenario" = hosted ]; then
    # A restart starts k3s's temporary etcd against the initialised datastore, which every --etcd-arg also reaches; the timing flags must not break it. The joined server is restarted, not the bootstrap one the Headscale server runs beside.
    log "Restarting a joined server against its initialised datastore, with the etcd timing set"
    restart_joined_server 2
    assert_cluster
    assert_etcd_timing
  fi
  if [ "$scenario" = in_cluster ]; then
    log "Cold-restarting the bootstrap server, which cannot rejoin a three-server cluster on its own, then recovering it"
    ansible_run -i "${work}/inventory.yml" "${repo_root}/playbooks/recover_in_cluster_mesh.yml" \
      || fail "the recovery playbook did not bring the bootstrap server back"
    if [ "$pin_context" = 1 ]; then grtest_assert_current_context_unchanged "$docker_config" || fail "the recovery playbook changed the current Docker context"; fi
    assert_cluster
  fi
  recreate_a_joined_server
}

# One node and only the settings the role reads before its first check fails, for the scenarios that expect it to stop before touching the host.
write_minimal_inventory() {
  local node="$1"
  cat > "${work}/inventory.yml" <<EOF
all:
  vars:
    ansible_connection: local
    ansible_python_interpreter: $(command -v python3)
  children:
    github_runner_cluster:
      vars:
        github_runner_cluster_k3s_token: ${k3s_token}
      hosts:
        ${node}:
          github_runner_cluster_dir: ${work}/${node}
          github_runner_cluster_compose_project: ${node}
EOF
}

# The role must refuse a Docker Compose release that recreates the containers it has just built (see the check in the cluster role's tasks/main.yml) before it writes anything to the host. The workflow installs such a release for this scenario; every other scenario runs under a good one and so also shows the check lets that through.
scenario_compose_faulty() {
  local output node
  reset_environment
  node="$(node_name 1)"
  mkdir -p "${work}/${node}"
  write_minimal_inventory "$node"
  log "The role must refuse Docker Compose $(docker compose version --short) before touching the host"
  if output=$(run_role 2>&1); then
    echo "$output"; fail "the role ran with a Compose release it should refuse"
  fi
  echo "$output" | grep -F "Fail if this host's Docker Compose recreates the containers it has just built" >/dev/null || { echo "$output"; fail "the role failed, but not at the Compose version check"; }
  echo "$output" | grep -F 'Upgrade the Docker Compose plugin on this host' >/dev/null || { echo "$output"; fail "the failure did not say to upgrade Compose"; }
  [ -z "$(ls -A "${work}/${node}")" ] || fail "the role wrote to the cluster directory before refusing: $(ls -A "${work}/${node}")"
  echo "$output" | grep -F 'Upgrade the Docker Compose plugin' | head -n 1
  log "It did"
}

# The role must refuse a pinned context that does not exist, and a pinned context alongside DOCKER_HOST, before it writes anything to the host.
scenario_context_checks() {
  local output node
  reset_environment
  # reset_environment has already made it when every scenario runs pinned.
  [ "$pin_context" = 1 ] || grtest_setup_pinned_contexts "$docker_config"
  node="$(node_name 1)"
  mkdir -p "${work}/${node}"
  write_minimal_inventory "$node"

  log "A pinned context that does not exist must stop the role before it touches the host"
  if output=$(pin_context=1 ansible_run -i "${work}/inventory.yml" "${repo_root}/tests/mesh/cluster.yml" -e github_runner_cluster_docker_context=grtest-missing 2>&1); then
    echo "$output"; fail "the role ran with a pinned context that does not exist"
  fi
  echo "$output" | grep -F "names the Docker context 'grtest-missing', which does not exist" >/dev/null || { echo "$output"; fail "the failure did not say the pinned context is missing"; }
  echo "$output" | grep -F "$grtest_pinned_context" >/dev/null || { echo "$output"; fail "the failure did not list the contexts that do exist"; }
  [ -z "$(ls -A "${work}/${node}")" ] || fail "the role wrote to the cluster directory before refusing: $(ls -A "${work}/${node}")"
  log "It did"

  log "A pinned context alongside DOCKER_HOST must stop the role before it touches the host"
  if output=$(pin_context=1 DOCKER_HOST="unix://${docker_config}/decoy.sock" ansible_run -i "${work}/inventory.yml" "${repo_root}/tests/mesh/cluster.yml" -e "github_runner_cluster_docker_context=${grtest_pinned_context}" 2>&1); then
    echo "$output"; fail "the role ran with both a pinned context and DOCKER_HOST"
  fi
  echo "$output" | grep -F 'but DOCKER_HOST is also set' >/dev/null || { echo "$output"; fail "the failure did not name the conflict with DOCKER_HOST"; }
  [ -z "$(ls -A "${work}/${node}")" ] || fail "the role wrote to the cluster directory before refusing: $(ls -A "${work}/${node}")"
  grtest_assert_current_context_unchanged "$docker_config" || fail "the role changed the current Docker context"
  log "It did"
}

scenario_existing() {
  reset_environment
  local fixtures="${repo_root}/tests/mesh/fixtures/headscale-existing" api_url api_key output
  docker run -d --name grtest-hs-existing --network "$network" --ip "$headscale_ip" -p 127.0.0.1:18081:8080 \
    -v "${fixtures}:/etc/headscale:ro" "headscale/headscale:v$(sed -n 's/^github_runner_cluster_headscale_version: "\(.*\)"$/\1/p' "${repo_root}/roles/github_runner_cluster/defaults/main.yml")" serve >/dev/null
  if [ "$api_on_host" = true ]; then api_url="http://127.0.0.1:18081"; else api_url="http://${headscale_ip}:8080"; fi
  until docker exec grtest-hs-existing headscale health >/dev/null 2>&1; do sleep 1; done
  api_key=$(docker exec grtest-hs-existing headscale apikeys create --expiration 1d | tail -n 1)

  log "headscale_existing against a policy without autoApprovers must fail with the entry to add"
  if output=$(cd "${repo_root}/ansible" && ANSIBLE_COLLECTIONS_PATH="${repo_root}/playbooks/collections:${ANSIBLE_COLLECTIONS_PATH:-}" \
    "$ansible_playbook" -i localhost, "${repo_root}/tests/mesh/headscale_existing.yml" -e "headscale_url=${api_url}" -e "headscale_api_key=${api_key}" -e "ansible_python_interpreter=$(command -v python3)" 2>&1); then
    echo "$output"; fail "the policy check passed without autoApprovers"
  fi
  echo "$output" | grep -F 'does not auto-approve the 10.42.0.0/16 routes' >/dev/null || { echo "$output"; fail "the failure did not say what is missing"; }
  echo "$output" | grep -F '\"autoApprovers\": {' >/dev/null || { echo "$output"; fail "the failure did not include the autoApprovers snippet"; }
  echo "$output" | grep -F '\"10.42.0.0/16\": [' >/dev/null || { echo "$output"; fail "the snippet does not name the pod CIDR"; }
  log "It did"

  log "With autoApprovers added, the check passes and the supplied join key is kept"
  mkdir -p "${work}/hs-fixed"
  cp "${fixtures}/config.yaml" "${work}/hs-fixed/"
  cat > "${work}/hs-fixed/policy.hujson" <<'EOF'
{
  "tagOwners": {"tag:github-runner-node": []},
  "autoApprovers": {"routes": {"10.42.0.0/16": ["tag:github-runner-node"]}},
}
EOF
  docker rm -f grtest-hs-existing >/dev/null
  docker run -d --name grtest-hs-existing --network "$network" --ip "$headscale_ip" -p 127.0.0.1:18081:8080 \
    -v "${work}/hs-fixed:/etc/headscale:ro" "headscale/headscale:v$(sed -n 's/^github_runner_cluster_headscale_version: "\(.*\)"$/\1/p' "${repo_root}/roles/github_runner_cluster/defaults/main.yml")" serve >/dev/null
  until docker exec grtest-hs-existing headscale health >/dev/null 2>&1; do sleep 1; done
  api_key=$(docker exec grtest-hs-existing headscale apikeys create --expiration 1d | tail -n 1)
  local join_key
  join_key=$(docker exec grtest-hs-existing headscale preauthkeys create --reusable --tags tag:github-runner-node --expiration 90d | tail -n 1)
  output=$(cd "${repo_root}/ansible" && ANSIBLE_COLLECTIONS_PATH="${repo_root}/playbooks/collections:${ANSIBLE_COLLECTIONS_PATH:-}" \
    "$ansible_playbook" -i localhost, "${repo_root}/tests/mesh/headscale_existing.yml" -e "headscale_url=${api_url}" -e "headscale_api_key=${api_key}" -e "headscale_join_key=${join_key}" -e "ansible_python_interpreter=$(command -v python3)" 2>&1) || { echo "$output"; fail "the policy check failed with autoApprovers present"; }
  echo "$output" | grep -F "join key prefix: ${join_key:0:17}" >/dev/null || { echo "$output"; fail "the supplied join key was not kept"; }
  log "It did"
}

scenarios=("$@")
[ "${#scenarios[@]}" -gt 0 ] || scenarios=(existing hosted in_cluster)
for scenario in "${scenarios[@]}"; do
  log "Scenario: ${scenario}"
  case "$scenario" in
    hosted | in_cluster) scenario_cluster "$scenario" ;;
    existing) scenario_existing ;;
    compose_faulty) scenario_compose_faulty ;;
    context_checks) scenario_context_checks ;;
    # Re-checks a cluster a previous run kept with GRTEST_KEEP=1 and the same GRTEST_WORK, without rebuilding it.
    assert) assert_cluster ;;
    *) fail "unknown scenario ${scenario}" ;;
  esac
  log "Scenario ${scenario} passed"
done
