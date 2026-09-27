"""Windows handle-relative source-selection boundary regressions.

The portable unit cases exercise the Windows backend's own gates without
requiring NT. The integration cases drive the public selector path and run
on the Windows CI lane against real filesystem objects.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from csk import transactions
from csk.sources import (
    _selection_fs,
    boundaries,
    local_snapshot,
    publish,
    selection,
    snapshot,
)
from csk.sources import _selection_fs_windows as winfs
from csk.sources import errors as source_errors
from csk.sources.skillfile_v2 import CollectionSelector, IndividualSelector

requires_windows = pytest.mark.skipif(
    os.name != "nt", reason="requires the Windows handle-relative selection backend"
)


def _write_skill(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Windows selection fixture\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return directory


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Windows symbolic-link creation unavailable: {exc}")


def _junction_or_skip(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(
            "Windows junction creation unavailable: "
            f"{result.stdout.strip()} {result.stderr.strip()}"
        )


def test_windows_component_validation_rejects_win32_aliases() -> None:
    """A narrowing mutant that opens one invalid component fails this matrix."""

    for name in (
        "",
        ".",
        "..",
        "file:stream",
        "a\\b",
        "a/b",
        "bad\0name",
        "trailing.",
        "trailing ",
        "CON",
        "NUL.txt",
    ):
        with pytest.raises(OSError, match="invalid|publishable|device-name"):
            winfs.validate_component(name)


@pytest.mark.parametrize("name", ["SKILL.md:stream", "a\\b", "a/b", "bad\0name"])
def test_windows_raw_relative_open_validates_before_resolving_nt_api(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Invalid selector components never reach the NT API loader/open path."""

    monkeypatch.setattr(
        winfs,
        "_api",
        lambda: pytest.fail("invalid component reached Windows API resolution"),
    )
    with pytest.raises(OSError):
        winfs._create_relative_raw(
            1,
            name,
            directory=None,
            nofollow=True,
            case_sensitive=False,
        )


def test_windows_root_path_rejects_nul_before_resolving_win32_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CreateFileW's NUL-terminated path cannot silently open a prefix."""

    monkeypatch.setattr(
        winfs,
        "_api",
        lambda: pytest.fail("NUL root path reached Windows API resolution"),
    )
    with pytest.raises(OSError, match="NUL"):
        winfs.open_path(
            "C:\\source\0ignored", directory=True, nofollow=False
        )


def test_windows_untyped_nt_probe_requests_only_handle_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[int, int]] = []

    class Ntdll:
        @staticmethod
        def NtCreateFile(
            output: object,
            desired_access: int,
            _object_attributes: object,
            _io_status: object,
            _allocation_size: object,
            _file_attributes: int,
            _share_access: int,
            _disposition: int,
            create_options: int,
            _ea_buffer: object,
            _ea_length: int,
        ) -> int:
            seen.append((desired_access, create_options))
            ctypes.cast(
                output, ctypes.POINTER(ctypes.c_void_p)
            ).contents.value = 23
            return 0

    link = winfs.WindowsStat(
        st_mode=stat.S_IFLNK | 0o777,
        st_ino=0x1234,
        st_dev=4,
        st_nlink=1,
        st_uid=0,
        st_gid=0,
        st_size=0,
        st_atime=0,
        st_mtime=0,
        st_ctime=0,
        st_file_attributes=winfs._FILE_ATTRIBUTE_REPARSE_POINT,
        st_reparse_tag=0xA000000C,
    )
    monkeypatch.setattr(winfs, "_api", lambda: winfs._WinApi(SimpleNamespace(), Ntdll()))
    monkeypatch.setattr(winfs, "stat_handle", lambda _handle: link)

    result = winfs._create_relative_raw(
        11,
        "managed",
        directory=None,
        nofollow=True,
        case_sensitive=False,
    )

    assert result == 23
    assert seen == [
        (
            winfs._FILE_READ_ATTRIBUTES | winfs._SYNCHRONIZE,
            winfs._FILE_SYNCHRONOUS_IO_NONALERT | winfs._FILE_OPEN_REPARSE_POINT,
        )
    ]


def test_windows_enumeration_matcher_does_not_accept_short_name_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matcher mutant that falls back to NT's 8.3 lookup fails this case."""

    monkeypatch.setattr(
        winfs,
        "names_equivalent",
        lambda left, right, *, case_sensitive: left == right
        if case_sensitive
        else left.casefold() == right.casefold(),
    )
    with pytest.raises(FileNotFoundError, match="not an enumerated name"):
        winfs.match_enumerated_name(
            "LONGFO~1", ["LongFolderName"], case_sensitive=False
        )


def test_windows_enumeration_matcher_honors_case_sensitive_directories() -> None:
    assert winfs.match_enumerated_name(
        "Review", ["Review"], case_sensitive=True
    ) == "Review"
    with pytest.raises(FileNotFoundError, match="not an enumerated name"):
        winfs.match_enumerated_name("review", ["Review"], case_sensitive=True)


def test_windows_enumeration_matcher_resolves_insensitive_case_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutant that keeps only exact matching cannot resolve this selector."""

    class Kernel32:
        @staticmethod
        def CompareStringOrdinal(
            left: str,
            _left_length: int,
            right: str,
            _right_length: int,
            _ignore_case: int,
        ) -> int:
            return winfs._CSTR_EQUAL if left.casefold() == right.casefold() else 1

    monkeypatch.setattr(winfs, "_api", lambda: SimpleNamespace(kernel32=Kernel32()))
    assert winfs.match_enumerated_name(
        "rEvIeW", ["Review"], case_sensitive=False
    ) == "Review"


def test_windows_open_descriptor_routes_normalized_long_names_and_refuses_short_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production opens use on-disk names and reject short aliases."""

    opened: list[tuple[str, bool | None, bool]] = []
    closed: list[int] = []
    normalized = {101: "Review", 102: "LongFolderName"}
    next_handle = iter((101, 201, 102, 202))

    def fake_open_relative(
        _parent: int,
        name: str,
        *,
        directory: bool | None,
        nofollow: bool,
        case_sensitive: bool,
        allow_reparse: bool = False,
        allow_parent: bool = False,
    ) -> int:
        opened.append((name, directory, nofollow))
        return next(next_handle)

    monkeypatch.setattr(_selection_fs, "_WINDOWS_SELECTION", True)
    monkeypatch.setattr(winfs, "directory_case_sensitive", lambda _parent: False)
    monkeypatch.setattr(winfs, "open_relative", fake_open_relative)
    monkeypatch.setattr(
        winfs, "normalized_component_name", lambda handle: normalized[handle]
    )
    monkeypatch.setattr(
        winfs,
        "names_equivalent",
        lambda left, right, *, case_sensitive: (
            left == right if case_sensitive else left.casefold() == right.casefold()
        ),
    )
    monkeypatch.setattr(winfs, "close_handle", closed.append)
    monkeypatch.setattr(winfs, "identity_from_handle", lambda _handle: (1, 2))
    monkeypatch.setattr(
        winfs,
        "_directory_names",
        lambda _handle: pytest.fail("component resolution enumerated its parent"),
    )

    with _selection_fs.trace_filesystem(lambda _event: None):
        opened_file = _selection_fs._open_descriptor(
            "rEvIeW", _selection_fs._WINDOWS_FILE_FLAG, dir_fd=77
        )
        assert opened_file == 201
        _selection_fs._close_quietly(opened_file)

        short_alias_handle: int | None = None
        try:
            with pytest.raises(FileNotFoundError, match="normalized on-disk name"):
                short_alias_handle = _selection_fs._open_descriptor(
                    "LONGFO~1", _selection_fs._WINDOWS_FILE_FLAG, dir_fd=77
                )
        finally:
            if short_alias_handle is not None:
                _selection_fs._close_quietly(short_alias_handle)

    assert opened == [
        ("rEvIeW", None, True),
        ("Review", False, True),
        ("LONGFO~1", None, True),
    ]
    assert closed == [101, 201, 102]


def test_windows_component_refuses_when_normalized_name_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unavailable on-disk spelling fails closed and closes the probe handle."""

    opened: list[str] = []
    closed: list[int] = []
    next_handle = iter((301, 302))

    def fake_open_relative(
        _parent: int,
        name: str,
        *,
        directory: bool | None,
        nofollow: bool,
        case_sensitive: bool,
        allow_reparse: bool = False,
        allow_parent: bool = False,
    ) -> int:
        opened.append(name)
        return next(next_handle)

    def no_normalized_name(_handle: int) -> str:
        raise OSError(errno.ENOTSUP, "GetFinalPathNameByHandleW name unavailable")

    monkeypatch.setattr(_selection_fs, "_WINDOWS_SELECTION", True)
    monkeypatch.setattr(winfs, "directory_case_sensitive", lambda _parent: False)
    monkeypatch.setattr(winfs, "open_relative", fake_open_relative)
    monkeypatch.setattr(winfs, "normalized_component_name", no_normalized_name)
    monkeypatch.setattr(
        winfs,
        "names_equivalent",
        lambda *_args, **_kwargs: pytest.fail("unavailable name was guessed"),
    )
    monkeypatch.setattr(winfs, "close_handle", closed.append)
    monkeypatch.setattr(
        winfs,
        "_directory_names",
        lambda _handle: pytest.fail("component resolution enumerated its parent"),
    )

    admitted_handle: int | None = None
    try:
        with pytest.raises(OSError, match="GetFinalPathNameByHandleW name unavailable"):
            admitted_handle = _selection_fs._open_descriptor(
                "requested", _selection_fs._WINDOWS_FILE_FLAG, dir_fd=79
            )
    finally:
        if admitted_handle is not None:
            _selection_fs._close_quietly(admitted_handle)

    assert opened == ["requested"]
    assert closed == [301]


def _reparse_buffer(
    tag: int,
    substitute_name: str,
    print_name: str = "",
    *,
    flags: int = 0,
) -> bytes:
    substitute = substitute_name.encode("utf-16-le")
    printable = print_name.encode("utf-16-le")
    path_buffer = substitute + printable
    substitute_offset = 0
    print_offset = len(substitute)
    if tag == winfs._IO_REPARSE_TAG_SYMLINK:
        data_length = 12 + len(path_buffer)
        header = (
            tag.to_bytes(4, "little")
            + data_length.to_bytes(2, "little")
            + (0).to_bytes(2, "little")
            + substitute_offset.to_bytes(2, "little")
            + len(substitute).to_bytes(2, "little")
            + print_offset.to_bytes(2, "little")
            + len(printable).to_bytes(2, "little")
            + flags.to_bytes(4, "little")
        )
    else:
        data_length = 8 + len(path_buffer)
        header = (
            tag.to_bytes(4, "little")
            + data_length.to_bytes(2, "little")
            + (0).to_bytes(2, "little")
            + substitute_offset.to_bytes(2, "little")
            + len(substitute).to_bytes(2, "little")
            + print_offset.to_bytes(2, "little")
            + len(printable).to_bytes(2, "little")
        )
    return header + path_buffer


def _read_reparse_buffer(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    tag: int,
    *,
    containing_directory: int | None = None,
    containing_path: str | None = None,
) -> str:
    class Kernel32:
        @staticmethod
        def DeviceIoControl(
            _handle: object,
            control: int,
            _input: object,
            _input_size: int,
            output: object,
            output_size: int,
            returned: object,
            _overlapped: object,
        ) -> int:
            assert control == winfs._FSCTL_GET_REPARSE_POINT
            assert output_size >= len(payload)
            ctypes.memmove(output, payload, len(payload))
            ctypes.cast(returned, ctypes.POINTER(ctypes.c_uint32)).contents.value = len(
                payload
            )
            return 1

    monkeypatch.setattr(winfs, "_api", lambda: SimpleNamespace(kernel32=Kernel32()))
    if containing_path is not None:
        monkeypatch.setattr(
            winfs, "_final_path_from_handle", lambda _handle: containing_path
        )
    return winfs._read_reparse_target(
        71,
        tag,
        containing_directory=containing_directory,
    )


@pytest.mark.parametrize(
    ("target", "flags"),
    [(r"C:\target\skill", 0), (r"..\target\skill", winfs._SYMLINK_FLAG_RELATIVE)],
)
def test_windows_read_reparse_target_decodes_symlink_absolute_and_relative(
    monkeypatch: pytest.MonkeyPatch, target: str, flags: int
) -> None:
    assert _read_reparse_buffer(
        monkeypatch,
        _reparse_buffer(winfs._IO_REPARSE_TAG_SYMLINK, target, flags=flags),
        winfs._IO_REPARSE_TAG_SYMLINK,
    ) == target


@pytest.mark.parametrize(
    ("target", "flags"),
    [
        (r"C:\absolute-target", winfs._SYMLINK_FLAG_RELATIVE),
        (r"relative-target", 0),
    ],
)
def test_windows_read_reparse_target_enforces_relative_flag(
    monkeypatch: pytest.MonkeyPatch, target: str, flags: int
) -> None:
    with pytest.raises(OSError) as excinfo:
        _read_reparse_buffer(
            monkeypatch,
            _reparse_buffer(
                winfs._IO_REPARSE_TAG_SYMLINK,
                target,
                flags=flags,
            ),
            winfs._IO_REPARSE_TAG_SYMLINK,
        )
    assert excinfo.value.errno == errno.EIO


def test_windows_read_reparse_target_decodes_mount_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _read_reparse_buffer(
        monkeypatch,
        _reparse_buffer(
            winfs._IO_REPARSE_TAG_MOUNT_POINT, r"\??\C:\source\inside"
        ),
        winfs._IO_REPARSE_TAG_MOUNT_POINT,
    ) == r"C:\source\inside"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (r"\??\C:\source\inside", r"C:\source\inside"),
        (r"\??\UNC\server\share\inside", r"\\server\share\inside"),
        (
            r"\??\Volume{01234567-89ab-cdef-0123-456789abcdef}\inside",
            r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdef}\inside",
        ),
    ],
)
def test_windows_read_reparse_target_strips_nt_prefixes(
    monkeypatch: pytest.MonkeyPatch, target: str, expected: str
) -> None:
    assert _read_reparse_buffer(
        monkeypatch,
        _reparse_buffer(winfs._IO_REPARSE_TAG_MOUNT_POINT, target),
        winfs._IO_REPARSE_TAG_MOUNT_POINT,
    ) == expected


def test_windows_reparse_substitute_name_wins_over_print_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutant M-C (using presentation text) must fail this path assertion."""

    result = _read_reparse_buffer(
        monkeypatch,
        _reparse_buffer(
            winfs._IO_REPARSE_TAG_SYMLINK,
            r"C:\real-target",
            r"C:\display-only",
        ),
        winfs._IO_REPARSE_TAG_SYMLINK,
    )
    assert result == r"C:\real-target"


def test_windows_root_relative_reparse_target_uses_link_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _read_reparse_buffer(
        monkeypatch,
        _reparse_buffer(
            winfs._IO_REPARSE_TAG_SYMLINK,
            r"\dir\x",
        ),
        winfs._IO_REPARSE_TAG_SYMLINK,
        containing_directory=88,
        containing_path=r"\\?\D:\source\links",
    )
    assert result == r"\\?\D:\dir\x"


@pytest.mark.parametrize(
    "payload",
    [
        b"\x00" * 10,
        _reparse_buffer(winfs._IO_REPARSE_TAG_SYMLINK, "target")[:-2],
        _reparse_buffer(winfs._IO_REPARSE_TAG_SYMLINK, "target")[:8]
        + (0xFFFF).to_bytes(2, "little")
        + _reparse_buffer(winfs._IO_REPARSE_TAG_SYMLINK, "target")[10:],
        _reparse_buffer(winfs._IO_REPARSE_TAG_SYMLINK, "target")[:8]
        + (1).to_bytes(2, "little")
        + _reparse_buffer(winfs._IO_REPARSE_TAG_SYMLINK, "target")[10:],
        _reparse_buffer(winfs._IO_REPARSE_TAG_SYMLINK, "target")[:10]
        + (3).to_bytes(2, "little")
        + _reparse_buffer(winfs._IO_REPARSE_TAG_SYMLINK, "target")[12:],
    ],
    ids=["short-header", "declared-length", "overflow", "odd-offset", "odd-length"],
)
def test_windows_read_reparse_target_rejects_malformed_buffers(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    with pytest.raises(OSError) as excinfo:
        _read_reparse_buffer(
            monkeypatch,
            payload,
            winfs._IO_REPARSE_TAG_SYMLINK,
        )
    assert excinfo.value.errno == errno.EIO


def test_windows_file_id_info_preserves_the_128_bit_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_file_id = int("00112233445566778899AABBCCDDEEFF", 16)

    class Kernel:
        @staticmethod
        def GetFileInformationByHandleEx(
            _handle: object, info_class: int, output: object, _size: int
        ) -> int:
            assert info_class == winfs._FILE_ID_INFO_CLASS
            info = ctypes.cast(output, ctypes.POINTER(winfs._FileIdInfo)).contents
            info.VolumeSerialNumber = 0x123456789ABCDEF0
            raw = expected_file_id.to_bytes(16, "little")
            for index, byte in enumerate(raw):
                info.FileId.Identifier[index] = byte
            return 1

    monkeypatch.setattr(
        winfs, "_api", lambda: winfs._WinApi(Kernel(), SimpleNamespace())
    )
    assert winfs.identity_from_handle(41) == (0x123456789ABCDEF0, expected_file_id)


def test_wide_windows_identity_roundtrips_through_publication_recheck() -> None:
    identity = (0xF423456789ABCDEF, (1 << 127) + 0x123456789ABCDEF)
    record = boundaries.BoundaryRecord(
        source_root=r"C:\sources\review",
        source_identity=identity,
        home_root=None,
        home_identity=None,
        home_contains_source=False,
        source_inside_managed=None,
        managed_roots=(),
    )

    encoded = boundaries.boundary_record_to_json(record)
    assert encoded["source_identity"] == [str(identity[0]), str(identity[1])]
    transactions._validate_recheck_payload(
        {"kind": "source-boundary", "record": encoded},
        subject="wide Windows identity",
        corruption=False,
    )
    assert boundaries.boundary_record_from_json(encoded) == record


def test_publication_recheck_encodes_wide_ancestor_and_admitted_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    (project / "out").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    project_info = project.stat()
    record = boundaries.BoundaryRecord(
        source_root=os.fspath(project),
        source_identity=(project_info.st_dev, project_info.st_ino),
        home_root=os.fspath(home),
        home_identity=None,
        home_contains_source=False,
        source_inside_managed=None,
        managed_roots=(),
    )
    wide_identity = (0xF423456789ABCDEF, (1 << 127) + 0x123456789ABCDEF)
    freeze = publish._freeze_managed_ancestors

    def freeze_with_wide_id(
        frozen_record: boundaries.BoundaryRecord,
        root: str,
        components: list[str],
        *,
        subject: str,
    ) -> list[tuple[int, int]] | None:
        assert freeze(frozen_record, root, components, subject=subject) is not None
        return [wide_identity]

    monkeypatch.setattr(publish, "_freeze_managed_ancestors", freeze_with_wide_id)
    key = (publish.CLASS_CONTEXT, "project/review")
    target = publish.TargetSpec(
        target_class=key[0],
        identifier=key[1],
        live_path=project / "out" / "review",
        kind="entry",
        staged=None,
    )
    payload = publish.build_recheck_payloads(
        record=record,
        project_path=project,
        home=home,
        staged_specs=(target,),
        live_digests={key: transactions.ABSENT_DIGEST},
        admitted=frozenset({wide_identity}),
    )[key]

    assert payload["ancestors"] == [[str(wide_identity[0]), str(wide_identity[1])]]
    assert payload["admitted"] == [[str(wide_identity[0]), str(wide_identity[1])]]
    transactions._validate_recheck_payload(
        payload, subject="wide Windows ancestor identity", corruption=False
    )
    assert publish._payload_identities(
        payload, "ancestors", subject="wide Windows ancestor identity"
    ) == [wide_identity]
    assert publish._payload_identities(
        payload, "admitted", subject="wide Windows admitted identity"
    ) == [wide_identity]


@pytest.mark.parametrize(
    "wide_value",
    (
        "01",
        "9223372036854775807",
        "-9223372036854775809",
        "340282366920938463463374607431768211456",
    ),
)
def test_boundary_record_rejects_noncanonical_wide_identity(
    wide_value: str,
) -> None:
    identity = (0xF423456789ABCDEF, (1 << 127) + 0x123456789ABCDEF)
    record = boundaries.BoundaryRecord(
        source_root=r"C:\sources\review",
        source_identity=identity,
        home_root=None,
        home_identity=None,
        home_contains_source=False,
        source_inside_managed=None,
        managed_roots=(),
    )
    encoded = boundaries.boundary_record_to_json(record)
    encoded["source_identity"] = [wide_value, str(identity[1])]

    with pytest.raises(ValueError, match="canonical wide-integer"):
        boundaries.boundary_record_from_json(encoded)


@pytest.mark.parametrize("winerror", [50, 87, 120])
def test_windows_file_id_legacy_fallback_is_limited_to_unsupported_errors(
    monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    class Kernel:
        fallback_calls = 0

        @staticmethod
        def GetFileInformationByHandleEx(
            _handle: object, _info_class: int, _output: object, _size: int
        ) -> int:
            return 0

        def GetFileInformationByHandle(self, _handle: object, output: object) -> int:
            self.fallback_calls += 1
            info = ctypes.cast(
                output, ctypes.POINTER(winfs._ByHandleFileInformation)
            ).contents
            info.VolumeSerialNumber = 0x10203040
            info.FileIndexHigh = 0x89ABCDEF
            info.FileIndexLow = 0x01234567
            return 1

    kernel = Kernel()

    def unsupported_error(*, operation: str, code: int | None = None) -> OSError:
        error = OSError(code or winerror, operation)
        error.winerror = code or winerror  # type: ignore[attr-defined]
        return error

    monkeypatch.setattr(winfs, "_api", lambda: winfs._WinApi(kernel, SimpleNamespace()))
    monkeypatch.setattr(winfs, "_win_error", unsupported_error)
    assert winfs.identity_from_handle(41) == (
        0x10203040,
        (0x89ABCDEF << 32) | 0x01234567,
    )
    assert kernel.fallback_calls == 1


def test_windows_file_id_fallback_does_not_hide_other_query_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Kernel:
        fallback_calls = 0

        @staticmethod
        def GetFileInformationByHandleEx(
            _handle: object, _info_class: int, _output: object, _size: int
        ) -> int:
            return 0

        def GetFileInformationByHandle(self, _handle: object, _output: object) -> int:
            self.fallback_calls += 1
            return 1

    kernel = Kernel()

    def access_error(*, operation: str, code: int | None = None) -> OSError:
        error = OSError(code or 5, operation)
        error.winerror = code or 5  # type: ignore[attr-defined]
        return error

    monkeypatch.setattr(winfs, "_api", lambda: winfs._WinApi(kernel, SimpleNamespace()))
    monkeypatch.setattr(winfs, "_win_error", access_error)
    with pytest.raises(OSError):
        winfs.identity_from_handle(41)
    assert kernel.fallback_calls == 0


@pytest.mark.parametrize(
    "tag",
    (
        0xDEADBEEF,
        0x80000013,  # dedup
        0x80000018,  # WCI
        0x8000001B,  # AppExecLink
        0x9000001A,  # cloud-file placeholder
    ),
)
def test_windows_unsupported_reparse_tags_are_rejected_by_relative_open(
    monkeypatch: pytest.MonkeyPatch, tag: int
) -> None:
    """Admitting any tag outside symlink/junction fails at the NT open seam."""

    fake = winfs.WindowsStat(
        st_mode=0,
        st_ino=9,
        st_dev=2,
        st_nlink=1,
        st_uid=0,
        st_gid=0,
        st_size=0,
        st_atime=0,
        st_mtime=0,
        st_ctime=0,
        st_file_attributes=0x400,
        st_reparse_tag=tag,
    )
    closed: list[int] = []
    monkeypatch.setattr(
        winfs, "_create_relative_raw", lambda *_args, **_kwargs: 17
    )
    monkeypatch.setattr(winfs, "stat_handle", lambda _handle: fake)
    monkeypatch.setattr(winfs, "close_handle", closed.append)
    with pytest.raises(winfs.UnsupportedReparseTagError, match=f"0x{tag:08X}"):
        winfs.open_relative(
            11,
            "placeholder",
            directory=None,
            nofollow=True,
            case_sensitive=False,
            allow_reparse=True,
        )
    assert closed == [17]


def test_windows_nofollow_probe_inspects_reparse_before_type_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A type-constrained probe must not hide a directory reparse point."""

    opened: list[tuple[bool | None, bool]] = []
    closed: list[int] = []
    handles = iter((17, 18))
    identity = (4, 0x1234)

    def create_relative(
        _parent: int,
        _name: str,
        *,
        directory: bool | None,
        nofollow: bool,
        case_sensitive: bool,
    ) -> int:
        assert case_sensitive is False
        opened.append((directory, nofollow))
        return next(handles)

    def regular_directory(_handle: int) -> winfs.WindowsStat:
        return winfs.WindowsStat(
            st_mode=stat.S_IFDIR | 0o777,
            st_ino=identity[1],
            st_dev=identity[0],
            st_nlink=1,
            st_uid=0,
            st_gid=0,
            st_size=0,
            st_atime=0,
            st_mtime=0,
            st_ctime=0,
            st_file_attributes=winfs._FILE_ATTRIBUTE_DIRECTORY,
            st_reparse_tag=0,
        )

    monkeypatch.setattr(winfs, "_create_relative_raw", create_relative)
    monkeypatch.setattr(winfs, "stat_handle", regular_directory)
    monkeypatch.setattr(winfs, "identity_from_handle", lambda _handle: identity)
    monkeypatch.setattr(winfs, "close_handle", closed.append)

    result = winfs.open_relative(
        11,
        "managed",
        directory=True,
        nofollow=True,
        case_sensitive=False,
    )

    assert result == 18
    assert opened == [(None, True), (True, True)]
    assert closed == [17]


def test_windows_file_open_requests_non_directory_type_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutant that omits FILE_NON_DIRECTORY_FILE fails this seam test."""

    opened: list[tuple[bool | None, bool]] = []
    closed: list[int] = []
    handles = iter((17, 18))
    regular_file = winfs.WindowsStat(
        st_mode=stat.S_IFREG | 0o666,
        st_ino=0x1234,
        st_dev=4,
        st_nlink=1,
        st_uid=0,
        st_gid=0,
        st_size=12,
        st_atime=0,
        st_mtime=0,
        st_ctime=0,
        st_file_attributes=0,
        st_reparse_tag=0,
    )

    def create_relative(
        _parent: int,
        _name: str,
        *,
        directory: bool | None,
        nofollow: bool,
        case_sensitive: bool,
    ) -> int:
        assert case_sensitive is False
        opened.append((directory, nofollow))
        return next(handles)

    monkeypatch.setattr(winfs, "_create_relative_raw", create_relative)
    monkeypatch.setattr(winfs, "stat_handle", lambda _handle: regular_file)
    monkeypatch.setattr(winfs, "identity_from_handle", lambda _handle: (4, 0x1234))
    monkeypatch.setattr(winfs, "close_handle", closed.append)

    result = winfs.open_relative(
        11,
        "SKILL.md",
        directory=False,
        nofollow=True,
        case_sensitive=False,
    )

    assert result == 18
    assert opened == [(None, True), (False, True)]
    assert closed == [17]


def test_windows_file_open_rechecks_type_after_typed_reopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutant that admits a directory replacement returns its handle."""

    opened: list[bool | None] = []
    closed: list[int] = []
    handles = iter((17, 18))
    file_identity = (4, 0x1234)
    file_stat = winfs.WindowsStat(
        st_mode=stat.S_IFREG | 0o666,
        st_ino=file_identity[1],
        st_dev=file_identity[0],
        st_nlink=1,
        st_uid=0,
        st_gid=0,
        st_size=12,
        st_atime=0,
        st_mtime=0,
        st_ctime=0,
        st_file_attributes=0,
        st_reparse_tag=0,
    )
    directory_stat = winfs.WindowsStat(
        st_mode=stat.S_IFDIR | 0o777,
        st_ino=file_identity[1],
        st_dev=file_identity[0],
        st_nlink=1,
        st_uid=0,
        st_gid=0,
        st_size=0,
        st_atime=0,
        st_mtime=0,
        st_ctime=0,
        st_file_attributes=winfs._FILE_ATTRIBUTE_DIRECTORY,
        st_reparse_tag=0,
    )

    def create_relative(
        _parent: int,
        _name: str,
        *,
        directory: bool | None,
        nofollow: bool,
        case_sensitive: bool,
    ) -> int:
        assert nofollow is True
        assert case_sensitive is False
        opened.append(directory)
        return next(handles)

    monkeypatch.setattr(winfs, "_create_relative_raw", create_relative)
    monkeypatch.setattr(
        winfs,
        "stat_handle",
        lambda handle: file_stat if handle == 17 else directory_stat,
    )
    monkeypatch.setattr(winfs, "identity_from_handle", lambda _handle: file_identity)
    monkeypatch.setattr(winfs, "close_handle", closed.append)

    with pytest.raises(IsADirectoryError):
        winfs.open_relative(
            11,
            "SKILL.md",
            directory=False,
            nofollow=True,
            case_sensitive=False,
        )

    assert opened == [None, False]
    assert closed == [17, 18]


@pytest.mark.parametrize("tag", (0xA000000C, 0xA0000003))
def test_windows_nofollow_directory_probe_exposes_supported_links(
    monkeypatch: pytest.MonkeyPatch, tag: int
) -> None:
    opened: list[bool | None] = []
    closed: list[int] = []
    link = winfs.WindowsStat(
        st_mode=stat.S_IFLNK | 0o777,
        st_ino=0x1234,
        st_dev=4,
        st_nlink=1,
        st_uid=0,
        st_gid=0,
        st_size=0,
        st_atime=0,
        st_mtime=0,
        st_ctime=0,
        st_file_attributes=winfs._FILE_ATTRIBUTE_REPARSE_POINT,
        st_reparse_tag=tag,
    )

    def create_relative(
        _parent: int,
        _name: str,
        *,
        directory: bool | None,
        nofollow: bool,
        case_sensitive: bool,
    ) -> int:
        assert nofollow is True
        assert case_sensitive is False
        opened.append(directory)
        return 17

    monkeypatch.setattr(winfs, "_create_relative_raw", create_relative)
    monkeypatch.setattr(winfs, "stat_handle", lambda _handle: link)
    monkeypatch.setattr(winfs, "close_handle", closed.append)

    with pytest.raises(OSError, match="reparse link"):
        winfs.open_relative(
            11,
            "managed",
            directory=True,
            nofollow=True,
            case_sensitive=False,
        )

    assert opened == [None]
    assert closed == [17]


@requires_windows
def test_windows_capability_gate_is_true_when_nt_entry_points_resolve() -> None:
    assert winfs.missing_apis() == []
    assert _selection_fs.supports_descriptor_traversal() is True


@requires_windows
def test_windows_parent_open_rechecks_identity_from_held_directory(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    flags = _selection_fs._directory_flags(nofollow=True)
    child_handle = _selection_fs._open_descriptor(child, flags)
    parent_handle: int | None = None
    expected_handle: int | None = None
    try:
        parent_handle = _selection_fs._open_descriptor(
            "..", flags, dir_fd=child_handle, allow_parent=True
        )
        expected_handle = _selection_fs._open_descriptor(parent, flags)
        assert _selection_fs._safe_identity(parent_handle) == (
            _selection_fs._safe_identity(expected_handle)
        )
    finally:
        if expected_handle is not None:
            _selection_fs._close_quietly(expected_handle)
        if parent_handle is not None:
            _selection_fs._close_quietly(parent_handle)
        _selection_fs._close_quietly(child_handle)


def test_windows_capability_gate_names_missing_nt_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutant that reports capability despite one missing API fails closed."""

    monkeypatch.setattr(_selection_fs, "_WINDOWS_SELECTION", True)
    monkeypatch.setattr(winfs, "_API", None)
    monkeypatch.setattr(winfs, "_API_MISSING", ("ntdll.NtCreateFile",))
    assert _selection_fs.supports_descriptor_traversal() is False
    assert _selection_fs._missing_traversal_mechanisms() == ["ntdll.NtCreateFile"]
    with pytest.raises(source_errors.SourceError) as excinfo:
        selection.resolve_selector_directory(Path("C:/does-not-exist"), ".")
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID
    assert "ntdll.NtCreateFile" in excinfo.value.detail


@requires_windows
def test_windows_ads_selector_is_rejected_before_relative_nt_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing component guard would hand this existing ADS to NtCreateFile."""

    root = tmp_path / "source"
    root.mkdir()
    file = root / "SKILL.md"
    file.write_text("contents", encoding="utf-8")
    stream = f"{file}:selection-probe"
    try:
        with open(stream, "wb") as handle:
            handle.write(b"ads")
    except OSError as exc:
        pytest.skip(f"NTFS alternate data stream creation unavailable: {exc}")

    with pytest.raises(source_errors.SourceError) as selector_error:
        selection.resolve_selector_directory(root, "SKILL.md:selection-probe")
    assert selector_error.value.code == source_errors.CODE_SELECTION_INVALID

    parent = _selection_fs._open_descriptor(
        root, _selection_fs._directory_flags(nofollow=False)
    )
    relative_open_called = False

    def unexpected_nt_open(*_args: object, **_kwargs: object) -> int:
        nonlocal relative_open_called
        relative_open_called = True
        raise AssertionError("ADS component reached NtCreateFile")

    monkeypatch.setattr(winfs, "_create_relative_raw", unexpected_nt_open)
    try:
        with pytest.raises(OSError, match="invalid Windows path component"):
            _selection_fs._open_descriptor(
                "SKILL.md:selection-probe",
                _selection_fs._WINDOWS_FILE_FLAG,
                dir_fd=parent,
            )
        assert not relative_open_called
    finally:
        _selection_fs._close_quietly(parent)


@requires_windows
@pytest.mark.parametrize("name", ("trailing.", "trailing ", "CON", "NUL.txt"))
def test_windows_win32_alias_selectors_refuse_before_relative_nt_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """A publication-normalization alias must never reach a relative NT open."""

    root = tmp_path / "source"
    root.mkdir()
    parent = _selection_fs._open_descriptor(
        root, _selection_fs._directory_flags(nofollow=False)
    )
    original_open = winfs._create_relative_raw
    relative_name_reached = False

    def record_component(
        parent_handle: int,
        component: str,
        *,
        directory: bool | None,
        nofollow: bool,
        case_sensitive: bool,
    ) -> int:
        nonlocal relative_name_reached
        if component == name:
            relative_name_reached = True
            raise AssertionError(f"Win32 alias {component!r} reached NtCreateFile")
        return original_open(
            parent_handle,
            component,
            directory=directory,
            nofollow=nofollow,
            case_sensitive=case_sensitive,
        )

    monkeypatch.setattr(winfs, "_create_relative_raw", record_component)
    try:
        with pytest.raises(OSError, match="not publishable"):
            _selection_fs._open_descriptor(
                name,
                _selection_fs._WINDOWS_ANY_FLAG,
                dir_fd=parent,
            )
        assert not relative_name_reached
    finally:
        _selection_fs._close_quietly(parent)


@requires_windows
def test_windows_phase_a_absolute_link_plan_refuses_escape(tmp_path: Path) -> None:
    """A mutant that admits a target outside the held root fails this test."""

    root = tmp_path / "source"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    root_handle = _selection_fs._open_root(root, phase="a")
    try:
        root_stat = _selection_fs._fstat(
            root_handle,
            code=source_errors.CODE_SELECTION_INVALID,
            context="Windows containment fixture",
            path=".",
        )
        plan = _selection_fs._phase_a_absolute_link_plan(
            root / "alias",
            os.fspath(outside),
            root_identity=_selection_fs._identity_from_stat(root_stat),
            home_identity=None,
            root_spelling=os.fspath(root),
        )
    finally:
        _selection_fs._close_quietly(root_handle)

    assert plan.components is None
    assert plan.detail is not None
    assert "symlink target escapes the source root" in plan.detail


def test_windows_symlink_escape_is_rejected(tmp_path: Path) -> None:
    """A mutant that follows an escaping symbolic link admits an outside selector."""

    root = tmp_path / "source"
    root.mkdir()
    # A bad escape fallback that treats the external target as the source root
    # would select this decoy and make the test observe the admitted path.
    _write_skill(root / "review", "review")
    _write_skill(tmp_path / "outside" / "review", "review")
    _symlink_or_skip(root / "skills", tmp_path / "outside")
    with pytest.raises(source_errors.SourceError) as excinfo:
        selection.resolve_selector_directory(root, "skills/review")
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@requires_windows
def test_windows_junction_escape_is_rejected(tmp_path: Path) -> None:
    """A mutant that treats mount-point reparse data as an ordinary directory fails."""

    root = tmp_path / "source"
    root.mkdir()
    _write_skill(root / "review", "review")
    outside = tmp_path / "outside"
    _write_skill(outside / "review", "review")
    _junction_or_skip(root / "skills", outside)
    with pytest.raises(source_errors.SourceError) as excinfo:
        selection.resolve_selector_directory(root, "skills/review")
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@requires_windows
def test_windows_in_root_junction_resolves_through_link_plan(tmp_path: Path) -> None:
    """An in-root mount point resolves to its frozen physical directory."""

    root = tmp_path / "source"
    target = _write_skill(root / "physical" / "review", "review")
    _junction_or_skip(root / "alias", root / "physical")

    selected = selection.resolve_selector_directory(root, "alias/review")

    assert os.path.samefile(selected, target)


def test_windows_link_identity_is_rechecked_after_phase_a(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mutant that drops the Phase-A link identity comparison admits the swap."""

    root = tmp_path / "source"
    _write_skill(root / "inside" / "review", "review")
    link = root / "alias"
    replacement = root / "replacement"
    _symlink_or_skip(link, Path("inside"))
    _symlink_or_skip(replacement, Path("inside"))
    original_prepare = _selection_fs._prepare_preflight
    swapped = False

    def prepare_then_swap(*args: object, **kwargs: object) -> object:
        nonlocal swapped
        result = original_prepare(*args, **kwargs)
        if not swapped:
            link.unlink()
            # Move a pre-existing link with a distinct file ID into place.
            # Keep the target identical so only the changed reparse-point
            # identity can reject this Phase-A/Phase-B replacement.
            replacement.replace(link)
            swapped = True
        return result

    monkeypatch.setattr(_selection_fs, "_prepare_preflight", prepare_then_swap)
    with pytest.raises(source_errors.SourceError) as excinfo:
        selection.resolve_selector_directory(root, "alias/review")
    assert swapped
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_windows_hard_link_is_refused(tmp_path: Path) -> None:
    """A mutant that admits a NumberOfLinks value above one fails this case."""

    root = tmp_path / "source"
    skill = _write_skill(root / "review", "review")
    os.link(skill / "SKILL.md", root / "duplicate.md")
    with pytest.raises(source_errors.SourceError) as excinfo:
        selection.resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="review"
            ),
        )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@requires_windows
def test_windows_skill_manifest_case_pair_collision_refuses(tmp_path: Path) -> None:
    """A mutant that admits names differing only by case accepts both skills."""

    root = tmp_path / "source"
    _write_skill(root / "collection" / "first", "Review")
    _write_skill(root / "collection" / "second", "review")
    with pytest.raises(source_errors.SourceError) as excinfo:
        selection.expand_collection(
            root,
            CollectionSelector(
                from_alias="local",
                directory="collection",
                include=("first", "second"),
            ),
        )
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT


def test_windows_default_directory_case_pair_collision_refuses(
    tmp_path: Path,
) -> None:
    """A case-fold mutant accepts two inventory paths on a conflating host."""

    root = tmp_path / "source"
    root.mkdir()
    (root / "CaseProbe.txt").write_text("probe", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    captured = snapshot.capture_package_snapshot(root, ".", home=home)
    if not captured.equivalence.case_known or not captured.equivalence.case_conflates:
        pytest.skip(
            "temporary source directory did not prove case-insensitive name equivalence"
        )

    with pytest.raises(source_errors.SourcePathConflictError):
        local_snapshot.build_inventory(
            (
                ("Alpha/Review.md", "sha256:" + "0" * 64, False),
                ("alpha/review.md", "sha256:" + "1" * 64, False),
            ),
            equivalent=captured.equivalence.equivalent,
        )


@requires_windows
def test_windows_snapshot_capture_preserves_raw_bytes_and_inventory_rule(
    tmp_path: Path,
) -> None:
    """Snapshot reads raw handle bytes and keeps Windows executable bits false."""

    root = tmp_path / "source"
    root.mkdir()
    raw = b"snapshot bytes\r\nline two\n"
    (root / "SKILL.md").write_bytes(raw)
    home = tmp_path / "home"
    home.mkdir()

    captured = snapshot.capture_package_snapshot(root, ".", home=home)

    frozen = captured.frozen_files()
    assert frozen["SKILL.md"].data == raw
    assert frozen["SKILL.md"].executable is False
    assert captured.inventory["files"][0]["sha256"] == (
        "sha256:" + hashlib.sha256(raw).hexdigest()
    )


@requires_windows
def test_windows_selector_case_variant_matches_enumerated_name(tmp_path: Path) -> None:
    """A mutant that compares case by OS default instead of directory behavior fails."""

    root = tmp_path / "source"
    actual = _write_skill(root / "Review", "Review")
    resolved = selection.resolve_selector_directory(root, "rEvIeW")
    assert os.path.samefile(resolved, actual)


@requires_windows
def test_windows_short_name_only_selector_refuses(tmp_path: Path) -> None:
    """A mutant that opens selectors before long-name enumeration admits 8.3 aliases."""

    root = tmp_path / "source"
    actual = _write_skill(root / "LongFolderNameForShortAlias", "LongFolderNameForShortAlias")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    short_path = kernel32.GetShortPathNameW
    short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    short_path.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = int(short_path(str(actual), buffer, len(buffer)))
    if length == 0:
        pytest.skip("GetShortPathNameW could not inspect the runner volume")
    short_name = Path(buffer.value).name
    if short_name.casefold() == actual.name.casefold():
        pytest.skip("runner volume has 8.3 name generation disabled; matcher unit covers this rule")
    with pytest.raises(source_errors.SourceError) as excinfo:
        selection.resolve_selector_directory(root, short_name)
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_windows_file_replaced_by_directory_between_listing_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mutant that admits the opened directory as a member fails this test."""

    root = tmp_path / "source"
    skill = _write_skill(root / "review", "review")
    skill_file = skill / "SKILL.md"
    original_open = _selection_fs._open_child_file
    replaced = False
    opened_directory = False

    def replace_before_open(parent_fd: int, name: str, *, parent_path: Path | None = None) -> int:
        nonlocal opened_directory, replaced
        if name == "SKILL.md" and not replaced:
            skill_file.unlink()
            skill_file.mkdir()
            replaced = True
        handle = original_open(parent_fd, name, parent_path=parent_path)
        opened_stat = winfs.stat_handle(handle) if os.name == "nt" else os.fstat(handle)
        if replaced and stat.S_ISDIR(opened_stat.st_mode):
            opened_directory = True
        return handle

    monkeypatch.setattr(_selection_fs, "_open_child_file", replace_before_open)
    with pytest.raises(source_errors.SourceError) as excinfo:
        selection.resolve_individual(
            root,
            IndividualSelector(
                name="review", from_alias="local", directory="review"
            ),
        )
    assert replaced
    if os.name == "nt":
        assert not opened_directory, "NT open returned a directory handle for the listed file"
    else:
        assert opened_directory, "POSIX control did not exercise the post-open type guard"
        assert "is not a regular file" in excinfo.value.detail
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
