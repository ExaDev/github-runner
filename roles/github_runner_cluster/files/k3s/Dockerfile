# k3s with the tailscale/tailscaled binaries layered on top, for k3s's own
# built-in --vpn-auth integration (see docker-compose.yml's k3s/k3s-agent
# services). rancher/k3s's own base image is a minimal, distroless-style
# image with no package manager at all - the only practical way to add
# tailscale is copying its official static binaries in directly, confirmed
# live: this is exactly what upstream's own docs mean by "installing the
# Tailscale service on all nodes" as a prerequisite for the integration.
ARG K3S_VERSION=latest
FROM docker.io/tailscale/tailscale:latest AS tailscale
FROM rancher/k3s:${K3S_VERSION}
COPY --from=tailscale /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscale
