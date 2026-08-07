from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Mapping, Sequence

from .build_repository import (
    LockedCommit,
    RepositorySource,
    is_valid_ref_name,
    parse_locked_commit,
    parse_repository_source,
)


IDENTITY_INVALID = "build_repository_identity_invalid"
SOURCE_UNAVAILABLE = "build_repository_source_unavailable"
REF_MOVED = "build_repository_ref_moved"
INCOMPLETE_SOURCE = "build_repository_incomplete_source"
OBJECT_SEMANTICS_INVALID = "build_repository_git_object_semantics_invalid"
LFS_UNSUPPORTED = "build_repository_git_lfs_unsupported"
LOCAL_GITFILE_UNSUPPORTED = "build_repository_local_gitfile_unsupported"
LOCAL_BARE_UNSUPPORTED = "build_repository_local_bare_unsupported"
LOCAL_LINKED_UNSUPPORTED = "build_repository_local_linked_worktree_unsupported"
LOCAL_LAYOUT_UNSAFE = "build_repository_local_layout_unsafe"
LOCAL_FORMAT_UNSUPPORTED = "build_repository_local_format_unsupported"
LOCAL_OBJECT_FORMAT_UNSUPPORTED = "build_repository_local_object_format_unsupported"
LOCAL_CLEANUP_FAILED = "build_repository_local_cleanup_failed"

_HEX_BY_FORMAT = {
    "sha1": re.compile(r"^[0-9a-f]{40}$"),
    "sha256": re.compile(r"^[0-9a-f]{64}$"),
}
_CONFIG_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


class GitAdmissionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


_CLEANUP_ATTEMPTS = 5
_CLEANUP_BACKOFF_SECONDS = 0.1


def _unseal_tree(root: Path) -> None:
    # Symlinks are skipped rather than chmodded: os.chmod follows them, and
    # rmtree unlinks them without following, so the target is never ours to
    # touch.  Failures are ignored here because rmtree reports them precisely.
    for directory, directories, files in os.walk(root, topdown=True):
        parent = Path(directory)
        for name in (*directories, *files):
            child = parent / name
            try:
                if not child.is_symlink():
                    os.chmod(child, stat.S_IRWXU)
            except OSError:
                pass
    try:
        os.chmod(root, stat.S_IRWXU)
    except OSError:
        pass


def _remove_private_root(root: Path) -> OSError | None:
    """Remove the private admission root and report, never raise, a failure.

    ``_seal_object_store`` leaves 0o400 files under 0o500 directories.  On POSIX
    a plain ``rmtree`` cannot unlink inside a directory without write
    permission; on Windows the same ``chmod`` sets FILE_ATTRIBUTE_READONLY and
    ``DeleteFile`` refuses the entry, so both platforms need the tree unsealed
    first.  Windows additionally refuses to delete a file another handle still
    maps: a just-terminated ``git cat-file`` leaves its pack index mapped for a
    short moment, which surfaces as ERROR_ACCESS_DENIED on ``pack-*.idx``.  That
    window is transient, so retry with a short backoff before giving up.
    """
    last: OSError | None = None
    for attempt in range(_CLEANUP_ATTEMPTS):
        _unseal_tree(root)
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            return None
        except OSError as exc:
            last = exc
        else:
            return None
        if attempt + 1 < _CLEANUP_ATTEMPTS:
            time.sleep(_CLEANUP_BACKOFF_SECONDS * (attempt + 1))
    return last


@dataclass(frozen=True)
class Limits:
    timeout_seconds: float = 120.0
    max_objects: int = 200_000
    max_object_bytes: int = 512 << 20
    max_expanded_bytes: int = 2 << 30
    max_files: int = 200_000
    max_path_bytes: int = 4096
    max_tree_depth: int = 128
    max_tag_depth: int = 16


@dataclass(frozen=True)
class GitTool:
    executable: Path
    exec_path: Path
    allowed_versions: tuple[str, ...]
    askpass: Path | None = None
    ssh_wrapper: Path | None = None


@dataclass(frozen=True)
class SSHPolicy:
    wrapper: Path
    ssh: Path
    expected_host: str
    repository_path: str
    empty_config: Path
    known_hosts: Path
    empty_known_hosts: Path
    identity: Path | None = None
    agent_socket: Path | None = None
    connect_timeout: int = 15


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    content: bytes
    executable: bool = False


@dataclass(frozen=True)
class Snapshot:
    object_format: str
    commit: str
    files: tuple[SnapshotFile, ...]
    canonical_bytes: bytes
    digest: str
    tag_verified: bool = False

    def materialize(self, destination: Path) -> None:
        if destination.exists():
            if (
                destination.is_symlink()
                or not destination.is_dir()
                or any(destination.iterdir())
            ):
                raise GitAdmissionError(
                    LOCAL_LAYOUT_UNSAFE,
                    "snapshot destination is not an empty directory",
                )
        else:
            destination.mkdir(mode=0o700, parents=True)
        root = destination.resolve(strict=True)
        for item in self.files:
            target = destination.joinpath(*item.path.split("/"))
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if (
                target.parent.resolve(strict=True) != root
                and root not in target.parent.resolve(strict=True).parents
            ):
                raise GitAdmissionError(
                    OBJECT_SEMANTICS_INVALID, "snapshot path escapes destination"
                )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(target, flags, 0o700 if item.executable else 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(item.content)
                    stream.flush()
                os.fsync(fd)
            finally:
                os.close(fd)


@dataclass(frozen=True)
class _PrivatePaths:
    root: Path
    repository: Path
    work: Path
    home: Path
    config: Path
    template: Path
    hooks: Path
    empty_path: Path


@dataclass(frozen=True)
class _FileProof:
    path: Path
    device: int
    inode: int
    size: int
    mode: int
    mtime_ns: int
    digest: bytes


@dataclass(frozen=True)
class _RawObject:
    oid: str
    kind: str
    data: bytes


def error_code(error: BaseException | None) -> str:
    return error.code if isinstance(error, GitAdmissionError) else ""


def exact_ssh_command(policy: SSHPolicy, argv: Sequence[str]) -> tuple[str, ...]:
    expected_command = f"git-upload-pack '{policy.repository_path}'"
    expected = (os.fspath(policy.wrapper), policy.expected_host, expected_command)
    if tuple(argv) != expected:
        raise GitAdmissionError(
            IDENTITY_INVALID, "SSH wrapper invocation does not match protected policy"
        )
    required_paths = (
        policy.wrapper,
        policy.ssh,
        policy.empty_config,
        policy.known_hosts,
        policy.empty_known_hosts,
    )
    if (
        any(not path.is_absolute() for path in required_paths)
        or policy.connect_timeout <= 0
    ):
        raise GitAdmissionError(IDENTITY_INVALID, "SSH policy is invalid")
    command = [
        os.fspath(policy.ssh),
        "-F",
        os.fspath(policy.empty_config),
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "NumberOfPasswordPrompts=0",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "HostbasedAuthentication=no",
        "-o",
        "GSSAPIAuthentication=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={policy.known_hosts}",
        "-o",
        f"GlobalKnownHostsFile={policy.empty_known_hosts}",
        "-o",
        "CheckHostIP=no",
        "-o",
        "VerifyHostKeyDNS=no",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ControlPersist=no",
        "-o",
        "RequestTTY=no",
        "-o",
        "EscapeChar=none",
        "-o",
        "EnableEscapeCommandline=no",
        "-o",
        "CanonicalizeHostname=no",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        f"ConnectTimeout={policy.connect_timeout}",
    ]
    if (
        policy.identity is not None
        and policy.agent_socket is None
        and policy.identity.is_absolute()
    ):
        command.extend(
            (
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
                "-i",
                os.fspath(policy.identity),
            )
        )
    elif (
        policy.agent_socket is not None
        and policy.identity is None
        and policy.agent_socket.is_absolute()
    ):
        command.extend(
            (
                "-o",
                "IdentitiesOnly=no",
                "-o",
                "IdentityFile=none",
                "-o",
                f"IdentityAgent={policy.agent_socket}",
            )
        )
    else:
        raise GitAdmissionError(
            IDENTITY_INVALID, "SSH authentication policy must select exactly one mode"
        )
    command.extend((policy.expected_host, expected_command))
    return tuple(command)


def validate_git_tool(tool: GitTool) -> None:
    for label, path, directory in (
        ("git", tool.executable, False),
        ("git exec path", tool.exec_path, True),
    ):
        try:
            info = path.lstat()
        except OSError as exc:
            raise GitAdmissionError(
                IDENTITY_INVALID, f"{label} is unavailable"
            ) from exc
        valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not path.is_absolute() or stat.S_ISLNK(info.st_mode) or not valid:
            raise GitAdmissionError(
                IDENTITY_INVALID, f"{label} path is not an admitted ordinary object"
            )
    for label, optional_path in (
        ("credential broker", tool.askpass),
        ("SSH wrapper", tool.ssh_wrapper),
    ):
        if optional_path is None:
            continue
        try:
            info = optional_path.lstat()
        except OSError as exc:
            raise GitAdmissionError(
                IDENTITY_INVALID, f"{label} is unavailable"
            ) from exc
        if (
            not optional_path.is_absolute()
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
        ):
            raise GitAdmissionError(
                IDENTITY_INVALID, f"{label} is not an admitted absolute regular file"
            )
    try:
        result = subprocess.run(
            (os.fspath(tool.executable), "--version"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_clean_discovery_environment(),
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitAdmissionError(
            IDENTITY_INVALID, "trusted Git version probe failed"
        ) from exc
    if len(result.stdout) > 256:
        raise GitAdmissionError(IDENTITY_INVALID, "trusted Git version probe failed")
    version = result.stdout.decode("ascii", "strict").strip()
    if not any(
        prefix and version.startswith(prefix) for prefix in tool.allowed_versions
    ):
        raise GitAdmissionError(
            IDENTITY_INVALID, "Git release family is not operator-pinned"
        )


def acquire_network(
    source: RepositorySource,
    lock: LockedCommit,
    tool: GitTool,
    *,
    tag: str | None = None,
    limits: Limits = Limits(),
) -> Snapshot:
    validate_git_tool(tool)
    try:
        parsed_source = parse_repository_source(source.git)
    except ValueError as exc:
        raise GitAdmissionError(IDENTITY_INVALID, "network source is invalid") from exc
    if parsed_source != source:
        raise GitAdmissionError(
            IDENTITY_INVALID, "network source is not canonical parsed input"
        )
    try:
        parse_locked_commit(
            {"object_format": lock.object_format, "hex": lock.hex}, field="lock"
        )
    except ValueError as exc:
        raise GitAdmissionError(IDENTITY_INVALID, "invalid immutable lock") from exc
    if source.transport == "https" and tool.askpass is None:
        raise GitAdmissionError(
            IDENTITY_INVALID, "HTTPS requires a manager credential broker"
        )
    if source.transport == "ssh" and tool.ssh_wrapper is None:
        raise GitAdmissionError(
            IDENTITY_INVALID, "SSH requires the exact manager wrapper"
        )
    if source.transport not in {"https", "ssh"} or (
        tag is not None and not is_valid_ref_name(tag)
    ):
        raise GitAdmissionError(
            IDENTITY_INVALID, "network source or exact tag is invalid"
        )
    with tempfile.TemporaryDirectory(prefix="csk-buildrepo-") as raw_root:
        paths = _make_private_paths(Path(raw_root))
        environment = _clean_git_environment(paths, tool, source.transport)
        _run_git(
            tool,
            paths,
            environment,
            f"--git-dir={paths.repository}",
            "-c",
            "init.defaultBranch=csk-invalid",
            "init",
            "--bare",
            "--quiet",
            f"--template={paths.template}",
            f"--object-format={lock.object_format}",
            "--ref-format=files",
            limits=limits,
        )
        destination = "refs/csk/tag" if tag is not None else "refs/csk/locked"
        source_ref = f"refs/tags/{tag}" if tag is not None else lock.hex
        _run_git(
            tool,
            paths,
            environment,
            *_strict_fetch_args(paths, tool, source, f"{source_ref}:{destination}"),
            limits=limits,
        )
        _validate_private_repository(paths.repository, lock.object_format)
        selected = _read_single_oid(
            paths.repository.joinpath(*destination.split("/")), lock.object_format
        )
        snapshot = _prove_repository(
            tool, environment, paths, lock.object_format, selected, tag, limits
        )
        if tag is not None and snapshot.commit != lock.hex:
            raise GitAdmissionError(
                REF_MOVED, "exact tag terminal commit differs from lock"
            )
        if tag is None and snapshot.commit != lock.hex:
            raise GitAdmissionError(
                INCOMPLETE_SOURCE, "locked object is not the selected commit"
            )
        return Snapshot(**{**snapshot.__dict__, "tag_verified": tag is not None})


def _make_private_paths(root: Path) -> _PrivatePaths:
    paths = _PrivatePaths(
        root=root,
        repository=root / "repo.git",
        work=root / "work",
        home=root / "home",
        config=root / "config",
        template=root / "empty-template",
        hooks=root / "empty-hooks",
        empty_path=root / "empty-path",
    )
    for path in (
        paths.work,
        paths.home,
        paths.config,
        paths.template,
        paths.hooks,
        paths.empty_path,
    ):
        path.mkdir(mode=0o700)
    for name in ("global.gitconfig", "system.gitconfig"):
        (root / name).write_bytes(b"")
        (root / name).chmod(0o600)
    return paths


def _clean_discovery_environment() -> dict[str, str]:
    environment = {"LANG": "C", "LC_ALL": "C"}
    for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
        if value := os.environ.get(name):
            environment[name] = value
    return environment


def _clean_git_environment(
    paths: _PrivatePaths, tool: GitTool, transport: str
) -> dict[str, str]:
    environment = _clean_discovery_environment()
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.fspath(paths.root / "global.gitconfig"),
            "GIT_CONFIG_SYSTEM": os.fspath(paths.root / "system.gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_EXEC_PATH": os.fspath(tool.exec_path),
            "HOME": os.fspath(paths.home),
            "XDG_CONFIG_HOME": os.fspath(paths.config),
            "PATH": os.fspath(paths.empty_path),
        }
    )
    if transport == "https" and tool.askpass is not None:
        environment["GIT_ASKPASS"] = os.fspath(tool.askpass)
    if transport == "ssh" and tool.ssh_wrapper is not None:
        environment.update(
            {"GIT_SSH": os.fspath(tool.ssh_wrapper), "GIT_SSH_VARIANT": "ssh"}
        )
    return environment


def _strict_fetch_args(
    paths: _PrivatePaths, tool: GitTool, source: RepositorySource, refspec: str
) -> tuple[str, ...]:
    askpass = "" if tool.askpass is None else os.fspath(tool.askpass)
    return (
        f"--git-dir={paths.repository}",
        "--no-replace-objects",
        "--no-lazy-fetch",
        "--no-optional-locks",
        "-c",
        "protocol.allow=never",
        "-c",
        f"protocol.{source.transport}.allow=always",
        "-c",
        "protocol.version=0",
        "-c",
        "credential.helper=",
        "-c",
        f"core.askPass={askpass}",
        "-c",
        f"core.hooksPath={paths.hooks}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "submodule.recurse=false",
        "-c",
        "fetch.recurseSubmodules=false",
        "-c",
        "maintenance.auto=false",
        "-c",
        "fetch.writeCommitGraph=false",
        "-c",
        "fetch.fsckObjects=true",
        "-c",
        "transfer.fsckObjects=true",
        "-c",
        "http.followRedirects=false",
        "-c",
        "http.sslVerify=true",
        "-c",
        "http.proxy=",
        "-c",
        "https.proxy=",
        "fetch",
        "--quiet",
        "--atomic",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-auto-maintenance",
        "--no-write-fetch-head",
        "--no-write-commit-graph",
        "--refmap=",
        "--jobs=1",
        "--upload-pack=git-upload-pack",
        "--",
        source.git,
        refspec,
    )


def _run_git(
    tool: GitTool,
    paths: _PrivatePaths,
    environment: Mapping[str, str],
    *arguments: str,
    limits: Limits,
) -> None:
    try:
        subprocess.run(
            (os.fspath(tool.executable), *arguments),
            cwd=paths.work,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=limits.timeout_seconds,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        operation = "initialization" if "init" in arguments else "fetch"
        code = SOURCE_UNAVAILABLE
        raise GitAdmissionError(code, f"private Git {operation} failed") from exc


def _validate_private_repository(repository: Path, object_format: str) -> None:
    allowed = {"HEAD", "config", "objects", "refs"}
    try:
        entries = list(repository.iterdir())
    except OSError as exc:
        raise GitAdmissionError(
            INCOMPLETE_SOURCE, "private repository is unreadable"
        ) from exc
    if any(entry.name not in allowed or entry.is_symlink() for entry in entries):
        raise GitAdmissionError(
            INCOMPLETE_SOURCE, "private repository contains unexpected state"
        )
    forbidden = (
        "FETCH_HEAD",
        "shallow",
        "objects/info/alternates",
        "objects/info/http-alternates",
        "info/grafts",
        "objects/info/commit-graph",
        "objects/pack/multi-pack-index",
        "refs/replace",
    )
    if any(repository.joinpath(*name.split("/")).exists() for name in forbidden):
        raise GitAdmissionError(
            INCOMPLETE_SOURCE, "private repository contains forbidden state"
        )
    try:
        config = (repository / "config").read_bytes().lower()
    except OSError as exc:
        raise GitAdmissionError(
            INCOMPLETE_SOURCE, "private repository config is unreadable"
        ) from exc
    if object_format == "sha256" and b"objectformat = sha256" not in config:
        raise GitAdmissionError(
            INCOMPLETE_SOURCE, "private repository object format is invalid"
        )


def _valid_oid(value: str, object_format: str) -> bool:
    expression = _HEX_BY_FORMAT.get(object_format)
    return expression is not None and expression.fullmatch(value) is not None


def _read_single_oid(path: Path, object_format: str) -> str:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise GitAdmissionError(
            INCOMPLETE_SOURCE, "manager destination ref is invalid"
        ) from exc
    value = payload.removesuffix(b"\n").removesuffix(b"\r").decode("ascii", "strict")
    if len(payload) > 66 or not _valid_oid(value, object_format):
        raise GitAdmissionError(INCOMPLETE_SOURCE, "manager destination ref is invalid")
    return value


class _ObjectReader:
    def __init__(
        self,
        tool: GitTool,
        environment: Mapping[str, str],
        paths: _PrivatePaths,
        object_format: str,
        limits: Limits,
    ) -> None:
        arguments = (
            os.fspath(tool.executable),
            f"--git-dir={paths.repository}",
            "--no-replace-objects",
            "--no-lazy-fetch",
            "--no-optional-locks",
            "-c",
            f"core.hooksPath={paths.hooks}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-c",
            "maintenance.auto=false",
            "cat-file",
            "--batch=%(objectname) %(objecttype) %(objectsize)",
        )
        try:
            self._process = subprocess.Popen(
                arguments,
                cwd=paths.work,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            raise GitAdmissionError(
                INCOMPLETE_SOURCE, "object reader could not start"
            ) from exc
        if self._process.stdin is None or self._process.stdout is None:
            self._process.kill()
            raise GitAdmissionError(
                INCOMPLETE_SOURCE, "object reader pipes are unavailable"
            )
        self._stdin: IO[bytes] = self._process.stdin
        self._stdout: IO[bytes] = self._process.stdout
        self._format = object_format
        self._limits = limits
        self._count = 0
        self._expanded = 0
        self._cache: dict[str, _RawObject] = {}
        self._timed_out = False
        self._timer = threading.Timer(limits.timeout_seconds, self._kill_for_timeout)
        self._timer.daemon = True
        self._timer.start()

    def _kill_for_timeout(self) -> None:
        self._timed_out = True
        self._process.kill()

    def __enter__(self) -> _ObjectReader:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def read(self, oid: str) -> _RawObject:
        cached = self._cache.get(oid)
        if cached is not None:
            return cached
        if not _valid_oid(oid, self._format) or self._count >= self._limits.max_objects:
            raise GitAdmissionError(
                INCOMPLETE_SOURCE, "invalid or excessive object request"
            )
        try:
            self._stdin.write(oid.encode("ascii") + b"\n")
            self._stdin.flush()
            header = self._stdout.readline(257)
        except OSError as exc:
            raise GitAdmissionError(INCOMPLETE_SOURCE, "object request failed") from exc
        if not header.endswith(b"\n") or len(header) > 256:
            raise GitAdmissionError(INCOMPLETE_SOURCE, "malformed object header")
        try:
            parts = header[:-1].decode("ascii", "strict").split(" ")
        except UnicodeDecodeError as exc:
            raise GitAdmissionError(
                INCOMPLETE_SOURCE, "malformed object response"
            ) from exc
        if (
            len(parts) != 3
            or parts[0] != oid
            or parts[1] not in {"commit", "tag", "tree", "blob"}
        ):
            raise GitAdmissionError(INCOMPLETE_SOURCE, "malformed object response")
        size_text = parts[2]
        if not size_text.isdecimal() or (
            len(size_text) > 1 and size_text.startswith("0")
        ):
            raise GitAdmissionError(INCOMPLETE_SOURCE, "non-canonical object size")
        size = int(size_text)
        if (
            size > self._limits.max_object_bytes
            or self._expanded + size > self._limits.max_expanded_bytes
        ):
            raise GitAdmissionError(INCOMPLETE_SOURCE, "object size limit exceeded")
        data = self._stdout.read(size)
        terminator = self._stdout.read(1)
        if len(data) != size or terminator != b"\n":
            raise GitAdmissionError(INCOMPLETE_SOURCE, "truncated or malformed object")
        if _compute_oid(self._format, parts[1], data) != oid:
            raise GitAdmissionError(INCOMPLETE_SOURCE, "object identity mismatch")
        result = _RawObject(oid=oid, kind=parts[1], data=data)
        self._count += 1
        self._expanded += size
        self._cache[oid] = result
        return result

    def close(self) -> None:
        if self._process.stdin is None:
            return
        self._stdin.close()
        trailing = self._stdout.read(2)
        try:
            status = self._process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            self._process.kill()
            self._process.wait()
            raise GitAdmissionError(
                INCOMPLETE_SOURCE, "object reader did not terminate"
            ) from exc
        self._process.stdin = None
        self._timer.cancel()
        if self._timed_out:
            raise GitAdmissionError(INCOMPLETE_SOURCE, "object reader timed out")
        if status != 0 or trailing:
            raise GitAdmissionError(
                INCOMPLETE_SOURCE, "object reader did not terminate cleanly"
            )


def _compute_oid(object_format: str, kind: str, data: bytes) -> str:
    digest = (
        hashlib.sha1() if object_format == "sha1" else hashlib.sha256()
    )  # noqa: S324 -- Git protocol SHA-1.
    digest.update(f"{kind} {len(data)}".encode("ascii") + b"\0")
    digest.update(data)
    return digest.hexdigest()


def _prove_repository(
    tool: GitTool,
    environment: Mapping[str, str],
    paths: _PrivatePaths,
    object_format: str,
    selected: str,
    exact_tag: str | None,
    limits: Limits,
) -> Snapshot:
    with _ObjectReader(tool, environment, paths, object_format, limits) as reader:
        commit = _peel_commit(reader, selected, exact_tag, limits.max_tag_depth)
        tree = _parse_commit(reader.read(commit), object_format)
        files: list[SnapshotFile] = []
        _walk_tree(reader, tree, "", 0, limits, {}, files)
    files.sort(key=lambda item: item.path.encode("utf-8"))
    canonical = _frame_snapshot(files)
    return Snapshot(
        object_format=object_format,
        commit=commit,
        files=tuple(files),
        canonical_bytes=canonical,
        digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
    )


def _peel_commit(
    reader: _ObjectReader, oid: str, exact_tag: str | None, max_depth: int
) -> str:
    seen: set[str] = set()
    annotated = 0
    while True:
        if oid in seen:
            raise GitAdmissionError(OBJECT_SEMANTICS_INVALID, "tag chain is cyclic")
        seen.add(oid)
        obj = reader.read(oid)
        if obj.kind == "commit":
            return oid
        if obj.kind != "tag" or annotated >= max_depth:
            raise GitAdmissionError(
                OBJECT_SEMANTICS_INVALID,
                "selected object is not a bounded commit/tag chain",
            )
        target, target_type, name = _parse_tag(obj, reader._format)
        if annotated == 0 and exact_tag is not None and name != exact_tag:
            raise GitAdmissionError(
                OBJECT_SEMANTICS_INVALID, "outer annotated tag name mismatch"
            )
        target_object = reader.read(target)
        if target_object.kind != target_type:
            raise GitAdmissionError(
                OBJECT_SEMANTICS_INVALID, "annotated tag target type mismatch"
            )
        oid = target
        annotated += 1


@dataclass(frozen=True)
class _ObjectHeader:
    key: str
    value: str
    continuation: bool = False


def _parse_headers(data: bytes) -> list[_ObjectHeader]:
    separator = data.find(b"\n\n")
    if b"\0" in data or separator < 0 or b"\r" in data[:separator]:
        raise ValueError("invalid object headers")
    headers: list[_ObjectHeader] = []
    for line in data[:separator].split(b"\n"):
        if not line:
            raise ValueError("empty object header")
        if line.startswith(b" "):
            if not headers:
                raise ValueError("orphan continuation")
            headers.append(
                _ObjectHeader("", line[1:].decode("utf-8", "surrogateescape"), True)
            )
            continue
        space = line.find(b" ")
        if space <= 0:
            raise ValueError("malformed object header")
        key = line[:space].decode("ascii", "strict")
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", key) is None:
            raise ValueError("invalid object header key")
        headers.append(
            _ObjectHeader(key, line[space + 1 :].decode("utf-8", "surrogateescape"))
        )
    return headers


def _parse_commit(obj: _RawObject, object_format: str) -> str:
    if obj.kind != "commit":
        raise GitAdmissionError(
            OBJECT_SEMANTICS_INVALID, "terminal object is not a commit"
        )
    try:
        headers = _parse_headers(obj.data)
    except (ValueError, UnicodeError) as exc:
        raise GitAdmissionError(
            OBJECT_SEMANTICS_INVALID, "invalid commit headers"
        ) from exc
    if (
        len(headers) < 3
        or headers[0].continuation
        or headers[0].key != "tree"
        or not _valid_oid(headers[0].value, object_format)
    ):
        raise GitAdmissionError(
            OBJECT_SEMANTICS_INVALID, "commit tree header is invalid"
        )
    index = 1
    while (
        index < len(headers)
        and not headers[index].continuation
        and headers[index].key == "parent"
    ):
        if not _valid_oid(headers[index].value, object_format):
            raise GitAdmissionError(
                OBJECT_SEMANTICS_INVALID, "commit parent is invalid"
            )
        index += 1
    for required in ("author", "committer"):
        if (
            index >= len(headers)
            or headers[index].continuation
            or headers[index].key != required
            or not headers[index].value
        ):
            raise GitAdmissionError(
                OBJECT_SEMANTICS_INVALID, "commit required header is invalid"
            )
        index += 1
    structural = {"tree", "parent", "author", "committer"}
    last_extra = False
    for header in headers[index:]:
        if header.continuation:
            if not last_extra:
                raise GitAdmissionError(
                    OBJECT_SEMANTICS_INVALID, "invalid commit continuation"
                )
            continue
        if header.key in structural:
            raise GitAdmissionError(
                OBJECT_SEMANTICS_INVALID, "duplicate or misplaced commit header"
            )
        last_extra = True
    return headers[0].value


def _parse_tag(obj: _RawObject, object_format: str) -> tuple[str, str, str]:
    try:
        headers = _parse_headers(obj.data)
    except (ValueError, UnicodeError) as exc:
        raise GitAdmissionError(
            OBJECT_SEMANTICS_INVALID, "invalid tag headers"
        ) from exc
    required = ("object", "type", "tag")
    if len(headers) < 3 or any(
        headers[index].continuation
        or headers[index].key != key
        or not headers[index].value
        for index, key in enumerate(required)
    ):
        raise GitAdmissionError(OBJECT_SEMANTICS_INVALID, "invalid tag header order")
    target, target_type, name = headers[0].value, headers[1].value, headers[2].value
    if (
        not _valid_oid(target, object_format)
        or target_type not in {"commit", "tag"}
        or not is_valid_ref_name(name)
    ):
        raise GitAdmissionError(
            OBJECT_SEMANTICS_INVALID, "invalid annotated tag semantics"
        )
    index = 3
    if (
        index < len(headers)
        and not headers[index].continuation
        and headers[index].key == "tagger"
    ):
        if not headers[index].value:
            raise GitAdmissionError(OBJECT_SEMANTICS_INVALID, "empty tagger")
        index += 1
    structural = {"object", "type", "tag", "tagger"}
    last_extra = False
    for header in headers[index:]:
        if header.continuation:
            if not last_extra:
                raise GitAdmissionError(
                    OBJECT_SEMANTICS_INVALID, "invalid tag continuation"
                )
            continue
        if header.key in structural:
            raise GitAdmissionError(OBJECT_SEMANTICS_INVALID, "duplicate tag header")
        last_extra = True
    return target, target_type, name


def _walk_tree(
    reader: _ObjectReader,
    oid: str,
    prefix: str,
    depth: int,
    limits: Limits,
    seen_paths: dict[str, str],
    files: list[SnapshotFile],
) -> None:
    if depth > limits.max_tree_depth:
        raise GitAdmissionError(INCOMPLETE_SOURCE, "tree depth limit exceeded")
    obj = reader.read(oid)
    if obj.kind != "tree":
        raise GitAdmissionError(
            OBJECT_SEMANTICS_INVALID, "tree entry target type mismatch"
        )
    oid_bytes = 20 if reader._format == "sha1" else 32
    offset = 0
    local_names: set[str] = set()
    previous_sort_key: bytes | None = None
    while offset < len(obj.data):
        space = obj.data.find(b" ", offset)
        nul = obj.data.find(b"\0", offset)
        if space <= offset or nul <= space + 1 or nul + 1 + oid_bytes > len(obj.data):
            raise GitAdmissionError(OBJECT_SEMANTICS_INVALID, "malformed tree")
        mode = obj.data[offset:space].decode("ascii", "strict")
        name_bytes = obj.data[space + 1 : nul]
        try:
            name = name_bytes.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise GitAdmissionError(
                OBJECT_SEMANTICS_INVALID, "tree path is not valid UTF-8"
            ) from exc
        if not name or name in {".", ".."} or "/" in name or "\0" in name:
            raise GitAdmissionError(OBJECT_SEMANTICS_INVALID, "invalid tree component")
        local_key = unicodedata.normalize("NFC", name).casefold()
        if local_key in local_names:
            raise GitAdmissionError(
                OBJECT_SEMANTICS_INVALID, "platform-colliding tree names"
            )
        local_names.add(local_key)
        sort_key = name_bytes + (b"/" if mode == "40000" else b"")
        if previous_sort_key is not None and previous_sort_key >= sort_key:
            raise GitAdmissionError(
                OBJECT_SEMANTICS_INVALID, "tree entries are not uniquely sorted"
            )
        previous_sort_key = sort_key
        child = obj.data[nul + 1 : nul + 1 + oid_bytes].hex()
        offset = nul + 1 + oid_bytes
        path = f"{prefix}/{name}" if prefix else name
        if len(path.encode("utf-8")) > limits.max_path_bytes:
            raise GitAdmissionError(INCOMPLETE_SOURCE, "path length limit exceeded")
        collision = unicodedata.normalize("NFC", path).casefold()
        if collision in seen_paths and seen_paths[collision] != path:
            raise GitAdmissionError(OBJECT_SEMANTICS_INVALID, "platform path collision")
        seen_paths[collision] = path
        if mode == "40000":
            _walk_tree(reader, child, path, depth + 1, limits, seen_paths, files)
        elif mode in {"100644", "100755"}:
            blob = reader.read(child)
            if blob.kind != "blob":
                raise GitAdmissionError(
                    OBJECT_SEMANTICS_INVALID, "blob entry target type mismatch"
                )
            if _is_lfs_pointer(blob.data):
                raise GitAdmissionError(
                    LFS_UNSUPPORTED, "reachable Git LFS pointer is unsupported"
                )
            if len(files) >= limits.max_files:
                raise GitAdmissionError(INCOMPLETE_SOURCE, "file count limit exceeded")
            files.append(
                SnapshotFile(path=path, content=blob.data, executable=mode == "100755")
            )
        else:
            raise GitAdmissionError(OBJECT_SEMANTICS_INVALID, "unsupported tree mode")


def _frame_snapshot(files: Sequence[SnapshotFile]) -> bytes:
    framed = bytearray(b"curator-build-source-v1\0")
    for item in files:
        path = item.path.encode("utf-8")
        framed.extend(b"F")
        framed.extend(struct.pack(">Q", len(path)))
        framed.extend(path)
        framed.extend(struct.pack(">Q", len(item.content)))
        framed.extend(item.content)
    return bytes(framed)


def _is_lfs_pointer(data: bytes) -> bool:
    if not data or len(data) >= 1024:
        return False
    try:
        lines = data.decode("utf-8", "strict").strip().splitlines()
    except UnicodeDecodeError:
        return False
    state = 0
    priorities: dict[str, str] = {}
    versions = {
        "https://git-lfs.github.com/spec/v1",
        "https://hawser.github.com/spec/v1",
        "http://git-media.io/v/2",
    }
    for line in lines:
        if not line:
            continue
        try:
            key, value = line.split(" ", 1)
        except ValueError:
            return False
        if state == 3:
            return False
        if key == "version":
            if state != 0 or value not in versions:
                return False
            state = 1
        elif key == "oid":
            if (
                state != 1
                or not value.startswith("sha256:")
                or _HEX_BY_FORMAT["sha256"].fullmatch(value[7:]) is None
            ):
                return False
            state = 2
        elif key == "size":
            if state != 2 or re.fullmatch(r"\+?[0-9]+", value) is None:
                return False
            state = 3
        else:
            match = re.fullmatch(r"ext-([0-9])-([A-Za-z0-9_][^ ]*)", key)
            if (
                match is None
                or not value.startswith("sha256:")
                or _HEX_BY_FORMAT["sha256"].fullmatch(value[7:]) is None
            ):
                return False
            priority = match.group(1)
            if priority in priorities and priorities[priority] != key:
                return False
            priorities[priority] = key
    return state == 3


def admit_local(
    path: Path,
    tool: GitTool,
    *,
    limits: Limits = Limits(),
    after_object_copy: Callable[[], None] | None = None,
) -> Snapshot:
    validate_git_tool(tool)
    try:
        root_info = path.lstat()
    except OSError as exc:
        raise GitAdmissionError(
            LOCAL_LAYOUT_UNSAFE, "local selection is unavailable"
        ) from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise GitAdmissionError(
            LOCAL_LAYOUT_UNSAFE, "local selection is not a link-free directory"
        )
    git_dir = path / ".git"
    try:
        git_info = git_dir.lstat()
    except OSError as exc:
        if _looks_bare(path):
            raise GitAdmissionError(
                LOCAL_BARE_UNSUPPORTED, "bare local repository is unsupported"
            ) from exc
        raise GitAdmissionError(
            LOCAL_LAYOUT_UNSAFE, "local .git directory is missing"
        ) from exc
    if stat.S_ISREG(git_info.st_mode):
        raise GitAdmissionError(
            LOCAL_GITFILE_UNSUPPORTED, "local .git gitfile is unsupported"
        )
    if stat.S_ISLNK(git_info.st_mode) or not stat.S_ISDIR(git_info.st_mode):
        raise GitAdmissionError(
            LOCAL_LAYOUT_UNSAFE, "local .git is not an ordinary directory"
        )
    if any(
        (git_dir / name).exists()
        for name in ("commondir", "worktrees", "config.worktree")
    ):
        raise GitAdmissionError(
            LOCAL_LINKED_UNSUPPORTED, "linked worktree state is unsupported"
        )
    config_data, config_proof = _read_proved_file(git_dir / "config", 1 << 20)
    object_format = _parse_local_config(config_data)
    selected, ref_proofs = _read_local_head(git_dir, object_format)
    proofs = [config_proof, *ref_proofs]
    private_root = Path(tempfile.mkdtemp(prefix="csk-buildrepo-local-"))
    try:
        paths = _make_private_paths(private_root)
        environment = _clean_git_environment(paths, tool, "")
        _run_git(
            tool,
            paths,
            environment,
            f"--git-dir={paths.repository}",
            "-c",
            "init.defaultBranch=csk-invalid",
            "init",
            "--bare",
            "--quiet",
            f"--template={paths.template}",
            f"--object-format={object_format}",
            "--ref-format=files",
            limits=limits,
        )
        source_inventory = _object_inventory(git_dir / "objects")
        object_proofs = _copy_local_objects(
            git_dir / "objects", paths.repository / "objects", object_format, limits
        )
        proofs.extend(object_proofs)
        if after_object_copy is not None:
            after_object_copy()
        if _object_inventory(git_dir / "objects") != source_inventory:
            raise GitAdmissionError(
                LOCAL_LAYOUT_UNSAFE, "local object inventory changed during admission"
            )
        _validate_local_administration(git_dir, object_format)
        for proof in proofs:
            _recheck_proof(proof)
        _seal_object_store(paths.repository / "objects")
        snapshot = _prove_repository(
            tool, environment, paths, object_format, selected, None, limits
        )
        for proof in proofs:
            _recheck_proof(proof)
    finally:
        # Reporting instead of raising keeps a failed removal from replacing an
        # admission diagnostic that is already propagating out of the body.
        cleanup_failure = _remove_private_root(private_root)
    if cleanup_failure is not None:
        raise GitAdmissionError(
            LOCAL_CLEANUP_FAILED,
            f"private local admission state could not be removed: {cleanup_failure}",
        ) from cleanup_failure
    return snapshot


def _looks_bare(path: Path) -> bool:
    return all((path / name).exists() for name in ("HEAD", "config", "objects", "refs"))


def _parse_local_config(data: bytes) -> str:
    if len(data) > 1 << 20 or data.startswith(b"\xef\xbb\xbf") or b"\0" in data:
        raise GitAdmissionError(
            LOCAL_FORMAT_UNSUPPORTED, "local Git config encoding is unsupported"
        )
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise GitAdmissionError(
            LOCAL_FORMAT_UNSUPPORTED, "local Git config encoding is unsupported"
        ) from exc
    if "\r" in text.replace("\r\n", ""):
        raise GitAdmissionError(
            LOCAL_FORMAT_UNSUPPORTED, "local Git config line endings are unsupported"
        )
    section = ""
    subsection = ""
    entries: list[tuple[str, str, str, str]] = []
    for raw_line in text.replace("\r\n", "\n").split("\n"):
        stripped = raw_line.strip(" \t")
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if raw_line.endswith("\\"):
            raise GitAdmissionError(
                LOCAL_FORMAT_UNSUPPORTED, "config continuation is unsupported"
            )
        if stripped.startswith("["):
            section, subsection = _parse_config_section(stripped)
            if section in {"include", "includeif"}:
                raise GitAdmissionError(
                    LOCAL_FORMAT_UNSUPPORTED, "config includes are unsupported"
                )
            continue
        if not section:
            raise GitAdmissionError(
                LOCAL_FORMAT_UNSUPPORTED, "config assignment precedes section"
            )
        key, value = _parse_config_assignment(stripped)
        entries.append((section, subsection, key, value))
    security: dict[str, str] = {}
    for entry_section, entry_subsection, key, value in entries:
        relevant = (
            entry_section == "core"
            and key in {"repositoryformatversion", "bare"}
            or entry_section == "extensions"
            or entry_section == "remote"
            and key in {"promisor", "partialclonefilter"}
        )
        if not relevant:
            continue
        identity = f"{entry_section}.{entry_subsection}.{key}"
        if identity in security:
            raise GitAdmissionError(
                LOCAL_FORMAT_UNSUPPORTED, "duplicate security-relevant config key"
            )
        security[identity] = value
    version = security.get("core..repositoryformatversion")
    bare = _parse_git_bool(security.get("core..bare", ""))
    if version is None or bare is None:
        raise GitAdmissionError(
            LOCAL_FORMAT_UNSUPPORTED, "required core config keys are missing or invalid"
        )
    if bare:
        raise GitAdmissionError(LOCAL_BARE_UNSUPPORTED, "local repository is bare")
    for identity, value in security.items():
        if identity.startswith("remote."):
            if identity.endswith(".promisor"):
                parsed = _parse_git_bool(value)
                if parsed is None or parsed:
                    raise GitAdmissionError(
                        LOCAL_FORMAT_UNSUPPORTED, "promisor state is unsupported"
                    )
            elif value:
                raise GitAdmissionError(
                    LOCAL_FORMAT_UNSUPPORTED, "partial clone state is unsupported"
                )
    extensions = {
        identity.removeprefix("extensions.."): value.lower()
        for identity, value in security.items()
        if identity.startswith("extensions..")
    }
    if version == "0" and not extensions:
        return "sha1"
    if (
        version == "1"
        and extensions.get("objectformat") == "sha256"
        and set(extensions) <= {"objectformat", "refstorage"}
        and extensions.get("refstorage", "files") == "files"
    ):
        return "sha256"
    raise GitAdmissionError(
        LOCAL_FORMAT_UNSUPPORTED, "repository format or extensions are unsupported"
    )


def _parse_config_section(line: str) -> tuple[str, str]:
    if not line.endswith("]"):
        raise GitAdmissionError(LOCAL_FORMAT_UNSUPPORTED, "malformed config section")
    inside = line[1:-1].strip()
    if " " not in inside and "\t" not in inside:
        if _CONFIG_TOKEN.fullmatch(inside) is None:
            raise GitAdmissionError(
                LOCAL_FORMAT_UNSUPPORTED, "malformed config section"
            )
        return inside.lower(), ""
    match = re.fullmatch(
        r'([A-Za-z][A-Za-z0-9-]*)[ \t]+"((?:[^"\\]|\\[\\"])*)"', inside
    )
    if match is None:
        raise GitAdmissionError(LOCAL_FORMAT_UNSUPPORTED, "malformed config section")
    subsection = match.group(2).replace(r"\"", '"').replace(r"\\", "\\")
    return match.group(1).lower(), subsection.lower()


def _strip_config_comment(value: str) -> str:
    quoted = False
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif (
            not quoted
            and character in "#;"
            and (index == 0 or value[index - 1].isspace())
        ):
            return value[:index]
    return value


def _parse_config_assignment(line: str) -> tuple[str, str]:
    if "=" not in line:
        key = _strip_config_comment(line).strip()
        if _CONFIG_TOKEN.fullmatch(key) is None:
            raise GitAdmissionError(
                LOCAL_FORMAT_UNSUPPORTED, "malformed config assignment"
            )
        return key.lower(), "true"
    key, raw_value = line.split("=", 1)
    key = key.strip()
    if _CONFIG_TOKEN.fullmatch(key) is None:
        raise GitAdmissionError(LOCAL_FORMAT_UNSUPPORTED, "malformed config assignment")
    value = _strip_config_comment(raw_value).strip()
    if value.startswith('"'):
        match = re.fullmatch(r'"((?:[^"\\]|\\[\\"ntb])*)"', value)
        if match is None:
            raise GitAdmissionError(
                LOCAL_FORMAT_UNSUPPORTED, "malformed quoted config value"
            )
        value = (
            match.group(1)
            .replace(r"\n", "\n")
            .replace(r"\t", "\t")
            .replace(r"\b", "\b")
            .replace(r"\"", '"')
            .replace(r"\\", "\\")
        )
    if any(
        ord(character) < 0x20 and character != "\t" or ord(character) == 0x7F
        for character in value
    ):
        raise GitAdmissionError(
            LOCAL_FORMAT_UNSUPPORTED, "config value contains control bytes"
        )
    return key.lower(), value


def _parse_git_bool(value: str) -> bool | None:
    normalized = value.lower()
    if normalized in {"true", "yes", "on", "1"}:
        return True
    if normalized in {"false", "no", "off", "0"}:
        return False
    return None


def _read_local_head(git_dir: Path, object_format: str) -> tuple[str, list[_FileProof]]:
    data, head_proof = _read_proved_file(git_dir / "HEAD", 512)
    value = _exact_one_line(data)
    proofs = [head_proof]
    if value.startswith("ref: "):
        ref = value.removeprefix("ref: ")
        if not ref.startswith("refs/heads/") or not is_valid_ref_name(
            ref.removeprefix("refs/heads/")
        ):
            raise GitAdmissionError(LOCAL_FORMAT_UNSUPPORTED, "HEAD ref is invalid")
        ref_path = git_dir.joinpath(*ref.split("/"))
        try:
            ref_data, ref_proof = _read_proved_file(ref_path, 512)
        except FileNotFoundError:
            packed, packed_proof = _read_packed_refs(git_dir, object_format)
            if ref not in packed:
                raise GitAdmissionError(
                    LOCAL_FORMAT_UNSUPPORTED, "selected HEAD ref is missing"
                )
            value = packed[ref]
            proofs.append(packed_proof)
        else:
            value = _exact_one_line(ref_data)
            proofs.append(ref_proof)
    if not _valid_oid(value, object_format):
        raise GitAdmissionError(LOCAL_FORMAT_UNSUPPORTED, "HEAD object ID is invalid")
    return value, proofs


def _read_packed_refs(
    git_dir: Path, object_format: str
) -> tuple[dict[str, str], _FileProof]:
    data, proof = _read_proved_file(git_dir / "packed-refs", 8 << 20)
    try:
        text = data.decode("utf-8", "strict").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise GitAdmissionError(
            LOCAL_FORMAT_UNSUPPORTED, "packed-refs encoding is invalid"
        ) from exc
    if "\r" in text:
        raise GitAdmissionError(
            LOCAL_FORMAT_UNSUPPORTED, "packed-refs encoding is invalid"
        )
    refs: dict[str, str] = {}
    previous_tag = False
    header_seen = False
    lines = text.removesuffix("\n").split("\n")
    for index, line in enumerate(lines):
        if not line:
            raise GitAdmissionError(
                LOCAL_FORMAT_UNSUPPORTED, "empty packed-refs record"
            )
        if line.startswith("#"):
            if index != 0 or header_seen or not line.startswith("# pack-refs with:"):
                raise GitAdmissionError(
                    LOCAL_FORMAT_UNSUPPORTED, "unsupported packed-refs header"
                )
            header_seen = True
            if any(
                trait not in {"peeled", "fully-peeled", "sorted"}
                for trait in line.removeprefix("# pack-refs with:").split()
            ):
                raise GitAdmissionError(
                    LOCAL_FORMAT_UNSUPPORTED, "unsupported packed-refs trait"
                )
            continue
        if line.startswith("^"):
            if not previous_tag or not _valid_oid(line[1:], object_format):
                raise GitAdmissionError(
                    LOCAL_FORMAT_UNSUPPORTED, "misplaced packed-ref peel"
                )
            previous_tag = False
            continue
        try:
            oid, ref = line.split(" ", 1)
        except ValueError as exc:
            raise GitAdmissionError(
                LOCAL_FORMAT_UNSUPPORTED, "malformed packed ref"
            ) from exc
        if (
            not _valid_oid(oid, object_format)
            or not ref.startswith("refs/")
            or not is_valid_ref_name(ref.removeprefix("refs/"))
        ):
            raise GitAdmissionError(LOCAL_FORMAT_UNSUPPORTED, "invalid packed ref")
        if ref.startswith("refs/replace/") or ref in refs:
            raise GitAdmissionError(
                LOCAL_FORMAT_UNSUPPORTED, "replace or duplicate packed ref"
            )
        refs[ref] = oid
        previous_tag = ref.startswith("refs/tags/")
    return refs, proof


def _exact_one_line(data: bytes) -> str:
    data = (
        data.removesuffix(b"\r\n")
        if data.endswith(b"\r\n")
        else data.removesuffix(b"\n")
    )
    if not data or b"\r" in data or b"\n" in data:
        raise GitAdmissionError(LOCAL_FORMAT_UNSUPPORTED, "ref is not exactly one line")
    try:
        return data.decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise GitAdmissionError(LOCAL_FORMAT_UNSUPPORTED, "ref is not ASCII") from exc


def _read_proved_file(path: Path, maximum: int) -> tuple[bytes, _FileProof]:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > maximum
    ):
        raise GitAdmissionError(LOCAL_LAYOUT_UNSAFE, "unsafe repository file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (
            opened.st_dev != info.st_dev
            or opened.st_ino != info.st_ino
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise GitAdmissionError(
                LOCAL_LAYOUT_UNSAFE, "repository file changed during open"
            )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
    finally:
        os.close(fd)
    if len(data) > maximum:
        raise GitAdmissionError(LOCAL_LAYOUT_UNSAFE, "repository file exceeds bound")
    return data, _FileProof(
        path,
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mode,
        info.st_mtime_ns,
        hashlib.sha256(data).digest(),
    )


def _recheck_proof(proof: _FileProof) -> None:
    try:
        data, current = _read_proved_file(proof.path, proof.size)
    except (OSError, GitAdmissionError) as exc:
        raise GitAdmissionError(
            LOCAL_LAYOUT_UNSAFE, "local repository changed during admission"
        ) from exc
    if (
        current.device != proof.device
        or current.inode != proof.inode
        or current.size != proof.size
        or current.mode != proof.mode
        or current.mtime_ns != proof.mtime_ns
        or hashlib.sha256(data).digest() != proof.digest
    ):
        raise GitAdmissionError(
            LOCAL_LAYOUT_UNSAFE, "local repository changed during admission"
        )


def _object_inventory(root: Path) -> tuple[tuple[str, int, int, int, int, int], ...]:
    inventory: list[tuple[str, int, int, int, int, int]] = []
    for directory, directories, files in os.walk(root, followlinks=False):
        root_path = Path(directory)
        names = sorted((*directories, *files), key=os.fsencode)
        for name in names:
            child = root_path / name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise GitAdmissionError(
                    LOCAL_LAYOUT_UNSAFE, "link in local object inventory"
                )
            inventory.append(
                (
                    child.relative_to(root).as_posix(),
                    info.st_mode,
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                )
            )
    return tuple(inventory)


def _validate_local_administration(git_dir: Path, object_format: str) -> None:
    allowed = {"HEAD", "config", "index", "objects", "refs", "packed-refs"}
    try:
        entries = list(git_dir.iterdir())
    except OSError as exc:
        raise GitAdmissionError(LOCAL_LAYOUT_UNSAFE, "cannot inventory .git") from exc
    for entry in entries:
        if entry.name not in allowed:
            raise GitAdmissionError(
                LOCAL_FORMAT_UNSUPPORTED, "unexpected local Git administration child"
            )
        if entry.is_symlink():
            raise GitAdmissionError(
                LOCAL_LAYOUT_UNSAFE, "link in local Git administration"
            )
    refs_root = git_dir / "refs"
    if refs_root.exists():
        for root, directories, files in os.walk(refs_root, followlinks=False):
            root_path = Path(root)
            for name in directories:
                if (root_path / name).is_symlink():
                    raise GitAdmissionError(LOCAL_LAYOUT_UNSAFE, "unsafe ref directory")
            for name in files:
                path = root_path / name
                if path.is_symlink():
                    raise GitAdmissionError(LOCAL_LAYOUT_UNSAFE, "unsafe ref entry")
                ref = "refs/" + path.relative_to(refs_root).as_posix()
                if ref.startswith("refs/replace/"):
                    raise GitAdmissionError(
                        LOCAL_FORMAT_UNSUPPORTED, "replace refs are unsupported"
                    )
                if not is_valid_ref_name(ref.removeprefix("refs/")):
                    raise GitAdmissionError(
                        LOCAL_FORMAT_UNSUPPORTED, "invalid loose ref name"
                    )
                data, _ = _read_proved_file(path, 512)
                if not _valid_oid(_exact_one_line(data), object_format):
                    raise GitAdmissionError(
                        LOCAL_FORMAT_UNSUPPORTED, "invalid loose ref"
                    )
    forbidden = (
        "info/grafts",
        "shallow",
        "objects/info/alternates",
        "objects/info/http-alternates",
        "objects/info/commit-graph",
        "objects/pack/multi-pack-index",
    )
    if any(git_dir.joinpath(*name.split("/")).exists() for name in forbidden):
        raise GitAdmissionError(LOCAL_FORMAT_UNSUPPORTED, "forbidden local Git state")
    if (git_dir / "packed-refs").exists():
        _read_packed_refs(git_dir, object_format)


def _copy_local_objects(
    source: Path, destination: Path, object_format: str, limits: Limits
) -> list[_FileProof]:
    hash_hex = 40 if object_format == "sha1" else 64
    try:
        entries = list(source.iterdir())
    except OSError as exc:
        raise GitAdmissionError(
            LOCAL_LAYOUT_UNSAFE, "objects directory is unreadable"
        ) from exc
    proofs: list[_FileProof] = []
    pack_files: dict[str, dict[str, Path]] = {}
    admitted_objects = 0
    admitted_bytes = 0
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            raise GitAdmissionError(
                LOCAL_LAYOUT_UNSAFE, "unsafe object inventory entry"
            )
        if entry.name == "info":
            if any(entry.iterdir()):
                raise GitAdmissionError(
                    LOCAL_FORMAT_UNSUPPORTED, "object info sidecars are unsupported"
                )
        elif entry.name == "pack":
            for child in entry.iterdir():
                if child.is_symlink() or not child.is_file():
                    raise GitAdmissionError(LOCAL_LAYOUT_UNSAFE, "unsafe pack entry")
                match = re.fullmatch(
                    rf"(pack-[0-9a-f]{{{hash_hex}}})(\.(?:pack|idx))", child.name
                )
                if match is None:
                    raise GitAdmissionError(
                        LOCAL_FORMAT_UNSUPPORTED, "unsupported pack sidecar"
                    )
                members = pack_files.setdefault(match.group(1), {})
                if match.group(2) in members:
                    raise GitAdmissionError(
                        LOCAL_OBJECT_FORMAT_UNSUPPORTED, "duplicate pack member"
                    )
                members[match.group(2)] = child
        elif re.fullmatch(r"[0-9a-f]{2}", entry.name):
            for child in entry.iterdir():
                if (
                    child.is_symlink()
                    or not child.is_file()
                    or re.fullmatch(rf"[0-9a-f]{{{hash_hex - 2}}}", child.name) is None
                ):
                    raise GitAdmissionError(
                        LOCAL_OBJECT_FORMAT_UNSUPPORTED, "invalid loose object name"
                    )
                destination_path = destination / entry.name / child.name
                size = child.stat(follow_symlinks=False).st_size
                admitted_objects += 1
                admitted_bytes += size
                if (
                    admitted_objects > limits.max_objects
                    or admitted_bytes > limits.max_expanded_bytes
                ):
                    raise GitAdmissionError(
                        INCOMPLETE_SOURCE, "local object inventory exceeds bounds"
                    )
                proof = _copy_proved_file(
                    child, destination_path, limits.max_object_bytes
                )
                proofs.append(proof)
        else:
            raise GitAdmissionError(
                LOCAL_FORMAT_UNSUPPORTED, "unexpected object inventory child"
            )
    for base, members in pack_files.items():
        if set(members) != {".pack", ".idx"}:
            raise GitAdmissionError(
                LOCAL_OBJECT_FORMAT_UNSUPPORTED, "pack/index pair is incomplete"
            )
        pack, pack_proof = _read_proved_file(
            members[".pack"], limits.max_expanded_bytes
        )
        index, index_proof = _read_proved_file(
            members[".idx"], limits.max_expanded_bytes
        )
        _validate_pack_index(base, pack, index, object_format)
        admitted_objects += struct.unpack(">I", pack[8:12])[0]
        admitted_bytes += len(pack) + len(index)
        if (
            admitted_objects > limits.max_objects
            or admitted_bytes > limits.max_expanded_bytes
        ):
            raise GitAdmissionError(
                INCOMPLETE_SOURCE, "local pack inventory exceeds bounds"
            )
        (destination / "pack").mkdir(mode=0o700, exist_ok=True)
        for extension, data in ((".pack", pack), (".idx", index)):
            target = destination / "pack" / f"{base}{extension}"
            target.write_bytes(data)
            target.chmod(0o600)
        proofs.extend((pack_proof, index_proof))
    return proofs


def _copy_proved_file(source: Path, destination: Path, maximum: int) -> _FileProof:
    data, proof = _read_proved_file(source, maximum)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
    finally:
        os.close(fd)
    return proof


def _object_digest(object_format: str, data: bytes) -> bytes:
    return (
        hashlib.sha1(data) if object_format == "sha1" else hashlib.sha256(data)
    ).digest()  # noqa: S324


def _validate_pack_index(
    base: str, pack: bytes, index: bytes, object_format: str
) -> None:
    hash_bytes = 20 if object_format == "sha1" else 32
    if len(pack) < 12 + hash_bytes or pack[:4] != b"PACK":
        raise GitAdmissionError(
            LOCAL_OBJECT_FORMAT_UNSUPPORTED, "malformed pack header"
        )
    version, count = struct.unpack(">II", pack[4:12])
    if version not in {2, 3}:
        raise GitAdmissionError(
            LOCAL_OBJECT_FORMAT_UNSUPPORTED, "unsupported pack version"
        )
    trailer = pack[-hash_bytes:]
    if (
        _object_digest(object_format, pack[:-hash_bytes]) != trailer
        or f"pack-{trailer.hex()}" != base
    ):
        raise GitAdmissionError(
            LOCAL_OBJECT_FORMAT_UNSUPPORTED, "pack checksum or name mismatch"
        )
    fanout_start = 8
    fanout_end = fanout_start + 256 * 4
    if (
        len(index) < fanout_end + 2 * hash_bytes
        or index[:4] != b"\xfftOc"
        or struct.unpack(">I", index[4:8])[0] != 2
    ):
        raise GitAdmissionError(LOCAL_OBJECT_FORMAT_UNSUPPORTED, "unsupported index")
    fanout = struct.unpack(">256I", index[fanout_start:fanout_end])
    if (
        any(right < left for left, right in zip(fanout, fanout[1:]))
        or fanout[-1] != count
    ):
        raise GitAdmissionError(
            LOCAL_OBJECT_FORMAT_UNSUPPORTED, "pack/index object counts differ"
        )
    object_count = int(count)
    oids_start = fanout_end
    oids_end = oids_start + object_count * hash_bytes
    crc_start = oids_end
    offsets_start = crc_start + object_count * 4
    large_start = offsets_start + object_count * 4
    minimum = large_start + 2 * hash_bytes
    if len(index) < minimum:
        raise GitAdmissionError(LOCAL_OBJECT_FORMAT_UNSUPPORTED, "index is truncated")
    if (
        index[-2 * hash_bytes : -hash_bytes] != trailer
        or _object_digest(object_format, index[:-hash_bytes]) != index[-hash_bytes:]
    ):
        raise GitAdmissionError(
            LOCAL_OBJECT_FORMAT_UNSUPPORTED, "index checksum mismatch"
        )
    oids = [
        index[oids_start + item * hash_bytes : oids_start + (item + 1) * hash_bytes]
        for item in range(object_count)
    ]
    if any(left >= right for left, right in zip(oids, oids[1:])):
        raise GitAdmissionError(
            LOCAL_OBJECT_FORMAT_UNSUPPORTED,
            "index object IDs are not unique and sorted",
        )
    cumulative = 0
    for bucket, reported in enumerate(fanout):
        while cumulative < len(oids) and oids[cumulative][0] <= bucket:
            cumulative += 1
        if reported != cumulative:
            raise GitAdmissionError(
                LOCAL_OBJECT_FORMAT_UNSUPPORTED,
                "index fanout does not match object IDs",
            )
    large_bytes = len(index) - large_start - 2 * hash_bytes
    if large_bytes < 0 or large_bytes % 8:
        raise GitAdmissionError(
            LOCAL_OBJECT_FORMAT_UNSUPPORTED, "invalid large-offset table"
        )
    large_count = large_bytes // 8
    used_large: set[int] = set()
    offsets: list[int] = []
    for item in range(object_count):
        raw_offset = struct.unpack(
            ">I", index[offsets_start + item * 4 : offsets_start + (item + 1) * 4]
        )[0]
        if raw_offset & 0x80000000:
            position = raw_offset & 0x7FFFFFFF
            if position >= large_count or position in used_large:
                raise GitAdmissionError(
                    LOCAL_OBJECT_FORMAT_UNSUPPORTED, "invalid large-offset reference"
                )
            used_large.add(position)
            offset = struct.unpack(
                ">Q",
                index[large_start + position * 8 : large_start + (position + 1) * 8],
            )[0]
        else:
            offset = raw_offset
        if offset < 12 or offset >= len(pack) - hash_bytes or offset in offsets:
            raise GitAdmissionError(
                LOCAL_OBJECT_FORMAT_UNSUPPORTED, "invalid or duplicate pack offset"
            )
        offsets.append(offset)
    if used_large != set(range(large_count)):
        raise GitAdmissionError(
            LOCAL_OBJECT_FORMAT_UNSUPPORTED, "unreferenced large offset"
        )
    crc_values = (
        struct.unpack(f">{object_count}I", index[crc_start:offsets_start])
        if object_count
        else ()
    )
    ordered = sorted(zip(offsets, crc_values))
    for position, (offset, expected_crc) in enumerate(ordered):
        end = (
            ordered[position + 1][0]
            if position + 1 < len(ordered)
            else len(pack) - hash_bytes
        )
        if end <= offset or zlib.crc32(pack[offset:end]) & 0xFFFFFFFF != expected_crc:
            raise GitAdmissionError(
                LOCAL_OBJECT_FORMAT_UNSUPPORTED, "pack entry CRC mismatch"
            )


def _seal_object_store(root: Path) -> None:
    paths: list[Path] = []
    for directory, directories, files in os.walk(root, followlinks=False):
        root_path = Path(directory)
        paths.append(root_path)
        for name in directories:
            child = root_path / name
            if child.is_symlink():
                raise GitAdmissionError(
                    LOCAL_LAYOUT_UNSAFE, "private object store contains a link"
                )
        for name in files:
            child = root_path / name
            if child.is_symlink() or not child.is_file():
                raise GitAdmissionError(
                    LOCAL_LAYOUT_UNSAFE, "private object store contains a special file"
                )
            child.chmod(0o400)
    for sealed_directory in sorted(
        paths, key=lambda value: len(value.parts), reverse=True
    ):
        sealed_directory.chmod(0o500)
