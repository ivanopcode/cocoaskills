"""Large logical fixtures without allocating their payload on Windows."""

from __future__ import annotations

import os
from pathlib import Path


def truncate_sparse(path: Path, size: int) -> None:
    with path.open("wb") as stream:
        if os.name == "nt":
            import ctypes
            import msvcrt

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            control = kernel.DeviceIoControl
            control.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                                ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
            control.restype = ctypes.c_int
            returned = ctypes.c_uint32()
            # NTFS truncate alone allocates the entire file. Mark this handle
            # sparse before extending EOF; POSIX truncate already leaves holes.
            if not control(msvcrt.get_osfhandle(stream.fileno()), 0x900C4,
                           None, 0, None, 0, ctypes.byref(returned), None):
                raise ctypes.WinError(ctypes.get_last_error())
        stream.truncate(size)
