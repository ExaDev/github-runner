# Tool-cache capability image for TFLint, following the tool-image contract in roles/github_runner_arc/README.md (Capabilities): /toolcache/tflint/<version>/<arch>/tflint is the release binary, with <arch>.complete beside it, and <arch> is named the way terraform-linters/setup-tflint looks it up (amd64 or arm64, its own mapping of Node's os.arch()). A job whose setup-tflint tflint_version is this version (with or without the leading v) finds it there and skips the download. The final stage is busybox, which supplies the sh and cp the role's init container runs.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 --build-arg TFLINT_VERSION=<version> -t <registry>/<name>:<version> tool-images/tflint
FROM alpine:3.22 AS fetch
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
ARG TFLINT_VERSION=0.64.0
ARG TARGETARCH
RUN apk add --no-cache curl unzip
WORKDIR /fetch
# The archive is checked against the release's own checksums.txt, fetched over HTTPS from the same release.
RUN case "${TARGETARCH}" in amd64|arm64) arch="${TARGETARCH}" ;; *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; esac \
    && archive="tflint_linux_${arch}.zip" \
    && release="https://github.com/terraform-linters/tflint/releases/download/v${TFLINT_VERSION}" \
    && curl -fsSLO "${release}/${archive}" \
    && curl -fsSL "${release}/checksums.txt" | grep "  ${archive}\$" | sha256sum -c - \
    && destination="/toolcache/tflint/${TFLINT_VERSION}/${arch}" \
    && mkdir -p "${destination}" \
    && unzip -q "${archive}" tflint -d "${destination}" \
    && touch "${destination}.complete" \
    && chmod -R a+rX /toolcache

FROM busybox:1.37
COPY --from=fetch /toolcache /toolcache
