# Runner image for jobs that compile native code: GitHub's stock actions-runner image, pinned by version and digest, with a C and C++ toolchain (build-essential: gcc, g++, make and the C library's headers), pkg-config, and the tools a workflow written for GitHub's ubuntu-24.04 image installs behind a guard when its job lands here (zstd, the GitHub CLI, and the system packages Playwright's Chromium needs) added, and nothing else. A compiler cannot be a capability: it is a tree of programs, headers and runtime pieces laid out for one fixed prefix, so it belongs in the image the runner container runs, while the libraries a build links against still come as system-library capabilities (the ARC role README's Capabilities section). The image is used as an org's or profile's runner image in place of the stock one; the runner, its user and its hooks are the stock image's, unchanged, unless RUNNER_UID or RUNNER_GID below gives the runner user other ids.
#
# The tag is the stock runner version it is built on (ARG RUNNER_VERSION), so moving to a new runner release publishes a new tag; the digest pins exactly which build of that release it extends.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/<name>:<runner version> tool-images/build (add --build-arg RUNNER_UID=<id> --build-arg RUNNER_GID=<id> for a runner user with other ids, and tag it <runner version>-uid<id>)
ARG RUNNER_VERSION=2.338.0
ARG RUNNER_DIGEST=sha256:4ffadc0002b2581327e06101fc8c06cd189232baf79fe561fac9caeb76f5e807
# Raised when the image's contents change while the runner version it extends does not, so the change is published under a new tag (scripts/ci/tool_image_tag.sh).
ARG IMAGE_REVISION=1
FROM ghcr.io/actions/actions-runner:${RUNNER_VERSION}@${RUNNER_DIGEST}
USER root
# The packages come from the base image's own Ubuntu release, so the toolchain matches the C library the runner and every system-library payload are built against. Their versions are whatever that release carries when the image is built; the published digest fixes them from then on.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*
# zstd: actions/cache compresses with it when it is on PATH and falls back to gzip, several times slower, when it is not. The GitHub CLI comes from GitHub's own apt repository, as on its hosted image. The Chromium packages are the ones Playwright's `install-deps chromium` installs for this Ubuntu release (fonts, Xvfb and the libraries Chromium links against), so an end-to-end job's `playwright install --with-deps` has nothing left to fetch; tests/runner_image/run.sh runs install-deps against the latest Playwright and fails when it would still install something, which is the signal to extend this list.
# hadolint ignore=DL3008
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh zstd \
        xvfb fonts-noto-color-emoji fonts-unifont fonts-liberation fonts-ipafont-gothic fonts-wqy-zenhei fonts-tlwg-loma-otf fonts-freefont-ttf xfonts-cyrillic xfonts-scalable \
        libfontconfig1 libfreetype6 libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 \
        libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
    && rm -rf /var/lib/apt/lists/*
# The runner user's id and its primary group's, the stock image's (1001) unless set. A runner pod that runs with hostUsers: false gets its hostPath volumes (the role's node cache and capability store) mounted idmapped, so a file the runner writes there is owned on the node by this same id; a fleet whose nodes give 1001 to a person's account builds the image with an id free on every node, which the nodes reserve for an account of their own, and sets the role's github_runner_arc_runner_uid and github_runner_arc_runner_gid to it. groupmod and usermod refuse an id the image already uses, and every file the old ids owned (the runner's home and its installation among them) moves to the new ones, so nothing is left owned by an id the runner no longer has. With the stock ids the step changes no file.
ARG RUNNER_UID=1001
ARG RUNNER_GID=1001
RUN set -eu; \
    old_uid="$(id -u runner)"; old_gid="$(id -g runner)"; \
    if [ "$RUNNER_GID" != "$old_gid" ]; then \
      groupmod -g "$RUNNER_GID" runner; \
      find / -xdev -gid "$old_gid" -exec chgrp -h "$RUNNER_GID" {} +; \
    fi; \
    if [ "$RUNNER_UID" != "$old_uid" ]; then \
      usermod -u "$RUNNER_UID" runner; \
      find / -xdev -uid "$old_uid" -exec chown -h "$RUNNER_UID" {} +; \
    fi
USER runner
