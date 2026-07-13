from __future__ import annotations

import json
from typing import Any


class ProtocolJSONError(ValueError):
    pass


def loads(raw: bytes | str) -> Any:
    """Decode portable protocol JSON with exact key and Unicode validation."""
    if (isinstance(raw, bytes) and raw.startswith(b"\xef\xbb\xbf")) or (
        isinstance(raw, str) and raw.startswith("\ufeff")
    ):
        raise ProtocolJSONError("protocol JSON must not contain a byte-order mark")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolJSONError(f"duplicate JSON object key: {key!r}")
            result[key] = value
        return result

    def reject_constant(text: str) -> None:
        raise ProtocolJSONError(f"protocol JSON does not allow non-finite number {text!r}")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except ProtocolJSONError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolJSONError(str(exc)) from exc
    _validate_unicode(value)
    return value


def _validate_unicode(value: Any) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
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
