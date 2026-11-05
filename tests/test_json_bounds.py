import io
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
    # Plant a forbidden decoder call to prove the observation guard is active.
    with pytest.raises(pytest.fail.Exception, match='reached decoder'):
        pj.json.loads('{}')
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
    monkeypatch.setattr(io, 'open', forbidden)
    # Plant an open attempt; the guard must detect it without reading bytes.
    with pytest.raises(pytest.fail.Exception, match='was opened'):
        io.open(path, 'rb')
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
    real_open = io.open
    def grow_then_open(self, *args, **kwargs):
        with real_open(self, 'wb') as stream:
            stream.truncate(pj.MAX_INPUT_BYTES + 1)
        return real_open(self, *args, **kwargs)
    monkeypatch.setattr(io, 'open', grow_then_open)
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
    monkeypatch.setattr(io, 'open', forbidden)
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


# Child timeouts detect a blocking open, including replacement after stat.
@pytest.mark.skipif(__import__('os').name == 'nt', reason='POSIX special nodes')
@pytest.mark.parametrize('kind', ['fifo', 'symlink', 'device', 'replace'])
def test_special_nodes_refuse_without_blocking(tmp_path, kind):
    import os
    import subprocess
    import sys
    target = tmp_path / 'input'
    regular = tmp_path / 'regular'
    regular.write_bytes(b'{}')
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo)
    if kind == 'fifo': target = fifo
    elif kind == 'symlink': target.symlink_to(fifo)
    elif kind == 'device': target.symlink_to('/dev/zero')
    else: target.write_bytes(b'{}')
    script = '''
import io, os, sys
from pathlib import Path
from csk import protocol_json as p, locking, _dispatch_runtime as runtime
regular, target, fifo = map(Path, sys.argv[1:4])
assert p.read_bytes(regular) == b'{}'
assert runtime._read_no_follow(str(regular), manager_home=str(regular.parent), what='test') == b'{}'
if sys.argv[4] == 'replace':
    original = io.open
    def replace(path, *args, **kwargs):
        if Path(path) == target:
            target.unlink()
            target.symlink_to(fifo)
        return original(path, *args, **kwargs)
    io.open = replace
try:
    p.read_bytes(target)
except p.ProtocolJSONError as exc:
    assert 'regular file' in str(exc)
else:
    raise AssertionError('special node admitted')
assert locking._read_lock(target) is None
try:
    runtime._read_no_follow(str(target), manager_home=str(target.parent), what='test')
except runtime.DispatchError as exc:
    assert exc.kind == 'registry_unreadable'
else:
    raise AssertionError('runtime admitted special node')
'''
    env = dict(os.environ, PYTHONPATH=str(Path(pj.__file__).parent.parent))
    result = subprocess.run([sys.executable, '-c', script, str(regular), str(target), str(fifo), kind],
                            env=env, capture_output=True, timeout=8)
    assert result.returncode == 0, result.stderr.decode()


# Compatible byte parsing preserves all stdlib encodings, BOMs, and duplicate
# keys while still bounding depth before constructing a value.
@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'utf-16', 'utf-16-le', 'utf-16-be', 'utf-32', 'utf-32-le', 'utf-32-be'])
def test_compatible_encoding(encoding):
    import json
    raw = '{"x":1,"x":2}'.encode(encoding)
    assert pj.loads_compatible(raw) == json.loads(raw)
    with pytest.raises(pj.ProtocolJSONError, match='maximum depth'):
        pj.loads_compatible(('[' * 1200 + '0' + ']' * 1200).encode(encoding))


# The path-based timeout-message reader keeps its existing 64 KiB budget.
def test_lock_path_size_bound(tmp_path, monkeypatch):
    from csk import locking
    path = tmp_path / 'lock'
    path.write_bytes(b'{"pid":1}')
    assert locking._read_lock(path) == {'pid': 1}
    with path.open('wb') as stream:
        stream.truncate(4 * 1024**3)
    def forbidden(*args, **kwargs):
        pytest.fail('oversized lock was opened')
    monkeypatch.setattr(io, 'open', forbidden)
    assert locking._read_lock(path) is None


# Manager-written broker state must fail closed for deep or sparse input,
# while the same prompt still admits an ordinary matching state.
def test_broker_state_bounds(tmp_path, monkeypatch):
    from csk import https_broker
    path = tmp_path / 'state'
    prompt = "Username for 'https://example.com':"
    path.write_bytes(b'{"host":"example.com","username":"example"}')
    assert https_broker.answer(str(path), prompt, {}) == 'example'
    path.write_bytes(b'[' * 1200 + b'0' + b']' * 1200)
    assert https_broker.answer(str(path), prompt, {}) is None
    with path.open('wb') as stream:
        stream.truncate(4 * 1024**3)
    def forbidden(*args, **kwargs):
        pytest.fail('oversized broker state was opened')
    monkeypatch.setattr(io, 'open', forbidden)
    assert https_broker.answer(str(path), prompt, {}) is None
