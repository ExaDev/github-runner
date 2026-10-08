# System-library capability image for GLPK, the GNU Linear Programming Kit, following the payload contract in roles/github_runner_arc/README.md (Capabilities): /sysroot is a relocatable prefix holding the shared library (lib/libglpk.so), its header (include/glpk.h) and the glpsol command-line solver (bin/glpsol), built on the runner image's own distribution release with GLPK's own arithmetic in place of GMP, so it needs nothing beyond what the runner image already has. GLPK ships no pkg-config file, as the distribution's own development package does not either, so a build finds it through the search paths the job-started hook sets (CPATH and LIBRARY_PATH: -lglpk alone compiles and links), exactly as it would find the distribution's copy in the system directories. GLPK reads no path at run time, so it needs no extra environment. The final stage is busybox, which supplies the sh and cp the role's init container runs.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 --build-arg GLPK_VERSION=<version> --build-arg GLPK_SHA256=<sha256> -t <registry>/<name>:<version> tool-images/glpk
FROM ubuntu:24.04 AS build
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
ARG GLPK_VERSION=5.0
# The release tarball's SHA-256, taken from the GNU release when this version was pinned; a different version needs its own.
ARG GLPK_SHA256=4a1013eebb50f728fc601bdd833b0b2870333c3b3e5a816eeba921d95bec6f15
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gcc libc6-dev make \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN archive="glpk-${GLPK_VERSION}.tar.gz" \
    && curl -fsSLO "https://ftp.gnu.org/gnu/glpk/${archive}" \
    && echo "${GLPK_SHA256}  ${archive}" | sha256sum -c - \
    && tar -xzf "${archive}" --strip-components=1 \
    && ./configure --prefix=/sysroot --disable-static \
    && make -j"$(nproc)" \
    && make install
# The payload contract's finishing steps: libtool archives, which name the build prefix, are dropped, and nothing else may name it (GLPK installs no pkg-config file or CMake package to rewrite).
RUN find /sysroot -name '*.la' -delete \
    && ! grep -rlI /sysroot /sysroot \
    && chmod -R a+rX /sysroot

FROM busybox:1.37
COPY --from=build /sysroot /sysroot
