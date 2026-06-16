from __future__ import annotations

from contextlib import ExitStack

from .. import global_install, installer, manifest
from ..config import GlobalConfig, ProjectConfig
from .pipeline import AuditReport, audit_plans


class AuditError(Exception):
    pass


def audit_projects(config: GlobalConfig, *, alias: str | None = None) -> tuple[AuditReport, ...]:
    reports: list[AuditReport] = []
    for project in _selected_projects(config, alias):
        reports.extend(audit_project(config, project))
    return tuple(reports)


def audit_project(config: GlobalConfig, project: ProjectConfig) -> tuple[AuditReport, ...]:
    project_manifest = manifest.load_manifest(project.path)
    if project_manifest is None:
        raise AuditError(f"{project.alias}: Skillfile.json not found")
    with ExitStack() as stack:
        plans = installer._build_plans(config, project_manifest, use_cache=False, stack=stack)
        return audit_plans(plans, config, scope=project.alias)


def audit_global(config: GlobalConfig) -> tuple[AuditReport, ...]:
    global_manifest = global_install.load_manifest(config.path.parent)
    with ExitStack() as stack:
        plans = installer._build_plans(config, global_manifest, use_cache=False, stack=stack)
        return audit_plans(plans, config, scope="global")


def _selected_projects(config: GlobalConfig, alias: str | None) -> list[ProjectConfig]:
    if alias is None:
        return list(config.projects.values())
    project = config.projects.get(alias)
    if project is None:
        raise AuditError(f"Unknown project alias: {alias}")
    return [project]
