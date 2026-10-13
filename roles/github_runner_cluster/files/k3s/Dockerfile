# k3s with the tailscale/tailscaled binaries layered on top, for k3s's own
# built-in --vpn-auth integration (see docker-compose.yml's k3s/k3s-agent
# services). rancher/k3s's own base image is a minimal, distroless-style
# image with no package manager at all - the only practical way to add
# tailscale is copying its official static binaries in directly, confirmed
# live: this is exactly what upstream's own docs mean by "installing the
# Tailscale service on all nodes" as a prerequisite for the integration.
#
# Both versions are required build arguments with no default, so a build that names neither fails instead of taking whatever latest is that day (the role writes both into .env from github_runner_cluster_k3s_version and github_runner_cluster_tailscale_version).
ARG K3S_VERSION
ARG TAILSCALE_VERSION
FROM docker.io/tailscale/tailscale:${TAILSCALE_VERSION} AS tailscale
FROM rancher/k3s:${K3S_VERSION}
COPY --from=tailscale /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscale
