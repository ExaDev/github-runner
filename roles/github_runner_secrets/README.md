# github_runner_secrets

Resolves the secrets the `github_runner_cluster` and `github_runner_arc` roles need and hands each role only its own fields as facts. It reads them either from one 1Password item on the control node, through a single `op item get` (plus one `op read` per org's GitHub App private key), or from values supplied directly. A 1Password read is cached on the control node at `~/.cache/github-runner-arc/secrets-<item>.json` so repeated runs need no further approval prompts; delete the file to force a fresh read.

It sets `github_runner_cluster_k3s_token`, `github_runner_cluster_tailscale_join_key`, `github_runner_cluster_tailscale_api_token`, `github_runner_cluster_headscale_api_key`, `github_runner_cluster_headscale_join_key` and `github_runner_cluster_join_key_listener` for the cluster role, and `github_runner_arc_ghcr_username`, `github_runner_arc_ghcr_token`, `github_runner_arc_heartbeat_gh_token` and each org's `private_key` (replacing its `private_key_op_reference`) for the ARC role. Either role works without this one if those variables are set some other way.

## Variables

- `github_runner_secrets_vault`, `github_runner_secrets_item`: the 1Password vault and item. The item carries the fields `k3s-token`, `ghcr-username`, `ghcr-token`, `heartbeat-gh-token`, `tailscale-join-key` and `tailscale-api-token`, and optionally `headscale-api-key` and `headscale-join-key` for the cluster role's Headscale mesh providers (an empty or missing Headscale field leaves any value set elsewhere in place); each org in `github_runner_arc_orgs` names its App private key with `private_key_op_reference`.
- `github_runner_secrets_provided`: the alternative to 1Password, a dict with the same keys (`k3s_token`, `ghcr_username`, `ghcr_token`, `heartbeat_gh_token`, `tailscale_join_key`, `tailscale_api_token`, `headscale_api_key`, `headscale_join_key`, the Headscale ones optional; each org's App key goes in its own `private_key`). Non-empty means 1Password is not used at all.
- `github_runner_secrets_cache_path`: where the 1Password read is cached on the control node.

In 1Password mode the role's handler, which listens for the cluster role's `github_runner_cluster join key created` notification, runs `tasks/persist_join_key.yml` to write a newly created Tailscale or Headscale join key back to the item's `tailscale-join-key` or `headscale-join-key` field and to the cache.

## Example

```yaml
# host_vars/<host>.yml
github_runner_secrets_vault: Vault
github_runner_secrets_item: example-github-runner
```
