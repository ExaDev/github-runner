# System-library capability image for Open MPI, following the payload contract in roles/github_runner_arc/README.md (Capabilities): /sysroot is a relocatable prefix (bin/, lib/, include/, lib/pkgconfig/) built on the runner image's own distribution release, so it needs nothing beyond what the runner image already has. Open MPI records its install prefix in its binaries, so the capability is declared with env OPAL_PREFIX set to the prefix it is extracted into; with that, mpicc, mpirun and the libraries find their help files, plugins and wrapper data there. The bundled hwloc, libevent, PMIx and PRRTE are built in, so nothing else needs installing; PMIx is built with zlib, which the runner image has, so it can compress large messages. Fortran bindings are left out, since the runner image has no Fortran runtime. The final stage is busybox, which supplies the sh and cp the role's init container runs.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 --build-arg OPENMPI_VERSION=<version> --build-arg OPENMPI_SHA256=<sha256> -t <registry>/<name>:<version> tool-images/openmpi
FROM ubuntu:24.04 AS build
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
ARG OPENMPI_VERSION=5.0.11
# The release tarball's SHA-256, as published on https://www.open-mpi.org/software/ompi/v5.0/; a different version needs its own.
ARG OPENMPI_SHA256=e668a3c4acd50c41dc204c8a6dd98a611e0f26af89cf677577fa9be8a2698003
RUN apt-get update \
    && apt-get install -y --no-install-recommends bzip2 ca-certificates curl g++ gcc make perl zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN archive="openmpi-${OPENMPI_VERSION}.tar.bz2" \
    && curl -fsSLO "https://download.open-mpi.org/release/open-mpi/v${OPENMPI_VERSION%.*}/${archive}" \
    && echo "${OPENMPI_SHA256}  ${archive}" | sha256sum -c - \
    && tar -xjf "${archive}" --strip-components=1 \
    && ./configure --prefix=/sysroot --disable-static --disable-mpi-fortran \
        --with-hwloc=internal --with-libevent=internal --with-pmix=internal --with-prrte=internal \
    && make -j"$(nproc)" \
    && make install
# The payload contract's finishing steps: libtool archives, which name the build prefix, are dropped; pkg-config files are rewritten relative to their own directory, so they work from any prefix; and a pkg-config file that still names the build prefix fails the build.
# ${pcfiledir} is pkg-config's own variable, written into the files literally, hence the single quotes.
# hadolint ignore=SC2016
RUN find /sysroot -name '*.la' -delete \
    && find /sysroot -name '*.pc' -exec sed -i 's|/sysroot|${pcfiledir}/../..|g' {} + \
    && ! grep -rl /sysroot --include='*.pc' /sysroot \
    && chmod -R a+rX /sysroot

FROM busybox:1.37
COPY --from=build /sysroot /sysroot
