"""Tests for how the github_runner_arc role installs the runner reaper: the manifests templates/runner-reaper.yaml.j2 renders (through tests/runner_reaper/render.yml, with ansible-playbook), and the toggle in tasks/install_runner_reaper.yml that installs them or removes them."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "github_runner_arc"
RENDER = ROOT / "tests" / "runner_reaper" / "render.yml"
NAMESPACE = "example-runners"


def render(**role_vars: Any) -> dict[str, dict[str, Any]]:
    """Render the reaper's manifests with ansible-playbook and return them by kind."""
    with tempfile.TemporaryDirectory() as work:
        output = Path(work) / "reaper.yaml"
        extra = Path(work) / "extra.json"
        extra.write_text(json.dumps({"reaper_namespace": NAMESPACE, "reaper_output": str(output), **role_vars}))
        env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false"}
        subprocess.run([os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(RENDER), "-e", f"@{extra}"], check=True, env=env, cwd=work, capture_output=True, text=True)
        documents = [document for document in yaml.safe_load_all(output.read_text()) if document]
    by_kind = {document["kind"]: document for document in documents}
    assert len(by_kind) == len(documents), "two manifests of one kind"
    return by_kind


class ManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifests = render()
        cls.pod = cls.manifests["CronJob"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        cls.container = cls.pod["containers"][0]

    def test_everything_is_in_the_runner_namespace(self) -> None:
        self.assertEqual(set(self.manifests), {"ServiceAccount", "Role", "RoleBinding", "ConfigMap", "CronJob"})
        for manifest in self.manifests.values():
            self.assertEqual(manifest["metadata"]["namespace"], NAMESPACE)

    def test_the_role_may_only_list_and_delete_ephemeral_runners(self) -> None:
        self.assertEqual(self.manifests["Role"]["rules"], [{"apiGroups": ["actions.github.com"], "resources": ["ephemeralrunners"], "verbs": ["list", "delete"]}])

    def test_the_binding_gives_that_role_to_the_cronjobs_service_account(self) -> None:
        binding = self.manifests["RoleBinding"]
        account = self.manifests["ServiceAccount"]["metadata"]["name"]
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": account, "namespace": NAMESPACE}])
        self.assertEqual(binding["roleRef"], {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": self.manifests["Role"]["metadata"]["name"]})
        self.assertEqual(self.pod["serviceAccountName"], account)

    def test_the_configmap_carries_the_script_and_the_token_minting_it_sources(self) -> None:
        files = ROLE / "files"
        self.assertEqual(self.manifests["ConfigMap"]["data"], {
            "reap-runners.sh": (files / "reap-runners.sh").read_text().rstrip("\n"),
            "github-app-token.sh": (files / "github-app-token.sh").read_text().rstrip("\n"),
        })

    def test_the_cronjob_runs_the_mounted_script_one_pass_at_a_time(self) -> None:
        cronjob = self.manifests["CronJob"]
        self.assertEqual(cronjob["spec"]["schedule"], "*/5 * * * *")
        self.assertEqual(cronjob["spec"]["concurrencyPolicy"], "Forbid")
        self.assertEqual(cronjob["spec"]["jobTemplate"]["spec"]["backoffLimit"], 0)
        mounts = {mount["name"]: mount for mount in self.container["volumeMounts"]}
        self.assertEqual(self.container["command"], ["bash", mounts["scripts"]["mountPath"] + "/reap-runners.sh"])
        self.assertTrue(mounts["scripts"]["readOnly"])
        volumes = {volume["name"]: volume for volume in self.pod["volumes"]}
        self.assertEqual(volumes["scripts"]["configMap"]["name"], self.manifests["ConfigMap"]["metadata"]["name"])
        self.assertEqual(volumes["github-app"]["secret"]["secretName"], "example-org-github-app")
        self.assertEqual(mounts["github-app"]["mountPath"], self.env()["GITHUB_APP_DIR"])

    def test_the_bound_grace_and_dry_run_reach_the_script(self) -> None:
        self.assertEqual({key: self.env()[key] for key in ("MAX_JOB_SECONDS", "COMPLETED_GRACE_SECONDS", "DRY_RUN")}, {"MAX_JOB_SECONDS": "432000", "COMPLETED_GRACE_SECONDS": "300", "DRY_RUN": "false"})
        container = render(github_runner_arc_runner_reaper_max_job_seconds=7200, github_runner_arc_runner_reaper_dry_run=True)["CronJob"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        self.assertEqual((env["MAX_JOB_SECONDS"], env["DRY_RUN"]), ("7200", "true"))

    def test_the_pod_runs_unprivileged_with_a_read_only_root(self) -> None:
        self.assertTrue(self.pod["securityContext"]["runAsNonRoot"])
        self.assertFalse(self.container["securityContext"]["allowPrivilegeEscalation"])
        self.assertTrue(self.container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(self.container["securityContext"]["capabilities"], {"drop": ["ALL"]})

    def test_the_image_defaults_to_the_pull_secret_renewers(self) -> None:
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        self.assertEqual(self.container["image"], defaults["github_runner_arc_image_pull_secret_renewer_image"])

    def env(self) -> dict[str, str]:
        return {item["name"]: item["value"] for item in self.container["env"] if "value" in item}


class ToggleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tasks = yaml.safe_load((ROLE / "tasks" / "install_runner_reaper.yml").read_text())

    def task(self, prefix: str) -> dict[str, Any]:
        [task] = [task for task in self.tasks if task["name"].startswith(prefix)]
        return task

    def test_it_is_off_by_default(self) -> None:
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        self.assertIs(defaults["github_runner_arc_runner_reaper_enabled"], False)
        specs = yaml.safe_load((ROLE / "meta" / "argument_specs.yml").read_text())
        options = specs["argument_specs"]["main"]["options"]
        self.assertIs(options["github_runner_arc_runner_reaper_enabled"]["default"], False)

    def test_on_installs_every_manifest(self) -> None:
        install = self.task("Install the runner reaper")
        self.assertEqual(install["when"], "github_runner_arc_runner_reaper_enabled | bool")
        self.assertEqual(install["loop"], "{{ github_runner_arc_runner_reaper_resources }}")
        self.assertEqual(install["kubernetes.core.k8s"], {"definition": "{{ item }}"})

    def test_off_removes_every_manifest_cronjob_first(self) -> None:
        remove = self.task("Remove any runner reaper")
        self.assertEqual(remove["when"], "not (github_runner_arc_runner_reaper_enabled | bool)")
        self.assertEqual(remove["kubernetes.core.k8s"]["state"], "absent")
        self.assertEqual(remove["loop"], "{{ github_runner_arc_runner_reaper_resources | reverse | list }}")
        self.assertEqual(list(render())[-1], "CronJob")

    def test_each_profile_includes_it(self) -> None:
        profile_tasks = yaml.safe_load((ROLE / "tasks" / "install_scale_set_profile.yml").read_text())
        self.assertIn("install_runner_reaper.yml", [task.get("ansible.builtin.include_tasks") for task in profile_tasks])


if __name__ == "__main__":
    unittest.main()
