"""Filters for the github_runner_arc role: expanding orgs into scale-set profiles, deriving a runner ceiling from node capacity (uniform figures, or each node's measured free capacity), and splitting an image reference."""

from __future__ import annotations

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
    # The static maxRunners the role sets on the release: the profile's own max_runners wins; otherwise sizing supplies it, except on the autoscaled profile, whose sizing sets the autoscaler's ceiling rather than its floor.
    if explicit_max_runners is not None:
        max_runners = explicit_max_runners
    elif sizing_result is not None and not sizing_result["errors"] and not autoscale:
        max_runners = sizing_result["max_runners"]
    else:
        max_runners = None
    expanded = {
        "org_name": org_name,
        "suffix": suffix,
        "namespace": _optional_name(profile, "namespace", where, errors) or f"arc-runners-{org_lower}{suffix}",
        "release": _optional_name(profile, "release_name", where, errors) or f"{org_lower}-runners{suffix}",
        "app_secret": app_secret,
        "scale_set_labels": _scale_set_labels(profile, f"{org_lower}-runners", where, errors),
        "values_file": _text(profile, "values_file"),
        "node_selector": dict(node_selector),
        "autoscale": autoscale,
        "sizing": sizing_result,
        "max_runners": max_runners,
        "label": f"{org_name}{suffix}",
        "settings": dict(profile),
    }
    # The role's own values template needs a static maxRunners: max_runners, or sizing on a profile the autoscaler does not manage (the autoscaled profile's sizing sets the autoscaler's ceiling, not its floor).
    if not expanded["values_file"] and profile.get("max_runners") is None and (sizing_result is None or autoscale):
        errors.append(f"{where}: a profile with no values_file needs max_runners{'' if autoscale else ' or sizing'}")
    _check_dns_label("namespace", expanded["namespace"], where, errors)
    _check_dns_label("release name", expanded["release"], where, errors)
    return expanded


def arc_profiles(orgs: Sequence[Any]) -> dict[str, Any]:
    """Expand github_runner_arc_orgs entries into one record per scale-set profile, and check them.

    Pass every org configured anywhere in the inventory, not one host's, so that the cross-host checks (an org configured twice, two profiles sharing a namespace, more than one autoscaled profile) see the whole fleet.

    Args:
        orgs: github_runner_arc_orgs entries, each with name, app_id, image and a non-empty scale_set_profiles list, at most one of private_key and private_key_op_reference, and optionally app_secret_name. A profile may override its namespace and release_name, and set scale_set_labels in place of runs_on_label.

    Returns:
        A dict with ``errors`` (messages, empty when valid), ``profiles`` (one dict per profile with org_name, suffix, namespace, release, app_secret, scale_set_labels, values_file, node_selector, autoscale, sizing, max_runners (the static maxRunners the role sets, or None), label and settings, the profile's own keys) and ``autoscaled`` (the one profile flagged autoscale, or None).
    """
    errors: list[str] = []
    profiles: list[dict[str, Any]] = []
    if not isinstance(orgs, Sequence) or isinstance(orgs, (str, bytes)):
        return {"errors": ["github_runner_arc_orgs must be a list"], "profiles": [], "autoscaled": None}
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
        for key in ("app_id", "image"):
            if not _text(org, key):
                errors.append(f"{org_name}: {key} is required")
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
    autoscaled = [profile for profile in profiles if profile["autoscale"]]
    if len(autoscaled) > 1:
        errors.append(f"At most one scale-set profile may set autoscale: true, because the autoscaler budgets one pool's memory for one scale set; found {', '.join(profile['label'] for profile in autoscaled)}")
    return {"errors": errors, "profiles": profiles, "autoscaled": autoscaled[0] if len(autoscaled) == 1 else None}


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


class FilterModule:
    """Registers the ARC role's filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {"arc_profiles": arc_profiles, "arc_max_runners": arc_max_runners, "arc_measured_max_runners": arc_measured_max_runners, "arc_image_ref": arc_image_ref}
