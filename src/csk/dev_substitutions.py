from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def dev_manifest_path(project_root: Path) -> Path:
    return project_root / DEV_MANIFEST_NAME


def load_substitutions(project_root: Path) -> dict[str, Substitution]:
    path = dev_manifest_path(project_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DevSubstitutionError(f"Malformed JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DevSubstitutionError(f"{path} must contain a JSON object")
    unknown = sorted(set(data) - {"substitutions"})
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise DevSubstitutionError(f"{DEV_MANIFEST_NAME} has unsupported field(s): {joined}")
    raw = data.get("substitutions", {})
    if not isinstance(raw, dict):
        raise DevSubstitutionError(f"{DEV_MANIFEST_NAME} field 'substitutions' must be an object")

    substitutions: dict[str, Substitution] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not name:
            raise DevSubstitutionError("Substitution names must be non-empty strings")
        if not is_valid_identifier(name):
            raise DevSubstitutionError(f"Substitution name {name!r} {IDENTIFIER_RULE}")
        substitutions[name] = _parse_entry(project_root, name, entry)
    return substitutions


def _parse_entry(project_root: Path, name: str, entry: Any) -> Substitution:
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
        if not isinstance(path_raw, str) or not path_raw:
            raise DevSubstitutionError(f"{label}.path must be a non-empty string")
        if "ref" in entry:
            raise DevSubstitutionError(f"{label} with 'path' reads the local checkout; 'ref' does not apply")
        resolved = Path(path_raw).expanduser()
        if not resolved.is_absolute():
            resolved = project_root / resolved
        return Substitution(name=name, path=resolved)

    if not isinstance(git_raw, str) or not git_raw:
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
    if not isinstance(value, str) or not value:
        raise DevSubstitutionError(f"{label}.ref.value must be a non-empty string")
    return Substitution(name=name, git=git_raw, ref_kind=kind, ref_value=value)
