#!/usr/bin/env bash
# Brings up this machine as a fleet host without an Ansible control node, inventory entry, or 1Password: reads .env.bootstrap (copy .env.bootstrap.example), installs Ansible into a repo-local venv, and runs the same ansible/ role every inventory-managed host runs, against localhost over a local connection. There is deliberately no second implementation of any deployment step here - everything past translating .env.bootstrap into role variables is the role's job. Safe to rerun; extra arguments pass straight through to ansible-playbook (e.g. ./bootstrap.sh --check).
set -euo pipefail
cd "$(dirname "$0")"
repo_root="$(pwd)"
venv="${repo_root}/.ansible-venv"

log() { echo "==> $*"; }

# set -a exports everything sourced so jq's own `env` below can see it.
set -a
# shellcheck source=/dev/null
source .env.bootstrap
set +a

if [ ! -x "${venv}/bin/ansible-playbook" ]; then
  log "Installing Ansible into ${venv}..."
  python3 -m venv "$venv"
  # --prefer-binary: cryptography (an ansible-core dependency) stopped publishing Intel macOS wheels after 48.0.1, so without it pip picks the newest version on an Intel Mac and compiles it from source, which needs a working Rust and C toolchain.
  "${venv}/bin/pip" install --quiet --prefer-binary ansible-core
fi
"${venv}/bin/ansible-galaxy" collection install -r ansible/requirements.yml -p "${venv}/collections" >/dev/null

if [ -n "${K3S_AGENT_SERVER_URL:-}" ]; then
  node_role=agent
  node_name="${K3S_AGENT_NODE_NAME:-}"
else
  node_role=server
  node_name="${K3S_NODE_NAME:-}"
fi
if [ -z "$node_name" ]; then
  node_name="$(hostname -s | tr '[:upper:]' '[:lower:]')"
fi

org_private_key=""
if [ -n "${EXADEV_APP_ID:-}" ]; then
  org_private_key="$(cat "$EXADEV_APP_PRIVATE_KEY_PATH")"
fi

# Contains every secret below, so it lives only for this run: created 0600 and removed on exit, however the script exits.
vars_file="$(mktemp)"
trap 'rm -f "$vars_file"' EXIT
chmod 600 "$vars_file"

jq -n \
  --arg repo_root "$repo_root" \
  --arg node_role "$node_role" \
  --arg node_name "$node_name" \
  --arg org_private_key "$org_private_key" \
  '
  def opt($name; $var): if (env[$var] // "") != "" then {($name): env[$var]} else {} end;
  {
    github_runner_arc_repo_root: $repo_root,
    github_runner_arc_node_role: $node_role,
    github_runner_arc_node_name: $node_name,
    github_runner_arc_etcd_bootstrap: ($node_role == "server" and (env.K3S_JOIN_SERVER_URL // "") == ""),
    github_runner_arc_join_server_url: (env.K3S_JOIN_SERVER_URL // ""),
    github_runner_arc_agent_server_url: (env.K3S_AGENT_SERVER_URL // ""),
    github_runner_arc_tls_sans: ((env.K3S_TLS_SAN_LIST // "") | split(" ") | map(select(. != ""))),
    github_runner_arc_k3s_version: (env.K3S_VERSION // "" | if . == "" then "latest" else . end),
    github_runner_arc_heartbeat_gist_id: (env.HEARTBEAT_GIST_ID // ""),
    github_runner_arc_orgs: (
      if (env.EXADEV_APP_ID // "") == "" then []
      else [{
        name: "ExaDev",
        app_id: env.EXADEV_APP_ID,
        installation_id: (env.EXADEV_APP_INSTALLATION_ID // ""),
        image: "ghcr.io/exadev/github-runner:latest",
        scale_set_profiles: [{suffix: "", values_file: "exadev-runners-values.yaml"}]
      }]
      end
    ),
    github_runner_arc_provided_secrets: {
      k3s_token: (env.K3S_TOKEN // ""),
      tailscale_join_key: (env.K3S_VPN_AUTH_JOIN_KEY // ""),
      tailscale_api_token: (env.TAILSCALE_API_TOKEN // ""),
      ghcr_username: (env.GHCR_PULL_USERNAME // ""),
      ghcr_token: (env.GHCR_PULL_TOKEN // ""),
      heartbeat_gh_token: (env.HEARTBEAT_GH_TOKEN // ""),
      org_private_keys: (if $org_private_key == "" then {} else {ExaDev: $org_private_key} end)
    }
  }
  + opt("github_runner_arc_autoscaler_dry_run"; "AUTOSCALER_DRY_RUN")
  + opt("github_runner_arc_autoscaler_poll_seconds"; "AUTOSCALER_POLL_SECONDS")
  + opt("github_runner_arc_autoscaler_usable_budget_gi"; "AUTOSCALER_USABLE_BUDGET_GI")
  + opt("github_runner_arc_autoscaler_max_ceiling"; "AUTOSCALER_MAX_CEILING")
  + opt("github_runner_arc_autoscaler_floor"; "AUTOSCALER_FLOOR")
  + opt("github_runner_arc_autoscaler_raise_confirm_polls"; "AUTOSCALER_RAISE_CONFIRM_POLLS")
  + opt("github_runner_arc_autoscaler_mem_available_pressure_pct"; "AUTOSCALER_MEM_AVAILABLE_PRESSURE_PCT")
  ' >"$vars_file"

# From ansible/ so its ansible.cfg applies. No ansible_python_interpreter here: as an extra var it would override the role's own switch to its kubernetes-client virtualenv.
log "Running the github_runner_arc role against this machine as ${node_name} (${node_role})..."
cd ansible
ANSIBLE_COLLECTIONS_PATH="${venv}/collections" \
  "${venv}/bin/ansible-playbook" -i localhost, --connection=local playbook.yml -e @"$vars_file" "$@"
