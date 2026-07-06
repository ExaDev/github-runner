# ExaDev GitHub Actions Runner

Self-hosted GitHub Actions runner for the ExaDev org, running natively on Apple Silicon (arm64) via Docker Compose. It aims to approximate GitHub's hosted `ubuntu-latest` runner as closely as practical, rather than reimplementing runner registration from scratch.

**This repo is unrelated to `~/github-runner` on this same machine.** That directory is an earlier, deprecated attempt and is broken; nothing here reuses its code, config, or containers. Do not look there for context on this setup.

## Fidelity tradeoff

GitHub's real `ubuntu-latest` image is VM-shaped: it depends on systemd and a full boot sequence, and GitHub's own [runner-images](https://github.com/actions/runner-images) maintainers have said it cannot be faithfully containerized. Rather than fight that, this repo runs jobs inside [`catthehacker/ubuntu:act-latest`](https://github.com/catthehacker/docker_images), the [nektos/act](https://github.com/nektos/act) project's arm64-native approximation of the hosted image (roughly 500MB).

The alternative, `catthehacker/ubuntu:full-latest`, is a much closer software match to the real hosted image but is amd64-only and roughly 75GB, which on Apple Silicon means running everything under emulation. For day-to-day CI that tradeoff isn't worth it, so `act-latest` is the default here. If a workflow needs something only `full-latest` provides, that's a deliberate exception to make per-workflow, not the baseline.

Runner registration itself, org scoping, GitHub App installation-token refresh, and ephemeral re-registration, is vendored from [myoung34/docker-github-actions-runner](https://github.com/myoung34/docker-github-actions-runner) rather than reimplemented here. That project already solves the fiddly parts of talking to GitHub's runner API correctly; this repo just wraps it in an image and compose setup tuned for arm64 and for ExaDev.

## Setup

1. **Create a GitHub App** on the ExaDev org with **Self-hosted runners: read & write** permission, and install it on the org.
2. **Configure the environment.** Copy `.env.exadev.example` to `.env.exadev` and fill in:
   - `APP_ID` - the GitHub App's ID
   - `APP_PRIVATE_KEY` - the App's PEM private key
   - `APP_LOGIN=ExaDev`
3. **Build the image.**
   ```bash
   docker compose build
   ```
4. **Start runners.**
   ```bash
   docker compose up -d --scale exadev=2
   ```
   Adjust the replica count to however many concurrent runners you want registered.

## Adding another org

The Dockerfile and image are org-agnostic; everything org-specific lives in the env file and the compose service block. To register runners for a second org:

1. Copy `.env.exadev.example` to `.env.<neworg>` and fill in that org's App credentials.
2. Add a new service block to `docker-compose.yml` for `<neworg>`, pointing it at `.env.<neworg>` via `env_file`.
3. Start it:
   ```bash
   docker compose up -d <neworg>
   ```

No changes to the Dockerfile, the existing ExaDev service, or any other org's config are needed.

## Verification

After `docker compose up`, confirm the runners registered under the org's **Settings > Actions > Runners**. Each should show as online with labels `self-hosted`, `linux`, `arm64`, `act-latest`, `node-b`.

Runners are started with `EPHEMERAL=true`, so each container handles exactly one job and then de-registers; Docker Compose immediately restarts the container, which re-registers a fresh one-shot runner. A steady turnover of runner names in the org's runner list (rather than the same names persisting indefinitely) is expected behaviour, not a bug.
