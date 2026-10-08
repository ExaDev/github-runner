# System-library capability image for MariaDB Connector/C, the LGPL client library for MySQL and MariaDB servers, following the payload contract in roles/github_runner_arc/README.md (Capabilities): /sysroot is a relocatable prefix holding the shared library (lib/libmariadb.so, with the libmysqlclient.so names linked to it), its headers (include/mariadb/, with the include/mariadb/mysql/ subdirectory MySQL's client headers sit in), a pkg-config file (lib/pkgconfig/libmariadb.pc) and mariadb_config with its mysql_config name, built on the runner image's own distribution release against its OpenSSL and zlib, so it needs nothing beyond what the runner image already has. It is what the distribution's MySQL client development package is for a build: the mysqlclient Python package, for one, finds it through pkg-config as libmariadb, and a build that asks mysql_config or links -lmysqlclient finds it under those names.
#
# The library loads its authentication plugins from a directory compiled in as an absolute path, which a relocated payload cannot honour, so every plugin a client on Linux uses is built into the library instead (caching_sha2_password and sha256_password, which MySQL 8's default accounts need, and MariaDB's ed25519, PARSEC, dialog and clear-text ones), leaving no plugin to load and no variable to set; GSSAPI, which would need the Kerberos libraries, and the remote-file plugin, which would need curl, are left out. mariadb_config finds its installation from its own location, so it reports the relocated prefix. The final stage is busybox, which supplies the sh and cp the role's init container runs.
#
# Build for both architectures: docker buildx build --platform linux/amd64,linux/arm64 --build-arg MARIADB_CONNECTOR_C_VERSION=<version> --build-arg MARIADB_CONNECTOR_C_SHA256=<sha256> -t <registry>/<name>:<version> tool-images/libmariadb
FROM ubuntu:24.04 AS build
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
ARG MARIADB_CONNECTOR_C_VERSION=3.4.11
# The SHA-256 of GitHub's source archive of the release tag, taken when this version was pinned; a different version needs its own.
ARG MARIADB_CONNECTOR_C_SHA256=7cdb35c571dd0c187f806f569285829d31b4aba4feb93957a034b0b1e887e276
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates cmake curl gcc libc6-dev libssl-dev make zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# INSTALL_LIBDIR=lib keeps the library in lib/, where the payload contract puts it, rather than the project's default lib/mariadb/. WITH_MYSQLCOMPAT adds the libmysqlclient names. mysql_config is a copy of mariadb_config rather than a symbolic link to it: the program finds its installation by looking for bin/<the name it was run as> in the path the kernel reports it was executed from, which for a link is the target's name, so a linked mysql_config would fall back to the build prefix. The manual pages are dropped, since every runner pod copies the payload.
RUN archive="mariadb-connector-c-${MARIADB_CONNECTOR_C_VERSION}.tar.gz" \
    && curl -fsSL -o "${archive}" "https://github.com/mariadb-corporation/mariadb-connector-c/archive/refs/tags/v${MARIADB_CONNECTOR_C_VERSION}.tar.gz" \
    && echo "${MARIADB_CONNECTOR_C_SHA256}  ${archive}" | sha256sum -c - \
    && tar -xzf "${archive}" --strip-components=1 \
    && cmake -S . -B out -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/sysroot \
        -DINSTALL_LIBDIR=lib -DINSTALL_INCLUDEDIR=include/mariadb -DINSTALL_PCDIR=lib/pkgconfig -DINSTALL_PLUGINDIR=lib/mariadb/plugin \
        -DWITH_SSL=OPENSSL -DWITH_EXTERNAL_ZLIB=ON -DWITH_MYSQLCOMPAT=ON -DWITH_UNIT_TESTS=OFF \
        -DCLIENT_PLUGIN_CACHING_SHA2_PASSWORD=STATIC -DCLIENT_PLUGIN_SHA256_PASSWORD=STATIC \
        -DCLIENT_PLUGIN_CLIENT_ED25519=STATIC -DCLIENT_PLUGIN_PARSEC=STATIC \
        -DCLIENT_PLUGIN_DIALOG=STATIC -DCLIENT_PLUGIN_MYSQL_CLEAR_PASSWORD=STATIC \
        -DCLIENT_PLUGIN_AUTH_GSSAPI_CLIENT=OFF -DCLIENT_PLUGIN_REMOTE_IO=OFF \
    && cmake --build out --parallel "$(nproc)" \
    && cmake --install out \
    && cp /sysroot/bin/mariadb_config /sysroot/bin/mysql_config \
    && rm -rf /sysroot/lib/*.a /sysroot/man
# The payload contract's finishing steps: libtool archives, which name the build prefix, are dropped; pkg-config files are rewritten relative to their own directory, so they work from any prefix; a pkg-config file that still names the build prefix fails the build; and so does any plugin left to load from the compiled-in directory, since none should be.
# ${pcfiledir} is pkg-config's own variable, written into the files literally, hence the single quotes.
# hadolint ignore=SC2016
RUN find /sysroot -name '*.la' -delete \
    && find /sysroot -name '*.pc' -exec sed -i 's|/sysroot|${pcfiledir}/../..|g' {} + \
    && ! grep -rl /sysroot --include='*.pc' /sysroot \
    && [ -z "$(find /sysroot -name '*.so' -path '*/plugin/*')" ] \
    && rm -rf /sysroot/lib/mariadb \
    && chmod -R a+rX /sysroot

FROM busybox:1.37
COPY --from=build /sysroot /sysroot
