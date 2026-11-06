from __future__ import annotations

from contextlib import ExitStack

from .. import global_install, installer, manifest
from ..config import GlobalConfig, ProjectConfig
from .pipeline import AuditInputTooLarge, AuditReport, audit_plans


class AuditError(Exception):
    pass


def audit_projects(config: GlobalConfig, *, alias: str | None = None, errors: list[str] | None = None) -> tuple[AuditReport, ...]:
    reports: list[AuditReport] = []
    for project in _selected_projects(config, alias):
        reports.extend(audit_project(config, project, errors=errors))
    return tuple(reports)


def audit_project(config: GlobalConfig, project: ProjectConfig, *, errors: list[str] | None = None) -> tuple[AuditReport, ...]:
    project_manifest = manifest.load_manifest(project.path)
    if project_manifest is None:
        raise AuditError(f"{project.alias}: Skillfile.json not found")
    with ExitStack() as stack:
        plans = installer._build_plans(config, project_manifest, use_cache=False, stack=stack)
        return _audit_each(plans, config, scope=project.alias, errors=errors)


def audit_global(config: GlobalConfig, *, errors: list[str] | None = None) -> tuple[AuditReport, ...]:
    global_manifest = global_install.load_manifest(config.path.parent)
    with ExitStack() as stack:
        plans = installer._build_plans(config, global_manifest, use_cache=False, stack=stack)
        return _audit_each(plans, config, scope="global", errors=errors)


def _selected_projects(config: GlobalConfig, alias: str | None) -> list[ProjectConfig]:
    if alias is None:
        return list(config.projects.values())
    project = config.projects.get(alias)
    if project is None:
        raise AuditError(f"Unknown project alias: {alias}")
    return [project]


def _audit_each(
    plans: list[installer.SkillPlan], config: GlobalConfig, *, scope: str,
    errors: list[str] | None,
) -> tuple[AuditReport, ...]:
    reports: list[AuditReport] = []
    for plan in plans:
        try:
            reports.extend(audit_plans([plan], config, scope=scope))
        except AuditInputTooLarge as exc:
            if errors is None:
                raise
            errors.append(f"{scope}: {plan.decl.name}: {exc}")
    return tuple(reports)
