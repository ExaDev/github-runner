# System-library capability image for HiGHS, the linear and mixed-integer programming solver, following the payload contract in roles/github_runner_arc/README.md (Capabilities): /sysroot is a relocatable prefix holding the shared library (lib/libhighs.so), its C and C++ headers (include/highs/), a pkg-config file (lib/pkgconfig/highs.pc), a CMake package (lib/cmake/highs/) and the highs command-line solver (bin/highs), built on the runner image's own distribution release, so it needs nothing beyond what the runner image already has. HiGHS records no install path that it reads at run time, so it needs no extra environment. The final stage is busybox, which supplies the sh and cp the role's init container runs.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 --build-arg HIGHS_VERSION=<version> --build-arg HIGHS_SHA256=<sha256> -t <registry>/<name>:<version> tool-images/highs
FROM ubuntu:24.04 AS build
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
ARG HIGHS_VERSION=1.15.1
# The SHA-256 of GitHub's source archive of the release tag, taken when this version was pinned; a different version needs its own.
ARG HIGHS_SHA256=a840d269dff2fafb371dd247df13ad5e026d7ce3b35ad3dc1eedd59bf0c2fb16
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates cmake curl g++ make zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# CMAKE_INSTALL_LIBDIR=lib keeps the libraries in lib/, where the payload contract puts them, whatever the distribution's own multiarch convention.
RUN archive="highs-${HIGHS_VERSION}.tar.gz" \
    && curl -fsSL -o "${archive}" "https://github.com/ERGO-Code/HiGHS/archive/refs/tags/v${HIGHS_VERSION}.tar.gz" \
    && echo "${HIGHS_SHA256}  ${archive}" | sha256sum -c - \
    && tar -xzf "${archive}" --strip-components=1 \
    && cmake -S . -B out -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/sysroot -DCMAKE_INSTALL_LIBDIR=lib \
        -DBUILD_SHARED_LIBS=ON -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF \
    && cmake --build out --parallel "$(nproc)" \
    && cmake --install out
# The payload contract's finishing steps: libtool archives, which name the build prefix, are dropped; pkg-config files are rewritten relative to their own directory, so they work from any prefix; and a pkg-config file or CMake package that still names the build prefix fails the build.
# ${pcfiledir} is pkg-config's own variable, written into the files literally, hence the single quotes.
# hadolint ignore=SC2016
RUN find /sysroot -name '*.la' -delete \
    && find /sysroot -name '*.pc' -exec sed -i 's|/sysroot|${pcfiledir}/../..|g' {} + \
    && ! grep -rl /sysroot --include='*.pc' --include='*.cmake' /sysroot \
    && chmod -R a+rX /sysroot

FROM busybox:1.37
COPY --from=build /sysroot /sysroot
