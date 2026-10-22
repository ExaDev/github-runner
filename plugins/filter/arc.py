"""Filters for the github_runner_arc role: expanding orgs into scale-set profiles, placing their runner pods (on the eligible nodes, or overflowing onto burst nodes), deriving a runner ceiling from node capacity (uniform figures, or each node's measured free capacity), adding a profile's capabilities (tool-cache and system-library init containers and job-started hooks) to its Helm values, and splitting an image reference."""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping, Sequence
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, NamedTuple

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

# A capability's keys (see arc_capability_errors): a tool-cache capability names an image, and optionally a path put on PATH at job start; a system-library capability names a sysroot_image, and optionally env, extra variables its libraries need; a hook capability carries a job-started script; a node-cache capability names a node_cache mapping, {init_image, dirs}, and may also set env.
_CAPABILITY_KEYS = frozenset({"image", "path", "sysroot_image", "env", "job_started_hook", "node_cache"})
# The keys of which a capability sets exactly one, each naming its kind.
_CAPABILITY_KINDS = ("image", "sysroot_image", "job_started_hook", "node_cache")
# The keys of a node-cache capability's node_cache mapping.
_NODE_CACHE_KEYS = frozenset({"init_image", "dirs"})
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
# The node-local volume every node-cache capability of a profile shares: a hostPath directory of the node, one per profile, mounted once so that a hard link between two of its directories stays on one mount.
_NODE_CACHE_VOLUME = "node-cache"
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


def _tolerations(value: Any, where: str, errors: list[str]) -> list[dict[str, Any]]:
    """Return a list of Kubernetes tolerations as plain dicts, or record why value is not one and return an empty list."""
    tolerations = value or []
    if not isinstance(tolerations, Sequence) or isinstance(tolerations, (str, bytes)) or not all(isinstance(toleration, Mapping) for toleration in tolerations):
        errors.append(f"{where} must be a list of Kubernetes tolerations")
        return []
    for index, toleration in enumerate(tolerations):
        errors.extend(f"{where}[{index}] has an unknown key '{key}'" for key in sorted(set(toleration) - _TOLERATION_KEYS))
    return [dict(toleration) for toleration in tolerations]


def _tolerates(tolerations: Sequence[Mapping[str, Any]], taint: Mapping[str, Any]) -> bool:
    """Whether one of tolerations matches taint, by the rules of core/v1 Toleration: an empty key with operator Exists matches every taint; otherwise the keys are equal, an empty effect matches any effect, and operator Equal (the default) also needs equal values while Exists ignores them."""
    for toleration in tolerations:
        key = toleration.get("key", "")
        operator = toleration.get("operator") or "Equal"
        if key == "" and operator == "Exists":
            return True
        if key != taint.get("key"):
            continue
        if toleration.get("effect") and toleration["effect"] != taint.get("effect"):
            continue
        if operator == "Exists" or toleration.get("value", "") == taint.get("value", ""):
            return True
    return False


def _node_eligibility(node: Mapping[str, Any], node_selector: Mapping[str, Any], tolerations: Sequence[Mapping[str, Any]] = ()) -> tuple[str, str]:
    """Return why runner pods can never be placed on node (empty when they can), and any passing condition that keeps them off it for now. A taint that one of tolerations matches does not keep them off."""
    labels = (node.get("metadata") or {}).get("labels") or {}
    for key, value in node_selector.items():
        if labels.get(key) != str(value):
            return f"does not have the label {key}={value}", ""
    spec = node.get("spec") or {}
    conditions = ["is cordoned"] if spec.get("unschedulable") else []
    for taint in spec.get("taints") or []:
        if taint.get("effect") not in _REPELLING_TAINT_EFFECTS or _tolerates(tolerations, taint):
            continue
        description = f"has the taint {taint.get('key')}:{taint.get('effect')}"
        if not str(taint.get("key", "")).startswith(_CONDITION_TAINT_PREFIX):
            return description, ""
        conditions.append(description)
    return "", ", ".join(conditions)


def arc_measured_max_runners(sizing: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]], pods: Sequence[Mapping[str, Any]], node_selector: Mapping[str, Any] | None = None, excluded_namespaces: Sequence[str] = (), tolerations: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Derive how many runner pods fit on the eligible nodes as they are now.

    A node is eligible when it carries every label in node_selector and has no NoSchedule or NoExecute taint other than the ones Kubernetes adds for a passing condition (not ready, unreachable, cordoned and the like): the ceiling lasts until the next run, so a node that is only briefly unavailable still counts. For each one, its allocatable CPU and memory, less what the pods already on it request and less the sizing's reserve, is divided by one runner pod's requests; the tighter bound is how many fit there. The sum over the nodes, scaled down by the safety margin, is the result. Pods in excluded_namespaces are not counted, so the runner pods themselves never lower the ceiling they are sized by, and pods that have finished are not counted, as the scheduler does not count them.

    Args:
        sizing: a measured sizing mapping (see arc_max_runners).
        nodes: Node objects, as kubernetes.core.k8s_info returns them.
        pods: Pod objects from every namespace.
        node_selector: labels an eligible node must carry.
        excluded_namespaces: namespaces whose pods are left out of each node's requests.
        tolerations: the runner pods' tolerations; a node whose only repelling taints they match is eligible, as it is for the pods.

    Returns:
        A dict with ``errors`` (messages, empty when it could be worked out), ``nodes`` (per eligible node: name, allocatable_cpu, allocatable_memory_gib, requested_cpu, requested_memory_gib, by_cpu, by_memory, by_pods (None when the node reports no pod capacity), runners, and condition, naming any passing condition it was counted despite), ``skipped_nodes`` (name and reason for each node that is not eligible), ``theoretical`` and ``max_runners``, which is None when there are errors.
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
    # Pods other than runner pods on each node, which hold slots of the node's pod capacity the runners cannot have.
    other_pods: dict[str, int] = {}
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
        other_pods[node_name] = other_pods.get(node_name, 0) + 1
    eligible: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for node in sorted(nodes, key=lambda item: (item.get("metadata") or {}).get("name", "")):
        name = (node.get("metadata") or {}).get("name", "")
        reason, condition = _node_eligibility(node, selector, tolerations)
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
        # A node's pod capacity (the kubelet's max-pods) can be set below what its cpu and memory would hold, to keep a node whose disk is the limit from running more jobs than it can serve. Kubernetes always reports it, so a node without it is a test fixture or an unusual one, and is not limited by it.
        by_pods = None
        if "pods" in allocatable:
            try:
                by_pods = max(0, int(arc_quantity(allocatable["pods"])) - other_pods.get(name, 0))
            except ValueError as error:
                errors.append(f"node {name}: {error}")
                continue
        eligible.append({
            "name": name,
            "allocatable_cpu": float(allocatable_cpu),
            "allocatable_memory_gib": float(allocatable_memory / _GIB),
            "requested_cpu": float(used_cpu),
            "requested_memory_gib": float(used_memory / _GIB),
            "by_cpu": by_cpu,
            "by_memory": by_memory,
            "by_pods": by_pods,
            "runners": min(by_cpu, by_memory) if by_pods is None else min(by_cpu, by_memory, by_pods),
            "condition": condition,
        })
    if not eligible and not errors:
        errors.append(f"no node is eligible for runner pods{' with the labels ' + ', '.join(f'{key}={value}' for key, value in selector.items()) if selector else ''}")
    if errors:
        return {"errors": errors, "nodes": eligible, "skipped_nodes": skipped, "theoretical": None, "max_runners": None}
    theoretical = sum(node["runners"] for node in eligible)
    # The safety margin covers how far a node's cpu and memory estimate can be off, other workloads on it growing included. A pod capacity is not an estimate: it is a hard limit, so the runners a node holds because of it are counted in full, and only the rest are scaled by the margin.
    pod_bound = sum(node["runners"] for node in eligible if node["by_pods"] is not None and node["by_pods"] < min(node["by_cpu"], node["by_memory"]))
    return {
        "errors": [],
        "nodes": eligible,
        "skipped_nodes": skipped,
        "theoretical": theoretical,
        "max_runners": pod_bound + _floor(Decimal(theoretical - pod_bound) * Decimal(str(checked["safety_margin"]))),
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
    tolerations = _tolerations(burst.get("tolerations"), f"{where}.burst.tolerations", errors)
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
    own_tolerations = profile["tolerations"]
    if burst is None:
        placement: dict[str, Any] = {"nodeSelector": {**selector, **profile["node_selector"]}}
        if own_tolerations:
            placement["tolerations"] = own_tolerations
        return placement
    burst_node = {"key": burst["node_label_key"], "operator": "In", "values": burst["node_label_values"]} if burst["node_label_values"] else {"key": burst["node_label_key"], "operator": "Exists"}
    # An exclusive profile runs on burst nodes only, whatever the eligibility label, so there is no fixed node to prefer.
    if burst["exclusive"]:
        return {"nodeSelector": dict(profile["node_selector"]), "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [burst_node]}]}}}, "tolerations": own_tolerations + burst["tolerations"]}
    node_affinity: dict[str, Any] = {"preferredDuringSchedulingIgnoredDuringExecution": [{"weight": _PREFER_FIXED_NODES_WEIGHT, "preference": {"matchExpressions": [{"key": burst["node_label_key"], "operator": "DoesNotExist"}]}}]}
    # With no eligibility label every untainted node is already allowed, and the tolerations alone add the burst nodes.
    if selector:
        eligible_node = [{"key": key, "operator": "In", "values": [value]} for key, value in selector.items()]
        node_affinity["requiredDuringSchedulingIgnoredDuringExecution"] = {"nodeSelectorTerms": [{"matchExpressions": eligible_node}, {"matchExpressions": [burst_node]}]}
    return {"nodeSelector": dict(profile["node_selector"]), "affinity": {"nodeAffinity": node_affinity}, "tolerations": own_tolerations + burst["tolerations"]}


def _expand_profile(org_name: str, runner_image: str, app_secret: str, index: int, profile: Any, errors: list[str]) -> dict[str, Any] | None:
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
    try:
        capability_store = boolean(profile.get("capability_store", False), strict=True)
    except TypeError:
        errors.append(f"{where}.capability_store must be a boolean")
        capability_store = False
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
        "runner_image": runner_image,
        "scale_set_labels": _scale_set_labels(profile, f"{org_lower}-runners", where, errors),
        "values_file": _text(profile, "values_file"),
        "node_selector": dict(node_selector),
        "autoscale": autoscale,
        "sizing": sizing_result,
        "tolerations": _tolerations(profile.get("tolerations"), f"{where}.tolerations", errors),
        "burst": burst,
        "burst_runners": burst_runners,
        "max_runners": max_runners,
        "capabilities": _capability_names(profile, where, errors),
        "capability_store": capability_store,
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

    Pass every org that installs into one cluster, not one host's, so that the cross-host checks (an org configured twice, two profiles sharing a namespace, more than one autoscaled profile setting its own sizing) see the whole cluster; arc_cluster_profiles gathers them from the inventory.

    Args:
        require_app_id: whether each org must set app_id. The role needs it only when it writes the App Secret; otherwise the App's id is in the existing App Secret, and an app_id that is set is only checked against it.
        orgs: github_runner_arc_orgs entries, each with name, app_id (see require_app_id), image and a non-empty scale_set_profiles list, at most one of private_key and private_key_op_reference, and optionally app_secret_name. A profile may override its namespace and release_name, and set scale_set_labels in place of runs_on_label. A profile may set capability_store (a boolean, default false) to copy its tool-cache and system-library payloads, and the dind sidecar's externals, into the capability store, which the pods of a node share, instead of into every pod (see arc_capability_pod). A profile with sizing may set burst (node_label_key, node_label_values, tolerations, max_runners) to let its runner pods overflow onto burst nodes, and a profile without sizing may set burst with exclusive true to run on burst nodes only, with burst.max_runners as its maxRunners; see arc_runner_placement.

    Returns:
        A dict with ``errors`` (messages, empty when valid), ``profiles`` (one dict per profile with org_name, suffix, namespace, release, target (namespace/release, joined), app_secret, runner_image (its org's image), scale_set_labels, values_file, node_selector, autoscale, sizing, burst (the burst settings with defaults filled in, or None), burst_runners (burst.max_runners, 0 without burst), max_runners (the static maxRunners the role sets, including burst_runners, or None), capabilities (the names of the capabilities its runner pods carry, in order; see arc_capability_pod), capability_store (whether it uses the capability store), label and settings, the profile's own keys) and ``autoscaled`` (every profile flagged autoscale, sharing one autoscaler-managed memory pool across their combined scale sets; empty when none are).
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
            errors.append(f"Org '{org_name}' is configured more than once in github_runner_arc_orgs (also as '{seen_orgs[org_name.lower()]}'). Each org's Helm releases in a cluster must be installed from one entry on one host; remove the duplicate.")
            continue
        seen_orgs[org_name.lower()] = org_name
        org_profiles = org.get("scale_set_profiles")
        if not isinstance(org_profiles, Sequence) or isinstance(org_profiles, (str, bytes)) or not org_profiles:
            errors.append(f"{org_name}: scale_set_profiles must be a non-empty list")
            continue
        app_secret = _optional_name(org, "app_secret_name", org_name, errors) or f"{org_name.lower()}-github-app"
        for profile_index, profile in enumerate(org_profiles):
            expanded = _expand_profile(org_name, _text(org, "image"), app_secret, profile_index, profile, errors)
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


def _is_list(value: Any) -> bool:
    """Return whether value is a list in the YAML sense: a sequence that is not a string."""
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def arc_cluster_profiles(orgs_by_host: Mapping[str, Any], cluster: Sequence[str] | None = None) -> dict[str, Any]:
    """Expand and check the orgs of every host that installs into one cluster, and check their scale sets against the hosts of every other cluster.

    The hosts of one cluster share its controller, namespaces and autoscaled pool, so an org is installed into a cluster from one host, and arc_profiles' checks apply across all of them. A host of another cluster (another cluster group, with a controller of its own) may install the same org, since each cluster's releases and namespaces are its own, but not the same scale set: GitHub gives an org's scale set name to one controller at a time, so a second cluster's listener would contend with the first for it.

    Args:
        orgs_by_host: each inventory host's github_runner_arc_orgs, by host name, in inventory order. cluster: the names of the hosts that install into the same cluster as the host being checked, or None when that is not known, which treats every host as one cluster.

    Returns:
        arc_profiles' result for the orgs of the cluster's hosts (see arc_profiles; app_id is not required, since only each host's own check knows whether it writes the App Secret), with errors that also name an org configured on two of the cluster's hosts and a scale set registered from both this cluster and another.
    """
    members = set(orgs_by_host) if cluster is None else set(cluster)
    errors: list[str] = []
    cluster_orgs: list[Any] = []
    installers: dict[str, tuple[str, str]] = {}
    for host, orgs in orgs_by_host.items():
        if host not in members:
            continue
        if not _is_list(orgs):
            errors.append(f"{host}: github_runner_arc_orgs must be a list")
            continue
        for org in orgs:
            name = _text(org, "name") if isinstance(org, Mapping) else ""
            installer = installers.get(name.lower()) if name else None
            # The same org twice on one host is arc_profiles' own check, so it is passed on for that.
            if installer is not None and installer[0] != host:
                errors.append(f"Org '{name}' is configured more than once in github_runner_arc_orgs for one cluster, on {installer[0]} (as '{installer[1]}') and on {host}. Each org's Helm releases in a cluster must be installed from one host; move its scale_set_profiles onto one of them.")
                continue
            if name:
                installers.setdefault(name.lower(), (host, name))
            cluster_orgs.append(org)
    result = arc_profiles(cluster_orgs, require_app_id=False)
    # Another cluster's hosts are checked in full by that cluster's own validation, so only the scale sets that expand are compared here.
    registered: dict[tuple[str, str], tuple[str, str]] = {}
    elsewhere: list[tuple[str, dict[str, Any]]] = []
    for host, orgs in orgs_by_host.items():
        if not _is_list(orgs):
            continue
        for profile in arc_profiles(orgs, require_app_id=False)["profiles"]:
            if host in members:
                registered.setdefault((profile["org_name"].lower(), profile["release"]), (host, profile["label"]))
            else:
                elsewhere.append((host, profile))
    for host, profile in elsewhere:
        match = registered.get((profile["org_name"].lower(), profile["release"]))
        if match is not None:
            errors.append(f"{match[1]} on {match[0]} and {profile['label']} on {host}, which installs into another cluster, both register scale set '{profile['release']}' for org '{profile['org_name']}'. GitHub gives a scale set name to one controller at a time; uninstall it from one cluster before installing it in the other, or give one of them a different suffix or release_name.")
    return {**result, "errors": errors + result["errors"]}


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
    container = f"{_CAPABILITY_CONTAINER_PREFIX}{name}"
    errors = [f"{where}: its init container would be named {container}, which is the name of one of the capability store's own init containers (a pod cannot have two with one name)"] if container in _CAPABILITY_STORE_CONTAINER_NAMES else []
    if not isinstance(capability, Mapping):
        return [*errors, f"{where} must be a mapping"]
    errors.extend(f"{where} has an unknown key '{key}'" for key in sorted(set(capability) - _CAPABILITY_KEYS))
    image = capability.get("image")
    sysroot_image = capability.get("sysroot_image")
    hook = capability.get("job_started_hook")
    if sum(capability.get(kind) is not None for kind in _CAPABILITY_KINDS) != 1:
        errors.append(f"{where} must set exactly one of image (a tool-cache capability), sysroot_image (a system-library capability), job_started_hook (a hook capability) and node_cache (a node-cache capability)")
    node_cache = capability.get("node_cache")
    if node_cache is not None:
        errors.extend(_node_cache_errors(f"{where}.node_cache", node_cache))
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
        if sysroot_image is None and node_cache is None:
            errors.append(f"{where}.env is only for a system-library capability (one with a sysroot_image) or a node-cache capability (one with a node_cache)")
        errors.extend(_env_errors(f"{where}.env", env))
    return errors


def _node_cache_errors(where: str, node_cache: Any) -> list[str]:
    """Return what is wrong with a node-cache capability's node_cache mapping: an init_image pinned by digest (name@sha256:<64 hex digits>), and dirs, a non-empty mapping of environment variable name to the subdirectory of the node cache that variable names, each subdirectory one path component and no two variables sharing one."""
    if not isinstance(node_cache, Mapping):
        return [f"{where} must be a mapping with init_image and dirs"]
    errors = [f"{where} has an unknown key '{key}'" for key in sorted(set(node_cache) - _NODE_CACHE_KEYS)]
    init_image = node_cache.get("init_image")
    errors.extend(_pinned_image_errors(f"{where}.init_image", init_image))
    # It runs as root beside a hostPath volume, so a tag a registry can move is not enough: the digest names the bytes.
    if isinstance(init_image, str) and init_image and not re.search(r"@sha256:[0-9a-f]{64}$", init_image):
        errors.append(f"{where}.init_image must be pinned by digest (name@sha256:<64 hex digits>), since it runs as root with a hostPath volume (got {init_image!r})")
    dirs = node_cache.get("dirs")
    if not isinstance(dirs, Mapping) or not dirs:
        errors.append(f"{where}.dirs must be a non-empty mapping of environment variable name to a subdirectory of the node cache")
        return errors
    for name, subdirectory in dirs.items():
        if not isinstance(name, str) or not _ENV_NAME.match(name):
            errors.append(f"{where}.dirs has a key that is not an environment variable name ({name!r})")
        elif name in _RESERVED_ENV:
            errors.append(f"{where}.dirs may not set {name}, which the capabilities set themselves")
        if not isinstance(subdirectory, str) or not _PATH_COMPONENT.match(subdirectory):
            errors.append(f"{where}.dirs.{name} must be one directory name, letters, digits and '._+-' (got {subdirectory!r})")
    subdirectories = [value for value in dirs.values() if isinstance(value, str)]
    errors.extend(f"{where}.dirs names the subdirectory {value} for more than one variable" for value in sorted({value for value in subdirectories if subdirectories.count(value) > 1}))
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
        The problems found, empty when there are none: invalid entries, a profile naming an undefined capability, two of a profile's system-library capabilities setting one variable to different values, and a profile with capability_store whose image or sysroot_image is not pinned by digest (arc_capability_store_errors checks the store's settings against the role's paths).
    """
    if not isinstance(catalogue, Mapping):
        return ["github_runner_arc_capabilities must be a mapping of capability name to capability"]
    errors = [error for name, capability in catalogue.items() for error in _capability_entry_errors(name, capability)]
    for profile in profiles:
        errors.extend(f"{profile['label']}: capability '{name}' is not defined in github_runner_arc_capabilities" for name in profile["capabilities"] if name not in catalogue)
        errors.extend(_sysroot_env_conflicts(profile["label"], profile["capabilities"], catalogue))
        if profile["capability_store"]:
            errors.extend(f"{profile['label']}: {message}" for message in _capability_store_digest_errors(profile["capabilities"], catalogue))
    return errors


def _runner_container(spec: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the pod template's container named runner, which the chart builds the runner from, or None when the values define none."""
    for container in spec.get("containers") or []:
        if isinstance(container, dict) and container.get("name") == "runner":
            return container
    return None


def _overlap_errors(subject: str, path: str, others: Mapping[str, str]) -> list[str]:
    """Return a message for each of the other mounted directories that path equals, contains or sits inside."""
    root = path.rstrip("/")
    errors: list[str] = []
    for what, other in others.items():
        other_root = str(other).rstrip("/")
        if root == other_root or root.startswith(f"{other_root}/") or other_root.startswith(f"{root}/"):
            errors.append(f"the {subject} {path} overlaps the {what} {other}; each needs a directory of its own")
    return errors


def _sysroot_path_errors(sysroot_path: str, others: Mapping[str, str]) -> list[str]:
    """Return what is wrong with the sysroot's mount path: it must be an absolute path that can sit in a colon-separated search path, apart from the other directories the capabilities mount."""
    if not isinstance(sysroot_path, str) or not sysroot_path.startswith("/") or sysroot_path == "/" or any(character in sysroot_path for character in ":\n\r"):
        return [f"the sysroot path must be an absolute directory with no ':' or line break, since it is joined into search paths such as LD_LIBRARY_PATH (got {sysroot_path!r})"]
    return _overlap_errors("sysroot path", sysroot_path, others)


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


# The node directories the cache root may not be, or sit inside. The system's own, then the state roots of the Kubernetes components and storage systems a node may run, each as its project documents it: the kubelet root (https://kubernetes.io/docs/reference/command-line-tools-reference/kubelet/), etcd under kubeadm (https://kubernetes.io/docs/reference/config-api/kubeadm-config.v1beta4/), k3s (https://docs.k3s.io/cli/server), containerd (https://github.com/containerd/containerd/blob/main/docs/ops.md), CRI-O's /var/lib/containers (https://github.com/cri-o/cri-o/blob/main/docs/crio.conf.5.md), Calico (https://github.com/projectcalico/calico, its manifests mount /var/lib/calico), Longhorn's default data path (https://github.com/longhorn/longhorn, chart values), Rook's dataDirHostPath (https://github.com/rook/rook, deploy/examples/cluster.yaml), Rancher's local-path-provisioner (https://github.com/rancher/local-path-provisioner), the local static provisioner's discovery directory (https://github.com/kubernetes-sigs/sig-storage-local-static-provisioner) and Headscale's data directory (https://github.com/juanfont/headscale, config-example.yaml). The name rule on the last component is the main protection; this list is defence in depth.
_NODE_CACHE_DENIED_HOST_PATHS = (
    "/etc", "/proc", "/sys", "/dev", "/boot", "/run", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/root", "/home", "/tmp", "/var/tmp", "/var/run", "/var/log", "/var/cache", "/var/spool",
    "/var/lib/kubelet", "/var/lib/rancher", "/var/lib/containerd", "/var/lib/docker", "/var/lib/etcd", "/var/lib/dpkg", "/var/lib/apt", "/var/lib/systemd", "/var/lib/cni", "/var/lib/kubernetes",
    "/var/lib/k0s", "/var/lib/calico", "/var/lib/containers", "/var/lib/snapd", "/var/lib/k3s", "/var/lib/microk8s", "/var/lib/postgresql", "/var/lib/mysql",
    "/var/lib/longhorn", "/var/lib/rook", "/var/lib/headscale", "/opt/local-path-provisioner", "/mnt/disks",
)
# The fewest path components a host path has: a dedicated subdirectory of a directory such as /var/lib, never a top-level directory or /var/lib itself.
_NODE_CACHE_HOST_PATH_MIN_COMPONENTS = 3


# What the final component of the host path must be: node-cache itself, or a lower-case name ending in -node-cache. The root is a directory the role creates and empties as root, so it carries a name that says so, which rules out pointing it at a system or runtime directory by accident.
_NODE_CACHE_HOST_PATH_NAME = re.compile(r"^(?:node-cache|[a-z0-9][a-z0-9-]*-node-cache)$")


def _canonical_path(path: str) -> str:
    """Return an absolute path without repeated or trailing slashes."""
    return "/" + "/".join(component for component in path.split("/") if component)


def _node_cache_host_path_errors(host_path: Any) -> list[str]:
    """Return what is wrong with the node cache's host path: an absolute path with no colon or line break, already in canonical form (no repeated or trailing slash, so every string built from it agrees), at least a dedicated subdirectory's depth with no '.' or '..', whose last component ends with node-cache, and which is not a system directory or inside one. A symbolic link at or above the path is not detected: it defeats this validation."""
    if not isinstance(host_path, str) or not host_path.startswith("/") or any(character in host_path for character in ":\n\r"):
        return [f"the node cache's host path must be an absolute directory with no ':' or line break (got {host_path!r})"]
    if host_path != _canonical_path(host_path):
        return [f"the node cache's host path must not contain '//' or end with '/', since the pods' volume paths and the pruner's comparison are built from it (got {host_path!r}, which is {_canonical_path(host_path)!r} written plainly)"]
    components = host_path.split("/")[1:]
    if len(components) < _NODE_CACHE_HOST_PATH_MIN_COMPONENTS or ".." in components or "." in components:
        return [f"the node cache's host path must be a dedicated directory at least {_NODE_CACHE_HOST_PATH_MIN_COMPONENTS} levels deep with no '.' or '..' (such as /var/lib/github-runner/node-cache) (got {host_path!r})"]
    if not _NODE_CACHE_HOST_PATH_NAME.match(components[-1]):
        return [f"the node cache's host path must end in a directory named node-cache, or a lower-case name ending in -node-cache (got {host_path!r}), since the role creates and empties it as root"]
    lowered = host_path.lower()
    inside = [denied for denied in _NODE_CACHE_DENIED_HOST_PATHS if lowered == denied or lowered.startswith(f"{denied}/")]
    return [f"the node cache's host path {host_path} is inside {inside[0]}, which the node itself uses"] if inside else []


def arc_node_cache_root(host_path: str) -> str:
    """Return github_runner_arc_node_cache_host_path as the one string every path of the cache is built from: with repeated and trailing slashes removed, which a validated value already is."""
    return _canonical_path(host_path)


def arc_node_cache_dir(host_path: str, namespace: str, release: str) -> str:
    """Return the node directory of one profile's cache, <root>/<namespace>/<release>, which is the hostPath of the profile's pods and, under the pruner's mount of the root, the directory the pruner compares the pods' volumes with."""
    return f"{_canonical_path(host_path)}/{namespace}/{release}"


def arc_node_cache_host_path_errors(host_path: Any) -> list[str]:
    """Return what is wrong with github_runner_arc_node_cache_host_path, the root every profile's cache directory sits under (the pruner mounts it read-write as root), whether or not any profile has a node cache: empty when it is a dedicated directory of a node."""
    return _node_cache_host_path_errors(host_path)


def arc_node_cache_path_errors(node_cache_path: Any) -> list[str]:
    """Return what is wrong with github_runner_arc_node_cache_path, where the cache is mounted on the runner container: empty when it is an absolute directory other than / with no colon or line break."""
    if not isinstance(node_cache_path, str) or not node_cache_path.startswith("/") or node_cache_path == "/" or any(character in node_cache_path for character in ":\n\r"):
        return [f"the node cache path must be an absolute directory other than / with no ':' or line break (got {node_cache_path!r})"]
    return []


def _node_cache_path_errors(node_cache_path: str, node_cache_host_path: str, others: Mapping[str, str]) -> list[str]:
    """Return what is wrong with the node cache's paths: the mount path must be an absolute directory with no colon or line break, apart from the other directories the capabilities mount, and the pod's host directory a canonical absolute path."""
    errors = arc_node_cache_path_errors(node_cache_path)
    if not errors:
        errors.extend(_overlap_errors("node cache path", node_cache_path, others))
    # The root every profile's directory sits under is checked as a whole (arc_node_cache_host_path_errors); what the pod gets is that root joined with the profile's namespace and release, so only its form is checked here.
    if not isinstance(node_cache_host_path, str) or not node_cache_host_path.startswith("/") or node_cache_host_path != _canonical_path(node_cache_host_path) or any(character in node_cache_host_path for character in ":\n\r"):
        errors.append(f"the node cache's host directory must be an absolute path without ':', repeated '/' or a trailing '/' (got {node_cache_host_path!r})")
    return errors


def arc_node_cache_profiles(profiles: Sequence[Mapping[str, Any]], catalogue: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return the profiles that carry a node-cache capability, each as its namespace, release and target (<namespace>/<release>, which is also the name of its directory under the node cache root), in the order given.

    Args:
        profiles: expanded profiles from arc_profiles. catalogue: github_runner_arc_capabilities, already checked by arc_capability_errors.
    """
    return [
        {"namespace": profile["namespace"], "release": profile["release"], "target": profile["target"]}
        for profile in profiles
        if any(isinstance(catalogue.get(name), Mapping) and catalogue[name].get("node_cache") is not None for name in profile["capabilities"])
    ]


def arc_bounded_integer_errors(value: Any, name: str, maximum: int, pattern: str, allow_zero: bool = False) -> list[str]:
    """Return what is wrong with a numeric node cache setting, the way the pruner (scripts/node-cache-pruner.sh) checks it at start-up: the text of the value must match pattern entirely, which is a positive integer with no sign, leading zero, space or fraction, and be at most maximum. allow_zero lets 0 through, for a setting where it means that nothing is installed.

    Args:
        value: the setting as given, a number or a string. name: the setting's name, for the message. maximum: the largest value allowed. pattern: the pruner's POSITIVE_INTEGER pattern, from the role's variables. allow_zero: whether 0 is valid.
    """
    text = str(value)
    if allow_zero and text == "0":
        return []
    if not re.fullmatch(pattern, text):
        return [f"{name} must be a positive integer with no sign, leading zero, space or fraction (got {text!r})"]
    if int(text) > maximum:
        return [f"{name} must be at most {maximum}, so that the pruner's arithmetic cannot wrap (got {text!r})"]
    return []


def arc_node_cache_legacy_access(found: Sequence[Mapping[str, Any]], platform_namespace: str) -> dict[str, list[Any]]:
    """Return what to remove of the ClusterRoleBinding and ClusterRole named node-cache-pruner that release 1.31.0 created, and what to report instead, and nothing that merely shares the name.

    Release 1.31.0 put no labels on them, so what identifies them is their content, which was exactly this: a ClusterRole, not aggregated, whose only rule lists pods, and a ClusterRoleBinding that binds that ClusterRole to the node-cache-pruner ServiceAccount of the platform namespace and to nothing else. An object with that name and any other content belongs to someone else and is left alone.

    The two go together. The binding is removed when it is ours, and the role when it is ours and the binding is gone or ours. A ClusterRole that is ours while a binding of that name exists that is not (the platform namespace changed since 1.31.0, or another subject was added) is left with it: removing the role would leave that binding to grant cluster-wide pod listing again if a ClusterRole of the name ever reappeared. That case is reported in ``left``, once, with a stable message.

    Args:
        found: the ClusterRoles and ClusterRoleBindings called node-cache-pruner, as the cluster returned them, the binding first. platform_namespace: the namespace the pruner's ServiceAccount is in.

    Returns:
        ``remove``, the kind and name of each object to delete in the order found (the binding before the role it binds), and ``left``, messages that start with NODE_CACHE_LEGACY_ACCESS_LEFT for whatever is deliberately left in place.
    """
    own_rules = [{"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}]
    own_subject = {"kind": "ServiceAccount", "name": "node-cache-pruner", "namespace": platform_namespace}
    named = [resource for resource in found if resource.get("metadata", {}).get("name") == "node-cache-pruner"]
    roles = [resource for resource in named if resource.get("kind") == "ClusterRole"]
    bindings = [resource for resource in named if resource.get("kind") == "ClusterRoleBinding"]
    role_is_ours = any(role.get("rules") == own_rules and not role.get("aggregationRule") for role in roles)

    def binding_is_ours(binding: Mapping[str, Any]) -> bool:
        subjects = binding.get("subjects") or []
        role_ref = binding.get("roleRef") or {}
        return len(subjects) == 1 and all(subjects[0].get(key) == value for key, value in own_subject.items()) and role_ref.get("kind") == "ClusterRole" and role_ref.get("name") == "node-cache-pruner"

    ours = [binding for binding in bindings if binding_is_ours(binding)]
    foreign = [binding for binding in bindings if not binding_is_ours(binding)]
    remove: list[dict[str, str]] = [{"kind": "ClusterRoleBinding", "name": "node-cache-pruner"}] if ours else []
    left: list[str] = []
    if role_is_ours and foreign:
        left.append(
            "NODE_CACHE_LEGACY_ACCESS_LEFT ClusterRole/node-cache-pruner was created by release 1.31.0, but ClusterRoleBinding/node-cache-pruner binds a ClusterRole of that name to something else "
            f"(subjects {foreign[0].get('subjects') or []}); both are left in place. Delete them by hand if they are not wanted."
        )
    elif role_is_ours:
        remove.append({"kind": "ClusterRole", "name": "node-cache-pruner"})
    return {"remove": remove, "left": left}


def arc_label_selectors(labels: Mapping[str, str]) -> list[str]:
    """Return a label mapping as the list of key=value selectors that matches exactly the objects carrying all of them."""
    return [f"{key}={value}" for key, value in labels.items()]


def arc_node_cache_access_to_remove(found: Sequence[Mapping[str, Any]], labels: Mapping[str, str], pruner_wanted: bool, store_role: str, store_namespaces: Sequence[str]) -> list[dict[str, str]]:
    """Return the Roles and RoleBindings of the node cache pruner to remove, as kind, namespace and name, bindings before the roles they bind.

    While the pruner is wanted a profile that has left the fleet keeps its grant, which only lists pods in that profile's own namespace, for as long as the pruner is installed, so staying is harmless and the pruner can still check, and so clear, a directory the profile left. The capability store's grant (the object named store_role) is not kept the same way: it lists scale sets as well, and only the namespaces the pruner reads the store's references from (store_namespaces, empty while no profile uses the store) need it, so one anywhere else goes. When the pruner is not wanted every object found goes. Only an object carrying every one of labels is the role's own, so another software's object that shares one label is never removed.

    Args:
        found: Roles and RoleBindings as the cluster returned them. labels: the labels the role puts on every object it creates for the pruner. pruner_wanted: whether the pruner is installed. store_role: the name of the Role and RoleBinding that grant the capability store's references. store_namespaces: the namespaces from arc_capability_store_namespaces.
    """
    ours = [resource for resource in found if all((resource["metadata"].get("labels") or {}).get(key) == value for key, value in labels.items())]
    if pruner_wanted:
        ours = [resource for resource in ours if resource["metadata"]["name"] == store_role and resource["metadata"]["namespace"] not in store_namespaces]
    stale = [{"kind": resource["kind"], "namespace": resource["metadata"]["namespace"], "name": resource["metadata"]["name"]} for resource in ours]
    return sorted(stale, key=lambda item: (item["kind"] != "RoleBinding", item["namespace"], item["name"]))


def arc_terminating_namespaces(namespaces: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the names, sorted, of the Namespaces as the cluster returned them whose phase is Terminating, in which the API server refuses to create anything."""
    return sorted(namespace["metadata"]["name"] for namespace in namespaces if namespace["status"]["phase"] == "Terminating")


def arc_capability_store_namespaces(profiles: Sequence[Mapping[str, Any]], found: Sequence[Mapping[str, Any]], labels: Mapping[str, str], store_role: str, terminating: Sequence[str]) -> list[str]:
    """Return the namespaces the node cache pruner reads the capability store's references from, sorted, or none while no profile uses the store (the pruner then leaves the store alone).

    A pod of a profile that has since switched the store off, or left the fleet, keeps running from the template it was made from and keeps using the entries that template named, so its namespace stays among them: the namespaces of every profile in the fleet, and of every per-profile Role the pruner has installed, which the role keeps for a profile that has left the fleet (see arc_node_cache_access_to_remove). An entry is then never removed under a pod because its profile stopped appearing in the fleet. The store's own Role (store_role) does not count, since it is what this list produces and a namespace would otherwise keep itself in it for ever. Only a Role carrying every one of labels counts, so another software's objects are ignored. A Terminating namespace is left out, since the API server refuses to create a Role in one and its pods are being deleted.

    Args:
        profiles: expanded profiles from arc_profiles, each with its namespace and capability_store. found: the Roles and RoleBindings carrying the pruner's labels, as the cluster returned them. labels: the labels the role puts on every object it creates for the pruner. store_role: the name of the Role that grants the capability store's references. terminating: the names of the namespaces in phase Terminating, from arc_terminating_namespaces.
    """
    if not any(profile["capability_store"] for profile in profiles):
        return []
    roles = [resource["metadata"]["namespace"] for resource in found if resource["kind"] == "Role" and resource["metadata"]["name"] != store_role and all((resource["metadata"].get("labels") or {}).get(key) == value for key, value in labels.items())]
    return sorted(({profile["namespace"] for profile in profiles} | set(roles)) - set(terminating))


def _node_cache_env(node_cache_path: str, capabilities: Sequence[tuple[str, Mapping[str, Any]]], errors: list[str]) -> list[dict[str, str]]:
    """Return the runner container's variables for a profile's node-cache capabilities: each dirs variable naming its subdirectory of the node cache, and each env variable as given. A variable that two capabilities set to different values is recorded in errors."""
    root = node_cache_path.rstrip("/")
    values: dict[str, tuple[str, str]] = {}
    for name, capability in capabilities:
        entries = {variable: f"{root}/{subdirectory}" for variable, subdirectory in capability["node_cache"]["dirs"].items()}
        entries.update(capability.get("env") or {})
        for variable, value in entries.items():
            if variable in values and values[variable][1] != value:
                errors.append(f"node-cache capabilities {values[variable][0]} and {name} set {variable} to different values")
            values.setdefault(variable, (name, value))
    return [{"name": variable, "value": value} for variable, (_, value) in values.items()]


def _node_cache_init_container(name: str, capability: Mapping[str, Any], node_cache_path: str, runner_user: int, runner_group: int) -> dict[str, Any]:
    """Return the init container that creates a node-cache capability's subdirectories, owned by the runner's user. It runs as root, since the node's directory belongs to root until a pod claims it. Its capabilities are CHOWN, to hand a directory to the runner user, and DAC_OVERRIDE, because root without it is an ordinary user of a directory it has handed over, or finds already owned by the runner user from an earlier pod or the pruner's emptying, and cannot create the next subdirectory in it."""
    root = node_cache_path.rstrip("/")
    # One init container with DAC_OVERRIDE rather than two: a root container that only creates the directories, then a non-root one running as the runner user for the rest, would need no capability beyond CHOWN, but costs a container per capability and requires the image to run as a non-root user, which the digest-pinned busybox default does not guarantee. DAC_OVERRIDE is confined to a container that runs a fixed command on one mounted directory.
    # The mount root is the profile's own directory, created by the kubelet as root. The runner user owns it so that a job can recreate a subdirectory the pruner has set aside; this container recreates the capability's own subdirectories at the start of every pod.
    directories = [root] + [f"{root}/{subdirectory}" for subdirectory in capability["node_cache"]["dirs"].values()]
    return {
        "name": f"{_CAPABILITY_CONTAINER_PREFIX}{name}",
        "image": capability["node_cache"]["init_image"],
        # The image contract: a POSIX sh, mkdir and chown. chown is not recursive: what a pod creates under a directory is already the runner user's.
        "command": ["/bin/sh", "-c", 'owner=$1; shift; for directory in "$@"; do mkdir -p "$directory" && chown "$owner" "$directory"; done', "prepare-node-cache", f"{int(runner_user)}:{int(runner_group)}", *directories],
        "securityContext": {"runAsUser": 0, "runAsNonRoot": False, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "DAC_OVERRIDE"]}},
        "volumeMounts": [{"name": _NODE_CACHE_VOLUME, "mountPath": node_cache_path}],
    }


# --- The capability store: a shared copy per node of what every runner pod would otherwise copy at its start ---
# A pod's tool-cache and system-library copies (and the runner's externals for the dind sidecar) are the same bytes on every pod of a profile, taken from digest-pinned images. With a profile's capability_store on, each image's payload is copied into <store>/<kind>-<digest> (by every pod that starts before the entry is published, the first publish winning, and by none after it), where <digest> is the image's sha256 digest and <kind> the payload it holds (see arc_capability_store_key), and pods mount the store read-only instead of writing their own copy. The store root is a hostPath of its own, apart from the node cache, whose last component names it.
_CAPABILITY_STORE_HOST_PATH_NAME = re.compile(r"^(?:capability-store|[a-z0-9][a-z0-9-]*-capability-store)$")
# A copy is written under <store>/.incoming/<key>.<pod uid>.<container> and published with one rename onto <store>/<key>: rename(2) onto a non-empty directory fails, so of two pods copying the same image at once the first rename wins and the other's copy is deleted, and a reader never sees a part-written entry, since an entry exists only once it is complete. The pod uid comes from the downward API, which keeps one pod's copy apart from another's, and the container name only records which of the pod's copy containers wrote it.
_CAPABILITY_STORE_INCOMING = ".incoming"
_CAPABILITY_STORE_VOLUME = "capability-store"
_CAPABILITY_STORE_PREPARE = "capability-store-prepare"
_CAPABILITY_STORE_PUBLISH = "capability-store-publish"
_CAPABILITY_STORE_SYSROOT = "capability-store-sysroot"
_CAPABILITY_STORE_TOOL_CACHE = "capability-store-tool-cache"
_CAPABILITY_STORE_EXTERNALS_PUBLISH = "capability-store-publish-externals"
# Every init container the role names itself with the store, in contrast to the capability-<name> ones its capabilities get. A capability whose container would be named one of these is refused (see _capability_entry_errors), whether or not its profile uses the store, since a profile may switch it on later.
_CAPABILITY_STORE_CONTAINER_NAMES = frozenset({_CAPABILITY_STORE_PREPARE, _CAPABILITY_STORE_PUBLISH, _CAPABILITY_STORE_SYSROOT, _CAPABILITY_STORE_TOOL_CACHE, _CAPABILITY_STORE_EXTERNALS_PUBLISH})
# The kinds of payload an entry holds, the first part of its name (see arc_capability_store_key): the tool cache of a tool image, the sysroot of a system-library image and the runner's externals for the dind sidecar.
_STORE_KIND_TOOL_CACHE = "toolcache"
_STORE_KIND_SYSROOT = "sysroot"
_STORE_KIND_EXTERNALS = "externals"
_CAPABILITY_STORE_MERGED_PREFIX = "merged-sysroot-"
# The annotation that lists the store entries a pod uses, separated by spaces, which the node cache pruner reads so that it never removes an entry a pod on its node still uses.
_CAPABILITY_STORE_KEYS_ANNOTATION = "github-runner.exadev.io/capability-store-keys"
_IMAGE_DIGEST = re.compile(r"@sha256:([0-9a-f]{64})$")
_POD_UID_ENV = {"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}
# The chart's names for the runner externals copy the dind sidecar mounts (gha-runner-scale-set templates/_helpers.tpl: dind-init-container and dind-volume), which a values file that writes the dind containers out uses as the chart does.
_DIND_EXTERNALS_INIT = "init-dind-externals"
_DIND_EXTERNALS_VOLUME = "dind-externals"


def arc_capability_store_host_path_errors(host_path: Any) -> list[str]:
    """Return what is wrong with github_runner_arc_capability_store_host_path: the same rules as the node cache's root (an absolute canonical path at least a dedicated directory deep, not a system directory or inside one), with a last component named capability-store or ending in -capability-store, since the role creates it and the pruner removes entries from it as root."""
    if not isinstance(host_path, str) or not host_path.startswith("/") or any(character in host_path for character in ":\n\r"):
        return [f"the capability store's host path must be an absolute directory with no ':' or line break (got {host_path!r})"]
    if host_path != _canonical_path(host_path):
        return [f"the capability store's host path must not contain '//' or end with '/' (got {host_path!r}, which is {_canonical_path(host_path)!r} written plainly)"]
    components = host_path.split("/")[1:]
    if len(components) < _NODE_CACHE_HOST_PATH_MIN_COMPONENTS or ".." in components or "." in components:
        return [f"the capability store's host path must be a dedicated directory at least {_NODE_CACHE_HOST_PATH_MIN_COMPONENTS} levels deep with no '.' or '..' (such as /var/lib/github-runner/capability-store) (got {host_path!r})"]
    if not _CAPABILITY_STORE_HOST_PATH_NAME.match(components[-1]):
        return [f"the capability store's host path must end in a directory named capability-store, or a lower-case name ending in -capability-store (got {host_path!r}), since the role creates it and removes entries from it as root"]
    lowered = host_path.lower()
    inside = [denied for denied in _NODE_CACHE_DENIED_HOST_PATHS if lowered == denied or lowered.startswith(f"{denied}/")]
    return [f"the capability store's host path {host_path} is inside {inside[0]}, which the node itself uses"] if inside else []


def arc_host_path_overlap_errors(node_cache_host_path: Any, capability_store_host_path: Any) -> list[str]:
    """Return an error when github_runner_arc_node_cache_host_path and github_runner_arc_capability_store_host_path are the same directory or one is inside the other, and none otherwise.

    The pruner empties each as root and treats whatever it finds directly under a root as that root's own entries: a node cache inside the store is an unreferenced store entry and is deleted while pods use it, and a store inside the node cache is a profile directory the fleet no longer wants. A value that is not a string is reported by the setting's own check, so it is not compared here.
    """
    if not isinstance(node_cache_host_path, str) or not isinstance(capability_store_host_path, str):
        return []
    cache = _canonical_path(node_cache_host_path)
    store = _canonical_path(capability_store_host_path)
    if cache == store or cache.startswith(f"{store}/") or store.startswith(f"{cache}/"):
        return [f"github_runner_arc_node_cache_host_path ({node_cache_host_path}) and github_runner_arc_capability_store_host_path ({capability_store_host_path}) must be separate directories, neither inside the other, since the pruner removes what it finds directly under each as root"]
    return []


def arc_capability_store_path_errors(store_path: Any) -> list[str]:
    """Return what is wrong with github_runner_arc_capability_store_path, under which the runner container mounts each store entry it uses, read-only, and the init containers mount the store's root: empty when it is an absolute directory other than / with no colon or line break."""
    if not isinstance(store_path, str) or not store_path.startswith("/") or store_path == "/" or any(character in store_path for character in ":\n\r"):
        return [f"the capability store path must be an absolute directory other than / with no ':' or line break (got {store_path!r})"]
    return []


def arc_capability_store_image_errors(image: Any) -> list[str]:
    """Return what is wrong with github_runner_arc_capability_store_image: empty when it is pinned by digest (name@sha256:<64 hex digits>), as an image whose container runs as root on a hostPath must be."""
    if isinstance(image, str) and _IMAGE_DIGEST.search(image) and image.split("@")[0]:
        return []
    return [f"github_runner_arc_capability_store_image must be pinned by digest (name@sha256:<64 hex digits>), since its preparation container runs as root on a hostPath (got {image!r})"]


def arc_capability_store_key(image: str, kind: str) -> str | None:
    """Return the store entry name of the payload of kind that an image holds, <kind>-<sha256 digest in hex>, or None when the image is not pinned by digest and so has no name that changes exactly when its content does.

    The kind is part of the name because one image can be used for more than one payload (a runner image is both a tool image and the source of the dind sidecar's externals): the entry is a copy of one directory of the image, so two kinds of one digest are two entries, and neither can be taken for the other.
    """
    match = _IMAGE_DIGEST.search(image)
    return f"{kind}-{match.group(1)}" if match else None


def _capability_store_digest_errors(names: Sequence[str], catalogue: Mapping[str, Any]) -> list[str]:
    """Return one message for each of names that is a tool-cache or system-library capability whose image is not pinned by digest, which the capability store needs because it names the entry for the digest. A name the catalogue lacks, or an entry that is not a mapping or has no string image, is arc_capability_errors' to report."""
    errors: list[str] = []
    for name in names:
        capability = catalogue.get(name)
        if not isinstance(capability, Mapping):
            continue
        image = capability.get("image") if capability.get("image") is not None else capability.get("sysroot_image")
        if isinstance(image, str) and _IMAGE_DIGEST.search(image) is None:
            errors.append(f"capability {name}'s image must be pinned by digest (name@sha256:<64 hex digits>) for the capability store, since its store entry is named for the digest")
    return errors


def _capability_store_uses_entries(names: Sequence[str], catalogue: Mapping[str, Any]) -> bool:
    """Return whether any of names is a tool-cache or system-library capability, the ones the store holds a payload for."""
    return any(isinstance(catalogue.get(name), Mapping) and (catalogue[name].get("image") is not None or catalogue[name].get("sysroot_image") is not None) for name in names)


def _capability_store_mount_errors(store_path: Any, others: Mapping[str, str]) -> list[str]:
    """Return what is wrong with the store's mount path on the runner container: the rules of arc_capability_store_path_errors, or else that it overlaps one of the other mounted directories in others."""
    return arc_capability_store_path_errors(store_path) or _overlap_errors("capability store path", store_path, others)


def _capability_store_externals_image_errors(image: Any) -> list[str]:
    """Return what is wrong with the image the dind sidecar's externals are copied from, which the store names its entry for: empty when it is pinned by digest."""
    if arc_capability_store_key(str(image), _STORE_KIND_EXTERNALS) is None:
        return [f"the capability store needs {_DIND_EXTERNALS_INIT}'s image pinned by digest (name@sha256:<64 hex digits>), since its store entry is named for the digest (got {image!r})"]
    return []


def _capability_store_merged_key(keys: Sequence[str]) -> str:
    """Return the store entry name of the sysroot merged from keys in their order: a hash of the ordered keys, so a different set or order of system libraries is a different entry, since a later one's file overwrites an earlier one's. Its prefix is not a payload kind's, so it cannot be the name of an image's entry."""
    return _CAPABILITY_STORE_MERGED_PREFIX + hashlib.sha256(" ".join(keys).encode()).hexdigest()


def _capability_store_prepare_container(store: Mapping[str, Any], runner_user: int, runner_group: int) -> dict[str, Any]:
    """Return the init container that creates the store's root and its incoming directory and gives both to the runner's user, as the node cache's preparation does: as root with CHOWN, and DAC_OVERRIDE to create the incoming directory in a root already handed over."""
    root = store["path"].rstrip("/")
    return {
        "name": _CAPABILITY_STORE_PREPARE,
        "image": store["image"],
        "command": ["/bin/sh", "-c", 'owner=$1; shift; for directory in "$@"; do mkdir -p "$directory" && chown "$owner" "$directory"; done', "prepare-capability-store", f"{int(runner_user)}:{int(runner_group)}", root, f"{root}/{_CAPABILITY_STORE_INCOMING}"],
        "securityContext": {"runAsUser": 0, "runAsNonRoot": False, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"], "add": ["CHOWN", "DAC_OVERRIDE"]}},
        "volumeMounts": [{"name": _CAPABILITY_STORE_VOLUME, "mountPath": store["path"]}],
    }


def _runner_security_context(runner_user: int, runner_group: int) -> dict[str, Any]:
    """Return the securityContext of an init container that runs as the runner's user, as the per-pod copies do."""
    return {"runAsUser": int(runner_user), "runAsGroup": int(runner_group), "runAsNonRoot": True, "allowPrivilegeEscalation": False}


# Makes this pod's own incoming copy owner-writable, ahead of removing it (a copy's files are its own, unlike the sysroot merge's hard links: see _WRITABLE_MERGE_DIRECTORIES). A copy can hold directories without write permission (a Go module cache's are mode 0555), and rm -rf cannot empty such a directory as the non-root runner user, so the init container would fail on every kubelet retry and the pod never start. chmod -R does not follow symbolic links, on busybox or GNU, so it changes nothing outside the copy; its failure on a copy that does not exist is silenced, and a copy it could not make removable fails the rm -rf after it.
_MAKE_INCOMING_WRITABLE = 'chmod -R u+rwx "$incoming" 2>/dev/null;'

# Copies an image's payload to this pod's own incoming directory, unless the store already holds the entry. The destination is removed first so that a rerun after a copy killed part-way starts from nothing: cp -R onto an existing directory would nest the payload under it and keep the partial copy, and publishing would then rename that mixed tree. The copy's own top directory is then made owner-writable, since cp -R gives it the payload root's mode (0555 for a tool cache after chmod -R a-w) and renaming a directory into another parent needs write permission on the directory itself; only that directory, because the rename needs nothing below it.
_COPY_SCRIPT = (
    '[ -d "$1/$2" ] && exit 0; incoming="$1/' + _CAPABILITY_STORE_INCOMING + '/$2.$POD_UID.$4";'
    " " + _MAKE_INCOMING_WRITABLE + ' rm -rf "$incoming" && cp -R "$3" "$incoming" && chmod u+rwx "$incoming"'
)


def _capability_store_copy_container(name: str, image: str, payload: str, key: str, store: Mapping[str, Any], runner_user: int, runner_group: int) -> dict[str, Any]:
    """Return the init container that copies an image's payload into the store's incoming directory, unless the store already holds it. It runs the capability's own image, so it may use only what the image contract promises: a POSIX sh, its test builtin, chmod, rm and cp. It makes this pod's own destination owner-writable and removes it first, since the kubelet reruns a pod's init containers after a sandbox change and a copy killed part-way leaves the destination behind, and cp -R onto a directory that exists nests the payload inside it and keeps the partial first copy. cp -R onto a path that does not exist creates it as the copy, so no mkdir is needed. It then makes the copy's top directory owner-writable so that publishing can rename it whatever mode the payload's root has."""
    root = store["path"].rstrip("/")
    return {
        "name": name,
        "image": image,
        "command": ["/bin/sh", "-c", _COPY_SCRIPT, "copy-to-capability-store", root, key, payload, name],
        "env": [dict(_POD_UID_ENV)],
        "securityContext": _runner_security_context(runner_user, runner_group),
        "volumeMounts": [{"name": _CAPABILITY_STORE_VOLUME, "mountPath": store["path"]}],
    }


# Publishes each key's copies from this pod: a rename onto <store>/<key> for the first, and deletion for a copy whose key another pod published first. Then fails unless every key is in the store, so a pod never starts on an entry that is not there, saying why the rename failed when it did: mv's message is held back while the entry may be there (a lost race fails the rename too, and is no error) and printed only when it is not. Every copy is flushed to disk (sync) before its rename: a rename that reaches the disk ahead of the data it names leaves, after a power loss or crash, an entry that exists but holds empty files, and since an entry is named for an image digest and never revalidated, every later pod would use it. With the data written first, an entry that exists is complete whatever the crash, and a rename that did not reach the disk leaves no entry, which the next pod copies again. A completeness marker the readers check was left out: it would restate what the order of writes already guarantees, and cost every pod a check at its start. That sync is sync(2), which writes out every dirty page on the host and not only this copy's, and waits for it: busybox's sync takes no argument (given one it prints a warning and ignores it, so it cannot be scoped to the store's filesystem), and its fsync applet syncs one file or directory per call, which for a tree of many small files is one journal commit each where sync is one pass over the dirty pages, so scoping the flush would cost more work than it saves. The host-wide flush is accepted because it runs only in a pod that copied the entry, which is every pod that starts before the entry is published and none after it (a pod that finds the entry in the store copies and flushes nothing). If the store image's busybox ever gains a scoped sync (sync -f on a path), use it here and in the sysroot merge.
_PUBLISH_SCRIPT = (
    'root=$1; shift; for key in "$@"; do moved=;'
    ' for incoming in "$root/' + _CAPABILITY_STORE_INCOMING + '/$key.$POD_UID".*; do [ -e "$incoming" ] || continue; sync || exit 1; moved=$(mv -T "$incoming" "$root/$key" 2>&1) || { ' + _MAKE_INCOMING_WRITABLE + ' rm -rf "$incoming"; }; done;'
    ' [ -d "$root/$key" ] || { echo "capability store: $key is not in $root after publishing${moved:+: $moved}" >&2; exit 1; };'
    ' done'
)


def _capability_store_publish_container(name: str, keys: Sequence[str], store: Mapping[str, Any], runner_user: int, runner_group: int) -> dict[str, Any]:
    """Return the init container that publishes this pod's copies of keys into the store with the store image (busybox), whose mv has -T."""
    return {
        "name": name,
        "image": store["image"],
        "command": ["/bin/sh", "-c", _PUBLISH_SCRIPT, "publish-capability-store", store["path"].rstrip("/"), *keys],
        "env": [dict(_POD_UID_ENV)],
        "securityContext": _runner_security_context(runner_user, runner_group),
        "volumeMounts": [{"name": _CAPABILITY_STORE_VOLUME, "mountPath": store["path"]}],
    }


# Makes every directory of the merged sysroot (and nothing else) owner-writable. The merge's files are hard links to the store entries' own, so chmod -R would change the entries' file modes, while the directories are created by the merge and are its own: each takes its mode from the entry's (cp -a), and one of mode 0555 would refuse the next entry's files, the rename into the store and rm -rf alike.
_WRITABLE_MERGE_DIRECTORIES = 'find "$incoming" -type d -exec chmod u+rwx {} +'

# Builds the merged sysroot from the system-library entries in the profile's order, as hard links (cp -l) so it costs no data, later entries' files replacing earlier ones' (cp -f) as the per-pod copies did, then flushes (with the host-wide sync described at _PUBLISH_SCRIPT) and publishes it like any other entry (reporting a rename that failed for another reason than a lost race, as _PUBLISH_SCRIPT does), rebuilding from nothing when this pod's own earlier partial merge is found.
_SYSROOT_MERGE_SCRIPT = (
    'root=$1; merged=$2; shift 2; [ -d "$root/$merged" ] && exit 0;'
    ' incoming="$root/' + _CAPABILITY_STORE_INCOMING + '/$merged.$POD_UID.merge"; ' + _WRITABLE_MERGE_DIRECTORIES + ' 2>/dev/null; rm -rf "$incoming" && mkdir "$incoming" || exit 1;'
    ' for key in "$@"; do cp -alf "$root/$key/." "$incoming/" && ' + _WRITABLE_MERGE_DIRECTORIES + ' || exit 1; done;'
    ' sync || exit 1; moved=$(mv -T "$incoming" "$root/$merged" 2>&1) || { ' + _WRITABLE_MERGE_DIRECTORIES + ' 2>/dev/null; rm -rf "$incoming"; };'
    ' [ -d "$root/$merged" ] || { echo "capability store: $merged is not in $root after publishing${moved:+: $moved}" >&2; exit 1; }'
)

# Fills the pod's own tool cache from the store so that a job can still change its tools: every tool tree is rebuilt in the pod as real directories holding a symlink to each store file, so a job can create, replace and remove files and directories in a tool's own tree while the store stays untouched (a store file cannot be written through its link, since the store is mounted read-only). Two kinds of store entry are copied into the pod instead of linked: a symlink, so that its relative target resolves inside the pod's tree and not the store's, and each regular file directly in <arch>/bin, since a launcher such as node, python or go finds its own tree (npm's global prefix, a Python's site-packages) from its resolved executable path, which a symlink would put in the read-only store. A tool or version directory that is itself a symbolic link (an image aliasing node and nodejs, or 1.0 and 1.0.0) is copied as the link too, and nothing below it is walked, since the glob would otherwise reach the real tree through it and rebuild it as a second real tree of its own, in which a job's write through one name would not appear through the other. An <arch> that is itself a symbolic link (an image aliasing x64 and amd64) is copied as the link, as an entry inside the tree is, and never linked to the store's link: the link's relative target then names the real directory the pod's tree holds beside it, which the same loop links, and no command ever runs on a path that resolves into the store (cp -as of a link to a link would make the pod's <arch> a link to the store's, which a chmod -R follows from the command line into the read-only store and fails on). The <arch>.complete marker, which is how the Actions tool cache finds a tool, is copied as well, for an alias too, with cp -f since it keeps the store file's mode and a rerun over a read-only copy must still replace it. cp -as gives each directory the store directory's mode, and a store tree can hold directories without write permission (a Go module cache's are mode 0555), in which a job could create nothing and the rm -f of a copied entry fails as the non-root runner user, so every directory of the pod's tree is made owner-writable (chmod -R u+rwx, which does not follow symbolic links, so it changes only the pod's directories) after it is built and, for a rerun after a run killed between building and chmod, before the rebuild as well. cp -asf, cp -f, chmod -R, find -exec ... + and cp -P are busybox's and GNU's alike, and rerunning the script over a filled cache converges on the same tree.
_TOOL_CACHE_COPY_SCRIPT = 'source=$1; destination=$2; shift 2; for entry; do target=$destination/${entry#"$source"/}; rm -f "$target" && cp -P "$entry" "$target" || exit 1; done'
_TOOL_CACHE_LINK_SCRIPT = (
    'root=$1; cache=$2; shift 2; for key in "$@"; do'
    ' for link in "$root/$key"/* "$root/$key"/*/*; do'
    ' [ -L "$link" ] || continue; relative=${link#"$root/$key/"}; parent=$cache;'
    ' case $relative in */*) [ ! -L "$root/$key/${relative%%/*}" ] || continue; parent="$cache/${relative%/*}";; esac;'
    ' mkdir -p "$parent" && rm -f "$cache/$relative" && cp -P "$link" "$cache/$relative" || exit 1;'
    ' done;'
    ' for arch in "$root/$key"/*/*/*; do'
    ' [ -d "$arch" ] || continue; relative=${arch#"$root/$key/"}; parent="$cache/${relative%/*}"; tree="$cache/$relative"; tool=${relative%%/*}; version=${relative#*/}; version=${version%%/*};'
    ' [ ! -L "$root/$key/$tool" ] && [ ! -L "$root/$key/$tool/$version" ] || continue;'
    ' mkdir -p "$parent" || exit 1;'
    ' if [ -L "$arch" ]; then rm -f "$tree" && cp -P "$arch" "$tree" || exit 1; else'
    ' chmod -R u+rwx "$tree" 2>/dev/null;'
    ' cp -asf "$arch" "$parent/" && chmod -R u+rwx "$tree" || exit 1;'
    " find \"$arch\" -type l -exec sh -c '" + _TOOL_CACHE_COPY_SCRIPT + "' copy-from-capability-store \"$arch\" \"$tree\" {} + || exit 1;"
    " [ ! -d \"$arch/bin\" ] || find \"$arch/bin\" -maxdepth 1 -type f -exec sh -c '" + _TOOL_CACHE_COPY_SCRIPT + "' copy-from-capability-store \"$arch\" \"$tree\" {} + || exit 1;"
    ' fi;'
    ' [ ! -e "$arch.complete" ] || cp -f "$arch.complete" "$cache/$relative.complete" || exit 1;'
    ' done; done'
)


def _capability_store_volume(store: Mapping[str, Any]) -> dict[str, Any]:
    return {"name": _CAPABILITY_STORE_VOLUME, "hostPath": {"path": store["host_path"], "type": "DirectoryOrCreate"}}


def _annotate_store_keys(template: dict[str, Any], keys: Sequence[str]) -> None:
    """Add keys to the pod template's list of the store entries its pods use, keeping any already there."""
    annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
    existing = annotations.get(_CAPABILITY_STORE_KEYS_ANNOTATION, "").split()
    annotations[_CAPABILITY_STORE_KEYS_ANNOTATION] = " ".join(dict.fromkeys([*existing, *keys]))


def _ensure_store_prepared(spec: dict[str, Any], store: Mapping[str, Any], runner_user: int, runner_group: int) -> None:
    """Add the store's volume to spec unless it has it, and make the store's preparation spec's first init container, moving it there when the capabilities put it after the values' own init containers, since every copy into the store needs the directories it creates."""
    if not any(volume.get("name") == _CAPABILITY_STORE_VOLUME for volume in spec.get("volumes") or []):
        spec["volumes"] = list(spec.get("volumes") or []) + [_capability_store_volume(store)]
    others = [container for container in spec.get("initContainers") or [] if container.get("name") != _CAPABILITY_STORE_PREPARE]
    spec["initContainers"] = [_capability_store_prepare_container(store, runner_user, runner_group), *others]


class _StoreInit(NamedTuple):
    """What a pod's store init containers leave: the containers, every entry the pod uses (the annotation's list), the tool-cache entries its tool tree links into (the only entries the runner container mounts besides the merged sysroot), and the merged sysroot's entry (None without a system library)."""

    containers: list[dict[str, Any]]
    keys: list[str]
    tool_keys: list[str]
    merged: str | None


def _capability_store_init_containers(names: Sequence[str], catalogue: Mapping[str, Any], tool_cache_path: str, store: Mapping[str, Any], runner_user: int, runner_group: int) -> _StoreInit:
    """Return the init containers that put a profile's tool-cache and system-library payloads in the store and give the pod its view of them, with the store entries the pod uses."""
    root = store["path"].rstrip("/")
    copies: list[dict[str, Any]] = []
    copied: set[str] = set()
    tool_keys: list[str] = []
    sysroot_keys: list[str] = []
    for name in names:
        capability = catalogue[name]
        image = capability.get("image") or capability.get("sysroot_image")
        if image is None:
            continue
        is_tool = capability.get("image") is not None
        key = arc_capability_store_key(image, _STORE_KIND_TOOL_CACHE if is_tool else _STORE_KIND_SYSROOT)
        assert key is not None
        payload = _TOOL_IMAGE_PAYLOAD if is_tool else _SYSROOT_IMAGE_PAYLOAD
        # Two capabilities on one image name one entry, so only the first copies it: a second copy would write the same bytes again to be deleted at publishing.
        if key not in copied:
            copied.add(key)
            copies.append(_capability_store_copy_container(f"{_CAPABILITY_CONTAINER_PREFIX}{name}", image, payload, key, store, runner_user, runner_group))
        (tool_keys if is_tool else sysroot_keys).append(key)
    # Two capabilities may name one image, and the merge links each entry's files into the merged directory, so an entry named twice would be linked over itself (cp reports the same file). Each entry stays at its last position, where its files win over the earlier entries' as they did when every capability was copied into the pod in order, so the merged entry's name, a hash of the order, is that of the order that merges the same files.
    sysroot_keys = list(reversed(list(dict.fromkeys(reversed(sysroot_keys)))))
    keys = list(dict.fromkeys([*tool_keys, *sysroot_keys]))
    containers = copies + [_capability_store_publish_container(_CAPABILITY_STORE_PUBLISH, keys, store, runner_user, runner_group)]
    merged = _capability_store_merged_key(sysroot_keys) if sysroot_keys else None
    if merged is not None:
        containers.append({
            "name": _CAPABILITY_STORE_SYSROOT,
            "image": store["image"],
            "command": ["/bin/sh", "-c", _SYSROOT_MERGE_SCRIPT, "merge-capability-store-sysroot", root, merged, *sysroot_keys],
            "env": [dict(_POD_UID_ENV)],
            "securityContext": _runner_security_context(runner_user, runner_group),
            "volumeMounts": [{"name": _CAPABILITY_STORE_VOLUME, "mountPath": store["path"]}],
        })
    if tool_keys:
        containers.append({
            "name": _CAPABILITY_STORE_TOOL_CACHE,
            "image": store["image"],
            "command": ["/bin/sh", "-c", _TOOL_CACHE_LINK_SCRIPT, "link-capability-store-tool-cache", root, tool_cache_path, *dict.fromkeys(tool_keys)],
            "securityContext": _runner_security_context(runner_user, runner_group),
            "volumeMounts": [{"name": _CAPABILITY_STORE_VOLUME, "mountPath": store["path"], "readOnly": True}, {"name": _TOOL_CACHE_VOLUME, "mountPath": tool_cache_path}],
        })
    return _StoreInit(containers, keys + ([merged] if merged is not None else []), list(dict.fromkeys(tool_keys)), merged)


def arc_capability_store_errors(profiles: Sequence[Mapping[str, Any]], catalogue: Mapping[str, Any], store_path: Any, tool_cache_path: str, sysroot_path: str, hooks_dir: str, node_cache_path: str, registry_cache_enabled: bool) -> list[str]:
    """Check what a profile with capability_store needs of the role's settings, before anything is installed: the checks arc_capability_pod and arc_capability_store_externals make when they render, so that validation fails on them and not the install, which has by then applied the controller, the pruner and earlier profiles.

    Args:
        profiles: expanded profiles from arc_profiles. catalogue: github_runner_arc_capabilities, already checked by arc_capability_errors. store_path: where the store's root is mounted in the init containers, and under which the runner container mounts each entry it uses. tool_cache_path, sysroot_path, hooks_dir, node_cache_path: where the role mounts the tool cache, the sysroot, the job-started hooks and the node cache, which the store's path may not equal, contain or sit inside. registry_cache_enabled: whether the role writes the registry cache into the values, which writes the dind sidecar out with its externals copy for a profile with container_mode dind.

    Returns:
        The problems found, empty when there are none: for a profile that holds a tool-cache or system-library payload in the store, a store path that is not a directory of its own, and, for a profile in dind mode with the registry cache, a runner image not pinned by digest, since the externals copy's entry is named for it. A values file's own externals copy is checked when it is rendered, since its content is not known here.
    """
    errors: list[str] = []
    for profile in profiles:
        if not profile["capability_store"]:
            continue
        messages: list[str] = []
        if _capability_store_uses_entries(profile["capabilities"], catalogue):
            messages.extend(_capability_store_mount_errors(store_path, {"tool cache path": tool_cache_path, "sysroot path": sysroot_path, "job-started hooks directory": hooks_dir, "node cache path": node_cache_path}))
        if registry_cache_enabled and not profile["values_file"] and profile["settings"].get("container_mode") == "dind":
            messages.extend(_capability_store_externals_image_errors(profile["runner_image"]))
        errors.extend(f"{profile['label']}: {message}" for message in messages)
    return errors


def arc_capability_store_externals(values: Mapping[str, Any], store: Mapping[str, Any] | None, runner_user: int, runner_group: int) -> dict[str, Any]:
    """Move a scale set's copy of the runner's externals for the dind sidecar into the capability store.

    Applies to values that write the dind containers out (a values file, or the role's registry cache wiring), whose init container named init-dind-externals copies the runner image's externals into the volume named dind-externals, as the chart's own dind template does. That copy becomes a store entry named for the image's digest and the kind externals, published like a capability's, and every container that mounted the volume (the dind container, whether it is a container or a native sidecar under initContainers) mounts it read-only through a subPath of the store instead. Values without that init container, or a profile without the store, are returned as given.

    Args:
        values: the release's rendered Helm values. store: the profile's capability store settings ({host_path, path, image}), or None when the profile does not use the store. runner_user, runner_group: the runner's user and group ids, which the copy and publish containers run as.

    Returns:
        A dict with ``errors`` (empty when the values can use the store) and ``values`` (the values with the copy moved, or as given).
    """
    if store is None:
        return {"errors": [], "values": values}
    result = copy.deepcopy(dict(values))
    template = result.setdefault("template", {})
    spec = template.setdefault("spec", {})
    init_containers = list(spec.get("initContainers") or [])
    copier = next((container for container in init_containers if container.get("name") == _DIND_EXTERNALS_INIT), None)
    if copier is None:
        return {"errors": [], "values": values}
    image_errors = _capability_store_externals_image_errors(copier.get("image", ""))
    if image_errors:
        return {"errors": image_errors, "values": values}
    key = arc_capability_store_key(copier["image"], _STORE_KIND_EXTERNALS)
    assert key is not None
    index = init_containers.index(copier)
    replacement = [
        _capability_store_copy_container(_DIND_EXTERNALS_INIT, copier["image"], _RUNNER_EXTERNALS_DIR, key, store, runner_user, runner_group),
        _capability_store_publish_container(_CAPABILITY_STORE_EXTERNALS_PUBLISH, [key], store, runner_user, runner_group),
    ]
    init_containers[index:index + 1] = replacement
    spec["volumes"] = [volume for volume in spec.get("volumes") or [] if volume.get("name") != _DIND_EXTERNALS_VOLUME]
    # The dind container is an ordinary container in the chart's layout before Kubernetes 1.29 and an init container with restartPolicy Always (a native sidecar) in the layout after it, which is also what the registry cache writes out, so both lists are rewritten. The copy and publish containers just inserted mount the store alone, so they carry no mount of the removed volume to rewrite.
    for container in [*(spec.get("containers") or []), *init_containers]:
        if "volumeMounts" in container:
            container["volumeMounts"] = [
                {"name": _CAPABILITY_STORE_VOLUME, "mountPath": mount["mountPath"], "subPath": key, "readOnly": True} if mount.get("name") == _DIND_EXTERNALS_VOLUME else mount
                for mount in container["volumeMounts"]
            ]
    spec["initContainers"] = init_containers
    _ensure_store_prepared(spec, store, runner_user, runner_group)
    _annotate_store_keys(template, [key])
    return {"errors": [], "values": result}


def arc_capability_pod(values: Mapping[str, Any], names: Sequence[str], catalogue: Mapping[str, Any], tool_cache_path: str, sysroot_path: str, node_cache_path: str, node_cache_host_path: str, runner_user: int, runner_group: int, hooks_dir: str, hooks_configmap: str, store: Mapping[str, Any] | None) -> dict[str, Any]:
    """Add a profile's capabilities to its scale set's Helm values.

    Each tool-cache capability becomes an init container, running its image as the runner's user, that copies the image's /toolcache payload into an emptyDir mounted on the runner container at tool_cache_path, which RUNNER_TOOL_CACHE names; the files then belong to the runner user, so a setup action can still add a version beside them. Each system-library capability likewise becomes an init container that copies the image's /sysroot payload into an emptyDir mounted at sysroot_path; several share it, later ones overwriting a file an earlier one also wrote. Hook capabilities, the path of any tool-cache capability that sets one, and the sysroot's environment (its search paths and the capabilities' env) go into a ConfigMap mounted at hooks_dir, whose dispatcher ACTIONS_RUNNER_HOOK_JOB_STARTED names. Everything is appended after what the values already hold, so a values file's own init containers, volumes, environment and mounts are kept. Lists are replaced wholesale when Helm merges values, which is why this edits the merged values rather than adding an overlay.

    Args:
        values: the release's merged Helm values (the profile's values with the role's overlay over them). names: the profile's capabilities, in order; the init containers and hook scripts run in this order. catalogue: github_runner_arc_capabilities, already checked by arc_capability_errors. tool_cache_path: where the tool cache is mounted on the runner container. sysroot_path: where the sysroot is mounted on the runner container. runner_user, runner_group: the runner container's user and group ids, which the init containers run as. hooks_dir: where the job-started ConfigMap is mounted on the runner container. hooks_configmap: the job-started ConfigMap's name, in the profile's namespace. store: the profile's capability store settings ({host_path, path, image}: the store's root on the node, where it is mounted in the pod, and the digest-pinned busybox image that publishes and links its entries), or None when the profile copies into each pod. With the store, each tool-cache and system-library payload is copied into it, to be shared by the pods of a node, instead of into each pod (see arc_capability_store_externals for the store's layout): the tool cache stays the pod's own emptyDir, rebuilt as real directories holding links to the store's files (see _TOOL_CACHE_LINK_SCRIPT), the sysroot is the store's merged entry mounted read-only, the runner container mounts, read-only, only the tool-cache entries the tool tree links into (each at <store path>/<key>) and that merged entry, never the store's root, and the pod template is annotated with the entries it uses.

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
    node_cache_capabilities = [(name, catalogue[name]) for name in names if catalogue[name].get("node_cache") is not None]
    if sysroot_capabilities:
        errors.extend(_sysroot_path_errors(sysroot_path, {"tool cache path": tool_cache_path, "job-started hooks directory": hooks_dir}))
    if node_cache_capabilities:
        errors.extend(_node_cache_path_errors(node_cache_path, node_cache_host_path, {"tool cache path": tool_cache_path, "sysroot path": sysroot_path, "job-started hooks directory": hooks_dir}))
    if store is not None and _capability_store_uses_entries(names, catalogue):
        errors.extend(_capability_store_mount_errors(store["path"], {"tool cache path": tool_cache_path, "sysroot path": sysroot_path, "job-started hooks directory": hooks_dir, "node cache path": node_cache_path}))
        errors.extend(_capability_store_digest_errors(names, catalogue))
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
    use_store = store is not None and bool(tool_capabilities or sysroot_capabilities)
    if use_store:
        assert store is not None
        volumes.append(_capability_store_volume(store))
    elif sysroot_capabilities:
        volumes.append({"name": _SYSROOT_VOLUME, "emptyDir": {}})
        mounts.append({"name": _SYSROOT_VOLUME, "mountPath": sysroot_path})
    if node_cache_capabilities:
        volumes.append({"name": _NODE_CACHE_VOLUME, "hostPath": {"path": node_cache_host_path, "type": "DirectoryOrCreate"}})
        mounts.append({"name": _NODE_CACHE_VOLUME, "mountPath": node_cache_path})
        env.extend(_node_cache_env(node_cache_path, node_cache_capabilities, errors))
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
    if use_store:
        assert store is not None
        init = _capability_store_init_containers(names, catalogue, tool_cache_path, store, runner_user, runner_group)
        # The runner container, which runs a job's steps, sees only the entries its own pod uses, each at <store>/<key> (where the tool tree's links point) and the merged sysroot at its own path, never the store's root: a job could otherwise read every other profile's payloads on its node. The init containers mount the root, since they copy and publish into it before any job runs.
        mounts.extend({"name": _CAPABILITY_STORE_VOLUME, "mountPath": f"{store['path'].rstrip('/')}/{key}", "subPath": key, "readOnly": True} for key in init.tool_keys)
        if init.merged is not None:
            mounts.append({"name": _CAPABILITY_STORE_VOLUME, "mountPath": sysroot_path, "subPath": init.merged, "readOnly": True})
        init_containers = [_capability_store_prepare_container(store, runner_user, runner_group), *init.containers] + [
            _node_cache_init_container(name, catalogue[name], node_cache_path, runner_user, runner_group) for name in names if catalogue[name].get("node_cache") is not None
        ]
        _annotate_store_keys(result.setdefault("template", {}), init.keys)
        spec["initContainers"] = list(spec.get("initContainers") or []) + init_containers
        spec["volumes"] = list(spec.get("volumes") or []) + volumes
        runner["env"] = list(runner.get("env") or []) + env
        runner["volumeMounts"] = list(runner.get("volumeMounts") or []) + mounts
        return {"errors": [], "values": result, "hooks": hooks}
    # In the profile's order, so a later system-library capability's files overwrite an earlier one's.
    init_containers = [
        _copy_init_container(name, catalogue[name]["image"], _TOOL_IMAGE_PAYLOAD, _TOOL_CACHE_VOLUME, tool_cache_path, runner_user, runner_group)
        if catalogue[name].get("image") is not None
        else _copy_init_container(name, catalogue[name]["sysroot_image"], _SYSROOT_IMAGE_PAYLOAD, _SYSROOT_VOLUME, sysroot_path, runner_user, runner_group)
        if catalogue[name].get("sysroot_image") is not None
        else _node_cache_init_container(name, catalogue[name], node_cache_path, runner_user, runner_group)
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


def arc_helm_server_side(version: str) -> bool:
    """Report whether the Helm client applies releases with server-side apply, which decides whether the role forces field conflicts.

    Helm 4 upgrades a release with server-side apply when it was installed that way (its default), and fails on any field another field manager has since set to a different value, such as a scale set's maxRunners after the autoscaler has patched it. Helm 3 has no server-side apply; below Helm 4 the helm module refuses server_side in any form and force_conflicts when it is true, so the role passes neither there.

    Args:
        version: the client version as `helm version --short` prints it, for example v4.3.0+gbec5b06 or v3.16.2+g13654a5.

    Returns:
        True for Helm 4 or later, False for an earlier major version.

    Raises:
        ValueError: when the version has no leading major number.
    """
    match = re.match(r"^v?(\d+)\.", str(version).strip())
    if match is None:
        raise ValueError(f"cannot read a Helm major version from {version!r}")
    return int(match.group(1)) >= 4


class FilterModule:
    """Registers the ARC role's filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {
            "arc_profiles": arc_profiles,
            "arc_cluster_profiles": arc_cluster_profiles,
            "arc_max_runners": arc_max_runners,
            "arc_measured_max_runners": arc_measured_max_runners,
            "arc_upgrade_blocker": arc_upgrade_blocker,
            "arc_helm_server_side": arc_helm_server_side,
            "arc_image_ref": arc_image_ref,
            "arc_runner_placement": arc_runner_placement,
            "arc_capability_errors": arc_capability_errors,
            "arc_capability_pod": arc_capability_pod,
            "arc_capability_store_errors": arc_capability_store_errors,
            "arc_capability_store_externals": arc_capability_store_externals,
            "arc_capability_store_host_path_errors": arc_capability_store_host_path_errors,
            "arc_capability_store_path_errors": arc_capability_store_path_errors,
            "arc_capability_store_image_errors": arc_capability_store_image_errors,
            "arc_capability_store_namespaces": arc_capability_store_namespaces,
            "arc_host_path_overlap_errors": arc_host_path_overlap_errors,
            "arc_node_cache_profiles": arc_node_cache_profiles,
            "arc_node_cache_host_path_errors": arc_node_cache_host_path_errors,
            "arc_node_cache_root": arc_node_cache_root,
            "arc_node_cache_access_to_remove": arc_node_cache_access_to_remove,
            "arc_terminating_namespaces": arc_terminating_namespaces,
            "arc_node_cache_legacy_access": arc_node_cache_legacy_access,
            "arc_label_selectors": arc_label_selectors,
            "arc_bounded_integer_errors": arc_bounded_integer_errors,
            "arc_node_cache_dir": arc_node_cache_dir,
            "arc_node_cache_path_errors": arc_node_cache_path_errors,
            "arc_registry_cache": arc_registry_cache,
            "arc_registry_cache_pod": arc_registry_cache_pod,
            "arc_registry_cache_configmap": arc_registry_cache_configmap,
            "arc_registry_cache_secret_problems": arc_registry_cache_secret_problems,
        }
