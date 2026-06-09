from __future__ import annotations

import os
import subprocess
import shutil
import tarfile
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path


# Skillfile 'git' URLs reach git clone as untrusted input. Restricting the
# transport protocols blocks remote-helper URLs such as ext::sh -c ... which
# would otherwise execute arbitrary commands during csk install.
ALLOWED_GIT_PROTOCOLS = "file:git:http:https:ssh"


class GitError(Exception):
    pass


@dataclass(frozen=True)
class ResolvedRef:
    kind: str
    ref: str
    commit: str


def git(repo: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(repo), *args]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise GitError(f"git {' '.join(args)} failed in {repo}: {stderr}")
    return proc


def clone_repo(remote_url: str, destination: Path) -> None:
    if destination.exists():
        raise GitError(f"Clone destination already exists: {destination}")
    if not remote_url.strip() or remote_url.startswith("-"):
        raise GitError(f"Refusing to clone suspicious git URL: {remote_url!r}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_ALLOW_PROTOCOL": ALLOWED_GIT_PROTOCOLS}
    proc = subprocess.run(
        ["git", "clone", "--", remote_url, str(destination)],
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
    destination.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", commit],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise GitError(f"git archive failed in {repo}: {proc.stderr.decode(errors='replace').strip()}")
    with tarfile.open(fileobj=BytesIO(proc.stdout), mode="r:") as archive_file:
        _extract_archive(archive_file, destination)


def fetch_repo(repo: Path) -> None:
    ensure_git_repo(repo)
    git(repo, ["fetch", "--all", "--tags", "--prune"])


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
    try:
        archive_file.extractall(destination, filter="data")
        return
    except TypeError:
        # Python 3.11 has no extraction filters. Fall through to manual path checks.
        pass
    destination_resolved = destination.resolve()
    for member in archive_file.getmembers():
        target = (destination / member.name).resolve()
        try:
            target.relative_to(destination_resolved)
        except ValueError as exc:
            raise GitError(f"Unsafe path in git archive: {member.name}") from exc
        if member.issym() or member.islnk():
            raise GitError(f"Links in git archives are unsupported in MVP: {member.name}")
    archive_file.extractall(destination)
