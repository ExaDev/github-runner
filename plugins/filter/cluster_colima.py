"""Filters the github_runner_cluster role uses to size a Colima node VM from the inventory (tasks/colima_size.yml): the memory it is to have, set or derived from what the host must keep, which sizes are wanted, and what `colima start` must be given to bring the profile's VM to them."""

from __future__ import annotations

import json
import re
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from ansible.errors import AnsibleFilterError

# `colima start --memory` takes GiB (abiosoft/colima cmd/start.go, the "memory" flag), and `colima list --json` reports the VM's memory in bytes, as Lima does (environment/vm/lima/limautil/instance.go, InstanceInfo.Memory), so a GiB is 2^30 of them.
BYTES_PER_GIB = 1024**3
WHOLE_NUMBER = re.compile(r"^[1-9][0-9]*$")
MEMORY_VARIABLE = "github_runner_cluster_colima_memory_gib"
CPUS_VARIABLE = "github_runner_cluster_colima_cpus"
RESERVED_VARIABLE = "github_runner_cluster_colima_memory_reserved_gib"
HOST_RESERVE_VARIABLE = "github_runner_cluster_colima_memory_host_reserve_gib"
BASELINE_VARIABLE = "github_runner_cluster_colima_guest_baseline_gib"
RUNNER_VARIABLE = "github_runner_cluster_colima_runner_memory_limit_gib"
HOST_MEMORY_FACT = "ansible_facts['memtotal_mb']"
# ansible_facts['memtotal_mb'] is in MiB (the hardware facts divide the bytes macOS's hw.memsize reports by 2^20), so a GiB is 2^10 of them.
MIB_PER_GIB = 1024
AMOUNT = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def _size(name: str, value: Any) -> int | None:
    """A positive whole number given as a number or as text, or None for an empty value, which keeps the profile's own."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not WHOLE_NUMBER.match(str(value)):
        raise AnsibleFilterError(f"{name} must be a positive whole number, or empty to keep the profile's own, not {value!r}.")
    return int(value)


def cluster_colima_size(memory_gib: Any, cpus: Any) -> dict[str, Any]:
    """Check the sizes the inventory sets for a Colima node VM and give the `colima start` arguments that set them.

    :param memory_gib: github_runner_cluster_colima_memory_gib: the VM's memory in GiB, or empty. :param cpus: github_runner_cluster_colima_cpus: the VM's CPUs, or empty.
    :returns: ``{"memory_gib": int | None, "cpus": int | None, "arguments": [...]}``, the arguments naming only the sizes that are set, as `colima start` keeps the profile's own value of a flag it is not given.
    :raises AnsibleFilterError: if a value is set and is not a positive whole number.
    """
    memory = _size(MEMORY_VARIABLE, memory_gib)
    count = _size(CPUS_VARIABLE, cpus)
    arguments = (["--memory", str(memory)] if memory is not None else []) + (["--cpus", str(count)] if count is not None else [])
    return {"memory_gib": memory, "cpus": count, "arguments": arguments}


def _amount(name: str, value: Any) -> Decimal | None:
    """A non-negative number of GiB given as a number or as text, or None for an empty value."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)) or not AMOUNT.match(str(value)):
        raise AnsibleFilterError(f"{name} must be a number of GiB, zero or more, not {value!r}.")
    return Decimal(str(value))


def _needed(name: str, value: Any) -> Decimal:
    amount = _amount(name, value)
    if amount is None:
        raise AnsibleFilterError(f"{name} must be set to derive the Colima VM's memory from {RESERVED_VARIABLE}.")
    return amount


def _number(amount: Decimal) -> int | float:
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def cluster_colima_memory(memory_gib: Any, host_memory_mb: Any, reserved_gib: Any, host_reserve_gib: Any, guest_baseline_gib: Any, runner_limit_gib: Any) -> dict[str, Any]:
    """Work out the memory a Colima node VM is to have: the value set, within what the host can spare when a reserve is set, or what the host can spare, never less than a node needs to run one runner.

    :param memory_gib: github_runner_cluster_colima_memory_gib: the VM's memory in GiB, which wins when set. :param host_memory_mb: ansible_facts['memtotal_mb'], the host's memory in MiB, needed only when a reserve is set. :param reserved_gib: github_runner_cluster_colima_memory_reserved_gib: the GiB the host's other workloads need; set, it bounds a memory set explicitly and derives one otherwise; empty, it does neither. :param host_reserve_gib: github_runner_cluster_colima_memory_host_reserve_gib: the GiB kept for the host's operating system. :param guest_baseline_gib: github_runner_cluster_colima_guest_baseline_gib: the GiB the node VM uses with no runner pod on it. :param runner_limit_gib: github_runner_cluster_colima_runner_memory_limit_gib: one runner pod's memory limit in GiB.
    :returns: ``{"memory_gib": int | None, "source": "set" | "derived" | "none", "available_gib": <the host's memory less both reserves, or None without a reserve>, "floor_gib": <the guest baseline and one runner's limit rounded up to a whole GiB, or None>, "clamped": bool}``. A derived memory is the available memory rounded down to a whole GiB, since `colima start --memory` takes whole GiB and rounding up would take memory the reserves keep; below the floor it is the floor, with ``clamped`` true. ``memory_gib`` None keeps the profile's own.
    :raises AnsibleFilterError: if a value is set and is not a number of the kind its variable takes, a reserve is set without the host's memory, a derivation lacks the runner's limit, or a memory set explicitly is more than the host can spare (the budget is a safety limit, so a workload whose reserve grows can never leave the VM larger than the host affords).
    """
    explicit = _size(MEMORY_VARIABLE, memory_gib)
    reserved = _amount(RESERVED_VARIABLE, reserved_gib)
    if reserved is None:
        source = "none" if explicit is None else "set"
        return {"memory_gib": explicit, "source": source, "available_gib": None, "floor_gib": None, "clamped": False}
    host_reserve = _needed(HOST_RESERVE_VARIABLE, host_reserve_gib)
    if isinstance(host_memory_mb, bool) or not isinstance(host_memory_mb, (int, str)) or not WHOLE_NUMBER.match(str(host_memory_mb)):
        raise AnsibleFilterError(f"{HOST_MEMORY_FACT} must give the host's memory in MiB to size the Colima VM within {RESERVED_VARIABLE}, not {host_memory_mb!r}.")
    host = Decimal(int(host_memory_mb)) / MIB_PER_GIB
    available = host - reserved - host_reserve
    if explicit is not None:
        if explicit > available:
            raise AnsibleFilterError(
                f"{MEMORY_VARIABLE} {explicit} is more than the {_number(available)} GiB this host can spare: its {_number(host)} GiB less "
                f"{_number(reserved)} GiB for its other workloads ({RESERVED_VARIABLE}) and {_number(host_reserve)} GiB for its operating system "
                f"({HOST_RESERVE_VARIABLE}). Lower the memory, or the reserve if the workloads need less."
            )
        return {"memory_gib": explicit, "source": "set", "available_gib": _number(available), "floor_gib": None, "clamped": False}
    baseline = _needed(BASELINE_VARIABLE, guest_baseline_gib)
    runner = _needed(RUNNER_VARIABLE, runner_limit_gib)
    if runner == 0:
        raise AnsibleFilterError(f"{RUNNER_VARIABLE} must be more than zero: a node VM is sized to run at least one runner pod.")
    floor = int((baseline + runner).to_integral_value(rounding=ROUND_CEILING))
    derived = int(available.to_integral_value(rounding=ROUND_FLOOR))
    return {"memory_gib": max(derived, floor), "source": "derived", "available_gib": _number(available), "floor_gib": floor, "clamped": derived < floor}


def _gib(memory: int) -> str:
    return f"{memory / BYTES_PER_GIB:g}"


def cluster_colima_resize(listing: Any, profile: Any, memory_gib: Any, cpus: Any) -> dict[str, Any]:
    """Compare the size of a profile's VM with the sizes the inventory sets.

    :param listing: what `colima list --profile <profile> --json` printed: one JSON document per profile and line, with ``name``, ``cpus`` and ``memory`` (bytes). It is the VM Lima runs, which a `colima start` given new sizes while the profile runs does not change (it writes them to the profile's configuration and leaves the VM as it is), so a size already written there but not applied still counts as a change. :param profile: the Colima profile. :param memory_gib: github_runner_cluster_colima_memory_gib, as cluster_colima_size takes it. :param cpus: github_runner_cluster_colima_cpus, likewise. :returns: ``{"changes": [...], "arguments": [...]}``: a sentence part for each size that differs, empty when the VM already has every size set, and the `colima start` arguments of cluster_colima_size. :raises AnsibleFilterError: if a size is not valid, the listing is not Colima's, or it does not name the profile.
    """
    wanted = cluster_colima_size(memory_gib, cpus)
    try:
        records = [json.loads(line) for line in str(listing).splitlines() if line.strip()]
    except ValueError as error:
        raise AnsibleFilterError(f"`colima list --json` printed something that is not JSON: {error}") from error
    record = next((item for item in records if isinstance(item, dict) and item.get("name") == profile), None)
    if record is None:
        raise AnsibleFilterError(f"Colima lists no profile named {profile}, so its size cannot be read (it listed {', '.join(str(item.get('name')) for item in records if isinstance(item, dict)) or 'none'}).")
    if not isinstance(record.get("memory"), int) or not isinstance(record.get("cpus"), int):
        raise AnsibleFilterError(f"Colima's listing of the profile {profile} gives no memory and CPU count: {record!r}.")
    changes: list[str] = []
    if wanted["memory_gib"] is not None and record["memory"] != wanted["memory_gib"] * BYTES_PER_GIB:
        changes.append(f"memory from {_gib(record['memory'])} GiB to {wanted['memory_gib']} GiB")
    if wanted["cpus"] is not None and record["cpus"] != wanted["cpus"]:
        changes.append(f"CPUs from {record['cpus']} to {wanted['cpus']}")
    return {"changes": changes, "arguments": wanted["arguments"]}


class FilterModule:
    """Registers the Colima sizing filters with Ansible."""

    def filters(self) -> dict[str, Any]:
        """Return the filters this plugin provides."""
        return {"cluster_colima_memory": cluster_colima_memory, "cluster_colima_size": cluster_colima_size, "cluster_colima_resize": cluster_colima_resize}
