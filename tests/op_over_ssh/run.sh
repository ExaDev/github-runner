#!/usr/bin/env bash
# Checks scripts/op-over-ssh.sh with a stub `ssh` that records the destination and remote command it was given, so no machine is contacted.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/../../scripts/op-over-ssh.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

mkdir "$work/bin"
cat > "$work/bin/ssh" <<'STUB'
#!/usr/bin/env bash
# The last two arguments are the destination and the remote command.
printf '%s\n' "${@: -2:1}" > "$STUB_DIR/destination"
printf '%s\n' "${@: -1}" > "$STUB_DIR/command"
echo "stub output"
STUB
chmod +x "$work/bin/ssh"
export STUB_DIR="$work"
export PATH="$work/bin:$PATH"

fail() { echo "FAIL: $1" >&2; exit 1; }

# A read is forwarded to the named machine, quoted, and its output returned.
out="$(OP_SSH_HOST=air "$script" item get 'my item' --vault 'a b' --format json --reveal)"
[ "$out" = "stub output" ] || fail "output not returned"
[ "$(cat "$work/destination")" = "air" ] || fail "wrong destination"
grep -q "op item get my\\\\ item --vault a\\\\ b --format json --reveal" "$work/command" || fail "arguments not quoted: $(cat "$work/command")"

OP_SSH_HOST=air "$script" read 'op://DevOps/x/private key' > /dev/null
grep -q "op read op://DevOps/x/private\\\\ key" "$work/command" || fail "read not forwarded"

# A value that would end the remote command stays one argument.
OP_SSH_HOST=air "$script" item get 'x; touch pwned' > /dev/null
grep -q "x\\\;\\\\ touch\\\\ pwned" "$work/command" || fail "shell characters not quoted"

# Anything that writes is refused and never reaches ssh.
rm -f "$work/command"
for refused in "item edit x" "run -- env" "inject -i f" "signin" ""; do
  # shellcheck disable=SC2086
  if OP_SSH_HOST=air "$script" $refused 2> /dev/null; then fail "forwarded: $refused"; fi
  [ ! -e "$work/command" ] || fail "reached ssh: $refused"
done

# OP_ACCOUNT reaches the remote op, so a machine with several accounts does not refuse.
OP_ACCOUNT=a.1password.com OP_SSH_HOST=air "$script" read x > /dev/null
grep -q 'OP_ACCOUNT=a.1password.com op read x' "$work/command" || fail "account not passed: $(cat "$work/command")"

# No machine named is an error, not a guess.
if env -u OP_SSH_HOST "$script" read x 2> /dev/null; then fail "ran without OP_SSH_HOST"; fi

echo "op-over-ssh: ok"
