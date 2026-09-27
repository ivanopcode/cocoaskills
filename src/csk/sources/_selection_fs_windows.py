"""Handle-relative Windows backend for schema-2 source selection.

The backend deliberately uses NT relative opens for every descendant. Win32
path opens are reserved for an explicitly supplied root; directory traversal,
metadata, enumeration, links, reads, and close all stay on held handles.
"""

from __future__ import annotations

import ctypes
import errno
import ntpath
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_GENERIC_READ = 0x80000000
_FILE_READ_DATA = 0x0001
_FILE_LIST_DIRECTORY = 0x0001
_FILE_READ_ATTRIBUTES = 0x0080
_SYNCHRONIZE = 0x00100000
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_OPEN = 1
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_OPEN_REPARSE_POINT = 0x00200000
_OBJ_CASE_INSENSITIVE = 0x00000040
_FILE_STANDARD_INFO_CLASS = 1
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_FILE_ID_INFO_CLASS = 18
_FILE_CASE_SENSITIVE_INFO_CLASS = 23
_FILE_ID_BOTH_DIRECTORY_INFORMATION_CLASS = 37
_FILE_CASE_SENSITIVE_DIR = 0x00000001
_FSCTL_GET_REPARSE_POINT = 0x000900A8
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_IO_REPARSE_TAG_SYMLINK = 0xA000000C
_SYMLINK_FLAG_RELATIVE = 0x00000001
_SUPPORTED_REPARSE_TAGS = frozenset(
    {_IO_REPARSE_TAG_MOUNT_POINT, _IO_REPARSE_TAG_SYMLINK}
)
_STATUS_NO_MORE_FILES = 0x80000006
_CSTR_EQUAL = 2
_FILE_ID_UNSUPPORTED_ERRORS = frozenset({1, 50, 87, 120})

_REPARSE_TAG_NAMES = {
    _IO_REPARSE_TAG_MOUNT_POINT: "IO_REPARSE_TAG_MOUNT_POINT",
    _IO_REPARSE_TAG_SYMLINK: "IO_REPARSE_TAG_SYMLINK",
    0x80000013: "IO_REPARSE_TAG_DEDUP",
    0x80000018: "IO_REPARSE_TAG_WCI",
    0x8000001B: "IO_REPARSE_TAG_APPEXECLINK",
    0x9000001A: "IO_REPARSE_TAG_CLOUD",
    0x9000101A: "IO_REPARSE_TAG_CLOUD_1",
    0x9000201A: "IO_REPARSE_TAG_CLOUD_2",
    0x9000301A: "IO_REPARSE_TAG_CLOUD_3",
    0x9000401A: "IO_REPARSE_TAG_CLOUD_4",
    0x9000501A: "IO_REPARSE_TAG_CLOUD_5",
    0x9000601A: "IO_REPARSE_TAG_CLOUD_6",
    0x9000701A: "IO_REPARSE_TAG_CLOUD_7",
    0x9000801A: "IO_REPARSE_TAG_CLOUD_8",
    0x9000901A: "IO_REPARSE_TAG_CLOUD_9",
    0x9000A01A: "IO_REPARSE_TAG_CLOUD_A",
    0x9000B01A: "IO_REPARSE_TAG_CLOUD_B",
    0x9000C01A: "IO_REPARSE_TAG_CLOUD_C",
    0x9000D01A: "IO_REPARSE_TAG_CLOUD_D",
    0x9000E01A: "IO_REPARSE_TAG_CLOUD_E",
    0x9000F01A: "IO_REPARSE_TAG_CLOUD_F",
}


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_wchar_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", ctypes.c_ulong),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class _FileStandardInfo(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", ctypes.c_ulong),
        ("DeletePending", ctypes.c_ubyte),
        ("Directory", ctypes.c_ubyte),
    ]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", ctypes.c_ulong), ("ReparseTag", ctypes.c_ulong)]


class _FileId128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_ulonglong), ("FileId", _FileId128)]


class _FileCaseSensitiveInfo(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_ulong)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", ctypes.c_ulong),
        ("CreationTimeLow", ctypes.c_ulong),
        ("CreationTimeHigh", ctypes.c_ulong),
        ("LastAccessTimeLow", ctypes.c_ulong),
        ("LastAccessTimeHigh", ctypes.c_ulong),
        ("LastWriteTimeLow", ctypes.c_ulong),
        ("LastWriteTimeHigh", ctypes.c_ulong),
        ("VolumeSerialNumber", ctypes.c_ulong),
        ("FileSizeHigh", ctypes.c_ulong),
        ("FileSizeLow", ctypes.c_ulong),
        ("NumberOfLinks", ctypes.c_ulong),
        ("FileIndexHigh", ctypes.c_ulong),
        ("FileIndexLow", ctypes.c_ulong),
    ]


@dataclass(frozen=True)
class WindowsStat:
    """The stat fields consumed by the platform-neutral selection layer."""

    st_mode: int
    st_ino: int
    st_dev: int
    st_nlink: int
    st_uid: int
    st_gid: int
    st_size: int
    st_atime: float
    st_mtime: float
    st_ctime: float
    st_file_attributes: int
    st_reparse_tag: int


@dataclass(frozen=True)
class _WinApi:
    kernel32: Any
    ntdll: Any


class UnsupportedReparseTagError(OSError):
    """A reparse point whose semantics are outside source selection's contract."""

    def __init__(self, tag: int) -> None:
        self.tag = tag
        super().__init__(errno.EINVAL, f"unsupported {describe_reparse_tag(tag)}")


_API: _WinApi | None = None
_API_MISSING: tuple[str, ...] | None = None


_REQUIRED_APIS = (
    ("kernel32", "CreateFileW"),
    ("kernel32", "CloseHandle"),
    ("kernel32", "GetFileInformationByHandleEx"),
    ("kernel32", "GetFileInformationByHandle"),
    ("kernel32", "GetFinalPathNameByHandleW"),
    ("kernel32", "DeviceIoControl"),
    ("kernel32", "ReadFile"),
    ("kernel32", "CompareStringOrdinal"),
    ("ntdll", "NtCreateFile"),
    ("ntdll", "NtQueryDirectoryFile"),
    ("ntdll", "RtlNtStatusToDosError"),
)


def _load_api() -> tuple[_WinApi | None, tuple[str, ...]]:
    if os.name != "nt":
        return None, ("Windows NT APIs (requires Windows)",)
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return None, ("ctypes.WinDLL",)
    try:
        kernel32 = loader("kernel32", use_last_error=True)
        ntdll = loader("ntdll", use_last_error=True)
    except (OSError, AttributeError):
        return None, ("kernel32/ntdll entry points",)
    libraries = {"kernel32": kernel32, "ntdll": ntdll}
    missing = tuple(
        f"{library}.{name}"
        for library, name in _REQUIRED_APIS
        if not hasattr(libraries[library], name)
    )
    if missing:
        return None, missing

    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.GetFileInformationByHandleEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.GetFileInformationByHandleEx.restype = ctypes.c_int
    kernel32.GetFileInformationByHandle.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = ctypes.c_int
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar),
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
    kernel32.DeviceIoControl.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.restype = ctypes.c_int
    kernel32.ReadFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = ctypes.c_int
    kernel32.CompareStringOrdinal.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    kernel32.CompareStringOrdinal.restype = ctypes.c_int
    ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    ntdll.NtCreateFile.restype = ctypes.c_long
    ntdll.NtQueryDirectoryFile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_ubyte,
        ctypes.POINTER(_UnicodeString),
        ctypes.c_ubyte,
    ]
    ntdll.NtQueryDirectoryFile.restype = ctypes.c_long
    ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
    ntdll.RtlNtStatusToDosError.restype = ctypes.c_ulong
    return _WinApi(kernel32, ntdll), ()


def _api() -> _WinApi | None:
    global _API, _API_MISSING
    if _API_MISSING is None:
        _API, _API_MISSING = _load_api()
    return _API


def missing_apis() -> list[str]:
    """Return unresolved entry points, without treating the OS name as proof."""

    _api()
    return list(_API_MISSING or ())


def _handle_arg(handle: int) -> ctypes.c_void_p:
    return ctypes.c_void_p(handle)


def _win_error(code: int | None = None, *, operation: str) -> OSError:
    if code is None:
        get_last_error = getattr(ctypes, "get_last_error", None)
        code = int(get_last_error()) if get_last_error is not None else 1
    win_error = getattr(ctypes, "WinError", None)
    if win_error is not None:
        error = cast(OSError, win_error(code))
        error.args = (*error.args[:1], f"{operation}: {error}")
        return error
    mapped = {
        2: errno.ENOENT,
        3: errno.ENOENT,
        5: errno.EACCES,
        32: errno.EACCES,
        50: errno.ENOTSUP,
        87: errno.EINVAL,
        123: errno.EINVAL,
        267: errno.ENOTDIR,
        1920: errno.ELOOP,
    }.get(code, errno.EIO)
    error = OSError(mapped, f"{operation}: WinError {code}")
    error.winerror = code  # type: ignore[attr-defined]
    return error


def _nt_error(api: _WinApi, status: int, *, operation: str) -> OSError:
    dos_error = int(api.ntdll.RtlNtStatusToDosError(status))
    return _win_error(dos_error, operation=operation)


def _query_info(handle: int, info_class: int, structure: Any, *, operation: str) -> Any:
    api = _api()
    if api is None:
        raise OSError(errno.ENOSYS, "Windows handle APIs are unavailable")
    ok = api.kernel32.GetFileInformationByHandleEx(
        _handle_arg(handle), info_class, ctypes.byref(structure), ctypes.sizeof(structure)
    )
    if not ok:
        raise _win_error(operation=operation)
    return structure


def _file_identity(handle: int) -> tuple[int, int]:
    api = _api()
    if api is None:
        raise OSError(errno.ENOSYS, "Windows handle APIs are unavailable")
    info = _FileIdInfo()
    if api.kernel32.GetFileInformationByHandleEx(
        _handle_arg(handle), _FILE_ID_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ):
        return (
            int(info.VolumeSerialNumber),
            int.from_bytes(bytes(info.FileId.Identifier), "little"),
        )
    error = _win_error(operation="GetFileInformationByHandleEx(FileIdInfo)")
    error_code = getattr(error, "winerror", None)
    if error_code is None and error.errno in _FILE_ID_UNSUPPORTED_ERRORS:
        error_code = error.errno
    if error_code not in _FILE_ID_UNSUPPORTED_ERRORS:
        raise error

    legacy = _ByHandleFileInformation()
    if not api.kernel32.GetFileInformationByHandle(
        _handle_arg(handle), ctypes.byref(legacy)
    ):
        raise _win_error(operation="GetFileInformationByHandle")
    file_id = (int(legacy.FileIndexHigh) << 32) | int(legacy.FileIndexLow)
    return int(legacy.VolumeSerialNumber), file_id


def stat_handle(handle: int) -> WindowsStat:
    """Read identity, type, link count, size and reparse metadata by handle."""

    standard = _query_info(
        handle,
        _FILE_STANDARD_INFO_CLASS,
        _FileStandardInfo(),
        operation="GetFileInformationByHandleEx(FileStandardInfo)",
    )
    attributes = _query_info(
        handle,
        _FILE_ATTRIBUTE_TAG_INFO_CLASS,
        _FileAttributeTagInfo(),
        operation="GetFileInformationByHandleEx(FileAttributeTagInfo)",
    )
    volume, file_id = _file_identity(handle)
    attrs = int(attributes.FileAttributes)
    tag = int(attributes.ReparseTag) if attrs & _FILE_ATTRIBUTE_REPARSE_POINT else 0
    if tag in _SUPPORTED_REPARSE_TAGS:
        mode = stat.S_IFLNK | 0o777
    elif attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        mode = stat.S_IFCHR | 0o666
    elif bool(standard.Directory):
        mode = stat.S_IFDIR | 0o777
    else:
        mode = stat.S_IFREG | 0o666
    return WindowsStat(
        st_mode=mode,
        st_ino=file_id,
        st_dev=volume,
        st_nlink=int(standard.NumberOfLinks),
        st_uid=0,
        st_gid=0,
        st_size=int(standard.EndOfFile),
        st_atime=0.0,
        st_mtime=0.0,
        st_ctime=0.0,
        st_file_attributes=attrs,
        st_reparse_tag=tag,
    )


def _reparse_tag(value: WindowsStat) -> int:
    return value.st_reparse_tag


def _check_supported_reparse(value: WindowsStat) -> None:
    if (
        value.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        and _reparse_tag(value) not in _SUPPORTED_REPARSE_TAGS
    ):
        raise UnsupportedReparseTagError(_reparse_tag(value))


def _create_file_path(path: str, *, directory: bool, nofollow: bool) -> int:
    if "\0" in path:
        # c_wchar_p passes a NUL-terminated pointer to CreateFileW, which
        # would otherwise silently open only the prefix before the NUL.
        raise OSError(errno.EINVAL, "NUL is invalid in a Windows root path")
    api = _api()
    if api is None:
        missing = ", ".join(missing_apis()) or "Windows NT APIs"
        raise OSError(errno.ENOSYS, f"missing Windows filesystem API: {missing}")
    flags = _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT
    raw = api.kernel32.CreateFileW(
        path,
        _GENERIC_READ | _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if raw == _INVALID_HANDLE_VALUE:
        raise _win_error(operation="CreateFileW")
    handle = int(raw)
    try:
        entry = stat_handle(handle)
        _check_supported_reparse(entry)
        if entry.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            if nofollow:
                raise OSError(errno.ELOOP, f"reparse link at {path}")
            _close_raw(handle)
            handle = 0
            follow_flags = _FILE_FLAG_BACKUP_SEMANTICS
            followed = api.kernel32.CreateFileW(
                path,
                _GENERIC_READ | _FILE_READ_ATTRIBUTES,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                None,
                _OPEN_EXISTING,
                follow_flags,
                None,
            )
            if followed == _INVALID_HANDLE_VALUE:
                raise _win_error(operation="CreateFileW(follow root link)")
            handle = int(followed)
            entry = stat_handle(handle)
        _check_expected_kind(entry, directory=directory, path=path)
        return handle
    except BaseException:
        _close_raw(handle)
        raise


def _final_path_from_handle(handle: int) -> str:
    """Read the normalized DOS path Windows reports for a held handle."""

    api = _api()
    if api is None:
        missing = ", ".join(missing_apis()) or "Windows NT APIs"
        raise OSError(errno.ENOSYS, f"missing Windows filesystem API: {missing}")
    capacity = 512
    while capacity <= 32768:
        buffer = ctypes.create_unicode_buffer(capacity)
        length = int(
            api.kernel32.GetFinalPathNameByHandleW(
                _handle_arg(handle), buffer, capacity, 0
            )
        )
        if length == 0:
            raise _win_error(operation="GetFinalPathNameByHandleW")
        if length < capacity:
            return buffer.value
        capacity = length + 1
    raise OSError(errno.ENAMETOOLONG, "normalized handle path exceeds Windows limit")


def normalized_component_name(handle: int) -> str:
    """Return the normalized long on-disk name for one held child handle."""

    path = _final_path_from_handle(handle).rstrip("\\/")
    name = ntpath.basename(path)
    if not name or name in {".", ".."}:
        raise OSError(
            errno.EIO,
            "GetFinalPathNameByHandleW returned no normalized child component",
        )
    return name


def _open_parent_handle(handle: int) -> int:
    """Open and identity-check the physical parent of a held directory.

    NtCreateFile does not resolve ``..`` relative to RootDirectory. Use the
    kernel-reported normalized name only for this upward ancestry operation,
    then verify the original handle still appears under the opened parent by
    its exact name and FileId. Descendant traversal continues to use bare-name
    NtCreateFile opens exclusively.
    """

    final_path = _final_path_from_handle(handle)
    drive, tail = ntpath.splitdrive(final_path)
    if drive and not tail.strip("\\/"):
        return _create_file_path(final_path, directory=True, nofollow=True)

    child_path = final_path.rstrip("\\/")
    parent_path = ntpath.dirname(child_path)
    child_name = ntpath.basename(child_path)
    if not parent_path or not child_name:
        raise OSError(errno.EIO, "cannot derive parent of held directory handle")

    parent_handle = _create_file_path(parent_path, directory=True, nofollow=True)
    try:
        child = _create_relative_raw(
            parent_handle,
            child_name,
            directory=True,
            nofollow=True,
            case_sensitive=True,
        )
        try:
            if identity_from_handle(child) != identity_from_handle(handle):
                raise OSError(
                    getattr(errno, "ESTALE", errno.EIO),
                    "held directory changed physical parent",
                )
        finally:
            close_handle(child)
        return parent_handle
    except BaseException:
        close_handle(parent_handle)
        raise


def _create_relative_raw(
    parent: int,
    name: str,
    *,
    directory: bool | None,
    nofollow: bool,
    case_sensitive: bool,
) -> int:
    validate_component(name)
    api = _api()
    if api is None:
        missing = ", ".join(missing_apis()) or "Windows NT APIs"
        raise OSError(errno.ENOSYS, f"missing Windows filesystem API: {missing}")
    name_buffer = ctypes.create_unicode_buffer(name)
    encoded_length = len(name.encode("utf-16-le", errors="surrogatepass"))
    unicode_name = _UnicodeString(
        encoded_length,
        encoded_length + ctypes.sizeof(ctypes.c_wchar),
        ctypes.cast(name_buffer, ctypes.c_wchar_p),
    )
    object_attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        _handle_arg(parent),
        ctypes.pointer(unicode_name),
        0 if case_sensitive else _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    io_status = _IoStatusBlock()
    handle_value = ctypes.c_void_p()
    data_access = (
        _FILE_LIST_DIRECTORY
        if directory is True
        else _FILE_READ_DATA
        if directory is False
        else 0
    )
    access = data_access | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    options = _FILE_SYNCHRONOUS_IO_NONALERT
    if nofollow:
        options |= _FILE_OPEN_REPARSE_POINT
    if directory is True:
        options |= _FILE_DIRECTORY_FILE
    elif directory is False:
        options |= _FILE_NON_DIRECTORY_FILE
    status = int(
        api.ntdll.NtCreateFile(
            ctypes.byref(handle_value),
            access,
            ctypes.byref(object_attributes),
            ctypes.byref(io_status),
            None,
            _FILE_ATTRIBUTE_NORMAL,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            _FILE_OPEN,
            options,
            None,
            0,
        )
    )
    if status < 0:
        raise _nt_error(api, status, operation=f"NtCreateFile({name!r})")
    if handle_value.value is None:
        raise OSError(errno.EIO, f"NtCreateFile({name!r}) returned a null handle")
    handle = int(handle_value.value)
    try:
        entry = stat_handle(handle)
        _check_supported_reparse(entry)
        if (
            not entry.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            and directory is not None
        ):
            _check_expected_kind(entry, directory=directory, path=name)
        return handle
    except BaseException:
        _close_raw(handle)
        raise


def _check_expected_kind(value: WindowsStat, *, directory: bool, path: str) -> None:
    if directory and not stat.S_ISDIR(value.st_mode):
        raise NotADirectoryError(errno.ENOTDIR, f"{path!r} is not a directory")
    if not directory and stat.S_ISDIR(value.st_mode):
        raise IsADirectoryError(errno.EISDIR, f"{path!r} is a directory")


def _close_raw(handle: int) -> None:
    api = _api()
    if api is not None:
        api.kernel32.CloseHandle(_handle_arg(handle))


def close_handle(handle: int) -> None:
    """Close one owned NT handle."""

    _close_raw(handle)


def open_path(path: str, *, directory: bool, nofollow: bool) -> int:
    """Open an explicitly supplied root path with editor-friendly sharing."""

    return _create_file_path(path, directory=directory, nofollow=nofollow)


def open_relative(
    parent: int,
    name: str,
    *,
    directory: bool | None,
    nofollow: bool,
    case_sensitive: bool,
    allow_reparse: bool = False,
    allow_parent: bool = False,
) -> int:
    """Open one validated component relative to a held directory handle."""

    if allow_parent and name == "..":
        return _open_parent_handle(parent)
    validate_component(name, allow_parent=allow_parent)
    probe = _create_relative_raw(
        parent,
        name,
        # A no-follow open must be able to return the reparse point itself.
        # Asking NT for FILE_DIRECTORY_FILE/FILE_NON_DIRECTORY_FILE at this
        # stage can reject a directory symlink before its tag is inspectable.
        # Ordinary objects are reopened with their required type flag below.
        directory=None,
        nofollow=True,
        case_sensitive=case_sensitive,
    )
    try:
        value = stat_handle(probe)
        _check_supported_reparse(value)
        is_reparse = bool(value.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
        if is_reparse and allow_reparse:
            return probe
        if is_reparse and nofollow:
            raise OSError(errno.ELOOP, f"reparse link {name!r}")
        if not is_reparse:
            if directory is not None:
                _check_expected_kind(value, directory=directory, path=name)
                probe_identity = identity_from_handle(probe)
                close_handle(probe)
                probe = 0
                typed = _create_relative_raw(
                    parent,
                    name,
                    directory=directory,
                    nofollow=True,
                    case_sensitive=case_sensitive,
                )
                try:
                    typed_value = stat_handle(typed)
                    _check_supported_reparse(typed_value)
                    if typed_value.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                        raise OSError(errno.ELOOP, f"reparse link {name!r}")
                    _check_expected_kind(typed_value, directory=directory, path=name)
                    if identity_from_handle(typed) != probe_identity:
                        raise OSError(
                            getattr(errno, "ESTALE", errno.EIO),
                            f"{name!r} changed between no-follow and typed opens",
                        )
                except BaseException:
                    close_handle(typed)
                    raise
                return typed
            return probe
    except BaseException:
        if probe:
            close_handle(probe)
        raise
    close_handle(probe)
    followed = _create_relative_raw(
        parent,
        name,
        directory=directory,
        nofollow=False,
        case_sensitive=case_sensitive,
    )
    try:
        value = stat_handle(followed)
        _check_supported_reparse(value)
        if value.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(errno.ELOOP, f"unresolved reparse link {name!r}")
        if directory is not None:
            _check_expected_kind(value, directory=directory, path=name)
        return followed
    except BaseException:
        close_handle(followed)
        raise


def validate_component(name: str, *, allow_parent: bool = False) -> None:
    """Reject names that NT or later Win32 publication could reinterpret."""

    if allow_parent and name == "..":
        return
    if not name or name in {".", ".."}:
        raise OSError(errno.EINVAL, "empty, dot, and parent components are invalid")
    if any(character in name for character in ("\0", ":", "\\", "/")):
        raise OSError(errno.EINVAL, f"invalid Windows path component {name!r}")
    if name.endswith((".", " ")):
        raise OSError(errno.EINVAL, f"Win32-normalized component is not publishable: {name!r}")
    stem = name.split(".", 1)[0].rstrip(" .").upper()
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"COM{number}" for number in range(1, 10))
    reserved.update(f"LPT{number}" for number in range(1, 10))
    reserved.update(f"COM{digit}" for digit in "¹²³")
    reserved.update(f"LPT{digit}" for digit in "¹²³")
    if stem in reserved:
        raise OSError(errno.EINVAL, f"Win32 device-name component is not publishable: {name!r}")


def directory_case_sensitive(handle: int) -> bool:
    info = _query_info(
        handle,
        _FILE_CASE_SENSITIVE_INFO_CLASS,
        _FileCaseSensitiveInfo(),
        operation="GetFileInformationByHandleEx(FileCaseSensitiveInfo)",
    )
    return bool(info.Flags & _FILE_CASE_SENSITIVE_DIR)


def names_equivalent(left: str, right: str, *, case_sensitive: bool) -> bool:
    """Compare names with the directory's actual Windows case rule."""

    if left == right:
        return True
    if case_sensitive:
        return False
    api = _api()
    if api is None:
        raise OSError(errno.ENOSYS, "CompareStringOrdinal is unavailable")
    result = int(api.kernel32.CompareStringOrdinal(left, -1, right, -1, 1))
    if result == 0:
        raise _win_error(operation="CompareStringOrdinal")
    return result == _CSTR_EQUAL


def match_enumerated_name(
    selector: str, names: list[str], *, case_sensitive: bool
) -> str:
    """Resolve only names returned by handle enumeration, never 8.3 aliases."""

    validate_component(selector)
    matches = [
        name
        for name in names
        if names_equivalent(selector, name, case_sensitive=case_sensitive)
    ]
    if not matches:
        raise FileNotFoundError(
            errno.ENOENT, f"component {selector!r} is not an enumerated name"
        )
    if len(matches) != 1:
        raise OSError(
            errno.EEXIST,
            f"component {selector!r} has ambiguous case-equivalent names",
        )
    return matches[0]


def _directory_names(handle: int) -> list[str]:
    api = _api()
    if api is None:
        raise OSError(errno.ENOSYS, "NtQueryDirectoryFile is unavailable")
    names: list[str] = []
    restart = True
    buffer_size = 64 * 1024
    while True:
        buffer = ctypes.create_string_buffer(buffer_size)
        io_status = _IoStatusBlock()
        status = int(
            api.ntdll.NtQueryDirectoryFile(
                _handle_arg(handle),
                None,
                None,
                None,
                ctypes.byref(io_status),
                ctypes.cast(buffer, ctypes.c_void_p),
                buffer_size,
                _FILE_ID_BOTH_DIRECTORY_INFORMATION_CLASS,
                0,
                None,
                int(restart),
            )
        )
        restart = False
        status_u32 = status & 0xFFFFFFFF
        if status_u32 == _STATUS_NO_MORE_FILES:
            break
        if status < 0:
            raise _nt_error(api, status, operation="NtQueryDirectoryFile")
        returned = int(io_status.Information)
        if returned <= 0:
            break
        view = memoryview(buffer).cast("B")
        offset = 0
        while offset + 104 <= returned:
            next_offset = int.from_bytes(view[offset : offset + 4], "little")
            name_length = int.from_bytes(view[offset + 60 : offset + 64], "little")
            end = offset + 104 + name_length
            if name_length % 2 or end > returned:
                raise OSError(errno.EIO, "NtQueryDirectoryFile returned a malformed name")
            name = view[offset + 104 : end].tobytes().decode(
                "utf-16-le", errors="strict"
            )
            if name not in {".", ".."}:
                names.append(name)
            if next_offset == 0:
                break
            if next_offset < 104 or offset + next_offset > returned:
                raise OSError(errno.EIO, "NtQueryDirectoryFile returned an invalid entry offset")
            offset += next_offset
        view.release()
    return names


class WindowsDirEntry:
    """A directory entry whose metadata is opened relative to its parent handle."""

    __slots__ = ("_stat_entry", "name")

    def __init__(
        self,
        name: str,
        stat_entry: Callable[[str, bool], WindowsStat],
    ) -> None:
        self.name = name
        self._stat_entry = stat_entry

    def stat(self, *, follow_symlinks: bool = True) -> WindowsStat:
        return self._stat_entry(self.name, follow_symlinks)


@contextmanager
def scandir(
    handle: int,
    *,
    stat_entry: Callable[[str, bool], WindowsStat],
) -> Iterator[Iterator[WindowsDirEntry]]:
    """Enumerate one held directory; entries retain only that parent handle."""

    entries = iter(WindowsDirEntry(name, stat_entry) for name in _directory_names(handle))
    yield entries


def readlink_handle(
    handle: int, tag: int, *, containing_directory: int | None = None
) -> str:
    """Read a symlink or junction target from its no-follow handle."""

    if tag not in _SUPPORTED_REPARSE_TAGS:
        raise UnsupportedReparseTagError(tag)
    return _read_reparse_target(
        handle, tag, containing_directory=containing_directory
    )


def _read_reparse_target(
    handle: int,
    tag: int,
    *,
    containing_directory: int | None = None,
) -> str:
    api = _api()
    if api is None:
        raise OSError(errno.ENOSYS, "DeviceIoControl is unavailable")
    output = ctypes.create_string_buffer(16 * 1024)
    returned = ctypes.c_uint32()
    ok = api.kernel32.DeviceIoControl(
        _handle_arg(handle),
        _FSCTL_GET_REPARSE_POINT,
        None,
        0,
        ctypes.cast(output, ctypes.c_void_p),
        len(output),
        ctypes.byref(returned),
        None,
    )
    if not ok:
        raise _win_error(operation="DeviceIoControl(FSCTL_GET_REPARSE_POINT)")
    payload_length = int(returned.value)
    if payload_length > len(output):
        raise OSError(errno.EIO, "FSCTL_GET_REPARSE_POINT returned an invalid length")
    payload = ctypes.string_at(ctypes.addressof(output), payload_length)
    return _decode_reparse_target(
        payload,
        tag,
        containing_directory=containing_directory,
    )


def _decode_reparse_target(
    payload: bytes,
    tag: int,
    *,
    containing_directory: int | None = None,
) -> str:
    """Decode the declared reparse buffer and normalize only its target name."""

    if len(payload) < 16:
        raise OSError(errno.EIO, "FSCTL_GET_REPARSE_POINT returned a short buffer")
    actual_tag = int.from_bytes(payload[0:4], "little")
    if actual_tag != tag:
        raise OSError(errno.EIO, "reparse tag changed while reading its target")
    data_length = int.from_bytes(payload[4:6], "little")
    if data_length + 8 != len(payload):
        raise OSError(errno.EIO, "reparse buffer length does not match its header")
    if tag == _IO_REPARSE_TAG_SYMLINK:
        if data_length < 12 or len(payload) < 20:
            raise OSError(errno.EIO, "symbolic-link reparse data is truncated")
        substitute_offset = int.from_bytes(payload[8:10], "little")
        substitute_length = int.from_bytes(payload[10:12], "little")
        print_offset = int.from_bytes(payload[12:14], "little")
        print_length = int.from_bytes(payload[14:16], "little")
        symlink_flags = int.from_bytes(payload[16:20], "little")
        if symlink_flags & ~_SYMLINK_FLAG_RELATIVE:
            raise OSError(errno.EIO, "symbolic-link reparse data has unknown flags")
        relative = bool(symlink_flags & _SYMLINK_FLAG_RELATIVE)
        path_start = 20
    elif tag == _IO_REPARSE_TAG_MOUNT_POINT:
        if data_length < 8:
            raise OSError(errno.EIO, "mount-point reparse data is truncated")
        substitute_offset = int.from_bytes(payload[8:10], "little")
        substitute_length = int.from_bytes(payload[10:12], "little")
        print_offset = int.from_bytes(payload[12:14], "little")
        print_length = int.from_bytes(payload[14:16], "little")
        relative = False
        path_start = 16
    else:
        raise UnsupportedReparseTagError(tag)
    # SubstituteName is the path the filesystem follows. PrintName is only
    # presentation metadata and can intentionally differ from the target.
    name_offset = substitute_offset if substitute_length else print_offset
    name_length = substitute_length if substitute_length else print_length
    start = path_start + name_offset
    end = start + name_length
    path_buffer_length = len(payload) - path_start
    if any(
        offset % 2 or length % 2 or offset + length > path_buffer_length
        for offset, length in (
            (substitute_offset, substitute_length),
            (print_offset, print_length),
        )
    ) or name_length <= 0 or start < path_start or end > len(payload):
        raise OSError(errno.EIO, "reparse target name is malformed")
    target = payload[start:end].decode("utf-16-le", errors="strict")
    if target.startswith("\\??\\UNC\\"):
        target = "\\\\" + target[len("\\??\\UNC\\") :]
    elif target.startswith("\\??\\Volume{"):
        target = "\\\\?\\" + target[len("\\??\\") :]
    elif target.startswith("\\??\\"):
        target = target[len("\\??\\") :]
    elif target.startswith("\\\\?\\UNC\\"):
        target = "\\\\" + target[len("\\\\?\\UNC\\") :]
    elif (
        target.startswith("\\\\?\\")
        and len(target) >= 7
        and target[4].isalpha()
        and target[5] == ":"
    ):
        target = target[len("\\\\?\\") :]
    if not target:
        raise OSError(errno.EIO, "reparse target is empty")
    if tag == _IO_REPARSE_TAG_SYMLINK:
        drive, _ = ntpath.splitdrive(target)
        is_unc = target.startswith(("\\\\", "//"))
        is_root_relative = (
            not drive
            and not is_unc
            and target.startswith(("\\", "/"))
        )
        if is_root_relative:
            # A drive-less rooted name inherits the link's volume, never the
            # process's current drive. Relative links without a root remain
            # relative and are replayed from the held parent in the link plan.
            if containing_directory is None:
                raise OSError(
                    errno.EIO,
                    "root-relative symbolic-link target has no containing directory handle",
                )
            volume, _ = ntpath.splitdrive(_final_path_from_handle(containing_directory))
            if not volume:
                raise OSError(
                    errno.EIO,
                    "cannot resolve root-relative symbolic-link target volume",
                )
            target = volume + target.replace("/", "\\")
        elif relative and (drive or is_unc):
            raise OSError(
                errno.EIO,
                "relative symbolic-link flag conflicts with an absolute target",
            )
        elif not relative and not (drive or is_unc):
            raise OSError(
                errno.EIO,
                "absolute symbolic-link flag conflicts with a relative target",
            )
    return target


def read_handle(handle: int, size: int) -> bytes:
    """Read from an already opened Phase-B file handle."""

    if size <= 0:
        return b""
    api = _api()
    if api is None:
        raise OSError(errno.ENOSYS, "ReadFile is unavailable")
    # The shared seam asks for 1 MiB chunks. Windows selection only needs a
    # bounded kernel buffer; copying the returned byte count avoids materializing
    # the unused tail of every short read.
    buffer_size = min(size, 64 * 1024)
    buffer = ctypes.create_string_buffer(buffer_size)
    read = ctypes.c_uint32()
    ok = api.kernel32.ReadFile(
        _handle_arg(handle),
        ctypes.cast(buffer, ctypes.c_void_p),
        buffer_size,
        ctypes.byref(read),
        None,
    )
    if not ok:
        raise _win_error(operation="ReadFile")
    return ctypes.string_at(ctypes.addressof(buffer), int(read.value))


def identity_from_handle(handle: int) -> tuple[int, int]:
    """Expose the same two-integer identity shape as ``(st_dev, st_ino)``."""

    return _file_identity(handle)


def describe_reparse_tag(tag: int) -> str:
    name = _REPARSE_TAG_NAMES.get(tag)
    return f"{name} (0x{tag:08X})" if name else f"unknown reparse tag 0x{tag:08X}"
