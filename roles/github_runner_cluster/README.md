# github_runner_cluster

Runs a k3s cluster under Docker Compose on each host, from the `docker-compose.yml` and k3s image build context the role ships in `files/` and copies to `github_runner_cluster_dir` on the target host: embedded etcd across the server nodes (or SQLite for a single server), and a Tailscale client inside each node's k3s container (k3s `--vpn-auth`) so nodes on different networks can reach one another over a mesh. It templates that directory's `.env` and starts the `server` or `agent` Compose profile. The mesh's control server is pluggable: Tailscale's own, or Headscale in one of three arrangements (see [Mesh providers](#mesh-providers)).

Which hosts are servers is decided automatically from the inventory group named by `github_runner_cluster_group`, in inventory order. The first N hosts are servers, where N is the largest odd number no greater than both the number of hosts and `github_runner_cluster_max_servers`: 1 or 2 hosts give 1 server, 3 or 4 give 3, and 5 or more give 5. The first server bootstraps the cluster, the other servers join it, and the remaining hosts join it as agents. The group is used rather than the play's hosts so that `--limit` never changes the result. The logic lives in `tasks/topology.yml` and the collection's `exadev.github_runner.cluster_topology` filter (`plugins/filter/cluster_topology.py`), and publishes `github_runner_cluster_effective_node_role`, `github_runner_cluster_effective_bootstrap`, `github_runner_cluster_effective_server_url` and `github_runner_cluster_effective_tls_sans` for each host.

## Docker Compose versions

The role reads `docker compose version` first and fails, before changing anything on the host, if it is 2.37.1 or later but older than 2.39.0. Those releases create a container from an image they have just built without recording that image's ID on it ([docker/compose#13047](https://github.com/docker/compose/pull/13047)), so the next run sees a different image and recreates the container, which restarts every node on the run after the one that built the k3s image. Releases before 2.37.1 and from 2.39.0 on keep the containers. GitHub's ubuntu-latest runner image has shipped 2.38.2, so a CI job that runs the role there needs a different Compose installed first, as `.github/workflows/mesh-integration.yml` does.

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

#### Backups and a warm standby

Headscale has no high availability of its own: one server, on SQLite. While it is down, tunnels that already exist keep working, but nothing can join, no key can be created, and route or endpoint changes stop reaching nodes. Setting `github_runner_cluster_headscale_litestream_replica` adds [Litestream](https://litestream.io) to the Compose project, pinned at `github_runner_cluster_headscale_litestream_version`. A `litestream-restore` step runs before Headscale on every start and restores the database from the replica when the host has none, then a `litestream` sidecar streams every change to the replica. The replica is Litestream's own replica block, so anything Litestream supports works: S3 or an S3-compatible store, SFTP to another host, or a file path on mounted storage (`github_runner_cluster_headscale_litestream_mounts`). Credentials go in `github_runner_cluster_headscale_litestream_env` and extra files such as an SFTP key in `github_runner_cluster_headscale_litestream_files`. Both are written to `litestream/` on the Headscale host with mode 0600, never into the Compose file. With Litestream on, Headscale's `wal_autocheckpoint` is 0, as Litestream recommends, so Litestream does the checkpointing itself.

The database does not hold Headscale's Noise private key, which identifies the server to every node. The role keeps a copy in `keys/` beside the configuration and the restore step puts it in place, so a server restored on the same host, or promoted from the standby, is the same server to its nodes. To rebuild on a new host with no standby, supply the key as `github_runner_cluster_headscale_noise_private_key` from wherever you keep secrets. A run fails, rather than changing the key, if it does not match the running server's.

`github_runner_cluster_headscale_standby_host` adds a warm standby on another inventory host. It gets the same configuration, keys, API key and join key, and a Compose project in which only `litestream-follow` runs, keeping a separate copy of the database up to date from the replica (`litestream restore -f`). Headscale, its restore step and its sidecar sit in the `active` profile and stay stopped. `playbooks/promote_headscale_standby.yml` promotes it. It stops Headscale and then its sidecar on the old server, so the sidecar copies the last writes as it stops. If the old server cannot be reached it refuses to go on unless `-e github_runner_cluster_headscale_promote_without_stopping=true` says that server is down, since two servers writing to one replica would each overwrite the other's history. It then restores the replica's latest state on the standby, replacing the follower's copy, and fails rather than start an empty server if the replica has nothing. Finally it starts Headscale and its sidecar there and prints the DNS change to make and the inventory change for the next `site.yml` run (`github_runner_cluster_headscale_host` becomes the standby). It changes neither. A host that was down when it was promoted away from restarts its containers when it comes back, because they were running when it went down, so stop its project (`docker compose stop` in its Headscale directory) before anything else.

What to expect:

| Setup | Data loss (RPO) | Recovery time (RTO) |
| --- | --- | --- |
| No Litestream | Everything since the last copy you made yourself; all of it if the host is lost | Rebuilding the host, then every node registering again |
| Litestream, no standby | Up to Litestream's sync interval (1 second by default), plus anything written while the replica was unreachable | Provisioning a host and running `site.yml`: the restore step brings the database back before Headscale starts |
| Litestream and a warm standby | The same | Running the promotion playbook (seconds to a minute, dominated by the restore), plus the DNS record's TTL, plus each node's next control reconnection attempt |

Nodes find the promoted server only through DNS, so keep the Headscale name's TTL short (60 seconds or so) where you use a standby. They reconnect by themselves once the name resolves to it. `tests/litestream/run.sh` measures the whole failover against a real Tailscale client. That client pins itself to HTTPS on port 443 after its first failed control connection, so a server behind `github_runner_cluster_headscale_tls: none` needs a TLS-terminating proxy on port 443 for its nodes to come back after an outage. Plain HTTP on another port is not enough.

### headscale_in_cluster

Runs Headscale inside the cluster it serves, as a static pod with host networking on the bootstrap server, published on that host's port. A static pod rather than a Deployment behind a LoadBalancer Service, because kubelet runs a static pod with no API server and no pod network, both of which here depend on the mesh. The bootstrap server's `k3s/node-entrypoint.sh` supervises k3s rather than exec'ing it: every time the container starts, k3s starts off the mesh (no `--vpn-auth`, and on SQLite until the cluster first reaches the mesh), kubelet starts Headscale, and once Headscale answers the script restarts only the k3s process on the mesh, with `--cluster-init` converting the datastore to etcd with the mesh address as etcd's peer address. Pods, Headscale included, keep running across that restart. On the first run the join key does not exist yet, so the role starts the bootstrap server, waits for Headscale, creates the keys, and only then starts the other nodes.

The limitation: the control server lives on one node. While the bootstrap server is down, nodes already on the mesh keep talking to each other, but nothing can join or re-register. A single-server cluster recovers from any restart on its own. With several servers, starting off the mesh leaves the bootstrap server's etcd member unable to reach the others, so it may never run the static pod and never rejoin; `playbooks/recover_in_cluster_mesh.yml` handles that by restarting the container, and if Headscale still does not answer, running a temporary Headscale container, from the image the role keeps in the host's own Docker for this, on the same configuration and state in the k3s container's network until the node is back on the mesh. The bootstrap server stays reachable on its direct address throughout (its k3s API port and Headscale's port are both published on the host). Prefer `headscale_hosted` on a separate machine when that matters.

Headscale's database lives on the bootstrap server's k3s data volume, and a static pod cannot use a replicated persistent volume instead, since kubelet runs it with no API server to bind a claim. So the provider requires Litestream: set `github_runner_cluster_headscale_litestream_replica` (see [Backups and a warm standby](#backups-and-a-warm-standby) for the settings), and the pod gets a `litestream-restore` init container and a `litestream` sidecar. A rebuilt bootstrap server then restores the database before Headscale starts, with the same data loss as `headscale_hosted`. The Noise key copy in `keys/` sits on the host, outside the k3s volume. It survives losing that volume but not the host, so supply `github_runner_cluster_headscale_noise_private_key` too if the host itself may be replaced. To run without Litestream, set `github_runner_cluster_headscale_accept_node_local_state: true`, and accept that losing that volume loses every registration: every node must then join again with a new key. There is no standby for this provider; recovery is rebuilding the bootstrap server.

## Multiple sites

Each site is its own cluster: its own inventory group, bootstrap server and mesh provider, run by its own play with `github_runner_cluster_group` set to that group (see `examples/two-site/`). One cluster spanning two meshes is not supported: flannel's tailscale backend carries pod traffic over the single mesh every node belongs to, so every node of a cluster must be on the same tailnet or Headscale server. A host belongs to at most one cluster group.

## Variables

- `github_runner_cluster_dir` (default `~/.github-runner` on the target host): the directory the role manages as the host's Compose project. It copies the collection's `docker-compose.yml` and `k3s/` build context there, templates `.env` there, and k3s writes the kubeconfig to its `kubeconfig/` subdirectory, so the host needs no checkout of this repository.
- `github_runner_cluster_compose_project` (default `github-runner`): the Compose project name. Compose prefixes the node's named volumes with it, and those volumes hold the k3s datastore and the node's mesh identity, so changing it on a running host brings up a new, empty node. Because it is pinned rather than taken from the directory's name, `github_runner_cluster_dir` can move without losing the cluster.
- `github_runner_cluster_k3s_token` (required), `github_runner_cluster_tailscale_join_key`, `github_runner_cluster_tailscale_api_token`, `github_runner_cluster_headscale_api_key`, `github_runner_cluster_headscale_join_key`: secrets, as plain variables from any source Ansible reads.
- `github_runner_cluster_join_key_path`, `github_runner_cluster_join_key_listener`: where a join key the role creates is kept (see [Join keys](#join-keys)).
- `github_runner_cluster_group` (default `github_runner_cluster`), `github_runner_cluster_max_servers` (default 5), `github_runner_cluster_api_port` (default 6443), `github_runner_cluster_datastore` (`etcd`, the default, or `sqlite`, which makes the cluster a single server with every other host an agent).
- `github_runner_cluster_mesh` (default `tailscale`), `github_runner_cluster_mesh_tag` (default `tag:github-runner-node`), `github_runner_cluster_pod_cidr` (default k3s's `10.42.0.0/16`).
- `github_runner_cluster_tailnet_domain`: the mesh's MagicDNS domain. Each node's address is `<node name>.<domain>`, and join URLs and TLS SANs use it.
- `github_runner_cluster_headscale_*`: the Headscale providers' settings, documented in `defaults/main.yml`.
- `github_runner_cluster_node_name`: the k3s node name and mesh hostname, defaulting to the inventory name.
- Per-host overrides: `github_runner_cluster_node_role` (`server` or `agent`), `github_runner_cluster_bootstrap` (true picks the bootstrap host, false excludes a host), `github_runner_cluster_server_url` (join this server instead, for a cluster whose servers are outside the inventory), `github_runner_cluster_tls_sans`, `github_runner_cluster_node_address`. The run fails if the result has an even number of servers, no servers, or more than one bootstrap host.
- `github_runner_cluster_compose_files`: extra Compose files for the host's k3s project, merged after `docker-compose.yml`, as paths relative to `github_runner_cluster_dir` or absolute. Several nodes on one Docker host each need their own `github_runner_cluster_dir` and `github_runner_cluster_compose_project`.
- `github_runner_cluster_wait_for_nodes`: wait for every node to be Ready afterwards, clearing stale node-password registrations if a node fails to rejoin (installs the kubernetes Python client through `github_runner_k8s_client`).

## Examples

`examples/` in this repository has inventories for a single host on SQLite, three servers on Tailscale, each Headscale provider, and two sites, and for secrets from ansible-vault (`examples/ansible-vault/`), environment and file lookups (`examples/env/`) or 1Password (`examples/onepassword/`). The smallest:

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

`tests/litestream/run.sh` runs the `headscale_hosted` provider with Litestream and a warm standby against an S3-compatible store on this machine's Docker, registers a real Tailscale client, promotes the standby and checks that users, nodes and pre-auth keys survive, that the client reconnects and that the promoted server keeps replicating. It also checks that `headscale_in_cluster` refuses to run without Litestream. `.github/workflows/litestream.yml` runs it on pull requests that touch the Headscale providers.

`tests/mesh/run.sh` brings up three k3s nodes in Docker on one machine and joins them through Headscale with this role, for the `headscale_hosted` and `headscale_in_cluster` providers, and checks the `headscale_existing` policy check against a real server. Each cluster scenario runs the role a second time and fails if any node container restarted. The `compose_faulty` scenario checks the role refuses a Compose release that has that fault. `.github/workflows/mesh-integration.yml` runs them on pull requests that touch the role, the hosted scenario on 2.39.0, the first fixed release, as well as on a current one.
