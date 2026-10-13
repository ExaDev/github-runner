# github_runner_cluster

Runs a k3s cluster under Docker Compose on each host, from the `docker-compose.yml` and k3s image build context the role ships in `files/` and copies to `github_runner_cluster_dir` on the target host: embedded etcd across the server nodes (or SQLite for a single server), and a Tailscale client inside each node's k3s container (k3s `--vpn-auth`) so nodes on different networks can reach one another over a mesh. It templates that directory's `.env` and starts the `server` or `agent` Compose profile. The mesh's control server is pluggable: Tailscale's own, or Headscale in one of three arrangements (see [Mesh providers](#mesh-providers)).

Which hosts are servers is decided automatically from the inventory group named by `github_runner_cluster_group`, in inventory order. The first N hosts are servers, where N is the largest odd number no greater than both the number of hosts and `github_runner_cluster_max_servers`: 1 or 2 hosts give 1 server, 3 or 4 give 3, and 5 or more give 5. The first server bootstraps the cluster, the other servers join it, and the remaining hosts join it as agents. The group is used rather than the play's hosts so that `--limit` never changes the result. The logic lives in `tasks/topology.yml` and the collection's `exadev.github_runner.cluster_topology` filter (`plugins/filter/cluster_topology.py`), and publishes `github_runner_cluster_effective_node_role`, `github_runner_cluster_effective_bootstrap`, `github_runner_cluster_effective_server_url` and `github_runner_cluster_effective_tls_sans` for each host.

## Docker Compose versions

The role reads `docker compose version` first and fails, before changing anything on the host, if it is 2.37.1 or later but older than 2.39.0. Those releases create a container from an image they have just built without recording that image's ID on it ([docker/compose#13047](https://github.com/docker/compose/pull/13047)), so the next run sees a different image and recreates the container, which restarts every node on the run after the one that built the k3s image. Releases before 2.37.1 and from 2.39.0 on keep the containers. GitHub's ubuntu-latest runner image has shipped 2.38.2, so a CI job that runs the role there needs a different Compose installed first, as `.github/workflows/mesh-integration.yml` does.

## Versions

Every node's image is built from two pinned tags: `github_runner_cluster_k3s_version` (a `rancher/k3s` release tag, k3s's version with `+` written as `-`, such as `v1.34.1-k3s1`; release candidates are not accepted) and `github_runner_cluster_tailscale_version` (a `tailscale/tailscale` tag such as `v1.102.4`). The defaults are the versions this collection's integration tests run. The role writes both into the host's `.env`, the Compose file refuses to build without them, and the Dockerfile has no default for either, so no node is ever built from `latest`. Set them once for the whole group, in the inventory or `group_vars`, not per host and not in play vars.

Before it changes anything on a host the role fails:

- if either is not a pinned tag;
- if the hosts of `github_runner_cluster_group` do not all name the same k3s minor version, listing each host's version. Every host is read from the inventory, and one that sets nothing counts as the default, so the check gives the same answer on every host, including under `--limit`;
- if the k3s version would break k3s's upgrade path or Kubernetes' version skew policy from what runs now. k3s cannot be downgraded and upgrades the cluster one minor at a time, servers before agents; the [version skew policy](https://kubernetes.io/releases/version-skew-policy/) ([archived](http://web.archive.org/web/2026/https://kubernetes.io/releases/version-skew-policy/)) keeps the API servers within one minor of each other and never lets a kubelet be newer than an API server, while older kubelets are supported. So the role refuses to build this node older than it runs (by patch release and k3s build too), any node more than one minor above another, a server an older minor than any node runs, and an agent a newer minor than any server runs. Patch releases on other nodes are not compared. The agent rule applies only when the run builds the agent's image (it is missing, as for a new agent): changing the pin builds nothing, so writing a new pin to every host before the servers are rebuilt, as the upgrade below does, passes. The role reads `k3s --version` in this node's container, and the nodes' kubelet versions and roles through every server of the group in the inventory, each on its own host with its pinned Docker context, so one server stopped or being rebuilt does not hide what the others run. Each server's read, its Docker daemon and `docker exec` included, gets at most `github_runner_cluster_cluster_read_timeout` seconds, so one that hangs is cut off and named rather than holding up every host; the docker process it ran may stay on that host until the daemon answers. When servers run but none of them can be read, it refuses, naming each and why. When no server's container runs it compares only this node's own container and says why: a first bootstrap, a group that joins a cluster whose other servers are outside the inventory (as `bootstrap.sh` builds for a joining server), or a group whose inventory holds no server at all (a host `bootstrap.sh` joins as an agent). `-e github_runner_cluster_allow_k3s_version_jump=true` skips this comparison for one run: for a deliberate downgrade, a cluster that is being replaced, or one that cannot be read. Another server's container name and Docker context are read from its inventory (hostvars), so set them there, not in play vars.

A new cluster can start on a newer k3s than the default by setting both variables; the integration tests have run only the defaults.

### Upgrading from a release before the pins

Releases before the pins built every node from `latest`, and this change refuses what they accepted:

- `github_runner_cluster_k3s_version: latest`, or `K3S_VERSION=latest` in `.env.bootstrap`: set the `rancher/k3s` tag the cluster runs instead (`k3s --version` in a node's container, with `+` written as `-`), or leave it unset for the default;
- a cluster that floated to a newer k3s minor than the default: set `github_runner_cluster_k3s_version` to the tag it runs, once for the whole group, before running the role;
- a Compose file path (in `github_runner_cluster_dir` or `github_runner_cluster_compose_files`) with a colon in it: move it, since `.env` now lists the files in `COMPOSE_FILE`.

Set `github_runner_cluster_tailscale_version` too if the nodes run another Tailscale than the default (`tailscale version` in a node's container). The first run after upgrading rewrites each node's `.env` and recreates nothing.

Changing a pin changes no running node. Compose builds the image only when it is missing and leaves the build arguments out of a container's configuration hash, so the next run of the role rewrites `.env` and leaves the container alone. To move the cluster to a new version, run the role once with the new pins, so every host's `.env` names them, then rebuild and restart the nodes by hand, one at a time.

The node's `.env` names the Compose files, as absolute paths, and the project the role runs the node with (`COMPOSE_FILE` and `COMPOSE_PROJECT_NAME`), so a `docker compose` command run by hand in `github_runner_cluster_dir`, or with `--project-directory` pointing at it, acts on the same project with the same files, including the extra file a `headscale_in_cluster` bootstrap server or a `github_runner_cluster_compose_files` entry adds. Each command still needs `--profile server` (or `--profile agent` on an agent), since every service sits in a profile, and, on a host with `github_runner_cluster_docker_context` set, that context: run it as `docker --context <context> compose ...`.

1. Read k3s's release notes for every minor version between the old and the new one. A jump of more than one minor is several upgrades, each run to the end before the next, and the role refuses to build a node more than one minor ahead of the cluster.
2. Servers first, one at a time. Servers run runner pods as well as the control plane, so before each one:
   - check that every server's node is Ready (`kubectl get nodes`) and that each server's own API reports etcd healthy (`kubectl get --raw '/readyz?verbose'` through that server lists `[+]etcd ok`), so the cluster keeps its quorum while this one restarts: with three servers, at most one may be down;
   - cordon it (`kubectl cordon <node>`) and wait until no runner pod is left on it (`kubectl get pods --all-namespaces --field-selector spec.nodeName=<node>`), so no job is cut off, then drain the rest (`kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`).

   Then, in `github_runner_cluster_dir`, run `docker compose --profile server build k3s` and `docker compose --profile server up -d k3s`. Wait until its node is Ready again and the etcd checks above pass on every server, then `kubectl uncordon <node>`, before the next server.
3. Then agents, one at a time, the same way: cordon, wait for its runner pods to finish, drain, `docker compose --profile agent build k3s-agent` and `docker compose --profile agent up -d k3s-agent`, wait for Ready, uncordon.

A Tailscale upgrade on its own follows the same order; it restarts each node's mesh client with the node.

The role copies the new `docker-compose.yml` before it writes `.env`. A run that stops between the two (a failed check, a lost connection) leaves a Compose file that needs `TAILSCALE_VERSION` beside an `.env` without it, and every hand-run `docker compose` command in that directory fails with a missing-variable error until a run of the role succeeds.

## Docker host disk

Each host also runs a `docker-prune` service that removes Docker build cache and dangling images older than `DOCKER_PRUNE_AFTER` (a week by default) once every `DOCKER_PRUNE_INTERVAL_SECONDS` (daily). The node's ephemeral storage lives on the host's Docker disk, shared with anything else that builds there, so an untrimmed cache eventually puts the node under disk pressure and taints it unschedulable. Tagged images, containers and volumes are never touched. Set either variable in the host's `.env` to change them.

## Docker contexts

By default the role drives whichever Docker daemon the host's current Docker context points at (`docker context show`), or the one `DOCKER_HOST` names when that is set. That is right for a host with one Docker engine, but the current context is shared, mutable state that other software changes. The case that matters: a Mac that runs its node in Colima and also has Docker Desktop installed. Launching Docker Desktop switches the current context to `desktop-linux` without asking, so the next run of the role would find no node in Docker Desktop and build a second, empty k3s node there, alongside the real one in Colima, instead of managing it.

Set `github_runner_cluster_docker_context` in the host's variables to pin the context by name (`colima` in that example) on any host with more than one engine. Every `community.docker` module call in the role then passes it as `cli_context`, and every `docker` command the role runs (Compose, `exec`, `inspect`, `volume`, image pulls, and the commands in `playbooks/recover_in_cluster_mesh.yml` and `playbooks/promote_headscale_standby.yml`) runs with `DOCKER_CONTEXT` set to it, so the modules and the CLI always reach the same daemon. The role checks the pin before it touches the host and fails if the context does not exist there, or if `DOCKER_HOST` is also set, since the two would name the daemon in conflicting ways. It never creates a context and never changes the host's current context; `docker context show` on the host reads the same before and after a run. The Headscale tasks that act on another host (the `headscale_hosted` server and standby, and the promotion playbook) use that host's own value. Run `docker context show` on each host to see what it currently points at before choosing a value.

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

## Networking

Every node's k3s uses its mesh address for everything: its node IP, etcd's peer URLs, the API server and pod traffic. Which physical path the mesh takes between two nodes is Tailscale's choice, among the endpoints each node's Tailscale client can offer its peers. On Compose's default bridge network (`github_runner_cluster_network_mode: bridge`, the default) the client sits behind the Docker host's NAT and, inside a Docker VM, behind the VM's too, so it has no address its peers can reach and every pair of nodes is relayed, even on one LAN.

`github_runner_cluster_network_mode: host` runs the node's container on the Docker host's own network instead (`docker-compose.host-network.yml`, merged last). Its client then offers the host's own addresses, so nodes that share a network connect directly and fall back to a relay or a hole-punched path when they do not. The container publishes no port, since its ports are the host's; with `github_runner_cluster_wait_for_nodes` or `github_runner_cluster_record_deployed_version` on, the role checks that the API server answers on the host's loopback before it uses the kubeconfig there. k3s's ports, routes and firewall rules, Tailscale's, and the entrypoint's unreachable route for the mesh ranges then live in the host's namespace, shared with any other container on the host network there. Nothing k3s uses changes, so a node moves between the two modes with a container recreate.

On the host network the node's ports are the Docker host's. The API server (6443) and kubelet (10250) then answer on every address the host has, so on a bridged VM they are reachable from the LAN on the VM's address; etcd stays on loopback and the mesh address. In bridge mode only the API server's port is published, on the Mac's own addresses. Other containers on the same Docker host also gain a route into the mesh through the node's Tailscale routing, so give a node on the host network a VM of its own.

On a Linux machine running Docker without a VM, the host network is the machine's own. The node's Tailscale client would then create `tailscale0`, Tailscale's routing table and rules and its firewall chains beside the machine's own Tailscale client, and a second node on the same Docker host would share all of that and the kubelet's port. Neither works, so the role refuses host mode when the Docker host's network already has a Tailscale device that is not the node's own (as it is once the node runs on the host network), and a node that should go direct on such a machine needs a VM of its own.

Host mode needs the Docker host to have an address on the LAN the nodes share, given as `github_runner_cluster_lan_cidr`: before starting the node, the role reads the host's addresses from a throwaway container on the host network (`github_runner_cluster_network_probe_image`) and fails, changing nothing, when none lies in that network. Whenever the node's running container is on the other kind of network than the one asked for, Compose is about to recreate it, so the role first refuses unless the cluster stays safe without the node: the other servers whose node is Ready and whose own API server reports etcd healthy (`/readyz/etcd`, read through each server's container on its host) must still be a majority of all servers, and no ARC runner pod that has not finished may be on the node (cordon it and let its jobs finish). What cannot be told refuses the move: a cluster that cannot be read, servers the cluster does not label as servers, a server node that is missing, a server the inventory does not hold, or a server that cannot be reached. Only one node may move per run, since each host's checks see the others as healthy before any is recreated. All of this runs before the role changes anything, under `--check` too, and only reads (the address probe pulls its small image if the host lacks it). The check does not see which server leads etcd; the runbook below moves leadership off a node by hand.

On macOS the Docker host is a VM, which needs an address of its own on the LAN, and the role does not create or change VMs. With Colima that means a profile created bridged: a profile's network mode is fixed when it is created and its LAN address cannot be turned off afterwards, so an existing user-mode VM cannot be converted, and a node moves to a new profile instead. Docker Desktop has no bridged VM network.

### Moving a node to a bridged Colima VM

One node at a time. The node keeps its identity because its two volumes move with it: the k3s data (etcd member, certificates, token) and the Tailscale state, so its mesh address, node IP and etcd peer URL stay the same and nothing re-joins. Run every `docker` command below against the engine it names (`docker --context <context> ...`).

Before stopping anything:

1. **Check etcd on every server.** On each server, `docker exec <container> kubectl get --raw '/readyz?verbose'` lists `[+]etcd ok`. k3s ships no `etcdctl`, so read the members' health and the leader with one in a throwaway container that shares a healthy server's network namespace and reads its certificates from the k3s data volume, using an etcd image of the version the k3s release embeds (its release notes name it):

   ```
   etcdctl() {
     docker run --rm --network container:<container> -v <project>_k3s-data:/var/lib/rancher/k3s:ro \
       --entrypoint etcdctl gcr.io/etcd-development/etcd:<etcd version> \
       --cacert /var/lib/rancher/k3s/server/tls/etcd/server-ca.crt \
       --cert /var/lib/rancher/k3s/server/tls/etcd/client.crt \
       --key /var/lib/rancher/k3s/server/tls/etcd/client.key "$@"
   }
   etcdctl --endpoints https://127.0.0.1:2379 endpoint health --cluster
   etcdctl --endpoints https://127.0.0.1:2379 endpoint status --cluster -w table
   ```

   Every member must be healthy and started, and the other servers must stay a majority without this one. Record each member's ID and peer URL for the checks after the move.
2. **Move etcd leadership off the node** if `endpoint status` shows it as the leader. k3s has no command for it ([k3s docs, "Using etcdctl"](https://docs.k3s.io/advanced), [archived](http://web.archive.org/web/2026/https://docs.k3s.io/advanced)); with the function above, run `etcdctl --endpoints https://<this node's mesh address>:2379 move-leader <another member's ID in hex>` and read `endpoint status` again.
3. **Take an etcd snapshot as the last resort,** on another server: `docker exec <container> k3s etcd-snapshot save --name before-network-move`.
4. **Empty the node of jobs.** `kubectl cordon <node>`, then wait until `kubectl get pods --all-namespaces --field-selector spec.nodeName=<node>` lists no runner pod.

The new engine:

5. **Choose the bridged interface.** Pick a wired one where the host has one: the VM then reaches its peers without the host's Wi-Fi. A profile bridged to an adapter that is missing when the VM starts (an undocked laptop's dock adapter) does not start at all: `colima start` fails, and every container in that VM stays down until the adapter is back. Bridging over Wi-Fi works, through macOS's MAC NAT, so the router sees a second lease on the Mac's own Wi-Fi MAC. The interface is not fixed when the profile is created: a stopped profile started again with another `--network-interface` came back on that interface in testing, so a node bridged over Wi-Fi now should later move to a wired adapter with a stop and a start of its profile, not another profile and volume move. That has been seen with a test profile, not yet with a node on it.
6. **Give the new profile a home of its own whenever another profile already runs in the default home** (`~/.colima`): VMs in one home share its user-mode network daemon. Create the `COLIMA_HOME` directory before running any `colima` command with it, since Colima falls back silently to `~/.colima` when it does not exist.
7. **Approve Colima's network helper once.** The first start of a profile with an address installs `socket_vmnet` and a sudoers entry for it, which an administrator approves once per host.
8. **Stop the node and keep its image and node password.** Run `docker update --restart=no` and `docker stop` on the node's container, so nothing starts the old copy again. Then `docker save` the node's image under the name its container uses, so the new engine runs the same build rather than building one, and `docker cp <container>:/etc/rancher/node/password` to a file (never print it): the node password lives in the container's own layer, and carrying it avoids a `NodePasswordValidationFailed` event when the node registers again.
9. **Export both volumes with the container stopped,** so the etcd data is consistent. Use a throwaway container that tars each volume to a file. The image's `agent/containerd` directory can be left out of the k3s data; kubelet pulls its images again.
10. **Create the bridged profile**, sized for what the node runs: Colima's default VM is far smaller than a server that also runs runner pods needs. For example `colima start --profile <name> --vm-type vz --cpus <n> --memory <GiB> --disk <GiB> --network-address --network-mode bridged --network-interface <interface> --activate=false`. `--activate=false` leaves the host's current Docker context alone; without it Colima switches every other Docker client on the host to the new VM. Check that `colima status --profile <name> --json` reports an `ip_address` on the LAN. On a memory-tight host, quit the old Docker engine first.
11. **Import onto the new engine.** `docker load` the image, create the two volumes as Compose would, with its labels (`docker volume create --label com.docker.compose.project=<project> --label com.docker.compose.volume=<volume> <project>_<volume>`, for `k3s-data` and `k3s-tailscale-data`), and untar each export into its volume.

Move the engine first and the network after, one change at a time, so that the second change goes through the role's guard:

12. **Create the node on the new engine, still on the bridge network,** and give it its password: in `github_runner_cluster_dir`, whose `.env` the last run of the role wrote for the bridge network, run `docker --context <new context> compose --profile server create --no-build k3s` and `docker --context <new context> cp <password file> <container>:/etc/rancher/node/password`.
13. **Start it through the role.** Point the host's `github_runner_cluster_docker_context` at the new profile's context, leave `github_runner_cluster_network_mode` as it is, and run the role for this host alone (`--limit <host>`, after a `--check --diff` run). It starts the node from the loaded image, building nothing. This is the first start on the new engine, after which the old copy must never run again (see the rollback limits).
14. **Check the node kept its identity.** It is Ready with the same InternalIP; `endpoint status` from step 1 lists the same member IDs and peer URLs, every member healthy; `tailscale status --self` in the node's container shows the same device and mesh address, and no new device has joined the tailnet; the node's log has no `membershipError`; no `NodePasswordValidationFailed` event names it (`kubectl get events --all-namespaces --field-selector involvedObject.name=<node>`); and `kubeconfig/kubeconfig.yaml` in `github_runner_cluster_dir` has been rewritten since the start, which shows the bind mounts reach the host through the new VM's mount of the host's directories.
15. **Move it to the host network through the role.** Check etcd again and move leadership off the node if it leads (steps 1 and 2), set `github_runner_cluster_network_mode: host` and `github_runner_cluster_lan_cidr`, and run the role for this host alone again. The node's container is now running on the bridge network, so the role's guard reads the cluster before Compose recreates it on the host network, and refuses while the cluster needs the node. Do not start or recreate it by hand: until the role has rewritten `.env`, `COMPOSE_FILE` names only the bridge network's files.
16. **Check it again,** as in step 14, and that `tailscale status` shows `direct` paths to every peer that is bridged too. A node on a user-mode VM never gets a direct path to a bridged one, because macOS Local Network privacy drops the user-mode VM's traffic to other Macs, so the benefit arrives once two nodes are bridged.
17. **Uncordon the node.**
18. **Make the new profile start with the host, and the old one not.** `brew services start colima` runs `colima start -f` for the default profile in the default home only ([Colima FAQ](https://github.com/abiosoft/colima/blob/v0.10.3/docs/FAQ.md), [archived](http://web.archive.org/web/2026/https://github.com/abiosoft/colima/blob/v0.10.3/docs/FAQ.md), and Homebrew's service definition for the formula). A profile with another name or home needs a launchd agent of its own that runs `colima start --profile <name> -f` with its `COLIMA_HOME`, and whatever started the old engine must no longer start the old copy of the node. The collection manages neither.

**Rollback limits.** Starting the old container again on the old engine is safe only until the node's first start on the new one. After that the moved etcd member has appended to its log and voted, and the old copy would come back behind what the cluster believes it stored. Roll back by exporting the new volumes back the same way, or by removing the node (`kubectl delete node`, which removes its etcd member) and joining it fresh. Never run both copies at once: they are the same mesh node and the same etcd member.

Tailscale settings a node needs go on the `tailscaled` command line in `k3s/node-entrypoint.sh` or through `--vpn-auth`'s `extraArgs`: k3s runs `tailscale up --reset` on every start, which drops any other preference.

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
- `github_runner_cluster_k3s_version`, `github_runner_cluster_tailscale_version`: the pinned image tags every node is built from, `github_runner_cluster_allow_k3s_version_jump` (default false) to skip the comparison with what runs, and `github_runner_cluster_cluster_read_timeout` (default 30, a positive whole number of seconds) for each server's read (see [Versions](#versions)). `github_runner_cluster_k3s_tested_version` is the k3s default, which the minor-version check compares hosts that set nothing against; set `github_runner_cluster_k3s_version` instead.
- `github_runner_cluster_node_name`: the k3s node name and mesh hostname, defaulting to the inventory name.
- `github_runner_cluster_node_taints`: Kubernetes taints (`key=value:Effect`, no spaces) the host's node registers with, empty by default. `NoSchedule` keeps pods without a matching toleration off it and `NoExecute` also evicts the ones already there. k3s applies them on every registration, so they come back if the node object is deleted, which a taint set with `kubectl` does not.
- Per-host overrides: `github_runner_cluster_node_role` (`server` or `agent`), `github_runner_cluster_bootstrap` (true picks the bootstrap host, false excludes a host), `github_runner_cluster_server_url` (join this server instead, for a cluster whose servers are outside the inventory), `github_runner_cluster_tls_sans`, `github_runner_cluster_node_address`. The run fails if the result has an even number of servers, no servers, or more than one bootstrap host.
- `github_runner_cluster_docker_context` (default empty): pins the Docker context this host's node runs in (see [Docker contexts](#docker-contexts)).
- `github_runner_cluster_network_mode` (default `bridge`, or `host`): the network the node's container runs on, and with `host`, `github_runner_cluster_lan_cidr` (required: the LAN the nodes share) and `github_runner_cluster_network_probe_image` (default `busybox:1.37`, for reading the host's addresses) (see [Networking](#networking)).
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

`tests/mesh/run.sh` brings up three k3s nodes in Docker on one machine and joins them through Headscale with this role, for the `headscale_hosted` and `headscale_in_cluster` providers, and checks the `headscale_existing` policy check against a real server. Each cluster scenario runs the role a second time and fails if any node container restarted. The `compose_faulty` scenario checks the role refuses a Compose release that has that fault. With `GRTEST_PIN_CONTEXT=1` the cluster scenarios pin `github_runner_cluster_docker_context` while the current Docker context the playbooks see points at a socket that does not exist, so any docker call that ignored the pin fails, and they check the current context is unchanged afterwards; `tests/litestream/run.sh` takes the same setting for the Headscale hosts it delegates to and the promotion playbook. The `context_checks` scenario checks the role refuses a pinned context that does not exist, and one set alongside `DOCKER_HOST`, before writing anything. The `host_network` scenario (Linux only) gives each node a Docker daemon of its own at a fixed address on the test network, standing in for a VM with an address on the LAN, runs the role with `github_runner_cluster_network_mode: host`, and checks that every node's Tailscale client reaches every other directly at that address and that no port is published. `tests/unit/test_cluster_network.py` checks the rendered Compose project in both modes and the role's list of Compose files; `tests/unit/test_cluster_guard.py` checks the address and quorum filters, and `tests/unit/test_host_network_role.py` runs the role's `tasks/host_network.yml` against stand-in `docker` and `kubectl` commands. `.github/workflows/mesh-integration.yml` runs them on pull requests that touch the role, the hosted scenario on 2.39.0, the first fixed release, as well as on a current one.
