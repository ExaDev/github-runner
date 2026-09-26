# Image for the node recovery Deployment (see roles/github_runner_arc/templates/node-recovery.yaml.j2 and scripts/node-recovery.sh): bash, jq and kubectl, all from Alpine's apk, so it is small and builds for either architecture. Build context is the repository root so it can COPY the script. The Deployment pulls it without a pull Secret, so the published package must be public.
FROM alpine:3.20

RUN apk add --no-cache bash jq kubectl

COPY node-recovery/loop.sh /app/loop.sh
COPY scripts/node-recovery.sh /app/scripts/node-recovery.sh
RUN chmod +x /app/loop.sh /app/scripts/node-recovery.sh

# A non-root user by number, so the Deployment's runAsNonRoot check can verify it without reading /etc/passwd.
USER 65532:65532
WORKDIR /app
ENTRYPOINT ["/app/loop.sh"]
