"""Read Windows known-folder identity without environment-based expansion."""
from __future__ import annotations

import ctypes
import uuid


_FOLDERS = {
    "%PROGRAMFILES%": "905e63b6-c1bf-494e-b29c-65b732d3d21a",
    "%PROGRAMFILES(X86)%": "7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e",
    "%LOCALAPPDATA%": "f1b32785-6fba-4fcf-9d55-7b8e7f157091",
}


def windows_known_folder(root: str) -> str:
    """Return the current user's OS folder; API errors have no env fallback."""
    class GUID(ctypes.Structure):
        _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                    ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

    folder = GUID.from_buffer_copy(uuid.UUID(_FOLDERS[root]).bytes_le)
    dll = getattr(ctypes, "WinDLL")
    shell = dll("shell32.dll", winmode=0x800)
    ole = dll("ole32.dll", winmode=0x800)
    get = shell.SHGetKnownFolderPath
    get.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p)]
    get.restype = ctypes.c_int32
    free = ole.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    output = ctypes.c_void_p()
    try:
        if get(ctypes.byref(folder), 0, None, ctypes.byref(output)) != 0 or not output.value:
            raise OSError("SHGetKnownFolderPath failed")
        return ctypes.wstring_at(output)
    finally:
        # SHGetKnownFolderPath requires freeing the output even on failure.
        if output.value:
            free(output)
