#!/usr/bin/env bash
# Idempotent setup: brings up the k3s cluster (if not already running), waits for it to be ready, installs/upgrades the ARC controller and the in-cluster fleet-health platform (heartbeat + autoscaler - see install_platform below), resolves each org's GitHub App installation ID, creates/updates the GitHub App and GHCR pull secrets, and installs or upgrades each org's runner scale set release. Safe to rerun any time - e.g. after rotating a key, or to add an org by adding a new install_org call below with its own values/<org>-runners-values.yaml.
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

# ---- 3. Fleet-health platform (heartbeat + autoscaler) -------------------- Installs both as genuinely unpinned, in-cluster Deployments - the same pattern ARC's own controller pods already use - rather than host-pinned Docker Compose services. Mirrors ansible/roles/github_runner_arc/tasks/install_platform.yml exactly; keep both in sync if either changes. RBAC is scoped to exactly what each container reads/writes - see scripts/heartbeat.sh/scripts/autoscaler.sh.
install_platform() {
  local namespace="github-runner-platform"

  log "Installing/upgrading the fleet-health platform (heartbeat + autoscaler)..."

  kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f -

  kubectl create serviceaccount heartbeat --namespace="$namespace" --dry-run=client -o yaml | kubectl apply -f -
  kubectl create serviceaccount autoscaler --namespace="$namespace" --dry-run=client -o yaml | kubectl apply -f -

  kubectl create secret generic heartbeat-gh-token \
    --namespace="$namespace" \
    --from-literal=token="$HEARTBEAT_GH_TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f -

  kubectl create secret docker-registry ghcr-pull \
    --namespace="$namespace" \
    --docker-server=ghcr.io \
    --docker-username="$GHCR_PULL_USERNAME" \
    --docker-password="$GHCR_PULL_TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f -

  # Pre-created empty, not left for the autoscaler to create on first poll - neither ServiceAccount below is granted `create` on configmaps, only get/patch.
  kubectl create configmap autoscaler-status \
    --namespace="$namespace" \
    --from-literal=status.json="" \
    --from-literal=raise-confirm-count="0" \
    --dry-run=client -o yaml | kubectl apply -f -

  kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: github-runner-heartbeat-nodes-reader
rules:
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: github-runner-heartbeat-nodes-reader
subjects:
  - kind: ServiceAccount
    name: heartbeat
    namespace: ${namespace}
roleRef:
  kind: ClusterRole
  name: github-runner-heartbeat-nodes-reader
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: github-runner-heartbeat-controller-reader
  namespace: actions-runner-controller
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: github-runner-heartbeat-controller-reader
  namespace: actions-runner-controller
subjects:
  - kind: ServiceAccount
    name: heartbeat
    namespace: ${namespace}
roleRef:
  kind: Role
  name: github-runner-heartbeat-controller-reader
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: github-runner-heartbeat-status-reader
  namespace: ${namespace}
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    resourceNames: ["autoscaler-status"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: github-runner-heartbeat-status-reader
  namespace: ${namespace}
subjects:
  - kind: ServiceAccount
    name: heartbeat
    namespace: ${namespace}
roleRef:
  kind: Role
  name: github-runner-heartbeat-status-reader
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: github-runner-autoscaler-node-metrics-reader
rules:
  - apiGroups: ["metrics.k8s.io"]
    resources: ["nodes"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: github-runner-autoscaler-node-metrics-reader
subjects:
  - kind: ServiceAccount
    name: autoscaler
    namespace: ${namespace}
roleRef:
  kind: ClusterRole
  name: github-runner-autoscaler-node-metrics-reader
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: github-runner-autoscaler-runnerset-manager
  namespace: arc-runners-exadev
rules:
  - apiGroups: ["actions.github.com"]
    resources: ["autoscalingrunnersets"]
    resourceNames: ["exadev-runners"]
    verbs: ["get", "patch"]
  - apiGroups: ["metrics.k8s.io"]
    resources: ["pods"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: github-runner-autoscaler-runnerset-manager
  namespace: arc-runners-exadev
subjects:
  - kind: ServiceAccount
    name: autoscaler
    namespace: ${namespace}
roleRef:
  kind: Role
  name: github-runner-autoscaler-runnerset-manager
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: github-runner-autoscaler-status-writer
  namespace: ${namespace}
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    resourceNames: ["autoscaler-status"]
    verbs: ["get", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: github-runner-autoscaler-status-writer
  namespace: ${namespace}
subjects:
  - kind: ServiceAccount
    name: autoscaler
    namespace: ${namespace}
roleRef:
  kind: Role
  name: github-runner-autoscaler-status-writer
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: heartbeat
  namespace: ${namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: heartbeat
  template:
    metadata:
      labels:
        app: heartbeat
    spec:
      serviceAccountName: heartbeat
      imagePullSecrets:
        - name: ghcr-pull
      containers:
        - name: heartbeat
          image: ghcr.io/exadev/github-runner-heartbeat:latest
          env:
            - name: HEARTBEAT_GIST_ID
              value: "${HEARTBEAT_GIST_ID}"
            - name: HEARTBEAT_WINDOW_SECONDS
              value: "${HEARTBEAT_WINDOW_SECONDS:-600}"
            - name: HEARTBEAT_INTERVAL_SECONDS
              value: "${HEARTBEAT_INTERVAL_SECONDS:-180}"
            - name: HEARTBEAT_STATE_NAMESPACE
              value: "${namespace}"
            - name: HEARTBEAT_GH_TOKEN
              valueFrom:
                secretKeyRef:
                  name: heartbeat-gh-token
                  key: token
          resources:
            requests: {cpu: "50m", memory: "32Mi"}
            limits: {cpu: "200m", memory: "64Mi"}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: autoscaler
  namespace: ${namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: autoscaler
  template:
    metadata:
      labels:
        app: autoscaler
    spec:
      serviceAccountName: autoscaler
      imagePullSecrets:
        - name: ghcr-pull
      containers:
        - name: autoscaler
          image: ghcr.io/exadev/github-runner-autoscaler:latest
          env:
            - name: AUTOSCALER_DRY_RUN
              value: "${AUTOSCALER_DRY_RUN:-true}"
            - name: AUTOSCALER_POLL_SECONDS
              value: "${AUTOSCALER_POLL_SECONDS:-45}"
            - name: AUTOSCALER_USABLE_BUDGET_GI
              value: "${AUTOSCALER_USABLE_BUDGET_GI:-24}"
            - name: AUTOSCALER_MAX_CEILING
              value: "${AUTOSCALER_MAX_CEILING:-7}"
            - name: AUTOSCALER_FLOOR
              value: "${AUTOSCALER_FLOOR:-3}"
            - name: AUTOSCALER_RAISE_CONFIRM_POLLS
              value: "${AUTOSCALER_RAISE_CONFIRM_POLLS:-2}"
            - name: AUTOSCALER_MEM_AVAILABLE_PRESSURE_PCT
              value: "${AUTOSCALER_MEM_AVAILABLE_PRESSURE_PCT:-15}"
            - name: AUTOSCALER_STATE_NAMESPACE
              value: "${namespace}"
          resources:
            requests: {cpu: "50m", memory: "32Mi"}
            limits: {cpu: "200m", memory: "64Mi"}
EOF
}

# ---- 4. Per-org helpers -----------------------------------------------------

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

  # Every Helm upgrade above reverts maxRunners to the values file's safe floor (see values/exadev-runners-values.yaml). Trigger one immediate autoscaler poll so the safe-floor window after this install shrinks from up to a full poll interval down to seconds, rather than waiting for the loop's own next tick. Best-effort: the autoscaler service is exadev-specific today (see scripts/autoscaler.sh), and the ordinary loop catches this regardless if the exec fails for any reason.
  # kubectl exec, not docker compose exec: the autoscaler runs as an in-cluster Deployment now (see install_platform above), genuinely unpinned to any one host, so this goes through the API server rather than a local Docker socket.
  kubectl exec deploy/autoscaler -n github-runner-platform -- /app/scripts/autoscaler.sh || true
}

# ---- 5. Install the fleet-health platform, then each org ------------------
install_platform

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
