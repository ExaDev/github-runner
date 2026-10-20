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

## Node passwords and recreating servers

k3s keeps a node's password in `/etc/rancher/node/password`, and the cluster holds a Secret for it (`kube-system/<node>.node-password.k3s`). A container with a fresh filesystem generates a new password, the cluster rejects it ("Node password rejected" in the node's log) and the node stays `NotReady`. Compose recreates a container whenever its rendered configuration differs from the one it was made with, so every node service mounts a named volume at `/etc/rancher/node` (`<project>_k3s-node` on a server, `<project>_k3s-agent-node` on an agent), and the password outlives the container.

### Cutover for an existing node

A node made before the volume existed keeps its identity if the volume is seeded from its running container before Compose recreates it with the volume mounted, and the role does that on its own:

- Before the mesh step and every Compose task, the role looks for the node's volume. When it does not exist and the node's container does (running or stopped), the role creates the volume with the labels Compose gives its own volumes and copies `/etc/rancher/node` from the container into it. The copy is one `docker cp` piped into another, into a throwaway container made from the node's own image and never started, so the password is on no disk of the host, in no command line and in no output, and the volume keeps its mode and owner. A failed copy removes the volume again and fails the run, so the next run tries again. A host with no container yet, or with the volume already, is left alone, and a second run changes nothing. With `--check` the role only reports that it would seed.
- The Compose file now mounts the volume, which changes the configuration hash of the node's service, so the run that follows recreates each node's container once, with the password in place and the node rejoining as itself. That restarts k3s and the pods on that node: on a cluster whose jobs must not be cut off, run the first converge host by host with `--limit`, draining each node first as in [Versions](#versions). Servers are recreated one at a time whatever the play (see below). Agents are not held to that, so limit a cutover run to a few agents at a time if the cluster cannot lose them together.
- Under `--check` the cutover run reports the seeding. The recreates it will cause are not visible yet, since `--check` writes nothing and the check reads the files already on the host: they show under `--check` once the Compose file that mounts the volume has been copied to the host.

The volume is named from the Compose project, so changing `github_runner_cluster_compose_project` on a running host starts a new, empty volume as it does a new, empty node.

### Recreating servers one at a time

Compose recreates a container when the hash it stored on it (the `com.docker.compose.config-hash` label) differs from the hash of the configuration it renders now. Before it starts a server, the role makes the same comparison, between the container's label and `docker compose config --hash '*'` for the files and `.env` on the host:

- With `--check`, a server that would be recreated is reported as a change, with the reason (the two hashes, shortened). A hash is read from the files already on the host, so a difference that only this run's own copy of the Compose file or `.env` would create is visible once they are written, not under `--check`.
- A running server is recreated only while every other server reports Ready, read through the server's own container. Recreating it stops its etcd member, and the cluster must keep its quorum meanwhile. When another server is not Ready, is not registered or cannot be read, the run fails for that host with the server named and nothing changed, under `--check` too. While none of the other servers is registered yet (the bootstrap server of a cluster that is still being brought up, restarted before anything has joined it) there is no quorum to keep, so that recreate goes ahead and is not waited for. To recreate a server anyway when the others cannot come back, run `docker compose up -d` for it by hand, as in the upgrade steps under [Versions](#versions). A server whose container is stopped is not a member of the quorum, so it is recreated without the check. Agents are exempt from it.
- The servers take turns, one at a time and in no fixed order: each turn checks, starts the server and, when it recreated a running container, waits for that server's node to report Ready on a heartbeat from the new container, meaning its lease (`kube-node-lease/<node>`) was renewed after the container started, since the API keeps the old container's Ready condition for a while (for as long as `github_runner_cluster_server_ready_timeout` allows) before the next server's turn begins, so no two servers are ever away together and a server that does not return stops the run with the others untouched. This relies on Ansible's linear strategy, the default; under `strategy: free` hosts do not wait for each other and nothing keeps the turns apart.

### A replaced node

The manual Secret deletion is for a node that is genuinely new under an old name: a host replaced with the same node name, or a node whose volume was removed (`docker compose down -v`, `docker volume rm`). Its new container generates a password the cluster does not know. Fix: on the bootstrap host, `kubectl delete secret <node>.node-password.k3s -n kube-system` (and, for an agent, `kubectl delete node <node>`), then restart the container. With `github_runner_cluster_wait_for_nodes` the role does this itself for a non-bootstrap host whose node is missing or not Ready after the run. Never delete a server's Node object: k3s then removes its etcd member, which cannot rejoin until its local datastore is moved aside.

## Docker host disk

Each host also runs a `docker-prune` service that removes Docker build cache and dangling images older than `DOCKER_PRUNE_AFTER` (a week by default) once every `DOCKER_PRUNE_INTERVAL_SECONDS` (daily). The node's ephemeral storage lives on the host's Docker disk, shared with anything else that builds there, so an untrimmed cache eventually puts the node under disk pressure and taints it unschedulable. Tagged images, containers and volumes are never touched. Set either variable in the host's `.env` to change them.

## Docker contexts

By default the role drives whichever Docker daemon the host's current Docker context points at (`docker context show`), or the one `DOCKER_HOST` names when that is set. That is right for a host with one Docker engine, but the current context is shared, mutable state that other software changes. The case that matters: a Mac that runs its node in Colima and also has Docker Desktop installed. Launching Docker Desktop switches the current context to `desktop-linux` without asking, so the next run of the role would find no node in Docker Desktop and build a second, empty k3s node there, alongside the real one in Colima, instead of managing it.

Set `github_runner_cluster_docker_context` in the host's variables to pin the context by name (`colima` in that example) on any host with more than one engine. Every `community.docker` module call in the role then passes it as `cli_context`, and every `docker` command the role runs (Compose, `exec`, `inspect`, `volume`, image pulls, and the commands in `playbooks/recover_in_cluster_mesh.yml` and `playbooks/promote_headscale_standby.yml`) runs with `DOCKER_CONTEXT` set to it, so the modules and the CLI always reach the same daemon. The role checks the pin before it touches the host and fails if the context does not exist there, or if `DOCKER_HOST` is also set, since the two would name the daemon in conflicting ways. It never creates a context and never changes the host's current context; `docker context show` on the host reads the same before and after a run. The Headscale tasks that act on another host (the `headscale_hosted` server and standby, and the promotion playbook) use that host's own value. Run `docker context show` on each host to see what it currently points at before choosing a value.

## Container runtime at login (macOS)

On a Mac the container runtime is a Linux VM (Colima, or Docker Desktop) that stays stopped after a reboot until something starts it. The node's k3s container has `restart: unless-stopped`, so it comes back by itself once the runtime does, but a host whose runtime never starts never rejoins the cluster. With three servers, two such hosts leave the cluster without quorum and no runner pods anywhere. Only macOS hosts are affected by any of this: the role does nothing about the runtime on a Linux host, where Docker is a system service.

Which runtime a Mac runs is taken from the Docker context the role drives (see [Docker contexts](#docker-contexts)): `colima`, or `colima-<profile>` for another profile, is Colima, and `desktop-linux` is Docker Desktop. `github_runner_cluster_runtime` says so explicitly for a host whose context does not tell, for example one that uses `DOCKER_HOST`, and `github_runner_cluster_colima_profile` names the profile to start when the context does not.

**Colima.** With `github_runner_cluster_runtime_autostart` on (the default, which acts on Colima hosts only), the role:

- starts the profile if it is stopped, with `colima start --profile <profile> --activate=false`, so the profile does not make its context the host's current one;
- installs a launchd agent, `com.exadev.github-runner.colima.<profile>`, as `~/Library/LaunchAgents/<label>.plist` of the user Ansible connects to, which runs the same command at every login with a `PATH` that names both Homebrew prefixes (`github_runner_cluster_runtime_path`), since launchd gives an agent only the system `PATH` and Colima starts `limactl` and `docker` from the one it has; and
- loads the agent into that user's login session. A user with no login session on the host (nobody has logged in at the console since the last boot) cannot have an agent loaded, so the role installs the file, says so, and leaves launchd to load it at the next login.

The role reads what it needs to tell a Mac from another host (the kernel name) and to name the agent (the connecting user's id and home directory) with minimal `setup` subsets of its own when the caller has not gathered them, so a play with `gather_facts: false` works. The role never uses `become` for this: a LaunchAgent belongs to the user whose login session loads it, and Colima's VM runs as that user. Changing a definition unloads the agent before loading the new one; a rerun with nothing changed touches neither the agent nor the VM, and `--check --diff` shows the property list that would be written. With the variable off, the role installs nothing and leaves an agent from an earlier run as it is.

A Mac still has to log in for the agent to run: a launch agent starts with the user's login session, not with the machine. A host that should recover from a reboot unattended needs automatic login for that user (System Settings > Users & Groups).

**Docker Desktop.** The role cannot start Docker Desktop. Turn on Settings > General > Start Docker Desktop when you sign in to your computer on each such host (https://docs.docker.com/desktop/settings-and-maintenance/settings/), and give it the same automatic login. If it is not running when the role runs, the run fails with that message.

**End checks.** On every macOS host the role checks, before anything reads the daemon and again after it has started the node's Compose project, that the runtime answers (`docker info` through the host's Docker context), and after starting the project that the node's k3s container (`github-runner-k3s`, or `github-runner-k3s-agent` on an agent) is running. A converge of a host that rebooted therefore repairs a stopped Colima or fails with a message that says what to do, and never reports success over a node that is not up. A `--check` run does not fail on a stopped runtime or container it has not started.

A Colima VM that starts after a reboot is a restarted VM, so the `br_netfilter` note under Non-obvious behaviour in the repository README applies to it: pods can lose cluster service IPs until that module is loaded in the VM.

This is the role's whole scope: it never moves a host between Colima and Docker Desktop.

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

## etcd timing

k3s's embedded etcd runs with a 100 ms heartbeat interval and a 1000 ms election timeout. Where the servers' peer traffic goes through a relay, as it does when each node's container sits behind NAT, round trips and jitter can exceed that: followers miss heartbeats, elections repeat, reads against etcd fail as Unavailable and k3s exits and restarts, which kills the pods on that node. `github_runner_cluster_etcd_heartbeat_interval` and `github_runner_cluster_etcd_election_timeout` (milliseconds, both empty by default so k3s's own timing applies and nothing changes for an existing fleet) raise them. When set, `env.j2` writes them into each server's `.env`, Compose passes them into the container, and `k3s/node-entrypoint.sh` adds `--etcd-arg=heartbeat-interval=<ms>` and `--etcd-arg=election-timeout=<ms>` to `k3s server`, on the mesh and when a self-hosted bootstrap server starts off it. Agents run no etcd and take neither.

```yaml
# group_vars/github_runner_cluster.yml
github_runner_cluster_etcd_heartbeat_interval: 500
github_runner_cluster_etcd_election_timeout: 5000
```

The role checks, before it changes anything on a host, that both are positive whole numbers, that they are set together or not at all, that the election timeout is at least five times the heartbeat interval (etcd refuses to start otherwise), and that every server of the group has the same two values, read from the inventory. etcd requires identical timing on every member, so set them once for the whole group.

A change reaches a server when the role recreates its container, which restarts k3s there. Edit the inventory once, then either run the role against every server together, which restarts all the control-plane nodes at about the same time and so takes the API down until a majority is back, or roll it out one server at a time with `--limit <server>`, waiting for that node to be Ready before the next (the identical-values check reads the whole group's inventory, so a limited run passes). Members briefly disagree either way. The role cannot see the timing the running servers use, so it does not warn when they differ: after a rollout that stopped partway, compare each server's `/var/lib/rancher/k3s/server/db/etcd/config`, which holds `heartbeat-interval` and `election-timeout` once they were passed.

An `--etcd-arg` is global: k3s also gives it to the temporary etcd it starts on every restart against an already-initialised datastore, never on a first boot. An earlier `listen-peer-urls` override broke exactly that restart, and only that one (see the first of the known issues in the [repository README](../../README.md) about `--etcd-arg`). These two arguments do not change what etcd binds to, but test a restart of an already-initialised datastore with them set before relying on them: `tests/mesh/run.sh` does that for the `hosted` scenario, restarting one server after the cluster is up. A timing that is too slow for the relay also lengthens how long a real leader failure takes to notice, so raise them only as far as the observed round trips need.

## Memory-pressure regrading

A server on a laptop or a small board can run short of memory while it still holds an etcd vote. Without this the choices are to leave it running whole and risk the node, or to stop it and lose the vote. `github_runner_cluster_regrade: true` lets a server step down while it keeps its etcd seat, and step back up. It is off by default and changes nothing on a running node until it is turned on.

There are three levels, from the most capable down:

- `full`: the server as it runs: control plane, etcd and workloads.
- `control-plane`: still a server and an etcd voter, tainted, cordoned and drained, so it runs no workloads.
- `etcd-only`: k3s runs with its API server, controller manager, scheduler and agent switched off (`--disable-apiserver`, `--disable-controller-manager`, `--disable-scheduler` and `--disable-agent`), so the server holds its etcd member and little else.

Giving up the vote is never part of it: a server that leaves etcd would have to be removed from the member list, which on a partition is unsafe, and k3s removes an etcd member when its Node object is deleted. The Node object of a stepped-down server stays, with its etcd role label.

A `regrade` service runs beside the server's k3s container (in its own Compose profile, which the role adds when the variable is true). It reads the memory pressure of the Docker host's kernel, the `some` line's `avg300` of `/proc/pressure/memory`, and `MemAvailable`; both are needed, because pressure alone also rises on reclaim while plenty is available, and a low `MemAvailable` alone is normal once the page cache has filled. A server counts as over the limit when the pressure is at least `github_runner_cluster_regrade_pressure_threshold` percent and `MemAvailable` is under `github_runner_cluster_regrade_available_floor_percent` of `MemTotal`. It steps down one level after being over for `github_runner_cluster_regrade_sustain_seconds` without a reading under the limit in between, and waits `github_runner_cluster_regrade_cooldown_seconds` after any step before the next. The cooldown has to exceed the pressure window, or the next reading still holds data from before the step; the role refuses a shorter one. The defaults are multiples of the window and the sample period, so they follow if either changes. The threshold and the floor are reasoned starting points, not measurements: read the service's log with `docker compose logs regrade` on a real host and tune them.

With `github_runner_cluster_regrade_step_up: automatic` (the default) the service also steps back up by itself, one level at a time. A server counts as calm when the pressure is under the threshold and `MemAvailable` is at or above the floor, both, which is stricter than not being over the limit: a reading between the two ends the streak towards stepping down and the streak towards stepping up alike, so the server does not step both ways on the same noise. It steps up after being calm for `github_runner_cluster_regrade_sustain_seconds`, once the cooldown since the last step, in either direction, has passed, through the same Lease and peer check as a step down; stepping up restarts the server's etcd at the first level that leaves etcd-only, so it never starts while a peer is down. A server that cannot hold the higher level cycles down and up no faster than once per cooldown and sustain period, which is the sign it is too small for the workloads it is given. `manual` leaves stepping up to `fleet-regrade up`.

Steps are serialised across the whole cluster. A server starts a step only while it holds a Lease named `fleet-regrade` in `kube-system` and every other etcd-voting Node is Ready, so two servers never step at once, and a step never starts while a peer is down. A server at etcd-only is not Ready (it has no kubelet), which also means no other server steps down while one is etcd-only. The Lease expires on its own after `github_runner_cluster_regrade_lease_duration_seconds` if its holder never gives it back. Every call the service makes to the cluster goes to another server's API and never to the stepping server's own: at etcd-only the server has no API, and its own kubeconfig answers `apiserver disabled` on some servers.

A level is applied by the Docker actuator, which records it in a file on the k3s data volume, which the container's entrypoint reads at every start, and restarts the container when the change is between etcd-only and another level. Between `full` and `control-plane` nothing restarts, because both run the same k3s; the difference is the taint and cordon the service puts on the Node. The container is restarted, never created again, so the node's password and mesh identity are the ones the cluster already holds, and an Ansible run, which changes nothing a container is created from, cannot undo a step. A level survives the container being recreated and the Docker host restarting: a server stepped down stays down, with its cordon, until it is stepped up, whether by hand or, in `automatic`, by the service once memory has been calm. The entrypoint also records, on the container's tmpfs, the level k3s started at; that is what the service reads as the level the node is at, since k3s hides its arguments from `/proc`. A server whose entrypoint predates this feature is at `full` until it next restarts.

A step that restarts the container returns only when etcd serves again, read from the container's log since it started, and, for a level that runs the agent, when the node reports Ready on a heartbeat from the new container. The Lease is released as soon as a step returns, so a step that returned early would let another server step on top of an etcd that has not come back.

The Node carries the label `github-runner.exadev.com/regrade-level` (`github_runner_cluster_regrade_level_label`) with the level as its value, from the first step down until the server is back at `full`. The role's wait for every node to be Ready leaves such a node out, so running the role against a fleet with a stepped-down server neither fails nor clears its node-password Secret. The heartbeat publishes the level beside each not-Ready node in `fleet-health.json`, and `scripts/fleet-alert.sh` does not count a stepped-down server as an outage; a heartbeat image that predates the field alerts on it as it would on any server that is not Ready, so update that image before turning regrading on.

By hand, in the project directory (the `.env` the role writes names the Compose files and project, so these act on the right one):

```sh
docker compose --profile regrade exec regrade fleet-regrade status
docker compose --profile regrade exec regrade fleet-regrade down   # one step down
docker compose --profile regrade exec regrade fleet-regrade up     # one step up
```

Both go through the same Lease and peer check as the watcher.

The role refuses regrading where it could not work, before it changes anything: on an agent; where the server is the only one in the group, since a server at etcd-only needs another server's API; and on the bootstrap server of a `headscale_in_cluster` mesh, whose Headscale server is a static pod on the kubelet that etcd-only switches off. It also refuses to turn regrading off, or to remove the service, while the server is below `full`, because nothing else would step it up: step it up first, then run the role again.

The service mounts the Docker socket, which is a permission cost: whatever runs in that container can start containers on the host with any mount. `docker-prune` has the same mount for the same reason, and the regrade service's scripts are mounted read-only from the project directory, not fetched at run time. Its Compose profile is separate from `server` and `agent`, so a host that does not enable regrading never builds or starts it. The image is built locally by Compose from `regrade/Dockerfile` (Alpine with bash, jq and the Docker CLI), the first time the service starts.

The decision logic lives in `files/regrade/fleet-regrade-core.sh` and calls only three functions of an actuator: `levels`, `current_level` and `apply_level NAME`. The Docker actuator is `files/regrade/fleet-regrade-docker.sh`. Another platform supplies its own three functions and uses the core unchanged; a NixOS image can take the core from a pinned release of this collection.

## Multiple sites

Each site is its own cluster: its own inventory group, bootstrap server and mesh provider, run by its own play with `github_runner_cluster_group` set to that group (see `examples/two-site/`). One cluster spanning two meshes is not supported: flannel's tailscale backend carries pod traffic over the single mesh every node belongs to, so every node of a cluster must be on the same tailnet or Headscale server. An inventory host belongs to at most one cluster group; to run a second cluster on the same machines, give each machine a second inventory host (see [Several clusters on one host](#several-clusters-on-one-host)).

## Several clusters on one host

A machine can run a node of a second, independent cluster beside a node of the first, for example to rebuild a cluster on another mesh (a different tailnet, a new k3s token, a fresh etcd) and move the runners to it before the first is removed. Each cluster is its own inventory group with its own play (`github_runner_cluster_group`), and the machine appears in each group as its own inventory host, both with the same `ansible_host`. The two nodes share the machine's Docker daemon, so the second cluster's hosts need their own values of everything that names something on it:

- `github_runner_cluster_container_name` and `github_runner_cluster_agent_container_name`: container names are unique on a daemon.
- `github_runner_cluster_api_port`: the port the server's API listens on inside its container, publishes on the Docker host and writes into its kubeconfig (`https://127.0.0.1:<port>`), and that joining nodes dial, so it is set once for the whole group. On a Mac every VM's published ports are forwarded to the Mac's own loopback, so it must differ from the first cluster's even when the second runs in a VM of its own. A port other than k3s's own 6443 adds `docker-compose.api-port.yml`, which starts k3s with `--https-listen-port`; the default adds nothing, so a cluster on the defaults keeps exactly the container configuration it had before the port was configurable and upgrading recreates none of its servers.
- `github_runner_cluster_compose_project`: Compose names the node's volumes (datastore, mesh identity, node password) and its image after the project.
- `github_runner_cluster_dir`: the directory holds the project's `.env` and the kubeconfig.

Before it writes anything to a host the role reads every container on its daemon and refuses to go on when a container of another Compose project, or of none, already has one of this cluster's container names, publishes this server's API port, or runs from this cluster's directory, naming each one. It also refuses an API port that is not a port number, hosts of one group that use different ports, and container names Docker would refuse or that are the same. Under `--check` the same read runs, so a dry run against hosts that run the first cluster shows whether the second fits.

The two nodes share the Docker host's memory, CPU and kernel limits. Each k3s server needs its own memory beside the pods scheduled on it, and both nodes' kubelets, containerd and pods draw on the same `fs.inotify.max_user_instances`, which a small VM may set low; measure both on the host before adding the second node. Each mesh device is the node's own container, so the two nodes are separate devices even when they are on the same tailnet and share a node name, and a name that is already a device there registers with a suffix (see `github_runner_cluster_node_name`). Each cluster's ARC role installs into the cluster its own kubeconfig reaches; a runner scale set's name can belong to only one controller at a time, so a scale set is moved by uninstalling it from one cluster before installing it in the other.

Each Compose project runs its own `docker-prune` service against the same daemon. Two of them do the same work and are harmless.

## Variables

- `github_runner_cluster_dir` (default `~/.github-runner` on the target host): the directory the role manages as the host's Compose project. It copies the collection's `docker-compose.yml` and `k3s/` build context there, templates `.env` there, and k3s writes the kubeconfig to its `kubeconfig/` subdirectory, so the host needs no checkout of this repository.
- `github_runner_cluster_compose_project` (default `github-runner`): the Compose project name. Compose prefixes the node's named volumes with it, and those volumes hold the k3s datastore and the node's mesh identity, so changing it on a running host brings up a new, empty node. Because it is pinned rather than taken from the directory's name, `github_runner_cluster_dir` can move without losing the cluster.
- `github_runner_cluster_k3s_token` (required), `github_runner_cluster_tailscale_join_key`, `github_runner_cluster_tailscale_api_token`, `github_runner_cluster_headscale_api_key`, `github_runner_cluster_headscale_join_key`: secrets, as plain variables from any source Ansible reads.
- `github_runner_cluster_join_key_path`, `github_runner_cluster_join_key_listener`: where a join key the role creates is kept (see [Join keys](#join-keys)).
- `github_runner_cluster_group` (default `github_runner_cluster`), `github_runner_cluster_max_servers` (default 5), `github_runner_cluster_api_port` (default 6443, k3s's own; the same on every host of the group, see [Several clusters on one host](#several-clusters-on-one-host)), `github_runner_cluster_datastore` (`etcd`, the default, or `sqlite`, which makes the cluster a single server with every other host an agent).
- `github_runner_cluster_mesh` (default `tailscale`), `github_runner_cluster_mesh_tag` (default `tag:github-runner-node`), `github_runner_cluster_pod_cidr` (default k3s's `10.42.0.0/16`).
- `github_runner_cluster_tailnet_domain`: the mesh's MagicDNS domain. Each node's address is `<node name>.<domain>`, and join URLs and TLS SANs use it.
- `github_runner_cluster_headscale_*`: the Headscale providers' settings, documented in `defaults/main.yml`.
- `github_runner_cluster_k3s_version`, `github_runner_cluster_tailscale_version`: the pinned image tags every node is built from, `github_runner_cluster_allow_k3s_version_jump` (default false) to skip the comparison with what runs, and `github_runner_cluster_cluster_read_timeout` (default 30, a positive whole number of seconds) for each server's read (see [Versions](#versions)). `github_runner_cluster_k3s_tested_version` is the k3s default, which the minor-version check compares hosts that set nothing against; set `github_runner_cluster_k3s_version` instead.
- `github_runner_cluster_node_name`: the k3s node name and mesh hostname, defaulting to the inventory name.
- `github_runner_cluster_node_taints`: Kubernetes taints (`key=value:Effect`, no spaces) the host's node registers with, empty by default. `NoSchedule` keeps pods without a matching toleration off it and `NoExecute` also evicts the ones already there. k3s applies them on every registration, so they come back if the node object is deleted, which a taint set with `kubectl` does not.
- `github_runner_cluster_etcd_heartbeat_interval`, `github_runner_cluster_etcd_election_timeout`: etcd's timing in milliseconds for every server, empty by default (k3s's own). Set together, the same on every server, the election timeout at least five times the heartbeat interval; see [etcd timing](#etcd-timing).
- `github_runner_cluster_regrade` (default false): run the `regrade` service, which steps this server down under memory pressure while it keeps its etcd vote (see [Memory-pressure regrading](#memory-pressure-regrading)). Servers only. `github_runner_cluster_regrade_pressure_threshold` (percent, default 10) and `github_runner_cluster_regrade_available_floor_percent` (default 15) are the limit; `github_runner_cluster_regrade_step_up` (`automatic`, the default, or `manual`), `github_runner_cluster_regrade_sample_seconds`, `github_runner_cluster_regrade_sustain_seconds`, `github_runner_cluster_regrade_cooldown_seconds`, `github_runner_cluster_regrade_operation_timeout_seconds` and `github_runner_cluster_regrade_lease_duration_seconds` are the timing, all derived from `github_runner_cluster_regrade_pressure_window_seconds` (the kernel's `avg300`, 300 seconds, not a setting) unless set. `github_runner_cluster_regrade_level_label` and `github_runner_cluster_regrade_taint` are derived, never set directly.
- Per-host overrides: `github_runner_cluster_node_role` (`server` or `agent`), `github_runner_cluster_bootstrap` (true picks the bootstrap host, false excludes a host), `github_runner_cluster_server_url` (join this server instead, for a cluster whose servers are outside the inventory), `github_runner_cluster_tls_sans`, `github_runner_cluster_node_address`. The run fails if the result has an even number of servers, no servers, or more than one bootstrap host.
- `github_runner_cluster_docker_context` (default empty): pins the Docker context this host's node runs in (see [Docker contexts](#docker-contexts)).
- `github_runner_cluster_runtime_autostart` (default true), `github_runner_cluster_runtime` (default `auto`; `colima`, `docker_desktop` or `other`), `github_runner_cluster_colima_profile` (default empty, taken from the Docker context), `github_runner_cluster_runtime_path` (default both Homebrew prefixes and the system directories): on a macOS host, start Colima at login through a launchd agent and check the runtime (see [Container runtime at login](#container-runtime-at-login-macos)).
- `github_runner_cluster_container_name` (default `github-runner-k3s`) and `github_runner_cluster_agent_container_name` (default `github-runner-k3s-agent`): the names of the node's server and agent containers, which a second cluster on the same Docker hosts sets to names of its own (see [Several clusters on one host](#several-clusters-on-one-host)).
- `github_runner_cluster_compose_files`: extra Compose files for the host's k3s project, merged after `docker-compose.yml`, as paths relative to `github_runner_cluster_dir` or absolute. Several nodes on one Docker host each need their own `github_runner_cluster_dir`, `github_runner_cluster_compose_project` and container names; nodes of one cluster share its API port, so all but one of them on a host also need an extra file that publishes none (`ports: !reset []`, as `tests/mesh` does).
- `github_runner_cluster_wait_for_nodes`: wait for every node to be Ready afterwards, clearing the node-password registration of a replaced node that fails to rejoin (installs the kubernetes Python client through `github_runner_k8s_client`).
- `github_runner_cluster_server_ready_timeout` (default 600): the most seconds, about, the role waits for a recreated server's node to report Ready before the next server's turn (see [Node passwords and recreating servers](#node-passwords-and-recreating-servers)).

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

`tests/mesh/run.sh` brings up three k3s nodes in Docker on one machine and joins them through Headscale with this role, for the `headscale_hosted` and `headscale_in_cluster` providers, and checks the `headscale_existing` policy check against a real server. Each cluster scenario runs the role a second time and fails if any node container restarted. Both set the etcd timing and check each server's etcd config file carries it, and the `hosted` scenario then restarts a joined server against its initialised datastore and checks the cluster recovers with the timing still in place. The `compose_faulty` scenario checks the role refuses a Compose release that has that fault. With `GRTEST_PIN_CONTEXT=1` the cluster scenarios pin `github_runner_cluster_docker_context` while the current Docker context the playbooks see points at a socket that does not exist, so any docker call that ignored the pin fails, and they check the current context is unchanged afterwards; `tests/litestream/run.sh` takes the same setting for the Headscale hosts it delegates to and the promotion playbook. The `context_checks` scenario checks the role refuses a pinned context that does not exist, and one set alongside `DOCKER_HOST`, before writing anything. `.github/workflows/mesh-integration.yml` runs them on pull requests that touch the role, the hosted scenario on 2.39.0, the first fixed release, as well as on a current one.

`tests/regrade` tests the regrading: the core's decision, stepping up by itself, and the Lease and peer coordination (`regrade-core.sh`, `regrade-recover.sh`, `regrade-lease.sh`) and the Docker actuator (`regrade-docker.sh`), all against stand-ins for `docker` and `kubectl`, run by `tests/unit/test_regrade_shell.py`; `tests/unit/test_cluster_regrade.py` covers the role's checks, the settings file, the Compose service, the levels the entrypoint starts k3s at and the tasks that remove the service.

`tests/unit/test_runtime_autostart.py` covers the [container runtime tasks](#container-runtime-at-login-macos) without a Mac: it renders the launchd agent through `tests/runtime_autostart/render_plist.yml`, and runs `tasks/runtime.yml` and `tasks/end_checks.yml` through the role against stand-ins for `docker`, `colima`, `launchctl` and `plutil` on `PATH`. It checks the variable defaults, that Linux hosts are left alone, that a second run changes nothing, that `--check` writes and starts nothing, and that each way the runtime or the node's container can be down fails with its message. `tests/unit/test_container_runtime.py` covers the filter that works out the runtime from the Docker context. Nothing there touches a real Colima, launchd or Docker Desktop, so loading the agent into a real login session at a real reboot is the part only a run on a Mac can show.

`tests/unit/test_node_password.py` covers [the node password volume and the servers' turns](#node-passwords-and-recreating-servers) without Docker: the filters in `plugins/filter/cluster_recreate.py`, the Compose file's volumes, and `tasks/node_password.yml`, `tasks/server_recreate_check.yml`, `tasks/server_ready.yml` and `tasks/start_servers.yml` run through the role (`tests/node_password/run.yml`) against a stand-in for `docker` that moves a real tar stream between its `docker cp` processes and keeps containers, volumes and node readiness as files, and a stand-in for the Compose module that recreates a container whose rendered hash differs and takes its node out of Ready until the role has waited for it. It checks the seeding (mode, ownership, labels, no copy of the password anywhere on the host, a failed copy leaving no volume), that `--check` changes nothing and reports each recreate, that a server is refused while another is not Ready and that agents are not, and from the order of the calls that the turns do not overlap. The `hosted` and `in_cluster` mesh scenarios recreate a joined server in Docker (`recreate_a_joined_server` in `tests/mesh/run.sh`) and check that its node rejoins with the same password and Secret, that no other container restarts, and that the role's `--check` report names exactly that server.
