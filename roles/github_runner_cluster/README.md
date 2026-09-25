# github_runner_cluster

Runs a k3s cluster under Docker Compose on each host, from the `docker-compose.yml` in a checkout of this repository on the target host: embedded etcd across the server nodes (or SQLite for a single server), and a Tailscale client inside each node's k3s container (k3s `--vpn-auth`) so nodes on different networks can reach one another over a mesh. It templates the checkout's `.env` and starts the `server` or `agent` Compose profile. The mesh's control server is pluggable: Tailscale's own, or Headscale in one of three arrangements (see [Mesh providers](#mesh-providers)).

Which hosts are servers is decided automatically from the inventory group named by `github_runner_cluster_group`, in inventory order. The first N hosts are servers, where N is the largest odd number no greater than both the number of hosts and `github_runner_cluster_max_servers`: 1 or 2 hosts give 1 server, 3 or 4 give 3, and 5 or more give 5. The first server bootstraps the cluster, the other servers join it, and the remaining hosts join it as agents. The group is used rather than the play's hosts so that `--limit` never changes the result. The logic lives in `tasks/topology.yml` and the collection's `exadev.github_runner.cluster_topology` filter (`plugins/filter/cluster_topology.py`), and publishes `github_runner_cluster_effective_node_role`, `github_runner_cluster_effective_bootstrap`, `github_runner_cluster_effective_server_url` and `github_runner_cluster_effective_tls_sans` for each host.

## Mesh providers

`github_runner_cluster_mesh` picks the provider. Each one lives in `tasks/mesh/<provider>.yml` and publishes the same facts, so nothing else in the role depends on which is in use:

- `github_runner_cluster_mesh_join_key`: a reusable, pre-authorised join key that tags each node with `github_runner_cluster_mesh_tag`.
- `github_runner_cluster_mesh_control_server_url`: the `--vpn-auth` `controlServerURL`, empty for Tailscale's own control plane.
- `github_runner_cluster_mesh_vpn_auth`: the resulting `--vpn-auth` value (`name=tailscale,joinKey=<key>[,controlServerURL=<url>]`), which `k3s/node-entrypoint.sh` builds the same way from `.env`.
- `github_runner_cluster_mesh_policy`: the tailnet policy as the provider knows it.

`tasks/mesh/main.yml` includes the provider, then runs the checks every provider shares: `tasks/mesh/check_policy.yml` fails, printing the exact JSON to merge in, unless the policy defines the node tag in `tagOwners` and auto-approves `github_runner_cluster_pod_cidr` for it in `autoApprovers.routes` (each node advertises its slice of the pod CIDR as a subnet route for flannel, and an unapproved route leaves pod traffic between nodes silently going nowhere). The role never writes a policy it does not own.

| Provider | Control server | Policy | Join key |
| --- | --- | --- | --- |
| `tailscale` (default) | Tailscale's | With a Tailscale API token, the role adds the tag, route approval and pod grant to the tailnet ACL, leaving every other entry alone; without one, it leaves the ACL alone | Supplied, or created through the API when it will be kept (see [Join keys](#join-keys)) |
| `headscale_existing` | A Headscale server someone else runs | Read through the REST API and checked, never written | Supplied, or created through the API when missing, expired or within `github_runner_cluster_headscale_join_key_renew_days` of expiry, when it will be kept (see [Join keys](#join-keys)) |
| `headscale_hosted` | Headscale under Docker Compose on one host outside k3s | Role-owned (`github_runner_cluster_headscale_policy`) | Created through the API and kept on the Headscale host |
| `headscale_in_cluster` | Headscale as a static pod on the bootstrap server | Role-owned | Created through the API and kept on the bootstrap server |

For every Headscale provider, `github_runner_cluster_tailnet_domain` is Headscale's MagicDNS base domain, since node addresses are `<node name>.<domain>`; it must not be, or be a parent of, the Headscale URL's own host name. The Headscale API is reached from the control node at `github_runner_cluster_headscale_api_url`, or the URL itself.

### headscale_existing

Needs `github_runner_cluster_headscale_url` and an API key (`headscale apikeys create --expiration 90d`) in `github_runner_cluster_headscale_api_key`. If the server's policy lacks the tag or the route approval, the run stops before creating anything and prints what to add.

### Join keys

Neither Tailscale nor Headscale lets a join key be read back after it is created, so the `tailscale` and `headscale_existing` providers create one only when it will be kept for the next run. The new key is set as the fact `github_runner_cluster_new_join_key` (`{mesh: tailscale or headscale, key: ...}`) and kept in either or both of two ways:

- `github_runner_cluster_join_key_path`: a file on the control node. The role writes a new key there (mode 0600) and reads the key from it whenever `github_runner_cluster_tailscale_join_key` or `github_runner_cluster_headscale_join_key` is empty.
- `github_runner_cluster_join_key_listener: true`: a secrets adapter in the play keeps it. The role notifies `github_runner_cluster join key created` and flushes handlers at once, so the key is stored even if a later task fails. The 1Password adapter does this and writes the key to its item.

With neither set, the role creates nothing, and a run with no join key stops asking for one to be supplied or for somewhere to keep a new one.

### headscale_hosted

Runs `headscale/headscale` at `github_runner_cluster_headscale_version` (pinned) under Compose on `github_runner_cluster_headscale_host`, by default the bootstrap server but any inventory host, including one outside the cluster group. Its files live in `github_runner_cluster_headscale_dir` on that host: `config/`, the Compose project, and `secrets/` (mode 0700) holding the API key it creates with Headscale's own CLI and the join key. `github_runner_cluster_headscale_tls: letsencrypt` (the default) has Headscale obtain its certificate itself over TLS-ALPN-01 on port 443; `none` serves plain HTTP on `github_runner_cluster_headscale_port`, for a TLS-terminating proxy or a private network. `github_runner_cluster_headscale_derp: embedded` adds Headscale's own DERP relay (STUN on UDP 3478) to Tailscale's public DERP map.

### headscale_in_cluster

Runs Headscale inside the cluster it serves, as a static pod with host networking on the bootstrap server, published on that host's port. A static pod rather than a Deployment behind a LoadBalancer Service, because kubelet runs a static pod with no API server and no pod network, both of which here depend on the mesh. The bootstrap server's `k3s/node-entrypoint.sh` supervises k3s rather than exec'ing it: every time the container starts, k3s starts off the mesh (no `--vpn-auth`, and on SQLite until the cluster first reaches the mesh), kubelet starts Headscale, and once Headscale answers the script restarts only the k3s process on the mesh, with `--cluster-init` converting the datastore to etcd with the mesh address as etcd's peer address. Pods, Headscale included, keep running across that restart. On the first run the join key does not exist yet, so the role starts the bootstrap server, waits for Headscale, creates the keys, and only then starts the other nodes.

The limitation: the control server lives on one node. While the bootstrap server is down, nodes already on the mesh keep talking to each other, but nothing can join or re-register. A single-server cluster recovers from any restart on its own. With several servers, starting off the mesh leaves the bootstrap server's etcd member unable to reach the others, so it may never run the static pod and never rejoin; `playbooks/recover_in_cluster_mesh.yml` handles that by restarting the container, and if Headscale still does not answer, running a temporary Headscale container on the same configuration and state in the k3s container's network until the node is back on the mesh. The bootstrap server stays reachable on its direct address throughout (its k3s API port and Headscale's port are both published on the host). Prefer `headscale_hosted` on a separate machine when that matters.

## Multiple sites

Each site is its own cluster: its own inventory group, bootstrap server and mesh provider, run by its own play with `github_runner_cluster_group` set to that group (see `examples/two-site/`). One cluster spanning two meshes is not supported: flannel's tailscale backend carries pod traffic over the single mesh every node belongs to, so every node of a cluster must be on the same tailnet or Headscale server. A host belongs to at most one cluster group.

## Variables

- `github_runner_cluster_repo_root` (required): the checkout's absolute path on the target host.
- `github_runner_cluster_k3s_token` (required), `github_runner_cluster_tailscale_join_key`, `github_runner_cluster_tailscale_api_token`, `github_runner_cluster_headscale_api_key`, `github_runner_cluster_headscale_join_key`: secrets, as plain variables from any source Ansible reads.
- `github_runner_cluster_join_key_path`, `github_runner_cluster_join_key_listener`: where a join key the role creates is kept (see [Join keys](#join-keys)).
- `github_runner_cluster_group` (default `github_runner_cluster`), `github_runner_cluster_max_servers` (default 5), `github_runner_cluster_api_port` (default 6443), `github_runner_cluster_datastore` (`etcd`, the default, or `sqlite`, which makes the cluster a single server with every other host an agent).
- `github_runner_cluster_mesh` (default `tailscale`), `github_runner_cluster_mesh_tag` (default `tag:github-runner-node`), `github_runner_cluster_pod_cidr` (default k3s's `10.42.0.0/16`).
- `github_runner_cluster_tailnet_domain`: the mesh's MagicDNS domain. Each node's address is `<node name>.<domain>`, and join URLs and TLS SANs use it.
- `github_runner_cluster_headscale_*`: the Headscale providers' settings, documented in `defaults/main.yml`.
- `github_runner_cluster_node_name`: the k3s node name and mesh hostname, defaulting to the inventory name.
- Per-host overrides: `github_runner_cluster_node_role` (`server` or `agent`), `github_runner_cluster_bootstrap` (true picks the bootstrap host, false excludes a host), `github_runner_cluster_server_url` (join this server instead, for a cluster whose servers are outside the inventory), `github_runner_cluster_tls_sans`, `github_runner_cluster_node_address`. The run fails if the result has an even number of servers, no servers, or more than one bootstrap host.
- `github_runner_cluster_compose_files`: extra Compose files for the host's k3s project, merged after `docker-compose.yml`.
- `github_runner_cluster_wait_for_nodes`: wait for every node to be Ready afterwards, clearing stale node-password registrations if a node fails to rejoin (installs the kubernetes Python client through `github_runner_k8s_client`).

## Examples

`examples/` in this repository has inventories for a single host on SQLite, three servers on Tailscale, each Headscale provider, and two sites. The smallest:

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
    - exadev.github_runner.github_runner_cluster
```

with the secrets set from any source, for example:

```yaml
# group_vars/github_runner_cluster.yml
github_runner_cluster_k3s_token: "{{ vault_k3s_token }}"
github_runner_cluster_tailscale_join_key: "{{ lookup('ansible.builtin.env', 'TAILSCALE_JOIN_KEY') }}"
```

or read from 1Password by adding `exadev.github_runner.github_runner_secrets_onepassword` (with `github_runner_secrets_onepassword_consumers: [cluster]`) before it.

## Testing

`tests/mesh/run.sh` brings up three k3s nodes in Docker on one machine and joins them through Headscale with this role, for the `headscale_hosted` and `headscale_in_cluster` providers, and checks the `headscale_existing` policy check against a real server; `.github/workflows/mesh-integration.yml` runs it on pull requests that touch the role.
