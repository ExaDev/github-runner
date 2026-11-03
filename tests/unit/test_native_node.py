"""Tests the github_runner_cluster role's native node mode: the filters (plugins/filter/cluster_native.py), the helper scripts the k3s-agent and k3s units run, the unit and configuration templates, the defaults, argument specs and README, and the role's tasks (tests/native/run.yml) against stand-ins for systemctl, tailscale, dpkg-query, apt-get, docker and the kubectl that `k3s kubectl` and `docker exec <container> kubectl` reach on PATH, a local server for the k3s release, and a k3s data directory with a server's real layout, so that no real service, package, tailnet or cluster is touched."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from native_stand_ins import DOMAIN, FACTS, JOIN_KEY, ROLE, ROOT, TAILSCALE_VERSION, TOKEN, NativeRoleHarness, stand_in, tailscale_status

PLUGIN = ROOT / "plugins" / "filter" / "cluster_native.py"
DEFAULTS: dict[str, Any] = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
SPECS: dict[str, Any] = yaml.safe_load((ROLE / "meta" / "argument_specs.yml").read_text())["argument_specs"]["main"]["options"]
README = (ROLE / "README.md").read_text()

_spec = importlib.util.spec_from_file_location("cluster_native", PLUGIN)
assert _spec is not None and _spec.loader is not None
cluster_native = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cluster_native)


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
            "github_runner_cluster_native_regrade_level_file",
            "github_runner_cluster_native_regrade_started_file",
            "github_runner_cluster_native_regrade_dir",
            "github_runner_cluster_native_regrade_service",
            "github_runner_cluster_native_regrade_commands",
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


class NativeRoleTest(NativeRoleHarness):
    """Runs the role against a native node as an agent, with stand-ins for the host's commands and a local k3s release."""

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

    def test_a_host_that_uses_no_voter_setting_gets_no_node_labels(self) -> None:
        self.assertEqual(self.run_role().returncode, 0)
        config = yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "agent.yaml").read_text())
        self.assertNotIn("node-label", config)

    def test_an_eligible_host_gets_its_voter_labels_as_k3s_node_labels(self) -> None:
        self.assertEqual(self.run_role(host_vars={"github_runner_cluster_voter_eligible": True}).returncode, 0)
        config = yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "agent.yaml").read_text())
        self.assertEqual(config["node-label"], ["github-runner.exadev.com/voter-eligible=true", "github-runner.exadev.com/voter-pinned=false"])

    def test_with_the_role_controller_on_every_host_gets_both_labels_false(self) -> None:
        self.assertEqual(self.run_role(host_vars={"github_runner_cluster_role_controller": True}).returncode, 0)
        config = yaml.safe_load(self.file("github_runner_cluster_native_config_dir", "agent.yaml").read_text())
        self.assertEqual(config["node-label"], ["github-runner.exadev.com/voter-eligible=false", "github-runner.exadev.com/voter-pinned=false"])

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


if __name__ == "__main__":
    unittest.main()
