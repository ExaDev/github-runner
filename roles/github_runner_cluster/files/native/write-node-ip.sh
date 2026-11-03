#!/bin/sh
# Writes the k3s configuration drop-in that sets a native server's node-ip to the host's own Tailscale IPv4 address, from the server unit's ExecStartPre (templates/native/k3s.service.j2), before every start.
#
# k3s builds etcd's peer URL from its PrivateIP, which it takes from --node-ip before the --vpn-auth integration sets the advertise address (pkg/cli/server/server.go at v1.34.1+k3s1: `serverConfig.ControlConfig.PrivateIP = util.GetFirstValidIPString(cmds.AgentConfig.NodeIP.Value())`, and `Etcd IP (PrivateIP) remains the local IP` otherwise; the peer URL is `https://<address>:2380`, peerURL in pkg/etcd/etcd.go). Without node-ip a native server's etcd would advertise a LAN address, which breaks the member's peer traffic as soon as a laptop changes network; the Docker servers pass --node-ip for the same reason (k3s/node-entrypoint.sh). k3s reads every *.yaml in the directory beside its --config file after the file itself, in name order (dotDFiles and readConfigFile in pkg/configfilearg/parser.go), so this drop-in sets node-ip without the role writing the address into server.yaml.
#
# The address is the one the host's Tailscale client has now. A client with none (a host whose client has never been logged in to the cluster's tailnet) is logged in first with exactly the `tailscale up` k3s's own --vpn-auth start runs (StartVPN in pkg/vpn/vpn.go: `up --authkey <joinKey> --timeout=30s --reset`, then `--login-server <controlServerURL>` when set and the words after `,extraArgs=`), from the same --vpn-auth file, since k3s runs that only after it has read node-ip. A logged-in client keeps its device and address through that call, so it registers nothing new.
#
# Usage: write-node-ip.sh OUTPUT_FILE VPN_AUTH_FILE. Reads GITHUB_RUNNER_TAILSCALE, the real Tailscale command, from the unit's environment. The join key is never printed. The file is replaced in one rename, so k3s never reads half of it.
set -eu

output=${1:?usage: write-node-ip.sh OUTPUT_FILE VPN_AUTH_FILE}
vpn_auth_file=${2:?usage: write-node-ip.sh OUTPUT_FILE VPN_AUTH_FILE}
tailscale=${GITHUB_RUNNER_TAILSCALE:?GITHUB_RUNNER_TAILSCALE must name the real tailscale command}

address() { "$tailscale" ip -4 2>/dev/null | head -n 1; }

ip=$(address || true)
if [ -z "$ip" ]; then
  vpn_auth=$(cat "$vpn_auth_file")
  # The parameters before ",extraArgs=" are comma-separated key=value pairs; the words after it are passed to `tailscale up` as they are (processCLIArgs and getVPNAuthInfo in pkg/vpn/vpn.go).
  case "$vpn_auth" in
    *",extraArgs="*) parameters=${vpn_auth%%,extraArgs=*}; extra=${vpn_auth#*,extraArgs=} ;;
    *) parameters=$vpn_auth; extra="" ;;
  esac
  join_key=""
  control_server_url=""
  old_ifs=$IFS
  IFS=,
  for parameter in $parameters; do
    case "$parameter" in
      joinKey=*) join_key=${parameter#joinKey=} ;;
      controlServerURL=*) control_server_url=${parameter#controlServerURL=} ;;
    esac
  done
  IFS=$old_ifs
  [ -n "$join_key" ] || { echo "write-node-ip: ${vpn_auth_file} names no joinKey" >&2; exit 1; }
  set -- up --authkey "$join_key" --timeout=30s --reset
  if [ -n "$control_server_url" ]; then set -- "$@" --login-server "$control_server_url"; fi
  # shellcheck disable=SC2086 # The extra arguments are separate words, as k3s splits them (strings.Fields).
  set -- "$@" $extra
  "$tailscale" "$@" >/dev/null
  ip=$(address)
fi

case "$ip" in
  *.*.*.*) ;;
  *) echo "write-node-ip: the Tailscale client reports no IPv4 address ('${ip}')" >&2; exit 1 ;;
esac

mkdir -p "$(dirname "$output")"
temporary="${output}.tmp"
printf '# Written by write-node-ip.sh before every start of the server unit: the host'"'"'s Tailscale IPv4 address, which etcd advertises as its peer URL.\nnode-ip: "%s"\n' "$ip" > "$temporary"
mv "$temporary" "$output"
