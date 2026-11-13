from __future__ import annotations

import os
import re
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
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import url2pathname

from . import git_trust
from .identifiers import is_valid_portable_path
from .source_identity import SourceIdentityError, canonical_source_identity
from .sources.errors import sanitize_credentials
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
    validate_git_url(remote_url)
    if destination.exists():
        raise GitError(f"Clone destination already exists: {destination}")
    with _private_trust() as trust:
        environment = _trust_environment(trust)
        executable = _git_executable()
        discovery = _run(list(git_trust.pinned_command(
            [executable, "ls-remote", "--get-url", "--", remote_url], environment,
        )), text=True, capture_output=True, env=environment)
        if discovery.returncode or not discovery.stdout.strip():
            raise GitError("Cannot resolve clone URL through Git configuration")
        effective_url = discovery.stdout.strip()
        validate_git_url(effective_url)
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

        refused = _refused_redirect(_classify_git_failure_output(stderr), stderr, remote_url, effective_url, None)
        if refused is not None:
            raise GitError(f"git clone failed: {redirect_refusal(remote_url, refused.canonical)}")
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


def validate_git_url(url: str) -> None:
    """Reject unsafe declarations before they can enter Git argv or diagnostics."""
    if not url.strip() or url.startswith("-") or any(ord(char) < 0x20 for char in url):
        raise GitError("Refusing suspicious Git source URL")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise GitError("Refusing suspicious Git source URL") from exc
    if parsed.scheme.lower() in {"http", "https"} and "@" in parsed.netloc:
        raise GitError("HTTP(S) Git URLs must not contain userinfo; use a credential helper")


def _local_source_path(url: str, base: Path) -> Path:
    if url.lower().startswith("file:"):
        parsed = urlsplit(url)
        if parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
            raise GitError("Cannot identify local Git source URL")
        path = Path(url2pathname(parsed.path))
        if not path.is_absolute():
            raise GitError("Local file URL must contain an absolute path")
    else:
        path = Path(url)
    return (base / path).resolve()


def declared_fetch_url(url: str, base: Path | None = None) -> str:
    """Anchor local declarations before Git -C can reinterpret them.

    ``base`` is the directory of the manifest that roots the declaration
    (project, global or hybrid Skillfile). Without one, a relative local path
    keeps Git's own meaning: relative to the invoking working directory.
    """
    validate_git_url(url)
    try:
        identity = canonical_source_identity(url)
    except SourceIdentityError as exc:
        raise GitError(f"Invalid declared Git source: {exc}") from exc
    if identity is None and not url.lower().startswith("file:"):
        return str(_local_source_path(url, base if base is not None else Path.cwd()))
    return url


def display_url(url: str) -> str:
    """Render a URL for diagnostics without userinfo, query or fragment data."""
    return sanitize_credentials(url.split("?", 1)[0].split("#", 1)[0])


@dataclass(frozen=True)
class FetchSource:
    remote: str
    stored_url: str
    target: str


def validate_repo_source(repo: Path, declared_url: str | None) -> FetchSource | None:
    """Validate cached provenance without fetching or changing the repository.

    A remote-less repository can satisfy only a local declaration of itself,
    or an undeclared local skill. It cannot vouch for a network declaration.
    """
    target = declared_fetch_url(declared_url) if declared_url is not None else None
    ensure_git_repo(repo)
    # Native discovery preserves include/includeIf and Git's boolean syntax.
    # The shared Git entry point checks trust even for this local command.
    remotes = sorted(git(repo, ["remote"]).stdout.splitlines())
    if not remotes:
        if target is not None and not (
            canonical_source_identity(target) is None
            and _local_source_path(target, Path.cwd()) == repo.resolve()
        ):
            raise GitError("Cannot verify declared source: repository has no fetch remote; restore the correct origin or use a fresh clone")
        return None
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
    # Compare declarations before insteadOf expansion: transport rewrites
    # are operator configuration, not the repository's declared identity.
    urls = git(repo, ["config", "--get-all", f"remote.{remote}.url"], check=False)
    stored_urls = urls.stdout.splitlines() if urls.returncode == 0 else []
    if not stored_urls or (declared_url is not None and len(stored_urls) != 1):
        raise GitError("Cannot reconcile fetch URL: configure exactly one URL for the fetch remote")
    stored_url = stored_urls[0]
    try:
        validate_git_url(stored_url)
    except GitError as exc:
        raise GitError(
            f"Stored {remote} URL of {repo} is not usable: {exc}. Correct it with "
            f"'git -C <clone> remote set-url {remote} <url>'"
        ) from exc
    if target is not None:
        try:
            stored_identity = canonical_source_identity(stored_url)
            declared_identity = canonical_source_identity(target)
            same_repository = (
                stored_identity == declared_identity
                and (stored_identity is not None or
                     _local_source_path(stored_url, repo) == _local_source_path(target, Path.cwd()))
            )
        except SourceIdentityError:
            same_repository = False
        if not same_repository:
            # Drop all query/fragment data, including unknown credential keys.
            safe_stored, safe_declared = (display_url(url) for url in (stored_url, target))
            raise GitError(
                f"Refusing fetch: stored {remote} URL {safe_stored!r} and declared URL "
                f"{safe_declared!r} do not identify the same repository. Correct the "
                "declaration or move the existing clone aside and clone the intended repository."
            )
    return FetchSource(remote, stored_url, target if target is not None else remote)


def fetch_repo(repo: Path, declared_url: str | None = None) -> None:
    source = validate_repo_source(repo, declared_url)
    if source is None:
        return
    remote, stored_url, fetch_target = source.remote, source.stored_url, source.target
    if declared_url is not None:
        discovery = git(repo, ["ls-remote", "--get-url", "--", fetch_target], check=False)
        if discovery.returncode:
            raise GitError("Cannot resolve declared fetch URL through Git configuration")
        effective_url = discovery.stdout.strip()
    else:
        effective_url = git(repo, ["remote", "get-url", "--", remote]).stdout.strip()
    if not effective_url:
        raise GitError("Cannot resolve fetch URL through Git configuration")
    validate_git_url(effective_url)
    with _private_trust() as trust:
        environment = _trust_environment(trust)
        args = [
            "-C", str(repo), "fetch", "--force", "--prune", "--no-recurse-submodules", "--", fetch_target,
            f"+refs/heads/*:refs/remotes/{remote}/*", "+refs/tags/*:refs/tags/*",
        ]
        proc = _run(list(git_trust.pinned_command(
            [_git_executable(), *args], environment, url=effective_url,
        )), text=True, capture_output=True, env=environment)
    if proc.returncode != 0:
        if declared_url is not None:
            from .git_admission import _classify_git_failure_output

            failure = _classify_git_failure_output(proc.stderr)
            refused = _refused_redirect(failure, proc.stderr, fetch_target, effective_url, repo)
            if refused is not None:
                raise GitError(f"git fetch failed in {repo}: {redirect_refusal(fetch_target, refused.canonical)}")
            raise GitError(f"git fetch failed in {repo}: {failure}; verify the declared endpoint and authentication")
        raise GitError(f"git fetch failed in {repo}: {proc.stderr.strip()}")
    if declared_url is not None and stored_url != fetch_target:
        updated = git(repo, ["remote", "set-url", "--", remote, fetch_target], check=False)
        if updated.returncode:
            raise GitError("Fetch succeeded but the stored remote URL could not be updated; check repository configuration")
        persisted = git(repo, ["config", "--get-all", f"remote.{remote}.url"], check=False)
        if persisted.returncode or persisted.stdout.splitlines() != [fetch_target]:
            raise GitError(
                "Fetch succeeded but the stored remote URL is still ambiguous; "
                "configure exactly one URL for the fetch remote, including included Git configuration"
            )


REDIRECT_PROBE_TIMEOUT = 60


def redirect_refusal(declared: str, canonical: str | None) -> str:
    """csk's own refusal for a declared URL that the server answers with a redirect."""
    head = (
        f"declared URL {display_url(declared)!r} is not canonical: the server answers it with an "
        "HTTP redirect, and HTTP redirects are refused"
    )
    where = (
        "everywhere this repository is declared (Skillfiles on every branch and worktree, global "
        "and hybrid declarations, dependency declarations); see 'Canonical source URLs' in the csk documentation"
    )
    if canonical is not None:
        return (
            f"{head}. The server redirects to the same repository at {display_url(canonical)!r}: "
            f"declare exactly that URL {where}"
        )
    return (
        f"{head}. Declare the URL the server serves without a redirect, for example with .git "
        "(check the provider's canonical Git clone URL in its Clone UI or API; .git is provider-specific; "
        "do not copy an arbitrary redirect target: it may be a login or browser page), "
        f"{where}"
    )


def _strip_repository_suffix(path: str) -> str:
    path = path.rstrip("/")
    return path[: -len(".git")].rstrip("/") if path.endswith(".git") else path


def canonical_from_redirect(declared: str, effective_url: str, location: str | None) -> str | None:
    """Derive the canonical spelling of ``declared`` from a refused redirect.

    Only a redirect of Git's discovery request to the same scheme, host, port
    and repository path, differing in the ``.git`` suffix or a trailing slash,
    determines it. The result is a respelling of the declared URL itself; the
    raw Location (which may carry credentials, queries or a login page) is
    never returned.
    """
    if not location:
        return None
    request = effective_url.rstrip("/") + "/info/refs?service=git-upload-pack"
    try:
        source, target, origin = urlsplit(request), urlsplit(urljoin(request, location.strip())), urlsplit(declared)
        same_endpoint = (
            target.scheme.lower() == source.scheme.lower()
            and target.hostname == source.hostname
            and target.port == source.port
        )
    except ValueError:
        return None
    if not same_endpoint or not target.path.endswith("/info/refs") or origin.scheme.lower() not in {"http", "https"}:
        return None
    served = target.path[: -len("/info/refs")]
    requested = source.path[: -len("/info/refs")]
    if not served or _strip_repository_suffix(served) != _strip_repository_suffix(requested):
        return None
    suffix = ".git" if served.rstrip("/").endswith(".git") else ""
    host = origin.netloc.rpartition("@")[2]
    canonical = urlunsplit((origin.scheme, host, _strip_repository_suffix(origin.path) + suffix, "", ""))
    return None if canonical == declared or not origin.path.strip("/") else canonical


_TRACE_RECORD = re.compile(r"\d{2}:\d{2}:\d{2}\.\d+ ")


def _split_curl_trace(stderr: bytes) -> tuple[list[str], str]:
    """Split Git's raw stderr into curl trace records and Git's own messages.

    Git writes each trace record as one line that starts with an
    ``HH:MM:SS.micro`` timestamp (the probe runs without the user's trace
    settings, so ``GIT_TRACE_BARE`` cannot remove it); Git's messages
    (``fatal:``, ``error:``, ``remote:`` ...) never start that way. The bytes
    are split on LF only, before decoding, and each line is decoded on its
    own with replacement characters: a non-UTF-8 byte or a bare CR that a
    server puts in a response header stays inside its record and cannot
    break the split or turn header text into a Git message. The messages
    only feed the failure classifier, which matches anchored Git and curl
    frames; neither part is printed.
    """
    records: list[str] = []
    messages: list[str] = []
    for raw in stderr.split(b"\n"):
        line = raw.decode("utf-8", "replace")
        (records if _TRACE_RECORD.match(line) else messages).append(line)
    return records, "\n".join(messages)


# Event markers Git puts in front of each curl trace record. Server bytes
# only ever follow the marker, so the earliest marker in a record is Git's.
_TRACE_EVENTS = (
    "== Info:", "=> Send header", "<= Recv header", "=> Send data", "<= Recv data",
    "=> Send SSL data", "<= Recv SSL data",
)
_STATUS_LINE = re.compile(r"HTTP/\d(?:\.\d)? ([1-5]\d\d)(?: .*)?")


def _last_response(records: list[str]) -> tuple[int | None, str | None]:
    """Status code and ``Location`` of the last response in a curl trace.

    A response's status line is the first header Git records after a request
    (``=> Send header``), and one more after an interim 1xx response; any
    later header that looks like a status line is server text, not a status.
    Each record is attributed by its earliest event marker, so a header value
    that quotes another marker cannot start a request or a response.
    """
    status: int | None = None
    location: str | None = None
    expect_status = False
    for line in records:
        found = [(index, marker) for marker in _TRACE_EVENTS if (index := line.find(marker)) >= 0]
        if not found:
            continue
        index, marker = min(found)
        rest = line[index + len(marker):]
        if marker == "=> Send header":
            expect_status = True
        elif marker == "<= Recv header" and rest.startswith(":"):
            header = rest[1:].strip()
            if expect_status:
                match = _STATUS_LINE.fullmatch(header)
                status = int(match.group(1)) if match else None
                location = None
                expect_status = status is not None and status < 200
                continue
            name, separator, value = header.partition(":")
            if separator and name.strip().lower() == "location" and value.strip():
                location = value.strip()
    return status, location


@dataclass(frozen=True)
class RedirectProbe:
    """Outcome of :func:`probe_declared_url`: ``ok``, ``redirect`` or ``unverified``."""

    outcome: Literal["ok", "redirect", "unverified"]
    canonical: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class _Discovery:
    """One discovery request without credentials, as seen in Git's curl trace."""

    proc: subprocess.CompletedProcess[str]
    status: int | None
    location: str | None

    @property
    def redirected(self) -> bool:
        """Git failed, and the server answered the last request with a 3xx status line."""
        return self.proc.returncode != 0 and self.status is not None and 300 <= self.status < 400


def _discovery_without_credentials(target: str, effective_url: str, repo: Path | None) -> _Discovery | None:
    """Make Git's discovery request for ``target`` once, without csk credentials.

    The same trust pins apply and redirects stay refused. Credential helpers,
    prompts, cookie files and extra HTTP headers are disabled, so the request
    carries no credential of csk's or Git's configuration. Git always lets
    libcurl read ``~/.netrc``, though: when the host answers with an
    authentication challenge, libcurl may answer it from there, as it does for
    the fetch itself, and only to that same host (redirects are refused).

    The curl trace goes to Git's stderr pipe, never to a file, so no response
    header (``Set-Cookie`` included) reaches the disk, also if csk is killed.
    The user's trace environment (``GIT_TRACE*``, ``GIT_CURL_VERBOSE``) is
    removed, so the curl trace keeps Git's default record format with its
    timestamp prefix. Other lines on the pipe (a trace2 target configured in
    Git's own configuration, say) count as Git's messages; the classifier
    only accepts anchored outcome records. Only the status line and the
    Location header of the last response are read from the trace (see
    :func:`_last_response`). Returns the finished process, whose stderr
    holds Git's own messages without the trace, with that status and
    Location; None when Git could not run.
    """
    if urlsplit(effective_url).scheme.lower() not in {"http", "https"}:
        return None
    try:
        with _private_trust() as trust:
            environment = {
                key: value for key, value in _trust_environment(trust).items()
                if not key.upper().startswith("GIT_TRACE") and key.upper() != "GIT_CURL_VERBOSE"
            }
            environment.update({
                "GIT_TRACE_CURL": "2",
                "GIT_TRACE_CURL_NO_DATA": "1",
                "GIT_TRACE_REDACT": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "SSH_ASKPASS": "",
            })
            command = [
                _git_executable(), *(["-C", str(repo)] if repo is not None else []),
                "-c", "credential.helper=",
                "-c", "http.extraHeader=", "-c", f"http.{effective_url}.extraHeader=",
                "-c", "http.cookieFile=", "-c", f"http.{effective_url}.cookieFile=",
                "ls-remote", "--heads", "--", target,
            ]
            proc = _run(
                list(git_trust.pinned_command(command, environment, url=effective_url)),
                capture_output=True, env=environment, timeout=REDIRECT_PROBE_TIMEOUT,
            )
    except (GitError, OSError, subprocess.SubprocessError, ValueError):
        return None
    records, messages = _split_curl_trace(proc.stderr or b"")
    stdout = (proc.stdout or b"").decode("utf-8", "replace")
    proc = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, messages)
    status, location = _last_response(records) if proc.returncode else (None, None)
    return _Discovery(proc, status, location)


def _answers_with_redirect(discovery: _Discovery) -> bool:
    """Whether the discovery request was answered with a redirect that Git refused.

    Git's own redirect record decides. libcurl may also refuse a response
    after its status line, for a header it rejects (newer releases reject a
    bare CR, for example); Git then reports that libcurl error, which csk
    does not classify, and the server's 3xx status line in the trace decides.
    """
    from .git_admission import _classify_git_failure_output

    if discovery.proc.returncode == 0:
        return False
    failure = _classify_git_failure_output(discovery.proc.stderr)
    return failure == "redirect" or (failure == "unclassified" and discovery.redirected)


_ACCESS_FAILURE = re.compile(r"fatal: unable to access '[^']+': .+")


def _unclassified_access_failure(stderr: str) -> bool:
    """Git's own frame for a failed HTTP request with a libcurl error csk does not classify."""
    from .git_admission import _evidence_class

    for raw in stderr.lower().split("\n"):
        record = raw.removesuffix("\r").strip()
        if _ACCESS_FAILURE.fullmatch(record) and _evidence_class(record) is None:
            return True
    return False


def _refused_redirect(
    failure: str, stderr: str, target: str, effective_url: str, repo: Path | None,
) -> RedirectProbe | None:
    """The redirect verdict for a failed clone or fetch of ``target``, or None.

    When Git reports the redirect, one repeat of the request without
    credentials only reads its Location. When Git reports an HTTP failure
    that csk does not classify (``unable to access`` with a libcurl error),
    the server may still have answered with a redirect that libcurl refused
    for a later header; the same single repeat then decides, through
    :func:`_answers_with_redirect`. Any other failure makes no request.
    """
    if failure == "redirect":
        discovery = _discovery_without_credentials(target, effective_url, repo)
    elif failure == "unclassified" and _unclassified_access_failure(stderr):
        discovery = _discovery_without_credentials(target, effective_url, repo)
        if discovery is None or not _answers_with_redirect(discovery):
            return None
    else:
        return None
    location = discovery.location if discovery is not None else None
    return RedirectProbe("redirect", canonical=canonical_from_redirect(target, effective_url, location))


def probe_declared_url(url: str) -> RedirectProbe:
    """Check whether a declared HTTP(S) URL answers with a redirect.

    Used by ``csk check --canonical-urls --network``: one discovery request
    per URL (two HTTP requests when ``~/.netrc`` answers an authentication
    challenge), under the same trust pins as a fetch, with redirects refused
    and no credential helper, prompt, extra header or cookie (see
    :func:`_discovery_without_credentials`). A server that requires
    authentication before it would redirect reports ``unverified``.
    """
    validate_git_url(url)
    from .git_admission import _classify_git_failure_output

    with _private_trust() as trust:
        environment = _trust_environment(trust)
        discovery = _run(list(git_trust.pinned_command(
            [_git_executable(), "ls-remote", "--get-url", "--", url], environment,
        )), text=True, capture_output=True, env=environment)
    effective_url = discovery.stdout.strip() if discovery.returncode == 0 else ""
    if not effective_url:
        return RedirectProbe("unverified", detail="cannot resolve the URL through Git configuration")
    validate_git_url(effective_url)
    if urlsplit(effective_url).scheme.lower() not in {"http", "https"}:
        return RedirectProbe("ok", detail="not an HTTP(S) endpoint; redirects do not apply")
    answer = _discovery_without_credentials(url, effective_url, None)
    if answer is None:
        return RedirectProbe("unverified", detail="the request could not be completed")
    if answer.proc.returncode == 0:
        return RedirectProbe("ok")
    if _answers_with_redirect(answer):
        return RedirectProbe("redirect", canonical=canonical_from_redirect(url, effective_url, answer.location))
    return RedirectProbe("unverified", detail=_classify_git_failure_output(answer.proc.stderr))


def skill_source_path(skills_root: Path, source: str) -> Path:
    """Join a declared portable source path below skills_root.

    Symlinks are followed exactly as install and upgrade follow them; callers
    validate the repository they reach. Git metadata never names a source.
    """
    if not is_valid_portable_path(source):
        raise GitError("Skill source must be a portable relative path")
    parts = source.split("/")
    if any(part.casefold() == ".git" for part in parts):
        raise GitError(f"Refusing skill source {source!r}: Git metadata in source path")
    return skills_root.joinpath(*parts)


def skill_clones(skills_root: Path) -> dict[str, Path]:
    """Immediate skills_root entries that are Git clones, symlinked ones included.

    Entry names come from the filesystem rather than a declaration, so they
    need not be portable. Nothing below an entry is crawled.
    """
    try:
        children = sorted(skills_root.iterdir())
    except FileNotFoundError:
        return {}
    return {child.name: child for child in children if child.is_dir() and (child / ".git").exists()}


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
