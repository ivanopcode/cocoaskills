from __future__ import annotations

from pathlib import Path

from . import git_ops
from .tool_paths import resolve_tool


class GitignoreError(Exception):
    code = "gitignore_required"


def missing_entries(project_root: Path, entries: list[str]) -> list[str]:
    missing: list[str] = []
    for entry in entries:
        probe = (
            entry.lstrip("/")
            if entry.startswith("/") and not entry.endswith("/")
            else entry.rstrip("/") + "/.csk-probe"
        )
        proc = git_ops.run_git(
            [resolve_tool("git"), "-C", str(project_root), "check-ignore", "-q", probe],
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            missing.append(entry)
    return missing


def ensure_ignored(project_root: Path, entries: list[str], *, fix: bool = False) -> None:
    missing = missing_entries(project_root, entries)
    if not missing:
        return
    if fix:
        append_entries(project_root / ".gitignore", missing)
        missing = missing_entries(project_root, entries)
        if not missing:
            return
    raise GitignoreError(
        "Generated CocoaSkill paths are not ignored by git. Missing entries: "
        + ", ".join(missing)
    )


def append_entries(path: Path, entries: list[str]) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = set(line.strip() for line in existing.splitlines())
    to_add = [entry for entry in entries if entry not in lines]
    if not to_add:
        return
    prefix = "" if existing.endswith("\n") or not existing else "\n"
    header = "" if "# CocoaSkill" in lines else "# CocoaSkill\n"
    block = prefix + header + "\n".join(to_add) + "\n"
    path.write_text(existing + block, encoding="utf-8")


def is_tracked(project_root: Path, relpath: str) -> bool:
    proc = git_ops.run_git(
        [
            resolve_tool("git"),
            "-C",
            str(project_root),
            "ls-files",
            "--error-unmatch",
            "--",
            relpath,
        ],
        capture_output=True,
    )
    if proc.returncode not in (0, 1):
        raise GitignoreError("Cannot determine whether the native config is tracked")
    return proc.returncode == 0


def plan_ignored(project_root: Path, entries: list[str], *, fix: bool) -> bytes | None:
    """Use the ignore gate without writing; the caller transacts a repair."""
    missing = missing_entries(project_root, entries)
    if not missing:
        return None
    if not fix:
        raise GitignoreError(
            "Generated CocoaSkill paths are not ignored by git. Missing entries: "
            + ", ".join(missing)
        )
    path = project_root / ".gitignore"
    if path.is_symlink():
        raise GitignoreError("Cannot repair a linked .gitignore")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = {line.strip() for line in existing.splitlines()}
    # Effective missing entries may already occur before a later negation.
    # Append them last so the proposed root policy actually repairs that case.
    to_add = missing
    prefix = "" if existing.endswith("\n") or not existing else "\n"
    header = "" if "# CocoaSkill" in lines else "# CocoaSkill\n"
    return (existing + prefix + header + "\n".join(to_add) + "\n").encode(
        "utf-8"
    )


def verify_planned_ignored(
    project_root: Path, entries: list[str], proposed: bytes | None, staging_root: Path
) -> None:
    """Ask Git to evaluate a private copy of the proposed ignore hierarchy.

    Reuse repository metadata read-only, without creating a repository or
    changing the index/worktree. Include ancestor and nested ignore files:
    a child negation can defeat an exact root rule and must refuse publication.
    """
    if not entries:
        return
    executable = resolve_tool("git")
    discovery = git_ops.run_git(
        [executable, "-C", str(project_root), "rev-parse", "--show-toplevel", "--absolute-git-dir"],
        text=True, capture_output=True,
    )
    roots = discovery.stdout.splitlines()
    if discovery.returncode or len(roots) != 2:
        raise GitignoreError("Cannot evaluate the proposed MCP ignore policy")
    worktree, metadata = map(Path, roots)
    # Git resolves a relative excludesFile from the live worktree root, not
    # from the config file or the invocation's subdirectory. Freeze that source
    # before changing worktrees. --path also applies Git's own ~/ expansion.
    excludes = git_ops.run_git(
        [executable, "-C", str(worktree), "config", "--path", "--get", "core.excludesFile"],
        text=True, capture_output=True,
    )
    if excludes.returncode not in (0, 1):
        raise GitignoreError("Cannot resolve the live Git excludes source")
    config_args: list[str] = []
    if excludes.returncode == 0:
        value = excludes.stdout.removesuffix("\n")
        if value:
            source_path = Path(value)
            if not source_path.is_absolute():
                source_path = worktree / source_path
            value = str(source_path)
        config_args = ["-c", "core.excludesFile=" + value]
    # info/exclude remains in the reused metadata; implicit XDG/HOME excludes
    # and config includes retain their original environment and source paths.
    try:
        project_rel = project_root.resolve().relative_to(worktree.resolve())
        staged = staging_root / "ignore-worktree"
        staged.mkdir(exist_ok=True)
        probes = [
            project_rel / entry.lstrip("/") for entry in entries
        ]
        directories = {Path(".")}
        for probe in probes:
            directories.update(probe.parents)
        for rel in sorted(directories, key=lambda p: len(p.parts)):
            destination = staged / rel
            destination.mkdir(parents=True, exist_ok=True)
            source = worktree / rel / ".gitignore"
            target = destination / ".gitignore"
            if rel == project_rel and proposed is not None:
                target.write_bytes(proposed)
            elif source.is_symlink():
                raise GitignoreError("Cannot evaluate a linked ignore policy")
            elif source.exists():
                target.write_bytes(source.read_bytes())
            elif target.exists():
                target.unlink()
        for entry, probe in zip(entries, probes):
            result = git_ops.run_git(
                [executable, *config_args, "--git-dir=" + str(metadata), "--work-tree=" + str(staged),
                 "-C", str(staged), "check-ignore", "--no-index", "-q", "--", probe.as_posix()],
                text=True, capture_output=True,
            )
            if result.returncode:
                raise GitignoreError(
                    f"Proposed MCP ignore policy does not effectively ignore {entry}; "
                    "repair conflicting ignore rules before retrying"
                )
    except (OSError, ValueError) as exc:
        raise GitignoreError("Cannot evaluate the proposed MCP ignore policy") from exc
