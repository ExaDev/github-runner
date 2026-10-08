#!/usr/bin/env bash
# GLPK from the relocated sysroot. In the stock runner image: its program and library resolve and the glpsol command solves a model. With a compiler: a C program compiled with -lglpk alone, which finds the header and library through the CPATH and LIBRARY_PATH the hook set (GLPK ships no pkg-config file), solves a small linear programme through the library and loads it from the sysroot. Sourced by tests/system_libraries/run.sh.
# shellcheck source=tests/system_libraries/checks/common.sh
. "$(dirname "${BASH_SOURCE[0]}")/common.sh"

check_stock() {
  check_search_paths
  check_dependencies_resolve
  echo "glpsol: $(command -v glpsol) ($(glpsol --version | head -1))"
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
  glpsol --lp model.lp -o solution.txt >/dev/null
  grep -E '^(Status|Objective):' solution.txt
  grep -q '^Status: *OPTIMAL' solution.txt
  grep -q '^Objective: *obj = 2\.8 ' solution.txt
  echo "the glpsol command solved a model from ${sysroot}"
}

check_compiled() {
  cd "$(mktemp -d)" || return
  cat >lp.c <<'EOF'
#include <glpk.h>
#include <math.h>
#include <stdio.h>

int main(void) {
    glp_prob *lp = glp_create_prob();
    glp_set_obj_dir(lp, GLP_MAX);
    glp_add_rows(lp, 2);
    glp_set_row_bnds(lp, 1, GLP_UP, 0.0, 4.0);
    glp_set_row_bnds(lp, 2, GLP_UP, 0.0, 6.0);
    glp_add_cols(lp, 2);
    for (int j = 1; j <= 2; j++) {
        glp_set_col_bnds(lp, j, GLP_LO, 0.0, 0.0);
        glp_set_obj_coef(lp, j, 1.0);
    }
    int ia[] = {0, 1, 1, 2, 2}, ja[] = {0, 1, 2, 1, 2};
    double ar[] = {0.0, 1.0, 2.0, 3.0, 1.0};
    glp_load_matrix(lp, 4, ia, ja, ar);
    glp_smcp parm;
    glp_init_smcp(&parm);
    parm.msg_lev = GLP_MSG_OFF;
    int ret = glp_simplex(lp, &parm);
    double objective = glp_get_obj_val(lp);
    int optimal = ret == 0 && glp_get_status(lp) == GLP_OPT;
    printf("GLPK %s, status %s, x %.3f, y %.3f, objective %.3f\n", glp_version(), optimal ? "optimal" : "not optimal", glp_get_col_prim(lp, 1), glp_get_col_prim(lp, 2), objective);
    glp_delete_prob(lp);
    return optimal && fabs(objective - 2.8) < 1e-6 ? 0 : 1;
}
EOF
  # No -I, -L or pkg-config: the hook's CPATH and LIBRARY_PATH are what find glpk.h and libglpk, as a build that expects the distribution's copy in the system directories would rely on.
  gcc -o lp lp.c -lglpk -lm
  ldd ./lp | grep -F "${sysroot}/lib/libglpk.so"
  ./lp
  echo "a program compiled against GLPK in ${sysroot} solved an LP"
}
