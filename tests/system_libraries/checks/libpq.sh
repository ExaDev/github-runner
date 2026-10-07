#!/usr/bin/env bash
# libpq from the relocated sysroot. In the stock runner image: its programs and library resolve, pg_config reports the relocated prefix and psql runs. With a compiler: a C program compiled against the payload's headers through its pkg-config file loads the library, reports its version and SSL library, and reports a refused connection through the library. Sourced by tests/system_libraries/run.sh.
# shellcheck source=tests/system_libraries/checks/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

check_stock() {
  check_search_paths
  check_dependencies_resolve
  echo "psql: $(command -v psql) ($(psql --version))"
  # PostgreSQL's programs work out their installation from their own location, so pg_config reports the prefix they run from.
  echo "pg_config --includedir: $(pg_config --includedir)"
  [ "$(pg_config --includedir)" = "${sysroot}/include" ]
  if psql "host=127.0.0.1 port=1 connect_timeout=2 dbname=example user=example" -c 'select 1' >/tmp/psql.txt 2>&1; then
    echo "psql connected to a closed port" >&2
    return 1
  fi
  grep -i 'connection' /tmp/psql.txt | head -2
  echo "psql ran from ${sysroot} and reported the refused connection through libpq"
}

check_compiled() {
  check_pkg_config libpq
  cd "$(mktemp -d)" || return
  cat >client.c <<'EOF'
#include <libpq-fe.h>
#include <stdio.h>

int main(void) {
    PGconn *conn = PQconnectdb("host=127.0.0.1 port=1 connect_timeout=2 dbname=example user=example");
    int bad = PQstatus(conn) == CONNECTION_BAD;
    const char *ssl = PQsslAttribute(NULL, "library");
    printf("libpq %d, SSL library %s, connection to a closed port refused: %s\n", PQlibVersion(), ssl ? ssl : "none", bad ? "yes" : "no");
    PQfinish(conn);
    return bad && ssl && PQlibVersion() >= 180000 ? 0 : 1;
}
EOF
  # shellcheck disable=SC2046 # pkg-config's output is a list of flags, split on purpose.
  gcc -o client client.c $(pkg-config --cflags --libs libpq)
  ./client
  echo "a program compiled against libpq in ${sysroot} ran"
}
