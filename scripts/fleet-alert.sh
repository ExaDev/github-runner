#!/usr/bin/env bash
# Raises a notification when the runner fleet has been down for longer than a window, and a recovery note when it is back. Reads the fleet-health.json the heartbeat publishes to its gist (see scripts/heartbeat.sh) and never touches the cluster, so it can run anywhere that reaches api.github.com: a scheduled GitHub Actions workflow on a GitHub-hosted runner (examples/fleet-alert/fleet-alert.yml) or cron on a host outside the fleet. It must not run only inside the cluster it watches, because it could not then report that the cluster is down.
#
# An outage is any of these holding for at least FLEET_ALERT_AFTER_SECONDS:
# - the heartbeat has not published for that long (the heartbeat pod, the cluster or the gist write is down);
# - a control-plane server node has not been Ready for that long;
# - the heartbeat has not vouched for the fleet for that long (a scale set missing or without a listener, no ready ARC controller, no Ready node, runner pods that cannot be scheduled).
# The onset of the last two is recorded by the heartbeat itself (unhealthy_since, and since on each entry of not_ready_nodes), so this script keeps no clock of its own. An alert fires once per outage, not on every pass, and a recovery note follows when every condition has cleared.
#
# Delivery is any of these, independently:
# - an issue in FLEET_ALERT_ISSUE_REPO (owner/repo): opened when the outage begins and found again by a hidden marker in its body, so the open issue is the record that this outage has been announced; closed with a comment on recovery. Needs FLEET_ALERT_ISSUE_TOKEN with permission to read and write issues in that repository (a workflow's own GITHUB_TOKEN with `issues: write` is enough for the repository the workflow lives in). The heartbeat's gist token cannot do this, since a gist-scoped token carries no repository permission.
# - a JSON POST to FLEET_ALERT_WEBHOOK_URL, which needs FLEET_ALERT_STATE_FILE to remember that the outage was announced (the file holds only whether it is firing).
# With neither set the script evaluates and prints the outage state and delivers nothing, which is also how to try it.
#
# Environment:
# - FLEET_ALERT_GIST_ID: the heartbeat gist (default: HEARTBEAT_GIST_ID). A secret gist is readable by its id alone, as runner-fallback-action reads it, so no token is needed to read it.
# - FLEET_ALERT_GIST_TOKEN: optional token sent when reading the gist, only to lift the unauthenticated rate limit that GitHub-hosted runners share.
# - FLEET_ALERT_HEALTH_FILE: the gist file holding the health document (default fleet-health.json, as the heartbeat's HEALTH_GIST_FILE).
# - FLEET_ALERT_AFTER_SECONDS: the window (default 900, longer than the heartbeat's own 600 second window, so a missed tick or a rebooting node does not alert).
# - FLEET_ALERT_API_URL: GitHub's API root (default https://api.github.com).
# Exit status: 0 when the pass finished (whether or not an outage is firing), 1 when the health document could not be read or understood or a delivery failed. A failed delivery is not recorded, so the next pass tries again.
set -euo pipefail

gist_id="${FLEET_ALERT_GIST_ID:-${HEARTBEAT_GIST_ID:-}}"
gist_token="${FLEET_ALERT_GIST_TOKEN:-}"
health_file="${FLEET_ALERT_HEALTH_FILE:-fleet-health.json}"
after="${FLEET_ALERT_AFTER_SECONDS:-900}"
api="${FLEET_ALERT_API_URL:-https://api.github.com}"
issue_repo="${FLEET_ALERT_ISSUE_REPO:-}"
issue_token="${FLEET_ALERT_ISSUE_TOKEN:-}"
webhook_url="${FLEET_ALERT_WEBHOOK_URL:-}"
state_file="${FLEET_ALERT_STATE_FILE:-}"

# Identifies the issue this script owns. Hidden in the body so a person can retitle the issue without the next pass opening a second one.
marker='<!-- github-runner-fleet-alert -->'
issue_title='Runner fleet is down'

die() {
  echo "fleet-alert: $*" >&2
  exit 1
}

[ -n "$gist_id" ] || die "FLEET_ALERT_GIST_ID (or HEARTBEAT_GIST_ID) must be set"
[[ "$after" =~ ^[0-9]+$ ]] || die "FLEET_ALERT_AFTER_SECONDS must be a whole number of seconds, got '${after}'"
if [ -n "$issue_repo" ]; then
  [[ "$issue_repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "FLEET_ALERT_ISSUE_REPO must be owner/repo, got '${issue_repo}'"
  [ -n "$issue_token" ] || die "FLEET_ALERT_ISSUE_TOKEN must be set to deliver to FLEET_ALERT_ISSUE_REPO"
fi
if [ -n "$webhook_url" ]; then
  [[ "$webhook_url" =~ ^https?://[^[:space:]\"\\]+$ ]] || die "FLEET_ALERT_WEBHOOK_URL must be an http(s) URL"
  [ -n "$state_file" ] || die "FLEET_ALERT_STATE_FILE must be set to deliver to FLEET_ALERT_WEBHOOK_URL, so an outage is announced once"
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# One HTTP request. The URL and token go to curl on stdin as a config, never on its command line, so neither shows in a process listing (a webhook URL is itself a secret). Prints the response body; a non-2xx status fails. Usage: request METHOD URL TOKEN [BODY_FILE].
request() {
  local method="$1" url="$2" token="$3" body="${4:-}"
  {
    printf 'url = "%s"\n' "$url"
    printf 'request = "%s"\n' "$method"
    printf 'fail\nsilent\nshow-error\n'
    printf 'header = "Accept: application/vnd.github+json"\n'
    if [ -n "$token" ]; then
      printf 'header = "Authorization: Bearer %s"\n' "$token"
    fi
    if [ -n "$body" ]; then
      printf 'header = "Content-Type: application/json"\n'
      printf 'data-binary = "@%s"\n' "$body"
    fi
  } | curl -K -
}

gist="$(request GET "${api}/gists/${gist_id}" "$gist_token")" || die "could not read gist ${gist_id}"
health="$(jq -c --arg file "$health_file" '.files[$file].content // empty | fromjson' <<< "$gist" 2>/dev/null)" || true
[ -n "$health" ] || die "gist ${gist_id} has no readable ${health_file}"
# A heartbeat that predates the onset fields cannot be judged against a window, so say so rather than never alerting.
if ! jq -e 'type == "object" and (.at | type == "string") and (.healthy | type == "boolean") and (.not_ready_nodes | type == "array") and (.healthy or (.unhealthy_since | type == "string"))' <<< "$health" >/dev/null 2>&1; then
  die "${health_file} lacks at, healthy, not_ready_nodes or unhealthy_since: update the heartbeat image"
fi

now="$(date +%s)"
conditions="$(jq -c --argjson now "$now" --argjson after "$after" '
  def age: $now - (. | fromdateiso8601);
  if (.at | age) >= $after then
    [{kind: "heartbeat_stale", detail: "the heartbeat has not published since \(.at)", since: .at}]
  else
    [ (.not_ready_nodes[] | select(.control_plane and (.since | age) >= $after)
        | {kind: "server_not_ready", detail: "server node \(.node) is not Ready", since: .since}),
      (select((.healthy | not) and (.unhealthy_since | age) >= $after)
        | {kind: "fleet_not_vouched", detail: "the heartbeat is not vouching for the fleet: \(.reasons | join("; "))", since: .unhealthy_since}) ]
  end' <<< "$health")"
firing=false
if [ "$(jq 'length' <<< "$conditions")" -gt 0 ]; then
  firing=true
fi
checked_at="$(jq -r '.at' <<< "$health")"
if [ "$firing" = true ]; then
  jq -r '.[] | "firing: \(.detail) (since \(.since))"' <<< "$conditions"
else
  echo "no outage lasting at least ${after}s"
fi

failed=false

deliver_issue() {
  local page=1 found="" listing
  while true; do
    listing="$(request GET "${api}/repos/${issue_repo}/issues?state=open&per_page=100&page=${page}" "$issue_token")" || return 1
    found="$(jq -r --arg marker "$marker" '[.[] | select((.pull_request | not) and ((.body // "") | contains($marker)))][0].number // empty' <<< "$listing")"
    if [ -n "$found" ] || [ "$(jq 'length' <<< "$listing")" -lt 100 ]; then
      break
    fi
    page=$((page + 1))
  done

  if [ "$firing" = true ] && [ -z "$found" ]; then
    jq -n --arg title "$issue_title" --arg marker "$marker" --argjson conditions "$conditions" --arg checked "$checked_at" --argjson after "$after" \
      '{title: $title, body: ($marker + "\n\nThe runner fleet has been down for at least \($after) seconds, as of the heartbeat of \($checked).\n\n" + ($conditions | map("- \(.detail) (since \(.since))") | join("\n")) + "\n\nThis issue closes itself, with a comment, when every condition has cleared.")}' > "$work/issue.json"
    request POST "${api}/repos/${issue_repo}/issues" "$issue_token" "$work/issue.json" >/dev/null || return 1
    echo "opened an issue in ${issue_repo}"
  elif [ "$firing" = false ] && [ -n "$found" ]; then
    jq -n --arg checked "$checked_at" '{body: "The fleet has recovered: no condition was holding at the heartbeat of \($checked)."}' > "$work/comment.json"
    request POST "${api}/repos/${issue_repo}/issues/${found}/comments" "$issue_token" "$work/comment.json" >/dev/null || return 1
    echo '{"state": "closed", "state_reason": "completed"}' > "$work/close.json"
    request PATCH "${api}/repos/${issue_repo}/issues/${found}" "$issue_token" "$work/close.json" >/dev/null || return 1
    echo "closed issue ${found} in ${issue_repo}"
  fi
}

deliver_webhook() {
  local announced=false
  if [ -s "$state_file" ]; then
    announced="$(jq -r '.firing == true' "$state_file")" || return 1
  fi
  if [ "$firing" = "$announced" ]; then
    return 0
  fi
  local event=resolved
  local text="The runner fleet has recovered."
  if [ "$firing" = true ]; then
    event=firing
    text="The runner fleet is down: $(jq -r 'map(.detail) | join("; ")' <<< "$conditions")"
  fi
  jq -n --arg event "$event" --arg text "$text" --argjson conditions "$conditions" --arg at "$checked_at" \
    '{event: $event, text: $text, conditions: $conditions, heartbeat_at: $at}' > "$work/webhook.json"
  request POST "$webhook_url" "" "$work/webhook.json" >/dev/null || return 1
  jq -n --argjson firing "$firing" '{firing: $firing}' > "${state_file}.tmp"
  mv "${state_file}.tmp" "$state_file"
  echo "sent the ${event} webhook"
}

if [ -n "$issue_repo" ] && ! deliver_issue; then
  echo "fleet-alert: delivery to the issue repository failed" >&2
  failed=true
fi
if [ -n "$webhook_url" ] && ! deliver_webhook; then
  echo "fleet-alert: delivery to the webhook failed" >&2
  failed=true
fi
if [ -z "$issue_repo" ] && [ -z "$webhook_url" ]; then
  echo "no delivery configured: nothing sent"
fi

[ "$failed" = false ]
