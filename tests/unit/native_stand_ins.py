"""Shared stand-ins and a base class for the tests that run the github_runner_cluster role against a native node (test_native_node.py, its agent, test_native_server.py, its server, and test_native_regrade.py, its regrading): stand-ins for systemctl, journalctl, tailscale, dpkg-query, apt-get, docker and kubectl first on the playbook's PATH, a local server for the k3s release, a k3s data directory laid out as a server leaves it, and the helpers that run the role through tests/native/run.yml, so that no real service, package, tailnet or cluster is touched."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_cluster"
RUN = ROOT / "tests" / "native" / "run.yml"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")

TOKEN = "k3s-token-example-0123456789"
JOIN_KEY = "tskey-auth-example-0123456789"
DOMAIN = "example.ts.net"
K3S_TAG = "v1.34.1-k3s1"
K3S_RELEASE = "v1.34.1+k3s1"
TAILSCALE_VERSION = "1.102.4"
FACTS = {"system": "Linux", "architecture": "x86_64", "os_family": "Debian", "distribution": "Ubuntu", "distribution_release": "plucky", "service_mgr": "systemd"}


def tailscale_status(backend: str, domain: str | None) -> str:
    """Return what `tailscale status --json` prints for a client in ``backend`` that is logged in to ``domain`` (none when it is logged out)."""
    return json.dumps({"BackendState": backend, "CurrentTailnet": None if domain is None else {"Name": "x", "MagicDNSSuffix": domain, "MagicDNSEnabled": True}})


def stand_in(path: Path, body: str) -> None:
    """Write an executable shell script."""
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class ReleaseServer(http.server.ThreadingHTTPServer):
    """Serves a directory as the k3s release download and Tailscale repository."""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the signature of the method it replaces
        return


class NativeRoleHarness(unittest.TestCase):
    """Runs the role against a native node, stand-ins for the host's commands and a local k3s release. Holds no tests of its own."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mirror = Path(tempfile.mkdtemp(prefix="native-mirror-"))
        release = cls.mirror / K3S_RELEASE
        release.mkdir()
        # `k3s kubectl` reaches the test's kubectl stand-in, which answers for the cluster as the stand-in for `docker exec <container> kubectl` does.
        cls.binary = f'#!/bin/sh\nif [ "$1" = kubectl ]; then shift; exec "$STUB/bin/kubectl" "$@"; fi\necho "k3s version {K3S_RELEASE} (stand-in)"\n'
        (release / "k3s").write_text(cls.binary)
        cls.digest = hashlib.sha256(cls.binary.encode()).hexdigest()
        (release / "sha256sum-amd64.txt").write_text(f"{cls.digest}  k3s\n")
        repository = cls.mirror / "ubuntu"
        repository.mkdir()
        (repository / "plucky.noarmor.gpg").write_text("key\n")
        (repository / "plucky.tailscale-keyring.list").write_text("deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/ubuntu plucky main\n")
        cls.server = ReleaseServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(cls.mirror)))
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.mirror, True)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="native-role-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stubs = self.tmp / "stubs"
        self.state = self.stubs / "state"
        self.state.mkdir(parents=True)
        self.root = self.tmp / "host"
        self.paths = {
            "github_runner_cluster_native_bin_dir": self.root / "usr" / "local" / "bin",
            "github_runner_cluster_native_lib_dir": self.root / "usr" / "local" / "lib" / "github-runner-k3s",
            "github_runner_cluster_native_config_dir": self.root / "etc" / "rancher" / "k3s",
            "github_runner_cluster_native_unit_dir": self.root / "etc" / "systemd" / "system",
            "github_runner_cluster_native_data_dir": self.root / "var" / "lib" / "rancher" / "k3s",
            "github_runner_cluster_native_run_dir": self.root / "run",
            "github_runner_cluster_native_psi_file": self.root / "proc" / "pressure" / "memory",
            "github_runner_cluster_native_apt_keyring": self.root / "usr" / "share" / "keyrings" / "tailscale-archive-keyring.gpg",
            "github_runner_cluster_native_apt_source": self.root / "etc" / "apt" / "sources.list.d" / "tailscale.list",
        }
        for path in self.paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        # Directories every host has already: the role puts files in them and creates no others of the system's.
        self.paths["github_runner_cluster_native_bin_dir"].mkdir(exist_ok=True)
        self.paths["github_runner_cluster_native_unit_dir"].mkdir(exist_ok=True)
        # A kernel with memory pressure, as every kernel the role supports has unless it was built or booted without it.
        self.paths["github_runner_cluster_native_psi_file"].write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        self.make_stand_ins()
        self.set_tailscale(None, None)
        self.facts = dict(FACTS)
        # What the host holds before the role runs, which written() leaves out.
        self.host_files = self.files_on_host()

    def files_on_host(self) -> set[Path]:
        return {path for path in self.root.rglob("*") if path.is_file()}

    def written(self) -> list[Path]:
        """The files on the stand-in host that were not there before the role ran, in path order."""
        return sorted(self.files_on_host() - self.host_files)

    def make_stand_ins(self) -> None:
        bin_dir = self.stubs / "bin"
        bin_dir.mkdir()
        log = 'echo "$(basename "$0") $*" >> "$STUB/calls.log"\n'
        stand_in(bin_dir / "systemctl", log + r"""state="$STUB/state"
command=$1
shift
now=""
quiet=""
for argument in "$@"; do
  case "$argument" in
    --quiet) quiet=1 ;;
    --now) now=1 ;;
    *) unit=$argument ;;
  esac
done
join() {
  # k3s runs `tailscale up` with the join key as it starts, which logs the host's client in to the cluster's tailnet.
  if [ ! -e "$state/no-join" ]; then
    printf '%s' '{"BackendState":"Running","CurrentTailnet":{"MagicDNSSuffix":"example.ts.net"}}' > "$state/tailscale.json"
  fi
}
case "$command" in
  # --quiet prints nothing, as systemctl's does.
  is-enabled) if [ -e "$state/enabled-$unit" ]; then [ -n "$quiet" ] || echo enabled; exit 0; fi; [ -n "$quiet" ] || echo disabled; exit 1 ;;
  is-active) if [ -e "$state/active-$unit" ]; then [ -n "$quiet" ] || echo active; exit 0; fi; [ -n "$quiet" ] || echo inactive; exit 3 ;;
  enable)
    touch "$state/enabled-$unit"
    if [ -n "$now" ]; then touch "$state/active-$unit"; case "$unit" in k3s|k3s-agent) join ;; esac; fi ;;
  disable) rm -f "$state/enabled-$unit"; if [ -n "$now" ]; then rm -f "$state/active-$unit"; fi ;;
  start|restart) touch "$state/active-$unit"; case "$unit" in k3s|k3s-agent) join ;; esac ;;
  # Each start of a unit is a new run, with an invocation id of its own.
  show) cat "$state/invocation-$unit" 2>/dev/null || echo 0 ;;
esac
# A start of the server unit runs its ExecStartPre that records the regrading level, as systemd would, from the unit the role wrote: the real helper the role placed, with the paths the unit gives it.
case "$command:$unit" in
  enable:k3s|start:k3s|restart:k3s)
    if [ "$command" != enable ] || [ -n "$now" ]; then
      echo $(( $(cat "$state/invocation-k3s" 2>/dev/null || echo 0) + 1 )) > "$state/invocation-k3s"
      pre=$(sed -n 's/^ExecStartPre=\(.*write-regrade-level .*\)$/\1/p' "$UNIT_DIR/k3s.service")
      [ -z "$pre" ] || sh -c "$pre"
    fi ;;
esac
# What the role file named when a k3s unit was started, which is what its ExecCondition would have read.
case "$command:$unit" in
  enable:k3s*|start:k3s*|restart:k3s*) if [ "$command" != enable ] || [ -n "$now" ]; then echo "$command $unit $(cat "$ROLE_FILE" 2>/dev/null || echo none)" >> "$STUB/starts.log"; fi ;;
esac
""")
        # What the journal holds for a run of a unit: the line k3s logs once its etcd serves, unless the state says it never came.
        stand_in(bin_dir / "journalctl", log + r"""[ -e "$STUB/state/no-etcd" ] || echo "ETCD server is now running"
""")
        stand_in(bin_dir / "tailscale", log + r"""case "$1" in
  status) [ -e "$STUB/state/tailscale.json" ] || exit 1; cat "$STUB/state/tailscale.json" ;;
  logout) printf '%s' '{"BackendState":"NeedsLogin","CurrentTailnet":null}' > "$STUB/state/tailscale.json" ;;
esac
""")
        stand_in(bin_dir / "dpkg-query", r"""if [ -e "$STUB/state/tailscale-version" ]; then cat "$STUB/state/tailscale-version"; else exit 1; fi
""")
        stand_in(bin_dir / "apt-get", log + r"""for argument in "$@"; do
  case "$argument" in
    tailscale=*)
      printf '%s' "${argument#tailscale=}" > "$STUB/state/tailscale-version"
      # The package's scripts start tailscaled, which has no login on a host that never had one.
      if [ ! -e "$STUB/state/tailscale.json" ]; then printf '%s' '{"BackendState":"NeedsLogin","CurrentTailnet":null}' > "$STUB/state/tailscale.json"; fi ;;
  esac
done
""")
        # `docker exec <container> kubectl ...` reaches the same kubectl stand-in as `k3s kubectl ...`.
        stand_in(bin_dir / "docker", log + r"""case "$*" in
  "ps "*) if [ -e "$STUB/state/docker-stopped" ]; then echo exited; else echo running; fi ;;
  "exec "*" kubectl "*) shift 3; exec "$STUB/bin/kubectl" "$@" ;;
  *) echo "docker stand-in: unexpected $*" >&2; exit 1 ;;
esac
""")
        # The cluster as the API reports it, from files in the state directory the tests write: the nodes with their versions and live roles, the voters' readiness, this node's readiness and etcd vote, its member annotations, and the runner pods on it.
        stand_in(bin_dir / "kubectl", log + r"""state="$STUB/state"
case "$*" in
  "get nodes "*kubeletVersion*)
    if [ -e "$state/nodes-unreadable" ]; then echo "The connection to the server was refused" >&2; exit 1; fi
    if [ -e "$state/nodes" ]; then cat "$state/nodes"; else printf 'server1 v1.34.1+k3s1 true\n'; fi ;;
  "get nodes "*) if [ -e "$state/voters" ]; then cat "$state/voters"; else printf 'server1,True,true\n'; fi ;;
  "get node "*removed-node-name*) if [ -e "$state/member-removed" ]; then printf 'laptop-0a1b2c3d,'; else printf ',laptop-0a1b2c3d'; fi ;;
  "annotate --overwrite node "*"etcd.k3s.cattle.io/remove=true"*) [ -e "$state/member-stays" ] || touch "$state/member-removed" ;;
  "get node "*) if [ -e "$state/node-ready" ]; then cat "$state/node-ready"; else printf 'True,True'; fi ;;
  "get lease "*) if [ -e "$state/lease" ]; then cat "$state/lease"; else echo 2099-01-01T00:00:00Z; fi ;;
  "get pods "*) [ -e "$state/runner-pods" ] && cat "$state/runner-pods"; exit 0 ;;
  cordon*|uncordon*|drain*|label*|annotate*) ;;
  *) echo "kubectl stand-in: unexpected $*" >&2; exit 1 ;;
esac
""")

    def set_tailscale(self, backend: str | None, domain: str | None) -> None:
        path = self.state / "tailscale.json"
        if backend is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(tailscale_status(backend, domain))

    def set_package(self, version: str | None) -> None:
        """Install (or remove) the Tailscale package, whose scripts enable and start tailscaled."""
        path = self.state / "tailscale-version"
        for name in ("enabled-tailscaled", "active-tailscaled"):
            (self.state / name).unlink(missing_ok=True)
        if version is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(version)
            (self.state / "enabled-tailscaled").touch()
            (self.state / "active-tailscaled").touch()

    def inventory(self, only_the_node: bool = False, second_server: bool = False, **host_vars: Any) -> Path:
        shared = {
            "ansible_connection": "local",
            "ansible_become": False,
            "github_runner_cluster_tailnet_domain": DOMAIN,
            "github_runner_cluster_k3s_version": K3S_TAG,
            "github_runner_cluster_k3s_token": TOKEN,
            "github_runner_cluster_tailscale_join_key": JOIN_KEY,
            "github_runner_cluster_native_k3s_download_url": self.url,
            "github_runner_cluster_native_tailscale_repository_url": self.url,
            "github_runner_cluster_native_tailscale_cli": str(self.stubs / "bin" / "tailscale"),
            "github_runner_cluster_native_ready_timeout": 20,
            **{name: str(path) for name, path in self.paths.items()},
        }
        node = {**shared, "github_runner_cluster_node_mode": "native", **host_vars}
        hosts = {"laptop": node} if only_the_node else {"server1": dict(shared), **({"server2": dict(shared)} if second_server else {}), "laptop": node}
        inventory = {"all": {"children": {"github_runner_cluster": {"hosts": hosts}}}}
        path = self.tmp / "inventory.yml"
        path.write_text(yaml.safe_dump(inventory))
        return path

    def run_role(self, *args: str, host_vars: dict[str, Any] | None = None, check: bool = False, only_the_node: bool = False, second_server: bool = False, **extra: Any) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.stubs / 'bin'}:{os.environ['PATH']}",
            "STUB": str(self.stubs),
            "ROLE_FILE": str(self.file("github_runner_cluster_native_data_dir", "fleet-role")),
            "UNIT_DIR": str(self.paths["github_runner_cluster_native_unit_dir"]),
            "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"),
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_FORCE_COLOR": "0",
            "ANSIBLE_LOCALHOST_WARNING": "false",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
            "ANSIBLE_RETRY_FILES_ENABLED": "false",
        }
        command = [ANSIBLE_PLAYBOOK, "-i", str(self.inventory(only_the_node, second_server, **(host_vars or {}))), str(RUN), "-e", json.dumps({"host_facts": self.facts, **extra}), *args]
        if check:
            command.append("--check")
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=300, check=False, cwd=ROOT / "ansible")

    def calls(self) -> list[str]:
        """The commands the stand-ins were run with, one per call (a call whose arguments hold a line break logs more lines, which are not calls)."""
        log = self.stubs / "calls.log"
        return [line for line in log.read_text().splitlines() if re.match(r"^(systemctl|journalctl|tailscale|apt-get|dpkg-query|docker|kubectl) ", line)] if log.exists() else []

    def mutations(self, since: int = 0) -> list[str]:
        """The calls that change something, from the ``since``-th call on."""
        return [call for call in self.calls()[since:] if call.split()[0] in ("apt-get", "systemctl") and call.split()[1] not in ("is-enabled", "is-active", "show")] + [call for call in self.calls()[since:] if call in ("tailscale logout",) or (call.startswith("kubectl ") and call.split()[1] in ("cordon", "uncordon", "drain", "label", "annotate"))]

    def file(self, key: str, name: str) -> Path:
        return self.paths[key] / name

    def recap(self, result: subprocess.CompletedProcess[str]) -> dict[str, int]:
        line = next(line for line in result.stdout.splitlines() if re.match(r"^laptop\s+:", line))
        return {name: int(count) for name, count in re.findall(r"(\w+)=(\d+)", line)}

    def server_tree(self) -> Path:
        """Lay out a k3s server's data directory as k3s leaves it once its etcd has run: the datastore under server/db (etcd's member, its name and config files and the scheduled snapshots) beside the server's own certificates and token, which stay where they are. Returns the WAL file, whose bytes a test can follow."""
        data = self.paths["github_runner_cluster_native_data_dir"]
        files = {
            "server/db/etcd/config": "heartbeat-interval: 500\nelection-timeout: 5000\n",
            "server/db/etcd/name": "laptop-0a1b2c3d",
            "server/db/etcd/member/snap/db": "bolt",
            "server/db/etcd/member/wal/0000000000000000-0000000000000000.wal": "wal-bytes",
            "server/db/snapshots/etcd-snapshot-laptop-1767225600": "snapshot",
            "server/tls/server-ca.crt": "ca",
            "server/token": "K10secure::server:token",
            "server/cred/admin.kubeconfig": "kubeconfig",
            "agent/etc/containerd/config.toml": "containerd",
        }
        for name, content in files.items():
            path = data / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (data / "server").chmod(0o700)
        return data / "server/db/etcd/member/wal/0000000000000000-0000000000000000.wal"

    def starts(self) -> list[str]:
        log = self.stubs / "starts.log"
        return log.read_text().splitlines() if log.exists() else []

    def unit_calls(self, since: int = 0) -> list[str]:
        return [call for call in self.calls()[since:] if call.startswith("systemctl") and " is-" not in call and "daemon-reload" not in call and not call.startswith("systemctl show ")]

    def running_as(self, role: str) -> None:
        """Leave the host as a run of the role leaves one that runs ``role``: the role file names it, and its unit is enabled and active. For a test whose subject is the next run, so that it need not spend a run of the role getting there; the role writes every file it reads afresh."""
        self.file("github_runner_cluster_native_data_dir", "fleet-role").parent.mkdir(parents=True, exist_ok=True)
        self.file("github_runner_cluster_native_data_dir", "fleet-role").write_text(role)
        unit = "k3s" if role == "server" else "k3s-agent"
        for state in ("enabled", "active"):
            (self.state / f"{state}-{unit}").touch()

    def role_file(self) -> str:
        return self.file("github_runner_cluster_native_data_dir", "fleet-role").read_text()

    def run_three(self, *args: str, laptop: dict[str, Any] | None = None, check: bool = False, **extra: Any) -> subprocess.CompletedProcess[str]:
        """Run the role on laptop in a group of the Docker servers server1 and server2 and laptop, a native server unless ``laptop`` says otherwise."""
        return self.run_role(*args, host_vars={"github_runner_cluster_node_role": "server", **(laptop or {})}, check=check, second_server=True, **extra)

    def demote(self, voters: str = "server1,True,true\nserver2,True,true\nserver3,True,true\nlaptop,True,true\n") -> subprocess.CompletedProcess[str]:
        """Run laptop as a server, lay out its datastore as k3s leaves it, then run it as an agent with the cluster's voters read as ``voters``."""
        self.running_as("server")
        self.wal = self.server_tree()
        older = self.paths["github_runner_cluster_native_data_dir"] / "server" / "db.demoted" / "etcd" / "name"
        older.parent.mkdir(parents=True)
        older.write_text("laptop-from-an-earlier-demotion")
        (self.state / "voters").write_text(voters)
        self.demote_before = len(self.calls())
        return self.run_three(laptop={"github_runner_cluster_node_role": "agent"})

    def assert_demotion_refused(self, voters: str, message: str) -> None:
        result = self.demote(voters)
        before = self.demote_before
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, re.sub(r"\s+", " ", result.stdout))
        self.assertEqual(self.role_file(), "server")
        self.assertTrue((self.state / "active-k3s").exists())
        self.assertTrue(self.wal.exists())
        # The Tailscale package is installed on every run of this test's host, whose dpkg-query stand-in reports none.
        # The role writes its units before it decides what to do with them (here, for the first time), which reloads systemd; that touches no running unit.
        self.assertEqual([call for call in self.mutations(before) if not call.startswith(("apt-get", "systemctl enable --now tailscaled", "systemctl daemon-reload"))], [])

    def demote_quickly(self) -> subprocess.CompletedProcess[str]:
        self.running_as("server")
        self.wal = self.server_tree()
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\nserver3,True,true\nlaptop,True,true\n")
        return self.run_three(laptop={"github_runner_cluster_node_role": "agent", "github_runner_cluster_native_ready_timeout": 1})
