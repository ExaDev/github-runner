# github_runner_arc

Installs GitHub's [Actions Runner Controller](https://github.com/actions/actions-runner-controller) into a Kubernetes cluster: the controller, one or more orgs' runner scale sets (one Helm release and namespace per scale-set profile), and an optional fleet-health platform (a heartbeat Deployment that refreshes a gist while the cluster is healthy, and a usage-driven autoscaler). Each part runs only on a host whose own `github_runner_arc_orgs` or `github_runner_arc_heartbeat_gist_id` is set.

It runs in two ways. Inside `playbooks/site.yml`, against a k3s server host that `github_runner_cluster` brought up, using the kubeconfig that role writes; an agent host installs nothing. Or on its own through `playbooks/arc.yml`, from the control machine against any cluster a kubeconfig reaches. Helm values are rendered on the control node either way, so the target needs no checkout.

## Validation

`tasks/validate.yml` checks every input that needs neither secrets nor a cluster (a measured sizing's node figures are read later, when the role installs the profile): each org's fields, profile names (derived or overridden) that would collide or make invalid Kubernetes names, two of an org's profiles sharing a release name, a `max_runners` that is not a whole number, the controller's release name and namespace, the same org configured on two hosts, more than one autoscaled profile, values files that do not exist, the autoscaler's pool settings, sizing inputs, burst settings, and the controller, probe and node label settings. Both playbooks run it in a play of its own before anything changes; the role runs it itself when included some other way. It also bootstraps the heartbeat gist when `github_runner_arc_heartbeat_bootstrap_gist` is on and no host in the inventory has one: it creates the gist with the control node's `gh` session and stops, printing the id to record.

Before touching the cluster the role then checks the secrets it is about to write (each org's App key looks like a PEM key, the pull credential and heartbeat token are set) and proves the pull credential can read every image it installs from `github_runner_arc_image_pull_registry`, by getting a pull-scoped registry token and fetching each image's manifest. With an App-sourced pull Secret (see below) it mints each org's token and proves it the same way, before installing anything.

## Variables

- `github_runner_arc_orgs`: the orgs this host installs. Each entry has `name`, `app_id` (needed only when the role writes the App Secret, see `github_runner_arc_manage_secrets`), `image`, `private_key` (the App's PEM private key, from any source Ansible reads) or `private_key_op_reference` (an `op://` reference that the `github_runner_secrets_onepassword` adapter resolves into `private_key`), optionally `installation_id` (resolved from the App when empty), optionally `app_secret_name` (the App Secret's name in each of the org's namespaces, default `<org>-github-app`), and `scale_set_profiles`, a list of profiles:
  - `suffix`: appended to the namespace (`arc-runners-<org><suffix>`) and release (`<org>-runners<suffix>`) names. Default empty.
  - `namespace`, `release_name`: the profile's namespace and Helm release name, in place of the names derived from the suffix. The release name is also the scale set's name on GitHub, so two profiles of one org cannot share it.
  - `values_file`: Helm values for the release, a Jinja template read from `github_runner_arc_values_dir` on the control node (or an absolute path). Without it the role's `templates/runner-scale-set-values.yaml.j2` is used, driven by the profile's own `max_runners`, `min_runners`, `container_mode` (`dind` for the chart's Docker-in-Docker mode) and `resources` (the runner container's requests and limits).
  - `node_selector`: a nodeSelector for the runner pods, merged over the eligibility label below.
  - `max_runners`: the release's `maxRunners`. It takes precedence over the values file and over `sizing`; on the autoscaled profile it is the floor the autoscaler starts from.
  - `runs_on_label`: the extra `scaleSetLabels` entry the runners carry. Default `<org>-runners`, which pools every profile of the org under one `runs-on` label; set a different one to keep a profile out of that pool.
  - `scale_set_labels`: the whole `scaleSetLabels` list, in place of `runs_on_label`. An empty list sets no `scaleSetLabels`, so jobs target the scale set by its release name.
  - `autoscale`: `true` on at most one profile in the whole inventory makes it the scale set the autoscaler manages.
  - `sizing`: derive a runner ceiling from node capacity, given as uniform figures or measured from each eligible node (see Sizing). On an ordinary profile without `max_runners` it sets `maxRunners`; on the autoscaled profile it sets the autoscaler's ceiling, leaving `maxRunners` as the floor.
  - `capabilities`: names of entries in `github_runner_arc_capabilities` that the profile's runner pods carry, in order (see [Capabilities](#capabilities)).
  - `burst`: let the runner pods overflow onto nodes a cluster autoscaler adds on demand, and count those nodes' runners into `maxRunners` (see [Burst nodes](#burst-nodes)). Needs `sizing`, and no `max_runners` or `autoscale`; with `exclusive: true` the pods run on burst nodes only and `burst.max_runners` is the whole ceiling, with no `sizing`.
- `github_runner_arc_values_dir`: the control-node directory relative `values_file` paths are read from.
- `github_runner_arc_capabilities`: the capabilities profiles can name, as a mapping of name to capability (see [Capabilities](#capabilities)). `github_runner_arc_tool_cache_path` (default `/opt/hostedtoolcache`) is where the tool cache is mounted, `github_runner_arc_sysroot_path` (default `/opt/sysroot`) is where system-library capabilities are extracted, and `github_runner_arc_runner_uid` and `github_runner_arc_runner_gid` (default `1001`, the runner user of ARC's `actions-runner` image) are the ids the capabilities' init containers run as.
- `github_runner_arc_kubeconfig_path`: the kubeconfig on the host the role runs against. Defaults to `github_runner_cluster`'s kubeconfig; empty means `KUBECONFIG` or `~/.kube/config`.
- `github_runner_arc_controller_chart_version`, `github_runner_arc_scaleset_chart_version`: chart pins; unset installs the latest chart. The controller chart's CRDs are applied (server-side) from `github_runner_arc_controller_chart_ref` at the controller's version before every controller install or upgrade, because Helm never upgrades a chart's CRDs itself; without that, a newer scale-set chart is rejected for fields the cluster's CRDs predate. Move the two pins together, and the CRDs follow. `tests/arc_upgrade/run.sh` covers the upgrade in a kind cluster.

Upgrading ARC on a live fleet. The new controller deletes every scale set whose version differs from its own (its log says "Autoscaling runner set version doesn't match the build version. Deleting the resource."), together with the set's listener and running ephemeral runners, until Helm upgrades that scale set. A controller version change therefore kills the jobs on running runners, so the role refuses it while any ephemeral runner is running (`github_runner_arc_allow_busy_controller_upgrade` accepts that), and a fleet with no running runner loses nothing. The check needs the controller chart version pinned, since an unpinned run cannot know what it will install. Dry-run (`--check`) cannot show a CRD or ownership failure, so it is not a rehearsal. Note the scale sets that exist before (`kubectl get autoscalingrunnerset -A`) and check the same set exists afterwards. Never apply a release's manifests by hand with `kubectl apply --server-side` to recover one: Helm's server-side apply then conflicts with `kubectl` on the labels a chart upgrade changes, and the ServiceAccount, Role and RoleBinding must be deleted for Helm to recreate them. The heartbeat reports a missing scale set within a few minutes (`fleet-health.json` in its gist), and jobs fall back to GitHub-hosted runners meanwhile.
- `github_runner_arc_controller_release_name`, `github_runner_arc_controller_namespace`: the controller's Helm release and namespace, default `arc` in `actions-runner-controller`. The stale-listener check, the heartbeat's RBAC and the heartbeat's controller check use them too.
- `github_runner_arc_controller_replicas`: above 1, the chart turns on leader election and the role adds a preferred pod anti-affinity across nodes. `github_runner_arc_controller_extra_values` is merged over the role's controller values.
- `github_runner_arc_metrics_enabled` (and the `_addr`/`_endpoint` settings): Prometheus metrics for the controller and every listener; the 0.14 chart only enables them together.
- `github_runner_arc_listener_probes_enabled`: readiness and liveness probes on each listener's metrics endpoint, so a listener that starts and then fails its GitHub authentication is not counted ready during a rollout. Needs metrics on.
- `github_runner_arc_node_label_key`, `github_runner_arc_node_label_value`: when the key is set, the controller, listeners and runner pods are restricted to nodes carrying the label. The role labels the nodes named in `github_runner_arc_labelled_nodes`.
- `github_runner_arc_ghcr_username`, `github_runner_arc_ghcr_token`, `github_runner_arc_heartbeat_gh_token`: secrets, as plain variables. The pull credential is needed only for a static pull Secret.
- `github_runner_arc_manage_secrets`: `false` stops the role writing the App Secrets and `heartbeat-gh-token`, and instead checks before any change that they already exist, and that each App Secret holds `github_app_id`, `github_app_installation_id` and `github_app_private_key`. The role then needs no `private_key` or `installation_id`, and no `app_id` either: it reads the App's id from the Secret where it needs it. An `app_id` that is set must match the Secret's `github_app_id`.
- `github_runner_arc_manage_image_pull_secret`: `false` stops the role writing the pull Secret, so an existing one named `github_runner_arc_image_pull_secret_name` is used as it is, kept current by something else. No pull credential is needed and the pull check is skipped; the role checks the Secret exists. Follows `github_runner_arc_manage_secrets` unless set.
- `github_runner_arc_image_pull_secret_source`: where a pull Secret the role writes gets its credential: `static` (the default) from `github_runner_arc_ghcr_username` and `github_runner_arc_ghcr_token`, or `app` from each org's GitHub App, renewed in the cluster (see [Image pull Secret from the GitHub App](#image-pull-secret-from-the-github-app)). `github_runner_arc_image_pull_secret_renewal_schedule` (default every 15 minutes) and `github_runner_arc_image_pull_secret_renewer_image` configure the renewal.
- `github_runner_arc_image_pull_registry`, `github_runner_arc_image_pull_secret_name`, `github_runner_arc_verify_image_pull`: the pull Secret's registry and name, and whether to prove the credential before writing it.
- `github_runner_arc_heartbeat_gist_id`: install the fleet-health platform from this host, refreshing this gist. `github_runner_arc_heartbeat_bootstrap_gist`, `github_runner_arc_heartbeat_gist_description` and `github_runner_arc_heartbeat_gist_consumer` control the bootstrap described above.
- `github_runner_arc_autoscaler_usable_budget_gi`, `github_runner_arc_autoscaler_max_ceiling`, `github_runner_arc_autoscaler_floor`: required when a profile sets `autoscale: true` and the platform is installed. The floor must equal the autoscaled profile's `maxRunners`, which every Helm upgrade reverts to. The other `github_runner_arc_autoscaler_*` and `github_runner_arc_heartbeat_*` settings have defaults; see `defaults/main.yml`.
- `github_runner_arc_node_recovery_enabled` (default `true`), `github_runner_arc_node_recovery_after_seconds` (default 120), `github_runner_arc_node_recovery_poll_seconds` (default 15), `github_runner_arc_node_recovery_image`: the node recovery watcher (see [Node recovery](#node-recovery)).
- `github_runner_arc_app_setup_*`, `github_runner_arc_app_manifest_code`, `github_runner_arc_app_private_key_path`: inputs to `playbooks/github_app_setup.yml` (see below), including `github_runner_arc_app_setup_write_secret`, `github_runner_arc_app_setup_secret_namespaces`, `github_runner_arc_app_setup_secret_name`, `github_runner_arc_app_setup_keep_private_key` and `github_runner_arc_app_setup_command`.

## Sizing

A profile's `sizing` works out how many runner pods a pool of identical nodes holds: each node's allocatable CPU and memory, less a baseline fraction reserved for what already runs there, divided by one pod's requests (the runner plus any sidecar, such as dind); the tighter of the two gives pods per node, times the node count, scaled by the safety margin and rounded down.

```yaml
sizing:
  node_allocatable_cpu: 8
  node_allocatable_memory_gib: 16
  node_count: 3
  pod_cpu_request: 1
  pod_memory_request_gib: 2
  sidecar_cpu_request: 0.25         # optional, default 0
  sidecar_memory_request_gib: 0.5   # optional, default 0
  baseline_cpu_fraction: 0.1        # optional, default 0
  baseline_memory_fraction: 0.25    # optional, default 0
  safety_margin: 0.8                # optional, default 1
```

That assumes the nodes are alike. Where their free capacity differs a lot, `measured: true` works it out from the cluster instead, each time the role runs. It reads every node the runner pods may be placed on: every node carrying the eligibility label and the profile's `node_selector`, with no `NoSchedule` or `NoExecute` taint other than those Kubernetes adds for a passing condition. The ceiling lasts until the next run, so a node that is not ready, unreachable or cordoned when it is measured still counts, as it will once it is back. For each one it takes the node's allocatable CPU and memory, subtracts what the pods already on it request and the per-node reserve, and divides what is left by one runner pod's requests; the tighter of the two gives the runners that fit there. The sum over the nodes, scaled by the safety margin and rounded down, is the ceiling. Pods in any scale set's namespace are left out, so running runners never lower the ceiling they are sized by, as are pods that have finished. The reserve covers what requests do not show, such as a workload that uses more than it requests. The kubeconfig needs cluster-wide read access to nodes and pods. The role prints each node's figures when it runs.

```yaml
sizing:
  measured: true
  pod_cpu_request: 1
  pod_memory_request_gib: 2
  sidecar_cpu_request: 0.25         # optional, default 0
  sidecar_memory_request_gib: 0.5   # optional, default 0
  reserve_cpu: 0.5                  # optional, per node, default 0
  reserve_memory_gib: 1             # optional, per node, default 0
  safety_margin: 0.8                # optional, default 1
```

A profile's own `max_runners` still takes precedence, and on the autoscaled profile the measured figure is the autoscaler's ceiling. Measured sizing uses the `exadev.github_runner.arc_measured_max_runners` filter.

The filter behind it is `exadev.github_runner.arc_max_runners`.

## Burst nodes

A fixed pool can overflow onto nodes that exist only while there is work for them, such as cloud instances a cluster autoscaler (Kubernetes' Cluster Autoscaler, for one) launches when pods are Pending and removes once they are idle. Such nodes usually carry a label and a matching `NoSchedule` taint so nothing else lands on them, and no eligibility label, so measured sizing never counts them. A profile's `burst` lets its runner pods onto them:

```yaml
burst:
  node_label_key: example.com/ci-burst        # the label every burst node carries
  node_label_values: [standard]               # optional; unset accepts any value of the label
  tolerations:                                # optional; for the burst nodes' taint
    - key: example.com/ci-burst
      operator: Exists
      effect: NoSchedule
  max_runners: 12                             # how many runners the burst nodes hold at most
```

The runner pods then lose the eligibility `nodeSelector`, and get instead a required node affinity accepting a node that carries either the eligibility label or the burst label, a preferred one (at the highest weight) for nodes without the burst label, so the scheduler keeps using the fixed pool while it has room, and the tolerations. The profile's own `node_selector` still applies everywhere. The listener keeps the eligibility `nodeSelector`, so it never runs on a node that can disappear. Without an eligibility label the runner pods may already go on any untainted node, so only the tolerations and the preference are added.

`burst.max_runners` is added to the ceiling `sizing` derives from the fixed nodes (uniform or measured, safety margin included), so ARC creates more runner pods than the fixed nodes hold; the extra pods stay Pending, which is what makes the autoscaler add a burst node. The safety margin is not applied to it, since burst nodes carry nothing else. Derive it from what the autoscaler may launch: each node group's maximum size times the runners one of its nodes holds (`exadev.github_runner.arc_max_runners` with the node's allocatable figures works that out). The placement comes from the `exadev.github_runner.arc_runner_placement` filter.

`burst.exclusive: true` puts a profile on the burst nodes only: the required node affinity has the burst term alone, with nothing preferred, and the eligibility label no longer applies to its runner pods. It suits work the fixed nodes cannot hold, such as image builds whose Docker daemon needs more memory than those nodes can spare, and pairs with `minRunners: 0` in the profile's values, so no burst node is kept for an idle runner. Such a profile takes no `sizing`, `max_runners` or `autoscale`: `burst.max_runners` is its whole `maxRunners`. Where it shares burst node groups with an overflowing profile, split the groups' capacity between the two `burst.max_runners`, since each is a claim on the same nodes.

## Capabilities

A capability adds something to a profile's runner pods when they start, so the runner image itself can stay a public, stock image such as `ghcr.io/actions/actions-runner`. `github_runner_arc_capabilities` defines them by name, and each profile lists the ones it wants in `capabilities`. There are three kinds.

A tool-cache capability, `{image, path?}`, brings a tool. For each one the runner pod gets an init container, named `capability-<name>`, that copies the tool from the image into an `emptyDir` mounted on the runner container at `github_runner_arc_tool_cache_path`, and `RUNNER_TOOL_CACHE` is set to that path. The runner hands `RUNNER_TOOL_CACHE` to every step, and setup actions that consult the Actions tool cache before downloading, such as `actions/setup-node` and `terraform-linters/setup-tflint`, then find the tool there and skip the download, with no change to the workflow. The init containers run as the runner's user, so the copies belong to it and a setup action can still add another version beside them. `path`, `<tool>/<version>` optionally followed by `/<subdirectory>`, also puts the tool on `PATH` before the job's first step, for a tool no setup action looks up (Terraform, whose `hashicorp/setup-terraform` always downloads, or the AWS CLI).

A system-library capability, `{sysroot_image, env?}`, brings shared libraries, their headers and their programs, for what cannot live in the tool cache: an MPI implementation, a solver library, a database client library, anything a compiler or the dynamic loader has to find. For each one the runner pod gets an init container, also named `capability-<name>`, that copies the image's relocatable prefix into an `emptyDir` mounted on the runner container at `github_runner_arc_sysroot_path` (`/opt/sysroot` by default); every system-library capability of a profile shares that one prefix, the init containers running in the profile's order, so a later capability's file replaces an earlier one's of the same name. Before the job's first step the job-started hook prepends the prefix's directories to the search paths a build and the loader use, `bin` to `PATH`, `lib` to `LD_LIBRARY_PATH` and `LIBRARY_PATH`, `include` to `CPATH`, `lib/pkgconfig` to `PKG_CONFIG_PATH` and the prefix itself to `CMAKE_PREFIX_PATH`, and then sets each variable in `env`, a mapping of name to single-line string for anything else the libraries need, such as the relocation variable Open MPI reads. This takes the place of an `apt-get install` layer in a runner image.

A hook capability, `{job_started_hook}`, is a bash script the runner runs before each job's first step, for anything small: configuring git, writing a proxy setting to `GITHUB_ENV`, checking a mount.

```yaml
github_runner_arc_capabilities:
  node-24:
    image: ghcr.io/example/github-runner-toolcache-node:24.21.0@sha256:<digest>
  tflint:
    image: ghcr.io/example/github-runner-toolcache-tflint:0.64.0@sha256:<digest>
  terraform:
    image: ghcr.io/example/github-runner-toolcache-terraform:1.16.5@sha256:<digest>
    path: terraform/1.16.5
  awscli:
    image: ghcr.io/example/github-runner-toolcache-awscli:2.37.10@sha256:<digest>
    path: aws-cli/2.37.10
  openmpi:
    sysroot_image: ghcr.io/example/github-runner-sysroot-openmpi:5.0.11@sha256:<digest>
    env:
      OPAL_PREFIX: "{{ github_runner_arc_sysroot_path }}"
  highs:
    sysroot_image: ghcr.io/example/github-runner-sysroot-highs:1.15.1@sha256:<digest>
  libpq:
    sysroot_image: ghcr.io/example/github-runner-sysroot-libpq:18.6@sha256:<digest>
  git-identity:
    job_started_hook: |
      git config --global user.name "Example CI"
      git config --global user.email ci@example.com
github_runner_arc_orgs:
  - name: ExampleOrg
    image: ghcr.io/actions/actions-runner:latest
    scale_set_profiles:
      - max_runners: 4
        capabilities: [node-24, git-identity]
      - suffix: -infra
        runs_on_label: example-infra
        max_runners: 2
        capabilities: [terraform, tflint, awscli, git-identity]
      - suffix: -scientific
        runs_on_label: example-scientific
        max_runners: 2
        capabilities: [openmpi, highs, libpq]
```

`<digest>` stands for the digest a published image has; see [Publishing the reference images](#publishing-the-reference-images). A capability image must be pinned to a version tag or a digest, never `latest` or no tag, so every runner pod gets the same tool; the role refuses an unpinned one. A digest, alone or after the tag as above, also pins what the tag could otherwise be moved to. Images are pulled with the pod's pull Secret, which a public image does not need.

### Profiles select capabilities

A profile is the unit a workflow selects, so a set of capabilities becomes a named runner by giving the profile its own `runs-on` label, as the `-infra` and `-scientific` profiles above do: `runs-on: example-infra` lands on a pod with Terraform, TFLint and the AWS CLI, `runs-on: example-scientific` on one with Open MPI, HiGHS and libpq, while `runs-on: exampleorg-runners` keeps landing on the org's pooled profiles. Profiles that share a label share a pool, and a job for that label can land on any of them, so give a profile with capabilities a label of its own unless every profile in the pool carries the same ones. A tool-cache capability that a job's setup action finds is only a shortcut, since the action downloads the tool when it is missing, but a tool put on `PATH` through `path`, or a library in the sysroot, is simply absent from a pod without it.

### The tool-image contract

A tool image is any image that has:

- a POSIX `sh` and `cp` at `/bin/sh` and on its `PATH`, which the init container runs as `/bin/sh -c 'cp -R /toolcache/. "$1"/'`, with the tool-cache mount as `$1`;
- the tool under `/toolcache`, already in the Actions tool-cache layout: `/toolcache/<tool>/<version>/<arch>/` holding the tool, and an empty `/toolcache/<tool>/<version>/<arch>.complete` file beside that directory, which `@actions/tool-cache` requires before it treats an entry as present;
- files readable, and directories searchable, by any user, since the copy runs as the runner's user.

`<tool>` and `<version>` are what the consuming setup action looks up: `node` and the plain version (`24.21.0`, no leading `v`) for `actions/setup-node`, `tflint` and the version without its `v` for `setup-tflint`. `<arch>` is named the way that action names the architecture, and actions differ: `actions/setup-node` uses Node's `os.arch()` (`x64`, `arm64`), while `setup-tflint` maps `x64` to `amd64` (`amd64`, `arm64`). For a tool used only through `path` the name does not matter, since `path` finds whichever architecture is there; the reference images use `os.arch()` names, the tool cache's own default. A multi-architecture image holds, for each platform, only that platform's `<arch>` directory, so the tool cache in any one pod has one architecture per tool, which is what `path` relies on. `path` adds `<tool>/<version>/<arch>[/<subdirectory>]` for the one `<arch>` with a `.complete` marker, and the job fails at its start if there is none or more than one.

`tool-images/` holds Dockerfiles for reference images following this contract: Node.js, Terraform, TFLint and the AWS CLI. Each downloads the official release for the target architecture in a first stage, verifies it (against the release's checksums, and for Terraform and the AWS CLI against the vendor's signing key, pinned by fingerprint), lays it out under `/toolcache`, and copies that into `busybox`, which supplies `sh` and `cp`. To build one yourself, for both architectures:

```sh
docker buildx build --platform linux/amd64,linux/arm64 --build-arg NODE_VERSION=24.21.0 \
  -t ghcr.io/example/toolcache-node:24.21.0 --push tool-images/node
```

A capability image of your own takes a few lines. This one packages a static binary, `example-cli`, built beside the Dockerfile for each architecture, for use through `path`:

```dockerfile
FROM busybox:1.37
ARG TARGETARCH
COPY example-cli-linux-${TARGETARCH} /toolcache/example-cli/1.2.3/${TARGETARCH}/example-cli
RUN touch "/toolcache/example-cli/1.2.3/${TARGETARCH}.complete" && chmod -R a+rX /toolcache
```

and is declared as `{image: ghcr.io/example/toolcache-example-cli:1.2.3, path: example-cli/1.2.3}`.

### The system-library payload contract

A system-library image is any image that has:

- a POSIX `sh` and `cp` at `/bin/sh` and on its `PATH`, which the init container runs as `/bin/sh -c 'cp -R /sysroot/. "$1"/'`, with the sysroot mount as `$1`; `cp -R` keeps symbolic links as links, which a library's versioned names (`libfoo.so` to `libfoo.so.1`) rely on;
- the payload under `/sysroot`, laid out as an install prefix: programs in `bin/`, shared libraries in `lib/` (not `lib64/` or a multiarch subdirectory, which the search paths do not include), headers in `include/`, pkg-config files in `lib/pkgconfig/` or `share/pkgconfig/`, and CMake packages anywhere CMake's search under a prefix finds them, such as `lib/cmake/<name>/`;
- a payload that works from whatever directory it is copied to, given the search paths and the capability's `env`: it must not need its build prefix to exist. In practice that means pkg-config files that name their prefix relative to their own location (`prefix=${pcfiledir}/../..`, which pkg-config and pkgconf both expand), CMake packages that compute their paths relative to themselves (what `install(EXPORT)` generates), no libtool `.la` archives (they record absolute paths), and, for a library that records its install prefix in its binaries, an environment variable it reads to find its files elsewhere, set through `env`;
- programs and libraries built for the runner image's own C library, on the same distribution release as the runner image or an older one, linking only libraries the runner image already has or the payload carries itself. The payload should not carry its own copy of a library the runner image has, such as the C library, OpenSSL or the C++ runtime: `LD_LIBRARY_PATH` puts the sysroot first for every step, so its copy would replace the image's for everything those steps run;
- files readable, and directories searchable, by any user, since the copy runs as the runner's user.

The search paths and `env` are applied by the job-started hook, through `GITHUB_PATH` and `GITHUB_ENV`, not set on the runner container. A container variable replaces the image's own value instead of extending it, which for `PATH` would remove every directory the image puts there, and the runner process itself then never loads a library from the sysroot. A value already in a search path when the job starts stays after the sysroot's directory.

The reference images under `tool-images/` build from source in an `ubuntu` stage of the runner image's release, with `--prefix=/sysroot`, then drop `.la` files, rewrite each pkg-config file's `/sysroot` to `${pcfiledir}/../..`, fail the build if a pkg-config file (or, for HiGHS, a CMake package) still names `/sysroot`, and copy `/sysroot` into `busybox`. Each source archive is checked against a SHA-256: the release's own checksum file for PostgreSQL, and a checksum pinned in the Dockerfile for Open MPI (as Open MPI publishes it) and HiGHS (taken from GitHub's archive of the release tag). The payloads are built under `/sysroot` and copied to `/opt/sysroot`, so the default path is itself a relocation.

| Image | What it holds | Declared with |
|---|---|---|
| `tool-images/openmpi` | Open MPI with its bundled hwloc, libevent, PMIx (built with zlib) and PRRTE, without Fortran bindings (the runner image has no Fortran runtime): `mpicc`, `mpirun`, `mpiexec`, `ompi_info`, `libmpi` and its headers, `ompi-c.pc` | `env: {OPAL_PREFIX: "{{ github_runner_arc_sysroot_path }}"}` |
| `tool-images/highs` | HiGHS, the linear and mixed-integer programming solver: `libhighs`, its C and C++ headers under `include/highs/`, `highs.pc`, a CMake package and the `highs` command | nothing extra |
| `tool-images/libpq` | PostgreSQL's client library, built against the runner image's OpenSSL: `libpq`, its headers, `libpq.pc` (without its `Requires.private` on OpenSSL's modules, so compiling against it needs no OpenSSL development package), `pg_config` and `psql` | nothing extra |

Open MPI records its install prefix in its libraries and programs, and from `/opt/sysroot` without `OPAL_PREFIX` `mpirun` fails at start because it looks for its help files and components under `/sysroot`. With `OPAL_PREFIX` set to the sysroot, `mpirun` launches processes and `mpicc` compiles against the relocated headers and libraries; the bundled PMIx and PRRTE need no variable of their own. HiGHS reads no path at run time, and PostgreSQL's programs (`pg_config`, `psql`) work out their installation from their own location, so neither needs `env`.

The stock `actions-runner` image has no C compiler, and its runner user has no passwordless `sudo`, so a job that compiles against a sysroot library needs a runner image that has one; a job that only runs a payload's programs, or loads its libraries from an interpreter, works on the stock image. `tests/system_libraries/run.sh` checks each reference image both ways: it copies the payload to `/opt/sysroot` as the init container does, renders the profile's hook data through the role's own tasks, runs the role's job-started hook as the runner user, and then, in the stock image, runs the payload's programs (a two-process `mpirun`, the `highs` command solving a model, `psql` and `pg_config`) and checks that every program and library resolves its dependencies; and, in the same image with `gcc` and `pkg-config` added, compiles a small program against the payload through its pkg-config file and runs it (for Open MPI, an `MPI_Allreduce` across two processes under `mpirun`). It also checks that `mpirun` fails without `OPAL_PREFIX`, so the declaration above stays necessary. `.github/workflows/system-libraries.yml` runs it for each image on amd64 and arm64 on pull requests that touch the images, the hook or the role's rendering.

A system-library image of your own follows the same pattern: build the library in a stage of the runner image's distribution release with `--prefix=/sysroot` (for CMake, `-DCMAKE_INSTALL_PREFIX=/sysroot -DCMAKE_INSTALL_LIBDIR=lib`), finish the payload, and copy it into `busybox`:

```dockerfile
FROM ubuntu:24.04 AS build
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gcc libc6-dev make
WORKDIR /build
COPY example-lib-1.2.3.tar.gz .
RUN tar -xzf example-lib-1.2.3.tar.gz --strip-components=1 \
    && ./configure --prefix=/sysroot --disable-static && make -j"$(nproc)" && make install
# hadolint ignore=SC2016
RUN find /sysroot -name '*.la' -delete \
    && find /sysroot -name '*.pc' -exec sed -i 's|/sysroot|${pcfiledir}/../..|g' {} + \
    && ! grep -rl /sysroot --include='*.pc' /sysroot \
    && chmod -R a+rX /sysroot

FROM busybox:1.37
COPY --from=build /sysroot /sysroot
```

Then prove it the way `tests/system_libraries/run.sh` does before relying on it: copy it to a prefix other than `/sysroot`, and run its programs and a program linked against it with only the search paths and its `env`, in the runner image the profile uses.

### Publishing the reference images

`.github/workflows/publish-tool-images.yml` builds every reference image in `tool-images/` natively for `linux/amd64` and `linux/arm64` and pushes it to the repository owner's GitHub Container Registry namespace, as `ghcr.io/<owner>/github-runner-toolcache-<name>` and `ghcr.io/<owner>/github-runner-sysroot-<name>`, tagged with the version its Dockerfile pins. It runs only when started by hand (`gh workflow run publish-tool-images.yml`) or by pushing a tag named `tool-images-<anything>`; the release workflow does not build these images. A version tag that already exists is left alone, so a published version never changes under anyone using it, and a run publishes only the images whose pinned version is new; to publish a new version, change the Dockerfile's version (and, for Open MPI and HiGHS, its checksum) and run it again. The run's summary prints each pushed image as `<image>:<version>@sha256:<digest>`, the reference to put in a capability. A newly created GitHub Container Registry package is private until its visibility is changed to public in the package's settings, which pods without a pull credential need.

### The job-started hook

When a profile has a hook capability, a `path` or a system-library capability, the role writes a ConfigMap named `github-runner-job-started` into the profile's namespace and mounts it on the runner container at `/etc/github-runner/hooks`, read-only. It holds `job-started.sh` (the role's `files/job-started.sh`), `tool-paths` (one `path` per line), `sysroot-env` (the sysroot's search paths, as `NAME+=directory` lines the hook prepends, and the capabilities' `env`, as `NAME=value` lines it sets) and `job-started.d/`, the hook capabilities' scripts, numbered in the profile's order. `ACTIONS_RUNNER_HOOK_JOB_STARTED` names `job-started.sh`, the same runner mechanism the runner image uses for its job-completed hook (`ACTIONS_RUNNER_HOOK_JOB_COMPLETED`), so the runner runs it as the runner user before the job's first step, in a step named "Set up runner", with the job's default environment variables and its environment files, which the runner applies to the job's steps once the hook ends. It appends each tool path to `GITHUB_PATH`, applies `sysroot-env` through `GITHUB_PATH` and `GITHUB_ENV` (and exports it, so the scripts after it see it too), then runs every script in `job-started.d` in name order with `bash -e`. Unlike the job-completed hook, which never fails the job it ends, a failure here fails the job, since a job whose tools or setup did not arrive should stop before its first step. A profile without any of these gets no ConfigMap, and the role removes one an earlier run left. The scripts live in a ConfigMap owned by root, so a job cannot change what the next one runs, and each runner pod reads the ConfigMap when it starts, so a change reaches the next job without restarting anything.

### Where capabilities apply

The tool cache, the sysroot and the hook are on the runner container, so they serve the steps that run there: every step in the default container mode and in Docker-in-Docker mode (`container_mode: dind`). A job that runs in a `container:` reaches the tool cache only through the runner's own mapping of it, which in Docker-in-Docker mode points at the Docker daemon's filesystem, where the cache is not mounted, and it does not see the sysroot at all. The `kubernetes` container modes run every step in a separate pod, so the role refuses capabilities on a profile whose values use them. It also refuses them when the values define no runner container (the role's own template does), when the runner container already sets `RUNNER_TOOL_CACHE` or `ACTIONS_RUNNER_HOOK_JOB_STARTED`, when a volume already uses the name `tool-cache`, `sysroot` or `job-started-hooks`, or, for a profile with a system-library capability, when `github_runner_arc_sysroot_path` is not an absolute directory without a `:` or overlaps the tool cache or the hooks directory. During validation it refuses a system-library capability whose `env` sets one of the search paths, `RUNNER_TOOL_CACHE` or `ACTIONS_RUNNER_HOOK_JOB_STARTED`, a name that is not a variable name or a value that is not a single-line string (a line break would add a line to `GITHUB_ENV`), and two of a profile's system-library capabilities that set one variable to different values, since they share one sysroot. A values file's own init containers, volumes, environment and mounts are kept, and the capabilities' are added after them, by the `exadev.github_runner.arc_capability_pod` filter. `exadev.github_runner.arc_capability_errors` checks the catalogue and each profile's references during validation, before anything changes.

Each tool-cache and system-library capability adds its copy to every pod's start, which is not yet measured. The `emptyDir` volumes live on the node's disk for the pod's lifetime, so a large tool set or sysroot counts against the node's ephemeral storage.

`tests/capabilities/run.sh` exercises all of this in a kind cluster: it builds the reference Node.js and libpq images, renders a profile through the role's own tasks, renders the scale-set chart with the result and checks it against the ARC CRDs, then starts a pod from the chart's runner pod template and checks that the init containers filled the tool cache and the sysroot, that the hook ran, put the tool on `PATH` and the library on its search paths with its `env`, and that `@actions/tool-cache`, the library behind `actions/setup-node`'s lookup, finds the tool. `.github/workflows/capabilities.yml` runs it on pull requests that touch the role, its filters, the tool images or the test.

## Image pull Secret from the GitHub App

When the runner image is a private package owned by the org, `github_runner_arc_image_pull_secret_source: app` pulls it with the runners' own GitHub App rather than a registry token tied to a person. The App needs the `packages: read` permission (add it to `github_runner_arc_app_setup_permissions` when creating the App, or to an existing App's settings), approved by the organisation on the App's installation.

For each org the role signs an App JWT with the App's private key (the org's `private_key`, or the existing App Secret when `github_runner_arc_manage_secrets` is `false`), mints an installation token restricted to `packages: read`, and proves it can read the org's image with the registry's own token exchange and a manifest read, all before changing anything. It then writes the token into each of the org's namespaces as the pull Secret, with `x-access-token` as the username and the token's expiry in the `github-runner.exadev/pull-token-expires-at` annotation.

An installation token lasts an hour, so the role also installs a CronJob in each of those namespaces, named after the pull Secret with `-renewer` appended, that repeats the mint and the check on `github_runner_arc_image_pull_secret_renewal_schedule` and patches the Secret only when both succeed; a refused run leaves the current Secret in place and fails the Job. It reads the App Secret from a mounted volume, and its Role allows only `get` and `patch` on the pull Secret by name. Its image, `github_runner_arc_image_pull_secret_renewer_image`, is pulled without a pull Secret, so an expired token never stops the job that replaces it, and the image must be public. With `static` the role removes any such CronJob and its RBAC.

The fleet-health platform belongs to no one org, so with `app` its Deployments pull without a pull Secret; the role's default platform images are public.

```yaml
github_runner_arc_image_pull_secret_source: app
github_runner_arc_orgs:
  - name: ExampleOrg
    app_id: 123456
    private_key: "{{ vault_example_app_private_key }}"
    image: "ghcr.io/exampleorg/github-runner:1.0.0"
    scale_set_profiles:
      - max_runners: 4
```

## Node recovery

Every host that installs anything also installs a small watcher, the `node-recovery` Deployment in `github_runner_arc_platform_namespace`, so that a node that stops does not leave its pods `Terminating` for ever. ARC waits for a listener's old pod to go before starting a new one, so a listener on a stopped node otherwise stalls its scale set until the pod is force-deleted by hand. Once a node's `Ready` condition has been `Unknown` for `github_runner_arc_node_recovery_after_seconds`, the watcher gives it the `node.kubernetes.io/out-of-service=nodeshutdown:NoExecute` taint of Kubernetes' non-graceful node shutdown, which makes the control plane evict the node's pods and force-delete the terminating ones; it removes the taint when the node reports `Ready` again. It never acts on a node reporting `NotReady`, never on its own node, and never removes a taint it did not apply. Its ClusterRole allows `get`, `list` and `patch` on nodes and nothing else. `github_runner_arc_node_recovery_enabled: false` removes the watcher, its RBAC and any taint it left behind. Its image is pulled without a pull Secret, so it must be public. The repository README's Node recovery section explains the default threshold.

## GitHub App setup

`playbooks/github_app_setup.yml` creates the App the scale sets authenticate as, through GitHub's manifest flow. The first run renders a local form that posts the manifest (organisation self-hosted runner write access, no webhook, private) to GitHub; an organisation owner submits it and copies the one-time code GitHub returns. The second run, with that code and a path for the key, exchanges the code, writes the private key there, waits while the App is installed on the organisation, and prints the `app_id` and `installation_id` to record.

```sh
ansible-playbook playbooks/github_app_setup.yml -e github_runner_arc_app_setup_org=example-org -e '{"github_runner_arc_app_setup_name": "Example runners"}'
ansible-playbook playbooks/github_app_setup.yml -e github_runner_arc_app_setup_org=example-org -e '{"github_runner_arc_app_setup_name": "Example runners"}' \
  -e github_runner_arc_app_manifest_code=<code> -e github_runner_arc_app_private_key_path=~/example-app.pem
```

With `github_runner_arc_app_setup_write_secret: true`, the second run also writes the App Secret, with the `github_app_id`, `github_app_installation_id` and `github_app_private_key` keys the role's install reads, into the cluster `github_runner_arc_kubeconfig_path` reaches. It goes into `github_runner_arc_app_setup_secret_namespaces`, or by default each namespace of the organisation's scale-set profiles in `github_runner_arc_orgs`, under `github_runner_arc_app_setup_secret_name`, or by default the organisation's `app_secret_name` (`<org>-github-app` unless set). The run reaches the cluster and creates those namespaces before it uses the one-time code, so a broken cluster path costs only a rerun. Once the Secret is written it removes the key file, unless `github_runner_arc_app_setup_keep_private_key` keeps it; a run that stops earlier, for example because nobody installed the App within the wait, leaves the file so the App can still be finished without a new code. The private key is never printed, even with `-v`. The role's install then needs no `private_key` for that organisation: set `github_runner_arc_manage_secrets: false` so it uses the Secret as it is.

A playbook that wraps this one, for example to set the organisation and App name from its own variables, sets `github_runner_arc_app_setup_command` to the command that runs the wrapper, so the form and the first run's instructions name it:

```yaml
- name: Set the App's identity
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Hand the identity to the collection's flow
      ansible.builtin.set_fact:
        github_runner_arc_app_setup_org: example-org
        github_runner_arc_app_setup_name: Example runners
        github_runner_arc_app_setup_command: ansible-playbook app-setup.yml
        github_runner_arc_app_setup_write_secret: true
        github_runner_arc_app_setup_secret_namespaces: [example-runners]
        github_runner_arc_app_setup_secret_name: example-github-app

- name: Run the collection's App manifest flow
  ansible.builtin.import_playbook: exadev.github_runner.github_app_setup
```

## Example

```yaml
# host_vars/<host>.yml, or a vars file for playbooks/arc.yml
github_runner_arc_values_dir: "{{ inventory_dir }}/values"
github_runner_arc_orgs:
  - name: ExampleOrg
    app_id: 123456
    # The App's PEM key, from ansible-vault here; any lookup works too. With the 1Password adapter, private_key_op_reference: "op://Vault/example-app-private-key/private key" instead.
    private_key: "{{ vault_example_app_private_key }}"
    image: "ghcr.io/example/github-runner:latest"
    scale_set_profiles:
      - suffix: ""
        max_runners: 4
        resources:
          requests: {cpu: "2", memory: 4Gi}
          limits: {cpu: "2", memory: 4Gi}
      - suffix: "-docker"
        values_file: "example-docker-values.yaml"
        runs_on_label: "example-docker"
```

## Managing an existing install

A cluster whose controller and scale set were installed under other names can be managed by setting those names, rather than getting a second controller and scale set beside them. The role then upgrades the existing releases in place.

```yaml
github_runner_arc_controller_release_name: "existing-controller"
github_runner_arc_controller_namespace: "existing-controller-ns"
# The App Secret and the pull Secret already exist and are kept current elsewhere.
github_runner_arc_manage_secrets: false
github_runner_arc_manage_image_pull_secret: false
github_runner_arc_image_pull_secret_name: "existing-pull-secret"
github_runner_arc_orgs:
  # No app_id: the role reads it from the existing App Secret.
  - name: ExampleOrg
    app_secret_name: "existing-app-secret"
    image: "ghcr.io/example/github-runner:2026.01.01"
    scale_set_profiles:
      - namespace: "existing-runners-ns"
        release_name: "existing-runners"
        values_file: "existing-runners-values.yaml"
        max_runners: 6
        # Jobs target the release name, so no scaleSetLabels.
        scale_set_labels: []
```
