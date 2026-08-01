from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import (
    closure,
    dev_substitutions,
    git_ops,
    hashing,
    hybrid,
    install_marker,
    installer,
    manifest,
    snapshot,
)
from .builds import cache as build_cache
from .builds import currentness as build_currentness
from .builds import go_v1
from .builds import planner as build_planner
from .builds import source as build_source
from .builds import toolchain as build_toolchain
from .config import GlobalConfig, ProjectConfig


@dataclass(frozen=True)
class SkillStatus:
    name: str
    ref_kind: str
    ref: str
    installed_commit: str | None
    resolved_commit: str | None
    label: str
    detail: str | None = None


@dataclass(frozen=True)
class ProjectStatus:
    alias: str
    path: Path
    skillfile_present: bool
    skills: list[SkillStatus]
    substitutions: tuple[str, ...] = ()
    builds: tuple[build_currentness.BuildStatus, ...] = ()
    errors: tuple[str, ...] = ()
    capability_evidence: Mapping[str, object] | None = None
    capability_evidence_error: str | None = None

    @property
    def clean(self) -> bool:
        return (
            self.skillfile_present
            and not self.errors
            and all(skill.label == "up-to-date" for skill in self.skills)
            and all(build.current for build in self.builds)
        )


@dataclass(frozen=True)
class _MarkerInspection:
    status: SkillStatus
    marker: install_marker.InstallMarker | None
    raw: bytes | None
    build_boundary_error: tuple[str, str] | None = None


def collect_status(
    config: GlobalConfig,
    *,
    alias: str | None = None,
) -> list[ProjectStatus]:
    statuses = [
        _collect_project_status(config, project)
        for project in _selected_projects(config, alias)
    ]
    return _attach_capability_evidence(statuses)


def collect_global_status(config: GlobalConfig) -> ProjectStatus:
    csk_home = config.path.parent
    root = csk_home / "global"
    global_manifest = manifest.load_manifest(root)
    if global_manifest is None:
        return ProjectStatus("global", root, False, [])
    status = _collect_scope(
        config,
        alias="global",
        path=root,
        scope_manifest=global_manifest,
        effective_manifest=global_manifest,
        substitutions={},
        substitution_lines=(),
        skills_root=root / "skills",
        bin_dir=root / "bin",
        hybrid_store_names=frozenset(),
        effective_locale=global_manifest.locale or config.preferred_locale,
        agents=global_manifest.agents or config.default_agents,
        generation_paths=(root / manifest.MANIFEST_NAME, root / "skills", root / "bin"),
    )
    return _attach_capability_evidence([status])[0]


def _collect_project_status(
    config: GlobalConfig,
    project: ProjectConfig,
) -> ProjectStatus:
    try:
        substitutions = dev_substitutions.load_substitutions(project.path)
        substitution_lines = tuple(
            f"{item.name} -> {item.describe()}"
            for item in substitutions.values()
        )
    except dev_substitutions.DevSubstitutionError as exc:
        substitutions = {}
        substitution_lines = (f"error: {exc}",)

    project_manifest = manifest.load_manifest(project.path)
    if project_manifest is None:
        return ProjectStatus(
            project.alias,
            project.path,
            False,
            [],
            substitution_lines,
        )

    errors: list[str] = []
    csk_home = config.path.parent
    try:
        hybrid_decls = hybrid.load_hybrid_decls(csk_home)
    except hybrid.HybridError as exc:
        hybrid_decls = []
        errors.append(str(exc))
    aliases = tuple(
        value
        for value in (
            project.alias,
            project.project_alias,
            project_manifest.project_alias,
        )
        if value
    )
    applicable = [
        item
        for item in hybrid_decls
        if hybrid.applies_to_project(
            item,
            aliases=aliases,
            project_path=project.path,
        )
    ]
    declared = {item.name for item in project_manifest.skills}
    hybrid_direct = [
        item.decl for item in applicable if item.decl.name not in declared
    ]
    effective_manifest = (
        replace(
            project_manifest,
            skills=[*project_manifest.skills, *hybrid_direct],
        )
        if hybrid_direct
        else project_manifest
    )

    # Determine the shared hybrid-store boundary with the same graph rule the
    # installer uses.  The actual set is recomputed after closure expansion.
    result = _collect_scope(
        config,
        alias=project.alias,
        path=project.path,
        scope_manifest=project_manifest,
        effective_manifest=effective_manifest,
        substitutions=substitutions,
        substitution_lines=substitution_lines,
        skills_root=project.path / ".agents" / "skills",
        bin_dir=project.path / ".agents" / "bin",
        hybrid_store_names=None,
        effective_locale=project_manifest.locale or config.preferred_locale,
        agents=project_manifest.agents or project.agents or config.default_agents,
        generation_paths=(
            project.path / manifest.MANIFEST_NAME,
            project.path / dev_substitutions.DEV_MANIFEST_NAME,
            project.path / ".agents" / "skills",
            project.path / ".agents" / "bin",
            csk_home / "hybrid" / manifest.MANIFEST_NAME,
            csk_home / "hybrid" / "skills",
        ),
        project_declared=declared,
    )
    if errors:
        result = replace(result, errors=(*result.errors, *errors))
    return result


def _collect_scope(
    config: GlobalConfig,
    *,
    alias: str,
    path: Path,
    scope_manifest: manifest.ProjectManifest,
    effective_manifest: manifest.ProjectManifest,
    substitutions: dict[str, dev_substitutions.Substitution],
    substitution_lines: tuple[str, ...],
    skills_root: Path,
    bin_dir: Path,
    hybrid_store_names: frozenset[str] | None,
    effective_locale: str | None,
    agents: list[str],
    generation_paths: tuple[Path, ...],
    project_declared: set[str] | None = None,
) -> ProjectStatus:
    csk_home = config.path.parent
    try:
        with ExitStack() as stack:
            nodes = closure.build_closure(
                config,
                effective_manifest,
                substitutions,
                use_cache=True,
                fetch_existing=False,
                stack=stack,
                read_only=True,
            )
            if hybrid_store_names is None:
                hybrid_store_names = frozenset(
                    installer._hybrid_store_names(
                        nodes,
                        project_declared or set(),
                    )
                )
            return _collect_resolved_scope(
                config,
                alias=alias,
                path=path,
                scope_manifest=scope_manifest,
                nodes=nodes,
                skills_root=skills_root,
                bin_dir=bin_dir,
                hybrid_store_names=hybrid_store_names,
                effective_locale=effective_locale,
                agents=agents,
                substitution_lines=substitution_lines,
                generation_paths=generation_paths,
                stack=stack,
            )
    except Exception as exc:  # noqa: BLE001 - status must preserve per-scope diagnostics
        skills = [
            _basic_skill_status(config, skills_root, decl)
            for decl in scope_manifest.skills
        ]
        skills = [
            replace(skill, label="error", detail=str(exc))
            if skill.label == "up-to-date"
            else skill
            for skill in skills
        ]
        builds = _unavailable_recorded_builds(
            skills_root,
            scope_manifest.skills,
            detail=str(exc),
        )
        return ProjectStatus(
            alias=alias,
            path=path,
            skillfile_present=True,
            skills=skills,
            substitutions=substitution_lines,
            builds=builds,
            errors=(str(exc),),
        )


def _collect_resolved_scope(
    config: GlobalConfig,
    *,
    alias: str,
    path: Path,
    scope_manifest: manifest.ProjectManifest,
    nodes: list[closure.ClosureNode],
    skills_root: Path,
    bin_dir: Path,
    hybrid_store_names: frozenset[str],
    effective_locale: str | None,
    agents: list[str],
    substitution_lines: tuple[str, ...],
    generation_paths: tuple[Path, ...],
    stack: ExitStack,
) -> ProjectStatus:
    csk_home = config.path.parent
    cache_backend = build_cache.cache_for_manager_home(csk_home)
    providers: list[build_planner.BuildProvider] = []
    provider_identities: dict[str, build_source.BuildSourceIdentity] = {}
    provider_errors: dict[str, tuple[str, str]] = {}
    for node in nodes:
        active = installer._active_build_command_names(node)
        if not active:
            continue
        persistent_snapshot = snapshot.snapshot_dir(
            csk_home,
            node.decl.source,
            node.resolved.commit,
        )
        if node.snapshot != persistent_snapshot:
            provider_errors[node.name] = (
                "build-source-unavailable",
                f"required raw snapshot is missing: {persistent_snapshot}",
            )
            continue
        try:
            frozen = stack.enter_context(
                build_source.freeze_snapshot(node.snapshot)
            )
            provider = build_planner.provider_from_spec(
                node.name,
                frozen,
                node.spec,
                active_commands=active,
            )
        except build_planner.BuildPlanningError as exc:
            provider_errors[node.name] = (_planning_label(exc.code), str(exc))
        except Exception as exc:  # noqa: BLE001 - raw snapshot failures are status rows
            provider_errors[node.name] = (
                "build-source-unavailable",
                str(exc),
            )
        else:
            providers.append(provider)
            provider_identities[node.name] = frozen.identity

    planned: dict[tuple[str, str], build_planner.BuildPlan] = {}
    if providers:
        try:
            operator_path = build_toolchain.capture_operator_search_path()
            probe = build_planner.FilesystemGenerationProbe(
                (
                    config.path,
                    csk_home / "builds",
                    *generation_paths,
                    *(node.snapshot for node in nodes),
                )
            )
            plans = build_planner.plan_builds(
                providers,
                manager_home=csk_home,
                operator_search_path=operator_path,
                forbidden_roots=(
                    path,
                    config.skills_root,
                    *(node.repo for node in nodes),
                ),
                cache_backend=cache_backend,
                generation_probe=probe,
                expected_generation=probe.capture(),
                max_generation_attempts=1,
            )
            planned = {(plan.provider, plan.command): plan for plan in plans}
        except build_planner.BuildPlanningError as exc:
            for provider in providers:
                provider_errors[provider.name] = (
                    _planning_label(exc.code),
                    str(exc),
                )
        except Exception as exc:  # noqa: BLE001 - toolchain status is non-current
            for provider in providers:
                provider_errors[provider.name] = (
                    "unusable-build-toolchain",
                    str(exc),
                )

    statuses: list[SkillStatus] = []
    build_statuses: list[build_currentness.BuildStatus] = []
    direct_names = {decl.name for decl in scope_manifest.skills}
    for node in nodes:
        node_skills_root = (
            csk_home / "hybrid" / "skills"
            if node.name in hybrid_store_names
            else skills_root
        )
        installed_dir = node_skills_root / node.name
        expected_identity = provider_identities.get(node.name)
        marker_inspection = _inspect_node_marker(
            node,
            installed_dir,
            runtime_dir=(
                csk_home
                / "runtime"
                / node.name
                / node.resolved.commit
            ),
            effective_locale=(
                config.preferred_locale
                if node.name in hybrid_store_names and node.context_active
                else effective_locale
                if node.context_active
                else None
            ),
            agents=(
                []
                if node.name in hybrid_store_names or not node.context_active
                else agents
            ),
            expected_build_source=expected_identity,
        )
        node_builds = _node_build_statuses(
            config,
            node,
            marker_inspection,
            planned,
            provider_errors,
            cache_backend,
            bin_dir,
        )
        if (
            marker_inspection.raw is not None
            and any(build.current for build in node_builds)
            and not _marker_bytes_unchanged(
                installed_dir / ".csk-install.json",
                marker_inspection.raw,
            )
        ):
            node_builds = tuple(
                replace(
                    build,
                    label="build-state-changed",
                    detail="install marker changed during read-only status",
                )
                if build.current
                else build
                for build in node_builds
            )
        skill_status = marker_inspection.status
        if any(not build.current for build in node_builds) and skill_status.label == "up-to-date":
            first = next(build for build in node_builds if not build.current)
            skill_status = replace(
                skill_status,
                label="build-drift",
                detail=f"{first.command}: {first.label}: {first.detail}",
            )
        # Direct declarations retain their familiar order; closure-only
        # providers follow and remain visible because their builds and markers
        # are equally capable of making the installed closure non-current.
        statuses.append(skill_status)
        build_statuses.extend(node_builds)

    statuses.sort(
        key=lambda item: (
            0 if item.name in direct_names else 1,
            item.name.encode("utf-8"),
        )
    )
    build_statuses.sort(
        key=lambda item: (
            item.provider.encode("utf-8"),
            item.command.encode("utf-8"),
        )
    )
    return ProjectStatus(
        alias=alias,
        path=path,
        skillfile_present=True,
        skills=statuses,
        substitutions=substitution_lines,
        builds=tuple(build_statuses),
    )


def _inspect_node_marker(
    node: closure.ClosureNode,
    installed_dir: Path,
    *,
    runtime_dir: Path,
    effective_locale: str | None,
    agents: list[str],
    expected_build_source: build_source.BuildSourceIdentity | None,
) -> _MarkerInspection:
    marker_path = installed_dir / ".csk-install.json"
    try:
        raw = marker_path.read_bytes()
    except FileNotFoundError:
        return _MarkerInspection(
            SkillStatus(
                node.name,
                node.resolved.kind,
                node.resolved.ref,
                None,
                node.resolved.commit,
                "missing",
            ),
            None,
            None,
            ("missing-build-marker", f"install marker is missing: {marker_path}"),
        )
    except OSError as exc:
        detail = f"unreadable install marker {marker_path}: {exc}"
        return _MarkerInspection(
            SkillStatus(
                node.name,
                node.resolved.kind,
                node.resolved.ref,
                None,
                node.resolved.commit,
                "error",
                detail,
            ),
            None,
            None,
            ("build-marker-drift", detail),
        )
    try:
        marker = install_marker.read_install_marker(raw)
    except install_marker.InstallMarkerError as exc:
        detail = f"invalid install marker {marker_path}: {exc}"
        return _MarkerInspection(
            SkillStatus(
                node.name,
                node.resolved.kind,
                node.resolved.ref,
                None,
                node.resolved.commit,
                "error",
                detail,
            ),
            None,
            raw,
            ("build-marker-drift", detail),
        )

    base = SkillStatus(
        node.name,
        node.resolved.kind,
        node.resolved.ref,
        marker.commit,
        node.resolved.commit,
        "up-to-date",
    )
    if marker.commit != node.resolved.commit:
        return _MarkerInspection(
            replace(base, label="update-available"),
            marker,
            raw,
            ("build-input-drift", "installed commit differs from the selected ref"),
        )
    expected_activation = install_marker.MarkerActivation(
        context=node.context_active,
        commands=tuple(
            sorted(
                node.active_commands()
                | installer._active_build_command_names(node)
            )
        ),
    )
    expected_commands = tuple(
        sorted(
            command.name
            for command in node.spec.commands.values()
            if command.type in {"script", "build"}
        )
    )
    expected_requirements = (
        tuple(sorted(node.spec.requirements))
        if node.spec.requirements
        else None
    )
    expected_requirers = tuple(sorted(node.consumers())) or None
    common_current = (
        install_marker.marker_can_be_current(
            marker,
            skill_schema_version=node.spec.schema_version,
        )
        and marker.name == node.name
        and marker.source == node.decl.source
        and marker.ref_kind == node.resolved.kind
        and marker.ref == node.resolved.ref
        and marker.git == node.decl.git
        and marker.locale == effective_locale
        and marker.agents == tuple(sorted(set(agents)))
        and marker.commands == expected_commands
        and marker.dependencies == tuple(sorted(node.spec.dependencies))
        and marker.runtime_roots == tuple(sorted(node.spec.runtime_roots))
        and marker.requirements == expected_requirements
        and marker.activation == expected_activation
        and marker.requirers == expected_requirers
        and marker.substituted == node.substituted
    )
    if not common_current:
        return _MarkerInspection(
            replace(
                base,
                label="manifest-drift",
                detail="marker manifest or activation fields differ from the current closure",
            ),
            marker,
            raw,
            (
                "build-marker-drift",
                "marker manifest or activation fields differ from the current closure",
            ),
        )
    if installer._installed_context_exposes_build_roots(
        marker.to_json(),
        installed_dir,
        node.spec.build_roots,
    ):
        detail = "installed agent context exposes a declared build root"
        return _MarkerInspection(
            replace(base, label="build-drift", detail=detail),
            marker,
            raw,
            ("build-context-exposed", detail),
        )
    if _runtime_exposes_build_roots(runtime_dir, node.spec.build_roots):
        detail = "installed runtime exposes a declared build root"
        return _MarkerInspection(
            replace(base, label="build-drift", detail=detail),
            marker,
            raw,
            ("build-runtime-exposed", detail),
        )
    try:
        actual_hash = hashing.content_sha256(installed_dir)
        actual_files = _installed_files(installed_dir)
    except Exception as exc:  # noqa: BLE001 - unsafe installed state is drift
        detail = f"could not validate installed content {installed_dir}: {exc}"
        return _MarkerInspection(
            replace(base, label="error", detail=detail),
            marker,
            raw,
            ("build-context-exposed", detail),
        )
    if marker.content_sha256 != actual_hash or marker.files != actual_files:
        return _MarkerInspection(
            replace(
                base,
                label="content-drift",
                detail="installed bytes or marker file references differ",
            ),
            marker,
            raw,
        )

    if isinstance(marker, install_marker.InstallMarkerV1):
        if node.spec.build_roots or installer._active_build_command_names(node):
            detail = "marker v1 cannot describe build-enabled state"
            return _MarkerInspection(
                replace(base, label="build-drift", detail=detail),
                marker,
                raw,
                ("build-marker-drift", detail),
            )
        return _MarkerInspection(base, marker, raw)

    if marker.build_roots != tuple(sorted(node.spec.build_roots)):
        detail = "marker build_roots differ from the current static descriptor"
        return _MarkerInspection(
            replace(base, label="build-drift", detail=detail),
            marker,
            raw,
            ("build-context-drift", detail),
        )
    active_builds = installer._active_build_command_names(node)
    if active_builds:
        if expected_build_source is None:
            detail = "the selected raw snapshot could not be validated"
            return _MarkerInspection(
                replace(base, label="build-drift", detail=detail),
                marker,
                raw,
                ("build-source-unavailable", detail),
            )
        if marker.build_source != expected_build_source:
            detail = "marker build_source differs from the validated raw snapshot"
            return _MarkerInspection(
                replace(base, label="build-drift", detail=detail),
                marker,
                raw,
                ("build-source-drift", detail),
            )
    elif marker.build_source is not None or marker.builds:
        detail = "marker records builds that are not active in the current closure"
        return _MarkerInspection(
            replace(base, label="build-drift", detail=detail),
            marker,
            raw,
            ("build-command-drift", detail),
        )
    return _MarkerInspection(base, marker, raw)


def _node_build_statuses(
    config: GlobalConfig,
    node: closure.ClosureNode,
    marker_inspection: _MarkerInspection,
    planned: Mapping[tuple[str, str], build_planner.BuildPlan],
    provider_errors: Mapping[str, tuple[str, str]],
    cache_backend: build_cache.BuildCacheBackend,
    bin_dir: Path,
) -> tuple[build_currentness.BuildStatus, ...]:
    marker_builds: Mapping[str, install_marker.InstallMarkerBuild] = {}
    if isinstance(marker_inspection.marker, install_marker.InstallMarkerV2):
        marker_builds = marker_inspection.marker.builds
    active = installer._active_build_command_names(node)
    commands = sorted(active | set(marker_builds))
    plan_wrapper = installer.SkillPlan(
        decl=node.decl,
        resolved=node.resolved,
        repo=node.repo,
        snapshot=node.snapshot,
        spec=node.spec,
    )
    path_entries = installer._runtime_path_entries(plan_wrapper, bin_dir)
    result: list[build_currentness.BuildStatus] = []
    for name in commands:
        recorded = marker_builds.get(name)
        command = node.spec.commands.get(name)
        if command is None or command.type != "build":
            result.append(
                build_currentness.unavailable_status(
                    node.name,
                    name,
                    label="build-command-drift",
                    detail="marker records a command absent from the current build descriptor",
                    recorded=recorded,
                )
            )
            continue
        provider_error = provider_errors.get(node.name)
        if provider_error is not None:
            label, detail = provider_error
            result.append(
                build_currentness.unavailable_status(
                    node.name,
                    name,
                    label=label,
                    detail=detail,
                    recorded=recorded,
                )
            )
            continue
        result.append(
            build_currentness.classify_build(
                csk_home=config.path.parent,
                bin_dir=bin_dir,
                provider=node.name,
                command=command,
                plan=planned.get((node.name, name)),
                recorded=recorded,
                cache_backend=cache_backend,
                path_entries=path_entries,
                boundary_error=marker_inspection.build_boundary_error,
            )
        )
    return tuple(result)


def _installed_files(installed_dir: Path) -> tuple[str, ...]:
    files: list[str] = []
    for path in installed_dir.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"installed content contains a link: {path}")
        if stat.S_ISREG(info.st_mode):
            relative = path.relative_to(installed_dir).as_posix()
            if relative != ".csk-install.json":
                files.append(relative)
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"installed content contains a special entry: {path}")
    return tuple(sorted(files))


def _runtime_exposes_build_roots(
    runtime_dir: Path,
    build_roots: tuple[str, ...],
) -> bool:
    """Fail closed when excluded build input appears in installed runtime."""

    for build_root in build_roots:
        installed_root = runtime_dir.joinpath(*build_root.split("/"))
        try:
            installed_root.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        return True
    return False


def _marker_bytes_unchanged(path: Path, expected: bytes) -> bool:
    try:
        return path.read_bytes() == expected
    except OSError:
        return False


def _planning_label(code: str) -> str:
    if code == "unsupported_build_driver":
        return "unsupported-build-driver"
    if code in {"concurrent_state_change", "snapshot_mutated"}:
        return "build-state-changed"
    if "toolchain" in code:
        return "unusable-build-toolchain"
    return "build-planning-error"


def _attach_capability_evidence(
    statuses: list[ProjectStatus],
) -> list[ProjectStatus]:
    index = next(
        (position for position, item in enumerate(statuses) if item.builds),
        None,
    )
    if index is None:
        return statuses
    evidence, error = _capability_evidence()
    updated = list(statuses)
    updated[index] = replace(
        updated[index],
        capability_evidence=evidence,
        capability_evidence_error=error,
    )
    return updated


def _capability_evidence() -> tuple[Mapping[str, object] | None, str | None]:
    try:
        platform, probes = go_v1.probe_native_controls(go_v1.ResourceLimits())
        record = go_v1.evidence_from_applied(
            platform,
            probes,
            [
                probe.name
                for probe in probes
                if probe.availability == go_v1.AVAILABILITY_AVAILABLE
            ],
        )
        go_v1.validate_capability_evidence(record, platform, probes)
        return record.to_dict(), None
    except Exception as exc:  # noqa: BLE001 - evidence never changes currentness
        return None, str(exc)


def _basic_skill_status(
    config: GlobalConfig,
    skills_root: Path,
    decl: manifest.SkillDecl,
) -> SkillStatus:
    try:
        resolved = git_ops.resolve_ref(
            config.skills_root / decl.source,
            decl.ref.kind,
            decl.ref.value,
        )
    except Exception as exc:  # noqa: BLE001 - preserve resolution diagnostics
        return SkillStatus(
            decl.name,
            decl.ref.kind,
            decl.ref.value,
            None,
            None,
            "error",
            str(exc),
        )
    marker_path = skills_root / decl.name / ".csk-install.json"
    try:
        marker = install_marker.read_install_marker(marker_path.read_bytes())
    except FileNotFoundError:
        return SkillStatus(
            decl.name,
            decl.ref.kind,
            decl.ref.value,
            None,
            resolved.commit,
            "missing",
        )
    except Exception as exc:  # noqa: BLE001 - preserve marker diagnostics
        return SkillStatus(
            decl.name,
            decl.ref.kind,
            decl.ref.value,
            None,
            resolved.commit,
            "error",
            str(exc),
        )
    if marker.commit != resolved.commit:
        return SkillStatus(
            decl.name,
            decl.ref.kind,
            decl.ref.value,
            marker.commit,
            resolved.commit,
            "update-available",
        )
    try:
        actual_hash = hashing.content_sha256(marker_path.parent)
    except Exception as exc:  # noqa: BLE001 - unsafe installed state is drift
        return SkillStatus(
            decl.name,
            decl.ref.kind,
            decl.ref.value,
            marker.commit,
            resolved.commit,
            "error",
            str(exc),
        )
    label = "up-to-date" if marker.content_sha256 == actual_hash else "content-drift"
    return SkillStatus(
        decl.name,
        decl.ref.kind,
        decl.ref.value,
        marker.commit,
        resolved.commit,
        label,
    )


def _unavailable_recorded_builds(
    skills_root: Path,
    declarations: list[manifest.SkillDecl],
    *,
    detail: str,
) -> tuple[build_currentness.BuildStatus, ...]:
    result: list[build_currentness.BuildStatus] = []
    for decl in declarations:
        marker_path = skills_root / decl.name / ".csk-install.json"
        try:
            marker = install_marker.read_install_marker(marker_path.read_bytes())
        except Exception:  # noqa: BLE001 - the scope error is already reported
            continue
        if not isinstance(marker, install_marker.InstallMarkerV2):
            continue
        for name, recorded in marker.builds.items():
            result.append(
                build_currentness.unavailable_status(
                    decl.name,
                    name,
                    label="build-source-unavailable",
                    detail=detail,
                    recorded=recorded,
                )
            )
    return tuple(result)


def statuses_to_payload(statuses: list[ProjectStatus]) -> list[dict[str, Any]]:
    return [_project_to_payload(project) for project in statuses]


def global_status_to_payload(project: ProjectStatus) -> dict[str, Any]:
    return _project_to_payload(project)


def _project_to_payload(project: ProjectStatus) -> dict[str, Any]:
    return {
        "alias": project.alias,
        "builds": [build.to_json() for build in project.builds],
        "capability_evidence": (
            dict(project.capability_evidence)
            if project.capability_evidence is not None
            else None
        ),
        "capability_evidence_error": project.capability_evidence_error,
        "clean": project.clean,
        "errors": list(project.errors),
        "path": str(project.path),
        "skillfile_present": project.skillfile_present,
        "substitutions": list(project.substitutions),
        "skills": [
            {
                "detail": skill.detail,
                "installed_commit": skill.installed_commit,
                "label": skill.label,
                "name": skill.name,
                "ref": skill.ref,
                "ref_kind": skill.ref_kind,
                "resolved_commit": skill.resolved_commit,
            }
            for skill in project.skills
        ],
    }


def render_status(config: GlobalConfig, *, alias: str | None = None) -> str:
    return render_collected(collect_status(config, alias=alias))


def render_global_status(config: GlobalConfig) -> str:
    return render_global_collected(collect_global_status(config))


def render_global_collected(project: ProjectStatus) -> str:
    return _render_project_status(project, global_scope=True)


def render_collected(statuses: list[ProjectStatus]) -> str:
    return "\n".join(_render_project_status(project) for project in statuses)


def _render_project_status(
    project: ProjectStatus,
    *,
    global_scope: bool = False,
) -> str:
    lines = (
        [f"Global skills ({project.path})"]
        if global_scope
        else [f"Project {project.alias} ({project.path})"]
    )
    for substitution in project.substitutions:
        lines.append(f"  SUBSTITUTION {substitution}")
    if not project.skillfile_present:
        lines.append("  Skillfile.json missing")
        return "\n".join(lines)
    if not project.skills:
        lines.append("  no skills declared")
    for skill in project.skills:
        commit = (skill.installed_commit or "")[:7]
        suffix = ""
        if skill.label == "update-available" and skill.resolved_commit:
            suffix = f" -> {skill.resolved_commit[:7]}"
        if skill.detail:
            suffix += f" ({skill.detail})"
        lines.append(
            f"  {skill.name:<20} {skill.ref_kind:<8} {skill.ref:<12} "
            f"{commit:<7}  {skill.label}{suffix}"
        )
    for build in project.builds:
        key = build.recorded_cache_key or build.expected_cache_key or ""
        lines.append(
            f"  BUILD {build.provider}/{build.command:<20} {key}  "
            f"{build.label} ({build.detail})"
        )
    if project.capability_evidence is not None:
        lines.append(
            "  CAPABILITY "
            + json.dumps(
                dict(project.capability_evidence),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    elif project.capability_evidence_error is not None:
        lines.append(
            "  CAPABILITY unavailable (result-only; currentness unchanged): "
            + project.capability_evidence_error
        )
    for error in project.errors:
        lines.append(f"  ERROR {error}")
    return "\n".join(lines)


def _selected_projects(
    config: GlobalConfig,
    alias: str | None,
) -> list[ProjectConfig]:
    if alias is None:
        return list(config.projects.values())
    project = config.projects.get(alias)
    if project is None:
        raise ValueError(f"Unknown project alias: {alias}")
    return [project]
