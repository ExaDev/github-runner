"""Filters for the github_runner_arc role: expanding orgs into scale-set profiles, placing their runner pods (on the eligible nodes, or overflowing onto burst nodes), deriving a runner ceiling from node capacity (uniform figures, or each node's measured free capacity), adding a profile's capabilities (tool-cache and system-library init containers and job-started hooks) to its Helm values, and splitting an image reference."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from ansible.module_utils.parsing.convert_bool import boolean

# Kubernetes namespace and Helm release names must be RFC 1123 DNS labels.
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_DNS_LABEL_MAX_LENGTH = 63

# Sizing inputs: (key, required, default, lower bound, lower bound inclusive, upper bound, upper bound inclusive). None means unbounded.
_SIZING_INPUTS: tuple[tuple[str, bool, str, str | None, bool, str | None, bool], ...] = (
    ("node_allocatable_cpu", True, "0", "0", False, None, False),
    ("node_allocatable_memory_gib", True, "0", "0", False, None, False),
    ("node_count", True, "0", "1", True, None, False),
    ("pod_cpu_request", True, "0", "0", False, None, False),
    ("pod_memory_request_gib", True, "0", "0", False, None, False),
    ("sidecar_cpu_request", False, "0", "0", True, None, False),
    ("sidecar_memory_request_gib", False, "0", "0", True, None, False),
    ("baseline_cpu_fraction", False, "0", "0", True, "1", False),
    ("baseline_memory_fraction", False, "0", "0", True, "1", False),
    ("safety_margin", False, "1", "0", False, "1", True),
)


# Measured sizing inputs, in the same form: the node figures come from the cluster instead, and reserve_* is held back on every node for what the requests do not show.
_MEASURED_SIZING_INPUTS: tuple[tuple[str, bool, str, str | None, bool, str | None, bool], ...] = (
    ("pod_cpu_request", True, "0", "0", False, None, False),
    ("pod_memory_request_gib", True, "0", "0", False, None, False),
    ("sidecar_cpu_request", False, "0", "0", True, None, False),
    ("sidecar_memory_request_gib", False, "0", "0", True, None, False),
    ("reserve_cpu", False, "0", "0", True, None, False),
    ("reserve_memory_gib", False, "0", "0", True, None, False),
    ("safety_margin", False, "1", "0", False, "1", True),
)

# Kubernetes resource quantity suffixes (k8s.io/apimachinery resource.Quantity): binary powers of 1024, and decimal SI prefixes.
_QUANTITY = re.compile(r"^([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)?$")
_QUANTITY_SUFFIXES = {
    "": Decimal(1),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
    "Ki": Decimal(1024),
    "Mi": Decimal(1024) ** 2,
    "Gi": Decimal(1024) ** 3,
    "Ti": Decimal(1024) ** 4,
    "Pi": Decimal(1024) ** 5,
    "Ei": Decimal(1024) ** 6,
}
_GIB = Decimal(1024) ** 3

# Pod phases whose containers have all stopped, so the scheduler no longer counts the pod's requests against its node.
_TERMINAL_POD_PHASES = frozenset({"Succeeded", "Failed"})
# Taint effects that keep a pod without a matching toleration off a node. Runner pods are taken to tolerate none.
_REPELLING_TAINT_EFFECTS = frozenset({"NoSchedule", "NoExecute"})
# The prefix of the taints Kubernetes itself puts on a node for a passing condition (not ready, unreachable, under pressure, cordoned). The ceiling outlasts the moment it is measured, so a node in such a state still counts, as it will when it recovers; only a node's labels and deliberately added taints decide whether it is eligible.
_CONDITION_TAINT_PREFIX = "node.kubernetes.io/"

# A profile's burst keys (see _burst), and the fields of a Kubernetes toleration (core/v1 Toleration).
_BURST_KEYS = frozenset({"node_label_key", "node_label_values", "tolerations", "max_runners", "exclusive"})
_TOLERATION_KEYS = frozenset({"key", "operator", "value", "effect", "tolerationSeconds"})
# The highest weight a preferred node affinity term may carry (1-100 in the Kubernetes API), so keeping runner pods on the fixed nodes outweighs any other preference the scheduler scores.
_PREFER_FIXED_NODES_WEIGHT = 100

# A capability's keys (see arc_capability_errors): a tool-cache capability names an image, and optionally a path put on PATH at job start; a system-library capability names a sysroot_image, and optionally env, extra variables its libraries need; a hook capability carries a job-started script.
_CAPABILITY_KEYS = frozenset({"image", "path", "sysroot_image", "env", "job_started_hook"})
# The keys of which a capability sets exactly one, each naming its kind.
_CAPABILITY_KINDS = ("image", "sysroot_image", "job_started_hook")
# Each tool-cache or system-library capability becomes an init container named with this prefix, and a container name is a DNS label, so a capability name is at most what is left of one.
_CAPABILITY_CONTAINER_PREFIX = "capability-"
_CAPABILITY_NAME_MAX_LENGTH = _DNS_LABEL_MAX_LENGTH - len(_CAPABILITY_CONTAINER_PREFIX)
# Where a tool image keeps its payload, already in the Actions tool-cache layout: <tool>/<version>/<arch>/ beside its <arch>.complete marker. Part of the tool-image contract in the role README.
_TOOL_IMAGE_PAYLOAD = "/toolcache"
# Where a system-library image keeps its payload: a relocatable prefix (bin/, lib/, include/, lib/pkgconfig/) that is copied into the sysroot. Part of the payload contract in the role README.
_SYSROOT_IMAGE_PAYLOAD = "/sysroot"
# One component of a capability's path: a tool name, a version or a directory name, never '.' or '..'.
_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_+-][A-Za-z0-9._+-]*$")
# A variable a system-library capability may set: a shell and POSIX environment variable name.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# The pod volumes the capabilities add: the tool cache every tool-cache capability copies into, the sysroot every system-library capability copies into, and the ConfigMap holding the job-started hook.
_TOOL_CACHE_VOLUME = "tool-cache"
_SYSROOT_VOLUME = "sysroot"
_HOOKS_VOLUME = "job-started-hooks"
# The runner's own variables the capabilities set on the runner container: where it and the setup actions look for cached tools, and the hook it runs before a job's first step.
_TOOL_CACHE_ENV = "RUNNER_TOOL_CACHE"
_JOB_STARTED_HOOK_ENV = "ACTIONS_RUNNER_HOOK_JOB_STARTED"
# The search paths a sysroot joins, as (variable, directory under the sysroot), in the order the job-started hook prepends them: commands, the dynamic loader's libraries, the linker's libraries, the compiler's headers, pkg-config files, and CMake packages. The hook prepends each to the variable's value at job start rather than the container's environment setting it, because a container variable replaces the image's own value (PATH above all) instead of extending it, and because the runner process itself then never loads a library from the sysroot.
_SYSROOT_SEARCH_PATHS = (
    ("PATH", "bin"),
    ("LD_LIBRARY_PATH", "lib"),
    ("LIBRARY_PATH", "lib"),
    ("CPATH", "include"),
    ("PKG_CONFIG_PATH", "lib/pkgconfig"),
    ("CMAKE_PREFIX_PATH", ""),
)
# Variables a system-library capability's env may not set: the search paths the sysroot already extends, and the runner variables the capabilities set.
_RESERVED_ENV = frozenset(name for name, _ in _SYSROOT_SEARCH_PATHS) | {_TOOL_CACHE_ENV, _JOB_STARTED_HOOK_ENV}
# Keys of the job-started ConfigMap: the dispatcher the runner runs (roles/github_runner_arc/files/job-started.sh), the tool paths it puts on PATH, the sysroot's environment it applies, and the directory of capability scripts it runs after that.
_HOOK_DISPATCHER = "job-started.sh"
_HOOK_TOOL_PATHS = "tool-paths"
_HOOK_SYSROOT_ENV = "sysroot-env"
_HOOK_SCRIPTS_DIR = "job-started.d"
# Hook files are read by the runner user, never written, and the runner runs them with bash, so they need no execute bit: 0444.
_HOOK_FILE_MODE = 0o444
# Container modes whose job steps run in a separate pod (the chart's container hooks), which sees neither the runner container's tool cache nor its PATH.
_SEPARATE_POD_CONTAINER_MODES = frozenset({"kubernetes", "kubernetes-novolume"})


def _decimal(value: Any) -> Decimal | None:
    """Return value as an exact Decimal, or None when it is not a finite number."""
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _floor(value: Decimal) -> int:
    """Round a Decimal down to a whole number, exactly."""
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _sizing_values(sizing: Mapping[str, Any], inputs: tuple[tuple[str, bool, str, str | None, bool, str | None, bool], ...], errors: list[str]) -> dict[str, Decimal]:
    """Check sizing against one of the input tables, recording each problem in errors, and return the values that are valid, with defaults filled in."""
    values: dict[str, Decimal] = {}
    for key, required, default, low, low_inclusive, high, high_inclusive in inputs:
        if key not in sizing or sizing[key] is None:
            if required:
                errors.append(f"sizing.{key} is required")
                continue
            values[key] = Decimal(default)
            continue
        number = _decimal(sizing[key])
        if number is None:
            errors.append(f"sizing.{key} must be a number (got {sizing[key]!r})")
            continue
        if low is not None and (number < Decimal(low) or (number == Decimal(low) and not low_inclusive)):
            errors.append(f"sizing.{key} must be {'at least' if low_inclusive else 'greater than'} {low} (got {number})")
            continue
        if high is not None and (number > Decimal(high) or (number == Decimal(high) and not high_inclusive)):
            errors.append(f"sizing.{key} must be {'at most' if high_inclusive else 'less than'} {high} (got {number})")
            continue
        values[key] = number
    return values


def _is_measured(sizing: Mapping[str, Any], errors: list[str]) -> bool:
    """Return whether sizing asks for measured node figures, recording an error when its measured key is not a boolean."""
    try:
        return boolean(sizing.get("measured", False), strict=True)
    except TypeError:
        errors.append(f"sizing.measured must be a boolean (got {sizing.get('measured')!r})")
        return False


def arc_max_runners(sizing: Mapping[str, Any]) -> dict[str, Any]:
    """Derive how many runner pods a pool of identical nodes can hold, or check the inputs of a measured sizing.

    Each node's allocatable CPU and memory, less a baseline fraction reserved for what already runs there, is divided by one runner pod's requests (the runner container plus any sidecar such as dind). The tighter of the two bounds gives pods per node; that times the node count, scaled down by the safety margin, is the result. Decimal arithmetic keeps the floors exact for inputs such as 0.1.

    With ``measured: true`` the node figures are not inputs: they are read from the cluster at install time and passed to arc_measured_max_runners. Only the pod's requests, the per-node reserve and the safety margin are checked here, and max_runners is None until then.

    Args:
        sizing: the inputs named in _SIZING_INPUTS, or with measured true those in _MEASURED_SIZING_INPUTS. Optional ones default to no sidecar, no baseline reservation or reserve, and no safety margin.

    Returns:
        A dict with ``errors`` (a list of messages, empty when the inputs are valid), ``measured`` and ``max_runners``. For uniform sizing with valid inputs it also has every intermediate figure (``usable_cpu``, ``usable_memory_gib``, ``pod_cpu``, ``pod_memory_gib``, ``per_node_by_cpu``, ``per_node_by_memory``, ``per_node``, ``theoretical``). For measured sizing it has ``pod_cpu``, ``pod_memory_gib``, ``reserve_cpu``, ``reserve_memory_gib`` and ``safety_margin``, and ``max_runners`` is None. ``max_runners`` is None whenever there are errors.
    """
    errors: list[str] = []
    if not isinstance(sizing, Mapping):
        return {"errors": ["sizing must be a mapping"], "measured": False, "max_runners": None}
    measured = _is_measured(sizing, errors)
    inputs = _MEASURED_SIZING_INPUTS if measured else _SIZING_INPUTS
    known = {spec[0] for spec in inputs} | {"measured"}
    errors.extend(f"sizing has an unknown key '{key}'{' (measured sizing reads the nodes from the cluster)' if measured and key in {spec[0] for spec in _SIZING_INPUTS} else ''}" for key in sorted(set(sizing) - known))
    values = _sizing_values(sizing, inputs, errors)
    if not measured and "node_count" in values and values["node_count"] != values["node_count"].to_integral_value():
        errors.append(f"sizing.node_count must be a whole number (got {values['node_count']})")
    if errors:
        return {"errors": errors, "measured": measured, "max_runners": None}
    pod_cpu = values["pod_cpu_request"] + values["sidecar_cpu_request"]
    pod_memory = values["pod_memory_request_gib"] + values["sidecar_memory_request_gib"]
    if measured:
        return {
            "errors": [],
            "measured": True,
            "pod_cpu": float(pod_cpu),
            "pod_memory_gib": float(pod_memory),
            "reserve_cpu": float(values["reserve_cpu"]),
            "reserve_memory_gib": float(values["reserve_memory_gib"]),
            "safety_margin": float(values["safety_margin"]),
            "max_runners": None,
        }

    usable_cpu = values["node_allocatable_cpu"] * (1 - values["baseline_cpu_fraction"])
    usable_memory = values["node_allocatable_memory_gib"] * (1 - values["baseline_memory_fraction"])
    per_node_by_cpu = _floor(usable_cpu / pod_cpu)
    per_node_by_memory = _floor(usable_memory / pod_memory)
    per_node = min(per_node_by_cpu, per_node_by_memory)
    theoretical = per_node * int(values["node_count"])
    return {
        "errors": [],
        "measured": False,
        "usable_cpu": float(usable_cpu),
        "usable_memory_gib": float(usable_memory),
        "pod_cpu": float(pod_cpu),
        "pod_memory_gib": float(pod_memory),
        "per_node_by_cpu": per_node_by_cpu,
        "per_node_by_memory": per_node_by_memory,
        "per_node": per_node,
        "theoretical": theoretical,
        "max_runners": _floor(theoretical * values["safety_margin"]),
    }


def arc_quantity(value: Any) -> Decimal:
    """Parse a Kubernetes resource quantity such as 3500m, 4251Mi, 1.5 or 2e3 into its value in base units (cores, or bytes).

    Raises:
        ValueError: when value is not a quantity.
    """
    match = _QUANTITY.match(str(value).strip())
    if match is None:
        raise ValueError(f"not a Kubernetes quantity: {value!r}")
    return Decimal(match.group(1)) * _QUANTITY_SUFFIXES[match.group(2) or ""]


def _container_requests(container: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    """Return a container's CPU (cores) and memory (bytes) requests, zero where it sets none."""
    requests = (container.get("resources") or {}).get("requests") or {}
    return arc_quantity(requests.get("cpu", 0)), arc_quantity(requests.get("memory", 0))


def _pod_requests(pod: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    """Return what the scheduler counts a pod as requesting, per resource: the larger of its app and sidecar containers' sum and its largest ordinary init container (each running beside the sidecars started before it), plus the pod's overhead."""
    spec = pod.get("spec") or {}
    totals = []
    for index in (0, 1):
        sidecars = Decimal(0)
        init_peak = Decimal(0)
        for container in spec.get("initContainers") or []:
            request = _container_requests(container)[index]
            if container.get("restartPolicy") == "Always":
                sidecars += request
            else:
                init_peak = max(init_peak, request + sidecars)
        apps = sum((_container_requests(container)[index] for container in spec.get("containers") or []), Decimal(0))
        overhead = arc_quantity((spec.get("overhead") or {}).get(("cpu", "memory")[index], 0))
        totals.append(max(apps + sidecars, init_peak) + overhead)
    return totals[0], totals[1]


def _node_eligibility(node: Mapping[str, Any], node_selector: Mapping[str, Any]) -> tuple[str, str]:
    """Return why runner pods can never be placed on node (empty when they can), and any passing condition that keeps them off it for now."""
    labels = (node.get("metadata") or {}).get("labels") or {}
    for key, value in node_selector.items():
        if labels.get(key) != str(value):
            return f"does not have the label {key}={value}", ""
    spec = node.get("spec") or {}
    conditions = ["is cordoned"] if spec.get("unschedulable") else []
    for taint in spec.get("taints") or []:
        if taint.get("effect") not in _REPELLING_TAINT_EFFECTS:
            continue
        description = f"has the taint {taint.get('key')}:{taint.get('effect')}"
        if not str(taint.get("key", "")).startswith(_CONDITION_TAINT_PREFIX):
            return description, ""
        conditions.append(description)
    return "", ", ".join(conditions)


def arc_measured_max_runners(sizing: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]], pods: Sequence[Mapping[str, Any]], node_selector: Mapping[str, Any] | None = None, excluded_namespaces: Sequence[str] = ()) -> dict[str, Any]:
    """Derive how many runner pods fit on the eligible nodes as they are now.

    A node is eligible when it carries every label in node_selector and has no NoSchedule or NoExecute taint other than the ones Kubernetes adds for a passing condition (not ready, unreachable, cordoned and the like): the ceiling lasts until the next run, so a node that is only briefly unavailable still counts. For each one, its allocatable CPU and memory, less what the pods already on it request and less the sizing's reserve, is divided by one runner pod's requests; the tighter bound is how many fit there. The sum over the nodes, scaled down by the safety margin, is the result. Pods in excluded_namespaces are not counted, so the runner pods themselves never lower the ceiling they are sized by, and pods that have finished are not counted, as the scheduler does not count them.

    Args:
        sizing: a measured sizing mapping (see arc_max_runners).
        nodes: Node objects, as kubernetes.core.k8s_info returns them.
        pods: Pod objects from every namespace.
        node_selector: labels an eligible node must carry.
        excluded_namespaces: namespaces whose pods are left out of each node's requests.

    Returns:
        A dict with ``errors`` (messages, empty when it could be worked out), ``nodes`` (per eligible node: name, allocatable_cpu, allocatable_memory_gib, requested_cpu, requested_memory_gib, by_cpu, by_memory, runners, and condition, naming any passing condition it was counted despite), ``skipped_nodes`` (name and reason for each node that is not eligible), ``theoretical`` and ``max_runners``, which is None when there are errors.
    """
    checked = arc_max_runners(sizing)
    errors = list(checked["errors"])
    if not errors and not checked["measured"]:
        errors.append("sizing is not measured (set measured: true)")
    if errors:
        return {"errors": errors, "nodes": [], "skipped_nodes": [], "theoretical": None, "max_runners": None}
    selector = dict(node_selector or {})
    excluded = set(excluded_namespaces)
    pod_cpu = Decimal(str(checked["pod_cpu"]))
    pod_memory = Decimal(str(checked["pod_memory_gib"])) * _GIB
    reserve_cpu = Decimal(str(checked["reserve_cpu"]))
    reserve_memory = Decimal(str(checked["reserve_memory_gib"])) * _GIB
    requested: dict[str, list[Decimal]] = {}
    for pod in pods:
        metadata = pod.get("metadata") or {}
        node_name = (pod.get("spec") or {}).get("nodeName")
        if not node_name or metadata.get("namespace") in excluded or (pod.get("status") or {}).get("phase") in _TERMINAL_POD_PHASES:
            continue
        try:
            cpu, memory = _pod_requests(pod)
        except ValueError as error:
            errors.append(f"pod {metadata.get('namespace')}/{metadata.get('name')}: {error}")
            continue
        totals = requested.setdefault(node_name, [Decimal(0), Decimal(0)])
        totals[0] += cpu
        totals[1] += memory
    eligible: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for node in sorted(nodes, key=lambda item: (item.get("metadata") or {}).get("name", "")):
        name = (node.get("metadata") or {}).get("name", "")
        reason, condition = _node_eligibility(node, selector)
        if reason:
            skipped.append({"name": name, "reason": reason})
            continue
        allocatable = (node.get("status") or {}).get("allocatable") or {}
        try:
            allocatable_cpu = arc_quantity(allocatable.get("cpu", 0))
            allocatable_memory = arc_quantity(allocatable.get("memory", 0))
        except ValueError as error:
            errors.append(f"node {name}: {error}")
            continue
        used_cpu, used_memory = requested.get(name, [Decimal(0), Decimal(0)])
        by_cpu = max(0, _floor((allocatable_cpu - used_cpu - reserve_cpu) / pod_cpu))
        by_memory = max(0, _floor((allocatable_memory - used_memory - reserve_memory) / pod_memory))
        eligible.append({
            "name": name,
            "allocatable_cpu": float(allocatable_cpu),
            "allocatable_memory_gib": float(allocatable_memory / _GIB),
            "requested_cpu": float(used_cpu),
            "requested_memory_gib": float(used_memory / _GIB),
            "by_cpu": by_cpu,
            "by_memory": by_memory,
            "runners": min(by_cpu, by_memory),
            "condition": condition,
        })
    if not eligible and not errors:
        errors.append(f"no node is eligible for runner pods{' with the labels ' + ', '.join(f'{key}={value}' for key, value in selector.items()) if selector else ''}")
    if errors:
        return {"errors": errors, "nodes": eligible, "skipped_nodes": skipped, "theoretical": None, "max_runners": None}
    theoretical = sum(node["runners"] for node in eligible)
    return {
        "errors": [],
        "nodes": eligible,
        "skipped_nodes": skipped,
        "theoretical": theoretical,
        "max_runners": _floor(Decimal(theoretical) * Decimal(str(checked["safety_margin"]))),
    }


def _text(mapping: Mapping[str, Any], key: str) -> str:
    """Return a string field, treating an unset or null value as empty."""
    value = mapping.get(key)
    return "" if value is None else str(value)


def _check_dns_label(kind: str, name: str, where: str, errors: list[str]) -> None:
    """Record an error when name is not a valid Kubernetes DNS label."""
    if len(name) > _DNS_LABEL_MAX_LENGTH or not _DNS_LABEL.match(name):
        errors.append(f"{where}: the derived {kind} '{name}' is not a valid Kubernetes name (lower-case letters, digits and '-', at most {_DNS_LABEL_MAX_LENGTH} characters)")


def _optional_name(mapping: Mapping[str, Any], key: str, where: str, errors: list[str]) -> str:
    """Return an optional name override, or empty when it is unset, recording an error when it is set but not a non-empty string."""
    value = mapping.get(key)
    if value is None:
        return ""
    if not isinstance(value, str) or not value:
        errors.append(f"{where}.{key} must be a non-empty string when set")
        return ""
    return value


def _scale_set_labels(profile: Mapping[str, Any], default: str, where: str, errors: list[str]) -> list[str]:
    """Return the scaleSetLabels a profile's release sets: scale_set_labels when given (an empty list sets none), otherwise runs_on_label or the org's pooled default."""
    if profile.get("scale_set_labels") is None:
        return [_text(profile, "runs_on_label") or default]
    if profile.get("runs_on_label") is not None:
        errors.append(f"{where}: set runs_on_label or scale_set_labels, not both")
    labels = profile["scale_set_labels"]
    if not isinstance(labels, Sequence) or isinstance(labels, (str, bytes)) or not all(isinstance(label, str) and label for label in labels):
        errors.append(f"{where}.scale_set_labels must be a list of non-empty strings (empty sets no scaleSetLabels)")
        return []
    return list(labels)


def _capability_names(profile: Mapping[str, Any], where: str, errors: list[str]) -> list[str]:
    """Return the capabilities a profile names, in its order, recording an error when they are not a list of distinct non-empty strings."""
    names = profile.get("capabilities")
    if names is None:
        return []
    if not isinstance(names, Sequence) or isinstance(names, (str, bytes)) or not all(isinstance(name, str) and name for name in names):
        errors.append(f"{where}.capabilities must be a list of capability names")
        return []
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        errors.append(f"{where}.capabilities names {', '.join(duplicates)} more than once")
    return list(dict.fromkeys(names))


def _explicit_max_runners(profile: Mapping[str, Any], where: str, errors: list[str]) -> int | None:
    """Return the profile's own max_runners as a whole number, None when unset, recording an error when it is not a whole number of at least 0."""
    value = profile.get("max_runners")
    if value is None:
        return None
    number = _decimal(value)
    if number is None or number < 0 or number != number.to_integral_value():
        errors.append(f"{where}.max_runners must be a whole number of at least 0 (got {value!r})")
        return None
    return int(number)


def _burst(profile: Mapping[str, Any], where: str, errors: list[str]) -> dict[str, Any] | None:
    """Return a profile's burst settings with defaults filled in, None when it sets none, recording each problem in errors."""
    burst = profile.get("burst")
    if burst is None:
        return None
    if not isinstance(burst, Mapping):
        errors.append(f"{where}.burst must be a mapping")
        return None
    errors.extend(f"{where}.burst has an unknown key '{key}'" for key in sorted(set(burst) - _BURST_KEYS))
    label_key = burst.get("node_label_key")
    if not isinstance(label_key, str) or not label_key:
        errors.append(f"{where}.burst.node_label_key must be a non-empty string")
    label_values = burst.get("node_label_values") or []
    if not isinstance(label_values, Sequence) or isinstance(label_values, (str, bytes)) or not all(isinstance(value, str) and value for value in label_values):
        errors.append(f"{where}.burst.node_label_values must be a list of non-empty strings (empty or unset accepts any value)")
        label_values = []
    tolerations = burst.get("tolerations") or []
    if not isinstance(tolerations, Sequence) or isinstance(tolerations, (str, bytes)) or not all(isinstance(toleration, Mapping) for toleration in tolerations):
        errors.append(f"{where}.burst.tolerations must be a list of Kubernetes tolerations")
        tolerations = []
    for toleration_index, toleration in enumerate(tolerations):
        errors.extend(f"{where}.burst.tolerations[{toleration_index}] has an unknown key '{key}'" for key in sorted(set(toleration) - _TOLERATION_KEYS))
    max_runners = _decimal(burst.get("max_runners"))
    if max_runners is None or max_runners < 0 or max_runners != max_runners.to_integral_value():
        errors.append(f"{where}.burst.max_runners must be a whole number of at least 0 (got {burst.get('max_runners')!r})")
        max_runners = Decimal(0)
    try:
        exclusive = boolean(burst.get("exclusive", False), strict=True)
    except TypeError:
        errors.append(f"{where}.burst.exclusive must be a boolean")
        exclusive = False
    return {"node_label_key": label_key, "node_label_values": list(label_values), "tolerations": [dict(toleration) for toleration in tolerations], "max_runners": int(max_runners), "exclusive": exclusive}


def arc_runner_placement(profile: Mapping[str, Any], eligibility: Mapping[str, Any]) -> dict[str, Any]:
    """Return where a profile's runner pods may be scheduled, as pod spec fields: nodeSelector, and with burst also affinity and tolerations.

    With burst.exclusive the pods may go only on a burst node: the required node affinity has the burst term alone and nothing is preferred. That suits work too large for the fixed nodes, such as image builds whose Docker daemon needs more memory than they can spare.

    Without burst the pods are held by nodeSelector to nodes carrying the eligibility label and the profile's node_selector. With burst the eligibility label moves into a required node affinity that also accepts a node carrying the burst label, so a pod that finds no room on an eligible node stays Pending until a burst node can take it, which is what makes Cluster Autoscaler add one. A preferred term keeps the pods off burst nodes while an eligible node has room. The profile's node_selector still applies to every node, and the tolerations let the pods past the burst nodes' taint.

    Args:
        profile: an expanded profile from arc_profiles. eligibility: the eligibility label as {key: value}, empty when the role sets none.

    Returns:
        A dict of pod spec fields for the runner pod template.
    """
    selector = {key: str(value) for key, value in eligibility.items()}
    burst = profile["burst"]
    if burst is None:
        return {"nodeSelector": {**selector, **profile["node_selector"]}}
    burst_node = {"key": burst["node_label_key"], "operator": "In", "values": burst["node_label_values"]} if burst["node_label_values"] else {"key": burst["node_label_key"], "operator": "Exists"}
    # An exclusive profile runs on burst nodes only, whatever the eligibility label, so there is no fixed node to prefer.
    if burst["exclusive"]:
        return {"nodeSelector": dict(profile["node_selector"]), "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [burst_node]}]}}}, "tolerations": burst["tolerations"]}
    node_affinity: dict[str, Any] = {"preferredDuringSchedulingIgnoredDuringExecution": [{"weight": _PREFER_FIXED_NODES_WEIGHT, "preference": {"matchExpressions": [{"key": burst["node_label_key"], "operator": "DoesNotExist"}]}}]}
    # With no eligibility label every untainted node is already allowed, and the tolerations alone add the burst nodes.
    if selector:
        eligible_node = [{"key": key, "operator": "In", "values": [value]} for key, value in selector.items()]
        node_affinity["requiredDuringSchedulingIgnoredDuringExecution"] = {"nodeSelectorTerms": [{"matchExpressions": eligible_node}, {"matchExpressions": [burst_node]}]}
    return {"nodeSelector": dict(profile["node_selector"]), "affinity": {"nodeAffinity": node_affinity}, "tolerations": burst["tolerations"]}


def _expand_profile(org_name: str, app_secret: str, index: int, profile: Any, errors: list[str]) -> dict[str, Any] | None:
    """Describe one scale-set profile of an org, or record why it cannot be described."""
    where = f"{org_name} scale_set_profiles[{index}]"
    if not isinstance(profile, Mapping):
        errors.append(f"{where} must be a mapping")
        return None
    suffix = _text(profile, "suffix")
    org_lower = org_name.lower()
    node_selector = profile.get("node_selector") or {}
    if not isinstance(node_selector, Mapping):
        errors.append(f"{where}.node_selector must be a mapping")
        node_selector = {}
    try:
        autoscale = boolean(profile.get("autoscale", False), strict=True)
    except TypeError:
        errors.append(f"{where}.autoscale must be a boolean")
        autoscale = False
    sizing_result = None
    if profile.get("sizing") is not None:
        sizing_result = arc_max_runners(profile["sizing"])
        errors.extend(f"{where}: {message}" for message in sizing_result["errors"])
    explicit_max_runners = _explicit_max_runners(profile, where, errors)
    burst = _burst(profile, where, errors)
    # Burst runners are added to a ceiling the role derives from the fixed nodes, so they need sizing on a profile that sets its own maxRunners: an explicit max_runners is final, and the autoscaler's usage-driven ceiling has no notion of nodes that do not exist yet.
    if burst is not None and burst["exclusive"] and (sizing_result is not None or explicit_max_runners is not None or autoscale):
        errors.append(f"{where}: an exclusive burst takes no sizing, max_runners or autoscale, since its runners use no eligible node and burst.max_runners is the whole ceiling")
    if burst is not None and not burst["exclusive"] and (sizing_result is None or explicit_max_runners is not None or autoscale):
        errors.append(f"{where}: burst needs sizing, and no max_runners or autoscale, since its max_runners is added to the ceiling sizing derives")
    burst_runners = burst["max_runners"] if burst is not None else 0
    # The static maxRunners the role sets on the release: the profile's own max_runners wins; otherwise sizing supplies it, plus any burst runners, except on the autoscaled profile, whose sizing sets the autoscaler's ceiling rather than its floor. A measured sizing's result is known only at install time, so its max_runners stays None here and the burst runners are added then.
    if burst is not None and burst["exclusive"]:
        max_runners = burst_runners
    elif explicit_max_runners is not None:
        max_runners = explicit_max_runners
    elif sizing_result is not None and not sizing_result["errors"] and not autoscale:
        max_runners = None if sizing_result["max_runners"] is None else sizing_result["max_runners"] + burst_runners
    else:
        max_runners = None
    namespace = _optional_name(profile, "namespace", where, errors) or f"arc-runners-{org_lower}{suffix}"
    release = _optional_name(profile, "release_name", where, errors) or f"{org_lower}-runners{suffix}"
    expanded = {
        "org_name": org_name,
        "suffix": suffix,
        "namespace": namespace,
        "release": release,
        # namespace/release joined for the autoscaler's own AUTOSCALER_TARGETS list (see scripts/autoscaler.sh): both are validated DNS labels below, so neither can itself contain '/', keeping each token unambiguous.
        "target": f"{namespace}/{release}",
        "app_secret": app_secret,
        "scale_set_labels": _scale_set_labels(profile, f"{org_lower}-runners", where, errors),
        "values_file": _text(profile, "values_file"),
        "node_selector": dict(node_selector),
        "autoscale": autoscale,
        "sizing": sizing_result,
        "burst": burst,
        "burst_runners": burst_runners,
        "max_runners": max_runners,
        "capabilities": _capability_names(profile, where, errors),
        "label": f"{org_name}{suffix}",
        "settings": dict(profile),
    }
    # The role's own values template needs a static maxRunners: max_runners, or sizing on a profile the autoscaler does not manage (the autoscaled profile's sizing sets the autoscaler's ceiling, not its floor).
    if not expanded["values_file"] and profile.get("max_runners") is None and (sizing_result is None or autoscale):
        errors.append(f"{where}: a profile with no values_file needs max_runners{'' if autoscale else ' or sizing'}")
    _check_dns_label("namespace", expanded["namespace"], where, errors)
    _check_dns_label("release name", expanded["release"], where, errors)
    return expanded


def arc_profiles(orgs: Sequence[Any], require_app_id: bool = True) -> dict[str, Any]:
    """Expand github_runner_arc_orgs entries into one record per scale-set profile, and check them.

    Pass every org configured anywhere in the inventory, not one host's, so that the cross-host checks (an org configured twice, two profiles sharing a namespace, more than one autoscaled profile setting its own sizing) see the whole fleet.

    Args:
        require_app_id: whether each org must set app_id. The role needs it only when it writes the App Secret; otherwise the App's id is in the existing App Secret, and an app_id that is set is only checked against it.
        orgs: github_runner_arc_orgs entries, each with name, app_id (see require_app_id), image and a non-empty scale_set_profiles list, at most one of private_key and private_key_op_reference, and optionally app_secret_name. A profile may override its namespace and release_name, and set scale_set_labels in place of runs_on_label. A profile with sizing may set burst (node_label_key, node_label_values, tolerations, max_runners) to let its runner pods overflow onto burst nodes, and a profile without sizing may set burst with exclusive true to run on burst nodes only, with burst.max_runners as its maxRunners; see arc_runner_placement.

    Returns:
        A dict with ``errors`` (messages, empty when valid), ``profiles`` (one dict per profile with org_name, suffix, namespace, release, target (namespace/release, joined), app_secret, scale_set_labels, values_file, node_selector, autoscale, sizing, burst (the burst settings with defaults filled in, or None), burst_runners (burst.max_runners, 0 without burst), max_runners (the static maxRunners the role sets, including burst_runners, or None), capabilities (the names of the capabilities its runner pods carry, in order; see arc_capability_pod), label and settings, the profile's own keys) and ``autoscaled`` (every profile flagged autoscale, sharing one autoscaler-managed memory pool across their combined scale sets; empty when none are).
    """
    errors: list[str] = []
    profiles: list[dict[str, Any]] = []
    if not isinstance(orgs, Sequence) or isinstance(orgs, (str, bytes)):
        return {"errors": ["github_runner_arc_orgs must be a list"], "profiles": [], "autoscaled": []}
    seen_orgs: dict[str, str] = {}
    for index, org in enumerate(orgs):
        where = f"github_runner_arc_orgs[{index}]"
        if not isinstance(org, Mapping):
            errors.append(f"{where} must be a mapping")
            continue
        org_name = _text(org, "name")
        if not org_name:
            errors.append(f"{where} has no name")
            continue
        if require_app_id and not _text(org, "app_id"):
            errors.append(f"{org_name}: app_id is required when the role writes the App Secret (github_runner_arc_manage_secrets); with an existing App Secret it is read from that Secret's github_app_id")
        if not _text(org, "image"):
            errors.append(f"{org_name}: image is required")
        # Presence only: reading the value would decrypt an ansible-vault string for no reason.
        if org.get("private_key") is not None and org.get("private_key_op_reference") is not None:
            errors.append(f"{org_name}: set private_key or private_key_op_reference, not both (the 1Password adapter fills private_key from the reference)")
        if org_name.lower() in seen_orgs:
            errors.append(f"Org '{org_name}' is configured more than once in github_runner_arc_orgs across the inventory (also as '{seen_orgs[org_name.lower()]}'). Each org's Helm releases must be installed from exactly one host; remove the duplicate.")
            continue
        seen_orgs[org_name.lower()] = org_name
        org_profiles = org.get("scale_set_profiles")
        if not isinstance(org_profiles, Sequence) or isinstance(org_profiles, (str, bytes)) or not org_profiles:
            errors.append(f"{org_name}: scale_set_profiles must be a non-empty list")
            continue
        app_secret = _optional_name(org, "app_secret_name", org_name, errors) or f"{org_name.lower()}-github-app"
        for profile_index, profile in enumerate(org_profiles):
            expanded = _expand_profile(org_name, app_secret, profile_index, profile, errors)
            if expanded is not None:
                profiles.append(expanded)
    namespaces: dict[str, str] = {}
    releases: dict[tuple[str, str], str] = {}
    for profile in profiles:
        if profile["namespace"] in namespaces:
            errors.append(f"{profile['label']} and {namespaces[profile['namespace']]} both map to namespace '{profile['namespace']}'; give one of them a different suffix or namespace")
        namespaces.setdefault(profile["namespace"], profile["label"])
        # The release name is also the scale set's registration name on GitHub, so two of an org's releases sharing it would register a duplicate even from different namespaces.
        release_key = (profile["org_name"].lower(), profile["release"])
        if release_key in releases:
            errors.append(f"{profile['label']} and {releases[release_key]} both use release name '{profile['release']}', which is the scale set's name on GitHub; give one of them a different suffix or release_name")
        releases.setdefault(release_key, profile["label"])
    # Several autoscaled profiles are allowed - the autoscaler pools their combined scale sets against one shared memory budget - but each one's own sizing derives that budget's ceiling, so more than one sizing among them would leave the ceiling ambiguous.
    autoscaled = [profile for profile in profiles if profile["autoscale"]]
    sized_autoscaled = [profile for profile in autoscaled if profile["sizing"] is not None]
    if len(sized_autoscaled) > 1:
        errors.append(f"At most one autoscaled scale-set profile may set sizing, because it derives the shared pool's ceiling; found {', '.join(profile['label'] for profile in sized_autoscaled)}")
    return {"errors": errors, "profiles": profiles, "autoscaled": autoscaled}


def _pinned_image_errors(where: str, image: Any) -> list[str]:
    """Return what is wrong with a capability's image reference: it must be a non-empty reference pinned to a version tag or a digest."""
    if not isinstance(image, str) or not image:
        return [f"{where} must be a non-empty image reference"]
    if arc_image_ref(image)["reference"] == "latest":
        return [f"{where} must be pinned to a version tag or a digest, not latest or no tag (got {image!r}), so every runner pod gets the same tools"]
    return []


def _env_errors(where: str, env: Any) -> list[str]:
    """Return what is wrong with a system-library capability's env: a mapping of variable names to single-line strings, none of them a variable the role sets itself."""
    if not isinstance(env, Mapping):
        return [f"{where} must be a mapping of variable name to value"]
    errors: list[str] = []
    for name, value in env.items():
        if not isinstance(name, str) or not _ENV_NAME.match(name):
            errors.append(f"{where}: {name!r} is not a variable name (letters, digits and '_', not starting with a digit)")
        elif name in _RESERVED_ENV:
            errors.append(f"{where}.{name} is set by the role itself, so a capability may not set it")
        # A string, so a YAML number or boolean is quoted rather than turned into text it was not written as; one line, because the job-started hook writes NAME=value lines to GITHUB_ENV.
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            errors.append(f"{where}.{name} must be a single-line string (quote a number or a boolean)")
    return errors


def _capability_entry_errors(name: Any, capability: Any) -> list[str]:
    """Return what is wrong with one entry of the capability catalogue."""
    where = f"github_runner_arc_capabilities.{name}"
    if not isinstance(name, str) or len(name) > _CAPABILITY_NAME_MAX_LENGTH or not _DNS_LABEL.match(name):
        return [f"{where}: a capability name must be lower-case letters, digits and '-', at most {_CAPABILITY_NAME_MAX_LENGTH} characters, since its init container is named {_CAPABILITY_CONTAINER_PREFIX}<name>"]
    if not isinstance(capability, Mapping):
        return [f"{where} must be a mapping"]
    errors = [f"{where} has an unknown key '{key}'" for key in sorted(set(capability) - _CAPABILITY_KEYS)]
    image = capability.get("image")
    sysroot_image = capability.get("sysroot_image")
    hook = capability.get("job_started_hook")
    if sum(capability.get(kind) is not None for kind in _CAPABILITY_KINDS) != 1:
        errors.append(f"{where} must set exactly one of image (a tool-cache capability), sysroot_image (a system-library capability) and job_started_hook (a hook capability)")
    if image is not None:
        errors.extend(_pinned_image_errors(f"{where}.image", image))
    if sysroot_image is not None:
        errors.extend(_pinned_image_errors(f"{where}.sysroot_image", sysroot_image))
    if hook is not None and (not isinstance(hook, str) or not hook.strip()):
        errors.append(f"{where}.job_started_hook must be a non-empty script")
    path = capability.get("path")
    if path is not None:
        if image is None:
            errors.append(f"{where}.path is only for a tool-cache capability, one with an image")
        components = path.split("/") if isinstance(path, str) else []
        if len(components) < 2 or not all(_PATH_COMPONENT.match(component) for component in components):
            errors.append(f"{where}.path must be <tool>/<version>, optionally followed by /<subdirectory>, each part letters, digits and '._+-' (got {path!r})")
    env = capability.get("env")
    if env is not None:
        if sysroot_image is None:
            errors.append(f"{where}.env is only for a system-library capability, one with a sysroot_image")
        errors.extend(_env_errors(f"{where}.env", env))
    return errors


def _sysroot_env_conflicts(label: str, names: Sequence[str], catalogue: Mapping[str, Any]) -> list[str]:
    """Return the variables that two of a profile's system-library capabilities set to different values, since a sysroot has one environment."""
    first: dict[str, tuple[str, Any]] = {}
    errors: list[str] = []
    for name in names:
        capability = catalogue.get(name)
        env = capability.get("env") if isinstance(capability, Mapping) else None
        if not isinstance(env, Mapping):
            continue
        for variable, value in env.items():
            if variable in first and first[variable][1] != value:
                errors.append(f"{label}: capabilities '{first[variable][0]}' and '{name}' set {variable} to different values")
            first.setdefault(variable, (name, value))
    return errors


def arc_capability_errors(profiles: Sequence[Mapping[str, Any]], catalogue: Any) -> list[str]:
    """Check the capability catalogue, and that every capability a profile names is in it.

    Args:
        profiles: expanded profiles from arc_profiles. catalogue: github_runner_arc_capabilities, a mapping of capability name to one of {image, path?} (a tool-cache capability: a pinned image holding a payload in the tool-cache layout under /toolcache, and optionally a <tool>/<version>[/<subdirectory>] to put on PATH at job start), {sysroot_image, env?} (a system-library capability: a pinned image holding a relocatable prefix under /sysroot, and optionally extra variables its libraries need, such as a relocation variable) or {job_started_hook} (a hook capability: a script the runner runs before each job).

    Returns:
        The problems found, empty when there are none: invalid entries, a profile naming an undefined capability, and two of a profile's system-library capabilities setting one variable to different values.
    """
    if not isinstance(catalogue, Mapping):
        return ["github_runner_arc_capabilities must be a mapping of capability name to capability"]
    errors = [error for name, capability in catalogue.items() for error in _capability_entry_errors(name, capability)]
    for profile in profiles:
        errors.extend(f"{profile['label']}: capability '{name}' is not defined in github_runner_arc_capabilities" for name in profile["capabilities"] if name not in catalogue)
        errors.extend(_sysroot_env_conflicts(profile["label"], profile["capabilities"], catalogue))
    return errors


def _runner_container(spec: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the pod template's container named runner, which the chart builds the runner from, or None when the values define none."""
    for container in spec.get("containers") or []:
        if isinstance(container, dict) and container.get("name") == "runner":
            return container
    return None


def _sysroot_path_errors(sysroot_path: str, others: Mapping[str, str]) -> list[str]:
    """Return what is wrong with the sysroot's mount path: it must be an absolute path that can sit in a colon-separated search path, apart from the other directories the capabilities mount."""
    if not isinstance(sysroot_path, str) or not sysroot_path.startswith("/") or sysroot_path == "/" or any(character in sysroot_path for character in ":\n\r"):
        return [f"the sysroot path must be an absolute directory with no ':' or line break, since it is joined into search paths such as LD_LIBRARY_PATH (got {sysroot_path!r})"]
    root = sysroot_path.rstrip("/")
    errors: list[str] = []
    for what, other in others.items():
        other_root = str(other).rstrip("/")
        if root == other_root or root.startswith(f"{other_root}/") or other_root.startswith(f"{root}/"):
            errors.append(f"the sysroot path {sysroot_path} overlaps the {what} {other}; each needs a directory of its own")
    return errors


def _sysroot_env(sysroot_path: str, capabilities: Sequence[tuple[str, Mapping[str, Any]]]) -> str:
    """Return the job-started hook's sysroot-env: a NAME+=directory line for each search path the sysroot extends (the hook prepends it to the variable, and PATH goes through GITHUB_PATH), then a NAME=value line for each variable the system-library capabilities set, in the profile's order."""
    root = sysroot_path.rstrip("/")
    lines = [f"{name}+={root}/{directory}".rstrip("/") for name, directory in _SYSROOT_SEARCH_PATHS]
    seen: set[str] = set()
    for _, capability in capabilities:
        for name, value in (capability.get("env") or {}).items():
            if name not in seen:
                seen.add(name)
                lines.append(f"{name}={value}")
    return "".join(f"{line}\n" for line in lines)


def _copy_init_container(name: str, image: str, payload: str, volume: str, mount_path: str, runner_user: int, runner_group: int) -> dict[str, Any]:
    """Return the init container that copies a capability image's payload directory into a volume, running as the runner's user."""
    return {
        "name": f"{_CAPABILITY_CONTAINER_PREFIX}{name}",
        "image": image,
        # The image contract: a POSIX sh and cp in the image, and the payload under the given directory. cp -R without -p, so the copies belong to the runner user rather than keeping the image's owner; cp -R keeps symbolic links as links, which a library's versioned names rely on.
        "command": ["/bin/sh", "-c", f'cp -R {payload}/. "$1"/', f"copy-{volume}", mount_path],
        "securityContext": {"runAsUser": int(runner_user), "runAsGroup": int(runner_group), "runAsNonRoot": True, "allowPrivilegeEscalation": False},
        "volumeMounts": [{"name": volume, "mountPath": mount_path}],
    }


def arc_capability_pod(values: Mapping[str, Any], names: Sequence[str], catalogue: Mapping[str, Any], tool_cache_path: str, sysroot_path: str, runner_user: int, runner_group: int, hooks_dir: str, hooks_configmap: str) -> dict[str, Any]:
    """Add a profile's capabilities to its scale set's Helm values.

    Each tool-cache capability becomes an init container, running its image as the runner's user, that copies the image's /toolcache payload into an emptyDir mounted on the runner container at tool_cache_path, which RUNNER_TOOL_CACHE names; the files then belong to the runner user, so a setup action can still add a version beside them. Each system-library capability likewise becomes an init container that copies the image's /sysroot payload into an emptyDir mounted at sysroot_path; several share it, later ones overwriting a file an earlier one also wrote. Hook capabilities, the path of any tool-cache capability that sets one, and the sysroot's environment (its search paths and the capabilities' env) go into a ConfigMap mounted at hooks_dir, whose dispatcher ACTIONS_RUNNER_HOOK_JOB_STARTED names. Everything is appended after what the values already hold, so a values file's own init containers, volumes, environment and mounts are kept. Lists are replaced wholesale when Helm merges values, which is why this edits the merged values rather than adding an overlay.

    Args:
        values: the release's merged Helm values (the profile's values with the role's overlay over them). names: the profile's capabilities, in order; the init containers and hook scripts run in this order. catalogue: github_runner_arc_capabilities, already checked by arc_capability_errors. tool_cache_path: where the tool cache is mounted on the runner container. sysroot_path: where the sysroot is mounted on the runner container. runner_user, runner_group: the runner container's user and group ids, which the init containers run as. hooks_dir: where the job-started ConfigMap is mounted on the runner container. hooks_configmap: the job-started ConfigMap's name, in the profile's namespace.

    Returns:
        A dict with ``errors`` (empty when the values can carry the capabilities), ``values`` (the values with the capabilities added, or as given when there are none or errors) and ``hooks`` (the ConfigMap's data apart from the dispatcher, which the role adds from its own file; empty when the profile needs no ConfigMap).
    """
    if not names:
        return {"errors": [], "values": values, "hooks": {}}
    result = copy.deepcopy(dict(values))
    errors: list[str] = []
    mode = (result.get("containerMode") or {}).get("type") or ""
    if mode in _SEPARATE_POD_CONTAINER_MODES:
        errors.append(f"capabilities need the job's steps to run in the runner container, and containerMode {mode} runs them in a separate pod")
    spec = result.setdefault("template", {}).setdefault("spec", {})
    runner = _runner_container(spec)
    if runner is None:
        errors.append("capabilities need the values to define the runner container (template.spec.containers[] named runner), as the role's own values template does")
    tool_capabilities = [(name, catalogue[name]) for name in names if catalogue[name].get("image") is not None]
    sysroot_capabilities = [(name, catalogue[name]) for name in names if catalogue[name].get("sysroot_image") is not None]
    hook_capabilities = [(name, catalogue[name]) for name in names if catalogue[name].get("job_started_hook") is not None]
    if sysroot_capabilities:
        errors.extend(_sysroot_path_errors(sysroot_path, {"tool cache path": tool_cache_path, "job-started hooks directory": hooks_dir}))
    tool_paths = [capability["path"] for _, capability in tool_capabilities if capability.get("path") is not None]
    hooks: dict[str, str] = {}
    if tool_paths:
        hooks[_HOOK_TOOL_PATHS] = "".join(f"{path}\n" for path in tool_paths)
    if sysroot_capabilities:
        hooks[_HOOK_SYSROOT_ENV] = _sysroot_env(sysroot_path, sysroot_capabilities)
    # Numbered in the profile's order so the dispatcher, which runs them in name order, runs them in that order.
    width = len(str(len(hook_capabilities)))
    scripts = {f"{index:0{width}d}-{name}.sh": capability["job_started_hook"] for index, (name, capability) in enumerate(hook_capabilities, start=1)}

    volumes: list[dict[str, Any]] = []
    mounts: list[dict[str, Any]] = []
    env: list[dict[str, Any]] = []
    if tool_capabilities:
        volumes.append({"name": _TOOL_CACHE_VOLUME, "emptyDir": {}})
        mounts.append({"name": _TOOL_CACHE_VOLUME, "mountPath": tool_cache_path})
        env.append({"name": _TOOL_CACHE_ENV, "value": tool_cache_path})
    if sysroot_capabilities:
        volumes.append({"name": _SYSROOT_VOLUME, "emptyDir": {}})
        mounts.append({"name": _SYSROOT_VOLUME, "mountPath": sysroot_path})
    if hooks or scripts:
        items = [{"key": _HOOK_DISPATCHER, "path": _HOOK_DISPATCHER}] + [{"key": key, "path": key} for key in hooks] + [{"key": key, "path": f"{_HOOK_SCRIPTS_DIR}/{key}"} for key in scripts]
        volumes.append({"name": _HOOKS_VOLUME, "configMap": {"name": hooks_configmap, "defaultMode": _HOOK_FILE_MODE, "items": items}})
        mounts.append({"name": _HOOKS_VOLUME, "mountPath": hooks_dir, "readOnly": True})
        env.append({"name": _JOB_STARTED_HOOK_ENV, "value": f"{hooks_dir}/{_HOOK_DISPATCHER}"})
    hooks.update(scripts)

    existing_volumes = {volume.get("name") for volume in spec.get("volumes") or []}
    errors.extend(f"the values already define a volume named {volume['name']}, which capabilities add" for volume in volumes if volume["name"] in existing_volumes)
    if runner is not None:
        existing_env = {variable.get("name") for variable in runner.get("env") or []}
        errors.extend(f"the runner container already sets {variable['name']}, which capabilities set" for variable in env if variable["name"] in existing_env)
    if errors:
        return {"errors": errors, "values": values, "hooks": {}}
    assert runner is not None
    # In the profile's order, so a later system-library capability's files overwrite an earlier one's.
    init_containers = [
        _copy_init_container(name, catalogue[name]["image"], _TOOL_IMAGE_PAYLOAD, _TOOL_CACHE_VOLUME, tool_cache_path, runner_user, runner_group)
        if catalogue[name].get("image") is not None
        else _copy_init_container(name, catalogue[name]["sysroot_image"], _SYSROOT_IMAGE_PAYLOAD, _SYSROOT_VOLUME, sysroot_path, runner_user, runner_group)
        for name in names
        if catalogue[name].get("job_started_hook") is None
    ]
    if init_containers:
        spec["initContainers"] = list(spec.get("initContainers") or []) + init_containers
    spec["volumes"] = list(spec.get("volumes") or []) + volumes
    runner["env"] = list(runner.get("env") or []) + env
    runner["volumeMounts"] = list(runner.get("volumeMounts") or []) + mounts
    return {"errors": [], "values": result, "hooks": hooks}


# A registry cache entry's keys (see arc_registry_cache), and those of its credentials reference.
_REGISTRY_CACHE_KEYS = frozenset({"name", "host", "url", "credentials"})
_REGISTRY_CREDENTIALS_KEYS = frozenset({"secret_name", "username_key", "password_key"})
# The keys a credentials reference reads from its Secret unless it names others: the field names a kubernetes.io/basic-auth Secret uses.
_DEFAULT_USERNAME_KEY = "username"
_DEFAULT_PASSWORD_KEY = "password"
# An entry's name becomes its cache container's name and its Service port's name, and a Service port name is an IANA service name: at most 15 lower-case letters, digits and '-', with at least one letter and no '-' at either end or doubled.
_PORT_NAME = re.compile(r"^(?=.*[a-z])[a-z0-9]([a-z0-9]|-(?!-))*[a-z0-9]$|^[a-z]$")
_PORT_NAME_MAX_LENGTH = 15
# A registry host as image references name it: a DNS name with an optional port, which is also the directory dockerd reads its hosts.toml from and the key of buildkitd's registry table.
_REGISTRY_HOST = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*(:[0-9]{1,5})?$")
# The upstream a cache container pulls from: an http or https URL with a host and an optional port, and nothing after it, since it is also the server dockerd falls back to.
_REGISTRY_URL = re.compile(r"^https?://[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*(:[0-9]{1,5})?$")
# Image references name Docker Hub docker.io, while its registry API is served from registry-1.docker.io; every other registry serves its API from the host its references name.
_DOCKER_HUB_HOST = "docker.io"
_DOCKER_HUB_URL = "https://registry-1.docker.io"
# The highest TCP port number; each entry's port is the base port plus its index, so the last must not pass it.
_MAX_PORT = 65535
# The chart's Docker-in-Docker wiring (gha-runner-scale-set templates/_helpers.tpl: dind-init-container, dind-container, dind-runner-container and dind-volume), written out so that the dind container can carry the registry cache's configuration, which the chart's own dind container has no way to add. These are the chart's own names, paths and values for Kubernetes 1.29 and later, where it runs dind as a native sidecar.
_DIND_IMAGE = "docker:dind"
_DIND_SOCKET_DIR = "/var/run"
_DIND_DOCKER_HOST = "unix:///var/run/docker.sock"
_DIND_DOCKER_GROUP_GID = "123"
_DIND_RUNNER_WAIT_SECONDS = "120"
_RUNNER_WORK_DIR = "/home/runner/_work"
_RUNNER_EXTERNALS_DIR = "/home/runner/externals"
_DIND_EXTERNALS_COPY_DIR = "/home/runner/tmpDir"
_DIND_STARTUP_PROBE = {"exec": {"command": ["docker", "info"]}, "initialDelaySeconds": 0, "failureThreshold": 24, "periodSeconds": 5}
_DIND_CONTAINER = "dind"
# Where dockerd reads each registry's hosts.toml, one directory per registry host (moby daemon/hosts.go RegistryHosts, through containerd's ConfigureHosts).
_DOCKER_CERTS_DIR = "/etc/docker/certs.d"
# The volumes the registry cache adds to a runner pod, and buildx's own name for its default BuildKit configuration file in its configuration directory (buildx util/confutil/config.go).
_REGISTRY_CACHE_HOSTS_VOLUME = "registry-cache-hosts"
_REGISTRY_CACHE_BUILDX_VOLUME = "registry-cache-buildx"
_REGISTRY_CACHE_BUILDKITD_VOLUME = "registry-cache-buildkitd"
_BUILDKITD_DEFAULT_CONFIG = "buildkitd.default.toml"
_BUILDX_CONFIG_ENV = "BUILDX_CONFIG"
# The registry cache ConfigMap holds the BuildKit configuration under buildx's file name, and each registry's hosts.toml under its entry's name with this suffix.
_REGISTRY_CACHE_HOSTS_KEY_SUFFIX = ".hosts.toml"


def _registry_credentials(where: str, credentials: Any, errors: list[str]) -> dict[str, str] | None:
    """Return a registry entry's credentials reference with its keys filled in, or None when it has none, recording why it cannot be used."""
    if credentials is None:
        return None
    if not isinstance(credentials, Mapping):
        errors.append(f"{where}.credentials must be a mapping naming a Secret: {{secret_name, username_key?, password_key?}}")
        return None
    unknown = sorted(str(key) for key in set(credentials) - _REGISTRY_CREDENTIALS_KEYS)
    if unknown:
        # Named plainly, since a username or password written here would otherwise sit in the inventory and the rendered manifests.
        errors.append(f"{where}.credentials has unknown key(s) {', '.join(unknown)}: credentials are only read from a Secret in the cache's namespace, by secret_name, username_key and password_key, never given inline")
    reference = {
        "secret_name": credentials.get("secret_name"),
        "username_key": credentials.get("username_key", _DEFAULT_USERNAME_KEY),
        "password_key": credentials.get("password_key", _DEFAULT_PASSWORD_KEY),
    }
    if not isinstance(reference["secret_name"], str) or len(reference["secret_name"]) > 253 or not re.match(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$", reference["secret_name"]):
        errors.append(f"{where}.credentials.secret_name must be the name of a Secret (lower-case letters, digits, '-' and '.')")
    for key in ("username_key", "password_key"):
        if not isinstance(reference[key], str) or not re.match(r"^[-._a-zA-Z0-9]+$", reference[key]):
            errors.append(f"{where}.credentials.{key} must be a Secret data key (letters, digits, '-', '_' and '.')")
    return {key: str(value) for key, value in reference.items()}


def arc_registry_cache(registries: Any, namespace: str, service: str, cluster_domain: str, base_port: int) -> dict[str, Any]:
    """Describe the registry cache: one pull-through cache per upstream registry behind one Service, and the configuration that points a runner pod's Docker daemon and BuildKit at it.

    Each entry becomes a container of the cache's Deployment, serving the registry on its own port (base_port plus the entry's index) of the Service. A runner pod's dockerd reads a hosts.toml per registry from /etc/docker/certs.d/<host>/ that lists the cache as a pull, resolve and referrers mirror ahead of the registry itself, and a docker-container BuildKit builder that buildx creates reads the same mirrors from buildx's default buildkitd.default.toml. Both resolvers try the mirror first and the registry itself when the mirror cannot be reached or answers with an error, so a cache that is down or unhealthy slows a pull rather than failing it.

    Args:
        registries: github_runner_arc_registry_cache_registries, a list of {name, host, url?, credentials?}: name (the container and port name, at most 15 characters), host (the registry as image references name it, docker.io for Docker Hub), url (the registry API the cache pulls from; default https://<host>, or https://registry-1.docker.io for docker.io) and credentials ({secret_name, username_key?, password_key?}, a Secret in the cache's namespace whose keys hold a read-only username and token; default keys username and password). namespace: the cache's namespace. service: the cache's Service name. cluster_domain: the cluster's DNS domain. base_port: the first entry's port.

    Returns:
        A dict with ``errors`` (empty when the registries are valid), ``registries`` (each entry with its url, port, address (host:port of the Service as runner pods reach it) and credentials filled in, or None for credentials when it has none), ``hosts`` (a mapping of <name>.hosts.toml, each registry's key in the runner pods' ConfigMap, to its hosts.toml) and ``buildkitd`` (the BuildKit configuration).
    """
    errors: list[str] = []
    if not isinstance(registries, Sequence) or isinstance(registries, (str, bytes)) or not registries:
        return {"errors": ["github_runner_arc_registry_cache_registries must be a non-empty list of registries"], "registries": [], "hosts": {}, "buildkitd": ""}
    address = f"{service}.{namespace}.svc.{cluster_domain}"
    described: list[dict[str, Any]] = []
    names: set[str] = set()
    hosts: set[str] = set()
    for index, entry in enumerate(registries):
        where = f"github_runner_arc_registry_cache_registries[{index}]"
        if not isinstance(entry, Mapping):
            errors.append(f"{where} must be a mapping")
            continue
        errors.extend(f"{where} has an unknown key '{key}'" for key in sorted(str(key) for key in set(entry) - _REGISTRY_CACHE_KEYS))
        name = entry.get("name")
        if not isinstance(name, str) or len(name) > _PORT_NAME_MAX_LENGTH or not _PORT_NAME.match(name):
            errors.append(f"{where}.name must be at most {_PORT_NAME_MAX_LENGTH} lower-case letters, digits and single '-', with at least one letter, since it names the entry's container and Service port (got {name!r})")
        elif name in names:
            errors.append(f"{where}.name {name} is used by another registry")
        host = entry.get("host")
        if not isinstance(host, str) or not _REGISTRY_HOST.match(host):
            errors.append(f"{where}.host must be a registry host as image references name it, such as docker.io or ghcr.io, with an optional port (got {host!r})")
        elif host in hosts:
            errors.append(f"{where}.host {host} is cached by another entry")
        default_url = _DOCKER_HUB_URL if host == _DOCKER_HUB_HOST else f"https://{host}"
        url = entry.get("url", default_url)
        if not isinstance(url, str) or not _REGISTRY_URL.match(url):
            errors.append(f"{where}.url must be the registry's http or https base URL with no path, such as https://ghcr.io (got {url!r})")
        credentials = _registry_credentials(where, entry.get("credentials"), errors)
        port = int(base_port) + index
        if port > _MAX_PORT:
            errors.append(f"{where}: its port {port} (github_runner_arc_registry_cache_base_port plus its index) is above {_MAX_PORT}")
        names.add(str(name))
        hosts.add(str(host))
        described.append({"name": name, "host": host, "url": url, "port": port, "address": f"{address}:{port}", "credentials": credentials})
    if errors:
        return {"errors": errors, "registries": [], "hosts": {}, "buildkitd": ""}
    # Keyed by the entry's name rather than its host, since a ConfigMap key cannot hold the ':' of a host with a port. The mirror also answers referrers lookups, which dockerd makes on every pull (containerd remotes/docker/referrers.go): without the capability they go to the registry alone, which for a registry that needs a login fails the pull, since the job has none.
    hosts_toml = {
        f"{registry['name']}{_REGISTRY_CACHE_HOSTS_KEY_SUFFIX}": (
            f"server = {_toml_string(registry['url'])}\n\n[host.{_toml_string('http://' + registry['address'])}]\n  capabilities = [\"pull\", \"resolve\", \"referrers\"]\n"
        )
        for registry in described
    }
    buildkitd = "".join(
        f"[registry.{_toml_string(registry['host'])}]\n  mirrors = [{_toml_string(registry['address'])}]\n\n[registry.{_toml_string(registry['address'])}]\n  http = true\n\n" for registry in described
    )
    return {"errors": [], "registries": described, "hosts": hosts_toml, "buildkitd": buildkitd.rstrip("\n") + "\n"}


def _toml_string(value: str) -> str:
    """Return value as a TOML basic string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _named(items: Sequence[Any], name: str) -> dict[str, Any] | None:
    """Return the item in a list of named Kubernetes objects (containers, volumes, variables, mounts) that carries name, or None."""
    for item in items:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def _append_missing(items: list[dict[str, Any]], additions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return items with each addition appended whose name none of them already carries, as the chart does for the dind runner's own variables and mounts."""
    present = {item.get("name") for item in items if isinstance(item, dict)}
    return items + [addition for addition in additions if addition["name"] not in present]


def arc_registry_cache_pod(values: Mapping[str, Any], cache: Mapping[str, Any], runner_image: str, configmap: str, buildx_config_dir: str) -> dict[str, Any]:
    """Point a scale set's Docker daemon, and the BuildKit builders its jobs create, at the registry cache.

    A profile in the chart's Docker-in-Docker mode (containerMode dind) has its dind wiring written out in the values, as the chart itself renders it on Kubernetes 1.29 and later, because only then can the dind container mount the registries' hosts.toml files; containerMode is removed so the chart renders the pod from these values alone. Values that already write out a container named dind get the mount added to it. The runner container gets BUILDX_CONFIG, a writable directory holding buildkitd.default.toml, which buildx reads when it creates a docker-container builder without a configuration of its own, as docker/setup-buildx-action does by default. Values with no dind container, which run no Docker daemon, are returned unchanged.

    Args:
        values: the release's rendered Helm values (after the capabilities are added). cache: the result of arc_registry_cache. runner_image: the runner image, which the chart's dind mode copies the runner's externals from. configmap: the name of the ConfigMap in the profile's namespace holding the hosts.toml files and the BuildKit configuration. buildx_config_dir: where BUILDX_CONFIG points on the runner container.

    Returns:
        A dict with ``errors`` (empty when the values can carry the cache's configuration), ``values`` (with it added, or as given when there is no dind or there are errors) and ``wired`` (whether the values now use the cache, so the profile's namespace needs the ConfigMap).
    """
    result = copy.deepcopy(dict(values))
    spec = result.setdefault("template", {}).setdefault("spec", {})
    mode = (result.get("containerMode") or {}).get("type") or ""
    containers: list[dict[str, Any]] = list(spec.get("containers") or [])
    init_containers: list[dict[str, Any]] = list(spec.get("initContainers") or [])
    dind = _named(containers, _DIND_CONTAINER) or _named(init_containers, _DIND_CONTAINER)
    if mode != "dind" and dind is None:
        return {"errors": [], "values": values, "wired": False}
    errors: list[str] = []
    runner = _runner_container(spec)
    if runner is None:
        errors.append("the registry cache needs the values to define the runner container (template.spec.containers[] named runner), as the role's own values template does")
    volumes: list[dict[str, Any]] = list(spec.get("volumes") or [])
    added_volumes = (_REGISTRY_CACHE_HOSTS_VOLUME, _REGISTRY_CACHE_BUILDX_VOLUME, _REGISTRY_CACHE_BUILDKITD_VOLUME)
    errors.extend(f"the values already define a volume named {name}, which the registry cache adds" for name in added_volumes if _named(volumes, name) is not None)
    if runner is not None and _named(list(runner.get("env") or []), _BUILDX_CONFIG_ENV) is not None:
        errors.append(f"the runner container already sets {_BUILDX_CONFIG_ENV}, which the registry cache sets")
    if mode == "dind" and dind is not None:
        errors.append("containerMode dind and a container named dind in the values cannot be combined; the chart would render two")
    if errors:
        return {"errors": errors, "values": values, "wired": False}
    assert runner is not None

    hosts_mount = {"name": _REGISTRY_CACHE_HOSTS_VOLUME, "mountPath": _DOCKER_CERTS_DIR, "readOnly": True}
    if mode == "dind":
        del result["containerMode"]
        dind = {
            "name": _DIND_CONTAINER,
            "image": _DIND_IMAGE,
            "args": ["dockerd", f"--host={_DIND_DOCKER_HOST}", "--group=$(DOCKER_GROUP_GID)"],
            "env": [{"name": "DOCKER_GROUP_GID", "value": _DIND_DOCKER_GROUP_GID}],
            "securityContext": {"privileged": True},
            "restartPolicy": "Always",
            "startupProbe": copy.deepcopy(_DIND_STARTUP_PROBE),
            "volumeMounts": [
                {"name": "work", "mountPath": _RUNNER_WORK_DIR},
                {"name": "dind-sock", "mountPath": _DIND_SOCKET_DIR},
                {"name": "dind-externals", "mountPath": _RUNNER_EXTERNALS_DIR},
            ],
        }
        externals = {
            "name": "init-dind-externals",
            "image": runner_image,
            "command": ["cp"],
            "args": ["-r", f"{_RUNNER_EXTERNALS_DIR}/.", f"{_DIND_EXTERNALS_COPY_DIR}/"],
            "volumeMounts": [{"name": "dind-externals", "mountPath": _DIND_EXTERNALS_COPY_DIR}],
        }
        # Ahead of the values' own init containers, as the chart puts them, so the Docker daemon is up before any of them runs.
        init_containers = [externals, dind] + init_containers
        runner["env"] = _append_missing(list(runner.get("env") or []), [{"name": "DOCKER_HOST", "value": _DIND_DOCKER_HOST}, {"name": "RUNNER_WAIT_FOR_DOCKER_IN_SECONDS", "value": _DIND_RUNNER_WAIT_SECONDS}])
        runner["volumeMounts"] = _append_missing(list(runner.get("volumeMounts") or []), [{"name": "work", "mountPath": _RUNNER_WORK_DIR}, {"name": "dind-sock", "mountPath": _DIND_SOCKET_DIR}])
        volumes = _append_missing(volumes, [{"name": "dind-sock", "emptyDir": {}}, {"name": "dind-externals", "emptyDir": {}}, {"name": "work", "emptyDir": {}}])
    assert dind is not None
    dind["volumeMounts"] = list(dind.get("volumeMounts") or []) + [hosts_mount]
    hosts_items = [{"key": f"{registry['name']}{_REGISTRY_CACHE_HOSTS_KEY_SUFFIX}", "path": f"{registry['host']}/hosts.toml"} for registry in cache["registries"]]
    volumes += [
        {"name": _REGISTRY_CACHE_HOSTS_VOLUME, "configMap": {"name": configmap, "items": hosts_items}},
        # buildx writes its builder records beside its configuration, so the directory is a writable emptyDir with the read-only configuration file mounted into it.
        {"name": _REGISTRY_CACHE_BUILDX_VOLUME, "emptyDir": {}},
        {"name": _REGISTRY_CACHE_BUILDKITD_VOLUME, "configMap": {"name": configmap, "items": [{"key": _BUILDKITD_DEFAULT_CONFIG, "path": _BUILDKITD_DEFAULT_CONFIG}]}},
    ]
    runner["env"] = list(runner.get("env") or []) + [{"name": _BUILDX_CONFIG_ENV, "value": buildx_config_dir}]
    runner["volumeMounts"] = list(runner.get("volumeMounts") or []) + [
        {"name": _REGISTRY_CACHE_BUILDX_VOLUME, "mountPath": buildx_config_dir},
        {"name": _REGISTRY_CACHE_BUILDKITD_VOLUME, "mountPath": f"{buildx_config_dir.rstrip('/')}/{_BUILDKITD_DEFAULT_CONFIG}", "subPath": _BUILDKITD_DEFAULT_CONFIG, "readOnly": True},
    ]
    spec["containers"] = containers
    if init_containers:
        spec["initContainers"] = init_containers
    spec["volumes"] = volumes
    return {"errors": [], "values": result, "wired": True}


def arc_registry_cache_secret_problems(results: Sequence[Mapping[str, Any]]) -> list[str]:
    """Say which registry credential Secrets are missing, or lack a key their reference names, without repeating any of their data.

    Args:
        results: the results of a kubernetes.core.k8s_info loop over the cache's registries that have credentials, each with ``item`` (the registry, as arc_registry_cache describes it) and ``resources`` (the Secret, or nothing when it does not exist).

    Returns:
        One message per registry whose Secret is missing or lacks its username or password key; empty when every one is in place.
    """
    problems: list[str] = []
    for result in results:
        registry = result["item"]
        credentials = registry["credentials"]
        resources = result.get("resources") or []
        if not resources:
            problems.append(f"{registry['name']}: Secret {credentials['secret_name']} does not exist")
            continue
        keys = set((resources[0].get("data") or {}).keys())
        missing = [credentials[key] for key in ("username_key", "password_key") if credentials[key] not in keys]
        if missing:
            problems.append(f"{registry['name']}: Secret {credentials['secret_name']} has no key {' or '.join(missing)}")
    return problems


def arc_registry_cache_configmap(cache: Mapping[str, Any]) -> dict[str, str]:
    """Return the data of the ConfigMap a wired runner pod mounts: each registry's hosts.toml under its own key, and the BuildKit configuration."""
    return {**cache["hosts"], _BUILDKITD_DEFAULT_CONFIG: cache["buildkitd"]}


def arc_image_ref(image: str) -> dict[str, str]:
    """Split a container image reference into registry, repository and tag or digest.

    Follows the Docker reference rules: the first path component is a registry only when it contains '.' or ':' or is 'localhost'; otherwise the image is on Docker Hub, where a single-component name lives under library/. A missing tag means latest.

    Args:
        image: a reference such as ghcr.io/example/runner:1.2 or ghcr.io/example/runner@sha256:...

    Returns:
        A dict with registry, repository and reference (the tag or the digest).
    """
    remainder = str(image)
    if "@" in remainder:
        remainder, reference = remainder.split("@", 1)
    else:
        reference = ""
    first, _, rest = remainder.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, rest
    else:
        registry, path = "docker.io", remainder
    if not reference:
        last_slash = path.rfind("/")
        colon = path.rfind(":")
        if colon > last_slash:
            path, reference = path[:colon], path[colon + 1 :]
        else:
            reference = "latest"
    if registry == "docker.io" and "/" not in path:
        path = f"library/{path}"
    return {"registry": registry, "repository": path, "reference": reference}


def arc_upgrade_blocker(deployed_chart: str, desired_version: str, running_runners: int) -> str:
    """Explain why a controller chart change must wait, or return an empty string when it may proceed.

    ARC's controller deletes every scale set whose version differs from its own, together with its listener and ephemeral runners, until Helm upgrades that scale set. A running job is killed with its runner, so a controller version change belongs in a window with no running runners.

    Args:
        deployed_chart: the installed controller release's chart as Helm reports it, name and version joined by a dash (for example gha-runner-scale-set-controller-0.14.2); empty when there is no release yet.
        desired_version: the controller chart version about to be installed; empty when it is not pinned, in which case the target is unknown and nothing is blocked.
        running_runners: how many ephemeral runners exist across the cluster.

    Returns:
        A message when the version changes while runners are running, otherwise an empty string.
    """
    deployed = str(deployed_chart).rsplit("-", 1)[-1] if deployed_chart else ""
    if not deployed or not desired_version or deployed == str(desired_version) or int(running_runners) == 0:
        return ""
    return (
        f"The ARC controller is at chart {deployed} and this run installs {desired_version}, which deletes every scale set at the old version "
        f"and the {int(running_runners)} running ephemeral runner(s) with it until the scale sets are upgraded. Run it when no runner is running, "
        "or set github_runner_arc_allow_busy_controller_upgrade to accept killing the jobs on them."
    )


class FilterModule:
    """Registers the ARC role's filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "arc_profiles": arc_profiles,
            "arc_max_runners": arc_max_runners,
            "arc_measured_max_runners": arc_measured_max_runners,
            "arc_upgrade_blocker": arc_upgrade_blocker,
            "arc_image_ref": arc_image_ref,
            "arc_runner_placement": arc_runner_placement,
            "arc_capability_errors": arc_capability_errors,
            "arc_capability_pod": arc_capability_pod,
            "arc_registry_cache": arc_registry_cache,
            "arc_registry_cache_pod": arc_registry_cache_pod,
            "arc_registry_cache_configmap": arc_registry_cache_configmap,
            "arc_registry_cache_secret_problems": arc_registry_cache_secret_problems,
        }
