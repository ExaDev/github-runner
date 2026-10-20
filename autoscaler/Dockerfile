# Tiny image for the autoscaler Deployment (an in-cluster Kubernetes pod, not
# a host-level Docker Compose service - see
# roles/github_runner_arc/tasks/install_platform.yml): it only needs
# kubectl (read/patch the AutoscalingRunnerSet, kubectl top pod/nodes, read
# the nodes and pods the ceiling is worked out from), jq,
# and bash. Same shape as heartbeat/Dockerfile: Alpine keeps it arch-agnostic
# and small. Build context is the repo root so it can COPY
# the scripts the role runs from it (autoscaler.sh, psi-reporter.sh,
# node-cache-pruner.sh); built and pushed multi-arch by the release workflow
# (.github/workflows/release.yml, build-images), tagged with the version the
# role's defaults are stamped with, so the image is pullable from whichever
# node the scheduler places a pod on and always carries the scripts of the
# collection version that runs it.
FROM alpine:3.20

RUN apk add --no-cache bash curl jq kubectl

COPY autoscaler/loop.sh /app/loop.sh
COPY scripts/autoscaler.sh /app/scripts/autoscaler.sh
# The jq programs autoscaler.sh runs from its own directory, and the module they share.
COPY scripts/autoscaler-ceiling.jq /app/scripts/autoscaler-ceiling.jq
COPY scripts/autoscaler-usage.jq /app/scripts/autoscaler-usage.jq
COPY scripts/autoscaler-quantity.jq /app/scripts/autoscaler-quantity.jq
COPY scripts/psi-reporter.sh /app/scripts/psi-reporter.sh
COPY scripts/node-cache-pruner.sh /app/scripts/node-cache-pruner.sh
RUN chmod +x /app/loop.sh /app/scripts/autoscaler.sh /app/scripts/psi-reporter.sh /app/scripts/node-cache-pruner.sh

WORKDIR /app
ENTRYPOINT ["/app/loop.sh"]
