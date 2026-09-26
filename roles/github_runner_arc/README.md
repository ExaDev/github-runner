# github_runner_arc

Installs GitHub's [Actions Runner Controller](https://github.com/actions/actions-runner-controller) into a Kubernetes cluster: the controller, one or more orgs' runner scale sets (one Helm release and namespace per scale-set profile), and an optional fleet-health platform (a heartbeat Deployment that refreshes a gist while the cluster is healthy, and a usage-driven autoscaler). Each part runs only on a host whose own `github_runner_arc_orgs` or `github_runner_arc_heartbeat_gist_id` is set.

It runs in two ways. Inside `playbooks/site.yml`, against a k3s server host that `github_runner_cluster` brought up, using the kubeconfig that role writes; an agent host installs nothing. Or on its own through `playbooks/arc.yml`, from the control machine against any cluster a kubeconfig reaches. Helm values are rendered on the control node either way, so the target needs no checkout.

## Validation

`tasks/validate.yml` checks every input that needs neither secrets nor a cluster (a measured sizing's node figures are read later, when the role installs the profile): each org's fields, profile names (derived or overridden) that would collide or make invalid Kubernetes names, two of an org's profiles sharing a release name, a `max_runners` that is not a whole number, the controller's release name and namespace, the same org configured on two hosts, more than one autoscaled profile, values files that do not exist, the autoscaler's pool settings, sizing inputs, and the controller, probe and node label settings. Both playbooks run it in a play of its own before anything changes; the role runs it itself when included some other way. It also bootstraps the heartbeat gist when `github_runner_arc_heartbeat_bootstrap_gist` is on and no host in the inventory has one: it creates the gist with the control node's `gh` session and stops, printing the id to record.

Before touching the cluster the role then checks the secrets it is about to write (each org's App key looks like a PEM key, the pull credential and heartbeat token are set) and proves the pull credential can read every image it installs from `github_runner_arc_image_pull_registry`, by getting a pull-scoped registry token and fetching each image's manifest.

## Variables

- `github_runner_arc_orgs`: the orgs this host installs. Each entry has `name`, `app_id`, `image`, `private_key` (the App's PEM private key, from any source Ansible reads) or `private_key_op_reference` (an `op://` reference that the `github_runner_secrets_onepassword` adapter resolves into `private_key`), optionally `installation_id` (resolved from the App when empty), optionally `app_secret_name` (the App Secret's name in each of the org's namespaces, default `<org>-github-app`), and `scale_set_profiles`, a list of profiles:
  - `suffix`: appended to the namespace (`arc-runners-<org><suffix>`) and release (`<org>-runners<suffix>`) names. Default empty.
  - `namespace`, `release_name`: the profile's namespace and Helm release name, in place of the names derived from the suffix. The release name is also the scale set's name on GitHub, so two profiles of one org cannot share it.
  - `values_file`: Helm values for the release, a Jinja template read from `github_runner_arc_values_dir` on the control node (or an absolute path). Without it the role's `templates/runner-scale-set-values.yaml.j2` is used, driven by the profile's own `max_runners`, `min_runners`, `container_mode` (`dind` for the chart's Docker-in-Docker mode) and `resources` (the runner container's requests and limits).
  - `node_selector`: a nodeSelector for the runner pods, merged over the eligibility label below.
  - `max_runners`: the release's `maxRunners`. It takes precedence over the values file and over `sizing`; on the autoscaled profile it is the floor the autoscaler starts from.
  - `runs_on_label`: the extra `scaleSetLabels` entry the runners carry. Default `<org>-runners`, which pools every profile of the org under one `runs-on` label; set a different one to keep a profile out of that pool.
  - `scale_set_labels`: the whole `scaleSetLabels` list, in place of `runs_on_label`. An empty list sets no `scaleSetLabels`, so jobs target the scale set by its release name.
  - `autoscale`: `true` on at most one profile in the whole inventory makes it the scale set the autoscaler manages.
  - `sizing`: derive a runner ceiling from node capacity, given as uniform figures or measured from each eligible node (see Sizing). On an ordinary profile without `max_runners` it sets `maxRunners`; on the autoscaled profile it sets the autoscaler's ceiling, leaving `maxRunners` as the floor.
- `github_runner_arc_values_dir`: the control-node directory relative `values_file` paths are read from.
- `github_runner_arc_kubeconfig_path`: the kubeconfig on the host the role runs against. Defaults to `github_runner_cluster`'s kubeconfig; empty means `KUBECONFIG` or `~/.kube/config`.
- `github_runner_arc_controller_chart_version`, `github_runner_arc_scaleset_chart_version`: chart pins; unset installs the latest chart.
- `github_runner_arc_controller_release_name`, `github_runner_arc_controller_namespace`: the controller's Helm release and namespace, default `arc` in `actions-runner-controller`. The stale-listener check, the heartbeat's RBAC and the heartbeat's controller check use them too.
- `github_runner_arc_controller_replicas`: above 1, the chart turns on leader election and the role adds a preferred pod anti-affinity across nodes. `github_runner_arc_controller_extra_values` is merged over the role's controller values.
- `github_runner_arc_metrics_enabled` (and the `_addr`/`_endpoint` settings): Prometheus metrics for the controller and every listener; the 0.14 chart only enables them together.
- `github_runner_arc_listener_probes_enabled`: readiness and liveness probes on each listener's metrics endpoint, so a listener that starts and then fails its GitHub authentication is not counted ready during a rollout. Needs metrics on.
- `github_runner_arc_node_label_key`, `github_runner_arc_node_label_value`: when the key is set, the controller, listeners and runner pods are restricted to nodes carrying the label. The role labels the nodes named in `github_runner_arc_labelled_nodes`.
- `github_runner_arc_ghcr_username`, `github_runner_arc_ghcr_token`, `github_runner_arc_heartbeat_gh_token`: secrets, as plain variables.
- `github_runner_arc_manage_secrets`: `false` stops the role writing the App Secrets and `heartbeat-gh-token`, and instead checks before any change that they already exist. The role then needs no `private_key` or `installation_id`.
- `github_runner_arc_manage_image_pull_secret`: `false` stops the role writing the pull Secret, so an existing one named `github_runner_arc_image_pull_secret_name` is used as it is, for example one that something else mints from a GitHub App installation token. No pull credential is needed and the pull check is skipped; the role checks the Secret exists. Follows `github_runner_arc_manage_secrets` unless set.
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

That assumes the nodes are alike. Where their free capacity differs a lot, `measured: true` works it out from the cluster instead, each time the role runs. It reads every node the runner pods may be placed on: every node carrying the eligibility label and the profile's `node_selector`, with no `NoSchedule` or `NoExecute` taint other than those Kubernetes adds for a passing condition. The ceiling lasts until the next run, so a node that is not ready, unreachable or cordoned when it is measured still counts, as it will once it is back. For each one it takes the node's allocatable CPU and memory, subtracts what the pods already on it request and the per-node reserve, and divides what is left by one runner pod's requests; the tighter of the two gives the runners that fit there. The sum over the nodes, scaled by the safety margin and rounded down, is the ceiling. Pods in any scale set's namespace are left out, so running runners never lower the ceiling they are sized by, as are pods that have finished. The reserve covers what requests do not show, such as a workload that uses more than it requests. The kubeconfig needs cluster-wide read access to nodes and pods. The role prints each node's figures when it runs.

```yaml
sizing:
  measured: true
  pod_cpu_request: 1
  pod_memory_request_gib: 2
  sidecar_cpu_request: 0.25         # optional, default 0
  sidecar_memory_request_gib: 0.5   # optional, default 0
  reserve_cpu: 0.5                  # optional, per node, default 0
  reserve_memory_gib: 1             # optional, per node, default 0
  safety_margin: 0.8                # optional, default 1
```

A profile's own `max_runners` still takes precedence, and on the autoscaled profile the measured figure is the autoscaler's ceiling. Measured sizing uses the `exadev.github_runner.arc_measured_max_runners` filter.

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
github_runner_arc_values_dir: "{{ inventory_dir }}/values"
github_runner_arc_orgs:
  - name: ExampleOrg
    app_id: 123456
    # The App's PEM key, from ansible-vault here; any lookup works too. With the 1Password adapter, private_key_op_reference: "op://Vault/example-app-private-key/private key" instead.
    private_key: "{{ vault_example_app_private_key }}"
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

## Managing an existing install

A cluster whose controller and scale set were installed under other names can be managed by setting those names, rather than getting a second controller and scale set beside them. The role then upgrades the existing releases in place.

```yaml
github_runner_arc_controller_release_name: "existing-controller"
github_runner_arc_controller_namespace: "existing-controller-ns"
# The App Secret and the pull Secret already exist and are kept current elsewhere.
github_runner_arc_manage_secrets: false
github_runner_arc_manage_image_pull_secret: false
github_runner_arc_image_pull_secret_name: "existing-pull-secret"
github_runner_arc_orgs:
  - name: ExampleOrg
    app_id: 123456
    app_secret_name: "existing-app-secret"
    image: "ghcr.io/example/github-runner:2026.01.01"
    scale_set_profiles:
      - namespace: "existing-runners-ns"
        release_name: "existing-runners"
        values_file: "existing-runners-values.yaml"
        max_runners: 6
        # Jobs target the release name, so no scaleSetLabels.
        scale_set_labels: []
```
