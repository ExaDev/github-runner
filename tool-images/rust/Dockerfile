# Tool-cache capability image for Rust with cross-compilation tools, following the tool-image contract in roles/github_runner_arc/README.md (Capabilities): /toolcache/rust/<version>/<arch>/ holds one toolchain, with <arch>.complete beside it, and <arch> is Node's os.arch() name (x64 or arm64), the tool cache's own default. bin/ holds rustc, rustdoc, cargo, rustfmt, cargo-clippy (with clippy-driver), cargo-zigbuild, cargo-deny and zig (zig.sh, which runs the Zig in zig/, whose lib/ Zig finds beside its own executable), and lib/rustlib/ holds the standard library of the image's own platform and of every target in RUST_TARGETS. No setup action looks this toolchain up in the tool cache (rustup does not use it), so the capability is meant to be declared with path: rust/<version>/bin, which puts every tool on PATH at job start. The runner image must supply a C compiler (cc), which links build scripts, procedural macros and the runner's own platform. The macOS targets are built against the system library stubs Zig carries and the Windows targets are the gnullvm ones, linked against Zig's MinGW-w64 import libraries, so the image holds no Apple SDK and nothing of Microsoft's. The final stage is busybox, which supplies the sh, cp, rm and chmod the role's init containers run.
#
# The published tag is <RUST_VERSION>-r<IMAGE_REVISION>, so a job can compare the toolchain with its rust-toolchain.toml. A published tag is never rebuilt, so raise IMAGE_REVISION when any instruction here or another file in this directory changes and RUST_VERSION does not (set it back to 1 when RUST_VERSION is raised); the Rust toolchain workflow fails a pull request that does not.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 -t <registry>/<name>:<version> tool-images/rust
FROM alpine:3.22 AS fetch
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
ARG RUST_VERSION=1.100.0
ARG IMAGE_REVISION=1
ARG RUST_TARGETS="aarch64-apple-darwin x86_64-apple-darwin aarch64-unknown-linux-musl x86_64-unknown-linux-musl x86_64-pc-windows-gnullvm aarch64-pc-windows-gnullvm"
# The Rust project's release signing key, by fingerprint, as published at https://forge.rust-lang.org/infra/other-installation-methods.html.
ARG RUST_KEY_FINGERPRINT=108F66205EAEB0AAA8DD5E1C85AB96E6FA1BE5FE
# Zig's checksums are those https://ziglang.org/download/index.json publishes for the release, and cargo-zigbuild's and cargo-deny's are those of the .sha256 file each release publishes beside its archive. All are to be re-derived when a release is raised.
ARG ZIG_RELEASE=0.17.1
ARG ZIG_SHA256_AMD64=3581863e3cbcfd0fce0efc682ce7e9e93da96668a977791d356a26d2a7e9b17a
ARG ZIG_SHA256_ARM64=a1546d5646eb184437918d6635ca6b762a0e889eac09cef720ee7a1ed3fe7795
ARG CARGO_ZIGBUILD_RELEASE=0.24.1
ARG CARGO_ZIGBUILD_SHA256_AMD64=32581c8e13f27a807bb244aaaac870522f5b51263c766cf77b1a99771ce9fa5c
ARG CARGO_ZIGBUILD_SHA256_ARM64=70bb4c9cc71a6d16211b9079996e3cd588671053312adeff4d043499ed887601
ARG CARGO_DENY_RELEASE=0.22.1
ARG CARGO_DENY_SHA256_AMD64=13f7e825e9cb811c2f8de63f29b0f45851dfb53004029f0f2501af524728364d
ARG CARGO_DENY_SHA256_ARM64=0bac0838a1653ce693e4602b8634d15ca179edd2198e438af9c35ab1c551d509
ARG TARGETARCH
# bash runs the install.sh each Rust component ships.
RUN apk add --no-cache bash curl gnupg tar xz
COPY zig.sh /zig.sh
WORKDIR /fetch
# rustup's proxies put lib/ on the loader's path, which nothing does here, and rust-objcopy (which strips the macOS targets' executables) looks for LLVM only beside lib/rustlib/<host>/, so LLVM is linked there.
# Each Rust component is checked against the .sha256 file published beside it and against its detached signature by the key with the fingerprint above, so a changed archive cannot pass with a matching checksum file.
RUN case "${TARGETARCH}" in amd64) arch=x64; host=x86_64-unknown-linux-gnu ;; arm64) arch=arm64; host=aarch64-unknown-linux-gnu ;; *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; esac \
    && curl -fsSL https://static.rust-lang.org/rust-key.gpg.ascii | gpg --batch --import \
    && gpg --batch --with-colons --fingerprint | grep -q "^fpr:::::::::${RUST_KEY_FINGERPRINT}:" \
    && destination="/toolcache/rust/${RUST_VERSION}/${arch}" \
    && components="rustc-${RUST_VERSION}-${host} cargo-${RUST_VERSION}-${host} rustfmt-${RUST_VERSION}-${host} clippy-${RUST_VERSION}-${host} rust-std-${RUST_VERSION}-${host}" \
    && for target in ${RUST_TARGETS}; do \
         if [ "${target}" != "${host}" ]; then components="${components} rust-std-${RUST_VERSION}-${target}"; fi; \
       done \
    && for component in ${components}; do \
         archive="${component}.tar.xz"; \
         curl -fsSLO "https://static.rust-lang.org/dist/${archive}"; \
         curl -fsSLO "https://static.rust-lang.org/dist/${archive}.sha256"; \
         curl -fsSLO "https://static.rust-lang.org/dist/${archive}.asc"; \
         sha256sum -c "${archive}.sha256"; \
         gpg --batch --verify "${archive}.asc" "${archive}"; \
         tar -xJf "${archive}"; \
         "./${component}/install.sh" --prefix="${destination}" --disable-ldconfig; \
         rm -rf "${component}" "${archive}" "${archive}.sha256" "${archive}.asc"; \
       done \
    && mkdir -p "${destination}/lib/rustlib/${host}/lib" \
    && for llvm in "${destination}"/lib/libLLVM.so.*; do \
         ln -s "../../../${llvm##*/}" "${destination}/lib/rustlib/${host}/lib/${llvm##*/}"; \
       done
RUN case "${TARGETARCH}" in amd64) arch=x64; machine=x86_64; zig_sha256="${ZIG_SHA256_AMD64}"; zigbuild_sha256="${CARGO_ZIGBUILD_SHA256_AMD64}"; deny_sha256="${CARGO_DENY_SHA256_AMD64}" ;; arm64) arch=arm64; machine=aarch64; zig_sha256="${ZIG_SHA256_ARM64}"; zigbuild_sha256="${CARGO_ZIGBUILD_SHA256_ARM64}"; deny_sha256="${CARGO_DENY_SHA256_ARM64}" ;; *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; esac \
    && destination="/toolcache/rust/${RUST_VERSION}/${arch}" \
    && zig="zig-${machine}-linux-${ZIG_RELEASE}" \
    && curl -fsSLO "https://ziglang.org/download/${ZIG_RELEASE}/${zig}.tar.xz" \
    && echo "${zig_sha256}  ${zig}.tar.xz" | sha256sum -c - \
    && mkdir -p "${destination}/zig" \
    && tar -xJf "${zig}.tar.xz" -C "${destination}/zig" --strip-components=1 \
    && cp /zig.sh "${destination}/bin/zig" \
    && zigbuild="cargo-zigbuild-${machine}-unknown-linux-gnu" \
    && curl -fsSLO "https://github.com/rust-cross/cargo-zigbuild/releases/download/v${CARGO_ZIGBUILD_RELEASE}/${zigbuild}.tar.xz" \
    && echo "${zigbuild_sha256}  ${zigbuild}.tar.xz" | sha256sum -c - \
    && mkdir zigbuild \
    && tar -xJf "${zigbuild}.tar.xz" -C zigbuild \
    && find zigbuild -type f -name cargo-zigbuild -exec cp {} "${destination}/bin/cargo-zigbuild" \; \
    && deny="cargo-deny-${CARGO_DENY_RELEASE}-${machine}-unknown-linux-musl" \
    && curl -fsSLO "https://github.com/EmbarkStudios/cargo-deny/releases/download/${CARGO_DENY_RELEASE}/${deny}.tar.gz" \
    && echo "${deny_sha256}  ${deny}.tar.gz" | sha256sum -c - \
    && tar -xzf "${deny}.tar.gz" \
    && cp "${deny}/cargo-deny" "${destination}/bin/cargo-deny" \
    && chmod 0755 "${destination}/bin/zig" "${destination}/bin/cargo-zigbuild" "${destination}/bin/cargo-deny" \
    && touch "${destination}.complete" \
    && chmod -R a+rX /toolcache

FROM busybox:1.37
COPY --from=fetch /toolcache /toolcache
