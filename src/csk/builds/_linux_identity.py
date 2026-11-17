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
import platform
import struct

from ._elf_identity import MAX_IMAGE, parse_elf, refuse
from .go_v1 import (
    PLATFORM_LINUX,
    _ExecutableIdentity,
    _InterpreterIdentity,
    _InterpreterLinkIdentity,
    _InterpreterRuntimeIdentity,
    _LINUX_ELF_HOST_MACHINES,
    _canonical_runtime_directory,
    _linux_loaded_identities,
    _pyvenv_home,
    _resolve_configuration_identity,
    _resolve_file_identity,
)

LOADERS = {62: "/lib64/ld-linux-x86-64.so.2", 183: "/lib/ld-linux-aarch64.so.1"}
VERSIONS = ("3.11", "3.12", "3.13", "3.14")
# U3 profile: distribution CPython 3.14 on glibc x86-64. Other versions and
# machines stay accepted by the generic resolver above, not by this profile.
# The host-machine table is the single one in go_v1 (_LINUX_ELF_HOST_MACHINES);
# AArch64 later is one row there for both artifact check and this profile.
PROFILE_VERSION = "3.14"
_PROFILE_MACHINE_REQUIREMENT = (
    "profile requires host machine one of "
    + ", ".join(sorted(_LINUX_ELF_HOST_MACHINES))
)


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


# Bound both wire size and expansion: offsets may alias long strings repeatedly.
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_CACHE_ENTRIES = 100000
MAX_CACHE_NAME = 4096
MAX_CACHE_OUTPUT = 8 * 1024 * 1024
MAX_CACHE_CANDIDATES = 64


def cache_entries(data: bytes, machine: int = 62) -> dict[str, tuple[str, ...]]:
    """Read bounded new-format glibc cache entries for exactly one ABI."""
    expected_flags = {62: 0x303, 183: 0xA03}.get(machine)
    if (expected_flags is None or len(data) < 48 or len(data) > MAX_CACHE_BYTES
            or not data.startswith(b"glibc-ld.so.cache1.1")):
        raise refuse("unsupported loader cache")
    count, strings_size = struct.unpack_from("<II", data, 20)
    end = 48 + count * 24
    if count > MAX_CACHE_ENTRIES or end > len(data) or strings_size > len(data) - end or data[28] not in (0, 2):
        raise refuse("loader cache bounds/endian")
    # First pass validates every applicable string and the total expansion before
    # decoding/retaining any. Bounded find prevents repeated unbounded scanning.
    def string_end(offset: int) -> int:
        if not end <= offset < end + strings_size:
            raise refuse("loader cache string bounds")
        stop = data.find(b"\0", offset, min(end + strings_size, offset + MAX_CACHE_NAME + 1))
        if stop < 0:
            raise refuse("loader cache unterminated or oversized string")
        return stop

    output_bytes = 0
    for i in range(count):
        flags, key, value, _, _ = struct.unpack_from("<iIIIQ", data, 48 + i * 24)
        # glibc ignores all foreign ABI flags, including architectures unknown
        # here. They cannot poison a supported same-SONAME entry.
        if flags != expected_flags:
            continue
        output_bytes += string_end(key) - key + string_end(value) - value
        if output_bytes > MAX_CACHE_OUTPUT:
            raise refuse("loader cache output exceeds bound")

    result: dict[str, list[str]] = {}
    for i in range(count):
        flags, key, value, osversion, hwcap = struct.unpack_from("<iIIIQ", data, 48 + i * 24)
        if flags != expected_flags:
            continue
        try:
            name = data[key:string_end(key)].decode("ascii")
            path = data[value:string_end(value)].decode("ascii")
        except UnicodeDecodeError as exc:
            raise refuse("loader cache encoding") from exc
        if hwcap or osversion or not path.startswith("/"):
            # Same-ABI conditional entries may win. Without modeling their
            # applicability, refuse selection rather than ignore them.
            path = "!unsupported-cache-entry"
        candidates = result.setdefault(name, [])
        if len(candidates) >= MAX_CACHE_CANDIDATES:
            raise refuse("loader cache candidate count exceeds bound")
        candidates.append(path)
    return {name: tuple(paths) for name, paths in result.items()}


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
        raise refuse(
            f"unsupported dynamic loader {elf.interpreter!r}: "
            f"profile requires glibc loader {LOADERS[elf.machine]!r}"
        )
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
    if total_bytes > 256 * 1024 * 1024:
        raise refuse("dependency closure bound")
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


@dataclass(frozen=True)
class LinuxInterpreterIdentity:
    """Retained pre-launch identity in the shape the worker launch consumes.

    ``interpreter`` is the same value macOS/Windows resolution produces;
    ``linux`` additionally retains the loader, cache/preload slots and the
    dependency closure. Production admission does not call this yet.
    """

    linux: LinuxIdentity
    interpreter: _InterpreterIdentity
    host_machine: str
    distribution_root: Path = Path("/usr")
    distribution_owner: int = 0

    def verify(self) -> None:
        current = resolve_linux_interpreter_identity(
            self.interpreter.invocation_path,
            version=self.linux.version,
            host_machine=self.host_machine,
            cache_path=self.linux.cache_path,
            preload_path=self.linux.preload_path,
            _distribution_root=self.distribution_root,
            _distribution_owner=self.distribution_owner,
        )
        if current != self:
            raise refuse("loader configuration or image mutated")

    def watch_paths(self) -> tuple[Path, ...]:
        linux = self.linux
        images = (
            linux.process, linux.runtime, linux.loader,
            linux.cache, linux.preload, *linux.dependencies,
        )
        retained = (
            linux.loader.identity.path,
            linux.cache.identity.path if linux.cache else linux.cache_path,
            linux.preload.identity.path if linux.preload else linux.preload_path,
            *linux.search_slots,
            *(image.identity.path for image in linux.dependencies),
            # Every retained image's link components and their parents,
            # so a retargeted SONAME link cannot change unnoticed
            # between checkpoints.
            *(link[0] for image in images if image is not None
              for link in image.links),
            *(link[0].parent for image in images if image is not None
              for link in image.links),
        )
        return tuple(dict.fromkeys((*self.interpreter.watch_paths(), *retained)))


def resolve_linux_interpreter_identity(
    path: Path, *, version: str = PROFILE_VERSION,
    host_machine: str | None = None,
    cache_path: Path = Path("/etc/ld.so.cache"),
    preload_path: Path = Path("/etc/ld.so.preload"),
    _distribution_root: Path = Path("/usr"),
    _distribution_owner: int = 0,
) -> LinuxInterpreterIdentity:
    """Resolve the U3 profile interpreter to retained launch identities."""
    if version != PROFILE_VERSION:
        raise refuse(
            f"unsupported Python version {version}: "
            f"profile requires distribution CPython {PROFILE_VERSION}"
        )
    resolved_host_machine = host_machine or platform.machine()
    expected_machine = _LINUX_ELF_HOST_MACHINES.get(
        resolved_host_machine.casefold()
    )
    if expected_machine is None:
        raise refuse(
            f"unsupported host machine {resolved_host_machine!r}: "
            f"{_PROFILE_MACHINE_REQUIREMENT}"
        )
    linux = resolve_linux_identity(
        path, version=version, cache_path=cache_path, preload_path=preload_path
    )
    elf = parse_elf(linux.process.contents)
    if elf.machine != expected_machine:
        raise refuse(
            f"unsupported ELF machine {elf.machine}: "
            f"{_PROFILE_MACHINE_REQUIREMENT} ({expected_machine})"
        )
    if elf.rpath is not None or elf.runpath is not None:
        # A relocatable $ORIGIN layout is a standalone/pyenv property. The
        # named profile resolves libpython through the glibc cache or embeds
        # it, so refuse instead of widening the profile to that layout.
        raise refuse(
            "unsupported standalone Python layout: "
            "$ORIGIN loader search is outside the distribution profile"
        )
    executable = linux.process.identity
    # Distribution tie: only the distribution's own interpreter qualifies,
    # so a pyenv or self-built static build refuses by location even when
    # its bytes otherwise parse. The root and owner are test seams.
    distribution_root = _distribution_root.resolve()
    expected_executable = (
        distribution_root / "bin" / f"python{PROFILE_VERSION}"
    )
    if executable.path != expected_executable:
        raise refuse(
            f"unsupported non-distribution Python location "
            f"{executable.path}: profile requires {expected_executable}"
        )
    try:
        owner = os.stat(executable.path).st_uid
    except OSError as exc:
        raise refuse("distribution interpreter is unavailable") from exc
    if owner != _distribution_owner:
        raise refuse(
            f"distribution interpreter {executable.path} is not "
            f"root-owned (uid {owner}, expected {_distribution_owner})"
        )
    if executable.mode & 0o022:
        raise refuse(
            f"distribution interpreter {executable.path} is writable "
            "by group or other"
        )
    # python_home is the installation prefix in both cases, as the launch
    # path consumes it (stdlib search and PYTHONHOME). The pyvenv `home`
    # key names the base executable's directory and must agree with the
    # resolved base executable; it is never the prefix itself.
    python_home = _canonical_runtime_directory(
        executable.path.parent.parent,
        "installed manager base Python home",
    )
    configuration: _ExecutableIdentity | None = None
    configuration_path = linux.process.invocation.parent.parent / "pyvenv.cfg"
    if os.path.lexists(configuration_path):
        configuration, configuration_text = _resolve_configuration_identity(
            configuration_path
        )
        venv_home = _pyvenv_home(configuration_text)
        if venv_home != executable.path.parent:
            raise refuse(
                f"pyvenv home {venv_home} does not name the base "
                f"executable directory {executable.path.parent}"
            )
    runtime = _InterpreterRuntimeIdentity(
        python_home=python_home,
        configuration=configuration,
        base_executable=executable,
        process_image=linux.process.identity,
        runtime_image=linux.runtime.identity,
        platform=PLATFORM_LINUX,
    )
    interpreter = _InterpreterIdentity(
        invocation_path=linux.process.invocation,
        links=tuple(
            _InterpreterLinkIdentity(
                path=link_path, target=target, device=device, inode=inode
            )
            for link_path, target, device, inode in linux.process.links
        ),
        executable=executable,
        runtime=runtime,
    )
    return LinuxInterpreterIdentity(
        linux, interpreter, resolved_host_machine,
        distribution_root, _distribution_owner,
    )


def prove_loaded_identities(
    retained: LinuxIdentity, *,
    proc_exe: Path = Path("/proc/self/exe"),
    proc_maps: Path = Path("/proc/self/maps"),
    maps_text: str | None = None,
    address: int | None = None,
    version_info: tuple[int, int] | None = None,
) -> tuple[_ExecutableIdentity, _ExecutableIdentity]:
    """Prove loaded process/runtime images are the retained identities.

    Both identities come from kernel data for the running process: the
    process image is ``/proc/self/exe`` opened through procfs, and the
    runtime owner is the single ``/proc/self/maps`` mapping containing
    the ``Py_GetVersion`` address, with its kernel device and inode
    (see ``go_v1._linux_loaded_identities``, the one helper the worker
    session proof also calls). Comparison is full
    ``_ExecutableIdentity`` equality (path, device, inode, size, mode
    and content hash). Missing procfs, a deleted, anonymous, replaced
    or ambiguous mapping, a parse failure or an undeterminable owner
    refuses; no owner is guessed from loader or argv strings.
    """
    loaded_process, loaded_runtime = _linux_loaded_identities(
        _proc_exe=proc_exe, _proc_maps=proc_maps,
        _maps_text=maps_text, _address=address,
        _version_info=version_info,
    )
    if loaded_process != retained.process.identity:
        raise refuse("loaded process image differs from the retained identity")
    if (
        retained.runtime.identity == retained.process.identity
        and loaded_runtime != loaded_process
    ):
        raise refuse(
            "embedded Python runtime image does not equal /proc/self/exe"
        )
    if loaded_runtime != retained.runtime.identity:
        raise refuse("loaded Python runtime image differs from the retained identity")
    return loaded_process, loaded_runtime
