from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
import time
from datetime import datetime, timezone
from pathlib import Path

from . import adapters, audit_registry, closure, consumers, dev_substitutions, env_files, gc, git_ops, gitignore_gate, hashing, hybrid, locale, manifest, mcp_configs, protocol_json, shims, skillcheck, skillspec, snapshot, whitelist
from . import source_identity as source_identity_mod
from .audit import pipeline as audit_pipeline
from .config import GlobalConfig, ProjectConfig
from .skillspec import CommandSpec


class InstallError(Exception):
    pass


@dataclass(frozen=True)
class InstallOptions:
    dry_run: bool = False
    fix_gitignore: bool = False
    strict_tags: bool = False
    verbose: bool = False


@dataclass
class ProjectResult:
    alias: str
    path: Path
    status: str
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.errors)


@dataclass(frozen=True)
class SkillPlan:
    decl: manifest.SkillDecl
    resolved: git_ops.ResolvedRef
    repo: Path
    snapshot: Path
    spec: skillspec.SkillSpec


def install(config: GlobalConfig, *, alias: str | None = None, options: InstallOptions | None = None) -> list[ProjectResult]:
    options = options or InstallOptions()
    selected = _selected_projects(config, alias)
    results: list[ProjectResult] = []
    for project in selected:
        results.append(_install_project(config, project, options))
    if not options.dry_run:
        gc.collect_runtime(config, config.path.parent)
    return results


def _selected_projects(config: GlobalConfig, alias: str | None) -> list[ProjectConfig]:
    if alias is None:
        return list(config.projects.values())
    project = config.projects.get(alias)
    if project is None:
        raise InstallError(f"Unknown project alias: {alias}")
    return [project]


def _install_project(config: GlobalConfig, project: ProjectConfig, options: InstallOptions) -> ProjectResult:
    result = ProjectResult(alias=project.alias, path=project.path, status="ok")
    try:
        project_manifest = manifest.load_manifest(project.path)
        if project_manifest is None:
            result.status = "skipped"
            result.messages.append(f"{project.alias}: Skillfile.json not found; skipped")
            return result

        agents = project_manifest.agents or project.agents or config.default_agents
        expected_ignore = adapters.required_gitignore_entries(agents)
        try:
            gitignore_gate.ensure_ignored(project.path, expected_ignore, fix=options.fix_gitignore and not options.dry_run)
        except gitignore_gate.GitignoreError as exc:
            result.status = "skipped"
            result.messages.append(f"{project.alias}: {exc}; skipped")
            return result

        substitutions = dev_substitutions.load_substitutions(project.path)
        if substitutions:
            if config.audit.enabled and config.audit.mode == "strict":
                raise InstallError(
                    f"Dev substitutions are active in {dev_substitutions.DEV_MANIFEST_NAME}; "
                    "strict audit refuses substituted installs"
                )
            try:
                gitignore_gate.ensure_ignored(
                    project.path,
                    [dev_substitutions.DEV_MANIFEST_NAME],
                    fix=options.fix_gitignore and not options.dry_run,
                )
            except gitignore_gate.GitignoreError as exc:
                result.status = "skipped"
                result.messages.append(f"{project.alias}: {exc}; skipped")
                return result
            for substitution in substitutions.values():
                result.messages.append(
                    f"{project.alias}: SUBSTITUTION {substitution.name} -> {substitution.describe()}"
                )

        try:
            hybrid_decls = hybrid.load_hybrid_decls(config.path.parent)
        except hybrid.HybridError as exc:
            raise InstallError(str(exc)) from exc
        aliases = tuple(
            value
            for value in (project.alias, project.project_alias, project_manifest.project_alias)
            if value
        )
        applicable = [
            item
            for item in hybrid_decls
            if hybrid.applies_to_project(item, aliases=aliases, project_path=project.path)
        ]
        project_declared = {decl.name for decl in project_manifest.skills}
        for shadowed in sorted(item.decl.name for item in applicable if item.decl.name in project_declared):
            result.messages.append(
                f"{project.alias}: hybrid skill {shadowed} is shadowed by the project declaration"
            )
        hybrid_direct = [item.decl for item in applicable if item.decl.name not in project_declared]
        effective_manifest = (
            replace(project_manifest, skills=list(project_manifest.skills) + hybrid_direct)
            if hybrid_direct
            else project_manifest
        )

        effective_locale = project_manifest.locale or config.preferred_locale
        with ExitStack() as stack:
            nodes = closure.build_closure(
                config, effective_manifest, substitutions, use_cache=not options.dry_run, stack=stack
            )
            hybrid_store_names = _hybrid_store_names(nodes, project_declared)
            plans = [
                SkillPlan(decl=node.decl, resolved=node.resolved, repo=node.repo, snapshot=node.snapshot, spec=node.spec)
                for node in nodes
            ]
            validation_issues = _validate_skills(plans, effective_locale)
            result.messages.extend(_skill_validation_warnings(project.alias, validation_issues))
            _check_skill_validation_errors(validation_issues)
            closure.detect_active_command_collisions(nodes)
            _check_dependencies(plans)
            mcp_found, mcp_warnings = _check_mcp_servers(plans, project.path, agents, alias=project.alias)
            result.messages.extend(mcp_warnings)
            result.messages.extend(_migration_warnings(project.alias, plans))
            audit_gate = audit_pipeline.gate_plans(plans, config, scope=project.alias, record=not options.dry_run)
            result.messages.extend(audit_gate.warnings)
            if audit_gate.blocked:
                raise InstallError("; ".join(audit_gate.errors))
            registry_attest = _check_audit_registries(plans, config, result, alias=project.alias)
            if options.strict_tags:
                _check_moved_tags_strict(project.path / ".agents" / "skills", plans)
            else:
                result.messages.extend(_moved_tag_warnings(project.path / ".agents" / "skills", plans))

            if options.dry_run:
                for node in nodes:
                    result.messages.append(f"{project.alias}: {_node_summary(node)} (planned)")
                result.messages.append(f"{project.alias}: dry-run; no files modified")
                return result

            consumers.record_consumer(config.path.parent, project.path)
            hybrid_store = hybrid.hybrid_skills_root(config.path.parent)
            context_names: list[str] = []
            hybrid_context_names: list[str] = []
            expected_commands: set[str] = set()
            nodes_by_name = {node.name: node for node in nodes}
            for node in nodes:
                plan = SkillPlan(
                    decl=node.decl, resolved=node.resolved, repo=node.repo, snapshot=node.snapshot, spec=node.spec
                )
                active = node.active_commands()
                command_names: set[str] = set()
                if active:
                    command_names = install_runtime_commands(
                        config.path.parent, project.path / ".agents" / "bin", plan, only=active
                    )
                    expected_commands.update(command_names)
                activation = {"context": node.context_active, "commands": sorted(active)}
                requirers = node.consumers()
                is_hybrid = node.name in hybrid_store_names
                if node.context_active and is_hybrid:
                    # Hybrid context renders once per machine with the machine
                    # locale; per-project variance stays out of the shared marker.
                    installed = _install_skill_context_to_root(
                        hybrid_store,
                        plan,
                        config.preferred_locale,
                        [],
                        activation=activation,
                        requirers=requirers,
                        substituted=node.substituted,
                    )
                    hybrid_context_names.append(node.name)
                elif node.context_active:
                    installed = _install_skill_context(
                        project.path,
                        plan,
                        effective_locale,
                        agents,
                        activation=activation,
                        requirers=requirers,
                        substituted=node.substituted,
                        mcp_servers=mcp_found.get(node.name),
                        attestation=registry_attest.get(node.name),
                    )
                    context_names.append(node.name)
                else:
                    installed = _install_marker_only(
                        project.path,
                        plan,
                        activation=activation,
                        requirers=requirers,
                        substituted=node.substituted,
                        mcp_servers=mcp_found.get(node.name),
                        target_root=hybrid_store if is_hybrid else None,
                        attestation=registry_attest.get(node.name),
                    )
                suffix = " (hybrid)" if is_hybrid else ""
                result.messages.append(f"{project.alias}: {_node_summary(node)}{suffix} {installed}")
                if options.verbose:
                    result.messages.append(f"{project.alias}: {node.name} commit {node.resolved.commit}")
                    for command_name in sorted(command_names):
                        result.messages.append(
                            f"{project.alias}: {node.name} command {command_name} -> .agents/bin/{command_name}"
                        )

            _cleanup_removed_skills(project.path, set(nodes_by_name) - hybrid_store_names)
            all_hybrid_names = {item.decl.name for item in hybrid_decls}
            _cleanup_removed_skills_root(hybrid_store, all_hybrid_names | hybrid_store_names)
            shims.remove_stale_shims(project.path, expected_commands)
            env_files.write_env_files(project.path)
            adapters.refresh_adapter_groups(
                project.path,
                agents,
                [
                    (project.path / ".agents" / "skills", context_names),
                    (hybrid_store, hybrid_context_names),
                ],
                config.adapter_mode,
            )
            project_bin = project.path / ".agents" / "bin"
            if expected_commands and not _directory_is_on_path(project_bin):
                result.messages.append(
                    f"{project.alias}: commands are installed in {project_bin}, which is not on PATH; "
                    "invoke that directory explicitly or run 'csk shell-init <shell> --install' once "
                    "and source the printed hook from your shell profile"
                )
            return result
    except Exception as exc:
        result.status = "failed"
        result.errors.append(str(exc))
        return result


def _hybrid_store_names(nodes: list[closure.ClosureNode], project_declared: set[str]) -> set[str]:
    """Names materialized in the hybrid store: unreachable from project declarations."""
    by_name = {node.name: node for node in nodes}
    reachable: set[str] = set()
    stack = [name for name in project_declared if name in by_name]
    while stack:
        current = stack.pop()
        if current in reachable:
            continue
        reachable.add(current)
        for requirement in by_name[current].spec.requirements.values():
            if requirement.name in by_name and requirement.name not in reachable:
                stack.append(requirement.name)
    return set(by_name) - reachable


def _directory_is_on_path(directory: Path, *, path_value: str | None = None) -> bool:
    expected = os.path.normcase(os.path.abspath(directory))
    value = os.environ.get("PATH", "") if path_value is None else path_value
    return any(
        os.path.normcase(os.path.abspath(Path(entry).expanduser())) == expected
        for entry in value.split(os.pathsep)
        if entry
    )


def _node_summary(node: closure.ClosureNode) -> str:
    active = ",".join(sorted(node.active_commands()))
    consumers_label = ",".join(node.consumers())
    summary = (
        f"{node.name} {node.resolved.kind} {node.resolved.ref} {node.resolved.commit[:7]} "
        f"context={'yes' if node.context_active else 'no'} commands=[{active}] via={consumers_label}"
    )
    if node.substituted:
        summary += f" SUBSTITUTED ({node.substituted})"
    return summary


def _migration_warnings(project_alias: str, plans: list[SkillPlan]) -> list[str]:
    warnings: list[str] = []
    for plan in plans:
        if any(dependency.type == "skill" for dependency in plan.spec.dependencies.values()):
            warnings.append(
                f"{project_alias}: {plan.decl.name} uses dependencies.commands with type 'skill'; "
                "migrate to csk-skill.json schema v4 dependencies.skills"
            )
    return warnings


def _build_plans(
    config: GlobalConfig,
    project_manifest: manifest.ProjectManifest,
    *,
    use_cache: bool = True,
    stack: ExitStack | None = None,
) -> list[SkillPlan]:
    plans: list[SkillPlan] = []
    for decl in project_manifest.skills:
        repo = _ensure_skill_repo(config, decl, use_persistent_clone=use_cache, stack=stack)
        resolved = git_ops.resolve_ref(repo, decl.ref.kind, decl.ref.value)
        if use_cache:
            snap = snapshot.get_snapshot(config.path.parent, decl.source, repo, resolved.commit)
        else:
            if stack is None:
                raise InstallError("dry-run snapshot planning requires an ExitStack")
            tmp_root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="csk-dry-run-snapshot-")))
            snap = tmp_root / decl.source
            git_ops.archive(repo, resolved.commit, snap)
        if git_ops.repository_has_submodules(snap):
            raise InstallError(f"Submodules are unsupported in MVP: {decl.source}")
        spec = skillspec.load_skill_spec(snap)
        plans.append(SkillPlan(decl=decl, resolved=resolved, repo=repo, snapshot=snap, spec=spec))
    return plans


def _ensure_skill_repo(
    config: GlobalConfig,
    decl: manifest.SkillDecl,
    *,
    use_persistent_clone: bool,
    stack: ExitStack | None,
) -> Path:
    repo = config.skills_root / decl.source
    if repo.exists():
        if not (repo / ".git").exists():
            raise InstallError(f"Local skill path exists but is not a git repository: {repo}")
        git_ops.ensure_git_repo(repo)
        return repo
    if not decl.git:
        raise InstallError(f"Skill repository not found for {decl.name}: {repo}")
    if use_persistent_clone:
        try:
            git_ops.clone_repo(decl.git, repo)
        except git_ops.GitError as exc:
            raise InstallError(f"Failed to clone {decl.name} from {decl.git}: {exc}") from exc
        return repo
    if stack is None:
        raise InstallError("dry-run source cloning requires an ExitStack")
    tmp_root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="csk-dry-run-source-")))
    tmp_repo = tmp_root / decl.source
    try:
        git_ops.clone_repo(decl.git, tmp_repo)
    except git_ops.GitError as exc:
        raise InstallError(f"Failed to clone {decl.name} from {decl.git}: {exc}") from exc
    return tmp_repo


def _detect_command_collisions(plans: list[SkillPlan]) -> None:
    owners: dict[str, str] = {}
    for plan in plans:
        for command in plan.spec.commands.values():
            if command.type != "script":
                continue
            previous = owners.get(command.name)
            if previous:
                raise InstallError(
                    f"Command collision for {command.name!r}: exported by {previous} and {plan.decl.name}"
                )
            owners[command.name] = plan.decl.name


def _check_dependencies(plans: list[SkillPlan]) -> None:
    _check_system_commands(plans)
    _check_skill_command_dependencies(plans)


def _check_system_commands(plans: list[SkillPlan]) -> None:
    for plan in plans:
        for command in _system_dependencies(plan):
            if not command.command or shutil.which(command.command) is None:
                hint = f" Hint: {command.hint}" if command.hint else ""
                raise InstallError(f"Missing system command {command.command!r} for {plan.decl.name}.{hint}")


def _check_skill_command_dependencies(plans: list[SkillPlan]) -> None:
    errors: list[str] = []
    for plan in plans:
        errors.extend(skill_command_dependency_errors(plan, plans))
    if errors:
        raise InstallError("; ".join(errors))


def skill_command_dependency_errors(plan: SkillPlan, plans: list[SkillPlan]) -> list[str]:
    by_skill = {candidate.decl.name: candidate for candidate in plans}
    errors: list[str] = []
    for dependency in plan.spec.dependencies.values():
        if dependency.type != "skill":
            continue
        if not dependency.skill or not dependency.command:
            errors.append(f"Invalid skill dependency for {plan.decl.name}: {dependency.name}")
            continue
        provider = by_skill.get(dependency.skill)
        if provider is None:
            hint = f" Hint: {dependency.hint}" if dependency.hint else ""
            errors.append(
                f"Missing skill dependency {dependency.skill!r} for {plan.decl.name}; "
                f"add {dependency.skill} to Skillfile.json.{hint}"
            )
            continue
        provided = provider.spec.commands.get(dependency.command)
        if provided is None or provided.type != "script":
            errors.append(
                f"Skill dependency {plan.decl.name} requires {dependency.skill}.{dependency.command}, "
                f"but {dependency.skill} does not export a script command named {dependency.command!r}"
            )
    return errors


def _check_audit_registries(
    plans: list[SkillPlan],
    config: GlobalConfig,
    result: ProjectResult,
    *,
    alias: str,
) -> dict[str, dict[str, object]]:
    """Resolve each skill against trusted audit registries (RFC 0008).

    A verified revocation denies the install. An unknown artifact is advisory
    at this stage. Returns the authorizing attestation per skill for the
    marker.
    """
    registries = config.trusted_registries()
    if not registries:
        return {}
    strict = config.audit.registry_policy == "strict"
    cache_dir = config.path.parent / "cache" / "registry"
    unavailable, snapshot_warnings = audit_registry.check_snapshots(
        registries,
        cache_dir,
        fetch_snapshot=audit_registry.http_get_snapshot,
        now=time.time(),
        max_age_seconds=config.audit.snapshot_max_age_seconds,
        clock_skew_seconds=config.audit.snapshot_clock_skew_seconds,
    )
    for warning in snapshot_warnings:
        result.messages.append(f"{alias}: registry: {warning}")
    registries = tuple(r for r in registries if r.url not in unavailable)
    if not registries:
        # Every trusted registry served a tampered snapshot; refuse to proceed.
        raise InstallError("every trusted audit registry served a tampered snapshot")
    fetch = audit_registry.make_http_fetch(
        cache_dir,
        ttl_seconds=config.audit.cache_ttl_seconds,
        grace_seconds=config.audit.offline_grace_seconds,
    )
    attestations: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    for plan in plans:
        identity = source_identity_mod.canonical_source_identity(plan.decl.git) if plan.decl.git else None
        if identity is None:
            continue
        content_hash = hashing.content_sha256(plan.snapshot)
        resolution = audit_registry.resolve(
            registries,
            source_identity=identity,
            commit=plan.resolved.commit,
            content_sha256=content_hash,
            fetch=fetch,
        )
        for warning in resolution.warnings:
            result.messages.append(f"{alias}: registry: {warning}")
        if resolution.result == audit_registry.RESULT_REVOKED:
            registry = resolution.attestation.registry if resolution.attestation else "a trusted registry"
            errors.append(f"{plan.decl.name} is revoked by {registry}")
            continue
        if resolution.result == audit_registry.RESULT_DEPRECATED:
            result.messages.append(f"{alias}: registry: {plan.decl.name} is marked deprecated")
        if strict and resolution.result == audit_registry.RESULT_UNKNOWN:
            errors.append(
                f"{plan.decl.name} is not audited by any trusted registry (registry_policy is strict)"
            )
            continue
        if resolution.attestation is not None:
            att = resolution.attestation
            attestations[plan.decl.name] = {
                "registry": att.registry,
                "status": att.status,
                "key_id": att.key_id,
            }
    if errors:
        raise InstallError("; ".join(errors))
    return attestations


def _check_mcp_servers(
    plans: list[SkillPlan], project_root: Path, agents: list[str], *, alias: str = ""
) -> tuple[dict[str, dict[str, list[str]]], list[str]]:
    """Verify declared MCP servers against the target agent environments.

    Returns, per skill, the agents where each declared server was found, plus
    warnings for servers that are configured but statically unlikely to run:
    a stdio command missing from PATH, or a project-only declaration that the
    agent holds pending until the checkout is trusted.
    Raises InstallError when a requirement is not satisfied.
    """
    prefix = f"{alias}: " if alias else ""
    found: dict[str, dict[str, list[str]]] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for plan in plans:
        if not plan.spec.mcp_servers:
            continue
        per_skill: dict[str, list[str]] = {}
        for requirement in plan.spec.mcp_servers.values():
            resolution = mcp_configs.resolve_server(project_root, agents, requirement.name)
            available = sorted(agent for agent, ok in resolution.items() if ok)
            per_skill[requirement.name] = available
            if requirement.required_in == "all":
                missing = sorted(agent for agent, ok in resolution.items() if not ok)
                if missing:
                    errors.append(
                        f"MCP server {requirement.name!r} required by {plan.decl.name} is not configured "
                        f"for agent(s): {', '.join(missing)}. Hint: {requirement.hint}"
                    )
            elif not available:
                errors.append(
                    f"MCP server {requirement.name!r} required by {plan.decl.name} is not configured "
                    f"in any target agent environment. Hint: {requirement.hint}"
                )
            for agent, command in sorted(
                mcp_configs.missing_stdio_commands(project_root, available, requirement.name).items()
            ):
                warnings.append(
                    f"{prefix}MCP server {requirement.name!r} for {agent} runs {command!r}, "
                    "which is not on PATH"
                )
            trust_gated = sorted(
                agent
                for agent in available
                if requirement.name in mcp_configs.project_only_servers(project_root, agent)
            )
            if trust_gated:
                warnings.append(
                    f"{prefix}MCP server {requirement.name!r} is declared only in project-level "
                    f"config for {', '.join(trust_gated)}; agents keep project servers pending "
                    "until the checkout is trusted"
                )
        found[plan.decl.name] = per_skill
    if errors:
        raise InstallError("; ".join(errors))
    return found, warnings


def _system_dependencies(plan: SkillPlan) -> list[CommandSpec]:
    legacy = [command for command in plan.spec.commands.values() if command.type == "system"]
    explicit = [
        CommandSpec(
            name=dependency.name,
            type="system",
            command=dependency.command,
            hint=dependency.hint,
            source=dependency.source,
        )
        for dependency in plan.spec.dependencies.values()
        if dependency.type == "system"
    ]
    return legacy + explicit


def _validate_skills(
    plans: list[SkillPlan], effective_locale: str | None
) -> list[tuple[SkillPlan, skillcheck.ValidationIssue]]:
    issues: list[tuple[SkillPlan, skillcheck.ValidationIssue]] = []
    for plan in plans:
        for issue in skillcheck.validate_skill(plan.snapshot, locale_value=effective_locale):
            issues.append((plan, issue))
    return issues


def _skill_validation_warnings(
    project_alias: str, issues: list[tuple[SkillPlan, skillcheck.ValidationIssue]]
) -> list[str]:
    warnings: list[str] = []
    for plan, issue in issues:
        if issue.severity == "warning":
            warnings.append(
                f"{project_alias}: {plan.decl.name}: {skillcheck.format_issue(issue)}"
            )
    return warnings


def _check_skill_validation_errors(issues: list[tuple[SkillPlan, skillcheck.ValidationIssue]]) -> None:
    errors: list[str] = []
    for plan, issue in issues:
        if issue.severity == "error":
            errors.append(f"{plan.decl.name}: {issue.message}")
    if errors:
        raise InstallError("; ".join(errors))


def _check_moved_tags_strict(skills_dir: Path, plans: list[SkillPlan]) -> None:
    warnings = _moved_tag_warnings(skills_dir, plans)
    if warnings:
        raise InstallError("; ".join(warnings))


def _moved_tag_warnings(skills_dir: Path, plans: list[SkillPlan]) -> list[str]:
    warnings: list[str] = []
    for plan in plans:
        if plan.resolved.kind != "tag":
            continue
        marker = _read_marker(skills_dir / plan.decl.name / ".csk-install.json")
        if not marker:
            continue
        if (
            marker.get("ref_kind") == "tag"
            and marker.get("ref") == plan.resolved.ref
            and marker.get("commit") != plan.resolved.commit
        ):
            warnings.append(
                f"Moved tag for {plan.decl.name}: {plan.resolved.ref} "
                f"{marker.get('commit')} -> {plan.resolved.commit}"
            )
    return warnings


def install_runtime_commands(csk_home: Path, bin_dir: Path, plan: SkillPlan, *, only: set[str] | None = None) -> set[str]:
    commands: set[str] = set()
    if plan.spec.runtime_roots:
        shims.install_runtime_roots(
            csk_home=csk_home,
            skill_name=plan.decl.name,
            commit=plan.resolved.commit,
            snapshot=plan.snapshot,
            runtime_roots=plan.spec.runtime_roots,
        )
    for command in plan.spec.commands.values():
        if command.type != "script":
            continue
        if only is not None and command.name not in only:
            continue
        if plan.spec.runtime_roots:
            runtime_path = shims.runtime_root_command_path(
                csk_home=csk_home,
                skill_name=plan.decl.name,
                commit=plan.resolved.commit,
                command=command,
            )
        else:
            runtime_path = shims.install_runtime_command(
                csk_home=csk_home,
                skill_name=plan.decl.name,
                commit=plan.resolved.commit,
                snapshot=plan.snapshot,
                command=command,
            )
        shims.write_bin_shim(bin_dir, command.name, runtime_path)
        commands.add(command.name)
    return commands


def _install_skill_context(
    project_root: Path,
    plan: SkillPlan,
    effective_locale: str | None,
    agents: list[str],
    *,
    activation: dict[str, object] | None = None,
    requirers: list[str] | None = None,
    substituted: str | None = None,
    mcp_servers: dict[str, list[str]] | None = None,
    attestation: dict[str, object] | None = None,
) -> str:
    return _install_skill_context_to_root(
        project_root / ".agents" / "skills",
        plan,
        effective_locale,
        agents,
        activation=activation,
        requirers=requirers,
        substituted=substituted,
        mcp_servers=mcp_servers,
        attestation=attestation,
    )


def _install_skill_context_to_root(
    target_root: Path,
    plan: SkillPlan,
    effective_locale: str | None,
    agents: list[str],
    *,
    activation: dict[str, object] | None = None,
    requirers: list[str] | None = None,
    substituted: str | None = None,
    mcp_servers: dict[str, list[str]] | None = None,
    attestation: dict[str, object] | None = None,
) -> str:
    target = target_root / plan.decl.name
    marker = _read_marker(target / ".csk-install.json")
    if _marker_is_current(
        marker, target, plan, effective_locale, agents,
        activation=activation, substituted=substituted, mcp_servers=mcp_servers, attestation=attestation,
    ):
        return "up-to-date"

    tmp = target.parent / f".{plan.decl.name}.tmp-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    include_scripts = not plan.spec.commands and (plan.snapshot / "scripts").exists()
    files = whitelist.copy_context(
        plan.snapshot,
        tmp,
        include_scripts=include_scripts,
        exclude_roots=plan.spec.runtime_roots,
    )
    locale.render_locale(plan.snapshot, tmp, effective_locale)
    content_hash = hashing.content_sha256(tmp)
    marker_data = _marker_payload(
        plan,
        effective_locale,
        agents,
        content_hash=content_hash,
        files=files,
        activation=activation,
        requirers=requirers,
        substituted=substituted,
        mcp_servers=mcp_servers,
        attestation=attestation,
    )
    (tmp / ".csk-install.json").write_text(json.dumps(marker_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _replace_dir(tmp, target)
    return "installed"


def _install_marker_only(
    project_root: Path,
    plan: SkillPlan,
    *,
    activation: dict[str, object],
    requirers: list[str],
    substituted: str | None,
    mcp_servers: dict[str, list[str]] | None = None,
    target_root: Path | None = None,
    attestation: dict[str, object] | None = None,
) -> str:
    """Record a runtime-only or context-less node without agent prompt files.

    The marker directory keeps the runtime store referenced by GC and the
    closure auditable offline; adapters never mirror it.
    """
    if target_root is None:
        target_root = project_root / ".agents" / "skills"
    target = target_root / plan.decl.name
    marker = _read_marker(target / ".csk-install.json")
    if _marker_is_current(
        marker, target, plan, None, [], activation=activation, substituted=substituted,
        mcp_servers=mcp_servers, attestation=attestation,
    ):
        return "up-to-date"

    tmp = target_root / f".{plan.decl.name}.tmp-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    content_hash = hashing.content_sha256(tmp)
    marker_data = _marker_payload(
        plan,
        None,
        [],
        content_hash=content_hash,
        files=[],
        activation=activation,
        requirers=requirers,
        substituted=substituted,
        mcp_servers=mcp_servers,
        attestation=attestation,
    )
    (tmp / ".csk-install.json").write_text(json.dumps(marker_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _replace_dir(tmp, target)
    return "installed"


def _marker_payload(
    plan: SkillPlan,
    effective_locale: str | None,
    agents: list[str],
    *,
    content_hash: str,
    files: list[str],
    activation: dict[str, object] | None,
    requirers: list[str] | None,
    substituted: str | None,
    mcp_servers: dict[str, list[str]] | None = None,
    attestation: dict[str, object] | None = None,
) -> dict[str, object]:
    marker_data: dict[str, object] = {
        "schema_version": 1,
        "name": plan.decl.name,
        "source": plan.decl.source,
        "ref_kind": plan.resolved.kind,
        "ref": plan.resolved.ref,
        "commit": plan.resolved.commit,
        "content_sha256": content_hash,
        "locale": effective_locale,
        "agents": sorted(set(agents)),
        "commands": sorted(command.name for command in plan.spec.commands.values() if command.type == "script"),
        "dependencies": sorted(plan.spec.dependencies),
        "skill_schema_version": plan.spec.schema_version,
        "runtime_roots": sorted(set(plan.spec.runtime_roots)),
        "installed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "files": sorted(set(files)),
    }
    if plan.decl.git is not None:
        marker_data["git"] = plan.decl.git
    if plan.spec.requirements:
        marker_data["requirements"] = sorted(plan.spec.requirements)
    if mcp_servers is not None:
        marker_data["mcp_servers"] = {
            name: sorted(set(found)) for name, found in sorted(mcp_servers.items())
        }
    if attestation is not None:
        marker_data["attestation"] = attestation
    if activation is not None:
        activation_commands = activation.get("commands", [])
        if not isinstance(activation_commands, list) or not all(
            isinstance(command, str) for command in activation_commands
        ):
            raise InstallError("marker activation.commands must be a list of strings")
        marker_data["activation"] = {
            **activation,
            "commands": sorted(set(activation_commands)),
        }
    if requirers:
        marker_data["requirers"] = sorted(set(requirers))
    if substituted is not None:
        marker_data["substituted"] = substituted
    return marker_data


def _marker_is_current(
    marker: dict[str, object] | None,
    target: Path,
    plan: SkillPlan,
    locale_value: str | None,
    agents: list[str],
    *,
    activation: dict[str, object] | None = None,
    substituted: str | None = None,
    mcp_servers: dict[str, list[str]] | None = None,
    attestation: dict[str, object] | None = None,
) -> bool:
    if not marker or not target.exists():
        return False
    if marker.get("schema_version") != 1:
        raise InstallError(f"Unsupported installed marker schema in {target / '.csk-install.json'}")
    if marker.get("ref_kind") != plan.resolved.kind or marker.get("ref") != plan.resolved.ref:
        return False
    if marker.get("commit") != plan.resolved.commit:
        return False
    if marker.get("locale") != locale_value:
        return False
    if marker.get("agents") != sorted(set(agents)):
        return False
    if activation is not None:
        activation_commands = activation.get("commands", [])
        if not isinstance(activation_commands, list) or not all(
            isinstance(command, str) for command in activation_commands
        ):
            return False
        expected_activation = {**activation, "commands": sorted(set(activation_commands))}
        if marker.get("activation") != expected_activation:
            return False
    if marker.get("substituted") != substituted:
        return False
    if mcp_servers is not None:
        expected_mcp = {name: sorted(set(found)) for name, found in sorted(mcp_servers.items())}
        if marker.get("mcp_servers") != expected_mcp:
            return False
    if marker.get("attestation") != attestation:
        return False
    actual_hash = hashing.content_sha256(target)
    return marker.get("content_sha256") == actual_hash


def _read_marker(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        data = protocol_json.loads(path.read_bytes())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _replace_dir(new_dir: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.parent / f".{target.name}.backup-{os.getpid()}"
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists() or target.is_symlink():
        target.rename(backup)
    try:
        new_dir.rename(target)
    except Exception:
        if backup.exists() and not target.exists():
            backup.rename(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _cleanup_removed_skills(project_root: Path, expected: set[str]) -> None:
    _cleanup_removed_skills_root(project_root / ".agents" / "skills", expected)


def _cleanup_removed_skills_root(skills_root: Path, expected: set[str]) -> None:
    if not skills_root.exists():
        return
    for child in skills_root.iterdir():
        if not child.is_dir() and not child.is_symlink():
            continue
        if child.name.startswith("."):
            continue
        if child.name not in expected:
            if child.is_symlink():
                child.unlink()
            else:
                shutil.rmtree(child)
