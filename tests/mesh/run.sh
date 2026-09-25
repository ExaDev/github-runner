#!/usr/bin/env bash
# Mesh integration test for the github_runner_cluster role: stands up three k3s nodes in Docker on one machine, each in its own copy of the Compose project, and has the role join them through Headscale. Scenarios: hosted      headscale_hosted: the role runs Headscale under Compose beside the first node. in_cluster  headscale_in_cluster: the role runs Headscale inside the cluster, on the bootstrap server. existing    headscale_existing: the policy check fails, with the entry to add, against a server whose policy lacks autoApprovers, and passes once it has one. Every scenario uses automatic server selection (three hosts, no overrides, so three servers). The cluster scenarios assert that etcd has three members, that every node is Ready, and that pods on different nodes reach each other over the mesh.
#
# Usage: tests/mesh/run.sh <scenario>... (default: all three). Needs Docker with Compose, and Python 3 with ansible-core (ANSIBLE_PLAYBOOK overrides which ansible-playbook runs). Creates only Docker objects named grtest-*, and removes them again on exit unless GRTEST_KEEP=1.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ansible_playbook="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
network=grtest-mesh
subnet_prefix=172.31.250
headscale_ip="${subnet_prefix}.10"
node_count=3
etcd_image=gcr.io/etcd-development/etcd:v3.5.21
busybox_image=busybox:1.37
work="${GRTEST_WORK:-$(mktemp -d "${TMPDIR:-/tmp}/grtest-mesh.XXXXXX")}"
k3s_token="grtest-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"

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
}

# Each node gets its own cluster directory, which the role fills with the Compose file and k3s build context, plus an override that renames its container, drops the host port every node would otherwise publish, and puts it on the shared test network at a fixed address.
prepare_nodes() {
  local scenario="$1" index dir
  for index in $(seq 1 "$node_count"); do
    dir="${work}/$(node_name "$index")"
    mkdir -p "$dir"
    {
      echo "services:"
      echo "  k3s:"
      echo "    container_name: $(container "$index")"
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
  (cd "${repo_root}/ansible" && ANSIBLE_COLLECTIONS_PATH="${repo_root}/playbooks/collections:${ANSIBLE_COLLECTIONS_PATH:-}" \
    "$ansible_playbook" -i "${work}/inventory.yml" "${repo_root}/tests/mesh/cluster.yml")
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
      kubectl_on 1 exec "$pod" -- wget -q -T 10 -O - "http://${ip}:8080/" </dev/null | grep -q . || fail "pod ${pod} on ${node} cannot reach ${ip} on ${other}"
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

started_at() {
  local index
  for index in $(seq 1 "$node_count"); do docker inspect --format '{{.State.StartedAt}}' "$(container "$index")"; done | tr '\n' ' '
}

scenario_cluster() {
  local scenario="$1"
  reset_environment
  prepare_nodes "$scenario"
  write_inventory "$scenario"
  run_role
  assert_cluster
  log "Rerunning the role, which must not restart any node"
  local before after
  before=$(started_at)
  run_role
  after=$(started_at)
  [ "$before" = "$after" ] || fail "rerunning the role restarted a node container: ${before} -> ${after}"
  assert_cluster
  if [ "$scenario" = in_cluster ]; then
    log "Cold-restarting the bootstrap server, which cannot rejoin a three-server cluster on its own, then recovering it"
    (cd "${repo_root}/ansible" && ANSIBLE_COLLECTIONS_PATH="${repo_root}/playbooks/collections:${ANSIBLE_COLLECTIONS_PATH:-}" \
      "$ansible_playbook" -i "${work}/inventory.yml" "${repo_root}/playbooks/recover_in_cluster_mesh.yml")
    assert_cluster
  fi
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
    # Re-checks a cluster a previous run kept with GRTEST_KEEP=1 and the same GRTEST_WORK, without rebuilding it.
    assert) assert_cluster ;;
    *) fail "unknown scenario ${scenario}" ;;
  esac
  log "Scenario ${scenario} passed"
done
