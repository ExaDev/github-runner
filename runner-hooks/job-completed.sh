#!/usr/bin/env bash
# Runner job-completed hook (ACTIONS_RUNNER_HOOK_JOB_COMPLETED): reads the runner container's peak memory for the job that just ended and records it as a Kubernetes Event in the pod's own namespace, where the autoscaler collects it (see record_measured_peaks in scripts/autoscaler.sh).
#
# It must never fail the job it ends, because the runner fails a job whose hook exits non-zero and a memory figure is not worth a user's build. Every step is best-effort, problems are reported on stderr, and the exit status is always 0.
#
# The paths and API address are overridable only so the hook can be tested without a cluster; the defaults are what a runner pod has. The only API permission the pod's service account holds for this is create on events in its own namespace (roles/github_runner_arc/tasks/install_job_peak_rbac.yml).
set -u

cgroup_dir="${JOB_PEAK_CGROUP_DIR:-/sys/fs/cgroup}"
account_dir="${JOB_PEAK_SERVICEACCOUNT_DIR:-/var/run/secrets/kubernetes.io/serviceaccount}"
api_url="${JOB_PEAK_API_URL:-https://kubernetes.default.svc}"

warn() { echo "job-peak: $*" >&2; }

# memory.peak is the highest the container's cgroup has reached, in bytes, so a spike shorter than any polling interval is still in it.
peak="$(cat "${cgroup_dir}/memory.peak" 2>/dev/null)" || { warn "no memory.peak, nothing recorded"; exit 0; }
case "$peak" in '' | *[!0-9]*) warn "memory.peak was not a number ('${peak}'), nothing recorded"; exit 0 ;; esac

namespace="$(cat "${account_dir}/namespace" 2>/dev/null)" || { warn "no service account namespace, nothing recorded"; exit 0; }
token="$(cat "${account_dir}/token" 2>/dev/null)" || { warn "no service account token, nothing recorded"; exit 0; }

body="$(jq -nc \
  --arg namespace "$namespace" \
  --arg pod "${HOSTNAME:-}" \
  --arg repo "${GITHUB_REPOSITORY:-}" \
  --arg workflow "${GITHUB_WORKFLOW_REF:-}" \
  --arg job "${GITHUB_JOB:-}" \
  --argjson peak "$peak" '
  {
    apiVersion: "v1",
    kind: "Event",
    metadata: {generateName: "job-peak-", namespace: $namespace},
    involvedObject: {kind: "Pod", name: $pod, namespace: $namespace},
    reason: "JobMemoryPeak",
    type: "Normal",
    source: {component: "job-completed-hook"},
    message: ({repo: $repo, workflow: $workflow, job: $job, peak_bytes: $peak} | tojson)
  }')" || { warn "could not build the event, nothing recorded"; exit 0; }

status="$(curl -sS -m 5 -o /dev/null -w '%{http_code}' -X POST \
  --cacert "${account_dir}/ca.crt" \
  -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" \
  -d "$body" "${api_url}/api/v1/namespaces/${namespace}/events" 2>/dev/null)" || status="no response"
[ "$status" = "201" ] || warn "the API server answered ${status}, nothing recorded"
exit 0
