#!/usr/bin/env python3
"""Check that a cross-compiled executable is what its Rust target calls for, from the file's own headers, so a binary no machine here can run is still checked.

    inspect_binary.py TARGET FILE

For an Apple target FILE must be a 64-bit Mach-O executable of the target's processor that loads only libraries under /usr/lib: the system library has a stub in Zig, while a framework (or any other library) needs Apple's SDK, so loading one fails the check. At least one library must be loaded, so a file whose load commands could not be read never passes as loading nothing. For a Linux musl target it must be a 64-bit little-endian ELF of the target's processor with no interpreter and no needed shared library. For a Windows target it must be a PE image of the target's processor.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

# <mach-o/loader.h>: the 64-bit magic, the executable file type, and the load commands that name a library to load.
_MACHO_MAGIC_64 = 0xFEEDFACF
_MACHO_EXECUTE = 2
_MACHO_LOAD_DYLIB = frozenset({0xC, 0x20, 0x80000018, 0x8000001F, 0x80000023})
_MACHO_CPU = {"aarch64": 0x0100000C, "x86_64": 0x01000007}
_MACHO_SYSTEM_LIBRARIES = "/usr/lib/"

# The ELF specification: machine numbers, and the program header and dynamic section tags a dynamically linked executable has.
_ELF_MACHINE = {"aarch64": 183, "x86_64": 62}
_ELF_PT_DYNAMIC = 2
_ELF_PT_INTERP = 3
_ELF_DT_NULL = 0
_ELF_DT_NEEDED = 1

# The PE format's IMAGE_FILE_MACHINE values.
_PE_MACHINE = {"aarch64": 0xAA64, "x86_64": 0x8664}


class BinaryError(Exception):
    """The file is not what its target calls for."""


def macho_libraries(data: bytes, processor: str) -> list[str]:
    """The libraries a Mach-O executable of the processor loads, in load command order. Raises BinaryError for any other file."""
    if len(data) < 32:
        raise BinaryError("too short for a Mach-O header")
    magic, cpu, _, file_type, command_count, _, _, _ = struct.unpack_from("<IIIIIIII", data, 0)
    if magic != _MACHO_MAGIC_64:
        raise BinaryError(f"not a 64-bit Mach-O file (magic {magic:#x})")
    if cpu != _MACHO_CPU[processor]:
        raise BinaryError(f"Mach-O processor type {cpu:#x} is not {processor}")
    if file_type != _MACHO_EXECUTE:
        raise BinaryError(f"Mach-O file type {file_type} is not an executable")
    libraries = []
    offset = 32
    for _ in range(command_count):
        command, size = struct.unpack_from("<II", data, offset)
        if size < 8 or offset + size > len(data):
            raise BinaryError(f"load command at {offset} has an impossible size {size}")
        if command in _MACHO_LOAD_DYLIB:
            (name_offset,) = struct.unpack_from("<I", data, offset + 8)
            libraries.append(data[offset + name_offset : offset + size].split(b"\0", 1)[0].decode())
        offset += size
    return libraries


def check_macho(data: bytes, processor: str) -> str:
    libraries = macho_libraries(data, processor)
    if not libraries:
        raise BinaryError("loads no library at all, not even the system library")
    outside = [library for library in libraries if not library.startswith(_MACHO_SYSTEM_LIBRARIES) or ".framework/" in library]
    if outside:
        raise BinaryError(f"loads an Apple framework or a library outside {_MACHO_SYSTEM_LIBRARIES}: {', '.join(outside)}")
    return f"Mach-O {processor}, loads {', '.join(libraries)}"


def check_elf(data: bytes, processor: str) -> str:
    # \x02\x01 is the 64-bit class and little-endian data encoding.
    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01":
        raise BinaryError("not a 64-bit little-endian ELF file")
    (machine,) = struct.unpack_from("<H", data, 18)
    if machine != _ELF_MACHINE[processor]:
        raise BinaryError(f"ELF machine {machine} is not {processor}")
    (headers_offset,) = struct.unpack_from("<Q", data, 32)
    header_size, header_count = struct.unpack_from("<HH", data, 54)
    if header_count == 0:
        raise BinaryError("ELF file has no program headers")
    for index in range(header_count):
        start = headers_offset + index * header_size
        (kind,) = struct.unpack_from("<I", data, start)
        if kind == _ELF_PT_INTERP:
            raise BinaryError("dynamically linked: it names an interpreter")
        if kind == _ELF_PT_DYNAMIC:
            # A static position-independent executable has a dynamic section too, for its own relocations; only a needed library makes it dynamically linked.
            (section_offset,) = struct.unpack_from("<Q", data, start + 8)
            (section_size,) = struct.unpack_from("<Q", data, start + 32)
            for entry in range(section_offset, section_offset + section_size, 16):
                (tag,) = struct.unpack_from("<q", data, entry)
                if tag == _ELF_DT_NULL:
                    break
                if tag == _ELF_DT_NEEDED:
                    raise BinaryError("dynamically linked: it needs a shared library")
    return f"static ELF {processor}"


def check_pe(data: bytes, processor: str) -> str:
    if len(data) < 64 or data[:2] != b"MZ":
        raise BinaryError("not a PE file (no MZ header)")
    (header,) = struct.unpack_from("<I", data, 0x3C)
    if data[header : header + 4] != b"PE\0\0":
        raise BinaryError("not a PE file (no PE signature)")
    (machine,) = struct.unpack_from("<H", data, header + 4)
    if machine != _PE_MACHINE[processor]:
        raise BinaryError(f"PE machine {machine:#x} is not {processor}")
    return f"PE {processor}"


def check(target: str, data: bytes) -> str:
    """A description of the file when it is what the target calls for; raises BinaryError when it is not, or ValueError for a target there is no check for."""
    processor = target.split("-", 1)[0]
    if processor in _MACHO_CPU:
        if target.endswith("-apple-darwin"):
            return check_macho(data, processor)
        if target.endswith("-unknown-linux-musl"):
            return check_elf(data, processor)
        if target.endswith("-pc-windows-gnullvm"):
            return check_pe(data, processor)
    raise ValueError(f"no check for target {target}")


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        print("usage: inspect_binary.py TARGET FILE", file=sys.stderr)
        return 2
    target, path = arguments
    try:
        description = check(target, Path(path).read_bytes())
    except BinaryError as error:
        print(f"{target}: {path}: {error}", file=sys.stderr)
        return 1
    print(f"{target}: {description}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
