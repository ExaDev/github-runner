#!/usr/bin/env bash
# Run each published image twice in an ABBA order on one disposable runner.
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
image_a="${1:?first image required}"
image_b="${2:?second image required}"
scenario="${3:?scenario required}"
results="${4:?results directory required}"
bash "${root}/tests/mesh/validate-canary.sh" "$image_a" "$scenario" || exit 1
bash "${root}/tests/mesh/validate-canary.sh" "$image_b" "$scenario" || exit 1
if [ "$image_a" = "$image_b" ]; then
  printf '%s\n' 'Images must be different digest references' >&2
  exit 1
fi
mkdir -p "$results"
printf 'run\timage\tscenario\tresult\telapsed_seconds\n' > "$results/results.tsv"
failed=0
for run in 1 2 3 4; do
  case "$run" in 1 | 4) image="$image_a" ;; *) image="$image_b" ;; esac
  printf 'Running %s: %s (%s)\n' "$run" "$image" "$scenario"
  start=$SECONDS
  if GRTEST_ROLE_IMAGE="$image" GRTEST_WORK="${results}/mesh-${run}" "${root}/tests/mesh/run.sh" "$scenario" > "$results/run-${run}.log" 2>&1; then
    result=pass
  else
    result=fail
    failed=1
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$run" "$image" "$scenario" "$result" "$((SECONDS - start))" | tee -a "$results/results.tsv"
done
exit "$failed"
