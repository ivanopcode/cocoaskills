from __future__ import annotations

import json
from typing import Any, Final


class ProtocolJSONError(ValueError):
    pass


# CCJ-1 keeps integers inside the range every conforming JSON implementation
# represents exactly, so a signed or hashed object survives a round trip
# through a language that stores numbers as IEEE 754 doubles.
MAX_SAFE_INTEGER: Final = 9_007_199_254_740_991
MIN_SAFE_INTEGER: Final = -MAX_SAFE_INTEGER


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
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProtocolJSONError(f"{invalid_message}: {exc}") from exc
    else:
        text = raw
    if text.startswith("\ufeff"):
        raise ProtocolJSONError(bom_message)
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
