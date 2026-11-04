#!/usr/bin/env bash
# node-identity.sh [--stopped] CONTAINER PATH VOLUME PROJECT VOLUME_KEY: copies what the directory PATH holds in the node container CONTAINER (running or stopped) into the named volume VOLUME, for the cluster role's seeding of a node's password volume (tasks/node_password.yml) and for the role actuator's switch of a Docker host between its server and agent services (files/role/fleet-role-actuator-docker.sh), which moves the node's password (/etc/rancher/node) and its Tailscale state (/var/lib/tailscale) from the volumes of one service to the other's, so the node keeps its name in the cluster and its device on the tailnet.
#
# The files go from one docker cp straight into another, so they pass through a pipe between the two commands and are on no disk of the host, in no command line and in no output. The throwaway container that receives them is created from CONTAINER's own image and never started: docker cp writes through a created container's mounts, so the copy lands in the volume with the ownership and mode the tar stream carries, on any engine (Linux, Docker Desktop, Colima). A volume this script creates carries the labels Compose puts on the volumes it creates (com.docker.compose.project=PROJECT, com.docker.compose.volume=VOLUME_KEY), so Compose adopts it without warning, and is removed again when the copy fails, so a later attempt starts from the same state. A volume that exists already keeps its other files: the copy replaces each file CONTAINER holds at PATH, which for a node password and a Tailscale state is the whole identity.
#
# --stopped refuses, with nothing changed, while CONTAINER runs or any running container mounts VOLUME, so that neither side of a switch can write its identity while it is copied.
#
# Exit status: 0 copied, 1 the copy failed (docker's own message on standard error), 2 refused or bad usage.
set -euo pipefail

stopped=0
if [ "${1:-}" = --stopped ]; then
  stopped=1
  shift
fi
if [ $# -ne 5 ]; then
  echo "usage: node-identity.sh [--stopped] CONTAINER PATH VOLUME PROJECT VOLUME_KEY" >&2
  exit 2
fi
container=$1 path=$2 volume=$3 project=$4 volume_key=$5

if [ "$stopped" = 1 ]; then
  if [ "$(docker ps --all --filter "name=^/${container}\$" --format '{{.State}}')" = running ]; then
    echo "node-identity: ${container} is running, so its files at ${path} could change while they are copied" >&2
    exit 2
  fi
  users=$(docker ps --filter "volume=${volume}" --format '{{.Names}}')
  if [ -n "$users" ]; then
    echo "node-identity: ${volume} is mounted by the running container ${users//$'\n'/, }, which could change it while it is written" >&2
    exit 2
  fi
fi

copy="${volume}-identity-copy"
created=0
cleanup() {
  local rc=$?
  docker rm --force "$copy" > /dev/null 2>&1 || true
  if [ "$rc" -ne 0 ] && [ "$created" = 1 ]; then
    docker volume rm "$volume" > /dev/null 2>&1 || true
  fi
  exit "$rc"
}
trap cleanup EXIT

docker rm --force "$copy" > /dev/null 2>&1 || true
image=$(docker inspect --format '{{.Image}}' "$container")
if ! docker volume ls --quiet --filter "name=${volume}" | grep -Fx -- "$volume" > /dev/null; then
  docker volume create --label "com.docker.compose.project=${project}" --label "com.docker.compose.volume=${volume_key}" "$volume" > /dev/null
  created=1
fi
docker create --name "$copy" --volume "${volume}:${path}" "$image" > /dev/null
docker cp --archive "${container}:${path}/." - | docker cp --archive - "${copy}:${path}"
