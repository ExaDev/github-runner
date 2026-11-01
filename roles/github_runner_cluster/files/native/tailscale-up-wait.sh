#!/bin/sh
# Stands in for `tailscale` on the PATH of the native k3s-agent service (templates/native/k3s-agent.service.j2 puts this file's directory first), so that k3s's own `tailscale up` is followed by a wait for the host this node joins. It runs the real Tailscale CLI with the arguments it was given and, after a successful `tailscale up`, waits until GITHUB_RUNNER_JOIN_HOST is a peer in this node's network map and resolves, then says so in the journal.
#
# k3s's --vpn-auth start (StartVPN in k3s's pkg/vpn/vpn.go) runs `tailscale up --authkey ... --timeout=30s --reset`, which restarts the Tailscale backend from its cached network map, and k3s dials the join host as soon as that command returns. A cached map can hold no peers (the container node logged "Start: loaded netmap from disk cache; 0 peers" with Tailscale v1.102.4), so for about a second after the command the join host is neither a peer nor a MagicDNS name, and k3s exits with "no such host". The wait is the same one node-entrypoint.sh installs for the container nodes; the name is looked up the way k3s looks it up, through the host's resolver, which Tailscale only configures once it has the map.
#
# Reads GITHUB_RUNNER_TAILSCALE (the real CLI) and GITHUB_RUNNER_JOIN_HOST (the host in the join URL) from the service's environment. Every other command passes through unchanged, so k3s's `tailscale status --json` reads the real thing.
set -u

real=${GITHUB_RUNNER_TAILSCALE:?GITHUB_RUNNER_TAILSCALE must name the real tailscale command}

"$real" "$@" || exit

if [ "${1:-}" = up ] && [ -n "${GITHUB_RUNNER_JOIN_HOST:-}" ]; then
  logger -t github-runner-k3s "waiting for ${GITHUB_RUNNER_JOIN_HOST} to be a peer that resolves" 2>/dev/null || true
  until "$real" ip -4 "$GITHUB_RUNNER_JOIN_HOST" >/dev/null 2>&1 && getent hosts "$GITHUB_RUNNER_JOIN_HOST" >/dev/null 2>&1; do sleep 0.5; done
  logger -t github-runner-k3s "${GITHUB_RUNNER_JOIN_HOST} is a peer and resolves after tailscale up" 2>/dev/null || true
fi
