# How many runner pods of the autoscaler's pooled scale sets the nodes can place now, run by scripts/autoscaler.sh each poll. The input is a PodList of every pod that has not finished (the API server's field selector leaves out Succeeded and Failed, as the scheduler does); $nodes is a slurped NodeList, $templates the slurped runner pod specs, one per pooled scale set, and $pooled the namespaces those scale sets' runner pods run in.
#
# A node counts when every pooled scale set's runner pods could be placed on it: it carries their nodeSelector, is Ready, is not cordoned, and their tolerations match each of its NoSchedule and NoExecute taints. The runner pod is taken as the largest request of the pooled templates, per resource. On each counted node, its allocatable cpu and memory less the requests of the pods on it that are not pooled runner pods, divided by that request, and its pod capacity less those pods, give how many fit; the smallest of the three, floored, is the node's figure, and the ceiling is their sum. The pooled runner pods are left out of each node's requests so that the runners already running never lower the ceiling they are counted in.
#
# Any figure that is missing or is not a Kubernetes quantity stops the program with an error, so the autoscaler never acts on a ceiling worked out from part of the cluster.
#
# Output: {ceiling, pod: {cpu_millicores, memory_bytes}, nodes: [{name, by_cpu, by_memory, by_pods, runners}], skipped: [{name, reason}]}.

# Kubernetes resource quantity suffixes (k8s.io/apimachinery resource.Quantity): binary powers of 1024, and decimal SI prefixes.
def suffixes: {"": 1, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18, "Ki": 1024, "Mi": 1048576, "Gi": 1073741824, "Ti": 1099511627776, "Pi": 1125899906842624, "Ei": 1152921504606846976};

# A quantity's value in base units (cores, or bytes).
def quantity:
  tostring as $text
  | ($text | capture("^(?<n>[+-]?(?:[0-9]+\\.?[0-9]*|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?)(?<s>Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)?$")
     // error("not a Kubernetes quantity: \($text | tojson)"))
  | (.n | tonumber) * suffixes[.s // ""];

# Whole units, rounded up as Kubernetes rounds a request (a cpu request below a millicore is one). jq's numbers are doubles, so a product such as 0.1 * 1000 can land a hair above a whole number; rounding to a millionth of a unit first keeps ceil from adding one for that.
def whole: (. * 1e6 | round) / 1e6 | ceil;
def amount($resource): if $resource == "cpu" then quantity * 1000 | whole else quantity | whole end;

# A container's request for one resource. A container that sets only a limit is given that limit as its request when its pod is created, which a scale set's template has not been yet; a running pod already carries the request, so the fallback changes nothing there.
def container_request($resource): (.resources.requests[$resource] // .resources.limits[$resource] // 0) | amount($resource);

# What the scheduler counts a pod spec as requesting for one resource: the larger of its app and sidecar containers' sum and its largest ordinary init container (each running beside the sidecars started before it), plus the pod's overhead.
def pod_request($resource):
  (reduce (.initContainers // [])[] as $container ({sidecars: 0, init_peak: 0};
    ($container | container_request($resource)) as $request
    | if $container.restartPolicy == "Always" then .sidecars += $request
      else .init_peak = ([.init_peak, $request + .sidecars] | max) end)) as $init
  | ([(.containers // [])[] | container_request($resource)] | add // 0) as $apps
  | ([$apps + $init.sidecars, $init.init_peak] | max) + ((.overhead[$resource] // 0) | amount($resource));

# Whether one of $tolerations matches $taint, by the rules of core/v1 Toleration: an empty key with operator Exists matches every taint; otherwise the keys are equal, an empty effect matches any effect, and operator Equal (the default) also needs equal values while Exists ignores them.
def tolerates($tolerations; $taint):
  any($tolerations[];
    (.key // "") as $key | (.operator // "Equal") as $operator
    | if $key == "" and $operator == "Exists" then true
      elif $key != $taint.key then false
      elif (.effect // "") != "" and .effect != $taint.effect then false
      else $operator == "Exists" or (.value // "") == ($taint.value // "") end);

# Why a node cannot take a runner pod of the template $spec now, or "" when it can.
def unplaceable($spec):
  (.metadata.labels // {}) as $labels
  | ([.status.conditions[]? | select(.type == "Ready") | .status] | first) as $ready
  | if any(($spec.nodeSelector // {}) | to_entries[]; $labels[.key] != (.value | tostring)) then "does not carry the runner pods' nodeSelector"
    elif $ready != "True" then "is not Ready"
    elif .spec.unschedulable == true then "is cordoned"
    else [.spec.taints[]? | select(.effect == "NoSchedule" or .effect == "NoExecute") | select(tolerates($spec.tolerations // []; .) | not) | "has the taint \(.key):\(.effect)"] | first // ""
    end;

def allocatable($node; $resource):
  $node.status.allocatable[$resource] // error("node \($node.metadata.name) reports no allocatable \($resource)");

def at_least_zero: if . < 0 then 0 else . end;

{cpu_millicores: ([$templates[] | pod_request("cpu")] | max), memory_bytes: ([$templates[] | pod_request("memory")] | max)} as $pod
| if $pod.cpu_millicores <= 0 or $pod.memory_bytes <= 0 then error("the runner pod requests no cpu or no memory, so how many fit on a node cannot be worked out") else . end
| [.items[] | select(.spec.nodeName != null and (.metadata.namespace as $namespace | $pooled | index($namespace) | not))] as $others
| [$nodes[0].items[] as $node
   | ([$templates[] as $spec | $node | unplaceable($spec) | select(. != "")] | first) as $reason
   | if $reason != null then {skipped: {name: $node.metadata.name, reason: $reason}}
     else [$others[] | select(.spec.nodeName == $node.metadata.name) | .spec] as $on
       | ((allocatable($node; "cpu") | amount("cpu")) - ([$on[] | pod_request("cpu")] | add // 0)) as $free_cpu
       | ((allocatable($node; "memory") | amount("memory")) - ([$on[] | pod_request("memory")] | add // 0)) as $free_memory
       | {
           name: $node.metadata.name,
           by_cpu: ($free_cpu / $pod.cpu_millicores | floor | at_least_zero),
           by_memory: ($free_memory / $pod.memory_bytes | floor | at_least_zero),
           by_pods: ((allocatable($node; "pods") | quantity | floor) - ($on | length) | at_least_zero)
         }
       | {placed: (. + {runners: ([.by_cpu, .by_memory, .by_pods] | min)})}
     end] as $results
| {
    ceiling: ([$results[].placed.runners // empty] | add // 0),
    pod: $pod,
    nodes: [$results[].placed // empty],
    skipped: [$results[].skipped // empty]
  }
