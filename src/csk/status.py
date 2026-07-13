from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import dev_substitutions, git_ops, hashing, manifest, protocol_json
from .config import GlobalConfig, ProjectConfig


@dataclass(frozen=True)
class SkillStatus:
    name: str
    ref_kind: str
    ref: str
    installed_commit: str | None
    resolved_commit: str | None
    label: str
    detail: str | None = None


@dataclass(frozen=True)
class ProjectStatus:
    alias: str
    path: Path
    skillfile_present: bool
    skills: list[SkillStatus]
    substitutions: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return self.skillfile_present and all(skill.label == "up-to-date" for skill in self.skills)


def collect_status(config: GlobalConfig, *, alias: str | None = None) -> list[ProjectStatus]:
    statuses: list[ProjectStatus] = []
    for project in _selected_projects(config, alias):
        substitutions = _substitution_lines(project.path)
        project_manifest = manifest.load_manifest(project.path)
        if project_manifest is None:
            statuses.append(ProjectStatus(project.alias, project.path, False, [], substitutions))
            continue
        skills = [_skill_status(config, project.path, decl) for decl in project_manifest.skills]
        statuses.append(ProjectStatus(project.alias, project.path, True, skills, substitutions))
    return statuses


def _substitution_lines(project_root: Path) -> tuple[str, ...]:
    try:
        substitutions = dev_substitutions.load_substitutions(project_root)
    except dev_substitutions.DevSubstitutionError as exc:
        return (f"error: {exc}",)
    return tuple(
        f"{substitution.name} -> {substitution.describe()}" for substitution in substitutions.values()
    )


def statuses_to_payload(statuses: list[ProjectStatus]) -> list[dict[str, Any]]:
    payload = []
    for project in statuses:
        payload.append(
            {
                "alias": project.alias,
                "path": str(project.path),
                "skillfile_present": project.skillfile_present,
                "clean": project.clean,
                "substitutions": list(project.substitutions),
                "skills": [
                    {
                        "name": skill.name,
                        "ref_kind": skill.ref_kind,
                        "ref": skill.ref,
                        "installed_commit": skill.installed_commit,
                        "resolved_commit": skill.resolved_commit,
                        "label": skill.label,
                        "detail": skill.detail,
                    }
                    for skill in project.skills
                ],
            }
        )
    return payload


def render_status(config: GlobalConfig, *, alias: str | None = None) -> str:
    return render_collected(collect_status(config, alias=alias))


def render_collected(statuses: list[ProjectStatus]) -> str:
    blocks: list[str] = []
    for project in statuses:
        blocks.append(_render_project_status(project))
    return "\n".join(blocks)


def _render_project_status(project: ProjectStatus) -> str:
    lines = [f"Project {project.alias} ({project.path})"]
    for substitution in project.substitutions:
        lines.append(f"  SUBSTITUTION {substitution}")
    if not project.skillfile_present:
        lines.append("  Skillfile.json missing")
        return "\n".join(lines)
    if not project.skills:
        lines.append("  no skills declared")
        return "\n".join(lines)
    for status in project.skills:
        commit = (status.installed_commit or "")[:7]
        suffix = ""
        if status.label == "update-available" and status.resolved_commit:
            suffix = f" -> {status.resolved_commit[:7]}"
        if status.label == "error" and status.detail:
            suffix = f" ({status.detail})"
        lines.append(
            f"  {status.name:<20} {status.ref_kind:<8} {status.ref:<12} {commit:<7}  {status.label}{suffix}"
        )
    return "\n".join(lines)


def _selected_projects(config: GlobalConfig, alias: str | None) -> list[ProjectConfig]:
    if alias is None:
        return list(config.projects.values())
    project = config.projects.get(alias)
    if project is None:
        raise ValueError(f"Unknown project alias: {alias}")
    return [project]



def _skill_status(config: GlobalConfig, project_root: Path, decl: manifest.SkillDecl) -> SkillStatus:
    resolved_commit: str | None = None
    try:
        resolved = git_ops.resolve_ref(config.skills_root / decl.source, decl.ref.kind, decl.ref.value)
        resolved_commit = resolved.commit
    except Exception as exc:
        return SkillStatus(decl.name, decl.ref.kind, decl.ref.value, None, None, "error", detail=str(exc))

    marker_path = project_root / ".agents" / "skills" / decl.name / ".csk-install.json"
    if not marker_path.exists():
        return SkillStatus(decl.name, decl.ref.kind, decl.ref.value, None, resolved_commit, "missing")
    try:
        marker = protocol_json.loads(marker_path.read_bytes())
    except Exception as exc:
        return SkillStatus(
            decl.name, decl.ref.kind, decl.ref.value, None, resolved_commit, "error",
            detail=f"unreadable install marker {marker_path}: {exc}",
        )
    installed_commit = marker.get("commit") if isinstance(marker.get("commit"), str) else None
    if installed_commit != resolved_commit:
        return SkillStatus(decl.name, decl.ref.kind, decl.ref.value, installed_commit, resolved_commit, "update-available")
    installed_dir = marker_path.parent
    try:
        actual_hash = hashing.content_sha256(installed_dir)
    except Exception as exc:
        return SkillStatus(
            decl.name, decl.ref.kind, decl.ref.value, installed_commit, resolved_commit, "error",
            detail=f"could not hash {installed_dir}: {exc}",
        )
    if marker.get("content_sha256") != actual_hash:
        return SkillStatus(decl.name, decl.ref.kind, decl.ref.value, installed_commit, resolved_commit, "content-drift")
    return SkillStatus(decl.name, decl.ref.kind, decl.ref.value, installed_commit, resolved_commit, "up-to-date")
