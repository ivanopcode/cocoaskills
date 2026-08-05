from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import protocol_json
from .build_repository import (
    BuildRepositoryError,
    is_valid_ref_name,
    local_repository_identity,
    normalize_local_selector,
    parse_repository_source,
    resolve_local_selector,
)
from .identifiers import IDENTIFIER_RULE, is_valid_identifier


# Skillfile.dev.json substitutes providers locally during development. The
# file sits next to Skillfile.json, belongs to the managed .gitignore block,
# and stays out of version control; committed manifests remain the single
# declaration of the graph.
DEV_MANIFEST_NAME = "Skillfile.dev.json"

_SUB_REF_KINDS = {"tag", "revision", "branch"}


class DevSubstitutionError(Exception):
    pass


@dataclass(frozen=True)
class Substitution:
    name: str
    path: Path | None = None
    git: str | None = None
    ref_kind: str | None = None
    ref_value: str | None = None

    def describe(self) -> str:
        if self.path is not None:
            return f"path {self.path}"
        return f"git {self.git} {self.ref_kind} {self.ref_value}"


@dataclass(frozen=True)
class BuildRepositorySubstitution:
    skill_name: str
    repository_name: str
    path: Path | None = None
    selector: str | None = None
    git: str | None = None
    identity: str | None = None
    transport: str | None = None
    ref_kind: str | None = None
    ref_value: str | None = None

    def effective_identity(self, project_identity: str) -> str:
        if self.selector is not None:
            return local_repository_identity(project_identity, self.selector)
        assert self.identity is not None
        return self.identity


@dataclass(frozen=True)
class DevManifest:
    schema_version: int
    substitutions: dict[str, Substitution]
    build_repository_substitutions: dict[str, dict[str, BuildRepositorySubstitution]]

    def build_repository_substitution(
        self, skill_name: str, repository_name: str
    ) -> BuildRepositorySubstitution | None:
        return self.build_repository_substitutions.get(skill_name, {}).get(repository_name)


def dev_manifest_path(project_root: Path) -> Path:
    return project_root / DEV_MANIFEST_NAME


def load_substitutions(project_root: Path) -> dict[str, Substitution]:
    return load_manifest(project_root).substitutions


def load_manifest(project_root: Path) -> DevManifest:
    path = dev_manifest_path(project_root)
    if not path.exists():
        return DevManifest(schema_version=1, substitutions={}, build_repository_substitutions={})
    try:
        return parse_manifest(path.read_bytes(), project_root)
    except protocol_json.ProtocolJSONError as exc:
        raise DevSubstitutionError(f"Malformed JSON in {path}: {exc}") from exc


def parse_manifest(raw: bytes | str, project_root: Path) -> DevManifest:
    data = protocol_json.loads(raw)
    if not isinstance(data, dict):
        raise DevSubstitutionError(f"{DEV_MANIFEST_NAME} must contain a JSON object")
    schema_raw = data.get("schema_version")
    if "schema_version" in data:
        if not isinstance(schema_raw, int) or isinstance(schema_raw, bool) or schema_raw != 2:
            raise DevSubstitutionError(f"{DEV_MANIFEST_NAME} field 'schema_version' must be integer 2")
        schema = 2
    else:
        schema = 1
    allowed = {"substitutions"}
    if schema == 2:
        allowed.update({"schema_version", "build_repository_substitutions"})
    unknown = sorted(set(data) - allowed)
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise DevSubstitutionError(f"{DEV_MANIFEST_NAME} has unsupported field(s): {joined}")
    if schema == 2 and "substitutions" not in data:
        raise DevSubstitutionError(f"{DEV_MANIFEST_NAME} schema 2 requires field 'substitutions'")
    substitutions_raw = data.get("substitutions", {})
    if not isinstance(substitutions_raw, dict):
        raise DevSubstitutionError(f"{DEV_MANIFEST_NAME} field 'substitutions' must be an object")

    substitutions: dict[str, Substitution] = {}
    for name in sorted(substitutions_raw):
        entry = substitutions_raw[name]
        if not isinstance(name, str) or not name:
            raise DevSubstitutionError("Substitution names must be non-empty strings")
        if not is_valid_identifier(name):
            raise DevSubstitutionError(f"Substitution name {name!r} {IDENTIFIER_RULE}")
        substitutions[name] = _parse_entry(project_root, name, entry, schema=schema)
    build_substitutions = (
        _parse_build_repository_substitutions(
            data.get("build_repository_substitutions", {}), project_root
        )
        if schema == 2
        else {}
    )
    return DevManifest(
        schema_version=schema,
        substitutions=substitutions,
        build_repository_substitutions=build_substitutions,
    )


def _parse_entry(project_root: Path, name: str, entry: Any, *, schema: int) -> Substitution:
    label = f"substitutions.{name}"
    if not isinstance(entry, dict):
        raise DevSubstitutionError(f"{label} must be an object")
    unknown = sorted(set(entry) - {"path", "git", "ref"})
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise DevSubstitutionError(f"{label} has unsupported field(s): {joined}")

    path_raw = entry.get("path")
    git_raw = entry.get("git")
    if (path_raw is None) == (git_raw is None):
        raise DevSubstitutionError(f"{label} must declare exactly one of 'path' or 'git'")

    if path_raw is not None:
        if not _valid_bounded_string(path_raw, 8192 if schema == 2 else None):
            raise DevSubstitutionError(f"{label}.path must be a non-empty string")
        if "ref" in entry:
            raise DevSubstitutionError(f"{label} with 'path' reads the local checkout; 'ref' does not apply")
        resolved = Path(path_raw).expanduser()
        if not resolved.is_absolute():
            resolved = project_root / resolved
        return Substitution(name=name, path=resolved)

    if not _valid_bounded_string(git_raw, 8192 if schema == 2 else None):
        raise DevSubstitutionError(f"{label}.git must be a non-empty string")
    ref = entry.get("ref")
    if not isinstance(ref, dict):
        raise DevSubstitutionError(f"{label} with 'git' requires a 'ref' object")
    unknown_ref = sorted(set(ref) - {"kind", "value"})
    if unknown_ref:
        joined = ", ".join(repr(item) for item in unknown_ref)
        raise DevSubstitutionError(f"{label}.ref has unsupported field(s): {joined}")
    kind = ref.get("kind")
    if kind not in _SUB_REF_KINDS:
        raise DevSubstitutionError(f"{label}.ref.kind must be one of tag, revision, or branch")
    value = ref.get("value")
    if not _valid_bounded_string(value, 1024 if schema == 2 else None):
        raise DevSubstitutionError(f"{label}.ref.value must be a non-empty string")
    return Substitution(name=name, git=git_raw, ref_kind=kind, ref_value=value)


def _parse_build_repository_substitutions(
    raw: Any, project_root: Path
) -> dict[str, dict[str, BuildRepositorySubstitution]]:
    if not isinstance(raw, dict):
        raise DevSubstitutionError("build_repository_substitutions must be an object")
    result: dict[str, dict[str, BuildRepositorySubstitution]] = {}
    for skill_name in sorted(raw):
        label = f"build_repository_substitutions.{skill_name}"
        if not isinstance(skill_name, str) or not is_valid_identifier(skill_name):
            raise DevSubstitutionError(f"{label} skill name {IDENTIFIER_RULE}")
        repositories = raw[skill_name]
        if not isinstance(repositories, dict) or not repositories:
            raise DevSubstitutionError(f"{label} must be a non-empty object")
        parsed_repositories: dict[str, BuildRepositorySubstitution] = {}
        for repository_name in sorted(repositories):
            repository_label = f"{label}.{repository_name}"
            if not isinstance(repository_name, str) or not is_valid_identifier(repository_name):
                raise DevSubstitutionError(f"{repository_label} repository name {IDENTIFIER_RULE}")
            parsed_repositories[repository_name] = _parse_build_repository_substitution(
                repositories[repository_name],
                project_root,
                skill_name,
                repository_name,
                repository_label,
            )
        result[skill_name] = parsed_repositories
    return result


def _parse_build_repository_substitution(
    entry: Any,
    project_root: Path,
    skill_name: str,
    repository_name: str,
    label: str,
) -> BuildRepositorySubstitution:
    if not isinstance(entry, dict):
        raise DevSubstitutionError(f"{label} must be an object")
    unknown = sorted(set(entry) - {"path", "git", "ref"})
    if unknown:
        raise DevSubstitutionError(
            f"{label} has unsupported field(s): {', '.join(repr(item) for item in unknown)}"
        )
    has_path = "path" in entry
    has_git = "git" in entry
    if has_path == has_git:
        raise DevSubstitutionError(f"{label} must declare exactly one of 'path' or 'git'")
    if has_path:
        if "ref" in entry:
            raise DevSubstitutionError(f"{label} local path substitution must not declare ref")
        path_raw = entry["path"]
        if not _valid_bounded_string(path_raw, 8192):
            raise DevSubstitutionError(f"{label}.path must be a non-empty string of at most 8192 Unicode scalars")
        try:
            selector = normalize_local_selector(path_raw)
            path = resolve_local_selector(project_root, selector)
        except BuildRepositoryError as exc:
            raise DevSubstitutionError(f"{label}.path {exc}") from exc
        return BuildRepositorySubstitution(
            skill_name=skill_name,
            repository_name=repository_name,
            path=path,
            selector=selector,
        )

    git = entry["git"]
    if not isinstance(git, str):
        raise DevSubstitutionError(f"{label}.git must be an HTTPS or SSH repository URL")
    try:
        source = parse_repository_source(git)
    except BuildRepositoryError as exc:
        raise DevSubstitutionError(f"{label}.git {exc}") from exc
    ref = entry.get("ref")
    if not isinstance(ref, dict):
        raise DevSubstitutionError(f"{label}.ref requires a structured ref")
    unknown_ref = sorted(set(ref) - {"kind", "value"})
    if unknown_ref:
        raise DevSubstitutionError(
            f"{label}.ref has unsupported field(s): {', '.join(repr(item) for item in unknown_ref)}"
        )
    kind = ref.get("kind")
    value = ref.get("value")
    valid_revision = (
        kind == "revision"
        and isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )
    valid_named_ref = kind in {"tag", "branch"} and isinstance(value, str) and is_valid_ref_name(value)
    if not valid_revision and not valid_named_ref:
        raise DevSubstitutionError(f"{label}.ref must be a full lowercase revision or safe tag/branch")
    return BuildRepositorySubstitution(
        skill_name=skill_name,
        repository_name=repository_name,
        git=git,
        identity=source.identity,
        transport=source.transport,
        ref_kind=kind,
        ref_value=value,
    )


def _valid_bounded_string(value: Any, maximum: int | None) -> bool:
    return isinstance(value, str) and bool(value) and (maximum is None or len(value) <= maximum)
