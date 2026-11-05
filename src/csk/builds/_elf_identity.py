"""Bounded ELF evidence for Linux identity; deliberately disconnected from admission.

Captured bytes establish linkage, not actual loaded-symbol ownership. The latter
remains a mandatory worker proof before this module can be admitted to builds.
"""
from __future__ import annotations

from dataclasses import dataclass
import struct

from .go_v1 import CODE_WORKER_IDENTITY_INVALID, GoV1Error

MAX_IMAGE = 128 * 1024 * 1024
MAX_HEADERS = 256
MAX_DYNAMIC = 4096
MAX_STRINGS = 4 * 1024 * 1024
MAX_SYMBOLS = 100000


def refuse(reason: str) -> GoV1Error:
    return GoV1Error(CODE_WORKER_IDENTITY_INVALID, "Linux ELF identity: " + reason)


@dataclass(frozen=True)
class Segment:
    kind: int
    flags: int
    offset: int
    address: int
    size: int
    memory_size: int


@dataclass(frozen=True)
class ELF:
    machine: int
    kind: int
    interpreter: str | None
    needed: tuple[str, ...]
    soname: str | None
    rpath: str | None
    runpath: str | None
    defines_python: bool
    segments: tuple[Segment, ...]


def parse_elf(data: bytes) -> ELF:
    """Accept only bounded little-endian ELF64 x86-64/AArch64 images."""
    def span(offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
            raise refuse("truncated or overflowing range")
        return data[offset:offset + size]

    def unpack(fmt: str, offset: int) -> tuple[int, ...]:
        return struct.unpack(fmt, span(offset, struct.calcsize(fmt)))

    if len(data) > MAX_IMAGE:
        raise refuse("image exceeds bound")
    ident = span(0, 16)
    if ident[:7] != b"\x7fELF\x02\x01\x01" or ident[7] not in (0, 3) or any(ident[8:]):
        raise refuse("unsupported class, endian, version or ABI")
    kind, machine, version, entry, phoff, shoff, flags, ehsize, phsize, phnum, *_ = unpack("<HHIQQQIHHHHHH", 16)
    if (kind not in (2, 3) or machine not in (62, 183) or version != 1
            or flags != 0 or ehsize != 64 or phsize != 56
            or not 0 < phnum <= MAX_HEADERS or phoff < 64):
        raise refuse("unsupported header or machine")
    span(phoff, phsize * phnum)
    segments = []
    for i in range(phnum):
        typ, flg, off, addr, _, size, mem, align = unpack("<IIQQQQQQ", phoff + i * phsize)
        span(off, size)
        if (size > mem or addr + mem >= 1 << 64 or flg & ~7
                or (align not in (0, 1) and (align & (align - 1) or off % align != addr % align))):
            raise refuse("invalid segment bounds")
        segments.append(Segment(typ, flg, off, addr, size, mem))
    loads = [s for s in segments if s.kind == 1]
    if not loads:
        raise refuse("missing load segments")

    def mapped(address: int, size: int) -> int:
        if size <= 0 or address + size >= 1 << 64:
            raise refuse("invalid virtual range")
        covering = sorted((s for s in loads if s.address < address + size
                           and s.address + s.size > address), key=lambda s: s.address)
        cursor = address
        delta: int | None = None
        for segment in covering:
            if segment.address > cursor:
                raise refuse("unmapped virtual range")
            translation = segment.offset - segment.address
            if delta is not None and translation != delta:
                raise refuse("ambiguous or noncontiguous virtual mapping")
            delta = translation
            cursor = max(cursor, segment.address + segment.size)
        if cursor < address + size or delta is None:
            raise refuse("unmapped virtual range")
        return address + delta

    def name(raw: bytes, offset: int = 0) -> str:
        if not 0 <= offset < len(raw):
            raise refuse("string offset outside table")
        end = raw.find(b"\0", offset)
        if end < 0 or end - offset > 4096:
            raise refuse("unterminated or oversized name")
        try:
            return raw[offset:end].decode("ascii")
        except UnicodeDecodeError as exc:
            raise refuse("non-ASCII loader name") from exc

    interp = [s for s in segments if s.kind == 3]
    dynamic = [s for s in segments if s.kind == 2]
    if len(interp) > 1 or len(dynamic) != 1:
        raise refuse("duplicate interpreter or missing/duplicate dynamic table")
    interpreter = None
    if interp:
        s = interp[0]
        interpreter = name(span(s.offset, s.size))
        if s.size != len(interpreter) + 1:
            raise refuse("trailing interpreter bytes")
    d = dynamic[0]
    if mapped(d.address, d.size) != d.offset:
        raise refuse("dynamic table differs from loaded mapping")
    if d.size % 16 or not 0 < d.size // 16 <= MAX_DYNAMIC:
        raise refuse("invalid dynamic bounds")
    tags: dict[int, int] = {}
    needed_offsets = []
    terminated = False
    for pos in range(d.offset, d.offset + d.size, 16):
        tag, value = unpack("<qQ", pos)
        if tag == 0:
            terminated = True
            break
        if tag == 1:
            needed_offsets.append(value)
        else:
            if tag in tags:
                raise refuse("duplicate dynamic declaration")
            tags[tag] = value
    if not terminated or 5 not in tags or not 0 < tags.get(10, 0) <= MAX_STRINGS:
        raise refuse("missing dynamic terminator or string table")
    if (any(tag in tags for tag in (16, 0x6FFFFEFC, 0x6FFFFEFB, 0x7FFFFFFD, 0x7FFFFFFF))
            or tags.get(30, 0) & ~0x18
            or tags.get(0x6FFFFFFB, 0) & ~(1 | 0x08000000)):
        raise refuse("unsupported loader scope, audit or filter flags")
    strings = span(mapped(tags[5], tags[10]), tags[10])
    needed = tuple(name(strings, off) for off in needed_offsets)
    if len(set(needed)) != len(needed) or any(not n or "/" in n for n in needed):
        raise refuse("duplicate or path-valued dependency")
    names = {tag: name(strings, tags[tag]) if tag in tags else None for tag in (14, 15, 29)}
    # A defined exported function is positive embedding evidence, never a
    # filename/absence inference. Symbol ownership still requires worker proof.
    defines_python = False
    if 6 in tags:
        if tags.get(11) != 24:
            raise refuse("unsupported symbol size")
        symoff = mapped(tags[6], 24)
        # Dynamic hashes bound the symbol table without section headers.
        if 4 in tags:
            hash_off = mapped(tags[4], 8)
            buckets, count = unpack("<II", hash_off)
            if not 0 < buckets <= MAX_SYMBOLS or not 0 < count <= MAX_SYMBOLS:
                raise refuse("symbol count exceeds bound")
            mapped(tags[4], 8 + 4 * (buckets + count))
        elif 0x6FFFFEF5 in tags:
            address = tags[0x6FFFFEF5]
            buckets, first, bloom, shift = unpack("<IIII", mapped(address, 16))
            if not 0 < buckets <= MAX_SYMBOLS or not 0 < bloom <= MAX_SYMBOLS or first > MAX_SYMBOLS:
                raise refuse("GNU hash bounds")
            bucket_address = address + 16 + bloom * 8
            bucket_off = mapped(bucket_address, buckets * 4)
            starts = unpack("<" + "I" * buckets, bucket_off)
            count = first
            budget = MAX_SYMBOLS
            for start in starts:
                if start == 0:
                    continue
                if not first <= start < MAX_SYMBOLS:
                    raise refuse("GNU hash symbol index")
                index = start
                while True:
                    budget -= 1
                    if budget < 0 or index >= MAX_SYMBOLS:
                        raise refuse("GNU hash chain bound")
                    chain_address = bucket_address + buckets * 4 + (index - first) * 4
                    value, = unpack("<I", mapped(chain_address, 4))
                    index += 1
                    if value & 1:
                        break
                count = max(count, index)
        else:
            raise refuse("missing bounded symbol index")
        mapped(tags[6], count * 24)
        for i in range(count):
            nm, info, other, section, value, size = unpack("<IBBHQQ", symoff + i * 24)
            symbol = name(strings, nm)
            if symbol == "Py_GetVersion" and section != 0:
                if defines_python or info != 0x12 or other != 0 or not size:
                    raise refuse("ambiguous Python symbol")
                mapped(value, size)
                if not any(s.flags & 1 and s.address <= value and value + size <= s.address + s.size for s in loads):
                    raise refuse("Python symbol is not executable")
                defines_python = True
    return ELF(machine, kind, interpreter, needed, names[14], names[15], names[29], defines_python, tuple(segments))
