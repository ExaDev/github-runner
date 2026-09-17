# ExaDev GitHub Actions Runner

> Self-hosted GitHub Actions runner fleet for the ExaDev org. Runs on Apple Silicon (arm64) via GitHub's own [Actions Runner Controller](https://github.com/actions/actions-runner-controller) (ARC) on a single-node Kubernetes cluster.

**This repo is unrelated to `~/github-runner` on this same machine.** That directory is an earlier, deprecated attempt and it is broken. Nothing here reuses its code, config, or containers. Do not look there for context on this setup.

## Getting started

Prerequisites on the host: Docker (or Colima) with the Compose plugin, `helm`, `kubectl`, `gh`, `jq`, and `openssl`.

1. **Create a GitHub App** on the ExaDev org with **Self-hosted runners: read & write** permission, and install it on the org.
2. **Configure the environment.** Copy `.env.example` to `.env` and fill in:
   - `K3S_TOKEN` — any random string (`openssl rand -hex 32`). It authenticates node-join inside the cluster only; it has no relation to GitHub.
   - `EXADEV_APP_ID`, `EXADEV_APP_PRIVATE_KEY_PATH` — the App ID and a path to its private key PEM file (for example under `./secrets/`, already gitignored).
   - `EXADEV_APP_INSTALLATION_ID` — optional. Leave it blank and `bootstrap.sh` resolves it automatically from the App ID and key.
   - `GHCR_PULL_USERNAME`, `GHCR_PULL_TOKEN` — a GHCR personal access token (`read:packages` scope) to pull the private runner image.
   - `HEARTBEAT_GH_TOKEN`, `HEARTBEAT_GIST_ID` — see [Heartbeat](#heartbeat) below.
3. **Build and push the runner image.** The fleet spans both `arm64` and `amd64` hosts, so this needs a real multi-arch push, not a plain `docker build`: `docker buildx build --platform linux/arm64,linux/amd64 --push -t ghcr.io/exadev/github-runner:latest .` (see [Build, test, and smoke-test](#build-test-and-smoke-test) for why this is currently the only working path, not just a manual fallback).
4. **Bootstrap everything:**
   ```bash
   ./bootstrap.sh
   ```
   This starts k3s, waits for it to be ready, installs the ARC controller, resolves the App installation ID if needed, creates the GitHub App and GHCR pull secrets, and installs the ExaDev runner scale set. It is safe to rerun at any time, for example after rotating the App's private key.

## Ansible

`ansible/` deploys this same fleet from a control machine over SSH, instead of running `bootstrap.sh` locally on the target Mac. It is self-contained: its own inventory, its own `ansible.cfg`, and its own requirements, with no dependency on any other Ansible repo — anyone who clones `ExaDev/github-runner` alone can run it.

```bash
cd ansible
ansible-galaxy collection install -r requirements.yml
ansible-playbook playbook.yml --limit node-b --check   # dry run
ansible-playbook playbook.yml --limit node-b           # real run
```

Every secret (the App private key, GHCR credentials, the heartbeat PAT, `K3S_TOKEN`) is read live from 1Password via `op read` at task time, never stored in the role or in ansible-vault. `ansible/roles/github_runner_arc/defaults/main.yml` documents the `op://` references and the `github_runner_arc_orgs` shape each host's own `ansible/host_vars/<host>.yml` sets; `ansible/host_vars/node-b.yml` has `node-b`'s already wired up. The role templates a real `.env` at the repo root from these values, so `docker-compose.yml` itself needs no changes and keeps working unmodified if you fall back to running `bootstrap.sh` directly.

Add a host to `ansible/inventory.yml` and give it its own `ansible/host_vars/<host>.yml` to run the fleet, or a second org, on another machine. The role no-ops entirely on any host without a populated `github_runner_arc_orgs`.

`bootstrap.sh` stays the proven, exact source of truth for these steps until the Ansible role has actually been run and verified end to end against real infrastructure (`docker ps` and `kubectl get pods -A` on the target host must match a `bootstrap.sh` run). Prefer Ansible once that verification has happened, since it is host-agnostic and needs no interactive shell on the target Mac.

## Build, test, and smoke-test

- **Build the runner image locally (the only working path right now):** `docker buildx build --platform linux/arm64,linux/amd64 --push -t ghcr.io/exadev/github-runner:latest .` from a machine with buildx/QEMU cross-platform support (Docker Desktop has this built in). A plain `docker build` with no `--platform` only produces an image for the building machine's own architecture, which silently breaks scheduling on the fleet's other architecture.
- **Build and push via CI:** `.github/workflows/build-runner-image.yml` runs on GitHub-hosted `ubuntu-latest`, which this org cannot currently schedule at all - every run of this workflow has failed instantly with no runner ever assigned (see issue #10). Don't rely on this until that's fixed.
- **Smoke-test the ARC scale set** (confirms a job gets a real ephemeral pod): `gh workflow run test-arc-runner.yml`, then watch `kubectl get pods -n arc-runners-exadev -w`. A pod must appear only once the job is queued, run to completion, and be deleted within seconds.
- **Smoke-test `ubuntu-latest` routing** (confirms `runner-fallback-action` still reaches GitHub-hosted runners when needed): `gh workflow run test-ubuntu-latest.yml`.
- **Verify cluster health:**
  ```bash
  kubectl get nodes                              # must show one Ready node
  kubectl get pods -n actions-runner-controller  # controller pod and the scale set's listener pod both run here
  kubectl get pods -n arc-runners-exadev          # zero runner pods when idle is expected, not a bug
  ```
- **Confirm no disk accumulates across repeated runner jobs:** `colima ssh -- df -h /mnt/lima-colima` must stay flat across repeated `test-arc-runner.yml` runs.

There is no application test suite or linter configured in this repo; the checks above are the project's actual verification surface.

## Architecture

An earlier version of this repo ran a fixed number of long-lived containers via Docker Compose, cycling the GitHub runner *registration* between jobs (`EPHEMERAL=true`) but never resetting the container's own filesystem. Two problems followed: build artifacts, `node_modules`, and Playwright browser caches accumulated indefinitely inside the same container and filled the host's Docker disk allocation, and a fixed replica count meant jobs queued whenever more were in flight than there were replicas, with no way to burst beyond it.

ARC's `gha-runner-scale-set` solves both by design: it runs one fresh pod per job and deletes it afterwards, and it autoscales on queue depth. ARC needs a real Kubernetes cluster to run in. [k3s ships an official Docker image](https://hub.docker.com/r/rancher/k3s) that runs the whole control plane and kubelet inside one privileged container, a "cluster in a box". This makes the whole setup exactly one service in `docker-compose.yml`, portable to any machine with Docker (plus `helm` and `kubectl` on the host) — no Colima-specific flags, no separately installed `kind` or `k3d` CLI. ARC itself (the controller, the runner scale sets, and the ephemeral runner pods) is not more Compose services; it is Kubernetes-native resources installed via Helm into this cluster — see `bootstrap.sh`.

The custom runner toolchain (GCC 11/G++ for C++20, Bun, the `gh` CLI — see `Dockerfile`) carries over from the previous design, rebased onto ARC's own [`actions/actions-runner`](https://github.com/actions/runner/pkgs/container/actions-runner) base image instead of `catthehacker/ubuntu:act-latest`. ARC's runner pods use their own registration flow, so the previous design's vendored [myoung34/docker-github-actions-runner](https://github.com/myoung34/docker-github-actions-runner) scripts (registration, GitHub App JWT signing, ephemeral re-registration) are not needed.

`.github/workflows/build-runner-image.yml` builds and pushes the runner image on GitHub-hosted `ubuntu-latest`, deliberately not on this fleet itself — building the fleet's own image on the fleet would be circular.

### Heartbeat

ARC's autoscaling means there is normally no pre-existing "online runner" to check the way the previous design's fallback logic did (querying `/orgs/{org}/actions/runners` for an online match); with `minRunners: 0`, pods exist only while a job is running. To still support falling back to `ubuntu-latest` if this Mac, k3s, or ARC itself goes down, the `heartbeat` service in `docker-compose.yml` checks every `HEARTBEAT_INTERVAL_SECONDS` (default 180s) that the k3s node and the ARC controller are healthy, and if so, refreshes a single secret gist with a unix timestamp set `HEARTBEAT_WINDOW_SECONDS` (default 600s) into the future. [`exadev/runner-fallback-action`](https://github.com/ExaDev/runner-fallback-action) reads that gist over the unauthenticated GitHub API and routes to `exadev-runners` while the timestamp is still fresh, or to `ubuntu-latest` otherwise — see that repo's `docs/spec.md` for the algorithm.

The heartbeat runs as a Compose service (Alpine plus `kubectl`, `curl`, and `jq`), not a macOS launchd job, so the whole fleet and its health reporter come up from one `docker compose up` with nothing host-specific. It uses host networking, so `kubectl` (with the host kubeconfig k3s generates) reaches the k3s API at `127.0.0.1:6443` directly, with no TLS-SAN or kubeconfig changes needed.

Create the secret gist once:
```bash
echo "$(($(date +%s) + 600))" | gh gist create --filename arc-healthy-until --desc "ExaDev ARC fleet health heartbeat" -
# put the hex id from the returned gist URL into .env as HEARTBEAT_GIST_ID,
# and into exadev/runner-fallback-action's action.yml as the gist-id default
```
Confirm it refreshes: `gh gist view "$HEARTBEAT_GIST_ID"` must show a timestamp near `now + 600`, advancing roughly every 3 minutes.

## Conventions

Adding a second org is additive, never a change to anything existing:

1. Copy `.env.example` to `.env.<neworg>` and fill in that org's App credentials.
2. Add `values/<neworg>-runners-values.yaml` (copy `values/exadev-runners-values.yaml` as a starting point).
3. Add an `install_org` call for `<neworg>` at the bottom of `bootstrap.sh`, sourcing `.env.<neworg>` — or, via Ansible, add another entry to the target host's `github_runner_arc_orgs` in its `ansible/host_vars/<host>.yml`.

The controller install stays shared: it watches all namespaces by default, so one install serves every org's runner scale set release. No changes to the `Dockerfile`, the controller install, or any other org's release are needed to add an org.

`bootstrap.sh` must stay idempotent — every step uses `kubectl apply` (via `--dry-run=client -o yaml | kubectl apply -f -`) or `helm upgrade --install`, never a plain `create`, so reruns never fail on an already-existing resource.

`Dockerfile` must not set its own `ENTRYPOINT`/`CMD`, and must not touch the base image's `runner` user or working directory. The ARC Helm chart's pod template supplies `command: ["/home/runner/run.sh"]` itself; it only needs ExaDev's toolchain layered on top.

Commits follow Conventional Commits (`fix:`, `feat:`, `tune:`, and so on).

## Non-obvious behaviour

- **k3s cannot overwrite an existing kubeconfig through Colima's macOS share.** The container-root-to-Mac-user file mapping leaves `/output/kubeconfig.yaml` owner-read-only, so k3s crash-loops with "permission denied" on every restart (for example after a Mac reboot) unless the file is removed first. `docker-compose.yml`'s `k3s` entrypoint deletes it before starting k3s each time — do not remove that step.
- **The k3s node name must stay stable across container recreations.** Without a pinned `hostname`, k3s derives the node name from the container's hostname (its ID), so every recreation (for example on an image pull) registers a new node and leaves the old one stale in etcd. `docker-compose.yml` pins `hostname: k3s-server` for this reason.
- **`kubectl get nodes` can succeed with zero nodes.** It returns an empty, still-successful list before the node object has registered. `bootstrap.sh` polls for at least one node before calling `kubectl wait`, which fails outright ("no matching resources found") against zero nodes.
- **Zero runner pods in `arc-runners-exadev` when idle is expected, not a bug** — `minRunners: 0` means pods exist only while a job is running.
- **The ExaDev org's Settings > Actions > Runners page shows the runner scale set itself**, not individual long-lived runner names the way the previous design did. ARC's ephemeral runners do not persist between jobs, so there is nothing to list while idle.
- **`values/exadev-runners-values.yaml`'s `maxRunners` and per-pod resource limits are tuned from a real out-of-memory and pod-eviction incident**, not arbitrary defaults — read that file's comments in full before changing either value; a higher `maxRunners` without matching headroom reproduces the same failure.

## References

- [`ExaDev/runner-fallback-action`](https://github.com/ExaDev/runner-fallback-action) — reads the heartbeat gist; its `docs/spec.md` documents the routing algorithm.
- [`actions/actions-runner-controller`](https://github.com/actions/actions-runner-controller) — upstream ARC project.
- `values/controller-values.yaml`, `values/exadev-runners-values.yaml` — Helm values for the ARC controller and the ExaDev runner scale set, including the resource-tuning rationale referenced above.
