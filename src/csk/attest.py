from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import audit_registry
from .config import GlobalConfig, ProjectConfig, RegistryConfig
from . import hybrid


# Re-check installed skills against the trusted audit registries so a
# revocation issued after install surfaces on demand. This reads the install
# markers rather than the source, so it is offline-friendly and fast.


@dataclass(frozen=True)
class AttestResult:
    project: str
    skill: str
    result: str
    registry: str | None
    detail: str | None = None


def attest_projects(config: GlobalConfig, *, alias: str | None = None) -> list[AttestResult]:
    registries = config.trusted_registries()
    fetch = audit_registry.make_http_fetch(config.path.parent / "cache" / "registry")
    results: list[AttestResult] = []
    for project in _selected(config, alias):
        marker_dirs = [project.path / ".agents" / "skills"]
        for skills_root in marker_dirs:
            for result in _attest_root(project.alias, skills_root, registries, fetch):
                results.append(result)
    # Hybrid store is machine-level; report it once under its own scope.
    if alias is None:
        hybrid_root = hybrid.hybrid_skills_root(config.path.parent)
        for result in _attest_root("<hybrid>", hybrid_root, registries, fetch):
            results.append(result)
    return results


def _attest_root(
    project_alias: str,
    skills_root: Path,
    registries: tuple[RegistryConfig, ...],
    fetch: audit_registry.FetchFn,
) -> list[AttestResult]:
    results: list[AttestResult] = []
    if not skills_root.exists():
        return results
    for marker_path in sorted(skills_root.glob("*/.csk-install.json")):
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = marker.get("name")
        git = marker.get("git")
        commit = marker.get("commit")
        content_sha256 = marker.get("content_sha256")
        if not isinstance(name, str):
            continue
        if not registries:
            results.append(AttestResult(project_alias, name, "no-registries", None))
            continue
        if not (isinstance(commit, str) and isinstance(content_sha256, str)):
            results.append(AttestResult(project_alias, name, "unattestable", None, "marker lacks commit or hash"))
            continue
        from . import source_identity as source_identity_mod

        identity = source_identity_mod.canonical_source_identity(git) if isinstance(git, str) else None
        if identity is None:
            results.append(AttestResult(project_alias, name, "unattestable", None, "no canonical source identity"))
            continue
        resolution = audit_registry.resolve(
            registries,
            source_identity=identity,
            commit=commit,
            content_sha256=content_sha256,
            fetch=fetch,
        )
        registry = resolution.attestation.registry if resolution.attestation else None
        results.append(AttestResult(project_alias, name, resolution.result, registry))
    return results


def render(results: list[AttestResult]) -> str:
    if not results:
        return "no installed skills to attest"
    lines: list[str] = []
    for item in results:
        suffix = f" ({item.detail})" if item.detail else ""
        registry = f" via {item.registry}" if item.registry else ""
        lines.append(f"{item.project}: {item.skill:<24} {item.result}{registry}{suffix}")
    return "\n".join(lines)


def has_revocation(results: list[AttestResult]) -> bool:
    return any(item.result == audit_registry.RESULT_REVOKED for item in results)


def _selected(config: GlobalConfig, alias: str | None) -> list[ProjectConfig]:
    if alias is None:
        return list(config.projects.values())
    project = config.projects.get(alias)
    if project is None:
        raise ValueError(f"Unknown project alias: {alias}")
    return [project]
