#!/bin/sh
# Writes the resolver file the native k3s-agent hands to the kubelet (--resolv-conf), from the host's own upstream name servers and without Tailscale's. Run by the service before k3s starts, with the file to write as its only argument.
#
# The container nodes point the kubelet at a file naming only Tailscale's resolver (100.100.100.100), which forwards every other name to the container's original servers. On a host the Tailscale client may not do that: with systemd-resolved it cannot read the host's servers to forward to (GetBaseConfig in net/dns/resolved.go of the Tailscale source returns ErrGetBaseConfigNotSupported), so it forwards other names only to the global nameservers the tailnet's DNS settings name, and a file naming only 100.100.100.100 would leave pods unable to resolve public names when the tailnet names none. Pods resolve through CoreDNS, which forwards to the servers in this file, so they are the host's real upstream servers, with no search domains and no options (the kubelet adds its own), and the Tailscale client's resolvers are left out, the tailnet's search domains with them.
#
# The servers come from the first of GITHUB_RUNNER_RESOLV_SOURCES (default: systemd-resolved's list of every link's servers, then /etc/resolv.conf) that has any left after dropping Tailscale's resolvers, loopback addresses (a pod cannot reach the host's own stub) and link-local addresses with a zone. A host with none left fails the service, which retries.
set -eu

output=${1:?usage: write-resolv-conf.sh OUTPUT_FILE}
sources=${GITHUB_RUNNER_RESOLV_SOURCES:-"/run/systemd/resolve/resolv.conf /etc/resolv.conf"}

for source in $sources; do
  [ -r "$source" ] || continue
  servers=$(awk '
    $1 == "nameserver" && $2 != "100.100.100.100" && $2 != "fd7a:115c:a1e0::53" && $2 !~ /^127\./ && $2 != "::1" && $2 !~ /%/ { print "nameserver " $2 }
  ' "$source")
  if [ -n "$servers" ]; then
    temporary="${output}.tmp"
    printf '%s\n' "$servers" > "$temporary"
    mv "$temporary" "$output"
    exit 0
  fi
done

echo "write-resolv-conf: no upstream name server outside Tailscale and loopback in: ${sources}" >&2
exit 1
