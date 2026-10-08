"""Tests for the registry cache: the registry description and runner pod wiring in plugins/filter/arc.py, the scale-set values the role renders through tasks/render_scale_set_values.yml (run from tests/capabilities/render.yml), and the cache's own resources rendered through tasks/render_registry_cache.yml (run from tests/registry_cache/render.yml)."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "filter" / "arc.py"
RENDER_VALUES = ROOT / "tests" / "capabilities" / "render.yml"
RENDER_CACHE = ROOT / "tests" / "registry_cache" / "render.yml"
_spec = importlib.util.spec_from_file_location("arc_filters", PLUGIN)
assert _spec is not None and _spec.loader is not None
arc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arc)

NAMESPACE = "github-runner-registry-cache"
SERVICE = "registry-cache"
ADDRESS = f"{SERVICE}.{NAMESPACE}.svc.cluster.local"
RUNNER_IMAGE = "ghcr.io/example/runner:1"
CONFIGMAP = "registry-cache"
BUILDX_DIR = "/etc/github-runner/buildx"
# A registry with credentials and one without, and a registry on a port of its own.
REGISTRIES: list[dict[str, Any]] = [
    {"name": "docker-hub", "host": "docker.io", "credentials": {"secret_name": "hub-read"}},
    {"name": "ghcr", "host": "ghcr.io"},
    {"name": "internal", "host": "registry.example.com:8443", "credentials": {"secret_name": "internal-read", "username_key": "user", "password_key": "token"}},
]
DEFAULT_VALUES: dict[str, Any] = {"minRunners": 0, "maxRunners": 2, "template": {"spec": {"containers": [{"name": "runner", "command": ["/home/runner/run.sh"]}]}}}
DIND_VALUES: dict[str, Any] = {**DEFAULT_VALUES, "containerMode": {"type": "dind"}}


def cache(registries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Describe the cache with the role's default names, failing on any error."""
    result: dict[str, Any] = arc.arc_registry_cache(REGISTRIES if registries is None else registries, NAMESPACE, SERVICE, "cluster.local", 5000)
    assert result["errors"] == [], result["errors"]
    return result


def errors(registries: Any) -> list[str]:
    """Return the problems arc_registry_cache finds in registries."""
    found: list[str] = arc.arc_registry_cache(registries, NAMESPACE, SERVICE, "cluster.local", 5000)["errors"]
    return found


def wire(values: dict[str, Any]) -> dict[str, Any]:
    """Point values at the cache described from REGISTRIES."""
    result: dict[str, Any] = arc.arc_registry_cache_pod(values, cache(), RUNNER_IMAGE, CONFIGMAP, BUILDX_DIR)
    return result


def named(items: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """Return the item of a list of named Kubernetes objects that carries name."""
    item: dict[str, Any] = next(item for item in items if item["name"] == name)
    return item


def ansible_env(work: str) -> dict[str, str]:
    """Return the environment ansible-playbook runs the render playbooks with: this checkout's collection, and nothing written outside work."""
    return {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(ROOT / "playbooks" / "collections"), "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false", "ANSIBLE_HOME": work, "ANSIBLE_LOCAL_TEMP": work}


def run_playbook(playbook: Path, extra: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    """Run a render playbook with extra variables, returning the completed process with the playbook's output in stdout, and the rendered JSON, when it wrote any, in the attribute rendered."""
    with tempfile.TemporaryDirectory() as work:
        output = Path(work) / "rendered.json"
        extra_file = Path(work) / "extra.json"
        extra_file.write_text(json.dumps({**extra, "render_output": str(output)}))
        completed = subprocess.run([os.environ.get("ANSIBLE_PLAYBOOK", "ansible-playbook"), str(playbook), "-e", f"@{extra_file}"], env=ansible_env(work), cwd=work, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        completed.rendered = json.loads(output.read_text()) if output.exists() else None  # type: ignore[attr-defined]
        return completed


def expanded(profile: dict[str, Any]) -> dict[str, Any]:
    """Return the one profile arc_profiles expands from profile, failing on any error."""
    result = arc.arc_profiles([{"name": "Example", "image": RUNNER_IMAGE, "scale_set_profiles": [profile]}], require_app_id=False)
    assert result["errors"] == [], result["errors"]
    profile_expanded: dict[str, Any] = result["profiles"][0]
    return profile_expanded


class RegistriesTest(unittest.TestCase):
    def test_each_registry_gets_its_own_port_and_its_upstream(self) -> None:
        registries = cache()["registries"]
        self.assertEqual([(registry["name"], registry["port"], registry["url"]) for registry in registries], [("docker-hub", 5000, "https://registry-1.docker.io"), ("ghcr", 5001, "https://ghcr.io"), ("internal", 5002, "https://registry.example.com:8443")])
        self.assertEqual(registries[1]["address"], f"{ADDRESS}:5001")

    def test_credentials_are_a_secret_reference_with_default_keys(self) -> None:
        registries = cache()["registries"]
        self.assertEqual(registries[0]["credentials"], {"secret_name": "hub-read", "username_key": "username", "password_key": "password"})
        self.assertIsNone(registries[1]["credentials"])
        self.assertEqual(registries[2]["credentials"], {"secret_name": "internal-read", "username_key": "user", "password_key": "token"})

    def test_hosts_toml_lists_the_cache_ahead_of_the_registry(self) -> None:
        hosts = cache()["hosts"]
        self.assertEqual(set(hosts), {"docker-hub.hosts.toml", "ghcr.hosts.toml", "internal.hosts.toml"})
        self.assertEqual(hosts["ghcr.hosts.toml"], f'server = "https://ghcr.io"\n\n[host."http://{ADDRESS}:5001"]\n  capabilities = ["pull", "resolve", "referrers"]\n')
        self.assertIn('server = "https://registry-1.docker.io"', hosts["docker-hub.hosts.toml"])

    def test_buildkitd_mirrors_every_registry_through_the_cache_over_http(self) -> None:
        buildkitd = cache()["buildkitd"]
        for host, port in (("docker.io", 5000), ("ghcr.io", 5001), ("registry.example.com:8443", 5002)):
            with self.subTest(host=host):
                self.assertIn(f'[registry."{host}"]\n  mirrors = ["{ADDRESS}:{port}"]\n', buildkitd)
                self.assertIn(f'[registry."{ADDRESS}:{port}"]\n  http = true\n', buildkitd)

    def test_inline_credentials_are_refused(self) -> None:
        found = errors([{"name": "ghcr", "host": "ghcr.io", "credentials": {"secret_name": "ghcr-read", "username": "someone", "password": "not-a-real-token"}}])
        self.assertTrue(any("never given inline" in error for error in found), found)
        # The message names the keys, never their values.
        self.assertFalse(any("not-a-real-token" in error for error in found), found)

    def test_bad_registries_are_errors(self) -> None:
        cases = {
            "an empty list": ([], "non-empty list"),
            "not a list": ("docker.io", "non-empty list"),
            "a name too long for a port name": ([{"name": "a-very-long-registry", "host": "ghcr.io"}], "at most 15"),
            "a name with no letter": ([{"name": "123", "host": "ghcr.io"}], "at most 15"),
            "an upper-case name": ([{"name": "GHCR", "host": "ghcr.io"}], "at most 15"),
            "two entries with one name": ([{"name": "ghcr", "host": "ghcr.io"}, {"name": "ghcr", "host": "quay.io"}], "used by another registry"),
            "two entries for one host": ([{"name": "a", "host": "ghcr.io"}, {"name": "b", "host": "ghcr.io"}], "cached by another entry"),
            "a host with a path": ([{"name": "ghcr", "host": "ghcr.io/example"}], "registry host"),
            "a url with a path": ([{"name": "ghcr", "host": "ghcr.io", "url": "https://ghcr.io/v2"}], "no path"),
            "a url that is not http": ([{"name": "ghcr", "host": "ghcr.io", "url": "ftp://ghcr.io"}], "no path"),
            "an unknown key": ([{"name": "ghcr", "host": "ghcr.io", "mirror": "x"}], "unknown key 'mirror'"),
            "credentials without a Secret": ([{"name": "ghcr", "host": "ghcr.io", "credentials": {}}], "secret_name must be"),
            "credentials that are not a mapping": ([{"name": "ghcr", "host": "ghcr.io", "credentials": "ghcr-read"}], "must be a mapping naming a Secret"),
            "an entry that is not a mapping": (["ghcr.io"], "must be a mapping"),
        }
        for case, (registries, message) in cases.items():
            with self.subTest(case):
                found = errors(registries)
                self.assertTrue(any(message in error for error in found), found)

    def test_a_port_past_the_last_is_an_error(self) -> None:
        found = arc.arc_registry_cache([{"name": "a", "host": "ghcr.io"}, {"name": "b", "host": "quay.io"}], NAMESPACE, SERVICE, "cluster.local", 65535)["errors"]
        self.assertTrue(any("above 65535" in error for error in found), found)


class SecretProblemsTest(unittest.TestCase):
    def test_missing_secrets_and_keys_are_named_without_their_data(self) -> None:
        registries = cache()["registries"]
        results = [
            {"item": registries[0], "resources": []},
            {"item": registries[2], "resources": [{"data": {"user": "c29tZW9uZQ==", "other": "bm90LWEtdG9rZW4="}}]},
        ]
        problems = arc.arc_registry_cache_secret_problems(results)
        self.assertEqual(problems, ["docker-hub: Secret hub-read does not exist", "internal: Secret internal-read has no key token"])

    def test_complete_secrets_have_no_problems(self) -> None:
        registries = cache()["registries"]
        self.assertEqual(arc.arc_registry_cache_secret_problems([{"item": registries[0], "resources": [{"data": {"username": "eA==", "password": "eA=="}}]}]), [])


class PodTest(unittest.TestCase):
    def test_values_without_docker_are_left_unchanged(self) -> None:
        result = wire(DEFAULT_VALUES)
        self.assertEqual(result, {"errors": [], "values": DEFAULT_VALUES, "wired": False})

    def test_dind_mode_is_written_out_with_the_hosts_mounted_on_the_daemon(self) -> None:
        result = wire(DIND_VALUES)
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["wired"])
        values = result["values"]
        self.assertNotIn("containerMode", values)
        spec = values["template"]["spec"]
        self.assertEqual([container["name"] for container in spec["initContainers"]], ["init-dind-externals", "dind"])
        self.assertEqual(spec["initContainers"][0]["image"], RUNNER_IMAGE)
        dind = named(spec["initContainers"], "dind")
        self.assertEqual(dind["restartPolicy"], "Always")
        self.assertEqual(dind["image"], "docker:dind")
        self.assertIn({"name": "registry-cache-hosts", "mountPath": "/etc/docker/certs.d", "readOnly": True}, dind["volumeMounts"])
        hosts = named(spec["volumes"], "registry-cache-hosts")["configMap"]
        self.assertEqual(hosts["name"], CONFIGMAP)
        self.assertEqual(hosts["items"], [{"key": "docker-hub.hosts.toml", "path": "docker.io/hosts.toml"}, {"key": "ghcr.hosts.toml", "path": "ghcr.io/hosts.toml"}, {"key": "internal.hosts.toml", "path": "registry.example.com:8443/hosts.toml"}])

    def test_the_runner_reaches_the_daemon_and_buildx_finds_its_configuration(self) -> None:
        spec = wire(DIND_VALUES)["values"]["template"]["spec"]
        runner = named(spec["containers"], "runner")
        self.assertEqual(runner["command"], ["/home/runner/run.sh"])
        self.assertEqual({variable["name"]: variable["value"] for variable in runner["env"]}, {"DOCKER_HOST": "unix:///var/run/docker.sock", "RUNNER_WAIT_FOR_DOCKER_IN_SECONDS": "120", "BUILDX_CONFIG": BUILDX_DIR})
        self.assertIn({"name": "dind-sock", "mountPath": "/var/run"}, runner["volumeMounts"])
        self.assertIn({"name": "registry-cache-buildx", "mountPath": BUILDX_DIR}, runner["volumeMounts"])
        self.assertIn({"name": "registry-cache-buildkitd", "mountPath": f"{BUILDX_DIR}/buildkitd.default.toml", "subPath": "buildkitd.default.toml", "readOnly": True}, runner["volumeMounts"])
        self.assertEqual(named(spec["volumes"], "registry-cache-buildx"), {"name": "registry-cache-buildx", "emptyDir": {}})

    def test_the_values_own_init_containers_volumes_and_runner_settings_are_kept(self) -> None:
        values = copy.deepcopy(DIND_VALUES)
        spec = values["template"]["spec"]
        spec["initContainers"] = [{"name": "capability-node", "image": "ghcr.io/example/toolcache-node:22"}]
        spec["volumes"] = [{"name": "work", "ephemeral": {"volumeClaimTemplate": {"spec": {"resources": {"requests": {"storage": "10Gi"}}}}}}]
        spec["containers"][0]["env"] = [{"name": "RUNNER_WAIT_FOR_DOCKER_IN_SECONDS", "value": "300"}]
        wired = wire(values)["values"]["template"]["spec"]
        self.assertEqual([container["name"] for container in wired["initContainers"]], ["init-dind-externals", "dind", "capability-node"])
        self.assertEqual(named(wired["volumes"], "work"), spec["volumes"][0])
        self.assertEqual([volume["name"] for volume in wired["volumes"]].count("work"), 1)
        self.assertEqual(named(named(wired["containers"], "runner")["env"], "RUNNER_WAIT_FOR_DOCKER_IN_SECONDS")["value"], "300")

    def test_a_values_file_with_its_own_dind_container_gets_the_mount(self) -> None:
        values = copy.deepcopy(DEFAULT_VALUES)
        values["template"]["spec"]["containers"].append({"name": "dind", "image": "docker:29-dind", "volumeMounts": [{"name": "dind-sock", "mountPath": "/var/run"}]})
        result = wire(values)
        self.assertTrue(result["wired"])
        dind = named(result["values"]["template"]["spec"]["containers"], "dind")
        self.assertEqual(dind["image"], "docker:29-dind")
        self.assertEqual(dind["volumeMounts"][-1]["name"], "registry-cache-hosts")
        self.assertNotIn("initContainers", result["values"]["template"]["spec"])

    def test_conflicts_are_errors(self) -> None:
        cases = {
            "no runner container": ({"containerMode": {"type": "dind"}}, "define the runner container"),
            "a volume of the same name": ({**DIND_VALUES, "template": {"spec": {"containers": [{"name": "runner"}], "volumes": [{"name": "registry-cache-hosts", "emptyDir": {}}]}}}, "volume named registry-cache-hosts"),
            "BUILDX_CONFIG already set": ({**DIND_VALUES, "template": {"spec": {"containers": [{"name": "runner", "env": [{"name": "BUILDX_CONFIG", "value": "/x"}]}]}}}, "already sets BUILDX_CONFIG"),
            "dind mode and a dind container": ({**DIND_VALUES, "template": {"spec": {"containers": [{"name": "runner"}, {"name": "dind"}]}}}, "cannot be combined"),
        }
        for case, (values, message) in cases.items():
            with self.subTest(case):
                result = wire(values)
                self.assertFalse(result["wired"])
                self.assertIs(result["values"], values)
                self.assertTrue(any(message in error for error in result["errors"]), result["errors"])

    def test_the_input_values_are_not_modified(self) -> None:
        values = copy.deepcopy(DIND_VALUES)
        wire(values)
        self.assertEqual(values, DIND_VALUES)


class RenderedValuesTest(unittest.TestCase):
    """The scale-set values the role installs, rendered through its own tasks, with the cache off and on."""

    def render(self, profile: dict[str, Any], **settings: Any) -> dict[str, Any]:
        completed = run_playbook(RENDER_VALUES, {"profile": expanded(profile), "org": {"name": "Example", "image": RUNNER_IMAGE}, "github_runner_arc_capabilities": {"node": {"image": "ghcr.io/example/toolcache-node:22.11.0"}}, **settings})
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        rendered: dict[str, Any] = completed.rendered  # type: ignore[attr-defined]
        return rendered

    def test_off_renders_exactly_what_the_role_rendered_before_the_cache(self) -> None:
        # The role's rendering of these profiles before the registry cache existed, written out in full.
        before = {
            "dind": {"minRunners": 0, "maxRunners": 3, "containerMode": {"type": "dind"}, "template": {"spec": {"containers": [{"name": "runner", "command": ["/home/runner/run.sh"]}], "nodeSelector": {}}}},
            "default": {"minRunners": 0, "maxRunners": 2, "template": {"spec": {"containers": [{"name": "runner", "command": ["/home/runner/run.sh"]}], "nodeSelector": {}}}},
        }
        profiles = {"dind": {"max_runners": 3, "container_mode": "dind"}, "default": {"max_runners": 2}}
        for case, profile in profiles.items():
            for settings in ({}, {"github_runner_arc_registry_cache_enabled": False, "github_runner_arc_registry_cache_registries": REGISTRIES}):
                with self.subTest(case, settings=bool(settings)):
                    rendered = self.render(profile, **settings)
                    self.assertEqual(rendered["values"], before[case])
                    self.assertFalse(rendered["registry_cache_wired"])
                    self.assertEqual(rendered["registry_cache_configmap"], {})

    def test_on_points_a_dind_profile_at_the_cache_after_its_capabilities(self) -> None:
        rendered = self.render({"max_runners": 3, "container_mode": "dind", "capabilities": ["node"]}, github_runner_arc_registry_cache_enabled=True, github_runner_arc_registry_cache_registries=REGISTRIES)
        values = rendered["values"]
        self.assertTrue(rendered["registry_cache_wired"])
        self.assertNotIn("containerMode", values)
        self.assertEqual(values["maxRunners"], 3)
        self.assertEqual([container["name"] for container in values["template"]["spec"]["initContainers"]], ["init-dind-externals", "dind", "capability-node"])
        self.assertEqual(set(rendered["registry_cache_configmap"]), {"docker-hub.hosts.toml", "ghcr.hosts.toml", "internal.hosts.toml", "buildkitd.default.toml"})
        self.assertIn(f'mirrors = ["{ADDRESS}:5001"]', rendered["registry_cache_configmap"]["buildkitd.default.toml"])

    def test_on_leaves_a_profile_without_docker_alone(self) -> None:
        rendered = self.render({"max_runners": 2}, github_runner_arc_registry_cache_enabled=True)
        self.assertFalse(rendered["registry_cache_wired"])
        self.assertEqual(rendered["values"]["template"]["spec"]["containers"], [{"name": "runner", "command": ["/home/runner/run.sh"]}])


class RenderedCacheTest(unittest.TestCase):
    """The cache's own resources, rendered through the role's tasks."""

    def render(self, **settings: Any) -> dict[str, dict[str, Any]]:
        fleet = arc.arc_profiles([{"name": "Example", "image": RUNNER_IMAGE, "scale_set_profiles": [{"max_runners": 1}, {"suffix": "-big", "max_runners": 1}]}], require_app_id=False)
        completed = run_playbook(RENDER_CACHE, {"github_runner_arc_fleet": fleet, "github_runner_arc_registry_cache_enabled": True, "github_runner_arc_registry_cache_registries": REGISTRIES, **settings})
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        resources: list[dict[str, Any]] = completed.rendered  # type: ignore[attr-defined]
        return {resource["kind"]: resource for resource in resources}

    def test_one_container_per_registry_behind_one_service(self) -> None:
        resources = self.render()
        self.assertEqual(set(resources), {"ConfigMap", "PersistentVolumeClaim", "Deployment", "Service", "NetworkPolicy"})
        containers = resources["Deployment"]["spec"]["template"]["spec"]["containers"]
        self.assertEqual([(container["name"], container["ports"][0]["containerPort"]) for container in containers], [("docker-hub", 5000), ("ghcr", 5001), ("internal", 5002)])
        env = {variable["name"]: variable.get("value") for variable in named(containers, "ghcr")["env"]}
        self.assertEqual(env["REGISTRY_PROXY_REMOTEURL"], "https://ghcr.io")
        self.assertEqual(env["REGISTRY_HTTP_ADDR"], ":5001")
        self.assertEqual(env["REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY"], "/var/lib/registry/ghcr")
        self.assertEqual([(port["name"], port["port"]) for port in resources["Service"]["spec"]["ports"]], [("docker-hub", 5000), ("ghcr", 5001), ("internal", 5002)])

    def test_credentials_come_only_from_secret_references(self) -> None:
        containers = self.render()["Deployment"]["spec"]["template"]["spec"]["containers"]
        internal = {variable["name"]: variable for variable in named(containers, "internal")["env"]}
        self.assertEqual(internal["REGISTRY_PROXY_USERNAME"], {"name": "REGISTRY_PROXY_USERNAME", "valueFrom": {"secretKeyRef": {"name": "internal-read", "key": "user"}}})
        self.assertEqual(internal["REGISTRY_PROXY_PASSWORD"], {"name": "REGISTRY_PROXY_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "internal-read", "key": "token"}}})
        self.assertEqual(named(named(containers, "docker-hub")["env"], "REGISTRY_PROXY_PASSWORD")["valueFrom"]["secretKeyRef"], {"name": "hub-read", "key": "password"})
        ghcr = {variable["name"] for variable in named(containers, "ghcr")["env"]}
        self.assertFalse({"REGISTRY_PROXY_USERNAME", "REGISTRY_PROXY_PASSWORD"} & ghcr)

    def test_persistent_storage_is_a_claim_of_the_size_and_class(self) -> None:
        resources = self.render(github_runner_arc_registry_cache_storage_size="50Gi", github_runner_arc_registry_cache_storage_class="fast")
        claim = resources["PersistentVolumeClaim"]["spec"]
        self.assertEqual(claim["resources"]["requests"]["storage"], "50Gi")
        self.assertEqual(claim["storageClassName"], "fast")
        self.assertEqual(named(resources["Deployment"]["spec"]["template"]["spec"]["volumes"], "storage"), {"name": "storage", "persistentVolumeClaim": {"claimName": "registry-cache"}})
        self.assertEqual(resources["Deployment"]["spec"]["strategy"], {"type": "Recreate"})

    def test_an_empty_storage_class_leaves_the_cluster_default(self) -> None:
        self.assertNotIn("storageClassName", self.render()["PersistentVolumeClaim"]["spec"])

    def test_ephemeral_storage_is_a_capped_empty_dir_and_no_claim(self) -> None:
        resources = self.render(github_runner_arc_registry_cache_storage="ephemeral", github_runner_arc_registry_cache_storage_size="8Gi")
        self.assertNotIn("PersistentVolumeClaim", resources)
        self.assertEqual(named(resources["Deployment"]["spec"]["template"]["spec"]["volumes"], "storage"), {"name": "storage", "emptyDir": {"sizeLimit": "8Gi"}})

    def test_only_the_runner_namespaces_may_reach_it(self) -> None:
        policy = self.render()["NetworkPolicy"]["spec"]
        self.assertEqual(policy["ingress"][0]["from"][0]["namespaceSelector"]["matchExpressions"][0]["values"], ["arc-runners-example", "arc-runners-example-big"])
        self.assertEqual([port["port"] for port in policy["ingress"][0]["ports"]], [5000, 5001, 5002])


if __name__ == "__main__":
    unittest.main()
