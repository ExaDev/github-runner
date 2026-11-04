#!/usr/bin/env python3
"""A stand-in for docker for fleet-role-actuator's Docker platform tests, keeping one Docker host in the directory FAKE_DOCKER.

containers/<name>/ holds a container: state (running, exited or created), image, mounts (one "<volume> <path>" per line), started-at, and tailscale while its k3s runs with Tailscale up. volumes/<name>/ holds a volume's files and volumes/<name>.labels its labels. log/<container> holds "<time> <text>" lines, of which `docker logs --since` prints those from the time given on.

The node containers are the Compose project FAKE_PROJECT's k3s service (FAKE_SERVER_CONTAINER) and k3s-agent service (FAKE_AGENT_CONTAINER), which `docker compose up` creates with the volumes docker-compose.yml gives them, named as Compose names them, and starts as files/k3s/node-entrypoint.sh does: idling, with no Tailscale, while the role file FAKE_ROLE_FILE names the other role, otherwise with Tailscale up, and for the server at the level LEVEL_FILE records, logging that etcd serves. `docker restart` does the same. `docker exec` runs a command on the local filesystem, save `test -S` of the Tailscale socket, which answers from the tailscale marker, kubectl, which goes to the kubectl on the PATH (tests/role_actuator/fake-kubectl.sh) after reading the kubeconfig the caller gives it on standard input into FAKE_DOCKER/kubeconfigs, and k3s, which goes to the k3s on the PATH (tests/role_actuator/fake-k3s.sh). docker cp moves a real tar stream between processes, so a pipeline that is wired wrongly moves nothing.

Every call is recorded in FAKE_DOCKER/calls. A start that leaves both node containers running is recorded in FAKE_DOCKER/both-running, and a copy out of a running container or into a volume a running container mounts in FAKE_DOCKER/copied-while-running.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path

root = Path(os.environ["FAKE_DOCKER"])
project = os.environ["FAKE_PROJECT"]
server = os.environ["FAKE_SERVER_CONTAINER"]
agent = os.environ["FAKE_AGENT_CONTAINER"]
args = sys.argv[1:]
with (root / "calls").open("a") as calls:
    calls.write(" ".join(args) + "\n")

# The volumes each node service mounts, by Compose key and path, as files/docker-compose.yml declares them.
SERVICE_VOLUMES = {
    "k3s": [("k3s-data", "/var/lib/rancher/k3s"), ("k3s-tailscale-data", "/var/lib/tailscale"), ("k3s-node", "/etc/rancher/node")],
    "k3s-agent": [("k3s-agent-data", "/var/lib/rancher/k3s"), ("k3s-agent-tailscale-data", "/var/lib/tailscale"), ("k3s-agent-node", "/etc/rancher/node")],
}
SERVICE_CONTAINER = {"k3s": server, "k3s-agent": agent}
ROLE_OF = {server: "server", agent: "agent"}


def die(message: str, status: int = 1) -> None:
    sys.stderr.write(message + "\n")
    sys.exit(status)


def container(name: str) -> Path:
    return root / "containers" / name


def state(name: str) -> str:
    path = container(name) / "state"
    return path.read_text() if path.exists() else ""


def mounts(name: str) -> list[tuple[str, str]]:
    path = container(name) / "mounts"
    return [tuple(line.split(" ", 1)) for line in path.read_text().splitlines()] if path.exists() else []  # type: ignore[misc]


def volume_at(name: str, path: str) -> Path:
    for volume, target in mounts(name):
        if target == path:
            return root / "volumes" / volume
    die(f"Error response from daemon: Could not find the file {path} in container {name}")
    raise AssertionError


def tick(seconds: int) -> str:
    clock = root / "ticks"
    ticks = (int(clock.read_text()) if clock.exists() else 0) + seconds
    clock.write_text(str(ticks))
    return "2026-01-01T00:%02d:%02dZ" % (ticks // 60, ticks % 60)


def other(name: str) -> str:
    return agent if name == server else server


def start(name: str) -> None:
    """Starts a node container as its entrypoint does."""
    if state(other(name)) == "running":
        with (root / "both-running").open("a") as both:
            both.write(f"{name} started while {other(name)} runs\n")
    started = tick(10)
    (container(name) / "state").write_text("running")
    (container(name) / "started-at").write_text(started)
    role_file = Path(os.environ["FAKE_ROLE_FILE"])
    marker = container(name) / "tailscale"
    log = root / "log" / name
    log.parent.mkdir(exist_ok=True)
    if role_file.exists() and role_file.read_text() != ROLE_OF[name]:
        marker.unlink(missing_ok=True)
        with log.open("a") as handle:
            handle.write(f"{started} idling\n")
        return
    marker.touch()
    if name == server:
        level_file = Path(os.environ["LEVEL_FILE"])
        started_file = Path(os.environ["STARTED_FILE"])
        started_file.parent.mkdir(parents=True, exist_ok=True)
        started_file.write_text(level_file.read_text() if level_file.exists() else "full\n")
        if not os.environ.get("FAKE_NO_ETCD"):
            with log.open("a") as handle:
                handle.write(f"{started} ETCD server is now running\n")


def stop(name: str) -> None:
    if state(name) == "running":
        (container(name) / "state").write_text("exited")
    (container(name) / "tailscale").unlink(missing_ok=True)
    started_file = Path(os.environ["STARTED_FILE"])
    if name == server:
        started_file.unlink(missing_ok=True)


def create_service(service: str) -> None:
    name = SERVICE_CONTAINER[service]
    if state(name):
        return
    container(name).mkdir(parents=True)
    (container(name) / "image").write_text(f"sha256:{service}-image")
    lines = []
    for key, path in SERVICE_VOLUMES[service]:
        volume = f"{project}_{key}"
        if not (root / "volumes" / volume).is_dir():
            (root / "volumes" / volume).mkdir(parents=True)
            (root / "volumes" / f"{volume}.labels").write_text(f"com.docker.compose.project={project}\ncom.docker.compose.volume={key}\n")
        lines.append(f"{volume} {path}")
    (container(name) / "mounts").write_text("\n".join(lines) + "\n")
    (container(name) / "state").write_text("created")


def option(name: str) -> str:
    return args[args.index(name) + 1]


command = args[0]
if command == "ps":
    filters = [args[index + 1] for index, value in enumerate(args) if value == "--filter"]
    wanted = re.match(r"name=\^/(.*)\$$", filters[0])
    if wanted:
        print(state(wanted.group(1)))
    else:
        volume = filters[0].removeprefix("volume=")
        users = sorted(path.name for path in (root / "containers").iterdir() if state(path.name) == "running" and any(v == volume for v, _ in mounts(path.name))) if (root / "containers").is_dir() else []
        print("\n".join(users), end="\n" if users else "")
elif command == "inspect":
    name = args[-1]
    if not state(name):
        die(f"Error: No such object: {name}")
    print((container(name) / ("image" if ".Image" in option("--format") else "started-at")).read_text())
elif args[:2] == ["volume", "ls"]:
    wanted = option("--filter").removeprefix("name=")
    names = sorted(path.name for path in (root / "volumes").iterdir() if path.is_dir() and wanted in path.name) if (root / "volumes").is_dir() else []
    print("\n".join(names), end="\n" if names else "")
elif args[:2] == ["volume", "create"]:
    name = args[-1]
    if (root / "volumes" / name).is_dir():
        die("volume already exists")
    (root / "volumes" / name).mkdir(parents=True)
    (root / "volumes" / f"{name}.labels").write_text("".join(args[index + 1] + "\n" for index, value in enumerate(args) if value == "--label"))
elif args[:2] == ["volume", "rm"]:
    shutil.rmtree(root / "volumes" / args[-1], ignore_errors=True)
    (root / "volumes" / f"{args[-1]}.labels").unlink(missing_ok=True)
elif command == "create":
    name = option("--name")
    volume, path = option("--volume").split(":", 1)
    if state(name):
        die("Conflict. The container name is already in use")
    container(name).mkdir(parents=True)
    (container(name) / "state").write_text("created")
    (container(name) / "image").write_text(args[-1])
    (container(name) / "mounts").write_text(f"{volume} {path}\n")
elif command == "rm":
    shutil.rmtree(container(args[-1]), ignore_errors=True)
elif command == "cp":
    source, destination = args[-2], args[-1]
    if source != "-":
        name, path = source.split(":", 1)
        path = path.removesuffix("/.")
        if not state(name):
            die(f"Error response from daemon: No such container: {name}")
        if state(name) == "running":
            with (root / "copied-while-running").open("a") as violations:
                violations.write(f"out of {name}\n")
        directory = volume_at(name, path)
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
            for entry in sorted(directory.rglob("*")):
                info = archive.gettarinfo(str(entry), arcname="./" + str(entry.relative_to(directory)))
                info.uid = info.gid = 0
                if entry.is_file():
                    with entry.open("rb") as handle:
                        archive.addfile(info, handle)
                else:
                    archive.addfile(info)
    else:
        name, path = destination.split(":", 1)
        if not state(name):
            die(f"Error response from daemon: No such container: {name}")
        directory = volume_at(name, path)
        volume = directory.name
        if any(state(other_name.name) == "running" and any(v == volume for v, _ in mounts(other_name.name)) for other_name in (root / "containers").iterdir()):
            with (root / "copied-while-running").open("a") as violations:
                violations.write(f"into {volume}\n")
        data = sys.stdin.buffer.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|") as archive:
            archive.extractall(directory, filter="tar")
elif command == "exec":
    rest = args[1:]
    interactive = rest[0] == "-i"
    if interactive:
        rest = rest[1:]
    name, rest = rest[0], rest[1:]
    if state(name) != "running":
        die(f"Error response from daemon: container {name} is not running")
    if rest[:2] == ["test", "-S"]:
        sys.exit(0 if (container(name) / "tailscale").exists() else 1)
    if rest[:1] == ["kubectl"]:
        if not interactive:
            die("kubectl was given no standard input to read its kubeconfig from")
        kubeconfig = sys.stdin.read()
        (root / "kubeconfigs").write_text(kubeconfig)
        if rest[1:3] != ["--kubeconfig", "/dev/stdin"] or not kubeconfig:
            die("error: no kubeconfig on standard input")
        os.execvp("kubectl", ["kubectl", *rest[3:]])
    if rest[:1] == ["k3s"]:
        os.execvp("k3s", rest)
    os.execvp(rest[0], rest)
elif command == "restart":
    name = args[-1]
    if os.environ.get("FAKE_RESTART_FAILS"):
        die("restart failed")
    stop(name)
    start(name)
elif command == "stop":
    stop(args[-1])
elif command == "logs":
    since = option("--since")
    log = root / "log" / args[-1]
    for line in log.read_text().splitlines() if log.exists() else []:
        time, text = line.split(" ", 1)
        if time >= since:
            print(text)
elif command == "compose":
    service = args[-1]
    if service not in SERVICE_CONTAINER:
        die(f"no such service: {service}")
    if "--project-name" not in args or option("--project-name") != project or option("--profile") != ("server" if service == "k3s" else "agent"):
        die("compose was not run on the host's project with the service's profile: " + " ".join(args))
    if "stop" in args:
        stop(SERVICE_CONTAINER[service])
    elif "up" in args:
        if os.environ.get("FAKE_UP_FAILS") == service:
            die(f"Error response from daemon: {service} failed to start")
        create_service(service)
        if state(SERVICE_CONTAINER[service]) != "running":
            start(SERVICE_CONTAINER[service])
    else:
        die("unexpected compose call: " + " ".join(args))
else:
    die("unexpected docker call: " + " ".join(args), 2)

