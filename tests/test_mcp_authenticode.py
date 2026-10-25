"""Bounded PE signature-presence and Windows trust result controls."""
from __future__ import annotations

import struct

import pytest

from csk import mcp_authenticode as trust


def pe(signed=False):
    raw = bytearray(512)
    raw[:2] = b'MZ'
    struct.pack_into('<I', raw, 60, 64)
    raw[64:68] = b'PE\0\0'
    struct.pack_into('<H', raw, 84, 224)
    struct.pack_into('<H', raw, 88, 0x10b)
    struct.pack_into('<I', raw, 88 + 92, 16)
    if signed: struct.pack_into('<II', raw, 88 + 96 + 32, 400, 32)
    return raw


# Unsigned PE is distinguishable from a present or malformed certificate table;
# malformed objects cannot use the allow-unverified unsigned override.
@pytest.mark.parametrize('mutation', ['unsigned', 'signed', 'not-pe', 'truncated', 'outside', 'one-zero', 'unaligned'])
def test_embedded_signature_presence(tmp_path, mutation):
    path = tmp_path / 'app.exe'
    raw = pe(mutation != 'unsigned')
    if mutation == 'not-pe': raw[:2] = b'xx'
    if mutation == 'truncated': raw = raw[:100]
    if mutation in {'outside', 'one-zero', 'unaligned'}:
        struct.pack_into('<II', raw, 88 + 96 + 32, {'outside': 1000, 'one-zero': 0, 'unaligned': 401}[mutation], 32)
    path.write_bytes(raw)
    if mutation in {'unsigned', 'signed'}:
        assert trust._has_certificate(path) is (mutation == 'signed')
    else:
        with pytest.raises(ValueError): trust._has_certificate(path)
        assert trust.verify_windows(path).signature == 'invalid'


# Production wrapper refuses API/extraction failure, with no unsigned fallback;
# only an explicit successful verification result can produce valid identity.
def test_api_failure_is_not_unsigned(tmp_path, monkeypatch):
    path = tmp_path / 'app.exe'
    path.write_bytes(pe())
    def failed(*args): raise OSError('API unavailable')
    monkeypatch.setattr(trust, '_verify', failed)
    assert trust.verify_windows(path).signature == 'invalid'
    monkeypatch.setattr(trust, '_verify', lambda *a: trust.WindowsVerification('valid', 'Example Inc'))
    assert trust.verify_windows(path) == trust.WindowsVerification('valid', 'Example Inc')


# The real ctypes binding accepts only LONG zero, retrieves the verified primary
# signer, loads system DLLs only, disables UI/network retrieval and always CLOSEs
# state; NOSIGNATURE is unsigned only when the PE has no certificate table.
@pytest.mark.parametrize('result,signed,subject,expected', [
    (0, True, 'Example Inc', 'valid'), (0, True, '', 'invalid'),
    (0, True, None, 'invalid'), (0, True, 'Example Inc\0suffix', 'invalid'),
    (1, False, 'Example Inc', 'invalid'),
    (0x800b0100, False, '', 'unsigned'), (0x800b0100, True, '', 'invalid'),
    (0x80096010, True, '', 'invalid'), (0x800b0003, False, '', 'invalid')])
def test_wintrust_binding_contract(tmp_path, monkeypatch, result, signed, subject, expected):
    import ctypes
    from types import SimpleNamespace
    keep = []
    actions = []
    class Function:
        def __init__(self, callback): self.callback = callback
        def __call__(self, *args): return self.callback(*args)
    def check(hwnd, action, data):
        value = data._obj
        actions.append(value.action)
        assert hwnd.value == ctypes.c_void_p(-1).value
        assert action._obj.a == 0x00aac56b and action._obj.b == 0xcd44
        assert value.ui == 2 and value.choice == 1 and value.revocation == 0
        assert value.flags == 0x3010
        value.state = 15
        return result
    primary = None
    def signer(state, index, counter, counter_index):
        assert (state, index, counter, counter_index) == (16, 0, 0, 0)
        cls = primary.restype._type_
        signer_value = cls()
        signer_value.size, signer_value.count = ctypes.sizeof(cls), 1
        cert_cls = dict(cls._fields_)['chain']._type_
        certificate = cert_cls()
        certificate.certificate = 9
        signer_value.chain = ctypes.pointer(certificate)
        keep.extend([signer_value, certificate])
        return ctypes.pointer(signer_value)
    primary = Function(signer)
    api = SimpleNamespace(WinVerifyTrust=Function(check),
        WTHelperProvDataFromStateData=Function(lambda state: 16 if state == 15 else None),
        WTHelperGetProvSignerFromChain=primary)
    calls = []
    def dll(name, **kw):
        calls.append((name, kw))
        return api
    monkeypatch.setattr(ctypes, 'WinDLL', dll, raising=False)
    def cn(crypt, certificate):
        assert certificate == 9
        if subject is None: raise OSError('subject extraction failed')
        return subject
    monkeypatch.setattr(trust, '_subject_cn', cn)
    path = tmp_path / 'app.exe'
    path.write_bytes(pe(signed))
    observation = trust.verify_windows(path)
    assert observation.signature == expected
    if expected == 'valid': assert observation.publisher == 'Example Inc'
    assert actions == [1, 2]
    assert calls == [('wintrust.dll', {'winmode': 0x800}), ('crypt32.dll', {'winmode': 0x800})]


# Subject extraction passes only the leaf's actual Subject DER to the decoder,
# requests CN specifically, preserves its exact text (including an embedded NUL
# so the caller can refuse it) and refuses absent/failed CN.
# Fake system functions check this binding contract, not native trust validity.
@pytest.mark.parametrize('failure', [None, 'embedded-nul', 'no-subject', 'decode', 'no-cn', 'value'])
def test_subject_cn_has_no_display_or_san_fallback(monkeypatch, failure):
    import ctypes as c
    from types import SimpleNamespace
    class Blob(c.Structure):
        _fields_ = [('size', c.c_uint32), ('data', c.c_void_p)]
    class Algorithm(c.Structure):
        _fields_ = [('oid', c.c_char_p), ('parameters', Blob)]
    class Time(c.Structure):
        _fields_ = [('low', c.c_uint32), ('high', c.c_uint32)]
    class Info(c.Structure):
        _fields_ = [('version', c.c_uint32), ('serial', Blob), ('algorithm', Algorithm),
                    ('issuer', Blob), ('before', Time), ('after', Time), ('subject', Blob)]
    class Context(c.Structure):
        _fields_ = [('encoding', c.c_uint32), ('encoded', c.c_void_p), ('size', c.c_uint32),
                    ('info', c.POINTER(Info)), ('store', c.c_void_p)]
    class Function:
        def __init__(self, callback): self.callback = callback
        def __call__(self, *args): return self.callback(*args)
    subject = c.create_string_buffer(b'actual subject DER')
    whole = c.create_string_buffer(b'whole cert including SAN')
    info = Info()
    info.subject = Blob(0 if failure == 'no-subject' else len(subject.raw), c.addressof(subject))
    cert = Context(1, c.addressof(whole), len(whole.raw), c.pointer(info), None)
    calls, keep = [], []
    def decode(encoding, kind, source, length, flags, allocator, output, size):
        calls.append('decode')
        assert encoding == 1 and kind.value == 7
        assert c.string_at(source, length) == subject.raw
        assert flags == 0 and allocator is None
        if failure == 'decode': return 0
        size._obj.value = 64
        return 1
    def find(oid, name):
        calls.append('cn')
        assert oid == b'2.5.4.3'
        if failure == 'no-cn': return None
        attribute = api.CertFindRDNAttr.restype._type_()
        attribute.kind = 12
        keep.append(attribute)
        return c.pointer(attribute)
    expected = 'Example Inc\0suffix' if failure == 'embedded-nul' else 'Example Inc, Exact Case'
    def value(kind, blob, output, length):
        calls.append('value')
        assert kind == 12
        if failure == 'value': return 0
        if output is not None:
            assert length == len(expected) + 1
            output.value = expected
        return len(expected) + 1
    api = SimpleNamespace(CryptDecodeObjectEx=Function(decode),
        CertFindRDNAttr=Function(find), CertRDNValueToStrW=Function(value))
    assert trust._subject_cn(api, c.addressof(cert)) == (expected if failure in {None, 'embedded-nul'} else '')
    assert calls == ([] if failure == 'no-subject' else ['decode'] if failure == 'decode'
                     else ['decode', 'decode', 'cn'] + ([] if failure == 'no-cn'
                          else ['value'] if failure == 'value' else ['value', 'value']))
