from __future__ import annotations

import platform
import struct
import sys
from pathlib import Path

import pytest

from csk.builds import go_v1

_MACOS_ABSOLUTE_NAME = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Absolute Mach-O install names require macOS filesystem path semantics",
)


def _cpu() -> int:
    return (
        0x0100000C if platform.machine().lower() in {"arm64", "aarch64"} else 0x01000007
    )


def _image(*names: str, cpu: int | None = None, command: int = 0xC) -> bytes:
    commands = []
    for name in names:
        encoded = name.encode() + b"\0"
        size = (24 + len(encoded) + 7) & ~7
        commands.append(
            struct.pack("<6I", command, size, 24, 0, 0, 0)
            + encoded.ljust(size - 24, b"\0")
        )
    data = b"".join(commands)
    return (
        struct.pack(
            "<8I", 0xFEEDFACF, cpu or _cpu(), 0, 2, len(commands), len(data), 0, 0
        )
        + data
    )


def _fat(slices: list[tuple[int, bytes]], wide: bool) -> bytes:
    entry_size = 32 if wide else 20
    offset = 8 + entry_size * len(slices)
    entries = []
    for cpu, data in slices:
        entries.append(
            struct.pack(">IIQQII", cpu, 0, offset, len(data), 0, 0)
            if wide
            else struct.pack(">5I", cpu, 0, offset, len(data), 0)
        )
        offset += len(data)
    return (
        struct.pack(">II", 0xCAFEBABF if wide else 0xCAFEBABE, len(slices))
        + b"".join(entries)
        + b"".join(data for _, data in slices)
    )


def _layout(
    tmp_path: Path, image: bytes, candidates: tuple[str, ...] = ()
) -> go_v1._ExecutableIdentity:
    executable = tmp_path / "bin" / "python3.13"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(image)
    executable.chmod(0o755)
    library = tmp_path / "lib"
    library.mkdir()
    for name in candidates:
        path = library / name
        path.write_bytes(b"runtime")
        path.chmod(0o755)
    return go_v1._resolve_executable_identity(executable)


def test_static_interpreter_ignores_stray_dylibs(tmp_path: Path):
    # An unlinked libpython beside a static interpreter never becomes its runtime identity.
    executable = _layout(
        tmp_path, _image(), ("libpython3.13.dylib", "libpython3.14.dylib")
    )
    runtime = go_v1._resolve_macos_interpreter_runtime(executable)
    assert runtime.runtime_image == runtime.process_image == executable


@pytest.mark.parametrize("command", (0xC, 0x80000018, 0x8000001F, 0x20, 0x80000023))
@pytest.mark.parametrize(
    "form",
    ("rpath", "executable", pytest.param("absolute", marks=_MACOS_ABSOLUTE_NAME)),
)
def test_dynamic_interpreter_binds_approved_link(
    tmp_path: Path, command: int, form: str
):
    # Every supported dylib command binds the sole matching candidate through an approved name.
    # Absolute names are exercised on macOS, where the resolver's filesystem paths apply.
    candidate = tmp_path / "lib" / "libpython3.13.dylib"
    names = {
        "rpath": "@rpath/" + candidate.name,
        "executable": "@executable_path/../lib/" + candidate.name,
        "absolute": str(candidate),
    }
    executable = _layout(
        tmp_path, _image(names[form], command=command), (candidate.name,)
    )
    assert (
        go_v1._resolve_macos_interpreter_runtime(executable).runtime_image.path
        == candidate
    )


@pytest.mark.parametrize("wide", (False, True))
@pytest.mark.parametrize("dynamic", (False, True))
@pytest.mark.parametrize("architecture", ("arm64", "x86_64"))
def test_fat_interpreter_uses_running_cpu_slice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wide: bool,
    dynamic: bool,
    architecture: str,
):
    # The selected architecture decides the runtime even when the other slice disagrees.
    monkeypatch.setattr(platform, "machine", lambda: architecture)
    other = 0x01000007 if _cpu() == 0x0100000C else 0x0100000C
    link = "@rpath/libpython3.13.dylib"
    selected = _image(*([link] if dynamic else []))
    alternate = _image(*([] if dynamic else [link]), cpu=other)
    executable = _layout(
        tmp_path,
        _fat([(other, alternate), (_cpu(), selected)], wide),
        ("libpython3.13.dylib",),
    )
    runtime = go_v1._resolve_macos_interpreter_runtime(executable)
    assert runtime.runtime_image.path == (
        tmp_path / "lib" / "libpython3.13.dylib" if dynamic else executable.path
    )


@pytest.mark.parametrize(
    "case",
    (
        "missing",
        "mismatch",
        "loader",
        pytest.param("foreign-absolute", marks=_MACOS_ABSOLUTE_NAME),
        "duplicate",
        "ambiguous",
    ),
)
def test_invalid_runtime_links_return_no_identity(tmp_path: Path, case: str):
    # Invalid or ambiguous libpython links refuse with the identity error and return no runtime.
    # Foreign absolute names are exercised with macOS filesystem path semantics.
    link = "@rpath/libpython3.13.dylib"
    candidates = ("libpython3.13.dylib",)
    names = [link]
    if case == "missing":
        candidates = ()
    elif case == "mismatch":
        candidates = ("libpython3.14.dylib",)
    elif case == "loader":
        names = ["@loader_path/../lib/libpython3.13.dylib"]
    elif case == "foreign-absolute":
        names = [str(tmp_path / "elsewhere" / "libpython3.13.dylib")]
    elif case == "duplicate":
        names *= 2
    elif case == "ambiguous":
        candidates += ("libpython3.14.dylib",)
    executable = _layout(tmp_path, _image(*names), candidates)
    runtime = None
    with pytest.raises(go_v1.GoV1Error) as raised:
        runtime = go_v1._resolve_macos_interpreter_runtime(executable)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    assert runtime is None


@pytest.mark.parametrize(
    "case",
    (
        "header",
        "ncmds",
        "sizeofcmds",
        "offset",
        "unterminated",
        "missing-cpu",
        "fat-header",
        "fat-slice",
        "command-size",
        "slice-cpu",
    ),
)
def test_malformed_macho_returns_no_identity(tmp_path: Path, case: str):
    # Malformed headers, slices, commands and strings cannot yield a runtime identity.
    data = bytearray(_image("@rpath/libpython3.13.dylib"))
    if case == "header":
        data = data[:20]
    elif case == "ncmds":
        struct.pack_into("<I", data, 16, 100)
    elif case == "sizeofcmds":
        struct.pack_into("<I", data, 20, len(data))
    elif case == "offset":
        struct.pack_into("<I", data, 40, len(data))
    elif case == "unterminated":
        data[56:] = b"x" * (len(data) - 56)
    elif case == "missing-cpu":
        other = 0x01000007 if _cpu() == 0x0100000C else 0x0100000C
        data = bytearray(_fat([(other, _image(cpu=other))], False))
    elif case == "fat-header":
        data = bytearray(struct.pack(">II", 0xCAFEBABE, 2))
    elif case == "fat-slice":
        data = bytearray(_fat([(_cpu(), _image())], True))
        struct.pack_into(">Q", data, 24, len(data))
    elif case == "command-size":
        struct.pack_into("<I", data, 36, 8)
    elif case == "slice-cpu":
        other = 0x01000007 if _cpu() == 0x0100000C else 0x0100000C
        data = bytearray(_fat([(_cpu(), _image(cpu=other))], False))
    executable = _layout(tmp_path, bytes(data), ("libpython3.13.dylib",))
    runtime = None
    with pytest.raises(go_v1.GoV1Error) as raised:
        runtime = go_v1._resolve_macos_interpreter_runtime(executable)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    assert runtime is None


@pytest.mark.parametrize("same_size", (False, True))
def test_macho_read_rejects_changed_bound_executable(tmp_path: Path, same_size: bool):
    # A valid replacement image cannot supply load commands for the previously bound executable.
    executable = _layout(tmp_path, _image())
    replacement = bytearray(_image("@rpath/libpython3.13.dylib"))
    if same_size:
        replacement = bytearray(_image())
        struct.pack_into("<I", replacement, 24, 1)
    executable.path.write_bytes(replacement)
    runtime = None
    with pytest.raises(go_v1.GoV1Error) as raised:
        runtime = go_v1._resolve_macos_interpreter_runtime(executable)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    assert runtime is None


def test_macho_inspects_the_same_bytes_it_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Inspection opens the executable once and never reopens an unverified path to parse it.
    executable = _layout(tmp_path, _image())
    original_open = Path.open
    opens = []

    def open_once(path, *args, **kwargs):
        if path == executable.path:
            opens.append(path)
            assert len(opens) == 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_once)
    assert (
        go_v1._resolve_macos_interpreter_runtime(executable).runtime_image == executable
    )
    assert opens == [executable.path]


@_MACOS_ABSOLUTE_NAME
def test_absolute_runtime_link_can_canonicalize_to_candidate(tmp_path: Path):
    # An absolute alias resolving to the single canonical candidate is an approved install name.
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "lib", target_is_directory=True)
    executable = _layout(
        tmp_path, _image(str(alias / "libpython3.13.dylib")), ("libpython3.13.dylib",)
    )
    assert (
        go_v1._resolve_macos_interpreter_runtime(executable).runtime_image.path
        == tmp_path / "lib" / "libpython3.13.dylib"
    )


def test_runtime_candidate_symlink_is_not_a_regular_candidate(tmp_path: Path):
    # A matching symlink in lib cannot satisfy the regular-file candidate requirement.
    executable = _layout(tmp_path, _image("@rpath/libpython3.13.dylib"))
    target = tmp_path / "target"
    target.write_bytes(b"runtime")
    (tmp_path / "lib" / "libpython3.13.dylib").symlink_to(target)
    runtime = None
    with pytest.raises(go_v1.GoV1Error) as raised:
        runtime = go_v1._resolve_macos_interpreter_runtime(executable)
    assert raised.value.code == go_v1.CODE_WORKER_IDENTITY_INVALID
    assert runtime is None
