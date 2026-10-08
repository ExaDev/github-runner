#!/usr/bin/env bash
# Runs as the image pull Secret renewal CronJob the github_runner_arc role installs in each runner namespace when github_runner_arc_image_pull_secret_source is app (see roles/github_runner_arc/tasks/install_pull_secret_renewal.yml). It signs a GitHub App JWT with the App's private key, mints an installation token restricted to packages: read, proves that token can read every image it is asked to check, and only then writes the token into the pull Secret. Any refusal exits non-zero and leaves the existing Secret alone, so a working credential is never replaced by one that cannot pull.
#
# Environment (provided by the CronJob):
# - GITHUB_APP_DIR: directory holding the App Secret's three keys as files (github_app_id, github_app_installation_id, github_app_private_key), mounted from the App Secret
# - PULL_SECRET_NAME, PULL_SECRET_NAMESPACE: the kubernetes.io/dockerconfigjson Secret to write
# - PULL_REGISTRY: the registry host the Secret authenticates to, such as ghcr.io
# - VERIFY_IMAGES: a JSON list of {"repository", "reference"} on that registry to prove the token can read
# - EXPIRY_ANNOTATION: annotation on the Secret that records when the token expires
# - FIELD_MANAGER: server-side apply field manager for the write
# - GITHUB_API_URL: the GitHub API, default https://api.github.com
# kubectl needs no KUBECONFIG: it uses the pod's ServiceAccount token.
#
# The JWT, the token and the registry bearer token never appear in a command's arguments: curl reads its credentials from a config file and jq reads the token from a file, all under a private temporary directory that is removed on exit.
set -euo pipefail

GITHUB_APP_DIR="${GITHUB_APP_DIR:-/var/run/github-app}"
PULL_SECRET_NAME="${PULL_SECRET_NAME:?PULL_SECRET_NAME must be set}"
PULL_SECRET_NAMESPACE="${PULL_SECRET_NAMESPACE:?PULL_SECRET_NAMESPACE must be set}"
PULL_REGISTRY="${PULL_REGISTRY:?PULL_REGISTRY must be set}"
VERIFY_IMAGES="${VERIFY_IMAGES:-[]}"
EXPIRY_ANNOTATION="${EXPIRY_ANNOTATION:?EXPIRY_ANNOTATION must be set}"
FIELD_MANAGER="${FIELD_MANAGER:?FIELD_MANAGER must be set}"
GITHUB_API_URL="${GITHUB_API_URL:-https://api.github.com}"

# The username GitHub documents for authenticating to its registries with a token that belongs to no user.
TOKEN_USERNAME="x-access-token"
MANIFEST_ACCEPT="application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.docker.distribution.manifest.v2+json"

fail() {
  echo "renew-pull-secret: $*; leaving ${PULL_SECRET_NAMESPACE}/${PULL_SECRET_NAME} unchanged" >&2
  exit 1
}

umask 077
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The image carries the shared token minting beside this script (see pull-secret-renewer/Dockerfile).
# shellcheck source=roles/github_runner_arc/files/github-app-token.sh
source "$(dirname "${BASH_SOURCE[0]}")/github-app-token.sh"

github_app_mint_token "$GITHUB_APP_DIR" "$GITHUB_API_URL" '{"packages":"read"}' "$work"
expires_at="$(cat "$work/expires_at")"
token="$(cat "$work/token")"

# The registry's own two-step handshake against each real image: a bearer token scoped to pull it, then a manifest read. A token can authenticate to the registry and still be denied the package, which is only visible at the second step.
count="$(jq -r 'length' <<< "$VERIFY_IMAGES")" || fail "VERIFY_IMAGES is not a JSON list"
curl_config user "${TOKEN_USERNAME}:${token}" > "$work/registry.curl"
for ((index = 0; index < count; index++)); do
  repository="$(jq -r ".[$index].repository" <<< "$VERIFY_IMAGES")"
  reference="$(jq -r ".[$index].reference" <<< "$VERIFY_IMAGES")"
  image="${PULL_REGISTRY}/${repository}:${reference}"
  status="$(curl -sS -o "$work/registry-token.json" -w '%{http_code}' -K "$work/registry.curl" \
    "https://${PULL_REGISTRY}/token?scope=repository:${repository}:pull&service=${PULL_REGISTRY}")" || fail "could not reach ${PULL_REGISTRY}"
  [ "$status" = "200" ] || fail "${PULL_REGISTRY} refused the token for ${image} (HTTP ${status})"
  curl_config header "Authorization: Bearer $(jq -r '.token // empty' "$work/registry-token.json")" > "$work/manifest.curl"
  rm -f "$work/registry-token.json"
  status="$(curl -sS -o /dev/null -w '%{http_code}' -K "$work/manifest.curl" --head -H "Accept: ${MANIFEST_ACCEPT}" \
    "https://${PULL_REGISTRY}/v2/${repository}/manifests/${reference}")" || fail "could not reach ${PULL_REGISTRY}"
  [ "$status" = "200" ] || fail "the token cannot read ${image} (reading the manifest returned HTTP ${status})"
  echo "renew-pull-secret: the token can read ${image}" >&2
done

# Server-side apply with --force-conflicts takes the fields over from whichever manager wrote them last (the role writes the first token), and stores no last-applied annotation holding the token.
jq -n \
  --arg name "$PULL_SECRET_NAME" --arg namespace "$PULL_SECRET_NAMESPACE" --arg registry "$PULL_REGISTRY" \
  --arg username "$TOKEN_USERNAME" --arg annotation "$EXPIRY_ANNOTATION" --arg expires "$expires_at" \
  --rawfile token "$work/token" \
  '($token | rtrimstr("\n")) as $password
   | {apiVersion: "v1", kind: "Secret", type: "kubernetes.io/dockerconfigjson",
      metadata: {name: $name, namespace: $namespace, annotations: {($annotation): $expires}},
      data: {".dockerconfigjson": ({auths: {($registry): {username: $username, password: $password, auth: ("\($username):\($password)" | @base64)}}} | tojson | @base64)}}' \
  | kubectl apply --server-side --force-conflicts --field-manager="$FIELD_MANAGER" -f - >&2 \
  || fail "could not write the Secret"
unset token
echo "renew-pull-secret: wrote ${PULL_SECRET_NAMESPACE}/${PULL_SECRET_NAME}; the token expires at ${expires_at}" >&2
