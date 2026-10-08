#!/usr/bin/env bash
# Shared by the per-payload checks, sourced rather than run, as the runner user after the job-started hook's environment has been applied (tests/system_libraries/run.sh). Each payload's check file defines check_stock, run in the stock actions-runner image, which has no C compiler, and check_compiled, run in the reference build runner image (tool-images/build), which adds build-essential and pkg-config to that same image.

sysroot=/opt/sysroot

# Fails unless every shared library and program under the sysroot resolves all of its dependencies, from the sysroot or the runner image itself.
check_dependencies_resolve() {
  local file missing=0
  while IFS= read -r -d '' file; do
    # ELF files only: the first four bytes are 0x7f 'E' 'L' 'F'.
    [ "$(head -c 4 "$file" | od -An -c | tr -d ' ')" = '177ELF' ] || continue
    if ldd "$file" 2>&1 | grep -q 'not found'; then
      echo "unresolved dependencies in ${file}:" >&2
      ldd "$file" | grep 'not found' >&2
      missing=1
    fi
  done < <(find "${sysroot}/bin" "${sysroot}/lib" -type f -print0)
  [ "$missing" = 0 ]
  echo "every program and library under ${sysroot} resolves its dependencies"
}

# Fails unless the search paths the job-started hook set start with the sysroot's directories.
check_search_paths() {
  [ "${PATH%%:*}" = "${sysroot}/bin" ]
  [ "${LD_LIBRARY_PATH%%:*}" = "${sysroot}/lib" ]
  [ "${LIBRARY_PATH%%:*}" = "${sysroot}/lib" ]
  [ "${CPATH%%:*}" = "${sysroot}/include" ]
  [ "${PKG_CONFIG_PATH%%:*}" = "${sysroot}/lib/pkgconfig" ]
  [ "${CMAKE_PREFIX_PATH%%:*}" = "${sysroot}" ]
  echo "the hook put ${sysroot} on PATH, LD_LIBRARY_PATH, LIBRARY_PATH, CPATH, PKG_CONFIG_PATH and CMAKE_PREFIX_PATH"
}

# Fails unless pkg-config resolves the module's library directory inside the sysroot, which shows its .pc file was made relocatable. The payloads' .pc files name their prefix as ${pcfiledir}/../.., which pkg-config does not simplify, so each -L directory is resolved before it is compared.
check_pkg_config() {
  local module="$1" flags flag
  # A step such as actions/setup-python replaces PKG_CONFIG_PATH, so the module must resolve without it.
  PKG_CONFIG_PATH=/nonexistent/lib/pkgconfig pkg-config --exists "$module"
  flags="$(pkg-config --cflags --libs "$module")"
  echo "pkg-config ${module}: ${flags}"
  for flag in $flags; do
    case "$flag" in
      -L*) [ "$(realpath "${flag#-L}")" != "${sysroot}/lib" ] || return 0 ;;
    esac
  done
  echo "pkg-config ${module} names no library directory resolving to ${sysroot}/lib" >&2
  return 1
}
