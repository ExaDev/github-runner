#!/bin/sh
# The zig on PATH in the Rust tool image: it runs the Zig beside it with the arguments it was given, adding -lunwind to a link that asks for LLVM's libunwind. rustc links its gnullvm Windows targets with -nolibc and --unwindlib=libunwind and relies on the compiler driver to add the library, as clang does. zig cc ignores --unwindlib, and cargo-zigbuild does not translate it, so without this every such link fails with undefined _Unwind_* symbols. rustc passes a long command line in a response file (@<file>), which is searched too. tests/rust_toolchain/run.sh links both Windows targets; this script can go once it passes without it.
set -eu

for argument in "$@"; do
  case "$argument" in
    --unwindlib=libunwind) ;;
    @*) grep -qF -e --unwindlib=libunwind "${argument#@}" 2>/dev/null || continue ;;
    *) continue ;;
  esac
  set -- "$@" -lunwind
  break
done
exec "$(dirname "$(readlink -f "$0")")/../zig/zig" "$@"
