from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any, Final


class ProtocolJSONError(ValueError):
    pass


# Admission limits apply to operator-controlled documents, before decoding.
MAX_INPUT_BYTES: Final = 16 * 1024 * 1024
MAX_DEPTH: Final = 256


class ProtocolJSONSizeError(ProtocolJSONError, OSError):
    """A document exceeds the read budget; also fits existing I/O refusals."""


def read_bytes(path: Path) -> bytes:
    """Admit size before opening, then cap the read if the file grows."""
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ProtocolJSONSizeError("JSON input exceeds maximum size")
    with path.open("rb") as stream:
        try:
            size = os.fstat(stream.fileno()).st_size
        except io.UnsupportedOperation:
            # Virtual snapshot paths expose in-memory streams, not descriptors.
            size = path.stat().st_size
        if size > MAX_INPUT_BYTES:
            raise ProtocolJSONSizeError("JSON input exceeds maximum size")
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
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
    text = _decode_utf8(raw, invalid_message="JSON must be UTF-8", bom_message="JSON must not contain a byte-order mark")
    try:
        return json.loads(text)
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
