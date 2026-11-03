# A stand-in for a Linux host that runs a native k3s node (github_runner_cluster_node_mode: native), for tests/mesh/run.sh native_server: Ubuntu 24.04 with systemd as PID 1, as a dedicated host boots, and what the cluster role needs to manage it over Ansible's docker connection (python3, sudo for become). The role installs Tailscale and k3s itself, as on a real host. Run privileged, with tmpfs on /run and /run/lock and a volume for k3s's data directory, so containerd's overlay snapshots sit on the volume's filesystem rather than on this container's own overlay, which overlayfs cannot stack on.
FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends systemd systemd-sysv dbus python3 sudo ca-certificates curl gnupg iproute2 iptables kmod \
 && rm -rf /var/lib/apt/lists/* \
 && systemctl mask getty.target console-getty.service systemd-firstboot.service systemd-udevd.service systemd-modules-load.service
# The signal systemd shuts down on, which docker stop sends instead of SIGTERM.
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
