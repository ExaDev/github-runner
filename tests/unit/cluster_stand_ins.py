"""Shared stand-ins and a base class for the tests of how the cluster role keeps a k3s node's password across container recreation and recreates servers one at a time (test_node_password.py and test_server_recreate.py): a stand-in for docker and one for the Compose module, both first on the playbook's PATH and collection path so that no real Docker daemon, kubectl or launchd is ever reached, and the helpers that run the role's task files through tests/node_password/run.yml."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_cluster"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
RUN = ROOT / "tests" / "node_password" / "run.yml"
HASH_OLD = "a" * 64
HASH_NEW = "b" * 64


def has_compose() -> bool:
    """Whether a Docker Compose CLI is installed. The tests that use it only render configuration, which needs no daemon."""
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode == 0


def render_compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run ``docker compose`` over the role's Compose file with the variables it requires set, both profiles active and no daemon needed (config and the like)."""
    clean = {key: value for key, value in os.environ.items() if not key.startswith(("DOCKER_", "COMPOSE_", "K3S_", "TAILSCALE_"))}
    env = clean | {"K3S_TOKEN": "t", "K3S_TLS_SAN_LIST": "n", "K3S_VERSION": "v1.34.1-k3s1", "TAILSCALE_VERSION": "v1.102.4"}
    args = ["docker", "compose", "--project-directory", tempfile.gettempdir(), "--project-name", "example", "-f", str(ROLE / "files" / "docker-compose.yml"), "--profile", "server", "--profile", "agent", *arguments]
    return subprocess.run(args, capture_output=True, text=True, check=False, env=env)


def role_tasks(name: str) -> list[dict[str, Any]]:
    """Load one of the role's task files."""
    loaded: list[dict[str, Any]] = yaml.safe_load((ROLE / "tasks" / name).read_text())
    return loaded


def flatten(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every task of a list, with the tasks inside its blocks, rescues and always sections."""
    found: list[dict[str, Any]] = []
    for task in tasks:
        found.append(task)
        for key in ("block", "rescue", "always"):
            found.extend(flatten(task.get(key, [])))
    return found


# Stands in for the docker CLI, with its state in files under $DOCKER_STUB: containers/<name>.state (what docker ps prints), .image, .hash (the Compose configuration hash label), .node/ (the files of the container's /etc/rancher/node), .volume (the volume mounted there) and .cp-fails; volumes/<name>/ and labels/<name>; rendered/<project>.txt (what docker compose config --hash prints); cluster.nodes (what kubectl get nodes prints, "<node> <Ready status>" per line); and containers/<name>.kubectl-fails and never-ready-<node>; leases/<node> counts reads of a node's lease, whose renewal is older than its container's start on the first read and later after that, which is also when the node reports Ready (kubelet confirming itself) unless never-ready-<node> exists. Every call is appended to calls.log with its DOCKER_CONTEXT, and the owner and mode of each file docker cp extracts to owners.log. docker cp moves a real tar stream between processes, so a pipeline that is wired wrongly moves nothing.
DOCKER_STUB = """#!{python}
import os, re, sys, tarfile
stub = os.environ["DOCKER_STUB"]
args = sys.argv[1:]
def p(*parts):
    return os.path.join(stub, *parts)
def read(*parts):
    path = p(*parts)
    if not os.path.exists(path):
        return None
    with open(path) as handle:
        return handle.read()
def write(*parts, text):
    path = p(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(text)
def die(message):
    sys.stderr.write(message + "\\n")
    sys.exit(1)
with open(p("calls.log"), "a") as log:
    log.write(os.environ.get("DOCKER_CONTEXT", "-") + " " + " ".join(args) + "\\n")
if read("daemon-down") is not None:
    die("Cannot connect to the Docker daemon")
def filesystem(container):
    volume = read("containers", container + ".volume")
    return p("volumes", volume.strip()) if volume else p("containers", container + ".node")
def emit(directory):
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        archive.addfile(archive.gettarinfo(directory, arcname="./"))
        for name in sorted(os.listdir(directory)):
            info = archive.gettarinfo(os.path.join(directory, name), arcname="./" + name)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            with open(os.path.join(directory, name), "rb") as handle:
                archive.addfile(info, handle)
def absorb(directory):
    os.makedirs(directory, exist_ok=True)
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
        for member in archive:
            if member.isfile():
                name = os.path.basename(member.name)
                with open(os.path.join(directory, name), "wb") as handle:
                    handle.write(archive.extractfile(member).read())
                os.chmod(os.path.join(directory, name), member.mode & 0o777)
                with open(p("owners.log"), "a") as owners:
                    owners.write("%s %d:%d %o\\n" % (name, member.uid, member.gid, member.mode & 0o777))
def set_node(node, status):
    lines = [line for line in (read("cluster.nodes") or "").splitlines() if line.split()[0] != node]
    write("cluster.nodes", text="\\n".join(lines + [node + " " + status]) + "\\n")
command = args[0]
if command == "ps":
    name = re.match(r"name=\\^/(.*)\\$$", args[args.index("--filter") + 1]).group(1)
    print(read("containers", name + ".state") or "", end="")
elif command == "inspect":
    if read("containers", args[-1] + ".state") is None:
        die("Error: No such object: " + args[-1])
    if ".Image" in args[2]:
        print(read("containers", args[-1] + ".image"))
    elif ".State.StartedAt" in args[2]:
        print(read("containers", args[-1] + ".started") or "2000-01-01T00:00:00.000000000Z")
    else:
        print(read("containers", args[-1] + ".hash") or "")
elif args[:2] == ["volume", "ls"]:
    wanted = args[args.index("--filter") + 1].removeprefix("name=")
    print("\\n".join(sorted(name for name in os.listdir(p("volumes")) if wanted in name)) if os.path.isdir(p("volumes")) else "")
elif args[:2] == ["volume", "create"]:
    name = args[-1]
    if os.path.isdir(p("volumes", name)):
        die("volume already exists")
    os.makedirs(p("volumes", name))
    write("labels", name, text="\\n".join(args[index + 1] for index, value in enumerate(args) if value == "--label"))
elif args[:2] == ["volume", "rm"]:
    import shutil
    shutil.rmtree(p("volumes", args[-1]), ignore_errors=True)
    if os.path.exists(p("labels", args[-1])):
        os.remove(p("labels", args[-1]))
elif command == "create":
    name = args[args.index("--name") + 1]
    volume = args[args.index("--volume") + 1].split(":")[0]
    if read("containers", name + ".state") is not None:
        die("Conflict. The container name is already in use")
    if not os.path.isdir(p("volumes", volume)):
        die("no such volume: " + volume)
    write("containers", name + ".state", text="created")
    write("containers", name + ".volume", text=volume)
elif command == "rm":
    import glob, shutil
    for path in glob.glob(p("containers", args[-1] + ".*")):
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
elif command == "cp":
    source, destination = args[-2], args[-1]
    if source != "-":
        container, path = source.split(":", 1)
        if read("containers", container + ".state") is None:
            die("Error response from daemon: No such container: " + container)
        if read("containers", container + ".cp-fails") is not None:
            die("Error response from daemon: Could not find the file /etc/rancher/node in container " + container)
        emit(filesystem(container))
    else:
        container = destination.split(":", 1)[0]
        if read("containers", container + ".state") is None:
            die("Error response from daemon: No such container: " + container)
        absorb(filesystem(container))
elif command == "compose" and "config" in args:
    text = read("rendered", args[args.index("--project-name") + 1] + ".txt")
    if text is None:
        die("no configuration to render")
    print(text, end="")
elif command == "exec":
    container, rest = args[1], args[2:]
    if read("containers", container + ".state") != "running":
        die("Error response from daemon: container " + container + " is not running")
    if rest[:2] == ["kubectl", "get"]:
        if read("containers", container + ".kubectl-fails") is not None:
            die("The connection to the server 127.0.0.1:6443 was refused")
        if rest[2] == "nodes":
            print(read("cluster.nodes") or "", end="")
        elif rest[2] == "node":
            print(dict(line.split() for line in (read("cluster.nodes") or "").splitlines() if len(line.split()) == 2).get(rest[3], ""), end="")
        elif rest[2] == "lease":
            node = rest[5]
            calls = int(read("leases", node) or 0) + 1
            write("leases", node, text=str(calls))
            if read("never-ready-" + node) is None and calls >= 2:
                set_node(node, "True")
                print("2000-01-01T00:00:10.000000Z", end="")
            else:
                print("2000-01-01T00:00:00.000000Z", end="")
        else:
            die("unexpected docker exec: " + " ".join(rest))
    else:
        die("unexpected docker exec: " + " ".join(rest))
else:
    die("unexpected docker call: " + " ".join(args))
"""

# Stands in for a tool the tests must never reach for real: records the call and fails.
FORBIDDEN_STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "$DOCKER_STUB/forbidden.log"
echo "stand-in: $(basename "$0") must not be called in this test" >&2
exit 99
"""

# Stands in for community.docker.docker_compose_v2, which would drive a real Compose project: logs the call, and, outside check mode, starts a project's k3s container, or recreates it when the hash Compose renders differs from the container's, which takes its node out of Ready (or, with a stale-ready file in the stub directory, leaves its old Ready condition in place, as the API does for a while) until a read of its lease shows the new kubelet. The project is named for its node.
COMPOSE_MODULE = """#!/usr/bin/python
from __future__ import annotations
import os
from ansible.module_utils.basic import AnsibleModule

def main():
    module = AnsibleModule(
        argument_spec={"project_src": {"type": "str"}, "project_name": {"type": "str"}, "profiles": {"type": "list", "elements": "str"}, "cli_context": {"type": "str"}, "files": {"type": "list", "elements": "str"}, "state": {"type": "str"}},
        supports_check_mode=True,
    )
    stub = os.environ["DOCKER_STUB"]
    project = module.params["project_name"]
    with open(os.path.join(stub, "calls.log"), "a") as log:
        log.write("module up %s profiles=%s check=%s\\n" % (project, ",".join(module.params["profiles"] or []), module.check_mode))
    changed = False
    if not module.check_mode:
        state = os.path.join(stub, "containers", project + "-k3s.state")
        rendered = open(os.path.join(stub, "rendered", project + ".txt")).read().split()
        wanted = rendered[rendered.index("k3s") + 1]
        hash_path = os.path.join(stub, "containers", project + "-k3s.hash")
        current = open(hash_path).read() if os.path.exists(hash_path) else None
        if not os.path.exists(state) or current != wanted:
            changed = True
            os.makedirs(os.path.dirname(state), exist_ok=True)
            with open(state, "w") as handle:
                handle.write("running")
            with open(hash_path, "w") as handle:
                handle.write(wanted)
            lease = os.path.join(stub, "leases", project)
            if os.path.exists(lease):
                os.remove(lease)
            nodes = os.path.join(stub, "cluster.nodes")
            lines = [line for line in (open(nodes).read().splitlines() if os.path.exists(nodes) else []) if line.split()[0] != project]
            stale = os.path.exists(os.path.join(stub, "stale-ready"))
            with open(nodes, "w") as handle:
                handle.write("\\n".join(lines + [project + (" True" if stale else " False")]) + "\\n")
    module.exit_json(changed=changed)

main()
"""


def ansible_env() -> dict[str, str]:
    return {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_RETRY_FILES_ENABLED": "false"}


def recap(stdout: str, host: str) -> dict[str, int]:
    """Return the counts of ansible-playbook's play recap for one host."""
    line = next(line for line in stdout.splitlines() if re.match(rf"{re.escape(host)}\s+: ok=", line))
    return {key: int(value) for key, value in re.findall(r"(\w+)=(\d+)", line)}


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None, "ansible-playbook is not installed")
class RoleTestCase(unittest.TestCase):
    SECRET = "grtest-password-" + secrets.token_hex(16)

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.stub = self.work / "stub"
        self.bin = self.work / "bin"
        self.stub.mkdir()
        self.bin.mkdir()
        (self.bin / "docker").write_text(DOCKER_STUB.format(python=sys.executable))
        for tool in ("kubectl", "launchctl", "colima"):
            (self.bin / tool).write_text(FORBIDDEN_STUB)
        for tool in self.bin.iterdir():
            tool.chmod(0o755)
        modules = self.work / "collections" / "ansible_collections" / "community" / "docker" / "plugins" / "modules"
        modules.mkdir(parents=True)
        (modules / "docker_compose_v2.py").write_text(COMPOSE_MODULE)

    def tearDown(self) -> None:
        # Whatever a test did, no real tool was reached.
        self.assertFalse((self.stub / "forbidden.log").exists(), (self.stub / "forbidden.log").read_text() if (self.stub / "forbidden.log").exists() else "")

    def hosts(self, servers: tuple[str, ...], agents: tuple[str, ...] = (), **extra: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """The inventory's hosts and the group variables for a cluster whose servers and agents are named, each host's container named for it."""
        topology = {"servers": list(servers), "hosts": {name: {"role": "server" if name in servers else "agent", "bootstrap": name == servers[0], "node_name": name} for name in (*servers, *agents)}}
        hosts: dict[str, Any] = {}
        for name in (*servers, *agents):
            server = name in servers
            hosts[name] = {
                "github_runner_cluster_effective_node_role": "server" if server else "agent",
                "github_runner_cluster_effective_node_name": name,
                "github_runner_cluster_container_name": f"{name}-k3s",
                "github_runner_cluster_dir": str(self.work / "nodes" / name),
                "github_runner_cluster_compose_project": name,
                "github_runner_cluster_compose_file_list": ["docker-compose.yml"],
                "github_runner_cluster_docker_cli_environment": {},
                "github_runner_cluster_docker_cli_context": "",
                **extra,
            }
        return hosts, {"github_runner_cluster_topology": topology}

    def run_tasks(self, tasks: str, hosts: dict[str, Any], group_vars: dict[str, Any], *extra: str, check: bool = False, limit: str | None = None) -> subprocess.CompletedProcess[str]:
        inventory = self.work / "inventory.yml"
        inventory.write_text(yaml.safe_dump({"all": {"children": {"github_runner_cluster": {"vars": group_vars, "hosts": hosts}}}}))
        args = [ANSIBLE_PLAYBOOK, "-i", str(inventory), str(RUN), "-e", f"run_tasks={tasks}", *(["--check"] if check else []), *(["--limit", limit] if limit else []), *extra]
        # The stand-ins come first on the playbook's own PATH and collection path as well as the tasks', so nothing the role runs can reach this machine's real Docker, kubectl or launchd, or a real Compose project.
        env = ansible_env() | {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "DOCKER_STUB": str(self.stub),
            "ANSIBLE_COLLECTIONS_PATH": f"{self.work / 'collections'}{os.pathsep}{ROOT / 'playbooks' / 'collections'}",
        }
        return subprocess.run(args, capture_output=True, text=True, check=False, env=env, cwd=self.work, timeout=300)

    def write(self, *parts: str, text: str) -> None:
        path = self.stub.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def container(self, name: str, state: str = "running", config_hash: str | None = HASH_OLD, password: str | None = None, image: str = "sha256:0123") -> None:
        self.write("containers", f"{name}.state", text=state)
        self.write("containers", f"{name}.image", text=image)
        if config_hash is not None:
            self.write("containers", f"{name}.hash", text=config_hash)
        if password is not None:
            node = self.stub / "containers" / f"{name}.node"
            node.mkdir(parents=True, exist_ok=True)
            (node / "password").write_text(password)
            (node / "password").chmod(0o600)

    def rendered(self, project: str, config_hash: str) -> None:
        self.write("rendered", f"{project}.txt", text=f"docker-prune {'1' * 64}\nk3s {config_hash}\n")

    def nodes(self, **status: str) -> None:
        self.write("cluster.nodes", text="".join(f"{name} {value}\n" for name, value in status.items()))

    def calls(self) -> list[str]:
        log = self.stub / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def mutating(self) -> list[str]:
        """The calls that create, copy into or remove anything."""
        return [call for call in self.calls() if re.match(r"\S+ (create|rm|cp|volume create|volume rm)\b", call) or call.startswith("module up") and "check=False" in call]

    def nothing_on_the_host_holds_the_secret(self, result: subprocess.CompletedProcess[str]) -> None:
        """The password is in no output, no log and no file of the host: only the stand-ins' container and volume directories hold it."""
        self.assertNotIn(self.SECRET, result.stdout + result.stderr)
        for path in self.work.rglob("*"):
            if path.is_file() and not path.is_relative_to(self.stub / "containers") and not path.is_relative_to(self.stub / "volumes"):
                self.assertNotIn(self.SECRET.encode(), path.read_bytes(), str(path))
