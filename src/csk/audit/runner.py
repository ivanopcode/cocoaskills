from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import tempfile

from .. import config as config_module
from .. import git_admission, global_install, installer, manifest
from ..builds import toolchain
from ..config import GlobalConfig, ProjectConfig
from ..sources import modes, publish, repository_policy, transport
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
        if project_manifest.schema_version in (2, 3):
            plans = _source_plans(config, project, project_manifest, stack)
        else:
            plans = installer._build_plans(config, project_manifest, use_cache=False, stack=stack)
        return _audit_each(plans, config, scope=project.alias, errors=errors)


def _source_plans(
    config: GlobalConfig, project: ProjectConfig,
    project_manifest: manifest.ProjectManifest, stack: ExitStack,
) -> list[installer.SkillPlan]:
    # Audit resolves the live selection, including pinned transitive members.
    # Only acquisition/capture/closure run here: no publication, lock recovery,
    # build, adapter or runtime writer is invoked, even for an installed project.
    workspace = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="csk-audit-sources-")))
    mode = modes.ResolvingSources(
        home=config.path.parent,
        workspace=workspace,
        tool_for_endpoint=_source_tools(config),
        policy_path=config_module.source_policy_path(config.path),
    )
    roots, resolutions = publish.resolve_schema2_source_roots(project.path, project_manifest, mode=mode)
    members = publish.resolve_schema2_members(
        project.path, project_manifest, mode=mode,
        source_roots=roots, git_resolutions=resolutions,
    )
    if not members:
        raise AuditError(f"{project.alias}: no selected skills to audit")
    captures = publish.capture_schema2_members(members, home=mode.home, project_root=project.path)
    nodes = publish.resolve_source_closure(
        config, members, captures, project_manifest, resolutions, mode=mode,
    )
    return [installer.SkillPlan(node.decl, node.resolved, node.repo, node.snapshot, node.spec)
            for node in nodes]


def _source_tools(config: GlobalConfig) -> transport.ToolProvider:
    search_path = toolchain.capture_operator_search_path()

    def endpoint_tool(
        endpoint: repository_policy.ResolvedEndpoint,
        limits: git_admission.Limits | None = None,
    ) -> git_admission.GitTool:
        # Use the installer's endpoint-selected providers and bounded Git tool.
        # Resolve credentials lazily: local-only audits never consult a provider.
        ssh = https = None
        if endpoint.transport == "ssh":
            ssh = installer._endpoint_ssh_credentials(
                endpoint, config,
                installer._capture_operator_ssh_credentials(installer.InstallOptions()),
            )
        else:
            https = installer._endpoint_https_credentials(
                endpoint, config, installer._capture_operator_https_token(), search_path,
            )
        return installer._external_git_tool(
            search_path, require_ssh=endpoint.transport == "ssh",
            ssh_credentials=ssh, https_credentials=https,
            https_trust=git_admission.https_trust_args(config=config),
            timeout_seconds=10.0 if limits is None else limits.timeout_seconds,
            deadline=None if limits is None else limits.deadline,
        )

    return endpoint_tool


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
