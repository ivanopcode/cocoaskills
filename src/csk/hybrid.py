from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import manifest


# Hybrid-scope skills are stored once per machine under the csk home and
# activated for selected projects only. The declaration lives in a
# machine-level manifest owned by whoever operates the machine (a platform
# team through configuration management, or the developer), so nothing about
# a hybrid skill is committed to the target project repository.

HYBRID_DIR = "hybrid"
HYBRID_MANIFEST = "Skillfile.json"


class HybridError(Exception):
    pass


@dataclass(frozen=True)
class HybridDecl:
    decl: manifest.SkillDecl
    targets: tuple[str, ...]


def hybrid_root(csk_home: Path) -> Path:
    return csk_home / HYBRID_DIR


def hybrid_manifest_path(csk_home: Path) -> Path:
    return hybrid_root(csk_home) / HYBRID_MANIFEST


def hybrid_skills_root(csk_home: Path) -> Path:
    return hybrid_root(csk_home) / "skills"


def load_hybrid_decls(csk_home: Path) -> list[HybridDecl]:
    path = hybrid_manifest_path(csk_home)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HybridError(f"Malformed JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise HybridError(f"{path} must contain a JSON object")
    parsed = manifest.parse_manifest(data, path)
    targets_by_name = _targets_by_name(data, path)
    decls: list[HybridDecl] = []
    for decl in parsed.skills:
        decls.append(HybridDecl(decl=decl, targets=targets_by_name.get(decl.name, ())))
    return decls


def applies_to_project(hybrid_decl: HybridDecl, *, aliases: tuple[str, ...], project_path: Path) -> bool:
    """A target matches by project alias, exact path, or glob over the path."""
    resolved = str(project_path.resolve()).replace("\\", "/")
    for target in hybrid_decl.targets:
        if target in aliases:
            return True
        candidate = target.replace("\\", "/")
        if candidate == resolved:
            return True
        if fnmatch.fnmatchcase(resolved, candidate):
            return True
    return False


def add_hybrid_decl(
    csk_home: Path,
    *,
    name: str,
    ref_kind: str,
    ref: str,
    git: str | None,
    targets: list[str],
) -> Path:
    path = hybrid_manifest_path(csk_home)
    data = _read_or_init(path)
    skills = data.setdefault("skills", [])
    if not isinstance(skills, list):
        raise HybridError("Hybrid Skillfile field 'skills' must be a list")
    entry: dict[str, Any] = {"name": name, ref_kind: ref, "targets": targets}
    if git:
        entry["git"] = git
    replaced = False
    for index, existing in enumerate(skills):
        if isinstance(existing, dict) and existing.get("name") == name:
            skills[index] = entry
            replaced = True
            break
    if not replaced:
        skills.append(entry)
    _validate_and_write(path, data)
    return path


def remove_hybrid_decl(csk_home: Path, name: str) -> Path:
    path = hybrid_manifest_path(csk_home)
    data = _read_or_init(path)
    skills = data.get("skills")
    if not isinstance(skills, list):
        raise HybridError("Hybrid Skillfile field 'skills' must be a list")
    kept = [entry for entry in skills if not (isinstance(entry, dict) and entry.get("name") == name)]
    if len(kept) == len(skills):
        raise HybridError(f"Skill not declared in hybrid Skillfile: {name}")
    data["skills"] = kept
    _validate_and_write(path, data)
    return path


def _targets_by_name(data: dict[str, Any], path: Path) -> dict[str, tuple[str, ...]]:
    targets: dict[str, tuple[str, ...]] = {}
    raw_skills = data.get("skills")
    if not isinstance(raw_skills, list):
        return targets
    for index, raw in enumerate(raw_skills):
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        raw_targets = raw.get("targets")
        if raw_targets is None:
            raise HybridError(
                f"{path}: hybrid skill at index {index} requires a non-empty 'targets' list "
                "(project alias, absolute path, or path glob)"
            )
        if (
            not isinstance(raw_targets, list)
            or not raw_targets
            or not all(isinstance(item, str) and item for item in raw_targets)
        ):
            raise HybridError(f"{path}: hybrid skill at index {index} field 'targets' must be a non-empty list of strings")
        if isinstance(name, str):
            targets[name] = tuple(raw_targets)
    return targets


def _read_or_init(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": manifest.SCHEMA_VERSION, "skills": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HybridError(f"Malformed JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise HybridError(f"{path} must contain a JSON object")
    return data


def _validate_and_write(path: Path, data: dict[str, Any]) -> None:
    manifest.parse_manifest(data, path)
    _targets_by_name(data, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
