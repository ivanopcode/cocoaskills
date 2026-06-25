from __future__ import annotations

import json
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import adapters, env_files, git_ops, global_bins, hashing, installer, manifest, shims
from .audit import pipeline as audit_pipeline
from .config import DEFAULT_AGENTS, GlobalConfig


class GlobalInstallError(Exception):
    pass


@dataclass
class GlobalResult:
    status: str = "ok"
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

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
    result = GlobalResult()
    csk_home = config.path.parent
    try:
        global_manifest = load_manifest(csk_home)
        agents = global_manifest.agents or config.default_agents
        effective_locale = global_manifest.locale or config.preferred_locale
        with ExitStack() as stack:
            plans = _build_plans(config, global_manifest, options=options, stack=stack, result=result)
            try:
                installer._detect_command_collisions(plans)
            except Exception as exc:
                result.status = "failed"
                result.errors.append(str(exc))
                return result
            plans = _plans_with_available_dependencies(plans, result)
            audit_gate = audit_pipeline.gate_plans(plans, config, scope="global", record=not options.dry_run)
            result.messages.extend(audit_gate.warnings)
            if audit_gate.blocked:
                result.status = "failed"
                result.errors.extend(audit_gate.errors)
                return result
            if options.strict_tags:
                installer._check_moved_tags_strict(global_skills_root(csk_home), plans)
            else:
                for warning in installer._moved_tag_warnings(global_skills_root(csk_home), plans):
                    result.messages.append(f"global: {warning}")
            if options.dry_run:
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
            installer._cleanup_removed_skills_root(
                global_skills_root(csk_home),
                {decl.name for decl in global_manifest.skills},
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
        single_skill_manifest = manifest.ProjectManifest(
            path=global_manifest.path,
            project_alias=global_manifest.project_alias,
            agents=global_manifest.agents,
            locale=global_manifest.locale,
            skills=[decl],
        )
        try:
            plans.extend(
                installer._build_plans(
                    config,
                    single_skill_manifest,
                    use_cache=not options.dry_run,
                    stack=stack,
                )
            )
        except Exception as exc:
            result.errors.append(f"{decl.name}: {exc}")
    return plans


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
            errors = installer._skill_command_dependency_errors(plan, available)
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
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
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
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GlobalInstallError(f"Malformed JSON in global Skillfile {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GlobalInstallError(f"Global Skillfile must contain a JSON object: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
