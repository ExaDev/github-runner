"""Tests for the cluster role's link reader (roles/github_runner_cluster/files/node-link.sh) run as the role runs it, and for tasks/link.yml, which chooses the interface it reads (tests/cluster_link/link.yml runs it through the role). The Linux reading of sysfs is held to the kernel's tree in test_fleet_role_probe.py's LinkTest, through the probe that sources the same file; here it is the macOS reading, against stand-ins for networksetup and route, and which interface the role asks about."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
READER = ROOT / "roles" / "github_runner_cluster" / "files" / "node-link.sh"
PLAYBOOK = ROOT / "tests" / "cluster_link" / "link.yml"
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook")
LINK_LABEL = "github-runner.exadev.com/link"

# What `networksetup -listallhardwareports` prints on a Mac with built-in Wi-Fi, a USB Ethernet adapter and Thunderbolt ports, in its own layout.
HARDWARE_PORTS = textwrap.dedent(
    """\

    Hardware Port: Ethernet Adapter (en4)
    Device: en4
    Ethernet Address: 02:00:00:00:00:04

    Hardware Port: USB 10/100/1000 LAN
    Device: en7
    Ethernet Address: 02:00:00:00:00:07

    Hardware Port: Thunderbolt Ethernet Slot 0
    Device: en8
    Ethernet Address: 02:00:00:00:00:08

    Hardware Port: Thunderbolt Bridge
    Device: bridge0
    Ethernet Address: 02:00:00:00:00:00

    Hardware Port: Wi-Fi
    Device: en0
    Ethernet Address: 02:00:00:00:00:01

    Hardware Port: Thunderbolt 1
    Device: en1
    Ethernet Address: 02:00:00:00:00:02

    VLAN Configurations
    ===================
    """
)


class Mac:
    """A scratch directory with stand-ins for a Mac's networksetup and route on a PATH, route answering that the default route leaves by `default_interface`."""

    def __init__(self, default_interface: str | None) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        (self.dir / "ports").write_text(HARDWARE_PORTS)
        self.write("networksetup", f'[ "$*" = -listallhardwareports ] || exit 2\ncat {self.dir / "ports"}\n')
        route = "echo 'route: writing to routing socket: not in table' >&2\nexit 1\n" if default_interface is None else (
            f'[ "$*" = "-n get default" ] || exit 2\nprintf "   route to: default\\ndestination: default\\n    gateway: 192.0.2.1\\n  interface: {default_interface}\\n      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>\\n"\n'
        )
        self.write("route", route)

    def write(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def env(self) -> dict[str, str]:
        return {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "NODE_LINK_SYSTEM": "Darwin"}

    def cleanup(self) -> None:
        self.tmp.cleanup()


class MacReaderTest(unittest.TestCase):
    def read(self, *args: str, default_interface: str | None = "en0") -> dict[str, str]:
        mac = Mac(default_interface)
        self.addCleanup(mac.cleanup)
        completed = subprocess.run([str(READER), *args], env=mac.env(), capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_the_wi_fi_port_is_wifi(self) -> None:
        self.assertEqual(self.read("en0"), {"interface": "en0", "link": "wifi"})

    def test_a_port_named_for_ethernet_or_a_lan_is_wired(self) -> None:
        for interface in ("en4", "en7", "en8"):
            with self.subTest(interface):
                self.assertEqual(self.read(interface)["link"], "wired")

    def test_a_thunderbolt_port_or_bridge_is_unknown(self) -> None:
        for interface in ("en1", "bridge0"):
            with self.subTest(interface):
                self.assertEqual(self.read(interface)["link"], "unknown")

    def test_an_interface_networksetup_does_not_list_is_unknown(self) -> None:
        self.assertEqual(self.read("utun3"), {"interface": "utun3", "link": "unknown"})

    def test_a_device_name_that_only_begins_another_is_not_it(self) -> None:
        # en1 is listed; en10 is not, and must not be read as en1's port.
        self.assertEqual(self.read("en10")["link"], "unknown")

    def test_with_no_interface_given_the_default_routes_is_read(self) -> None:
        self.assertEqual(self.read(default_interface="en7"), {"interface": "en7", "link": "wired"})

    def test_with_no_default_route_the_link_is_unknown_and_there_is_no_interface(self) -> None:
        self.assertEqual(self.read(default_interface=None), {"interface": "", "link": "unknown"})

    def test_sourced_it_defines_the_readers_and_prints_nothing(self) -> None:
        mac = Mac("en0")
        self.addCleanup(mac.cleanup)
        completed = subprocess.run(["bash", "-c", f'. {READER} && link_type "$(default_route_interface)"'], env=mac.env(), capture_output=True, text=True, check=False)
        self.assertEqual((completed.returncode, completed.stdout), (0, "wifi\n"), completed.stderr)


class RoleTest(unittest.TestCase):
    """tasks/link.yml through the role: which interface it reads, and the label it gives the node."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.home = self.dir / "home"
        self.home.mkdir()

    def colima_profile(self, colima_home: Path, profile: str, *, mode: str, interface: str, address: bool) -> None:
        """A Colima profile's colima.yaml as Colima writes it on start, every network key present."""
        directory = colima_home / profile
        directory.mkdir(parents=True)
        network = {"address": address, "mode": mode, "interface": interface, "preferredRoute": False, "dns": None, "dnsHosts": {}, "hostAddresses": False, "gatewayAddress": "192.168.5.2"}
        (directory / "colima.yaml").write_text(yaml.safe_dump({"cpu": 4, "memory": 8, "vmType": "vz", "network": network}))

    def run_role(self, *, system: str, env: dict[str, str], **extra: object) -> dict[str, object]:
        output = self.dir / "link.json"
        variables = {"link_system": system, "link_home": str(self.home), "link_output": str(output), "github_runner_cluster_role_controller": True, **extra}
        completed = subprocess.run(
            [ANSIBLE_PLAYBOOK, str(PLAYBOOK), "-e", json.dumps(variables)],
            env={**env, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_NOCOLOR": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result: dict[str, object] = json.loads(output.read_text())
        return result

    def mac(self, default_interface: str | None = "en0") -> dict[str, str]:
        mac = Mac(default_interface)
        self.addCleanup(mac.cleanup)
        return mac.env()

    def colima(self, profile: str = "node", colima_home: str = "") -> dict[str, object]:
        return {"github_runner_cluster_effective_runtime": {"kind": "colima", "colima_profile": profile}, "github_runner_cluster_runtime_colima_home": colima_home}

    def test_a_bridged_colima_vm_reads_the_interface_it_is_bridged_to_not_the_macs_default_route(self) -> None:
        # The Mac's own traffic leaves by its USB adapter, as macOS prefers Ethernet, while the VM stays bridged to the Wi-Fi.
        home = self.dir / "colima-node"
        self.colima_profile(home, "node", mode="bridged", interface="en0", address=True)
        result = self.run_role(system="Darwin", env=self.mac(default_interface="en7"), **self.colima(colima_home=str(home)))
        self.assertEqual((result["link"], result["interface"]), ("wifi", "en0"))
        self.assertIn(f"{LINK_LABEL}=wifi", result["labels"])

    def test_a_vm_bridged_to_the_usb_adapter_is_wired(self) -> None:
        home = self.dir / "colima-node"
        self.colima_profile(home, "node", mode="bridged", interface="en7", address=True)
        result = self.run_role(system="Darwin", env=self.mac(default_interface="en0"), **self.colima(colima_home=str(home)))
        self.assertEqual((result["link"], result["interface"]), ("wired", "en7"))

    def test_a_profile_in_colimas_default_home_is_read_from_it(self) -> None:
        self.colima_profile(self.home / ".colima", "default", mode="bridged", interface="en7", address=True)
        result = self.run_role(system="Darwin", env=self.mac(default_interface="en0"), **self.colima(profile="default"))
        self.assertEqual(result["interface"], "en7")

    def test_a_vm_that_is_not_bridged_uses_the_macs_routes(self) -> None:
        home = self.dir / "colima-node"
        for mode, address in (("shared", False), ("shared", True), ("bridged", False)):
            with self.subTest(mode=mode, address=address):
                if (home / "node").exists():
                    (home / "node" / "colima.yaml").unlink()
                    (home / "node").rmdir()
                self.colima_profile(home, "node", mode=mode, interface="en0", address=address)
                result = self.run_role(system="Darwin", env=self.mac(default_interface="en7"), **self.colima(colima_home=str(home)))
                self.assertEqual((result["link"], result["interface"]), ("wired", "en7"))

    def test_a_mac_runtime_that_is_not_colima_uses_the_macs_routes(self) -> None:
        result = self.run_role(system="Darwin", env=self.mac(default_interface="en0"), github_runner_cluster_effective_runtime={"kind": "docker_desktop", "colima_profile": ""})
        self.assertEqual((result["link"], result["interface"]), ("wifi", "en0"))

    def test_a_linux_host_reads_the_interface_its_default_route_leaves_by(self) -> None:
        net = self.dir / "net"
        device = net / "devices" / "pci0000:00" / "0000:00:14.3" / "net" / "wlp2s0"
        (device / "wireless").mkdir(parents=True)
        (net / "class" / "net").mkdir(parents=True)
        (net / "class" / "net" / "wlp2s0").symlink_to(device)
        (net / "route").write_text("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\nwlp2s0\t00000000\t0100000A\t0003\t0\t0\t600\t00000000\t0\t0\t0\n")
        env = {**os.environ, "NODE_LINK_SYSTEM": "Linux", "SYS_CLASS_NET": str(net / "class" / "net"), "PROC_NET_ROUTE": str(net / "route")}
        result = self.run_role(system="Linux", env=env)
        self.assertEqual((result["link"], result["interface"]), ("wifi", "wlp2s0"))
        self.assertIn(f"{LINK_LABEL}=wifi", result["labels"])


if __name__ == "__main__":
    unittest.main()
