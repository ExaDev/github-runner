#!/usr/bin/env bash
# Mesh integration test for the github_runner_cluster role: stands up three k3s nodes in Docker on one machine, each in its own copy of the Compose project, and has the role join them through Headscale. Scenarios: hosted      headscale_hosted: the role runs Headscale under Compose beside the first node. in_cluster  headscale_in_cluster: the role runs Headscale inside the cluster, on the bootstrap server. existing    headscale_existing: the policy check fails, with the entry to add, against a server whose policy lacks autoApprovers, and passes once it has one. Every scenario uses automatic server selection (three hosts, no overrides, so three servers). native_server  three Docker servers and a native host (a privileged systemd container, tests/mesh/native/Dockerfile, managed over Ansible's docker connection) that joins as an agent, is then made a server by the inventory and promoted in place: it must join etcd as a learner, be promoted to a voter with its mesh address as its peer URL, and keep the cluster writable throughout, and a rerun must not restart it. role_controller  four hosts (three servers and a standby agent, with etcd's data on memory so the machine's disk does not decide): the role controller's probe and controller run in the cluster from the image built from this checkout, with the manifests the ARC role renders; every host is reported fit from a full window of real samples, with the link the cluster role labelled its node with, a voter made slow with tc netem on its mesh interface is reported unfit with the swap named and every unfit verdict attributed to it, a paused voter makes the controller report that it would not act, and the controller changes nothing (not in the default set; needs jq, and takes a window of 100 samples before it can judge). role_actuator  three Docker servers and a native standby, every host eligible and the role controller on, with the test acting as the controller (holding the regrade Lease and writing the Node's desired-role label and swap annotations by hand): the native host's role actuator promotes it (a learner, then a voter, with writes succeeding throughout), steps it down (its etcd serving at etcd-only with no pod's shim left), and, once its member is removed through k3s's annotation, finishes it as an agent with the same node password and Tailscale address and its datastore moved aside, the two k3s units never running together. role_swap  three Docker servers and an eligible native standby, with the role controller's probe and controller running from this checkout's image and none pinned: the controller's own `swap` command takes the Docker bootstrap host out and the native host in as one swap under the regrade Lease, with writes succeeding throughout, three voters at the end, the demoted node's labels and annotations gone, its datastore moved aside and its identity kept, and a second swap brings the former bootstrap host back into the same etcd cluster. role_swap_paused  the outgoing voter's k3s is paused once the new voter votes: the removal still completes with three voters, and on unpause the tombstone keeps the removed server out of etcd for a learner stall time while its actuator finishes it as an agent. role_swap_blocked_peer  the candidate's etcd peer port is dropped, so its learner never catches up: the controller rolls the promotion back and leaves the members, the voters and the host as they were. role_swap_refused  a voter is paused before the swap is asked for: the command exits 1 naming it and nothing is set. The role_controller scenario also turns the dry run off after the slow voter is reported unfit, and the controller swaps it for the Docker standby. rejoin  three Docker servers whose hosts the inventory marks eligible, so the role mounts each one's role directory: the bootstrap server's etcd member is removed through another server, and restarted on its tombstone it idles instead of joining again; with its datastore moved aside and no server to join it refuses to start rather than bootstrap a second cluster; given a join URL naming another server in its role directory it joins the existing cluster as a learner and is promoted, with the same cluster ID before and after; and a role file naming the agent leaves its server container idle with no k3s. compose_faulty  the role refuses a Docker Compose release that recreates the containers it has just built, before writing anything (run it under such a release; it is not in the default set). host_network  each node runs in a Docker daemon of its own (a docker:dind container standing in for a VM with an address on the LAN, on the shared test network) with github_runner_cluster_network_mode host, joined through a Headscale server the test runs (headscale_existing); the nodes' Tailscale clients must then reach each other directly at those daemons' addresses, and the role must not publish any port; then the role must refuse to move two running nodes to the bridge network in one run, and move one there and back through its guard, the node keeping its mesh address and etcd membership. Linux only, since the control node reaches each daemon at its address on the test network. The cluster scenarios assert that etcd has three members, that every node is Ready, and that pods on different nodes reach each other over the mesh.
#
# With GRTEST_PIN_CONTEXT=1, the cluster scenarios run the role, and the in_cluster recovery playbook, with github_runner_cluster_docker_context pinned while the current Docker context they see points at nothing (tests/lib/docker_context.sh), so any docker call that ignored the pin fails the run; afterwards the current context must be unchanged. context_checks  the role refuses a pinned context that does not exist, and a pinned context alongside DOCKER_HOST, before writing anything (not in the default set).
#
# Usage: tests/mesh/run.sh <scenario>... (default: all three). Needs Docker with Compose, and Python 3 with ansible-core (ANSIBLE_PLAYBOOK overrides which ansible-playbook runs). Creates only Docker objects named grtest-*, and removes them again on exit unless GRTEST_KEEP=1. Set GRTEST_ROLE_IMAGE to a published image reference to have k3s pull it for role_controller and role_swap scenarios instead of building and importing the checkout's image; the registry must be reachable without a pull secret.
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
# Whether the inventory turns memory-pressure regrading on; the regrade scenario sets it.
regrade=false
etcd_image=gcr.io/etcd-development/etcd:v3.5.21
# The Docker daemon each host_network node runs in, standing in for a VM with an address on the LAN.
dind_image=docker:27.5.1-dind
# Set by scenario_host_network: every node's containers are then on that node's own daemon, reached through a Docker context per node.
host_network=0
busybox_image=busybox:1.37
# The native_server scenario's native host: its container, image, address and data volume. Unset (empty native_role) in every other scenario, which then has no native host.
native_host=grtest-native
native_image=grtest-native-host
native_ip="${subnet_prefix}.20"
native_role=""
# Whether the inventory turns memory-pressure regrading on for the native host alone; native_server's last part sets it.
native_regrade=false
# The Docker hosts the inventory lists, by index; all of them unless a scenario leaves one out (native_server's second run, where node 3 keeps running as a voter outside the inventory).
inventory_nodes=""
# The node whose API the swap scenarios read and write through: a node that keeps its server through the scenario (the swap scenarios take node 1, the bootstrap host, out of the cluster). Node 1 unless a scenario says otherwise.
api_node=1
# Whether the inventory pins the first host (role_controller=1 turns the controller on for every host and pins node 1, which a swap of node 1 must not be).
pin_first=1
# Where tc comes from for the role_controller scenario: a throwaway container in a node's network namespace, since the node's own image has none.
tc_image=alpine:3.20
# The probe's period in the role_controller scenario while it fills a window of samples: long enough that a sample's own work (a few kubectl calls) is a small part of it, since a probe that has not reported for three periods counts as silent.
role_controller_sample_seconds=5
# The probe's period in the role_controller_swap scenario and the role_swap ones: the ARC role's own default. A swap holds its steps while any voter's probe is silent, and a probe on a runner with this many k3s nodes under way misses three periods of 5 seconds, which only stalls the swap; the default keeps a silent probe a fault and not a flutter.
swap_sample_seconds=15
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
  local index reports
  for index in $(seq 1 "$node_count"); do
    docker_on "$index" inspect "$(container "$index")" >/dev/null 2>&1 || continue
    echo "----- $(container "$index"): tailscale status"
    docker_on "$index" exec "$(container "$index")" tailscale status </dev/null 2>&1 | head -n 20 || true
    echo "----- $(container "$index"): last log lines"
    docker_on "$index" logs --tail 60 "$(container "$index")" 2>&1 | cut -c1-400 || true
    # Where regrading has the node: how often and when the container started, what it recorded, and which credentials exist (an etcd-only server writes no kubeconfig).
    echo "----- $(container "$index"): regrade state"
    docker_on "$index" inspect --format 'restarts={{.RestartCount}} started={{.State.StartedAt}} running={{.State.Running}}' "$(container "$index")" || true
    # shellcheck disable=SC2016 # the expansions are for the shell in the node's container, not this one.
    docker_on "$index" exec "$(container "$index")" sh -c 'echo "started: $(cat /run/regrade-level 2>&1)"; echo "requested: $(cat /var/lib/rancher/k3s/regrade-level 2>&1)"; ls /etc/rancher/k3s /var/lib/rancher/k3s/server/cred 2>&1' </dev/null 2>&1 | head -n 40 || true
    docker_on "$index" exec "grtest-node${index}-regrade-1" fleet-regrade status </dev/null 2>&1 | head -n 5 || true
    docker_on "$index" logs --tail 40 "grtest-node${index}-regrade-1" 2>&1 | cut -c1-400 || true
    # The role actuator service of a Docker host the role controller may switch: whether it runs (or restarts in a loop) and what its watch logged.
    if docker_on "$index" inspect "grtest-node${index}-role-actuator-1" >/dev/null 2>&1; then
      echo "----- grtest-node${index}-role-actuator-1: state and last log lines"
      docker_on "$index" inspect --format 'restarts={{.RestartCount}} started={{.State.StartedAt}} status={{.State.Status}} exit={{.State.ExitCode}}' "grtest-node${index}-role-actuator-1" || true
      docker_on "$index" logs --tail 60 "grtest-node${index}-role-actuator-1" 2>&1 | cut -c1-400 || true
    fi
    # The agent container a server's host runs once it has been demoted, which the node's own container above is not: whether it runs, whether it reached the mesh, and why it is not Ready.
    if [ "$index" -lt "$agent_from" ] && docker_on "$index" inspect "grtest-node${index}-agent" >/dev/null 2>&1; then
      echo "----- grtest-node${index}-agent: state, tailscale and last log lines"
      docker_on "$index" inspect --format 'restarts={{.RestartCount}} started={{.State.StartedAt}} status={{.State.Status}} exit={{.State.ExitCode}}' "grtest-node${index}-agent" || true
      docker_on "$index" exec "grtest-node${index}-agent" tailscale status </dev/null 2>&1 | head -n 20 || true
      docker_on "$index" logs --tail 80 "grtest-node${index}-agent" 2>&1 | cut -c1-400 || true
    fi
  done
  # The reports every actuator made (JSON of swap, step, phase, role, times and message, no secret), through the first node that answers.
  if [ -n "${platform_namespace:-}" ]; then
    echo "----- the role actuators' reports"
    for index in $(seq 1 "$node_count"); do
      docker_on "$index" inspect "$(container "$index")" >/dev/null 2>&1 || continue
      if reports=$(kubectl_on "$index" --namespace "$platform_namespace" get configmap fleet-role-actuators -o 'jsonpath={.data}' </dev/null 2>&1); then
        echo "$reports" | cut -c1-2000
        break
      fi
    done
    echo "----- etcd membership and per-voter peer metrics"
    # etcdctl's simple output prints member IDs in the same hexadecimal form as the metrics' Remote labels. Read both before teardown so an extra active series can be compared with actual membership.
    etcd_members "${api_node:-1}" 2>&1 || echo "could not read etcd membership"
    for index in $(seq 1 "$node_count"); do
      docker_on "$index" inspect "$(container "$index")" >/dev/null 2>&1 || continue
      if reports=$(kubectl_on "$index" --namespace "$platform_namespace" get pods -l app.kubernetes.io/name=fleet-role-probe -o json </dev/null 2>/dev/null); then
        while IFS=$'\t' read -r probe node; do
          [ -n "$probe" ] || continue
          echo "----- ${node}: etcd server and peer series"
          kubectl_on "$index" --namespace "$platform_namespace" exec "$probe" -- sh -c 'curl -fsS --max-time 5 http://127.0.0.1:2381/metrics | grep -E "^etcd_(server_id|network_active_peers|network_known_peers)(\\{| )"' </dev/null 2>&1 || echo "could not read peer metrics for ${node}"
        done < <(jq -r '.items[] | [.metadata.name, .spec.nodeName] | @tsv' <<< "$reports")
        break
      fi
    done
    echo "----- the role controller's status and log"
    for index in $(seq 1 "$node_count"); do
      docker_on "$index" inspect "$(container "$index")" >/dev/null 2>&1 || continue
      if kubectl_on "$index" --namespace "$platform_namespace" get configmap fleet-role-status -o 'jsonpath={.data.status\.json}' </dev/null 2>/dev/null | jq -c '{swap, waiting, last_swap, request, gates, blocks, decision}' 2>/dev/null | cut -c1-3000; then
        kubectl_on "$index" --namespace "$platform_namespace" logs deployment/fleet-role --tail 80 </dev/null 2>&1 | cut -c1-400 || true
        break
      fi
    done
  fi
  if docker inspect "$native_host" >/dev/null 2>&1; then
    echo "----- ${native_host}: units, role file, Tailscale and the last journal lines"
    docker exec "$native_host" sh -c 'systemctl is-active k3s k3s-agent tailscaled; cat /var/lib/rancher/k3s/fleet-role; tailscale status | head -n 20' </dev/null 2>&1 || true
    docker exec "$native_host" journalctl -u k3s -u k3s-agent -u tailscaled -u fleet-role-actuator --no-pager -n 120 </dev/null 2>&1 | cut -c1-400 || true
  fi
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
# The container of node $1: its k3s server, or from agent_from on, the k3s agent the topology makes of a host beyond the servers (only the role_controller scenario has one).
agent_from=99
container() { if [ "$1" -ge "$agent_from" ]; then echo "grtest-node$1-agent"; else echo "grtest-node$1-k3s"; fi; }
vm() { echo "grtest-vm$1"; }

# The docker CLI for node $1's daemon: the machine's own, or with host_network that node's own daemon through its context.
docker_on() {
  local index="$1"
  shift
  if [ "$host_network" = 1 ]; then docker --context "$(vm "$index")" "$@"; else docker "$@"; fi
}

teardown() {
  for index in $(seq 1 "$node_count"); do
    if [ -f "${work}/$(node_name "$index")/docker-compose.yml" ]; then
      docker compose --project-directory "${work}/$(node_name "$index")" -f "${work}/$(node_name "$index")/docker-compose.yml" --profile server --profile agent --profile regrade down --remove-orphans -v --rmi local || return
    fi
  done
  if [ -f "${work}/headscale/docker-compose.yml" ]; then
    docker compose --project-directory "${work}/headscale" -f "${work}/headscale/docker-compose.yml" down -v >/dev/null 2>&1 || true
  fi
  docker rm -f grtest-headscale grtest-hs-existing grtest-etcdctl "$native_host" >/dev/null 2>&1 || true
  docker volume rm "${native_host}-k3s" >/dev/null 2>&1 || true
  for index in $(seq 1 "$node_count"); do
    docker rm -f -v "$(vm "$index")" >/dev/null 2>&1 || true
    docker context rm -f "$(vm "$index")" >/dev/null 2>&1 || true
  done
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -rf "$work"
}

# Runs ansible-playbook from ansible/, with the throwaway Docker configuration when the context is pinned.
ansible_run() {
  local config=()
  if [ "$pin_context" = 1 ]; then config=("DOCKER_CONFIG=${docker_config}"); fi
  (cd "${repo_root}/ansible" && env "${config[@]}" ANSIBLE_COLLECTIONS_PATH="${repo_root}/playbooks/collections:${ANSIBLE_COLLECTIONS_PATH:-}" "$ansible_playbook" "$@")
}

# The background loop that loads the role controller's image into new node containers, once a scenario has started one.
image_loader_pid=
cleanup() {
  if [ -n "$image_loader_pid" ]; then kill "$image_loader_pid" 2>/dev/null || true; fi
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
      # A host the topology makes an agent runs the k3s-agent service, which needs the same fixed address. Its fdatasync probe directory is on memory for the reason etcd's data is.
      echo "  k3s-agent:"
      if [ "${etcd_on_memory:-0}" = 1 ]; then
        echo "    tmpfs:"
        echo "      - /var/lib/rancher/k3s/fleet-role-probe"
      fi
      echo "    networks:"
      echo "      ${network}:"
      echo "        ipv4_address: $(node_ip "$index")"
      echo "networks:"
      echo "  ${network}:"
      echo "    external: true"
      # etcd's data on memory when asked: the role_controller scenario judges the voters' and the standby's disk against etcd's own limits (a WAL fsync under 10 ms), which a laptop's or a runner's virtual disk does not reliably meet (a laptop's measured 15 to 30 ms), and what it tests is the controller's pipeline, not the disk of the machine it runs on. The server's whole data volume is on memory, rather than a tmpfs mount over server/db in the k3s service alone: the volume is what the role actuator mounts too, so it sees the datastore the server runs on (its member name, k3s's tombstone) and moves that one aside, and it outlives a restart of the server's container while the actuator holds it, as a disk does. A tmpfs mount is emptied at every restart, so a server restarted into etcd-only, or by its restart policy after its member was removed, came back with no datastore and no tombstone and joined etcd again as a new member.
      if [ "${etcd_on_memory:-0}" = 1 ]; then
        echo "volumes:"
        echo "  k3s-data:"
        echo "    driver_opts: {type: tmpfs, device: tmpfs}"
      fi
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
    echo "        github_runner_cluster_regrade: ${regrade}"
    if [ "${role_controller:-0}" = 1 ]; then echo "        github_runner_cluster_role_controller: true"; fi
    # The test cluster is throwaway: the role must not install a login item that starts the host's Colima profile.
    echo "        github_runner_cluster_runtime_autostart: false"
    if [ "$pin_context" = 1 ]; then
      echo "        github_runner_cluster_docker_context: ${grtest_pinned_context}"
    fi
    # Litestream has its own test (tests/litestream); this one exercises the in-cluster bootstrap on node-local state, which the role only allows once acknowledged.
    if [ "$scenario" = in_cluster ]; then
      echo "        github_runner_cluster_headscale_accept_node_local_state: true"
    fi
    echo "      hosts:"
    for index in ${inventory_nodes:-$(seq 1 "$node_count")}; do
      echo "        $(node_name "$index"):"
      echo "          github_runner_cluster_dir: ${work}/$(node_name "$index")"
      echo "          github_runner_cluster_compose_project: $(node_name "$index")"
      echo "          github_runner_cluster_container_name: grtest-node${index}-k3s"
      # Used only by a host the topology makes an agent (the role_controller scenario has one); the role refuses one name for both services.
      echo "          github_runner_cluster_agent_container_name: grtest-node${index}-agent"
      # The role_controller scenario marks every host a candidate for a vote, through the inventory, as an owner would, and pins the first; the rejoin scenario marks them without turning the controller on, which is enough for the role to mount each host's role directory.
      if [ "${role_controller:-0}" = 1 ] || [ "${voters_eligible:-0}" = 1 ]; then
        echo "          github_runner_cluster_voter_eligible: true"
      fi
      if [ "${role_controller:-0}" = 1 ] && [ "$index" = 1 ] && [ "$pin_first" = 1 ]; then echo "          github_runner_cluster_voter_pinned: true"; fi
    done
    if [ -n "$native_role" ]; then
      echo "        ${native_host}:"
      echo "          ansible_connection: community.docker.docker"
      echo "          ansible_host: ${native_host}"
      echo "          ansible_python_interpreter: /usr/bin/python3"
      echo "          github_runner_cluster_node_mode: native"
      echo "          github_runner_cluster_node_role: ${native_role}"
      echo "          github_runner_cluster_regrade: ${native_regrade}"
      # The role_actuator scenario marks the native host a candidate for a vote too, which stages it as a standby server and runs its role actuator.
      if [ "${native_eligible:-0}" = 1 ]; then echo "          github_runner_cluster_voter_eligible: true"; fi
    fi
  } > "${work}/inventory.yml"
}

run_role() {
  log "Running the cluster role"
  # Returns the playbook's own status, which a caller testing it (compose_faulty) relies on, errexit being off there. Any arguments go to ansible-playbook.
  ansible_run -i "${work}/inventory.yml" "${repo_root}/tests/mesh/cluster.yml" "$@" || { diagnose >&2 || true; return 1; }
  if [ "$pin_context" = 1 ]; then grtest_assert_current_context_unchanged "$docker_config" || fail "the role changed the current Docker context"; fi
}

kubectl_on() { docker_on "$1" exec -i "$(container "$1")" kubectl "${@:2}"; }

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
    docker_on "$index" logs "$(container "$index")" 2>&1 | grep -F "github-runner-node: $(node_name 1).mesh.grtest.internal is a peer and resolves after tailscale up" >/dev/null \
      || fail "$(container "$index") never logged the wait after k3s's tailscale up"
  done

  log "Checking the servers were chosen automatically (all three) and etcd has three members"
  nodes=$(kubectl_on 1 get nodes -l node-role.kubernetes.io/etcd=true --no-headers | wc -l | tr -d ' ')
  [ "$nodes" = "$node_count" ] || fail "expected ${node_count} etcd nodes, found ${nodes}"
  docker_on 1 rm -f grtest-etcdctl >/dev/null 2>&1 || true
  members=$(docker_on 1 run --rm --name grtest-etcdctl --network "container:$(container 1)" --volumes-from "$(container 1)" --entrypoint etcdctl "$etcd_image" \
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
      route=$(docker_on "$index" exec "$(container "$index")" ip route get "$ip" </dev/null)
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
  for index in $(seq 1 "$node_count"); do docker_on "$index" inspect --format '{{.State.StartedAt}}' "$(container "$index")"; done | tr '\n' ' '
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

# host_network: each node gets a Docker daemon of its own, a docker:dind container on the shared test network at the node's address, standing in for a VM with an address on the LAN, and a Docker context the role pins for it. The work directory is mounted at the same path inside each, so the Compose project's bind mounts, which the daemon resolves, find the files the role writes on this machine.
start_vms() {
  local index
  for index in $(seq 1 "$node_count"); do
    docker run -d --privileged --name "$(vm "$index")" --hostname "$(vm "$index")" --network "$network" --ip "$(node_ip "$index")" \
      -e DOCKER_TLS_CERTDIR= -v "${work}:${work}" "$dind_image" --host=tcp://0.0.0.0:2375 --host=unix:///var/run/docker.sock >/dev/null
    docker context create "$(vm "$index")" --docker "host=tcp://$(node_ip "$index"):2375" >/dev/null
  done
  for index in $(seq 1 "$node_count"); do
    local deadline=$((SECONDS + 120))
    until docker --context "$(vm "$index")" info >/dev/null 2>&1; do
      [ "$SECONDS" -lt "$deadline" ] || fail "the Docker daemon in $(vm "$index") did not start"
      sleep 1
    done
  done
  # Each node's Tailscale client advertises the endpoints it finds for itself: its interface addresses, and the address a STUN server on a public DERP region sees it come from, which on a hosted runner is the runner's public address. That address hairpins back through the runner's NAT, and magicsock picks between it and the test network address by measured latency, with a bonus for a private address that sub-millisecond jitter can outweigh (betterAddr in wgengine/magicsock/endpoint.go of the pinned Tailscale), so which one a node uses varied between runs. STUN is UDP to port 3478, so it is dropped in every VM, as on a LAN whose NAT does not hairpin: a node then advertises only addresses of its own VM's interfaces (see assert_direct_paths), and DERP, over TCP, still carries the disco messages that set a direct path up.
  for index in $(seq 1 "$node_count"); do
    docker exec "$(vm "$index")" iptables -I OUTPUT 1 -p udp --dport 3478 -j DROP
  done
}

# A Headscale server on this machine's Docker, at a fixed address on the test network, with a policy that lets the nodes register, reach each other and route pods; prints the API key and a join key on two lines.
start_headscale_for_host_network() {
  local fixtures="${repo_root}/tests/mesh/fixtures/headscale-existing"
  mkdir -p "${work}/hs-host-network"
  sed -e "s#^server_url: .*#server_url: http://${headscale_ip}:8080#" -e 's#^  base_domain: .*#  base_domain: mesh.grtest.internal#' \
    "${fixtures}/config.yaml" > "${work}/hs-host-network/config.yaml"
  cat > "${work}/hs-host-network/policy.hujson" <<'POLICY'
{
  "tagOwners": {"tag:github-runner-node": []},
  "autoApprovers": {"routes": {"10.42.0.0/16": ["tag:github-runner-node"]}},
  "grants": [{"src": ["tag:github-runner-node", "10.42.0.0/16"], "dst": ["tag:github-runner-node", "10.42.0.0/16"], "ip": ["*"]}],
}
POLICY
  docker run -d --name grtest-hs-existing --network "$network" --ip "$headscale_ip" \
    -v "${work}/hs-host-network:/etc/headscale:ro" "headscale/headscale:v$(sed -n 's/^github_runner_cluster_headscale_version: "\(.*\)"$/\1/p' "${repo_root}/roles/github_runner_cluster/defaults/main.yml")" serve >/dev/null
  until docker exec grtest-hs-existing headscale health >/dev/null 2>&1; do sleep 1; done
  docker exec grtest-hs-existing headscale apikeys create --expiration 1d | tail -n 1
  # 90 days: the headscale_existing provider refuses a join key that expires within 14.
  docker exec grtest-hs-existing headscale preauthkeys create --reusable --tags tag:github-runner-node --expiration 90d | tail -n 1
}

write_host_network_inventory() {
  local api_key="$1" join_key="$2" index
  for index in $(seq 1 "$node_count"); do
    mkdir -p "${work}/$(node_name "$index")"
    printf 'services:\n  k3s:\n    container_name: %s\n' "$(container "$index")" > "${work}/$(node_name "$index")/compose.grtest.yml"
  done
  {
    echo "all:"
    echo "  vars:"
    echo "    ansible_connection: local"
    echo "    ansible_python_interpreter: $(command -v python3)"
    echo "  children:"
    echo "    github_runner_cluster:"
    echo "      vars:"
    echo "        github_runner_cluster_mesh: headscale_existing"
    echo "        github_runner_cluster_tailnet_domain: mesh.grtest.internal"
    echo "        github_runner_cluster_headscale_url: http://${headscale_ip}:8080"
    echo "        github_runner_cluster_headscale_api_key: ${api_key}"
    echo "        github_runner_cluster_headscale_join_key: ${join_key}"
    echo "        github_runner_cluster_compose_files: [compose.grtest.yml]"
    echo "        github_runner_cluster_network_mode: host"
    echo "        github_runner_cluster_lan_cidr: ${subnet_prefix}.0/24"
    echo "        github_runner_cluster_k3s_token: ${k3s_token}"
    echo "      hosts:"
    for index in $(seq 1 "$node_count"); do
      echo "        $(node_name "$index"):"
      echo "          github_runner_cluster_dir: ${work}/$(node_name "$index")"
      echo "          github_runner_cluster_compose_project: $(node_name "$index")"
      echo "          github_runner_cluster_container_name: $(container "$index")"
      echo "          github_runner_cluster_docker_context: $(vm "$index")"
    done
  } > "${work}/inventory.yml"
}

# With host networking, every node's Tailscale client must reach every other node directly at that node's daemon's own address (its stand-in LAN address), not through a relay, and the node container must publish no port.
assert_direct_paths() {
  local index other status deadline own outside
  for index in $(seq 1 "$node_count"); do
    [ "$(docker_on "$index" inspect --format '{{.HostConfig.NetworkMode}}' "$(container "$index")")" = host ] || fail "$(container "$index") is not on the host network"
    [ -z "$(docker_on "$index" port "$(container "$index")")" ] || fail "$(container "$index") publishes ports: $(docker_on "$index" port "$(container "$index")")"
    for other in $(seq 1 "$node_count"); do
      [ "$index" = "$other" ] && continue
      deadline=$((SECONDS + 120))
      # tailscale ping keeps pinging until a direct path answers, or gives up and says the connection is relayed.
      until status=$(docker_on "$index" exec "$(container "$index")" tailscale ping --c 10 "$(node_name "$other")" </dev/null 2>&1) && echo "$status" | grep -q "via $(node_ip "$other"):"; do
        [ "$SECONDS" -lt "$deadline" ] || fail "$(node_name "$index") reaches $(node_name "$other") only indirectly: ${status}"
        sleep 5
      done
      echo "  $(node_name "$index") -> $(node_name "$other"): $(echo "$status" | tail -n 1)"
    done
  done
  # Every endpoint each node advertises for itself is an address of its own VM's interfaces (the test network address, and the bridges its Docker and k3s create): none is an address learnt over STUN, which belongs to the runner's NAT and would let its peers pick a path through it instead (see start_vms). tailscale status lists a node's own endpoints under Self.Addrs, and none for its peers (UpdateStatus in wgengine/magicsock/magicsock.go of the pinned Tailscale fills Self only). The node container is on its VM's host network, so the VM's interfaces are the node's.
  for index in $(seq 1 "$node_count"); do
    own=$(docker exec "$(vm "$index")" ip -o addr show | awk '{ split($4, parts, "/"); print parts[1] }')
    status=$(docker_on "$index" exec "$(container "$index")" tailscale status --json </dev/null)
    outside=$(echo "$status" | python3 -c 'import json, sys
own = set(sys.argv[1].split())
print(" ".join(addr for addr in json.load(sys.stdin)["Self"]["Addrs"] if addr.rsplit(":", 1)[0].strip("[]") not in own))' "$own")
    [ -z "$outside" ] || fail "$(node_name "$index") advertises endpoints that are not its own VM's addresses: ${outside}"
  done
  log "Every node reaches every other directly at its daemon's address"
}

scenario_host_network() {
  local keys api_key join_key before after
  [ "$(uname -s)" = Linux ] || fail "the host_network scenario reaches each node's Docker daemon at its address on the test network, which only Linux routes to"
  host_network=1
  reset_environment
  start_vms
  keys=$(start_headscale_for_host_network)
  api_key=$(echo "$keys" | sed -n 1p)
  join_key=$(echo "$keys" | sed -n 2p)
  write_host_network_inventory "$api_key" "$join_key"
  run_role
  assert_cluster
  assert_direct_paths
  log "Rerunning the role, which must not restart any node"
  before=$(started_at)
  run_role
  after=$(started_at)
  [ "$before" = "$after" ] || fail "rerunning the role restarted a node container: ${before} -> ${after}"
  move_between_networks
}

# After node $1's container was recreated, the API can still show its echo pod at an address its new sandbox no longer has; replacing the pod makes assert_cluster's rollout wait for the node's kubelet and probe the pod's real address.
replace_echo_pod() {
  kubectl_on 1 delete pod -l app=grtest-echo --field-selector "spec.nodeName=$(node_name "$1")" --timeout=300s || fail "the echo pod on $(node_name "$1") was not replaced"
}

# Moves running nodes between the host and the bridge network through the role, so its guard reads a real cluster: two nodes in one run must be refused before anything changes, and one node must move to the bridge network and back keeping its mesh address and etcd membership.
move_between_networks() {
  local output before mesh_address
  log "Moving two nodes to the bridge network in one run must be refused before anything changes"
  before=$(started_at)
  if output=$(ansible_run -i "${work}/inventory.yml" "${repo_root}/tests/mesh/cluster.yml" --limit "$(node_name 2),$(node_name 3)" -e github_runner_cluster_network_mode=bridge 2>&1); then
    echo "$output"
    fail "the role moved two nodes between networks in one run"
  fi
  echo "$output" | grep -q "to another network at once" || { echo "$output"; fail "the role failed for another reason than moving two nodes at once"; }
  [ "$(started_at)" = "$before" ] || fail "a refused move restarted a node container"
  mesh_address=$(docker_on 3 exec "$(container 3)" tailscale ip -4 </dev/null)
  log "Moving $(node_name 3) to the bridge network through the role's guard"
  ansible_run -i "${work}/inventory.yml" "${repo_root}/tests/mesh/cluster.yml" --limit "$(node_name 3)" -e github_runner_cluster_network_mode=bridge || fail "the role refused or failed to move a node of a healthy cluster to the bridge network"
  [ "$(docker_on 3 inspect --format '{{.HostConfig.NetworkMode}}' "$(container 3)")" != host ] || fail "$(container 3) is still on the host network"
  replace_echo_pod 3
  assert_cluster
  [ "$(docker_on 3 exec "$(container 3)" tailscale ip -4 </dev/null)" = "$mesh_address" ] || fail "$(node_name 3) changed its mesh address when it moved to the bridge network"
  log "Moving $(node_name 3) back to the host network"
  ansible_run -i "${work}/inventory.yml" "${repo_root}/tests/mesh/cluster.yml" --limit "$(node_name 3)" || fail "the role refused or failed to move a node back to the host network"
  replace_echo_pod 3
  assert_cluster
  assert_direct_paths
  [ "$(docker_on 3 exec "$(container 3)" tailscale ip -4 </dev/null)" = "$mesh_address" ] || fail "$(node_name 3) changed its mesh address when it moved back to the host network"
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

# --- Memory-pressure regrading (github_runner_cluster_regrade) ---

regrade_container() { echo "grtest-node$1-regrade-1"; }
# Runs a command in a node's regrade service.
regrade_on() { docker exec -i "$(regrade_container "$1")" "${@:2}"; }
# The level a node's regrade service reports.
level_of() { regrade_on "$1" fleet-regrade status | sed -n 's/^level: \([^ ]*\) .*/\1/p'; }
node_status() { kubectl_on "$1" get node "$(node_name "$2")" --no-headers 2>/dev/null | awk '{print $2}'; }
node_password_hash() { docker exec "$(container "$1")" sha256sum /etc/rancher/node/password </dev/null | cut -d' ' -f1; }
level_label() { kubectl_on 1 get node "$(node_name "$1")" -o jsonpath='{.metadata.labels.github-runner\.exadev\.com/regrade-level}'; }
service_started_at() {
  local index
  for index in $(seq 1 "$node_count"); do docker inspect --format '{{.State.StartedAt}}' "$(regrade_container "$index")"; done | tr '\n' ' '
}

# The ids of the etcd members, read with a throwaway etcdctl that shares a running node's network and volumes, as assert_cluster does.
etcd_member_ids() {
  docker rm -f grtest-etcdctl >/dev/null 2>&1 || true
  docker run --rm --name grtest-etcdctl --network "container:$(container "$1")" --volumes-from "$(container "$1")" --entrypoint etcdctl "$etcd_image" \
    --endpoints https://127.0.0.1:2379 --cacert /var/lib/rancher/k3s/server/tls/etcd/server-ca.crt \
    --cert /var/lib/rancher/k3s/server/tls/etcd/client.crt --key /var/lib/rancher/k3s/server/tls/etcd/client.key \
    member list -w simple </dev/null | awk -F', ' '{print $1}' | sort | tr '\n' ' '
}

# Runs a check until it passes, failing the scenario after the given number of seconds.
wait_for() {
  local description="$1" deadline=$((SECONDS + $2))
  shift 2
  until "$@"; do
    [ "$SECONDS" -lt "$deadline" ] || fail "timed out waiting for ${description}"
    sleep 3
  done
}

# Recreates a node's k3s container from its own Compose project, as the role does when it changes the container's configuration; the project's .env names the Compose files and the project.
recreate_node() {
  docker compose --project-directory "${work}/$(node_name "$1")" --profile server up -d --force-recreate --no-deps k3s >/dev/null
}

# A write to the cluster, under a name of its own each time: a write the API server committed but did not answer is not repeated under the same name.
write_marker() { kubectl_on 1 create configmap "grtest-vote-${RANDOM}" --from-literal=a=1 >/dev/null; }
# A step by hand on node 3 is refused, and the refusal names the stepped-down server. Retried because node 3's own API may still be settling after it was stopped, which is a refusal too but for another reason; a step that goes ahead is a failure, not a retry.
peer_refusal() {
  case "$(level_of 3 || true)" in
    control-plane | etcd-only) fail "$(node_name 3) stepped while $(node_name 2) was stepped down" ;;
  esac
  ! regrade_on 3 fleet-regrade down >"${work}/peer.log" 2>&1 && grep -F "etcd-voting nodes not ready: $(node_name 2)" "${work}/peer.log" >/dev/null
}
at_level() { [ "$(level_of "$1")" = "$2" ]; }
# Whether the regrade Lease exists and nobody holds it, as a release leaves it, read through node 1's API. A Lease left to expire keeps its holder, so this tells a release from an expiry at any time.
lease_free() { [ "$(kubectl_on 1 get lease fleet-regrade --namespace kube-system -o 'jsonpath={.spec.holderIdentity}' 2>/dev/null; echo "/$?")" = /0 ]; }
# Whether the regrade service of node $1 has logged, in the file $2 inside it, that it reached the level $3.
logged_level() { regrade_on "$1" cat "$2" | grep -Fx "now at $3" >/dev/null; }
# A step gives the Lease back once it has finished, and a step down to etcd-only does so from another server's API, since the stepped-down server has none: afterwards the Lease is free, and the service never logged that it could not release it. The service logs the end of a step before it releases the Lease, so a process stopped as soon as a level is reached can leave the Lease held until it expires.
assert_released() {
  wait_for "the Lease to be released after $(node_name "$1")'s last step" 120 lease_free
  if regrade_on "$1" cat "$2" | grep -F 'lease could not be released' >/dev/null; then regrade_on "$1" cat "$2"; fail "$(node_name "$1") could not release the Lease"; fi
}
node_is() { [ "$(node_status "$1" "$2")" = "$3" ]; }
# Whether the regrade service of node $1 can read the cluster's nodes as a step does, with the kubectl and kubeconfig of its own server's container and the API of another server (the first of its candidates, which every other server is up to serve here). A recreated container has no kubeconfig until its k3s has written one, some seconds after the container starts, and a step before then is refused for it.
# shellcheck disable=SC2016 # The expansions are for the shell in the service container, which sources the service's own settings and splits KUBECTL, a command of several words, on purpose.
can_list_nodes() { regrade_on "$1" sh -c '. /usr/local/lib/fleet-regrade/fleet-regrade.env && $KUBECTL --kubeconfig "$KUBECONFIG_PATH" --server "${KUBE_SERVERS%% *}" get nodes' >/dev/null 2>&1; }

# The numbers of the nodes whose regrade service reports a level below full. A read that gives no level, or unknown, is not a step: a test node's k3s container can restart by itself under the strain, and its status then reads unknown or fails for a moment, which says nothing about whether the server stepped.
stepped_nodes() {
  local index
  for index in $(seq 1 "$node_count"); do
    case "$(level_of "$index" || true)" in
      control-plane | etcd-only) printf '%s ' "$index" ;;
    esac
  done
}

scenario_regrade() {
  local index ids password before after stepped deadline output
  regrade=true
  reset_environment
  prepare_nodes hosted
  write_inventory hosted
  run_role
  assert_cluster

  log "Every server runs the regrade service, at full"
  for index in $(seq 1 "$node_count"); do
    [ "$(docker inspect --format '{{.State.Running}}' "$(regrade_container "$index")")" = true ] || fail "$(regrade_container "$index") is not running"
    wait_for "$(node_name "$index")'s regrade service to see its level" 120 at_level "$index" full
  done

  log "Rerunning the role must restart neither the nodes nor the services"
  before="$(started_at)$(service_started_at)"
  run_role
  after="$(started_at)$(service_started_at)"
  [ "$before" = "$after" ] || fail "rerunning the role restarted a container: ${before} -> ${after}"

  ids=$(etcd_member_ids 1)
  password=$(node_password_hash 2)
  echo "etcd members: ${ids}"

  log "A step is refused while another server holds the cluster-wide lease"
  kubectl_on 1 apply -f - <<EOF
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata: {name: fleet-regrade, namespace: kube-system}
spec: {holderIdentity: grtest-other, leaseDurationSeconds: 3600, acquireTime: "$(date -u +%Y-%m-%dT%H:%M:%S.000000Z)", renewTime: "$(date -u +%Y-%m-%dT%H:%M:%S.000000Z)"}
EOF
  if regrade_on 2 fleet-regrade down >"${work}/held.log" 2>&1; then cat "${work}/held.log"; fail "a step ran with the lease held by another server"; fi
  grep -F 'the lease is held by grtest-other' "${work}/held.log" >/dev/null || { cat "${work}/held.log"; fail "the refusal did not name the lease holder"; }
  wait_for "$(node_name 2) to read full, since a step without the lease is a failure" 180 at_level 2 full
  kubectl_on 1 delete lease fleet-regrade --namespace kube-system >/dev/null

  log "Memory pressure on $(node_name 2) steps it down a level at a time, and no other server steps with it"
  # The watcher reads its pressure from files in place of the kernel's, so the test controls it. It runs in the service container, with the service's own settings but for the times, which are seconds here, so it has the service's Lease, Docker access and kubectl.
  regrade_on 2 sh -c 'cp /usr/local/lib/fleet-regrade/fleet-regrade.env /tmp/pressure.env && printf "SAMPLE_SECONDS=1\nSUSTAIN_SECONDS=3\nCOOLDOWN_SECONDS=5\n" >> /tmp/pressure.env && printf "some avg10=0.00 avg60=0.00 avg300=50.00 total=1\n" > /tmp/psi && printf "MemTotal: 1000000 kB\nMemAvailable: 1000 kB\n" > /tmp/meminfo'
  # shellcheck disable=SC2016 # The expansion is for the shell in the service container, whose process becomes the watcher through exec, so the pid it records is the watcher's.
  docker exec -d "$(regrade_container 2)" sh -c 'echo $$ > /tmp/watch.pid; FLEET_REGRADE_ENV=/tmp/pressure.env PSI_FILE=/tmp/psi MEMINFO_FILE=/tmp/meminfo exec fleet-regrade watch >/tmp/watch.log 2>&1'
  deadline=$((SECONDS + 900))
  until at_level 2 etcd-only; do
    stepped=$(stepped_nodes)
    [ "$stepped" = "" ] || [ "$stepped" = "2 " ] || fail "another server stepped while $(node_name 2) did: ${stepped}"
    [ "$SECONDS" -lt "$deadline" ] || { regrade_on 2 cat /tmp/watch.log || true; fail "$(node_name 2) did not reach etcd-only"; }
    sleep 2
  done
  wait_for "the watcher on $(node_name 2) to finish its step to etcd-only" 300 logged_level 2 /tmp/watch.log etcd-only
  assert_released 2 /tmp/watch.log
  regrade_on 2 cat /tmp/watch.log
  regrade_on 2 cat /tmp/watch.log | grep -F 'step down full -> control-plane' >/dev/null || fail "the watcher did not step down through control-plane first"

  log "At etcd-only the server keeps its seat: the same members, healthy, and its Node still registered"
  wait_for "$(node_name 2)'s Node to report NotReady" 240 node_is 1 2 NotReady,SchedulingDisabled
  [ "$(etcd_member_ids 1)" = "$ids" ] || fail "the etcd members changed: $(etcd_member_ids 1) (was ${ids})"
  [ "$(level_label 2)" = etcd-only ] || fail "the Node does not carry the level label"
  kubectl_on 1 get node "$(node_name 2)" -o jsonpath='{.metadata.labels}' | grep -F 'node-role.kubernetes.io/etcd' >/dev/null || fail "the Node lost its etcd role label"
  if docker exec "$(container 2)" sh -c 'for p in /proc/[0-9]*; do cat "$p/comm"; done' | grep -Fx containerd >/dev/null; then fail "$(node_name 2) still runs containerd at etcd-only"; fi
  [ "$(node_password_hash 2)" = "$password" ] || fail "$(node_name 2)'s node password changed"

  log "The stepped-down server still votes: the cluster takes writes with the third server stopped"
  docker stop "$(container 3)" >/dev/null
  wait_for "a write through $(node_name 1) with $(node_name 3) stopped" 180 write_marker
  docker start "$(container 3)" >/dev/null
  # Through its own API, not the others' view of it: for the grace period of the node controller the others still show the Ready it had before it was stopped.
  wait_for "$(node_name 3) to be Ready again" 600 node_is 3 3 Ready

  log "While $(node_name 2) is stepped down no other server steps"
  wait_for "$(node_name 3)'s refusal to name the stepped-down server" 300 peer_refusal
  cat "${work}/peer.log"
  wait_for "$(node_name 3) to read full, since a step while another was stepped down is a failure" 180 at_level 3 full

  log "Rerunning the role with a server stepped down changes nothing, and turning regrading off is refused"
  # Judged by the role's own recap, not by the containers' start times: after a server was stopped and started a container can restart by itself under the strain, which says nothing about the role.
  output=$(run_role 2>&1) || { echo "$output"; fail "the role failed with $(node_name 2) stepped down"; }
  [ "$(grep -cE '^grtest-node[0-9]+ +: .* changed=0 ' <<< "$output")" = "$node_count" ] || { echo "$output" | tail -n 20; fail "rerunning the role changed something while $(node_name 2) was stepped down"; }
  wait_for "$(node_name 2) to read etcd-only after the role ran" 180 at_level 2 etcd-only
  regrade=false
  write_inventory hosted
  if output=$(run_role 2>&1); then echo "$output"; fail "the role removed the service of a stepped-down server"; fi
  echo "$output" | grep -F 'is running at the level etcd-only' >/dev/null || { echo "$output"; fail "the refusal did not name the level"; }
  for index in $(seq 1 "$node_count"); do
    docker inspect "$(regrade_container "$index")" >/dev/null 2>&1 || fail "$(regrade_container "$index") was removed in spite of the refusal, which is the whole play's"
  done
  regrade=true
  write_inventory hosted

  log "The pressure passes: the watcher steps back up by itself, one level at a time, to the same node"
  regrade_on 2 sh -c 'printf "some avg10=0.00 avg60=0.00 avg300=0.00 total=1\n" > /tmp/psi && printf "MemTotal: 1000000 kB\nMemAvailable: 900000 kB\n" > /tmp/meminfo'
  deadline=$((SECONDS + 900))
  until at_level 2 full; do
    stepped=$(stepped_nodes)
    [ "$stepped" = "" ] || [ "$stepped" = "2 " ] || fail "another server stepped while $(node_name 2) stepped up: ${stepped}"
    [ "$SECONDS" -lt "$deadline" ] || { regrade_on 2 cat /tmp/watch.log || true; fail "$(node_name 2) did not step back up to full"; }
    sleep 2
  done
  # The level is recorded before the Node is made schedulable again and the Lease given back, so the watcher is stopped only once its last step has finished, or it would be stopped part-way and leave the Lease held for the next step.
  wait_for "the watcher on $(node_name 2) to finish its step up to full" 300 logged_level 2 /tmp/watch.log full
  assert_released 2 /tmp/watch.log
  regrade_on 2 cat /tmp/watch.log
  regrade_on 2 cat /tmp/watch.log | grep -F 'step up etcd-only -> control-plane' >/dev/null || fail "the watcher did not step up through control-plane first"
  # shellcheck disable=SC2016 # The substitution is for the shell in the service container, which holds the watcher.
  regrade_on 2 sh -c 'kill "$(cat /tmp/watch.pid)"' || fail "the watcher on $(node_name 2) could not be stopped"
  wait_for "$(node_name 2) to be Ready and schedulable" 300 node_is 1 2 Ready
  [ -z "$(level_label 2)" ] || fail "the level label is still on the Node at full"
  [ "$(etcd_member_ids 1)" = "$ids" ] || fail "the etcd members changed after stepping up"
  [ "$(node_password_hash 2)" = "$password" ] || fail "$(node_name 2)'s node password changed"
  assert_cluster

  log "By hand, a server goes down to etcd-only, stays there through a restart and a recreate, and up is the way back"
  password=$(node_password_hash 3)
  regrade_on 3 fleet-regrade down
  wait_for "$(node_name 3) to read control-plane after a step down by hand" 180 at_level 3 control-plane
  wait_for "$(node_name 3) to be cordoned" 60 node_is 1 3 Ready,SchedulingDisabled
  [ "$(level_label 3)" = control-plane ] || fail "the Node does not carry the level label at control-plane"
  regrade_on 3 sh -c 'fleet-regrade down >/tmp/down.log 2>&1' || { regrade_on 3 cat /tmp/down.log; fail "the second step down by hand on $(node_name 3) failed"; }
  regrade_on 3 cat /tmp/down.log
  wait_for "$(node_name 3) to read etcd-only after a second step down by hand" 180 at_level 3 etcd-only
  assert_released 3 /tmp/down.log
  # The level is a file on the data volume that the entrypoint reads at every start, so neither a restart by hand nor a recreate, which is what Compose does when the role changes the container's configuration, brings the server back at full.
  docker restart "$(container 3)" >/dev/null
  wait_for "$(node_name 3) to be at etcd-only after a restart by hand" 300 at_level 3 etcd-only
  before=$(docker inspect --format '{{.Id}}' "$(container 3)")
  recreate_node 3
  [ "$before" != "$(docker inspect --format '{{.Id}}' "$(container 3)")" ] || fail "$(node_name 3)'s container was not recreated"
  wait_for "$(node_name 3) to be at etcd-only after a recreate" 300 at_level 3 etcd-only
  wait_for "$(node_name 3)'s service to read the nodes through its recreated container" 300 can_list_nodes 3
  [ "$(level_label 3)" = etcd-only ] || fail "the Node lost its level label across the recreate"
  [ "$(node_password_hash 3)" = "$password" ] || fail "$(node_name 3)'s node password changed across the recreate"
  [ "$(etcd_member_ids 1)" = "$ids" ] || fail "the etcd members changed across the restart and the recreate"
  regrade_on 3 fleet-regrade up
  wait_for "$(node_name 3) to be Ready at control-plane" 300 node_is 1 3 Ready,SchedulingDisabled
  regrade_on 3 fleet-regrade up
  wait_for "$(node_name 3) to be schedulable again" 300 node_is 1 3 Ready
  wait_for "$(node_name 3) to read full after stepping up by hand" 180 at_level 3 full
  [ -z "$(level_label 3)" ] || fail "the level label is still on the Node at full"

  log "With every server at full, turning regrading off removes the services and leaves the nodes running"
  before=$(started_at)
  regrade=false
  write_inventory hosted
  run_role
  [ "$before" = "$(started_at)" ] || fail "turning regrading off restarted a node"
  for index in $(seq 1 "$node_count"); do
    if docker inspect "$(regrade_container "$index")" >/dev/null 2>&1; then fail "$(regrade_container "$index") is still there"; fi
  done
}

# native_server: the native host is a container running systemd as PID 1 (tests/mesh/native/Dockerfile), on the test network at its own address, with k3s's data directory on a volume. The role reaches it over Ansible's docker connection and installs Tailscale and k3s on it, as on a real host.
start_native_host() {
  docker build --quiet --tag "$native_image" "${repo_root}/tests/mesh/native" >/dev/null
  docker run -d --privileged --name "$native_host" --hostname "$native_host" --network "$network" --ip "$native_ip" \
    --tmpfs /run --tmpfs /run/lock --volume "${native_host}-k3s:/var/lib/rancher/k3s" --volume /lib/modules:/lib/modules:ro \
    "$native_image" >/dev/null
  wait_for "systemd to be running in ${native_host}" 120 native_systemd_up
  # Docker answers the container's DNS at 127.0.0.11, which a pod cannot reach and write-resolv-conf.sh therefore leaves out, and the container has no systemd-resolved, so the kubelet's resolver file is pointed at public servers through a drop-in for both units. A real host's systemd-resolved lists its upstream servers instead; nothing else of the role's is changed.
  # shellcheck disable=SC2016 # Expanded by the container's shell.
  native_exec sh -c 'printf "nameserver 1.1.1.1\nnameserver 8.8.8.8\n" > /etc/grtest-upstream-resolv.conf
    for unit in k3s k3s-agent; do
      mkdir -p "/etc/systemd/system/${unit}.service.d"
      printf "[Service]\nEnvironment=GITHUB_RUNNER_RESOLV_SOURCES=/etc/grtest-upstream-resolv.conf\n" > "/etc/systemd/system/${unit}.service.d/grtest.conf"
    done'
}

native_exec() { docker exec -i "$native_host" "$@"; }
native_systemd_up() { case "$(native_exec systemctl is-system-running 2>/dev/null)" in running | degraded) return 0 ;; *) return 1 ;; esac; }
# Whether every Docker server's own API server reports ready (/readyz through its own container) and every Docker node is Ready. The role's first start of a server returns once Compose has started its container, which is before its API server serves, so a play that reads the cluster straight after (the native host's version check reads through every server, and refuses when none answers) waits for this first.
docker_servers_serving() {
  local index
  for index in $(seq 1 "$node_count"); do
    [ "$(kubectl_on "$index" get --raw /readyz 2>/dev/null </dev/null)" = ok ] || return 1
  done
  [ "$(kubectl_on 1 get nodes --no-headers 2>/dev/null </dev/null | grep -c ' Ready ')" = "$node_count" ]
}

# node_status names a Docker node by its index; the native host's node is named for the host.
native_node_ready() { [ "$(kubectl_on "$api_node" get node "$native_host" --no-headers 2>/dev/null | awk '{print $2}')" = Ready ]; }
native_unit_is() { [ "$(native_exec systemctl is-active "$1" </dev/null 2>/dev/null)" = "$2" ]; }

# The etcd member list, as assert_cluster reads it, through the node given or the api node.
etcd_members() {
  docker rm -f grtest-etcdctl >/dev/null 2>&1 || true
  docker run --rm --name grtest-etcdctl --network "container:$(container "${1:-$api_node}")" --volumes-from "$(container "${1:-$api_node}")" --entrypoint etcdctl "$etcd_image" \
    --endpoints https://127.0.0.1:2379 --cacert /var/lib/rancher/k3s/server/tls/etcd/server-ca.crt \
    --cert /var/lib/rancher/k3s/server/tls/etcd/client.crt --key /var/lib/rancher/k3s/server/tls/etcd/client.key \
    member list -w simple </dev/null
}

# Whether the Node $1 (the native host's by default) reports itself a voting member of etcd.
node_is_voter() {
  [ "$(kubectl_on "$api_node" get node "${1:-$native_host}" -o 'jsonpath={.status.conditions[?(@.type=="EtcdIsVoter")].status}' 2>/dev/null)" = True ]
}

# Writes to the cluster through node 1 once a second until the stop file appears, recording each write as ok, or as failed with the time and what kubectl said. Each write is a ConfigMap named by its sequence number, so no two writes share a name: write_marker's random suffix repeats often enough over a promotion's worth of writes that a refusal for a name already taken would read as a lost write.
write_throughout() {
  local log="$1" stop="$2" sequence=0 error
  : > "$log"
  : > "${log}.times"
  until [ -e "$stop" ]; do
    sequence=$((sequence + 1))
    if error=$(kubectl_on "$api_node" create configmap "grtest-native-write-${sequence}" --from-literal=a=1 2>&1 >/dev/null </dev/null); then
      echo ok >> "$log"
      echo "$(date +%s) ok" >> "${log}.times"
    else
      echo "failed $(date -u +%H:%M:%S) ${error}" >> "$log"
      echo "$(date +%s) failed" >> "${log}.times"
    fi
    sleep 1
  done
}

# Watches the member list until the stop file appears, recording every line that names the native host's member, so a learner seen at any point is on record.
# Fails unless every write that failed in the log $1 (kept with its time by write_throughout) was followed by a write that succeeded within the time a leader election takes: etcd's election timeout, plus the up to one more election timeout of randomised delay a candidate waits before it stands, since stepping down the member that happens to lead makes the others elect another and a write in flight when it goes is answered "request timed out, possibly due to previous leader failure". Quorum lost is writes failing and not recovering; one election is not.
assert_writes_recovered() {
  local window=$(((etcd_election_ms * 2 + 999) / 1000)) bad
  # A run of failures is judged from its first failure to the success that ends it.
  bad=$(awk -v window="$window" '
    $2 == "failed" { if (start == "") start = $1; last = $1; next }
    $2 == "ok" { if (start != "" && $1 - start > window) print "writes failed from " start " until " $1; start = "" }
    END { if (start != "") print "writes were still failing at " last " after failing from " start }' "${1}.times")
  [ -z "$bad" ] || { echo "$bad"; grep '^failed' "$1"; return 1; }
}

watch_native_member() {
  local log="$1" stop="$2"
  : > "$log"
  until [ -e "$stop" ]; do
    etcd_members 2>/dev/null | grep -F ", ${native_host}-" >> "$log" || true
    sleep 1
  done
}

scenario_native_server() {
  reset_environment
  prepare_nodes hosted
  start_native_host
  native_role=agent
  write_inventory hosted
  # A play runs a native host's tasks before any Docker host's (the role's first task includes them, and Ansible's linear strategy runs an include for its hosts while the others wait), so the servers a native node joins are brought up first, as the role README says.
  log "Bringing up three Docker servers, then the native host as an agent"
  run_role --limit "$(node_name 1),$(node_name 2),$(node_name 3)"
  # Said either way, so the log shows whether this run's role returned before its servers served, which is the race the wait closes.
  if docker_servers_serving; then log "The Docker servers already serve as the role returns"; else log "The Docker servers do not serve yet as the role returns; waiting"; fi
  wait_for "every Docker server's API to serve and every Docker node to be Ready" 600 docker_servers_serving
  run_role --limit "$native_host"
  wait_for "${native_host} to be Ready as an agent" 300 native_node_ready
  native_unit_is k3s-agent active || fail "${native_host} does not run k3s-agent"
  native_unit_is k3s inactive || fail "${native_host} runs k3s as an agent"
  [ "$(native_exec cat /var/lib/rancher/k3s/fleet-role)" = agent ] || fail "the role file does not name the agent"
  [ "$(kubectl_on 1 get nodes -l node-role.kubernetes.io/etcd=true --no-headers | wc -l | tr -d ' ')" = 3 ] || fail "expected three etcd nodes before the promotion"
  local password
  password=$(native_exec sha256sum /etc/rancher/node/password | cut -d' ' -f1)

  log "Promoting the native host to a server, with writes throughout; node 3 keeps running as a voter outside the inventory, so the inventory has three servers"
  native_role=server
  inventory_nodes="1 2"
  write_inventory hosted
  local writes="${work}/writes.log" members="${work}/native-member.log" stop="${work}/stop-writing"
  rm -f "$stop"
  write_throughout "$writes" "$stop" &
  local writer=$!
  watch_native_member "$members" "$stop" &
  local watcher=$!
  run_role --limit "$native_host" || { touch "$stop"; wait "$writer" "$watcher" || true; fail "the role did not promote ${native_host}"; }
  wait_for "${native_host} to be a voting member of etcd" 300 node_is_voter
  touch "$stop"
  wait "$writer" "$watcher"

  log "Checking it joined etcd as a learner and was promoted, with its mesh address as its peer URL"
  local list
  list=$(etcd_members)
  echo "$list"
  [ "$(echo "$list" | grep -c ', started, ')" = 4 ] || fail "expected four started etcd members after the promotion"
  echo "$list" | grep -F ", ${native_host}-" | grep -Eq ', https://100\.[0-9.]+:2380, .*, false$' \
    || fail "${native_host}'s member is not a voter with a mesh peer URL"
  # k3s adds every joining server as a learner (MemberAddAsLearner) and the etcd leader logs each promotion; the member watch shows the learner unless it was promoted between two reads.
  local promoted=0 index
  for index in 1 2 3; do
    if docker logs "$(container "$index")" 2>&1 | grep -F "Promoted learner ${native_host}-" >/dev/null; then promoted=1; fi
  done
  [ "$promoted" = 1 ] || fail "no server's k3s logged promoting ${native_host}'s learner"
  if grep -Eq ', true$' "$members"; then log "The member watch saw ${native_host} as a learner"; else log "The member watch did not catch ${native_host} as a learner (promoted between reads); the promotion is in the servers' logs"; fi
  native_exec journalctl -u k3s --no-pager | grep -F "Adding member ${native_host}-" >/dev/null || fail "${native_host}'s k3s did not add itself to etcd"

  log "Checking every write succeeded throughout"
  local ok failed
  ok=$(grep -c '^ok$' "$writes" || true)
  failed=$(grep -c '^failed' "$writes" || true)
  echo "  ${ok} writes succeeded, ${failed} failed"
  [ "$ok" -gt 0 ] || fail "no write was made during the promotion"
  [ "$failed" = 0 ] || { grep '^failed' "$writes"; fail "writes failed during the promotion"; }

  log "Checking the host now runs the server unit only, under the same identity"
  native_unit_is k3s active || fail "${native_host} does not run k3s"
  native_unit_is k3s-agent inactive || fail "${native_host} still runs k3s-agent"
  [ "$(native_exec cat /var/lib/rancher/k3s/fleet-role)" = server ] || fail "the role file does not name the server"
  [ "$(native_exec sha256sum /etc/rancher/node/password | cut -d' ' -f1)" = "$password" ] || fail "${native_host}'s node password changed"
  [ "$(kubectl_on 1 get nodes --no-headers | grep -c "^${native_host} ")" = 1 ] || fail "${native_host} is not exactly one Node"
  native_exec cat /etc/rancher/k3s/server.yaml.d/00-node-ip.yaml | grep -Eq '^node-ip: "100\.' || fail "the node-ip drop-in does not hold the mesh address"
  # Restart=always must not start the agent unit again, whose condition now fails.
  native_exec systemctl start k3s-agent || true
  native_unit_is k3s-agent inactive || fail "k3s-agent started on a server"
  # And the skipped start must leave the server's own runtime directory alone, which holds the resolver file its kubelet reads.
  native_exec test -s /run/github-runner-k3s-server/resolv.conf </dev/null || fail "a skipped start of k3s-agent removed the server's resolver file"

  log "Rerunning the role on the native server, which must not restart it"
  local before
  before=$(native_exec systemctl show k3s --property ActiveEnterTimestampMonotonic)
  run_role --limit "$native_host"
  [ "$before" = "$(native_exec systemctl show k3s --property ActiveEnterTimestampMonotonic)" ] || fail "rerunning the role restarted ${native_host}'s server"

  regrade_native_server
}

# The slow link the role_controller scenario puts on a voter, in milliseconds each way on its mesh interface: above the unfit round trip limit, a tenth of the election timeout the inventory sets.
role_controller_delay_ms=$((etcd_election_ms / 10 + 100))
# shellcheck source=tests/mesh/role-image.sh
. "${repo_root}/tests/mesh/role-image.sh"
grtest_select_role_image
platform_namespace=github-runner-platform

role_status() { kubectl_on "$api_node" --namespace "$platform_namespace" get configmap fleet-role-status -o 'jsonpath={.data.status\.json}' 2>/dev/null; }
# Whether the controller's last status satisfies a jq test, as an exit status.
role_status_is() { role_status | jq -e "$1" >/dev/null 2>&1; }
# The node a pod of the app runs on.
role_pod_node() { kubectl_on "$api_node" --namespace "$platform_namespace" get pods -l "app.kubernetes.io/name=$1" -o 'jsonpath={.items[0].spec.nodeName}'; }
# Puts a delay on a node's mesh interface, or takes it off, from a throwaway container in the node's network namespace (the node's own image has no tc).
mesh_delay() {
  local index="$1" action="$2" ms="${3:-0}" command
  if [ "$action" = add ]; then command="apk add --quiet iproute2-tc && tc qdisc add dev tailscale0 root netem delay ${ms}ms"; else command="apk add --quiet iproute2-tc && tc qdisc del dev tailscale0 root"; fi
  docker run --rm --network "container:$(container "$index")" --cap-add NET_ADMIN --entrypoint sh "$tc_image" -c "$command" </dev/null
}

# Loads the role controller's image, from the tar file $1, into every node container whose containerd runs and lacks it, until the scenario ends.
keep_role_image_loaded() {
  local container
  while true; do
    for container in $(docker ps --format '{{.Names}}' --filter 'name=^grtest-node[0-9]+-(k3s|agent)$'); do
      if docker exec "$container" /bin/ctr --address /run/k3s/containerd/containerd.sock --namespace k8s.io images ls -q </dev/null 2>/dev/null | grep -F "$role_controller_image" >/dev/null; then continue; fi
      docker exec -i "$container" /bin/ctr --address /run/k3s/containerd/containerd.sock --namespace k8s.io images import - < "$1" >/dev/null 2>&1 || true
    done
    sleep 3
  done
}

# Runs the role controller's probe and controller in the cluster, the probes at $1 seconds (role_controller_sample_seconds by default): the ARC role renders its manifests for the selected image, which is built and loaded into every node by default or pulled by k3s when GRTEST_ROLE_IMAGE is set.
deploy_role_controller() {
  local index image_file manifests sample_seconds=${1:-$role_controller_sample_seconds}
  if [ "$role_image_published" = 1 ]; then
    log "Using published role controller image ${role_controller_image}; k3s pulls it on each node"
  else
    log "Building the role controller image from this checkout and loading it into every node"
    docker build --quiet --file "${repo_root}/fleet-role/Dockerfile" --tag "$role_controller_image" "$repo_root" >/dev/null
    image_file="${work}/fleet-role-image.tar"
    docker save --output "$image_file" "$role_controller_image"
    for index in $(seq 1 "$node_count"); do
      docker exec -i "$(container "$index")" /bin/ctr --address /run/k3s/containerd/containerd.sock --namespace k8s.io images import - < "$image_file" >/dev/null
    done
    if [ -n "$native_role" ]; then native_exec k3s ctr --namespace k8s.io images import - < "$image_file" >/dev/null; fi
    # A host that changes role starts a container whose containerd has no copy of the image (the server and agent services keep separate data volumes, and a real host pulls the image from its registry), so a loop loads it into each node container as one comes up, which keeps the new voter's probe running.
    keep_role_image_loaded "$image_file" &
    image_loader_pid=$!
  fi

  log "Rendering the ARC role's manifests for the controller and the probe, and applying them"
  manifests="${work}/fleet-role.yaml"
  ANSIBLE_COLLECTIONS_PATH="${repo_root}/playbooks/collections" ANSIBLE_LOCALHOST_WARNING=false ANSIBLE_INVENTORY_UNPARSED_WARNING=false \
    "$ansible_playbook" "${repo_root}/tests/fleet_role/render.yml" -e "fleet_role_output=${manifests}" \
    -e "github_runner_arc_role_controller_image=${role_controller_image}" -e "github_runner_arc_role_controller_sample_seconds=${sample_seconds}" \
    -e "github_runner_arc_role_controller_election_timeout_ms=${etcd_election_ms}" >/dev/null
  # Applied rather than created: the cluster role makes the namespace too, for the role actuators' credential, which it sets up while the controller is on.
  kubectl_on "$api_node" apply -f - >/dev/null <<MANIFESTS
apiVersion: v1
kind: Namespace
metadata: {name: ${platform_namespace}}
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata: {name: github-runner-platform}
value: 1000000
---
apiVersion: v1
kind: ConfigMap
metadata: {name: fleet-role-probe, namespace: ${platform_namespace}}
data: {}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: fleet-role-status, namespace: ${platform_namespace}}
data: {"status.json": ""}
MANIFESTS
  kubectl_on "$api_node" apply -f - < "$manifests" >/dev/null
  kubectl_on "$api_node" --namespace "$platform_namespace" rollout status daemonset/fleet-role-probe --timeout=300s
  kubectl_on "$api_node" --namespace "$platform_namespace" rollout status deployment/fleet-role --timeout=300s
  kubectl_on "$api_node" --namespace "$platform_namespace" get pods -o wide
}

# Brings up three servers and a standby agent with the role controller on and waits for every node to be Ready: the start of both role_controller scenarios. Sets `ids`, the etcd members at the start, in the caller's variable of that name.
role_controller_cluster_up() {
  local deadline
  node_count=4
  agent_from=4
  etcd_on_memory=1
  role_controller=1
  reset_environment
  prepare_nodes hosted
  write_inventory hosted
  run_role

  log "Waiting for the four nodes: three servers and a standby agent"
  deadline=$((SECONDS + 600))
  until [ "$(kubectl_on 1 get nodes --no-headers 2>/dev/null | grep -c ' Ready ')" = "$node_count" ]; do
    [ "$SECONDS" -lt "$deadline" ] || { kubectl_on 1 get nodes -o wide || true; fail "not every node became Ready"; }
    sleep 5
  done
  [ "$(kubectl_on 1 get nodes -l node-role.kubernetes.io/etcd=true --no-headers | wc -l | tr -d ' ')" = 3 ] || fail "expected three etcd nodes"
  if kubectl_on 1 get node "$(node_name 4)" -o 'jsonpath={.metadata.labels}' | grep -F 'node-role.kubernetes.io/etcd' >/dev/null; then fail "the fourth node is a server"; fi
  ids=$(etcd_member_ids 1)
}

# Brings up three servers and a standby agent, runs the role controller's probe and controller in the cluster (the pods from the image built from this checkout, the manifests the ARC role renders), and checks that the controller reports what it sees on real k3s and real etcd: all hosts fit, then one voter made slow with tc netem on its mesh interface reported unfit with the swap named, then a voter paused and the controller reporting that it would not act, and throughout that it has changed nothing.
scenario_role_controller() {
  local index ids image_file manifests slow paused paused_name controller_node deadline account verb expected link
  role_controller_cluster_up

  log "A check run on hosts that lack the Compose files of the role controller, as a host that ran the role before the controller was turned on does, passes and writes nothing"
  local before_started file
  before_started="$(started_at)"
  for index in $(seq 1 "$node_count"); do
    for file in docker-compose.node-labels.yml docker-compose.fleet-role.yml; do rm -f "${work}/$(node_name "$index")/${file}"; done
  done
  run_role --check || fail "the role's check run failed on hosts without the role controller's Compose files"
  for index in $(seq 1 "$node_count"); do
    for file in docker-compose.node-labels.yml docker-compose.fleet-role.yml; do
      [ ! -e "${work}/$(node_name "$index")/${file}" ] || fail "the check run wrote ${file} on $(node_name "$index")"
    done
  done
  [ "$before_started" = "$(started_at)" ] || fail "the check run restarted a node"
  log "The real run after it writes the files first, then names them, and recreates no container"
  run_role
  for index in $(seq 1 "$node_count"); do
    for file in docker-compose.node-labels.yml docker-compose.fleet-role.yml; do
      [ -e "${work}/$(node_name "$index")/${file}" ] || fail "the real run did not write ${file} on $(node_name "$index")"
    done
  done
  [ "$before_started" = "$(started_at)" ] || fail "the real run restarted a node whose configuration it left as it was"

  deploy_role_controller
  # Every host of this scenario is this machine, which the role reads the link of with its own reader.
  link="$("${repo_root}/roles/github_runner_cluster/files/node-link.sh" | jq -r .link)"
  log "The role published the labels from the inventory, eligible on every host and pinned on the first alone, and the medium of this machine's link (${link}) on every host"
  for index in $(seq 1 "$node_count"); do
    [ "$(kubectl_on 1 get node "$(node_name "$index")" -o 'jsonpath={.metadata.labels.github-runner\.exadev\.com/link}')" = "$link" ] || fail "$(node_name "$index") does not carry link=${link}"
    [ "$(kubectl_on 1 get node "$(node_name "$index")" -o 'jsonpath={.metadata.labels.github-runner\.exadev\.com/voter-eligible}')" = true ] || fail "$(node_name "$index") is not labelled eligible"
    expected=false
    if [ "$index" = 1 ]; then expected=true; fi
    [ "$(kubectl_on 1 get node "$(node_name "$index")" -o 'jsonpath={.metadata.labels.github-runner\.exadev\.com/voter-pinned}')" = "$expected" ] || fail "$(node_name "$index") does not carry voter-pinned=${expected}"
  done

  log "The probe can read nodes and nothing more; the controller can also patch them, as a swap needs, but not update, delete or create one, and neither can create or patch a ConfigMap it does not own"
  for account in fleet-role fleet-role-probe; do
    [ "$(kubectl_on 1 auth can-i list nodes --as "system:serviceaccount:${platform_namespace}:${account}")" = yes ] || fail "${account} cannot list nodes"
    for verb in update delete create; do
      [ "$(kubectl_on 1 auth can-i "$verb" nodes --as "system:serviceaccount:${platform_namespace}:${account}" || true)" = no ] || fail "${account} can ${verb} nodes"
    done
    expected=no
    if [ "$account" = fleet-role ]; then expected=yes; fi
    [ "$(kubectl_on 1 auth can-i patch nodes --as "system:serviceaccount:${platform_namespace}:${account}" || true)" = "$expected" ] || fail "${account} does not get patch on nodes as ${expected}"
    [ "$(kubectl_on 1 auth can-i create configmaps --namespace "$platform_namespace" --as "system:serviceaccount:${platform_namespace}:${account}" || true)" = no ] || fail "${account} can create configmaps"
  done
  [ "$(kubectl_on 1 auth can-i patch configmaps/fleet-role-probe --namespace "$platform_namespace" --as "system:serviceaccount:${platform_namespace}:fleet-role" || true)" = no ] || fail "the controller can patch the probes' ConfigMap"
  [ "$(kubectl_on 1 auth can-i update lease/fleet-regrade --namespace kube-system --as "system:serviceaccount:${platform_namespace}:fleet-role" || true)" = yes ] || fail "the controller cannot replace the regrade Lease"
  [ "$(kubectl_on 1 auth can-i update lease/another --namespace kube-system --as "system:serviceaccount:${platform_namespace}:fleet-role" || true)" = no ] || fail "the controller can replace a Lease that is not the regrade Lease"
  [ "$(kubectl_on 1 auth can-i update lease/fleet-regrade --namespace kube-system --as "system:serviceaccount:${platform_namespace}:fleet-role-probe" || true)" = no ] || fail "the probe can replace the regrade Lease"

  log "Every host is fit, from a window of real samples, and the controller would not act"
  wait_for "every host to be reported fit and no swap wanted" 1800 role_status_is '(.hosts | length == 4) and ([.hosts[].verdict] | all(. == "fit")) and .decision.action == "none" and (.blocks | length == 0)'
  role_status | jq .
  role_status | jq -e "(.hosts | to_entries | all(.value.eligible)) and ([.hosts[] | select(.pinned)] | length == 1) and .hosts[\"$(node_name 1)\"].pinned" >/dev/null || fail "the controller did not read the labels the role published: $(role_status | jq -c '[.hosts[] | {eligible, pinned}]')"
  # A node in a container sees only its veth, so what each probe reports is the label the role published.
  role_status | jq -e --arg link "$link" '[.hosts[] | .link == $link and .link_source == "label"] | all' >/dev/null || fail "the probes did not report the link the role labelled the nodes with (${link}): $(role_status | jq -c '[.hosts[] | {link, link_source}]')"

  role_controller_slow_voter_named

  log "A voter is paused: the controller reports that it would not act"
  controller_node=$(role_pod_node fleet-role)
  paused=2
  [ "$(node_name "$paused")" != "$controller_node" ] || paused=$slow
  paused_name=$(node_name "$paused")
  docker pause "$(container "$paused")" >/dev/null
  wait_for "the controller to say it would not act with ${paused_name} paused" 600 role_status_is \
    ".decision.action == \"none\" and (.decision.because + .blocks | any(contains(\"${paused_name}\")))"
  role_status | jq '{decision, blocks}'
  kubectl_on 1 --namespace "$platform_namespace" logs deployment/fleet-role --tail 5 | grep -F 'DRY RUN: would not act' >/dev/null || fail "the controller did not log that it would not act"
  docker unpause "$(container "$paused")" >/dev/null

  log "The controller changed nothing: the same etcd members, the same servers, no node touched"
  [ "$(etcd_member_ids 1)" = "$ids" ] || fail "the etcd members changed: $(etcd_member_ids 1) (was ${ids})"
  [ "$(kubectl_on 1 get nodes -l node-role.kubernetes.io/etcd=true --no-headers | wc -l | tr -d ' ')" = 3 ] || fail "the set of etcd nodes changed"
  kubectl_on 1 get nodes -o 'jsonpath={range .items[*]}{.metadata.name}{" "}{.spec.unschedulable}{" "}{.spec.taints}{"\n"}{end}'
  if kubectl_on 1 get nodes -o json | jq -e '[.items[] | select(.metadata.annotations["etcd.k3s.cattle.io/remove"] != null)] | length > 0' >/dev/null; then fail "a node was annotated for removal"; fi
  [ -z "$(kubectl_on 1 get lease fleet-regrade --namespace kube-system -o 'jsonpath={.metadata.name}' 2>/dev/null </dev/null || true)" ] || fail "a dry run took the regrade Lease"
}

# Makes the voter $slow (node 3, set here in the caller's variable of that name) slow with tc netem and waits until the controller, which is in dry run, reports it unfit and names the swap for the standby.
role_controller_slow_voter_named() {
  log "A voter is made slow with tc netem on its mesh interface (${role_controller_delay_ms} ms each way): the controller reports it unfit and names the swap"
  slow=3
  mesh_delay "$slow" add "$role_controller_delay_ms"
  wait_for "$(node_name "$slow") to be reported unfit and the swap named" 900 role_status_is \
    ".hosts[\"$(node_name "$slow")\"].verdict == \"unfit\" and .decision.action == \"swap\" and .decision.swap == {out: \"$(node_name "$slow")\", in: \"$(node_name 4)\"}"
  role_status | jq .
  role_status | jq -r '.decision.because[]'
  role_status | jq -e ".hosts[\"$(node_name "$slow")\"].reasons | any(test(\"round trip p99\"))" >/dev/null || fail "the reason does not name the round trip"
  role_status | jq -e --arg slow "$(node_name "$slow")" '([.hosts[] | select(.verdict == "unfit") | .attributed_to | index($slow) != null] | all) and (.attribution[$slow] | index($slow) != null)' >/dev/null \
    || fail "the unfit verdicts are not attributed to $(node_name "$slow"): $(role_status | jq -c '.attribution')"
  kubectl_on 1 --namespace "$platform_namespace" logs deployment/fleet-role --tail 20 | grep -F "DRY RUN: would swap out $(node_name "$slow") for $(node_name 4)" >/dev/null || fail "the controller did not log the swap it would make"
}

# The window of samples the controller reads each figure over, in seconds, at the probes' period $1: the count is the controller's own, read from the module it takes it from.
role_controller_window_seconds() { echo $(($1 * $(jq -nr -L "${repo_root}/scripts" 'include "fleet-role-stats"; window_samples'))); }

# The role controller at the ARC role's default period carrying out the swap it reported: a voter is made slow, the controller (in dry run) names the swap, and with the dry run turned off it makes it. It is a scenario of its own because a window of samples at the default period takes as long to fill as the controller says (role_controller_window_seconds), and a swap has its own bounds (swap_wait_seconds), which together do not fit beside role_controller's other checks in one job.
scenario_role_controller_swap() {
  local ids slow window_seconds
  window_seconds=$(role_controller_window_seconds "$swap_sample_seconds")
  role_controller_cluster_up
  deploy_role_controller "$swap_sample_seconds"

  log "Every host is fit, from a window of real samples at the default period, and the controller would not act"
  wait_for "every host to be reported fit and no swap wanted" $((window_seconds + 900)) role_status_is '(.hosts | length == 4) and ([.hosts[].verdict] | all(. == "fit")) and .decision.action == "none" and (.blocks | length == 0)'

  role_controller_slow_voter_named

  log "With the dry run turned off, the controller carries out the swap it reported, bringing the standby agent, a Docker host that was never a server, in for the slow voter, which stays slow"
  kubectl_on 1 --namespace "$platform_namespace" set env deployment/fleet-role FLEET_ROLE_DRY_RUN=false >/dev/null
  kubectl_on 1 --namespace "$platform_namespace" rollout status deployment/fleet-role --timeout=300s
  wait_for_swap "$(node_name "$slow")" "$(node_name 4)" completed
  role_status | jq -e '.dry_run == false and .request == null' >/dev/null || fail "the swap was not the controller's own decision: $(role_status | jq -c '{dry_run, request, last_swap}')"
  # The slow voter stays up throughout, so its actuator finishes the demotion inside the swap; a swap that leaves it to node recovery has lost the finish, whatever the outcome says.
  role_status | jq -e '.last_swap.outgoing_pending != true' >/dev/null || fail "the swap left $(node_name "$slow") to node recovery rather than finishing it as an agent: $(role_status | jq -c .last_swap)"
  [ "$(node_is_voter "$(node_name 4)" && echo yes)" = yes ] || fail "$(node_name 4) is not a voter after the swap"
  [ "$(voter_count)" = 3 ] || fail "expected three voters after the swap, got $(voter_count)"
  node_is_clean "$(node_name "$slow")" || fail "$(node_name "$slow")'s labels and annotations were not cleaned up"
  [ "$(docker_state grtest-node3-agent)" = running ] || fail "$(node_name "$slow") does not run its agent"
  [ "$(docker_state grtest-node3-k3s)" = exited ] || fail "$(node_name "$slow") still runs its server"
  [ "$(docker_state grtest-node4-k3s)" = running ] || fail "$(node_name 4) does not run a server container"
  [ "$(started_member_count)" = 3 ] || { etcd_members; fail "expected three started etcd members after the swap"; }
  [ "$(etcd_member_ids 1)" != "$ids" ] || fail "the etcd members are the ones the swap started from"
  lease_released || fail "the regrade Lease was not released"
}

# --- Memory-pressure regrading of the native server (native_server's last part) ---

# The level the native server's fleet-regrade reports, read with its own command, as an operator would.
native_level() { native_exec fleet-regrade status </dev/null 2>/dev/null | sed -n 's/^level: \([^ ]*\) .*/\1/p'; }
native_at_level() { [ "$(native_level)" = "$1" ]; }
native_node_is() { [ "$(kubectl_on 1 get node "$native_host" --no-headers 2>/dev/null | awk '{print $2}')" = "$1" ]; }
native_level_label() { kubectl_on 1 get node "$native_host" -o jsonpath='{.metadata.labels.github-runner\.exadev\.com/regrade-level}'; }
# The keys the regrading drop-in sets, sorted, one space after each; a comment is not a key.
native_dropin_keys() { native_exec sh -c "grep -v '^#' /etc/rancher/k3s/server.yaml.d/50-regrade.yaml | cut -d: -f1 | sort | tr '\n' ' '" </dev/null; }
native_etcd_only_keys="disable-agent disable-apiserver disable-controller-manager disable-scheduler "
# How many of the containerd shims k3s starts for pods run on the native host, found as the systemd actuator finds them.
native_shims() { native_exec sh -c 'ps -e -o args= | grep -c "^/var/lib/rancher/k3s/data/[^/]*/bin/containerd-shim" || true' </dev/null; }
# Whether k3s's own containerd runs on the native host.
native_containerd_runs() { native_exec ps -e -o args= </dev/null | grep '^containerd ' >/dev/null; }
native_member_ids() { etcd_members | awk -F', ' '{print $1}' | sort | tr '\n' ' '; }
# Whether the member list names the native host's member as started and not a learner.
native_voter_started() { etcd_members | grep -F ", ${native_host}-" | grep -E ', started, .*, false$' >/dev/null; }
# The fleet-regrade unit's log, for its steps and for a failure.
native_regrade_log() { native_exec journalctl -u fleet-regrade --no-pager --output cat </dev/null; }
native_logged() { native_regrade_log | grep -Fx "now at $1" >/dev/null; }
grtest_daemonset_ready() { [ "$(kubectl_on 1 get daemonset grtest-native-pod -o 'jsonpath={.status.numberReady}' 2>/dev/null)" = 1 ]; }
# Whether the etcd of the native host's k3s is serving in the unit's current run, as the actuator reads it.
native_etcd_serving() {
  # shellcheck disable=SC2016 # The expansions are for the shell in the native host.
  native_exec sh -c 'journalctl --no-pager --output cat "_SYSTEMD_INVOCATION_ID=$(systemctl show --property InvocationID --value k3s)" | grep -F "ETCD server is now running" >/dev/null' </dev/null
}
native_server_started() { native_exec systemctl show k3s --property ActiveEnterTimestampMonotonic </dev/null; }

# The native server steps down to etcd-only and back up under simulated memory pressure, through the real fleet-regrade unit and the real k3s unit, whose ExecStartPre writes the drop-in: the membership unchanged, the vote kept, the level kept across `systemctl restart` and across the host stopping and starting, and no pod left running without a kubelet at etcd-only.
regrade_native_server() {
  local ids password before shims output
  log "Turning regrading on for ${native_host}, which must not restart its server"
  before=$(native_server_started)
  native_regrade=true
  write_inventory hosted
  run_role --limit "$native_host"
  [ "$before" = "$(native_server_started)" ] || fail "turning regrading on restarted ${native_host}'s server"
  native_unit_is fleet-regrade active || { native_regrade_log || true; fail "${native_host} does not run fleet-regrade"; }
  wait_for "${native_host}'s fleet-regrade to read full" 120 native_at_level full
  native_exec grep -Fx 'FLEET_REGRADE_ACTUATOR=systemd' /usr/local/lib/github-runner-k3s/regrade/fleet-regrade.env </dev/null >/dev/null || fail "the native settings do not name the systemd actuator"
  if native_exec grep -F "${native_host}." /usr/local/lib/github-runner-k3s/regrade/fleet-regrade.env </dev/null | grep '^KUBE_SERVERS=' >/dev/null; then fail "the native server's candidate API servers name its own"; fi

  log "A pod that outlives a drain, a DaemonSet's, runs on ${native_host}"
  kubectl_on 1 apply -f - <<EOF
apiVersion: apps/v1
kind: DaemonSet
metadata: {name: grtest-native-pod, namespace: default}
spec:
  selector: {matchLabels: {app: grtest-native-pod}}
  template:
    metadata: {labels: {app: grtest-native-pod}}
    spec:
      nodeSelector: {kubernetes.io/hostname: ${native_host}}
      tolerations: [{operator: Exists}]
      containers: [{name: sleep, image: "${busybox_image}", command: [sleep, "86400"]}]
EOF
  wait_for "the DaemonSet's pod to run on ${native_host}" 300 grtest_daemonset_ready
  shims=$(native_shims)
  echo "  containerd shims of k3s's running on ${native_host} at full: ${shims}"
  [ "$shims" -gt 0 ] || fail "no containerd shim of k3s's runs on ${native_host} at full, so the count at etcd-only could not tell"

  ids=$(native_member_ids)
  password=$(native_exec sha256sum /etc/rancher/node/password </dev/null | cut -d' ' -f1)
  echo "etcd members: ${ids}"

  log "Memory pressure on ${native_host} steps it down a level at a time, through its fleet-regrade unit"
  # The unit's own watcher, reading its pressure from files in place of the kernel's and with its times in seconds through a drop-in, so the test controls it; everything else is the unit and the settings the role wrote.
  native_exec sh -c 'cp /usr/local/lib/github-runner-k3s/regrade/fleet-regrade.env /etc/grtest-pressure.env && printf "SAMPLE_SECONDS=1\nSUSTAIN_SECONDS=3\nCOOLDOWN_SECONDS=5\n" >> /etc/grtest-pressure.env
    printf "some avg10=0.00 avg60=0.00 avg300=50.00 total=1\n" > /etc/grtest-psi && printf "MemTotal: 1000000 kB\nMemAvailable: 1000 kB\n" > /etc/grtest-meminfo
    mkdir -p /etc/systemd/system/fleet-regrade.service.d
    printf "[Service]\nEnvironment=FLEET_REGRADE_ENV=/etc/grtest-pressure.env PSI_FILE=/etc/grtest-psi MEMINFO_FILE=/etc/grtest-meminfo\n" > /etc/systemd/system/fleet-regrade.service.d/grtest.conf
    systemctl daemon-reload && systemctl restart fleet-regrade' </dev/null
  wait_for "${native_host} to reach etcd-only" 900 native_logged etcd-only
  native_regrade_log | grep -F 'step down full -> control-plane' >/dev/null || { native_regrade_log; fail "the watcher did not step down through control-plane first"; }
  wait_for "the Lease to be released after ${native_host}'s step down" 120 lease_free
  native_regrade_log

  log "At etcd-only ${native_host} keeps its seat and its vote, runs all but etcd off, and runs no pod"
  native_at_level etcd-only || fail "${native_host} does not read etcd-only"
  [ "$(native_dropin_keys)" = "$native_etcd_only_keys" ] || fail "the drop-in does not turn all but etcd off: $(native_dropin_keys)"
  [ "$(native_exec cat /run/github-runner-k3s-server/regrade-level </dev/null)" = etcd-only ] || fail "the unit's start did not record etcd-only"
  wait_for "${native_host}'s Node to report NotReady" 240 native_node_is NotReady,SchedulingDisabled
  [ "$(native_level_label)" = etcd-only ] || fail "the Node does not carry the level label"
  [ "$(native_member_ids)" = "$ids" ] || fail "the etcd members changed: $(native_member_ids) (was ${ids})"
  native_voter_started || { etcd_members; fail "${native_host}'s member is not a started voter at etcd-only"; }
  shims=$(native_shims)
  echo "  containerd shims of k3s's running on ${native_host} at etcd-only: ${shims}"
  [ "$shims" = 0 ] || fail "${shims} containerd shims still run on ${native_host} at etcd-only"
  if native_containerd_runs; then fail "${native_host} still runs containerd at etcd-only"; fi
  native_regrade_log | grep -F 'stopping the pods left without a kubelet at etcd-only' >/dev/null || fail "the actuator did not say it stopped the pods"

  log "The stepped-down native server still votes: the cluster takes writes with a Docker server stopped"
  docker stop "$(container 3)" >/dev/null
  wait_for "a write through $(node_name 1) with $(node_name 3) stopped" 180 write_marker
  docker start "$(container 3)" >/dev/null
  wait_for "$(node_name 3) to be Ready again" 600 node_is 3 3 Ready

  log "The level stays across a restart of the server unit by hand"
  native_exec systemctl restart k3s </dev/null
  wait_for "${native_host}'s etcd to serve again after the restart" 300 native_etcd_serving
  wait_for "${native_host} to read etcd-only after the restart" 120 native_at_level etcd-only
  [ "$(native_dropin_keys)" = "$native_etcd_only_keys" ] || fail "the restart lost the drop-in"
  [ "$(native_shims)" = 0 ] || fail "a pod came back on ${native_host} after the restart at etcd-only"

  log "And across the host stopping and starting"
  docker stop "$native_host" >/dev/null
  docker start "$native_host" >/dev/null
  wait_for "systemd to be running in ${native_host} again" 120 native_systemd_up
  wait_for "${native_host}'s etcd to serve again after the host started" 300 native_etcd_serving
  wait_for "${native_host} to read etcd-only after the host started" 120 native_at_level etcd-only
  [ "$(native_dropin_keys)" = "$native_etcd_only_keys" ] || fail "the host's start lost the drop-in"
  [ "$(native_shims)" = 0 ] || fail "a pod came back on ${native_host} after the host started at etcd-only"
  wait_for "${native_host}'s member to be a started voter again" 300 native_voter_started
  [ "$(native_member_ids)" = "$ids" ] || fail "the etcd members changed across the restarts: $(native_member_ids) (was ${ids})"
  native_unit_is fleet-regrade active || fail "fleet-regrade did not start with the host"

  log "The pressure passes: the watcher steps back up by itself, one level at a time, to the same node"
  native_exec sh -c 'printf "some avg10=0.00 avg60=0.00 avg300=0.00 total=1\n" > /etc/grtest-psi && printf "MemTotal: 1000000 kB\nMemAvailable: 900000 kB\n" > /etc/grtest-meminfo' </dev/null
  wait_for "${native_host} to step back up to full" 900 native_logged full
  wait_for "the Lease to be released after ${native_host}'s step up" 120 lease_free
  native_regrade_log | grep -F 'step up etcd-only -> control-plane' >/dev/null || { native_regrade_log; fail "the watcher did not step up through control-plane first"; }
  native_regrade_log
  wait_for "${native_host} to be Ready and schedulable" 300 native_node_is Ready
  [ -z "$(native_level_label)" ] || fail "the level label is still on the Node at full"
  [ "$(native_dropin_keys)" = "" ] || fail "the drop-in still turns something off at full: $(native_dropin_keys)"
  [ "$(native_member_ids)" = "$ids" ] || fail "the etcd members changed after stepping up"
  [ "$(native_exec sha256sum /etc/rancher/node/password </dev/null | cut -d' ' -f1)" = "$password" ] || fail "${native_host}'s node password changed"
  wait_for "the DaemonSet's pod to run on ${native_host} again" 300 grtest_daemonset_ready
  kubectl_on 1 delete daemonset grtest-native-pod >/dev/null

  log "With the server at full, turning regrading off removes the unit and leaves the server running"
  native_exec sh -c 'rm -rf /etc/systemd/system/fleet-regrade.service.d /etc/grtest-pressure.env /etc/grtest-psi /etc/grtest-meminfo && systemctl daemon-reload' </dev/null
  before=$(native_server_started)
  native_regrade=false
  write_inventory hosted
  output=$(run_role --limit "$native_host" 2>&1) || { echo "$output"; fail "turning regrading off failed"; }
  [ "$before" = "$(native_server_started)" ] || fail "turning regrading off restarted ${native_host}'s server"
  if native_exec test -e /etc/systemd/system/fleet-regrade.service </dev/null; then fail "the fleet-regrade unit is still installed"; fi
  native_unit_is fleet-regrade inactive || fail "fleet-regrade still runs"
}

# --- A former member of the cluster (the rejoin scenario) ---

# The etcd cluster ID as node $1's own etcd reports it, in etcdctl's fields output, which prints the 64-bit ID as an integer (a JSON reader would round it to a float).
etcd_cluster_id() {
  docker rm -f grtest-etcdctl >/dev/null 2>&1 || true
  docker run --rm --name grtest-etcdctl --network "container:$(container "$1")" --volumes-from "$(container "$1")" --entrypoint etcdctl "$etcd_image" \
    --endpoints https://127.0.0.1:2379 --cacert /var/lib/rancher/k3s/server/tls/etcd/server-ca.crt \
    --cert /var/lib/rancher/k3s/server/tls/etcd/client.crt --key /var/lib/rancher/k3s/server/tls/etcd/client.key \
    endpoint status -w fields </dev/null | sed -n 's/^"ClusterID" : \([0-9]*\)$/\1/p'
}
# Whether node $1's own etcd answers on its client port. A member that has just joined or been promoted can be counted a voter by the others, and its Node say EtcdIsVoter, before its own etcd serves its clients, so a read of its cluster ID waits for this rather than reading an empty ID as another cluster.
etcd_answers() { [ -n "$(etcd_cluster_id "$1")" ]; }
# Whether node $1's log has the line $2 in it; grep without -q reads the whole log, as in assert_cluster.
container_logged() { docker logs "$(container "$1")" 2>&1 | grep -F "$2" >/dev/null; }
removed_member_recorded() { [ -n "$(kubectl_on 2 get node "$(node_name 1)" -o 'jsonpath={.metadata.annotations.etcd\.k3s\.cattle\.io/removed-node-name}' 2>/dev/null)" ]; }
has_tombstone() { docker exec "$(container 1)" test -e /var/lib/rancher/k3s/server/db/etcd/tombstone </dev/null 2>/dev/null; }
started_members() { etcd_members 2 2>/dev/null | grep -c ', started, ' || true; }
node1_voter_started() { etcd_members 2 2>/dev/null | grep -F ", $(node_name 1)-" | grep -E ', started, .*, false$' >/dev/null; }
node1_is_voter() { [ "$(kubectl_on 2 get node "$(node_name 1)" -o 'jsonpath={.status.conditions[?(@.type=="EtcdIsVoter")].status}' 2>/dev/null)" = True ]; }
# Whether node 1's container has been started again by its restart policy since it was last started by hand.
node1_restarted() { [ "$(docker inspect --format '{{.RestartCount}}' "$(container 1)")" -gt 0 ]; }
# The command names of node 1's processes but the ones this read runs, one per line.
node1_processes() {
  # shellcheck disable=SC2016 # Expanded by the container's shell, which leaves out itself and the cat it runs.
  docker exec "$(container 1)" sh -c 'for p in /proc/[0-9]*; do [ "${p#/proc/}" = "$$" ] || cat "$p/comm" 2>/dev/null; done' </dev/null | grep -vx cat
}
# Node 1 runs only the entrypoint's shell and its sleep: no k3s, containerd or Tailscale.
node1_idles() {
  local processes
  processes=$(node1_processes)
  [ "$(sort -u <<< "$processes" | tr '\n' ' ')" = "sh sleep " ]
}

# A former member never starts a second cluster: the bootstrap Docker server's etcd member is removed through another server, as a demotion removes it, and the server restarted on its tombstone idles instead of joining again; with its datastore moved aside, as finishing a demotion leaves it, it refuses to start with no server to join (its inventory gives it none, being the bootstrap host); given a join URL naming node 2 in its role directory, it joins the cluster that exists as a learner and is promoted, with the same cluster ID before and after; and a role file naming the agent leaves its server container idle, with no k3s, until it is stopped.
scenario_rejoin() {
  local cluster_id password started_before refused_since
  voters_eligible=1
  reset_environment
  prepare_nodes hosted
  write_inventory hosted
  run_role
  assert_cluster

  log "Every node mounts its role directory read-only"
  for index in $(seq 1 "$node_count"); do
    [ "$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/etc/github-runner"}}{{.Source}} {{.RW}}{{end}}{{end}}' "$(container "$index")")" = "${work}/$(node_name "$index")/fleet-role.d false" ] \
      || fail "$(container "$index") does not mount ${work}/$(node_name "$index")/fleet-role.d read-only at /etc/github-runner"
  done
  cluster_id=$(etcd_cluster_id 1)
  [ -n "$cluster_id" ] || fail "could not read the etcd cluster ID"
  password=$(node_password_hash 1)
  log "The cluster ID is ${cluster_id}"

  log "Removing $(node_name 1)'s etcd member through $(node_name 2), as a demotion does"
  kubectl_on 2 annotate --overwrite node "$(node_name 1)" etcd.k3s.cattle.io/remove=true >/dev/null
  wait_for "k3s to record the removal of $(node_name 1)'s member" 300 removed_member_recorded
  wait_for "$(node_name 1)'s k3s to write its tombstone" 120 has_tombstone
  [ "$(started_members)" = 2 ] || { etcd_members 2; fail "expected two started etcd members after the removal"; }

  log "Restarted, the removed server idles on its tombstone instead of joining again"
  docker restart "$(container 1)" >/dev/null
  wait_for "$(node_name 1) to say it idles on its tombstone" 120 container_logged 1 "tombstone says this server's etcd member was removed from the cluster, and k3s started now would join it again as a new member, so k3s is not started"
  wait_for "$(node_name 1) to run nothing but the idle wait" 60 node1_idles
  [ "$(started_members)" = 2 ] || { etcd_members 2; fail "the removed server joined etcd again"; }
  [ "$(etcd_members 2 | wc -l | tr -d ' ')" = 2 ] || { etcd_members 2; fail "the removed server is a member again"; }

  log "With its datastore moved aside and no server to join, it refuses to start rather than bootstrap a second cluster"
  docker stop "$(container 1)" >/dev/null
  # The trap in the idle wait ends it on docker stop's SIGTERM; without it Docker would kill the container once its stop timeout ran out.
  [ "$(docker inspect --format '{{.State.ExitCode}}' "$(container 1)")" = 0 ] || fail "the idle container did not exit 0 on docker stop's SIGTERM"
  # The annotations the removal left are cleared as a demotion clears them, so the member that joins next is not taken for the one removed.
  kubectl_on 2 annotate node "$(node_name 1)" etcd.k3s.cattle.io/remove- etcd.k3s.cattle.io/removed-node-name- >/dev/null
  docker run --rm -v "$(node_name 1)_k3s-data:/data" "$busybox_image" mv /data/server/db /data/server/db.demoted
  refused_since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  docker start "$(container 1)" >/dev/null
  wait_for "$(node_name 1) to refuse to bootstrap a second cluster" 120 container_logged 1 "has no server to join, so starting k3s would bootstrap a second cluster; not starting it"
  # The refusal exits, so the restart policy keeps starting the container and each start refuses again; no start may get as far as k3s, whose first line names its version.
  if docker logs --since "$refused_since" "$(container 1)" 2>&1 | grep -F 'Starting k3s v' >/dev/null; then fail "$(node_name 1) started k3s with no server to join"; fi
  wait_for "$(node_name 1) to exit on its refusal and be started again by its restart policy" 120 node1_restarted
  [ "$(etcd_members 2 | wc -l | tr -d ' ')" = 2 ] || { etcd_members 2; fail "the members changed while $(node_name 1) refused to start"; }
  [ "$(etcd_cluster_id 2)" = "$cluster_id" ] || fail "the cluster ID changed while $(node_name 1) refused to start"

  log "Given a join URL naming $(node_name 2) in its role directory, it joins the cluster that exists"
  printf 'https://%s.mesh.grtest.internal:6443\n' "$(node_name 2)" > "${work}/$(node_name 1)/fleet-role.d/join-url"
  docker restart "$(container 1)" >/dev/null
  wait_for "$(node_name 1) to be a started voting member of etcd again" 600 node1_voter_started
  wait_for "$(node_name 1)'s Node to report EtcdIsVoter" 300 node1_is_voter
  local promoted=0 index
  for index in 2 3; do
    if container_logged "$index" "Promoted learner $(node_name 1)-"; then promoted=1; fi
  done
  [ "$promoted" = 1 ] || fail "no other server's k3s logged promoting $(node_name 1)'s learner"
  [ "$(started_members)" = 3 ] || { etcd_members 2; fail "expected three started etcd members after the rejoin"; }
  wait_for "$(node_name 1)'s etcd to answer" 300 etcd_answers 1
  [ "$(etcd_cluster_id 1)" = "$cluster_id" ] || fail "$(node_name 1) joined a cluster with another ID: $(etcd_cluster_id 1) (was ${cluster_id})"
  [ "$(node_password_hash 1)" = "$password" ] || fail "$(node_name 1)'s node password changed"
  docker run --rm -v "$(node_name 1)_k3s-data:/data" "$busybox_image" test -d /data/server/db.demoted || fail "the moved-aside datastore is gone"
  assert_cluster

  log "A role file naming the agent leaves the server container idle, with no k3s"
  printf 'agent\n' > "${work}/$(node_name 1)/fleet-role.d/role"
  docker restart "$(container 1)" >/dev/null
  wait_for "$(node_name 1) to say its role file names the agent" 120 container_logged 1 "/etc/github-runner/role names 'agent', not server, so k3s is not started"
  wait_for "$(node_name 1) to run nothing but the idle wait" 60 node1_idles
  started_before=$(docker inspect --format '{{.State.StartedAt}} {{.RestartCount}}' "$(container 1)")
  node1_idles || { node1_processes; fail "$(node_name 1) runs more than the idle wait"; }
  [ "$(docker inspect --format '{{.State.Running}}' "$(container 1)")" = true ] || fail "the idle container is not running"
  [ "$(docker inspect --format '{{.State.StartedAt}} {{.RestartCount}}' "$(container 1)")" = "$started_before" ] || fail "the idle container restarted"
}

# --- The role controller carrying out a voter swap (the role_swap scenarios) ---

# The controller's own waits, as the cluster role's defaults give them: github_runner_cluster_regrade_operation_timeout_seconds (the regrade pressure window), github_runner_cluster_native_runner_wait_timeout (its server ready timeout) and k3s's learnerMaxStallTime.
operation_timeout_seconds=300
runner_wait_seconds=600
learner_stall_seconds=300
# The most seconds a swap is waited for: the sum of every step's own bound (scripts/fleet-role-swap.jq's budget): the snapshot, the candidate's drain (the runner wait and an operation), the promotion (the learner stall time and an operation), the step-down, the removal, the finish and the clean-up of the outgoing host, an operation each.
swap_wait_seconds=$((7 * operation_timeout_seconds + runner_wait_seconds + learner_stall_seconds))

# Waits until the controller's status says no swap is under way and the last one that ended, from node $1 for node $2, ended as $3 (completed by default), saying the step the swap is at and what it waits for as they change.
wait_for_swap() {
  local out=$1 in=$2 outcome=${3:-completed} deadline=$((SECONDS + swap_wait_seconds)) seen='' now
  until role_status_is ".swap == null and .last_swap.outcome == \"${outcome}\" and .last_swap.out == \"${out}\" and .last_swap.in == \"${in}\""; do
    [ "$SECONDS" -lt "$deadline" ] || { role_status | jq '{swap, waiting, last_swap, blocks}' || true; fail "the swap of ${out} for ${in} did not end as ${outcome}"; }
    now=$(role_status | jq -r '(.swap.step // "none") + ": " + ((.waiting // []) | join("; "))' 2>/dev/null || true)
    if [ "$now" != "$seen" ]; then log "swap ${out} for ${in}: ${now}"; seen=$now; fi
    sleep 5
  done
  role_status | jq '{swap, last_swap}'
}

# Asks the controller, in its own pod as an owner does, for the swap of node $1 for node $2, once every gate holds: the command refuses while one does not, and the pod may be rescheduled meanwhile.
swap_command() { kubectl_on "$api_node" --namespace "$platform_namespace" exec deploy/fleet-role -- /app/scripts/fleet-role.sh swap "$1" "$2" </dev/null; }
request_swap() {
  log "Asking the controller for the swap of $1 for $2"
  wait_for "the controller to accept the swap of $1 for $2" 600 swap_command "$1" "$2"
}

# The number of nodes that carry the etcd label and report EtcdIsVoter True.
voter_count() {
  kubectl_on "$api_node" get nodes -o json </dev/null | jq '[.items[] | select(.metadata.labels["node-role.kubernetes.io/etcd"] == "true" and any(.status.conditions[]?; .type == "EtcdIsVoter" and .status == "True"))] | length'
}

# Whether node $1 carries no role label, etcd annotation, regrade level or swap annotation, and is schedulable.
node_is_clean() {
  kubectl_on "$api_node" get node "$1" -o json </dev/null | jq -e '
    ([.metadata.labels | keys[] | select(. == "node-role.kubernetes.io/etcd" or . == "node-role.kubernetes.io/control-plane" or . == "node-role.kubernetes.io/master" or . == "github-runner.exadev.com/regrade-level")] | length) == 0
    and ([.metadata.annotations | keys[] | select(startswith("etcd.k3s.cattle.io/") or startswith("github-runner.exadev.com/role-") or . == "github-runner.exadev.com/snapshot-request")] | length) == 0
    and ((.spec.unschedulable // false) | not)
    and ([(.spec.taints // [])[] | select(.key == "node-role.kubernetes.io/control-plane")] | length) == 0' >/dev/null
}

# Whether node $1 carries no swap annotation, regrade level or cordon: a voter, or an agent, that no swap has touched.
node_untouched() {
  kubectl_on "$api_node" get node "$1" -o json </dev/null | jq -e '
    ([.metadata.labels | keys[] | select(. == "github-runner.exadev.com/regrade-level")] | length) == 0
    and ([.metadata.annotations | keys[] | select(startswith("github-runner.exadev.com/role-") or . == "github-runner.exadev.com/snapshot-request" or . == "etcd.k3s.cattle.io/remove")] | length) == 0
    and ((.spec.unschedulable // false) | not)' >/dev/null
}

# Whether the regrade Lease is free or gone: nobody holds it.
lease_released() { [ -z "$(kubectl_on "$api_node" get lease fleet-regrade --namespace kube-system -o 'jsonpath={.spec.holderIdentity}' 2>/dev/null </dev/null || true)" ]; }

agent_password_hash() { docker exec "grtest-node$1-agent" sha256sum /etc/rancher/node/password </dev/null | cut -d' ' -f1; }
agent_address() { docker exec "grtest-node$1-agent" tailscale ip -4 </dev/null | head -n 1; }

# Brings up three Docker servers and an eligible native standby with the role controller on and none pinned, runs the probes and the controller from this checkout's image, and waits until every gate holds. Reads and writes go through node 2, which keeps its server through every swap here.
setup_swap_cluster() {
  role_controller=1
  native_eligible=1
  pin_first=0
  api_node=2
  reset_environment
  prepare_nodes hosted
  start_native_host
  native_role=agent
  write_inventory hosted
  log "Bringing up three Docker servers, then the native host as an eligible agent, all with the role controller on"
  run_role --limit "$(node_name 1),$(node_name 2),$(node_name 3)"
  wait_for "every Docker server's API to serve and every Docker node to be Ready" 600 docker_servers_serving
  run_role --limit "$native_host"
  wait_for "${native_host} to be Ready as an agent" 300 native_node_ready
  wait_for "${native_host}'s role actuator to run" 120 native_unit_is fleet-role-actuator active
  local index
  for index in 1 2 3; do wait_for "$(node_name "$index")'s role actuator service to run" 120 container_is "$(node_name "$index")-role-actuator-1" running; done
  deploy_role_controller "$swap_sample_seconds"
  log "Waiting for the controller to see three voters and every gate holding"
  wait_for "the gates to hold with three voters" 900 role_status_is '(.gates | length == 0) and (.membership.voters | length == 3)'
  role_status | jq '{membership, gates, blocks}'
}

# --- The role actuator switching a native host (the role_actuator scenario) ---

# The most seconds each step of the role_actuator scenario is waited for, the native_server scenario's bound for a learner's catch-up and promotion, and how long the scenario's Lease lasts: one such wait for each of the steps it holds the Lease across (the promotion, the vote, the step-down, the removal and the finish), so it never runs out while the test acts as the controller and renews nothing.
actuator_wait_seconds=600
actuator_lease_seconds=$((5 * actuator_wait_seconds))
actuator_prefix=github-runner.exadev.com

# The last report of the node $1 (the native host's by default) in the actuators' ConfigMap, or {}.
actuator_report() {
  kubectl_on "$api_node" --namespace "$platform_namespace" get configmap fleet-role-actuators -o json 2>/dev/null </dev/null | jq -c --arg node "${1:-$native_host}" '.data[$node] // "{}" | fromjson'
}
# Whether the last report of the node $3 (the native host's by default) is for the step $1 in the phase $2.
actuator_reported() { [ "$(actuator_report "${3:-}" | jq -r '.step + " " + .phase' 2>/dev/null)" = "$1 $2" ]; }
# Whether the Node $2 (the native host's by default) reports the Ready condition True and its kubelet has renewed the node's Lease (kube-node-lease/<node>) at or after the time $1 (RFC 3339, UTC), as tasks/native/server_ready.yml judges a restart: the API keeps a stopped kubelet's Ready condition until the node controller's grace period runs out, so the condition alone could be the server's from before its step-down. Read from the condition, not the STATUS column, which says Ready,SchedulingDisabled while the node is cordoned. Both times are cut to whole seconds before they are compared, as strings, since RFC 3339 in UTC sorts as it reads.
node_ready_since() {
  local renewed node=${2:-$native_host}
  [ "$(kubectl_on "$api_node" get node "$node" -o 'jsonpath={.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null </dev/null)" = True ] || return 1
  renewed=$(kubectl_on "$api_node" --namespace kube-node-lease get lease "$node" -o 'jsonpath={.spec.renewTime}' 2>/dev/null </dev/null) || return 1
  [ -n "$renewed" ] || return 1
  [[ ! "${renewed:0:19}" < "${1:0:19}" ]]
}
# Whether k3s has recorded the removal of the etcd member of the Node $1 (the native host's by default).
member_removed() { [ -n "$(kubectl_on "$api_node" get node "${1:-$native_host}" -o 'jsonpath={.metadata.annotations.etcd\.k3s\.cattle\.io/removed-node-name}' 2>/dev/null </dev/null)" ]; }
started_member_count() { etcd_members 2>/dev/null | grep -c ', started, ' || true; }

# Holds the regrade Lease as the role controller does for the whole of the swap $1 of the nodes $2.
hold_swap_lease() {
  local now
  now=$(date -u +%Y-%m-%dT%H:%M:%S.000000Z)
  kubectl_on 1 apply -f - >/dev/null <<EOF
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata:
  name: fleet-regrade
  namespace: kube-system
  annotations:
    ${actuator_prefix}/swap-id: "$1"
    ${actuator_prefix}/swap-nodes: "$2"
spec:
  holderIdentity: fleet-role
  leaseDurationSeconds: ${actuator_lease_seconds}
  acquireTime: "${now}"
  renewTime: "${now}"
EOF
}

# Records, once a second until the stop file appears, every moment the native host runs both k3s units.
watch_native_units() {
  local log="$1" stop="$2"
  : > "$log"
  until [ -e "$stop" ]; do
    if native_unit_is k3s active && native_unit_is k3s-agent active; then echo "$(date -u +%H:%M:%S) k3s and k3s-agent both active" >> "$log"; fi
    sleep 1
  done
}

# The role actuator's native part: three Docker servers and a native standby, every host eligible with the role controller on, and the test acting as the controller, holding the Lease and writing the Node's label and annotations by hand. The actuator promotes the standby (a learner, then a voter, with writes succeeding throughout), steps it down (its etcd serving at etcd-only and no pod's shim left), it is removed through k3s's annotation, and the actuator finishes it as an agent, keeping its node password and Tailscale address and moving its datastore aside, with the two units never running together. role_actuator_docker then does the same to a Docker server and promotes it again.
scenario_role_actuator() {
  local swap password address writes members stop units writer watcher unit_watcher list ok failed
  role_controller=1
  native_eligible=1
  reset_environment
  prepare_nodes hosted
  start_native_host
  native_role=agent
  write_inventory hosted
  log "Bringing up three Docker servers, then the native host as an eligible agent, all with the role controller on"
  run_role --limit "$(node_name 1),$(node_name 2),$(node_name 3)"
  wait_for "every Docker server's API to serve and every Docker node to be Ready" 600 docker_servers_serving
  run_role --limit "$native_host"
  wait_for "${native_host} to be Ready as an agent" 300 native_node_ready
  wait_for "${native_host}'s role actuator to run" 120 native_unit_is fleet-role-actuator active
  native_exec test -f /etc/rancher/k3s/server.yaml </dev/null || fail "${native_host} is eligible but has no staged server configuration"
  password=$(native_exec sha256sum /etc/rancher/node/password </dev/null | cut -d' ' -f1)
  address=$(native_exec tailscale ip -4 </dev/null | head -n 1)
  [ "$(started_member_count)" = 3 ] || { etcd_members; fail "expected three started etcd members before the swap"; }

  writes="${work}/actuator-writes.log"
  members="${work}/actuator-member.log"
  units="${work}/actuator-units.log"
  stop="${work}/actuator-stop"
  rm -f "$stop"
  watch_native_units "$units" "$stop" &
  unit_watcher=$!
  swap="$(date -u +%Y%m%dT%H%M%SZ)-$(node_name 3)-${native_host}"
  hold_swap_lease "$swap" "$(node_name 3),${native_host}"

  log "Promoting ${native_host} through its Node, as the controller asks, with writes throughout"
  kubectl_on 1 cordon "$native_host" >/dev/null
  kubectl_on 1 drain "$native_host" --ignore-daemonsets --delete-emptydir-data --timeout="${actuator_wait_seconds}s" >/dev/null
  write_throughout "$writes" "$stop" &
  writer=$!
  watch_native_member "$members" "$stop" &
  watcher=$!
  kubectl_on 1 label --overwrite node "$native_host" "${actuator_prefix}/desired-role=server" >/dev/null
  kubectl_on 1 annotate --overwrite node "$native_host" "${actuator_prefix}/role-swap=${swap}" "${actuator_prefix}/role-step=promote" "${actuator_prefix}/role-join-node=$(node_name 1)" >/dev/null
  wait_for "${native_host}'s actuator to report the promotion done" "$actuator_wait_seconds" actuator_reported promote "done"
  wait_for "${native_host} to be a voting member of etcd" "$actuator_wait_seconds" node_is_voter
  touch "$stop"
  wait "$writer" "$watcher"
  actuator_report
  list=$(etcd_members)
  echo "$list"
  [ "$(echo "$list" | grep -c ', started, ')" = 4 ] || fail "expected four started etcd members after the promotion"
  echo "$list" | grep -F ", ${native_host}-" | grep -Eq ', started, .*, false$' || fail "${native_host}'s member is not a started voter"
  local promoted=0 index
  for index in 1 2 3; do
    if docker logs "$(container "$index")" 2>&1 | grep -F "Promoted learner ${native_host}-" >/dev/null; then promoted=1; fi
  done
  [ "$promoted" = 1 ] || fail "no server's k3s logged promoting ${native_host}'s learner"
  if grep -Eq ', true$' "$members"; then log "The member watch saw ${native_host} as a learner"; else log "The member watch did not catch ${native_host} as a learner (promoted between reads); the promotion is in the servers' logs"; fi
  ok=$(grep -c '^ok$' "$writes" || true)
  failed=$(grep -c '^failed' "$writes" || true)
  echo "  ${ok} writes succeeded, ${failed} failed"
  [ "$ok" -gt 0 ] || fail "no write was made during the promotion"
  [ "$failed" = 0 ] || { grep '^failed' "$writes"; fail "writes failed during the promotion"; }
  native_unit_is k3s active || fail "${native_host} does not run k3s"
  native_unit_is k3s-agent inactive || fail "${native_host} still runs k3s-agent"
  [ "$(native_exec cat /var/lib/rancher/k3s/fleet-role </dev/null)" = server ] || fail "the role file does not name the server"
  native_exec grep -Fx "server: \"https://$(node_name 1).mesh.grtest.internal:6443\"" /etc/rancher/k3s/server.yaml.d/20-fleet-role-join.yaml </dev/null >/dev/null || fail "the join drop-in does not name $(node_name 1)"
  native_exec grep -Fx "Environment=\"GITHUB_RUNNER_JOIN_HOST=$(node_name 1).mesh.grtest.internal\"" /etc/systemd/system/k3s.service.d/20-fleet-role-join.conf </dev/null >/dev/null || fail "the unit's join drop-in does not name $(node_name 1)"
  kubectl_on 1 uncordon "$native_host" >/dev/null

  log "Stepping ${native_host} down, as the controller asks: drained, labelled etcd-only, desired-role agent"
  kubectl_on 1 taint --overwrite node "$native_host" node-role.kubernetes.io/control-plane=true:NoSchedule >/dev/null
  kubectl_on 1 drain "$native_host" --ignore-daemonsets --delete-emptydir-data --timeout="${actuator_wait_seconds}s" >/dev/null
  kubectl_on 1 label --overwrite node "$native_host" "${actuator_prefix}/regrade-level=etcd-only" "${actuator_prefix}/desired-role=agent" >/dev/null
  kubectl_on 1 annotate --overwrite node "$native_host" "${actuator_prefix}/role-step=step-down" >/dev/null
  wait_for "${native_host}'s actuator to report the step-down done" "$actuator_wait_seconds" actuator_reported step-down "done"
  actuator_report
  [ "$(native_exec cat /run/github-runner-k3s-server/regrade-level </dev/null)" = etcd-only ] || fail "${native_host}'s server did not start at etcd-only"
  native_etcd_serving || fail "${native_host}'s etcd is not serving at etcd-only"
  [ "$(native_shims)" = 0 ] || fail "$(native_shims) containerd shims are left on ${native_host} at etcd-only"
  [ "$(native_exec cat /var/lib/rancher/k3s/fleet-role </dev/null)" = agent ] || fail "the role file does not name the agent after the step-down"
  [ "$(started_member_count)" = 4 ] || { etcd_members; fail "the step-down changed the started members"; }

  log "Removing ${native_host}'s member through k3s's annotation, after which its actuator finishes it as an agent"
  kubectl_on 1 annotate --overwrite node "$native_host" etcd.k3s.cattle.io/remove=true >/dev/null
  wait_for "k3s to record the removal of ${native_host}'s member" "$actuator_wait_seconds" member_removed
  wait_for "${native_host}'s actuator to report the demotion finished" "$actuator_wait_seconds" actuator_reported finish-demote "done"
  actuator_report
  [ "$(started_member_count)" = 3 ] || { etcd_members; fail "expected three started etcd members after the removal"; }
  [ "$(etcd_members | wc -l | tr -d ' ')" = 3 ] || { etcd_members; fail "the removed member is still listed"; }
  native_unit_is k3s inactive || fail "${native_host} still runs k3s"
  native_unit_is k3s-agent active || fail "${native_host} does not run k3s-agent"
  native_exec test -d /var/lib/rancher/k3s/server/db.demoted </dev/null || fail "${native_host}'s datastore was not moved aside"
  if native_exec test -e /var/lib/rancher/k3s/server/db </dev/null; then fail "${native_host} still has a datastore in place"; fi
  [ "$(native_exec sha256sum /etc/rancher/node/password </dev/null | cut -d' ' -f1)" = "$password" ] || fail "${native_host}'s node password changed"
  [ "$(native_exec tailscale ip -4 </dev/null | head -n 1)" = "$address" ] || fail "${native_host}'s Tailscale address changed"

  log "Cleaning the Node up as the controller does, and waiting for the agent"
  kubectl_on 1 label node "$native_host" node-role.kubernetes.io/control-plane- node-role.kubernetes.io/etcd- node-role.kubernetes.io/master- \
    "${actuator_prefix}/regrade-level-" "${actuator_prefix}/desired-role-" >/dev/null
  kubectl_on 1 annotate node "$native_host" etcd.k3s.cattle.io/remove- etcd.k3s.cattle.io/removed-node-name- etcd.k3s.cattle.io/node-name- etcd.k3s.cattle.io/node-address- \
    "${actuator_prefix}/role-swap-" "${actuator_prefix}/role-step-" "${actuator_prefix}/role-join-node-" >/dev/null
  kubectl_on 1 taint node "$native_host" node-role.kubernetes.io/control-plane=true:NoSchedule- >/dev/null
  wait_for "${native_host} to be Ready as an agent again" "$actuator_wait_seconds" node_ready_since "$(actuator_report | jq -r .started_at)"
  kubectl_on 1 uncordon "$native_host" >/dev/null
  kubectl_on 1 delete lease fleet-regrade --namespace kube-system >/dev/null
  touch "$stop"
  wait "$unit_watcher"
  [ ! -s "$units" ] || { cat "$units"; fail "${native_host} ran both k3s units at once"; }
  [ "$(kubectl_on 1 get nodes --no-headers | grep -c "^${native_host} ")" = 1 ] || fail "${native_host} is not exactly one Node"

  role_actuator_docker
}

# A Docker host's container state, or nothing for one that does not exist.
docker_state() { docker ps --all --filter "name=^/$1\$" --format '{{.State}}'; }
container_is() { [ "$(docker_state "$1")" = "$2" ]; }
# Whether the node container $1's Tailscale client has a mesh address.
has_mesh_address() { [ -n "$(docker exec "$1" tailscale ip -4 </dev/null 2>/dev/null)" ]; }
# Runs a shell command against the named volume, mounted at /volume, in a throwaway container.
in_volume() { docker run --rm --volume "$1:/volume" "$busybox_image" sh -c "$2" </dev/null; }

# The role actuator's Docker part, after the native part: the third Docker server, whose role-actuator service the role runs since the controller is on, is demoted by the Node's annotations set by hand, as the controller sets them, and then promoted again. Its agent must keep the server's node password and Tailscale address as one Node, with the server's datastore moved aside in its k3s-data volume, and the promoted server must join the cluster it left, with the same etcd cluster ID.
role_actuator_docker() {
  local node server agent actuator project cluster_id password address swap
  node=$(node_name 3)
  server=$(container 3)
  agent="grtest-node3-agent"
  project="$node"
  actuator="${project}-role-actuator-1"
  wait_for "${node}'s role actuator service to run" 120 container_is "$actuator" running
  cluster_id=$(etcd_cluster_id 1)
  [ -n "$cluster_id" ] || fail "could not read the etcd cluster ID"
  password=$(docker exec "$server" sha256sum /etc/rancher/node/password </dev/null | cut -d' ' -f1)
  address=$(docker exec "$server" tailscale ip -4 </dev/null | head -n 1)
  swap="$(date -u +%Y%m%dT%H%M%SZ)-${node}-${native_host}"
  hold_swap_lease "$swap" "$node"

  log "Stepping ${node}, a Docker server, down through its role actuator service, as the controller asks"
  kubectl_on 1 cordon "$node" >/dev/null
  kubectl_on 1 taint --overwrite node "$node" node-role.kubernetes.io/control-plane=true:NoSchedule >/dev/null
  kubectl_on 1 drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout="${actuator_wait_seconds}s" >/dev/null
  kubectl_on 1 label --overwrite node "$node" "${actuator_prefix}/regrade-level=etcd-only" "${actuator_prefix}/desired-role=agent" >/dev/null
  kubectl_on 1 annotate --overwrite node "$node" "${actuator_prefix}/role-swap=${swap}" "${actuator_prefix}/role-step=step-down" >/dev/null
  wait_for "${node}'s actuator to report the step-down done" "$actuator_wait_seconds" actuator_reported step-down "done" "$node"
  actuator_report "$node"
  [ "$(docker exec "$server" cat /run/regrade-level </dev/null)" = etcd-only ] || fail "${node}'s server did not start at etcd-only"
  [ "$(cat "${work}/${node}/fleet-role.d/role")" = agent ] || fail "${node}'s role file does not name the agent after the step-down"

  log "Removing ${node}'s member through k3s's annotation, after which its actuator finishes it as an agent"
  kubectl_on 1 annotate --overwrite node "$node" etcd.k3s.cattle.io/remove=true >/dev/null
  wait_for "k3s to record the removal of ${node}'s member" "$actuator_wait_seconds" member_removed "$node"
  wait_for "${node}'s actuator to report the demotion finished" "$actuator_wait_seconds" actuator_reported finish-demote "done" "$node"
  actuator_report "$node"
  [ "$(docker_state "$server")" = exited ] || fail "${node}'s server container is $(docker_state "$server"), not stopped"
  [ "$(docker_state "$agent")" = running ] || fail "${node}'s agent container is not running"
  in_volume "${project}_k3s-data" 'test -d /volume/server/db.demoted && test ! -e /volume/server/db' || fail "${node}'s datastore was not moved aside in its k3s-data volume"
  [ "$(docker exec "$agent" sha256sum /etc/rancher/node/password </dev/null | cut -d' ' -f1)" = "$password" ] || fail "${node}'s agent has another node password"
  wait_for "${node}'s agent to be on the mesh" 120 has_mesh_address "$agent"
  [ "$(docker exec "$agent" tailscale ip -4 </dev/null | head -n 1)" = "$address" ] || fail "${node}'s agent has another Tailscale address"

  log "Cleaning ${node}'s Node up as the controller does, and waiting for its agent"
  kubectl_on 1 label node "$node" node-role.kubernetes.io/control-plane- node-role.kubernetes.io/etcd- node-role.kubernetes.io/master- \
    "${actuator_prefix}/regrade-level-" "${actuator_prefix}/desired-role-" >/dev/null
  kubectl_on 1 annotate node "$node" etcd.k3s.cattle.io/remove- etcd.k3s.cattle.io/removed-node-name- etcd.k3s.cattle.io/node-name- etcd.k3s.cattle.io/node-address- \
    "${actuator_prefix}/role-swap-" "${actuator_prefix}/role-step-" >/dev/null
  kubectl_on 1 taint node "$node" node-role.kubernetes.io/control-plane=true:NoSchedule- >/dev/null
  wait_for "${node} to be Ready as an agent" "$actuator_wait_seconds" node_ready_since "$(actuator_report "$node" | jq -r .started_at)" "$node"
  kubectl_on 1 uncordon "$node" >/dev/null
  [ "$(kubectl_on 1 get nodes --no-headers | grep -c "^${node} ")" = 1 ] || fail "${node} is not exactly one Node"

  log "Promoting ${node} again through its role actuator service: it must join the cluster it left"
  swap="$(date -u +%Y%m%dT%H%M%SZ)-${native_host}-${node}"
  hold_swap_lease "$swap" "$node"
  kubectl_on 1 cordon "$node" >/dev/null
  kubectl_on 1 drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout="${actuator_wait_seconds}s" >/dev/null
  kubectl_on 1 label --overwrite node "$node" "${actuator_prefix}/desired-role=server" >/dev/null
  kubectl_on 1 annotate --overwrite node "$node" "${actuator_prefix}/role-swap=${swap}" "${actuator_prefix}/role-step=promote" "${actuator_prefix}/role-join-node=$(node_name 1)" >/dev/null
  wait_for "${node}'s actuator to report the promotion done" "$actuator_wait_seconds" actuator_reported promote "done" "$node"
  wait_for "${node} to be a voting member of etcd" "$actuator_wait_seconds" node_is_voter "$node"
  actuator_report "$node"
  [ "$(docker_state "$agent")" = exited ] || fail "${node}'s agent container is $(docker_state "$agent"), not stopped"
  [ "$(docker exec "$server" sha256sum /etc/rancher/node/password </dev/null | cut -d' ' -f1)" = "$password" ] || fail "${node}'s server has another node password"
  [ "$(docker exec "$server" tailscale ip -4 </dev/null | head -n 1)" = "$address" ] || fail "${node}'s server has another Tailscale address"
  wait_for "${node}'s etcd to answer" "$actuator_wait_seconds" etcd_answers 3
  [ "$(etcd_cluster_id 3)" = "$cluster_id" ] || fail "${node} joined a cluster with another ID: $(etcd_cluster_id 3) (was ${cluster_id})"
  [ "$(started_member_count)" = 3 ] || { etcd_members; fail "expected three started etcd members after ${node}'s promotion"; }
  kubectl_on 1 uncordon "$node" >/dev/null
  kubectl_on 1 delete lease fleet-regrade --namespace kube-system >/dev/null
  [ "$(kubectl_on 1 get nodes --no-headers | grep -c "^${node} ")" = 1 ] || fail "${node} is not exactly one Node"
}

# A manual swap by the controller's own command: the native standby is promoted and the Docker bootstrap host demoted in one swap, with writes succeeding throughout, then the bootstrap host is promoted again in a second swap, joining the cluster it left.
scenario_role_swap() {
  local password address native_password cluster_id writes stop writer units unit_watcher ok failed node
  setup_swap_cluster
  node=$(node_name 1)
  password=$(node_password_hash 1)
  address=$(docker exec "$(container 1)" tailscale ip -4 </dev/null | head -n 1)
  native_password=$(native_exec sha256sum /etc/rancher/node/password </dev/null | cut -d' ' -f1)
  cluster_id=$(etcd_cluster_id 2)
  [ -n "$cluster_id" ] || fail "could not read the etcd cluster ID"
  writes="${work}/swap-writes.log"
  units="${work}/swap-units.log"
  stop="${work}/swap-stop"
  rm -f "$stop"
  write_throughout "$writes" "$stop" &
  writer=$!
  watch_native_units "$units" "$stop" &
  unit_watcher=$!

  request_swap "$node" "$native_host"
  wait_for_swap "$node" "$native_host"
  [ "$(voter_count)" = 3 ] || { etcd_members; fail "expected three voters after the swap, got $(voter_count)"; }
  node_is_voter "$native_host" || fail "${native_host} is not a voter"
  [ "$(started_member_count)" = 3 ] || { etcd_members; fail "expected three started etcd members after the swap"; }
  [ "$(etcd_members | wc -l | tr -d ' ')" = 3 ] || { etcd_members; fail "the removed member is still listed"; }
  etcd_members | grep -F ", ${native_host}-" | grep -Eq ', started, .*, false$' || fail "${native_host}'s member is not a started voter"
  node_is_clean "$node" || fail "${node}'s labels and annotations were not cleaned up"
  lease_released || fail "the regrade Lease was not released"
  [ "$(docker_state "$(container 1)")" = exited ] || fail "${node}'s server container is $(docker_state "$(container 1)"), not stopped"
  wait_for "${node}'s agent to run" 300 container_is "grtest-node1-agent" running
  in_volume "${node}_k3s-data" 'test -d /volume/server/db.demoted && test ! -e /volume/server/db' || fail "${node}'s datastore was not moved aside in its k3s-data volume"
  [ "$(cat "${work}/${node}/fleet-role.d/role")" = agent ] || fail "${node}'s role file does not name the agent"
  [ "$(agent_password_hash 1)" = "$password" ] || fail "${node}'s agent has another node password"
  wait_for "${node}'s agent to be on the mesh" 120 has_mesh_address grtest-node1-agent
  [ "$(agent_address 1)" = "$address" ] || fail "${node}'s agent has another Tailscale address"
  wait_for "${node}'s agent to be Ready on a heartbeat after its start" 300 node_ready_since "$(actuator_report "$node" | jq -r .started_at)" "$node"
  [ "$(kubectl_on "$api_node" get nodes --no-headers | grep -c "^${node} ")" = 1 ] || fail "${node} is not exactly one Node"
  [ "$(native_exec sha256sum /etc/rancher/node/password </dev/null | cut -d' ' -f1)" = "$native_password" ] || fail "${native_host}'s node password changed"
  [ "$(etcd_cluster_id 2)" = "$cluster_id" ] || fail "the etcd cluster ID changed during the swap"

  log "A second swap brings the former bootstrap host back in, taking the native host out, and it joins the cluster it left"
  request_swap "$native_host" "$node"
  wait_for_swap "$native_host" "$node"
  touch "$stop"
  wait "$writer" "$unit_watcher"
  [ "$(voter_count)" = 3 ] || { etcd_members; fail "expected three voters after the second swap, got $(voter_count)"; }
  node_is_voter "$node" || fail "${node} is not a voter again"
  wait_for "${node}'s etcd to answer" "$actuator_wait_seconds" etcd_answers 1
  [ "$(etcd_cluster_id 1)" = "$cluster_id" ] || fail "${node} joined a cluster with another ID: $(etcd_cluster_id 1) (was ${cluster_id})"
  [ "$(etcd_cluster_id 2)" = "$cluster_id" ] || fail "the etcd cluster ID changed after the second swap"
  [ "$(started_member_count)" = 3 ] || { etcd_members; fail "expected three started etcd members after the second swap"; }
  [ "$(docker_state grtest-node1-agent)" = exited ] || fail "${node}'s agent container is $(docker_state grtest-node1-agent), not stopped"
  [ "$(node_password_hash 1)" = "$password" ] || fail "${node}'s server has another node password"
  [ "$(docker exec "$(container 1)" tailscale ip -4 </dev/null | head -n 1)" = "$address" ] || fail "${node}'s server has another Tailscale address"
  native_unit_is k3s-agent active || fail "${native_host} does not run k3s-agent"
  ! native_unit_is k3s active || fail "${native_host} still runs k3s"
  native_exec test -d /var/lib/rancher/k3s/server/db.demoted </dev/null || fail "${native_host}'s datastore was not moved aside"
  node_is_clean "$native_host" || fail "${native_host}'s labels and annotations were not cleaned up"
  lease_released || fail "the regrade Lease was not released after the second swap"
  [ ! -s "$units" ] || { cat "$units"; fail "${native_host} ran both k3s units at once"; }
  ok=$(grep -c '^ok$' "$writes" || true)
  failed=$(grep -c '^failed' "$writes" || true)
  echo "  ${ok} writes succeeded, ${failed} failed"
  [ "$ok" -gt 0 ] || fail "no write was made during the swaps"
  assert_writes_recovered "$writes" || fail "writes did not recover within a leader election during the swaps"
}

# The outgoing voter is paused (its k3s container, as a host that dropped off would be) the moment the new voter votes: the controller still takes it out while the others are healthy, ends the swap with the host left to node recovery, and on unpause the tombstone keeps the removed server out of etcd (the member count stays three through a learner stall time) while its actuator finishes it as an agent.
scenario_role_swap_paused() {
  local out out_index out_node controller_node deadline
  setup_swap_cluster
  api_node=1
  controller_node=$(role_pod_node fleet-role)
  out_index=3
  [ "$(node_name "$out_index")" != "$controller_node" ] || out_index=2
  out=$(node_name "$out_index")
  out_node=$out_index
  request_swap "$out" "$native_host"
  wait_for "${native_host} to be a voting member of etcd" 1800 node_is_voter
  docker pause "$(container "$out_node")" >/dev/null
  log "${out}'s server is paused, after ${native_host} voted"
  wait_for_swap "$out" "$native_host"
  role_status | jq -e '.last_swap.outgoing_pending == true' >/dev/null || log "the swap did not record the outgoing host as pending: $(role_status | jq -c .last_swap)"
  [ "$(voter_count)" = 3 ] || { etcd_members 1; fail "expected three voters with ${out} paused, got $(voter_count)"; }
  [ "$(etcd_members 1 | grep -c ", ${out}-" || true)" = 0 ] || { etcd_members 1; fail "${out}'s member is still listed"; }
  [ "$(started_member_count)" -ge 3 ] || { etcd_members 1; fail "expected three started etcd members with ${out} paused"; }
  lease_released || fail "the regrade Lease was not released"

  log "Unpaused, ${out} must not rejoin etcd: the member count stays three for a learner stall time, and its actuator finishes it as an agent"
  docker unpause "$(container "$out_node")" >/dev/null
  deadline=$((SECONDS + learner_stall_seconds + 30))
  while [ "$SECONDS" -lt "$deadline" ]; do
    [ "$(etcd_members 1 | wc -l | tr -d ' ')" = 3 ] || { etcd_members 1; fail "${out} is a member of etcd again"; }
    sleep 10
  done
  wait_for "${out}'s agent to run" 600 container_is "grtest-node${out_node}-agent" running
  in_volume "${out}_k3s-data" 'test -d /volume/server/db.demoted && test ! -e /volume/server/db' || fail "${out}'s datastore was not moved aside"
  [ "$(cat "${work}/${out}/fleet-role.d/role")" = agent ] || fail "${out}'s role file does not name the agent"
  [ "$(docker_state "$(container "$out_node")")" = exited ] || fail "${out}'s server container is $(docker_state "$(container "$out_node")"), not stopped"
  [ "$(etcd_members 1 | wc -l | tr -d ' ')" = 3 ] || { etcd_members 1; fail "the member count changed after ${out} was finished"; }
}

# The candidate's peer port is dropped in its network namespace, so its learner never catches up: k3s removes it after a learner stall time, the controller's deadline passes and it rolls the promotion back, leaving the members and the voters as they were and the host an agent again.
scenario_role_swap_blocked_peer() {
  local out members_before out_index
  setup_swap_cluster
  api_node=1
  out_index=3
  out=$(node_name "$out_index")
  members_before=$(etcd_member_ids 1)
  docker run --rm --network "container:${native_host}" --cap-add NET_ADMIN --entrypoint sh "$tc_image" -c 'apk add --quiet iptables && iptables -A INPUT -p tcp --dport 2380 -j DROP && iptables -A OUTPUT -p tcp --dport 2380 -j DROP' </dev/null
  log "TCP 2380 is dropped in ${native_host}'s network namespace"
  request_swap "$out" "$native_host"
  wait_for_swap "$out" "$native_host" rolled-back
  [ "$(etcd_member_ids 1)" = "$members_before" ] || fail "the etcd members changed: $(etcd_member_ids 1) (was ${members_before})"
  [ "$(voter_count)" = 3 ] || fail "expected three voters after the rollback, got $(voter_count)"
  node_is_voter "$out" || fail "${out} is no longer a voter"
  ! node_is_voter "$native_host" || fail "${native_host} is a voter"
  native_unit_is k3s-agent active || fail "${native_host} does not run k3s-agent after the rollback"
  ! native_unit_is k3s active || fail "${native_host} still runs k3s after the rollback"
  [ "$(native_exec cat /var/lib/rancher/k3s/fleet-role </dev/null)" = agent ] || fail "${native_host}'s role file does not name the agent"
  node_is_clean "$native_host" || fail "${native_host}'s labels and annotations were not cleaned up"
  node_untouched "$out" || fail "${out} was changed by the rolled-back swap"
  lease_released || fail "the regrade Lease was not released"
  wait_for "${native_host} to be Ready as an agent" 300 native_node_ready
  [ "$(started_member_count)" = 3 ] || { etcd_members; fail "expected three started etcd members after the rollback"; }
}

# A voter is paused before the swap is asked for: the command exits 1 naming it, and no label, annotation or Lease is set.
scenario_role_swap_refused() {
  local paused controller_node out
  setup_swap_cluster
  api_node=1
  controller_node=$(role_pod_node fleet-role)
  paused=3
  [ "$(node_name "$paused")" != "$controller_node" ] || paused=2
  out=$(node_name 1)
  docker pause "$(container "$paused")" >/dev/null
  wait_for "the controller to report a gate failing for $(node_name "$paused")" 600 role_status_is ".gates | any(contains(\"$(node_name "$paused")\"))"
  local output status=0
  output=$(swap_command "$out" "$native_host" 2>&1) || status=$?
  echo "$output"
  [ "$status" = 1 ] || fail "the swap command exited ${status}, not 1"
  grep -F "$(node_name "$paused")" <<< "$output" >/dev/null || fail "the refusal does not name $(node_name "$paused")"
  docker unpause "$(container "$paused")" >/dev/null
  if kubectl_on "$api_node" get node "$native_host" -o json </dev/null | jq -e '(.metadata.labels | has("github-runner.exadev.com/desired-role")) or ([.metadata.annotations | keys[] | select(startswith("github-runner.exadev.com/role-") or . == "github-runner.exadev.com/snapshot-request")] | length > 0)' >/dev/null; then fail "a label or annotation was set on ${native_host}"; fi
  node_untouched "$out" || fail "${out} was changed by a refused request"
  lease_released || fail "the regrade Lease was taken by a refused request"
  sleep 30
  role_status | jq -e '.swap == null and .request == null and .last_swap == null' >/dev/null || fail "a refused request left a swap or a request: $(role_status | jq -c '{swap, request, last_swap}')"
}

scenarios=("$@")
[ "${#scenarios[@]}" -gt 0 ] || scenarios=(existing hosted in_cluster)
for scenario in "${scenarios[@]}"; do
  log "Scenario: ${scenario}"
  case "$scenario" in
    hosted | in_cluster) scenario_cluster "$scenario" ;;
    existing) scenario_existing ;;
    regrade) scenario_regrade ;;
    native_server) scenario_native_server ;;
    role_controller) scenario_role_controller ;;
    role_controller_swap) scenario_role_controller_swap ;;
    rejoin) scenario_rejoin ;;
    role_actuator) scenario_role_actuator ;;
    role_swap) scenario_role_swap ;;
    role_swap_paused) scenario_role_swap_paused ;;
    role_swap_blocked_peer) scenario_role_swap_blocked_peer ;;
    role_swap_refused) scenario_role_swap_refused ;;
    host_network) scenario_host_network ;;
    compose_faulty) scenario_compose_faulty ;;
    context_checks) scenario_context_checks ;;
    # Re-checks a cluster a previous run kept with GRTEST_KEEP=1 and the same GRTEST_WORK, without rebuilding it.
    assert) assert_cluster ;;
    *) fail "unknown scenario ${scenario}" ;;
  esac
  log "Scenario ${scenario} passed"
done
