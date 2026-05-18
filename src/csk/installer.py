from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import adapters, env_files, gc, git_ops, gitignore_gate, hashing, locale, manifest, shims, skillspec, snapshot, whitelist
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

        effective_locale = project_manifest.locale or config.preferred_locale
        with ExitStack() as stack:
            plans = _build_plans(config, project_manifest, use_cache=not options.dry_run, stack=stack)
            _detect_command_collisions(plans)
            _check_system_commands(plans)
            if options.strict_tags:
                _check_moved_tags_strict(project.path, plans)
            else:
                result.messages.extend(_moved_tag_warnings(project.path, plans))

            if options.dry_run:
                result.messages.append(f"{project.alias}: dry-run; no files modified")
                return result

            installed_names: list[str] = []
            expected_commands: set[str] = set()
            for plan in plans:
                command_names = _install_runtime_commands(config.path.parent, project.path, plan)
                expected_commands.update(command_names)
                installed = _install_skill_context(project.path, plan, effective_locale, agents)
                installed_names.append(plan.decl.name)
                result.messages.append(
                    f"{project.alias}: {plan.decl.name} {plan.resolved.kind} {plan.resolved.ref} "
                    f"{plan.resolved.commit[:7]} {installed}"
                )

            _cleanup_removed_skills(project.path, {plan.decl.name for plan in plans})
            shims.remove_stale_shims(project.path, expected_commands)
            env_files.write_env_files(project.path)
            adapters.refresh_adapters(project.path, agents, installed_names, config.adapter_mode)
            return result
    except Exception as exc:
        result.status = "failed"
        result.errors.append(str(exc))
        return result


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
        for command in plan.spec.commands:
            previous = owners.get(command)
            if previous:
                raise InstallError(
                    f"Command collision for {command!r}: exported by {previous} and {plan.decl.name}"
                )
            owners[command] = plan.decl.name


def _check_system_commands(plans: list[SkillPlan]) -> None:
    for plan in plans:
        for command in plan.spec.commands.values():
            if command.type != "system":
                continue
            if not command.command or shutil.which(command.command) is None:
                hint = f" Hint: {command.hint}" if command.hint else ""
                raise InstallError(f"Missing system command {command.command!r} for {plan.decl.name}.{hint}")


def _check_moved_tags_strict(project_root: Path, plans: list[SkillPlan]) -> None:
    warnings = _moved_tag_warnings(project_root, plans)
    if warnings:
        raise InstallError("; ".join(warnings))


def _moved_tag_warnings(project_root: Path, plans: list[SkillPlan]) -> list[str]:
    warnings: list[str] = []
    for plan in plans:
        if plan.resolved.kind != "tag":
            continue
        marker = _read_marker(project_root / ".agents" / "skills" / plan.decl.name / ".csk-install.json")
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


def _install_runtime_commands(csk_home: Path, project_root: Path, plan: SkillPlan) -> set[str]:
    commands: set[str] = set()
    for command in plan.spec.commands.values():
        if command.type != "script":
            continue
        runtime_path = shims.install_runtime_command(
            csk_home=csk_home,
            skill_name=plan.decl.name,
            commit=plan.resolved.commit,
            snapshot=plan.snapshot,
            command=command,
        )
        shims.write_project_shim(project_root, command.name, runtime_path)
        commands.add(command.name)
    return commands


def _install_skill_context(project_root: Path, plan: SkillPlan, effective_locale: str | None, agents: list[str]) -> str:
    target = project_root / ".agents" / "skills" / plan.decl.name
    marker = _read_marker(target / ".csk-install.json")
    if _marker_is_current(marker, target, plan, effective_locale, agents):
        return "up-to-date"

    tmp = target.parent / f".{plan.decl.name}.tmp-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    include_scripts = not plan.spec.commands and (plan.snapshot / "scripts").exists()
    files = whitelist.copy_context(plan.snapshot, tmp, include_scripts=include_scripts)
    locale.render_locale(plan.snapshot, tmp, effective_locale)
    content_hash = hashing.content_sha256(tmp)
    marker_data = {
        "schema_version": 1,
        "name": plan.decl.name,
        "source": plan.decl.source,
        "ref_kind": plan.resolved.kind,
        "ref": plan.resolved.ref,
        "commit": plan.resolved.commit,
        "content_sha256": content_hash,
        "locale": effective_locale,
        "agents": agents,
        "commands": sorted(plan.spec.commands),
        "installed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "files": files,
    }
    if plan.decl.git is not None:
        marker_data["git"] = plan.decl.git
    (tmp / ".csk-install.json").write_text(json.dumps(marker_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _replace_dir(tmp, target)
    return "installed"


def _marker_is_current(
    marker: dict[str, object] | None,
    target: Path,
    plan: SkillPlan,
    locale_value: str | None,
    agents: list[str],
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
    if marker.get("agents") != agents:
        return False
    actual_hash = hashing.content_sha256(target)
    return marker.get("content_sha256") == actual_hash


def _read_marker(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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
    skills_root = project_root / ".agents" / "skills"
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
