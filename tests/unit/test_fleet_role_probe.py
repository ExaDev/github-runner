"""Tests for scripts/fleet-role-probe.sh, one sample of the role controller's per-node probe. The probe reads real host state, and a stand-in with a flatter structure than the real thing has hidden a bug here before (a `find -L` over sysfs passed flat stand-ins and exited 1 on a real host, from a symlink loop), so each reading is taken from the real thing or from a tree that has its structure:

- the link type from a sysfs tree whose symlinks loop the way the kernel's do (device, subsystem, phy80211), and from the real /sys of the machine running the tests;
- TCP connect times from real listeners, and a connect that fails;
- etcd's metrics from pages a real etcd 3.6.15 three-member cluster served (tests/fleet_role/etcd-metrics), and from a live local cluster when an etcd binary is on the PATH or named by ETCD_BINARY;
- the memory signals in the kernel's own file formats, read by the regrade core's own functions;
- the fdatasync tail from the real fio.

Only the cluster's API is a stand-in (tests/unit/fleet_role_support.py)."""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fleet_role_support import (
    ELECTION_TIMEOUT_MS,
    NAMESPACE,
    REGRADE_CORE,
    SAMPLE_SECONDS,
    TARGET,
    WINDOW_SAMPLES,
    WINDOW_SECONDS,
    Cluster,
    entry,
    node,
    sample,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fleet_role" / "etcd-metrics"
# The members of the cluster the fixtures were captured from (etcd 3.6.15, three members on one host); the page is the first member's.
FIXTURE_SERVER_ID = "d5811b26ae265d01"
FIXTURE_PEER_IDS = ("2b6d7f19fe480be8", "819450a5536d704f")
FIXTURE_LEARNER_ID = "c65e62daf70a677b"
FIXTURE_QUOTA_BYTES = 2 * 1024**3
ETCD_BINARY = os.environ.get("ETCD_BINARY") or shutil.which("etcd")
FIO = shutil.which("fio")


@contextmanager
def listener(host: str = "127.0.0.1") -> Iterator[int]:
    """A TCP server that accepts connections and closes them, on a free port; yields the port."""

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            return

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    server = Server((host, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def closed_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


@contextmanager
def metrics_server(pages: list[str]) -> Iterator[str]:
    """Serves the pages one per request, the last again for any more; yields the metrics URL."""
    remaining = list(pages)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
            page = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/metrics"
    finally:
        server.shutdown()
        server.server_close()


def page(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text()


def cumulative(text: str, family: str, label: str | None = None) -> dict[str, dict[str, float]]:
    """The buckets of a histogram family in a page, by the value of `label` (or one group under "" without it), parsed independently of the probe: upper bound to cumulative count."""
    groups: dict[str, dict[str, float]] = {}
    pattern = re.compile(rf"^{family}_bucket\{{(?P<labels>[^}}]*)\}} (?P<value>\S+)$", re.M)
    for match in pattern.finditer(text):
        labels = dict(re.findall(r'(\w+)="([^"]*)"', match.group("labels")))
        groups.setdefault(labels[label] if label else "", {})[labels["le"]] = float(match.group("value"))
    return groups


def per_bucket(buckets: dict[str, float]) -> dict[str, float]:
    previous = 0.0
    result = {}
    for bound in sorted(buckets, key=lambda b: float("inf") if b == "+Inf" else float(b)):
        result[bound] = buckets[bound] - previous
        previous = buckets[bound]
    return result


def gained(before: dict[str, float], after: dict[str, float]) -> dict[str, int]:
    b, a = per_bucket(before), per_bucket(after)
    return {bound: int(a[bound] - b.get(bound, 0)) for bound in a if a[bound] - b.get(bound, 0) > 0}


class ProbeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster = Cluster()
        self.addCleanup(self.cluster.cleanup)
        self.sys = self.cluster.dir / "sys"
        self.route = self.cluster.dir / "route"
        self.psi = self.cluster.dir / "pressure-memory"
        self.meminfo = self.cluster.dir / "meminfo"
        self.fsync_dir = self.cluster.dir / "k3s-data"
        self.fsync_dir.mkdir()
        self.nodes: list[dict[str, Any]] = []
        self.write_default_host_files()

    def write_default_host_files(self) -> None:
        self.psi.write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        self.meminfo.write_text("MemTotal:       16384000 kB\nMemFree:         1024000 kB\nMemAvailable:    8192000 kB\nBuffers:          102400 kB\n")
        (self.sys / "class" / "net").mkdir(parents=True, exist_ok=True)
        self.route.write_text("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n")

    def probe(self, name: str = "me", *, voters: list[dict[str, Any]] | None = None, **env: str) -> subprocess.CompletedProcess[str]:
        """Takes one sample as node `name`, which is a voter when its Node is among `voters`."""
        self.cluster.nodes = voters if voters is not None else self.nodes
        base = {
            "NODE_NAME": name,
            "NAMESPACE": NAMESPACE,
            "ELECTION_TIMEOUT_MS": str(ELECTION_TIMEOUT_MS),
            "REPORT_SECONDS": str(SAMPLE_SECONDS),
            "REPORT_ONCE": "true",
            "ETCD_PEER_PORT": "1",
            "FSYNC_DIR": str(self.fsync_dir),
            "SYS_CLASS_NET": str(self.sys / "class" / "net"),
            "PROC_NET_ROUTE": str(self.route),
            "FLEET_REGRADE_CORE": str(REGRADE_CORE),
            "PSI_FILE": str(self.psi),
            "MEMINFO_FILE": str(self.meminfo),
        }
        return self.cluster.run("fleet-role-probe.sh", **{**base, **env})

    def newest(self, name: str = "me") -> dict[str, Any]:
        return self.cluster.probes()[name]["samples"][-1]

    def settled(self, name: str = "me") -> None:
        """Gives this node an entry whose fdatasync probe ran just now, so that a pass does not run fio."""
        self.cluster.set_probes({**self.cluster.probes(), name: {**entry([]), "fsync_at": int(time.time())}})


class SysfsTree:
    """A /sys/class/net and /sys/devices with the structure the kernel's has: class entries are symlinks into the device tree, and each interface has a device link back up to its bus device and a subsystem link back to the class directory, so that a walk following links loops."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.class_net = root / "class" / "net"
        self.class_net.mkdir(parents=True, exist_ok=True)

    def _link(self, path: Path, target: Path) -> None:
        path.symlink_to(os.path.relpath(target, path.parent))

    def interface(self, name: str, parent: str, *, wireless: bool = False, devtype: str | None = None) -> Path:
        """An interface under the device `parent` (a path below /sys/devices): `parent/net/name`, as for a PCI, USB or SoC network device."""
        bus_device = self.root / "devices" / parent
        directory = bus_device / "net" / name
        directory.mkdir(parents=True)
        (directory / "address").write_text("02:00:00:00:00:01\n")
        (directory / "type").write_text("1\n")
        uevent = f"INTERFACE={name}\nIFINDEX=2\n" + (f"DEVTYPE={devtype}\n" if devtype else "")
        (directory / "uevent").write_text(uevent)
        self._link(directory / "subsystem", self.class_net)
        if "virtual" not in parent:
            self._link(directory / "device", bus_device)
        if wireless:
            phy = bus_device / "ieee80211" / "phy0"
            phy.mkdir(parents=True)
            self._link(phy / "device", bus_device)
            self._link(directory / "phy80211", phy)
            (directory / "wireless").mkdir()
        self._link(self.class_net / name, directory)
        return directory

    def pci_ethernet(self, name: str = "enp3s0") -> None:
        self.interface(name, "pci0000:00/0000:00:1c.2/0000:03:00.0")

    def usb_ethernet(self, name: str = "enx001122334455") -> None:
        self.interface(name, "pci0000:00/0000:00:14.0/usb1/1-2/1-2:1.0")

    def pci_wifi(self, name: str = "wlan0") -> None:
        self.interface(name, "pci0000:00/0000:00:14.3", wireless=True, devtype="wlan")

    def soc_wifi(self, name: str = "wlan1") -> None:
        # A Raspberry Pi's wireless interface hangs below its SDIO host, and the phy link is all that marks it.
        self.interface(name, "platform/soc/fe300000.mmcnr/mmc_host/mmc1/mmc1:0001/mmc1:0001:1", wireless=True)


ROUTE_HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"


def route_line(iface: str, *, destination: str = "00000000", gateway: str = "0101A8C0", flags: str = "0003", metric: int = 100, mask: str = "00000000") -> str:
    return f"{iface}\t{destination}\t{gateway}\t{flags}\t0\t0\t{metric}\t{mask}\t0\t0\t0\n"


class LinkTest(ProbeCase):
    def setUp(self) -> None:
        super().setUp()
        self.tree = SysfsTree(self.sys)
        self.settled_node = node("me")
        self.nodes = [self.settled_node]
        self.cluster.set_probes({"me": {**entry([]), "fsync_at": int(time.time())}})

    def link_via(self, *routes: str) -> str:
        self.route.write_text(ROUTE_HEADER + "".join(routes))
        completed = self.probe()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return self.newest()["link"]

    def test_a_pci_ethernet_interface_is_wired(self) -> None:
        self.tree.pci_ethernet()
        self.assertEqual(self.link_via(route_line("enp3s0")), "wired")
        self.assertEqual(self.newest()["iface"], "enp3s0")

    def test_a_usb_ethernet_adapter_is_wired(self) -> None:
        self.tree.usb_ethernet()
        self.assertEqual(self.link_via(route_line("enx001122334455")), "wired")

    def test_a_pci_wireless_interface_is_wifi(self) -> None:
        self.tree.pci_wifi()
        self.assertEqual(self.link_via(route_line("wlan0")), "wifi")

    def test_a_wireless_interface_on_an_soc_bus_is_wifi(self) -> None:
        self.tree.soc_wifi()
        self.assertEqual(self.link_via(route_line("wlan1")), "wifi")

    def test_a_wireless_interface_is_wifi_by_its_devtype_alone(self) -> None:
        directory = self.tree.interface("wlp2s0", "pci0000:00/0000:00:1c.0/0000:02:00.0", devtype="wlan")
        self.assertFalse((directory / "phy80211").exists())
        self.assertEqual(self.link_via(route_line("wlp2s0")), "wifi")

    def test_a_virtual_interface_is_unknown_because_the_link_beneath_it_cannot_be_seen(self) -> None:
        # What a node running in a container (a veth), a bridge or a tunnel has as its default route.
        for name in ("eth0", "br0", "tun0"):
            with self.subTest(name):
                self.tree.interface(name, "virtual")
                self.assertEqual(self.link_via(route_line(name)), "unknown")

    def test_the_interface_with_the_lowest_metric_among_the_default_routes_decides(self) -> None:
        self.tree.pci_wifi()
        self.tree.pci_ethernet()
        wlan = route_line("wlan0", metric=600)
        eth = route_line("enp3s0", metric=100)
        self.assertEqual(self.link_via(wlan, eth), "wired")
        self.assertEqual(self.link_via(eth, wlan), "wired")
        self.assertEqual(self.link_via(route_line("wlan0", metric=50), route_line("enp3s0", metric=100)), "wifi")

    def test_a_route_that_is_not_a_default_route_through_a_gateway_does_not_decide(self) -> None:
        self.tree.pci_wifi()
        self.tree.pci_ethernet()
        routes = (
            route_line("enp3s0", destination="0001A8C0", gateway="00000000", flags="0001", mask="00FFFFFF"),
            route_line("enp3s0", flags="0001", metric=1),  # a default route that is up but has no gateway
            route_line("enp3s0", flags="0002", metric=1),  # a default route through a gateway that is down
            route_line("wlan0", metric=600),
        )
        self.assertEqual(self.link_via(*routes), "wifi")

    def test_no_default_route_is_unknown(self) -> None:
        self.tree.pci_ethernet()
        self.assertEqual(self.link_via(route_line("enp3s0", destination="0001A8C0", gateway="00000000", flags="0001", mask="00FFFFFF")), "unknown")
        self.assertIsNone(self.newest()["iface"])

    def test_an_interface_the_tree_does_not_have_is_unknown(self) -> None:
        self.assertEqual(self.link_via(route_line("enp9s0")), "unknown")

    def test_the_kernels_looping_links_do_not_stop_the_reading(self) -> None:
        # The tree really loops: from the class directory, an interface's subsystem is the class directory again, and its device leads back to the interface. A find that follows links fails on a tree like this, which is how a real host failed.
        self.tree.pci_ethernet()
        looped = self.sys / "class" / "net" / "enp3s0" / "subsystem" / "enp3s0" / "subsystem" / "enp3s0" / "device" / "net" / "enp3s0"
        self.assertTrue(looped.exists())
        self.assertEqual((self.sys / "class" / "net" / "enp3s0" / "subsystem").resolve(), (self.sys / "class" / "net").resolve())
        self.assertEqual(self.link_via(route_line("enp3s0")), "wired")

    @unittest.skipUnless(Path("/sys/class/net").is_dir() and Path("/proc/net/route").is_file(), "needs a Linux host's own /sys and /proc")
    def test_the_hosts_own_sysfs_and_routing_table_give_a_link_type(self) -> None:
        completed = self.probe(SYS_CLASS_NET="/sys/class/net", PROC_NET_ROUTE="/proc/net/route")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(self.newest()["link"], ("wired", "wifi", "unknown"))
        self.assertEqual(completed.stderr, "", "the real tree needs no tolerance for anything the probe reads")


class ConnectTest(ProbeCase):
    def setUp(self) -> None:
        super().setUp()
        self.settled()

    def peers(self, address: str = "127.0.0.1") -> list[dict[str, Any]]:
        return [node("peer", voter=True, address=address), node("me")]

    def test_a_connect_to_a_listening_peer_port_is_its_real_time_in_milliseconds(self) -> None:
        with listener() as port:
            completed = self.probe(voters=self.peers(), ETCD_PEER_PORT=str(port))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        reading = self.newest()["tcp"]["peer"]
        self.assertGreater(reading, 0)
        self.assertLess(reading, ELECTION_TIMEOUT_MS)

    def test_a_connect_that_does_not_complete_is_recorded_as_the_election_timeout(self) -> None:
        port = closed_port()
        completed = self.probe(voters=self.peers(), ETCD_PEER_PORT=str(port))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.newest()["tcp"]["peer"], ELECTION_TIMEOUT_MS)

    def test_a_peer_that_never_answers_costs_one_election_timeout_not_more(self) -> None:
        started = time.monotonic()
        # An address reserved for documentation, which nothing answers: the connect times out or the network is unreachable, and either way it never completes.
        completed = self.probe(voters=self.peers("192.0.2.1"), ELECTION_TIMEOUT_MS="400")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.newest()["tcp"]["peer"], 400)
        self.assertLess(time.monotonic() - started, 5)

    def test_an_ipv6_peer_address_is_bracketed(self) -> None:
        try:
            with listener("::1") as port:
                completed = self.probe(voters=self.peers("::1"), ETCD_PEER_PORT=str(port))
        except OSError:
            self.skipTest("no IPv6 loopback on this host")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertLess(self.newest()["tcp"]["peer"], ELECTION_TIMEOUT_MS)

    def test_a_node_does_not_measure_itself(self) -> None:
        with listener() as port:
            completed = self.probe("peer", voters=self.peers(), ETCD_PEER_PORT=str(port))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.newest("peer")["tcp"], {})


class EtcdReadingTest(ProbeCase):
    def setUp(self) -> None:
        super().setUp()
        self.nodes = [node("me", voter=True), node("p1", voter=True), node("p2", voter=True)]

    def pass_with(self, url: str) -> subprocess.CompletedProcess[str]:
        return self.probe(ETCD_METRICS_URL=url, ETCD_PEER_PORT=str(closed_port()))

    def test_the_first_sample_records_the_gauges_and_nothing_gained(self) -> None:
        with metrics_server([page("before")]) as url:
            self.assertEqual(self.pass_with(url).returncode, 0)
        etcd = self.newest()["etcd"]
        self.assertEqual(
            {k: etcd[k] for k in ("has_leader", "is_learner", "known_members", "active_peers", "quota_bytes", "leader_changes")},
            {"has_leader": True, "is_learner": False, "known_members": TARGET, "active_peers": TARGET - 1, "quota_bytes": FIXTURE_QUOTA_BYTES, "leader_changes": 0},
        )
        self.assertEqual((etcd["wal"], etcd["commit"]), ({}, {}))
        self.assertFalse(self.newest()["restarted"])
        self.assertEqual(self.cluster.probes()["me"]["etcd_id"], FIXTURE_SERVER_ID)

    def test_the_next_sample_holds_what_each_histogram_gained_as_a_real_etcd_counted_it(self) -> None:
        before, after = page("before"), page("after")
        with metrics_server([before, after]) as url:
            self.assertEqual(self.pass_with(url).returncode, 0)
            self.assertEqual(self.pass_with(url).returncode, 0)
        etcd = self.newest()["etcd"]
        wal = gained(cumulative(before, "etcd_disk_wal_fsync_duration_seconds")[""], cumulative(after, "etcd_disk_wal_fsync_duration_seconds")[""])
        commit = gained(cumulative(before, "etcd_disk_backend_commit_duration_seconds")[""], cumulative(after, "etcd_disk_backend_commit_duration_seconds")[""])
        self.assertGreater(sum(wal.values()), 0, "the captured pages differ, or this test proves nothing")
        self.assertEqual(etcd["wal"], wal)
        self.assertEqual(etcd["commit"], commit)
        rtt_before = cumulative(before, "etcd_network_peer_round_trip_time_seconds", "To")
        rtt_after = cumulative(after, "etcd_network_peer_round_trip_time_seconds", "To")
        for peer in FIXTURE_PEER_IDS:
            self.assertEqual(etcd["rtt"][peer], gained(rtt_before[peer], rtt_after[peer]), peer)
        self.assertFalse(self.newest()["restarted"])
        # Cumulative as etcd reports it, so the sample after this one has something to subtract from.
        self.assertEqual(self.cluster.probes()["me"]["last"]["wal"], cumulative(after, "etcd_disk_wal_fsync_duration_seconds")[""])

    def test_counters_that_go_backwards_mean_etcd_restarted(self) -> None:
        with metrics_server([page("after"), page("before")]) as url:
            self.pass_with(url)
            self.pass_with(url)
        sample_ = self.newest()
        self.assertTrue(sample_["restarted"])
        self.assertEqual(sample_["etcd"]["wal"], {}, "a reset has no gain to count")

    def test_a_member_that_is_added_and_not_started_is_a_known_member_that_is_not_connected(self) -> None:
        with metrics_server([page("learner-unstarted")]) as url:
            self.pass_with(url)
        etcd = self.newest()["etcd"]
        self.assertEqual(etcd["known_members"], TARGET + 1)
        self.assertEqual(etcd["active_peers"], TARGET - 1)
        self.assertIn(FIXTURE_LEARNER_ID, page("learner-unstarted"))

    def test_a_removed_member_that_etcd_still_lists_with_a_zero_is_not_a_known_member(self) -> None:
        self.assertRegex(page("before"), r'etcd_network_known_peers\{[^}]*\} 0')
        with metrics_server([page("before")]) as url:
            self.pass_with(url)
        self.assertEqual(self.newest()["etcd"]["known_members"], TARGET)

    def test_a_voter_whose_metrics_cannot_be_read_still_records_the_rest_and_keeps_its_baseline(self) -> None:
        with metrics_server([page("before")]) as url:
            self.pass_with(url)
        baseline = self.cluster.probes()["me"]["last"]
        completed = self.pass_with(f"http://127.0.0.1:{closed_port()}/metrics")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("could not read", completed.stderr)
        self.assertIsNone(self.newest()["etcd"])
        self.assertEqual(self.cluster.probes()["me"]["last"], baseline)
        self.assertEqual(self.cluster.probes()["me"]["etcd_id"], FIXTURE_SERVER_ID)

    def test_a_node_that_is_not_a_voter_reads_no_etcd_metrics(self) -> None:
        self.nodes = [node("me"), node("p1", voter=True)]
        self.settled()
        with metrics_server([page("before")]) as url:
            self.assertEqual(self.pass_with(url).returncode, 0)
        self.assertIsNone(self.newest()["etcd"])
        self.assertIsNone(self.cluster.probes()["me"]["etcd_id"])

    @unittest.skipUnless(ETCD_BINARY, "needs an etcd binary (on the PATH, or named by ETCD_BINARY)")
    def test_a_live_three_member_cluster_gives_the_readings_the_fixtures_were_captured_from(self) -> None:
        assert ETCD_BINARY is not None
        with live_etcd(ETCD_BINARY) as cluster_:
            url = cluster_.metrics_url(0)
            self.assertEqual(self.pass_with(url).returncode, 0)
            cluster_.put(20)
            time.sleep(1)
            self.assertEqual(self.pass_with(url).returncode, 0)
            etcd = self.newest()["etcd"]
            self.assertEqual((etcd["known_members"], etcd["active_peers"], etcd["has_leader"], etcd["is_learner"]), (TARGET, TARGET - 1, True, False))
            self.assertGreater(sum(etcd["wal"].values()), 0)
            self.assertEqual(self.cluster.probes()["me"]["etcd_id"], cluster_.member_id(0))
            cluster_.add_learner()
            time.sleep(2)
            self.assertEqual(self.pass_with(url).returncode, 0)
            self.assertEqual(self.newest()["etcd"]["known_members"], TARGET + 1)


class LiveEtcd:
    def __init__(self, binary: str, directory: Path) -> None:
        self.binary = binary
        self.etcdctl = str(Path(binary).with_name("etcdctl"))
        self.dir = directory
        self.ports = [(closed_port(), closed_port(), closed_port()) for _ in range(3)]
        self.processes: list[subprocess.Popen[bytes]] = []

    def start(self) -> None:
        initial = ",".join(f"n{i}=http://127.0.0.1:{peer}" for i, (_, peer, _) in enumerate(self.ports))
        for i, (client, peer, metrics) in enumerate(self.ports):
            self.processes.append(
                subprocess.Popen(
                    [
                        self.binary, "--name", f"n{i}", "--data-dir", str(self.dir / f"n{i}"),
                        "--listen-peer-urls", f"http://127.0.0.1:{peer}", "--initial-advertise-peer-urls", f"http://127.0.0.1:{peer}",
                        "--listen-client-urls", f"http://127.0.0.1:{client}", "--advertise-client-urls", f"http://127.0.0.1:{client}",
                        "--listen-metrics-urls", f"http://127.0.0.1:{metrics}", "--initial-cluster", initial, "--initial-cluster-token", "fleet-role-probe-test",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            done = subprocess.run([self.etcdctl, "--endpoints", self.endpoints(), "endpoint", "health"], capture_output=True, check=False)
            if done.returncode == 0:
                return
            time.sleep(0.5)
        raise AssertionError("the local etcd cluster did not become healthy")

    def stop(self) -> None:
        for process in self.processes:
            process.terminate()
        for process in self.processes:
            process.wait(timeout=20)

    def endpoints(self) -> str:
        return ",".join(f"127.0.0.1:{client}" for client, _, _ in self.ports)

    def metrics_url(self, index: int) -> str:
        return f"http://127.0.0.1:{self.ports[index][2]}/metrics"

    def put(self, count: int) -> None:
        for i in range(count):
            subprocess.run([self.etcdctl, "--endpoints", self.endpoints(), "put", f"key{i}", "value"], capture_output=True, check=True)

    def member_id(self, index: int) -> str:
        out = subprocess.run([self.etcdctl, "--endpoints", self.endpoints(), "member", "list", "-w", "json"], capture_output=True, text=True, check=True).stdout
        for member in json.loads(out)["members"]:
            if member["name"] == f"n{index}":
                return format(member["ID"], "x")
        raise AssertionError("no such member")

    def add_learner(self) -> None:
        """Adds a learner that is never started. etcd refuses a membership change until its members have all been connected for a while (strict reconfiguration checks), so it is retried for that long."""
        deadline = time.monotonic() + 30
        while True:
            added = subprocess.run(
                [self.etcdctl, "--endpoints", self.endpoints(), "member", "add", "n9", "--learner", f"--peer-urls=http://127.0.0.1:{closed_port()}"], capture_output=True, text=True, check=False
            )
            if added.returncode == 0:
                return
            assert "unhealthy cluster" in added.stderr and time.monotonic() < deadline, added.stderr
            time.sleep(1)


@contextmanager
def live_etcd(binary: str) -> Iterator[LiveEtcd]:
    with tempfile.TemporaryDirectory() as directory:
        cluster = LiveEtcd(binary, Path(directory))
        cluster.start()
        try:
            yield cluster
        finally:
            cluster.stop()


class MemoryTest(ProbeCase):
    def setUp(self) -> None:
        super().setUp()
        self.nodes = [node("me")]
        self.settled()

    def test_pressure_and_available_memory_are_read_from_the_kernels_files_by_the_regrade_cores_functions(self) -> None:
        self.psi.write_text("some avg10=0.00 avg60=1.50 avg300=12.34 total=987654\nfull avg10=0.00 avg60=0.10 avg300=3.21 total=12345\n")
        self.meminfo.write_text("MemTotal:       32768000 kB\nMemFree:          524288 kB\nMemAvailable:    4096000 kB\nBuffers:          102400 kB\n")
        self.assertEqual(self.probe().returncode, 0)
        latest = self.newest()
        self.assertEqual((latest["psi"], latest["avail_kb"], latest["total_kb"]), (12.34, 4096000, 32768000))

    def test_a_kernel_without_pressure_stall_information_gives_a_sample_without_a_pressure_reading(self) -> None:
        self.psi.unlink()
        completed = self.probe()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        latest = self.newest()
        self.assertIsNone(latest["psi"])
        self.assertEqual(latest["avail_kb"], 8192000)


class RingTest(ProbeCase):
    def setUp(self) -> None:
        super().setUp()
        self.nodes = [node("me")]

    def test_a_sample_is_appended_and_the_entry_keeps_the_last_window_of_them(self) -> None:
        old = [sample(1000 + i, fsync_ms=None) for i in range(WINDOW_SAMPLES)]
        self.cluster.set_probes({"me": {**entry(old), "fsync_at": int(time.time())}})
        self.assertEqual(self.probe().returncode, 0)
        kept = self.cluster.probes()["me"]["samples"]
        self.assertEqual(len(kept), WINDOW_SAMPLES)
        self.assertEqual(kept[0]["t"], 1001, "the oldest was dropped")
        self.assertGreater(kept[-1]["t"], 1000 + WINDOW_SAMPLES)

    def test_each_node_patches_only_its_own_key(self) -> None:
        other = entry([sample(5)], "other-id")
        self.cluster.set_probes({"other": other, "me": {**entry([]), "fsync_at": int(time.time())}})
        self.assertEqual(self.probe().returncode, 0)
        self.assertEqual(self.cluster.probes()["other"], other)
        patches = [json.loads(line) for line in self.cluster.calls.read_text().splitlines() if '"patch"' in line]
        self.assertEqual([call[:3] for call in patches], [["patch", "configmap", "fleet-role-probe"]])

    def test_a_node_that_is_not_a_voter_marks_its_first_sample_as_a_restart_and_a_later_one_as_none(self) -> None:
        self.cluster.set_probes({"me": {**entry([]), "fsync_at": int(time.time())}})
        # The loop form: one process taking two samples.
        self.cluster.write_store()
        process = subprocess.Popen(
            [str(Path(__file__).resolve().parents[2] / "scripts" / "fleet-role-probe.sh")],
            env=self.cluster.env(
                NODE_NAME="me", NAMESPACE=NAMESPACE, ELECTION_TIMEOUT_MS=str(ELECTION_TIMEOUT_MS), REPORT_SECONDS="1", ETCD_PEER_PORT="1", FSYNC_DIR=str(self.fsync_dir),
                SYS_CLASS_NET=str(self.sys / "class" / "net"), PROC_NET_ROUTE=str(self.route), FLEET_REGRADE_CORE=str(REGRADE_CORE), PSI_FILE=str(self.psi), MEMINFO_FILE=str(self.meminfo),
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 30
            samples: list[dict[str, Any]] = []
            while time.monotonic() < deadline and len(samples) < 2:
                time.sleep(0.2)
                try:
                    stored = self.cluster.read_store()["configmaps"][f"{NAMESPACE}/fleet-role-probe"]["data"].get("me", "{}")
                except json.JSONDecodeError:
                    continue  # the stand-in kubectl was rewriting the store
                samples = json.loads(stored).get("samples", [])
        finally:
            process.terminate()
            process.wait(timeout=20)
        self.assertGreaterEqual(len(samples), 2)
        self.assertEqual([s["restarted"] for s in samples[:2]], [True, False])


@unittest.skipUnless(FIO, "needs fio, which the probe's fdatasync reading is taken with")
class FsyncTest(ProbeCase):
    def setUp(self) -> None:
        super().setUp()
        self.nodes = [node("me"), node("peer", voter=True)]

    def test_a_node_that_is_not_a_voter_takes_the_reading_from_the_real_fio_and_leaves_no_file_behind(self) -> None:
        completed = self.probe()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        reading = self.newest()["fsync_ms"]
        self.assertIsInstance(reading, float)
        self.assertGreater(reading, 0)
        self.assertEqual(list(self.fsync_dir.iterdir()), [])
        self.assertIsNotNone(self.cluster.probes()["me"]["fsync_at"])

    def test_the_reading_is_taken_once_per_window_not_every_sample(self) -> None:
        self.assertEqual(self.probe().returncode, 0)
        first_at = self.cluster.probes()["me"]["fsync_at"]
        self.assertEqual(self.probe().returncode, 0)
        self.assertIsNone(self.newest()["fsync_ms"])
        self.assertEqual(self.cluster.probes()["me"]["fsync_at"], first_at)
        entries = self.cluster.probes()
        entries["me"]["fsync_at"] = int(time.time()) - WINDOW_SECONDS
        self.cluster.set_probes(entries)
        self.assertEqual(self.probe().returncode, 0)
        self.assertIsNotNone(self.newest()["fsync_ms"])

    def test_a_voter_never_runs_the_probe_because_its_etcd_is_measured_directly(self) -> None:
        marker = self.cluster.dir / "fio-ran"
        shim = self.cluster.bin / "fio"
        shim.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        shim.chmod(0o755)
        with metrics_server([page("before")]) as url:
            completed = self.probe(voters=[node("me", voter=True), node("peer", voter=True)], ETCD_METRICS_URL=url)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(marker.exists())
        self.assertIsNone(self.newest()["fsync_ms"])


class CheckTest(ProbeCase):
    """`check` takes one reading alone, with no cluster access, for comparing with a reading taken by hand on a real host."""

    def check(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        base = {
            "ELECTION_TIMEOUT_MS": str(ELECTION_TIMEOUT_MS),
            "REPORT_SECONDS": str(SAMPLE_SECONDS),
            "FSYNC_DIR": str(self.fsync_dir),
            "SYS_CLASS_NET": str(self.sys / "class" / "net"),
            "PROC_NET_ROUTE": str(self.route),
            "FLEET_REGRADE_CORE": str(REGRADE_CORE),
            "PSI_FILE": str(self.psi),
            "MEMINFO_FILE": str(self.meminfo),
        }
        completed = subprocess.run(
            [str(Path(__file__).resolve().parents[2] / "scripts" / "fleet-role-probe.sh"), "check", *args],
            env={"PATH": self.cluster.env()["PATH"], **base, **env},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(self.cluster.calls.read_text(), "", "a check must not reach the cluster")
        return completed

    def test_link_prints_the_interface_and_its_link_type(self) -> None:
        SysfsTree(self.sys).pci_wifi()
        self.route.write_text(ROUTE_HEADER + route_line("wlan0"))
        completed = self.check("link")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"interface": "wlan0", "link": "wifi"})

    def test_connect_prints_the_milliseconds_to_a_listener(self) -> None:
        with listener() as port:
            completed = self.check("connect", "127.0.0.1", ETCD_PEER_PORT=str(port))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertLess(float(completed.stdout), ELECTION_TIMEOUT_MS)
        self.assertGreater(float(completed.stdout), 0)

    def test_memory_prints_what_the_kernels_files_say(self) -> None:
        self.psi.write_text("some avg10=0.00 avg60=0.00 avg300=7.50 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        completed = self.check("memory")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"psi_avg300": 7.5, "mem_available_kb": 8192000, "mem_total_kb": 16384000})

    def test_etcd_prints_the_gauges_and_the_tails_of_the_cumulative_histograms(self) -> None:
        with metrics_server([page("before")]) as url:
            completed = self.check("etcd", ETCD_METRICS_URL=url)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        reading = json.loads(completed.stdout)
        self.assertEqual((reading["id"], reading["known_members"], reading["has_leader"]), (FIXTURE_SERVER_ID, TARGET, True))
        self.assertGreater(reading["wal_fsync_p99_ms"], 0)
        self.assertGreater(reading["backend_commit_p99_ms"], 0)

    @unittest.skipUnless(FIO, "needs fio")
    def test_fsync_prints_the_milliseconds_fio_measured(self) -> None:
        completed = self.check("fsync")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertGreater(float(completed.stdout), 0)

    def test_an_unknown_reading_is_a_usage_error(self) -> None:
        completed = self.check("nonsense")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("usage:", completed.stderr)


if __name__ == "__main__":
    unittest.main()
