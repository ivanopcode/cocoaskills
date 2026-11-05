from pathlib import Path

import pytest

from csk import manifest, protocol_json as pj


# Both decoders admit the boundary and reject its neighbor and the reported
# 1,200-level witness before the standard decoder can recurse.
@pytest.mark.parametrize("decode", [pj.loads, pj.loads_canonical, pj.loads_compatible])
def test_depth_admission(decode, monkeypatch):
    valid = '[' * pj.MAX_DEPTH + '0' + ']' * pj.MAX_DEPTH
    assert decode(valid) is not None
    assert decode('{"brackets":"[}\\\"["}') == {"brackets": '[}"['}
    def forbidden(*args, **kwargs):
        pytest.fail("over-depth input reached decoder")
    monkeypatch.setattr(pj.json, 'loads', forbidden)
    for depth in (pj.MAX_DEPTH + 1, 1200):
        with pytest.raises(pj.ProtocolJSONError, match='maximum depth'):
            decode('[' * depth + '0' + ']' * depth)


# A sparse oversized document must refuse without opening or reading content;
# the same reader admits a valid file before the planted forbidden-open guard.
@pytest.mark.parametrize("size", [pj.MAX_INPUT_BYTES + 1, 4 * 1024**3])
def test_sparse_read_refuses_before_open(tmp_path, monkeypatch, size):
    path = tmp_path / 'Skillfile.json'
    path.write_bytes(b'{}')
    assert pj.loads(pj.read_bytes(path)) == {}
    with path.open('wb') as stream:
        stream.truncate(size)
    def forbidden(*args, **kwargs):
        pytest.fail('oversized input was opened')
    monkeypatch.setattr(Path, 'open', forbidden)
    with pytest.raises(pj.ProtocolJSONSizeError, match='maximum size'):
        pj.read_bytes(path)
    with pytest.raises(manifest.ManifestError, match='maximum size'):
        manifest.load_manifest(tmp_path)


# Byte and Unicode-string inputs enforce a byte budget, with an exact valid
# boundary; canonical serialization and its existing strict refusals survive.
@pytest.mark.parametrize('decode', [pj.loads, pj.loads_canonical])
def test_size_and_existing_refusals(decode, monkeypatch):
    monkeypatch.setattr(pj, 'MAX_INPUT_BYTES', 8)
    assert decode(b'"123456"') == '123456'
    assert decode('"ééé"') == 'ééé'
    for raw in (b'"1234567"', '"éééé"'):
        with pytest.raises(pj.ProtocolJSONSizeError, match='maximum size'):
            decode(raw)


# Existing canonical bytes, duplicate-key and number refusals remain intact.
def test_canonical_controls():
    raw = b'{"a":[1,true,null],"z":"text"}'
    assert pj.canonical_bytes(pj.loads_canonical(raw)) == raw
    for raw, diagnostic in [(b'{"a":1,"a":2}', 'duplicate'),
                            (b'{"a":-0}', 'shortest-form'),
                            (b'{"a":1.5}', 'integers')]:
        with pytest.raises(pj.ProtocolJSONError, match=diagnostic):
            pj.loads_canonical(raw)


# The descriptor check catches replacement after path admission, before read.
def test_descriptor_recheck(tmp_path, monkeypatch):
    path = tmp_path / 'record.json'
    path.write_bytes(b'{}')
    real_open = Path.open
    def grow_then_open(self, *args, **kwargs):
        with real_open(self, 'wb') as stream:
            stream.truncate(pj.MAX_INPUT_BYTES + 1)
        return real_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', grow_then_open)
    with pytest.raises(pj.ProtocolJSONSizeError, match='maximum size'):
        pj.read_bytes(path)


# The copied standalone dispatcher enforces the same depth boundary.
def test_standalone_depth():
    from csk import _dispatch_runtime as runtime
    runtime._check_json_depth('[' * pj.MAX_DEPTH + '0' + ']' * pj.MAX_DEPTH)
    with pytest.raises(ValueError, match='maximum depth'):
        runtime._check_json_depth('[' * 1200 + '0' + ']' * 1200)


# A multi-GB marker must produce the existing typed non-current verdict before
# opening content; its valid JSON control passes the same file admission gate.
def test_sparse_marker_status(tmp_path, monkeypatch):
    from csk import install_marker
    path = tmp_path / '.csk-install.json'
    path.write_bytes(b'{}')
    assert pj.loads(pj.read_bytes(path)) == {}
    with path.open('wb') as stream:
        stream.truncate(4 * 1024**3)
    def forbidden(*args, **kwargs):
        pytest.fail('oversized marker was opened')
    monkeypatch.setattr(Path, 'open', forbidden)
    verdict = install_marker.evaluate_marker_status(path, None)
    assert not verdict.current
    assert 'maximum size' in verdict.detail


# Deep valid JSON must be translated at the real manifest entry point rather
# than leaking RecursionError; an ordinary empty manifest remains valid.
def test_deep_manifest_typed_refusal(tmp_path):
    path = tmp_path / 'Skillfile.json'
    path.write_text('{"schema_version":1,"skills":[]}')
    assert manifest.load_manifest(tmp_path) is not None
    path.write_text('[' * 1200 + '0' + ']' * 1200)
    with pytest.raises(manifest.ManifestError, match='maximum depth'):
        manifest.load_manifest(tmp_path)
