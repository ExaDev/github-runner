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

# GitHub rejects an App JWT whose exp is more than ten minutes after iat; iat is backdated a minute to absorb clock drift.
JWT_BACKDATE_SECONDS=60
JWT_LIFETIME_SECONDS=540
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

b64url() {
  openssl base64 -A | tr '+/' '-_' | tr -d '='
}

# Writes a curl config line for a header or user value. curl config values are double-quoted strings in which a backslash escapes the next character.
curl_config() {
  local option="$1" value="$2"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s = "%s"\n' "$option" "$value"
}

for field in github_app_id github_app_installation_id github_app_private_key; do
  [ -s "$GITHUB_APP_DIR/$field" ] || fail "the App Secret has no $field"
done
app_id="$(tr -d '[:space:]' < "$GITHUB_APP_DIR/github_app_id")"
installation_id="$(tr -d '[:space:]' < "$GITHUB_APP_DIR/github_app_installation_id")"
[[ "$app_id" =~ ^[0-9]+$ ]] || fail "the App Secret's github_app_id is not a number"
[[ "$installation_id" =~ ^[0-9]+$ ]] || fail "the App Secret's github_app_installation_id is not a number"

now="$(date +%s)"
header="$(printf '{"alg":"RS256","typ":"JWT"}' | b64url)"
claims="$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - JWT_BACKDATE_SECONDS))" "$((now + JWT_LIFETIME_SECONDS))" "$app_id" | b64url)"
signature="$(printf '%s.%s' "$header" "$claims" | openssl dgst -sha256 -sign "$GITHUB_APP_DIR/github_app_private_key" -binary | b64url)" \
  || fail "could not sign the App JWT with the App Secret's private key"
curl_config header "Authorization: Bearer ${header}.${claims}.${signature}" > "$work/github.curl"

status="$(curl -sS -o "$work/mint.json" -w '%{http_code}' -K "$work/github.curl" -X POST \
  -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' \
  --data '{"permissions":{"packages":"read"}}' \
  "${GITHUB_API_URL}/app/installations/${installation_id}/access_tokens")" || fail "could not reach the GitHub API"
if [ "$status" != "201" ]; then
  fail "GitHub refused to mint a packages: read installation token (HTTP ${status}: $(jq -r '.message // empty' "$work/mint.json" 2>/dev/null)). The App needs the packages: read permission, approved on this installation"
fi
jq -r '.token // empty' "$work/mint.json" > "$work/token"
expires_at="$(jq -r '.expires_at // empty' "$work/mint.json")"
rm -f "$work/mint.json"
if [ ! -s "$work/token" ] || [ "$(wc -l < "$work/token")" -gt 1 ]; then
  fail "GitHub's response held no token"
fi
[ -n "$expires_at" ] || fail "GitHub's response held no expiry"
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
