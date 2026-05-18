from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MANIFEST_NAME = "Skillfile.json"


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class SkillRef:
    kind: str
    value: str


@dataclass(frozen=True)
class SkillDecl:
    name: str
    source: str
    ref: SkillRef


@dataclass(frozen=True)
class ProjectManifest:
    path: Path
    project_alias: str | None = None
    agents: list[str] = field(default_factory=list)
    locale: str | None = None
    skills: list[SkillDecl] = field(default_factory=list)


def manifest_path(project_root: Path) -> Path:
    return project_root / MANIFEST_NAME


def ensure_empty_manifest(project_root: Path) -> Path:
    if not project_root.exists() or not project_root.is_dir():
        raise ManifestError(f"project path does not exist: {project_root}")
    path = manifest_path(project_root)
    if not path.exists():
        path.write_text(
            json.dumps({"schema_version": SCHEMA_VERSION, "agents": [], "skills": []}, indent=2)
            + "\n",
            encoding="utf-8",
        )
    return path


def ensure_project_manifest(project_root: Path, *, alias: str, agents: list[str]) -> Path:
    if not project_root.exists() or not project_root.is_dir():
        raise ManifestError(f"project path does not exist: {project_root}")
    path = manifest_path(project_root)
    if not path.exists():
        path.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "project": {"alias": alias},
                    "agents": agents,
                    "skills": [],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return path


def load_manifest(project_root: Path) -> ProjectManifest | None:
    path = manifest_path(project_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"Malformed JSON in {path}: {exc}") from exc
    return parse_manifest(data, path)


def parse_manifest(data: dict[str, Any], path: Path) -> ProjectManifest:
    if not isinstance(data, dict):
        raise ManifestError(f"{path} must contain a JSON object")
    schema = data.get("schema_version")
    if schema != SCHEMA_VERSION:
        raise ManifestError(
            f"Unsupported Skillfile schema_version {schema!r}; this Skillfile requires a newer csk"
        )

    project_alias = _parse_project_alias(data, path)

    agents = data.get("agents", [])
    if not _is_str_list(agents):
        raise ManifestError("Skillfile field 'agents' must be a list of strings")

    locale = data.get("locale")
    if locale is not None and not isinstance(locale, str):
        raise ManifestError("Skillfile field 'locale' must be a string when present")

    raw_skills = data.get("skills")
    if not isinstance(raw_skills, list):
        raise ManifestError("Skillfile requires field 'skills' as a list")

    skills: list[SkillDecl] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_skills):
        if not isinstance(raw, dict):
            raise ManifestError(f"Skill declaration at index {index} must be an object")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ManifestError(f"Skill declaration at index {index} requires non-empty string 'name'")
        if name in seen_names:
            raise ManifestError(f"Duplicate skill name in Skillfile: {name}")
        seen_names.add(name)

        source = raw.get("source", name)
        if not isinstance(source, str) or not source:
            raise ManifestError(f"Skill {name!r} field 'source' must be a non-empty string")

        ref_keys = [key for key in ("tag", "branch", "revision") if key in raw]
        if len(ref_keys) != 1:
            raise ManifestError(f"Skill {name!r} must specify exactly one of tag, branch, or revision")
        ref_kind = ref_keys[0]
        ref_value = raw[ref_kind]
        if not isinstance(ref_value, str) or not ref_value:
            raise ManifestError(f"Skill {name!r} field '{ref_kind}' must be a non-empty string")

        skills.append(SkillDecl(name=name, source=source, ref=SkillRef(ref_kind, ref_value)))

    return ProjectManifest(
        path=path,
        project_alias=project_alias,
        agents=list(agents),
        locale=locale,
        skills=skills,
    )


def _parse_project_alias(data: dict[str, Any], path: Path) -> str | None:
    project = data.get("project")
    if project is None:
        return None
    if not isinstance(project, dict):
        raise ManifestError("Skillfile field 'project' must be an object when present")
    alias = project.get("alias")
    if alias is None:
        return None
    if not isinstance(alias, str) or not alias:
        raise ManifestError(f"{path} field 'project.alias' must be a non-empty string when present")
    return alias


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
