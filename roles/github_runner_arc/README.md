# github_runner_arc

Installs GitHub's [Actions Runner Controller](https://github.com/actions/actions-runner-controller) into the k3s cluster that `github_runner_cluster` brought up: the controller, one or more orgs' runner scale sets (one Helm release and namespace per scale-set profile), and the fleet-health platform (heartbeat and autoscaler Deployments). Each part runs only on a host whose own `github_runner_arc_orgs` or `github_runner_arc_heartbeat_gist_id` is set, so any server host can be the installing host; an agent host installs nothing.

If no host in the inventory has a heartbeat gist id, the role creates the gist with the control node's `gh` session and stops, printing the id to record.

## Variables

- `github_runner_arc_orgs`: a list of orgs to install from this host. Each entry has `name`, `app_id`, `private_key_op_reference` (for the secrets role), optionally `installation_id` (resolved from the App when empty), `image`, and `scale_set_profiles`, a list of `{suffix, values_file, node_selector?, runs_on_label?}`.
- `github_runner_arc_heartbeat_gist_id`: install the fleet-health platform from this host, refreshing this gist.
- `github_runner_arc_ghcr_username`, `github_runner_arc_ghcr_token`, `github_runner_arc_heartbeat_gh_token`, `github_runner_arc_org_private_keys`: secrets, normally set by `github_runner_secrets`.
- `github_runner_arc_values_dir`, `github_runner_arc_kubeconfig_path`: the Helm values directory and kubeconfig on the target host, defaulting to paths under `github_runner_cluster_repo_root`.
- `github_runner_arc_autoscaler_*`, `github_runner_arc_heartbeat_*`, `github_runner_arc_platform_namespace`: tunables for the fleet-health platform; see `defaults/main.yml`.

## Example

```yaml
# host_vars/<host>.yml
github_runner_arc_heartbeat_gist_id: "<gist id>"
github_runner_arc_orgs:
  - name: ExampleOrg
    app_id: 123456
    private_key_op_reference: "op://Vault/example-app-private-key/private key"
    image: "ghcr.io/example/github-runner:latest"
    scale_set_profiles:
      - suffix: ""
        values_file: "exadev-runners-values.yaml"
```
