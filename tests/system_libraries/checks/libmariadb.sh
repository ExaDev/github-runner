#!/usr/bin/env bash
# MariaDB Connector/C from the relocated sysroot. In the stock runner image: its library and programs resolve, and mariadb_config and its mysql_config name report the relocated prefix. With a compiler: C programs compiled against the payload's headers, once through its pkg-config file (as the mysqlclient Python package finds it) and once through mysql_config and -lmysqlclient, load the library from the sysroot, find the authentication plugins MySQL 8 servers ask for without a plugin directory, and report a refused connection through the library. Sourced by tests/system_libraries/run.sh.
# shellcheck source=tests/system_libraries/checks/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

check_stock() {
  check_search_paths
  check_dependencies_resolve
  echo "mariadb_config: $(command -v mariadb_config) ($(mariadb_config --cc_version))"
  # mariadb_config works out its installation from its own location, so it reports the prefix it runs from, under either name.
  local config
  for config in mariadb_config mysql_config; do
    echo "${config} --include: $($config --include)"
    echo "${config} --libs: $($config --libs)"
    $config --include | grep -q -- "-I${sysroot}/include/mariadb"
    $config --libs | grep -q -- "-L${sysroot}/lib/"
  done
  echo "mariadb_config and mysql_config ran from ${sysroot} and reported it"
}

check_compiled() {
  check_pkg_config libmariadb
  cd "$(mktemp -d)" || return
  cat >client.c <<'EOF'
#include <mysql.h>
#include <stdio.h>

/* MYSQL_CLIENT_AUTHENTICATION_PLUGIN from mysql/client_plugin.h, which cannot be included: it includes ma_compress.h, which Connector/C does not install (nor does the distribution's libmariadb-dev). mysql.h declares mysql_client_find_plugin itself. */
#define AUTHENTICATION_PLUGIN 2

int main(void) {
    MYSQL *mysql = mysql_init(NULL);
    const char *plugins[] = {"caching_sha2_password", "sha256_password", "mysql_native_password", "client_ed25519"};
    int missing = 0;
    for (size_t i = 0; i < sizeof plugins / sizeof *plugins; i++) {
        if (mysql_client_find_plugin(mysql, plugins[i], AUTHENTICATION_PLUGIN) == NULL) {
            printf("authentication plugin %s not found: %s\n", plugins[i], mysql_error(mysql));
            missing = 1;
        }
    }
    unsigned int port = 1, timeout = 2;
    mysql_options(mysql, MYSQL_OPT_CONNECT_TIMEOUT, &timeout);
    MYSQL *connected = mysql_real_connect(mysql, "127.0.0.1", "example", "example", "example", port, NULL, 0);
    unsigned int error = mysql_errno(mysql);
    printf("client %s, authentication plugins %s, connection to a closed port: %s (%u)\n", mysql_get_client_info(), missing ? "missing" : "built in", connected ? "connected" : mysql_error(mysql), error);
    mysql_close(mysql);
    return !missing && !connected && (error == 2002 || error == 2003) ? 0 : 1;
}
EOF
  # shellcheck disable=SC2046 # pkg-config's and mysql_config's output is a list of flags, split on purpose.
  gcc -o client client.c $(pkg-config --cflags --libs libmariadb)
  ldd ./client | grep -F "${sysroot}/lib/libmariadb.so"
  ./client
  # shellcheck disable=SC2046
  gcc -o client-compat client.c $(mysql_config --cflags) -lmysqlclient
  # libmysqlclient.so is a link to the same library, so the program records and loads its libmariadb soname.
  ldd ./client-compat | grep -F "${sysroot}/lib/libmariadb.so"
  ./client-compat
  echo "programs compiled against MariaDB Connector/C in ${sysroot} ran"
}
