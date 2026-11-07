"""Native DACL operations on fixture-owned files; no shell or agent involved."""

import ctypes
from ctypes import wintypes
import os


_DACL_SECURITY_INFORMATION = 0x00000004
_SE_DACL_PROTECTED = 0x1000


def _api():
    api = ctypes.WinDLL('advapi32', use_last_error=True)
    api.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileSecurityW.restype = wintypes.BOOL
    api.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    api.SetFileSecurityW.restype = wintypes.BOOL
    api.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
                                                ctypes.POINTER(wintypes.DWORD)]
    api.GetSecurityDescriptorControl.restype = wintypes.BOOL
    api.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
    api.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    return api


def _checked(result, operation):
    if not result:
        error = ctypes.get_last_error()
        raise OSError(error, f'{operation}: {ctypes.FormatError(error)}')


def _descriptor(api, path):
    needed = wintypes.DWORD()
    ctypes.set_last_error(0)
    result = api.GetFileSecurityW(os.fspath(path), _DACL_SECURITY_INFORMATION, None, 0,
                                  ctypes.byref(needed))
    # The sizing query must fail with ERROR_INSUFFICIENT_BUFFER, not silently
    # turn a missing/unreadable fixture into an empty or unrestricted DACL.
    if result or ctypes.get_last_error() != 122:
        _checked(False, 'windows_acl_fixture read DACL size')
    descriptor = ctypes.create_string_buffer(needed.value)
    _checked(api.GetFileSecurityW(os.fspath(path), _DACL_SECURITY_INFORMATION,
                                  descriptor, needed.value, ctypes.byref(needed)),
             'windows_acl_fixture read DACL')
    return descriptor


def copy_dacl(source, destination):
    """Copy the access rules and inheritance protection, leaving ownership alone.

    Both generated siblings already have the same owner. Native keygen must see
    the private sibling's access rules on its public sibling, rather than
    inherited broad read permission.
    """
    api = _api()
    descriptor = _descriptor(api, source)
    control, revision = wintypes.WORD(), wintypes.DWORD()
    _checked(api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control),
                                              ctypes.byref(revision)),
             'windows_acl_fixture copy_dacl read protection')
    protection = 0x80000000 if control.value & _SE_DACL_PROTECTED else 0x20000000
    _checked(api.SetFileSecurityW(os.fspath(destination),
                                 _DACL_SECURITY_INFORMATION | protection, descriptor),
             'windows_acl_fixture copy_dacl write DACL')


def dacl_sddl(path):
    """Read back the DACL, including its protection flags, as Windows SDDL."""
    api = _api()
    descriptor = _descriptor(api, path)
    text = wintypes.LPWSTR()
    _checked(api.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        descriptor, 1, _DACL_SECURITY_INFORMATION, ctypes.byref(text), None),
        'windows_acl_fixture read DACL as SDDL')
    try:
        return text.value
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
