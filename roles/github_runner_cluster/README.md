# github_runner_cluster

Runs a k3s cluster under Docker Compose on each host, from the `docker-compose.yml` in a checkout of this repository on the target host: embedded etcd across the server nodes, and Tailscale inside each node's k3s container (k3s `--vpn-auth`) so nodes on different networks can reach one another. It templates the checkout's `.env` and starts the `server` or `agent` Compose profile.

## Variables

- `github_runner_cluster_repo_root` (required): the checkout's absolute path on the target host.
- `github_runner_cluster_k3s_token` (required), `github_runner_cluster_tailscale_join_key`, `github_runner_cluster_tailscale_api_token`: secrets, normally set by `github_runner_secrets`.
- `github_runner_cluster_node_role` (`server` or `agent`), `github_runner_cluster_bootstrap`, `github_runner_cluster_join_server_url`, `github_runner_cluster_agent_server_url`, `github_runner_cluster_tls_sans`: how this host joins the cluster.
- `github_runner_cluster_node_name`: the k3s node name and Tailscale hostname, defaulting to the inventory name.
- `github_runner_cluster_wait_for_nodes`: wait for every node to be Ready afterwards (installs the kubernetes Python client through `github_runner_k8s_client`).

## Example

```yaml
- hosts: all
  roles:
    - exadev.github_runner.github_runner_secrets
    - exadev.github_runner.github_runner_cluster
```
