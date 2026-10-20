# Shared by the regrade tests: a counter of failures and the two assertions every test uses. Sourced, never run.

failures=0

check() {
  local name=$1
  shift
  if "$@"; then
    echo "ok   $name"
  else
    echo "FAIL $name"
    failures=$((failures + 1))
  fi
}

check_not() {
  local name=$1
  shift
  if "$@"; then
    echo "FAIL $name"
    failures=$((failures + 1))
  else
    echo "ok   $name"
  fi
}

# Ends the test: a non-zero status if any assertion failed.
finish() {
  if [ "$failures" -ne 0 ]; then
    echo "$failures failed"
    exit 1
  fi
  echo "all passed"
}
