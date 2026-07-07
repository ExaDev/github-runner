#!/usr/bin/env bash
# Idempotent setup: brings up the k3s cluster (if not already running),
# waits for it to be ready, resolves each org's GitHub App installation ID,
# creates/updates the GitHub App and GHCR pull secrets, and installs or
# upgrades the ARC controller plus each org's runner scale set release.
# Safe to rerun any time - e.g. after rotating a key, or to add an org by
# adding a new install_org call below with its own values/<org>-runners-values.yaml.
set -euo pipefail
cd "$(dirname "$0")"

export KUBECONFIG
KUBECONFIG="$(pwd)/kubeconfig/kubeconfig.yaml"

log() { echo "==> $*"; }

lowercase() { echo "$1" | tr '[:upper:]' '[:lower:]'; }

# ---- 0. Load config. The file is named .env so docker compose reads it
# automatically (K3S_TOKEN etc.); we also source it here so the helm/kubectl
# commands below see the same vars directly in this shell.
# set -a exports everything sourced below so child processes (docker
# compose, helm, kubectl) can see it too, not just this script's own shell.
set -a
# shellcheck source=/dev/null
source .env
set +a

# ---- 1. Bring up k3s -------------------------------------------------------
log "Starting k3s..."
docker compose up -d

log "Waiting for k3s to be ready..."
# kubectl get nodes succeeding (exit 0) only means the API server is
# reachable - it returns an empty, still-successful list before the node
# object itself has registered. Wait for at least one node to actually
# appear before moving on to kubectl wait, which fails outright ("no
# matching resources found") if run against zero nodes.
until [ "$(kubectl get nodes --no-headers 2>/dev/null | wc -l)" -gt 0 ]; do
  sleep 2
done
kubectl wait --for=condition=Ready node --all --timeout=120s
kubectl get nodes

# ---- 2. Install the ARC controller (once, shared by every org) -----------
log "Installing/upgrading the ARC controller..."
helm upgrade --install arc \
  --namespace actions-runner-controller --create-namespace \
  -f values/controller-values.yaml \
  oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller

# ---- 3. Per-org helpers -----------------------------------------------------

# Resolves a GitHub App's installation ID for a given org login, using only
# the App ID and private key - signs a short-lived JWT per GitHub's
# documented App authentication flow and queries /app/installations.
resolve_installation_id() {
  local app_id="$1" private_key_path="$2" login="$3"
  local now iat exp header payload signing_input signature jwt response

  now=$(date +%s)
  iat=$((now - 60))
  exp=$((now + 600))
  header='{"alg":"RS256","typ":"JWT"}'
  payload=$(jq -c -n --arg iat "$iat" --arg exp "$exp" --arg iss "$app_id" \
    '{iat: ($iat | tonumber), exp: ($exp | tonumber), iss: ($iss | tonumber)}')

  signing_input="$(printf '%s' "$header" | base64 | tr -d '=\n' | tr '+/' '-_').$(printf '%s' "$payload" | base64 | tr -d '=\n' | tr '+/' '-_')"
  signature=$(printf '%s' "$signing_input" | openssl dgst -binary -sha256 -sign "$private_key_path" | base64 | tr -d '=\n' | tr '+/' '-_')
  jwt="${signing_input}.${signature}"

  response=$(curl -sX GET -H "Authorization: Bearer ${jwt}" -H "Accept: application/vnd.github+json" https://api.github.com/app/installations)
  echo "$response" | jq -r --arg login "$login" '.[] | select(.account.login == $login) | .id'
}

# Creates/updates the namespace, GitHub App secret, and GHCR pull secret for
# one org, then installs/upgrades that org's runner scale set release.
install_org() {
  local org="$1" app_id="$2" installation_id="$3" private_key_path="$4" image="$5"
  local org_lower ns secret_name
  org_lower="$(lowercase "$org")"
  ns="arc-runners-${org_lower}"
  secret_name="${org_lower}-github-app"

  log "Creating namespace ${ns}..."
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -

  log "Creating/updating GitHub App secret for ${org}..."
  kubectl create secret generic "$secret_name" \
    --namespace="$ns" \
    --from-literal=github_app_id="$app_id" \
    --from-literal=github_app_installation_id="$installation_id" \
    --from-file=github_app_private_key="$private_key_path" \
    --dry-run=client -o yaml | kubectl apply -f -

  log "Creating/updating GHCR pull secret for ${org}..."
  kubectl create secret docker-registry ghcr-pull \
    --namespace="$ns" \
    --docker-server=ghcr.io \
    --docker-username="$GHCR_PULL_USERNAME" \
    --docker-password="$GHCR_PULL_TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f -

  log "Installing/upgrading the ${org} runner scale set..."
  helm upgrade --install "${org_lower}-runners" \
    --namespace "$ns" \
    --set githubConfigUrl="https://github.com/${org}" \
    --set githubConfigSecret="$secret_name" \
    --set template.spec.imagePullSecrets[0].name=ghcr-pull \
    --set template.spec.containers[0].image="$image" \
    -f "values/${org_lower}-runners-values.yaml" \
    oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set
}

# ---- 4. Install each org ---------------------------------------------------
if [ -z "${EXADEV_APP_INSTALLATION_ID:-}" ]; then
  log "Resolving ExaDev App installation ID..."
  EXADEV_APP_INSTALLATION_ID=$(resolve_installation_id "$EXADEV_APP_ID" "$EXADEV_APP_PRIVATE_KEY_PATH" "ExaDev")
  log "Resolved installation ID: ${EXADEV_APP_INSTALLATION_ID}"
fi

install_org "ExaDev" "$EXADEV_APP_ID" "$EXADEV_APP_INSTALLATION_ID" "$EXADEV_APP_PRIVATE_KEY_PATH" "ghcr.io/exadev/github-runner:latest"

# To add another org, source its own env file (mirroring the pattern above)
# and add another install_org call here with its own values/<org>-runners-values.yaml -
# the controller install in step 2 is shared, nothing else above needs to change.

log "Done. Verify with: kubectl get pods -A"
