"""Tests for how the cluster role sizes a Colima node VM from the inventory (tasks/colima_size.yml and tasks/colima_size_turn.yml): the filters that read the profile's size and the fleet-regrade Lease (plugins/filter/cluster_colima.py, plugins/filter/cluster_guard.py), and the tasks run through the role (tests/colima_size/run.yml) against stand-ins for docker and colima, so no real VM, Docker daemon or cluster is reached."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_cluster"
RUN = ROOT / "tests" / "colima_size" / "run.yml"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
GIB = 1024**3


def load_filter(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{name}_filters", ROOT / "plugins" / "filter" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


colima = load_filter("cluster_colima")
guard = load_filter("cluster_guard")


def listing(name: str = "node", cpus: int = 2, memory_gib: int = 4, status: str = "Running") -> str:
    """What `colima list --profile <name> --json` prints: one JSON document per line, memory and disk in bytes."""
    return json.dumps({"name": name, "status": status, "arch": "aarch64", "cpus": cpus, "memory": memory_gib * GIB, "disk": 100 * GIB, "runtime": "docker"}) + "\n"


class ColimaSizeFilterTest(unittest.TestCase):
    def test_a_profile_of_another_size_is_resized_to_the_values_set(self) -> None:
        plan = colima.cluster_colima_resize(listing(cpus=2, memory_gib=4), "node", 8, 4)
        self.assertEqual(plan["arguments"], ["--memory", "8", "--cpus", "4"])
        self.assertEqual(plan["changes"], ["memory from 4 GiB to 8 GiB", "CPUs from 2 to 4"])

    def test_a_profile_of_the_size_set_needs_nothing(self) -> None:
        self.assertEqual(colima.cluster_colima_resize(listing(cpus=4, memory_gib=8), "node", "8", "4")["changes"], [])

    def test_an_empty_value_keeps_that_part_of_the_size(self) -> None:
        plan = colima.cluster_colima_resize(listing(cpus=2, memory_gib=4), "node", "", 4)
        self.assertEqual((plan["arguments"], plan["changes"]), (["--cpus", "4"], ["CPUs from 2 to 4"]))
        self.assertEqual(colima.cluster_colima_resize(listing(), "node", "", "")["arguments"], [])

    def test_memory_that_is_not_a_whole_number_of_gib_is_reported_as_it_is(self) -> None:
        line = json.dumps({"name": "node", "cpus": 2, "memory": 6 * GIB + GIB // 2}) + "\n"
        self.assertEqual(colima.cluster_colima_resize(line, "node", 6, "")["changes"], ["memory from 6.5 GiB to 6 GiB"])

    def test_the_profile_asked_for_is_the_one_read(self) -> None:
        plan = colima.cluster_colima_resize(listing("other", cpus=8, memory_gib=16) + listing("node", cpus=4, memory_gib=8), "node", 8, 4)
        self.assertEqual(plan["changes"], [])

    def test_a_profile_colima_does_not_list_is_an_error(self) -> None:
        with self.assertRaisesRegex(colima.AnsibleFilterError, "Colima lists no profile named node"):
            colima.cluster_colima_resize(listing("other"), "node", 8, 4)

    def test_a_value_that_is_not_a_positive_whole_number_is_refused_with_its_name(self) -> None:
        for memory, cpus, name in ((8.5, "", "github_runner_cluster_colima_memory_gib"), ("8.5", "", "github_runner_cluster_colima_memory_gib"), ("", "four", "github_runner_cluster_colima_cpus"), (0, "", "github_runner_cluster_colima_memory_gib"), ("", -2, "github_runner_cluster_colima_cpus"), (True, "", "github_runner_cluster_colima_memory_gib"), ("", "4 ", "github_runner_cluster_colima_cpus")):
            with self.subTest(memory=memory, cpus=cpus), self.assertRaisesRegex(colima.AnsibleFilterError, name):
                colima.cluster_colima_size(memory, cpus)

    def test_whole_numbers_given_as_text_or_numbers_are_taken(self) -> None:
        self.assertEqual(colima.cluster_colima_size("12", 6), {"memory_gib": 12, "cpus": 6, "arguments": ["--memory", "12", "--cpus", "6"]})
        self.assertEqual(colima.cluster_colima_size("", None), {"memory_gib": None, "cpus": None, "arguments": []})

    def test_the_filters_are_registered(self) -> None:
        filters = colima.FilterModule().filters()
        self.assertIs(filters["cluster_colima_size"], colima.cluster_colima_size)
        self.assertIs(filters["cluster_colima_resize"], colima.cluster_colima_resize)


# A 32 GiB host as ansible_facts['memtotal_mb'] gives it (MiB). With a 3.4 GiB guest baseline and a 7 GiB runner limit the node floor is ceil(3.4 + 7) = 11 GiB.
HOST_MIB = 32 * 1024


def memory(explicit: Any = "", reserved: Any = "", host_reserve: Any = 2, runner: Any = 7, host_mib: Any = HOST_MIB, baseline: Any = 3.4) -> dict[str, Any]:
    result: dict[str, Any] = colima.cluster_colima_memory(explicit, host_mib, reserved, host_reserve, baseline, runner)
    return result


class ColimaMemoryFilterTest(unittest.TestCase):
    def test_the_derived_memory_is_the_host_less_what_its_other_workloads_and_its_os_need(self) -> None:
        plan = memory(reserved=6, host_reserve=2)
        self.assertEqual((plan["memory_gib"], plan["source"], plan["clamped"]), (24, "derived", False))

    def test_changing_the_reserved_memory_changes_the_derived_cap(self) -> None:
        self.assertEqual([memory(reserved=reserved)["memory_gib"] for reserved in (4, 10, "12.5")], [26, 20, 17])

    def test_a_part_of_a_gib_left_over_is_kept_by_the_host(self) -> None:
        # 32 - 6.5 - 2 = 23.5 GiB available: the VM gets the whole GiB below it, never more than the host can spare.
        self.assertEqual(memory(reserved="6.5")["memory_gib"], 23)

    def test_the_floor_is_the_guest_baseline_and_one_runner_rounded_up_to_a_whole_gib(self) -> None:
        self.assertEqual(memory(reserved=6)["floor_gib"], 11)
        self.assertEqual(memory(reserved=6, runner=8, baseline=3)["floor_gib"], 11)

    def test_a_derivation_below_the_floor_is_clamped_to_it(self) -> None:
        plan = memory(reserved=20, host_reserve=4)
        self.assertEqual((plan["memory_gib"], plan["available_gib"], plan["clamped"]), (11, 8, True))

    def test_a_memory_set_explicitly_wins_over_the_derivation(self) -> None:
        plan = memory(explicit=16, reserved=6)
        self.assertEqual((plan["memory_gib"], plan["source"], plan["clamped"]), (16, "set", False))

    def test_nothing_declared_keeps_the_profile_as_it_is(self) -> None:
        plan = memory()
        self.assertEqual((plan["memory_gib"], plan["source"]), (None, "none"))
        # Without a derivation the host's memory and the runner's limit are not needed.
        self.assertEqual(memory(runner="", host_mib="")["memory_gib"], None)

    def test_a_derivation_needs_the_runner_limit_and_the_host_memory(self) -> None:
        with self.assertRaisesRegex(colima.AnsibleFilterError, "github_runner_cluster_colima_runner_memory_limit_gib"):
            memory(reserved=6, runner="")
        with self.assertRaisesRegex(colima.AnsibleFilterError, "memtotal_mb"):
            memory(reserved=6, host_mib="")

    def test_a_reserve_that_is_not_a_number_is_refused_with_its_name(self) -> None:
        for arguments, name in (({"reserved": "six"}, "github_runner_cluster_colima_memory_reserved_gib"), ({"reserved": -1}, "github_runner_cluster_colima_memory_reserved_gib"), ({"reserved": 6, "host_reserve": "two"}, "github_runner_cluster_colima_memory_host_reserve_gib"), ({"reserved": True}, "github_runner_cluster_colima_memory_reserved_gib")):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(colima.AnsibleFilterError, name):
                memory(**arguments)

    def test_the_filter_is_registered(self) -> None:
        self.assertIs(colima.FilterModule().filters()["cluster_colima_memory"], colima.cluster_colima_memory)


def lease(holder: str, renewed: str = "2026-01-01T00:00:00.000000Z", duration: int = 60, version: str = "7") -> dict[str, Any]:
    return {"rc": 0, "stdout": json.dumps({"metadata": {"name": "fleet-regrade", "namespace": "kube-system", "resourceVersion": version}, "spec": {"holderIdentity": holder, "renewTime": renewed, "leaseDurationSeconds": duration}}), "stderr": ""}


NOT_FOUND = {"rc": 1, "stdout": "", "stderr": 'Error from server (NotFound): leases.coordination.k8s.io "fleet-regrade" not found'}
# 2026-01-01T00:00:30Z as an epoch time: thirty seconds into a sixty-second Lease renewed at midnight.
DURING = 1767225630.0
AFTER = DURING + 60


class LeaseFilterTest(unittest.TestCase):
    def claim(self, read: dict[str, Any], now: float = DURING) -> dict[str, Any]:
        result: dict[str, Any] = guard.cluster_lease_claim(read, "colima-resize/node1", now, 900, "fleet-regrade", "kube-system")
        return result

    def test_no_lease_is_created(self) -> None:
        claim = self.claim(NOT_FOUND)
        self.assertEqual((claim["state"], claim["verb"]), ("absent", "create"))
        self.assertNotIn("resourceVersion", claim["manifest"]["metadata"])
        self.assertEqual(claim["manifest"]["spec"], {"holderIdentity": "colima-resize/node1", "leaseDurationSeconds": 900, "acquireTime": "2026-01-01T00:00:30.000000Z", "renewTime": "2026-01-01T00:00:30.000000Z"})
        self.assertEqual((claim["manifest"]["apiVersion"], claim["manifest"]["kind"]), ("coordination.k8s.io/v1", "Lease"))

    def test_a_free_expired_or_own_lease_is_replaced_against_the_version_read(self) -> None:
        for read, now, state in ((lease(""), DURING, "free"), (lease("node2"), AFTER, "expired"), (lease("colima-resize/node1"), DURING, "mine")):
            with self.subTest(state=state):
                claim = self.claim(read, now)
                self.assertEqual((claim["state"], claim["verb"]), (state, "replace"))
                self.assertEqual(claim["manifest"]["metadata"]["resourceVersion"], "7")
                self.assertEqual(claim["manifest"]["spec"]["holderIdentity"], "colima-resize/node1")

    def test_a_lease_another_holds_is_refused_and_names_the_holder(self) -> None:
        claim = self.claim(lease("node2"))
        self.assertEqual((claim["state"], claim["holder"], claim["verb"]), ("held", "node2", ""))

    def test_a_lease_that_cannot_be_read_says_why(self) -> None:
        claim = self.claim({"rc": 1, "stdout": "", "stderr": "The connection to the server was refused"})
        self.assertEqual(claim["state"], "unreadable")
        self.assertIn("The connection to the server was refused", claim["problem"])

    def test_the_release_clears_only_our_own_holder(self) -> None:
        release = guard.cluster_lease_release(lease("colima-resize/node1"), "colima-resize/node1")
        self.assertEqual(release["verb"], "replace")
        self.assertEqual((release["manifest"]["spec"]["holderIdentity"], release["manifest"]["metadata"]["resourceVersion"]), ("", "7"))
        self.assertEqual(guard.cluster_lease_release(lease("node2"), "colima-resize/node1")["verb"], "")
        self.assertEqual(guard.cluster_lease_release(NOT_FOUND, "colima-resize/node1")["verb"], "")

    def test_the_lease_is_the_one_fleet_regrade_takes(self) -> None:
        core = (ROLE / "files" / "regrade" / "fleet-regrade-core.sh").read_text()
        self.assertIn(f"LEASE_NAME=${{LEASE_NAME:-{DEFAULTS['github_runner_cluster_regrade_lease_name']}}}", core)
        self.assertIn(f"LEASE_NAMESPACE=${{LEASE_NAMESPACE:-{DEFAULTS['github_runner_cluster_regrade_lease_namespace']}}}", core)

    def test_the_filters_are_registered(self) -> None:
        filters = guard.FilterModule().filters()
        self.assertIs(filters["cluster_lease_claim"], guard.cluster_lease_claim)
        self.assertIs(filters["cluster_lease_release"], guard.cluster_lease_release)


# Stands in for docker and colima, one script under two names, with state in files under $STUB: vm/<profile>.json is the profile's VM as `colima list --json` prints it, and while its status is not Running the Docker daemon of that profile does not answer and its containers do not run; containers/<container> holds what `docker ps` prints for a container (each host's node container is <host>-k3s, in the profile named for the host, as its node is); nodes.json and pods.json are what kubectl lists, etcd-<container> exists while that server's etcd reports ok, lease.json is the fleet-regrade Lease (no file: NotFound), runners holds how many polls still find a runner pod on the node, restarted-<profile> exists once the VM was started again after a stop, which is when its node's kubelet lease is renewed later than the stop, and colima-start-fails makes a start fail. Every call is appended to calls.log as "<DOCKER_CONTEXT or -> <tool> <arguments>".
STUB = """#!{python}
import json, os, sys
stub = os.environ["STUB"]
tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
def path(*parts):
    return os.path.join(stub, *parts)
def read(*parts):
    return open(path(*parts)).read() if os.path.exists(path(*parts)) else None
def write(text, *parts):
    os.makedirs(os.path.dirname(path(*parts)), exist_ok=True)
    open(path(*parts), "w").write(text)
def die(message, code=1):
    sys.stderr.write(message + "\\n")
    sys.exit(code)
with open(path("calls.log"), "a") as log:
    log.write(os.environ.get("DOCKER_CONTEXT", "-") + " " + tool + " " + " ".join(args) + "\\n")
def vm(profile):
    return json.loads(read("vm", profile + ".json"))
def running(profile):
    return vm(profile)["status"] == "Running"
if tool == "colima":
    profile = args[args.index("--profile") + 1]
    state = vm(profile)
    if args[0] == "list":
        print(json.dumps(state))
    elif args[0] == "stop":
        state["status"] = "Stopped"
        write(json.dumps(state), "vm", profile + ".json")
    elif args[0] == "start":
        if read("colima-start-fails") is not None:
            die("colima: the VM did not start")
        if "--activate=false" not in args:
            die("colima start without --activate=false would change the host's Docker context")
        if "--memory" in args:
            state["memory"] = int(args[args.index("--memory") + 1]) * 1024 ** 3
        if "--cpus" in args:
            state["cpus"] = int(args[args.index("--cpus") + 1])
        state["status"] = "Running"
        write(json.dumps(state), "vm", profile + ".json")
        write("", "restarted-" + profile)
    else:
        die("unexpected colima call: " + " ".join(args))
    sys.exit(0)
command = args[0]
if command == "ps":
    container = args[args.index("--filter") + 1].removeprefix("name=^/").removesuffix("$")
    if not running(container.removesuffix("-k3s")):
        die("Cannot connect to the Docker daemon")
    print(read("containers", container) or "", end="")
    sys.exit(0)
if command != "exec":
    die("unexpected docker call: " + " ".join(args))
rest = args[1:]
interactive = rest[0] == "--interactive"
if interactive:
    rest = rest[1:]
container, kubectl = rest[0], rest[1:]
if not running(container.removesuffix("-k3s")):
    die("Cannot connect to the Docker daemon")
if (read("containers", container) or "") != "running":
    die("Error response from daemon: container " + container + " is not running")
if kubectl[0] != "kubectl":
    die("unexpected docker exec: " + " ".join(rest))
verb, words = kubectl[1], kubectl[2:]
if verb in ("cordon", "uncordon", "drain"):
    pass
elif verb in ("create", "replace"):
    if not interactive:
        die("kubectl " + verb + " -f - needs docker exec --interactive to read the manifest")
    manifest = json.loads(sys.stdin.read())
    current = read("lease.json")
    if verb == "create" and current is not None:
        die('Error from server (AlreadyExists): leases.coordination.k8s.io "fleet-regrade" already exists')
    if verb == "replace":
        if current is None:
            die("Error from server (NotFound)")
        if json.loads(current)["metadata"]["resourceVersion"] != manifest["metadata"].get("resourceVersion"):
            die("Error from server (Conflict): the object has been modified")
    version = int(json.loads(current)["metadata"]["resourceVersion"]) + 1 if current else 1
    manifest["metadata"]["resourceVersion"] = str(version)
    write(json.dumps(manifest), "lease.json")
elif verb == "get" and words[0] == "nodes":
    print(read("nodes.json"))
elif verb == "get" and words[0] == "node":
    print("True," + ("True" if container.removesuffix("-k3s") in json.loads(read("servers")) else ""), end="")
elif verb == "get" and "--raw" in words:
    etcd = read("etcd-" + container)
    if etcd is None:
        die("etcd failed: reason withheld")
    print(etcd, end="")
elif verb == "get" and words[0] == "lease" and words[1] == "fleet-regrade":
    current = read("lease.json")
    if current is None:
        die('Error from server (NotFound): leases.coordination.k8s.io "fleet-regrade" not found')
    print(current)
elif verb == "get" and words[0] == "lease":
    node = words[words.index("kube-node-lease") + 1]
    print("2999-01-01T00:00:00.000000Z" if read("restarted-" + node) is not None else "2000-01-01T00:00:00.000000Z", end="")
elif verb == "get" and words[0] == "pods" and "--selector" in words:
    left = int(read("runners") or 0)
    if left > 0:
        write(str(left - 1), "runners")
        print("pod/runner-1")
elif verb == "get" and words[0] == "pods":
    print(read("pods.json"))
else:
    die("unexpected kubectl call: " + " ".join(kubectl))
"""

# The calls that change something: a node's schedulability, the Lease, or a VM.
MUTATING = re.compile(r"\S+ (docker exec (--interactive )?\S+ kubectl (cordon|uncordon|drain|create|replace)|colima (stop|start))\b")


def node(name: str, ready: bool = True) -> dict[str, Any]:
    labels = {"node-role.kubernetes.io/control-plane": "true", "node-role.kubernetes.io/etcd": "true"} if name.startswith("server") else {}
    return {"metadata": {"name": name, "labels": labels}, "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "Unknown"}]}}


def failed_task(stdout: str) -> str | None:
    """The name of the task whose header last precedes a "fatal:" line in ansible-playbook's output, or None when nothing failed."""
    task = None
    for line in stdout.splitlines():
        header = re.match(r"TASK \[(?:[^:\]]+ : )?(.*)\] \*+$", line)
        if header:
            task = header.group(1)
        elif line.startswith("fatal: "):
            return task
    return None


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class ColimaSizeRoleTest(unittest.TestCase):
    SERVERS = ("server1", "server2", "server3")

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.stub = self.work / "stub"
        self.bin = self.work / "bin"
        self.stub.mkdir()
        self.bin.mkdir()
        for tool in ("docker", "colima"):
            (self.bin / tool).write_text(STUB.format(python=sys.executable))
            (self.bin / tool).chmod(0o755)
        self.write("servers", json.dumps(self.SERVERS))
        for host in (*self.SERVERS, "agent1"):
            self.vm(host)
            self.write(f"containers/{host}-k3s", "running")
        for server in self.SERVERS:
            self.write(f"etcd-{server}-k3s", "ok")
        self.nodes()
        self.write("pods.json", json.dumps({"items": []}))

    def write(self, name: str, text: str) -> None:
        path = self.stub / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def vm(self, profile: str, cpus: int = 2, memory_gib: int = 4, status: str = "Running") -> None:
        self.write(f"vm/{profile}.json", listing(profile, cpus, memory_gib, status).strip())

    def size(self, profile: str) -> tuple[int, int, str]:
        state = json.loads((self.stub / "vm" / f"{profile}.json").read_text())
        return state["cpus"], state["memory"] // GIB, state["status"]

    def nodes(self, *not_ready: str) -> None:
        self.write("nodes.json", json.dumps({"items": [node(name, name not in not_ready) for name in (*self.SERVERS, "agent1")]}))

    def run_role(self, *extra: str, limit: str | None = None, **variables: Any) -> subprocess.CompletedProcess[str]:
        topology = {"servers": list(self.SERVERS), "hosts": {name: {"role": "server" if name in self.SERVERS else "agent", "bootstrap": name == self.SERVERS[0], "node_name": name, "native": False} for name in (*self.SERVERS, "agent1")}}
        hosts = {
            name: {
                "github_runner_cluster_effective_node_role": "server" if name in self.SERVERS else "agent",
                "github_runner_cluster_effective_node_name": name,
                "github_runner_cluster_container_name": f"{name}-k3s",
                "github_runner_cluster_agent_container_name": f"{name}-k3s",
                "github_runner_cluster_docker_context": f"colima-{name}",
                "github_runner_cluster_docker_cli_environment": {"DOCKER_CONTEXT": f"colima-{name}"},
                "github_runner_cluster_effective_runtime": {"kind": "colima", "colima_profile": name},
                "github_runner_cluster_runtime_environment": {"DOCKER_CONTEXT": f"colima-{name}", "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin"},
            }
            for name in (*self.SERVERS, "agent1")
        }
        inventory = self.work / "inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"vars": {"github_runner_cluster_topology": topology, "github_runner_cluster_native_ready_timeout": 20, "github_runner_cluster_native_runner_wait_timeout": 20}, "hosts": hosts}}))
        # As extra variables, which win over the inventory's, so a test can override what each host sets.
        extra_file = self.work / "extra.json"
        extra_file.write_text(json.dumps(variables))
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "STUB": str(self.stub),
            "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"),
            "ANSIBLE_LOCALHOST_WARNING": "false",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
            "ANSIBLE_RETRY_FILES_ENABLED": "false",
        }
        command = [ANSIBLE_PLAYBOOK, "-i", str(inventory), str(RUN), "-e", f"@{extra_file}", *(["--limit", limit] if limit else []), *extra]
        return subprocess.run(command, capture_output=True, text=True, check=False, env=env, cwd=self.work, timeout=600)

    def calls(self) -> list[str]:
        log = self.stub / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def mutating(self) -> list[str]:
        """The calls that changed something, without the Docker context and with the kubectl container and the colima arguments that only repeat the profile shortened."""
        return [re.sub(r"^\S+ ", "", call).replace("docker exec --interactive ", "").replace("docker exec ", "") for call in self.calls() if MUTATING.match(call)]

    def lease(self) -> dict[str, Any]:
        return json.loads((self.stub / "lease.json").read_text())

    def recap(self, result: subprocess.CompletedProcess[str], host: str) -> dict[str, int]:
        match = re.search(rf"{host}\s+: ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", result.stdout)
        assert match is not None, result.stdout + result.stderr
        return dict(zip(("ok", "changed", "unreachable", "failed"), map(int, match.groups())))

    def test_nothing_set_reads_and_changes_nothing(self) -> None:
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_profile_of_the_size_set_is_left_running_and_only_read(self) -> None:
        result = self.run_role(limit="server1", github_runner_cluster_colima_memory_gib=4, github_runner_cluster_colima_cpus=2)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mutating(), [])
        self.assertEqual(self.calls(), ["colima-server1 colima list --profile server1 --json"])
        self.assertEqual(self.recap(result, "server1")["changed"], 0)

    def test_a_server_is_drained_resized_through_colima_and_uncordoned_holding_the_lease(self) -> None:
        self.write("runners", "2")
        result = self.run_role(limit="server1", github_runner_cluster_colima_memory_gib=8, github_runner_cluster_colima_cpus=4)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            self.mutating(),
            [
                "server1-k3s kubectl create -f -",
                "server1-k3s kubectl cordon server1",
                "server1-k3s kubectl drain server1 --ignore-daemonsets --delete-emptydir-data --timeout=20s",
                "colima stop --profile server1",
                "colima start --profile server1 --activate=false --memory 8 --cpus 4",
                "server1-k3s kubectl uncordon server1",
                "server1-k3s kubectl replace -f -",
            ],
        )
        self.assertEqual(self.size("server1"), (4, 8, "Running"))
        # The runner pods are waited for after the cordon and before the drain, until none is left.
        calls = self.calls()
        polls = [index for index, call in enumerate(calls) if "--selector actions-ephemeral-runner=True" in call]
        self.assertEqual(len(polls), 3)
        self.assertLess(next(index for index, call in enumerate(calls) if " cordon " in call), polls[0])
        self.assertLess(polls[-1], next(index for index, call in enumerate(calls) if " drain " in call))
        # The node came back Ready and an etcd voter on a heartbeat later than the stop before it was uncordoned.
        ready = next(index for index, call in enumerate(calls) if "EtcdIsVoter" in call)
        self.assertLess(next(index for index, call in enumerate(calls) if "colima start" in call), ready)
        self.assertLess(ready, next(index for index, call in enumerate(calls) if " uncordon " in call))
        # The Lease was the fleet-regrade one, given back with nobody holding it.
        self.assertEqual((self.lease()["metadata"]["name"], self.lease()["metadata"]["namespace"]), ("fleet-regrade", "kube-system"))
        self.assertEqual(self.lease()["spec"]["holderIdentity"], "")

    def test_the_cluster_is_checked_before_the_cordon_and_again_before_the_stop(self) -> None:
        self.run_role(limit="server1", github_runner_cluster_colima_memory_gib=8)
        calls = self.calls()
        # Each check reads the cluster through every server of the inventory; this server's own container is read first.
        reads = [index for index, call in enumerate(calls) if "exec server1-k3s kubectl get nodes" in call]
        etcd = [index for index, call in enumerate(calls) if "/readyz/etcd" in call]
        cordon = next(index for index, call in enumerate(calls) if " cordon " in call)
        stop = next(index for index, call in enumerate(calls) if "colima stop" in call)
        self.assertEqual(len(reads), 2)
        self.assertLess(reads[0], cordon)
        self.assertLess(cordon, reads[1])
        self.assertLess(reads[1], stop)
        self.assertEqual(sorted({re.search(r"exec (\S+)", calls[index]).group(1) for index in etcd}), ["server2-k3s", "server3-k3s"])

    def test_a_second_run_changes_nothing(self) -> None:
        self.run_role(limit="server1", github_runner_cluster_colima_memory_gib=8, github_runner_cluster_colima_cpus=4)
        before = len(self.mutating())
        result = self.run_role(limit="server1", github_runner_cluster_colima_memory_gib=8, github_runner_cluster_colima_cpus=4)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mutating()[before:], [])
        self.assertEqual(self.recap(result, "server1")["changed"], 0)

    def test_a_held_lease_refuses_before_anything_changes(self) -> None:
        self.write("lease.json", json.dumps({"metadata": {"name": "fleet-regrade", "namespace": "kube-system", "resourceVersion": "3"}, "spec": {"holderIdentity": "server2", "renewTime": "2999-01-01T00:00:00.000000Z", "leaseDurationSeconds": 900}}))
        result = self.run_role(limit="server1", github_runner_cluster_colima_memory_gib=8)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the fleet-regrade Lease is held by server2", result.stdout)
        self.assertEqual(self.mutating(), [])
        self.assertEqual(self.size("server1"), (2, 4, "Running"))
        self.assertEqual(self.lease()["spec"]["holderIdentity"], "server2")

    def test_another_server_that_is_not_ready_refuses_before_anything_changes(self) -> None:
        self.nodes("server3")
        result = self.run_role(limit="server1", github_runner_cluster_colima_memory_gib=8)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Not resizing the Colima VM of server1", result.stdout)
        self.assertIn("server3 (not Ready)", result.stdout)
        self.assertEqual(self.mutating(), [])

    def test_another_server_whose_etcd_is_unhealthy_refuses_before_anything_changes(self) -> None:
        (self.stub / "etcd-server2-k3s").unlink()
        result = self.run_role(limit="server1", github_runner_cluster_colima_memory_gib=8)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("server2 (etcd not reported healthy)", result.stdout)
        self.assertEqual(self.mutating(), [])

    def test_check_mode_reports_the_resize_and_changes_nothing(self) -> None:
        result = self.run_role("--check", limit="server1", github_runner_cluster_colima_memory_gib=8, github_runner_cluster_colima_cpus=4)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Would resize the Colima VM of server1 (memory from 4 GiB to 8 GiB, CPUs from 2 to 4)", result.stdout)
        self.assertEqual(self.mutating(), [])
        self.assertFalse((self.stub / "lease.json").exists())
        self.assertGreater(self.recap(result, "server1")["changed"], 0)

    def test_check_mode_still_refuses_what_a_real_run_would_refuse(self) -> None:
        self.nodes("server2")
        result = self.run_role("--check", limit="server1", github_runner_cluster_colima_memory_gib=8)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("server2 (not Ready)", result.stdout)

    def test_a_value_that_is_not_a_whole_number_fails_before_anything_is_read(self) -> None:
        result = self.run_role(limit="server1", github_runner_cluster_colima_memory_gib="7.5")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("github_runner_cluster_colima_memory_gib must be a positive whole number", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_host_whose_runtime_is_not_colima_is_left_alone(self) -> None:
        # The sizes and the reserve are typically set for a whole group, which can hold Docker Desktop and Linux hosts as well.
        for variables in ({"github_runner_cluster_effective_runtime": {"kind": "docker_desktop", "colima_profile": ""}}, {"runtime_system": "Linux"}):
            with self.subTest(variables=variables):
                (self.stub / "calls.log").unlink(missing_ok=True)
                result = self.run_role(limit="server1", github_runner_cluster_colima_cpus=4, github_runner_cluster_colima_memory_reserved_gib=6, **variables)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.calls(), [])
                self.assertEqual(self.recap(result, "server1")["changed"], 0)

    def test_the_memory_derived_from_the_host_is_what_colima_starts_with(self) -> None:
        result = self.run_role(limit="agent1", runtime_memtotal_mb=HOST_MIB, github_runner_cluster_colima_memory_reserved_gib=6, github_runner_cluster_colima_memory_host_reserve_gib=2, github_runner_cluster_colima_runner_memory_limit_gib=7)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("colima start --profile agent1 --activate=false --memory 24", self.mutating())
        # The warning task runs only for a clamp; its header is printed whether or not it runs, so its message is what is looked for.
        self.assertNotIn("GiB is below the node floor of", result.stdout)

    def test_a_larger_reserve_gives_a_smaller_vm(self) -> None:
        result = self.run_role(limit="agent1", runtime_memtotal_mb=HOST_MIB, github_runner_cluster_colima_memory_reserved_gib=12, github_runner_cluster_colima_memory_host_reserve_gib=2, github_runner_cluster_colima_runner_memory_limit_gib=7)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("colima start --profile agent1 --activate=false --memory 18", self.mutating())

    def test_a_derivation_below_the_floor_starts_at_the_floor_and_warns(self) -> None:
        result = self.run_role(limit="agent1", runtime_memtotal_mb=HOST_MIB, github_runner_cluster_colima_memory_reserved_gib=24, github_runner_cluster_colima_memory_host_reserve_gib=2, github_runner_cluster_colima_runner_memory_limit_gib=7)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("colima start --profile agent1 --activate=false --memory 11", self.mutating())
        self.assertIn("6 GiB is below the node floor of 11 GiB", result.stdout)

    def test_an_explicit_memory_wins_over_the_reserve(self) -> None:
        result = self.run_role(limit="agent1", runtime_memtotal_mb=HOST_MIB, github_runner_cluster_colima_memory_gib=9, github_runner_cluster_colima_memory_reserved_gib=6, github_runner_cluster_colima_runner_memory_limit_gib=7)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("colima start --profile agent1 --activate=false --memory 9", self.mutating())

    def test_an_agent_is_drained_through_a_server_and_waits_for_ready_without_a_vote(self) -> None:
        result = self.run_role(limit="agent1", github_runner_cluster_colima_cpus=4)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            self.mutating(),
            [
                "server1-k3s kubectl create -f -",
                "server1-k3s kubectl cordon agent1",
                "server1-k3s kubectl drain agent1 --ignore-daemonsets --delete-emptydir-data --timeout=20s",
                "colima stop --profile agent1",
                "colima start --profile agent1 --activate=false --cpus 4",
                "server1-k3s kubectl uncordon agent1",
                "server1-k3s kubectl replace -f -",
            ],
        )
        # An agent holds no etcd member, so no server's etcd decides whether it may restart.
        self.assertEqual([call for call in self.calls() if "/readyz/etcd" in call], [])

    def test_a_node_container_that_is_not_running_is_resized_without_the_cluster(self) -> None:
        self.write("containers/server1-k3s", "exited")
        result = self.run_role(limit="server1", github_runner_cluster_colima_memory_gib=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mutating(), ["colima stop --profile server1", "colima start --profile server1 --activate=false --memory 8"])
        self.assertEqual([call for call in self.calls() if " kubectl " in call], [])

    def test_several_hosts_are_resized_one_at_a_time(self) -> None:
        result = self.run_role(github_runner_cluster_colima_memory_gib=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        steps = [call for call in self.mutating() if re.search(r"(cordon|colima (stop|start))", call)]
        hosts = [re.search(r"(server\d|agent\d)\s*(--|$)", step).group(1) for step in steps]
        # Each host's cordon, stop, start and uncordon come together, before the next host's cordon.
        self.assertEqual(len(hosts), 16)
        for turn in range(4):
            self.assertEqual(len(set(hosts[turn * 4 : turn * 4 + 4])), 1, steps)
        self.assertEqual(len(set(hosts)), 4)
        for host in (*self.SERVERS, "agent1"):
            self.assertEqual(self.size(host), (2, 8, "Running"))

    def test_a_start_that_fails_names_the_vm_and_gives_the_lease_back(self) -> None:
        self.write("colima-start-fails", "")
        result = self.run_role(limit="agent1", github_runner_cluster_colima_memory_gib=8)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("The Colima VM of agent1 was stopped to resize it", result.stdout)
        self.assertEqual(self.mutating()[-1], "server1-k3s kubectl replace -f -")
        self.assertEqual(self.lease()["spec"]["holderIdentity"], "")


class StaticTest(unittest.TestCase):
    def test_the_sizes_keep_the_profile_as_it_is_by_default(self) -> None:
        self.assertEqual((DEFAULTS["github_runner_cluster_colima_memory_gib"], DEFAULTS["github_runner_cluster_colima_cpus"]), ("", ""))
        # No reserve declared, so nothing is derived until a host says what its other workloads need.
        self.assertEqual((DEFAULTS["github_runner_cluster_colima_memory_reserved_gib"], DEFAULTS["github_runner_cluster_colima_runner_memory_limit_gib"]), ("", ""))

    def test_the_resize_is_included_after_the_topology_and_the_runtime_and_before_the_node_starts(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        order = [task.get("ansible.builtin.include_tasks") for task in tasks]
        self.assertLess(order.index("runtime.yml"), order.index("colima_size.yml"))
        self.assertLess(order.index("topology.yml"), order.index("colima_size.yml"))
        first_start = next(index for index, task in enumerate(tasks) if "block" in task)
        self.assertLess(order.index("colima_size.yml"), first_start)

    def test_no_variable_the_tasks_register_or_set_is_a_role_default(self) -> None:
        def written(tasks: list[dict[str, Any]]) -> set[str]:
            names: set[str] = set()
            for task in tasks:
                if "register" in task:
                    names.add(task["register"])
                if "ansible.builtin.set_fact" in task:
                    names.update(name for name in task["ansible.builtin.set_fact"] if name != "cacheable")
                for key in ("block", "rescue", "always"):
                    names.update(written(task.get(key, [])))
            return names

        names = set().union(*(written(yaml.safe_load((ROLE / "tasks" / name).read_text())) for name in ("colima_size.yml", "colima_size_turn.yml", "restart_guard.yml")))
        self.assertTrue(names)
        self.assertEqual(sorted(names & DEFAULTS.keys()), [])

    def test_every_new_variable_is_in_the_readme(self) -> None:
        readme = (ROLE / "README.md").read_text()
        for name in ("github_runner_cluster_colima_memory_gib", "github_runner_cluster_colima_cpus", "github_runner_cluster_colima_memory_reserved_gib", "github_runner_cluster_colima_memory_host_reserve_gib", "github_runner_cluster_colima_guest_baseline_gib", "github_runner_cluster_colima_runner_memory_limit_gib"):
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", readme)


if __name__ == "__main__":
    unittest.main()
