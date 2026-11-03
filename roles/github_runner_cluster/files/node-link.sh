#!/usr/bin/env bash
# The medium of a host's network link, wired, wifi or unknown, read the same way by the cluster role on the host (which publishes it as the github_runner_cluster_link_label Node label, see tasks/link.yml) and by the role controller's probe inside the node (scripts/fleet-role-probe.sh, which sources this file). Bash 3.2 compatible, since the role runs it on macOS, whose own bash is that release.
#
# Run, it prints {"interface": "<name>", "link": "<medium>"} for the interface given as its argument, or for the interface the default route leaves by when none is given (empty, with the medium unknown, when there is no default route). Sourced, it defines the functions below and does nothing else.
#
# On Linux the medium is read from the interface's own /sys/class/net entry: wifi when the kernel gives it a wireless phy, wired when it is backed by a device, unknown when it is virtual (a veth in a container, a bridge, a tunnel) or absent, since then the link beneath it cannot be seen from there. On macOS it is read from the hardware port networksetup lists the interface under: wifi for Wi-Fi, wired for a port whose name says Ethernet or LAN (built-in Ethernet, Thunderbolt Ethernet, a USB LAN adapter), unknown for any other (a Thunderbolt port or bridge, an iPhone's USB link) and for an interface it does not list.
#
# Env:
# - NODE_LINK_SYSTEM: the kernel name to read as (default the output of uname -s), so the macOS reading can be tested on Linux.
# - SYS_CLASS_NET, PROC_NET_ROUTE: where Linux's /sys/class/net and /proc/net/route are (the defaults).

NODE_LINK_SYSTEM="${NODE_LINK_SYSTEM:-$(uname -s)}"
SYS_CLASS_NET="${SYS_CLASS_NET:-/sys/class/net}"
PROC_NET_ROUTE="${PROC_NET_ROUTE:-/proc/net/route}"

# Linux: the interface the default route leaves by, the one with the lowest metric among the routes that are up and have a gateway; empty when there is none. /proc/net/route is hexadecimal, tab separated, with a header line.
linux_default_route_interface() {
  local iface destination _gateway flags _refcnt _use metric mask best='' best_metric=''
  while read -r iface destination _gateway flags _refcnt _use metric mask _; do
    [ "$destination" = 00000000 ] && [ "$mask" = 00000000 ] || continue
    (( (0x$flags & 3) == 3 )) || continue
    if [ -z "$best" ] || [ "$metric" -lt "$best_metric" ]; then
      best=$iface
      best_metric=$metric
    fi
  done < "$PROC_NET_ROUTE"
  echo "$best"
}

# Linux: the medium of an interface. Reads the interface's own directory and resolves it with cd -P, never a find over /sys, whose symlinks (device, subsystem, phy80211) loop back on themselves.
linux_link_type() {
  local iface=$1 dir="${SYS_CLASS_NET}/$1" real
  if [ -z "$iface" ] || [ ! -d "$dir" ]; then
    echo unknown
    return
  fi
  if [ -e "${dir}/phy80211" ] || [ -d "${dir}/wireless" ] || grep -qx 'DEVTYPE=wlan' "${dir}/uevent" 2> /dev/null; then
    echo wifi
    return
  fi
  real="$(cd -P "$dir" && pwd -P)"
  case "$real" in
    */devices/virtual/*) echo unknown ;;
    *) echo wired ;;
  esac
}

# macOS: the interface the default route leaves by, from route's own answer; empty when there is none, for which route fails ("not in table").
macos_default_route_interface() {
  { route -n get default 2> /dev/null || true; } | awk '$1 == "interface:" { print $2; exit }'
}

# macOS: the medium of an interface, from the name of the hardware port networksetup lists it under. Each port is a "Hardware Port: <name>" line followed by its "Device: <interface>" line.
macos_link_type() {
  local iface=$1 port
  if [ -z "$iface" ]; then
    echo unknown
    return
  fi
  port="$(networksetup -listallhardwareports | awk -v iface="$iface" '
    /^Hardware Port: / { port = substr($0, length("Hardware Port: ") + 1) }
    /^Device: / && substr($0, length("Device: ") + 1) == iface { print port; exit }')"
  case "$port" in
    *Wi-Fi* | *AirPort*) echo wifi ;;
    *Ethernet* | *LAN*) echo wired ;;
    *) echo unknown ;;
  esac
}

default_route_interface() {
  if [ "$NODE_LINK_SYSTEM" = Darwin ]; then macos_default_route_interface; else linux_default_route_interface; fi
}

link_type() {
  if [ "$NODE_LINK_SYSTEM" = Darwin ]; then macos_link_type "$1"; else linux_link_type "$1"; fi
}

# Run rather than sourced: the reading for the interface given, or the default route's.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  node_link_interface="${1:-$(default_route_interface)}"
  printf '{"interface": "%s", "link": "%s"}\n' "$node_link_interface" "$(link_type "$node_link_interface")"
fi
