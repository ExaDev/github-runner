# Tool-cache capability image for the AWS CLI (version 2), following the tool-image contract in roles/github_runner_arc/README.md (Capabilities): /toolcache/aws-cli/<version>/<arch>/ holds the CLI's self-contained distribution (the aws executable and the files it loads beside it), with <arch>.complete beside it, and <arch> is Node's os.arch() name (x64 or arm64), the tool cache's own default. No setup action looks the AWS CLI up in the tool cache, so this capability is meant to be declared with path: aws-cli/<version>, which puts aws on PATH at job start. The final stage is busybox, which supplies the sh and cp the role's init container runs.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 --build-arg AWSCLI_VERSION=<version> -t <registry>/<name>:<version> tool-images/awscli
FROM alpine:3.22 AS fetch
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
ARG AWSCLI_VERSION=2.37.10
ARG TARGETARCH
# The AWS CLI team's signing key, by fingerprint, as published in the AWS CLI user guide's installation instructions.
ARG AWSCLI_KEY_FINGERPRINT=FB5DB77FD5C118B80511ADA8A6310ACC4672475C
RUN apk add --no-cache curl gnupg unzip
WORKDIR /fetch
# AWS publishes no checksum for the CLI, only a detached signature, so the archive is verified against the key with the fingerprint above. The key comes from a keyserver that keeps its user id, which gpg needs to import it; the fingerprint check is what makes it the right key.
RUN case "${TARGETARCH}" in amd64) arch=x64; machine=x86_64 ;; arm64) arch=arm64; machine=aarch64 ;; *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; esac \
    && archive="awscli-exe-linux-${machine}-${AWSCLI_VERSION}.zip" \
    && curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${AWSCLI_KEY_FINGERPRINT}" | gpg --batch --import \
    && gpg --batch --with-colons --fingerprint | grep -q "^fpr:::::::::${AWSCLI_KEY_FINGERPRINT}:" \
    && curl -fsSLO "https://awscli.amazonaws.com/${archive}" \
    && curl -fsSLO "https://awscli.amazonaws.com/${archive}.sig" \
    && gpg --batch --verify "${archive}.sig" "${archive}" \
    && unzip -q "${archive}" \
    && destination="/toolcache/aws-cli/${AWSCLI_VERSION}/${arch}" \
    && mkdir -p "${destination}" \
    && cp -R aws/dist/. "${destination}/" \
    && touch "${destination}.complete" \
    && chmod -R a+rX /toolcache

FROM busybox:1.37
COPY --from=fetch /toolcache /toolcache
