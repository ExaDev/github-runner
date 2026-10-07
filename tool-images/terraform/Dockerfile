# Tool-cache capability image for Terraform, following the tool-image contract in roles/github_runner_arc/README.md (Capabilities): /toolcache/terraform/<version>/<arch>/terraform is the release binary, with <arch>.complete beside it, and <arch> is Node's os.arch() name (x64 or arm64), the tool cache's own default. hashicorp/setup-terraform always downloads and never consults the tool cache, so this capability is meant to be declared with path: terraform/<version>, which puts the binary on PATH at job start. The final stage is busybox, which supplies the sh and cp the role's init container runs.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 --build-arg TERRAFORM_VERSION=<version> -t <registry>/<name>:<version> tool-images/terraform
FROM alpine:3.22 AS fetch
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
ARG TERRAFORM_VERSION=1.16.5
ARG TARGETARCH
# HashiCorp's release signing key, by fingerprint, as published at https://www.hashicorp.com/security.
ARG HASHICORP_KEY_FINGERPRINT=C874011F0AB405110D02105534365D9472D7468F
RUN apk add --no-cache curl gnupg unzip
WORKDIR /fetch
# The archive is checked against SHA256SUMS, and SHA256SUMS against its detached signature by the key with the fingerprint above, so a tampered mirror cannot pass with a matching checksum file.
RUN case "${TARGETARCH}" in amd64) arch=x64 ;; arm64) arch=arm64 ;; *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; esac \
    && archive="terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" \
    && release="https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}" \
    && curl -fsSL https://www.hashicorp.com/.well-known/pgp-key.txt | gpg --batch --import \
    && gpg --batch --with-colons --fingerprint | grep -q "^fpr:::::::::${HASHICORP_KEY_FINGERPRINT}:" \
    && curl -fsSLO "${release}/${archive}" \
    && curl -fsSLO "${release}/terraform_${TERRAFORM_VERSION}_SHA256SUMS" \
    && curl -fsSLO "${release}/terraform_${TERRAFORM_VERSION}_SHA256SUMS.sig" \
    && gpg --batch --verify "terraform_${TERRAFORM_VERSION}_SHA256SUMS.sig" "terraform_${TERRAFORM_VERSION}_SHA256SUMS" \
    && grep "  ${archive}\$" "terraform_${TERRAFORM_VERSION}_SHA256SUMS" | sha256sum -c - \
    && destination="/toolcache/terraform/${TERRAFORM_VERSION}/${arch}" \
    && mkdir -p "${destination}" \
    && unzip -q "${archive}" terraform -d "${destination}" \
    && touch "${destination}.complete" \
    && chmod -R a+rX /toolcache

FROM busybox:1.37
COPY --from=fetch /toolcache /toolcache
