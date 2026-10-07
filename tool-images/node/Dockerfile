# Tool-cache capability image for Node.js, following the tool-image contract in roles/github_runner_arc/README.md (Capabilities): /toolcache/node/<version>/<arch>/ holds the official Linux build, with <arch>.complete beside it, and <arch> is named the way actions/setup-node looks it up (Node's os.arch(): x64 or arm64). A job whose setup-node node-version matches the version finds it there and skips the download. The final stage is busybox, which supplies the sh and cp the role's init container runs.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 --build-arg NODE_VERSION=<version> -t <registry>/<name>:<version> tool-images/node
FROM alpine:3.22 AS fetch
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
ARG NODE_VERSION=24.21.0
ARG TARGETARCH
RUN apk add --no-cache curl tar xz
WORKDIR /fetch
# The archive is checked against the release's own SHASUMS256.txt, fetched over HTTPS from the same origin.
RUN case "${TARGETARCH}" in amd64) arch=x64 ;; arm64) arch=arm64 ;; *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; esac \
    && archive="node-v${NODE_VERSION}-linux-${arch}.tar.xz" \
    && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/${archive}" \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" | grep "  ${archive}\$" | sha256sum -c - \
    && destination="/toolcache/node/${NODE_VERSION}/${arch}" \
    && mkdir -p "${destination}" \
    && tar -xJf "${archive}" -C "${destination}" --strip-components=1 \
    && touch "${destination}.complete" \
    && chmod -R a+rX /toolcache

FROM busybox:1.37
COPY --from=fetch /toolcache /toolcache
