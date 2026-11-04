# Tool-cache capability image for strace, following the tool-image contract in roles/github_runner_arc/README.md (Capabilities): /toolcache/strace/<version>/<arch>/strace is one statically linked executable, with <arch>.complete beside it, and <arch> is Node's os.arch() name (x64 or arm64), the tool cache's own default. No setup action looks strace up in the tool cache, so this capability is meant to be declared with path: strace/<version>, which puts strace on PATH at job start. It is linked statically so it needs nothing of the runner image's libraries, and without a Linux capability: a runner pod's processes may trace their own descendants (ptrace_scope 1), which is all a job tracing its own test run needs. The final stage is busybox, which supplies the sh, cp, rm and chmod the role's init containers run.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 --build-arg STRACE_VERSION=<version> --build-arg STRACE_SHA256=<digest of that release's tarball> -t <registry>/<name>:<version> tool-images/strace
FROM alpine:3.22 AS build
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
ARG STRACE_VERSION=7.2
ARG TARGETARCH
# The SHA-256 of the release tarball, and the fingerprint of the key that signs the releases (the strace maintainer's primary key, whose signing subkey made this release's signature), both to be re-derived when the version is raised.
ARG STRACE_SHA256=4bde6246926890dcee824f6e6ac42a06752f47d77e5097d86e3c0d6d4b709fe5
ARG STRACE_KEY_FINGERPRINT=7BECFE3AF7B280BB52FF77F104BA4521C996DDE1
RUN apk add --no-cache build-base linux-headers curl gnupg xz
WORKDIR /build
# The tarball is checked against the digest above and its detached signature against the key with the fingerprint above, so neither a changed release asset nor a changed key alone can pass.
RUN release="https://github.com/strace/strace/releases/download/v${STRACE_VERSION}" \
    && curl -fsSLO "${release}/strace-${STRACE_VERSION}.tar.xz" \
    && curl -fsSLO "${release}/strace-${STRACE_VERSION}.tar.xz.asc" \
    && echo "${STRACE_SHA256}  strace-${STRACE_VERSION}.tar.xz" | sha256sum -c - \
    && curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${STRACE_KEY_FINGERPRINT}" | gpg --batch --import \
    && gpg --batch --with-colons --fingerprint | grep -q "^fpr:::::::::${STRACE_KEY_FINGERPRINT}:" \
    && gpg --batch --verify "strace-${STRACE_VERSION}.tar.xz.asc" "strace-${STRACE_VERSION}.tar.xz"
# One executable with no shared library and no personality (32-bit) support, which needs a multilib toolchain.
RUN case "${TARGETARCH}" in amd64) arch=x64 ;; arm64) arch=arm64 ;; *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; esac \
    && tar -xJf "strace-${STRACE_VERSION}.tar.xz" \
    && cd "strace-${STRACE_VERSION}" \
    && ./configure --enable-mpers=no LDFLAGS=-static \
    && make -j"$(nproc)" \
    && strip src/strace \
    && ! ldd src/strace >/dev/null 2>&1 \
    && destination="/toolcache/strace/${STRACE_VERSION}/${arch}" \
    && mkdir -p "${destination}" \
    && cp src/strace "${destination}/strace" \
    && touch "${destination}.complete" \
    && chmod -R a+rX /toolcache

FROM busybox:1.37
COPY --from=build /toolcache /toolcache
