from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, overload

from . import git_trust
from .identifiers import is_valid_portable_path
from .tool_paths import resolve_tool


# Skillfile 'git' URLs reach git clone as untrusted input. Restricting the
# transport protocols blocks remote-helper URLs such as ext::sh -c ... which
# would otherwise execute arbitrary commands during csk install.
ALLOWED_GIT_PROTOCOLS = "file:git:http:https:ssh"
MAX_ARCHIVE_ENTRIES = 100_000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


class GitError(Exception):
    code: str | None = None


@contextmanager
def _private_trust() -> Iterator[git_trust.PrivateGitTrust]:
    try:
        with git_trust.private_git_trust() as trust:
            yield trust
    except git_trust.GitTrustError as exc:
        error = GitError(str(exc))
        error.code = exc.code
        raise error from exc


def _trust_environment(trust: git_trust.PrivateGitTrust) -> dict[str, str]:
    environment = trust.environment(os.environ)
    environment["GIT_ALLOW_PROTOCOL"] = ALLOWED_GIT_PROTOCOLS
    environment["GIT_NO_LAZY_FETCH"] = "1"
    return environment


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError as exc:
        raise GitError("git executable not found; install git and ensure it is on PATH") from exc


@overload
def run_git(command: list[str] | tuple[str, ...], *, text: Literal[True], **kwargs: Any) -> subprocess.CompletedProcess[str]: ...


@overload
def run_git(command: list[str] | tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[Any]: ...


def run_git(command: list[str] | tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Guard ancillary Git commands, including discovery and credential plumbing."""
    with _private_trust() as trust:
        environment = trust.environment(kwargs.pop("env", os.environ))
        environment["GIT_NO_LAZY_FETCH"] = "1"
        return subprocess.run(git_trust.pinned_command(command, environment), env=environment, **kwargs)


@dataclass(frozen=True)
class ResolvedRef:
    kind: str
    ref: str
    commit: str


def _git_executable() -> str:
    try:
        return resolve_tool("git")
    except FileNotFoundError as exc:
        raise GitError(str(exc)) from exc


def git(repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    with _private_trust() as trust:
        environment = _trust_environment(trust)
        cmd = git_trust.pinned_command([_git_executable(), "-C", str(repo), *args], environment)
        proc = _run(list(cmd), text=True, capture_output=True, env=environment)
    if check and proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise GitError(f"git {' '.join(args)} failed in {repo}: {stderr}")
    return proc


def clone_repo(remote_url: str, destination: Path) -> None:
    if destination.exists():
        raise GitError(f"Clone destination already exists: {destination}")
    if not remote_url.strip() or remote_url.startswith("-"):
        raise GitError(f"Refusing to clone suspicious git URL: {remote_url!r}")
    with _private_trust() as trust:
        environment = _trust_environment(trust)
        executable = _git_executable()
        discovery = _run(list(git_trust.pinned_command(
            [executable, "ls-remote", "--get-url", "--", remote_url], environment,
        )), text=True, capture_output=True, env=environment)
        if discovery.returncode or not discovery.stdout.strip():
            raise GitError("Cannot resolve clone URL through Git configuration")
        effective_url = discovery.stdout.strip()
        destination.parent.mkdir(parents=True, exist_ok=True)
        proc = _run(list(git_trust.pinned_command(
            [executable, "clone", "--no-recurse-submodules", "--", remote_url, str(destination)],
            environment, url=effective_url,
        )), text=True, capture_output=True, env=environment)
    if proc.returncode != 0:
        if destination.exists():
            shutil.rmtree(destination)
        stderr = proc.stderr.strip() or proc.stdout.strip()
        from .git_admission import _classify_git_failure_output

        if _classify_git_failure_output(stderr) == "redirect":
            raise GitError(
                "git clone failed: HTTP redirects are refused. Verify the provider's "
                "canonical Git clone URL in its Clone UI or API; .git is provider-specific. "
                "Do not use an arbitrary redirect target: it may be a login or browser page."
            )
        raise GitError(f"git clone failed for {remote_url} -> {destination}: {stderr}")


def ensure_git_repo(repo: Path) -> None:
    if not (repo / ".git").exists():
        raise GitError(f"Not a git repository: {repo}")


def resolve_ref(repo: Path, kind: str, value: str) -> ResolvedRef:
    ensure_git_repo(repo)
    if not value or value.startswith("-") or any(ord(character) < 0x20 for character in value):
        raise GitError(f"Refusing suspicious git ref: {value!r}")
    if kind == "tag":
        commit = _rev_parse(repo, f"refs/tags/{value}^{{commit}}")
    elif kind == "branch":
        origin = git(repo, ["rev-parse", "--verify", f"refs/remotes/origin/{value}"], check=False)
        if origin.returncode == 0:
            commit = origin.stdout.strip()
        else:
            commit = _rev_parse(repo, f"refs/heads/{value}")
    elif kind == "revision":
        commit = _rev_parse(repo, f"{value}^{{commit}}")
    else:
        raise GitError(f"Unknown ref kind: {kind}")
    return ResolvedRef(kind=kind, ref=value, commit=commit)


def archive(repo: Path, commit: str, destination: Path) -> None:
    ensure_git_repo(repo)
    if not commit or commit.startswith("-") or not all(character in "0123456789abcdef" for character in commit):
        raise GitError(f"Refusing suspicious archive commit: {commit!r}")
    with _private_trust() as trust:
        destination.mkdir(parents=True, exist_ok=True)
        environment = _trust_environment(trust)
        proc = _run(list(git_trust.pinned_command(
            [_git_executable(), "-C", str(repo), "archive", "--format=tar", commit], environment,
        )), capture_output=True, env=environment)
    if proc.returncode != 0:
        raise GitError(f"git archive failed in {repo}: {proc.stderr.decode(errors='replace').strip()}")
    with tarfile.open(fileobj=BytesIO(proc.stdout), mode="r:") as archive_file:
        _extract_archive(archive_file, destination)


def fetch_repo(repo: Path) -> None:
    ensure_git_repo(repo)
    # Native discovery preserves include/includeIf and Git's boolean syntax.
    # The shared Git entry point checks trust even for this local command.
    remotes = sorted(git(repo, ["remote"]).stdout.splitlines())
    if not remotes:
        return
    if "origin" in remotes:
        remote = "origin"
    elif len(remotes) == 1:
        remote = remotes[0]
    else:
        raise GitError(
            f"Ambiguous fetch remote in {repo}: {', '.join(remotes)}; "
            "configure origin or keep a single remote"
        )
    # Installed markers, rather than cache refs, anchor moved-tag policy.
    # One remote owns the shared tag namespace; fetching all remotes would
    # make conflicting tags depend on fetch order.
    effective_url = git(repo, ["remote", "get-url", "--", remote]).stdout.strip()
    if not effective_url:
        raise GitError("Cannot resolve fetch URL through Git configuration")
    with _private_trust() as trust:
        environment = _trust_environment(trust)
        args = [
            "-C", str(repo), "fetch", "--force", "--prune", "--no-recurse-submodules", "--", remote,
            f"+refs/heads/*:refs/remotes/{remote}/*", "+refs/tags/*:refs/tags/*",
        ]
        proc = _run(list(git_trust.pinned_command(
            [_git_executable(), *args], environment, url=effective_url,
        )), text=True, capture_output=True, env=environment)
    if proc.returncode != 0:
        raise GitError(f"git fetch failed in {repo}: {proc.stderr.strip()}")


def fetch_all(skills_root: Path) -> list[tuple[Path, str | None]]:
    results: list[tuple[Path, str | None]] = []
    for child in sorted(skills_root.iterdir()):
        if not child.is_dir() or not (child / ".git").exists():
            continue
        try:
            fetch_repo(child)
            results.append((child, None))
        except GitError as exc:
            results.append((child, str(exc)))
    return results


def repository_has_submodules(snapshot: Path) -> bool:
    return (snapshot / ".gitmodules").exists()


def _rev_parse(repo: Path, spec: str) -> str:
    proc = git(repo, ["rev-parse", "--verify", spec])
    commit = proc.stdout.strip()
    if not commit:
        raise GitError(f"Could not resolve {spec} in {repo}")
    return commit


def _extract_archive(archive_file: tarfile.TarFile, destination: Path) -> None:
    members = archive_file.getmembers()
    if len(members) > MAX_ARCHIVE_ENTRIES:
        raise GitError(f"Git archive exceeds the {MAX_ARCHIVE_ENTRIES}-entry limit")
    total_size = 0
    seen: dict[str, str] = {}
    destination_resolved = destination.resolve()
    for member in members:
        relative = member.name.rstrip("/")
        if not relative or not is_valid_portable_path(relative):
            raise GitError(f"Non-portable path in git archive: {member.name}")
        if member.issym() or member.islnk():
            raise GitError(f"Links in git archives are unsupported: {member.name}")
        if not (member.isfile() or member.isdir()):
            raise GitError(f"Unsupported entry type in git archive: {member.name}")
        total_size += member.size
        if total_size > MAX_ARCHIVE_BYTES:
            raise GitError(f"Git archive exceeds the {MAX_ARCHIVE_BYTES}-byte limit")
        target = (destination / relative).resolve()
        try:
            target.relative_to(destination_resolved)
        except ValueError as exc:
            raise GitError(f"Unsafe path in git archive: {member.name}") from exc
        platform_key = os.path.normcase(relative)
        if sys.platform in {"darwin", "win32"}:
            platform_key = unicodedata.normalize("NFD", platform_key).casefold()
        previous = seen.get(platform_key)
        if previous is not None and previous != relative:
            raise GitError(f"Archive paths collide on this platform: {previous!r} and {relative!r}")
        seen[platform_key] = relative
    try:
        archive_file.extractall(destination, members=members, filter="data")
        return
    except TypeError:
        # Python 3.11 has no extraction filters. Fall through to manual path checks.
        pass
    archive_file.extractall(destination, members=members)
