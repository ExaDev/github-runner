# shellcheck shell=bash
# Sourced, not run: mints a GitHub App installation token restricted to the permissions a caller names. Shared by the pull Secret renewal (scripts/renew-pull-secret.sh, which its image carries beside this file) and the runner reaper (files/reap-runners.sh, which its ConfigMap carries beside this file).
#
# The caller defines fail MESSAGE, which must not return, and sets umask 077 before calling. The JWT never appears in a command's arguments: curl reads it from a config file under the caller's private working directory.

# GitHub rejects an App JWT whose exp is more than ten minutes after iat; iat is backdated a minute to absorb clock drift.
GITHUB_APP_JWT_BACKDATE_SECONDS=60
GITHUB_APP_JWT_LIFETIME_SECONDS=540

github_app_b64url() {
  openssl base64 -A | tr '+/' '-_' | tr -d '='
}

# Writes a curl config line for a header or user value. curl config values are double-quoted strings in which a backslash escapes the next character.
curl_config() {
  local option="$1" value="$2"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s = "%s"\n' "$option" "$value"
}

# github_app_mint_token APP_DIR API_URL PERMISSIONS WORK
#
# APP_DIR holds the App Secret's keys as files (github_app_id, github_app_installation_id, github_app_private_key). PERMISSIONS is the JSON object of permissions to restrict the token to, such as {"packages":"read"}. On success it writes the token, on one line, to WORK/token and its expiry to WORK/expires_at. Every refusal calls fail, naming the permissions the App needs when GitHub refuses them.
github_app_mint_token() {
  local app_dir="$1" api_url="$2" permissions="$3" work="$4"
  local field app_id installation_id now header claims signature status wanted
  for field in github_app_id github_app_installation_id github_app_private_key; do
    [ -s "$app_dir/$field" ] || fail "the App Secret has no $field"
  done
  app_id="$(tr -d '[:space:]' < "$app_dir/github_app_id")"
  installation_id="$(tr -d '[:space:]' < "$app_dir/github_app_installation_id")"
  [[ "$app_id" =~ ^[0-9]+$ ]] || fail "the App Secret's github_app_id is not a number"
  [[ "$installation_id" =~ ^[0-9]+$ ]] || fail "the App Secret's github_app_installation_id is not a number"
  wanted="$(jq -r 'to_entries | map("\(.key): \(.value)") | join(", ")' <<< "$permissions")" || fail "the permissions to request are not a JSON object"

  now="$(date +%s)"
  header="$(printf '{"alg":"RS256","typ":"JWT"}' | github_app_b64url)"
  claims="$(printf '{"iat":%d,"exp":%d,"iss":"%s"}' "$((now - GITHUB_APP_JWT_BACKDATE_SECONDS))" "$((now + GITHUB_APP_JWT_LIFETIME_SECONDS))" "$app_id" | github_app_b64url)"
  signature="$(printf '%s.%s' "$header" "$claims" | openssl dgst -sha256 -sign "$app_dir/github_app_private_key" -binary | github_app_b64url)" \
    || fail "could not sign the App JWT with the App Secret's private key"
  curl_config header "Authorization: Bearer ${header}.${claims}.${signature}" > "$work/github-app.curl"

  status="$(curl -sS -o "$work/mint.json" -w '%{http_code}' -K "$work/github-app.curl" -X POST \
    -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' \
    --data "$(jq -c '{permissions: .}' <<< "$permissions")" \
    "${api_url}/app/installations/${installation_id}/access_tokens")" || fail "could not reach the GitHub API"
  rm -f "$work/github-app.curl"
  if [ "$status" != "201" ]; then
    fail "GitHub refused to mint a ${wanted} installation token (HTTP ${status}: $(jq -r '.message // empty' "$work/mint.json" 2>/dev/null)). The App needs the ${wanted} permission, approved on this installation"
  fi
  jq -r '.token // empty' "$work/mint.json" > "$work/token"
  jq -r '.expires_at // empty' "$work/mint.json" > "$work/expires_at"
  rm -f "$work/mint.json"
  if [ ! -s "$work/token" ] || [ "$(wc -l < "$work/token")" -gt 1 ]; then
    fail "GitHub's response held no token"
  fi
  [ -s "$work/expires_at" ] || fail "GitHub's response held no expiry"
}
