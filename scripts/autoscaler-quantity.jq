# Kubernetes resource quantities for the autoscaler's jq programs (scripts/autoscaler-ceiling.jq, scripts/autoscaler-usage.jq), included with `include "autoscaler-quantity";` and jq -L naming this directory.

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
