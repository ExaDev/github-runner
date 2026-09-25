# github_runner_secrets_onepassword

An optional adapter that reads the `github_runner_cluster` and `github_runner_arc` roles' secrets from one 1Password item on the control node and sets them as those roles' own plain variables. Neither role needs it: they read plain variables, which can come from any source Ansible reads (ansible-vault, SOPS, an env or file lookup, HashiCorp Vault, AWS SSM). `playbooks/site.yml` and `playbooks/arc.yml` add this role only when `github_runner_secrets_source` is `onepassword`; `none`, the default, adds nothing.

It reads the item with a single `op item get` (plus one `op read` per org's GitHub App private key) and caches the result on the control node at `~/.cache/github-runner-arc/secrets-<item>.json`, so repeated runs need no further approval prompts; delete the file to force a fresh read. A later play that needs an org key the cache does not hold yet reads just that key and adds it.

It resolves only the fields of the roles named in `github_runner_secrets_onepassword_consumers`:

- `cluster`: `github_runner_cluster_k3s_token`, `github_runner_cluster_tailscale_join_key`, `github_runner_cluster_tailscale_api_token`, `github_runner_cluster_headscale_api_key` and `github_runner_cluster_headscale_join_key` (an empty or missing Headscale field leaves a value set elsewhere in place), and `github_runner_cluster_join_key_listener`, so that a join key the cluster role creates is written back to the item.
- `arc`: `github_runner_arc_ghcr_username`, `github_runner_arc_ghcr_token`, `github_runner_arc_heartbeat_gh_token`, and, for each org in `github_runner_arc_orgs` with a `private_key_op_reference`, the key itself in the org's `private_key` (the reference is removed).

## Variables

- `github_runner_secrets_onepassword_vault`, `github_runner_secrets_onepassword_item`: the 1Password vault and item. The item carries the fields `k3s-token`, `tailscale-join-key` and `tailscale-api-token` for the cluster role, `ghcr-username`, `ghcr-token` and `heartbeat-gh-token` for the ARC role, and optionally `headscale-api-key` and `headscale-join-key` for the cluster role's Headscale mesh providers. A field the play's roles do not use may be left out; the adapter stops with a clear message if the cluster role's `k3s-token` is missing, and the roles' own checks name any other secret they need. Each org in `github_runner_arc_orgs` names its App private key with `private_key_op_reference`.
- `github_runner_secrets_onepassword_consumers`: the roles in the play, `cluster` and/or `arc`. The playbooks set it; the default is both.
- `github_runner_secrets_onepassword_cache_path`: where the read is cached on the control node.

The role's handler listens for the cluster role's `github_runner_cluster join key created` notification and runs `tasks/persist_join_key.yml`, which writes the new Tailscale or Headscale join key to the item's `tailscale-join-key` or `headscale-join-key` field and to the cache.

## Example

```yaml
# inventory group or host vars
github_runner_secrets_source: onepassword
github_runner_secrets_onepassword_vault: Vault
github_runner_secrets_onepassword_item: example-github-runner
```

To use the role in a playbook of your own, add it before the roles it serves:

```yaml
- hosts: github_runner_cluster
  roles:
    - role: exadev.github_runner.github_runner_secrets_onepassword
      vars:
        github_runner_secrets_onepassword_consumers: [cluster]
    - exadev.github_runner.github_runner_cluster
```
