#!/usr/bin/env bash
# Litestream and warm-standby test for the headscale_hosted mesh provider. Runs the cluster role's mesh step (tests/litestream/headscale.yml) against two Headscale hosts on this machine's Docker (a server and a warm standby, each its own Compose project) and an S3-compatible store, registers a real Tailscale client, then promotes the standby with playbooks/promote_headscale_standby.yml and checks that users, nodes and pre-auth keys came through, that the client reconnects to the promoted server, and that the promoted server keeps replicating. It also checks that headscale_in_cluster refuses to run without Litestream.
#
# Everything the Tailscale client, the S3 store and Litestream talk to is on an internal Docker network with no route out, and Headscale serves a DERP map of its own, so nothing contacts Tailscale or any other outside service. A network alias on each project's TLS proxy stands in for the DNS name the promotion playbook tells the operator to move: only the running server's proxy answers it.
#
# Usage: tests/litestream/run.sh. Needs Docker with Compose, openssl, and Python 3 with ansible-core and the community.docker collection (ANSIBLE_PLAYBOOK overrides which ansible-playbook runs, and GRTEST_EXTRA_COLLECTIONS adds a collections directory, e.g. where community.docker was installed). Creates only Docker objects named grtest-ls-*, and removes them on exit unless GRTEST_KEEP=1.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
here="${repo_root}/tests/litestream"
ansible_playbook="${ANSIBLE_PLAYBOOK:-ansible-playbook}"
network=grtest-ls-net
s3=grtest-ls-s3
client=grtest-ls-client
primary=grtest-ls-primary
standby=grtest-ls-standby
headscale_name=headscale.grtest.internal
# Not the mesh integration test's port, so the two can run on one machine at once.
api_port="${GRTEST_LS_API_PORT:-18480}"
s3_image=docker.io/versity/versitygw:v1.8.0
caddy_image=docker.io/library/caddy:2.11.4
tailscale_image=docker.io/tailscale/tailscale:v1.102.5
s3_key=grtest-ls-access
s3_secret="grtest-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
work="${GRTEST_WORK:-$(mktemp -d "${TMPDIR:-/tmp}/grtest-ls.XXXXXX")}"

log() { echo "==> $*"; }
fail() {
  echo "FAIL: $*" >&2
  diagnose >&2 || true
  exit 1
}

diagnose() {
  local name
  for name in "$primary" "$standby"; do
    echo "----- ${name}: containers"
    docker ps -a --filter "label=com.docker.compose.project=${name}" --format '{{.Names}} {{.Status}}' || true
    for service in headscale litestream litestream-follow; do
      docker logs --tail 30 "${name}-${service}-1" 2>&1 | sed "s/^/${service}: /" || true
    done
    docker logs --tail 30 "$name" 2>&1 | sed 's/^/headscale: /' || true
  done
  echo "----- ${client}"
  docker logs --tail 40 "$client" 2>&1 | grep -v RATELIMIT || true
}

compose() {
  local name="$1"
  shift
  docker compose --project-directory "${work}/${name}" -f "${work}/${name}/docker-compose.yml" -f "${work}/${name}-override.yml" "$@"
}

teardown() {
  if [ "${GRTEST_KEEP:-0}" = 1 ]; then
    echo "Keeping the environment (GRTEST_KEEP=1); work directory: ${work}"
    return
  fi
  local name
  for name in "$primary" "$standby"; do
    if [ -f "${work}/${name}/docker-compose.yml" ]; then
      compose "$name" --profile active down -v >/dev/null 2>&1 || true
    fi
    # Whatever Compose could not take down (a failed run can leave the project files and the containers out of step), by this project's exact label.
    docker ps -aq --filter "label=com.docker.compose.project=${name}" | xargs -r docker rm -f >/dev/null 2>&1 || true
    docker volume ls -q --filter "label=com.docker.compose.project=${name}" | xargs -r docker volume rm >/dev/null 2>&1 || true
    docker network rm "${name}_default" >/dev/null 2>&1 || true
  done
  docker rm -f "$client" "$s3" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap teardown EXIT

# Waits until a command succeeds, up to $1 attempts two seconds apart.
wait_for() {
  local attempts="$1"
  shift
  local _
  for _ in $(seq 1 "$attempts"); do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

headscale_cli() {
  local container="$1"
  shift
  docker exec "$container" headscale "$@" --output json
}

# The state promotion must carry over, without fields that change on their own (last seen, online).
snapshot() {
  local container="$1"
  python3 - "$(headscale_cli "$container" users list)" "$(headscale_cli "$container" nodes list)" "$(headscale_cli "$container" preauthkeys list)" <<'EOF'
import json, sys
users, nodes, keys = (json.loads(arg) or [] for arg in sys.argv[1:])
print(json.dumps({
    "users": sorted((u["id"], u["name"]) for u in users),
    "nodes": sorted((n["id"], n["given_name"], n["machine_key"], n["node_key"]) for n in nodes),
    "preauthkeys": sorted((k["id"], k["key"], k.get("reusable", False)) for k in keys),
}, sort_keys=True))
EOF
}

client_online_on() {
  local container="$1"
  python3 -c 'import json,sys; nodes=json.loads(sys.argv[1]) or []; sys.exit(0 if any(n.get("online") for n in nodes) else 1)' "$(headscale_cli "$container" nodes list)"
}

# Restores the replica's latest state into a throwaway container, with a project's Litestream settings, and runs a query against it.
replica_restore() {
  local name="$1" query="$2"
  docker run --rm --network "$network" \
    -v "${work}/${name}/litestream:/etc/github-runner-litestream:ro" \
    --entrypoint /bin/sh "docker.io/litestream/litestream:$(litestream_version)" \
    -c '. /etc/github-runner-litestream/litestream.env && export LITESTREAM_ACCESS_KEY_ID LITESTREAM_SECRET_ACCESS_KEY && litestream restore -config /etc/github-runner-litestream/litestream.yml -o /tmp/check.db /var/lib/headscale/db.sqlite >/dev/null 2>&1 && sqlite3 /tmp/check.db "$0"' "$query"
}

user_in_replica() {
  [ "$(replica_restore "$1" "select count(*) from users where name = '$2'")" = 1 ]
}

user_in_standby_copy() {
  [ "$(docker exec "${standby}-litestream-follow-1" sqlite3 -readonly /var/lib/headscale-standby/db.sqlite "select count(*) from users where name = '$1'")" = 1 ]
}

litestream_version() {
  sed -n 's/^github_runner_cluster_headscale_litestream_version: "\(.*\)"$/\1/p' "${repo_root}/roles/github_runner_cluster/defaults/main.yml"
}

run_playbook() {
  local playbook="$1"
  shift
  ANSIBLE_COLLECTIONS_PATH="${repo_root}/playbooks/collections${GRTEST_EXTRA_COLLECTIONS:+:${GRTEST_EXTRA_COLLECTIONS}}" ANSIBLE_HOST_KEY_CHECKING=False ANSIBLE_RETRY_FILES_ENABLED=False \
    "$ansible_playbook" -i "${work}/inventory.yml" "$playbook" "$@" </dev/null
}

log "Work directory: ${work}"
mkdir -p "${work}/certs" "${work}/${primary}" "${work}/${standby}"

log "Creating a certificate authority and a certificate for ${headscale_name}"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=grtest-ls CA" \
  -keyout "${work}/certs/ca.key" -out "${work}/certs/ca.crt" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=${headscale_name}" \
  -keyout "${work}/certs/server.key" -out "${work}/certs/server.csr" 2>/dev/null
printf 'subjectAltName=DNS:%s\n' "$headscale_name" >"${work}/certs/san.ext"
openssl x509 -req -in "${work}/certs/server.csr" -CA "${work}/certs/ca.crt" -CAkey "${work}/certs/ca.key" -CAcreateserial \
  -days 1 -extfile "${work}/certs/san.ext" -out "${work}/certs/server.crt" 2>/dev/null
chmod 0644 "${work}/certs/server.key"

log "Starting the S3-compatible store on an internal network"
docker network create --internal "$network" >/dev/null
docker run -d --name "$s3" --network "$network" -e ROOT_ACCESS_KEY="$s3_key" -e ROOT_SECRET_KEY="$s3_secret" \
  --entrypoint /bin/sh "$s3_image" -c 'mkdir -p /data/grtest-ls && exec versitygw posix /data' >/dev/null

# A project's extra Compose file: the TLS proxy carrying the shared name, and Litestream's services on the internal network. A standby's proxy starts only with the active profile, like its Headscale; once promoted, the standby gets the server's shape.
write_override() {
  local name="$1" shape="$2" litestream_services service
  if [ "$shape" = standby ]; then
    litestream_services="litestream-restore litestream litestream-follow"
  else
    litestream_services="litestream-restore litestream"
  fi
  {
    echo "services:"
    echo "  tls-proxy:"
    echo "    image: ${caddy_image}"
    if [ "$shape" = standby ]; then
      echo "    profiles: [active]"
    fi
    echo "    depends_on: [headscale]"
    echo "    environment:"
    echo "      HEADSCALE_PORT: \"${api_port}\""
    echo "    volumes:"
    echo "      - ${here}/Caddyfile:/etc/caddy/Caddyfile:ro"
    echo "      - ${work}/certs:/certs:ro"
    echo "    networks:"
    echo "      default: {}"
    echo "      mesh:"
    echo "        aliases: [${headscale_name}]"
    for service in $litestream_services; do
      echo "  ${service}:"
      echo "    networks: [mesh]"
    done
    echo "networks:"
    echo "  mesh:"
    echo "    name: ${network}"
    echo "    external: true"
  } >"${work}/${name}-override.yml"
}
write_override "$primary" server
write_override "$standby" standby

cat >"${work}/inventory.yml" <<EOF
all:
  vars:
    ansible_connection: local
    ansible_python_interpreter: "$(command -v python3)"
  children:
    github_runner_cluster:
      vars:
        github_runner_cluster_mesh: headscale_hosted
        github_runner_cluster_headscale_url: https://${headscale_name}
        github_runner_cluster_headscale_api_url: http://127.0.0.1:${api_port}
        github_runner_cluster_headscale_tls: none
        github_runner_cluster_headscale_port: ${api_port}
        github_runner_cluster_tailnet_domain: tailnet.grtest.internal
        github_runner_cluster_headscale_host: primary
        github_runner_cluster_headscale_standby_host: standby
        github_runner_cluster_headscale_derp_urls: []
        github_runner_cluster_headscale_derp_map:
          regions:
            900:
              regionid: 900
              regioncode: grtest
              regionname: Unreachable test region
              nodes:
                - {name: 900a, regionid: 900, hostname: derp.grtest.invalid, stunport: -1, derpport: 443}
        github_runner_cluster_headscale_litestream_replica:
          url: s3://grtest-ls/headscale
          endpoint: http://${s3}:7070
          region: us-east-1
          force-path-style: true
        github_runner_cluster_headscale_litestream_env:
          LITESTREAM_ACCESS_KEY_ID: ${s3_key}
          LITESTREAM_SECRET_ACCESS_KEY: ${s3_secret}
      hosts:
        node: {}
    headscale:
      hosts:
        primary:
          github_runner_cluster_headscale_dir: ${work}/${primary}
          github_runner_cluster_headscale_container_name: ${primary}
          github_runner_cluster_headscale_compose_files: [${work}/${primary}-override.yml]
        standby:
          github_runner_cluster_headscale_dir: ${work}/${standby}
          github_runner_cluster_headscale_container_name: ${standby}
          github_runner_cluster_headscale_compose_files: [${work}/${standby}-override.yml]
EOF

log "headscale_in_cluster refuses to run without Litestream"
if guard_output="$(run_playbook "${here}/headscale.yml" -e '{"github_runner_cluster_mesh": "headscale_in_cluster", "github_runner_cluster_headscale_litestream_replica": {}}' 2>&1)"; then
  fail "headscale_in_cluster ran without Litestream or an explicit acknowledgement"
fi
grep -q "github_runner_cluster_headscale_accept_node_local_state" <<<"$guard_output" || { echo "$guard_output"; fail "headscale_in_cluster failed for another reason"; }

log "Running the mesh step: server with Litestream, and a warm standby"
run_playbook "${here}/headscale.yml" || fail "the mesh step failed"

log "Checking the standby keeps Headscale stopped and follows the replica"
[ "$(docker inspect -f '{{.State.Running}}' "$primary")" = true ] || fail "Headscale is not running on the server"
if docker inspect "$standby" >/dev/null 2>&1; then
  fail "the standby's Headscale container exists before promotion"
fi
[ "$(docker inspect -f '{{.State.Running}}' "${standby}-litestream-follow-1")" = true ] || fail "the standby is not following the replica"
cmp -s "${work}/${primary}/keys/noise_private.key" "${work}/${standby}/keys/noise_private.key" || fail "the standby does not hold the server's Noise key"

log "Registering a Tailscale client through the server"
join_key="$(cat "${work}/${primary}/secrets/join-key")"
docker run -d --name "$client" --network "$network" \
  -e TS_AUTHKEY="$join_key" -e TS_EXTRA_ARGS="--login-server=https://${headscale_name}" -e TS_HOSTNAME=grtest-ls-client \
  -e TS_USERSPACE=true -e TS_STATE_DIR=/var/lib/tailscale -e TS_NO_LOGS_NO_SUPPORT=true -e SSL_CERT_FILE=/certs/ca.crt \
  -v "${work}/certs/ca.crt:/certs/ca.crt:ro" "$tailscale_image" >/dev/null
wait_for 60 client_online_on "$primary" || fail "the client never came online on the server"

log "Creating a user and a pre-auth key, then waiting for them to reach the replica and the standby's copy"
docker exec "$primary" headscale users create grtest-user >/dev/null
docker exec "$primary" headscale preauthkeys create --user "$(headscale_cli "$primary" users list | python3 -c 'import json,sys; print(next(u["id"] for u in json.load(sys.stdin) if u["name"] == "grtest-user"))')" --reusable --expiration 24h >/dev/null
wait_for 30 user_in_standby_copy grtest-user || fail "the new user never reached the standby's copy of the database"
user_in_replica "$primary" grtest-user || fail "a restore from the replica does not have the new user"
before="$(snapshot "$primary")"

log "Promoting the standby"
promote_started="$(date +%s)"
run_playbook "${repo_root}/playbooks/promote_headscale_standby.yml" || fail "promotion failed"
promoted="$(date +%s)"

log "Checking the promoted server"
[ "$(docker inspect -f '{{.State.Running}}' "$primary")" = false ] || fail "the old server is still running"
[ "$(docker inspect -f '{{.State.Running}}' "${primary}-litestream-1")" = false ] || fail "the old server's Litestream is still running"
[ "$(docker inspect -f '{{.State.Running}}' "$standby")" = true ] || fail "Headscale is not running on the promoted standby"
after="$(snapshot "$standby")"
[ "$before" = "$after" ] || fail "users, nodes or pre-auth keys differ after promotion: before ${before}, after ${after}"

log "Waiting for the existing client to reconnect to the promoted server"
wait_for 150 client_online_on "$standby" || fail "the client did not reconnect to the promoted server"
reconnected="$(date +%s)"

log "Checking the promoted server keeps replicating"
docker exec "$standby" headscale users create grtest-after-promotion >/dev/null
wait_for 15 user_in_replica "$standby" grtest-after-promotion ||
  fail "a write on the promoted server never reached the replica"

log "Rerunning the mesh step with the standby as the server, as the promotion playbook tells the operator to"
write_override "$standby" server
run_playbook "${here}/headscale.yml" -e '{"github_runner_cluster_headscale_host": "standby", "github_runner_cluster_headscale_standby_host": ""}' ||
  fail "the mesh step failed on the promoted server"
if docker inspect "${standby}-litestream-follow-1" >/dev/null 2>&1; then
  fail "the promoted server still runs the standby's follower"
fi
[ "$(snapshot "$standby" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["users"]))')" -ge 2 ] || fail "the promoted server lost users when the role reran"
wait_for 60 client_online_on "$standby" || fail "the client is not online after the role reran on the promoted server"

log "PASS: promotion took $((promoted - promote_started))s; the client was back online $((reconnected - promote_started))s after promotion started"
