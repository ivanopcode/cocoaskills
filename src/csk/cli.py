from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__, adapters, attest, config, deprecation, dev_substitutions, gc, git_ops, gitignore_gate, global_install, hybrid, installer, manifest, project_resolver, shell_init, skillcheck, status
from .audit import pipeline as audit_pipeline
from .audit import runner as audit_runner
from .audit import trust as audit_trust
from .audit.backends import AuditBackendError
from .audit.model import Decision
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
        global_install.GlobalInstallError,
        installer.InstallError,
        hybrid.HybridError,
        audit_runner.AuditError,
        AuditBackendError,
        git_ops.GitError,
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
            "  csk init [path]        create project Skillfile.json and gitignore block\n"
            "  csk install [target]   apply Skillfile.json; clone missing URL sources\n"
            "  csk update             fetch local skill repositories\n"
            "  csk upgrade [target]   update, then install\n"
            "  csk status [target]    show manifest vs installed state\n"
            "  csk global <command>   manage user-wide global skills\n"
            "  csk audit [target]     run deterministic security audit\n"
            "  csk skill check <dir>  validate one skill directory\n"
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
    _add_init(sub)
    _add_skill(sub)
    _add_install(sub, "install", "Apply Skillfile.json using local refs. Missing git URL sources are cloned.")
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
    _add_global(sub)
    _add_audit(sub)
    status_parser = sub.add_parser(
        "status",
        help="Show manifest vs installed state.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Labels:\n  up-to-date, missing, update-available, content-drift, error\n\n"
            "Files read:\n  ~/.cocoaskills/config.json, Skillfile.json, .agents/skills/*/.csk-install.json\n\n"
            "Examples:\n  csk status\n  csk status --all\n  csk status demo-app-ios\n  csk status ."
        ),
    )
    status_parser.add_argument("target", nargs="?", help="project alias, '.', or project path")
    status_parser.add_argument("--all", action="store_true", help="show all registered projects")
    status_parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero unless every skill is up-to-date",
    )
    status_parser.add_argument(
        "--attest",
        action="store_true",
        help="re-check installed skills against trusted audit registries",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON instead of the table",
    )
    sub.add_parser(
        "gc",
        help="Remove unreferenced runtime entries, snapshot cache entries, and dead consumer registry entries.",
    )
    add_parser = sub.add_parser("add", help="Add or replace a skill declaration in the project Skillfile.")
    add_parser.add_argument("name")
    add_parser.add_argument("--git", help="git clone URL")
    add_parser.add_argument("--source", help="local source directory under skills_root")
    add_refs = add_parser.add_mutually_exclusive_group(required=True)
    add_refs.add_argument("--tag")
    add_refs.add_argument("--branch")
    add_refs.add_argument("--revision")
    add_parser.add_argument("--project", help="project alias, '.', or path (default: current project)")
    remove_parser = sub.add_parser("remove", help="Remove a skill declaration from the project Skillfile.")
    remove_parser.add_argument("name")
    remove_parser.add_argument("--project", help="project alias, '.', or path (default: current project)")

    hybrid_parser = sub.add_parser(
        "hybrid",
        help="Manage machine-level hybrid skills activated for selected projects.",
    )
    hybrid_sub = hybrid_parser.add_subparsers(dest="hybrid_command", required=True)
    hybrid_add = hybrid_sub.add_parser("add", help="add or replace a hybrid skill declaration")
    hybrid_add.add_argument("name")
    hybrid_add.add_argument("--git", help="git clone URL")
    hybrid_refs = hybrid_add.add_mutually_exclusive_group(required=True)
    hybrid_refs.add_argument("--tag")
    hybrid_refs.add_argument("--branch")
    hybrid_refs.add_argument("--revision")
    hybrid_add.add_argument(
        "--target",
        action="append",
        required=True,
        help="project alias, absolute path, or path glob (repeatable)",
    )
    hybrid_remove = hybrid_sub.add_parser("remove", help="remove a hybrid skill declaration")
    hybrid_remove.add_argument("name")
    hybrid_sub.add_parser("list", help="list hybrid skill declarations")
    hybrid_sub.add_parser("status", help="show hybrid declarations and installed store state")
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
            "Example:\n  csk project add demo-app-ios /path/to/project"
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
    shell.add_argument("--no-global", action="store_true", help="do not activate global CocoaSkills bin")
    return parser


def _add_bootstrap(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        "bootstrap",
        help="Create global config (interactive, or scripted with flags).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Asks for machine-level settings: skills_root, preferred_locale, "
            "default_agents, and shell hook instructions. Flags override the prompts; "
            "--non-interactive disables prompting entirely."
        ),
        epilog=(
            "Files written:\n  ~/.cocoaskills/config.json.\n\n"
            "Examples:\n  csk bootstrap\n"
            "  csk bootstrap --non-interactive --skills-root ~/skills --default-agents codex_cli,claude_code"
        ),
    )
    parser.add_argument("--skills-root", help="directory containing skill git repositories")
    parser.add_argument("--preferred-locale", help="preferred install locale, e.g. ru")
    parser.add_argument("--default-agents", help="comma-separated agent ids, e.g. codex_cli,claude_code")
    parser.add_argument("--non-interactive", action="store_true", help="never prompt; fail instead")
    parser.add_argument("--force", action="store_true", help="overwrite an existing config without asking")


def _add_init(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        "init",
        help="Create project Skillfile.json and CocoaSkill gitignore block.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Initializes CocoaSkill files in one project without installing skills.",
        epilog=(
            "Files written:\n  <project>/Skillfile.json and <project>/.gitignore.\n\n"
            "Examples:\n"
            "  csk init\n"
            "  csk init --alias demo-ios --agents codex_cli,cursor\n"
            "  csk init /path/to/project\n"
        ),
    )
    parser.add_argument("path", nargs="?", default=".", help="project directory to initialize")
    parser.add_argument("--alias", help="project.alias to write into Skillfile.json")
    parser.add_argument("--agents", help="comma-separated agents list for Skillfile.json")
    parser.add_argument("--no-interactive", action="store_true", help="accepted for scripting; prompts are not used")


def _add_skill(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("skill", help="Inspect and validate skill repositories.")
    skill_sub = parser.add_subparsers(dest="skill_command", required=True)
    check = skill_sub.add_parser(
        "check",
        help="Validate one skill directory without requiring csk config.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Validates intrinsic skill requirements in a working tree directory.",
        epilog=(
            "Files read:\n"
            "  <skill>/SKILL.md, <skill>/csk-skill.json, <skill>/agents/runtime.json,\n"
            "  <skill>/locales/metadata.json, <skill>/.skill_triggers.\n\n"
            "Notes:\n"
            "  This command reads the working tree as-is. csk install validates the committed\n"
            "  git snapshot resolved from Skillfile.json.\n\n"
            "Exit codes:\n"
            "  0 no errors, 1 one or more strict errors.\n\n"
            "Examples:\n"
            "  csk skill check .\n"
            "  csk skill check /path/to/skill --locale ru\n"
            "  csk skill check . --json"
        ),
    )
    check.add_argument("path", help="skill directory to validate")
    check.add_argument("--locale", help="selected locale to validate")
    check.add_argument("--json", action="store_true", dest="json_output", help="print machine-readable issues")


def _add_install(sub: argparse._SubParsersAction[argparse.ArgumentParser], name: str, description: str) -> None:
    parser = sub.add_parser(
        name,
        help=description,
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Files read:\n"
            "  ~/.cocoaskills/config.json, <project>/Skillfile.json, local skill git repositories.\n"
            "  Missing repositories declared with `git` may be cloned into skills_root.\n\n"
            "Files written:\n"
            "  <project>/.agents/skills, <project>/.agents/bin, .agents/env.sh, .agents/env.ps1,\n"
            "  agent adapter directories, ~/.cocoaskills/runtime, ~/.cocoaskills/cache.\n\n"
            "Exit codes:\n"
            "  0 success, 1 one or more projects/skills failed, 2 config error, 3 lock contention.\n\n"
            "Examples:\n"
            f"  csk {name}\n"
            f"  csk {name} --all\n"
            f"  csk {name} demo-app-ios\n"
            f"  csk {name} .\n"
            f"  csk {name} /path/to/project\n"
            f"  csk {name} --fix-gitignore\n"
        ),
    )
    parser.add_argument("target", nargs="?", help="project alias, '.', or project path")
    parser.add_argument("--all", action="store_true", help="operate on all registered projects")
    parser.add_argument("--dry-run", action="store_true", help="plan work without modifying files")
    parser.add_argument("--verbose", action="store_true", help="print detailed progress")
    parser.add_argument("--fix-gitignore", action="store_true", help="deprecated; prefer csk init")
    parser.add_argument("--strict-tags", action="store_true", help="fail if an installed tag moved to another commit")
    parser.add_argument(
        "--audit",
        nargs="?",
        const="advisory",
        choices=["advisory", "strict"],
        help="run audit gate for this install (default mode: advisory)",
    )


def _add_global(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        "global",
        help="Manage user-wide global skills.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Global scope:\n"
            "  Uses ~/.cocoaskills/global/Skillfile.json and installs adapters into user-level agent dirs.\n\n"
            "Examples:\n"
            "  csk global init\n"
            "  csk global add skill-metrics --git git@example.com:skills/skill-metrics.git --tag v1.0.0\n"
            "  csk global install\n"
            "  csk global upgrade\n"
        ),
    )
    global_sub = parser.add_subparsers(dest="global_command", required=True)
    global_sub.add_parser("init", help="create ~/.cocoaskills/global/Skillfile.json")
    add = global_sub.add_parser("add", help="add or replace a global skill declaration")
    add.add_argument("name")
    add.add_argument("--git", help="git clone URL")
    add.add_argument("--source", help="local source directory name under skills_root")
    refs = add.add_mutually_exclusive_group(required=True)
    refs.add_argument("--tag")
    refs.add_argument("--branch")
    refs.add_argument("--revision")
    remove = global_sub.add_parser("remove", help="remove a global skill declaration")
    remove.add_argument("name")
    global_sub.add_parser("list", help="list declared global skills")
    global_sub.add_parser("status", help="show global installed state")
    install = global_sub.add_parser("install", help="install global skills")
    install.add_argument("--dry-run", action="store_true", help="validate without modifying files")
    install.add_argument("--verbose", action="store_true", help="print detailed progress")
    install.add_argument("--strict-tags", action="store_true", help="fail if a tag was locally moved to another commit")
    install.add_argument(
        "--audit",
        nargs="?",
        const="advisory",
        choices=["advisory", "strict"],
        help="run audit gate for this install (default mode: advisory)",
    )
    global_sub.add_parser("update", help="fetch global skill source repositories")
    upgrade = global_sub.add_parser("upgrade", help="fetch global skill sources, then install")
    upgrade.add_argument("--dry-run", action="store_true", help="validate install without modifying installed files")
    upgrade.add_argument("--verbose", action="store_true", help="print detailed progress")
    upgrade.add_argument("--strict-tags", action="store_true", help="fail if a tag was locally moved to another commit")
    upgrade.add_argument(
        "--audit",
        nargs="?",
        const="advisory",
        choices=["advisory", "strict"],
        help="run audit gate for this install (default mode: advisory)",
    )


def _add_audit(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser(
        "audit",
        help="Run deterministic static security audit for declared skills.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Scope:\n"
            "  By default audits the current project. Use a target, --all, or --global for other scopes.\n"
            "  --all audits registered projects and global skills.\n\n"
            "Side effects:\n"
            "  Read-only. Missing git URL sources may be cloned into temporary directories only.\n\n"
            "Examples:\n"
            "  csk audit\n"
            "  csk audit . --json\n"
            "  csk audit --all\n"
            "  csk audit --global\n"
        ),
    )
    parser.add_argument("target", nargs="?", help="project alias, '.', or project path")
    parser.add_argument("--all", action="store_true", help="audit all registered projects and global skills")
    parser.add_argument("--global", dest="global_scope", action="store_true", help="audit global skills")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--allow",
        metavar="CONTENT_SHA256",
        help="pin a legacy content hash to satisfy strict schema v1/v2 capability declaration checks",
    )
    parser.add_argument("--reason", help="required reason for --allow")


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "bootstrap":
        return _cmd_bootstrap(args)
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "config" and args.config_command == "show":
        return _cmd_config_show()
    if args.command == "project" and args.project_command == "add":
        return _cmd_project_add(args.alias, Path(args.path))
    if args.command == "project" and args.project_command == "resolve":
        cfg = config.load_config()
        print(_render_project_resolution(cfg, args))
        return EXIT_OK
    if args.command == "shell-init":
        print(shell_init.shell_init(args.shell, include_global=not args.no_global))
        return EXIT_OK
    if args.command == "global":
        return _dispatch_global(args)
    if args.command == "audit":
        return _cmd_audit(args)
    if args.command == "skill" and args.skill_command == "check":
        return _cmd_skill_check(args)

    cfg = config.load_config()
    if args.command == "list":
        print(_render_list(cfg, show_paths=args.paths))
        return EXIT_OK
    if args.command == "status":
        cfg, alias = _cfg_and_alias_for_target(cfg, args)
        if getattr(args, "attest", False):
            results = attest.attest_projects(cfg, alias=alias)
            print(attest.render(results))
            return EXIT_PARTIAL_FAIL if attest.has_revocation(results) else EXIT_OK
        statuses = status.collect_status(cfg, alias=alias)
        if args.json:
            print(json.dumps(status.statuses_to_payload(statuses), indent=2, sort_keys=True))
        else:
            print(status.render_collected(statuses))
        if args.check and not all(project.clean for project in statuses):
            return EXIT_PARTIAL_FAIL
        return EXIT_OK

    if args.command in {"add", "remove"}:
        project_root = _resolve_project_root(cfg, args.project)
        if args.command == "add":
            ref_kind, ref = _global_ref_from_args(args)
            manifest.add_skill_decl(
                project_root,
                name=args.name,
                ref_kind=ref_kind,
                ref=ref,
                git=args.git,
                source=args.source,
            )
            print(f"Added skill {args.name}: {ref_kind} {ref} ({project_root / 'Skillfile.json'})")
        else:
            manifest.remove_skill_decl(project_root, args.name)
            print(f"Removed skill {args.name} ({project_root / 'Skillfile.json'})")
        print("Run 'csk install' to apply.")
        return EXIT_OK

    if args.command == "hybrid":
        return _cmd_hybrid(cfg, args)

    if args.command == "gc":
        with GlobalLock(cfg.path.parent):
            stats = gc.collect_runtime(cfg, cfg.path.parent)
        print(
            f"gc: removed {stats.runtime_removed} runtime dir(s), "
            f"{stats.snapshots_removed} snapshot(s), "
            f"pruned {stats.consumers_pruned} consumer(s)"
        )
        return EXIT_OK

    if args.command in {"install", "update", "upgrade"}:
        if not getattr(args, "dry_run", False):
            config.validate_skills_root_for_work(cfg)
        with GlobalLock(cfg.path.parent):
            if args.command == "update":
                return _cmd_update(cfg)
            if args.command == "upgrade":
                update_code = _cmd_update(cfg)
                cfg, args = _prepare_install_target(cfg, args)
                install_code = _cmd_install(cfg, args)
                return install_code if install_code != EXIT_OK else update_code
            cfg, args = _prepare_install_target(cfg, args)
            return _cmd_install(cfg, args)

    raise ValueError(f"Unknown command: {args.command}")


def _dispatch_global(args: argparse.Namespace) -> int:
    csk_home = config.config_path().parent
    if args.global_command == "init":
        default_agents = _global_default_agents()
        path = global_install.init(csk_home, default_agents=default_agents)
        print(f"Initialized global CocoaSkills at {path.parent}")
        return EXIT_OK
    if args.global_command == "add":
        ref_kind, ref = _global_ref_from_args(args)
        global_install.add_decl(
            csk_home,
            name=args.name,
            ref_kind=ref_kind,
            ref=ref,
            git=args.git,
            source=args.source,
            default_agents=_global_default_agents(),
        )
        print(f"Added global skill {args.name}: {ref_kind} {ref}")
        return EXIT_OK
    if args.global_command == "remove":
        global_install.remove_decl(csk_home, args.name)
        print(f"Removed global skill {args.name}")
        return EXIT_OK
    if args.global_command == "list":
        print(global_install.list_declared(csk_home))
        return EXIT_OK

    cfg = config.load_config()
    if args.global_command == "status":
        print(global_install.render_status(cfg))
        return EXIT_OK
    dry_run_install = args.global_command == "install" and getattr(args, "dry_run", False)
    if not dry_run_install:
        config.validate_skills_root_for_work(cfg)
    with GlobalLock(cfg.path.parent):
        if args.global_command == "update":
            return _cmd_global_update(cfg)
        if args.global_command == "install":
            return _cmd_global_install(cfg, args)
        if args.global_command == "upgrade":
            update_code = _cmd_global_update(cfg)
            install_code = _cmd_global_install(cfg, args)
            return install_code if install_code != EXIT_OK else update_code
    raise ValueError(f"Unknown global command: {args.global_command}")


def _global_default_agents() -> list[str]:
    try:
        cfg = config.load_config()
    except config.ConfigError:
        return list(config.DEFAULT_AGENTS)
    return list(cfg.default_agents or config.DEFAULT_AGENTS)


def _global_ref_from_args(args: argparse.Namespace) -> tuple[str, str]:
    for ref_kind in ("tag", "branch", "revision"):
        value = getattr(args, ref_kind, None)
        if value:
            return ref_kind, value
    raise ValueError("global add requires one of --tag, --branch, or --revision")


def _cmd_hybrid(cfg: config.GlobalConfig, args: argparse.Namespace) -> int:
    csk_home = cfg.path.parent
    if args.hybrid_command == "add":
        ref_kind, ref = _global_ref_from_args(args)
        hybrid.add_hybrid_decl(
            csk_home,
            name=args.name,
            ref_kind=ref_kind,
            ref=ref,
            git=args.git,
            targets=list(args.target),
        )
        print(f"Added hybrid skill {args.name}: {ref_kind} {ref} -> {', '.join(args.target)}")
        print("Apply with 'csk install' in a targeted project.")
        return EXIT_OK
    if args.hybrid_command == "remove":
        hybrid.remove_hybrid_decl(csk_home, args.name)
        print(f"Removed hybrid skill {args.name}")
        print("Run 'csk install' in previously targeted projects to clean up.")
        return EXIT_OK
    decls = hybrid.load_hybrid_decls(csk_home)
    if not decls:
        print("no hybrid skills declared")
        return EXIT_OK
    store = hybrid.hybrid_skills_root(csk_home)
    for item in decls:
        line = f"{item.decl.name:<24} {item.decl.ref.kind:<8} {item.decl.ref.value:<12} targets: {', '.join(item.targets)}"
        if args.hybrid_command == "status":
            marker_path = store / item.decl.name / ".csk-install.json"
            state = "missing"
            if marker_path.exists():
                try:
                    marker = json.loads(marker_path.read_text(encoding="utf-8"))
                    commit = marker.get("commit")
                    state = f"installed {str(commit)[:7]}" if isinstance(commit, str) else "installed"
                except ValueError:
                    state = "unreadable marker"
            line += f"  [{state}]"
        print(line)
    return EXIT_OK


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    path = config.config_path()
    non_interactive = getattr(args, "non_interactive", False)
    if path.exists():
        if non_interactive:
            if not getattr(args, "force", False):
                print(f"error: config exists at {path}; pass --force to overwrite", file=sys.stderr)
                return EXIT_CONFIG
        elif not getattr(args, "force", False):
            answer = input(f"Config exists at {path}. Overwrite? [y/N] ").strip().lower()
            if answer != "y":
                print(f"Kept existing config: {path}")
                return EXIT_OK
    skills_root = (getattr(args, "skills_root", None) or "").strip()
    if not skills_root and not non_interactive:
        skills_root = input("skills_root: ").strip()
    if not skills_root:
        print("error: skills_root must not be empty", file=sys.stderr)
        return EXIT_CONFIG
    preferred_locale = getattr(args, "preferred_locale", None)
    if preferred_locale is None and not non_interactive:
        preferred_locale = input("preferred_locale [none]: ").strip() or None
    default_agents_raw = getattr(args, "default_agents", None)
    if default_agents_raw is None and not non_interactive:
        default_agents_raw = input("default_agents comma-separated [codex_cli]: ").strip()
    default_agents = [item.strip() for item in (default_agents_raw or "").split(",") if item.strip()] or ["codex_cli"]
    cfg = config.GlobalConfig(
        path=path,
        skills_root=Path(skills_root).expanduser(),
        preferred_locale=preferred_locale,
        default_agents=default_agents,
        adapter_mode="auto",
        worktree_alias_pattern=config.DEFAULT_WORKTREE_ALIAS_PATTERN,
        projects={},
    )
    config.save_config(cfg)
    print(f"Wrote {path}")
    print("Install shell hook with: eval \"$(csk shell-init bash)\"")
    return EXIT_OK


def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    try:
        root = target.resolve()
    except FileNotFoundError as exc:
        raise manifest.ManifestError(f"target path does not exist: {target}") from exc
    if not root.exists() or not root.is_dir():
        raise manifest.ManifestError(f"target path does not exist: {root}")

    parent = _nearest_parent_manifest(root)
    if parent is not None:
        raise manifest.ManifestError(f"already inside project at {parent}; nested projects unsupported")

    agents = _init_agents(args)
    raw_alias = args.alias or root.name
    alias = project_resolver.clean_alias(raw_alias)
    if not alias:
        raise manifest.ManifestError("--alias must contain at least one alphanumeric character")

    manifest.ensure_project_manifest(root, alias=alias, agents=agents)
    gitignore_gate.append_entries(
        root / ".gitignore",
        adapters.all_gitignore_entries() + [dev_substitutions.DEV_MANIFEST_NAME],
    )
    if not _is_inside_git_worktree(root):
        print(
            "csk init: WARNING - target is not inside a git repository\n"
            "  Skillfile.json and .gitignore have been written.\n"
            "  The .gitignore block has no effect until this directory becomes a git repository.\n"
            "  Run 'git init' here if you intend to track it.",
            file=sys.stderr,
        )
    print(f"Initialized CocoaSkill project at {root}")
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
    manifest.ensure_project_manifest(path.expanduser(), alias=alias, agents=cfg.default_agents)
    print(f"Added project {alias}: {path}")
    return EXIT_OK


def _cmd_skill_check(args: argparse.Namespace) -> int:
    skill_dir = Path(args.path).expanduser()
    if not skill_dir.is_absolute():
        skill_dir = Path.cwd() / skill_dir
    skill_dir = skill_dir.resolve()
    issues = skillcheck.validate_skill(skill_dir, locale_value=args.locale)
    if args.json_output:
        print(json.dumps([skillcheck.issue_to_dict(issue) for issue in issues], ensure_ascii=False, indent=2))
    else:
        if not issues:
            print(f"{skill_dir}: ok")
        for issue in issues:
            print(skillcheck.format_issue(issue))
    return EXIT_PARTIAL_FAIL if skillcheck.has_errors(issues) else EXIT_OK


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
    cfg = _cfg_with_audit_override(cfg, args)
    results = installer.install(cfg, alias=args.alias, options=options)
    failed = False
    for result in results:
        for message in result.messages:
            print(message)
        for error in result.errors:
            failed = True
            print(f"{result.alias}: {error}", file=sys.stderr)
        if args.alias is not None and result.status == "skipped":
            # An explicitly requested project that was refused must not look
            # like a successful install to scripts and CI.
            failed = True
            print(f"{result.alias}: skipped; nothing installed", file=sys.stderr)
    return EXIT_PARTIAL_FAIL if failed else EXIT_OK


def _cmd_global_update(cfg: config.GlobalConfig) -> int:
    result = global_install.update(cfg)
    for message in result.messages:
        print(message)
    for error in result.errors:
        print(error, file=sys.stderr)
    return EXIT_PARTIAL_FAIL if result.failed else EXIT_OK


def _cmd_global_install(cfg: config.GlobalConfig, args: argparse.Namespace) -> int:
    options = installer.InstallOptions(
        dry_run=getattr(args, "dry_run", False),
        strict_tags=getattr(args, "strict_tags", False),
        verbose=getattr(args, "verbose", False),
    )
    cfg = _cfg_with_audit_override(cfg, args)
    result = global_install.install(cfg, options=options)
    for message in result.messages:
        print(message)
    for error in result.errors:
        print(f"global: {error}", file=sys.stderr)
    if not options.dry_run:
        gc.collect_runtime(cfg, cfg.path.parent)
    return EXIT_PARTIAL_FAIL if result.failed else EXIT_OK


def _cmd_audit(args: argparse.Namespace) -> int:
    cfg = config.load_config()
    if args.allow:
        if args.target is not None or args.all or args.global_scope or args.json:
            raise ValueError("--allow cannot be combined with targets, --all, --global, or --json")
        path = audit_trust.pin_content_hash(
            cfg.path.parent,
            args.allow,
            reason=args.reason or "",
            pinned_by=getpass.getuser(),
        )
        print(f"Pinned audit trust for {args.allow.lower()}: {path}")
        return EXIT_OK
    if args.all:
        if args.target is not None or args.global_scope:
            raise ValueError("--all cannot be combined with a project target or --global")
        reports = audit_runner.audit_projects(cfg, alias=None) + audit_runner.audit_global(cfg)
    elif args.global_scope:
        if getattr(args, "target", None) is not None or getattr(args, "all", False):
            raise ValueError("--global cannot be combined with a project target or --all")
        reports = audit_runner.audit_global(cfg)
    else:
        cfg, alias = _cfg_and_alias_for_target(cfg, args)
        reports = audit_runner.audit_projects(cfg, alias=alias)

    if args.json:
        print(json.dumps(audit_pipeline.reports_to_payload(reports), indent=2, sort_keys=True))
    else:
        print(audit_pipeline.render_reports(reports))
    return EXIT_PARTIAL_FAIL if any(report.decision in {Decision.BLOCK, Decision.REQUIRE_PIN} for report in reports) else EXIT_OK


def _cfg_with_audit_override(cfg: config.GlobalConfig, args: argparse.Namespace) -> config.GlobalConfig:
    audit_mode = getattr(args, "audit", None)
    if audit_mode is None:
        return cfg
    return replace(cfg, audit=replace(cfg.audit, enabled=True, mode=audit_mode))


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
    cfg, alias = _cfg_and_alias_for_target(cfg, args)
    args.alias = alias
    return cfg, args


def _cfg_and_alias_for_target(
    cfg: config.GlobalConfig,
    args: argparse.Namespace,
) -> tuple[config.GlobalConfig, str | None]:
    target = getattr(args, "target", None)
    if getattr(args, "all", False):
        if target is not None:
            raise ValueError("--all cannot be combined with a project target")
        if not cfg.projects:
            raise ValueError("no registered projects; use 'csk project add' or run 'csk install' inside a project")
        return cfg, None

    path_target = _path_target_from_args(args)
    if target is not None and path_target is None:
        return cfg, target

    path_target = path_target or Path.cwd()
    try:
        resolved = project_resolver.resolve(path_target, worktree_alias_pattern=cfg.worktree_alias_pattern)
    except project_resolver.ProjectResolutionError as exc:
        if target is not None:
            raise
        raise project_resolver.ProjectResolutionError(_missing_current_project_message(args.command, path_target, cfg)) from exc
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
    return updated, resolved.checkout_alias


def _resolve_project_root(cfg: config.GlobalConfig, target: str | None) -> Path:
    if target and not _looks_like_path(target) and target in cfg.projects:
        return cfg.projects[target].path
    start = Path(target).expanduser() if target else Path.cwd()
    resolved = project_resolver.resolve(start, worktree_alias_pattern=cfg.worktree_alias_pattern)
    return resolved.root


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


def _missing_current_project_message(command: str, start: Path, cfg: config.GlobalConfig) -> str:
    lines = [
        f"no Skillfile.json found at or above {start.resolve()}",
        "  hint: cd to a project root",
        f"  hint: or run 'csk {command} /abs/path/to/project'",
    ]
    if cfg.projects:
        lines.append(f"  hint: or run 'csk {command} --all' for multi-project sync ({len(cfg.projects)} projects configured)")
    return "\n".join(lines)


def _init_agents(args: argparse.Namespace) -> list[str]:
    if args.agents is not None:
        agents = [item.strip() for item in args.agents.split(",") if item.strip()]
        if not agents:
            raise manifest.ManifestError("--agents must contain at least one agent")
        return agents
    try:
        cfg = config.load_config()
    except config.ConfigError:
        return list(config.DEFAULT_AGENTS)
    return list(cfg.default_agents or config.DEFAULT_AGENTS)


def _nearest_parent_manifest(root: Path) -> Path | None:
    for parent in root.parents:
        if (parent / manifest.MANIFEST_NAME).exists():
            return parent
    return None


def _is_inside_git_worktree(root: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


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
