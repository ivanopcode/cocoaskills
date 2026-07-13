from __future__ import annotations

import hashlib
import os
import sys
import unicodedata
from pathlib import Path

from .identifiers import is_valid_portable_path


class HashingError(Exception):
    pass


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
