# github_runner_cluster

Runs a k3s cluster under Docker Compose on each host, from the `docker-compose.yml` in a checkout of this repository on the target host: embedded etcd across the server nodes, and Tailscale inside each node's k3s container (k3s `--vpn-auth`) so nodes on different networks can reach one another. It templates the checkout's `.env` and starts the `server` or `agent` Compose profile.

Which hosts are servers is decided automatically from the inventory group named by `github_runner_cluster_group`, in inventory order. The first N hosts are servers, where N is the largest odd number no greater than both the number of hosts and `github_runner_cluster_max_servers`: 1 or 2 hosts give 1 server, 3 or 4 give 3, and 5 or more give 5. The first server bootstraps the cluster, the other servers join it, and the remaining hosts join it as agents. The group is used rather than the play's hosts so that `--limit` never changes the result. The logic lives in `tasks/topology.yml` and the collection's `exadev.github_runner.cluster_topology` filter (`plugins/filter/cluster_topology.py`), and publishes `github_runner_cluster_effective_node_role`, `github_runner_cluster_effective_bootstrap`, `github_runner_cluster_effective_server_url` and `github_runner_cluster_effective_tls_sans` for each host.

## Variables

- `github_runner_cluster_repo_root` (required): the checkout's absolute path on the target host.
- `github_runner_cluster_k3s_token` (required), `github_runner_cluster_tailscale_join_key`, `github_runner_cluster_tailscale_api_token`: secrets, normally set by `github_runner_secrets`.
- `github_runner_cluster_group` (default `github_runner_cluster`), `github_runner_cluster_max_servers` (default 5), `github_runner_cluster_api_port` (default 6443).
- `github_runner_cluster_tailnet_domain`: the tailnet's MagicDNS domain. Each node's address is `<node name>.<domain>`, and join URLs and TLS SANs use it.
- `github_runner_cluster_node_name`: the k3s node name and Tailscale hostname, defaulting to the inventory name.
- Per-host overrides: `github_runner_cluster_node_role` (`server` or `agent`), `github_runner_cluster_bootstrap` (true picks the bootstrap host, false excludes a host), `github_runner_cluster_server_url` (join this server instead, for a cluster whose servers are outside the inventory), `github_runner_cluster_tls_sans`, `github_runner_cluster_node_address`. The run fails if the result has an even number of servers, no servers, or more than one bootstrap host.
- `github_runner_cluster_wait_for_nodes`: wait for every node to be Ready afterwards, clearing stale node-password registrations if a node fails to rejoin (installs the kubernetes Python client through `github_runner_k8s_client`).

## Example

```yaml
# inventory.yml
all:
  children:
    github_runner_cluster:
      vars:
        github_runner_cluster_tailnet_domain: example.ts.net
      hosts:
        node1:   # bootstraps the cluster
        node2:   # server
        node3:   # server
        node4:   # agent
```

```yaml
- hosts: github_runner_cluster
  roles:
    - exadev.github_runner.github_runner_secrets
    - exadev.github_runner.github_runner_cluster
```
