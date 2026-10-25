"""Read-only embedded Authenticode verification through Windows system DLLs."""
from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class WindowsVerification:
    signature: Literal["valid", "unsigned", "invalid"]
    publisher: str = ""


def _has_certificate(path: Path) -> bool:
    """Distinguish an unsigned PE from a broken/unsupported signed object.

    NOSIGNATURE alone is insufficient: malformed certificate tables must never
    receive the explicit unsigned override. WinVerifyTrust validates the actual
    signature; this bounded header inspection only establishes its presence.
    """
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("not a PE file")
        offset = struct.unpack_from("<I", header, 60)[0]
        if not 64 <= offset <= 16 * 1024 * 1024:
            raise ValueError("invalid PE offset")
        stream.seek(offset)
        coff = stream.read(24)
        if len(coff) != 24 or coff[:4] != b"PE\0\0":
            raise ValueError("invalid PE header")
        size = struct.unpack_from("<H", coff, 20)[0]
        if not 96 <= size <= 4096:
            raise ValueError("invalid optional header")
        optional = stream.read(size)
        if len(optional) != size:
            raise ValueError("truncated optional header")
        magic = struct.unpack_from("<H", optional)[0]
        directory = {0x10b: 96, 0x20b: 112}.get(magic)
        if directory is None or len(optional) < directory:
            raise ValueError("invalid PE magic")
        count = struct.unpack_from("<I", optional, directory - 4)[0]
        if count <= 4:
            return False
        if len(optional) < directory + 40:
            raise ValueError("missing certificate directory")
        position, length = struct.unpack_from("<II", optional, directory + 32)
        if position == length == 0:
            return False
        if not position or length < 8 or position % 8:
            raise ValueError("invalid certificate directory")
        stream.seek(0, 2)
        if position < offset + 24 + size or position + length > stream.tell():
            raise ValueError("certificate directory outside file")
        return True


def verify_windows(path: Path) -> WindowsVerification:
    """Use Authenticode's verified primary leaf CN, exact ordinal comparison.

    No UI or network retrieval; no additional revocation checks. This checks the
    OS trust/signature policy at this instant, not online revocation freshness.
    Always CLOSE provider state, including failed verification/extraction.
    """
    try:
        signed = _has_certificate(path)
        return _verify(path, signed)
    except (OSError, ValueError, AttributeError):
        return WindowsVerification("invalid")


def _verify(path: Path, signed: bool) -> WindowsVerification:
    # Explicit 32-bit Windows scalar types also keep layout tests portable.
    DWORD, LONG, PTR = ctypes.c_uint32, ctypes.c_int32, ctypes.c_void_p

    class GUID(ctypes.Structure):
        _fields_ = [("a", DWORD), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]

    class FileInfo(ctypes.Structure):
        _fields_ = [("size", DWORD), ("path", ctypes.c_wchar_p), ("handle", PTR), ("subject", PTR)]

    class TrustData(ctypes.Structure):
        _fields_ = [("size", DWORD), ("policy", PTR), ("sip", PTR), ("ui", DWORD),
                    ("revocation", DWORD), ("choice", DWORD), ("file", ctypes.POINTER(FileInfo)),
                    ("action", DWORD), ("state", PTR), ("url", ctypes.c_wchar_p),
                    ("flags", DWORD), ("context", DWORD), ("settings", PTR)]

    class FileTime(ctypes.Structure):
        _fields_ = [("low", DWORD), ("high", DWORD)]

    class CertificatePrefix(ctypes.Structure):
        _fields_ = [("size", DWORD), ("certificate", PTR)]

    class SignerPrefix(ctypes.Structure):
        _fields_ = [("size", DWORD), ("time", FileTime), ("count", DWORD),
                    ("chain", ctypes.POINTER(CertificatePrefix)), ("kind", DWORD),
                    ("signer", PTR), ("error", DWORD)]

    # LOAD_LIBRARY_SEARCH_SYSTEM32 prevents DLL lookup through cwd/PATH.
    trust = getattr(ctypes, "WinDLL")("wintrust.dll", winmode=0x800)
    crypt = getattr(ctypes, "WinDLL")("crypt32.dll", winmode=0x800)
    verify = trust.WinVerifyTrust
    verify.argtypes = [PTR, ctypes.POINTER(GUID), ctypes.POINTER(TrustData)]
    verify.restype = LONG
    provider = trust.WTHelperProvDataFromStateData
    provider.argtypes, provider.restype = [PTR], PTR
    signer = trust.WTHelperGetProvSignerFromChain
    signer.argtypes = [PTR, DWORD, ctypes.c_int32, DWORD]
    signer.restype = ctypes.POINTER(SignerPrefix)
    action = GUID(0x00aac56b, 0xcd44, 0x11d0, (ctypes.c_ubyte * 8)(0x8c, 0xc2, 0, 0xc0, 0x4f, 0xc2, 0x95, 0xee))
    info = FileInfo(ctypes.sizeof(FileInfo), str(path), None, None)
    data = TrustData()
    data.size, data.ui, data.choice = ctypes.sizeof(TrustData), 2, 1
    data.file, data.action = ctypes.pointer(info), 1  # FILE / VERIFY
    data.flags = 0x1000 | 0x10 | 0x2000  # cache only, no extra revocation, reject MD2/MD4
    try:
        result = verify(PTR(-1), ctypes.byref(action), ctypes.byref(data)) & 0xffffffff
        if result:
            return WindowsVerification("unsigned" if result == 0x800b0100 and not signed else "invalid")
        state = provider(data.state)
        if not state:
            return WindowsVerification("invalid")
        primary = signer(state, 0, 0, 0)  # primary signer, not timestamp countersigner
        if (not primary or primary.contents.size < ctypes.sizeof(SignerPrefix)
                or primary.contents.error or not primary.contents.count or not primary.contents.chain):
            return WindowsVerification("invalid")
        certificate = primary.contents.chain[0].certificate
        if not certificate:
            return WindowsVerification("invalid")
        subject = _subject_cn(crypt, certificate)
        if not subject or any(ord(c) < 32 or ord(c) == 127 for c in subject):
            return WindowsVerification("invalid")
        return WindowsVerification("valid", subject)
    finally:
        data.action = 2  # CLOSE on success, rejection and subject extraction failure
        verify(PTR(-1), ctypes.byref(action), ctypes.byref(data))


def _subject_cn(crypt: object, certificate: int) -> str:
    """Read the first actual subject CN; never a SAN/display-name fallback."""
    from typing import Any, cast
    api = cast(Any, crypt)
    DWORD, PTR = ctypes.c_uint32, ctypes.c_void_p

    class Blob(ctypes.Structure):
        _fields_ = [("size", DWORD), ("data", PTR)]

    class Algorithm(ctypes.Structure):
        _fields_ = [("oid", ctypes.c_char_p), ("parameters", Blob)]

    class FileTime(ctypes.Structure):
        _fields_ = [("low", DWORD), ("high", DWORD)]

    class InfoPrefix(ctypes.Structure):
        _fields_ = [("version", DWORD), ("serial", Blob), ("algorithm", Algorithm),
                    ("issuer", Blob), ("before", FileTime), ("after", FileTime), ("subject", Blob)]

    class Context(ctypes.Structure):
        _fields_ = [("encoding", DWORD), ("encoded", PTR), ("size", DWORD),
                    ("info", ctypes.POINTER(InfoPrefix)), ("store", PTR)]

    class Attribute(ctypes.Structure):
        _fields_ = [("oid", ctypes.c_char_p), ("kind", DWORD), ("value", Blob)]

    context = ctypes.cast(certificate, ctypes.POINTER(Context)).contents
    if not context.info:
        return ""
    subject = context.info.contents.subject
    if not subject.data or not 0 < subject.size <= 65536:
        return ""
    decode = api.CryptDecodeObjectEx
    decode.argtypes = [DWORD, PTR, PTR, DWORD, DWORD, PTR, PTR, ctypes.POINTER(DWORD)]
    decode.restype = ctypes.c_int32
    find = api.CertFindRDNAttr
    find.argtypes, find.restype = [ctypes.c_char_p, PTR], ctypes.POINTER(Attribute)
    value = api.CertRDNValueToStrW
    value.argtypes, value.restype = [DWORD, ctypes.POINTER(Blob), ctypes.c_wchar_p, DWORD], DWORD
    size = DWORD()
    if not decode(1, PTR(7), subject.data, subject.size, 0, None, None, ctypes.byref(size)):
        return ""
    if not 0 < size.value <= 1024 * 1024:
        return ""
    decoded = ctypes.create_string_buffer(size.value)
    if not decode(1, PTR(7), subject.data, subject.size, 0, None, decoded, ctypes.byref(size)):
        return ""
    attribute = find(b"2.5.4.3", decoded)
    if not attribute:
        return ""
    length = value(attribute.contents.kind, ctypes.byref(attribute.contents.value), None, 0)
    if not 1 < length <= 4096:
        return ""
    result = ctypes.create_unicode_buffer(length)
    if value(attribute.contents.kind, ctypes.byref(attribute.contents.value), result, length) != length:
        return ""
    return result.value
