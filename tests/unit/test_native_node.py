"""Tests the github_runner_cluster role's native node mode: the filters (plugins/filter/cluster_native.py), the two helper scripts the k3s-agent service runs, the unit and configuration templates, the defaults, argument specs and README, and the role's tasks (tests/native/run.yml) against stand-ins for systemctl, tailscale, dpkg-query, apt-get and docker on PATH and a local server for the k3s release, so that no real service, package or tailnet is touched."""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
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
PLUGIN = ROOT / "plugins" / "filter" / "cluster_native.py"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
SPECS: dict[str, Any] = yaml.safe_load((ROLE / "meta" / "argument_specs.yml").read_text())["argument_specs"]["main"]["options"]
README = (ROLE / "README.md").read_text()

_spec = importlib.util.spec_from_file_location("cluster_native", PLUGIN)
assert _spec is not None and _spec.loader is not None
cluster_native = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cluster_native)

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


class FilterTest(unittest.TestCase):
    def test_each_supported_architecture_names_its_release_asset_and_sum_file(self) -> None:
        self.assertEqual(cluster_native.cluster_native_k3s_asset("x86_64"), {"asset": "k3s", "checksums": "sha256sum-amd64.txt"})
        self.assertEqual(cluster_native.cluster_native_k3s_asset("aarch64"), {"asset": "k3s-arm64", "checksums": "sha256sum-arm64.txt"})

    def test_another_architecture_is_refused_with_the_supported_ones(self) -> None:
        for architecture in ("armv7l", "riscv64", "", None):
            with self.subTest(architecture=architecture), self.assertRaisesRegex(Exception, "not an architecture a native k3s node supports; use one of aarch64, x86_64"):
                cluster_native.cluster_native_k3s_asset(architecture)

    def test_the_image_tag_becomes_the_release_tag_with_a_plus(self) -> None:
        self.assertEqual(cluster_native.cluster_native_k3s_release("v1.34.1-k3s1"), "v1.34.1+k3s1")
        self.assertEqual(cluster_native.cluster_native_k3s_release(" v1.30.12-k3s2 "), "v1.30.12+k3s2")

    def test_a_tag_that_is_not_pinned_is_refused(self) -> None:
        for tag in ("latest", "v1.34", "v1.34.1+k3s1", "1.34.1-k3s1", ""):
            with self.subTest(tag=tag), self.assertRaisesRegex(Exception, "not a pinned rancher/k3s tag"):
                cluster_native.cluster_native_k3s_release(tag)

    def test_the_image_tag_becomes_the_package_version(self) -> None:
        self.assertEqual(cluster_native.cluster_native_tailscale_package_version("v1.102.4"), "1.102.4")
        for tag in ("latest", "1.102.4", "v1.102", "stable"):
            with self.subTest(tag=tag), self.assertRaisesRegex(Exception, "not a pinned tailscale/tailscale tag"):
                cluster_native.cluster_native_tailscale_package_version(tag)

    def test_a_client_is_told_apart_by_the_tailnet_it_is_logged_in_to(self) -> None:
        login = cluster_native.cluster_native_tailnet_login
        cases = [
            (("Running", DOMAIN), "cluster"),
            (("Stopped", DOMAIN), "cluster"),
            (("NeedsMachineAuth", DOMAIN), "cluster"),
            (("Running", "turtle-frog.ts.net"), "other"),
            (("Stopped", "turtle-frog.ts.net"), "other"),
            (("NeedsLogin", None), "logged_out"),
            (("NeedsLogin", "turtle-frog.ts.net"), "logged_out"),
            (("Stopped", None), "logged_out"),
            (("NoState", None), "settling"),
            (("Starting", DOMAIN), "settling"),
        ]
        for (backend, domain), state in cases:
            with self.subTest(backend=backend, domain=domain):
                self.assertEqual(login(tailscale_status(backend, domain), DOMAIN)["state"], state)

    def test_the_domain_is_compared_without_case_or_a_trailing_dot(self) -> None:
        self.assertEqual(cluster_native.cluster_native_tailnet_login(tailscale_status("Running", "Example.TS.net."), "example.ts.net")["state"], "cluster")
        self.assertEqual(cluster_native.cluster_native_tailnet_login(tailscale_status("Running", DOMAIN), "EXAMPLE.ts.net.")["state"], "cluster")

    def test_the_login_reports_the_tailnet_and_the_backend_it_read(self) -> None:
        self.assertEqual(cluster_native.cluster_native_tailnet_login(tailscale_status("Running", "turtle-frog.ts.net"), DOMAIN), {"state": "other", "backend": "Running", "domain": "turtle-frog.ts.net"})

    def test_a_status_that_is_not_the_clients_is_refused(self) -> None:
        for status in ("", "not json", "[]", "{}", '{"BackendState": 1}'):
            with self.subTest(status=status), self.assertRaisesRegex(Exception, "Tailscale status"):
                cluster_native.cluster_native_tailnet_login(status, DOMAIN)
        with self.assertRaisesRegex(Exception, "tailnet domain must be set"):
            cluster_native.cluster_native_tailnet_login(tailscale_status("Running", DOMAIN), "")

    def test_the_filters_are_registered_under_the_names_the_tasks_use(self) -> None:
        names = set(cluster_native.FilterModule().filters())
        used = set(re.findall(r"exadev\.github_runner\.(cluster_native_\w+)", "".join(path.read_text() for path in (ROLE / "tasks" / "native").glob("*.yml"))))
        self.assertEqual(names, used)


def stand_in(path: Path, body: str) -> None:
    """Write an executable shell script."""
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class ScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="native-scripts-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"

    def env(self, **extra: str) -> dict[str, str]:
        return {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "LOG": str(self.log), **extra}

    def wrapper(self, *args: str, host: str = "k3s-server.example.ts.net", **extra: str) -> subprocess.CompletedProcess[str]:
        env = self.env(GITHUB_RUNNER_TAILSCALE=str(self.bin / "tailscale"), **extra)
        if host:
            env["GITHUB_RUNNER_JOIN_HOST"] = host
        return subprocess.run(["sh", str(ROLE / "files" / "native" / "tailscale-up-wait.sh"), *args], env=env, capture_output=True, text=True, timeout=60, check=False)

    def test_the_wrapper_runs_the_real_command_and_passes_other_commands_through(self) -> None:
        stand_in(self.bin / "tailscale", 'echo "tailscale $*" >> "$LOG"; echo status-output; exit 0\n')
        result = self.wrapper("status", "--json")
        self.assertEqual((result.returncode, result.stdout), (0, "status-output\n"))
        self.assertEqual(self.log.read_text(), "tailscale status --json\n")

    def test_the_wrapper_fails_with_the_real_commands_status_and_does_not_wait(self) -> None:
        stand_in(self.bin / "tailscale", 'echo "tailscale $*" >> "$LOG"; exit 7\n')
        stand_in(self.bin / "getent", 'echo "getent $*" >> "$LOG"; exit 0\n')
        result = self.wrapper("up", "--authkey", "x")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.log.read_text(), "tailscale up --authkey x\n")

    def test_after_up_the_wrapper_waits_for_the_join_host_to_be_a_peer_that_resolves(self) -> None:
        # The peer appears on the third look, and the name does not resolve until the second time it is asked, which asks for the peer again first.
        stand_in(self.bin / "tailscale", 'echo "tailscale $*" >> "$LOG"; if [ "$1" = ip ]; then [ "$(grep -c "^tailscale ip" "$LOG")" -ge 3 ] || exit 1; fi\n')
        stand_in(self.bin / "getent", 'echo "getent $*" >> "$LOG"; [ "$(grep -c "^getent" "$LOG")" -ge 2 ]\n')
        stand_in(self.bin / "logger", 'echo "logger $*" >> "$LOG"\n')
        result = self.wrapper("up", "--authkey", "x", "--timeout=30s", "--reset")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        self.assertEqual(calls[0], "tailscale up --authkey x --timeout=30s --reset")
        self.assertEqual([call for call in calls if call.startswith("tailscale ip")], ["tailscale ip -4 k3s-server.example.ts.net"] * 4)
        self.assertEqual([call for call in calls if call.startswith("getent")], ["getent hosts k3s-server.example.ts.net"] * 2)
        self.assertEqual(calls[-1], "logger -t github-runner-k3s k3s-server.example.ts.net is a peer and resolves after tailscale up")

    def test_without_a_join_host_up_waits_for_nothing(self) -> None:
        stand_in(self.bin / "tailscale", 'echo "tailscale $*" >> "$LOG"\n')
        result = self.wrapper("up", host="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text(), "tailscale up\n")

    def test_the_wrapper_needs_the_real_command_named(self) -> None:
        result = subprocess.run(["sh", str(ROLE / "files" / "native" / "tailscale-up-wait.sh"), "status"], env={k: v for k, v in os.environ.items() if k != "GITHUB_RUNNER_TAILSCALE"}, capture_output=True, text=True, timeout=60, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GITHUB_RUNNER_TAILSCALE must name the real tailscale command", result.stderr)

    def resolv(self, *sources: str, **files: str) -> subprocess.CompletedProcess[str]:
        paths = []
        for name, content in files.items():
            (self.tmp / name).write_text(content)
            paths.append(str(self.tmp / name))
        output = self.tmp / "out" / "resolv.conf"
        output.parent.mkdir(exist_ok=True)
        return subprocess.run(["sh", str(ROLE / "files" / "native" / "write-resolv-conf.sh"), str(output)], env=self.env(GITHUB_RUNNER_RESOLV_SOURCES=" ".join(paths)), capture_output=True, text=True, timeout=60, check=False)

    def written(self) -> str:
        return (self.tmp / "out" / "resolv.conf").read_text()

    def test_the_hosts_upstream_servers_are_written_without_tailscale_search_domains_or_options(self) -> None:
        result = self.resolv(resolved="# comment\nnameserver 100.100.100.100\nnameserver 10.0.0.1\nnameserver 1.1.1.1\nsearch local example.ts.net\noptions edns0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.written(), "nameserver 10.0.0.1\nnameserver 1.1.1.1\n")
        self.assertEqual([path.name for path in (self.tmp / "out").iterdir()], ["resolv.conf"])

    def test_tailscales_resolvers_loopback_and_zoned_addresses_are_left_out(self) -> None:
        result = self.resolv(resolved="nameserver 127.0.0.53\nnameserver fd7a:115c:a1e0::53\nnameserver ::1\nnameserver fe80::1%wlp2s0\nnameserver 2001:db8::53\nnameserver 192.168.1.1\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.written(), "nameserver 2001:db8::53\nnameserver 192.168.1.1\n")

    def test_the_next_source_is_read_when_the_first_has_no_usable_server(self) -> None:
        result = self.resolv(first="nameserver 127.0.0.53\nnameserver 100.100.100.100\n", second="nameserver 192.168.1.1\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.written(), "nameserver 192.168.1.1\n")

    def test_a_host_with_no_usable_server_fails_and_writes_nothing(self) -> None:
        result = self.resolv(only="nameserver 127.0.0.53\nnameserver 100.100.100.100\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no upstream name server outside Tailscale and loopback", result.stderr)
        self.assertFalse((self.tmp / "out" / "resolv.conf").exists())

    def test_a_replaced_file_is_replaced_in_one_rename(self) -> None:
        self.resolv(resolved="nameserver 10.0.0.1\n")
        self.resolv(resolved="nameserver 10.0.0.2\n")
        self.assertEqual(self.written(), "nameserver 10.0.0.2\n")

    def test_both_scripts_are_executable_and_shellcheck_clean(self) -> None:
        scripts = sorted((ROLE / "files" / "native").glob("*.sh"))
        self.assertEqual(len(scripts), 2)
        for script in scripts:
            self.assertTrue(os.access(script, os.X_OK), script)
        if shutil.which("shellcheck"):
            self.assertEqual(subprocess.run(["shellcheck", *map(str, scripts)], capture_output=True, text=True, check=False).returncode, 0)


class ReleaseServer(http.server.ThreadingHTTPServer):
    """Serves a directory as the k3s release download and Tailscale repository."""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the signature of the method it replaces
        return


class DefaultsTest(unittest.TestCase):
    def test_native_mode_is_off_by_default_and_the_service_starts_when_it_is_on(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_node_mode"], "compose")
        self.assertIs(DEFAULTS["github_runner_cluster_native_start"], True)

    def test_a_host_is_never_logged_out_of_a_tailnet_unless_it_says_so(self) -> None:
        self.assertIs(DEFAULTS["github_runner_cluster_native_tailscale_leave_other_tailnet"], False)

    def test_the_download_is_checked_against_the_releases_own_sums_unless_a_sum_is_set(self) -> None:
        self.assertEqual(DEFAULTS["github_runner_cluster_native_k3s_sha256"], "")
        self.assertTrue(DEFAULTS["github_runner_cluster_native_k3s_download_url"].startswith("https://github.com/k3s-io/k3s/releases/download"))

    def test_every_native_default_is_declared_with_the_same_default_and_described_in_the_readme(self) -> None:
        native = {name for name in DEFAULTS if name.startswith("github_runner_cluster_native_") or name == "github_runner_cluster_node_mode"}
        derived = {"github_runner_cluster_native_service", "github_runner_cluster_native_join_host"}
        self.assertEqual(set(SPECS), native - derived)
        for name, spec in SPECS.items():
            with self.subTest(name=name):
                default = DEFAULTS[name]
                self.assertEqual(str(spec["default"]), str(default) if not isinstance(default, str) or "{{" not in default else str(spec["default"]), name)
                self.assertIn(f"`{name}`", README)
        for name in derived:
            self.assertIn(f"`{name}`", README)

    def test_the_mode_choices_are_the_ones_the_topology_filter_accepts(self) -> None:
        topology = importlib.util.spec_from_file_location("cluster_topology", ROOT / "plugins" / "filter" / "cluster_topology.py")
        assert topology is not None and topology.loader is not None
        module = importlib.util.module_from_spec(topology)
        topology.loader.exec_module(module)
        self.assertEqual(tuple(SPECS["github_runner_cluster_node_mode"]["choices"]), module.NODE_MODES)


class TemplateTest(unittest.TestCase):
    """Reads the unit and the configuration the role writes, from a run of the role (see NativeRoleTest)."""

    def test_the_unit_and_configuration_are_templates_the_role_writes(self) -> None:
        config = yaml.safe_load((ROLE / "tasks" / "native" / "config.yml").read_text())
        written = {task["ansible.builtin.template"]["src"] for task in config if "ansible.builtin.template" in task}
        self.assertEqual(written, {"native/config.yaml.j2", "native/k3s-agent.service.j2"})
        for name in written:
            self.assertTrue((ROLE / "templates" / name).is_file(), name)

    def test_every_secret_task_logs_nothing(self) -> None:
        for path in sorted((ROLE / "tasks" / "native").glob("*.yml")):
            for task in yaml.safe_load(path.read_text()):
                text = json.dumps(task)
                if "github_runner_cluster_k3s_token" in text or "github_runner_cluster_mesh_vpn_auth" in text or "github_runner_cluster_mesh_join_key" in text:
                    with self.subTest(path=path.name, task=task.get("name")):
                        self.assertIs(task.get("no_log"), True)

    def test_become_is_used_by_the_tasks_that_change_the_host_and_by_nothing_wider(self) -> None:
        for path in sorted((ROLE / "tasks" / "native").glob("*.yml")):
            for task in yaml.safe_load(path.read_text()):
                with self.subTest(path=path.name, task=task.get("name")):
                    self.assertNotIn("block", task) if "become" in task else None
                    self.assertIn(task.get("become"), (None, True))
        self.assertNotIn("become", (ROLE / "tasks" / "main.yml").read_text())

    def test_main_hands_native_hosts_to_the_native_tasks_and_ends_the_role_before_any_docker_task(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        names = [next(key for key in task if key.startswith("ansible.builtin.")) for task in tasks[:2]]
        self.assertEqual(names, ["ansible.builtin.include_tasks", "ansible.builtin.meta"])
        self.assertEqual(tasks[0]["ansible.builtin.include_tasks"], "native/main.yml")
        self.assertEqual(tasks[1]["ansible.builtin.meta"], "end_role")
        self.assertEqual(tasks[0]["when"], tasks[1]["when"])
        self.assertEqual(tasks[2]["ansible.builtin.include_tasks"], "docker_context.yml")


class NativeRoleTest(unittest.TestCase):
    """Runs the role against a native node, stand-ins for the host's commands and a local k3s release."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mirror = Path(tempfile.mkdtemp(prefix="native-mirror-"))
        release = cls.mirror / K3S_RELEASE
        release.mkdir()
        cls.binary = f'#!/bin/sh\necho "k3s version {K3S_RELEASE} (stand-in)"\n'
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
            "github_runner_cluster_native_apt_keyring": self.root / "usr" / "share" / "keyrings" / "tailscale-archive-keyring.gpg",
            "github_runner_cluster_native_apt_source": self.root / "etc" / "apt" / "sources.list.d" / "tailscale.list",
        }
        for path in self.paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
        # Directories every host has already: the role puts files in them and creates no others of the system's.
        self.paths["github_runner_cluster_native_bin_dir"].mkdir(exist_ok=True)
        self.paths["github_runner_cluster_native_unit_dir"].mkdir(exist_ok=True)
        self.make_stand_ins()
        self.set_tailscale(None, None)
        self.facts = dict(FACTS)

    def make_stand_ins(self) -> None:
        bin_dir = self.stubs / "bin"
        bin_dir.mkdir()
        log = 'echo "$(basename "$0") $*" >> "$STUB/calls.log"\n'
        stand_in(bin_dir / "systemctl", log + r"""state="$STUB/state"
case "$1" in
  is-enabled) if [ -e "$state/enabled-$2" ]; then echo enabled; exit 0; fi; echo disabled; exit 1 ;;
  is-active) if [ -e "$state/active-$2" ]; then echo active; exit 0; fi; echo inactive; exit 3 ;;
  enable) if [ "$2" = --now ]; then touch "$state/enabled-$3" "$state/active-$3"; else touch "$state/enabled-$2"; fi ;;
  start|restart)
    touch "$state/active-$2"
    # k3s runs `tailscale up` with the join key as it starts, which logs the host's client in to the cluster's tailnet.
    if [ "$2" = k3s-agent ] && [ ! -e "$state/no-join" ]; then
      printf '%s' '{"BackendState":"Running","CurrentTailnet":{"MagicDNSSuffix":"example.ts.net"}}' > "$state/tailscale.json"
    fi ;;
esac
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
        stand_in(bin_dir / "docker", log + r"""case "$*" in
  "ps "*) echo running ;;
  *"kubectl get nodes"*) printf 'server1 v1.34.1+k3s1 true\n' ;;
  *"kubectl get node "*) echo True ;;
  *"kubectl get lease"*) echo 2099-01-01T00:00:00Z ;;
  *) echo "docker stand-in: unexpected $*" >&2; exit 1 ;;
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

    def inventory(self, only_the_node: bool = False, **host_vars: Any) -> Path:
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
        hosts = {"laptop": node} if only_the_node else {"server1": dict(shared), "laptop": node}
        inventory = {"all": {"children": {"github_runner_cluster": {"hosts": hosts}}}}
        path = self.tmp / "inventory.yml"
        path.write_text(yaml.safe_dump(inventory))
        return path

    def run_role(self, *args: str, host_vars: dict[str, Any] | None = None, check: bool = False, only_the_node: bool = False, **extra: Any) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.stubs / 'bin'}:{os.environ['PATH']}",
            "STUB": str(self.stubs),
            "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"),
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_FORCE_COLOR": "0",
            "ANSIBLE_LOCALHOST_WARNING": "false",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
            "ANSIBLE_RETRY_FILES_ENABLED": "false",
        }
        command = [ANSIBLE_PLAYBOOK, "-i", str(self.inventory(only_the_node, **(host_vars or {}))), str(RUN), "-e", json.dumps({"host_facts": self.facts, **extra}), *args]
        if check:
            command.append("--check")
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=300, check=False, cwd=ROOT / "ansible")

    def calls(self) -> list[str]:
        """The commands the stand-ins were run with, one per call (a call whose arguments hold a line break logs more lines, which are not calls)."""
        log = self.stubs / "calls.log"
        return [line for line in log.read_text().splitlines() if re.match(r"^(systemctl|tailscale|apt-get|dpkg-query|docker) ", line)] if log.exists() else []

    def mutations(self) -> list[str]:
        return [call for call in self.calls() if call.split()[0] in ("apt-get", "systemctl") and call.split()[1] not in ("is-enabled", "is-active")] + [call for call in self.calls() if call in ("tailscale logout",)]

    def file(self, key: str, name: str) -> Path:
        return self.paths[key] / name

    def recap(self, result: subprocess.CompletedProcess[str]) -> dict[str, int]:
        line = next(line for line in result.stdout.splitlines() if re.match(r"^laptop\s+:", line))
        return {name: int(count) for name, count in re.findall(r"(\w+)=(\d+)", line)}

    def test_a_first_run_installs_configures_starts_and_checks_the_node(self) -> None:
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        recap = self.recap(result)
        self.assertEqual((recap["failed"], recap["unreachable"]), (0, 0))

        binary = self.file("github_runner_cluster_native_bin_dir", "k3s")
        self.assertEqual(binary.read_text(), self.binary)
        self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o755)

        config_dir = "github_runner_cluster_native_config_dir"
        config = yaml.safe_load(self.file(config_dir, "config.yaml").read_text())
        self.assertEqual(config, {
            "server": f"https://server1.{DOMAIN}:6443",
            "token-file": str(self.file(config_dir, "token")),
            "node-name": "laptop",
            "vpn-auth-file": str(self.file(config_dir, "vpn-auth")),
            "resolv-conf": "/run/github-runner-k3s/resolv.conf",
        })
        self.assertEqual(self.file(config_dir, "token").read_text(), TOKEN)
        self.assertEqual(self.file(config_dir, "vpn-auth").read_text(), f"name=tailscale,joinKey={JOIN_KEY}")
        for name in ("config.yaml", "token", "vpn-auth"):
            self.assertEqual(stat.S_IMODE(self.file(config_dir, name).stat().st_mode), 0o600, name)

        lib = "github_runner_cluster_native_lib_dir"
        self.assertEqual((self.file(lib, "bin/tailscale")).read_text(), (ROLE / "files" / "native" / "tailscale-up-wait.sh").read_text())
        self.assertEqual((self.file(lib, "write-resolv-conf")).read_text(), (ROLE / "files" / "native" / "write-resolv-conf.sh").read_text())
        self.assertTrue(os.access(self.file(lib, "bin/tailscale"), os.X_OK))
        self.assertTrue((self.state / "tailscale-version").read_text() == TAILSCALE_VERSION)
        self.assertEqual(self.paths["github_runner_cluster_native_apt_source"].read_text().split()[0], "deb")

        calls = self.calls()
        self.assertIn(f"apt-get install --yes --no-install-recommends --allow-downgrades tailscale={TAILSCALE_VERSION}", calls)
        self.assertEqual([call for call in calls if call.startswith("systemctl") and "is-" not in call], ["systemctl enable --now tailscaled", "systemctl daemon-reload", "systemctl enable k3s-agent", "systemctl start k3s-agent"])

    def test_the_unit_runs_the_installed_binary_under_the_wrapper_with_the_hosts_join_name(self) -> None:
        self.assertEqual(self.run_role().returncode, 0)
        text = self.file("github_runner_cluster_native_unit_dir", "k3s-agent.service").read_text()
        sections: dict[str, list[str]] = {}
        current = ""
        for line in text.splitlines():
            if line.startswith("["):
                current = line.strip("[]")
                sections[current] = []
            elif line and not line.startswith("#"):
                sections[current].append(line)
        self.assertEqual(set(sections), {"Unit", "Service", "Install"})
        lib, bin_dir = self.paths["github_runner_cluster_native_lib_dir"], self.paths["github_runner_cluster_native_bin_dir"]
        service = sections["Service"]
        self.assertIn(f"ExecStart={bin_dir}/k3s agent", service)
        self.assertIn(f"ExecStartPre={lib}/write-resolv-conf /run/github-runner-k3s/resolv.conf", service)
        self.assertIn(f'Environment="PATH={lib}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"', service)
        self.assertIn(f'Environment="GITHUB_RUNNER_TAILSCALE={self.stubs / "bin" / "tailscale"}"', service)
        self.assertIn(f'Environment="GITHUB_RUNNER_JOIN_HOST=server1.{DOMAIN}"', service)
        for setting in ("Type=exec", "KillMode=process", "Delegate=yes", "Restart=always", "TimeoutStartSec=0", "RuntimeDirectory=github-runner-k3s", "RuntimeDirectoryPreserve=restart"):
            self.assertIn(setting, service)
        self.assertIn("Wants=network-online.target tailscaled.service", sections["Unit"])
        self.assertIn("After=network-online.target tailscaled.service", sections["Unit"])
        self.assertEqual(sections["Install"], ["WantedBy=multi-user.target"])

    def test_node_taints_are_written_as_k3s_node_taints(self) -> None:
        taints = ["github-runner.example.com/excluded=true:NoExecute", "dedicated=ci:NoSchedule"]
        self.assertEqual(self.run_role(host_vars={"github_runner_cluster_node_taints": taints}).returncode, 0)
        config = yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "config.yaml").read_text())
        self.assertEqual(config["node-taint"], taints)

    def test_a_host_that_sets_its_node_name_joins_under_it(self) -> None:
        self.assertEqual(self.run_role(host_vars={"github_runner_cluster_node_name": "laptop-renamed"}).returncode, 0)
        config = yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "config.yaml").read_text())
        self.assertEqual(config["node-name"], "laptop-renamed")

    def test_a_second_run_changes_nothing(self) -> None:
        self.assertEqual(self.run_role().returncode, 0)
        before = len(self.calls())
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual([call for call in self.calls()[before:] if "is-" not in call and not call.startswith(("docker", "tailscale status"))], [])

    def test_a_changed_setting_restarts_the_running_service_once_and_enables_nothing_again(self) -> None:
        self.assertEqual(self.run_role().returncode, 0)
        before = len(self.calls())
        result = self.run_role(host_vars={"github_runner_cluster_node_taints": ["dedicated=ci:NoSchedule"]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls()[before:] if call.startswith("systemctl") and "is-" not in call], ["systemctl restart k3s-agent"])

    def test_a_changed_token_restarts_the_service_too(self) -> None:
        self.assertEqual(self.run_role().returncode, 0)
        before = len(self.calls())
        self.assertEqual(self.run_role(github_runner_cluster_k3s_token="another-token-0123456789").returncode, 0)
        self.assertEqual(self.file("github_runner_cluster_native_config_dir", "token").read_text(), "another-token-0123456789")
        self.assertIn("systemctl restart k3s-agent", self.calls()[before:])

    def test_an_installed_binary_with_the_right_sum_is_not_downloaded_again(self) -> None:
        self.assertEqual(self.run_role().returncode, 0)
        binary = self.file("github_runner_cluster_native_bin_dir", "k3s")
        before = binary.stat().st_ino
        self.assertEqual(self.run_role().returncode, 0)
        self.assertEqual(binary.stat().st_ino, before)

    def test_start_false_writes_everything_and_leaves_the_service_alone(self) -> None:
        result = self.run_role(host_vars={"github_runner_cluster_native_start": False})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.file("github_runner_cluster_native_unit_dir", "k3s-agent.service").is_file())
        self.assertTrue(self.file("github_runner_cluster_native_config_dir", "token").is_file())
        self.assertEqual([call for call in self.calls() if "k3s-agent" in call], [])
        self.assertEqual([call for call in self.calls() if call.startswith("docker exec") and "get node " in call], [])

    def test_a_check_run_changes_nothing_and_says_what_a_real_run_would_do(self) -> None:
        result = self.run_role(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mutations(), [])
        self.assertEqual([path for path in self.root.rglob("*") if path.is_file()], [])
        self.assertIn("apt-get install tailscale=1.102.4 (installed: none)", result.stdout)
        self.assertIn("enable start k3s-agent", result.stdout)
        self.assertEqual(self.recap(result)["failed"], 0)

    def test_a_check_run_after_a_real_one_reports_no_change(self) -> None:
        self.assertEqual(self.run_role().returncode, 0)
        result = self.run_role(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)

    def test_a_client_in_another_tailnet_stops_the_run_before_anything_is_changed(self) -> None:
        self.set_package(TAILSCALE_VERSION)
        self.set_tailscale("Running", "turtle-frog.ts.net")
        result = self.run_role()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("logged in to turtle-frog.ts.net, not the cluster's tailnet example.ts.net", result.stdout)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(self.file("github_runner_cluster_native_config_dir", "token").exists())

    def test_a_client_in_another_tailnet_is_logged_out_before_k3s_starts_when_the_host_says_it_may_leave(self) -> None:
        self.set_package(TAILSCALE_VERSION)
        self.set_tailscale("Running", "turtle-frog.ts.net")
        result = self.run_role(host_vars={"github_runner_cluster_native_tailscale_leave_other_tailnet": True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertEqual(calls.count("tailscale logout"), 1)
        self.assertLess(calls.index("tailscale logout"), calls.index("systemctl start k3s-agent"))

    def test_a_client_that_k3s_did_not_log_in_fails_the_run(self) -> None:
        (self.state / "no-join").touch()
        result = self.run_role(host_vars={"github_runner_cluster_native_ready_timeout": 1})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not on the cluster's tailnet example.ts.net", re.sub(r"\s+", " ", result.stdout))
        self.assertIn("journalctl -u k3s-agent", result.stdout)

    def test_a_check_run_reports_the_logout_and_does_not_make_it(self) -> None:
        self.set_package(TAILSCALE_VERSION)
        self.set_tailscale("Running", "turtle-frog.ts.net")
        result = self.run_role(check=True, host_vars={"github_runner_cluster_native_tailscale_leave_other_tailnet": True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("tailscale logout, leaving turtle-frog.ts.net for example.ts.net", result.stdout)
        self.assertNotIn("tailscale logout", self.calls())

    def test_a_logged_out_client_is_left_for_k3s_to_log_in(self) -> None:
        self.set_package(TAILSCALE_VERSION)
        self.set_tailscale("NeedsLogin", None)
        result = self.run_role(host_vars={"github_runner_cluster_native_start": False})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls() if call.startswith("tailscale") and not call.startswith("tailscale status")], [])

    def test_the_package_at_the_pin_is_not_installed_again_and_another_is_replaced(self) -> None:
        self.set_package(TAILSCALE_VERSION)
        self.set_tailscale("NeedsLogin", None)
        self.assertEqual(self.run_role(host_vars={"github_runner_cluster_native_start": False}).returncode, 0)
        self.assertEqual([call for call in self.calls() if call.startswith("apt-get")], [])
        self.set_package("1.84.0")
        self.assertEqual(self.run_role(host_vars={"github_runner_cluster_native_start": False}).returncode, 0)
        self.assertEqual((self.state / "tailscale-version").read_text(), TAILSCALE_VERSION)

    def test_install_false_leaves_the_hosts_tailscale_alone(self) -> None:
        self.set_tailscale("NeedsLogin", None)
        result = self.run_role(host_vars={"github_runner_cluster_native_tailscale_install": False, "github_runner_cluster_native_start": False})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call for call in self.calls() if call.startswith(("apt-get", "dpkg-query"))], [])
        self.assertFalse(self.paths["github_runner_cluster_native_apt_source"].exists())

    def test_a_client_that_does_not_answer_stops_the_run(self) -> None:
        self.set_package(TAILSCALE_VERSION)
        result = self.run_role()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("status --json failed", result.stdout)
        self.assertFalse(self.file("github_runner_cluster_native_config_dir", "token").exists())

    def test_an_unsupported_architecture_stops_the_run_before_anything_is_changed(self) -> None:
        self.facts["architecture"] = "riscv64"
        result = self.run_role()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not an architecture a native k3s node supports", result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual([path for path in self.root.rglob("*") if path.is_file()], [])

    def test_a_host_that_is_not_linux_under_systemd_is_refused(self) -> None:
        for change in ({"system": "Darwin"}, {"service_mgr": "openrc"}):
            with self.subTest(change=change):
                self.facts = {**FACTS, **change}
                result = self.run_role()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("needs a Linux host that systemd runs", result.stdout)
                self.assertEqual(self.calls(), [])

    def test_the_package_install_is_for_debian_hosts_only(self) -> None:
        self.facts = {**FACTS, "os_family": "RedHat", "distribution": "Fedora"}
        result = self.run_role()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Debian and Ubuntu only", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_settings_a_native_node_needs_are_checked_first(self) -> None:
        cases = [
            ({"github_runner_cluster_tailnet_domain": ""}, "github_runner_cluster_tailnet_domain is not set"),
            ({"github_runner_cluster_native_k3s_sha256": "ABC"}, "must be empty or 64 lower-case hexadecimal digits"),
            ({"github_runner_cluster_k3s_token": ""}, "github_runner_cluster_k3s_token is not set"),
            ({"github_runner_cluster_k3s_version": "latest"}, "must be a pinned rancher/k3s tag"),
            ({"github_runner_cluster_node_role": "server"}, "cannot be a server or bootstrap the cluster"),
            ({"github_runner_cluster_node_mode": "container"}, "must be one of: compose, native"),
            ({"github_runner_cluster_network_mode": "host"}, "which sets github_runner_cluster_node_mode: native"),
        ]
        for host_vars, message in cases:
            with self.subTest(host_vars=host_vars):
                result = self.run_role(host_vars=host_vars)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, re.sub(r"\s+", " ", result.stdout + result.stderr))
                self.assertEqual(self.mutations(), [])

    def test_a_sum_that_the_download_does_not_have_is_refused_and_installs_nothing(self) -> None:
        result = self.run_role(host_vars={"github_runner_cluster_native_k3s_sha256": "0" * 64, "github_runner_cluster_native_start": False})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.file("github_runner_cluster_native_bin_dir", "k3s").exists())

    def test_a_sum_that_matches_the_download_installs_it(self) -> None:
        result = self.run_role(host_vars={"github_runner_cluster_native_k3s_sha256": self.digest, "github_runner_cluster_native_start": False})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.file("github_runner_cluster_native_bin_dir", "k3s").read_text(), self.binary)

    def test_a_downgrade_is_refused_before_anything_is_changed(self) -> None:
        installed = self.file("github_runner_cluster_native_bin_dir", "k3s")
        installed.write_text('#!/bin/sh\necho "k3s version v1.35.0+k3s1 (stand-in)"\n')
        installed.chmod(0o755)
        result = self.run_role()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("newer than v1.34.1-k3s1; k3s cannot be downgraded", re.sub(r"\s+", " ", result.stdout))
        self.assertEqual(self.mutations(), [])
        self.assertIn("v1.35.0", installed.read_text())

    def test_a_deliberate_jump_is_allowed_when_asked_for(self) -> None:
        installed = self.file("github_runner_cluster_native_bin_dir", "k3s")
        installed.write_text('#!/bin/sh\necho "k3s version v1.35.0+k3s1 (stand-in)"\n')
        installed.chmod(0o755)
        result = self.run_role(github_runner_cluster_allow_k3s_version_jump=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(installed.read_text(), self.binary)

    def test_the_run_ends_with_a_node_that_a_server_sees_ready_and_a_node_that_is_not_is_reported(self) -> None:
        self.assertEqual(self.run_role().returncode, 0)
        reads = [call for call in self.calls() if call.startswith("docker exec") and "kubectl get node " in call or "kubectl get lease" in call]
        self.assertEqual(len(reads), 2)
        stand_in(self.stubs / "bin" / "docker", 'case "$*" in\n  "ps "*) echo running ;;\n  *"kubectl get nodes"*) printf "server1 v1.34.1+k3s1 true\\n" ;;\n  *"kubectl get node "*) echo False ;;\n  *"kubectl get lease"*) echo 2000-01-01T00:00:00Z ;;\nesac\n')
        result = self.run_role(host_vars={"github_runner_cluster_native_ready_timeout": 1}, github_runner_cluster_k3s_token="another-token-0123456789")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not come up as a node of the cluster", re.sub(r"\s+", " ", result.stdout))
        self.assertIn("journalctl -u k3s-agent", result.stdout)

    def test_a_service_that_is_not_running_after_the_start_fails_the_run(self) -> None:
        stand_in(self.stubs / "bin" / "systemctl", 'echo "systemctl $*" >> "$STUB/calls.log"\ncase "$1" in\n  is-enabled) echo disabled; exit 1 ;;\n  is-active) echo failed; exit 3 ;;\nesac\n')
        result = self.run_role()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("k3s-agent is failed on laptop", result.stdout)

    def test_no_secret_is_in_the_output(self) -> None:
        for check in (False, True):
            with self.subTest(check=check):
                result = self.run_role("-vvv", check=check)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)
                self.assertNotIn(JOIN_KEY, result.stdout + result.stderr)

    def test_a_node_joining_a_cluster_outside_the_inventory_cannot_be_confirmed_by_a_server_and_says_so(self) -> None:
        result = self.run_role(host_vars={"github_runner_cluster_server_url": "https://elsewhere.example.ts.net:6443"}, only_the_node=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = re.sub(r"\s+", " ", result.stdout)
        self.assertIn("bootstraps no cluster of its own, so no server of this inventory can confirm that laptop is Ready", text)
        self.assertEqual([call for call in self.calls() if call.startswith("docker")], [])
        config = yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "config.yaml").read_text())
        self.assertEqual(config["server"], "https://elsewhere.example.ts.net:6443")


if __name__ == "__main__":
    unittest.main()
