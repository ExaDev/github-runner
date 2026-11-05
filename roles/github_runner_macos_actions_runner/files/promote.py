#!/usr/bin/python3
"""Atomically publish a staged runner without replacing an existing path."""

import ctypes
import os
import sys


def promote(source: str, destination: str) -> None:
    """Rename a directory exclusively on Darwin, raising on any failure."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # Darwin's RENAME_EXCL refuses an existing destination, including empty directories.
    if rename(os.fsencode(source), os.fsencode(destination), 0x00000004) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


if __name__ == "__main__":
    promote(sys.argv[1], sys.argv[2])
