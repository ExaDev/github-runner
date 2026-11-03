"""Filters the github_runner_cluster role uses to size a Colima node VM from the inventory (tasks/colima_size.yml): which sizes are set, and what `colima start` must be given to bring the profile's VM to them."""

from __future__ import annotations

import json
import re
from typing import Any

from ansible.errors import AnsibleFilterError

# `colima start --memory` takes GiB (abiosoft/colima cmd/start.go, the "memory" flag), and `colima list --json` reports the VM's memory in bytes, as Lima does (environment/vm/lima/limautil/instance.go, InstanceInfo.Memory), so a GiB is 2^30 of them.
BYTES_PER_GIB = 1024**3
WHOLE_NUMBER = re.compile(r"^[1-9][0-9]*$")
MEMORY_VARIABLE = "github_runner_cluster_colima_memory_gib"
CPUS_VARIABLE = "github_runner_cluster_colima_cpus"


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
        return {"cluster_colima_size": cluster_colima_size, "cluster_colima_resize": cluster_colima_resize}
