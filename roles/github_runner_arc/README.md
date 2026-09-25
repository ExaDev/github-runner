# github_runner_arc

Installs GitHub's [Actions Runner Controller](https://github.com/actions/actions-runner-controller) into a Kubernetes cluster: the controller, one or more orgs' runner scale sets (one Helm release and namespace per scale-set profile), and an optional fleet-health platform (a heartbeat Deployment that refreshes a gist while the cluster is healthy, and a usage-driven autoscaler). Each part runs only on a host whose own `github_runner_arc_orgs` or `github_runner_arc_heartbeat_gist_id` is set.

It runs in two ways. Inside `playbooks/site.yml`, against a k3s server host that `github_runner_cluster` brought up, using the kubeconfig that role writes; an agent host installs nothing. Or on its own through `playbooks/arc.yml`, from the control machine against any cluster a kubeconfig reaches. Helm values are rendered on the control node either way, so the target needs no checkout.

## Validation

`tasks/validate.yml` checks every input that needs neither secrets nor a cluster: each org's fields, profile names that would collide or make invalid Kubernetes names, the same org configured on two hosts, more than one autoscaled profile, values files that do not exist, the autoscaler's pool settings, sizing inputs, and the controller, probe and node label settings. Both playbooks run it in a play of its own before anything changes; the role runs it itself when included some other way. It also bootstraps the heartbeat gist when `github_runner_arc_heartbeat_bootstrap_gist` is on and no host in the inventory has one: it creates the gist with the control node's `gh` session and stops, printing the id to record.

Before touching the cluster the role then checks the secrets it is about to write (each org's App key looks like a PEM key, the pull credential and heartbeat token are set) and proves the pull credential can read every image it installs from `github_runner_arc_image_pull_registry`, by getting a pull-scoped registry token and fetching each image's manifest.

## Variables

- `github_runner_arc_orgs`: the orgs this host installs. Each entry has `name`, `app_id`, `image`, `private_key` (the App's PEM private key, from any source Ansible reads) or `private_key_op_reference` (an `op://` reference that the 1Password secrets role resolves into `private_key`), optionally `installation_id` (resolved from the App when empty), and `scale_set_profiles`, a list of profiles:
  - `suffix`: appended to the namespace (`arc-runners-<org><suffix>`) and release (`<org>-runners<suffix>`) names. Default empty.
  - `values_file`: Helm values for the release, a Jinja template read from `github_runner_arc_values_dir` on the control node (or an absolute path). Without it the role's `templates/runner-scale-set-values.yaml.j2` is used, driven by the profile's own `max_runners`, `min_runners`, `container_mode` (`dind` for the chart's Docker-in-Docker mode) and `resources` (the runner container's requests and limits).
  - `node_selector`: a nodeSelector for the runner pods, merged over the eligibility label below.
  - `runs_on_label`: the extra `scaleSetLabels` entry the runners carry. Default `<org>-runners`, which pools every profile of the org under one `runs-on` label; set a different one to keep a profile out of that pool.
  - `autoscale`: `true` on at most one profile in the whole inventory makes it the scale set the autoscaler manages.
  - `sizing`: derive a runner ceiling from node capacity (see Sizing). On an ordinary profile it sets `maxRunners`; on the autoscaled profile it sets the autoscaler's ceiling, leaving `maxRunners` as the floor.
- `github_runner_arc_values_dir`: the control-node directory relative `values_file` paths are read from.
- `github_runner_arc_kubeconfig_path`: the kubeconfig on the host the role runs against. Defaults to `github_runner_cluster`'s kubeconfig; empty means `KUBECONFIG` or `~/.kube/config`.
- `github_runner_arc_controller_chart_version`, `github_runner_arc_scaleset_chart_version`: chart pins; unset installs the latest chart.
- `github_runner_arc_controller_replicas`: above 1, the chart turns on leader election and the role adds a preferred pod anti-affinity across nodes. `github_runner_arc_controller_extra_values` is merged over the role's controller values.
- `github_runner_arc_metrics_enabled` (and the `_addr`/`_endpoint` settings): Prometheus metrics for the controller and every listener; the 0.14 chart only enables them together.
- `github_runner_arc_listener_probes_enabled`: readiness and liveness probes on each listener's metrics endpoint, so a listener that starts and then fails its GitHub authentication is not counted ready during a rollout. Needs metrics on.
- `github_runner_arc_node_label_key`, `github_runner_arc_node_label_value`: when the key is set, the controller, listeners and runner pods are restricted to nodes carrying the label. The role labels the nodes named in `github_runner_arc_labelled_nodes`.
- `github_runner_arc_ghcr_username`, `github_runner_arc_ghcr_token`, `github_runner_arc_heartbeat_gh_token`: secrets, as plain variables.
- `github_runner_arc_manage_secrets`: `false` stops the role writing the App and pull Secrets, and instead checks before any change that each namespace already holds them.
- `github_runner_arc_image_pull_registry`, `github_runner_arc_image_pull_secret_name`, `github_runner_arc_verify_image_pull`: the pull Secret's registry and name, and whether to prove the credential before writing it.
- `github_runner_arc_heartbeat_gist_id`: install the fleet-health platform from this host, refreshing this gist. `github_runner_arc_heartbeat_bootstrap_gist`, `github_runner_arc_heartbeat_gist_description` and `github_runner_arc_heartbeat_gist_consumer` control the bootstrap described above.
- `github_runner_arc_autoscaler_usable_budget_gi`, `github_runner_arc_autoscaler_max_ceiling`, `github_runner_arc_autoscaler_floor`: required when a profile sets `autoscale: true` and the platform is installed. The floor must equal the autoscaled profile's `maxRunners`, which every Helm upgrade reverts to. The other `github_runner_arc_autoscaler_*` and `github_runner_arc_heartbeat_*` settings have defaults; see `defaults/main.yml`.
- `github_runner_arc_app_setup_*`, `github_runner_arc_app_manifest_code`, `github_runner_arc_app_private_key_path`: inputs to `playbooks/github_app_setup.yml` (see below).

## Sizing

A profile's `sizing` works out how many runner pods a pool of identical nodes holds: each node's allocatable CPU and memory, less a baseline fraction reserved for what already runs there, divided by one pod's requests (the runner plus any sidecar, such as dind); the tighter of the two gives pods per node, times the node count, scaled by the safety margin and rounded down.

```yaml
sizing:
  node_allocatable_cpu: 8
  node_allocatable_memory_gib: 16
  node_count: 3
  pod_cpu_request: 1
  pod_memory_request_gib: 2
  sidecar_cpu_request: 0.25         # optional, default 0
  sidecar_memory_request_gib: 0.5   # optional, default 0
  baseline_cpu_fraction: 0.1        # optional, default 0
  baseline_memory_fraction: 0.25    # optional, default 0
  safety_margin: 0.8                # optional, default 1
```

The filter behind it is `exadev.github_runner.arc_max_runners`.

## GitHub App setup

`playbooks/github_app_setup.yml` creates the App the scale sets authenticate as, through GitHub's manifest flow. The first run renders a local form that posts the manifest (organisation self-hosted runner write access, no webhook, private) to GitHub; an organisation owner submits it and copies the one-time code GitHub returns. The second run, with that code and a path for the key, exchanges the code, writes the private key there, waits while the App is installed on the organisation, and prints the `app_id` and `installation_id` to record.

```sh
ansible-playbook playbooks/github_app_setup.yml -e github_runner_arc_app_setup_org=example-org -e '{"github_runner_arc_app_setup_name": "Example runners"}'
ansible-playbook playbooks/github_app_setup.yml -e github_runner_arc_app_setup_org=example-org -e '{"github_runner_arc_app_setup_name": "Example runners"}' \
  -e github_runner_arc_app_manifest_code=<code> -e github_runner_arc_app_private_key_path=~/example-app.pem
```

## Example

```yaml
# host_vars/<host>.yml, or a vars file for playbooks/arc.yml
github_runner_arc_values_dir: "{{ playbook_dir }}/../values"
github_runner_arc_orgs:
  - name: ExampleOrg
    app_id: 123456
    private_key_op_reference: "op://Vault/example-app-private-key/private key"
    image: "ghcr.io/example/github-runner:latest"
    scale_set_profiles:
      - suffix: ""
        max_runners: 4
        resources:
          requests: {cpu: "2", memory: 4Gi}
          limits: {cpu: "2", memory: 4Gi}
      - suffix: "-docker"
        values_file: "example-docker-values.yaml"
        runs_on_label: "example-docker"
```
