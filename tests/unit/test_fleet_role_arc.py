"""Tests for how the github_runner_arc role installs the role controller: the manifests templates/fleet-role.yaml.j2 renders (through tests/fleet_role/render.yml, with ansible-playbook), the checks of tasks/validate.yml, and that the settings the scripts require are the ones the manifests give them."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_arc"
CLUSTER_ROLE = ROOT / "roles" / "github_runner_cluster"
RENDER = ROOT / "tests" / "fleet_role" / "render.yml"
# Renders one template file with the role's defaults and constants, the way the role evaluates an expression taken from a task.
EXPRESSION = ROOT / "tests" / "node_cache_pruner" / "kept.yml"
ARC = "github_runner_arc_role_controller_"


def ansible(playbook: Path, **variables: Any) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as work:
        extra = Path(work) / "extra.json"
        extra.write_text(json.dumps(variables))
        env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_HOME": str(Path(work) / "home")}
        return subprocess.run([os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(playbook), "-e", f"@{extra}"], env=env, cwd=work, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)


def render(**role_vars: Any) -> list[dict[str, Any]]:
    """Renders the manifests and returns them in order."""
    with tempfile.TemporaryDirectory() as work:
        output = Path(work) / "fleet-role.yaml"
        completed = ansible(RENDER, fleet_role_output=str(output), **role_vars)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return [document for document in yaml.safe_load_all(output.read_text()) if document]


def of_kind(manifests: list[dict[str, Any]], kind: str, name: str | None = None) -> list[dict[str, Any]]:
    return [m for m in manifests if m["kind"] == kind and (name is None or m["metadata"]["name"] == name)]


def container(manifest: dict[str, Any]) -> dict[str, Any]:
    [only] = manifest["spec"]["template"]["spec"]["containers"]
    return only


def env_of(manifest: dict[str, Any]) -> dict[str, Any]:
    return {item["name"]: item.get("value", item.get("valueFrom")) for item in container(manifest)["env"]}


def script_requirements(script: str, prefix: str = "") -> set[str]:
    """The environment variables a script requires with `${NAME:?`, or `: "${NAME:?`."""
    return set(re.findall(rf"\$\{{({prefix}[A-Z_]+):\?", (ROOT / "scripts" / script).read_text()))


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifests = render()

    def test_each_of_the_controller_and_the_probe_has_its_own_account_and_the_workloads_are_a_deployment_and_a_daemonset(self) -> None:
        kinds = sorted((m["kind"], m["metadata"]["name"]) for m in self.manifests)
        self.assertEqual(
            kinds,
            sorted(
                [("ServiceAccount", n) for n in ("fleet-role", "fleet-role-probe")]
                + [("ClusterRole", n) for n in ("github-runner-fleet-role", "github-runner-fleet-role-probe")]
                + [("ClusterRoleBinding", n) for n in ("github-runner-fleet-role", "github-runner-fleet-role-probe")]
                + [("Role", n) for n in ("fleet-role", "fleet-role-probe")]
                + [("RoleBinding", n) for n in ("fleet-role", "fleet-role-probe")]
                + [("Deployment", "fleet-role"), ("DaemonSet", "fleet-role-probe")]
            ),
        )

    def test_neither_can_write_a_node_and_both_can_only_read_them(self) -> None:
        for role in of_kind(self.manifests, "ClusterRole"):
            self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list"]}], role["metadata"]["name"])

    def test_their_only_namespaced_permissions_are_on_their_two_configmaps_by_name(self) -> None:
        controller = of_kind(self.manifests, "Role", "fleet-role")[0]["rules"]
        probe = of_kind(self.manifests, "Role", "fleet-role-probe")[0]["rules"]
        self.assertEqual(
            controller,
            [
                {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["fleet-role-probe"], "verbs": ["get"]},
                {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["fleet-role-status"], "verbs": ["get", "patch"]},
            ],
        )
        self.assertEqual(probe, [{"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["fleet-role-probe"], "verbs": ["get", "patch"]}])
        for rule in controller + probe:
            self.assertNotIn("create", rule["verbs"])
            self.assertNotIn("delete", rule["verbs"])

    def test_each_binding_grants_its_role_to_its_own_account(self) -> None:
        for kind, role_kind in (("ClusterRoleBinding", "ClusterRole"), ("RoleBinding", "Role")):
            for binding in of_kind(self.manifests, kind):
                self.assertEqual(binding["roleRef"]["kind"], role_kind)
                [subject] = binding["subjects"]
                self.assertEqual(subject["kind"], "ServiceAccount")
                self.assertIn(subject["name"], ("fleet-role", "fleet-role-probe"))
                self.assertTrue(binding["roleRef"]["name"].endswith(subject["name"]), binding)
                self.assertEqual(subject["namespace"], "github-runner-platform")

    def test_the_controller_runs_as_an_unprivileged_user_with_a_read_only_root_and_is_a_dry_run(self) -> None:
        deployment = of_kind(self.manifests, "Deployment")[0]
        pod = deployment["spec"]["template"]["spec"]
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(container(deployment)["securityContext"], {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}})
        self.assertEqual(env_of(deployment)["FLEET_ROLE_DRY_RUN"], "true")
        self.assertNotIn("hostNetwork", pod)

    def test_the_controller_gets_every_setting_its_script_requires(self) -> None:
        deployment = of_kind(self.manifests, "Deployment")[0]
        required = script_requirements("fleet-role.sh", "FLEET_ROLE_")
        self.assertTrue(required, "the script's requirements were not found")
        self.assertLessEqual(required, set(env_of(deployment)))
        self.assertIn("FLEET_ROLE_SAMPLE_SECONDS", set(re.findall(r"\$\{(FLEET_ROLE_[A-Z_]+):\?", (ROOT / "fleet-role" / "loop.sh").read_text())))

    def test_the_probe_gets_every_setting_its_script_requires(self) -> None:
        daemonset = of_kind(self.manifests, "DaemonSet")[0]
        required = script_requirements("fleet-role-probe.sh")
        self.assertEqual(required, {"NODE_NAME", "NAMESPACE", "ELECTION_TIMEOUT_MS", "ELIGIBLE_LABEL"})
        self.assertLessEqual(required, set(env_of(daemonset)))

    def test_the_default_settings_reach_the_controller_as_strings(self) -> None:
        env = env_of(of_kind(self.manifests, "Deployment")[0])
        self.assertEqual(
            {k: v for k, v in env.items() if k.startswith("FLEET_ROLE_") and k not in ("FLEET_ROLE_STATE_NAMESPACE",)},
            {
                "FLEET_ROLE_DRY_RUN": "true",
                "FLEET_ROLE_VOTERS": "3",
                "FLEET_ROLE_MAX_SERVERS": "5",
                "FLEET_ROLE_ELECTION_TIMEOUT_MS": "1000",
                "FLEET_ROLE_SAMPLE_SECONDS": "15",
                "FLEET_ROLE_PRESSURE_THRESHOLD": "10",
                "FLEET_ROLE_AVAILABLE_FLOOR_PERCENT": "15",
                "FLEET_ROLE_ELIGIBLE_LABEL": "github-runner.exadev.com/voter-eligible",
                "FLEET_ROLE_PINNED_LABEL": "github-runner.exadev.com/voter-pinned",
                "FLEET_ROLE_LEVEL_LABEL": "github-runner.exadev.com/regrade-level",
                "FLEET_ROLE_PROBE_CONFIGMAP": "fleet-role-probe",
                "FLEET_ROLE_STATUS_CONFIGMAP": "fleet-role-status",
            },
        )

    def test_the_cluster_roles_own_settings_reach_the_controller_when_they_are_defined(self) -> None:
        manifests = render(
            github_runner_cluster_max_servers=7,
            github_runner_cluster_etcd_election_timeout=3000,
            github_runner_cluster_regrade_pressure_threshold=20,
            github_runner_cluster_regrade_available_floor_percent=25,
            github_runner_cluster_regrade_level_label="example.com/level",
            github_runner_cluster_voters=5,
            github_runner_cluster_voter_eligible_label="example.com/candidate",
            github_runner_cluster_voter_pinned_label="example.com/pinned",
        )
        env = env_of(of_kind(manifests, "Deployment")[0])
        self.assertEqual(
            (env["FLEET_ROLE_MAX_SERVERS"], env["FLEET_ROLE_ELECTION_TIMEOUT_MS"], env["FLEET_ROLE_PRESSURE_THRESHOLD"], env["FLEET_ROLE_AVAILABLE_FLOOR_PERCENT"], env["FLEET_ROLE_LEVEL_LABEL"]),
            ("7", "3000", "20", "25", "example.com/level"),
        )
        self.assertEqual(env_of(of_kind(manifests, "DaemonSet")[0])["ELECTION_TIMEOUT_MS"], "3000")
        self.assertEqual(
            (env["FLEET_ROLE_VOTERS"], env["FLEET_ROLE_ELIGIBLE_LABEL"], env["FLEET_ROLE_PINNED_LABEL"], env_of(of_kind(manifests, "DaemonSet")[0])["ELIGIBLE_LABEL"]),
            ("5", "example.com/candidate", "example.com/pinned", "example.com/candidate"),
        )

    def test_the_controller_and_the_probe_agree_on_the_election_timeout_and_the_period(self) -> None:
        manifests = render(**{ARC + "sample_seconds": 30})
        controller, probe = env_of(of_kind(manifests, "Deployment")[0]), env_of(of_kind(manifests, "DaemonSet")[0])
        self.assertEqual(controller["FLEET_ROLE_ELECTION_TIMEOUT_MS"], probe["ELECTION_TIMEOUT_MS"])
        self.assertEqual((controller["FLEET_ROLE_SAMPLE_SECONDS"], probe["REPORT_SECONDS"]), ("30", "30"))

    def test_the_probe_measures_from_the_nodes_own_network_and_tolerates_every_taint(self) -> None:
        daemonset = of_kind(self.manifests, "DaemonSet")[0]
        pod = daemonset["spec"]["template"]["spec"]
        self.assertTrue(pod["hostNetwork"])
        self.assertEqual(pod["dnsPolicy"], "ClusterFirstWithHostNet")
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/os": "linux"})
        self.assertEqual(pod["tolerations"], [{"operator": "Exists"}])
        self.assertEqual(container(daemonset)["command"], ["/app/scripts/fleet-role-probe.sh"])
        self.assertEqual(env_of(daemonset)["NODE_NAME"], {"fieldRef": {"fieldPath": "spec.nodeName"}})

    @staticmethod
    def terms(manifests: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """The alternatives of the probe's node affinity, each the expressions a node must meet."""
        pod = of_kind(manifests, "DaemonSet")[0]["spec"]["template"]["spec"]
        return [term["matchExpressions"] for term in pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]]

    @staticmethod
    def scheduled(terms: list[list[dict[str, Any]]], labels: dict[str, str]) -> bool:
        """Whether a node with these labels meets one of the terms, as the scheduler reads In expressions."""
        return any(all(e["operator"] == "In" and labels.get(e["key"]) in e["values"] for e in term) for term in terms)

    def test_the_probe_is_scheduled_only_on_a_voter_or_an_eligible_node(self) -> None:
        terms = self.terms(self.manifests)
        voter, eligible = "node-role.kubernetes.io/etcd", "github-runner.exadev.com/voter-eligible"
        self.assertTrue(self.scheduled(terms, {voter: "true"}), "a voter that is not marked eligible")
        self.assertTrue(self.scheduled(terms, {eligible: "true"}), "an eligible standby that is not a voter")
        self.assertTrue(self.scheduled(terms, {voter: "true", eligible: "true"}))
        self.assertFalse(self.scheduled(terms, {}), "a node that is neither: an agent, or a host that is neither voter nor eligible")
        self.assertFalse(self.scheduled(terms, {eligible: "false"}), "the label with another value is not the owner's mark")
        self.assertFalse(self.scheduled(terms, {"node-role.kubernetes.io/control-plane": "true", "node-role.kubernetes.io/master": "true"}), "a control-plane node that holds no etcd member")

    def test_the_eligible_term_follows_the_controllers_label_name(self) -> None:
        manifests = render(**{f"{ARC}eligible_label": "example.com/standby"})
        terms = self.terms(manifests)
        self.assertTrue(self.scheduled(terms, {"example.com/standby": "true"}))
        self.assertFalse(self.scheduled(terms, {"github-runner.exadev.com/voter-eligible": "true"}))
        controller = env_of(of_kind(manifests, "Deployment")[0])
        self.assertEqual(controller["FLEET_ROLE_ELIGIBLE_LABEL"], "example.com/standby")

    def test_the_probe_reads_the_nodes_pressure_from_its_own_procfs_so_a_kernel_without_it_cannot_fail_the_pod(self) -> None:
        # A hostPath volume of /proc/pressure makes the container runtime create the directory when the kernel has none, and the pod then sits in CreateContainerError.
        daemonset = of_kind(self.manifests, "DaemonSet")[0]
        pod = daemonset["spec"]["template"]["spec"]
        self.assertNotIn("/proc", json.dumps(pod["volumes"]))
        self.assertNotIn("pressure", {v["name"] for v in pod["volumes"]} | {m["name"] for m in container(daemonset)["volumeMounts"]})
        self.assertNotIn("PSI_FILE", env_of(daemonset))
        self.assertIn('PSI_FILE="${PSI_FILE:-/proc/pressure/memory}"', (ROOT / "scripts" / "fleet-role-probe.sh").read_text())

    def test_the_voter_label_the_template_schedules_on_is_the_one_the_controller_and_the_probe_read(self) -> None:
        voter = "node-role.kubernetes.io/etcd"
        self.assertIn(f'voter_label: "{voter}"', (ROOT / "scripts" / "fleet-role.sh").read_text())
        self.assertIn(f'VOTER_LABEL="${{VOTER_LABEL:-{voter}=true}}"', (ROOT / "scripts" / "fleet-role-probe.sh").read_text())
        self.assertEqual(env_of(of_kind(self.manifests, "DaemonSet")[0])["VOTER_LABEL"], f"{voter}=true")

    def test_the_probe_is_root_only_for_its_own_directory_and_holds_no_capability(self) -> None:
        daemonset = of_kind(self.manifests, "DaemonSet")[0]
        self.assertEqual(
            container(daemonset)["securityContext"],
            {"runAsUser": 0, "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
        )
        self.assertNotIn("privileged", container(daemonset)["securityContext"])

    def test_the_probe_writes_only_in_its_own_directory(self) -> None:
        daemonset = of_kind(self.manifests, "DaemonSet")[0]
        pod = daemonset["spec"]["template"]["spec"]
        mounts = {m["name"]: m for m in container(daemonset)["volumeMounts"]}
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(volumes["fsync"]["hostPath"], {"path": "/var/lib/rancher/k3s/fleet-role-probe", "type": "DirectoryOrCreate"})
        self.assertEqual(mounts["fsync"]["mountPath"], env_of(daemonset)["FSYNC_DIR"])
        self.assertEqual(sorted(volumes), ["fsync", "tmp"])
        self.assertEqual({name for name, mount in mounts.items() if not mount.get("readOnly")}, {"tmp", "fsync"})

    def test_the_controller_and_the_probe_run_one_image(self) -> None:
        images = {container(m)["image"] for m in of_kind(self.manifests, "Deployment") + of_kind(self.manifests, "DaemonSet")}
        self.assertEqual(len(images), 1)
        self.assertRegex(images.pop(), r"^ghcr\.io/exadev/github-runner-fleet-role:\d+\.\d+\.\d+$")

    def test_every_script_the_manifests_run_is_copied_into_the_image_and_made_executable(self) -> None:
        dockerfile = (ROOT / "fleet-role" / "Dockerfile").read_text()
        template = (ROLE / "templates" / "fleet-role.yaml.j2").read_text()
        scripts = set(re.findall(r"/app/scripts/([A-Za-z0-9._-]+\.sh)", template))
        self.assertEqual(scripts, {"fleet-role-probe.sh"})
        scripts.add("fleet-role.sh")  # run by the image's entrypoint
        chmod = re.search(r"RUN chmod \+x ([^\n]+)", dockerfile)
        assert chmod is not None
        for script in scripts:
            with self.subTest(script):
                self.assertTrue((ROOT / "scripts" / script).is_file())
                self.assertRegex(dockerfile, rf"COPY [^\n]*scripts/{re.escape(script)}[^\n]* /app/scripts/")
                self.assertIn(f"/app/scripts/{script}", chmod.group(1).split())
        self.assertIn("/app/loop.sh", chmod.group(1).split())
        entrypoint = (ROOT / "fleet-role" / "loop.sh").read_text()
        self.assertIn("/app/scripts/fleet-role.sh", entrypoint)

    def test_every_jq_module_the_scripts_include_is_copied_beside_them(self) -> None:
        dockerfile = (ROOT / "fleet-role" / "Dockerfile").read_text()
        for program in ("fleet-role.jq", "fleet-role-stats.jq"):
            for module in re.findall(r'^include "([A-Za-z0-9._-]+)";', (ROOT / "scripts" / program).read_text(), re.MULTILINE):
                with self.subTest(program=program, module=module):
                    self.assertTrue((ROOT / "scripts" / f"{module}.jq").is_file())
                    self.assertRegex(dockerfile, rf"COPY [^\n]*scripts/{re.escape(module)}\.jq[^\n]* /app/scripts/")
        for program in ("fleet-role.jq", "fleet-role-stats.jq"):
            self.assertRegex(dockerfile, rf"COPY [^\n]*scripts/{re.escape(program)}[^\n]* /app/scripts/")

    def test_the_probe_reads_the_regrade_core_the_image_carries(self) -> None:
        dockerfile = (ROOT / "fleet-role" / "Dockerfile").read_text()
        script = (ROOT / "scripts" / "fleet-role-probe.sh").read_text()
        default = re.search(r'FLEET_REGRADE_CORE="\$\{FLEET_REGRADE_CORE:-([^}]+)\}"', script)
        assert default is not None
        self.assertIn(f"COPY roles/github_runner_cluster/files/regrade/fleet-regrade-core.sh {default.group(1)}", dockerfile)
        for name in ("fleet-role.sh", "fleet-role-probe.sh", "fleet-role.jq", "fleet-role-stats.jq"):
            self.assertIn(name, dockerfile)

    def test_the_two_workloads_run_under_the_platforms_priority_class_and_ephemeral_storage_bounds(self) -> None:
        for manifest in of_kind(self.manifests, "Deployment") + of_kind(self.manifests, "DaemonSet"):
            pod = manifest["spec"]["template"]["spec"]
            self.assertEqual(pod["priorityClassName"], "github-runner-platform")
            resources = container(manifest)["resources"]
            self.assertEqual((resources["requests"]["ephemeral-storage"], resources["limits"]["ephemeral-storage"]), ("64Mi", "128Mi"))


class NamesTest(unittest.TestCase):
    """Names that issue #184 fixes and the fleet's goal gate reads."""

    def test_the_opt_in_is_the_cluster_roles_variable_and_off_by_default(self) -> None:
        cluster = yaml.safe_load((CLUSTER_ROLE / "defaults" / "main.yml").read_text())
        self.assertIs(cluster["github_runner_cluster_role_controller"], False)

    def test_one_inventory_setting_turns_it_on_and_the_status_goes_to_fleet_role_status_in_the_platform_namespace(self) -> None:
        manifests = render()
        deployment = of_kind(manifests, "Deployment")[0]
        self.assertEqual(env_of(deployment)["FLEET_ROLE_STATUS_CONFIGMAP"], "fleet-role-status")
        self.assertEqual(deployment["metadata"]["namespace"], "github-runner-platform")
        self.assertEqual(env_of(deployment)["FLEET_ROLE_DRY_RUN"], "true")

    def test_the_arc_roles_switch_follows_the_cluster_roles(self) -> None:
        template = ROOT / "tests" / "fleet_role" / "enabled.j2"
        with tempfile.TemporaryDirectory() as work:
            output = Path(work) / "enabled.txt"
            for cluster_value, arc_value, expected in ((None, None, "False"), (True, None, "True"), (False, True, "True"), (True, False, "False")):
                variables: dict[str, Any] = {"kept_template": str(template), "kept_output": str(output)}
                if cluster_value is not None:
                    variables["github_runner_cluster_role_controller"] = cluster_value
                if arc_value is not None:
                    variables[ARC + "enabled"] = arc_value
                completed = ansible(EXPRESSION, **variables)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertEqual(output.read_text().strip(), expected, (cluster_value, arc_value))


class DefaultsAgreeWithTheClusterRoleTest(unittest.TestCase):
    """The fallbacks in the ARC role's defaults stand in for the cluster role's variables when they are not defined, so they must be their defaults."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.arc = (ROLE / "defaults" / "main.yml").read_text()
        cls.cluster = yaml.safe_load((CLUSTER_ROLE / "defaults" / "main.yml").read_text())

    def fallback(self, name: str) -> str:
        match = re.search(rf"^{ARC}{name}: \"\{{\{{ [a-z_]+ \| default\(([^,)]+)\)", self.arc, re.M)
        assert match is not None, name
        return match.group(1).strip("'")

    def test_max_servers(self) -> None:
        self.assertEqual(int(self.fallback("max_servers")), self.cluster["github_runner_cluster_max_servers"])

    def test_the_regrade_limit(self) -> None:
        self.assertEqual(int(self.fallback("pressure_threshold")), self.cluster["github_runner_cluster_regrade_pressure_threshold"])
        self.assertEqual(int(self.fallback("available_floor_percent")), self.cluster["github_runner_cluster_regrade_available_floor_percent"])

    def test_the_voter_target_and_the_two_voter_labels(self) -> None:
        self.assertEqual(int(self.fallback("voters")), self.cluster["github_runner_cluster_voters"])
        self.assertEqual(self.fallback("eligible_label"), self.cluster["github_runner_cluster_voter_eligible_label"])
        self.assertEqual(self.fallback("pinned_label"), self.cluster["github_runner_cluster_voter_pinned_label"])

    def test_the_level_label(self) -> None:
        self.assertEqual(self.fallback("level_label"), self.cluster["github_runner_cluster_regrade_level_label"])

    def test_the_election_timeout_falls_back_to_k3ss_own(self) -> None:
        # The controller's scripts hold no default of their own; the cluster role's own comment names the figure.
        self.assertIn("or 1000 }}", self.arc)
        self.assertEqual(self.cluster["github_runner_cluster_etcd_election_timeout"], "")
        self.assertIn("1000 ms election timeout", (CLUSTER_ROLE / "defaults" / "main.yml").read_text())


class ValidationTest(unittest.TestCase):
    """The role controller's checks in tasks/validate.yml, evaluated by Ansible itself the way the role evaluates them, every case in one run."""

    VOTERS = ARC + "voters"
    MAX = ARC + "max_servers"
    ON = {ARC + "enabled": True}
    CASES: dict[str, dict[str, Any]] = {
        "defaults": {},
        "on with the defaults": {**ON},
        "target 5 of 6": {**ON, VOTERS: 5, MAX: 6},
        "target 5 of 5": {**ON, VOTERS: 5, MAX: 5},
        "target 3 of 4": {**ON, VOTERS: 3, MAX: 4},
        "target 3 of 3": {**ON, VOTERS: 3, MAX: 3},
        "target 4": {**ON, VOTERS: 4, MAX: 9},
        "target 1": {**ON, VOTERS: 1},
        "target 2": {**ON, VOTERS: 2},
        "target zero": {**ON, VOTERS: 0},
        "target text": {**ON, VOTERS: "three"},
        "target as a string": {**ON, VOTERS: "3"},
        "sample zero": {**ON, ARC + "sample_seconds": 0},
        "sample text": {**ON, ARC + "sample_seconds": "fifteen"},
        "election timeout zero": {**ON, ARC + "election_timeout_ms": 0},
        "max servers text": {**ON, MAX: "many"},
        "same label": {**ON, ARC + "pinned_label": "example.com/x", ARC + "eligible_label": "example.com/x"},
        "bad label": {**ON, ARC + "eligible_label": "has space"},
        "good labels": {**ON, ARC + "eligible_label": "example.com/eligible", ARC + "pinned_label": "pinned"},
        "bad settings while off": {VOTERS: 4, ARC + "sample_seconds": 0, ARC + "eligible_label": "has space"},
    }

    @classmethod
    def setUpClass(cls) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "validate.yml").read_text())
        [task] = [task for task in tasks if task["name"].startswith("Fail on invalid controller, metrics, probe, pull Secret, node recovery")]
        cls.checks = {check["msg"].split(" ")[0]: check["failed"] for check in task["loop"] if "role_controller" in check["msg"]}
        lines = []
        for name, role_vars in cls.CASES.items():
            sets = "".join(f"{{% set {variable} = {json.dumps(value)} %}}" for variable, value in role_vars.items())
            for setting, expression in cls.checks.items():
                lines.append(f"{{% with %}}{sets}{{{{ [{json.dumps(name)}, {json.dumps(setting)}, ({expression.strip().removeprefix('{{').removesuffix('}}')}) | bool] | to_json }}}}{{% endwith %}}")
        with tempfile.TemporaryDirectory() as work:
            template, output = Path(work) / "failed.j2", Path(work) / "failed.txt"
            template.write_text(";".join(lines))
            completed = ansible(EXPRESSION, kept_template=str(template), kept_output=str(output))
            assert completed.returncode == 0, completed.stdout + completed.stderr
            cls.results: dict[str, dict[str, bool]] = {}
            for line in output.read_text().split(";"):
                name, setting, failed = json.loads(line)
                cls.results.setdefault(name, {})[setting] = failed

    def refused(self, case: str) -> bool:
        return any(self.results[case].values())

    def refused_by(self, case: str) -> list[str]:
        return [setting for setting, failed in self.results[case].items() if failed]

    def test_there_are_three_checks_and_they_are_named_for_the_settings_they_guard(self) -> None:
        self.assertEqual(len(self.checks), 3)
        self.assertEqual(sorted(self.checks)[0], ARC + "eligible_label")

    def test_the_defaults_and_a_switched_on_default_are_valid(self) -> None:
        self.assertFalse(self.refused("defaults"))
        self.assertFalse(self.refused("on with the defaults"))

    def test_the_target_must_be_odd_at_least_three_and_leave_a_place_of_headroom(self) -> None:
        for case in ("target 5 of 6", "target 3 of 4", "target as a string"):
            with self.subTest(case):
                self.assertFalse(self.refused(case))
        for case in ("target 5 of 5", "target 3 of 3", "target 4", "target 1", "target 2", "target zero", "target text"):
            with self.subTest(case):
                self.assertEqual(self.refused_by(case), [ARC + "voters"])

    def test_the_period_the_timeout_and_the_maximum_must_be_whole_numbers_of_at_least_one(self) -> None:
        for case in ("sample zero", "sample text", "election timeout zero", "max servers text"):
            with self.subTest(case):
                self.assertTrue(self.refused(case))

    def test_the_two_labels_must_be_label_keys_and_differ(self) -> None:
        for case in ("same label", "bad label"):
            with self.subTest(case):
                self.assertTrue(self.refused(case))
        self.assertFalse(self.refused("good labels"))

    def test_nothing_is_checked_while_the_controller_is_off(self) -> None:
        self.assertFalse(self.refused("bad settings while off"))


if __name__ == "__main__":
    unittest.main()
