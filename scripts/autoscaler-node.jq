# What the autoscaler's jq programs and shell share about a Node object (scripts/autoscaler-ceiling.jq, and the memory check in scripts/autoscaler.sh), found through jq's -L, which the autoscaler points at its own directory, and pulled in with `include "autoscaler-node";`.

# Whether the node's Ready condition is True. A node whose kubelet has stopped reporting (Unknown) or has reported a fault (False) is not Ready, and so is a node with no Ready condition at all: the scheduler places no pod on it and metrics-server has no figures for it, so it is left out of every count of where runner pods can go.
def is_ready: ([.status.conditions[]? | select(.type == "Ready") | .status] | first) == "True";
