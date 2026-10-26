from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import git_ops, locking
from .tool_paths import resolve_tool


class GitignoreError(Exception):
    code = "gitignore_required"


class ExcludeError(GitignoreError):
    code = "git_exclude_invalid"


_held = threading.local()


@contextmanager
def exclude_file_lock(path: Path) -> Iterator[None]:
    """Serialize shared exclude updates, including replay after process death."""
    lock_path = path.parent.parent / "csk-exclude.lock"
    held = getattr(_held, "paths", None)
    if held is None:
        held = _held.paths = set()
    if lock_path in held:
        yield
        return
    # No PID/owner record is written: a crash even during lock acquisition
    # cannot leave a partial record that would strand the next OS-lock holder.
    # Acquire after the home lock (or alone for init/schema 1); never unlink.
    fd: int | None = None
    acquired = False
    try:
        try:
            flags = os.O_CREAT | os.O_RDWR
            for flag in ("O_CLOEXEC", "O_NOFOLLOW", "O_BINARY", "O_NONBLOCK"):
                flags |= getattr(os, flag, 0)
            fd = os.open(lock_path, flags, 0o600)
            if not locking._path_matches_fd(lock_path, fd):
                raise ExcludeError("Git exclude lock must be a stable regular file")
            deadline = time.monotonic() + locking._timeout_from_env()
            while not (acquired := locking._try_file_lock(fd)):
                if time.monotonic() >= deadline:
                    raise ExcludeError("Timed out waiting for the Git exclude lock")
                time.sleep(0.05)
            if not locking._path_matches_fd(lock_path, fd):
                raise ExcludeError("Git exclude lock identity changed while acquiring")
        except OSError as exc:
            raise ExcludeError(f"Cannot lock Git info/exclude: {exc}") from exc
        held.add(lock_path)
        try:
            yield
        finally:
            held.remove(lock_path)
    finally:
        if fd is not None:
            try:
                if acquired:
                    locking._unlock_file(fd)
            finally:
                os.close(fd)


@contextmanager
def exclude_lock(project_root: Path, mode: str) -> Iterator[None]:
    if mode == "exclude":
        with exclude_file_lock(ignore_path(project_root, mode)):
            yield
    else:
        yield


def project_key(project_root: Path) -> str:
    discovery = git_ops.run_git(
        [resolve_tool("git"), "-C", str(project_root), "rev-parse", "--show-toplevel", "--absolute-git-dir"],
        text=True, encoding="utf-8", capture_output=True,
    )
    if discovery.returncode:
        raise ExcludeError("Cannot resolve Git worktree")
    roots = discovery.stdout.removesuffix("\n").split("\n")
    if len(roots) != 2:
        raise ExcludeError("Cannot resolve Git worktree identity")
    relative = _project_relative(project_root, Path(roots[0]))
    common = ignore_path(project_root, "exclude").parent.parent
    metadata = _project_relative(Path(roots[1]), common)
    identity = metadata.as_posix() + "\0" + relative.as_posix()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _block_range(data: bytes, key: str) -> tuple[int, int] | None:
    start_marker = f"# CocoaSkill project {key}".encode()
    end_marker = f"# End CocoaSkill project {key}".encode()
    lines = data.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == start_marker]
    ends = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n") == end_marker]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ExcludeError("Git info/exclude has an incomplete or duplicate project block")
    start, end = starts[0], ends[0]
    if any(line.startswith((b"# CocoaSkill", b"# End CocoaSkill")) for line in lines[start + 1:end]):
        raise ExcludeError("Git info/exclude has nested project blocks")
    return sum(map(len, lines[:start])), sum(map(len, lines[:end + 1]))


def project_block(data: bytes, key: str) -> bytes | None:
    span = _block_range(data, key)
    return None if span is None else data[span[0]:span[1]]


def replace_project_block(data: bytes, key: str, block: bytes | None) -> bytes:
    """Replace only this project's slice; all other bytes remain opaque."""
    if block is not None and project_block(block, key) != block:
        raise ExcludeError("Invalid staged Git exclude project block")
    span = _block_range(data, key)
    if span is not None:
        return data[:span[0]] + (block or b"") + data[span[1]:]
    if block is None:
        return data
    return data + (b"\n" if data and not data.endswith(b"\n") else b"") + block


def write_exclude(path: Path, data: bytes) -> None:
    """Atomically publish under the common lock, preserving target mode."""
    _exclude_bytes(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".csk-exclude-", delete=False) as stream:
        temporary_path = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def legacy_block(project_root: Path) -> bool:
    path = project_root / ".gitignore"
    return path.is_file() and b"# CocoaSkill" in path.read_bytes().splitlines()


def effective_mode(project_root: Path, declared: str | None) -> str:
    return declared or ("gitignore" if legacy_block(project_root) else "exclude")


def _project_relative(project_root: Path, worktree: Path) -> Path:
    """Find the worktree prefix by identity, including case/Unicode aliases."""
    current = Path(locking.canonical_project_identity(project_root))
    parts: list[str] = []
    try:
        while not current.samefile(worktree):
            if current.parent == current:
                raise GitignoreError("Project is outside the Git worktree")
            parts.append(current.name)
            current = current.parent
    except OSError as exc:
        raise GitignoreError("Cannot locate project in the Git worktree") from exc
    return Path(*reversed(parts))


def ignore_path(project_root: Path, mode: str) -> Path:
    if mode == "gitignore":
        return project_root / ".gitignore"
    result = git_ops.run_git(
        [resolve_tool("git"), "-C", str(project_root), "rev-parse", "--git-common-dir"],
        text=True, encoding="utf-8", capture_output=True,
    )
    if result.returncode:
        raise ExcludeError("Cannot resolve Git common directory; a git repository is required")
    common = Path(result.stdout.removesuffix("\n"))
    if not common.is_absolute():
        common = project_root / common
    return common.resolve() / "info/exclude"


def _exclude_bytes(path: Path) -> bytes:
    parent = path.parent
    while not parent.exists() and not parent.is_symlink():
        parent = parent.parent
    try:
        parent_mode = parent.lstat().st_mode
        if not stat.S_ISDIR(parent_mode) or not parent_mode & 0o222 or not os.access(parent, os.W_OK):
            raise ExcludeError("Git info/exclude parent must be a writable directory, never a symlink")
    except OSError as exc:
        raise ExcludeError("Cannot access Git info/exclude parent") from exc
    try:
        info = path.lstat()
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise ExcludeError("Cannot read Git info/exclude") from exc
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o222 or not os.access(path, os.W_OK):
        raise ExcludeError("Git info/exclude must be a writable regular file, never a symlink")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ExcludeError("Cannot read Git info/exclude") from exc


def _plan_exclude(project_root: Path, entries: list[str]) -> bytes | None:
    path = ignore_path(project_root, "exclude")
    existing = _exclude_bytes(path)
    discovery = git_ops.run_git(
        [resolve_tool("git"), "-C", str(project_root), "rev-parse", "--show-toplevel"],
        text=True, encoding="utf-8", capture_output=True,
    )
    if discovery.returncode:
        raise ExcludeError("Cannot resolve Git worktree")
    relative = _project_relative(project_root, Path(discovery.stdout.removesuffix("\n")))
    prefix = "" if relative == Path(".") else relative.as_posix() + "/"
    # info/exclude is rooted at the worktree, including for nested projects.
    patterns = {
        ("/" + "".join("\\" + char if char in "\\*?[]!# " else char
                       for char in prefix + entry.lstrip("/"))).encode("utf-8")
        for entry in entries
    }
    key = project_key(project_root)
    block = (f"# CocoaSkill project {key}\n".encode()
             + b"\n".join(sorted(patterns)) + b"\n"
             + f"# End CocoaSkill project {key}\n".encode()) if patterns else None
    desired = replace_project_block(existing, key, block)
    return desired if desired != existing else None


def missing_entries(project_root: Path, entries: list[str]) -> list[str]:
    missing: list[str] = []
    for entry in entries:
        probe = (
            entry.rstrip("/").lstrip("/") + "/.csk-probe"
            if entry.endswith("/") else entry.lstrip("/")
        )
        proc = git_ops.run_git(
            [resolve_tool("git"), "-C", str(project_root), "check-ignore", "-q", probe],
            text=True, encoding="utf-8",
            capture_output=True,
        )
        if proc.returncode != 0:
            missing.append(entry)
    return missing


def ensure_ignored(project_root: Path, entries: list[str], *, fix: bool = False, mode: str = "gitignore") -> None:
    if mode == "exclude":
        with exclude_lock(project_root, mode):
            path = ignore_path(project_root, mode)
            original = _exclude_bytes(path)
            proposed = plan_ignored(project_root, entries, fix=True, mode=mode)
            with tempfile.TemporaryDirectory(prefix=".csk-ignore-") as temporary:
                verify_planned_ignored(project_root, entries, proposed, Path(temporary), mode=mode)
            if proposed is not None:
                if _exclude_bytes(path) != original:
                    raise ExcludeError("Git info/exclude changed during planning")
                write_exclude(path, proposed)
        return
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


def plan_ignored(project_root: Path, entries: list[str], *, fix: bool, mode: str = "gitignore") -> bytes | None:
    """Use the ignore gate without writing; the caller transacts a repair."""
    if mode == "exclude":
        return _plan_exclude(project_root, entries)
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
    project_root: Path, entries: list[str], proposed: bytes | None, staging_root: Path,
    *, mode: str = "gitignore",
) -> None:
    """Ask Git to evaluate a private copy of the proposed ignore hierarchy.

    Reuse live metadata read-only for gitignore; exclude uses private metadata
    containing the proposed info/exclude. Never change the live index/worktree.
    Include ancestor and nested ignore files:
    a child negation can defeat an exact root rule and must refuse publication.
    """
    if not entries:
        return
    executable = resolve_tool("git")
    discovery = git_ops.run_git(
        [executable, "-C", str(project_root), "rev-parse", "--show-toplevel", "--absolute-git-dir"],
        text=True, encoding="utf-8", capture_output=True,
    )
    roots = discovery.stdout.removesuffix("\n").split("\n")
    if discovery.returncode or len(roots) != 2:
        raise GitignoreError("Cannot evaluate the proposed MCP ignore policy")
    worktree, metadata = map(Path, roots)
    metadata_args: list[str] = []
    if mode == "exclude":
        live_exclude = ignore_path(project_root, mode)
        current = _exclude_bytes(live_exclude)
        for entry in entries:
            if is_tracked(project_root, entry.lstrip("/").rstrip("/")):
                raise GitignoreError(f"Owned path is already tracked: {entry}")
        # Evaluate a private metadata directory rather than touching live info/exclude.
        # Includes retain the original config paths (and relative include semantics).
        private = staging_root / "ignore-metadata"
        for directory in ("objects", "refs", "info"):
            (private / directory).mkdir(parents=True, exist_ok=True)
        (private / "HEAD").write_text("ref: refs/heads/ignore-probe\n", encoding="utf-8")
        (private / "info/exclude").write_bytes(current if proposed is None else proposed)
        common = live_exclude.parent.parent
        metadata_args = ["-c", "include.path=" + str(common / "config")]
        worktree_config = metadata / "config.worktree"
        worktree_enabled = git_ops.run_git(
            [executable, "-C", str(project_root), "config", "--bool", "--get", "extensions.worktreeConfig"],
            text=True, encoding="utf-8", capture_output=True,
        )
        if worktree_enabled.returncode not in (0, 1):
            raise GitignoreError("Cannot resolve Git worktree configuration")
        if worktree_config.exists() and worktree_enabled.stdout.strip() == "true":
            metadata_args += ["-c", "include.path=" + str(worktree_config)]
        ignore_case = git_ops.run_git(
            [executable, "-C", str(project_root), "config", "--bool", "--get", "core.ignoreCase"],
            text=True, encoding="utf-8", capture_output=True,
        )
        if ignore_case.returncode not in (0, 1):
            raise GitignoreError("Cannot resolve Git ignore matching configuration")
        # Conditional gitdir includes must keep their live matching semantics.
        metadata_args += ["-c", "core.ignoreCase=" + (ignore_case.stdout.strip() or "false")]
        metadata = private
    # Git resolves a relative excludesFile from the live worktree root, not
    # from the config file or the invocation's subdirectory. Freeze that source
    # before changing worktrees. --path also applies Git's own ~/ expansion.
    excludes = git_ops.run_git(
        [executable, "-C", str(worktree), "config", "--path", "--get", "core.excludesFile"],
        text=True, encoding="utf-8", capture_output=True,
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
    else:
        # Git also permits relative XDG/HOME values for its implicit global
        # excludes source. Freeze those against the same live root too.
        xdg = os.environ.get("XDG_CONFIG_HOME")
        home = os.environ.get("HOME")
        default = Path(xdg) / "git/ignore" if xdg else (
            Path(home) / ".config/git/ignore" if home else None
        )
        if default is not None and not default.is_absolute():
            config_args = ["-c", "core.excludesFile=" + str(worktree / default)]
    # info/exclude remains in the reused metadata. Absolute implicit global
    # excludes and config includes retain their environment and source paths.
    try:
        project_rel = _project_relative(project_root, worktree)
        staged = staging_root / "ignore-worktree"
        staged.mkdir(exist_ok=True)
        probes = [project_rel / (
            entry.rstrip("/").lstrip("/") + "/.csk-probe"
            if entry.endswith("/") else entry.lstrip("/")
        ) for entry in entries]
        directories = {Path(".")}
        for probe in probes:
            directories.update(probe.parents)
        for rel in sorted(directories, key=lambda p: len(p.parts)):
            destination = staged / rel
            destination.mkdir(parents=True, exist_ok=True)
            source = worktree / rel / ".gitignore"
            target = destination / ".gitignore"
            if mode == "gitignore" and rel == project_rel and proposed is not None:
                target.write_bytes(proposed)
            elif source.is_symlink():
                raise GitignoreError("Cannot evaluate a linked ignore policy")
            elif source.exists():
                target.write_bytes(source.read_bytes())
            elif target.exists():
                target.unlink()
        for entry, probe in zip(entries, probes):
            result = git_ops.run_git(
                [executable, *metadata_args, *config_args, "--git-dir=" + str(metadata), "--work-tree=" + str(staged),
                 "-C", str(staged), "check-ignore", "--no-index", "-q", "--", probe.as_posix()],
                text=True, encoding="utf-8", capture_output=True,
            )
            if result.returncode:
                raise GitignoreError(
                    f"Proposed MCP ignore policy does not effectively ignore {entry}; "
                    "repair conflicting ignore rules before retrying"
                )
    except (OSError, ValueError) as exc:
        raise GitignoreError("Cannot evaluate the proposed MCP ignore policy") from exc
