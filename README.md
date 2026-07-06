# ExaDev GitHub Actions Runner

Self-hosted GitHub Actions runner fleet for the ExaDev org, running on Apple Silicon (arm64) via GitHub's own [Actions Runner Controller](https://github.com/actions/actions-runner-controller) (ARC) on a single-node Kubernetes cluster. ARC gives genuinely ephemeral, autoscaling runners natively - no custom scripts for either concern.

**This repo is unrelated to `~/github-runner` on this same machine.** That directory is an earlier, deprecated attempt and is broken; nothing here reuses its code, config, or containers. Do not look there for context on this setup.

## Why Kubernetes/ARC instead of a plain Docker Compose fleet

An earlier version of this repo ran a fixed number of long-lived containers via Docker Compose, cycling the GitHub runner *registration* between jobs (`EPHEMERAL=true`) but never resetting the container's own filesystem. Two real problems followed from that:

1. **No true ephemerality.** Build artifacts, `node_modules`, and Playwright browser caches accumulated indefinitely across jobs inside the same container, filling the host's entire Docker disk allocation.
2. **No autoscaling.** A fixed replica count meant real queueing whenever more jobs were in flight than there were replicas, with no way to burst beyond it.

ARC's `gha-runner-scale-set` is ephemeral by design (one fresh pod per job, deleted after) and autoscales on queue depth - both problems solved by the tool itself, not by anything bespoke here. It needs a real Kubernetes cluster to run in, but [k3s ships an official Docker image](https://hub.docker.com/r/rancher/k3s) that runs the whole control plane + kubelet inside one privileged container - a well-established "cluster in a box" pattern. That's what makes the whole setup **exactly one service in `docker-compose.yml`**, portable to any machine with Docker (plus `helm`/`kubectl` on the host to drive it) - no Colima-specific flags, no separately-installed `kind`/`k3d` CLI.

The custom runner toolchain (GCC 11/G++ for C++20, Bun, the `gh` CLI) carries over from the previous design, rebased onto ARC's own [`actions/actions-runner`](https://github.com/actions/runner/pkgs/container/actions-runner) base image instead of `catthehacker/ubuntu:act-latest` - ARC's runner pods use their own registration flow, so the previous design's vendored [myoung34/docker-github-actions-runner](https://github.com/myoung34/docker-github-actions-runner) scripts (registration, GitHub App JWT signing, ephemeral re-registration) are no longer needed at all.

## Setup

1. **Create a GitHub App** on the ExaDev org with **Self-hosted runners: read & write** permission, and install it on the org (the same App from the previous design can be reused).
2. **Configure the environment.** Copy `.env.arc.example` to `.env.arc` and fill in:
   - `K3S_TOKEN` - any random string (`openssl rand -hex 32`), used only for node-join auth inside the cluster
   - `EXADEV_APP_ID`, `EXADEV_APP_PRIVATE_KEY_PATH` (a PEM file path, e.g. under `./secrets/`, already gitignored)
   - `EXADEV_APP_INSTALLATION_ID` - optional; `bootstrap.sh` resolves it automatically from the App ID and key if left blank
   - `GHCR_PULL_USERNAME`, `GHCR_PULL_TOKEN` - a GHCR personal access token (`read:packages`) to pull the private runner image
3. **Build and push the runner image** (via `.github/workflows/build-runner-image.yml`, runs on GitHub-hosted `ubuntu-latest` - building the fleet's own image on the fleet itself would be circular) or build it locally for testing: `docker build -t ghcr.io/exadev/github-runner:latest .`
4. **Bootstrap everything**:
   ```bash
   ./bootstrap.sh
   ```
   This starts k3s, waits for it to be ready, installs the ARC controller, resolves the App installation ID if needed, creates the GitHub App and GHCR pull secrets, and installs the ExaDev runner scale set. Safe to rerun any time - e.g. after rotating the App's private key.

## Adding another org

The controller install is shared (it watches all namespaces by default). Adding a second org is additive, not a change to anything existing:

1. Copy `.env.arc.example` to `.env.<neworg>` and fill in that org's App credentials.
2. Add a `values/<neworg>-runners-values.yaml` (copy `values/exadev-runners-values.yaml` as a starting point).
3. Add an `install_org` call for `<neworg>` at the bottom of `bootstrap.sh`, sourcing `.env.<neworg>`.

No changes to the Dockerfile, the controller install, or any other org's release are needed.

## Verification

```bash
kubectl get nodes                              # one Ready node
kubectl get pods -n actions-runner-controller  # controller pod AND the scale set's listener pod both run here
kubectl get pods -n arc-runners-exadev          # zero runner pods when idle is expected, not a bug
```

The ExaDev org's **Settings > Actions > Runners** shows the runner scale set itself, rather than individual long-lived runner names the way the previous design did - ARC's ephemeral runners don't persist between jobs, so there's nothing to list when idle.

To confirm ephemeral-pod-per-job is actually working: run `.github/workflows/test-arc-runner.yml` (`gh workflow run test-arc-runner.yml`) and watch `kubectl get pods -n arc-runners-exadev -w` - a pod should appear only once the job is queued, run to completion, and be deleted within seconds. Unlike the previous design, no disk usage should accumulate on the k3s container/host across repeated runs (`colima ssh -- df -h /mnt/lima-colima` should stay flat).

## Heartbeat: how `runner-fallback-action` knows this fleet is healthy

ARC's autoscaling means there is normally **no pre-existing "online runner"** to check the way the previous design's fallback logic did (query `/orgs/{org}/actions/runners` for an online match) - with `minRunners: 0`, pods only exist while a job is actually running. To still support falling back to `ubuntu-latest` if this Mac/k3s/ARC itself goes down, `scripts/heartbeat.sh` runs every few minutes (via `launchd/com.exadev.github-runner.heartbeat.plist`), checks that the k3s node and ARC controller are healthy, and if so pushes a fresh `ARC_HEALTHY_UNTIL` timestamp to an ExaDev org-level Actions variable. [`exadev/runner-fallback-action`](https://github.com/ExaDev/runner-fallback-action) checks that timestamp instead of querying for an online runner - see that repo's `docs/spec.md` for the addendum describing this.

Install the heartbeat:
```bash
cp launchd/com.exadev.github-runner.heartbeat.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.exadev.github-runner.heartbeat.plist
```
