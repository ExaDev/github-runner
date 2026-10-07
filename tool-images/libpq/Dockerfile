# System-library capability image for libpq, PostgreSQL's client library, following the payload contract in roles/github_runner_arc/README.md (Capabilities): /sysroot is a relocatable prefix holding the shared library (lib/libpq.so), its headers (include/libpq-fe.h and the headers it includes), a pkg-config file (lib/pkgconfig/libpq.pc), and the psql and pg_config programs (bin/), built on the runner image's own distribution release against its OpenSSL, so it needs nothing beyond what the runner image already has. PostgreSQL's programs find their installation relative to their own location, so it needs no extra environment. Only the client side is built: no server, no readline, no ICU. The final stage is busybox, which supplies the sh and cp the role's init container runs.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 --build-arg POSTGRESQL_VERSION=<version> -t <registry>/<name>:<version> tool-images/libpq
FROM ubuntu:24.04 AS build
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
ARG POSTGRESQL_VERSION=18.6
RUN apt-get update \
    && apt-get install -y --no-install-recommends bison bzip2 ca-certificates curl flex gcc libc6-dev libssl-dev make perl pkg-config \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# The archive is checked against the release's own .sha256 file, fetched over HTTPS from the same origin.
RUN archive="postgresql-${POSTGRESQL_VERSION}.tar.bz2" \
    && release="https://ftp.postgresql.org/pub/source/v${POSTGRESQL_VERSION}" \
    && curl -fsSLO "${release}/${archive}" \
    && curl -fsSL "${release}/${archive}.sha256" | sha256sum -c - \
    && tar -xjf "${archive}" --strip-components=1 \
    && ./configure --prefix=/sysroot --with-openssl --without-readline --without-icu --without-zlib \
    && make -C src/include install \
    && make -C src/interfaces/libpq -j"$(nproc)" install \
    && make -C src/bin/pg_config install \
    && make -C src/bin/psql -j"$(nproc)" install \
    && rm -f /sysroot/lib/*.a
# The payload contract's finishing steps: libtool archives, which name the build prefix, are dropped; pkg-config files are rewritten relative to their own directory, so they work from any prefix; and a pkg-config file that still names the build prefix fails the build. libpq.pc's Requires.private names OpenSSL's pkg-config modules, which pkg-config insists on finding even when linking the shared library, which already names OpenSSL itself; with no static library in the payload it is dropped, so compiling against libpq needs no OpenSSL development package.
# ${pcfiledir} is pkg-config's own variable, written into the files literally, hence the single quotes.
# hadolint ignore=SC2016
RUN find /sysroot -name '*.la' -delete \
    && find /sysroot -name '*.pc' -exec sed -i 's|/sysroot|${pcfiledir}/../..|g' {} + \
    && sed -i '/^Requires.private:/d' /sysroot/lib/pkgconfig/libpq.pc \
    && ! grep -rl /sysroot --include='*.pc' /sysroot \
    && chmod -R a+rX /sysroot

FROM busybox:1.37
COPY --from=build /sysroot /sysroot
