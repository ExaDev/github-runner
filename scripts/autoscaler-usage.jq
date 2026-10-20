# The memory a pooled scale set's runner pods use now, run by scripts/autoscaler.sh for each pooled namespace every poll. The input is the namespace's PodMetricsList from the metrics API; $pods is the slurped PodList of its runner pods, and $fallback_mib the memory a pod with no memory limit of its own is assumed to reach (its scale set's template limit).
#
# A pod in the metrics list is measured: its containers' memory usage, summed and rounded up to a whole MiB. A runner pod placed on a node and not finished, with no entry in the list, is unmeasured: metrics-server samples a pod only after it has run for a while, so every new runner pod is unmeasured at first. It is assumed to use its own memory limit, the most it can reach, so a raise never counts on room it might take. A pod not yet placed uses nothing yet.
#
# Output: {usage_mib, observations: [{pod, mib}], unmeasured: [{pod, assumed_mib}], assumed_mib}.
include "autoscaler-quantity";

def mib: . / 1048576 | ceil;

[.items[] | {pod: .metadata.name, mib: ([.containers[]?.usage.memory // 0 | amount("memory")] | add // 0 | mib)}] as $observations
| ($observations | map(.pod)) as $measured
| [$pods[0].items[]
   | select(.spec.nodeName != null and (.status.phase as $phase | ["Succeeded", "Failed"] | index($phase) | not))
   | select(.metadata.name as $name | $measured | index($name) | not)
   | {pod: .metadata.name, assumed_mib: ([.spec.containers[]?.resources.limits.memory // 0 | amount("memory")] | add // 0 | mib | if . > 0 then . else $fallback_mib end)}
  ] as $unmeasured
| {
    usage_mib: ([$observations[].mib] | add // 0),
    observations: $observations,
    unmeasured: $unmeasured,
    assumed_mib: ([$unmeasured[].assumed_mib] | add // 0)
  }
