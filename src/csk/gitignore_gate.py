from __future__ import annotations

import subprocess
from pathlib import Path


class GitignoreError(Exception):
    pass


def missing_entries(project_root: Path, entries: list[str]) -> list[str]:
    missing: list[str] = []
    for entry in entries:
        probe = entry.rstrip("/") + "/.csk-probe"
        proc = subprocess.run(
            ["git", "-C", str(project_root), "check-ignore", "-q", probe],
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            missing.append(entry)
    return missing


def ensure_ignored(project_root: Path, entries: list[str], *, fix: bool = False) -> None:
    missing = missing_entries(project_root, entries)
    if not missing:
        return
    if fix:
        append_entries(project_root / ".gitignore", missing)
        missing = missing_entries(project_root, entries)
        if not missing:
            return
    raise GitignoreError(
        "Generated CocoaSkill paths are not ignored by git. Missing entries: "
        + ", ".join(missing)
    )


def append_entries(path: Path, entries: list[str]) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = set(line.strip() for line in existing.splitlines())
    to_add = [entry for entry in entries if entry not in lines]
    if not to_add:
        return
    prefix = "" if existing.endswith("\n") or not existing else "\n"
    block = prefix + "# CocoaSkill\n" + "\n".join(to_add) + "\n"
    path.write_text(existing + block, encoding="utf-8")

