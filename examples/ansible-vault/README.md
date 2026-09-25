# ansible-vault secrets

Create `group_vars/all/vault.yml` next to `inventory.yml` with `ansible-vault create group_vars/all/vault.yml` and put the values the inventory refers to in it:

```yaml
vault_k3s_token: <openssl rand -hex 32>
vault_tailscale_join_key: tskey-auth-...
vault_ghcr_username: <GitHub user>
vault_ghcr_token: <token with read:packages>
vault_heartbeat_gh_token: <token with gist scope>
vault_example_org_app_private_key: |
  -----BEGIN RSA PRIVATE KEY-----
  ...
  -----END RSA PRIVATE KEY-----
```

A single value can also be encrypted in place with `ansible-vault encrypt_string`.
