#!/usr/bin/env bash
# Runs as the runner reaper CronJob the github_runner_arc role installs in each runner namespace when github_runner_arc_runner_reaper_enabled is on (see roles/github_runner_arc/tasks/install_runner_reaper.yml). One pass: it mints a GitHub App installation token restricted to actions: read, lists the namespace's EphemeralRunners, and for each runner that ARC has recorded as running a job reads that job's workflow run from GitHub. It deletes the runner when the run is completed (which includes cancelled), when the runner's own job is completed, or when that job has been running for longer than MAX_JOB_SECONDS; ARC's scale set replaces a deleted runner. A completed run or job is only acted on once it has been completed for COMPLETED_GRACE_SECONDS, which keeps the reaper out of the way of a runner that is exiting normally.
#
# It never deletes a runner ARC has not assigned a job (no workflow run recorded on it), a runner already being deleted, or a runner whose run or job it could not read. A runner is deleted only on a positive answer from GitHub. If the token cannot be minted or the runners cannot be listed it deletes nothing and exits non-zero; if any one runner's run cannot be read it leaves that runner alone, carries on with the rest, and exits non-zero at the end.
#
# Environment (provided by the CronJob):
# - GITHUB_APP_DIR: directory holding the App Secret's three keys as files, mounted from the App Secret
# - RUNNER_NAMESPACE: the namespace whose EphemeralRunners it checks
# - MAX_JOB_SECONDS: a job running for longer than this has its runner deleted
# - COMPLETED_GRACE_SECONDS: how long a run or job must have been completed before its runner is deleted
# - DRY_RUN: true logs what it would delete and deletes nothing
# - GITHUB_API_URL: the GitHub API, default https://api.github.com
# The ConfigMap carries the shared token minting (github-app-token.sh) beside this script. kubectl needs no KUBECONFIG: it uses the pod's ServiceAccount token. The token never appears in a command's arguments: curl reads it from a config file under a private temporary directory that is removed on exit.
set -euo pipefail

GITHUB_APP_DIR="${GITHUB_APP_DIR:-/var/run/github-app}"
RUNNER_NAMESPACE="${RUNNER_NAMESPACE:?RUNNER_NAMESPACE must be set}"
MAX_JOB_SECONDS="${MAX_JOB_SECONDS:?MAX_JOB_SECONDS must be set}"
COMPLETED_GRACE_SECONDS="${COMPLETED_GRACE_SECONDS:?COMPLETED_GRACE_SECONDS must be set}"
DRY_RUN="${DRY_RUN:-false}"
GITHUB_API_URL="${GITHUB_API_URL:-https://api.github.com}"

# The largest page GitHub's list endpoints return.
JOBS_PER_PAGE=100
RESOURCE="ephemeralrunners.actions.github.com"

log() {
  echo "reap-runners: $*" >&2
}

# Before any runner is looked at, so nothing has been deleted when it is called.
fail() {
  log "$*; deleted nothing"
  exit 1
}

[[ "$MAX_JOB_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "MAX_JOB_SECONDS must be a positive whole number of seconds"
[[ "$COMPLETED_GRACE_SECONDS" =~ ^[0-9]+$ ]] || fail "COMPLETED_GRACE_SECONDS must be a whole number of seconds"
case "$DRY_RUN" in
  true | false) ;;
  *) fail "DRY_RUN must be true or false" ;;
esac

umask 077
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# shellcheck source=roles/github_runner_arc/files/github-app-token.sh
source "$(dirname "${BASH_SOURCE[0]}")/github-app-token.sh"

github_app_mint_token "$GITHUB_APP_DIR" "$GITHUB_API_URL" '{"actions":"read"}' "$work"
curl_config header "Authorization: Bearer $(cat "$work/token")" > "$work/api.curl"
rm -f "$work/token" "$work/expires_at"

kubectl get "$RESOURCE" -n "$RUNNER_NAMESPACE" -o json > "$work/runners.json" \
  || fail "could not list the EphemeralRunners in ${RUNNER_NAMESPACE}"
jq -e '.items | type == "array"' "$work/runners.json" > /dev/null || fail "the EphemeralRunner list in ${RUNNER_NAMESPACE} held no items"

# Prints the HTTP status of a GET on the API path, writing the body to the file.
github_get() {
  curl -sS -o "$2" -w '%{http_code}' -K "$work/api.curl" \
    -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' \
    "${GITHUB_API_URL}$1"
}

# Seconds since a GitHub timestamp, ignoring any fractional seconds, which fromdateiso8601 does not parse.
JQ_AGE='def age(t): now - (t | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601) | floor;'

checked=0
deleted=0
idle=0
kept=0
errors=0

error() {
  log "$*; left alone"
  errors=$((errors + 1))
}

delete_runner() {
  local name="$1" detail="$2"
  if [ "$DRY_RUN" = true ]; then
    log "would delete ${RUNNER_NAMESPACE}/${name}: ${detail} (dry run)"
    deleted=$((deleted + 1))
    return
  fi
  if kubectl delete "$RESOURCE" "$name" -n "$RUNNER_NAMESPACE" --wait=false >&2; then
    log "deleted ${RUNNER_NAMESPACE}/${name}: ${detail}"
    deleted=$((deleted + 1))
  else
    error "${name}: could not delete it (${detail})"
  fi
}

keep_runner() {
  log "kept ${RUNNER_NAMESPACE}/${1}: ${2}"
  kept=$((kept + 1))
}

# Finds the job of the run that names the runner, across every attempt and page, writing it to WORK/job.json. Returns 1 when no job names it and 2 when GitHub could not be read.
find_job() {
  local name="$1" repository="$2" run_id="$3" page=1 status
  while :; do
    status="$(github_get "/repos/${repository}/actions/runs/${run_id}/jobs?filter=all&per_page=${JOBS_PER_PAGE}&page=${page}" "$work/jobs.json")" || return 2
    [ "$status" = 200 ] || { log "listing the jobs of run ${run_id} returned HTTP ${status}"; return 2; }
    jq -e --arg name "$name" '[.jobs[] | select(.runner_name == $name)] | first // empty' "$work/jobs.json" > "$work/job.json" 2> /dev/null && return 0
    jq -e --argjson page "$page" --argjson size "$JOBS_PER_PAGE" '(.jobs | length) > 0 and $page * $size < .total_count' "$work/jobs.json" > /dev/null 2>&1 || return 1
    page=$((page + 1))
  done
}

while IFS= read -r runner <&3; do
  checked=$((checked + 1))
  name="$(jq -r '.metadata.name' <<< "$runner")"
  if [ "$(jq -r '.metadata.deletionTimestamp // empty' <<< "$runner")" != "" ]; then
    keep_runner "$name" "already being deleted"
    continue
  fi
  run_id="$(jq -r '.status.workflowRunId // 0' <<< "$runner")"
  repository="$(jq -r '.status.jobRepositoryName // empty' <<< "$runner")"
  # ARC records the run and repository on a runner only when the listener assigns it a job, and an ephemeral runner never takes a second one, so a runner without them is idle.
  if [ "$run_id" = 0 ] || [ -z "$repository" ]; then
    idle=$((idle + 1))
    continue
  fi
  if ! [[ "$run_id" =~ ^[0-9]+$ ]] || ! [[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    error "${name}: its recorded run '${run_id}' in '${repository}' is not a run GitHub could name"
    continue
  fi
  run="run ${run_id} in ${repository}"

  status="$(github_get "/repos/${repository}/actions/runs/${run_id}" "$work/run.json")" || { error "${name}: could not reach GitHub to read ${run}"; continue; }
  if [ "$status" != 200 ]; then
    error "${name}: reading ${run} returned HTTP ${status} ($(jq -r '.message // empty' "$work/run.json" 2> /dev/null)); the App's installation may not include ${repository}"
    continue
  fi
  verdict="$(jq -r --argjson grace "$COMPLETED_GRACE_SECONDS" "$JQ_AGE"'
    if .status == "completed" then
      age(.updated_at) as $age
      | if $age >= $grace then "delete\t\(.conclusion // "no conclusion"), completed \($age)s ago"
        else "keep\t\(.conclusion // "no conclusion"), completed \($age)s ago, inside the grace of \($grace)s" end
    else "jobs\t\(.status)" end' "$work/run.json")" || { error "${name}: could not read the state of ${run}"; continue; }
  IFS=$'\t' read -r action detail <<< "$verdict"
  case "$action" in
    delete) delete_runner "$name" "${run} is ${detail}"; continue ;;
    keep) keep_runner "$name" "${run} is ${detail}"; continue ;;
  esac

  # The run is still going: judge the runner by its own job.
  outcome=0
  find_job "$name" "$repository" "$run_id" || outcome=$?
  case "$outcome" in
    1) keep_runner "$name" "${run} is ${detail} and none of its jobs names this runner"; continue ;;
    2) error "${name}: could not read the jobs of ${run}"; continue ;;
  esac
  verdict="$(jq -r --argjson grace "$COMPLETED_GRACE_SECONDS" --argjson bound "$MAX_JOB_SECONDS" "$JQ_AGE"'
    if .status == "completed" then
      age(.completed_at) as $age
      | if $age >= $grace then "delete\tjob \(.id) is \(.conclusion // "no conclusion"), completed \($age)s ago"
        else "keep\tjob \(.id) is \(.conclusion // "no conclusion"), completed \($age)s ago, inside the grace of \($grace)s" end
    elif .started_at == null then "keep\tjob \(.id) is \(.status) and has not started"
    else
      age(.started_at) as $age
      | if $age > $bound then "delete\tjob \(.id) has been \(.status) for \($age)s, over the bound of \($bound)s"
        else "keep\tjob \(.id) has been \(.status) for \($age)s, within the bound of \($bound)s" end
    end' "$work/job.json")" || { error "${name}: could not read the state of its job in ${run}"; continue; }
  IFS=$'\t' read -r action detail <<< "$verdict"
  if [ "$action" = delete ]; then
    delete_runner "$name" "${run}: ${detail}"
  else
    keep_runner "$name" "${run}: ${detail}"
  fi
done 3< <(jq -c '.items[]' "$work/runners.json")

deleted_word=deleted
[ "$DRY_RUN" = false ] || deleted_word="to delete (dry run)"
log "checked ${checked} runner(s) in ${RUNNER_NAMESPACE}: ${deleted} ${deleted_word}, ${kept} kept, ${idle} idle, ${errors} not readable"
if [ "$errors" -gt 0 ]; then
  log "exiting non-zero because ${errors} runner(s) could not be judged; they were left alone"
  exit 1
fi
