#!/bin/sh
# Entry point for the k3s and k3s-agent services in docker-compose.yml: starts Tailscale inside the node's own container, then k3s with its --vpn-auth integration, so each node joins the mesh as its own device. Bind-mounted from the checkout rather than baked into the k3s image, so a host picks up a change to it with the Compose file that uses it, without rebuilding an image Compose would not otherwise rebuild.
#
# Usage: node-entrypoint.sh server|agent. Reads, from the container's environment (docker-compose.yml, from .env): K3S_VPN_AUTH_JOIN_KEY, K3S_VPN_AUTH_CONTROL_SERVER_URL (empty for Tailscale's own control plane, a Headscale URL otherwise), K3S_JOIN_SERVER_URL and K3S_TLS_SAN_LIST (server), and K3S_MESH_SELF_HOSTED_HEALTH_URL (the bootstrap server of a cluster that runs its own Headscale, see supervise_self_hosted below).
# No set -e: a failed `tailscale up` falls through to waiting for an address, as it always has, rather than exiting and restarting the container.
set -u

role="${1:?usage: node-entrypoint.sh server|agent}"
join_key="${K3S_VPN_AUTH_JOIN_KEY:-}"
control_server_url="${K3S_VPN_AUTH_CONTROL_SERVER_URL:-}"
self_hosted_health_url="${K3S_MESH_SELF_HOSTED_HEALTH_URL:-}"

# k3s's own vpn-auth integration only runs `tailscale up`; it does not start the daemon itself (without this, k3s fails outright with "tailscale up failed: ... it doesn't appear to be running"). Waits for the control socket before anything talks to it.
mkdir -p /var/run/tailscale /var/lib/tailscale
tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/var/run/tailscale/tailscaled.sock &
until [ -S /var/run/tailscale/tailscaled.sock ]; do sleep 0.5; done

# Only a self-hosted bootstrap server may start without a join key: its first start is what brings up the Headscale server that issues it.
if [ -z "$join_key" ] && [ -z "$self_hosted_health_url" ]; then
  echo "K3S_VPN_AUTH_JOIN_KEY must be set in .env: a reusable, pre-authorised join key for the mesh (see the README's Tailscale section for how it's provisioned)." >&2
  exit 1
fi

# k3s splits --vpn-auth on commas and each pair on "=", so neither may appear inside the key or the URL. controlServerURL makes k3s's own `tailscale up` register with that server (Headscale) instead of Tailscale's.
vpn_auth="name=tailscale,joinKey=$join_key"
if [ -n "$control_server_url" ]; then
  vpn_auth="$vpn_auth,controlServerURL=$control_server_url"
fi

# --resolv-conf points kubelet at a synthetic file containing only Tailscale's own fixed, well-known MagicDNS resolver IP (100.100.100.100, always this value, regardless of tailnet or control server), with no search domains at all. Without this, kubelet's dnsPolicy: ClusterFirst merges the container's own /etc/resolv.conf search list into every pod's resolv.conf, and that file is owned and overwritten by `tailscale up` itself to include the tailnet's configured search domains. Confirmed live: a personal domain configured as a global Tailscale DNS search path carried a wildcard record pointing at a Netlify-hosted site, and Kubernetes' ndots:5 default made a pod's resolver try every search-suffixed name (e.g. "api.github.com.<that domain>") before the bare hostname, so curl connected to Netlify's edge and TLS hostname verification failed. CoreDNS itself was never affected (it forwards queries verbatim with no ndots/search logic of its own) and still resolves every name through the real Tailscale resolver; this flag only stops kubelet copying the search list into pods.
printf 'nameserver 100.100.100.100\n' > /etc/k3s-resolv.conf

# Brings Tailscale up ourselves, ahead of k3s's own --vpn-auth doing the same thing, to learn this node's Tailscale IP for --node-ip before starting k3s. Confirmed against k3s's own vpn.go source: StartVPN checks the backend state first and skips its own `tailscale up` once the state is already "Running", so k3s's own call becomes a no-op rather than a second registration.
#
# Only calls `tailscale up` if the daemon isn't already authenticated from its persisted state volume. Confirmed live: calling it unconditionally on every container restart re-registered a brand new device each time under the reusable join key instead of reconnecting the existing one, leaving a growing trail of stale devices on the tailnet.
join_mesh() {
  if ! mesh_ip=$(tailscale ip -4 2>/dev/null) || [ -z "$mesh_ip" ]; then
    if [ -n "$control_server_url" ]; then
      tailscale up --auth-key="$join_key" --hostname="$(hostname)" --login-server="$control_server_url"
    else
      tailscale up --auth-key="$join_key" --hostname="$(hostname)"
    fi
    until mesh_ip=$(tailscale ip -4 2>/dev/null) && [ -n "$mesh_ip" ]; do sleep 0.5; done
  fi
}

# A joining server has to resolve K3S_JOIN_SERVER_URL's hostname (the bootstrap host's MagicDNS name) before k3s can even attempt to bootstrap against it. Confirmed live: k3s failed outright with "dial tcp: lookup <bootstrap host>: no such host" immediately after `tailscale ip -4` had returned a real address, because Tailscale assigns the IP and wires up its MagicDNS resolver as two separate steps. Only when joining; the bootstrap host has nothing to resolve.
wait_for_join_server() {
  if [ -n "${K3S_JOIN_SERVER_URL:-}" ]; then
    join_host=${K3S_JOIN_SERVER_URL#https://}
    join_host=${join_host%%:*}
    until nslookup "$join_host" >/dev/null 2>&1; do sleep 0.5; done
  fi
}

# Sets server_args to the arguments of `k3s server` on the mesh.
#
# --disable-network-policy: this fleet has no use for Kubernetes NetworkPolicy, and kube-router's enforcement chains sit directly in the FORWARD path every cross-node pod packet takes.
#
# --node-ip forces etcd's peer-advertise address onto this node's mesh IP. k3s builds etcd's --initial-cluster from config.PrivateIP, which is populated from --node-ip before --vpn-auth's own executor code rebuilds NodeIP from its Tailscale detection (traced through pkg/cli/server/server.go and pkg/executor/embed/embed.go). Without it PrivateIP stays the container's bridge IP, which no other machine can reach, and joining a second server failed with "MemberAdd request timed out". An --etcd-arg override is not a substitute: it also applies to the temporary etcd k3s starts on every restart to reconcile with its datastore, which has no TLS certificates for a 0.0.0.0:2380 listener and failed with "cannot listen on TLS for [::]:2380: KeyFile and CertFile are not presented".
#
# --cluster-init (only when K3S_JOIN_SERVER_URL is unset) bootstraps embedded etcd, or converts an existing single-node SQLite datastore to it, the one migration k3s documents; once initialised, k3s ignores it on restart, so it stays in place permanently. A host with K3S_JOIN_SERVER_URL set passes --server <url> instead and joins as another control-plane/etcd member.
#
# --tls-san is added once per entry of K3S_TLS_SAN_LIST: every server's certificate must cover every server's own mesh name, since a client reaching any server directly by that name needs the certificate to match it.
mesh_server_args() {
  set -- --disable=traefik --disable-network-policy --write-kubeconfig-mode=6443 \
    --vpn-auth="$vpn_auth" \
    --resolv-conf=/etc/k3s-resolv.conf \
    --node-ip="$mesh_ip"
  if [ -n "${K3S_JOIN_SERVER_URL:-}" ]; then
    set -- "$@" --server "$K3S_JOIN_SERVER_URL"
  else
    set -- "$@" --cluster-init
  fi
  for san in ${K3S_TLS_SAN_LIST:-}; do set -- "$@" --tls-san "$san"; done
  server_args="$*"
}

# Sets server_args to the arguments of `k3s server` off the mesh, for a self-hosted bootstrap server whose Headscale server is not answering yet. No --vpn-auth, no --node-ip and no mesh resolver, so k3s and the Headscale static pod come up on the container's own network. No --cluster-init either: a fresh server starts on SQLite, and restarting it on the mesh with --cluster-init converts that to etcd with the mesh IP as etcd's peer address from the start, while an already-initialised server finds its etcd datastore regardless.
direct_server_args() {
  set -- --disable=traefik --disable-network-policy --write-kubeconfig-mode=6443
  for san in ${K3S_TLS_SAN_LIST:-}; do set -- "$@" --tls-san "$san"; done
  server_args="$*"
}

control_server_answers() {
  wget -q -T 5 -O /dev/null "$self_hosted_health_url" 2>/dev/null
}

# The bootstrap server of a cluster whose Headscale server runs inside the cluster, as a static pod on this node. k3s cannot start with --vpn-auth until Headscale answers, and Headscale cannot answer until k3s runs its static pod, so this node always starts off the mesh first: k3s runs directly on the container's network, kubelet starts the Headscale static pod, and once Headscale answers (and there is a join key), k3s is stopped and started again on the mesh. Only the k3s process restarts, never the container, so the Headscale pod keeps running throughout and the mesh has a control server to register with. The same sequence covers the first start, when the join key does not exist yet, and every later cold start (a reboot, a recreated container).
supervise_self_hosted() {
  # k3s moves the container's processes out of the root cgroup itself only when it runs as PID 1, which it does not here, and cgroup v2 refuses kubelet's pod cgroups while processes remain in the root. So do what k3s would: move every process into a child cgroup and delegate every controller to the root's children. Without this kubelet fails with "cannot enter cgroupv2 /sys/fs/cgroup/kubepods with domain controllers -- it is in an invalid state" and k3s exits.
  if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    mkdir -p /sys/fs/cgroup/init
    xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || true
    sed -e 's/ / +/g' -e 's/^/+/' < /sys/fs/cgroup/cgroup.controllers > /sys/fs/cgroup/cgroup.subtree_control
  fi
  k3s_pid=""
  watcher_pid=""
  trap 'if [ -n "$k3s_pid" ]; then kill -TERM "$k3s_pid" 2>/dev/null; wait "$k3s_pid"; fi; exit 0' TERM INT
  while :; do
    if [ -n "$join_key" ] && control_server_answers; then
      mode=mesh
      join_mesh
      mesh_server_args
    else
      mode=direct
      direct_server_args
    fi
    echo "Starting k3s server ($mode)"
    rm -f /output/kubeconfig.yaml
    # shellcheck disable=SC2086 # Word splitting of server_args is deliberate: every argument is a flag or a value without spaces.
    /bin/k3s server $server_args &
    k3s_pid=$!
    watcher_pid=""
    if [ "$mode" = direct ] && [ -n "$join_key" ]; then
      (until control_server_answers; do sleep 2; done; echo "The control server answers; restarting k3s on the mesh"; kill -TERM "$k3s_pid") &
      watcher_pid=$!
    fi
    wait "$k3s_pid" || true
    if [ -n "$watcher_pid" ]; then kill "$watcher_pid" 2>/dev/null || true; fi
    sleep 2
  done
}

case "$role" in
  server)
    if [ -n "$self_hosted_health_url" ]; then
      supervise_self_hosted
    fi
    join_mesh
    wait_for_join_server
    rm -f /output/kubeconfig.yaml
    mesh_server_args
    # shellcheck disable=SC2086 # As above.
    exec /bin/k3s server $server_args
    ;;
  agent)
    exec /bin/k3s agent --vpn-auth="$vpn_auth" --resolv-conf=/etc/k3s-resolv.conf
    ;;
  *)
    echo "usage: node-entrypoint.sh server|agent" >&2
    exit 2
    ;;
esac
