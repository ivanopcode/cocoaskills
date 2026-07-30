from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import adapters, closure, env_files, git_ops, global_bins, hashing, installer, manifest, protocol_json, shims
from .audit import pipeline as audit_pipeline
from .builds import planner as build_planner
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


def install(config: GlobalConfig, *, options: installer.InstallOptions | None = None) -> GlobalResult:
    options = options or installer.InstallOptions()
    operator_search_path = (
        build_toolchain.capture_operator_search_path()
        if options.dry_run
        else None
    )
    if not options.dry_run:
        return _install_once(
            config,
            options=options,
            operator_search_path=operator_search_path,
            generation_probe=None,
            expected_generation=None,
        )

    generation_probe = _global_generation_probe(config)
    for attempt in range(2):
        try:
            expected_generation = generation_probe.capture()
            return _install_once(
                config,
                options=options,
                operator_search_path=operator_search_path,
                generation_probe=generation_probe,
                expected_generation=expected_generation,
            )
        except build_planner.BuildPlanningError as exc:
            if exc.code != "concurrent_state_change" or attempt == 1:
                return GlobalResult(
                    status="failed",
                    errors=[str(exc)],
                )
    raise AssertionError("unreachable global planning retry state")


def _install_once(
    config: GlobalConfig,
    *,
    options: installer.InstallOptions,
    operator_search_path: build_toolchain.OperatorSearchPath | None,
    generation_probe: build_planner.GenerationProbe | None,
    expected_generation: Mapping[str, str] | None,
) -> GlobalResult:
    result = GlobalResult()
    csk_home = config.path.parent
    try:
        global_manifest = load_manifest(csk_home)
        agents = global_manifest.agents or config.default_agents
        effective_locale = global_manifest.locale or config.preferred_locale
        with ExitStack() as stack:
            nodes: list[closure.ClosureNode] = []
            build_providers: tuple[build_planner.BuildProvider, ...] = ()
            if options.dry_run:
                nodes = _build_nodes(
                    config,
                    global_manifest,
                    options=options,
                    stack=stack,
                    result=result,
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
                try:
                    closure.detect_active_command_collisions(nodes)
                    build_planner.detect_command_collisions(
                        build_providers,
                        occupied=installer._active_script_owners(nodes),
                    )
                except Exception as exc:
                    result.status = "failed"
                    result.errors.append(str(exc))
                    return result
            else:
                plans = _build_plans(
                    config,
                    global_manifest,
                    options=options,
                    stack=stack,
                    result=result,
                )
                try:
                    installer._detect_command_collisions(plans)
                except Exception as exc:
                    result.status = "failed"
                    result.errors.append(str(exc))
                    return result
            plans = _plans_with_available_dependencies(plans, result)
            if options.dry_run:
                available_names = {plan.decl.name for plan in plans}
                build_providers = tuple(
                    provider
                    for provider in build_providers
                    if provider.name in available_names
                )
                _, mcp_warnings = installer._check_mcp_servers(
                    plans,
                    global_root(csk_home),
                    agents,
                    alias="global",
                )
                result.messages.extend(mcp_warnings)
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
            if options.dry_run:
                installer._check_audit_registries(
                    plans,
                    config,
                    result,
                    alias="global",
                    read_only=True,
                )
            if options.strict_tags:
                installer._check_moved_tags_strict(global_skills_root(csk_home), plans)
            else:
                for warning in installer._moved_tag_warnings(global_skills_root(csk_home), plans):
                    result.messages.append(f"global: {warning}")
            if options.dry_run:
                if result.errors:
                    _recheck_generation(
                        generation_probe,
                        expected_generation,
                    )
                else:
                    if operator_search_path is None:
                        raise AssertionError(
                            "dry-run build planning requires an operator search path"
                        )
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
                            generation_probe=generation_probe,
                            expected_generation=expected_generation,
                            max_generation_attempts=1,
                        )
                    )
                for build in result.builds:
                    payload = json.dumps(
                        build.to_json(),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    result.messages.append(f"global: build {payload}")
                result.messages.append("global: dry-run; no files modified")
                if result.errors:
                    result.status = "failed"
                return result

            global_skills_root(csk_home).mkdir(parents=True, exist_ok=True)
            global_bin_dir(csk_home).mkdir(parents=True, exist_ok=True)
            installed_names: list[str] = []
            expected_commands: set[str] = set()
            for plan in plans:
                command_names = installer.install_runtime_commands(csk_home, csk_home / "global" / "bin", plan)
                expected_commands.update(command_names)
                installed = installer._install_skill_context_to_root(
                    global_skills_root(csk_home),
                    plan,
                    effective_locale,
                    agents,
                )
                installed_names.append(plan.decl.name)
                result.messages.append(
                    f"global: {plan.decl.name} {plan.resolved.kind} {plan.resolved.ref} "
                    f"{plan.resolved.commit[:7]} {installed}"
                )
                if options.verbose:
                    result.messages.append(f"global: {plan.decl.name} commit {plan.resolved.commit}")
                    for command_name in sorted(command_names):
                        result.messages.append(
                            f"global: {plan.decl.name} command {command_name} -> global/bin/{command_name}"
                        )
            if not result.errors:
                installer._cleanup_removed_skills_root(
                    global_skills_root(csk_home),
                    {plan.decl.name for plan in plans},
                )
            # On partial failure, keep previously working command shims instead
            # of removing commands for skills that failed this install attempt.
            if not result.errors:
                shims.remove_stale_global_shims(csk_home, expected_commands)
                result.messages.extend(global_bins.refresh_user_bin_shims(csk_home, expected_commands))
            env_files.write_global_env_files(csk_home)
            # Refresh adapters from on-disk installs so an older installed
            # skill remains available when the current install attempt failed.
            declared_installed_names = [
                decl.name for decl in global_manifest.skills if (global_skills_root(csk_home) / decl.name).exists()
            ]
            adapters.refresh_global_adapters(csk_home, agents, declared_installed_names or installed_names, config.adapter_mode)
            if result.errors:
                result.status = "failed"
            return result
    except build_planner.BuildPlanningError as exc:
        if options.dry_run and exc.code == "concurrent_state_change":
            raise
        result.status = "failed"
        result.errors.append(str(exc))
        return result
    except Exception as exc:
        result.status = "failed"
        result.errors.append(str(exc))
        return result


def update(config: GlobalConfig) -> GlobalResult:
    result = GlobalResult()
    try:
        global_manifest = load_manifest(config.path.parent)
        for decl in global_manifest.skills:
            try:
                repo = installer._ensure_skill_repo(config, decl, use_persistent_clone=True, stack=None)
                git_ops.fetch_repo(repo)
                result.messages.append(f"fetched {decl.source}")
            except Exception as exc:
                result.errors.append(f"fetch failed {decl.source}: {exc}")
        return result
    except Exception as exc:
        result.status = "failed"
        result.errors.append(str(exc))
        return result


def render_status(config: GlobalConfig) -> str:
    csk_home = config.path.parent
    global_manifest = load_manifest(csk_home)
    lines = [f"Global skills ({global_root(csk_home)})"]
    if not global_manifest.skills:
        lines.append("  no skills declared")
        return "\n".join(lines)
    for decl in global_manifest.skills:
        skill_status = _skill_status(config, decl)
        commit = (skill_status["installed_commit"] or "")[:7]
        suffix = ""
        if skill_status["label"] == "update-available" and skill_status["resolved_commit"]:
            suffix = f" -> {skill_status['resolved_commit'][:7]}"
        lines.append(
            f"  {decl.name:<20} {decl.ref.kind:<8} {decl.ref.value:<12} {commit:<7}  {skill_status['label']}{suffix}"
        )
    return "\n".join(lines)



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
        except Exception as exc:
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
            fetch_existing=False,
            fetched_repos=set(),
            stack=stack,
        )

    try:
        return resolve(global_manifest.skills)
    except Exception as combined_error:
        available: list[manifest.SkillDecl] = []
        isolated_errors: list[str] = []
        for decl in global_manifest.skills:
            try:
                resolve([decl])
            except Exception as exc:
                isolated_errors.append(f"{decl.name}: {exc}")
            else:
                available.append(decl)
        if not isolated_errors:
            raise combined_error
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
    return build_planner.FilesystemGenerationProbe(
        (
            config.path,
            global_skillfile(csk_home),
            global_skills_root(csk_home),
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
        except Exception as exc:
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


def _skill_status(config: GlobalConfig, decl: manifest.SkillDecl) -> dict[str, str | None]:
    resolved_commit: str | None = None
    try:
        resolved = git_ops.resolve_ref(config.skills_root / decl.source, decl.ref.kind, decl.ref.value)
        resolved_commit = resolved.commit
    except Exception:
        return {"installed_commit": None, "resolved_commit": None, "label": "error"}

    marker_path = config.path.parent / "global" / "skills" / decl.name / ".csk-install.json"
    if not marker_path.exists():
        return {"installed_commit": None, "resolved_commit": resolved_commit, "label": "missing"}
    try:
        marker = protocol_json.loads(marker_path.read_bytes())
    except Exception:
        return {"installed_commit": None, "resolved_commit": resolved_commit, "label": "error"}
    installed_commit = marker.get("commit") if isinstance(marker.get("commit"), str) else None
    if installed_commit != resolved_commit:
        return {"installed_commit": installed_commit, "resolved_commit": resolved_commit, "label": "update-available"}
    try:
        actual_hash = hashing.content_sha256(marker_path.parent)
    except Exception:
        return {"installed_commit": installed_commit, "resolved_commit": resolved_commit, "label": "error"}
    if marker.get("content_sha256") != actual_hash:
        return {"installed_commit": installed_commit, "resolved_commit": resolved_commit, "label": "content-drift"}
    return {"installed_commit": installed_commit, "resolved_commit": resolved_commit, "label": "up-to-date"}


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
