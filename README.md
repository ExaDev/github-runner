# ExaDev GitHub Actions Runner

> Self-hosted GitHub Actions runner fleet for the ExaDev org. Runs on Apple Silicon (arm64) and Intel (amd64) hosts via GitHub's own [Actions Runner Controller](https://github.com/actions/actions-runner-controller) (ARC) on a Kubernetes cluster spanning both, joined over Tailscale.

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

`ansible/` deploys this same fleet from a control machine over SSH, instead of running `bootstrap.sh` locally on the target Mac. It is self-contained: its own inventory, its own `ansible.cfg`, and its own requirements, with no dependency on any other Ansible repo — anyone who clones `ExaDev/github-runner` alone can run it. Verified end to end against real infrastructure (both hosts, a real primary/agent cutover, real jobs scheduling on both) - this is the preferred path now, not just a draft alternative to `bootstrap.sh`.

```bash
cd ansible
ansible-galaxy collection install -r requirements.yml
ansible-playbook playbook.yml --limit node-a --check   # dry run
ansible-playbook playbook.yml --limit node-a           # real run
```

Every secret (the App private key, GHCR credentials, the heartbeat PAT, `K3S_TOKEN`) is read live from 1Password via `op read` at task time, never stored in the role or in ansible-vault. `ansible/roles/github_runner_arc/defaults/main.yml` documents the `op://` references and the shapes each host's own `ansible/host_vars/<host>.yml` sets; `ansible/host_vars/node-a.yml` has a real, working example of every variable the role uses, including a multi-profile org. The role templates a real `.env` at the repo root from these values, so `docker-compose.yml` itself needs no changes and keeps working unmodified if you fall back to running `bootstrap.sh` directly.

Two node roles, not one: `github_runner_arc_node_role` selects between `primary` (the full stack - k3s server, heartbeat, the ARC controller, every org's runner scale sets) and `agent` (joins an existing primary's cluster purely as a worker, so that host's own CPU/RAM become schedulable for runner pods too, with no control plane or org install of its own). Add a host to `ansible/inventory.yml` and give it its own `ansible/host_vars/<host>.yml` to add another agent, or a second org on the primary.

`github_runner_arc_orgs[].scale_set_profiles` is a list of `{suffix, values_file, node_selector?, runs_on_label?}` entries - one Helm release and one namespace per entry, pooled under the org's own shared `runs-on` label via `scaleSetLabels` (not `runnerScaleSetName`, which is a scale set's own unique GitHub-side registration identity, not just a label - two releases sharing it collide outright) unless a profile sets its own `runs_on_label` to deliberately opt out of that pool - e.g. a dedicated image-building profile ordinary CI jobs have no business landing on. `node_selector` is optional per profile, not mandatory: omitting it leaves that profile's pods genuinely unpinned, letting the Kubernetes scheduler place them on whichever node in the fleet actually has room - real failover if one machine goes down, not just capacity pooling. See `ansible/host_vars/node-a.yml` for ExaDev's own two-profile setup: the ordinary CI profile is unpinned across the whole fleet (sized with Guaranteed QoS for the smallest machine's real capacity - see `values/exadev-runners-values.yaml`'s own derivation comments for why, and the real trade-off that accepts), and a second, separately-labelled, Docker-in-Docker profile stays pinned to `node-b` for building the fleet's own runner image - a deliberate, unrelated isolation choice, not a capacity constraint.

## Build, test, and smoke-test

- **Build the runner image locally (the only working path right now):** `docker buildx build --platform linux/arm64,linux/amd64 --push -t ghcr.io/exadev/github-runner:latest .` from a machine with buildx/QEMU cross-platform support (Docker Desktop has this built in). A plain `docker build` with no `--platform` only produces an image for the building machine's own architecture, which silently breaks scheduling on the fleet's other architecture.
- **Build and push via CI:** `.github/workflows/build-runner-image.yml` runs on GitHub-hosted `ubuntu-latest`, which this org cannot currently schedule at all - every run of this workflow has failed instantly with no runner ever assigned (see issue #10). Don't rely on this until that's fixed.
- **Smoke-test the ARC scale set** (confirms a job gets a real ephemeral pod): `gh workflow run test-arc-runner.yml`, then watch `kubectl get pods -n arc-runners-exadev -w`. A pod must appear only once the job is queued, run to completion, and be deleted within seconds.
- **Smoke-test `ubuntu-latest` routing** (confirms `runner-fallback-action` still reaches GitHub-hosted runners when needed): `gh workflow run test-ubuntu-latest.yml`.
- **Generate load for the autoscaler** (see [Autoscaler](#autoscaler) below): `gh workflow run test-autoscaler.yml`.
- **Verify cluster health:**
  ```bash
  kubectl get nodes                              # must show one Ready node
  kubectl get pods -n actions-runner-controller  # controller pod and the scale set's listener pod both run here
  kubectl get pods -n arc-runners-exadev          # zero runner pods when idle is expected, not a bug
  ```
- **Confirm no disk accumulates across repeated runner jobs:** `colima ssh -- df -h /mnt/lima-colima` must stay flat across repeated `test-arc-runner.yml` runs.
- **Lint:** `.github/workflows/ci.yml` runs Shellcheck, actionlint, yamllint, and hadolint on every push and pull request, gated by one `required-checks` job so a repository ruleset only ever has to require that one check regardless of how many lint jobs exist. hadolint's warning-level pinning advice is visible but non-blocking (`--failure-threshold error`): this repo deliberately floats several images/packages on `latest` rather than pinning. yamllint's config lives in `.yamllint`, relaxing the three default rules that conflict with this project's own conventions (long single-line comments, no leading `---`, GitHub Actions' `on:` key).

There is no application test suite in this repo; the checks above are the project's actual verification surface.

## Architecture

An earlier version of this repo ran a fixed number of long-lived containers via Docker Compose, cycling the GitHub runner *registration* between jobs (`EPHEMERAL=true`) but never resetting the container's own filesystem. Two problems followed: build artifacts, `node_modules`, and Playwright browser caches accumulated indefinitely inside the same container and filled the host's Docker disk allocation, and a fixed replica count meant jobs queued whenever more were in flight than there were replicas, with no way to burst beyond it.

ARC's `gha-runner-scale-set` solves both by design: it runs one fresh pod per job and deletes it afterwards, and it autoscales on queue depth. ARC needs a real Kubernetes cluster to run in. [k3s ships an official Docker image](https://hub.docker.com/r/rancher/k3s) that runs the whole control plane and kubelet inside one privileged container, a "cluster in a box". This makes the cluster itself just one or two Compose services (`k3s` on a primary host, `k3s-agent` on a second host joining it - see the [Ansible](#ansible) section's node-role split), portable to any machine with Docker (plus `helm` and `kubectl` on the host) — no Colima-specific flags, no separately installed `kind` or `k3d` CLI. ARC itself (the controller, the runner scale sets, and the ephemeral runner pods) is not more Compose services; it is Kubernetes-native resources installed via Helm into this cluster — see `bootstrap.sh`.

Two physical hosts on two different home networks can't reach each other's LAN IPs directly, so cross-node cluster traffic (a second node joining, cross-node pod-to-pod traffic, the API server's proxy to a remote kubelet) needs a real overlay between them - see [Tailscale](#tailscale) below for how that overlay is built and the DNS pitfall it introduces.

The custom runner toolchain (GCC 11/G++ for C++20, Bun, the `gh` CLI — see `Dockerfile`) carries over from the previous design, rebased onto ARC's own [`actions/actions-runner`](https://github.com/actions/runner/pkgs/container/actions-runner) base image instead of `catthehacker/ubuntu:act-latest`. ARC's runner pods use their own registration flow, so the previous design's vendored [myoung34/docker-github-actions-runner](https://github.com/myoung34/docker-github-actions-runner) scripts (registration, GitHub App JWT signing, ephemeral re-registration) are not needed.

`.github/workflows/build-runner-image.yml` is meant to build and push the runner image on GitHub-hosted `ubuntu-latest`, deliberately not on this fleet itself, since building the fleet's own image on the fleet would be circular - but this org can't currently schedule `ubuntu-latest` at all, so every run of it has failed instantly (see [issue #10](https://github.com/ExaDev/github-runner/issues/10)). See [Build, test, and smoke-test](#build-test-and-smoke-test) for the actual current path.

`.github/workflows/ci.yml` dogfoods the fleet instead: its lint jobs prefer `exadev-runners`, routed through [`ExaDev/runner-fallback-action`](https://github.com/ExaDev/runner-fallback-action) so a run falls back to `ubuntu-latest` if the fleet's heartbeat reports it unhealthy. This is safe precisely because lint jobs (unlike the image build above) don't need the fleet to already be working to run — there's no circularity.

### Tailscale

Each node's k3s process joins a Tailscale tailnet as its own device, using k3s's own built-in (experimental) [`--vpn-auth` integration](https://docs.k3s.io/networking/distributed-multicloud): `--vpn-auth="name=tailscale,joinKey=<key>"` on both `k3s server` and `k3s agent`. This runs Tailscale *inside* the k3s container/process itself, separate from the host machine's own Tailscale identity if it has one, and flannel uses Tailscale's own subnet-route advertisement for pod-to-pod traffic instead of its usual VXLAN encapsulation - no manual `--node-ip`, no Docker port-publishing, no host-level Tailscale dependency at all. `k3s/Dockerfile` layers the `tailscale`/`tailscaled` binaries from the official Tailscale image onto `rancher/k3s`'s own minimal base (which has no package manager to install them at runtime), and `docker-compose.yml`'s entrypoint starts `tailscaled` itself before execing k3s, since k3s's vpn-auth integration only runs `tailscale up` and assumes the daemon is already running.

Provisioning the join key: create a reusable, pre-authorized Tailscale auth key (Tailscale admin console → Settings → Keys, or the `POST /api/v2/tailnet/-/keys` API), tag it so an ACL grant can target it, and set `K3S_VPN_AUTH_JOIN_KEY` in every host's `.env` (or, via Ansible, store it as the `tailscale-join-key` field on the shared 1Password item - see `ansible/roles/github_runner_arc/defaults/main.yml`). The tailnet's ACL needs a `grants` entry permitting traffic between the nodes' own tag *and* the pod CIDR itself (e.g. `10.42.0.0/16`), not just the tag - Tailscale evaluates a subnet-routed packet's actual embedded source IP, not just the sending device's identity, so a grant scoped to the tag alone silently blocks cross-node pod traffic ([k3s-io/k3s#8372](https://github.com/k3s-io/k3s/issues/8372)).

**Tailscale's own DNS management is a real trap here.** By default (`--accept-dns=true`, Tailscale's own default), `tailscale up` overwrites the container's `/etc/resolv.conf` with MagicDNS's resolver (a fixed, well-known `100.100.100.100`) plus whatever search domains the tailnet's own DNS settings configure - which can include search domains meant for entirely unrelated, personal use of the same tailnet. Kubernetes' `dnsPolicy: ClusterFirst` then copies the *node's* search domains into every pod's own `/etc/resolv.conf` alongside the cluster's own three, and the default `ndots:5` means a pod's resolver tries every search-suffixed name before the bare hostname for anything with fewer than 5 dots - which is essentially every real-world hostname. If any of those inherited search domains carries a wildcard DNS record, a pod's lookup of a real external hostname can resolve to that wildcard's target instead, while the application still sends the original hostname as its TLS SNI - silent certificate-verification failures for arbitrary outbound HTTPS traffic, indistinguishable at a glance from a hung network. Both `k3s server`/`k3s agent` invocations set `--resolv-conf` to a synthetic file containing only `nameserver 100.100.100.100` and no search domains, so pods still resolve everything correctly through Tailscale's own DNS proxy without inheriting a search list they have no use for.

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

### Autoscaler

`values/exadev-runners-values.yaml`'s `maxRunners: 3` is the mathematically safe ceiling for the currently-unpinned pool's own real worst case, proven against a real out-of-memory and pod-eviction incident (see that file's own comments) and re-derived for the combined-fleet, Guaranteed-QoS design that replaced node pinning. It leaves real idle capacity unused whenever the actual jobs in flight need less than that. The `autoscaler` service raises and lowers `maxRunners` on the single `exadev-runners` `AutoscalingRunnerSet` based on real, current memory usage and host pressure, never on job identity or GitHub Actions queue depth: every consuming repo's CI keeps using the single `exadev-runners` label unchanged, and no job is ever classified as light or heavy.

Each poll (`AUTOSCALER_POLL_SECONDS`, default 45s):
- Reads the currently-running count (`status.currentRunners`) and the per-pod hard memory limit (`spec.template.spec.containers[0].resources.limits.memory`) straight from the live `AutoscalingRunnerSet`, never a static config value, so both always match what is actually deployed.
- Sums real, current memory usage of the running runner pods (`kubectl top pod -n arc-runners-exadev`) — this, not a static per-pod request or limit, is the entire point of "usage-driven".
- Reads host/VM memory and swap pressure from `/proc/meminfo` inside the container. **Not** macOS's `vm_stat`/`sysctl vm.swapusage`: those are macOS-native and cannot run inside any Linux container, even a host-networked one — a container only ever shares a network namespace with the host that way, never its process or binary environment. `/proc/meminfo` inside an unprivileged, non-cgroup-virtualized container reflects the Colima VM's own real memory instead — this is measured per-node where the autoscaler container itself runs (the primary), not the whole pool, so it's one signal among the checks here, not the full picture of a genuinely multi-node fleet. This needs confirming empirically on the first real dry run, same as the `kubectl top pod` (metrics-server) assumption above.
- Raises `maxRunners` by exactly +1 per cycle (never straight to a computed target) once headroom has covered a full pod's hard limit for `AUTOSCALER_RAISE_CONFIRM_POLLS` (default 2) consecutive polls, capped at `AUTOSCALER_MAX_CEILING` (default 7 — the real bin-packing-safe ceiling across both nodes at the values file's own 3Gi Guaranteed-QoS pod size, not an arbitrary cap; see that file's own derivation).
- Lowers immediately, with no delay or averaging, the moment headroom drops below a pod's hard limit or real host pressure is detected — lowering never disrupts in-flight jobs, since ARC only gates new claims. Never patches below the currently-running count.
- Fails safe on any measurement error (`kubectl` unreachable, `/proc/meminfo` unreadable): patches down to `AUTOSCALER_FLOOR` (default 3, matching the values file's own static `maxRunners`) immediately rather than skipping the cycle silently.

Ships with `AUTOSCALER_DRY_RUN=true` by default: it computes and logs the target and writes a status file, but never patches. Generate real concurrent traffic to watch it react: `gh workflow run test-autoscaler.yml`, then watch `docker compose logs -f autoscaler` and `logs/autoscaler/autoscaler-status.json`. Only set `AUTOSCALER_DRY_RUN=false` in `.env` after watching real dry-run output across genuine CI traffic. `scripts/heartbeat.sh` republishes that same status file as a second file in the heartbeat gist, reusing its existing gist-write credential rather than giving the autoscaler its own.

`bootstrap.sh`'s `install_org()` triggers one immediate autoscaler poll after every Helm upgrade, so the safe-floor window after a redeploy is seconds, not a full poll interval.

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

- **A scale-set's listener pod can crash-loop forever with `ephemeralrunnersets... not found` after a Helm upgrade, even though the release itself succeeds.** ARC names the `EphemeralRunnerSet` it creates for a release with a spec hash suffix, and gives it a new name whenever the pod template changes (a `helm upgrade` that touches resource limits, `nodeSelector`, or similar). The `AutoscalingListener` is supposed to track this rename, but can be left pointing at the old, now-deleted name - it then restarts every second or two, each time failing at startup with `could not patch ephemeral runner set <old-name>... not found`, and the scale set's own job queue never gets serviced even though every other release's listener stays healthy. Diagnose with `kubectl get autoscalinglisteners -n actions-runner-controller -o custom-columns='NAME:.metadata.name,ERS:.spec.ephemeralRunnerSetName'` against `kubectl get ephemeralrunnersets -A` - a mismatch confirms it. Fix: `kubectl delete autoscalinglistener <name> -n actions-runner-controller`; the controller recreates it immediately with the current `EphemeralRunnerSet` name.
- **k3s cannot overwrite an existing kubeconfig through Colima's macOS share.** The container-root-to-Mac-user file mapping leaves `/output/kubeconfig.yaml` owner-read-only, so k3s crash-loops with "permission denied" on every restart (for example after a Mac reboot) unless the file is removed first. `docker-compose.yml`'s `k3s` entrypoint deletes it before starting k3s each time — do not remove that step.
- **The k3s node name must stay stable across container recreations.** Without a pinned `hostname`, k3s derives the node name from the container's hostname (its ID), so every recreation (for example on an image pull) registers a new node and leaves the old one stale in etcd. `docker-compose.yml` pins `hostname: k3s-server` for this reason.
- **`kubectl get nodes` can succeed with zero nodes.** It returns an empty, still-successful list before the node object has registered. `bootstrap.sh` polls for at least one node before calling `kubectl wait`, which fails outright ("no matching resources found") against zero nodes.
- **Zero runner pods in an `arc-runners-<org>` namespace (or its per-profile `-<suffix>` variant) when idle is expected, not a bug** — `minRunners: 0` means pods exist only while a job is running.
- **The ExaDev org's Settings > Actions > Runners page shows the runner scale set itself**, not individual long-lived runner names the way the previous design did. ARC's ephemeral runners do not persist between jobs, so there is nothing to list while idle.
- **`values/exadev-runners-values.yaml`'s `maxRunners` and per-pod resource limits are tuned from a real out-of-memory and pod-eviction incident**, not arbitrary defaults — read that file's comments in full before changing either value; a higher `maxRunners` without matching headroom reproduces the same failure. `scripts/autoscaler.sh` depends on this value staying the safe floor it reverts to.
- **The autoscaler cannot run macOS's own `vm_stat`/`sysctl` from inside its container**, even with `network_mode: host` — that only shares the network namespace, not the host's own binaries. It reads `/proc/meminfo` instead; confirm during the first real dry run that this genuinely reflects the Colima VM's pressure and not some other, less useful view.
- **`kubectl top pod` requires metrics-server.** k3s bundles it by default (this repo's `docker-compose.yml` only disables `traefik`), but confirm it is actually running during the first real autoscaler dry run — `scripts/autoscaler.sh` fails safe to the static floor if it is not. A very short-lived job (seconds, not minutes) can complete and its pod be garbage-collected before metrics-server's own scrape interval ever captures its usage - confirmed live against `test-arc-runner.yml`'s ~15-20s jobs, where `kubectl top pod` genuinely never returns data. This correctly triggers the fail-safe rather than a wrong reading, and doesn't matter in practice: a job that short never accumulates meaningful memory pressure anyway. `test-autoscaler.yml`'s own probe jobs hold for 120s specifically so metrics-server has time to observe them.
- **Recreating a `k3s-agent` container can leave it permanently `NotReady` with "Node password rejected" in its logs.** `/etc/rancher/node/password` lives inside the container's own writable filesystem, not a persisted volume, so a fresh container generates a brand-new random password - but the primary's own server still holds the *previous* container's password for that node name, stored as a Kubernetes Secret (`kube-system/<node>.node-password.k3s`), and rejects the mismatched rejoin. Fix: on the primary, `kubectl delete secret <node>.node-password.k3s -n kube-system && kubectl delete node <node>`, then restart the agent container so it registers fresh.
- **`ci.yml`'s `determine-runner` job is deliberately anchored on `exadev-runners` directly, not `ubuntu-latest`.** This is the fleet's own repo, so its CI dogfoods the fleet fully rather than hedging on it — unlike a consuming repo (for example `ExaDev/spot-of-the-day`), where `ubuntu-latest` is the recommended anchor so a fully-down fleet can still fall back. The trade-off here is accepted, not a workaround: if the fleet is entirely down, `determine-runner` cannot schedule and CI cannot fall back to `ubuntu-latest` either.

## References

- [`ExaDev/runner-fallback-action`](https://github.com/ExaDev/runner-fallback-action) — reads the heartbeat gist; its `docs/spec.md` documents the routing algorithm.
- [`actions/actions-runner-controller`](https://github.com/actions/actions-runner-controller) — upstream ARC project.
- `values/controller-values.yaml`, `values/exadev-runners-values.yaml` — Helm values for the ARC controller and the ExaDev runner scale set, including the resource-tuning rationale referenced above.
- [`actions/actions-runner-controller` gha-runner-scale-set-controller docs](https://github.com/actions/actions-runner-controller/blob/master/docs/gha-runner-scale-set-controller/README.md) — the `AutoscalingRunnerSet` CRD (`actions.github.com/v1alpha1`) `scripts/autoscaler.sh` reads and patches.
