from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Final

_ERROR_HANDLE_EOF: Final = 38
_ERROR_INVALID_PARAMETER: Final = 87
_STREAM_NAME_CAPACITY: Final = 296
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value


class _Win32FindStreamData(ctypes.Structure):
    _fields_ = [
        ("stream_size", ctypes.c_longlong),
        ("stream_name", ctypes.c_wchar * _STREAM_NAME_CAPACITY),
    ]


def named_data_streams(path: Path) -> tuple[str, ...]:
    """Return non-default Windows $DATA streams attached to a path."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    find_first = kernel32.FindFirstStreamW
    find_first.argtypes = [
        wintypes.LPCWSTR,
        wintypes.INT,
        ctypes.POINTER(_Win32FindStreamData),
        wintypes.DWORD,
    ]
    find_first.restype = wintypes.HANDLE
    find_next = kernel32.FindNextStreamW
    find_next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_Win32FindStreamData),
    ]
    find_next.restype = wintypes.BOOL
    find_close = kernel32.FindClose
    find_close.argtypes = [wintypes.HANDLE]
    find_close.restype = wintypes.BOOL

    data = _Win32FindStreamData()
    handle = find_first(_extended_path(path), 0, ctypes.byref(data), 0)
    if handle == _INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()  # type: ignore[attr-defined]
        if error in {_ERROR_HANDLE_EOF, _ERROR_INVALID_PARAMETER}:
            return ()
        raise OSError(error, "cannot enumerate Windows data streams", os.fspath(path))

    streams: list[str] = []
    enumeration_error: BaseException | None = None
    try:
        while True:
            name = str(data.stream_name)
            if name.casefold() != "::$data":
                streams.append(name)
            if find_next(handle, ctypes.byref(data)):
                continue
            error = ctypes.get_last_error()  # type: ignore[attr-defined]
            if error != _ERROR_HANDLE_EOF:
                raise OSError(error, "cannot enumerate Windows data streams", os.fspath(path))
            break
    except BaseException as exc:
        enumeration_error = exc

    close_error: OSError | None = None
    if not find_close(handle):
        error = ctypes.get_last_error()  # type: ignore[attr-defined]
        close_error = OSError(
            error,
            "cannot close Windows data stream enumeration",
            os.fspath(path),
        )

    if enumeration_error is not None:
        if close_error is not None:
            raise enumeration_error from close_error
        raise enumeration_error
    if close_error is not None:
        raise close_error
    return tuple(streams)


def _extended_path(path: Path) -> str:
    value = os.fspath(path)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value
