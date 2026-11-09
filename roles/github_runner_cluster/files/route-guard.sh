#!/bin/sh
# Managed by the exadev.github_runner collection's github_runner_cluster role. Do not edit by hand.
#
# Removes the IPv4 default routes of this machine whose gateway does not answer, provided at least one other default route's gateway does. A bridged interface that has lost its link keeps its default routes, and when they carry a lower metric than the working interface's, all outbound traffic leaves by the dead one. A route is judged only by its gateway: one that answers an ICMP echo through its own interface, or that holds a resolved neighbour entry (a gateway that drops ICMP still answers ARP), is alive. When no default route is alive nothing is removed, so the machine never ends with none, and a route without a gateway is never judged.
# A DHCP client may add a removed route back at its next renewal; the timer that runs this removes it again at the next tick.
set -eu

timeout=${ROUTE_GUARD_PROBE_TIMEOUT:-2}

answers() {
  gateway=$1
  device=$2
  if ping -n -c 1 -W "$timeout" -I "$device" "$gateway" >/dev/null 2>&1; then
    return 0
  fi
  case $(ip -4 neigh show to "$gateway" dev "$device" 2>/dev/null) in
    *FAILED* | *INCOMPLETE* | '') return 1 ;;
    *lladdr*) return 0 ;;
    *) return 1 ;;
  esac
}

routes=$(ip -4 route show default)
live=0
dead=''
newline='
'
while read -r line; do
  [ -n "$line" ] || continue
  gateway=''
  device=''
  metric=''
  # shellcheck disable=SC2086
  set -- $line
  while [ $# -gt 0 ]; do
    case $1 in
      via) gateway=${2:-} ;;
      dev) device=${2:-} ;;
      metric) metric=${2:-} ;;
    esac
    shift
  done
  [ -n "$gateway" ] && [ -n "$device" ] || continue
  if answers "$gateway" "$device"; then
    live=$((live + 1))
  else
    dead="$dead$gateway $device $metric$newline"
  fi
done <<EOF
$routes
EOF

if [ "$live" -eq 0 ]; then
  [ -z "$dead" ] || echo "route-guard: no default route has a gateway that answers, so none is removed" >&2
  exit 0
fi

while read -r gateway device metric; do
  [ -n "$gateway" ] || continue
  if [ -n "$metric" ]; then
    ip -4 route del default via "$gateway" dev "$device" metric "$metric"
  else
    ip -4 route del default via "$gateway" dev "$device"
  fi
  echo "route-guard: removed the default route via $gateway on $device (metric ${metric:-none}): its gateway does not answer"
done <<EOF
$dead
EOF
