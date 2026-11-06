from __future__ import annotations

import errno
import io
import json
import os
import stat
from pathlib import Path
from typing import Any, BinaryIO, Final, cast


class ProtocolJSONError(ValueError):
    pass


# Admission limits apply to operator-controlled documents, before decoding.
MAX_INPUT_BYTES: Final = 16 * 1024 * 1024
MAX_DEPTH: Final = 256


class ProtocolJSONSizeError(ProtocolJSONError, OSError):
    """A document exceeds the read budget; also fits existing I/O refusals."""


def _open_regular(path: str, flags: int) -> int:
    if os.name != "nt":
        return os.open(path, flags | os.O_NONBLOCK)
    # Open asynchronously first, reject pipes/devices, then reopen the same
    # disk object synchronously for the CRT. No path lookup races the check.
    import ctypes
    import msvcrt
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    reopen = kernel.ReOpenFile
    reopen.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
    reopen.restype = ctypes.c_void_p
    kind = kernel.GetFileType
    kind.argtypes = [ctypes.c_void_p]
    kind.restype = ctypes.c_uint32
    close = kernel.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    handle = create(path, 0x80000000, 7, None, 3, 0x40000000, None)
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
    try:
        if kind(handle) != 1:
            raise ProtocolJSONSizeError("JSON input must be a regular file")
        synchronous = reopen(handle, 0x80000000, 7, 0)
        if synchronous == invalid:
            raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
        try:
            return int(getattr(msvcrt, "open_osfhandle")(synchronous, os.O_RDONLY | getattr(os, "O_BINARY")))
        except BaseException:
            close(synchronous)
            raise
    finally:
        close(handle)


def _read_bounded(stream: BinaryIO, maximum: int) -> bytes:
    # A budget-sized read allocates that budget even for a tiny file on
    # Windows. Grow with the actual content, retaining one byte past the cap
    # so callers can distinguish an oversized file from a valid prefix.
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = stream.read(min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_bytes(path: Path, *, maximum: int = MAX_INPUT_BYTES) -> bytes:
    """Admit regular files without blocking, checking the opened descriptor."""
    info = path.stat()
    if stat.S_ISDIR(info.st_mode):
        code = errno.EACCES if os.name == "nt" else errno.EISDIR
        raise OSError(code, os.strerror(code), str(path))
    if not stat.S_ISREG(info.st_mode):
        raise ProtocolJSONSizeError("JSON input must be a regular file")
    if info.st_size > maximum:
        raise ProtocolJSONSizeError("JSON input exceeds maximum size")
    virtual_read = getattr(path, "_read_bounded_json", None)
    if virtual_read is not None:
        return cast(bytes, virtual_read(maximum))
    with io.open(path, "rb", opener=_open_regular) as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ProtocolJSONSizeError("JSON input must be a regular file")
        if opened.st_size > maximum:
            raise ProtocolJSONSizeError("JSON input exceeds maximum size")
        raw = _read_bounded(stream, maximum)
    if len(raw) > maximum:
        raise ProtocolJSONSizeError("JSON input exceeds maximum size")
    return raw


def check_depth(text: str) -> None:
    # Scan tokens without recursion or constructing a parsed object. Escaped
    # quotes and brackets inside strings do not affect container depth.
    depth = 0
    quoted = escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ProtocolJSONError("JSON input exceeds maximum depth")
        elif character in "]}":
            depth -= 1


# CCJ-1 keeps integers inside the range every conforming JSON implementation
# represents exactly, so a signed or hashed object survives a round trip
# through a language that stores numbers as IEEE 754 doubles.
MAX_SAFE_INTEGER: Final = 9_007_199_254_740_991
MIN_SAFE_INTEGER: Final = -MAX_SAFE_INTEGER


def loads_compatible(raw: bytes | str) -> Any:
    """Bound legacy JSON without changing duplicate-key or number semantics."""
    if len(raw) > MAX_INPUT_BYTES:
        raise ProtocolJSONSizeError("JSON input exceeds maximum size")
    try:
        text = raw.decode(json.detect_encoding(raw), "surrogatepass") if isinstance(raw, bytes) else raw
        if isinstance(raw, str) and len(raw.encode("utf-8", "surrogatepass")) > MAX_INPUT_BYTES:
            raise ProtocolJSONSizeError("JSON input exceeds maximum size")
        check_depth(text)
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ProtocolJSONError(str(exc)) from exc


def loads(raw: bytes | str) -> Any:
    """Decode portable protocol JSON with exact key and Unicode validation."""
    text = _decode_utf8(
        raw,
        invalid_message="protocol JSON must be valid UTF-8",
        bom_message="protocol JSON must not contain a byte-order mark",
    )

    def reject_constant(text: str) -> None:
        raise ProtocolJSONError(f"protocol JSON does not allow non-finite number {text!r}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=reject_constant,
        )
    except ProtocolJSONError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ProtocolJSONError(str(exc)) from exc
    _validate_unicode(value)
    return value


def loads_canonical(raw: bytes | str) -> Any:
    """Decode CCJ-1 text, rejecting every ambiguity CCJ-1 forbids.

    Unlike :func:`loads`, this rejects non-integer numbers, ``-0``, and
    integers outside the safe range instead of preserving them, so the decoded
    value is guaranteed to be representable by :func:`canonical_bytes`.
    """

    text = _decode_utf8(
        raw,
        invalid_message="CCJ-1 bytes must be valid UTF-8",
        bom_message="CCJ-1 bytes must not contain a byte-order mark",
    )

    def parse_integer(text: str) -> int:
        parsed = int(text)
        if text == "-0" or not MIN_SAFE_INTEGER <= parsed <= MAX_SAFE_INTEGER:
            raise ProtocolJSONError(f"CCJ-1 integer is not shortest-form or safe: {text}")
        return parsed

    def reject_number(text: str) -> None:
        raise ProtocolJSONError(f"CCJ-1 numbers must be integers: {text!r}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_int=parse_integer,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except ProtocolJSONError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ProtocolJSONError(str(exc)) from exc
    validate_canonical(value)
    return value


def canonical_bytes(value: Any) -> bytes:
    """Return Curator Canonical JSON 1 bytes for one complete protocol value.

    Object keys are sorted by Unicode scalar value, no insignificant
    whitespace is emitted, and there is no byte-order mark or terminal line
    feed. Signature-envelope callers are responsible for removing the
    top-level signature member before calling this value-level primitive.
    """

    validate_canonical(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def is_canonical(raw: bytes) -> bool:
    """Return whether ``raw`` is already the exact CCJ-1 encoding of its value.

    A caller that must accept only canonical stored bytes compares the stored
    bytes with the recanonicalized value rather than trusting the writer.
    """

    try:
        return canonical_bytes(loads_canonical(raw)) == raw
    except ProtocolJSONError:
        return False


def validate_canonical(value: Any) -> None:
    """Raise when a decoded value cannot be represented in CCJ-1."""
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not MIN_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise ProtocolJSONError(f"CCJ-1 integer outside safe range: {value}")
        return
    if isinstance(value, float):
        raise ProtocolJSONError("CCJ-1 numbers must be integers")
    if isinstance(value, str):
        if _has_lone_surrogate(value):
            raise ProtocolJSONError("CCJ-1 strings must not contain lone surrogates")
        return
    if isinstance(value, list):
        for item in value:
            validate_canonical(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolJSONError("CCJ-1 object keys must be strings")
            validate_canonical(key)
            validate_canonical(item)
        return
    raise ProtocolJSONError(f"CCJ-1 does not support {type(value).__name__}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolJSONError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _decode_utf8(
    raw: bytes | str,
    *,
    invalid_message: str,
    bom_message: str,
) -> str:
    if len(raw) > MAX_INPUT_BYTES:
        raise ProtocolJSONSizeError("JSON input exceeds maximum size")
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProtocolJSONError(f"{invalid_message}: {exc}") from exc
    else:
        text = raw
    if text.startswith("\ufeff"):
        raise ProtocolJSONError(bom_message)
    if not isinstance(raw, bytes) and len(text.encode("utf-8", errors="surrogatepass")) > MAX_INPUT_BYTES:
        raise ProtocolJSONSizeError("JSON input exceeds maximum size")
    check_depth(text)
    return text


def _has_lone_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _validate_unicode(value: Any) -> None:
    if isinstance(value, str):
        if _has_lone_surrogate(value):
            raise ProtocolJSONError("protocol JSON contains a lone Unicode surrogate")
        return
    if isinstance(value, list):
        for item in value:
            _validate_unicode(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_unicode(key)
            _validate_unicode(item)
