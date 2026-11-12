# ExaDev GitHub Actions Runner

[![GitHub](https://img.shields.io/badge/GitHub-181717?logo=github&logoColor=white)](https://github.com/ExaDev/github-runner) [![Release](https://img.shields.io/github/v/release/ExaDev/github-runner)](https://github.com/ExaDev/github-runner/releases/latest) [![CI](https://img.shields.io/github/actions/workflow/status/ExaDev/github-runner/ci.yml?branch=main)](https://github.com/ExaDev/github-runner/actions)

> An Ansible collection, `exadev.github_runner`, for a self-hosted GitHub Actions runner fleet: GitHub's own [Actions Runner Controller](https://github.com/actions/actions-runner-controller) (ARC) on a k3s cluster that spans Apple Silicon (arm64) and Intel (amd64) hosts on different networks, joined over Tailscale or Headscale. It also holds the runner image and the fleet-health platform's images. A fleet's own inventory, host variables and Helm values live in that fleet's repository, which installs this collection.

## Getting started

There are two ways to deploy. A fleet's hosts are managed from a control machine with Ansible, from the fleet's own inventory (see [Ansible](docs/ansible.md) below). `bootstrap.sh`, described here, brings up a single machine from a checkout of this repository without a control node or inventory. It runs the same Ansible role against the machine itself, so both paths deploy identically.

Prerequisites on the host: Docker (or Colima) with the Compose plugin, any release but 2.37.1 to 2.38.x (the cluster role refuses those; see Docker Compose versions in `roles/github_runner_cluster/README.md`), `python3`, `jq`, `openssl`, and `gh`. On macOS, the playbook installs `helm`, `kubectl` and Docker through Homebrew itself, and the cluster role starts a Colima host's profile again after a reboot (the node's profile at boot, with nobody logged in, any other at login) and fails unless the runtime is up (Docker Desktop needs its own start-at-login setting; see Container runtime after a reboot in `roles/github_runner_cluster/README.md`). Elsewhere, install `helm` and `kubectl` from the host's package manager first.

1. **Create a GitHub App** on the organisation with **Self-hosted runners: read & write** permission, and install it on the organisation. `playbooks/github_app_setup.yml` does this through GitHub's manifest flow (see `roles/github_runner_arc/README.md`). For one repository's runners instead, which is how a repository of a personal account uses the fleet, the App needs repository **Administration: read & write** and **Metadata: read**, installed where it reaches that repository, and `RUNNER_REPOSITORY` names it (see Repository scope in `roles/github_runner_arc/README.md`).
2. **Configure the environment.** Copy `.env.bootstrap.example` to `.env.bootstrap` and fill in the following (the file's own comments cover the rest, such as joining an existing cluster or running as an agent):
   - `K3S_TOKEN`: any random string (`openssl rand -hex 32`). It authenticates node-join inside the cluster only; it has no relation to GitHub.
   - `K3S_VPN_AUTH_JOIN_KEY`, `K3S_TLS_SAN_LIST`: see [Tailscale](docs/architecture.md#tailscale) below.
   - `RUNNER_ORG`, `RUNNER_APP_ID`, `RUNNER_APP_PRIVATE_KEY_PATH`: the organisation, the App ID and a path to its private key PEM file (for example under `./secrets/`, already gitignored). With `RUNNER_REPOSITORY` set, the runners register in that repository of `RUNNER_ORG`, which may then be a user.
   - `RUNNER_APP_INSTALLATION_ID`: optional. Leave it blank and the role resolves it automatically from the App ID and key.
   - `RUNNER_IMAGE`, and either `RUNNER_VALUES_FILE` (a Helm values template) or `RUNNER_MAX_RUNNERS` (the ARC role's own template with that many runners at most).
   - `GHCR_PULL_USERNAME`, `GHCR_PULL_TOKEN`: a registry credential, such as a GHCR personal access token with `read:packages` scope, to pull the runner image and the fleet-health platform's own images.
   - `HEARTBEAT_GH_TOKEN`, `HEARTBEAT_GIST_ID`: see [Heartbeat](docs/heartbeat.md) below.
3. **Build and push the runner images.** A fleet spanning both `arm64` and `amd64` hosts needs a real multi-arch push, not a plain `docker build`: `docker buildx build --platform linux/arm64,linux/amd64 --push -t ghcr.io/exadev/github-runner:latest .` for the main runner image (see [Build, test, and smoke-test](docs/build-and-test.md) for why this is currently the only working path, not just a manual fallback), plus the same for `heartbeat/Dockerfile` → `ghcr.io/exadev/github-runner-heartbeat:latest` and `autoscaler/Dockerfile` → `ghcr.io/exadev/github-runner-autoscaler:latest` - both now run as in-cluster pods that can land on either architecture, so both need pulling from either too.
4. **Bootstrap everything:**
   ```bash
   ./bootstrap.sh
   ```
   This installs Ansible into a repo-local virtualenv (`.ansible-venv/`, gitignored), turns `.env.bootstrap` into role variables, and runs `playbooks/site.yml` against this machine over a local connection. The role starts k3s, waits for it to be ready, installs the ARC controller and the fleet-health platform (heartbeat + autoscaler, as in-cluster Deployments), resolves the App installation ID if needed, creates the GitHub App and GHCR pull secrets, and installs the org's runner scale set. It is safe to rerun at any time, for example after rotating the App's private key. Extra arguments pass through to `ansible-playbook`, so `./bootstrap.sh --check` is a dry run. The role keeps the node's Compose project in `~/.github-runner` (`GITHUB_RUNNER_CLUSTER_DIR` changes it) and writes a `.env` there on every run, which is why `bootstrap.sh` reads its own input from `.env.bootstrap` instead. Multiple orgs and multiple scale-set profiles per org are configured through Ansible `host_vars` (see below), not `bootstrap.sh`.

## Ansible

The repository is an Ansible collection, `exadev.github_runner` (see `galaxy.yml`): roles in `roles/`, playbooks in `playbooks/` (`site.yml` for the whole fleet, `arc.yml`, `github_app_setup.yml`, `recover_in_cluster_mesh.yml`). A fleet keeps its inventory, `host_vars` and Helm values in its own repository, installs the collection there and runs the playbooks by name. `bootstrap.sh` is a thin wrapper that runs the same playbook locally. The roles, server election, scale-set profiles, host prerequisites, Unraid hosts, capabilities and the registry cache are in [docs/ansible.md](docs/ansible.md).

```bash
# in the fleet's own repository, next to its inventory
ansible-galaxy collection install git+https://github.com/ExaDev/github-runner.git
ansible-playbook -i inventory.yml exadev.github_runner.site --limit node1 --check   # dry run
ansible-playbook -i inventory.yml exadev.github_runner.site --limit node1           # real run
```

## Build, test, and smoke-test

There is no application test suite; the checks are the project's verification surface. Run the repository's own lint and checks before pushing: `.github/workflows/ci.yml` runs Shellcheck, actionlint, yamllint, hadolint, an Ansible syntax check, ansible-lint (`production` profile) and a collection build check, gated by one `required-checks` job. `.husky/commit-msg` runs commitlint locally after `npm ci` (without a machine-wide hooks dispatcher, `git config core.hooksPath .husky` enables it). The image builds (local `docker buildx` is the only working path for the runner image), the smoke tests (`gh workflow run test-arc-runner.yml`, `test-ubuntu-latest.yml`, `test-autoscaler.yml`), the cluster health commands, `tests/*/run.sh` for capabilities, the build image's ids and the registry cache, the published-image canaries, and the full lint notes are in [docs/build-and-test.md](docs/build-and-test.md).

## Releases

Every push to `main` runs `.github/workflows/release.yml` (semantic-release): it stamps the version, builds and attaches the collection tarball, commits the changelog back with `[skip ci]`, and builds and pushes every container image. `galaxy.yml`'s `version:` stays `0.0.0` in source. Commits must follow Conventional Commits. The release steps, the `[skip ci]` reasoning, the tool images and the deploy key the release push needs are in [docs/releases.md](docs/releases.md).

## Architecture

ARC's `gha-runner-scale-set` on a k3s cluster (embedded-etcd HA across three `server`-role hosts) that spans arm64 and amd64 hosts on different networks over Tailscale or Headscale; one fresh pod per job. Each part has its own page:

- [Architecture and Tailscale](docs/architecture.md): why the design replaced fixed Compose containers, HA, the mesh, the Tailscale DNS trap, the join key.
- [Heartbeat and fleet-down notification](docs/heartbeat.md): the health gist `runner-fallback-action` reads, `fleet-health.json`, `scripts/fleet-alert.sh`, where the check must run.
- [Autoscaler](docs/autoscaler.md): the combined `maxRunners` ceiling, pressure handling, dry-run default, Helm upgrades of scale sets.
- [Node recovery](docs/node-recovery.md): what happens when a node's host stops, pruning, the recovery image.
- [Role controller and voter swap](docs/role-controller.md): the probe, fitness rules, regrading, the swap procedure and its status.

## Conventions

Adding a second org, or a repository, is additive, never a change to anything existing:

1. Add a values file for the new org or repository to the fleet's `github_runner_arc_values_dir` (an existing org's is a starting point), or leave `values_file` out of the profile and use the ARC role's own values template.
2. Add another entry to the installing host's `github_runner_arc_orgs` in its `host_vars/<host>.yml` (with `repository` for one repository's runners), with a `private_key` holding that entry's App private key from any source Ansible reads, or a `private_key_op_reference` pointing at it in 1Password.

The controller install stays shared: it watches all namespaces by default, so one install serves every org's and repository's runner scale set release. No changes to the `Dockerfile`, the controller install, or any other entry's release are needed to add one.

The role must stay idempotent — every step uses `kubernetes.core.k8s` (server-side apply) or `helm upgrade --install` semantics, never a plain `create`, so reruns never fail on an already-existing resource. `bootstrap.sh` must stay a thin translation from `.env.bootstrap` to role variables: any deployment step added there instead of in the role reintroduces a second implementation that drifts.

`Dockerfile` must not set its own `ENTRYPOINT`/`CMD`, and must not touch the base image's `runner` user or working directory. The ARC Helm chart's pod template supplies `command: ["/home/runner/run.sh"]` itself; it only needs the toolchain layered on top.

The k3s node's `docker-compose.yml` and `k3s/` build context live only in `roles/github_runner_cluster/files/`. Keep `github_runner_cluster_compose_project` stable on a running host: the node's named volumes are prefixed with it, so a new name starts a new, empty node.

Commits follow Conventional Commits (`fix:`, `feat:`, `tune:`, and so on).

## Non-obvious behaviour

Each entry is a trap with a diagnosis and fix in [docs/non-obvious-behaviour.md](docs/non-obvious-behaviour.md); read the entry before acting on the symptom.

- **A scale-set's listener pod can crash-loop forever with `ephemeralrunnersets... not found` after a Helm upgrade, even though the release itself succeeds.**
- **k3s cannot overwrite an existing kubeconfig through Colima's macOS share.**
- **Pods can lose every cluster service IP, cluster DNS included, whenever the Colima Linux VM has not loaded `br_netfilter` (a restarted VM, after a host reboot for example).**
- **The k3s node name must stay stable across container recreations.**
- **`kubectl get nodes` can succeed with zero nodes.**
- **Zero runner pods in an `arc-runners-<org>` namespace (or its per-profile `-<suffix>` variant) when idle is expected, not a bug**
- **An org's Settings > Actions > Runners page shows the runner scale set itself**
- **An autoscaled profile's static `maxRunners` and per-pod resource limits have to come from the pool's measured capacity**
- **The metrics API's pod list and `kubectl top nodes` require metrics-server.**
- **A replaced node can stay permanently `NotReady` with "Node password rejected" in its logs.**
- **etcd's own peer traffic does not follow vpn-auth's `--node-ip`/`--advertise-address` the way every other kind of cluster traffic does - it stays on the container's own internal, cross-machine-unroutable Docker-bridge IP by default.**
- **A global `--etcd-arg` override is not scoped to the "real" server's own etcd instance - it also applies to k3s's separate, internal "temporary etcd", spun up on every *restart* (never on first boot) to reconcile with an already-initialized on-disk datastore before the real instance starts.**
- **Calling `tailscale up --authkey=...` unconditionally on every container restart re-registers a brand new tailnet device each time, instead of reconnecting the persisted one - even with the join key's own state volume intact.**
- **Tailscale assigns an IP and wires up its own MagicDNS resolver as two separate steps, not atomically - a node having a real IP is not proof DNS resolution against another tailnet member is working yet.**
- **A server that memory-pressure regrading has stepped to `etcd-only` is `NotReady` by design, and must stay registered.**
- **A single remaining healthy etcd server can get stuck indefinitely if another member is registered but permanently unreachable (e.g. a partial join that never completed), even though that member is a non-voting learner and shouldn't block quorum.**

## References

Further detail lives in `docs/`: [ansible.md](docs/ansible.md) (roles, server election, profiles, host prerequisites, Unraid, capabilities, registry cache), [build-and-test.md](docs/build-and-test.md) (builds, smoke tests, canaries, lint), [releases.md](docs/releases.md) (release steps and the deploy key), [architecture.md](docs/architecture.md) (design and Tailscale), [heartbeat.md](docs/heartbeat.md) (health gist and alerts), [autoscaler.md](docs/autoscaler.md), [node-recovery.md](docs/node-recovery.md), [role-controller.md](docs/role-controller.md) (voter regrading and swap), [non-obvious-behaviour.md](docs/non-obvious-behaviour.md) (read when a cluster, mesh or etcd symptom is odd). Each role also has its own README under `roles/`.

- [`ExaDev/runner-fallback-action`](https://github.com/ExaDev/runner-fallback-action) — reads the heartbeat gist; its `docs/spec.md` documents the routing algorithm.
- [`actions/actions-runner-controller`](https://github.com/actions/actions-runner-controller) — upstream ARC project.
- [`actions/actions-runner-controller` gha-runner-scale-set-controller docs](https://github.com/actions/actions-runner-controller/blob/master/docs/gha-runner-scale-set-controller/README.md) — the `AutoscalingRunnerSet` CRD (`actions.github.com/v1alpha1`) `scripts/autoscaler.sh` reads and patches.
