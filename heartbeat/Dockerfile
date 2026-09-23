# Tiny image for the heartbeat Deployment (an in-cluster Kubernetes pod, not
# a host-level Docker Compose service - see
# ansible/roles/github_runner_arc/tasks/install_platform.yml): it only needs
# kubectl (cluster health check), curl + jq (refresh the gist via the GitHub
# API), and bash. All available from Alpine's apk, so the image is
# arch-agnostic (no gh binary to fetch per-arch) and small. Build context is
# the repo root so it can COPY scripts/heartbeat.sh; built and pushed
# multi-arch the same manual way as the main runner image (see the README's
# Build/test section) so the image is pullable from whichever node the
# scheduler places this pod on.
FROM alpine:3.20

RUN apk add --no-cache bash curl jq kubectl

COPY heartbeat/loop.sh /app/loop.sh
COPY scripts/heartbeat.sh /app/scripts/heartbeat.sh
RUN chmod +x /app/loop.sh /app/scripts/heartbeat.sh

WORKDIR /app
ENTRYPOINT ["/app/loop.sh"]
