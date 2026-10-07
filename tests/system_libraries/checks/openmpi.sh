#!/usr/bin/env bash
# Open MPI from the relocated sysroot. In the stock runner image: its programs and libraries resolve, ompi_info reports the relocated prefix, and mpirun launches two processes; the same launch without OPAL_PREFIX must fail, since that variable is why the capability sets it. With a compiler: mpicc compiles an MPI program against the payload's headers and libraries, and mpirun runs it on two processes that exchange data through MPI. Sourced by tests/system_libraries/run.sh.
# shellcheck source=tests/system_libraries/checks/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

check_stock() {
  check_search_paths
  check_dependencies_resolve
  [ "$OPAL_PREFIX" = "$sysroot" ]
  echo "mpirun: $(command -v mpirun) ($(mpirun --version | head -1))"
  ompi_info --path prefix
  ompi_info --path prefix | grep -q "${sysroot}\$"
  # --oversubscribe because a container may report fewer cores than ranks.
  mpirun --oversubscribe -n 2 hostname >/tmp/launch.txt 2>&1
  cat /tmp/launch.txt
  [ "$(wc -l </tmp/launch.txt)" = 2 ]
  echo "mpirun launched two processes from ${sysroot}"
  if (unset OPAL_PREFIX; mpirun --oversubscribe -n 2 hostname) >/tmp/without.txt 2>&1; then
    cat /tmp/without.txt
    echo "mpirun also ran without OPAL_PREFIX, so the capability's env no longer needs it" >&2
    return 1
  fi
  echo "without OPAL_PREFIX, mpirun fails as expected:"
  grep -v '^ *$' /tmp/without.txt | head -6
}

check_compiled() {
  check_pkg_config ompi-c
  cd "$(mktemp -d)" || return
  cat >hello.c <<'EOF'
#include <mpi.h>
#include <stdio.h>

int main(int argc, char **argv) {
    int rank, size, sum;
    MPI_Init(&argc, &argv);
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    MPI_Comm_size(MPI_COMM_WORLD, &size);
    MPI_Allreduce(&rank, &sum, 1, MPI_INT, MPI_SUM, MPI_COMM_WORLD);
    printf("rank %d of %d, rank sum %d\n", rank, size, sum);
    MPI_Finalize();
    return 0;
}
EOF
  mpicc -o hello hello.c
  # The include and library directories mpicc uses come from the relocated prefix, not the build prefix.
  echo "mpicc --showme: $(mpicc --showme)"
  mpicc --showme | grep -q -- "-I${sysroot}/include"
  mpirun --oversubscribe -n 2 ./hello | sort | tee ranks.txt
  [ "$(cat ranks.txt)" = "$(printf 'rank 0 of 2, rank sum 1\nrank 1 of 2, rank sum 1')" ]
  echo "mpirun ran a two-process MPI program compiled against ${sysroot}"
}
