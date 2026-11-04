# Image for the role controller (see roles/github_runner_arc/templates/fleet-role.yaml.j2): the `fleet-role` Deployment runs scripts/fleet-role.sh through loop.sh (and an owner runs its `swap` command in the same pod), and the `fleet-role-probe` DaemonSet runs scripts/fleet-role-probe.sh from the same image. bash, curl, jq, kubectl and fio, all from Alpine's apk (fio is the disk check etcd's own documentation uses), so it is small and builds for either architecture. Build context is the repository root so it can COPY the scripts, the regrade core, whose own functions the probe reads memory with and the controller takes, renews and releases the Lease with, and the cluster role's link reader, whose own functions it reads the link with. The Deployment and the DaemonSet pull it without a pull Secret, so the published package must be public.
FROM alpine:3.20

RUN apk add --no-cache bash curl jq kubectl fio

COPY fleet-role/loop.sh /app/loop.sh
COPY scripts/fleet-role.sh scripts/fleet-role-probe.sh /app/scripts/
# The jq programs the scripts run from their own directory, and the module they share.
COPY scripts/fleet-role.jq scripts/fleet-role-swap.jq scripts/fleet-role-stats.jq /app/scripts/
COPY roles/github_runner_cluster/files/regrade/fleet-regrade-core.sh /app/fleet-regrade-core.sh
COPY roles/github_runner_cluster/files/node-link.sh /app/node-link.sh
RUN chmod +x /app/loop.sh /app/scripts/fleet-role.sh /app/scripts/fleet-role-probe.sh

# A non-root user by number, so the Deployment's runAsNonRoot check can verify it without reading /etc/passwd. The DaemonSet runs the same image as root, for the directory it measures the disk in.
USER 65532:65532
WORKDIR /app
ENTRYPOINT ["/app/loop.sh"]
