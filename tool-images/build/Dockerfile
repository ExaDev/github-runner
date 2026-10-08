# Runner image for jobs that compile native code: GitHub's stock actions-runner image, pinned by version and digest, with a C and C++ toolchain (build-essential: gcc, g++, make and the C library's headers) and pkg-config added, and nothing else. A compiler cannot be a capability: it is a tree of programs, headers and runtime pieces laid out for one fixed prefix, so it belongs in the image the runner container runs, while the libraries a build links against still come as system-library capabilities (the ARC role README's Capabilities section). The image is used as an org's or profile's runner image in place of the stock one; the runner, its user and its hooks are the stock image's, unchanged.
#
# The tag is the stock runner version it is built on (ARG RUNNER_VERSION), so moving to a new runner release publishes a new tag; the digest pins exactly which build of that release it extends.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/<name>:<runner version> tool-images/build
ARG RUNNER_VERSION=2.338.0
ARG RUNNER_DIGEST=sha256:4ffadc0002b2581327e06101fc8c06cd189232baf79fe561fac9caeb76f5e807
FROM ghcr.io/actions/actions-runner:${RUNNER_VERSION}@${RUNNER_DIGEST}
USER root
# The packages come from the base image's own Ubuntu release, so the toolchain matches the C library the runner and every system-library payload are built against. Their versions are whatever that release carries when the image is built; the published digest fixes them from then on.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*
USER runner
