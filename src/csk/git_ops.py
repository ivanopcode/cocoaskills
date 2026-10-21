from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
import tarfile
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

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


def _https_args(url: str) -> list[str]:
    if not url.lower().startswith("https://"):
        return []
    try:
        return [
            *git_trust.https_git_args(),
            "-c", "http.sslVerify=true",
            "-c", "http.followRedirects=false",
        ]
    except git_trust.GitTrustError as exc:
        error = GitError(str(exc))
        error.code = exc.code
        raise error from exc


def _https_environment(arguments: list[str]) -> dict[str, str]:
    excluded = {
        "GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "GIT_SSL_NO_VERIFY",
        "SSL_CERT_FILE", "SSL_CERT_DIR",
    }
    environment = {key: value for key, value in os.environ.items() if key not in excluded}
    # Git's TLS environment overrides even URL-specific settings in a
    # legacy clone's configuration. Pin the checked file there as well.
    for argument in arguments:
        if argument.startswith("http.sslCAInfo="):
            environment["GIT_SSL_CAINFO"] = argument.removeprefix("http.sslCAInfo=")
    environment["GIT_SSL_NO_VERIFY"] = "false"
    return environment


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError as exc:
        raise GitError("git executable not found; install git and ensure it is on PATH") from exc


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
    cmd = [_git_executable(), "-C", str(repo), *args]
    proc = _run(cmd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise GitError(f"git {' '.join(args)} failed in {repo}: {stderr}")
    return proc


def clone_repo(remote_url: str, destination: Path) -> None:
    if destination.exists():
        raise GitError(f"Clone destination already exists: {destination}")
    if not remote_url.strip() or remote_url.startswith("-"):
        raise GitError(f"Refusing to clone suspicious git URL: {remote_url!r}")
    trust_args = _https_args(remote_url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    env = {**(_https_environment(trust_args) if trust_args else os.environ), "GIT_ALLOW_PROTOCOL": ALLOWED_GIT_PROTOCOLS}
    proc = _run(
        [_git_executable(), *trust_args, "clone", "--", remote_url, str(destination)],
        text=True,
        capture_output=True,
        env=env,
    )
    if proc.returncode != 0:
        if destination.exists():
            shutil.rmtree(destination)
        stderr = proc.stderr.strip() or proc.stdout.strip()
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
    destination.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [_git_executable(), "-C", str(repo), "archive", "--format=tar", commit],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise GitError(f"git archive failed in {repo}: {proc.stderr.decode(errors='replace').strip()}")
    with tarfile.open(fileobj=BytesIO(proc.stdout), mode="r:") as archive_file:
        _extract_archive(archive_file, destination)


def _repository_config_path(repo: Path) -> Path:
    git_dir = repo / ".git"
    if git_dir.is_file():
        declaration = git_dir.read_text(encoding="utf-8").strip()
        if not declaration.startswith("gitdir: "):
            raise GitError("Cannot read repository Git directory")
        selected = Path(declaration[8:])
        git_dir = selected if selected.is_absolute() else repo / selected
        common_file = git_dir / "commondir"
        if common_file.exists():
            common = Path(common_file.read_text(encoding="utf-8").strip())
            git_dir = common if common.is_absolute() else git_dir / common
    return git_dir / "config"


def fetch_repo(repo: Path) -> None:
    ensure_git_repo(repo)
    # Inspect ordinary clone remotes before any Git discovery subprocess.
    parser = configparser.RawConfigParser(strict=False)
    try:
        with _repository_config_path(repo).open(encoding="utf-8") as config_file:
            parser.read_file(config_file)
        remotes = sorted(
            section[8:-1] for section in parser.sections()
            if section.startswith('remote "') and section.endswith('"')
        )
    except (OSError, configparser.Error, UnicodeError) as exc:
        raise GitError("Cannot read repository remote configuration") from exc
    if not remotes:
        # Preserve the existing missing-Git diagnostic even when no fetch is
        # needed. No HTTPS endpoint or credential environment is involved.
        git(repo, ["remote"])
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
    url = parser.get(f'remote "{remote}"', "url", fallback="").strip('"')
    trust_args = _https_args(url)
    args = [
        "fetch", "--force", "--prune", "--", remote,
        f"+refs/heads/*:refs/remotes/{remote}/*",
        "+refs/tags/*:refs/tags/*",
    ]
    if not trust_args:
        git(repo, args)
        return
    proc = _run(
        [_git_executable(), *trust_args, "-C", str(repo), *args],
        text=True, capture_output=True, env=_https_environment(trust_args),
    )
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
