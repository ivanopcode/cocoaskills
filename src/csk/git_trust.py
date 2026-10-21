"""Operator-selected HTTPS Git trust, checked before credential or Git work."""

from __future__ import annotations

import hashlib
import os
import re
import ssl
import stat
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
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
        try:
            path.lstat()
        except FileNotFoundError:
            # Bootstrap commands may invoke Git before any operator config exists.
            return None
        except OSError as exc:
            raise GitTrustError(
                INVALID, str(path), "cannot inspect Git trust configuration"
            ) from exc
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
    selected = _bundle_path(arguments)
    if selected is not None:
        _check_bundle(selected)
    return arguments


def _bundle_path(arguments: tuple[str, ...]) -> Path | None:
    if sys.platform == "win32" and arguments == ("-c", "http.sslBackend=schannel"):
        return None
    prefix = ("-c", "http.sslBackend=openssl") if sys.platform == "win32" else ()
    if (
        len(arguments) != len(prefix) + 2
        or arguments[: len(prefix)] != prefix
        or arguments[-2] != "-c"
        or not arguments[-1].startswith("http.sslCAInfo=")
    ):
        raise GitTrustError(
            INVALID, "<configuration>", "invalid frozen Git trust selection"
        )
    selected = Path(arguments[-1].removeprefix("http.sslCAInfo="))
    if not selected.is_absolute():
        raise GitTrustError(INVALID, str(selected), "expected an absolute regular file")
    return selected


def _check_bundle(path: Path) -> bytes:
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
        return content
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
            _require_exact_der(der)
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
    return content


def _require_exact_der(der: bytes) -> None:
    """The outer DER SEQUENCE must consume the entire PEM payload."""
    if len(der) < 2 or der[0] != 0x30:
        raise ValueError("expected an X.509 SEQUENCE")
    length = der[1]
    header = 2
    if length & 0x80:
        count = length & 0x7F
        if not count or count > 4 or len(der) < 2 + count or der[2] == 0:
            raise ValueError("invalid DER length")
        header += count
        length = int.from_bytes(der[2:header], "big")
        if length < 128:
            raise ValueError("nonminimal DER length")
    if header + length != len(der):
        raise ValueError("DER SEQUENCE does not consume the PEM payload")


@dataclass(frozen=True)
class PrivateGitTrust:
    ca_file: Path | None
    empty_directory: Path

    def environment(self, original: Mapping[str, str]) -> dict[str, str]:
        excluded = {
            "GIT_SSL_CAINFO",
            "GIT_SSL_CAPATH",
            "GIT_SSL_NO_VERIFY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        environment = {
            key: value for key, value in original.items() if key not in excluded
        }
        if self.ca_file is not None:
            environment["GIT_SSL_CAINFO"] = str(self.ca_file)
        environment["GIT_SSL_CAPATH"] = str(self.empty_directory)
        return environment


def materialize_git_trust(
    directory: Path, arguments: tuple[str, ...]
) -> PrivateGitTrust:
    """Write checked bytes exclusively inside an operation-owned private root."""
    selected = _bundle_path(arguments)
    content = None if selected is None else _check_bundle(selected)
    try:
        empty = directory / "empty-ca-directory"
        empty.mkdir(mode=0o700)
        target = None
        if content is not None:
            target = directory / "ca.pem"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(target, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        return PrivateGitTrust(target, empty)
    except OSError as exc:
        raise GitTrustError(
            INVALID, str(selected or directory), "cannot create private CA snapshot"
        ) from exc


@contextmanager
def private_git_trust(
    arguments: tuple[str, ...] | None = None,
) -> Iterator[PrivateGitTrust]:
    selected = https_git_args() if arguments is None else arguments
    try:
        directory = tempfile.TemporaryDirectory(prefix="csk-git-trust-")
    except OSError as exc:
        raise GitTrustError(
            INVALID, "<private Git state>", "cannot create private CA snapshot"
        ) from exc
    with directory as root:
        yield materialize_git_trust(Path(root), selected)


def pinned_command(
    command: Sequence[str],
    environment: Mapping[str, str],
    *,
    url: str | None = None,
) -> tuple[str, ...]:
    """Pin both the generic settings and the effective URL's most specific settings."""
    settings = {
        "sslVerify": "true",
        "followRedirects": "false",
        "schannelUseSSLCAInfo": "false",
    }
    if ca := environment.get("GIT_SSL_CAINFO"):
        settings["sslCAInfo"] = ca
    if sys.platform == "win32":
        settings["sslBackend"] = "openssl" if ca else "schannel"
    options: list[str] = []
    for prefix in ("http.", *((f"http.{url}.",) if url is not None else ())):
        for key, value in settings.items():
            options.extend(("-c", f"{prefix}{key}={value}"))
    # Remove earlier manager selections: Git must see only the snapshot path.
    retained = []
    index = 1
    while index < len(command):
        if (
            command[index] == "-c"
            and index + 1 < len(command)
            and command[index + 1].split("=", 1)[0].lower()
            in {
                "http.sslcainfo",
                "http.sslbackend",
                "http.sslverify",
                "http.followredirects",
                "http.schannelusesslcainfo",
            }
        ):
            index += 2
            continue
        retained.append(command[index])
        index += 1
    return (command[0], *options, *retained)


# Successful validation only: changing a file's bytes or the distrust policy
# always requires another parse. The cache retains no certificate bytes.
_VALIDATED: set[tuple[str, bytes, tuple[tuple[str, str], ...]]] = set()


def _checked(path: str, digest: bytes, policy: tuple[tuple[str, str], ...]) -> bool:
    return (path, digest, policy) in _VALIDATED


def _remember(path: str, digest: bytes, policy: tuple[tuple[str, str], ...]) -> None:
    _VALIDATED.add((path, digest, policy))
