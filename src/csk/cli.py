from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, config, deprecation, git_ops, installer, manifest, project_resolver, shell_init, status
from .locking import GlobalLock, LockError


EXIT_OK = 0
EXIT_PARTIAL_FAIL = 1
EXIT_CONFIG = 2
EXIT_LOCK = 3


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_CONFIG
    if args.version:
        print(f"csk {__version__}")
        return EXIT_OK
    if not args.command:
        parser.print_help()
        return EXIT_OK
    try:
        return _dispatch(args)
    except (
        config.ConfigError,
        manifest.ManifestError,
        project_resolver.ProjectResolutionError,
        installer.InstallError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except LockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csk",
        description="CocoaSkill local skill manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Local documentation index:\n"
            "  csk bootstrap          create ~/.cocoaskills/config.json\n"
            "  csk install [target]   apply Skillfile.json without fetching\n"
            "  csk update             fetch local skill repositories\n"
            "  csk upgrade [target]   update, then install\n"
            "  csk status [target]    show manifest vs installed state\n"
            "  csk list               list configured projects and skills\n"
            "  csk project add        add a configured project\n"
            "  csk project resolve    show current checkout resolution\n"
            "  csk config show        show config path and content\n"
            "  csk shell-init         print shell hook code\n\n"
            "Run 'csk <command> --help' for command-specific documentation."
        ),
    )
    parser.add_argument("--version", action="store_true", help="print csk version and exit")
    sub = parser.add_subparsers(dest="command")

    _add_bootstrap(sub)
    _add_install(sub, "install", "Apply Skillfile.json using local refs. No fetch is performed.")
    sub.add_parser(
        "update",
        help="Fetch all local skill repositories under skills_root.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Purpose:\n  Runs git fetch --all --tags --prune for every git repo under skills_root.\n\n"
            "Side effects:\n  Mutates local skill repositories only. Projects are not modified.\n\n"
            "Exit codes:\n  0 all repos fetched, 1 one or more fetches failed, 2 config error, 3 lock contention.\n\n"
            "Example:\n  csk update"
        ),
    )
    _add_install(sub, "upgrade", "Fetch skill repositories, then install.")
    status_parser = sub.add_parser(
        "status",
        help="Show manifest vs installed state.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Labels:\n  up-to-date, missing, update-available, content-drift, error\n\n"
            "Files read:\n  ~/.cocoaskills/config.json, Skillfile.json, .agents/skills/*/.csk-install.json\n\n"
            "Examples:\n  csk status\n  csk status partners-app-ios\n  csk status ."
        ),
    )
    status_parser.add_argument("target", nargs="?", help="project alias, '.', or project path")
    list_parser = sub.add_parser(
        "list",
        help="List configured projects and declared skills.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Files read:\n  ~/.cocoaskills/config.json and project Skillfile.json files when present.\n\n"
            "Examples:\n  csk list\n  csk list --paths"
        ),
    )
    list_parser.add_argument("--paths", action="store_true", help="include project_alias, checkout_alias, and paths")

    project = sub.add_parser("project", help="Manage configured projects.")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    add = project_sub.add_parser(
        "add",
        help="Add project to global config and create Skillfile.json if missing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Side effects:\n"
            "  Updates ~/.cocoaskills/config.json and creates <project>/Skillfile.json if missing.\n\n"
            "Example:\n  csk project add partners-app-ios /path/to/project"
        ),
    )
    add.add_argument("alias")
    add.add_argument("path")
    resolve = project_sub.add_parser(
        "resolve",
        help="Resolve a project alias/path without installing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Shows which Skillfile.json is used, which alias is selected, and where generated files land.\n\n"
            "Examples:\n  csk project resolve .\n  csk project resolve /path/to/worktree"
        ),
    )
    resolve.add_argument("target", nargs="?", default=".", help="project alias, '.', or project path")

    config_parser = sub.add_parser("config", help="Inspect csk config.")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser(
        "show",
        help="Print resolved config path and content.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n  csk config show",
    )

    shell = sub.add_parser(
        "shell-init",
        help="Print shell hook code for PATH activation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Purpose:\n  Prints shell code that activates nearest .agents/env.sh or .agents/env.ps1.\n\n"
            "Examples:\n  eval \"$(csk shell-init bash)\"\n  csk shell-init powershell >> $PROFILE"
        ),
    )
    shell.add_argument("shell", nargs="?", default="bash", choices=["zsh", "bash", "powershell"])
    return parser


def _add_bootstrap(sub) -> None:
    sub.add_parser(
        "bootstrap",
        help="Interactively create global config.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Interactively asks for skills_root, preferred_locale, default_agents, "
            "focused projects, and shell hook instructions."
        ),
        epilog=(
            "Files written:\n  ~/.cocoaskills/config.json and optional project Skillfile.json files.\n\n"
            "Example:\n  csk bootstrap"
        ),
    )


def _add_install(sub, name: str, description: str) -> None:
    parser = sub.add_parser(
        name,
        help=description,
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Files read:\n"
            "  ~/.cocoaskills/config.json, <project>/Skillfile.json, local skill git repositories.\n\n"
            "Files written:\n"
            "  <project>/.agents/skills, <project>/.agents/bin, .agents/env.sh, .agents/env.ps1,\n"
            "  agent adapter directories, ~/.cocoaskills/runtime, ~/.cocoaskills/cache.\n\n"
            "Exit codes:\n"
            "  0 success, 1 one or more projects/skills failed, 2 config error, 3 lock contention.\n\n"
            "Examples:\n"
            f"  csk {name}\n"
            f"  csk {name} partners-app-ios\n"
            f"  csk {name} .\n"
            f"  csk {name} /path/to/project\n"
            f"  csk {name} --fix-gitignore\n"
        ),
    )
    parser.add_argument("target", nargs="?", help="project alias, '.', or project path")
    parser.add_argument("--dry-run", action="store_true", help="plan work without modifying files")
    parser.add_argument("--verbose", action="store_true", help="print detailed progress")
    parser.add_argument("--fix-gitignore", action="store_true", help="append missing CocoaSkill gitignore entries")
    parser.add_argument("--strict-tags", action="store_true", help="fail if an installed tag moved to another commit")


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "bootstrap":
        return _cmd_bootstrap()
    if args.command == "config" and args.config_command == "show":
        return _cmd_config_show()
    if args.command == "project" and args.project_command == "add":
        return _cmd_project_add(args.alias, Path(args.path))
    if args.command == "project" and args.project_command == "resolve":
        cfg = config.load_config()
        print(_render_project_resolution(cfg, args))
        return EXIT_OK
    if args.command == "shell-init":
        print(shell_init.shell_init(args.shell))
        return EXIT_OK

    cfg = config.load_config()
    if args.command == "list":
        print(_render_list(cfg, show_paths=args.paths))
        return EXIT_OK
    if args.command == "status":
        _warn_bare_project_command(args, cfg)
        cfg, alias = _cfg_and_alias_for_target(cfg, args, save=False)
        print(status.render_status(cfg, alias=alias))
        return EXIT_OK

    if args.command in {"install", "update", "upgrade"}:
        config.validate_skills_root_for_work(cfg)
        with GlobalLock(cfg.path.parent):
            if args.command == "update":
                return _cmd_update(cfg)
            if args.command == "upgrade":
                _warn_bare_project_command(args, cfg)
                update_code = _cmd_update(cfg)
                cfg, args = _prepare_install_target(cfg, args)
                install_code = _cmd_install(cfg, args)
                return install_code if install_code != EXIT_OK else update_code
            _warn_bare_project_command(args, cfg)
            cfg, args = _prepare_install_target(cfg, args)
            return _cmd_install(cfg, args)

    raise ValueError(f"Unknown command: {args.command}")


def _cmd_bootstrap() -> int:
    path = config.config_path()
    if path.exists():
        answer = input(f"Config exists at {path}. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print(f"Kept existing config: {path}")
            return EXIT_OK
    skills_root = input("skills_root: ").strip()
    preferred_locale = input("preferred_locale [none]: ").strip() or None
    default_agents_raw = input("default_agents comma-separated [codex_cli]: ").strip()
    default_agents = [item.strip() for item in default_agents_raw.split(",") if item.strip()] or ["codex_cli"]
    projects: dict[str, config.ProjectConfig] = {}
    while True:
        alias = input("project alias [empty to finish]: ").strip()
        if not alias:
            break
        project_path = Path(input(f"path for {alias}: ").strip()).expanduser()
        projects[alias] = config.ProjectConfig(alias=alias, path=project_path, agents=list(default_agents))
        manifest.ensure_empty_manifest(project_path)
    cfg = config.GlobalConfig(
        path=path,
        skills_root=Path(skills_root).expanduser(),
        preferred_locale=preferred_locale,
        default_agents=default_agents,
        adapter_mode="auto",
        worktree_alias_pattern=config.DEFAULT_WORKTREE_ALIAS_PATTERN,
        projects=projects,
    )
    config.save_config(cfg)
    print(f"Wrote {path}")
    print("Install shell hook with: eval \"$(csk shell-init bash)\"")
    return EXIT_OK


def _cmd_config_show() -> int:
    path = config.config_path()
    print(f"Config path: {path}")
    if path.exists():
        print(path.read_text(encoding="utf-8"), end="")
    else:
        print("Config does not exist")
    return EXIT_OK


def _cmd_project_add(alias: str, path: Path) -> int:
    cfg = config.load_config()
    updated = config.add_project(cfg, alias, path)
    config.save_config(updated)
    manifest.ensure_empty_manifest(path)
    print(f"Added project {alias}: {path}")
    return EXIT_OK


def _cmd_update(cfg: config.GlobalConfig) -> int:
    results = git_ops.fetch_all(cfg.skills_root)
    failed = False
    for repo, error in results:
        if error:
            failed = True
            print(f"fetch failed {repo.name}: {error}", file=sys.stderr)
        else:
            print(f"fetched {repo.name}")
    return EXIT_PARTIAL_FAIL if failed else EXIT_OK


def _cmd_install(cfg: config.GlobalConfig, args: argparse.Namespace) -> int:
    if args.fix_gitignore:
        deprecation.warn_once("fix-gitignore")
    options = installer.InstallOptions(
        dry_run=args.dry_run,
        fix_gitignore=args.fix_gitignore,
        strict_tags=args.strict_tags,
        verbose=args.verbose,
    )
    results = installer.install(cfg, alias=args.alias, options=options)
    failed = False
    for result in results:
        for message in result.messages:
            print(message)
        for error in result.errors:
            failed = True
            print(f"{result.alias}: {error}", file=sys.stderr)
    return EXIT_PARTIAL_FAIL if failed else EXIT_OK


def _render_list(cfg: config.GlobalConfig, *, show_paths: bool = False) -> str:
    lines = [f"Config: {cfg.path}", f"Skills root: {cfg.skills_root}"]
    for alias, project in cfg.projects.items():
        if show_paths:
            suffix = "" if project.path.exists() else " (missing)"
            lines.append(
                f"Project {alias}: path={project.path}{suffix} "
                f"project_alias={project.project_alias or alias} checkout_alias={project.checkout_alias or alias}"
            )
        else:
            lines.append(f"Project {alias}: {project.path}")
        project_manifest = manifest.load_manifest(project.path)
        if project_manifest is None:
            lines.append("  Skillfile.json missing")
            continue
        if not project_manifest.skills:
            lines.append("  no skills declared")
            continue
        for decl in project_manifest.skills:
            lines.append(f"  {decl.name} ({decl.ref.kind} {decl.ref.value})")
    return "\n".join(lines)


def _prepare_install_target(
    cfg: config.GlobalConfig, args: argparse.Namespace
) -> tuple[config.GlobalConfig, argparse.Namespace]:
    cfg, alias = _cfg_and_alias_for_target(cfg, args, save=not args.dry_run)
    args.alias = alias
    return cfg, args


def _cfg_and_alias_for_target(
    cfg: config.GlobalConfig,
    args: argparse.Namespace,
    *,
    save: bool,
) -> tuple[config.GlobalConfig, str | None]:
    path_target = _path_target_from_args(args)
    if path_target is None:
        target = getattr(args, "target", None)
        return cfg, target
    if save:
        deprecation.warn_once("auto-register")
    resolved = project_resolver.resolve(path_target, worktree_alias_pattern=cfg.worktree_alias_pattern)
    project_manifest = manifest.load_manifest(resolved.root)
    agents = project_manifest.agents if project_manifest and project_manifest.agents else cfg.default_agents
    updated = config.add_project(
        cfg,
        resolved.checkout_alias,
        resolved.root,
        agents,
        project_alias=resolved.project_alias,
        checkout_alias=resolved.checkout_alias,
    )
    if save:
        config.save_config(updated)
    return updated, resolved.checkout_alias


def _warn_bare_project_command(args: argparse.Namespace, cfg: config.GlobalConfig) -> None:
    if getattr(args, "target", None) is not None or not cfg.projects:
        return
    if args.command == "install":
        deprecation.warn_once("bare-install", count=len(cfg.projects))
    elif args.command == "status":
        deprecation.warn_once("bare-status", count=len(cfg.projects))
    elif args.command == "upgrade":
        deprecation.warn_once("bare-upgrade", count=len(cfg.projects))


def _path_target_from_args(args: argparse.Namespace) -> Path | None:
    target = getattr(args, "target", None)
    if target and _looks_like_path(target):
        return Path(target).expanduser()
    return None


def _looks_like_path(value: str) -> bool:
    return (
        value in {".", "..", "~"}
        or value.startswith(("./", "../", "~/"))
        or Path(value).is_absolute()
    )


def _render_project_resolution(cfg: config.GlobalConfig, args: argparse.Namespace) -> str:
    path_target = _path_target_from_args(args)
    target = getattr(args, "target", None)
    if path_target is None and target and target in cfg.projects:
        project = cfg.projects[target]
        return _render_configured_project_resolution(project, cfg.worktree_alias_pattern)
    if path_target is None and target and target not in {".", ""}:
        raise ValueError(f"Unknown project alias: {target}")
    resolved = project_resolver.resolve(path_target or Path.cwd(), worktree_alias_pattern=cfg.worktree_alias_pattern)
    agents: list[str] = []
    project_manifest = manifest.load_manifest(resolved.root)
    if project_manifest:
        agents = project_manifest.agents
    lines = [
        f"project_alias: {resolved.project_alias}",
        f"checkout_alias: {resolved.checkout_alias}",
        f"path: {resolved.root}",
        f"skillfile: {resolved.skillfile}",
        f"branch: {resolved.branch or ''}",
        f"task_id: {resolved.task_id or ''}",
        f"path_hash: {resolved.path_hash}",
        "install_paths:",
        f"  skills: {resolved.root / '.agents' / 'skills'}",
        f"  bin: {resolved.root / '.agents' / 'bin'}",
        f"  claude_code: {resolved.root / '.claude' / 'skills'}",
        f"  codex_cli: {resolved.root / '.codex' / 'skills'}",
        f"  cursor: {resolved.root / '.cursor' / 'rules'}",
        f"  gemini: {resolved.root / '.gemini' / 'skills'}",
        f"agents: {', '.join(agents or cfg.default_agents)}",
    ]
    return "\n".join(lines)


def _render_configured_project_resolution(project: config.ProjectConfig, worktree_alias_pattern: str) -> str:
    root = project.path
    branch = project_resolver.git_branch(root)
    task_id = project_resolver.task_id_from_branch(branch, worktree_alias_pattern)
    path_hash = project_resolver.stable_path_hash(root) if root.exists() else ""
    return "\n".join(
        [
            f"project_alias: {project.project_alias or project.alias}",
            f"checkout_alias: {project.checkout_alias or project.alias}",
            f"path: {root}",
            f"skillfile: {root / manifest.MANIFEST_NAME}",
            f"branch: {branch or ''}",
            f"task_id: {task_id or ''}",
            f"path_hash: {path_hash}",
            "install_paths:",
            f"  skills: {root / '.agents' / 'skills'}",
            f"  bin: {root / '.agents' / 'bin'}",
            f"  claude_code: {root / '.claude' / 'skills'}",
            f"  codex_cli: {root / '.codex' / 'skills'}",
            f"  cursor: {root / '.cursor' / 'rules'}",
            f"  gemini: {root / '.gemini' / 'skills'}",
            f"agents: {', '.join(project.agents)}",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
