#!/usr/bin/env bash
# Runs a read-only 1Password CLI command on another machine over ssh and prints its output here, so the desktop app's unlock prompt appears on the machine the person is sitting at instead of on the control node. Point the secrets role at it with github_runner_secrets_onepassword_op, and name the machine in OP_SSH_HOST (an ssh destination whose `op` is signed in through its desktop app), and set OP_ACCOUNT when that machine has several accounts, which it passes on to the remote `op`.
#
# Only `op item get` and `op read` are forwarded, because they only read. Any other command (`op item edit`, `op run`, `op inject`) is refused rather than run on a machine the caller did not name for it. The command line travels as one quoted string, so a value with spaces or shell characters reaches the remote `op` intact. The remote command's output is returned unchanged and never logged here.
set -euo pipefail

if [ -z "${OP_SSH_HOST:-}" ]; then
  echo "op-over-ssh: OP_SSH_HOST is not set" >&2
  exit 2
fi

case "${1:-} ${2:-}" in
  "item get") ;;
  "read "*) ;;
  *)
    echo "op-over-ssh: only 'op item get' and 'op read' are forwarded, not: ${1:-} ${2:-}" >&2
    exit 2
    ;;
esac

# The $PATH is meant to expand on the remote machine, where op may live under Homebrew.
# shellcheck disable=SC2016
remote='PATH=/opt/homebrew/bin:/usr/local/bin:$PATH'
if [ -n "${OP_ACCOUNT:-}" ]; then
  remote+=" OP_ACCOUNT=$(printf '%q' "$OP_ACCOUNT")"
fi
remote+=' op'
for argument in "$@"; do
  remote+=" $(printf '%q' "$argument")"
done

exec ssh -o BatchMode=yes -o ConnectTimeout=5 "$OP_SSH_HOST" "$remote"
