"""Operator-selected HTTPS Git trust, checked before credential or Git work."""

from __future__ import annotations

import hashlib
import os
import re
import ssl
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import GlobalConfig

INVALID = "git_trust_anchor_invalid"
DISTRUSTED = "git_trust_anchor_distrusted"
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MACOS_BUNDLES = (
    "/opt/homebrew/etc/ca-certificates/cert.pem",
    "/usr/local/etc/ca-certificates/cert.pem",
    "/etc/ssl/cert.pem",
)
LINUX_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
)
DISTRUSTED_CERTIFICATES = {
    # emSign ECC Root CA - G3: emergency Mozilla removal, 2026-10-20,
    # following compromised eMudhra intermediates. Root identity/removal:
    # https://bugzilla.mozilla.org/show_bug.cgi?id=2084101
    # DER SHA-256 published by the disclosing researchers:
    # https://emudhra.we-have-a-problem.com/roots.json
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b": "emSign ECC Root CA - G3",
    # AAA Certificate Services: previously removed by Mozilla; cross-signs
    # compromised intermediates in the same incident (checked 2026-10-21).
    # https://bugzilla.mozilla.org/show_bug.cgi?id=2083449#c25
    # https://emudhra.we-have-a-problem.com/roots.json
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4": "AAA Certificate Services",
    # emSign Root TLS CA - G3: direct issuer of compromised intermediates;
    # never included by Mozilla (checked 2026-10-21), but may be in overrides.
    # https://emudhra.we-have-a-problem.com/roots.json
    "7dd78d5f4f13459a83dff9abbba62edbaf6f2d102bf257fd712f4d9f2746ed8d": "emSign Root TLS CA - G3",
}
_CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s+([A-Za-z0-9+/=\s]+?)-----END CERTIFICATE-----"
)


class GitTrustError(RuntimeError):
    def __init__(self, code: str, path: str, detail: str) -> None:
        self.code = code
        # repr escapes control characters in operator paths and keeps the
        # diagnostic independent of untrusted certificate text.
        self.detail = (
            f"CA bundle {path!r}: {detail}\n"
            "remediation: brew install ca-certificates (macOS), update the "
            "distribution ca-certificates package, or set CSK_GIT_CA_BUNDLE "
            "to an updated absolute PEM bundle path"
        )
        super().__init__(f"{code}: {self.detail}")


def _configured_bundle(config: GlobalConfig | None) -> str | None:
    if "CSK_GIT_CA_BUNDLE" in os.environ:
        return os.environ["CSK_GIT_CA_BUNDLE"]
    if config is None:
        from . import config as config_module

        path = config_module.config_path()
        if path.exists() or "CSK_CONFIG" in os.environ:
            try:
                config = config_module.load_config(path, quiet=True)
            except (OSError, ValueError, config_module.ConfigError) as exc:
                raise GitTrustError(
                    INVALID, str(path), "cannot load Git trust configuration"
                ) from exc
    return None if config is None else config.git.https_ca_bundle


def https_git_args(config: GlobalConfig | None = None) -> tuple[str, ...]:
    """Choose and check a bundle; return only manager-owned Git options."""
    override = _configured_bundle(config)
    if override is None and sys.platform == "win32":
        return ("-c", "http.sslBackend=schannel")
    if override is not None:
        path = Path(override)
        if not override or not path.is_absolute():
            raise GitTrustError(INVALID, override, "expected an absolute regular file")
    else:
        candidates = MACOS_BUNDLES if sys.platform == "darwin" else LINUX_BUNDLES
        path = next(
            (Path(item) for item in candidates if Path(item).exists()),
            Path(candidates[-1]),
        )
    _check_bundle(path)
    backend = ("-c", "http.sslBackend=openssl") if sys.platform == "win32" else ()
    return (*backend, "-c", f"http.sslCAInfo={path}")


def check_git_args(arguments: tuple[str, ...]) -> tuple[str, ...]:
    """Recheck frozen selection before a later operation can expose a token."""
    for argument in arguments:
        if argument.startswith("http.sslCAInfo="):
            _check_bundle(Path(argument.removeprefix("http.sslCAInfo=")))
    return arguments


def _check_bundle(path: Path) -> None:
    try:
        # Nonblocking open prevents a substituted FIFO from hanging before
        # fstat. Bound the actual read as well as the advertised file size.
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0),
        )
        with os.fdopen(descriptor, "rb") as bundle:
            info = os.fstat(bundle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BUNDLE_BYTES:
                raise GitTrustError(
                    INVALID, str(path), "expected a bounded regular PEM file"
                )
            content = bundle.read(MAX_BUNDLE_BYTES + 1)
        if len(content) > MAX_BUNDLE_BYTES:
            raise GitTrustError(INVALID, str(path), "PEM bundle exceeds size limit")
    except (OSError, ValueError) as exc:
        raise GitTrustError(
            INVALID, str(path), "cannot read a regular PEM file"
        ) from exc
    digest = hashlib.sha256(content).digest()
    policy = tuple(sorted(DISTRUSTED_CERTIFICATES.items()))
    if _checked(str(path), digest, policy):
        return
    matches = list(_CERTIFICATE.finditer(content))
    remainder = _CERTIFICATE.sub(b"", content)
    # Bundles commonly include comments and human-readable CA labels. Never
    # silently ignore a broken certificate block alongside a valid one.
    if not matches or b"-----BEGIN" in remainder or b"-----END" in remainder:
        raise GitTrustError(INVALID, str(path), "cannot parse PEM certificates")
    try:
        for match in matches:
            pem = match.group().decode("ascii")
            der = ssl.PEM_cert_to_DER_cert(pem)
            # Base64 decoding alone does not establish an X.509 certificate.
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=pem)
            name = DISTRUSTED_CERTIFICATES.get(hashlib.sha256(der).hexdigest())
            if name is not None:
                raise GitTrustError(DISTRUSTED, str(path), f"distrusted CA: {name}")
    except (ValueError, ssl.SSLError) as exc:
        raise GitTrustError(
            INVALID, str(path), "cannot parse X.509 certificates"
        ) from exc
    _remember(str(path), digest, policy)


# Successful validation only: changing a file's bytes or the distrust policy
# always requires another parse. The cache retains no certificate bytes.
_VALIDATED: set[tuple[str, bytes, tuple[tuple[str, str], ...]]] = set()


def _checked(path: str, digest: bytes, policy: tuple[tuple[str, str], ...]) -> bool:
    return (path, digest, policy) in _VALIDATED


def _remember(path: str, digest: bytes, policy: tuple[tuple[str, str], ...]) -> None:
    _VALIDATED.add((path, digest, policy))
