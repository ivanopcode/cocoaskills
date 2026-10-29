"""Opaque current-format fixtures; never executed or claimed loader-valid."""
import struct
import sys


def native_header(platform=None):
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 8, 0, 0) + b"\0" * 8
    if platform == "linux":
        return b"\x7fELF\x02\x01\x01" + b"\0" * 9 + struct.pack("<HHIQQQIHHHHHH", 3, 183, 1, 0, 0, 0, 0, 64, 0, 0, 0, 0, 0)
    return b"MZfixture"


def fat_header(count):
    return bytes.fromhex("CAFEBABE") + count.to_bytes(4, "big") + struct.pack(">5I", 0x0100000C, 0, 512, 32, 0) * count
