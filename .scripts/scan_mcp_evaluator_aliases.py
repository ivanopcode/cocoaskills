"""Read-only alias inventory; run from the repository with PYTHONPATH=src.

The output is a dated, reviewable fixture, not an install-time dependency.
Only the four system executable directories are scanned; nothing is executed.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import platform
import re
import shlex
import stat
from pathlib import Path

from csk.mcp_declarations import _COMMAND_KINDS, _command_kind, _normalize_command


ROOTS = (Path("/usr/bin"), Path("/bin"), Path("/usr/sbin"), Path("/sbin"))
REFUSED = {"shell", "indirection", "launcher", "evaluator"}


def scan(roots: tuple[Path, ...] = ROOTS) -> dict:
    entries = sorted((p for root in roots for p in root.iterdir()), key=str)
    identities: dict[tuple[int, int], list[str]] = {}
    for path in entries:
        try:
            info = path.stat()
            if stat.S_ISREG(info.st_mode) and _command_kind(_normalize_command(path.name)) in REFUSED:
                identities.setdefault((info.st_dev, info.st_ino), []).append(str(path))
        except OSError:
            continue
    found = []
    errors = []
    for path in entries:
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
                continue
            evidence = []
            if path.is_symlink():
                target = path.resolve(strict=True)
                if (_command_kind(_normalize_command(target.name)) in REFUSED
                        or (info.st_dev, info.st_ino) in identities):
                    evidence.append({"mechanism": "symlink", "target": str(target)})
            for peer in identities.get((info.st_dev, info.st_ino), []):
                if peer != str(path) and not path.is_symlink():
                    evidence.append({"mechanism": "hardlink", "target": peer})
            # Bounded script inspection; do not decode binary executables.
            try:
                with path.open("rb") as stream:
                    head = stream.read(65536)
            except OSError as exc:
                errors.append({"path": str(path), "error": type(exc).__name__})
                head = b""
            if head.startswith(b"#!"):
                text = head.decode("utf-8", errors="replace")
                line = text.splitlines()[0][2:].strip()
                words = shlex.split(line)
                if words and _command_kind(_normalize_command(words[0])) in REFUSED:
                    evidence.append({"mechanism": "shebang", "target": words[0]})
                for match in re.finditer(r"(?m)^\s*exec\s+([^\n;]+)", text):
                    try:
                        words = shlex.split(match[1])
                    except ValueError:
                        continue
                    if words and _command_kind(_normalize_command(words[0])) in REFUSED:
                        evidence.append({"mechanism": "literal-exec", "target": words[0]})
            if evidence:
                found.append({"name": path.name, "path": str(path), "evidence": evidence})
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append({"path": str(path), "error": type(exc).__name__})
    return {
        "schema_version": 1,
        "source": ".scripts/scan_mcp_evaluator_aliases.py",
        "date": datetime.date.today().isoformat(),
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "roots": [str(p) for p in roots],
        "seed_sha256": hashlib.sha256(json.dumps(_COMMAND_KINDS, sort_keys=True).encode()).hexdigest(),
        "limits": "Read-only executable entries; inode identity, resolved symlink basename, bounded shebang/literal exec. No execution or semantic evaluation of scripts.",
        "entries": found,
        "errors": errors,
    }


if __name__ == "__main__":
    print(json.dumps(scan(), indent=2, sort_keys=True))
