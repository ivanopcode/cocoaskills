from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import protocol_json
from .identifiers import IDENTIFIER_RULE, is_valid_identifier, is_valid_locale, is_valid_portable_path


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
    git: str | None = None


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


def add_skill_decl(
    project_root: Path,
    *,
    name: str,
    ref_kind: str,
    ref: str,
    git: str | None = None,
    source: str | None = None,
) -> Path:
    path = manifest_path(project_root)
    data = _read_payload(path)
    skills = data.setdefault("skills", [])
    if not isinstance(skills, list):
        raise ManifestError("Skillfile field 'skills' must be a list")
    decl: dict[str, str] = {"name": name, ref_kind: ref}
    if git:
        decl["git"] = git
    if source:
        decl["source"] = source
    replaced = False
    for index, existing in enumerate(skills):
        if isinstance(existing, dict) and existing.get("name") == name:
            skills[index] = decl
            replaced = True
            break
    if not replaced:
        skills.append(decl)
    parse_manifest(data, path)
    _write_payload(path, data)
    return path


def remove_skill_decl(project_root: Path, name: str) -> Path:
    path = manifest_path(project_root)
    data = _read_payload(path)
    skills = data.get("skills")
    if not isinstance(skills, list):
        raise ManifestError("Skillfile field 'skills' must be a list")
    kept = [entry for entry in skills if not (isinstance(entry, dict) and entry.get("name") == name)]
    if len(kept) == len(skills):
        raise ManifestError(f"Skill not declared in Skillfile: {name}")
    data["skills"] = kept
    parse_manifest(data, path)
    _write_payload(path, data)
    return path


def _read_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ManifestError(f"Skillfile.json not found at {path}; run 'csk init' first")
    try:
        data = protocol_json.loads(path.read_bytes())
    except protocol_json.ProtocolJSONError as exc:
        raise ManifestError(f"Malformed JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path} must contain a JSON object")
    return data


def _write_payload(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_manifest(project_root: Path) -> ProjectManifest | None:
    path = manifest_path(project_root)
    if not path.exists():
        return None
    try:
        data = protocol_json.loads(path.read_bytes())
    except protocol_json.ProtocolJSONError as exc:
        raise ManifestError(f"Malformed JSON in {path}: {exc}") from exc
    return parse_manifest(data, path)


def parse_manifest(
    data: dict[str, Any], path: Path, *, skill_extension_fields: set[str] | None = None
) -> ProjectManifest:
    if not isinstance(data, dict):
        raise ManifestError(f"{path} must contain a JSON object")
    unknown_top = sorted(set(data) - {"schema_version", "project", "agents", "locale", "skills"})
    if unknown_top:
        raise ManifestError(f"Skillfile has unsupported field(s): {', '.join(unknown_top)}")
    schema = data.get("schema_version")
    if schema is None:
        raise ManifestError(f"{path} is missing required field 'schema_version'")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise ManifestError(f"{path} field 'schema_version' must be an integer, got {schema!r}")
    if schema != SCHEMA_VERSION:
        raise ManifestError(
            f"Unsupported Skillfile schema_version {schema}; this Skillfile requires a newer csk"
        )

    project_alias = _parse_project_alias(data, path)

    agents = data.get("agents", [])
    if not _is_str_list(agents):
        raise ManifestError("Skillfile field 'agents' must be a list of strings")
    if len(set(agents)) != len(agents) or any(not is_valid_identifier(agent) for agent in agents):
        raise ManifestError("Skillfile field 'agents' must contain unique portable identifiers")

    locale = data.get("locale")
    if locale is not None and (not isinstance(locale, str) or not is_valid_locale(locale)):
        raise ManifestError("Skillfile field 'locale' must be a 1-64 character ASCII locale selector")

    raw_skills = data.get("skills")
    if not isinstance(raw_skills, list):
        raise ManifestError("Skillfile requires field 'skills' as a list")

    skills: list[SkillDecl] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_skills):
        if not isinstance(raw, dict):
            raise ManifestError(f"Skill declaration at index {index} must be an object")
        allowed_skill_fields = {
            "name",
            "source",
            "git",
            "tag",
            "branch",
            "revision",
            *(skill_extension_fields or set()),
        }
        unknown = sorted(set(raw) - allowed_skill_fields)
        if unknown:
            raise ManifestError(f"Skill declaration at index {index} has unsupported field(s): {', '.join(unknown)}")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ManifestError(f"Skill declaration at index {index} requires non-empty string 'name'")
        if not is_valid_identifier(name):
            raise ManifestError(f"Skill name {name!r} {IDENTIFIER_RULE}")
        if name in seen_names:
            raise ManifestError(f"Duplicate skill name in Skillfile: {name}")
        seen_names.add(name)

        source = raw.get("source", name)
        if not isinstance(source, str) or not source:
            raise ManifestError(f"Skill {name!r} field 'source' must be a non-empty string")
        if not is_valid_portable_path(source):
            raise ManifestError(f"Skill {name!r} field 'source' must be a portable relative path")

        git_url = raw.get("git")
        if git_url is not None and (not isinstance(git_url, str) or not git_url):
            raise ManifestError(f"Skill {name!r} field 'git' must be a non-empty string when present")

        ref_keys = [key for key in ("tag", "branch", "revision") if key in raw]
        if len(ref_keys) != 1:
            raise ManifestError(f"Skill {name!r} must specify exactly one of tag, branch, or revision")
        ref_kind = ref_keys[0]
        ref_value = raw[ref_kind]
        if not isinstance(ref_value, str) or not ref_value:
            raise ManifestError(f"Skill {name!r} field '{ref_kind}' must be a non-empty string")

        skills.append(SkillDecl(name=name, source=source, ref=SkillRef(ref_kind, ref_value), git=git_url))

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
    unknown = sorted(set(project) - {"alias"})
    if unknown:
        raise ManifestError(f"Skillfile field 'project' has unsupported field(s): {', '.join(unknown)}")
    alias = project.get("alias")
    if alias is None:
        return None
    if (
        not isinstance(alias, str)
        or not alias
        or len(alias) > 128
        or any(unicodedata.category(character) == "Cc" for character in alias)
    ):
        raise ManifestError(
            f"{path} field 'project.alias' must be a non-empty control-free string of at most 128 characters"
        )
    return alias


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)
