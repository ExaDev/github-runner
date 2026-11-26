#!/usr/bin/env bash
# Prints the version tag publish-tool-images.yml publishes a tool image under: the default of the Dockerfile's one ARG <TOOL>_VERSION, followed by -r<IMAGE_REVISION> where the Dockerfile also declares ARG IMAGE_REVISION (an image holding several tools, whose other pins can change while the version that names it does not).
#
# Usage: scripts/ci/tool_image_tag.sh <Dockerfile>. Fails unless the Dockerfile declares exactly one ARG <TOOL>_VERSION with a default, and at most one ARG IMAGE_REVISION, whose default is a number.
set -euo pipefail

dockerfile="${1:?usage: tool_image_tag.sh <Dockerfile>}"
version="$(sed -n 's/^ARG [A-Z_]*_VERSION=//p' "$dockerfile")"
if [ -z "$version" ] || [ "$(wc -l <<<"$version")" -ne 1 ]; then
  echo "${dockerfile} must pin exactly one ARG <TOOL>_VERSION" >&2
  exit 1
fi
if grep -q '^ARG IMAGE_REVISION' "$dockerfile"; then
  revision="$(sed -n 's/^ARG IMAGE_REVISION=//p' "$dockerfile")"
  if ! [[ "$revision" =~ ^[0-9]+$ ]]; then
    echo "${dockerfile} must give ARG IMAGE_REVISION one number" >&2
    exit 1
  fi
  version="${version}-r${revision}"
fi
echo "$version"
