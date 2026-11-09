"""Tests for files/route-guard.sh, which the cluster role installs in a Colima node VM: it removes default routes whose gateway does not answer, so a bridged interface that lost its link cannot carry the VM's traffic. `ip` and `ping` are stand-ins that read their state from files, so the script's own decisions are what is tested."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "roles" / "github_runner_cluster" / "files" / "route-guard.sh"

# `ip -4 route show default` as the VM printed it in the outage: the dead bridged interface held two default routes with lower metrics than the working one.
OUTAGE_ROUTES = """default via 10.0.0.1 dev col0 metric 100
default via 192.168.5.2 dev eth0 proto dhcp src 192.168.5.1 metric 200
default via 10.0.0.1 dev col0 proto dhcp src 10.0.191.9 metric 300
"""

FAKE_IP = """#!/bin/sh
case "$*" in
  "-4 route show default") cat "$FAKE_DIR/routes" ;;
  "-4 route del "*) echo "$*" >> "$FAKE_DIR/deleted" ;;
  "-4 neigh show to "*)
    gateway=$5
    device=$7
    grep "^$gateway $device " "$FAKE_DIR/neighbours" | cut -d' ' -f3- || true ;;
  *) echo "unexpected ip $*" >&2; exit 2 ;;
esac
"""

# ping -n -c 1 -W <timeout> -I <device> <gateway>
FAKE_PING = """#!/bin/sh
device=$7
gateway=$8
grep -q "^$gateway $device\\$" "$FAKE_DIR/answering"
"""


class RouteGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dir = Path(self.directory.name)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        for name, body in (("ip", FAKE_IP), ("ping", FAKE_PING)):
            path = bin_dir / name
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        for name in ("routes", "answering", "neighbours", "deleted"):
            (self.dir / name).write_text("")

    def run_guard(self, routes: str, answering: str = "", neighbours: str = "") -> tuple[subprocess.CompletedProcess[str], list[str]]:
        (self.dir / "routes").write_text(routes)
        (self.dir / "answering").write_text(answering)
        (self.dir / "neighbours").write_text(neighbours)
        env = {"PATH": f"{self.dir / 'bin'}:/usr/bin:/bin", "FAKE_DIR": str(self.dir)}
        result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)
        deleted = [line for line in (self.dir / "deleted").read_text().splitlines() if line]
        return result, deleted

    def test_the_dead_bridged_routes_are_removed_and_the_working_one_stays(self) -> None:
        result, deleted = self.run_guard(OUTAGE_ROUTES, answering="192.168.5.2 eth0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(deleted, ["-4 route del default via 10.0.0.1 dev col0 metric 100", "-4 route del default via 10.0.0.1 dev col0 metric 300"])

    def test_nothing_is_removed_when_no_gateway_answers(self) -> None:
        result, deleted = self.run_guard(OUTAGE_ROUTES)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(deleted, [])
        self.assertIn("none is removed", result.stderr)

    def test_a_single_working_route_is_left_alone(self) -> None:
        _, deleted = self.run_guard("default via 192.168.5.2 dev eth0 metric 200\n", answering="192.168.5.2 eth0\n")
        self.assertEqual(deleted, [])

    def test_a_gateway_that_drops_icmp_but_holds_a_neighbour_entry_is_alive(self) -> None:
        _, deleted = self.run_guard(OUTAGE_ROUTES, answering="192.168.5.2 eth0\n", neighbours="10.0.0.1 col0 10.0.0.1 lladdr 02:00:00:00:00:01 REACHABLE\n")
        self.assertEqual(deleted, [])

    def test_a_failed_neighbour_entry_does_not_count_as_alive(self) -> None:
        _, deleted = self.run_guard(OUTAGE_ROUTES, answering="192.168.5.2 eth0\n", neighbours="10.0.0.1 col0 10.0.0.1 FAILED\n")
        self.assertEqual(len(deleted), 2)

    def test_a_route_without_a_gateway_is_never_judged(self) -> None:
        _, deleted = self.run_guard("default dev tun0 scope link\ndefault via 192.168.5.2 dev eth0 metric 200\n", answering="192.168.5.2 eth0\n")
        self.assertEqual(deleted, [])

    def test_a_dead_route_without_a_metric_is_removed_without_one(self) -> None:
        _, deleted = self.run_guard("default via 10.0.0.1 dev col0\ndefault via 192.168.5.2 dev eth0 metric 200\n", answering="192.168.5.2 eth0\n")
        self.assertEqual(deleted, ["-4 route del default via 10.0.0.1 dev col0"])

    def test_no_default_route_is_a_clean_exit(self) -> None:
        result, deleted = self.run_guard("")
        self.assertEqual((result.returncode, deleted), (0, []))

    def test_the_script_is_executable_and_parses(self) -> None:
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        self.assertEqual(subprocess.run(["sh", "-n", str(SCRIPT)], check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
