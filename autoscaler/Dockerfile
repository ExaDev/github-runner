# Tiny image for the autoscaler Deployment (an in-cluster Kubernetes pod, not
# a host-level Docker Compose service - see
# ansible/roles/github_runner_arc/tasks/install_platform.yml): it only needs
# kubectl (read/patch the AutoscalingRunnerSet, kubectl top pod/nodes), jq,
# and bash. Same shape as heartbeat/Dockerfile: Alpine keeps it arch-agnostic
# and small. Build context is the repo root so it can COPY
# scripts/autoscaler.sh; built and pushed multi-arch the same manual way as
# the main runner image (see the README's Build/test section) so the image
# is pullable from whichever node the scheduler places this pod on.
FROM alpine:3.20

RUN apk add --no-cache bash curl jq kubectl

COPY autoscaler/loop.sh /app/loop.sh
COPY scripts/autoscaler.sh /app/scripts/autoscaler.sh
RUN chmod +x /app/loop.sh /app/scripts/autoscaler.sh

WORKDIR /app
ENTRYPOINT ["/app/loop.sh"]
