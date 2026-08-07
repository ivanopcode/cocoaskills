from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import (
    adapters,
    closure,
    consumers,
    dev_substitutions,
    env_files,
    gc,
    git_admission,
    git_ops,
    global_bins,
    installer,
    install_marker,
    locking,
    manifest,
    protocol_json,
    shims,
    status,
    transactions,
)
from .audit import pipeline as audit_pipeline
from .builds import cache as build_cache
from .builds import planner as build_planner
from .builds import source as build_source
from .builds import toolchain as build_toolchain
from .config import DEFAULT_AGENTS, GlobalConfig


class GlobalInstallError(Exception):
    pass


@dataclass
class GlobalResult:
    status: str = "ok"
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    builds: list[build_planner.BuildPlan] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.errors)


def global_root(csk_home: Path) -> Path:
    return csk_home / "global"


def global_skillfile(csk_home: Path) -> Path:
    return global_root(csk_home) / manifest.MANIFEST_NAME


def global_skills_root(csk_home: Path) -> Path:
    return global_root(csk_home) / "skills"


def global_bin_dir(csk_home: Path) -> Path:
    return global_root(csk_home) / "bin"


@dataclass(frozen=True)
class RetainedGlobalSkills:
    """Installed global skills that a selective run must leave untouched.

    A selective run resolves only the requested closure, so every other
    installed skill is invisible to the plan.  Reconciliation removes what the
    plan does not claim, so the retained set is what keeps an unselected skill,
    its shims, its runtime tree and its adapter entries alive.
    """

    names: frozenset[str] = frozenset()
    context_names: tuple[str, ...] = ()
    commands: Mapping[str, str] = field(default_factory=dict)
    references: frozenset[tuple[str, str]] = frozenset()


def _select_declarations(
    global_manifest: manifest.ProjectManifest,
    only: Sequence[str] | None,
) -> list[manifest.SkillDecl]:
    """Narrow declared global skills to the requested names.

    Requirements of a selected skill still join the closure; only the direct
    declarations are filtered here.
    """

    if not only:
        return list(global_manifest.skills)
    requested = list(dict.fromkeys(only))
    declared = {decl.name: decl for decl in global_manifest.skills}
    unknown = [name for name in requested if name not in declared]
    if unknown:
        available = ", ".join(sorted(declared)) or "none"
        raise GlobalInstallError(
            "Global skill not declared: "
            + ", ".join(unknown)
            + f"\n  Declared global skills: {available}"
        )
    return [declared[name] for name in requested]


def _collect_retained(
    csk_home: Path,
    selected_names: Iterable[str],
) -> RetainedGlobalSkills:
    """Read the installed state of every global skill outside the selection."""

    skills_root = global_skills_root(csk_home)
    if not skills_root.exists():
        return RetainedGlobalSkills()
    selected = set(selected_names)
    names: set[str] = set()
    context_names: list[str] = []
    commands: dict[str, str] = {}
    references: set[tuple[str, str]] = set()
    for child in sorted(skills_root.iterdir()):
        if child.name.startswith(".") or child.name in selected:
            continue
        if not child.is_dir() and not child.is_symlink():
            continue
        # A directory with an unreadable marker is still retained: a selective
        # run must never delete state it did not resolve.
        names.add(child.name)
        marker = _read_installed_marker(child / ".csk-install.json")
        if marker is None:
            continue
        references.add((marker.name, marker.commit))
        activation = marker.activation
        active_commands = (
            activation.commands if activation is not None else marker.commands
        )
        if activation is None or activation.context:
            context_names.append(child.name)
        for command in active_commands:
            commands[command] = child.name
    return RetainedGlobalSkills(
        names=frozenset(names),
        context_names=tuple(sorted(context_names)),
        commands=commands,
        references=frozenset(references),
    )


def _read_installed_marker(path: Path) -> install_marker.InstallMarker | None:
    try:
        return install_marker.read_install_marker(path.read_bytes())
    except (OSError, install_marker.InstallMarkerError):
        return None


def _retained_requirers(
    csk_home: Path,
    nodes: list[closure.ClosureNode],
    retained: RetainedGlobalSkills,
) -> list[str]:
    """Retained skills that require a skill this run reinstalls.

    A selective run resolves only its own closure, so a retained consumer keeps
    a marker written against the provider commit that was current before this
    run.  The provider's live marker still names its requirers, which is where
    that edge is readable without resolving the consumer.
    """

    skills_root = global_skills_root(csk_home)
    stale: set[str] = set()
    for node in nodes:
        marker = _read_installed_marker(
            skills_root / node.name / ".csk-install.json"
        )
        if marker is None or not marker.requirers:
            continue
        stale.update(set(marker.requirers) & retained.names)
    return sorted(stale)


def _selection_messages(
    csk_home: Path,
    nodes: list[closure.ClosureNode],
    retained: RetainedGlobalSkills,
    only: Sequence[str] | None,
) -> list[str]:
    if not only:
        return []
    selected = sorted(node.name for node in nodes)
    messages = [
        "global: selective run over "
        + (", ".join(selected) or "no skills")
        + "; "
        + (
            f"{len(retained.names)} installed skill(s) left untouched"
            if retained.names
            else "nothing else is installed"
        )
    ]
    stale = _retained_requirers(csk_home, nodes, retained)
    if stale:
        messages.append(
            "global: warning: "
            + ", ".join(stale)
            + " require a skill this run reinstalled and were not revalidated;"
            " run 'csk global status' or a full 'csk global install'"
        )
    return messages


def _check_retained_command_collisions(
    nodes: list[closure.ClosureNode],
    retained: RetainedGlobalSkills,
) -> None:
    if not retained.commands:
        return
    for node in nodes:
        commands = node.active_commands() | installer._active_build_command_names(node)
        for name in sorted(commands):
            owner = retained.commands.get(name)
            if owner is not None:
                raise installer.InstallError(
                    f"Command collision for {name!r}: exported by {owner} "
                    f"(installed, outside this selective run) and {node.name}"
                )


def init(csk_home: Path, *, default_agents: list[str] | None = None) -> Path:
    root = global_root(csk_home)
    root.mkdir(parents=True, exist_ok=True)
    global_skills_root(csk_home).mkdir(parents=True, exist_ok=True)
    global_bin_dir(csk_home).mkdir(parents=True, exist_ok=True)
    env_files.write_global_env_files(csk_home)
    path = global_skillfile(csk_home)
    if not path.exists():
        agents = list(default_agents or DEFAULT_AGENTS)
        _write_json(path, {"schema_version": 1, "agents": agents, "skills": []})
    return path


def add_decl(
    csk_home: Path,
    *,
    name: str,
    ref_kind: str,
    ref: str,
    git: str | None = None,
    source: str | None = None,
    default_agents: list[str] | None = None,
) -> None:
    if not name:
        raise GlobalInstallError("global skill name must be non-empty")
    if ref_kind not in {"tag", "branch", "revision"}:
        raise GlobalInstallError("global skill must specify tag, branch, or revision")
    path = init(csk_home, default_agents=default_agents)
    data = _read_global_payload(path)
    skills = data.setdefault("skills", [])
    if not isinstance(skills, list):
        raise GlobalInstallError("Global Skillfile field 'skills' must be a list")
    decl: dict[str, str] = {"name": name, ref_kind: ref}
    if git:
        decl["git"] = git
    if source:
        decl["source"] = source
    replaced = False
    for index, existing in enumerate(skills):
        if isinstance(existing, dict) and existing.get("name") == name:
            skills[index] = decl
            replaced = True
            break
    if not replaced:
        skills.append(decl)
    manifest.parse_manifest(data, path)
    _write_json(path, data)


def remove_decl(csk_home: Path, name: str) -> None:
    path = global_skillfile(csk_home)
    if not path.exists():
        raise GlobalInstallError(f"Global Skillfile not found: {path}\n  Run 'csk global init' first.")
    data = _read_global_payload(path)
    skills = data.get("skills")
    if not isinstance(skills, list):
        raise GlobalInstallError("Global Skillfile field 'skills' must be a list")
    kept = [entry for entry in skills if not (isinstance(entry, dict) and entry.get("name") == name)]
    if len(kept) == len(skills):
        raise GlobalInstallError(f"Global skill not declared: {name}")
    data["skills"] = kept
    manifest.parse_manifest(data, path)
    _write_json(path, data)


def load_manifest(csk_home: Path) -> manifest.ProjectManifest:
    path = global_skillfile(csk_home)
    if not path.exists():
        raise GlobalInstallError(f"Global Skillfile not found: {path}\n  Run 'csk global init' first.")
    loaded = manifest.load_manifest(global_root(csk_home))
    if loaded is None:
        raise GlobalInstallError(f"Global Skillfile not found: {path}\n  Run 'csk global init' first.")
    return loaded


def list_declared(csk_home: Path) -> str:
    global_manifest = load_manifest(csk_home)
    lines = [f"Global Skillfile: {global_skillfile(csk_home)}"]
    if not global_manifest.skills:
        lines.append("  no skills declared")
    for decl in global_manifest.skills:
        lines.append(f"  {decl.name} ({decl.ref.kind} {decl.ref.value})")
    return "\n".join(lines)


def install(
    config: GlobalConfig,
    *,
    options: installer.InstallOptions | None = None,
    only: Sequence[str] | None = None,
) -> GlobalResult:
    options = options or installer.InstallOptions()
    operator_search_path = build_toolchain.capture_operator_search_path()
    operator_ssh_credentials = installer._capture_operator_ssh_credentials(options)
    generation_probe = _global_generation_probe(config)
    csk_home = config.path.parent
    attempts = 2 if options.dry_run else 3
    operation_lock = (
        ExitStack()
        if options.dry_run
        else locking.ProjectLock(csk_home, global_root(csk_home))
    )
    with operation_lock:
        for attempt in range(attempts):
            try:
                expected_generation = generation_probe.capture()
                result = _install_once(
                    config,
                    options=options,
                    operator_search_path=operator_search_path,
                    operator_ssh_credentials=operator_ssh_credentials,
                    generation_probe=generation_probe,
                    expected_generation=expected_generation,
                    only=only,
                )
                break
            except build_planner.BuildPlanningError as exc:
                if (
                    exc.code == "concurrent_state_change"
                    and attempt + 1 < attempts
                ):
                    continue
                result = GlobalResult(
                    status="failed",
                    errors=[str(exc)],
                )
                break
        else:
            raise AssertionError("unreachable global planning retry state")

    if not options.dry_run and not result.failed:
        try:
            with locking.ManagerHomeLock(csk_home) as home_lock:
                gc.collect_runtime(config, csk_home, guard=home_lock)
        except locking.LockOrderError:
            raise
        except locking.LockError as exc:
            result.messages.append(
                f"global: post-install garbage collection skipped: {exc}"
            )
    return result


def _transaction_engine(csk_home: Path) -> transactions.TransactionEngine:
    return transactions.TransactionEngine(csk_home)


def _install_once(
    config: GlobalConfig,
    *,
    options: installer.InstallOptions,
    operator_search_path: build_toolchain.OperatorSearchPath,
    operator_ssh_credentials: git_admission.OperatorSSHCredentials | None = None,
    generation_probe: build_planner.GenerationProbe,
    expected_generation: Mapping[str, str],
    only: Sequence[str] | None = None,
) -> GlobalResult:
    result = GlobalResult()
    csk_home = config.path.parent
    try:
        global_manifest = load_manifest(csk_home)
        agents = global_manifest.agents or config.default_agents
        effective_locale = global_manifest.locale or config.preferred_locale
        selected_manifest = replace(
            global_manifest,
            skills=_select_declarations(global_manifest, only),
        )
        with ExitStack() as stack:
            nodes = _build_nodes(
                config,
                selected_manifest,
                options=options,
                stack=stack,
                result=result,
            )
            # Only a selective run has anything to retain: a full run resolves
            # every declared skill, so its closure already covers the world.
            retained = (
                _collect_retained(csk_home, (node.name for node in nodes))
                if only
                else RetainedGlobalSkills()
            )
            result.messages.extend(
                _selection_messages(csk_home, nodes, retained, only)
            )
            plans = [
                installer.SkillPlan(
                    decl=node.decl,
                    resolved=node.resolved,
                    repo=node.repo,
                    snapshot=node.snapshot,
                    spec=node.spec,
                )
                for node in nodes
            ]
            validation_issues = installer._validate_skills(
                plans,
                effective_locale,
            )
            result.messages.extend(
                installer._skill_validation_warnings(
                    "global",
                    validation_issues,
                )
            )
            installer._check_skill_validation_errors(validation_issues)
            build_providers = installer._freeze_build_providers(nodes, stack)
            closure.detect_active_command_collisions(nodes)
            _check_retained_command_collisions(nodes, retained)
            build_planner.detect_command_collisions(
                build_providers,
                occupied={
                    **retained.commands,
                    **installer._active_script_owners(nodes),
                },
            )
            plans = _plans_with_available_dependencies(plans, result)
            if result.errors:
                result.status = "failed"
                _recheck_generation(generation_probe, expected_generation)
                return result

            mcp_found, mcp_warnings = installer._check_mcp_servers(
                plans,
                global_root(csk_home),
                agents,
                alias="global",
            )
            result.messages.extend(mcp_warnings)
            result.messages.extend(
                installer._migration_warnings("global", plans)
            )
            audit_gate = audit_pipeline.gate_plans(
                plans,
                config,
                scope="global",
                record=not options.dry_run,
            )
            result.messages.extend(audit_gate.warnings)
            if audit_gate.blocked:
                result.status = "failed"
                result.errors.extend(audit_gate.errors)
                return result
            registry_attest = installer._check_audit_registries(
                plans,
                config,
                result,
                alias="global",
                read_only=options.dry_run,
            )
            if not options.dry_run and (
                config.audit.enabled or config.trusted_registries()
            ):
                expected_generation = installer._generation_after_gate_writes(
                    config,
                    generation_probe,
                    expected_generation,
                )
            if options.strict_tags:
                installer._check_moved_tags_strict(global_skills_root(csk_home), plans)
            else:
                for warning in installer._moved_tag_warnings(global_skills_root(csk_home), plans):
                    result.messages.append(f"global: {warning}")

            cache_backend = build_cache.cache_for_manager_home(csk_home)
            result.builds.extend(
                build_planner.plan_builds(
                    build_providers,
                    manager_home=csk_home,
                    operator_search_path=operator_search_path,
                    forbidden_roots=(
                        global_root(csk_home),
                        config.skills_root,
                        *(node.repo for node in nodes),
                    ),
                    cache_backend=cache_backend,
                    generation_probe=generation_probe,
                    expected_generation=expected_generation,
                    max_generation_attempts=1,
                )
            )
            external_published, external_messages = installer._publish_external_builds(
                config,
                project_root=global_root(csk_home),
                nodes=nodes,
                substitutions=dev_substitutions.DevManifest(
                    schema_version=1,
                    substitutions={},
                    build_repository_substitutions={},
                ),
                operator_search_path=operator_search_path,
                ssh_credentials=operator_ssh_credentials,
                stack=stack,
                dry_run=options.dry_run,
                marker_roots=(global_skills_root(csk_home),),
            )
            result.messages.extend(external_messages)
            if options.dry_run:
                for build in result.builds:
                    payload = json.dumps(
                        build.to_json(),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    result.messages.append(f"global: build {payload}")
                for node in nodes:
                    result.messages.append(
                        f"global: {installer._node_summary(node)} (planned)"
                    )
                result.messages.append("global: dry-run; no files modified")
                return result

            (
                materialization_targets,
                adapter_targets,
                user_bin_targets,
                publication_messages,
            ) = _global_materialization_targets(
                config,
                nodes,
                agents,
                retained=retained,
            )
            target_preimages = installer._capture_target_preimages(
                materialization_targets
            )
            private_builds = installer._build_private_misses(
                config,
                nodes,
                build_providers,
                tuple(result.builds),
                operator_search_path,
                cache_backend,
                stack,
                operation_roots=(global_root(csk_home),),
            )

            with locking.ManagerHomeLock(csk_home) as home_lock:
                engine = _transaction_engine(csk_home)
                engine.recover(home_lock)
                installer._assert_generation_current(
                    generation_probe,
                    expected_generation,
                    scope="global",
                )
                installer._assert_target_preimages_current(
                    materialization_targets,
                    target_preimages,
                )
                installer._revalidate_closure(nodes, build_providers)
                published = installer._publish_planned_builds(
                    csk_home,
                    tuple(result.builds),
                    private_builds,
                    cache_backend,
                    home_lock,
                )
                for provider, commands in external_published.items():
                    published.setdefault(provider, {}).update(commands)
                messages = _commit_global_materialization(
                    config,
                    options,
                    nodes=nodes,
                    agents=agents,
                    effective_locale=effective_locale,
                    mcp_found=mcp_found,
                    registry_attest=registry_attest,
                    published_builds=published,
                    materialization_targets=materialization_targets,
                    adapter_targets=adapter_targets,
                    user_bin_targets=user_bin_targets,
                    target_preimages=target_preimages,
                    expected_generation=expected_generation,
                    engine=engine,
                    home_lock=home_lock,
                    retained=retained,
                )
            result.messages.extend(messages)
            result.messages.extend(publication_messages)
            return result
    except build_planner.BuildPlanningError as exc:
        if exc.code == "concurrent_state_change":
            raise
        result.status = "failed"
        result.errors.append(str(exc))
        return result
    except locking.LockError:
        # Keep manager-home coordination failures distinct from ordinary
        # global installation failures for the CLI EXIT_LOCK contract.
        raise
    except Exception as exc:  # noqa: BLE001 - global boundary reports stable failures
        result.status = "failed"
        result.errors.append(str(exc))
        return result


def _global_materialization_targets(
    config: GlobalConfig,
    nodes: list[closure.ClosureNode],
    agents: list[str],
    *,
    retained: RetainedGlobalSkills = RetainedGlobalSkills(),
) -> tuple[
    tuple[installer._MaterializationTarget, ...],
    tuple[adapters.AdapterTarget, ...],
    tuple[global_bins.UserBinTarget, ...],
    list[str],
]:
    adapters.warn_unknown_agents(agents)
    csk_home = Path(os.path.abspath(config.path.parent))
    skills_root = global_skills_root(csk_home)
    runtime_root = csk_home / "runtime"
    bin_dir = global_bin_dir(csk_home)
    targets: list[installer._MaterializationTarget] = []
    expected_names = {node.name for node in nodes}

    for name in sorted(expected_names):
        targets.append(
            installer._MaterializationTarget(
                target_class="10-context",
                identifier=f"global/{name}",
                live_path=skills_root / name,
                kind="entry",
            )
        )
    targets.extend(
        installer._stale_entry_targets(
            skills_root,
            expected_names | set(retained.names),
            identifier_prefix="context/global",
        )
    )

    for node in nodes:
        if not node.active_commands():
            continue
        targets.append(
            installer._MaterializationTarget(
                target_class="20-runtime",
                identifier=f"{node.name}/{node.resolved.commit}",
                live_path=(
                    runtime_root / node.name / node.resolved.commit
                ),
                kind="entry",
            )
        )
    runtime_references = _global_runtime_references_for_plan(
        config,
        nodes,
        retained=retained,
    )
    if runtime_root.exists():
        for skill_dir in runtime_root.iterdir():
            if not skill_dir.is_dir() or skill_dir.is_symlink():
                continue
            for commit_dir in skill_dir.iterdir():
                if not commit_dir.is_dir() and not commit_dir.is_symlink():
                    continue
                if (skill_dir.name, commit_dir.name) in runtime_references:
                    continue
                targets.append(
                    installer._MaterializationTarget(
                        target_class="80-removal",
                        identifier=(
                            f"runtime/{skill_dir.name}/{commit_dir.name}"
                        ),
                        live_path=commit_dir,
                        kind="entry",
                    )
                )

    command_names = installer._active_command_names(nodes)
    expected_shims = {
        shims.shim_path(bin_dir, name)
        for name in command_names | set(retained.commands)
    }
    for name in sorted(command_names):
        targets.append(
            installer._MaterializationTarget(
                target_class="30-shim-canonical",
                identifier=name,
                live_path=shims.shim_path(bin_dir, name),
                kind="entry",
            )
        )
    if bin_dir.exists():
        for child in bin_dir.iterdir():
            if (
                (child.is_file() or child.is_symlink())
                and child not in expected_shims
            ):
                targets.append(
                    installer._MaterializationTarget(
                        target_class="80-removal",
                        identifier=f"shim/{child.name}",
                        live_path=child,
                        kind="entry",
                    )
                )

    for name in ("env.ps1", "env.sh"):
        targets.append(
            installer._MaterializationTarget(
                target_class="50-env-file",
                identifier=name,
                live_path=global_root(csk_home) / name,
            )
        )

    context_names = tuple(
        sorted(
            {node.name for node in nodes if node.context_active}
            | set(retained.context_names)
        )
    )
    adapter_targets = adapters.plan_global_adapter_targets(
        csk_home,
        agents,
        context_names,
    )
    targets.extend(
        installer._MaterializationTarget(
            target_class=target.target_class,
            identifier=target.identifier,
            live_path=target.live_path,
            kind=target.kind,
        )
        for target in adapter_targets
    )

    user_bin_targets, publication_messages = (
        global_bins.plan_user_bin_targets(
            csk_home,
            command_names | set(retained.commands),
        )
    )
    targets.extend(
        installer._MaterializationTarget(
            target_class=target.target_class,
            identifier=target.identifier,
            live_path=target.live_path,
            kind=target.kind,
        )
        for target in user_bin_targets
    )
    keys = [installer._target_key(target) for target in targets]
    if len(keys) != len(set(keys)):
        raise installer.InstallError(
            "global materialization plan contains duplicate targets"
        )
    return (
        tuple(targets),
        adapter_targets,
        user_bin_targets,
        publication_messages,
    )


def _global_runtime_references_for_plan(
    config: GlobalConfig,
    nodes: list[closure.ClosureNode],
    *,
    retained: RetainedGlobalSkills = RetainedGlobalSkills(),
) -> set[tuple[str, str]]:
    references = {
        (node.name, node.resolved.commit) for node in nodes
    }
    references.update(retained.references)
    csk_home = config.path.parent
    references.update(
        installer._marker_references(csk_home / "hybrid" / "skills")
    )
    seen_projects: set[Path] = set()
    for configured in config.projects.values():
        resolved = configured.path.resolve()
        if resolved in seen_projects:
            continue
        seen_projects.add(resolved)
        references.update(
            installer._marker_references(
                resolved / ".agents" / "skills"
            )
        )
    for consumer in consumers.load_consumers(csk_home):
        resolved = consumer.resolve()
        if resolved in seen_projects or not resolved.exists():
            continue
        seen_projects.add(resolved)
        references.update(
            installer._marker_references(
                resolved / ".agents" / "skills"
            )
        )
    return references


def _commit_global_materialization(
    config: GlobalConfig,
    options: installer.InstallOptions,
    *,
    nodes: list[closure.ClosureNode],
    agents: list[str],
    effective_locale: str | None,
    mcp_found: Mapping[str, dict[str, list[str]]],
    registry_attest: Mapping[str, dict[str, object]],
    published_builds: Mapping[
        str, Mapping[str, installer._PublishedBuild]
    ],
    materialization_targets: tuple[
        installer._MaterializationTarget, ...
    ],
    adapter_targets: tuple[adapters.AdapterTarget, ...],
    user_bin_targets: tuple[global_bins.UserBinTarget, ...],
    target_preimages: Mapping[tuple[str, str], str],
    expected_generation: Mapping[str, str],
    engine: transactions.TransactionEngine,
    home_lock: locking.ManagerHomeLock,
    retained: RetainedGlobalSkills = RetainedGlobalSkills(),
) -> list[str]:
    csk_home = config.path.parent
    staging_parents = tuple(
        dict.fromkeys((Path.home().resolve(strict=False), csk_home))
    )
    with ExitStack() as staging_stack:
        staging_root: Path | None = None
        staging_errors: list[str] = []
        for parent in staging_parents:
            try:
                temporary = staging_stack.enter_context(
                    tempfile.TemporaryDirectory(
                        prefix=".csk-global-materialization-plan-",
                        dir=parent,
                    )
                )
            except OSError as exc:
                staging_errors.append(f"{parent}: {exc}")
                continue
            staging_root = Path(temporary)
            break
        if staging_root is None:
            raise installer.InstallError(
                "cannot create private global materialization staging: "
                + "; ".join(staging_errors)
            )
        desired, messages = _stage_global_materialization(
            staging_root,
            config,
            options,
            nodes=nodes,
            agents=agents,
            effective_locale=effective_locale,
            mcp_found=mcp_found,
            registry_attest=registry_attest,
            published_builds=published_builds,
            materialization_targets=materialization_targets,
            adapter_targets=adapter_targets,
            user_bin_targets=user_bin_targets,
            retained=retained,
        )
        installer._commit_transaction_targets(
            transaction_prefix="global-install",
            project_identity=locking.canonical_project_identity(
                global_root(csk_home)
            ),
            materialization_targets=materialization_targets,
            desired=desired,
            target_preimages=target_preimages,
            expected_generation=expected_generation,
            engine=engine,
            home_lock=home_lock,
        )
        return messages


def _stage_global_materialization(
    staging_root: Path,
    config: GlobalConfig,
    options: installer.InstallOptions,
    *,
    nodes: list[closure.ClosureNode],
    agents: list[str],
    effective_locale: str | None,
    mcp_found: Mapping[str, dict[str, list[str]]],
    registry_attest: Mapping[str, dict[str, object]],
    published_builds: Mapping[
        str, Mapping[str, installer._PublishedBuild]
    ],
    materialization_targets: tuple[
        installer._MaterializationTarget, ...
    ],
    adapter_targets: tuple[adapters.AdapterTarget, ...],
    user_bin_targets: tuple[global_bins.UserBinTarget, ...],
    retained: RetainedGlobalSkills = RetainedGlobalSkills(),
) -> tuple[dict[tuple[str, str], Path | None], list[str]]:
    csk_home = Path(os.path.abspath(config.path.parent))
    staged_home = staging_root / "home"
    staged_home.mkdir()
    installer._copy_live_directory(
        global_root(csk_home),
        global_root(staged_home),
    )
    installer._copy_live_directory(
        csk_home / "runtime",
        staged_home / "runtime",
    )
    staged_skills = global_skills_root(staged_home)
    final_skills = global_skills_root(csk_home)
    final_bin = global_bin_dir(csk_home)
    desired: dict[tuple[str, str], Path | None] = {}
    messages: list[str] = []
    expected_commands: set[str] = set()

    for node in nodes:
        plan = installer.SkillPlan(
            decl=node.decl,
            resolved=node.resolved,
            repo=node.repo,
            snapshot=node.snapshot,
            spec=node.spec,
        )
        active_scripts = node.active_commands()
        active_builds = installer._active_build_command_names(node)
        active = active_scripts | active_builds
        command_names = installer.install_runtime_commands(
            staged_home,
            global_bin_dir(staged_home),
            plan,
            only=active_scripts,
            activation_home=csk_home,
            activation_bin_dir=final_bin,
        )
        provider_builds = dict(published_builds.get(node.name, {}))
        if set(provider_builds) != active_builds:
            raise installer._concurrent_state_change(
                "published build set changed before global "
                f"materialization: {node.name}"
            )
        marker_builds = {
            name: build.marker
            for name, build in sorted(provider_builds.items())
        }
        build_source_identity: (
            build_source.BuildSourceIdentity | None
        ) = None
        for name in sorted(provider_builds):
            published = provider_builds[name]
            external = (
                isinstance(
                    published.marker, install_marker.InstallMarkerBuildV3
                )
                and published.marker.driver == "go-repository-v1"
            )
            identity = (
                published.plan.input.build_source
                if published.plan is not None
                else published.build_source_identity
            )
            if identity is None:
                raise installer.InstallError(
                    f"build provider {node.name}.{name} has no source identity"
                )
            if not external and (
                build_source_identity is not None
                and identity != build_source_identity
            ):
                raise installer.InstallError(
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
                    raise installer.InstallError(
                        f"external build {node.name}.{name} has incomplete publication state"
                    )
                activation = shims.select_external_build_activation(
                    csk_home=csk_home,
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
                    raise installer.InstallError(
                        f"local build {node.name}.{name} has incomplete publication state"
                    )
                activation = shims.select_build_activation(
                    csk_home=csk_home,
                    command=command,
                    marker_build=published.marker,
                    inspection=published.inspection,
                )
            shims.write_global_build_shim(
                staged_home,
                activation,
                path_entries=installer._runtime_path_entries(
                    plan,
                    final_bin,
                ),
            )
            command_names.add(name)
        expected_commands.update(command_names)

        marker_activation = {
            "context": node.context_active,
            "commands": sorted(active),
        }
        if node.context_active:
            installed = installer._install_skill_context_to_root(
                staged_skills,
                plan,
                effective_locale,
                agents,
                activation=marker_activation,
                requirers=node.consumers(),
                substituted=node.substituted,
                mcp_servers=mcp_found.get(node.name),
                attestation=registry_attest.get(node.name),
                builds=marker_builds,
                build_source_identity=build_source_identity,
            )
        else:
            installed = installer._install_marker_only(
                staged_home,
                plan,
                activation=marker_activation,
                requirers=node.consumers(),
                substituted=node.substituted,
                mcp_servers=mcp_found.get(node.name),
                target_root=staged_skills,
                attestation=registry_attest.get(node.name),
                builds=marker_builds,
                build_source_identity=build_source_identity,
            )
        messages.append(
            f"global: {installer._node_summary(node)} {installed}"
        )
        if options.verbose:
            messages.append(
                f"global: {node.name} commit {node.resolved.commit}"
            )
            for command_name in sorted(command_names):
                messages.append(
                    f"global: {node.name} command {command_name} "
                    f"-> global/bin/{command_name}"
                )

    installer._cleanup_removed_skills_root(
        staged_skills,
        {node.name for node in nodes} | set(retained.names),
    )
    shims.remove_stale_global_shims(
        staged_home,
        expected_commands | set(retained.commands),
    )
    env_files.write_global_env_files(
        staged_home,
        activation_home=csk_home,
    )
    _prune_staged_global_runtime(
        config,
        nodes,
        staged_home / "runtime",
        retained=retained,
    )
    desired.update(
        adapters.stage_project_adapter_targets(
            staging_root / "adapters",
            adapter_targets,
            source_roots={final_skills: staged_skills},
            mode=config.adapter_mode,
        )
    )
    desired.update(
        global_bins.stage_user_bin_targets(
            staging_root / "user-bin",
            user_bin_targets,
            csk_home=csk_home,
        )
    )

    for target in materialization_targets:
        key = installer._target_key(target)
        if key in desired:
            continue
        if target.target_class == "10-context":
            _, name = target.identifier.split("/", 1)
            desired[key] = staged_skills / name
        elif target.target_class == "20-runtime":
            name, commit = target.identifier.split("/", 1)
            desired[key] = staged_home / "runtime" / name / commit
        elif target.target_class == "30-shim-canonical":
            desired[key] = shims.shim_path(
                global_bin_dir(staged_home),
                target.identifier,
            )
        elif target.target_class == "50-env-file":
            desired[key] = global_root(staged_home) / target.identifier
        elif target.target_class == "80-removal":
            desired[key] = None
        else:
            raise AssertionError(
                "global materialization target has no staged state: "
                f"{target.target_class}/{target.identifier}"
            )
    return desired, messages


def _prune_staged_global_runtime(
    config: GlobalConfig,
    nodes: list[closure.ClosureNode],
    staged_runtime: Path,
    *,
    retained: RetainedGlobalSkills = RetainedGlobalSkills(),
) -> None:
    references = _global_runtime_references_for_plan(
        config,
        nodes,
        retained=retained,
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


def update(
    config: GlobalConfig,
    *,
    only: Sequence[str] | None = None,
) -> GlobalResult:
    result = GlobalResult()
    try:
        global_manifest = load_manifest(config.path.parent)
        for decl in _select_declarations(global_manifest, only):
            try:
                repo = installer._ensure_skill_repo(config, decl, use_persistent_clone=True, stack=None)
                git_ops.fetch_repo(repo)
                result.messages.append(f"fetched {decl.source}")
            except Exception as exc:  # noqa: BLE001 - report every per-source fetch failure
                result.errors.append(f"fetch failed {decl.source}: {exc}")
        return result
    except Exception as exc:  # noqa: BLE001 - global update boundary reports failures
        result.status = "failed"
        result.errors.append(str(exc))
        return result


def render_status(config: GlobalConfig) -> str:
    return status.render_global_status(config)



def _build_plans(
    config: GlobalConfig,
    global_manifest: manifest.ProjectManifest,
    *,
    options: installer.InstallOptions,
    stack: ExitStack,
    result: GlobalResult,
) -> list[installer.SkillPlan]:
    plans: list[installer.SkillPlan] = []
    for decl in global_manifest.skills:
        try:
            plans.extend(
                installer._build_plans(
                    config,
                    replace(global_manifest, skills=[decl]),
                    use_cache=not options.dry_run,
                    stack=stack,
                )
            )
        except Exception as exc:  # noqa: BLE001 - preserve per-source diagnostics
            result.errors.append(f"{decl.name}: {exc}")
    return plans


def _build_nodes(
    config: GlobalConfig,
    global_manifest: manifest.ProjectManifest,
    *,
    options: installer.InstallOptions,
    stack: ExitStack,
    result: GlobalResult,
) -> list[closure.ClosureNode]:
    def resolve(
        decls: list[manifest.SkillDecl],
    ) -> list[closure.ClosureNode]:
        return closure.build_closure(
            config,
            replace(global_manifest, skills=decls),
            {},
            use_cache=not options.dry_run,
            fetch_existing=options.fetch,
            fetched_repos=set(),
            stack=stack,
        )

    try:
        return resolve(global_manifest.skills)
    except Exception:
        available: list[manifest.SkillDecl] = []
        isolated_errors: list[str] = []
        for decl in global_manifest.skills:
            try:
                resolve([decl])
            except Exception as exc:  # noqa: BLE001 - preserve per-source diagnostics
                isolated_errors.append(f"{decl.name}: {exc}")
            else:
                available.append(decl)
        if not isolated_errors:
            raise
        result.errors.extend(isolated_errors)
        if not available:
            return []
        return resolve(available)


def _recheck_generation(
    generation_probe: build_planner.GenerationProbe | None,
    expected_generation: Mapping[str, str] | None,
) -> None:
    if generation_probe is None:
        return
    if expected_generation is None:
        raise ValueError("expected_generation is required with a generation probe")
    if dict(generation_probe.capture()) != dict(expected_generation):
        raise build_planner.BuildPlanningError(
            "concurrent_state_change",
            "shared planning state changed during the read-only build plan",
        )


def _global_generation_probe(
    config: GlobalConfig,
) -> build_planner.FilesystemGenerationProbe:
    csk_home = config.path.parent
    root = global_root(csk_home)
    user_home = Path.home()
    project_skill_roots = tuple(
        configured.path / ".agents" / "skills"
        for configured in config.projects.values()
    )
    consumer_skill_roots = tuple(
        consumer / ".agents" / "skills"
        for consumer in consumers.load_consumers(csk_home)
    )
    return build_planner.FilesystemGenerationProbe(
        (
            config.path,
            global_skillfile(csk_home),
            global_skills_root(csk_home),
            global_bin_dir(csk_home),
            root / "env.ps1",
            root / "env.sh",
            csk_home / "hybrid" / "skills",
            consumers.registry_path(csk_home),
            *project_skill_roots,
            *consumer_skill_roots,
            csk_home / "audit",
            csk_home / "builds",
            csk_home / "cache" / "registry",
            csk_home / "state" / "registry",
            root / ".mcp.json",
            root / ".cursor" / "mcp.json",
            root / ".codex" / "config.toml",
            root / ".gemini" / "settings.json",
            root / "opencode.json",
            root / "opencode.jsonc",
            root / ".claude" / "settings.json",
            root / ".claude" / "settings.local.json",
            user_home / ".claude.json",
            user_home / ".cursor" / "mcp.json",
            user_home / ".codex" / "config.toml",
            user_home / ".gemini" / "settings.json",
            user_home / ".codeium" / "windsurf" / "mcp_config.json",
            user_home / ".config" / "opencode" / "opencode.json",
            user_home / ".config" / "opencode" / "opencode.jsonc",
        )
    )


def _plans_with_available_dependencies(
    plans: list[installer.SkillPlan], result: GlobalResult
) -> list[installer.SkillPlan]:
    available: list[installer.SkillPlan] = []
    for plan in plans:
        try:
            installer._check_system_commands([plan])
        except installer.InstallError as exc:
            result.errors.append(str(exc))
            continue
        available.append(plan)

    while True:
        kept: list[installer.SkillPlan] = []
        removed = False
        for plan in available:
            errors = installer.skill_command_dependency_errors(plan, available)
            if errors:
                result.errors.extend(errors)
                removed = True
                continue
            kept.append(plan)
        available = kept
        if not removed:
            return available


def _read_global_payload(path: Path) -> dict[str, Any]:
    try:
        data = protocol_json.loads(path.read_bytes())
    except protocol_json.ProtocolJSONError as exc:
        raise GlobalInstallError(f"Malformed JSON in global Skillfile {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GlobalInstallError(f"Global Skillfile must contain a JSON object: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
