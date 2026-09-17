# Tiny image for the autoscaler service: it only needs kubectl (read/patch the
# AutoscalingRunnerSet, kubectl top pod), jq, and bash. Same shape as
# heartbeat/Dockerfile: Alpine keeps it arch-agnostic and small. Build context
# is the repo root (see docker-compose.yml) so it can COPY scripts/autoscaler.sh.
FROM alpine:3.20

RUN apk add --no-cache bash curl jq kubectl

COPY autoscaler/loop.sh /app/loop.sh
COPY scripts/autoscaler.sh /app/scripts/autoscaler.sh
RUN chmod +x /app/loop.sh /app/scripts/autoscaler.sh

WORKDIR /app
ENTRYPOINT ["/app/loop.sh"]
