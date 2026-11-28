# Custom ARC runner pod image. Based on the official actions-runner image
# (not catthehacker/ubuntu:act-latest, which was the old myoung34-based
# design's base) - ARC's gha-runner-scale-set chart supplies its own
# registration/entrypoint flow, so this only needs to layer ExaDev's actual
# toolchain requirements on top, never touch the base image's runner
# user/workdir setup, and never set its own ENTRYPOINT/CMD - the Helm
# chart's pod template supplies `command: ["/home/runner/run.sh"]` itself.
FROM ghcr.io/actions/actions-runner:latest

USER root
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc-11 g++-11 make unzip python3-venv lsof tmux zstd \
    && update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-11 100 \
    && update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-11 100 \
    && update-alternatives --install /usr/bin/cc cc /usr/bin/gcc-11 100 \
    && update-alternatives --install /usr/bin/c++ c++ /usr/bin/g++-11 100 \
    && rm -rf /var/lib/apt/lists/*

# The system packages Playwright's `install-deps chromium` installs for Ubuntu 24.04 (its fonts, Xvfb and the libraries Chromium links against), which GitHub's ubuntu-24.04 image has through its browsers, so an end-to-end job's `playwright install --with-deps` has nothing left to fetch. tests/runner_image/run.sh runs install-deps against the latest Playwright and fails when it would still install something, which is the signal to extend this list.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb fonts-noto-color-emoji fonts-unifont fonts-liberation fonts-ipafont-gothic fonts-wqy-zenhei fonts-tlwg-loma-otf fonts-freefont-ttf xfonts-cyrillic xfonts-scalable \
        libfontconfig1 libfreetype6 libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 \
        libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

# Bun (JS runtime), installed system-wide. Explicit bash -c (the default RUN
# shell, /bin/sh/dash on this base image, has no pipefail): without it, a
# failed curl (e.g. a transient network error) would exit 0 through the
# pipe and silently install nothing.
RUN /bin/bash -c "set -o pipefail && curl -fsSL https://bun.sh/install | BUN_INSTALL=/usr/local bash"

# GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI + buildx plugin only, not the daemon: the dedicated
# exadev-image-builder scale-set profile runs this image with
# containerMode.type=dind, which supplies a docker:dind sidecar and shares
# its socket at DOCKER_HOST - every other profile also gets this image (an
# image build needs the same image the fleet already uses, so a separate
# builder-only image would have a chicken-and-egg bootstrap problem), but
# the CLI alone is inert with no daemon to talk to on an ordinary job.
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin \
    && rm -rf /var/lib/apt/lists/*

# yq (mikefarah, v4), which GitHub's ubuntu-24.04 image preinstalls, so a workflow that edits YAML with it runs unchanged here (issue 103). The release binary is checksum-verified per architecture.
ARG YQ_VERSION=4.54.3
ARG YQ_SHA256_AMD64=129534601871538efe0ba4c5e62a34fb1d5f469bcb975c4fddeb467d82974973
ARG YQ_SHA256_ARM64=89f69310b61b68d1245065a1241d1fc843f7a36cd6103ffd77ad987613bd1b67
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in amd64) sha256="$YQ_SHA256_AMD64" ;; arm64) sha256="$YQ_SHA256_ARM64" ;; *) echo "unsupported architecture: $arch" >&2; exit 1 ;; esac \
    && curl -fsSL -o /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/yq_linux_${arch}" \
    && echo "${sha256}  /usr/local/bin/yq" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/yq

# The job-completed hook records each job's peak memory (see runner-hooks/job-completed.sh). It lives outside the runner user's home, owned by root, so a job cannot rewrite what runs at the end of the next one.
COPY --chmod=0755 runner-hooks/job-completed.sh /usr/local/share/github-runner/hooks/job-completed.sh
ENV ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/usr/local/share/github-runner/hooks/job-completed.sh

USER runner
