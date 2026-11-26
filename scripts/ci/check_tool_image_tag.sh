#!/usr/bin/env bash
# Fails when a change to a tool image's directory changes what it builds but not the tag it is published under. publish-tool-images.yml leaves a tag that already exists alone, so such a change would never be published.
#
# Usage: scripts/ci/check_tool_image_tag.sh <old image directory> <new image directory>. What an image builds is every file in its directory, the Dockerfile without its comment and blank lines.
set -euo pipefail

old="${1:?usage: check_tool_image_tag.sh <old image directory> <new image directory>}"
new="${2:?usage: check_tool_image_tag.sh <old image directory> <new image directory>}"
here="$(dirname "$0")"

instructions() {
  sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' "$1"
}

old_instructions="$(instructions "${old}/Dockerfile")"
new_instructions="$(instructions "${new}/Dockerfile")"
# diff exits 1 for a difference and 2 when it could not compare, which must not read as either answer.
others=0
diff -r -q -x Dockerfile "$old" "$new" >/dev/null || others=$?
if [ "$others" -gt 1 ]; then
  echo "could not compare ${old} with ${new}" >&2
  exit 2
fi
if [ "$old_instructions" = "$new_instructions" ] && [ "$others" -eq 0 ]; then
  echo "${new}: what the image builds is unchanged"
  exit 0
fi
old_tag="$("${here}/tool_image_tag.sh" "${old}/Dockerfile")"
new_tag="$("${here}/tool_image_tag.sh" "${new}/Dockerfile")"
if [ "$old_tag" = "$new_tag" ]; then
  echo "${new} changes what the image builds but is still published as ${new_tag}, which is never rebuilt: raise ARG IMAGE_REVISION or the version" >&2
  exit 1
fi
echo "${new}: published as ${new_tag}, was ${old_tag}"
