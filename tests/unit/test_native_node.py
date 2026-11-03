"""Tests the github_runner_cluster role's native node mode: the filters (plugins/filter/cluster_native.py), the helper scripts the k3s-agent and k3s units run, the unit and configuration templates, the defaults, argument specs and README, and the role's tasks (tests/native/run.yml) against stand-ins for systemctl, tailscale, dpkg-query, apt-get, docker and the kubectl that `k3s kubectl` and `docker exec <container> kubectl` reach on PATH, a local server for the k3s release, and a k3s data directory with a server's real layout, so that no real service, package, tailnet or cluster is touched."""

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

    def test_a_role_file_names_the_role_now_and_a_legacy_agent_counts_as_one(self) -> None:
        role = cluster_native.cluster_native_role
        self.assertEqual(role("", False, False, "server"), {"current": "", "desired": "server"})
        self.assertEqual(role("", True, False, "agent"), {"current": "agent", "desired": "agent"})
        self.assertEqual(role("agent\n", True, False, "server"), {"current": "agent", "desired": "server"})
        self.assertEqual(role("server", False, False, "agent"), {"current": "server", "desired": "agent"})

    def test_an_eligible_host_keeps_the_role_its_file_names(self) -> None:
        role = cluster_native.cluster_native_role
        self.assertEqual(role("agent", True, True, "server"), {"current": "agent", "desired": "agent"})
        self.assertEqual(role("server", False, True, "agent"), {"current": "server", "desired": "server"})
        # The inventory gives an eligible host the role it starts with.
        self.assertEqual(role("", False, True, "server"), {"current": "", "desired": "server"})

    def test_while_exactly_one_unit_runs_its_role_is_the_role_now(self) -> None:
        role = cluster_native.cluster_native_role
        # A demotion that stopped after the role file and before the server unit: the next run takes it up again.
        self.assertEqual(role("agent", True, False, "agent", ["server"]), {"current": "server", "desired": "agent"})
        # A promotion that stopped after the role file and before the agent unit.
        self.assertEqual(role("server", True, False, "server", ["agent"]), {"current": "agent", "desired": "server"})
        # Both or neither running leaves the file to say.
        self.assertEqual(role("agent", True, False, "agent", ["server", "agent"]), {"current": "agent", "desired": "agent"})
        with self.assertRaisesRegex(Exception, "are not node roles"):
            role("agent", True, False, "agent", ["controller"])

    def test_a_role_file_holding_anything_else_is_refused(self) -> None:
        with self.assertRaisesRegex(Exception, "must be 'server' or 'agent'"):
            cluster_native.cluster_native_role("controller", False, False, "agent")

    def test_a_cluster_is_started_only_when_no_other_server_runs_one(self) -> None:
        init = cluster_native.cluster_native_cluster_init
        stopped = {"item": "server2", "rc": 0, "stdout": "stopped"}
        running = {"item": "server2", "rc": 0, "stdout": "running\nserver2 v1.34.1+k3s1 true"}
        own = {"item": "laptop", "rc": 0, "stdout": "running\nlaptop v1.34.1+k3s1 true"}
        self.assertEqual(init([], "laptop"), {"cluster_init": True, "running": [], "unread": []})
        self.assertEqual(init([own, stopped], "laptop"), {"cluster_init": True, "running": [], "unread": []})
        self.assertEqual(init([own, running], "laptop"), {"cluster_init": False, "running": ["server2"], "unread": []})
        unread = init([{"item": "server2", "rc": 1, "stderr": "Error: no route to host\n"}, {"item": "server3", "unreachable": True, "msg": "ssh: connect"}], "laptop")
        self.assertEqual(unread, {"cluster_init": False, "running": [], "unread": ["server2 could not be read (Error: no route to host)", "server3 could not be read (ssh: connect)"]})

    def test_a_server_waits_for_every_other_live_voter_to_be_ready(self) -> None:
        blockers = cluster_native.cluster_native_voter_blockers
        read = {"rc": 0, "stdout": "server1,True,true\nserver2,False,true\nagent1,False,\nlaptop,Unknown,true\nserver3,,true\n"}
        self.assertEqual(blockers(read, "laptop"), ["voter server2 is not Ready (its Ready condition is False)", "voter server3 is not Ready (its Ready condition is unknown)"])
        self.assertEqual(blockers({"rc": 0, "stdout": "server1,True,true\nagent1,False,\n"}, "laptop"), [])
        self.assertEqual(blockers({"rc": 0, "stdout": ""}, "laptop"), [])
        self.assertEqual(blockers({"rc": 0, "stdout": "server1,True,true\nserver2,True,true\nlaptop,True,true\n"}, "laptop", 3), ["only 2 other voters (server1, server2) would remain, fewer than the 3 a change that takes this server's vote away leaves"])
        self.assertEqual(blockers({"rc": 0, "stdout": "server1,True,true\nserver2,True,true\nserver3,True,true\nlaptop,True,true\n"}, "laptop", 3), [])
        self.assertIn("could not be listed (The connection to the server was refused)", blockers({"rc": 1, "stderr": "The connection to the server was refused"}, "laptop")[0])

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

    def role_is(self, role: str, recorded: str | None) -> subprocess.CompletedProcess[str]:
        file = self.tmp / "fleet-role"
        if recorded is not None:
            file.write_text(recorded)
        return subprocess.run(["sh", str(ROLE / "files" / "native" / "fleet-role-is.sh"), role, str(file)], env=self.env(), capture_output=True, text=True, timeout=60, check=False)

    def test_the_role_condition_holds_only_for_the_role_the_file_names(self) -> None:
        self.assertEqual(self.role_is("server", "server").returncode, 0)
        self.assertEqual(self.role_is("agent", "agent\n").returncode, 0)
        refused = self.role_is("agent", "server")
        # 1 to 254 is what systemd reads as a skipped start rather than a failure (systemd.service(5), ExecCondition=).
        self.assertEqual(refused.returncode, 1)
        self.assertIn("names 'server', not agent", refused.stderr)

    def test_without_a_role_file_neither_unit_starts(self) -> None:
        for role in ("server", "agent"):
            with self.subTest(role=role):
                result = self.role_is(role, None)
                self.assertEqual(result.returncode, 1)
                self.assertIn("names 'no role'", result.stderr)

    def node_ip(self, vpn_auth: str, address: str | None) -> subprocess.CompletedProcess[str]:
        """Run write-node-ip.sh with a stand-in Tailscale whose client has ``address`` (none when it is logged out, until `tailscale up` logs it in)."""
        (self.tmp / "vpn-auth").write_text(vpn_auth)
        if address is not None:
            (self.tmp / "address").write_text(address)
        stand_in(self.bin / "tailscale", 'echo "tailscale $*" >> "$LOG"\ncase "$1" in\n  ip) [ -e "$DIR/address" ] && cat "$DIR/address" ;;\n  up) echo 100.64.0.7 > "$DIR/address" ;;\nesac\n')
        output = self.tmp / "server.yaml.d" / "00-node-ip.yaml"
        return subprocess.run(["sh", str(ROLE / "files" / "native" / "write-node-ip.sh"), str(output), str(self.tmp / "vpn-auth")], env=self.env(GITHUB_RUNNER_TAILSCALE=str(self.bin / "tailscale"), DIR=str(self.tmp)), capture_output=True, text=True, timeout=60, check=False)

    def written_node_ip(self) -> Any:
        return yaml.safe_load((self.tmp / "server.yaml.d" / "00-node-ip.yaml").read_text())

    def test_the_node_ip_is_the_clients_tailscale_address_and_nothing_logs_in(self) -> None:
        result = self.node_ip(f"name=tailscale,joinKey={JOIN_KEY}", "100.64.0.5\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.written_node_ip(), {"node-ip": "100.64.0.5"})
        self.assertEqual(self.log.read_text(), "tailscale ip -4\n")
        self.assertEqual(sorted(path.name for path in (self.tmp / "server.yaml.d").iterdir()), ["00-node-ip.yaml"])

    def test_a_client_with_no_address_is_logged_in_as_k3s_would_log_it_in(self) -> None:
        result = self.node_ip(f"name=tailscale,joinKey={JOIN_KEY},controlServerURL=https://headscale.example.com,extraArgs=--accept-dns=false --netfilter-mode=off", None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.written_node_ip(), {"node-ip": "100.64.0.7"})
        self.assertEqual(self.log.read_text().splitlines(), [
            "tailscale ip -4",
            f"tailscale up --authkey {JOIN_KEY} --timeout=30s --reset --login-server https://headscale.example.com --accept-dns=false --netfilter-mode=off",
            "tailscale ip -4",
        ])
        self.assertNotIn(JOIN_KEY, result.stdout + result.stderr)

    def test_tailscales_own_control_plane_takes_no_login_server(self) -> None:
        self.assertEqual(self.node_ip(f"name=tailscale,joinKey={JOIN_KEY}", None).returncode, 0)
        self.assertIn(f"tailscale up --authkey {JOIN_KEY} --timeout=30s --reset\n", self.log.read_text())

    def test_no_address_and_no_join_key_fails_and_writes_nothing(self) -> None:
        result = self.node_ip("name=tailscale", None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("names no joinKey", result.stderr)
        self.assertFalse((self.tmp / "server.yaml.d" / "00-node-ip.yaml").exists())

    def test_an_address_that_is_not_ipv4_fails(self) -> None:
        result = self.node_ip(f"name=tailscale,joinKey={JOIN_KEY}", "fd7a:115c:a1e0::7\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reports no IPv4 address", result.stderr)

    def test_both_scripts_are_executable_and_shellcheck_clean(self) -> None:
        scripts = sorted((ROLE / "files" / "native").glob("*.sh"))
        self.assertEqual([script.name for script in scripts], ["fleet-role-is.sh", "tailscale-up-wait.sh", "write-node-ip.sh", "write-resolv-conf.sh"])
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
        derived = {
            "github_runner_cluster_native_service",
            "github_runner_cluster_native_agent_service",
            "github_runner_cluster_native_server_service",
            "github_runner_cluster_native_join_url",
            "github_runner_cluster_native_join_host",
            "github_runner_cluster_native_role_file",
            "github_runner_cluster_native_restart_marker",
        }
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

    def test_the_units_and_configurations_are_templates_the_role_has(self) -> None:
        templates = sorted(path.name for path in (ROLE / "templates" / "native").iterdir())
        self.assertEqual(templates, ["agent.yaml.j2", "join.yaml.j2", "k3s-agent.service.j2", "k3s.service.j2", "server.yaml.j2"])
        config = (ROLE / "tasks" / "native" / "config.yml").read_text()
        for name in ("agent.yaml.j2", "server.yaml.j2", "join.yaml.j2", "native/{{ item }}.service.j2"):
            self.assertIn(name, config)
        self.assertEqual((DEFAULTS["github_runner_cluster_native_agent_service"], DEFAULTS["github_runner_cluster_native_server_service"]), ("k3s-agent", "k3s"))

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
                    # A command run through another server's API takes become exactly when that server is native (api.yml).
                    self.assertIn(task.get("become"), (None, True, "{{ github_runner_cluster_native_api_become }}"))
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
esac
# What the role file named when a k3s unit was started, which is what its ExecCondition would have read.
case "$command:$unit" in
  enable:k3s*|start:k3s*|restart:k3s*) if [ "$command" != enable ] || [ -n "$now" ]; then echo "$command $unit $(cat "$ROLE_FILE" 2>/dev/null || echo none)" >> "$STUB/starts.log"; fi ;;
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
        return [line for line in log.read_text().splitlines() if re.match(r"^(systemctl|tailscale|apt-get|dpkg-query|docker|kubectl) ", line)] if log.exists() else []

    def mutations(self, since: int = 0) -> list[str]:
        """The calls that change something, from the ``since``-th call on."""
        return [call for call in self.calls()[since:] if call.split()[0] in ("apt-get", "systemctl") and call.split()[1] not in ("is-enabled", "is-active")] + [call for call in self.calls()[since:] if call in ("tailscale logout",) or (call.startswith("kubectl ") and call.split()[1] in ("cordon", "uncordon", "drain", "label", "annotate"))]

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
        config = yaml.safe_load(self.file(config_dir, "agent.yaml").read_text())
        self.assertEqual(config, {
            "server": f"https://server1.{DOMAIN}:6443",
            "token-file": str(self.file(config_dir, "token")),
            "node-name": "laptop",
            "vpn-auth-file": str(self.file(config_dir, "vpn-auth")),
            "resolv-conf": "/run/github-runner-k3s/resolv.conf",
        })
        self.assertEqual(self.file(config_dir, "token").read_text(), TOKEN)
        self.assertEqual(self.file(config_dir, "vpn-auth").read_text(), f"name=tailscale,joinKey={JOIN_KEY}")
        self.assertFalse(self.file(config_dir, "server.yaml").exists())
        self.assertEqual(self.file("github_runner_cluster_native_data_dir", "fleet-role").read_text(), "agent")
        for name in ("agent.yaml", "token", "vpn-auth"):
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
        self.assertIn(f"ExecStart={bin_dir}/k3s agent --config {self.paths['github_runner_cluster_native_config_dir']}/agent.yaml", service)
        self.assertIn(f"ExecCondition={lib}/fleet-role-is agent {self.paths['github_runner_cluster_native_data_dir']}/fleet-role", service)
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
        config = yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "agent.yaml").read_text())
        self.assertEqual(config["node-taint"], taints)

    def test_a_host_that_sets_its_node_name_joins_under_it(self) -> None:
        self.assertEqual(self.run_role(host_vars={"github_runner_cluster_node_name": "laptop-renamed"}).returncode, 0)
        config = yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "agent.yaml").read_text())
        self.assertEqual(config["node-name"], "laptop-renamed")

    def test_a_second_run_changes_nothing(self) -> None:
        self.assertEqual(self.run_role().returncode, 0)
        before = len(self.calls())
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recap(result)["changed"], 0, result.stdout)
        self.assertEqual([call for call in self.calls()[before:] if "is-" not in call and not call.startswith(("docker", "tailscale status", "kubectl get"))], [])

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
        self.assertEqual([call for call in self.calls() if "k3s-agent" in call and " is-active " not in call], [])
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
            ({"github_runner_cluster_regrade": True}, "a native server has none yet"),
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
        reads = [call for call in self.calls() if call.startswith("docker exec") and (" kubectl get node " in call or " kubectl get lease " in call)]
        self.assertEqual(len(reads), 2)
        (self.state / "node-ready").write_text("False,")
        (self.state / "lease").write_text("2000-01-01T00:00:00Z")
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
        self.assertEqual([call for call in self.calls() if call.startswith(("docker", "kubectl"))], [])
        config = yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "agent.yaml").read_text())
        self.assertEqual(config["server"], "https://elsewhere.example.ts.net:6443")


    # --- A native server ---

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
        return [call for call in self.calls()[since:] if call.startswith("systemctl") and " is-" not in call and "daemon-reload" not in call]

    def role_file(self) -> str:
        return self.file("github_runner_cluster_native_data_dir", "fleet-role").read_text()

    def test_a_native_bootstrap_server_starts_a_new_cluster_with_the_docker_servers_flags(self) -> None:
        result = self.run_role(host_vars={"github_runner_cluster_bootstrap": True, "github_runner_cluster_etcd_heartbeat_interval": 500, "github_runner_cluster_etcd_election_timeout": 5000}, only_the_node=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config_dir = "github_runner_cluster_native_config_dir"
        server = yaml.safe_load(self.file(config_dir, "server.yaml").read_text())
        self.assertEqual(server, {
            "token-file": str(self.file(config_dir, "token")),
            "vpn-auth-file": str(self.file(config_dir, "vpn-auth")),
            "node-name": "laptop",
            "disable": ["traefik"],
            "disable-network-policy": True,
            "write-kubeconfig-mode": "0600",
            "tls-san": [f"laptop.{DOMAIN}"],
            "etcd-arg": ["heartbeat-interval=500", "election-timeout=5000"],
            "resolv-conf": "/run/github-runner-k3s/resolv.conf",
        })
        self.assertEqual(yaml.safe_load(self.file(config_dir, "server.yaml.d/10-join.yaml").read_text()), {"cluster-init": True})
        for name in ("server.yaml", "server.yaml.d/10-join.yaml"):
            self.assertEqual(stat.S_IMODE(self.file(config_dir, name).stat().st_mode), 0o600, name)
        self.assertEqual(self.role_file(), "server")
        self.assertEqual(self.unit_calls(), ["systemctl enable --now tailscaled", "systemctl enable k3s", "systemctl start k3s"])
        self.assertEqual(self.starts(), ["start k3s server"])
        # The node is waited for through its own server, as root.
        self.assertTrue(any(call.startswith("kubectl get node laptop") for call in self.calls()))
        self.assertTrue(any(call.startswith("kubectl get lease") for call in self.calls()))
        self.assertFalse(any(call.startswith("docker") for call in self.calls()))

    def test_the_server_unit_runs_k3s_server_under_its_role_condition_with_the_mesh_address(self) -> None:
        self.assertEqual(self.run_role(host_vars={"github_runner_cluster_bootstrap": True}, only_the_node=True).returncode, 0)
        text = self.file("github_runner_cluster_native_unit_dir", "k3s.service").read_text()
        service = [line for line in text.split("[Service]")[1].split("[Install]")[0].splitlines() if line and not line.startswith("#")]
        lib, config = self.paths["github_runner_cluster_native_lib_dir"], self.paths["github_runner_cluster_native_config_dir"]
        self.assertIn(f"ExecCondition={lib}/fleet-role-is server {self.paths['github_runner_cluster_native_data_dir']}/fleet-role", service)
        self.assertIn(f"ExecStartPre={lib}/write-node-ip {config}/server.yaml.d/00-node-ip.yaml {config}/vpn-auth", service)
        self.assertIn(f"ExecStart={self.paths['github_runner_cluster_native_bin_dir']}/k3s server --config {config}/server.yaml", service)
        # Everything else is the agent unit's: one runs at a time, under the same limits.
        agent = [line for line in self.file("github_runner_cluster_native_unit_dir", "k3s-agent.service").read_text().split("[Service]")[1].split("[Install]")[0].splitlines() if line and not line.startswith("#")]
        self.assertEqual([line for line in service if not line.startswith(("ExecCondition=", "ExecStart"))], [line for line in agent if not line.startswith(("ExecCondition=", "ExecStart"))])
        for helper in ("write-node-ip", "fleet-role-is"):
            self.assertEqual(self.file("github_runner_cluster_native_lib_dir", helper).read_text(), (ROLE / "files" / "native" / f"{helper}.sh").read_text())

    def run_three(self, *args: str, laptop: dict[str, Any] | None = None, check: bool = False, **extra: Any) -> subprocess.CompletedProcess[str]:
        """Run the role on laptop in a group of the Docker servers server1 and server2 and laptop, a native server unless ``laptop`` says otherwise."""
        return self.run_role(*args, host_vars={"github_runner_cluster_node_role": "server", **(laptop or {})}, check=check, second_server=True, **extra)

    def test_a_native_server_joins_a_cluster_of_docker_servers_as_they_do(self) -> None:
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\n")
        result = self.run_three()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config_dir = "github_runner_cluster_native_config_dir"
        self.assertEqual(yaml.safe_load(self.file(config_dir, "server.yaml.d/10-join.yaml").read_text()), {"server": f"https://server1.{DOMAIN}:6443"})
        # The test inventory lists laptop first (yaml.safe_dump sorts the hosts), and the SANs are in inventory order.
        self.assertEqual(yaml.safe_load(self.file(config_dir, "server.yaml").read_text())["tls-san"], [f"laptop.{DOMAIN}", f"server1.{DOMAIN}", f"server2.{DOMAIN}"])
        unit = self.file("github_runner_cluster_native_unit_dir", "k3s.service").read_text()
        self.assertIn(f'Environment="GITHUB_RUNNER_JOIN_HOST=server1.{DOMAIN}"', unit)
        # The voters were read through the bootstrap server's container before the server started.
        calls = self.calls()
        read = next(index for index, call in enumerate(calls) if call.startswith("docker exec github-runner-k3s kubectl get nodes") and "etcd}" in call)
        self.assertLess(read, calls.index("systemctl start k3s"))
        self.assertEqual(self.starts(), ["start k3s server"])

    def test_a_native_server_does_not_join_while_another_voter_is_not_ready(self) -> None:
        (self.state / "voters").write_text("server1,True,true\nserver2,False,true\n")
        result = self.run_three()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("voter server2 is not Ready (its Ready condition is False)", re.sub(r"\s+", " ", result.stdout))
        self.assertEqual(self.starts(), [])
        self.assertNotIn("systemctl start k3s", self.calls())

    def test_an_agent_is_promoted_in_place_once_its_node_is_empty(self) -> None:
        self.assertEqual(self.run_three(laptop={"github_runner_cluster_node_role": "agent", "github_runner_cluster_voter_eligible": False}).returncode, 0)
        # A three-host group with laptop as an agent has one Docker server: the first run is the laptop as an agent of server1, the second makes it the third server.
        self.assertEqual(self.role_file(), "agent")
        password = self.root / "etc" / "rancher" / "node" / "password"
        password.parent.mkdir(parents=True)
        password.write_text("node-password")
        before = len(self.calls())
        result = self.run_three()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()[before:]
        order = [
            "kubectl cordon laptop",
            next(call for call in calls if call.startswith("kubectl get pods --all-namespaces --selector actions-ephemeral-runner=True --field-selector spec.nodeName=laptop,status.phase!=Succeeded,status.phase!=Failed")),
            next(call for call in calls if call.startswith("kubectl drain laptop --ignore-daemonsets --delete-emptydir-data")),
            "systemctl disable --now k3s-agent",
            "systemctl start k3s",
            "kubectl uncordon laptop",
        ]
        self.assertEqual([call for call in calls if call in order], order)
        self.assertEqual(self.role_file(), "server")
        self.assertEqual(self.starts()[-1], "start k3s server")
        self.assertFalse((self.state / "active-k3s-agent").exists())
        self.assertFalse((self.state / "enabled-k3s-agent").exists())
        self.assertEqual(password.read_text(), "node-password")
        # The cordon and drain went through another server, and the uncordon through this one, which now serves the API.
        self.assertIn("docker exec github-runner-k3s kubectl cordon laptop", calls)
        self.assertNotIn("docker exec github-runner-k3s kubectl uncordon laptop", calls)

    def test_a_promotion_whose_runner_pods_do_not_finish_is_abandoned_with_the_node_uncordoned(self) -> None:
        self.assertEqual(self.run_three(laptop={"github_runner_cluster_node_role": "agent"}).returncode, 0)
        (self.state / "runner-pods").write_text("pod/runner-abc12\n")
        before = len(self.calls())
        result = self.run_three(laptop={"github_runner_cluster_native_runner_wait_timeout": 1})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("its node could not be emptied", re.sub(r"\s+", " ", result.stdout))
        calls = self.calls()[before:]
        self.assertIn("kubectl uncordon laptop", calls)
        self.assertNotIn("systemctl disable --now k3s-agent", calls)
        self.assertEqual(self.role_file(), "agent")
        self.assertTrue((self.state / "active-k3s-agent").exists())

    def test_a_running_server_restarts_only_while_every_other_voter_is_ready_and_a_refused_restart_is_made_later(self) -> None:
        self.assertEqual(self.run_three().returncode, 0)
        (self.state / "voters").write_text("server1,True,true\nserver2,Unknown,true\nlaptop,True,true\n")
        taints = {"github_runner_cluster_node_taints": ["dedicated=ci:NoSchedule"]}
        before = len(self.calls())
        refused = self.run_three(laptop=taints)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Not going ahead with the restart of k3s on laptop: voter server2 is not Ready", re.sub(r"\s+", " ", refused.stdout))
        self.assertEqual(self.unit_calls(before), [])
        # The voters were read through this server's own k3s, which is running.
        self.assertTrue(any(call.startswith("kubectl get nodes") and "etcd}" in call for call in self.calls()[before:]))
        self.assertFalse(any(call.startswith("docker exec") and "etcd}" in call for call in self.calls()[before:]))
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\nlaptop,True,true\n")
        before = len(self.calls())
        result = self.run_three(laptop=taints)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.unit_calls(before), ["systemctl restart k3s"])
        before = len(self.calls())
        self.assertEqual(self.run_three(laptop=taints).returncode, 0)
        self.assertEqual(self.unit_calls(before), [])

    def test_a_check_run_reports_a_promotion_and_makes_none(self) -> None:
        self.assertEqual(self.run_three(laptop={"github_runner_cluster_node_role": "agent"}).returncode, 0)
        before = len(self.calls())
        result = self.run_three(check=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("promote k3s on laptop", result.stdout)
        self.assertEqual([call for call in self.mutations(before) if not call.startswith(("apt-get", "systemctl enable --now tailscaled"))], [])
        self.assertEqual(self.role_file(), "agent")

    def demote(self, voters: str = "server1,True,true\nserver2,True,true\nserver3,True,true\nlaptop,True,true\n") -> subprocess.CompletedProcess[str]:
        """Run laptop as a server, lay out its datastore as k3s leaves it, then run it as an agent with the cluster's voters read as ``voters``."""
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\n")
        self.assertEqual(self.run_three().returncode, 0)
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
        self.assertEqual([call for call in self.mutations(before) if not call.startswith(("apt-get", "systemctl enable --now tailscaled"))], [])

    def test_a_demotion_that_would_leave_two_voters_is_refused(self) -> None:
        self.assert_demotion_refused("server1,True,true\nserver2,True,true\nlaptop,True,true\n", "only 2 other voters (server1, server2) would remain, fewer than the 3")

    def test_a_demotion_while_another_voter_is_not_ready_is_refused(self) -> None:
        self.assert_demotion_refused("server1,True,true\nserver2,True,true\nserver3,False,true\nlaptop,True,true\n", "voter server3 is not Ready")

    def test_a_server_is_demoted_once_its_member_is_removed_with_its_datastore_moved_aside_and_its_identity_kept(self) -> None:
        before = len(self.calls())
        result = self.demote()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = self.paths["github_runner_cluster_native_data_dir"]
        self.assertEqual(self.role_file(), "agent")
        self.assertFalse((data / "server" / "db").exists())
        moved = data / "server" / "db.demoted"
        self.assertEqual((moved / "etcd/member/wal/0000000000000000-0000000000000000.wal").read_text(), "wal-bytes")
        self.assertEqual((moved / "snapshots/etcd-snapshot-laptop-1767225600").read_text(), "snapshot")
        # The newest copy replaces the one an earlier demotion left.
        self.assertEqual((moved / "etcd" / "name").read_text(), "laptop-0a1b2c3d")
        for kept in ("server/tls/server-ca.crt", "server/token", "agent/etc/containerd/config.toml"):
            self.assertTrue((data / kept).is_file(), kept)
        calls = self.calls()[before:]
        order = [
            "kubectl cordon laptop",
            "kubectl annotate --overwrite node laptop etcd.k3s.cattle.io/remove=true",
            "systemctl disable --now k3s",
            "kubectl label node laptop node-role.kubernetes.io/control-plane- node-role.kubernetes.io/etcd- node-role.kubernetes.io/master-",
            "kubectl annotate node laptop etcd.k3s.cattle.io/remove- etcd.k3s.cattle.io/removed-node-name- etcd.k3s.cattle.io/node-name- etcd.k3s.cattle.io/node-address-",
            "systemctl enable --now k3s-agent",
            "kubectl uncordon laptop",
        ]
        self.assertEqual([call for call in calls if call in order], order)
        # Everything went through another server's API, since this one stops.
        for call in order:
            if call.startswith("kubectl"):
                self.assertIn("docker exec github-runner-k3s " + call, calls)
        self.assertEqual(self.starts()[-1], "enable k3s-agent agent")
        self.assertFalse((self.state / "active-k3s").exists())

    def test_a_server_whose_member_is_not_removed_is_not_stopped(self) -> None:
        (self.state / "member-stays").touch()
        result = self.demote_quickly()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.state / "active-k3s").exists())
        self.assertTrue(self.wal.exists())
        # The role file already names the agent, so the server is not started again by Restart=always or a reboot; a rerun finishes the demotion.
        self.assertEqual(self.role_file(), "agent")
        (self.state / "member-stays").unlink()
        result = self.run_three(laptop={"github_runner_cluster_node_role": "agent"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.state / "active-k3s").exists())
        self.assertFalse(self.wal.exists())

    def demote_quickly(self) -> subprocess.CompletedProcess[str]:
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\n")
        self.assertEqual(self.run_three().returncode, 0)
        self.wal = self.server_tree()
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\nserver3,True,true\nlaptop,True,true\n")
        return self.run_three(laptop={"github_runner_cluster_node_role": "agent", "github_runner_cluster_native_ready_timeout": 1})

    def test_a_former_member_never_starts_a_second_cluster(self) -> None:
        moved = self.paths["github_runner_cluster_native_data_dir"] / "server" / "db.demoted" / "etcd"
        moved.mkdir(parents=True)
        (moved / "name").write_text("laptop-0a1b2c3d")
        alone = self.run_role(host_vars={"github_runner_cluster_bootstrap": True}, only_the_node=True)
        self.assertNotEqual(alone.returncode, 0)
        self.assertIn("with no server to join", re.sub(r"\s+", " ", alone.stdout))
        self.assertEqual(self.starts(), [])
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\n")
        result = self.run_three(laptop={"github_runner_cluster_bootstrap": True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "server.yaml.d/10-join.yaml").read_text()), {"server": f"https://server1.{DOMAIN}:6443"})

    def test_a_bootstrap_host_with_no_datastore_joins_a_cluster_another_server_runs(self) -> None:
        (self.state / "voters").write_text("server1,True,true\nserver2,True,true\n")
        result = self.run_three(laptop={"github_runner_cluster_bootstrap": True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "server.yaml.d/10-join.yaml").read_text()), {"server": f"https://server1.{DOMAIN}:6443"})

    def test_a_bootstrap_host_with_no_datastore_starts_a_cluster_only_when_every_other_server_is_stopped(self) -> None:
        (self.state / "docker-stopped").touch()
        result = self.run_three(laptop={"github_runner_cluster_bootstrap": True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "server.yaml.d/10-join.yaml").read_text()), {"cluster-init": True})

    def test_a_bootstrap_host_that_cannot_tell_whether_a_cluster_exists_is_refused(self) -> None:
        (self.state / "nodes-unreadable").touch()
        result = self.run_three(laptop={"github_runner_cluster_bootstrap": True}, github_runner_cluster_allow_k3s_version_jump=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("would start a new cluster (cluster-init) unless another server already runs one, and that cannot be told", re.sub(r"\s+", " ", result.stdout))
        self.assertEqual(self.mutations(), [])

    def test_an_agent_from_before_the_role_file_keeps_running_as_an_agent(self) -> None:
        unit = self.file("github_runner_cluster_native_unit_dir", "k3s-agent.service")
        unit.write_text("[Service]\nExecStart=/usr/local/bin/k3s agent\n")
        old = self.file("github_runner_cluster_native_config_dir", "config.yaml")
        old.parent.mkdir(parents=True)
        old.write_text("server: https://server1.example.ts.net:6443\n")
        for name in ("enabled-k3s-agent", "active-k3s-agent"):
            (self.state / name).touch()
        self.set_package(TAILSCALE_VERSION)
        self.set_tailscale("Running", DOMAIN)
        result = self.run_role()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.role_file(), "agent")
        self.assertFalse(old.exists())
        self.assertEqual(self.starts(), ["restart k3s-agent agent"])

    def test_an_eligible_agent_is_staged_as_a_standby_server_and_keeps_its_role(self) -> None:
        result = self.run_role(host_vars={"github_runner_cluster_voter_eligible": True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.role_file(), "agent")
        server = yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "server.yaml").read_text())
        self.assertEqual(server["tls-san"], [f"laptop.{DOMAIN}", f"server1.{DOMAIN}"])
        self.assertNotIn("systemctl start k3s", self.calls())
        # The inventory gives an eligible host only the role it starts with.
        before = len(self.calls())
        result = self.run_three(laptop={"github_runner_cluster_voter_eligible": True})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.role_file(), "agent")
        self.assertEqual(self.unit_calls(before), [])

    def test_a_native_bootstrap_host_is_refused_under_headscale_in_cluster(self) -> None:
        result = self.run_role(host_vars={"github_runner_cluster_bootstrap": True, "github_runner_cluster_mesh": "headscale_in_cluster"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("whose bootstrap server runs the Headscale server as a static pod", re.sub(r"\s+", " ", result.stdout))
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
