from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import git_ops, hashing, manifest
from .config import GlobalConfig, ProjectConfig


@dataclass(frozen=True)
class SkillStatus:
    name: str
    ref_kind: str
    ref: str
    installed_commit: str | None
    resolved_commit: str | None
    label: str


def render_status(config: GlobalConfig, *, alias: str | None = None) -> str:
    projects = _selected_projects(config, alias)
    blocks: list[str] = []
    for project in projects:
        blocks.append(_render_project(config, project))
    return "\n".join(blocks)


def _selected_projects(config: GlobalConfig, alias: str | None) -> list[ProjectConfig]:
    if alias is None:
        return list(config.projects.values())
    project = config.projects.get(alias)
    if project is None:
        raise ValueError(f"Unknown project alias: {alias}")
    return [project]


def _render_project(config: GlobalConfig, project: ProjectConfig) -> str:
    lines = [f"Project {project.alias} ({project.path})"]
    project_manifest = manifest.load_manifest(project.path)
    if project_manifest is None:
        lines.append("  Skillfile.json missing")
        return "\n".join(lines)
    if not project_manifest.skills:
        lines.append("  no skills declared")
        return "\n".join(lines)
    for decl in project_manifest.skills:
        status = _skill_status(config, project.path, decl)
        commit = (status.installed_commit or "")[:7]
        suffix = ""
        if status.label == "update-available" and status.resolved_commit:
            suffix = f" -> {status.resolved_commit[:7]}"
        lines.append(
            f"  {status.name:<20} {status.ref_kind:<8} {status.ref:<12} {commit:<7}  {status.label}{suffix}"
        )
    return "\n".join(lines)


def _skill_status(config: GlobalConfig, project_root: Path, decl: manifest.SkillDecl) -> SkillStatus:
    resolved_commit: str | None = None
    try:
        resolved = git_ops.resolve_ref(config.skills_root / decl.source, decl.ref.kind, decl.ref.value)
        resolved_commit = resolved.commit
    except Exception:
        return SkillStatus(decl.name, decl.ref.kind, decl.ref.value, None, None, "error")

    marker_path = project_root / ".agents" / "skills" / decl.name / ".csk-install.json"
    if not marker_path.exists():
        return SkillStatus(decl.name, decl.ref.kind, decl.ref.value, None, resolved_commit, "missing")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception:
        return SkillStatus(decl.name, decl.ref.kind, decl.ref.value, None, resolved_commit, "error")
    installed_commit = marker.get("commit") if isinstance(marker.get("commit"), str) else None
    if installed_commit != resolved_commit:
        return SkillStatus(decl.name, decl.ref.kind, decl.ref.value, installed_commit, resolved_commit, "update-available")
    installed_dir = marker_path.parent
    try:
        actual_hash = hashing.content_sha256(installed_dir)
    except Exception:
        return SkillStatus(decl.name, decl.ref.kind, decl.ref.value, installed_commit, resolved_commit, "error")
    if marker.get("content_sha256") != actual_hash:
        return SkillStatus(decl.name, decl.ref.kind, decl.ref.value, installed_commit, resolved_commit, "content-drift")
    return SkillStatus(decl.name, decl.ref.kind, decl.ref.value, installed_commit, resolved_commit, "up-to-date")
