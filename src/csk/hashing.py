from __future__ import annotations

import hashlib
import os
import sys
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from .identifiers import is_valid_portable_path


class HashingError(Exception):
    pass


BUILD_SOURCE_ALGORITHM: Final = "curator-build-source-v1"
_BUILD_SOURCE_DOMAIN: Final = BUILD_SOURCE_ALGORITHM.encode("ascii") + b"\0"
_UINT64_MAX: Final = (1 << 64) - 1


def content_sha256(root: Path, *, exclude: set[str] | None = None) -> str:
    exclude = exclude or {".csk-install.json"}
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise HashingError(f"symbolic links are not supported in protocol trees: {path}")
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in exclude:
            continue
        if not is_valid_portable_path(rel):
            raise HashingError(f"non-portable path in protocol tree: {rel}")
        files.append(path)
    _reject_platform_collisions(root, files)
    payload = bytearray()
    for index, path in enumerate(sorted(files, key=lambda item: item.relative_to(root).as_posix())):
        if index:
            payload.extend(b"\0")
        rel_bytes = path.relative_to(root).as_posix().encode("utf-8")
        payload.extend(rel_bytes)
        payload.extend(b"\0")
        payload.extend(path.read_bytes())
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class _BuildSourceHasher:
    """Incrementally frame already ordered regular files for build identity."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256(_BUILD_SOURCE_DOMAIN)
        self._last_path: bytes | None = None

    def add_file(self, path: str, content_length: int, chunks: Iterable[bytes]) -> None:
        path_bytes = _build_source_path_bytes(path)
        if self._last_path is not None and path_bytes <= self._last_path:
            raise HashingError("build-source paths must be unique and supplied in unsigned UTF-8 byte order")
        if content_length < 0 or content_length > _UINT64_MAX:
            raise HashingError(f"build-source file length is outside uint64 range: {content_length}")

        self._digest.update(b"F")
        self._digest.update(len(path_bytes).to_bytes(8, byteorder="big", signed=False))
        self._digest.update(path_bytes)
        self._digest.update(content_length.to_bytes(8, byteorder="big", signed=False))

        written = 0
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise HashingError("build-source content chunks must be bytes")
            written += len(chunk)
            if written > content_length:
                raise HashingError(f"build-source file grew while hashing: {path}")
            self._digest.update(chunk)
        if written != content_length:
            raise HashingError(
                f"build-source file length changed while hashing: {path} "
                f"(expected {content_length}, read {written})"
            )
        self._last_path = path_bytes

    def content_sha256(self) -> str:
        return "sha256:" + self._digest.hexdigest()


def build_source_sha256(files: Iterable[tuple[str, bytes]]) -> str:
    """Hash in-memory regular-file records with curator-build-source-v1."""

    prepared: list[tuple[bytes, str, bytes]] = []
    encoded_paths: set[bytes] = set()
    platform_paths: dict[str, str] = {}
    for path, content in files:
        path_bytes = _build_source_path_bytes(path)
        if path_bytes in encoded_paths:
            raise HashingError(f"duplicate build-source path: {path!r}")
        encoded_paths.add(path_bytes)
        platform_key = _build_source_platform_key(path)
        previous = platform_paths.get(platform_key)
        if previous is not None and previous != path:
            raise HashingError(f"build-source paths collide on a supported platform: {previous!r} and {path!r}")
        platform_paths[platform_key] = path
        if not isinstance(content, bytes):
            raise HashingError(f"build-source content must be bytes: {path!r}")
        prepared.append((path_bytes, path, content))

    hasher = _BuildSourceHasher()
    for _, path, content in sorted(prepared, key=lambda item: item[0]):
        hasher.add_file(path, len(content), (content,))
    return hasher.content_sha256()


def _build_source_path_bytes(path: str) -> bytes:
    if not isinstance(path, str) or not is_valid_portable_path(path):
        raise HashingError(f"non-portable path in build-source snapshot: {path!r}")
    try:
        encoded = path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise HashingError(f"invalid Unicode in build-source path: {path!r}") from exc
    if len(encoded) > _UINT64_MAX:
        raise HashingError(f"build-source path length is outside uint64 range: {path!r}")
    return encoded


def _build_source_platform_key(path: str) -> str:
    decomposed = unicodedata.normalize("NFD", path)
    return unicodedata.normalize("NFD", decomposed.casefold())


def _reject_platform_collisions(root: Path, files: list[Path]) -> None:
    seen: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        key = os.path.normcase(relative)
        if sys.platform in {"darwin", "win32"}:
            key = unicodedata.normalize("NFD", key).casefold()
        previous = seen.get(key)
        if previous is not None and previous != relative:
            raise HashingError(f"protocol paths collide on this platform: {previous!r} and {relative!r}")
        seen[key] = relative
