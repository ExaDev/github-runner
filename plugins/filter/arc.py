"""Filters for the github_runner_arc role: expanding orgs into scale-set profiles, deriving a runner ceiling from node capacity, and splitting an image reference."""

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


def arc_max_runners(sizing: Mapping[str, Any]) -> dict[str, Any]:
    """Derive how many runner pods a pool of identical nodes can hold.

    Each node's allocatable CPU and memory, less a baseline fraction reserved for what already runs there, is divided by one runner pod's requests (the runner container plus any sidecar such as dind). The tighter of the two bounds gives pods per node; that times the node count, scaled down by the safety margin, is the result. Decimal arithmetic keeps the floors exact for inputs such as 0.1.

    Args:
        sizing: the inputs named in _SIZING_INPUTS. Optional ones default to no sidecar, no baseline reservation and no safety margin.

    Returns:
        A dict with ``errors`` (a list of messages, empty when the inputs are valid) and, when valid, ``max_runners`` plus every intermediate figure (``usable_cpu``, ``usable_memory_gib``, ``pod_cpu``, ``pod_memory_gib``, ``per_node_by_cpu``, ``per_node_by_memory``, ``per_node``, ``theoretical``). ``max_runners`` is None when there are errors.
    """
    errors: list[str] = []
    if not isinstance(sizing, Mapping):
        return {"errors": ["sizing must be a mapping"], "max_runners": None}
    known = {spec[0] for spec in _SIZING_INPUTS}
    errors.extend(f"sizing has an unknown key '{key}'" for key in sorted(set(sizing) - known))
    values: dict[str, Decimal] = {}
    for key, required, default, low, low_inclusive, high, high_inclusive in _SIZING_INPUTS:
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
    if "node_count" in values and values["node_count"] != values["node_count"].to_integral_value():
        errors.append(f"sizing.node_count must be a whole number (got {values['node_count']})")
    if errors:
        return {"errors": errors, "max_runners": None}

    usable_cpu = values["node_allocatable_cpu"] * (1 - values["baseline_cpu_fraction"])
    usable_memory = values["node_allocatable_memory_gib"] * (1 - values["baseline_memory_fraction"])
    pod_cpu = values["pod_cpu_request"] + values["sidecar_cpu_request"]
    pod_memory = values["pod_memory_request_gib"] + values["sidecar_memory_request_gib"]
    per_node_by_cpu = _floor(usable_cpu / pod_cpu)
    per_node_by_memory = _floor(usable_memory / pod_memory)
    per_node = min(per_node_by_cpu, per_node_by_memory)
    theoretical = per_node * int(values["node_count"])
    return {
        "errors": [],
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


def _text(mapping: Mapping[str, Any], key: str) -> str:
    """Return a string field, treating an unset or null value as empty."""
    value = mapping.get(key)
    return "" if value is None else str(value)


def _check_dns_label(kind: str, name: str, where: str, errors: list[str]) -> None:
    """Record an error when name is not a valid Kubernetes DNS label."""
    if len(name) > _DNS_LABEL_MAX_LENGTH or not _DNS_LABEL.match(name):
        errors.append(f"{where}: the derived {kind} '{name}' is not a valid Kubernetes name (lower-case letters, digits and '-', at most {_DNS_LABEL_MAX_LENGTH} characters)")


def _expand_profile(org: Mapping[str, Any], org_name: str, index: int, profile: Any, errors: list[str]) -> dict[str, Any] | None:
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
    expanded = {
        "org_name": org_name,
        "suffix": suffix,
        "namespace": f"arc-runners-{org_lower}{suffix}",
        "release": f"{org_lower}-runners{suffix}",
        "app_secret": f"{org_lower}-github-app",
        "runs_on_label": _text(profile, "runs_on_label") or f"{org_lower}-runners",
        "values_file": _text(profile, "values_file"),
        "node_selector": dict(node_selector),
        "autoscale": autoscale,
        "sizing": sizing_result,
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
        orgs: github_runner_arc_orgs entries, each with name, app_id, image and a non-empty scale_set_profiles list, and at most one of private_key and private_key_op_reference.

    Returns:
        A dict with ``errors`` (messages, empty when valid), ``profiles`` (one dict per profile with org_name, suffix, namespace, release, app_secret, runs_on_label, values_file, node_selector, autoscale, sizing, label and settings, the profile's own keys) and ``autoscaled`` (the one profile flagged autoscale, or None).
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
        for profile_index, profile in enumerate(org_profiles):
            expanded = _expand_profile(org, org_name, profile_index, profile, errors)
            if expanded is not None:
                profiles.append(expanded)
    namespaces: dict[str, str] = {}
    for profile in profiles:
        if profile["namespace"] in namespaces:
            errors.append(f"{profile['label']} and {namespaces[profile['namespace']]} both map to namespace '{profile['namespace']}'; give one of them a different suffix")
        namespaces.setdefault(profile["namespace"], profile["label"])
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
        return {"arc_profiles": arc_profiles, "arc_max_runners": arc_max_runners, "arc_image_ref": arc_image_ref}
