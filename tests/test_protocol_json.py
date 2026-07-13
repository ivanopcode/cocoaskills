from __future__ import annotations

import pytest

from csk import protocol_json


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
