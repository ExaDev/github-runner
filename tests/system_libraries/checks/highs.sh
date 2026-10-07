#!/usr/bin/env bash
# HiGHS from the relocated sysroot. In the stock runner image: its program and library resolve and the highs command solves a model. With a compiler: a C program compiled against the payload's headers through its pkg-config file solves a small linear programme through the C API. Sourced by tests/system_libraries/run.sh.
# shellcheck source=tests/system_libraries/checks/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

check_stock() {
  check_search_paths
  check_dependencies_resolve
  echo "highs: $(command -v highs) ($(highs --version | head -1))"
  cd "$(mktemp -d)" || return
  # maximise x + y subject to x + 2y <= 4 and 3x + y <= 6: the optimum is x = 1.6, y = 1.2, objective 2.8.
  cat >model.lp <<'EOF'
Maximize
 obj: x + y
Subject To
 c1: x + 2 y <= 4
 c2: 3 x + y <= 6
End
EOF
  highs model.lp >solve.txt
  grep -i -E 'model status|objective value' solve.txt
  grep -qi 'model status *: *optimal' solve.txt
  grep -qi 'objective value *: *2\.8' solve.txt
  echo "the highs command solved a model from ${sysroot}"
}

check_compiled() {
  check_pkg_config highs
  cd "$(mktemp -d)" || return
  cat >lp.c <<'EOF'
#include <interfaces/highs_c_api.h>
#include <math.h>
#include <stdio.h>

int main(void) {
    const HighsInt num_col = 2, num_row = 2, num_nz = 4;
    double col_cost[] = {1.0, 1.0}, col_lower[] = {0.0, 0.0}, col_upper[] = {1.0e30, 1.0e30};
    double row_lower[] = {-1.0e30, -1.0e30}, row_upper[] = {4.0, 6.0};
    HighsInt a_start[] = {0, 2}, a_index[] = {0, 1, 0, 1};
    double a_value[] = {1.0, 3.0, 2.0, 1.0};
    double col_value[2], col_dual[2], row_value[2], row_dual[2];
    HighsInt col_basis[2], row_basis[2], model_status;
    HighsInt status = Highs_lpCall(num_col, num_row, num_nz, kHighsMatrixFormatColwise, kHighsObjSenseMaximize, 0.0,
                                   col_cost, col_lower, col_upper, row_lower, row_upper, a_start, a_index, a_value,
                                   col_value, col_dual, row_value, row_dual, col_basis, row_basis, &model_status);
    double objective = col_value[0] + col_value[1];
    printf("status %d, model status %d, x %.3f, y %.3f, objective %.3f\n", (int)status, (int)model_status, col_value[0], col_value[1], objective);
    return status == kHighsStatusOk && model_status == kHighsModelStatusOptimal && fabs(objective - 2.8) < 1e-6 ? 0 : 1;
}
EOF
  # shellcheck disable=SC2046 # pkg-config's output is a list of flags, split on purpose.
  gcc -o lp lp.c $(pkg-config --cflags --libs highs) -lm
  ./lp
  echo "a program compiled against HiGHS in ${sysroot} solved an LP"
}
