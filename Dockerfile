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
        gcc-11 g++-11 unzip \
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

USER runner
