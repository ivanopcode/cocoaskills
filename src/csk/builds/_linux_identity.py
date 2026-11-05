"""Experimental Linux image resolver, not wired into build admission.

Only glibc ELF64, direct versioned CPython linkage or a defined executable
Py_GetVersion symbol are accepted. A later worker must prove loaded ownership.
This identity binds the loader, dependency images and cache/preload slots; it
claims neither mutation retention nor a complete Python startup identity.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import struct

from ._elf_identity import MAX_IMAGE, parse_elf, refuse
from .go_v1 import _ExecutableIdentity, _resolve_file_identity

LOADERS = {62: "/lib64/ld-linux-x86-64.so.2", 183: "/lib/ld-linux-aarch64.so.1"}
VERSIONS = ("3.11", "3.12", "3.13", "3.14")


@dataclass(frozen=True)
class Image:
    invocation: Path
    links: tuple[tuple[Path, str, int, int], ...]
    identity: _ExecutableIdentity
    contents: bytes


def capture(path: Path, *, executable: bool = False) -> Image:
    if not path.is_absolute():
        raise refuse("nonabsolute image")
    original = path
    links = []
    # Include directory links as well as the final SONAME/interpreter link.
    for _ in range(16):
        parts = path.parts
        for i in range(1, len(parts)):
            component = Path(*parts[:i + 1])
            try:
                info = component.lstat()
                if component.is_symlink():
                    target = os.readlink(component)
                    links.append((component, target, info.st_dev, info.st_ino))
                    destination = Path(target)
                    if not destination.is_absolute():
                        destination = component.parent / destination
                    path = Path(os.path.abspath(destination.joinpath(*parts[i + 1:])))
                    break
            except OSError as exc:
                raise refuse("image/link unavailable") from exc
        else:
            contents = bytearray()
            identity = _resolve_file_identity(path, executable=executable,
                label="Linux image", max_bytes=MAX_IMAGE, _contents=contents)
            return Image(original, tuple(links), identity, bytes(contents))
    raise refuse("excessive link chain")


def cache_entries(data: bytes, machine: int = 62) -> dict[str, tuple[str, ...]]:
    """Read only the new glibc 1.1 cache; refuse compatibility/hwcap entries."""
    if not data.startswith(b"glibc-ld.so.cache1.1") or len(data) < 48 or len(data) > 8 * 1024 * 1024:
        raise refuse("unsupported loader cache")
    count, strings_size = struct.unpack_from("<II", data, 20)
    end = 48 + count * 24
    if count > 100000 or end > len(data) or strings_size > len(data) - end or data[28] not in (0, 2):
        raise refuse("loader cache bounds/endian")
    def string(offset: int) -> str:
        if not end <= offset < end + strings_size:
            raise refuse("loader cache string bounds")
        stop = data.find(b"\0", offset, end + strings_size)
        if stop < 0:
            raise refuse("loader cache unterminated string")
        try:
            return data[offset:stop].decode("ascii")
        except UnicodeDecodeError as exc:
            raise refuse("loader cache encoding") from exc
    result: dict[str, tuple[str, ...]] = {}
    for i in range(count):
        flags, key, value, osversion, hwcap = struct.unpack_from("<iIIIQ", data, 48 + i * 24)
        name, path = string(key), string(value)
        # glibc cache contains multiple ABIs. Exclude known foreign ELF ABIs;
        # never use a 32-bit entry to resolve a 64-bit dependency.
        expected_flags = {62: 0x303, 183: 0xA03}[machine]
        if flags in (0x3, 0x303, 0xA03, 0x803) and flags != expected_flags:
            continue
        if flags != expected_flags or hwcap or osversion or not path.startswith("/"):
            # Refuse a selected unsupported cache entry, without guessing its
            # applicability from the current manager CPU.
            path = "!unsupported-cache-entry"
        result[name] = result.get(name, ()) + (path,)
    return result


@dataclass(frozen=True)
class LinuxIdentity:
    platform: str
    version: str
    process: Image
    runtime: Image
    loader: Image
    cache: Image | None
    preload: Image | None
    cache_path: Path
    preload_path: Path
    search_slots: tuple[Path, ...]
    dependencies: tuple[Image, ...]

    def verify(self) -> None:
        current = resolve_linux_identity(self.process.invocation, version=self.version,
            cache_path=self.cache_path, preload_path=self.preload_path)
        if current != self:
            raise refuse("loader configuration or image mutated")


def resolve_linux_identity(path: Path, *, version: str,
    cache_path: Path = Path("/etc/ld.so.cache"),
    preload_path: Path = Path("/etc/ld.so.preload"),
) -> LinuxIdentity:
    """Select exact process/runtime bytes without executing a candidate.

    Version is the expected worker contract, not a version discovered by running
    the candidate. Worker proof must verify it, including embedded layouts.
    """
    if version not in VERSIONS:
        raise refuse("unsupported Python version")
    process = capture(path, executable=True)
    elf = parse_elf(process.contents)
    if elf.interpreter != LOADERS[elf.machine]:
        raise refuse("unsupported dynamic loader")
    loader = capture(Path(elf.interpreter), executable=True)
    loader_elf = parse_elf(loader.contents)
    if loader_elf.machine != elf.machine or loader_elf.kind != 3:
        raise refuse("loader ABI mismatch")
    preload = capture(preload_path) if os.path.lexists(preload_path) else None
    if preload is not None and preload.contents.strip():
        raise refuse("nonempty system preload")
    cache = capture(cache_path) if os.path.lexists(cache_path) else None
    entries = cache_entries(cache.contents, elf.machine) if cache else {}
    python = [n for n in elf.needed if "python" in n.lower()]
    expected = f"libpython{version}.so.1.0"
    if len(python) > 1 or (python and python != [expected]):
        raise refuse("duplicate Python dependencies or wrong ABI")
    if elf.rpath is not None and elf.runpath is not None:
        raise refuse("conflicting RPATH/RUNPATH")
    search = elf.runpath if elf.runpath is not None else elf.rpath
    directories = []
    if search is not None:
        for item in search.split(":"):
            # Exactly the approved relocatable prefix/lib layout. No arbitrary
            # tokens, empty CWD entries, absolute RPATHs or prefix escapes.
            if item not in ("$ORIGIN/../lib", "${ORIGIN}/../lib"):
                raise refuse("escaping or unsupported loader search")
            directory = process.identity.path.parent.parent / "lib"
            if directory.is_symlink() or directory.resolve() != directory:
                raise refuse("linked origin search directory")
            if directory in directories:
                raise refuse("ambiguous loader search")
            directories.append(directory)
    slots = tuple(d / expected for d in directories)
    for directory in directories:
        # glibc can prioritize these subdirectories; do not silently overlook
        # an existing hardware-specific search tree.
        if os.path.lexists(directory / "glibc-hwcaps"):
            raise refuse("unsupported glibc hardware search")
    if not python:
        if not elf.defines_python:
            raise refuse("absence of libpython does not prove embedding")
        runtime = process
    else:
        if elf.defines_python:
            raise refuse("competing embedded and shared Python")
        candidates = [slot for slot in slots if os.path.lexists(slot)]
        if not candidates:
            candidates = [Path(p) for p in entries.get(expected, ())]
        if len(candidates) != 1:
            raise refuse("unresolved or ambiguous Python search")
        runtime = capture(candidates[0])
        library = parse_elf(runtime.contents)
        if (library.machine != elf.machine or library.kind != 3 or library.interpreter
                or library.soname != expected or not library.defines_python
                or any("python" in n.lower() for n in library.needed)):
            raise refuse("runtime ABI or Python provider mismatch")
    # Walk the bounded dependency closure to reject hidden Python providers.
    # RPATH inheritance and transitive origin searches remain unsupported;
    # refusing them is preferable to selecting an image with different scope.
    dependencies: list[Image] = []
    pending = [(process, elf)]
    seen = {process.identity.path, runtime.identity.path, loader.identity.path}
    if runtime != process:
        pending.append((runtime, parse_elf(runtime.contents)))
    total_bytes = len(process.contents) + len(runtime.contents) + len(loader.contents)
    while pending:
        owner, metadata = pending.pop()
        if owner != process and (metadata.rpath is not None or metadata.runpath is not None):
            raise refuse("unsupported transitive loader search")
        for dependency in metadata.needed:
            if owner == process and dependency == expected:
                continue
            if "python" in dependency.lower():
                raise refuse("transitive Python dependency")
            if dependency == loader_elf.soname:
                continue
            paths = [d / dependency for d in directories
                     if os.path.lexists(d / dependency)]
            if paths and owner != process:
                raise refuse("unsupported inherited search")
            if not paths:
                paths = [Path(p) for p in entries.get(dependency, ())]
            if len(paths) != 1:
                raise refuse("ambiguous or unresolved dependency")
            image = capture(paths[0])
            if image.identity.path in seen:
                continue
            seen.add(image.identity.path)
            total_bytes += len(image.contents)
            if len(seen) > 128 or total_bytes > 256 * 1024 * 1024:
                raise refuse("dependency closure bound")
            parsed = parse_elf(image.contents)
            if parsed.machine != elf.machine or parsed.kind != 3 or parsed.defines_python:
                raise refuse("unexpected transitive Python provider or ABI")
            dependencies.append(image)
            pending.append((image, parsed))
    return LinuxIdentity("linux", version, process, runtime, loader, cache,
                         preload, cache_path, preload_path, slots,
                         tuple(dependencies))
