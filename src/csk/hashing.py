from __future__ import annotations

import hashlib
from pathlib import Path


def content_sha256(root: Path, *, exclude: set[str] | None = None) -> str:
    exclude = exclude or {".csk-install.json"}
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in exclude:
            continue
        files.append(path)
    payload = bytearray()
    for index, path in enumerate(sorted(files, key=lambda item: item.relative_to(root).as_posix())):
        if index:
            payload.extend(b"\0")
        rel_bytes = path.relative_to(root).as_posix().encode("utf-8")
        payload.extend(rel_bytes)
        payload.extend(b"\0")
        payload.extend(path.read_bytes())
    return "sha256:" + hashlib.sha256(payload).hexdigest()

