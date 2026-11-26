"""Tests of tests/rust_toolchain/inspect_binary.py, which stands between the Rust tool image's cross-compiled executables and a pass: each format is accepted as the target calls for it and refused for a framework or other library outside /usr/lib, a dynamic link, or another processor."""

from __future__ import annotations

import importlib.util
import struct
import sys
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("inspect_binary", ROOT / "tests" / "rust_toolchain" / "inspect_binary.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


inspect_binary = load()

ARM64_MACHO = 0x0100000C
SYSTEM = "/usr/lib/libSystem.B.dylib"
FRAMEWORK = "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"
LC_LOAD_DYLIB = 0xC
LC_LOAD_WEAK_DYLIB = 0x80000018


def macho(*libraries: tuple[int, str], cpu: int = ARM64_MACHO) -> bytes:
    commands = b""
    for command, library in libraries:
        name = library.encode() + b"\0"
        name += b"\0" * (-len(name) % 8)
        commands += struct.pack("<IIIIII", command, 24 + len(name), 24, 0, 0, 0) + name
    return struct.pack("<IIIIIIII", 0xFEEDFACF, cpu, 0, 2, len(libraries), len(commands), 0, 0) + commands


def elf(segments: list[int], dynamic_tags: tuple[int, ...] = (), machine: int = 62) -> bytes:
    dynamic_offset = 64 + 56 * len(segments)
    dynamic = b"".join(struct.pack("<qQ", tag, 0) for tag in (*dynamic_tags, 0))
    header = b"\x7fELF\x02\x01\x01" + b"\0" * 9 + struct.pack("<HHIQQQIHHHHHH", 2, machine, 1, 0, 64, 0, 0, 64, 56, len(segments), 0, 0, 0)
    programs = b"".join(struct.pack("<IIQQQQQQ", kind, 0, dynamic_offset, 0, 0, len(dynamic), 0, 0) for kind in segments)
    return header + programs + dynamic


def pe(machine: int) -> bytes:
    return b"MZ" + b"\0" * 58 + struct.pack("<I", 64) + b"PE\0\0" + struct.pack("<H", machine)


class MachOTest(unittest.TestCase):
    def test_the_system_library_alone_passes(self) -> None:
        self.assertIn(SYSTEM, inspect_binary.check("aarch64-apple-darwin", macho((LC_LOAD_DYLIB, SYSTEM))))

    def test_a_framework_is_refused_however_it_is_loaded(self) -> None:
        for command in (LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB, 0x20, 0x8000001F, 0x80000023):
            with self.subTest(command=hex(command)), self.assertRaisesRegex(inspect_binary.BinaryError, "CoreFoundation"):
                inspect_binary.check("aarch64-apple-darwin", macho((LC_LOAD_DYLIB, SYSTEM), (command, FRAMEWORK)))

    def test_a_library_outside_the_system_directory_is_refused(self) -> None:
        with self.assertRaisesRegex(inspect_binary.BinaryError, "libfoo"):
            inspect_binary.check("aarch64-apple-darwin", macho((LC_LOAD_DYLIB, SYSTEM), (LC_LOAD_DYLIB, "@rpath/libfoo.dylib")))

    def test_no_library_at_all_is_refused(self) -> None:
        with self.assertRaisesRegex(inspect_binary.BinaryError, "no library"):
            inspect_binary.check("aarch64-apple-darwin", macho())

    def test_another_processor_is_refused(self) -> None:
        with self.assertRaisesRegex(inspect_binary.BinaryError, "x86_64"):
            inspect_binary.check("x86_64-apple-darwin", macho((LC_LOAD_DYLIB, SYSTEM)))


class ElfTest(unittest.TestCase):
    def test_static_executables_pass(self) -> None:
        for data in (elf([1]), elf([1, 2], dynamic_tags=(5,))):
            self.assertIn("static", inspect_binary.check("x86_64-unknown-linux-musl", data))

    def test_an_interpreter_is_refused(self) -> None:
        with self.assertRaisesRegex(inspect_binary.BinaryError, "interpreter"):
            inspect_binary.check("x86_64-unknown-linux-musl", elf([1, 3]))

    def test_a_needed_library_is_refused(self) -> None:
        with self.assertRaisesRegex(inspect_binary.BinaryError, "shared library"):
            inspect_binary.check("x86_64-unknown-linux-musl", elf([1, 2], dynamic_tags=(5, 1)))

    def test_another_processor_is_refused(self) -> None:
        with self.assertRaisesRegex(inspect_binary.BinaryError, "aarch64"):
            inspect_binary.check("aarch64-unknown-linux-musl", elf([1]))


class PeTest(unittest.TestCase):
    def test_the_target_processor_passes(self) -> None:
        self.assertEqual(inspect_binary.check("x86_64-pc-windows-gnullvm", pe(0x8664)), "PE x86_64")
        self.assertEqual(inspect_binary.check("aarch64-pc-windows-gnullvm", pe(0xAA64)), "PE aarch64")

    def test_another_processor_or_format_is_refused(self) -> None:
        for data in (pe(0x8664), elf([1])):
            with self.assertRaises(inspect_binary.BinaryError):
                inspect_binary.check("aarch64-pc-windows-gnullvm", data)

    def test_an_unknown_target_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            inspect_binary.check("x86_64-pc-windows-msvc", pe(0x8664))


if __name__ == "__main__":
    unittest.main()
