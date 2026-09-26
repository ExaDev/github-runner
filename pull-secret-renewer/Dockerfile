# Image for the image pull Secret renewal CronJob (see roles/github_runner_arc/tasks/install_pull_secret_renewal.yml and scripts/renew-pull-secret.sh). It needs openssl to sign the GitHub App JWT, curl and jq for GitHub and the registry, kubectl to write the Secret, and bash. The heartbeat and autoscaler images have no openssl, and this job holds a GitHub App private key, so it gets an image of its own rather than widening one that runs with other permissions. Alpine keeps it small and arch-agnostic; build context is the repository root so it can COPY the script. The CronJob pulls it without a pull Secret, since the Secret it pulls with is the one this job renews, so the published package must be public.
FROM alpine:3.20

RUN apk add --no-cache bash curl jq kubectl openssl

COPY scripts/renew-pull-secret.sh /app/renew-pull-secret.sh
RUN chmod +x /app/renew-pull-secret.sh

# A non-root user by number, so the CronJob's runAsNonRoot check can verify it without reading /etc/passwd.
USER 65532:65532
WORKDIR /app
ENTRYPOINT ["/app/renew-pull-secret.sh"]
