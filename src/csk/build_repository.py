from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import protocol_json
from .identifiers import IDENTIFIER_RULE, is_valid_identifier, is_valid_portable_path


DESCRIPTOR_NAME = "skill-build.json"
GO_REPOSITORY_V1_DRIVER = "go-repository-v1"
PROTOCOL_VERSION = "1.0.0-rc.5"
CONFORMANCE_MANIFEST_SHA256 = "b6f56aacc0e37dcc6692f73f641bff761e89b645adfe20a47a06d81c6fda204c"

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
_SSH_USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SSH_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_SCP_RE = re.compile(
    r"^(?:([A-Za-z0-9][A-Za-z0-9._-]{0,63})@)?"
    r"([A-Za-z0-9][A-Za-z0-9.-]*):([A-Za-z0-9._/-]+)$"
)
_LOWER_HEX_RE = re.compile(r"^[0-9a-f]+$")
_WINDOWS_VOLUME_RE = re.compile(r"^[A-Za-z]:")


class BuildRepositoryError(ValueError):
    pass


@dataclass(frozen=True)
class LockedCommit:
    object_format: str
    hex: str


@dataclass(frozen=True)
class RepositorySource:
    git: str
    identity: str
    transport: str


@dataclass(frozen=True)
class BuildRepository:
    name: str
    git: str
    identity: str
    transport: str
    locked_commit: LockedCommit
    tag: str | None = None


@dataclass(frozen=True)
class BuildTarget:
    name: str
    driver: str
    build_root: str
    source_dir: str


@dataclass(frozen=True)
class SkillBuildDescriptor:
    schema_version: int
    targets: dict[str, BuildTarget]


def parse_locked_commit(raw: Any, *, field: str = "locked_commit") -> LockedCommit:
    if not isinstance(raw, dict):
        raise BuildRepositoryError(f"{field} must be an object")
    _reject_unknown_fields(raw, {"object_format", "hex"}, field)
    object_format = raw.get("object_format")
    hex_value = raw.get("hex")
    width = 40 if object_format == "sha1" else 64 if object_format == "sha256" else 0
    if (
        not isinstance(hex_value, str)
        or len(hex_value) != width
        or _LOWER_HEX_RE.fullmatch(hex_value) is None
    ):
        raise BuildRepositoryError(
            f"{field} requires a full lowercase sha1 or sha256 object id matching object_format"
        )
    assert isinstance(object_format, str)
    return LockedCommit(object_format=object_format, hex=hex_value)


def parse_repository_source(raw: str) -> RepositorySource:
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > 4096
        or any(character in raw for character in "%?#\\")
        or any(character.isspace() or unicodedata.category(character) == "Cc" for character in raw)
    ):
        raise BuildRepositoryError("repository git source is not in the released rc.5 grammar")

    host: str
    repository_path: str
    transport: str
    if raw.startswith("https://"):
        parsed = urlsplit(raw)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or _has_explicit_port(parsed.netloc)
        ):
            raise BuildRepositoryError("repository HTTPS source is invalid")
        host = parsed.hostname or ""
        repository_path = parsed.path.removeprefix("/")
        transport = "https"
    elif raw.startswith("ssh://"):
        parsed = urlsplit(raw)
        if (
            parsed.scheme != "ssh"
            or not parsed.netloc
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or _has_explicit_port(parsed.netloc)
            or (parsed.username is not None and _SSH_USER_RE.fullmatch(parsed.username) is None)
        ):
            raise BuildRepositoryError("repository SSH source is invalid")
        host = parsed.hostname or ""
        repository_path = parsed.path.removeprefix("/")
        transport = "ssh"
        if _SSH_PATH_RE.fullmatch(repository_path) is None:
            raise BuildRepositoryError("repository SSH path must be portable ASCII")
    else:
        match = _SCP_RE.fullmatch(raw)
        if match is None:
            raise BuildRepositoryError("repository source must use HTTPS or SSH")
        host = match.group(2)
        repository_path = match.group(3)
        transport = "ssh"

    if _HOST_RE.fullmatch(host) is None or not _valid_repository_path(
        repository_path, ascii_only=transport == "ssh"
    ):
        raise BuildRepositoryError("repository host or path is invalid")
    canonical_path = repository_path.removesuffix(".git")
    if not canonical_path:
        raise BuildRepositoryError("repository path is empty after canonicalization")
    identity = f"{host.lower()}/{canonical_path}"
    if len(identity) > 4096:
        raise BuildRepositoryError("canonical repository identity exceeds 4096 Unicode scalars")
    return RepositorySource(git=raw, identity=identity, transport=transport)


def is_valid_ref_name(value: str) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 255
        or value == "@"
        or value.startswith("/")
        or value.endswith(("/", "."))
        or "//" in value
        or ".." in value
        or "@{" in value
    ):
        return False
    if any(ord(character) <= 0x20 or ord(character) == 0x7F or character in "~^:?*[\\" for character in value):
        return False
    return all(not component.startswith(".") and not component.endswith(".lock") for component in value.split("/"))


def parse_skill_build(raw: bytes | str) -> SkillBuildDescriptor:
    try:
        data = protocol_json.loads(raw)
    except protocol_json.ProtocolJSONError as exc:
        raise BuildRepositoryError(f"Malformed JSON in {DESCRIPTOR_NAME}: {exc}") from exc
    if not isinstance(data, dict):
        raise BuildRepositoryError(f"{DESCRIPTOR_NAME} must contain a JSON object")
    _reject_unknown_fields(data, {"schema_version", "targets"}, DESCRIPTOR_NAME)
    schema_version = data.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
        raise BuildRepositoryError(f"{DESCRIPTOR_NAME} requires integer schema_version 1")
    raw_targets = data.get("targets")
    if not isinstance(raw_targets, dict) or not raw_targets:
        raise BuildRepositoryError(f"{DESCRIPTOR_NAME} field 'targets' must be a non-empty object")

    targets: dict[str, BuildTarget] = {}
    for name in sorted(raw_targets):
        label = f"targets.{name}"
        if not isinstance(name, str) or not is_valid_identifier(name):
            raise BuildRepositoryError(f"{label} target name {IDENTIFIER_RULE}")
        entry = raw_targets[name]
        if not isinstance(entry, dict):
            raise BuildRepositoryError(f"{label} must be an object")
        _reject_unknown_fields(entry, {"driver", "build_root", "source_dir"}, label)
        if entry.get("driver") != GO_REPOSITORY_V1_DRIVER:
            raise BuildRepositoryError(f"{label}.driver must be {GO_REPOSITORY_V1_DRIVER!r}")
        build_root = _root_or_portable_path(entry.get("build_root"), f"{label}.build_root")
        source_dir = _root_or_portable_path(entry.get("source_dir"), f"{label}.source_dir")
        if build_root != "." and source_dir != build_root and not source_dir.startswith(build_root + "/"):
            raise BuildRepositoryError(f"{label}.source_dir must be contained by build_root")
        targets[name] = BuildTarget(
            name=name,
            driver=GO_REPOSITORY_V1_DRIVER,
            build_root=build_root,
            source_dir=source_dir,
        )
    return SkillBuildDescriptor(schema_version=1, targets=targets)


def load_skill_build(repository_root: Path) -> SkillBuildDescriptor:
    return parse_skill_build((repository_root / DESCRIPTOR_NAME).read_bytes())


def normalize_local_selector(selector: str) -> str:
    if (
        not isinstance(selector, str)
        or not selector
        or selector.startswith("/")
        or selector.endswith("/")
        or "//" in selector
        or "\\" in selector
        or _WINDOWS_VOLUME_RE.match(selector) is not None
    ):
        raise BuildRepositoryError("local selector must be a non-empty project-relative POSIX selector")
    parts: list[str] = []
    for component in selector.split("/"):
        if component == ".":
            continue
        if component == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            else:
                parts.append(component)
        elif not component or "\0" in component:
            raise BuildRepositoryError("local selector contains an empty or invalid component")
        else:
            parts.append(component)
    return "/".join(parts) if parts else "."


def local_repository_identity(project_identity: str, selector: str) -> str:
    normalized = normalize_local_selector(selector)
    canonical = protocol_json.canonical_bytes(
        {
            "algorithm": "curator-operator-local-git-v1",
            "project": project_identity,
            "selector": normalized,
        }
    )
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def resolve_local_selector(project_root: Path, selector: str) -> Path:
    normalized = normalize_local_selector(selector)
    return Path(os.path.normpath(os.fspath(project_root / Path(*normalized.split("/")))))


def _has_explicit_port(netloc: str) -> bool:
    host_port = netloc.rsplit("@", 1)[-1]
    return ":" in host_port


def _valid_repository_path(value: str, *, ascii_only: bool) -> bool:
    if not value or value.startswith("/") or value.endswith("/") or "//" in value:
        return False
    if ascii_only and _SSH_PATH_RE.fullmatch(value) is None:
        return False
    return all(component not in {"", ".", ".."} and ":" not in component for component in value.split("/"))


def _root_or_portable_path(raw: Any, field: str) -> str:
    if not isinstance(raw, str) or (raw != "." and not is_valid_portable_path(raw)):
        raise BuildRepositoryError(f"{field} must be '.' or a portable relative path")
    return raw


def _reject_unknown_fields(data: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise BuildRepositoryError(f"{label} has unsupported field(s): {', '.join(repr(key) for key in unknown)}")
