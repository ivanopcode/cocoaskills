"""Pure-Python Ed25519 signature verification.

The CocoaSkills runtime depends on the standard library only, so it cannot use
a compiled crypto package to verify audit registry signatures. This module
vendors a verify-only Ed25519 implementation derived from the reference code
in RFC 8032 (public domain). Signing lives in the registry service, which is a
separate project and free to use a compiled library.

Only ``verify`` is exported. The implementation favors clarity over speed; it
runs a handful of times per install, not in a hot path.
"""
from __future__ import annotations

import hashlib

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)
# Base point.
_BY = (4 * pow(5, _P - 2, _P)) % _P
_BX = 0  # recovered below


def _sha512(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BX = _xrecover(_BY)
_B = (_BX % _P, _BY % _P, 1, (_BX * _BY) % _P)


def _edwards_add(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (t1 * 2 * _D * t2) % _P
    dd = (z1 * 2 * z2) % _P
    e = b - a
    f = dd - c
    g = dd + c
    h = b + a
    return ((e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P)


def _scalarmult(p: tuple[int, int, int, int], e: int) -> tuple[int, int, int, int]:
    result = (0, 1, 1, 0)
    while e > 0:
        if e & 1:
            result = _edwards_add(result, p)
        p = _edwards_add(p, p)
        e >>= 1
    return result


def _point_equal(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> bool:
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    if (x1 * z2 - x2 * z1) % _P != 0:
        return False
    if (y1 * z2 - y2 * z1) % _P != 0:
        return False
    return True


def _decode_point(s: bytes) -> tuple[int, int, int, int] | None:
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    if y >= _P:
        return None
    x = _xrecover(y)
    if x & 1 != (s[31] >> 7) & 1:
        x = _P - x
    point = (x, y, 1, (x * y) % _P)
    if not _on_curve(point):
        return None
    return point


def _on_curve(point: tuple[int, int, int, int]) -> bool:
    x, y, z, t = point
    if (z % _P) == 0:
        return False
    if (x * y % _P) != (z * t % _P):
        return False
    zz = (z * z) % _P
    xx = (x * x) % _P
    yy = (y * y) % _P
    lhs = (-xx + yy - zz) % _P
    rhs = (_D * xx * yy * pow(zz, _P - 2, _P)) % _P
    return (lhs - rhs) % _P == 0


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Return True when signature is a valid Ed25519 signature of message.

    public_key is 32 bytes, signature is 64 bytes. Any malformed input returns
    False rather than raising.
    """
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a = _decode_point(public_key)
    if a is None:
        return False
    rs = signature[:32]
    r = _decode_point(rs)
    if r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = _sha512(rs + public_key + message) % _L
    sb = _scalarmult(_B, s)
    ha = _scalarmult(a, h)
    rha = _edwards_add(r, ha)
    return _point_equal(sb, rha)
