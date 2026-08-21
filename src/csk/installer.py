from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from . import (
    adapters,
    audit_registry,
    build_ssh as build_ssh_module,
    closure,
    consumers,
    dev_substitutions,
    env_files,
    gc,
    git_ops,
    git_admission,
    gitignore_gate,
    hashing,
    hybrid,
    install_marker,
    locale,
    locking,
    manifest,
    mcp_configs,
    protocol_json,
    shims,
    skillcheck,
    skillspec,
    snapshot,
    transactions,
    whitelist,
)
from . import build_repository as build_repository_model
from . import build_repository_pipeline
from . import source_identity as source_identity_mod
from .audit import detectors as audit_detectors
from .audit.capabilities import CapabilityManifest
from .audit.model import Finding as AuditFinding, Severity
from .audit import pipeline as audit_pipeline
from .builds import cache as build_cache
from .builds import go_v1
from .builds import metadata as build_metadata
from .builds import planner as build_planner
from .builds import source as build_source
from .builds import toolchain as build_toolchain
from . import config as config_module
from .config import GlobalConfig, ProjectConfig
from .skillspec import CommandSpec


class InstallError(Exception):
    pass


_INSTALL_ORPHAN_RE = re.compile(r"^\..+\.(?:tmp|backup)-(\d+)$")


@dataclass(frozen=True)
class InstallOptions:
    dry_run: bool = False
    fix_gitignore: bool = False
    strict_tags: bool = False
    verbose: bool = False
    fetch: bool = False
    # Operator SSH selection for external build repositories, exactly as the
    # operator wrote it. Resolved once per run by capture_operator_ssh_credentials.
    ssh_identity: str | None = None
    ssh_agent: str | None = None
    ssh_known_hosts: str | None = None
    # True only when the CLI runs on an operator terminal; enables the
    # build-SSH precheck prompt. Never enabled for scripted runs.
    interactive: bool = False


@dataclass
class ProjectResult:
    alias: str
    path: Path
    status: str
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    builds: list[build_planner.BuildPlan] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.errors)


def failure_text(exc: BaseException) -> str:
    """Operator-visible text for a failure recorded at an install boundary.

    Install boundaries report failures as strings, and ``str(exc)`` drops the
    notes an exception carries.  Notes hold the part an operator acts on -- the
    remedy for a missed toolchain fingerprint deadline, the secondary failure
    behind a cleanup -- so render them under the message rather than lose them.
    The first line stays the exception's own message, which for build-driver
    errors is the cross-implementation protocol string.
    """

    return "\n".join([str(exc), *getattr(exc, "__notes__", ())])


@dataclass(frozen=True)
class SkillPlan:
    decl: manifest.SkillDecl
    resolved: git_ops.ResolvedRef
    repo: Path
    snapshot: Path
    spec: skillspec.SkillSpec


@dataclass(frozen=True)
class _MaterializationTarget:
    target_class: str
    identifier: str
    live_path: Path
    kind: transactions.TargetKind = "bytes"


@dataclass(frozen=True)
class _PublishedBuild:
    plan: build_planner.BuildPlan | None
    inspection: build_cache.CacheInspection | None
    marker: install_marker.MarkerBuild
    artifact_path: Path | None = None
    receipt_bytes: bytes | None = None
    build_source_identity: build_source.BuildSourceIdentity | None = None


class _MessageResult(Protocol):
    messages: list[str]


def install(config: GlobalConfig, *, alias: str | None = None, options: InstallOptions | None = None) -> list[ProjectResult]:
    options = options or InstallOptions()
    selected = _selected_projects(config, alias)
    results: list[ProjectResult] = []
    fetched_repos: set[Path] = set()
    operator_search_path = build_toolchain.capture_operator_search_path()
    operator_ssh_credentials = _capture_operator_ssh_credentials(options)
    for project in selected:
        results.append(
            _install_project(
                config,
                project,
                options,
                fetched_repos=fetched_repos,
                operator_search_path=operator_search_path,
                operator_ssh_credentials=operator_ssh_credentials,
            )
        )
    if (
        not options.dry_run
        and any(result.status == "ok" for result in results)
        and not any(result.failed for result in results)
    ):
        try:
            with locking.ManagerHomeLock(config.path.parent) as home_lock:
                gc.collect_runtime(
                    config,
                    config.path.parent,
                    guard=home_lock,
                )
        except locking.LockOrderError:
            raise
        except locking.LockError as exc:
            successful = next(
                result for result in results if result.status == "ok"
            )
            successful.messages.append(
                f"{successful.alias}: post-install garbage collection "
                f"skipped: {exc}"
            )
    return results


def _capture_operator_ssh_credentials(
    options: InstallOptions,
) -> git_admission.OperatorSSHCredentials:
    """Resolve the operator SSH surface once, before any project work begins."""

    try:
        return git_admission.capture_operator_ssh_credentials(
            identity=options.ssh_identity,
            agent=options.ssh_agent,
            known_hosts=options.ssh_known_hosts,
        )
    except git_admission.GitAdmissionError as exc:
        raise InstallError(str(exc)) from exc


def _selected_projects(config: GlobalConfig, alias: str | None) -> list[ProjectConfig]:
    if alias is None:
        return list(config.projects.values())
    project = config.projects.get(alias)
    if project is None:
        raise InstallError(f"Unknown project alias: {alias}")
    return [project]


def _install_project(
    config: GlobalConfig,
    project: ProjectConfig,
    options: InstallOptions,
    *,
    fetched_repos: set[Path],
    operator_search_path: build_toolchain.OperatorSearchPath | None,
    operator_ssh_credentials: git_admission.OperatorSSHCredentials | None = None,
) -> ProjectResult:
    generation_probe = _project_generation_probe(config, project)
    attempts = 2 if options.dry_run else 3
    csk_home = config.path.parent
    project_lock = (
        ExitStack()
        if options.dry_run
        else locking.ProjectLock(csk_home, project.path)
    )
    with project_lock:
        for attempt in range(attempts):
            try:
                expected_generation = generation_probe.capture()
                return _install_project_once(
                    config,
                    project,
                    options,
                    fetched_repos=fetched_repos,
                    operator_search_path=operator_search_path,
                    operator_ssh_credentials=operator_ssh_credentials,
                    generation_probe=generation_probe,
                    expected_generation=expected_generation,
                )
            except build_planner.BuildPlanningError as exc:
                if exc.code != "concurrent_state_change":
                    result = ProjectResult(
                        alias=project.alias,
                        path=project.path,
                        status="failed",
                    )
                    result.errors.append(failure_text(exc))
                    return result
                if attempt + 1 < attempts:
                    continue
                result = ProjectResult(
                    alias=project.alias,
                    path=project.path,
                    status="failed",
                )
                result.errors.append(failure_text(exc))
                return result
    raise AssertionError("unreachable project planning retry state")


def _transaction_engine(csk_home: Path) -> transactions.TransactionEngine:
    return transactions.TransactionEngine(csk_home)


def _concurrent_state_change(detail: str) -> build_planner.BuildPlanningError:
    return build_planner.BuildPlanningError("concurrent_state_change", detail)


def _capture_generation(
    probe: build_planner.GenerationProbe,
) -> dict[str, str]:
    return dict(probe.capture())


def _assert_generation_current(
    probe: build_planner.GenerationProbe,
    expected: Mapping[str, str],
    *,
    scope: str = "project",
) -> None:
    if _capture_generation(probe) != dict(expected):
        raise _concurrent_state_change(
            f"shared planning state changed before the atomic {scope} commit"
        )


def _generation_after_gate_writes(
    config: GlobalConfig,
    probe: build_planner.GenerationProbe,
    expected: Mapping[str, str],
) -> dict[str, str]:
    current = _capture_generation(probe)
    csk_home = Path(os.path.abspath(config.path.parent))
    gate_owned = {
        str(csk_home / "audit"),
        str(csk_home / "cache" / "registry"),
        str(csk_home / "state" / "registry"),
    }
    changed_inputs = sorted(
        key
        for key in set(expected) | set(current)
        if expected.get(key) != current.get(key) and key not in gate_owned
    )
    if changed_inputs:
        raise _concurrent_state_change(
            "shared planning input changed while trust gates were running: "
            + ", ".join(changed_inputs)
        )
    return current


def _install_project_once(
    config: GlobalConfig,
    project: ProjectConfig,
    options: InstallOptions,
    *,
    fetched_repos: set[Path],
    operator_search_path: build_toolchain.OperatorSearchPath | None,
    operator_ssh_credentials: git_admission.OperatorSSHCredentials | None = None,
    generation_probe: build_planner.GenerationProbe | None,
    expected_generation: Mapping[str, str] | None,
) -> ProjectResult:
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

        dev_manifest = dev_substitutions.load_manifest(project.path)
        substitutions = dev_manifest.substitutions
        if substitutions or dev_manifest.build_repository_substitutions:
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
            for skill_name in sorted(dev_manifest.build_repository_substitutions):
                for repository_name, repository_substitution in sorted(
                    dev_manifest.build_repository_substitutions[skill_name].items()
                ):
                    selected = (
                        f"path {repository_substitution.path}"
                        if repository_substitution.path is not None
                        else f"git {repository_substitution.git} "
                        f"{repository_substitution.ref_kind} {repository_substitution.ref_value}"
                    )
                    result.messages.append(
                        f"{project.alias}: BUILD REPOSITORY SUBSTITUTION "
                        f"{skill_name}.{repository_name} -> {selected}"
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
            fetched_before = set(fetched_repos)
            nodes = closure.build_closure(
                config,
                effective_manifest,
                substitutions,
                use_cache=not options.dry_run,
                fetch_existing=options.fetch,
                fetched_repos=fetched_repos,
                stack=stack,
            )
            if options.fetch:
                for repo in sorted(fetched_repos - fetched_before):
                    result.messages.append(f"{project.alias}: fetched {repo.name}")
            hybrid_store_names = _hybrid_store_names(nodes, project_declared)
            plans = [
                SkillPlan(decl=node.decl, resolved=node.resolved, repo=node.repo, snapshot=node.snapshot, spec=node.spec)
                for node in nodes
            ]
            validation_issues = _validate_skills(plans, effective_locale)
            result.messages.extend(_skill_validation_warnings(project.alias, validation_issues))
            _check_skill_validation_errors(validation_issues)
            build_providers = _freeze_build_providers(nodes, stack)
            closure.detect_active_command_collisions(nodes)
            build_planner.detect_command_collisions(
                build_providers,
                occupied=_active_script_owners(nodes),
            )
            _check_dependencies(plans)
            mcp_found, mcp_warnings = _check_mcp_servers(plans, project.path, agents, alias=project.alias)
            result.messages.extend(mcp_warnings)
            result.messages.extend(_migration_warnings(project.alias, plans))
            audit_gate = audit_pipeline.gate_plans(plans, config, scope=project.alias, record=not options.dry_run)
            result.messages.extend(audit_gate.warnings)
            if audit_gate.blocked:
                raise InstallError("; ".join(audit_gate.errors))
            registry_attest = _check_audit_registries(
                plans,
                config,
                result,
                alias=project.alias,
                read_only=options.dry_run,
            )
            if not options.dry_run and (
                config.audit.enabled or config.trusted_registries()
            ):
                if generation_probe is None or expected_generation is None:
                    raise AssertionError(
                        "trust gate planning requires generation state"
                    )
                expected_generation = _generation_after_gate_writes(
                    config,
                    generation_probe,
                    expected_generation,
                )
            if options.strict_tags:
                _check_moved_tags_strict(project.path / ".agents" / "skills", plans)
            else:
                result.messages.extend(_moved_tag_warnings(project.path / ".agents" / "skills", plans))

            if operator_search_path is None:
                raise AssertionError("build planning requires an operator search path")
            if generation_probe is None or expected_generation is None:
                raise AssertionError("project build planning requires generation state")
            cache_backend = build_cache.cache_for_manager_home(
                config.path.parent
            )
            result.builds.extend(
                build_planner.plan_builds(
                    build_providers,
                    manager_home=config.path.parent,
                    operator_search_path=operator_search_path,
                    forbidden_roots=(
                        project.path,
                        config.skills_root,
                        *(node.repo for node in nodes),
                    ),
                    cache_backend=cache_backend,
                    generation_probe=generation_probe,
                    expected_generation=expected_generation,
                    max_generation_attempts=1,
                )
            )
            external_published, external_messages = _publish_external_builds(
                config,
                project_root=project.path,
                nodes=nodes,
                substitutions=dev_manifest,
                operator_search_path=operator_search_path,
                ssh_credentials=operator_ssh_credentials,
                interactive=options.interactive and not options.dry_run,
                stack=stack,
                dry_run=options.dry_run,
                marker_roots=(
                    project.path / ".agents" / "skills",
                    hybrid.hybrid_skills_root(config.path.parent),
                ),
            )
            result.messages.extend(external_messages)
            if options.dry_run:
                for build in result.builds:
                    payload = json.dumps(
                        build.to_json(),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    result.messages.append(
                        f"{project.alias}: build {payload}"
                    )
                for node in nodes:
                    result.messages.append(f"{project.alias}: {_node_summary(node)} (planned)")
                result.messages.append(f"{project.alias}: dry-run; no files modified")
                return result

            (
                materialization_targets,
                adapter_targets,
            ) = _materialization_targets(
                config,
                project,
                nodes,
                hybrid_decls,
                hybrid_store_names,
                agents,
            )
            target_preimages = _capture_target_preimages(
                materialization_targets
            )
            private_builds = _build_private_misses(
                config,
                nodes,
                build_providers,
                tuple(result.builds),
                operator_search_path,
                cache_backend,
                stack,
                operation_roots=(project.path,),
            )

            with locking.ManagerHomeLock(config.path.parent) as home_lock:
                engine = _transaction_engine(config.path.parent)
                engine.recover(home_lock)
                _assert_generation_current(
                    generation_probe,
                    expected_generation,
                )
                _assert_target_preimages_current(
                    materialization_targets,
                    target_preimages,
                )
                _revalidate_closure(nodes, build_providers)
                published = _publish_planned_builds(
                    config.path.parent,
                    tuple(result.builds),
                    private_builds,
                    cache_backend,
                    home_lock,
                )
                for provider, commands in external_published.items():
                    published.setdefault(provider, {}).update(commands)
                messages = _commit_materialization(
                    config,
                    project,
                    options,
                    nodes=nodes,
                    hybrid_decls=hybrid_decls,
                    hybrid_store_names=hybrid_store_names,
                    agents=agents,
                    effective_locale=effective_locale,
                    mcp_found=mcp_found,
                    registry_attest=registry_attest,
                    published_builds=published,
                    materialization_targets=materialization_targets,
                    adapter_targets=adapter_targets,
                    target_preimages=target_preimages,
                    expected_generation=expected_generation,
                    engine=engine,
                    home_lock=home_lock,
                )
            result.messages.extend(messages)
            project_bin = project.path / ".agents" / "bin"
            if _active_command_names(nodes) and not _directory_is_on_path(
                project_bin
            ):
                result.messages.append(
                    f"{project.alias}: commands are installed in {project_bin}, which is not on PATH; "
                    "agent skills resolve that directory directly. For optional bare commands in an interactive "
                    "shell, run 'csk shell-init --install' once and source the printed hook from your profile"
                )
            return result
    except build_planner.BuildPlanningError as exc:
        if exc.code == "concurrent_state_change":
            raise
        result.status = "failed"
        result.errors.append(failure_text(exc))
        return result
    except locking.LockError:
        # Lock failures are process-coordination outcomes.  Preserve them for
        # the CLI boundary so callers receive EXIT_LOCK instead of a generic
        # per-project failure after the home lock moved into the commit phase.
        raise
    except Exception as exc:  # noqa: BLE001 - project boundary reports stable failures
        result.status = "failed"
        result.errors.append(failure_text(exc))
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


def _freeze_build_providers(
    nodes: list[closure.ClosureNode],
    stack: ExitStack,
) -> tuple[build_planner.BuildProvider, ...]:
    providers: list[build_planner.BuildProvider] = []
    for node in nodes:
        active = _active_local_build_command_names(node)
        if not active:
            continue
        frozen = stack.enter_context(
            build_source.freeze_snapshot(node.snapshot)
        )
        providers.append(
            build_planner.provider_from_spec(
                node.name,
                frozen,
                node.spec,
                active_commands=active,
            )
        )
    return tuple(providers)


def _active_build_command_names(node: closure.ClosureNode) -> set[str]:
    exported = {
        command.name
        for command in node.spec.commands.values()
        if command.type == "build"
    }
    if any(edge.mode == "full" for edge in node.edges):
        return exported
    active: set[str] = set()
    for edge in node.edges:
        if edge.mode != "runtime":
            continue
        active.update(
            command
            for command in (edge.commands or tuple(exported))
            if command in exported
        )
    return active


def _active_local_build_command_names(node: closure.ClosureNode) -> set[str]:
    return {
        name
        for name in _active_build_command_names(node)
        if node.spec.commands[name].driver == build_metadata.GO_V1_DRIVER
    }


def _active_external_build_command_names(node: closure.ClosureNode) -> set[str]:
    return {
        name
        for name in _active_build_command_names(node)
        if node.spec.commands[name].driver
        == build_repository_model.GO_REPOSITORY_V1_DRIVER
    }


def _operator_program(
    name: str, operator_search_path: build_toolchain.OperatorSearchPath
) -> Path:
    found = shutil.which(name, path=os.pathsep.join(operator_search_path.entries))
    if found is None:
        raise InstallError(f"operator-provided {name} is unavailable")
    return Path(found).resolve(strict=True)


def _external_git_tool(
    operator_search_path: build_toolchain.OperatorSearchPath,
    *,
    require_ssh: bool,
    ssh_credentials: git_admission.OperatorSSHCredentials | None = None,
) -> git_admission.GitTool:
    executable = _operator_program("git", operator_search_path)
    try:
        version = subprocess.run(
            (os.fspath(executable), "--version"),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).stdout.decode("ascii", "strict").strip()
        exec_path = Path(
            subprocess.run(
                (os.fspath(executable), "--exec-path"),
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).stdout.decode("utf-8", "strict").strip()
        ).resolve(strict=True)
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise InstallError("operator-provided Git identity cannot be frozen") from exc
    ssh = _operator_program("ssh", operator_search_path) if require_ssh else None
    # Public HTTPS never invokes askpass. If authentication is requested,
    # Python exits without yielding a credential rather than consulting a
    # repository-selected helper. SSH is likewise the frozen operator binary;
    # package data never supplies a wrapper or its options, and the identity
    # material comes only from the operator surface captured at process entry.
    return git_admission.GitTool(
        executable=executable,
        exec_path=exec_path,
        allowed_versions=(version,),
        askpass=Path(sys.executable).resolve(strict=True),
        ssh=ssh,
        ssh_credentials=ssh_credentials,
    )


def _vendored_inert_text(snapshot_root: Path, finding: AuditFinding) -> bool:
    """True for a high finding in non-executable text below a vendored module.

    The external build session runs exactly ``go list`` and ``go build`` with
    hooks, generators, and helpers denied, so prose in a vendored dependency
    (a third-party Makefile or README) never executes.  Such text stays an
    advisory finding but does not block the install; an executable file below
    ``vendor/`` and every critical finding still block.
    """

    if finding.severity is not Severity.HIGH:
        return False
    if finding.location is None:
        return False
    segments = finding.location.file.split("/")
    if "vendor" not in segments[:-1]:
        return False
    target = snapshot_root.joinpath(*segments)
    try:
        info = target.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    return not info.st_mode & 0o111


def _external_static_audit(subject: build_repository_pipeline.AuditSubject) -> None:
    findings = audit_detectors.detect_snapshot(
        subject.snapshot_root, CapabilityManifest.implicit_none()
    )
    blocked = [
        finding
        for finding in findings
        if finding.severity in {Severity.HIGH, Severity.CRITICAL}
        and not _vendored_inert_text(subject.snapshot_root, finding)
    ]
    if blocked:
        raise InstallError(
            "external repository static audit blocked: "
            + ", ".join(sorted({finding.id for finding in blocked}))
        )


def _existing_external_snapshot_key(
    marker_roots: tuple[Path, ...],
    provider: str,
    command: str,
) -> str | None:
    for root in marker_roots:
        path = root / provider / ".csk-install.json"
        try:
            marker = install_marker.read_install_marker(path.read_bytes())
        except (OSError, install_marker.InstallMarkerError):
            continue
        if not isinstance(marker, install_marker.InstallMarkerV3):
            continue
        build = marker.builds.get(command)
        if (
            build is None
            or build.driver != build_repository_model.GO_REPOSITORY_V1_DRIVER
            or build.effective_identity is None
            or build.object_format is None
            or build.commit is None
            or build.build_source is None
        ):
            continue
        return build_repository_pipeline.snapshot_key(
            build_repository_pipeline.EffectiveState(
                identity_kind=build.effective_identity.kind,
                identity=build.effective_identity.value,
                transport=None,
                object_format=build.object_format,
                commit=build.commit,
                substituted=bool(build.substituted),
            ),
            build.build_source.content_sha256,
        )
    return None


def _external_effective_state(
    project_identity: str,
    repository: build_repository_model.BuildRepository,
    substitution: dev_substitutions.BuildRepositorySubstitution | None,
) -> build_repository_pipeline.EffectiveState:
    if substitution is None:
        return build_repository_pipeline.EffectiveState(
            "network-git",
            repository.identity,
            repository.transport,
            repository.locked_commit.object_format,
            repository.locked_commit.hex,
        )
    if substitution.path is not None:
        assert substitution.selector is not None
        return build_repository_pipeline.EffectiveState(
            "operator-local-git",
            substitution.effective_identity(project_identity),
            None,
            repository.locked_commit.object_format,
            repository.locked_commit.hex,
            True,
            build_repository_pipeline.SubstitutionState("local-path"),
        )
    assert substitution.identity is not None and substitution.transport is not None
    assert substitution.ref_kind is not None and substitution.ref_value is not None
    commit = (
        substitution.ref_value
        if substitution.ref_kind == "revision"
        else repository.locked_commit.hex
    )
    return build_repository_pipeline.EffectiveState(
        "network-git",
        substitution.identity,
        substitution.transport,
        repository.locked_commit.object_format,
        commit,
        True,
        build_repository_pipeline.SubstitutionState(
            "network-git", substitution.ref_kind, substitution.ref_value
        ),
    )


def _rule_credentials(
    rule: build_ssh_module.BuildSSHRule,
    canonical_identity: str,
) -> git_admission.OperatorSSHCredentials:
    """Materialize one configured scope into validated operator credentials."""

    try:
        return git_admission.capture_operator_ssh_credentials(
            identity=(
                os.fspath(Path(rule.identity).expanduser())
                if rule.identity
                else None
            ),
            agent=rule.agent,
            known_hosts=(
                os.fspath(Path(rule.known_hosts).expanduser())
                if rule.known_hosts
                else None
            ),
        )
    except git_admission.GitAdmissionError as exc:
        raise InstallError(
            f"build_ssh scope {rule.scope!r} selected for {canonical_identity}: {exc}"
        ) from exc


def _prompt_build_ssh_rule(
    skill_name: str,
    command: str,
    canonical_identity: str,
) -> tuple[build_ssh_module.BuildSSHRule | None, bool]:
    """Ask the operator for credentials for one unmatched SSH build repository.

    Returns (rule, persist). The rule is None when the operator declines; the
    caller then fails closed with the non-interactive remedy. Nothing is
    persisted without the explicit scope choice.
    """

    namespace = build_ssh_module.default_scope(canonical_identity)
    host = canonical_identity.split("/")[0]
    print(
        f"Skill {skill_name!r} builds {command!r} from the private SSH repository\n"
        f"  {canonical_identity}\n"
        "No build-SSH credentials are configured for this scope."
    )
    agent_answer = input("Use your ssh-agent (SSH_AUTH_SOCK)? [Y/n] ").strip().lower()
    agent = "auto" if agent_answer in {"", "y", "yes"} else None
    identity_raw = input(
        "Identity file (a .pub pins which agent key is offered; empty for none): "
    ).strip()
    identity = identity_raw or None
    if agent is None and identity is None:
        return None, False
    scope_answer = input(
        "Persist to config for future installs?\n"
        f"  [1] {namespace}   (default)\n"
        f"  [2] {host}\n"
        "  [3] this run only\n"
        "  or type a custom scope: "
    ).strip()
    persist = True
    if scope_answer in {"", "1"}:
        scope = namespace
    elif scope_answer == "2":
        scope = host
    elif scope_answer == "3":
        scope, persist = namespace, False
    else:
        scope = scope_answer
    try:
        build_ssh_module.validate_scope(scope)
        rule = build_ssh_module.BuildSSHRule(
            scope=scope, agent=agent, identity=identity, known_hosts=None
        )
        if not build_ssh_module.match((rule,), canonical_identity):
            raise build_ssh_module.BuildSSHError(
                f"scope {scope!r} does not cover {canonical_identity}"
            )
    except build_ssh_module.BuildSSHError as exc:
        print(f"Rejected: {exc}")
        return None, False
    return rule, persist


def _resolve_build_ssh_credentials(
    config: GlobalConfig,
    selected: list[tuple[closure.ClosureNode, str]],
    substitutions: dev_substitutions.DevManifest,
    *,
    run_wide: git_admission.OperatorSSHCredentials | None,
    interactive: bool,
    messages: list[str],
    dry_run: bool,
) -> dict[tuple[str, str], git_admission.OperatorSSHCredentials | None]:
    """Select operator SSH credentials for every external build repository.

    Precedence: an explicit run-wide selection (flags or CSK_BUILD_SSH_*)
    covers every repository; otherwise the longest matching ``build_ssh``
    config scope covers its repositories; an interactive terminal may fill a
    gap on the spot; anything still unselected fails closed with the exact
    command that would fix it. Package data never reaches this choice: the
    match key is the canonical identity the manifest is already locked to.
    """

    run_selected = run_wide if run_wide is not None and run_wide.selected else None
    rules = config.build_ssh
    persisted: list[build_ssh_module.BuildSSHRule] = []
    selection: dict[
        tuple[str, str], git_admission.OperatorSSHCredentials | None
    ] = {}
    missing: list[tuple[str, str, str]] = []
    for node, name in selected:
        key = (node.name, name)
        command = node.spec.commands[name]
        repository = node.spec.build_repositories[command.repository or ""]
        substitution = substitutions.build_repository_substitution(
            node.name, repository.name
        )
        if substitution is not None and substitution.path is not None:
            selection[key] = None
            continue
        git = repository.git if substitution is None else substitution.git
        assert git is not None
        source = build_repository_model.parse_repository_source(git)
        if source.transport != "ssh":
            selection[key] = None
            continue
        if run_selected is not None:
            selection[key] = run_selected
            if dry_run:
                messages.append(
                    f"external build ssh: {source.identity} <- operator flags/env"
                )
            continue
        rule = build_ssh_module.match(rules, source.identity)
        if rule is None and interactive:
            rule, persist = _prompt_build_ssh_rule(node.name, name, source.identity)
            if rule is not None:
                rules = rules + (rule,)
                if persist:
                    persisted.append(rule)
        if rule is None:
            missing.append((node.name, name, source.identity))
            continue
        selection[key] = _rule_credentials(rule, source.identity)
        if dry_run:
            messages.append(
                f"external build ssh: {source.identity} <- config scope {rule.scope!r}"
            )
    if persisted:
        config_module.save_config(replace(config, build_ssh=rules))
        for rule in persisted:
            messages.append(
                f"build_ssh scope {rule.scope!r} saved to {config.path}"
            )
    if missing:
        lines = [
            f"{git_admission.SSH_CREDENTIAL_MISSING}: "
            "external build repositories need SSH credentials:",
        ]
        for skill_name, name, identity in missing:
            lines.append(f"  {identity} (command {name!r} of skill {skill_name!r})")
        first = missing[0][2]
        lines.append(
            "select credentials with: csk config build-ssh add "
            f"{build_ssh_module.default_scope(first)} --agent auto "
            "--identity ~/.ssh/<key>.pub"
        )
        lines.append(
            "or pass --build-ssh-agent/--build-ssh-identity, "
            "or set CSK_BUILD_SSH_AGENT/CSK_BUILD_SSH_IDENTITY"
        )
        raise InstallError("\n".join(lines))
    return selection


def _publish_external_builds(
    config: GlobalConfig,
    *,
    project_root: Path,
    nodes: list[closure.ClosureNode],
    substitutions: dev_substitutions.DevManifest,
    operator_search_path: build_toolchain.OperatorSearchPath,
    stack: ExitStack,
    dry_run: bool,
    marker_roots: tuple[Path, ...],
    ssh_credentials: git_admission.OperatorSSHCredentials | None = None,
    interactive: bool = False,
) -> tuple[dict[str, dict[str, _PublishedBuild]], list[str]]:
    messages: list[str] = []
    selected = [
        (node, name)
        for node in nodes
        for name in sorted(_active_external_build_command_names(node))
    ]
    if not selected:
        return {}, []
    if sys.platform not in {"darwin", "win32"}:
        raise InstallError(
            "go-repository-v1 is supported only on macOS and Windows; "
            "Linux qualification is deferred"
        )
    ssh_selection = _resolve_build_ssh_credentials(
        config,
        selected,
        substitutions,
        run_wide=ssh_credentials,
        interactive=interactive,
        messages=messages,
        dry_run=dry_run,
    )
    require_ssh = any(
        credentials is not None for credentials in ssh_selection.values()
    )
    git_tools: dict[
        git_admission.OperatorSSHCredentials | None, git_admission.GitTool
    ] = {}

    def _git_tool_for(
        credentials: git_admission.OperatorSSHCredentials | None,
    ) -> git_admission.GitTool:
        if credentials not in git_tools:
            git_tools[credentials] = _external_git_tool(
                operator_search_path,
                require_ssh=credentials is not None,
                ssh_credentials=credentials,
            )
        return git_tools[credentials]
    private_base = Path(
        stack.enter_context(
            tempfile.TemporaryDirectory(prefix="csk-external-build-operation-")
        )
    )
    session = stack.enter_context(
        build_toolchain.establish_toolchain(
            build_toolchain.ToolchainConfig(
                private_base=private_base,
                operator_search_path=operator_search_path,
                forbidden_roots=tuple(
                    path
                    for path in (
                        config.path.parent,
                        project_root,
                        config.skills_root,
                        *(node.repo for node in nodes),
                    )
                    if path.exists()
                ),
            )
        )
    )
    compiler = build_repository_pipeline.ExistingGoV1Session(session)
    store = build_repository_pipeline.DiskProtectedStore(
        config.path.parent / "external-builds"
    )
    project_identity = locking.canonical_project_identity(project_root)
    published: dict[str, dict[str, _PublishedBuild]] = {}
    for node, name in selected:
        command = node.spec.commands[name]
        assert command.repository is not None and command.target is not None
        repository = node.spec.build_repositories[command.repository]
        substitution = substitutions.build_repository_substitution(
            node.name, repository.name
        )
        effective = _external_effective_state(
            project_identity, repository, substitution
        )
        git_tool = _git_tool_for(ssh_selection[(node.name, name)])

        def acquire(
            repository: build_repository_model.BuildRepository = repository,
            substitution: dev_substitutions.BuildRepositorySubstitution | None = substitution,
            effective: build_repository_pipeline.EffectiveState = effective,
            git_tool: git_admission.GitTool = git_tool,
        ) -> git_admission.Snapshot:
            if substitution is not None and substitution.path is not None:
                return git_admission.admit_local(substitution.path, git_tool)
            git = repository.git if substitution is None else substitution.git
            assert git is not None
            source = build_repository_model.parse_repository_source(git)
            tag = repository.tag
            if substitution is not None:
                tag = (
                    substitution.ref_value
                    if substitution.ref_kind == "tag"
                    else None
                )
                if substitution.ref_kind == "branch":
                    raise InstallError(
                        "network build repository branch substitutions require "
                        "an independently pinned revision"
                    )
            return git_admission.acquire_network(
                source,
                build_repository_model.LockedCommit(
                    effective.object_format, effective.commit
                ),
                git_tool,
                tag=tag,
            )

        result = build_repository_pipeline.run_pipeline(
            build_repository_pipeline.PipelineRequest(
                operation=(
                    build_repository_pipeline.Operation.DRY_RUN
                    if dry_run
                    else build_repository_pipeline.Operation.INSTALL
                ),
                command=name,
                target=command.target,
                declared=build_repository_pipeline.declared_state(repository),
                effective=effective,
                acquire=acquire,
                audit=_external_static_audit,
                store=store,
                compiler=compiler,
                offline_snapshot_key=_existing_external_snapshot_key(
                    marker_roots, node.name, name
                ),
            )
        )
        messages.append(
            f"external build {node.name}.{name}: {result.state} "
            f"source={result.build_source} cache={result.cache_key}"
        )
        if dry_run:
            continue
        if (
            result.cache_key is None
            or result.receipt is None
            or result.artifact is None
            or result.subject is None
            or result.build_source is None
        ):
            raise InstallError(f"external build {node.name}.{name} returned incomplete state")
        receipt_value = protocol_json.loads_canonical(result.receipt)
        assert isinstance(receipt_value, dict)
        artifact_value = receipt_value.get("artifact")
        assert isinstance(artifact_value, dict)
        artifact_relative = artifact_value.get("path")
        if not isinstance(artifact_relative, str):
            raise InstallError("external build receipt has no artifact path")
        selected_state = result.subject.effective
        marker = install_marker.InstallMarkerBuildV3(
            driver=build_repository_model.GO_REPOSITORY_V1_DRIVER,
            receipt_schema_version=2,
            execution_policy=build_metadata.PORTABLE_EXECUTION_POLICY,
            repository=repository.name,
            declared_identity=install_marker.MarkerRepositoryIdentity(
                "network-git", repository.identity
            ),
            declared_locked_commit=install_marker.MarkerRepositoryCommit(
                repository.locked_commit.object_format,
                repository.locked_commit.hex,
            ),
            declared_tag=repository.tag,
            effective_identity=install_marker.MarkerRepositoryIdentity(
                selected_state.identity_kind, selected_state.identity
            ),
            object_format=selected_state.object_format,
            commit=selected_state.commit,
            substituted=selected_state.substituted,
            substitution=(
                install_marker.MarkerRepositorySubstitution(
                    type=selected_state.substitution.type,
                    ref=(
                        install_marker.MarkerRepositoryRef(
                            selected_state.substitution.ref_kind,
                            selected_state.substitution.ref_value or "",
                        )
                        if selected_state.substitution.ref_kind is not None
                        else None
                    ),
                )
                if selected_state.substitution is not None
                else None
            ),
            build_source=build_source.BuildSourceIdentity(
                "curator-build-source-v1", result.build_source
            ),
            descriptor_target=command.target,
            cache_key=result.cache_key,
            receipt_sha256="sha256:"
            + hashlib.sha256(result.receipt).hexdigest(),
            artifact_sha256="sha256:"
            + hashlib.sha256(result.artifact).hexdigest(),
            artifact_path=artifact_relative,
        )
        artifact_path = (
            store.root
            / "artifacts"
            / result.cache_key.removeprefix("sha256:")
            / build_metadata.derived_cache_artifact_name(artifact_relative)
        )
        published.setdefault(node.name, {})[name] = _PublishedBuild(
            plan=None,
            inspection=None,
            marker=marker,
            artifact_path=artifact_path,
            receipt_bytes=result.receipt,
            build_source_identity=build_source.BuildSourceIdentity(
                "curator-build-source-v1", result.build_source
            ),
        )
    return published, messages


def _active_script_owners(
    nodes: list[closure.ClosureNode],
) -> dict[str, str]:
    owners: dict[str, str] = {}
    for node in nodes:
        for command in node.active_commands():
            owners[command] = node.name
    return owners


def _project_generation_probe(
    config: GlobalConfig,
    project: ProjectConfig,
) -> build_planner.FilesystemGenerationProbe:
    csk_home = config.path.parent
    user_home = Path.home()
    paths = (
        config.path,
        project.path / manifest.MANIFEST_NAME,
        project.path / dev_substitutions.DEV_MANIFEST_NAME,
        hybrid.hybrid_manifest_path(csk_home),
        project.path / ".agents" / "skills",
        hybrid.hybrid_skills_root(csk_home),
        csk_home / "audit",
        csk_home / "builds",
        csk_home / "cache" / "registry",
        csk_home / "state" / "registry",
        project.path / ".mcp.json",
        project.path / ".cursor" / "mcp.json",
        project.path / ".codex" / "config.toml",
        project.path / ".gemini" / "settings.json",
        project.path / "opencode.json",
        project.path / "opencode.jsonc",
        project.path / ".claude" / "settings.json",
        project.path / ".claude" / "settings.local.json",
        user_home / ".claude.json",
        user_home / ".cursor" / "mcp.json",
        user_home / ".codex" / "config.toml",
        user_home / ".gemini" / "settings.json",
        user_home / ".codeium" / "windsurf" / "mcp_config.json",
        user_home / ".config" / "opencode" / "opencode.json",
        user_home / ".config" / "opencode" / "opencode.jsonc",
    )
    return build_planner.FilesystemGenerationProbe(paths)


def _materialization_targets(
    config: GlobalConfig,
    project: ProjectConfig,
    nodes: list[closure.ClosureNode],
    hybrid_decls: list[hybrid.HybridDecl],
    hybrid_store_names: set[str],
    agents: list[str],
) -> tuple[
    tuple[_MaterializationTarget, ...],
    tuple[adapters.AdapterTarget, ...],
]:
    adapters.warn_unknown_agents(agents)
    home = Path(os.path.abspath(config.path.parent))
    project_root = Path(os.path.abspath(project.path))
    project_skills = project_root / ".agents" / "skills"
    hybrid_skills = hybrid.hybrid_skills_root(home)
    runtime_root = home / "runtime"
    project_bin = project_root / ".agents" / "bin"
    targets: list[_MaterializationTarget] = []

    project_names = {
        node.name
        for node in nodes
        if node.name not in hybrid_store_names
    }
    for name in sorted(project_names):
        targets.append(
            _MaterializationTarget(
                target_class="10-context",
                identifier=f"project/{name}",
                live_path=project_skills / name,
                kind="entry",
            )
        )
    for name in sorted(hybrid_store_names):
        targets.append(
            _MaterializationTarget(
                target_class="10-context",
                identifier=f"hybrid/{name}",
                live_path=hybrid_skills / name,
                kind="entry",
            )
        )

    all_hybrid_names = {item.decl.name for item in hybrid_decls}
    targets.extend(
        _stale_entry_targets(
            project_skills,
            project_names,
            identifier_prefix="context/project",
        )
    )
    targets.extend(
        _stale_entry_targets(
            hybrid_skills,
            all_hybrid_names | hybrid_store_names,
            identifier_prefix="context/hybrid",
        )
    )

    for node in nodes:
        if not node.active_commands():
            continue
        targets.append(
            _MaterializationTarget(
                target_class="20-runtime",
                identifier=f"{node.name}/{node.resolved.commit}",
                live_path=(
                    runtime_root
                    / node.name
                    / node.resolved.commit
                ),
                kind="entry",
            )
        )
    runtime_references = _runtime_references_for_plan(
        config,
        project_root,
        nodes,
        all_hybrid_names | hybrid_store_names,
    )
    if runtime_root.exists():
        for skill_dir in runtime_root.iterdir():
            if not skill_dir.is_dir() or skill_dir.is_symlink():
                continue
            for commit_dir in skill_dir.iterdir():
                if (
                    not commit_dir.is_dir()
                    and not commit_dir.is_symlink()
                ):
                    continue
                if (
                    skill_dir.name,
                    commit_dir.name,
                ) in runtime_references:
                    continue
                targets.append(
                    _MaterializationTarget(
                        target_class="80-removal",
                        identifier=(
                            f"runtime/{skill_dir.name}/"
                            f"{commit_dir.name}"
                        ),
                        live_path=commit_dir,
                        kind="entry",
                    )
                )

    command_names = _active_command_names(nodes)
    expected_shims = {
        shims.shim_path(project_bin, name)
        for name in command_names
    }
    for name in sorted(command_names):
        targets.append(
            _MaterializationTarget(
                target_class="30-shim-canonical",
                identifier=name,
                live_path=shims.shim_path(project_bin, name),
                kind="entry",
            )
        )
    if project_bin.exists():
        for child in project_bin.iterdir():
            if (
                (child.is_file() or child.is_symlink())
                and child not in expected_shims
            ):
                targets.append(
                    _MaterializationTarget(
                        target_class="80-removal",
                        identifier=f"shim/{child.name}",
                        live_path=child,
                        kind="entry",
                    )
                )

    for name in ("env.ps1", "env.sh"):
        targets.append(
            _MaterializationTarget(
                target_class="50-env-file",
                identifier=name,
                live_path=project_root / ".agents" / name,
            )
        )

    project_context_names = tuple(
        sorted(
            node.name
            for node in nodes
            if (
                node.context_active
                and node.name not in hybrid_store_names
            )
        )
    )
    hybrid_context_names = tuple(
        sorted(
            node.name
            for node in nodes
            if node.context_active and node.name in hybrid_store_names
        )
    )
    adapter_groups = [
        adapters.AdapterGroup(
            canonical_root=project_skills,
            skill_names=project_context_names,
        ),
        adapters.AdapterGroup(
            canonical_root=hybrid_skills,
            skill_names=hybrid_context_names,
        ),
    ]
    adapter_targets = adapters.plan_project_adapter_targets(
        project_root,
        agents,
        adapter_groups,
    )
    targets.extend(
        _MaterializationTarget(
            target_class=target.target_class,
            identifier=target.identifier,
            live_path=target.live_path,
            kind=target.kind,
        )
        for target in adapter_targets
    )
    targets.append(
        _MaterializationTarget(
            target_class="90-consumer",
            identifier=consumers.REGISTRY_NAME,
            live_path=consumers.registry_path(home),
        )
    )
    keys = [_target_key(target) for target in targets]
    if len(keys) != len(set(keys)):
        raise InstallError("materialization plan contains duplicate targets")
    return tuple(targets), adapter_targets


def _stale_entry_targets(
    root: Path,
    expected: set[str],
    *,
    identifier_prefix: str,
) -> list[_MaterializationTarget]:
    targets: list[_MaterializationTarget] = []
    if not root.exists():
        return targets
    for child in root.iterdir():
        if child.name.startswith("."):
            if _is_dead_install_orphan(child):
                targets.append(
                    _MaterializationTarget(
                        target_class="80-removal",
                        identifier=(
                            f"{identifier_prefix}/orphan/{child.name}"
                        ),
                        live_path=child,
                        kind="entry",
                    )
                )
            continue
        if child.name in expected:
            continue
        if not child.is_dir() and not child.is_symlink():
            continue
        targets.append(
            _MaterializationTarget(
                target_class="80-removal",
                identifier=f"{identifier_prefix}/{child.name}",
                live_path=child,
                kind="entry",
            )
        )
    return targets


def _is_dead_install_orphan(path: Path) -> bool:
    match = _INSTALL_ORPHAN_RE.fullmatch(path.name)
    return (
        match is not None
        and not locking._pid_alive(int(match.group(1)))
    )


def _runtime_references_for_plan(
    config: GlobalConfig,
    project_root: Path,
    nodes: list[closure.ClosureNode],
    retained_hybrid_names: set[str],
) -> set[tuple[str, str]]:
    references = {
        (node.name, node.resolved.commit)
        for node in nodes
    }
    references.update(
        _marker_references(
            config.path.parent / "global" / "skills"
        )
    )
    references.update(
        _marker_references(
            hybrid.hybrid_skills_root(config.path.parent),
            only=retained_hybrid_names,
        )
    )
    current = project_root.resolve()
    seen = {current}
    for configured in config.projects.values():
        resolved = configured.path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        references.update(
            _marker_references(
                resolved / ".agents" / "skills"
            )
        )
    for consumer in consumers.load_consumers(config.path.parent):
        resolved = consumer.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        references.update(
            _marker_references(
                resolved / ".agents" / "skills"
            )
        )
    return references


def _target_key(target: _MaterializationTarget) -> tuple[str, str]:
    return target.target_class, target.identifier


def _capture_target_preimages(
    targets: tuple[_MaterializationTarget, ...],
) -> dict[tuple[str, str], str]:
    return {
        _target_key(target): transactions.digest_target(
            target.live_path,
            kind=target.kind,
        )
        for target in targets
    }


def _assert_target_preimages_current(
    targets: tuple[_MaterializationTarget, ...],
    expected: Mapping[tuple[str, str], str],
) -> None:
    for target in targets:
        key = _target_key(target)
        if (
            transactions.digest_target(
                target.live_path,
                kind=target.kind,
            )
            != expected[key]
        ):
            raise _concurrent_state_change(
                f"materialization target changed before commit: "
                f"{target.target_class}/{target.identifier}"
            )


def _build_private_misses(
    config: GlobalConfig,
    nodes: list[closure.ClosureNode],
    providers: tuple[build_planner.BuildProvider, ...],
    plans: tuple[build_planner.BuildPlan, ...],
    operator_search_path: build_toolchain.OperatorSearchPath,
    cache_backend: build_cache.BuildCacheBackend,
    stack: ExitStack,
    *,
    operation_roots: tuple[Path, ...],
) -> dict[str, build_cache.CachePublication]:
    candidates = [
        plan
        for plan in plans
        if plan.inspection.status is not build_cache.CacheEntryStatus.HIT
    ]
    for plan in candidates:
        if plan.inspection.status is build_cache.CacheEntryStatus.UNSUPPORTED:
            raise InstallError(
                f"{plan.driver} cache is unavailable: "
                f"{plan.inspection.reason}"
            )
    if not candidates:
        return {}

    private_base = Path(
        stack.enter_context(
            tempfile.TemporaryDirectory(prefix="csk-build-operation-")
        )
    )
    forbidden = tuple(
        path
        for path in (
            config.path.parent,
            *operation_roots,
            config.skills_root,
            *(node.repo for node in nodes),
            *(provider.snapshot.path for provider in providers),
        )
        if path.exists()
    )
    session = stack.enter_context(
        build_toolchain.establish_toolchain(
            build_toolchain.ToolchainConfig(
                private_base=private_base,
                operator_search_path=operator_search_path,
                forbidden_roots=forbidden,
            )
        )
    )
    for plan in plans:
        if (
            session.target != plan.input.target
            or session.toolchain != plan.input.toolchain
        ):
            raise _concurrent_state_change(
                "the selected Go toolchain changed between planning and build"
            )

    providers_by_name = {provider.name: provider for provider in providers}
    publications: dict[str, build_cache.CachePublication] = {}
    for plan in candidates:
        provider = providers_by_name.get(plan.provider)
        if provider is None:
            raise _concurrent_state_change(
                f"build provider disappeared before build: {plan.provider}"
            )
        with locking.BuildLock(config.path.parent, plan.cache_key):
            inspection = cache_backend.inspect(
                build_cache.CacheExpectation(input=plan.input)
            )
            if inspection.status is build_cache.CacheEntryStatus.HIT:
                continue
            if inspection.status is build_cache.CacheEntryStatus.UNSUPPORTED:
                raise InstallError(
                    f"{plan.driver} cache is unavailable: "
                    f"{inspection.reason}"
                )
            command = next(
                (
                    candidate
                    for candidate in provider.commands
                    if candidate.name == plan.command
                ),
                None,
            )
            if command is None:
                raise _concurrent_state_change(
                    f"build command disappeared before build: "
                    f"{plan.provider}.{plan.command}"
                )

            def run_build(
                frozen: build_source.FrozenSnapshot,
                command_spec: build_planner.BuildCommand = command,
            ) -> go_v1.BuildResult:
                return go_v1.build(
                    go_v1.BuildRequest(
                        toolchain_session=session,
                        source_snapshot=frozen,
                        command_object={
                            "type": "build",
                            "driver": command_spec.driver,
                            "source_dir": command_spec.source_dir,
                        },
                        build_root=command_spec.build_root,
                        source_dir=command_spec.source_dir,
                        command=command_spec.name,
                    )
                )

            built = provider.snapshot.use(run_build)
            artifact = built.artifact
            if artifact.metadata.path != plan.input.artifact_path:
                raise InstallError(
                    f"{plan.provider}.{plan.command} produced artifact "
                    f"{artifact.metadata.path!r}, expected "
                    f"{plan.input.artifact_path!r}"
                )
            # The artifact was produced by the compiler inside the manager's
            # private operation root, but only POSIX gives it the private,
            # owner-controlled state publication requires.
            build_cache.make_publication_source_private(artifact.staged_path)
            receipt = build_metadata.build_receipt(
                plan.input,
                build_metadata.BuildArtifact(
                    path=artifact.metadata.path,
                    sha256=artifact.metadata.sha256,
                    size=artifact.metadata.size,
                ),
            )
            publications[plan.cache_key] = build_cache.CachePublication(
                input=plan.input,
                receipt_bytes=build_metadata.canonical_receipt_bytes(
                    receipt
                ),
                artifact_source=artifact.staged_path,
            )
    return publications


def _revalidate_closure(
    nodes: list[closure.ClosureNode],
    providers: tuple[build_planner.BuildProvider, ...],
) -> None:
    by_name = {node.name: node for node in nodes}
    if len(by_name) != len(nodes):
        raise _concurrent_state_change(
            "dependency closure ownership changed before commit"
        )
    for node in nodes:
        resolved = git_ops.resolve_ref(
            node.repo,
            node.decl.ref.kind,
            node.decl.ref.value,
        )
        if resolved.commit != node.resolved.commit:
            raise _concurrent_state_change(
                f"resolved source changed before commit: {node.name}"
            )
    for provider in providers:
        provider_node = by_name.get(provider.name)
        if provider_node is None:
            raise _concurrent_state_change(
                f"build provider disappeared before commit: {provider.name}"
            )
        provider.snapshot.recheck()
        expected_commands = _active_local_build_command_names(provider_node)
        if {command.name for command in provider.commands} != expected_commands:
            raise _concurrent_state_change(
                f"build activation changed before commit: {provider.name}"
            )
    closure.detect_active_command_collisions(nodes)
    build_planner.detect_command_collisions(
        providers,
        occupied=_active_script_owners(nodes),
    )


def _publish_planned_builds(
    csk_home: Path,
    plans: tuple[build_planner.BuildPlan, ...],
    publications: Mapping[str, build_cache.CachePublication],
    cache_backend: build_cache.BuildCacheBackend,
    home_lock: locking.ManagerHomeLock,
) -> dict[str, dict[str, _PublishedBuild]]:
    published: dict[str, dict[str, _PublishedBuild]] = {}
    for plan in plans:
        expectation = build_cache.CacheExpectation(input=plan.input)
        inspection = cache_backend.inspect(expectation)
        if inspection.status is not build_cache.CacheEntryStatus.HIT:
            publication = publications.get(plan.cache_key)
            if publication is None:
                raise _concurrent_state_change(
                    f"cache winner changed before commit: "
                    f"{plan.provider}.{plan.command}"
                )
            cache_backend.publish(publication, guard=home_lock)
            inspection = cache_backend.inspect(expectation)
        if (
            inspection.status is not build_cache.CacheEntryStatus.HIT
            or inspection.receipt is None
            or inspection.receipt_sha256 is None
            or inspection.artifact_path is None
        ):
            raise InstallError(
                f"{plan.driver} cache did not yield a verified winner for "
                f"{plan.provider}.{plan.command}: {inspection.reason}"
            )
        receipt = inspection.receipt
        marker = install_marker.InstallMarkerBuild(
            driver=plan.driver,
            cache_key=plan.cache_key,
            receipt_sha256=inspection.receipt_sha256,
            artifact_sha256=receipt.artifact.sha256,
            artifact_path=receipt.artifact.path,
        )
        published.setdefault(plan.provider, {})[plan.command] = (
            _PublishedBuild(
                plan=plan,
                inspection=inspection,
                marker=marker,
            )
        )
    return published


def _active_command_names(nodes: list[closure.ClosureNode]) -> set[str]:
    commands: set[str] = set()
    for node in nodes:
        commands.update(node.active_commands())
        commands.update(_active_build_command_names(node))
    return commands


def _commit_materialization(
    config: GlobalConfig,
    project: ProjectConfig,
    options: InstallOptions,
    *,
    nodes: list[closure.ClosureNode],
    hybrid_decls: list[hybrid.HybridDecl],
    hybrid_store_names: set[str],
    agents: list[str],
    effective_locale: str | None,
    mcp_found: Mapping[str, dict[str, list[str]]],
    registry_attest: Mapping[str, dict[str, object]],
    published_builds: Mapping[str, Mapping[str, _PublishedBuild]],
    materialization_targets: tuple[_MaterializationTarget, ...],
    adapter_targets: tuple[adapters.AdapterTarget, ...],
    target_preimages: Mapping[tuple[str, str], str],
    expected_generation: Mapping[str, str],
    engine: transactions.TransactionEngine,
    home_lock: locking.ManagerHomeLock,
) -> list[str]:
    physical_project_parent = project.path.resolve(strict=False).parent
    staging_parents = tuple(
        dict.fromkeys((physical_project_parent, config.path.parent))
    )
    with ExitStack() as staging_stack:
        staging_root: Path | None = None
        staging_errors: list[str] = []
        for parent in staging_parents:
            try:
                temporary = staging_stack.enter_context(
                    tempfile.TemporaryDirectory(
                        prefix=".csk-materialization-plan-",
                        dir=parent,
                    )
                )
            except OSError as exc:
                staging_errors.append(f"{parent}: {exc}")
                continue
            staging_root = Path(temporary)
            break
        if staging_root is None:
            raise InstallError(
                "cannot create private materialization staging: "
                + "; ".join(staging_errors)
            )
        desired, messages = _stage_materialization(
            staging_root,
            config,
            project,
            options,
            nodes=nodes,
            hybrid_decls=hybrid_decls,
            hybrid_store_names=hybrid_store_names,
            agents=agents,
            effective_locale=effective_locale,
            mcp_found=mcp_found,
            registry_attest=registry_attest,
            published_builds=published_builds,
            materialization_targets=materialization_targets,
            adapter_targets=adapter_targets,
        )
        _commit_transaction_targets(
            transaction_prefix="install",
            project_identity=locking.canonical_project_identity(
                project.path
            ),
            materialization_targets=materialization_targets,
            desired=desired,
            target_preimages=target_preimages,
            expected_generation=expected_generation,
            engine=engine,
            home_lock=home_lock,
        )
        return messages


def _commit_transaction_targets(
    *,
    transaction_prefix: str,
    project_identity: str,
    materialization_targets: tuple[_MaterializationTarget, ...],
    desired: Mapping[tuple[str, str], Path | None],
    target_preimages: Mapping[tuple[str, str], str],
    expected_generation: Mapping[str, str],
    engine: transactions.TransactionEngine,
    home_lock: locking.ManagerHomeLock,
) -> None:
    targets: list[transactions.MutableTarget] = []
    for target in materialization_targets:
        key = _target_key(target)
        wanted = desired[key]
        expected = target_preimages[key]
        wanted_digest = (
            transactions.ABSENT_DIGEST
            if wanted is None
            else transactions.digest_target(
                wanted,
                kind=target.kind,
            )
        )
        if wanted_digest == expected:
            continue
        targets.append(
            transactions.MutableTarget(
                target_class=target.target_class,
                identifier=target.identifier,
                live_path=target.live_path,
                desired_path=wanted,
                expected_preimage_digest=expected,
                kind=target.kind,
            )
        )
    if not targets:
        return
    identity_hash = hashlib.sha256(
        project_identity.encode("utf-8")
    ).hexdigest()[:16]
    transaction_id = (
        f"{transaction_prefix}-{identity_hash}-{uuid.uuid4().hex}"
    )
    plan = transactions.TransactionPlan(
        transaction_id=transaction_id,
        project_identity=project_identity,
        targets=tuple(targets),
        generation_digests=dict(expected_generation),
    )
    created: list[Path] = []
    committed = False
    try:
        for committed_target in targets:
            if committed_target.desired_path is None:
                continue
            created.extend(
                _make_missing_directories(
                    committed_target.live_path.parent
                )
            )
        engine.prepare(home_lock, plan)
        engine.commit(home_lock, transaction_id)
        committed = True
    finally:
        if not committed:
            _remove_created_directories(created)


def _stage_materialization(
    staging_root: Path,
    config: GlobalConfig,
    project: ProjectConfig,
    options: InstallOptions,
    *,
    nodes: list[closure.ClosureNode],
    hybrid_decls: list[hybrid.HybridDecl],
    hybrid_store_names: set[str],
    agents: list[str],
    effective_locale: str | None,
    mcp_found: Mapping[str, dict[str, list[str]]],
    registry_attest: Mapping[str, dict[str, object]],
    published_builds: Mapping[str, Mapping[str, _PublishedBuild]],
    materialization_targets: tuple[_MaterializationTarget, ...],
    adapter_targets: tuple[adapters.AdapterTarget, ...],
) -> tuple[
    dict[tuple[str, str], Path | None],
    list[str],
]:
    staged_project = staging_root / "project"
    staged_home = staging_root / "home"
    staged_project.mkdir()
    staged_home.mkdir()
    desired: dict[tuple[str, str], Path | None] = {}
    _copy_live_directory(
        project.path / ".agents",
        staged_project / ".agents",
    )
    _copy_live_directory(
        config.path.parent / "hybrid",
        staged_home / "hybrid",
    )
    _copy_live_directory(
        config.path.parent / "runtime",
        staged_home / "runtime",
    )

    staged_hybrid_store = hybrid.hybrid_skills_root(staged_home)
    final_hybrid_store = hybrid.hybrid_skills_root(config.path.parent)
    final_project_bin = project.path / ".agents" / "bin"
    expected_commands: set[str] = set()
    messages: list[str] = []
    nodes_by_name = {node.name: node for node in nodes}

    for node in nodes:
        plan = SkillPlan(
            decl=node.decl,
            resolved=node.resolved,
            repo=node.repo,
            snapshot=node.snapshot,
            spec=node.spec,
        )
        active_scripts = node.active_commands()
        active_builds = _active_build_command_names(node)
        active = active_scripts | active_builds
        command_names = install_runtime_commands(
            staged_home,
            staged_project / ".agents" / "bin",
            plan,
            only=active_scripts,
            activation_home=config.path.parent,
            activation_bin_dir=final_project_bin,
        )
        provider_builds = dict(published_builds.get(node.name, {}))
        if set(provider_builds) != active_builds:
            raise _concurrent_state_change(
                f"published build set changed before materialization: "
                f"{node.name}"
            )
        marker_builds = {
            name: build.marker
            for name, build in sorted(provider_builds.items())
        }
        build_source_identity: build_source.BuildSourceIdentity | None = None
        for name in sorted(provider_builds):
            published = provider_builds[name]
            external = (
                isinstance(
                    published.marker, install_marker.InstallMarkerBuildV3
                )
                and published.marker.driver
                == build_repository_model.GO_REPOSITORY_V1_DRIVER
            )
            identity = (
                published.plan.input.build_source
                if published.plan is not None
                else published.build_source_identity
            )
            if identity is None:
                raise InstallError(
                    f"build provider {node.name}.{name} has no source identity"
                )
            if not external and (
                build_source_identity is not None
                and identity != build_source_identity
            ):
                raise InstallError(
                    f"build provider {node.name} has inconsistent source "
                    "identities"
                )
            if not external:
                build_source_identity = identity
            command = node.spec.commands[name]
            if external:
                assert isinstance(
                    published.marker, install_marker.InstallMarkerBuildV3
                )
                if published.receipt_bytes is None or published.artifact_path is None:
                    raise InstallError(
                        f"external build {node.name}.{name} has incomplete publication state"
                    )
                activation = shims.select_external_build_activation(
                    csk_home=config.path.parent,
                    command=command,
                    marker_build=published.marker,
                    receipt_bytes=published.receipt_bytes,
                    artifact_path=published.artifact_path,
                )
            else:
                if (
                    not isinstance(
                        published.marker, install_marker.InstallMarkerBuild
                    )
                    or published.inspection is None
                ):
                    raise InstallError(
                        f"local build {node.name}.{name} has incomplete publication state"
                    )
                activation = shims.select_build_activation(
                    csk_home=config.path.parent,
                    command=command,
                    marker_build=published.marker,
                    inspection=published.inspection,
                )
            shims.write_project_build_shim(
                staged_project,
                activation,
                path_entries=_runtime_path_entries(
                    plan,
                    final_project_bin,
                ),
            )
            command_names.add(name)
        expected_commands.update(command_names)

        marker_activation = {
            "context": node.context_active,
            "commands": sorted(active),
        }
        requirers = node.consumers()
        is_hybrid = node.name in hybrid_store_names
        if node.context_active and is_hybrid:
            installed = _install_skill_context_to_root(
                staged_hybrid_store,
                plan,
                config.preferred_locale,
                [],
                activation=marker_activation,
                requirers=requirers,
                substituted=node.substituted,
                builds=marker_builds,
                build_source_identity=build_source_identity,
            )
        elif node.context_active:
            installed = _install_skill_context(
                staged_project,
                plan,
                effective_locale,
                agents,
                activation=marker_activation,
                requirers=requirers,
                substituted=node.substituted,
                mcp_servers=mcp_found.get(node.name),
                attestation=registry_attest.get(node.name),
                builds=marker_builds,
                build_source_identity=build_source_identity,
            )
        else:
            installed = _install_marker_only(
                staged_project,
                plan,
                activation=marker_activation,
                requirers=requirers,
                substituted=node.substituted,
                mcp_servers=mcp_found.get(node.name),
                target_root=(
                    staged_hybrid_store if is_hybrid else None
                ),
                attestation=registry_attest.get(node.name),
                builds=marker_builds,
                build_source_identity=build_source_identity,
            )
        suffix = " (hybrid)" if is_hybrid else ""
        messages.append(
            f"{project.alias}: {_node_summary(node)}{suffix} {installed}"
        )
        if options.verbose:
            messages.append(
                f"{project.alias}: {node.name} commit "
                f"{node.resolved.commit}"
            )
            for command_name in sorted(command_names):
                messages.append(
                    f"{project.alias}: {node.name} command "
                    f"{command_name} -> .agents/bin/{command_name}"
                )

    _cleanup_removed_skills(
        staged_project,
        set(nodes_by_name) - hybrid_store_names,
    )
    all_hybrid_names = {item.decl.name for item in hybrid_decls}
    _cleanup_removed_skills_root(
        staged_hybrid_store,
        all_hybrid_names | hybrid_store_names,
    )
    shims.remove_stale_shims(staged_project, expected_commands)
    env_files.write_env_files(staged_project)
    _prune_staged_runtime(
        config,
        project.path,
        staged_home / "runtime",
        staged_project / ".agents" / "skills",
        staged_hybrid_store,
    )
    desired.update(
        adapters.stage_project_adapter_targets(
            staging_root / "adapters",
            adapter_targets,
            source_roots={
                project.path / ".agents" / "skills": (
                    staged_project / ".agents" / "skills"
                ),
                final_hybrid_store: staged_hybrid_store,
            },
            mode=config.adapter_mode,
        )
    )

    consumer_target = next(
        target
        for target in materialization_targets
        if target.target_class == "90-consumer"
    )
    consumer_path = staged_home / consumers.REGISTRY_NAME
    desired[_target_key(consumer_target)] = consumer_path
    consumer_path.write_bytes(
        consumers.encode_consumers(
            _desired_consumers(
                config.path.parent,
                project.path,
                staged_project / ".agents" / "skills",
            )
        )
    )
    for target in materialization_targets:
        key = _target_key(target)
        if key in desired:
            continue
        if target.target_class == "10-context":
            scope, name = target.identifier.split("/", 1)
            desired[key] = (
                staged_project / ".agents" / "skills" / name
                if scope == "project"
                else staged_hybrid_store / name
            )
        elif target.target_class == "20-runtime":
            name, commit = target.identifier.split("/", 1)
            desired[key] = (
                staged_home / "runtime" / name / commit
            )
        elif target.target_class == "30-shim-canonical":
            desired[key] = shims.shim_path(
                staged_project / ".agents" / "bin",
                target.identifier,
            )
        elif target.target_class == "50-env-file":
            desired[key] = (
                staged_project / ".agents" / target.identifier
            )
        elif target.target_class == "80-removal":
            desired[key] = None
        else:
            raise AssertionError(
                "materialization target has no staged state: "
                f"{target.target_class}/{target.identifier}"
            )
    return desired, messages


def _copy_live_directory(source: Path, destination: Path) -> None:
    try:
        info = source.lstat()
    except FileNotFoundError:
        destination.mkdir(parents=True)
        return
    if not stat.S_ISDIR(info.st_mode) or source.is_symlink():
        raise InstallError(
            f"managed materialization root is not a real directory: "
            f"{source}"
        )
    shutil.copytree(source, destination, symlinks=True)


def _make_missing_directories(path: Path) -> list[Path]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        parent = path.parent
        created = (
            _make_missing_directories(parent)
            if parent != path
            else []
        )
        try:
            path.mkdir(mode=0o755)
        except FileExistsError:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
                raise InstallError(
                    f"materialization parent is not a real directory: "
                    f"{path}"
                )
            return created
        return [*created, path]
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise InstallError(
            f"materialization parent is not a real directory: {path}"
        )
    return []


def _remove_created_directories(created: list[Path]) -> None:
    for path in reversed(created):
        try:
            path.rmdir()
        except OSError:
            pass


def _marker_references(
    skills_root: Path,
    *,
    only: set[str] | None = None,
) -> set[tuple[str, str]]:
    references: set[tuple[str, str]] = set()
    if not skills_root.exists():
        return references
    for marker_path in skills_root.glob("*/.csk-install.json"):
        if only is not None and marker_path.parent.name not in only:
            continue
        marker = _read_marker(marker_path)
        if marker is None:
            continue
        name = marker.get("name")
        commit = marker.get("commit")
        if isinstance(name, str) and isinstance(commit, str):
            references.add((name, commit))
    return references


def _desired_consumers(
    csk_home: Path,
    project_root: Path,
    staged_project_skills: Path,
) -> list[Path]:
    current = project_root.resolve()
    desired: set[Path] = set()
    for candidate in consumers.load_consumers(csk_home):
        resolved = candidate.resolve()
        if resolved == current:
            continue
        if (
            resolved.exists()
            and _marker_references(
                resolved / ".agents" / "skills"
            )
        ):
            desired.add(resolved)
    if _marker_references(staged_project_skills):
        desired.add(current)
    return sorted(desired, key=lambda path: str(path).encode("utf-8"))


def _prune_staged_runtime(
    config: GlobalConfig,
    project_root: Path,
    staged_runtime: Path,
    staged_project_skills: Path,
    staged_hybrid_skills: Path,
) -> None:
    references = _marker_references(
        config.path.parent / "global" / "skills"
    )
    references.update(_marker_references(staged_hybrid_skills))
    references.update(_marker_references(staged_project_skills))
    current = project_root.resolve()
    seen_projects = {current}
    for configured in config.projects.values():
        resolved = configured.path.resolve()
        if resolved in seen_projects:
            continue
        seen_projects.add(resolved)
        references.update(
            _marker_references(
                resolved / ".agents" / "skills"
            )
        )
    for consumer in consumers.load_consumers(config.path.parent):
        resolved = consumer.resolve()
        if resolved in seen_projects or not resolved.exists():
            continue
        seen_projects.add(resolved)
        references.update(
            _marker_references(
                resolved / ".agents" / "skills"
            )
        )
    if not staged_runtime.exists():
        return
    for skill_dir in list(staged_runtime.iterdir()):
        if not skill_dir.is_dir() or skill_dir.is_symlink():
            continue
        for commit_dir in list(skill_dir.iterdir()):
            if (
                commit_dir.is_dir()
                and not commit_dir.is_symlink()
                and (skill_dir.name, commit_dir.name) not in references
            ):
                shutil.rmtree(commit_dir)
        if not any(skill_dir.iterdir()):
            skill_dir.rmdir()


def _migration_warnings(project_alias: str, plans: list[SkillPlan]) -> list[str]:
    warnings: list[str] = []
    for plan in plans:
        if any(dependency.type == "skill" for dependency in plan.spec.dependencies.values()):
            warnings.append(
                f"{project_alias}: {plan.decl.name} uses dependencies.commands with type 'skill'; "
                "migrate to agent-skill.json schema v4 dependencies.skills"
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
        try:
            spec = skillspec.load_skill_spec(snap)
        except skillspec.SkillSpecError as exc:
            raise InstallError(
                f"Invalid skill manifest for {decl.name} "
                f"{resolved.kind} {resolved.ref}: {exc}"
            ) from exc
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
    result: _MessageResult,
    *,
    alias: str,
    read_only: bool = False,
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
    state_dir = config.path.parent / "state" / "registry"
    if not read_only:
        try:
            audit_registry.migrate_snapshot_states(cache_dir, state_dir)
        except audit_registry.RegistryError as exc:
            raise InstallError(
                f"audit registry rollback state migration failed: {exc}"
            ) from exc
    unavailable, snapshot_warnings = audit_registry.check_snapshots(
        registries,
        state_dir,
        fetch_snapshot=audit_registry.http_get_snapshot,
        now=time.time(),
        max_age_seconds=config.audit.snapshot_max_age_seconds,
        clock_skew_seconds=config.audit.snapshot_clock_skew_seconds,
        read_only=read_only,
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
        read_only=read_only,
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


def _runtime_path_entries(plan: SkillPlan, bin_dir: Path) -> tuple[Path, ...]:
    candidates = [bin_dir.absolute()]
    if sys.executable:
        candidates.append(Path(sys.executable).resolve().parent)
    for dependency in _system_dependencies(plan):
        if not dependency.command:
            continue
        executable = shutil.which(dependency.command)
        if executable:
            candidates.append(Path(executable).resolve().parent)

    entries: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        entries.append(candidate)
    return tuple(entries)


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


def install_runtime_commands(
    csk_home: Path,
    bin_dir: Path,
    plan: SkillPlan,
    *,
    only: set[str] | None = None,
    activation_home: Path | None = None,
    activation_bin_dir: Path | None = None,
) -> set[str]:
    commands: set[str] = set()
    final_home = csk_home if activation_home is None else activation_home
    final_bin = bin_dir if activation_bin_dir is None else activation_bin_dir
    path_entries = _runtime_path_entries(plan, final_bin)
    active_scripts = tuple(
        command
        for command in plan.spec.commands.values()
        if command.type == "script" and (only is None or command.name in only)
    )
    if plan.spec.runtime_roots:
        shims.install_runtime_roots(
            csk_home=csk_home,
            skill_name=plan.decl.name,
            commit=plan.resolved.commit,
            snapshot=plan.snapshot,
            runtime_roots=plan.spec.runtime_roots,
            required_commands=active_scripts,
        )
    for command in active_scripts:
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
        if activation_home is not None:
            runtime_path = final_home / runtime_path.relative_to(csk_home)
        shims.write_bin_shim(
            bin_dir,
            command.name,
            runtime_path,
            path_entries=path_entries,
        )
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
    builds: Mapping[str, install_marker.MarkerBuild] | None = None,
    build_source_identity: build_source.BuildSourceIdentity | None = None,
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
        builds=builds,
        build_source_identity=build_source_identity,
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
    builds: Mapping[str, install_marker.MarkerBuild] | None = None,
    build_source_identity: build_source.BuildSourceIdentity | None = None,
) -> str:
    target = target_root / plan.decl.name
    marker = _read_marker(target / ".csk-install.json")
    if _marker_is_current(
        marker, target, plan, effective_locale, agents,
        activation=activation, substituted=substituted, mcp_servers=mcp_servers, attestation=attestation,
        builds=builds,
        build_source_identity=build_source_identity,
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
        build_roots=plan.spec.build_roots,
    )
    locale.render_locale(
        plan.snapshot,
        tmp,
        effective_locale,
        exclude_roots=plan.spec.build_roots,
    )
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
        builds=builds,
        build_source_identity=build_source_identity,
    )
    (tmp / ".csk-install.json").write_bytes(install_marker.serialize_install_marker(marker_data))
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
    builds: Mapping[str, install_marker.MarkerBuild] | None = None,
    build_source_identity: build_source.BuildSourceIdentity | None = None,
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
        builds=builds,
        build_source_identity=build_source_identity,
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
        builds=builds,
        build_source_identity=build_source_identity,
    )
    (tmp / ".csk-install.json").write_bytes(install_marker.serialize_install_marker(marker_data))
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
    builds: Mapping[str, install_marker.MarkerBuild] | None = None,
    build_source_identity: build_source.BuildSourceIdentity | None = None,
) -> dict[str, object]:
    marker_activation: install_marker.MarkerActivation | None = None
    if activation is not None:
        activation_commands = activation.get("commands", [])
        if not isinstance(activation_commands, list) or not all(
            isinstance(command, str) for command in activation_commands
        ):
            raise InstallError("marker activation.commands must be a list of strings")
        context = activation.get("context")
        if not isinstance(context, bool):
            raise InstallError("marker activation.context must be a boolean")
        marker_activation = install_marker.MarkerActivation(
            context=context,
            commands=tuple(activation_commands),
        )
    marker_attestation: install_marker.MarkerAttestation | None = None
    if attestation is not None:
        registry = attestation.get("registry")
        status = attestation.get("status")
        key_id = attestation.get("key_id")
        if (
            not isinstance(registry, str)
            or not isinstance(status, str)
            or (key_id is not None and not isinstance(key_id, str))
        ):
            raise InstallError("marker attestation is invalid")
        marker_attestation = install_marker.MarkerAttestation(
            registry=registry,
            status=status,
            key_id=key_id,
        )
    common: dict[str, Any] = dict(
        name=plan.decl.name,
        source=plan.decl.source,
        ref_kind=plan.resolved.kind,
        ref=plan.resolved.ref,
        commit=plan.resolved.commit,
        content_sha256=content_hash,
        locale=effective_locale,
        agents=tuple(agents),
        commands=tuple(
            command.name
            for command in plan.spec.commands.values()
            if command.type in {"script", "build"}
        ),
        dependencies=tuple(plan.spec.dependencies),
        skill_schema_version=plan.spec.schema_version,
        runtime_roots=plan.spec.runtime_roots,
        installed_at=(
            datetime.now(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        files=tuple(files),
        git=plan.decl.git,
        requirements=(
            tuple(plan.spec.requirements)
            if plan.spec.requirements
            else None
        ),
        mcp_servers=(
            {
                name: tuple(found)
                for name, found in mcp_servers.items()
            }
            if mcp_servers is not None
            else None
        ),
        attestation=marker_attestation,
        activation=marker_activation,
        requirers=tuple(requirers) if requirers else None,
        substituted=substituted,
    )
    if plan.spec.schema_version == 7:
        marker_builds: dict[str, install_marker.InstallMarkerBuildV3] = {}
        for name, build in (builds or {}).items():
            if isinstance(build, install_marker.InstallMarkerBuildV3):
                marker_builds[name] = build
            else:
                marker_builds[name] = install_marker.InstallMarkerBuildV3(
                    driver=build.driver,
                    receipt_schema_version=1,
                    execution_policy=build_metadata.PORTABLE_EXECUTION_POLICY,
                    cache_key=build.cache_key,
                    receipt_sha256=build.receipt_sha256,
                    artifact_sha256=build.artifact_sha256,
                    artifact_path=build.artifact_path,
                )
        marker: install_marker.InstallMarker = install_marker.InstallMarkerV3(
            **common,
            build_roots=plan.spec.build_roots,
            builds=marker_builds,
            build_source=build_source_identity,
        )
    else:
        local_builds = {
            name: build
            for name, build in (builds or {}).items()
            if isinstance(build, install_marker.InstallMarkerBuild)
        }
        if len(local_builds) != len(builds or {}):
            raise InstallError("external marker state requires skill schema 7")
        marker = install_marker.InstallMarkerV2(
            **common,
            build_roots=plan.spec.build_roots,
            builds=local_builds,
            build_source=build_source_identity,
        )
    return marker.to_json()


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
    builds: Mapping[str, install_marker.MarkerBuild] | None = None,
    build_source_identity: build_source.BuildSourceIdentity | None = None,
) -> bool:
    if not marker or not target.exists():
        return False
    schema_version = marker.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version
        not in install_marker.SUPPORTED_INSTALL_MARKER_SCHEMA_VERSIONS
    ):
        raise InstallError(
            f"Unsupported installed marker schema in "
            f"{target / '.csk-install.json'}"
        )
    try:
        typed_marker = install_marker.parse_install_marker(marker)
    except install_marker.InstallMarkerError:
        return False
    if not install_marker.marker_can_be_current(
        typed_marker,
        skill_schema_version=plan.spec.schema_version,
    ):
        return False
    if marker.get("ref_kind") != plan.resolved.kind or marker.get("ref") != plan.resolved.ref:
        return False
    if marker.get("commit") != plan.resolved.commit:
        return False
    if marker.get("locale") != locale_value:
        return False
    if marker.get("agents") != sorted(set(agents)):
        return False
    expected_commands = sorted(
        {
            command.name
            for command in plan.spec.commands.values()
            if command.type in {"script", "build"}
        }
    )
    if marker.get("commands") != expected_commands:
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
    expected_builds = {
        name: build.to_json()
        for name, build in sorted((builds or {}).items())
    }
    if isinstance(typed_marker, install_marker.InstallMarkerV1):
        if (
            plan.spec.build_roots
            or expected_builds
            or build_source_identity is not None
        ):
            return False
    else:
        if marker.get("build_roots") != sorted(
            set(plan.spec.build_roots)
        ):
            return False
        if marker.get("builds") != expected_builds:
            return False
        expected_source: dict[str, str] | None = None
        if build_source_identity is not None:
            expected_source = {
                "algorithm": build_source_identity.algorithm,
                "content_sha256": (
                    build_source_identity.content_sha256
                ),
            }
        if marker.get("build_source") != expected_source:
            return False
    if _installed_context_exposes_build_roots(marker, target, plan.spec.build_roots):
        return False
    actual_hash = hashing.content_sha256(target)
    return marker.get("content_sha256") == actual_hash


def _installed_context_exposes_build_roots(
    marker: dict[str, object],
    target: Path,
    build_roots: tuple[str, ...],
) -> bool:
    if not build_roots:
        return False

    marker_files = marker.get("files")
    if not isinstance(marker_files, list):
        return True
    for relative in marker_files:
        if not isinstance(relative, str):
            return True
        if whitelist.is_below_excluded_root(Path(relative), build_roots):
            return True

    for build_root in build_roots:
        installed_root = target.joinpath(*build_root.split("/"))
        try:
            installed_root.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        return True
    return False


def _read_marker(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        data = protocol_json.loads(path.read_bytes())
    except (OSError, protocol_json.ProtocolJSONError):
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
