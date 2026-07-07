# Tiny image for the heartbeat service: it only needs kubectl (cluster health
# check), curl + jq (refresh the gist via the GitHub API), and bash. All
# available from Alpine's apk, so the image is arch-agnostic (no gh binary to
# fetch per-arch) and small. Build context is the repo root (see
# docker-compose.yml) so it can COPY scripts/heartbeat.sh.
FROM alpine:3.20

RUN apk add --no-cache bash curl jq kubectl

COPY heartbeat/loop.sh /app/loop.sh
COPY scripts/heartbeat.sh /app/scripts/heartbeat.sh
RUN chmod +x /app/loop.sh /app/scripts/heartbeat.sh

WORKDIR /app
ENTRYPOINT ["/app/loop.sh"]
