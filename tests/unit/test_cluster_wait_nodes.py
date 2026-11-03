"""Tests for the wait for nodes to report Ready (tasks/wait_for_nodes.yml and tasks/wait_for_ready.yml): it covers the nodes the inventory manages, never a node the cluster holds that no inventory host owns, which is reported when it is not Ready and does not fail the run, while an inventory host that stays not Ready still fails it after the rescue has cleared its stale registration. The stand-in for kubectl answers as the real one does for the commands the role runs (a wait over named nodes, or over every node but the stepped-down ones), and the stand-in for the Kubernetes module records the objects the rescue deletes."""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from typing import Any

import yaml
from cluster_stand_ins import ROOT, RoleTestCase

ROLE = ROOT / "roles" / "github_runner_cluster"
FOREIGN = "pi4-outside"

# Stands in for kubectl: `get nodes` prints cluster.nodes ("<node> <Ready status>" a line, printed as <node>=<status> as the role asks), or with --selector the names in stepped-down; `wait` fails for a node whose status is not True as the real one does at its timeout, and for no node at all, and logs each call to kubectl.log.
KUBECTL_STUB = """#!{python}
import os
import sys

stub = os.environ["DOCKER_STUB"]
args = sys.argv[1:]
with open(os.path.join(stub, "kubectl.log"), "a") as log:
    log.write(" ".join(args) + "\\n")

def read(name):
    path = os.path.join(stub, name)
    return open(path).read().splitlines() if os.path.exists(path) else []

nodes = dict(line.split(" ", 1) if " " in line else (line, "") for line in read("cluster.nodes"))
stepped_down = read("stepped-down")
if args[:2] == ["get", "nodes"]:
    names = stepped_down if "--selector" in args else [name + "=" + status for name, status in nodes.items()]
    print("\\n".join(names))
elif args[0] == "wait":
    named = [a.removeprefix("node/") for a in args if a.startswith("node/")]
    if "--selector" in args:
        named = [name for name in nodes if name not in stepped_down]
    if not named:
        print("error: no matching resources found", file=sys.stderr)
        sys.exit(1)
    for name in named:
        if nodes.get(name) != "True":
            print("error: timed out waiting for the condition on nodes/" + name, file=sys.stderr)
            sys.exit(1)
    print("\\n".join("node/" + name + " condition met" for name in named))
else:
    print("stand-in: unexpected kubectl call " + " ".join(args), file=sys.stderr)
    sys.exit(99)
"""

# Stands in for kubernetes.core.k8s: logs each object it is asked to delete.
K8S_MODULE = """#!/usr/bin/python
from __future__ import annotations
import os
from ansible.module_utils.basic import AnsibleModule

def main():
    module = AnsibleModule(
        argument_spec={"kubeconfig": {"type": "str"}, "state": {"type": "str"}, "kind": {"type": "str"}, "namespace": {"type": "str"}, "name": {"type": "str"}},
        supports_check_mode=True,
    )
    with open(os.path.join(os.environ["DOCKER_STUB"], "k8s.log"), "a") as log:
        log.write("%s %s %s\\n" % (module.params["state"], module.params["kind"], module.params["name"]))
    module.exit_json(changed=True)

main()
"""


class WaitTestCase(RoleTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.bin / "kubectl").write_text(KUBECTL_STUB.format(python=sys.executable))
        modules = self.work / "collections" / "ansible_collections" / "kubernetes" / "core" / "plugins" / "modules"
        modules.mkdir(parents=True)
        (modules / "k8s.py").write_text(K8S_MODULE)

    def wait(self, check: bool = False) -> subprocess.CompletedProcess[str]:
        hosts, group_vars = self.hosts(("node1", "node2", "node3"), ("agent1",))
        return self.run_tasks("wait_for_nodes", hosts, group_vars, check=check, limit="node1")

    def kubectl_calls(self) -> list[str]:
        return self.read_log("kubectl.log")

    def deletions(self) -> list[str]:
        return self.read_log("k8s.log")

    def read_log(self, name: str) -> list[str]:
        path = self.stub / name
        return path.read_text().splitlines() if path.exists() else []

    def waits(self) -> list[str]:
        return [call for call in self.kubectl_calls() if call.startswith("wait")]


class OutsideTheInventoryTest(WaitTestCase):
    def test_a_not_ready_node_outside_the_inventory_is_reported_and_does_not_fail_the_wait(self) -> None:
        self.nodes(node1="True", node2="True", node3="True", agent1="True", **{FOREIGN: "False"})
        result = self.wait()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"{FOREIGN} is not in the inventory and reports Ready=False; it is not waited for", result.stdout)
        self.assertEqual(len(self.waits()), 1, "the wait passed first time, so the rescue never ran")
        self.assertEqual(self.deletions(), [], "the rescue must not clear anything of a node nobody asked it to manage")

    def test_the_wait_names_the_inventory_nodes_and_never_the_node_outside_it(self) -> None:
        self.nodes(node1="True", node2="True", node3="True", agent1="True", **{FOREIGN: "Unknown"})
        result = self.wait()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        [wait] = self.waits()
        self.assertEqual(sorted(re.findall(r"node/(\S+)", wait)), ["agent1", "node1", "node2", "node3"])
        self.assertNotIn(FOREIGN, wait)

    def test_a_node_outside_the_inventory_with_no_ready_condition_is_reported_as_unknown(self) -> None:
        self.nodes(node1="True", node2="True", node3="True", agent1="True", **{FOREIGN: ""})
        result = self.wait()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"{FOREIGN} is not in the inventory and reports Ready=unknown; it is not waited for", result.stdout)

    def test_a_ready_node_outside_the_inventory_is_not_reported(self) -> None:
        self.nodes(node1="True", node2="True", node3="True", agent1="True", **{FOREIGN: "True"})
        result = self.wait()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("is not in the inventory", result.stdout)

    def test_the_rescue_still_runs_when_an_inventory_node_is_not_ready_and_leaves_the_node_outside_alone(self) -> None:
        self.nodes(node1="True", node2="True", node3="True", agent1="False", **{FOREIGN: "False"})
        result = self.wait()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.deletions(), ["absent Secret agent1.node-password.k3s", "absent Node agent1"])
        self.assertEqual(len(self.waits()), 2, "the wait and its retry")
        self.assertTrue(all(FOREIGN not in wait for wait in self.waits()))


class InsideTheInventoryTest(WaitTestCase):
    def test_an_inventory_host_that_stays_not_ready_fails_the_run_after_the_retry(self) -> None:
        self.nodes(node1="True", node2="False", node3="True", agent1="True")
        result = self.wait()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("node2", result.stdout + result.stderr)
        self.assertEqual(len(self.waits()), 2)
        self.assertEqual(self.deletions(), ["absent Secret node2.node-password.k3s"], "a server's Node object is never deleted, its etcd member would go with it")

    def test_every_inventory_node_ready_passes_without_the_rescue(self) -> None:
        self.nodes(node1="True", node2="True", node3="True", agent1="True")
        result = self.wait()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.waits()), 1)
        self.assertEqual(self.deletions(), [])

    def test_a_server_regrading_has_stepped_down_is_not_waited_for(self) -> None:
        self.nodes(node1="True", node2="False", node3="True", agent1="True")
        self.write("stepped-down", text="node2\n")
        result = self.wait()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        [wait] = self.waits()
        self.assertEqual(sorted(re.findall(r"node/(\S+)", wait)), ["agent1", "node1", "node3"])
        self.assertEqual(self.deletions(), [])

    def test_an_inventory_node_that_has_not_registered_is_not_waited_for(self) -> None:
        self.nodes(node1="True", node2="True", node3="True")
        result = self.wait()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        [wait] = self.waits()
        self.assertEqual(sorted(re.findall(r"node/(\S+)", wait)), ["node1", "node2", "node3"])

    def test_a_cluster_holding_no_node_of_the_inventory_fails_rather_than_passing_an_empty_wait(self) -> None:
        self.nodes(**{FOREIGN: "True"})
        result = self.wait()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No node of this cluster's inventory is registered", result.stdout)
        self.assertEqual(self.waits(), [])


class CheckModeTest(WaitTestCase):
    def test_a_check_run_reads_the_nodes_and_leaves_the_wait_itself_unrun(self) -> None:
        self.nodes(node1="True", node2="True", node3="True", agent1="True")
        result = self.wait(check=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.waits(), [])
        self.assertEqual(len([call for call in self.kubectl_calls() if call.startswith("get nodes")]), 2)


class WiringTest(unittest.TestCase):
    def tasks(self, name: str) -> list[dict[str, Any]]:
        loaded: list[dict[str, Any]] = yaml.safe_load((ROLE / "tasks" / name).read_text())
        return loaded

    def test_the_role_waits_through_the_task_file_and_names_no_selector_of_its_own(self) -> None:
        main = (ROLE / "tasks" / "main.yml").read_text()
        self.assertNotIn("kubectl wait", main)
        [step] = [task for task in self.tasks("main.yml") if task.get("ansible.builtin.include_tasks") == "wait_for_nodes.yml"]
        self.assertEqual(step["when"], "github_runner_cluster_wait_for_nodes | bool")

    def test_no_wait_covers_every_node_of_the_cluster(self) -> None:
        for name in ("wait_for_nodes.yml", "wait_for_ready.yml"):
            text = (ROLE / "tasks" / name).read_text()
            self.assertNotIn("--selector '!", text, name)
            self.assertNotIn("--all", text, name)

    def test_the_rescue_waits_again_through_the_same_file(self) -> None:
        [wait] = self.tasks("wait_for_nodes.yml")
        steps = [step.get("ansible.builtin.include_tasks") for step in wait["block"] + wait["rescue"]]
        self.assertEqual(steps.count("wait_for_ready.yml"), 2)
        self.assertEqual(wait["rescue"][-1]["ansible.builtin.include_tasks"], "wait_for_ready.yml")


if __name__ == "__main__":
    unittest.main()
