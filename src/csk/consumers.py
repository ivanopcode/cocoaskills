from __future__ import annotations

import json
from pathlib import Path


# Runtime GC must not delete runtime referenced by checkouts that are not
# registered in the global config: 'csk install .' deliberately does not
# register the project (v0.3 design). Every successful project install is
# therefore recorded here, and GC treats registry entries as additional
# runtime consumers. Entries whose checkout disappeared or holds no install
# markers are pruned during GC.
SCHEMA_VERSION = 1
REGISTRY_NAME = "consumers.json"


def registry_path(csk_home: Path) -> Path:
    return csk_home / REGISTRY_NAME


def load_consumers(csk_home: Path) -> list[Path]:
    path = registry_path(csk_home)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    raw = data.get("consumers") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [Path(item) for item in raw if isinstance(item, str) and item]


def record_consumer(csk_home: Path, project_root: Path) -> None:
    resolved = str(Path(project_root).resolve())
    existing = {str(path) for path in load_consumers(csk_home)}
    if resolved in existing:
        return
    existing.add(resolved)
    _write(csk_home, sorted(existing))


def replace_consumers(csk_home: Path, consumers: list[Path]) -> None:
    _write(csk_home, sorted({str(path) for path in consumers}))


def _write(csk_home: Path, consumers: list[str]) -> None:
    csk_home.mkdir(parents=True, exist_ok=True)
    registry_path(csk_home).write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "consumers": consumers}, indent=2) + "\n",
        encoding="utf-8",
    )
