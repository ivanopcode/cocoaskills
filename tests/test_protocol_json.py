from __future__ import annotations

import pytest

from csk import audit_registry, protocol_json


NON_UTF8_ENCODINGS = (
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "utf-32",
    "utf-32-le",
    "utf-32-be",
)


@pytest.mark.parametrize(
    "payload",
    [
        b'\xef\xbb\xbf{"a":1}',
        b'{"a":1,"a":2}',
        b'{"a":1} trailing',
        b'{"s":"\\ud800"}',
        b'{"n":NaN}',
        b'\xff',
    ],
)
def test_protocol_json_rejects_ambiguous_or_invalid_input(payload: bytes) -> None:
    with pytest.raises(protocol_json.ProtocolJSONError):
        protocol_json.loads(payload)


def test_protocol_json_preserves_schema_one_extension_numbers() -> None:
    value = protocol_json.loads(b'{"extension":{"fraction":1.5,"integer":9007199254740992}}')
    assert value == {"extension": {"fraction": 1.5, "integer": 9_007_199_254_740_992}}


@pytest.mark.parametrize("encoding", NON_UTF8_ENCODINGS)
def test_protocol_readers_reject_non_utf8_byte_encodings(encoding: str) -> None:
    payload = '{"a":1}'.encode(encoding)

    for reader in (protocol_json.loads, protocol_json.loads_canonical):
        with pytest.raises(protocol_json.ProtocolJSONError):
            reader(payload)

    with pytest.raises(audit_registry.RegistryError, match="registry returned invalid JSON"):
        audit_registry.load_protocol_json(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"n":-0}',
        b'{"n":1.0}',
        b'{"n":9007199254740992}',
        b'{"n":-9007199254740992}',
    ],
)
def test_ccj_reader_rejects_non_integer_or_unsafe_integer_forms(payload: bytes) -> None:
    with pytest.raises(protocol_json.ProtocolJSONError):
        protocol_json.loads_canonical(payload)


def test_ccj_value_canonicalization_does_not_strip_signature_members() -> None:
    value = {"z": 1, "sig": {"algorithm": "example"}}
    assert protocol_json.canonical_bytes(value) == (
        b'{"sig":{"algorithm":"example"},"z":1}'
    )
