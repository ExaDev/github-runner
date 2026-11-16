"""Tests for the cluster role's tasks/etcd_lan.yml, run with ansible-playbook (tests/etcd_lan/play.yml) on three servers against a stand-in docker command that records what it is asked and answers from files the test writes: etcd's member list, each server's etcd peer certificate (real certificates, read by the real openssl), whether a server's ping reaches an address, and a server's mesh address. A host with no address reads nothing and keeps its .env; with the addresses alone no member moves; with github_runner_cluster_etcd_peers_over_lan exactly one running member moves per run, and only once every other server's certificate names its own LAN address and every other server reaches the mover's, the mesh being kept otherwise; a member on the LAN stays there and moves back when the setting is off; and the address cannot be removed while .env still starts etcd on it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAY = ROOT / "tests" / "etcd_lan" / "play.yml"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
NAMES = ("node1", "node2", "node3")
LAN = {name: f"192.0.2.1{index}" for index, name in enumerate(NAMES, start=1)}
MESH = {name: f"100.64.0.{index}" for index, name in enumerate(NAMES, start=1)}
# The role's defaults, as the inventory's group variables: below every host's own settings, as role defaults are.
DEFAULTS: dict[str, Any] = yaml.safe_load((ROOT / "roles" / "github_runner_cluster" / "defaults" / "main.yml").read_text())
TOPOLOGY = {"hosts": {name: {"role": "server", "bootstrap": name == "node1", "node_name": name} for name in NAMES}}

# The stand-in docker logs each call with its DOCKER_CONTEXT, and answers: `run` of etcdctl with members.json (or fails while etcd-down exists), `run` of ping with failure when unreachable-<context>-<address> exists, `exec ... cat` of the peer certificate with cert-<container>.pem, and `exec ... tailscale ip -4` with mesh-<container>.
STUB = """\
    #!/bin/sh
    echo "docker ${DOCKER_CONTEXT:--} $*" >> "$STUB_DIR/calls.log"
    case "$1" in
      run)
        case "$*" in
          *etcdctl*) [ -e "$STUB_DIR/etcd-down" ] && { echo "context deadline exceeded" >&2; exit 1; }; cat "$STUB_DIR/members.json" ;;
          *ping*) for last; do :; done; [ -e "$STUB_DIR/unreachable-${DOCKER_CONTEXT:--}-$last" ] && exit 1; exit 0 ;;
        esac ;;
      exec)
        case "$*" in
          *peer-server-client.crt*) cat "$STUB_DIR/cert-$2.pem" ;;
          *tailscale*) cat "$STUB_DIR/mesh-$2" ;;
        esac ;;
    esac
    """


def member(index: int, name: str, host: str) -> dict[str, Any]:
    return {"ID": 1000 + index, "name": f"{name}-0a1b2c3{index}", "peerURLs": [f"https://{host}:2380"], "clientURLs": [f"https://{host}:2379"]}


@unittest.skipIf(shutil.which(ANSIBLE_PLAYBOOK) is None or shutil.which("openssl") is None, "ansible-playbook or openssl is not installed")
class EtcdLanRoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.bin = self.work / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(textwrap.dedent(STUB))
        docker.chmod(0o755)
        (self.work / "out").mkdir()
        self.members(on_lan=())
        for name in NAMES:
            self.certificate(name, named=True)
            (self.work / f"mesh-{name}-k3s").write_text(MESH[name] + "\n")

    def certificate(self, name: str, named: bool) -> None:
        """Writes the peer certificate node ``name``'s container holds: its mesh address always, and its LAN address when ``named``."""
        sans = f"IP:{MESH[name]}" + (f",IP:{LAN[name]}" if named else "")
        key = self.work / f"{name}.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(self.work / f"cert-{name}-k3s.pem"), "-days", "1", "-subj", "/CN=etcd-peer", "-addext", f"subjectAltName={sans}"], check=True, capture_output=True)

    def members(self, on_lan: tuple[str, ...]) -> None:
        listing = [member(index, name, LAN[name] if name in on_lan else MESH[name]) for index, name in enumerate(NAMES, start=1)]
        (self.work / "members.json").write_text(json.dumps({"header": {}, "members": listing}))

    def unreachable(self, from_host: str, address: str) -> None:
        (self.work / f"unreachable-ctx-{from_host}-{address}").touch()

    def run_tasks(self, addresses: bool = True, limit: str = "all", **extra: Any) -> subprocess.CompletedProcess[str]:
        hosts: dict[str, dict[str, Any]] = {}
        for name in NAMES:
            hosts[name] = {
                "github_runner_cluster_container_name": f"{name}-k3s",
                "github_runner_cluster_docker_context": f"ctx-{name}",
                "github_runner_cluster_dir": str(self.work / name),
                "github_runner_cluster_network_mode": "host",
                "github_runner_cluster_lan_cidr": "192.0.2.0/24",
                "stand_in_addresses": f"3: col0    inet {LAN[name]}/24 brd 192.0.2.255 scope global col0",
            }
            if addresses:
                hosts[name]["github_runner_cluster_etcd_lan_address"] = LAN[name]
        (self.work / "inventory.json").write_text(json.dumps({"all": {"vars": DEFAULTS, "hosts": hosts}}))
        extra_file = self.work / "extra.json"
        extra_file.write_text(json.dumps({"github_runner_cluster_topology": TOPOLOGY, "output_dir": str(self.work / "out"), **extra}))
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "STUB_DIR": str(self.work),
            "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"),
            "ANSIBLE_LOCALHOST_WARNING": "false",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
            "ANSIBLE_RETRY_FILES_ENABLED": "false",
        }
        return subprocess.run([ANSIBLE_PLAYBOOK, "-i", str(self.work / "inventory.json"), str(PLAY), "--limit", limit, "-e", f"@{extra_file}"], capture_output=True, text=True, check=False, env=env, cwd=self.work, timeout=300)

    def decision(self, name: str) -> dict[str, Any]:
        result: dict[str, Any] = json.loads((self.work / "out" / f"{name}.json").read_text())
        return result

    def calls(self) -> list[str]:
        log = self.work / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def passed(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_address_reads_nothing_and_changes_nothing(self) -> None:
        result = self.run_tasks(addresses=False)
        self.passed(result)
        self.assertEqual(self.calls(), [])
        for name in NAMES:
            self.assertEqual(self.decision(name), {"peer_on_lan": False, "switch": "", "candidate": "", "target": ""})

    def test_the_addresses_alone_read_the_members_and_move_none(self) -> None:
        self.passed(self.run_tasks())
        for name in NAMES:
            self.assertEqual(self.decision(name)["switch"], "")
            self.assertFalse(self.decision(name)["peer_on_lan"])
        self.assertTrue(all("etcdctl" in call for call in self.calls()), self.calls())

    def test_with_the_setting_on_only_the_first_member_moves_and_the_others_wait(self) -> None:
        result = self.run_tasks(github_runner_cluster_etcd_peers_over_lan=True)
        self.passed(result)
        self.assertEqual(self.decision("node1"), {"peer_on_lan": True, "switch": "to_lan", "candidate": "to_lan", "target": LAN["node1"]})
        for name in ("node2", "node3"):
            self.assertEqual(self.decision(name)["switch"], "")
            self.assertFalse(self.decision(name)["peer_on_lan"])
        self.assertIn("another server's member moves in this run", result.stdout)
        # The other servers were asked to reach node1's address, each through its own Docker context.
        self.assertIn(f"docker ctx-node2 run --rm --network host busybox:1.37 ping -c 3 -W 2 {LAN['node1']}", self.calls())
        self.assertIn(f"docker ctx-node3 run --rm --network host busybox:1.37 ping -c 3 -W 2 {LAN['node1']}", self.calls())

    def test_a_certificate_without_its_server_s_lan_address_holds_the_move(self) -> None:
        self.certificate("node2", named=False)
        result = self.run_tasks(github_runner_cluster_etcd_peers_over_lan=True)
        self.passed(result)
        self.assertEqual(self.decision("node1")["switch"], "")
        self.assertIn("node2 does not yet name its own LAN address", result.stdout)
        # node2's own move needs only the others' certificates, which name theirs.
        self.assertEqual(self.decision("node2")["switch"], "to_lan")

    def test_a_server_that_does_not_reach_the_address_keeps_the_member_on_the_mesh(self) -> None:
        self.unreachable("node3", LAN["node1"])
        result = self.run_tasks(github_runner_cluster_etcd_peers_over_lan=True)
        self.passed(result)
        self.assertEqual(self.decision("node1"), {"peer_on_lan": False, "switch": "", "candidate": "", "target": LAN["node1"]})
        self.assertIn(f"node3 could not reach {LAN['node1']}", result.stdout)
        self.assertEqual(self.decision("node2")["switch"], "to_lan")

    def test_a_member_on_the_lan_stays_there_and_the_next_one_moves(self) -> None:
        self.members(on_lan=("node1",))
        self.passed(self.run_tasks(github_runner_cluster_etcd_peers_over_lan=True))
        self.assertEqual(self.decision("node1"), {"peer_on_lan": True, "switch": "", "candidate": "", "target": ""})
        self.assertEqual(self.decision("node2")["switch"], "to_lan")
        self.assertFalse(self.decision("node3")["peer_on_lan"])

    def test_turned_off_a_member_on_the_lan_moves_back_to_its_mesh_address(self) -> None:
        self.members(on_lan=("node1", "node2"))
        self.passed(self.run_tasks())
        self.assertEqual(self.decision("node1"), {"peer_on_lan": False, "switch": "to_mesh", "candidate": "to_mesh", "target": MESH["node1"]})
        self.assertEqual(self.decision("node2"), {"peer_on_lan": True, "switch": "", "candidate": "to_mesh", "target": MESH["node2"]})
        self.assertEqual(self.decision("node3")["candidate"], "")

    def test_the_address_cannot_be_removed_while_env_starts_etcd_on_it(self) -> None:
        (self.work / "node1").mkdir()
        (self.work / "node1" / ".env").write_text(f"K3S_TOKEN=secret\nK3S_ETCD_LAN_ADDRESS={LAN['node1']}\nK3S_ETCD_PEER_ADDRESS={LAN['node1']}\n")
        result = self.run_tasks(addresses=False, limit="node1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is no longer set", result.stdout)
        self.assertNotIn("secret", result.stdout)

    def test_an_address_on_the_bridge_network_is_refused_before_anything_is_read(self) -> None:
        (self.work / "inventory.json").unlink(missing_ok=True)
        result = self.run_tasks(github_runner_cluster_network_mode="bridge")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("with github_runner_cluster_network_mode bridge", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_cluster_no_server_can_be_read_through_refuses(self) -> None:
        (self.work / "etcd-down").touch()
        result = self.run_tasks(github_runner_cluster_etcd_peers_over_lan=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not be read through any server", result.stdout)


if __name__ == "__main__":
    unittest.main()
