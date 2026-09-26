# github_runner_arc

Installs GitHub's [Actions Runner Controller](https://github.com/actions/actions-runner-controller) into a Kubernetes cluster: the controller, one or more orgs' runner scale sets (one Helm release and namespace per scale-set profile), and an optional fleet-health platform (a heartbeat Deployment that refreshes a gist while the cluster is healthy, and a usage-driven autoscaler). Each part runs only on a host whose own `github_runner_arc_orgs` or `github_runner_arc_heartbeat_gist_id` is set.

It runs in two ways. Inside `playbooks/site.yml`, against a k3s server host that `github_runner_cluster` brought up, using the kubeconfig that role writes; an agent host installs nothing. Or on its own through `playbooks/arc.yml`, from the control machine against any cluster a kubeconfig reaches. Helm values are rendered on the control node either way, so the target needs no checkout.

## Validation

`tasks/validate.yml` checks every input that needs neither secrets nor a cluster: each org's fields, profile names (derived or overridden) that would collide or make invalid Kubernetes names, two of an org's profiles sharing a release name, a `max_runners` that is not a whole number, the controller's release name and namespace, the same org configured on two hosts, more than one autoscaled profile, values files that do not exist, the autoscaler's pool settings, sizing inputs, and the controller, probe and node label settings. Both playbooks run it in a play of its own before anything changes; the role runs it itself when included some other way. It also bootstraps the heartbeat gist when `github_runner_arc_heartbeat_bootstrap_gist` is on and no host in the inventory has one: it creates the gist with the control node's `gh` session and stops, printing the id to record.

Before touching the cluster the role then checks the secrets it is about to write (each org's App key looks like a PEM key, the pull credential and heartbeat token are set) and proves the pull credential can read every image it installs from `github_runner_arc_image_pull_registry`, by getting a pull-scoped registry token and fetching each image's manifest. With an App-sourced pull Secret (see below) it mints each org's token and proves it the same way, before installing anything.

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
  - `sizing`: derive a runner ceiling from node capacity (see Sizing). On an ordinary profile without `max_runners` it sets `maxRunners`; on the autoscaled profile it sets the autoscaler's ceiling, leaving `maxRunners` as the floor.
- `github_runner_arc_values_dir`: the control-node directory relative `values_file` paths are read from.
- `github_runner_arc_kubeconfig_path`: the kubeconfig on the host the role runs against. Defaults to `github_runner_cluster`'s kubeconfig; empty means `KUBECONFIG` or `~/.kube/config`.
- `github_runner_arc_controller_chart_version`, `github_runner_arc_scaleset_chart_version`: chart pins; unset installs the latest chart.
- `github_runner_arc_controller_release_name`, `github_runner_arc_controller_namespace`: the controller's Helm release and namespace, default `arc` in `actions-runner-controller`. The stale-listener check, the heartbeat's RBAC and the heartbeat's controller check use them too.
- `github_runner_arc_controller_replicas`: above 1, the chart turns on leader election and the role adds a preferred pod anti-affinity across nodes. `github_runner_arc_controller_extra_values` is merged over the role's controller values.
- `github_runner_arc_metrics_enabled` (and the `_addr`/`_endpoint` settings): Prometheus metrics for the controller and every listener; the 0.14 chart only enables them together.
- `github_runner_arc_listener_probes_enabled`: readiness and liveness probes on each listener's metrics endpoint, so a listener that starts and then fails its GitHub authentication is not counted ready during a rollout. Needs metrics on.
- `github_runner_arc_node_label_key`, `github_runner_arc_node_label_value`: when the key is set, the controller, listeners and runner pods are restricted to nodes carrying the label. The role labels the nodes named in `github_runner_arc_labelled_nodes`.
- `github_runner_arc_ghcr_username`, `github_runner_arc_ghcr_token`, `github_runner_arc_heartbeat_gh_token`: secrets, as plain variables. The pull credential is needed only for a static pull Secret.
- `github_runner_arc_manage_secrets`: `false` stops the role writing the App Secrets and `heartbeat-gh-token`, and instead checks before any change that they already exist. The role then needs no `private_key` or `installation_id`.
- `github_runner_arc_manage_image_pull_secret`: `false` stops the role writing the pull Secret, so an existing one named `github_runner_arc_image_pull_secret_name` is used as it is, kept current by something else. No pull credential is needed and the pull check is skipped; the role checks the Secret exists. Follows `github_runner_arc_manage_secrets` unless set.
- `github_runner_arc_image_pull_secret_source`: where a pull Secret the role writes gets its credential: `static` (the default) from `github_runner_arc_ghcr_username` and `github_runner_arc_ghcr_token`, or `app` from each org's GitHub App, renewed in the cluster (see [Image pull Secret from the GitHub App](#image-pull-secret-from-the-github-app)). `github_runner_arc_image_pull_secret_renewal_schedule` (default every 15 minutes) and `github_runner_arc_image_pull_secret_renewer_image` configure the renewal.
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

## Image pull Secret from the GitHub App

When the runner image is a private package owned by the org, `github_runner_arc_image_pull_secret_source: app` pulls it with the runners' own GitHub App rather than a registry token tied to a person. The App needs the `packages: read` permission (add it to `github_runner_arc_app_setup_permissions` when creating the App, or to an existing App's settings), approved by the organisation on the App's installation.

For each org the role signs an App JWT with the App's private key (the org's `private_key`, or the existing App Secret when `github_runner_arc_manage_secrets` is `false`), mints an installation token restricted to `packages: read`, and proves it can read the org's image with the registry's own token exchange and a manifest read, all before changing anything. It then writes the token into each of the org's namespaces as the pull Secret, with `x-access-token` as the username and the token's expiry in the `github-runner.exadev/pull-token-expires-at` annotation.

An installation token lasts an hour, so the role also installs a CronJob in each of those namespaces, named after the pull Secret with `-renewer` appended, that repeats the mint and the check on `github_runner_arc_image_pull_secret_renewal_schedule` and patches the Secret only when both succeed; a refused run leaves the current Secret in place and fails the Job. It reads the App Secret from a mounted volume, and its Role allows only `get` and `patch` on the pull Secret by name. Its image, `github_runner_arc_image_pull_secret_renewer_image`, is pulled without a pull Secret, so an expired token never stops the job that replaces it, and the image must be public. With `static` the role removes any such CronJob and its RBAC.

The fleet-health platform belongs to no one org, so with `app` its Deployments pull without a pull Secret; the role's default platform images are public.

```yaml
github_runner_arc_image_pull_secret_source: app
github_runner_arc_orgs:
  - name: ExampleOrg
    app_id: 123456
    private_key: "{{ vault_example_app_private_key }}"
    image: "ghcr.io/exampleorg/github-runner:1.0.0"
    scale_set_profiles:
      - max_runners: 4
```

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
