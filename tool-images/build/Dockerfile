# Runner image for jobs that compile native code: GitHub's stock actions-runner image, pinned by version and digest, with a C and C++ toolchain (build-essential: gcc, g++, make and the C library's headers) and pkg-config added, and nothing else. A compiler cannot be a capability: it is a tree of programs, headers and runtime pieces laid out for one fixed prefix, so it belongs in the image the runner container runs, while the libraries a build links against still come as system-library capabilities (the ARC role README's Capabilities section). The image is used as an org's or profile's runner image in place of the stock one; the runner, its user and its hooks are the stock image's, unchanged, unless RUNNER_UID or RUNNER_GID below gives the runner user other ids.
#
# The tag is the stock runner version it is built on (ARG RUNNER_VERSION), so moving to a new runner release publishes a new tag; the digest pins exactly which build of that release it extends.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/<name>:<runner version> tool-images/build (add --build-arg RUNNER_UID=<id> --build-arg RUNNER_GID=<id> for a runner user with other ids, and tag it <runner version>-uid<id>)
ARG RUNNER_VERSION=2.338.0
ARG RUNNER_DIGEST=sha256:4ffadc0002b2581327e06101fc8c06cd189232baf79fe561fac9caeb76f5e807
FROM ghcr.io/actions/actions-runner:${RUNNER_VERSION}@${RUNNER_DIGEST}
USER root
# The packages come from the base image's own Ubuntu release, so the toolchain matches the C library the runner and every system-library payload are built against. Their versions are whatever that release carries when the image is built; the published digest fixes them from then on.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config \
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
