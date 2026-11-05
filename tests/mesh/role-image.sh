#!/usr/bin/env bash
# Select the image used by the ARC manifests. An explicitly supplied image is pulled by k3s.
# shellcheck disable=SC2034 # The caller reads both selected values.
grtest_select_role_image() {
  if [ "${GRTEST_ROLE_IMAGE+x}" ]; then
    if [ -z "$GRTEST_ROLE_IMAGE" ]; then
      printf '%s\n' 'GRTEST_ROLE_IMAGE must not be empty' >&2
      return 1
    fi
    role_controller_image="$GRTEST_ROLE_IMAGE"
    role_image_published=1
  else
    role_controller_image=grtest/fleet-role:test
    role_image_published=0
  fi
}
