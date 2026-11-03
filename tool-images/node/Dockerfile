# Tool-cache capability image for Node.js, following the tool-image contract in roles/github_runner_arc/README.md (Capabilities): /toolcache/node/<version>/<arch>/ holds the official Linux build, with <arch>.complete beside it, and <arch> is named the way actions/setup-node looks it up (Node's os.arch(): x64 or arm64). A job whose setup-node node-version matches the version finds it there and skips the download. The final stage is busybox, which supplies the sh, cp, rm and chmod the role's init containers run.
#
# One Dockerfile builds every Node.js version a fleet pins: the version is the NODE_VERSION build argument, whose default is the version published without an explicit one, and .github/workflows/publish-tool-images.yml names each further version in its matrix. The fetch stage installs nothing: it runs the wget, tar and sha256sum that the base image's busybox already provides, so no package manager runs at build time and no package index is trusted besides the release's own checksums.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 --build-arg NODE_VERSION=<version> -t <registry>/<name>:<version> tool-images/node
FROM alpine:3.22 AS fetch
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
ARG NODE_VERSION=24.21.0
ARG TARGETARCH
WORKDIR /fetch
# The archive is checked against the release's own SHASUMS256.txt, fetched over HTTPS from the same origin.
RUN case "${TARGETARCH}" in amd64) arch=x64 ;; arm64) arch=arm64 ;; *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; esac \
    && archive="node-v${NODE_VERSION}-linux-${arch}.tar.xz" \
    && wget -q "https://nodejs.org/dist/v${NODE_VERSION}/${archive}" \
    && wget -qO- "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" | grep "  ${archive}\$" | sha256sum -c - \
    && destination="/toolcache/node/${NODE_VERSION}/${arch}" \
    && mkdir -p "${destination}" \
    && tar -xJf "${archive}" -C "${destination}" --strip-components=1 \
    && touch "${destination}.complete" \
    && chmod -R a+rX /toolcache

FROM busybox:1.37
COPY --from=fetch /toolcache /toolcache
