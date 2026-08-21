"""Trusted, package-independent Go toolchain discovery and identity.

The caller captures :class:`OperatorSearchPath` before adding project command
shims to ``PATH`` and supplies every repository/project-owned root that must
not select Go.  This module never reads a skill manifest, enters a package
directory, or runs a source-aware Go command.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import platform
import posixpath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import BinaryIO, Final, Protocol


TOOLCHAIN_ALGORITHM: Final = "curator-go-toolchain-v1"
GO_RELPATH: Final = "bin/go"
TESTED_GO_FAMILIES: Final[tuple[str, ...]] = ("1.25",)

GO_ENV_FIELDS: Final[tuple[str, ...]] = (
    "GOROOT",
    "GOHOSTOS",
    "GOHOSTARCH",
    "GOOS",
    "GOARCH",
    "GO386",
    "GOAMD64",
    "GOARM",
    "GOARM64",
    "GOMIPS",
    "GOMIPS64",
    "GOPPC64",
    "GORISCV64",
    "GOWASM",
    "GOTELEMETRY",
    "GOTELEMETRYDIR",
)

TUNING_VARIABLES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "386": "GO386",
        "amd64": "GOAMD64",
        "arm": "GOARM",
        "arm64": "GOARM64",
        "mips": "GOMIPS",
        "mipsle": "GOMIPS",
        "mips64": "GOMIPS64",
        "mips64le": "GOMIPS64",
        "ppc64": "GOPPC64",
        "ppc64le": "GOPPC64",
        "riscv64": "GORISCV64",
        "wasm": "GOWASM",
    }
)

DEFAULT_PROBE_TIMEOUT: Final = 15.0
DEFAULT_FINGERPRINT_TIMEOUT: Final = 600.0
MAX_FINGERPRINT_TIMEOUT: Final = 3600.0
FINGERPRINT_TIMEOUT_ENV: Final = "CSK_GO_FINGERPRINT_TIMEOUT"
DEFAULT_OUTPUT_LIMIT: Final = 64 * 1024
MAX_VERSION_OUTPUT: Final = 4096

_TOOLCHAIN_DOMAIN: Final = TOOLCHAIN_ALGORITHM.encode("ascii") + b"\x00"
_GO_VERSION_RE: Final = re.compile(
    r"^go version go(1\.([0-9]+)(?:\.[0-9]+)?"
    r"(?:rc[0-9]+|beta[0-9]+)?) ([a-z0-9]+)/([a-z0-9]+)$"
)
_MACHO_MAGICS: Final[frozenset[bytes]] = frozenset(
    {
        b"\xfe\xed\xfa\xce",
        b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }
)


class ToolchainError(RuntimeError):
    """Stable failure at the package-independent Go trust boundary."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"go-v1 {code}: {detail}")


@dataclass(frozen=True)
class OperatorSearchPath:
    """Exact process search path captured before project shim augmentation."""

    entries: tuple[str, ...]


def capture_operator_search_path(
    environment: Mapping[str, str] | None = None,
) -> OperatorSearchPath:
    """Capture ``PATH`` without consulting it again during Go resolution.

    Callers must invoke this at process entry, before adding ``.agents/bin`` or
    any other project-managed directory.
    """

    source = os.environ if environment is None else environment
    raw = source.get("PATH", "")
    if not raw:
        return OperatorSearchPath(())
    return OperatorSearchPath(tuple(raw.split(os.pathsep)))


def resolve_fingerprint_timeout(
    timeout: float | None = None,
    environment: Mapping[str, str] | None = None,
) -> float:
    """Resolve the toolchain fingerprint deadline for one hashing pass.

    Precedence is caller, then operator, then default: an explicit ``timeout``
    wins, otherwise ``CSK_GO_FINGERPRINT_TIMEOUT`` is read from the process
    environment, otherwise :data:`DEFAULT_FINGERPRINT_TIMEOUT` applies.  The
    result is always clamped to ``(0, MAX_FINGERPRINT_TIMEOUT]`` so the pass
    keeps a liveness bound no input can remove, and an unusable operator value
    degrades to the default instead of failing the install.

    This deadline bounds how long hashing a complete GOROOT may take; it is not
    a trust decision.  A cold toolchain directory behind on-access antivirus is
    slow to read the first time, so the bound must be raisable without weakening
    any check: exceeding it still refuses the toolchain, never admits it.
    """

    if timeout is not None:
        return _bounded_fingerprint_timeout(timeout)
    source = os.environ if environment is None else environment
    raw = source.get(FINGERPRINT_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_FINGERPRINT_TIMEOUT
    try:
        parsed = float(raw)
    except ValueError:
        return DEFAULT_FINGERPRINT_TIMEOUT
    return _bounded_fingerprint_timeout(parsed)


def _bounded_fingerprint_timeout(value: float) -> float:
    if not value > 0:  # also rejects NaN, which would disable the deadline
        return DEFAULT_FINGERPRINT_TIMEOUT
    return min(value, MAX_FINGERPRINT_TIMEOUT)


@dataclass(frozen=True)
class NativeTarget:
    """Frozen native Go target with exactly one architecture tuning value."""

    goos: str
    goarch: str
    tuning: Mapping[str, str]

    def __post_init__(self) -> None:
        frozen = MappingProxyType(dict(self.tuning))
        if len(frozen) != 1:
            raise ValueError("a native Go target must carry exactly one tuning variable")
        object.__setattr__(self, "tuning", frozen)


@dataclass(frozen=True)
class ToolchainIdentity:
    """Portable logical identity of a complete GOROOT tree."""

    algorithm: str
    content_sha256: str
    go_relpath: str
    go_version: str


@dataclass(frozen=True)
class ToolchainSnapshot:
    """Frozen package-independent values consumed by later build stages."""

    executable: Path
    goroot: Path
    target: NativeTarget
    toolchain: ToolchainIdentity
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


@dataclass(frozen=True)
class ProbeResult:
    stdout: bytes = b""
    stderr: bytes = b""
    returncode: int = 0


class ProbeRunner(Protocol):
    """Direct-process seam used only for the three bootstrap probes."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout: float,
        output_limit: int,
    ) -> ProbeResult: ...


class SubprocessProbeRunner:
    """Run an absolute Go executable directly with closed stdin."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        timeout: float,
        output_limit: int,
    ) -> ProbeResult:
        if output_limit <= 0:
            raise ToolchainError(
                "process_output_limit",
                "Go probe output bound must be positive",
            )
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except OSError as exc:
            raise ToolchainError("process_failed", "cannot start the trusted Go executable") from exc

        stdout_pipe = process.stdout
        stderr_pipe = process.stderr
        if stdout_pipe is None or stderr_pipe is None:
            process.kill()
            process.wait()
            raise ToolchainError("process_failed", "cannot capture Go probe output")

        stdout = bytearray()
        stderr = bytearray()
        budget_lock = threading.Lock()
        output_exceeded = threading.Event()
        stop_requested = threading.Event()
        reader_errors: list[BaseException] = []
        remaining = output_limit

        def drain(stream: BinaryIO, destination: bytearray) -> None:
            nonlocal remaining
            try:
                while True:
                    chunk = stream.read(16 * 1024)
                    if not chunk:
                        return
                    with budget_lock:
                        accepted = min(len(chunk), remaining)
                        destination.extend(chunk[:accepted])
                        remaining -= accepted
                        if accepted != len(chunk):
                            output_exceeded.set()
                            stop_requested.set()
                            return
            except BaseException as exc:
                with budget_lock:
                    reader_errors.append(exc)
                stop_requested.set()

        stdout_thread = threading.Thread(
            target=drain,
            args=(stdout_pipe, stdout),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=drain,
            args=(stderr_pipe, stderr),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        deadline = _elapsed() + timeout
        while process.poll() is None:
            if stop_requested.wait(timeout=0.01):
                try:
                    process.kill()
                except OSError:
                    pass
                break
            if _elapsed() >= deadline:
                timed_out = True
                try:
                    process.kill()
                except OSError:
                    pass
                break
        process.wait()
        stdout_thread.join()
        stderr_thread.join()
        stdout_pipe.close()
        stderr_pipe.close()
        if timed_out:
            raise ToolchainError("process_timeout", "Go probe exceeded its deadline")
        if output_exceeded.is_set():
            raise ToolchainError("process_output_limit", "Go probe exceeded its output bound")
        if reader_errors:
            raise ToolchainError(
                "process_failed",
                "cannot capture Go probe output",
            ) from reader_errors[0]
        return ProbeResult(
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            returncode=process.returncode,
        )


@dataclass(frozen=True)
class ToolchainConfig:
    """Manager/operator inputs for one package-independent toolchain probe."""

    private_base: Path
    operator_search_path: OperatorSearchPath
    forbidden_roots: tuple[Path, ...]
    go_executable: Path | None = None
    goroot: Path | None = None
    runner: ProbeRunner | None = None
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT
    output_limit: int = DEFAULT_OUTPUT_LIMIT
    fingerprint_timeout: float | None = None


@dataclass(frozen=True)
class _Host:
    goos: str
    goarch: str
    windows: bool


@dataclass(frozen=True)
class _ProbeLayout:
    root: Path
    empty: Path
    empty_path: Path
    gopath: Path
    gomodcache: Path
    gocache: Path
    gotmp: Path
    home: Path
    config: Path
    tmp: Path
    appdata: Path
    localappdata: Path
    userprofile: Path


@dataclass(frozen=True)
class _DirectoryAnchor:
    path: Path
    initial_stat: os.stat_result


@dataclass(frozen=True)
class _ProbeBoundary:
    operation_root: Path
    config_root: Path
    anchors: tuple[_DirectoryAnchor, ...]


@dataclass(frozen=True)
class _Selection:
    executable: Path
    goroot: Path
    root_stat: os.stat_result


@dataclass(frozen=True)
class _TreeRecord:
    protocol_path: str
    path_bytes: bytes
    native_path: Path
    kind: str
    initial_stat: os.stat_result
    link_target: str | None = None


@dataclass(frozen=True)
class _TreeState:
    path_bytes: bytes
    kind: str
    size: int
    payload_sha256: bytes


class ToolchainSession:
    """Own one private probe root and revalidate GOROOT before teardown."""

    def __init__(
        self,
        *,
        snapshot: ToolchainSnapshot,
        operation_root: Path,
        private_base: Path,
        root_stat: os.stat_result,
        tree_state: tuple[_TreeState, ...],
        fingerprint_timeout: float,
        host: _Host,
    ):
        self._snapshot = snapshot
        self._operation_root = operation_root
        self._private_base = private_base
        self._root_stat = root_stat
        self._tree_state = tree_state
        self._fingerprint_timeout = fingerprint_timeout
        self._host = host
        self._closed = False
        self._close_error: BaseException | None = None

    @property
    def snapshot(self) -> ToolchainSnapshot:
        return self._snapshot

    @property
    def operation_root(self) -> Path:
        return self._operation_root

    @property
    def executable(self) -> Path:
        return self._snapshot.executable

    @property
    def goroot(self) -> Path:
        return self._snapshot.goroot

    @property
    def target(self) -> NativeTarget:
        return self._snapshot.target

    @property
    def toolchain(self) -> ToolchainIdentity:
        return self._snapshot.toolchain

    @property
    def environment(self) -> Mapping[str, str]:
        return self._snapshot.environment

    def verify(self) -> None:
        """Re-fingerprint the exact selected tree after the last child exits."""

        _verify_selected_root(
            self.goroot,
            self._root_stat,
            self.executable,
            self._host,
        )
        deadline = _deadline(self._fingerprint_timeout)
        digest, state = _fingerprint_normalized(
            self.goroot,
            self.toolchain.go_version,
            deadline=deadline,
        )
        if digest != self.toolchain.content_sha256 or state != self._tree_state:
            raise ToolchainError("toolchain_mutated", "toolchain tree changed during operation")

    def close(self) -> None:
        """Revalidate and remove all operation-private state, even on drift."""

        if self._closed:
            if self._close_error is not None:
                raise self._close_error
            return
        self._closed = True
        verify_error: BaseException | None = None
        try:
            self.verify()
        except BaseException as exc:
            verify_error = exc
        cleanup_error = _remove_private_state(self._operation_root, self._private_base)
        if verify_error is not None:
            if cleanup_error is not None:
                verify_error.add_note(str(cleanup_error))
            self._close_error = verify_error
            raise verify_error
        if cleanup_error is not None:
            self._close_error = cleanup_error
            raise cleanup_error

    def release(self) -> None:
        """Remove private state after a caller has already run ``verify``."""

        if self._closed:
            if self._close_error is not None:
                raise self._close_error
            return
        self._closed = True
        cleanup_error = _remove_private_state(self._operation_root, self._private_base)
        if cleanup_error is not None:
            self._close_error = cleanup_error
            raise cleanup_error

    def __enter__(self) -> ToolchainSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, traceback
        if exc is None:
            self.close()
            return
        try:
            self.close()
        except BaseException as close_error:
            exc.add_note(f"toolchain session teardown also failed: {close_error}")


def establish_toolchain(config: ToolchainConfig) -> ToolchainSession:
    """Resolve, probe, and fingerprint one trusted native Go installation."""

    return _establish_toolchain(config, _native_host())


def preflight_toolchain(config: ToolchainConfig) -> None:
    """Reject unsupported or non-native Go executables without writing state.

    The complete probe still runs in :func:`establish_toolchain`.  This first
    pass deliberately uses only ``go version`` so status failure boundaries
    can be decided before allocating operation-private configuration, cache,
    telemetry, or temporary directories.
    """

    host = _native_host()
    probe_timeout = _bounded_timeout(config.probe_timeout, DEFAULT_PROBE_TIMEOUT)
    output_limit = config.output_limit
    if output_limit <= 0 or output_limit > DEFAULT_OUTPUT_LIMIT:
        output_limit = DEFAULT_OUTPUT_LIMIT
    forbidden = _canonical_forbidden_roots(config.forbidden_roots)
    selection = _select_toolchain(config, host, forbidden)
    runner = SubprocessProbeRunner() if config.runner is None else config.runner
    environment: dict[str, str] = {
        "GOENV": "off",
        "GOROOT": str(selection.goroot),
        "GOTOOLCHAIN": "local",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "",
    }
    if host.windows:
        for name in ("SYSTEMROOT", "WINDIR"):
            inherited = os.environ.get(name)
            if inherited:
                environment[name] = inherited
    _verify_selected_root(
        selection.goroot,
        selection.root_stat,
        selection.executable,
        host,
    )
    try:
        result = runner.run(
            (str(selection.executable), "version"),
            cwd=selection.goroot,
            environment=dict(sorted(environment.items())),
            timeout=probe_timeout,
            output_limit=output_limit,
        )
    except ToolchainError:
        raise
    except Exception as exc:
        raise ToolchainError("go_version_failed", "version failed") from exc
    if len(result.stdout) + len(result.stderr) > output_limit:
        raise ToolchainError(
            "process_output_limit",
            "Go probe exceeded its output bound",
        )
    if result.returncode != 0:
        raise ToolchainError(
            "go_version_failed",
            f"version exited with status {result.returncode}",
        )
    _version, family, version_goos, version_goarch = _parse_go_version(
        result.stdout
    )
    if family not in TESTED_GO_FAMILIES:
        raise ToolchainError(
            "unsupported_go_family",
            f"Go release family {family} is not allowlisted",
        )
    if version_goos != host.goos or version_goarch != host.goarch:
        raise ToolchainError(
            "target_mismatch",
            "Go version target and manager platform must be identical",
        )
    _verify_selected_root(
        selection.goroot,
        selection.root_stat,
        selection.executable,
        host,
    )


def probe_toolchain(config: ToolchainConfig) -> ToolchainSnapshot:
    """One-shot package-independent probe that leaves no private state."""

    session = establish_toolchain(config)
    snapshot = session.snapshot
    session.close()
    return snapshot


def normalize_go_version(stdout: bytes) -> str:
    """Normalize the protocol's one-line LF/CRLF ``go version`` output."""

    if (
        not stdout
        or len(stdout) > MAX_VERSION_OUTPUT
        or not stdout.endswith(b"\n")
        or stdout.count(b"\n") != 1
        or b"\x00" in stdout
    ):
        raise ToolchainError(
            "malformed_go_version",
            "go version output must be one bounded UTF-8 line with a terminal LF",
        )
    payload = stdout[:-1]
    if payload.endswith(b"\r"):
        payload = payload[:-1]
    if not payload or b"\r" in payload or b"\n" in payload or b"\x00" in payload:
        raise ToolchainError("malformed_go_version", "go version output has invalid line structure")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ToolchainError("malformed_go_version", "go version output is not valid UTF-8") from exc


def parse_normalized_go_version(value: str) -> tuple[str, str, str, str]:
    """Parse one normalized ``go version`` identity using the trusted probe grammar."""

    if not isinstance(value, str):
        raise ToolchainError("malformed_go_version", "go version identity must be text")
    try:
        stdout = value.encode("utf-8", errors="strict") + b"\n"
    except UnicodeEncodeError as exc:
        raise ToolchainError(
            "malformed_go_version",
            "go version identity is not valid Unicode",
        ) from exc
    parsed = _parse_go_version(stdout)
    if parsed[0] != value:
        raise ToolchainError(
            "malformed_go_version",
            "go version identity is not normalized",
        )
    return parsed


def fingerprint_toolchain(
    goroot: Path,
    go_version_stdout: bytes,
    *,
    timeout: float | None = None,
) -> ToolchainIdentity:
    """Compute ``curator-go-toolchain-v1`` over a complete real GOROOT.

    ``timeout`` bounds this single hashing pass; leave it unset to accept the
    operator surface described by :func:`resolve_fingerprint_timeout`.
    """

    version = normalize_go_version(go_version_stdout)
    root = _canonical_directory(goroot, "toolchain_unreadable", "GOROOT is unavailable")
    deadline = _deadline(resolve_fingerprint_timeout(timeout))
    digest, _ = _fingerprint_normalized(root, version, deadline=deadline)
    return ToolchainIdentity(
        algorithm=TOOLCHAIN_ALGORITHM,
        content_sha256=digest,
        go_relpath=GO_RELPATH,
        go_version=version,
    )


def _establish_toolchain(config: ToolchainConfig, host: _Host) -> ToolchainSession:
    probe_timeout = _bounded_timeout(config.probe_timeout, DEFAULT_PROBE_TIMEOUT)
    fingerprint_timeout = resolve_fingerprint_timeout(config.fingerprint_timeout)
    output_limit = config.output_limit
    if output_limit <= 0 or output_limit > DEFAULT_OUTPUT_LIMIT:
        output_limit = DEFAULT_OUTPUT_LIMIT
    forbidden = _canonical_forbidden_roots(config.forbidden_roots)
    selection = _select_toolchain(config, host, forbidden)
    private_base = _validate_private_base(config.private_base, forbidden)

    operation_root: Path | None = None
    try:
        operation_root = Path(
            tempfile.mkdtemp(prefix=".csk-go-probe-", dir=private_base)
        ).resolve(strict=True)
        _restrict_directory(operation_root)
        layout = _create_probe_layout(operation_root, host)
        probe_boundary = _capture_probe_boundary(layout)
        bootstrap = _bootstrap_environment(layout, host)
        runner = SubprocessProbeRunner() if config.runner is None else config.runner

        def run_probe(arguments: tuple[str, ...], failure_code: str) -> ProbeResult:
            _verify_selected_root(
                selection.goroot,
                selection.root_stat,
                selection.executable,
                host,
            )
            _verify_probe_boundary(probe_boundary)
            argv = (str(selection.executable), *arguments)
            try:
                result = runner.run(
                    argv,
                    cwd=layout.empty,
                    environment=bootstrap,
                    timeout=probe_timeout,
                    output_limit=output_limit,
                )
            except ToolchainError:
                _verify_probe_boundary(probe_boundary)
                raise
            except Exception as exc:
                _verify_probe_boundary(probe_boundary)
                raise ToolchainError(failure_code, f"{' '.join(arguments)} failed") from exc
            _verify_probe_boundary(probe_boundary)
            if len(result.stdout) + len(result.stderr) > output_limit:
                raise ToolchainError(
                    "process_output_limit",
                    "Go probe exceeded its output bound",
                )
            if result.returncode != 0:
                raise ToolchainError(
                    failure_code,
                    f"{' '.join(arguments)} exited with status {result.returncode}",
                )
            return result

        run_probe(("telemetry", "off"), "telemetry_initialization_failed")
        version_result = run_probe(("version",), "go_version_failed")
        version, family, version_goos, version_goarch = _parse_go_version(
            version_result.stdout
        )
        if family not in TESTED_GO_FAMILIES:
            raise ToolchainError(
                "unsupported_go_family",
                f"Go release family {family} is not allowlisted",
            )
        environment_result = run_probe(
            ("env", "-json", *GO_ENV_FIELDS),
            "go_env_failed",
        )
        probed = _decode_probe_environment(environment_result.stdout, output_limit)
        _validate_probe(
            probed,
            selection,
            host,
            version_goos,
            version_goarch,
            probe_boundary,
        )
        if any(layout.empty.iterdir()):
            raise ToolchainError(
                "process_environment_poisoned",
                "Go probe modified its empty working directory",
            )
        target = _target_from_probe(probed)
        digest, tree_state = _fingerprint_normalized(
            selection.goroot,
            version,
            deadline=_deadline(fingerprint_timeout),
        )
        _verify_probe_boundary(probe_boundary)
        _verify_selected_root(
            selection.goroot,
            selection.root_stat,
            selection.executable,
            host,
        )
        environment = _build_environment(bootstrap, selection.goroot, target)
        snapshot = ToolchainSnapshot(
            executable=selection.executable,
            goroot=selection.goroot,
            target=target,
            toolchain=ToolchainIdentity(
                algorithm=TOOLCHAIN_ALGORITHM,
                content_sha256=digest,
                go_relpath=GO_RELPATH,
                go_version=version,
            ),
            environment=environment,
        )
        return ToolchainSession(
            snapshot=snapshot,
            operation_root=operation_root,
            private_base=private_base,
            root_stat=selection.root_stat,
            tree_state=tree_state,
            fingerprint_timeout=fingerprint_timeout,
            host=host,
        )
    except BaseException as exc:
        if operation_root is not None:
            cleanup_error = _remove_private_state(operation_root, private_base)
            if cleanup_error is not None:
                exc.add_note(str(cleanup_error))
        raise


def _parse_go_version(stdout: bytes) -> tuple[str, str, str, str]:
    normalized = normalize_go_version(stdout)
    match = _GO_VERSION_RE.fullmatch(normalized)
    if match is None:
        raise ToolchainError(
            "malformed_go_version",
            "go version output does not identify a release and target",
        )
    minor_text = match.group(2)
    if len(minor_text) > 1 and minor_text.startswith("0"):
        raise ToolchainError(
            "malformed_go_version",
            "go version release family has a leading zero",
        )
    minor = int(minor_text)
    if minor < 23:
        raise ToolchainError("unsupported_go_family", "Go release is older than 1.23")
    return normalized, f"1.{minor}", match.group(3), match.group(4)


class _DuplicateJSONKey(ValueError):
    pass


def _decode_probe_environment(payload: bytes, output_limit: int) -> dict[str, str]:
    if not payload or len(payload) > output_limit:
        raise ToolchainError(
            "invalid_go_env",
            "go env output is empty or oversized",
        )
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ToolchainError("invalid_go_env", "go env output is invalid UTF-8") from exc

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJSONKey(key)
            result[key] = value
        return result

    try:
        decoded = json.loads(text, object_pairs_hook=unique_object)
    except _DuplicateJSONKey as exc:
        raise ToolchainError("invalid_go_env", f"go env output repeats {exc}") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ToolchainError("invalid_go_env", "go env output is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ToolchainError("invalid_go_env", "go env output is not a JSON object")
    if set(decoded) != set(GO_ENV_FIELDS) or len(decoded) != len(GO_ENV_FIELDS):
        raise ToolchainError(
            "invalid_go_env",
            "go env output does not contain the exact fixed field set",
        )
    values: dict[str, str] = {}
    for key in GO_ENV_FIELDS:
        value = decoded[key]
        if not isinstance(value, str):
            raise ToolchainError("invalid_go_env", f"go env value {key} is not a string")
        values[key] = value
    return values


def _validate_probe(
    values: Mapping[str, str],
    selection: _Selection,
    host: _Host,
    version_goos: str,
    version_goarch: str,
    probe_boundary: _ProbeBoundary,
) -> None:
    _verify_probe_boundary(probe_boundary)
    try:
        probed_root = Path(values["GOROOT"]).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ToolchainError(
            "toolchain_executable_mismatch",
            "go env GOROOT is unavailable",
        ) from exc
    if not _same_path(probed_root, selection.goroot):
        raise ToolchainError(
            "toolchain_executable_mismatch",
            "go env GOROOT does not match the selected toolchain",
        )
    if (
        values["GOHOSTOS"] != host.goos
        or values["GOOS"] != host.goos
        or version_goos != host.goos
        or values["GOHOSTARCH"] != host.goarch
        or values["GOARCH"] != host.goarch
        or version_goarch != host.goarch
    ):
        raise ToolchainError(
            "target_mismatch",
            "Go host, target, version, and manager platform must be identical",
        )
    if values["GOTELEMETRY"] != "off":
        raise ToolchainError(
            "telemetry_initialization_failed",
            f"go env reports telemetry mode {values['GOTELEMETRY']!r}",
        )
    try:
        telemetry = Path(values["GOTELEMETRYDIR"]).resolve(strict=True)
        physical_config = probe_boundary.config_root.resolve(strict=True)
        physical_operation = probe_boundary.operation_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ToolchainError(
            "telemetry_directory_untrusted",
            "Go telemetry directory is unavailable",
        ) from exc
    if (
        not _same_path_or_lexical(physical_config, probe_boundary.config_root)
        or not _same_path_or_lexical(
            physical_operation,
            probe_boundary.operation_root,
        )
        or not _strictly_below(physical_config, physical_operation)
        or not _strictly_below(telemetry, physical_config)
        or not _strictly_below(telemetry, physical_operation)
    ):
        raise ToolchainError(
            "telemetry_directory_untrusted",
            "Go telemetry directory is outside the private operation root",
        )


def _target_from_probe(values: Mapping[str, str]) -> NativeTarget:
    goarch = values["GOARCH"]
    tuning_name = TUNING_VARIABLES.get(goarch)
    if tuning_name is None:
        raise ToolchainError(
            "target_mismatch",
            f"native architecture {goarch!r} has no closed tuning variable",
        )
    tuning_value = values[tuning_name]
    if (
        not tuning_value
        or len(tuning_value) > 8192
        or "\r" in tuning_value
        or "\n" in tuning_value
        or "\x00" in tuning_value
    ):
        raise ToolchainError(
            "target_mismatch",
            f"native tuning variable {tuning_name} is empty or malformed",
        )
    try:
        tuning_value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ToolchainError(
            "target_mismatch",
            f"native tuning variable {tuning_name} is invalid Unicode",
        ) from exc
    return NativeTarget(
        goos=values["GOOS"],
        goarch=goarch,
        tuning={tuning_name: tuning_value},
    )


def _select_toolchain(
    config: ToolchainConfig,
    host: _Host,
    forbidden: tuple[Path, ...],
) -> _Selection:
    launcher: Path
    configured_root: Path | None = None
    if config.go_executable is not None:
        launcher = config.go_executable
    elif config.goroot is not None:
        configured_root = config.goroot
        launcher = configured_root / "bin" / _platform_go_name(host)
    else:
        launcher = _search_operator_path(config.operator_search_path, host)

    if not launcher.is_absolute():
        raise ToolchainError(
            "untrusted_go_executable",
            "trusted Go executable must be absolute",
        )
    _reject_forbidden_launcher(launcher, forbidden)
    try:
        resolved_launcher = launcher.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ToolchainError(
            "go_toolchain_missing",
            "trusted Go executable is unavailable",
        ) from exc
    _reject_forbidden_launcher(resolved_launcher, forbidden)

    expected_name = _platform_go_name(host)
    if not _path_name_matches(resolved_launcher.name, expected_name, host):
        raise ToolchainError(
            "toolchain_executable_mismatch",
            f"selected executable is not {expected_name}",
        )
    if not _path_name_matches(resolved_launcher.parent.name, "bin", host):
        error = ToolchainError(
            "toolchain_executable_mismatch",
            "selected Go executable is not below a GOROOT bin directory",
        )
        # The detail is the cross-implementation protocol string; the operator
        # remedy rides along as a note (see the toolchain_timeout pattern).
        error.add_note(
            "version-manager shims (goenv, asdf, mise) are wrapper scripts "
            "outside the fingerprinted toolchain tree and are never accepted; "
            "put the real <GOROOT>/bin on PATH first, for example "
            "PATH=\"$(go env GOROOT)/bin:$PATH\""
        )
        raise error
    goroot = resolved_launcher.parent.parent
    try:
        goroot = goroot.resolve(strict=True)
        root_stat = goroot.lstat()
    except (OSError, RuntimeError) as exc:
        raise ToolchainError("go_toolchain_missing", "trusted GOROOT is unavailable") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise ToolchainError(
            "go_toolchain_missing",
            "trusted GOROOT is not a real directory",
        )
    expected = goroot / "bin" / expected_name
    if not _same_path(resolved_launcher, expected):
        raise ToolchainError(
            "toolchain_executable_mismatch",
            "selected Go executable is outside the derived GOROOT",
        )
    if configured_root is not None:
        if not configured_root.is_absolute():
            raise ToolchainError(
                "untrusted_go_executable",
                "configured GOROOT must be absolute",
            )
        try:
            physical_configured_root = configured_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolchainError(
                "go_toolchain_missing",
                "configured GOROOT is unavailable",
            ) from exc
        if not _same_path(physical_configured_root, goroot):
            raise ToolchainError(
                "toolchain_executable_mismatch",
                "configured GOROOT does not contain the selected Go executable",
            )
    if config.go_executable is not None and config.goroot is not None:
        try:
            physical_configured_root = config.goroot.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolchainError(
                "go_toolchain_missing",
                "configured GOROOT is unavailable",
            ) from exc
        if not _same_path(physical_configured_root, goroot):
            raise ToolchainError(
                "toolchain_executable_mismatch",
                "configured Go executable and GOROOT disagree",
            )
    _validate_launcher(resolved_launcher, host)
    return _Selection(
        executable=resolved_launcher,
        goroot=goroot,
        root_stat=root_stat,
    )


def _search_operator_path(search_path: OperatorSearchPath, host: _Host) -> Path:
    if not search_path.entries:
        raise ToolchainError(
            "go_toolchain_missing",
            "captured operator PATH contains no Go executable",
        )
    name = _platform_go_name(host)
    for entry in search_path.entries:
        if not entry or not os.path.isabs(entry):
            raise ToolchainError(
                "untrusted_operator_path",
                "captured operator PATH contains a relative or empty entry",
            )
        directory = Path(entry)
        candidate = directory / name
        if os.path.lexists(candidate):
            return candidate.absolute()
    raise ToolchainError(
        "go_toolchain_missing",
        "captured operator PATH contains no Go executable",
    )


def _validate_launcher(path: Path, host: _Host) -> None:
    try:
        initial = path.lstat()
    except OSError as exc:
        raise ToolchainError(
            "untrusted_go_executable",
            "selected Go launcher is unavailable",
        ) from exc
    if not stat.S_ISREG(initial.st_mode):
        raise ToolchainError(
            "untrusted_go_executable",
            "selected Go launcher is not a regular file",
        )
    if not host.windows and initial.st_mode & 0o111 == 0:
        raise ToolchainError(
            "untrusted_go_executable",
            "selected Go launcher is not executable",
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ToolchainError(
            "untrusted_go_executable",
            "cannot open selected Go launcher",
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_stat(initial, opened):
            raise ToolchainError(
                "untrusted_go_executable",
                "selected Go launcher changed while opening",
            )
        header = os.read(descriptor, 8)
    finally:
        os.close(descriptor)
    if not _native_executable_header(header, host):
        raise ToolchainError(
            "untrusted_go_executable",
            "selected Go launcher is a wrapper rather than a native executable",
        )


def _verify_selected_root(
    goroot: Path,
    root_stat: os.stat_result,
    executable: Path,
    host: _Host,
) -> None:
    try:
        current = goroot.lstat()
    except OSError as exc:
        raise ToolchainError("toolchain_mutated", "fingerprinted GOROOT disappeared") from exc
    if not stat.S_ISDIR(current.st_mode) or not _same_stat(root_stat, current):
        raise ToolchainError("toolchain_mutated", "fingerprinted GOROOT was replaced")
    try:
        _validate_launcher(executable, host)
    except ToolchainError as exc:
        raise ToolchainError("toolchain_mutated", "selected Go executable changed") from exc


def _native_executable_header(header: bytes, host: _Host) -> bool:
    if host.windows:
        return header.startswith(b"MZ")
    return header.startswith(b"\x7fELF") or header[:4] in _MACHO_MAGICS


def _create_probe_layout(root: Path, host: _Host) -> _ProbeLayout:
    home = root / "home"
    appdata = root / "appdata"
    config = appdata if host.windows else root / "config"
    if host.goos == "darwin":
        config = home / "Library" / "Application Support"
    layout = _ProbeLayout(
        root=root,
        empty=root / "empty",
        empty_path=root / "empty-path",
        gopath=root / "gopath",
        gomodcache=root / "gomodcache",
        gocache=root / "gocache",
        gotmp=root / "gotmp",
        home=home,
        config=config,
        tmp=root / "tmp",
        appdata=appdata,
        localappdata=root / "localappdata",
        userprofile=root / "userprofile",
    )
    directories = {
        layout.empty,
        layout.empty_path,
        layout.gopath,
        layout.gomodcache,
        layout.gocache,
        layout.gotmp,
        layout.home,
        layout.config,
        layout.tmp,
    }
    if host.windows:
        directories.update(
            {layout.appdata, layout.localappdata, layout.userprofile}
        )
    try:
        for directory in directories:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            _restrict_directory(directory)
    except OSError as exc:
        raise ToolchainError(
            "private_probe_failed",
            "cannot create operation-private probe layout",
        ) from exc
    return layout


def _capture_probe_boundary(layout: _ProbeLayout) -> _ProbeBoundary:
    try:
        relative_config = layout.config.relative_to(layout.root)
    except ValueError as exc:
        raise ToolchainError(
            "private_probe_failed",
            "platform configuration root is outside the operation-private root",
        ) from exc
    paths = [layout.root]
    current = layout.root
    for component in relative_config.parts:
        current /= component
        paths.append(current)
    anchors: list[_DirectoryAnchor] = []
    for path in paths:
        try:
            info = path.lstat()
            physical = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolchainError(
                "private_probe_failed",
                "cannot anchor the operation-private platform configuration root",
            ) from exc
        if (
            not _is_real_directory(info)
            or not _same_path_or_lexical(physical, path)
        ):
            raise ToolchainError(
                "private_probe_failed",
                "operation-private platform configuration path is not a real directory",
            )
        anchors.append(_DirectoryAnchor(path=path, initial_stat=info))
    return _ProbeBoundary(
        operation_root=layout.root,
        config_root=layout.config,
        anchors=tuple(anchors),
    )


def _verify_probe_boundary(boundary: _ProbeBoundary) -> None:
    for index, anchor in enumerate(boundary.anchors):
        try:
            current = anchor.path.lstat()
            physical = anchor.path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolchainError(
                "telemetry_directory_untrusted",
                "operation-private platform configuration path is unavailable",
            ) from exc
        if (
            not _is_real_directory(current)
            or not _same_stat(anchor.initial_stat, current)
            or not _same_path_or_lexical(physical, anchor.path)
            or (
                index > 0
                and not _strictly_below_lexical(
                    anchor.path,
                    boundary.operation_root,
                )
            )
        ):
            raise ToolchainError(
                "telemetry_directory_untrusted",
                "operation-private platform configuration path was replaced",
            )
    if (
        not boundary.anchors
        or boundary.anchors[0].path != boundary.operation_root
        or boundary.anchors[-1].path != boundary.config_root
    ):
        raise ToolchainError(
            "telemetry_directory_untrusted",
            "operation-private platform configuration anchors are invalid",
        )


def _bootstrap_environment(layout: _ProbeLayout, host: _Host) -> dict[str, str]:
    values: dict[str, str] = {
        "GOENV": "off",
        "GOTOOLCHAIN": "local",
        "LC_ALL": "C",
        "LANG": "C",
        "GOPATH": str(layout.gopath),
        "GOMODCACHE": str(layout.gomodcache),
        "GOCACHE": str(layout.gocache),
        "GOTMPDIR": str(layout.gotmp),
        "HOME": str(layout.home),
        "XDG_CONFIG_HOME": str(layout.config),
        "PATH": str(layout.empty_path),
        "TMPDIR": str(layout.tmp),
    }
    if host.windows:
        values.update(
            {
                "APPDATA": str(layout.appdata),
                "LOCALAPPDATA": str(layout.localappdata),
                "USERPROFILE": str(layout.userprofile),
                "TEMP": str(layout.tmp),
                "TMP": str(layout.tmp),
            }
        )
        for name in ("SYSTEMROOT", "WINDIR"):
            inherited = os.environ.get(name)
            if inherited:
                values[name] = inherited
    return dict(sorted(values.items()))


def _build_environment(
    bootstrap: Mapping[str, str],
    goroot: Path,
    target: NativeTarget,
) -> dict[str, str]:
    values = dict(bootstrap)
    values.update(
        {
            "GOROOT": str(goroot),
            "GOOS": target.goos,
            "GOARCH": target.goarch,
            "GO111MODULE": "on",
            "GOFLAGS": "",
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "GOPRIVATE": "",
            "GONOPROXY": "none",
            "GONOSUMDB": "none",
            "GOVCS": "*:off",
            "GOWORK": "off",
            "CGO_ENABLED": "0",
            "GO_EXTLINK_ENABLED": "0",
            "GOEXPERIMENT": "",
        }
    )
    values.update(target.tuning)
    return dict(sorted(values.items()))


def _fingerprint_normalized(
    goroot: Path,
    version: str,
    *,
    deadline: float,
) -> tuple[str, tuple[_TreeState, ...]]:
    _check_deadline(deadline)
    records = _collect_records(goroot, deadline=deadline)
    records = _canonical_records(records)
    digest = hashlib.sha256()
    digest.update(_TOOLCHAIN_DOMAIN)
    states: list[_TreeState] = []
    for record in records:
        _check_deadline(deadline)
        if record.kind == "D":
            _revalidate_directory(record)
            digest.update(_framed_record_header("D", record.path_bytes, 0))
            states.append(
                _TreeState(
                    path_bytes=record.path_bytes,
                    kind=record.kind,
                    size=0,
                    payload_sha256=b"",
                )
            )
        elif record.kind == "L":
            payload = _revalidate_link(record)
            digest.update(
                _framed_record_header("L", record.path_bytes, len(payload))
            )
            digest.update(payload)
            states.append(
                _TreeState(
                    path_bytes=record.path_bytes,
                    kind=record.kind,
                    size=len(payload),
                    payload_sha256=hashlib.sha256(payload).digest(),
                )
            )
        elif record.kind == "F":
            size, payload_digest = _digest_file(record, digest, deadline)
            states.append(
                _TreeState(
                    path_bytes=record.path_bytes,
                    kind=record.kind,
                    size=size,
                    payload_sha256=payload_digest,
                )
            )
        else:
            raise ToolchainError("special_file_forbidden", "unknown toolchain record kind")
    version_bytes = version.encode("utf-8", errors="strict")
    digest.update(_framed_record_header("V", b"", len(version_bytes)))
    digest.update(version_bytes)
    return f"sha256:{digest.hexdigest()}", tuple(states)


def _collect_records(goroot: Path, *, deadline: float) -> list[_TreeRecord]:
    records: list[_TreeRecord] = []

    def descend(directory: Path, components: tuple[str, ...]) -> None:
        _check_deadline(deadline)
        try:
            with os.scandir(directory) as iterator:
                names = [entry.name for entry in iterator]
        except OSError as exc:
            raise ToolchainError("toolchain_unreadable", "cannot walk GOROOT") from exc
        for component in names:
            _check_deadline(deadline)
            protocol_path = "/".join((*components, component))
            path_bytes = _protocol_path_bytes(protocol_path)
            native_path = directory / component
            try:
                info = os.lstat(native_path)
            except OSError as exc:
                raise ToolchainError(
                    "toolchain_unreadable",
                    f"cannot inspect toolchain path {protocol_path!r}",
                ) from exc
            mode = info.st_mode
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            file_attributes = getattr(info, "st_file_attributes", 0)
            unsupported_reparse = bool(
                file_attributes & reparse_flag
            ) and not stat.S_ISLNK(mode)
            if unsupported_reparse:
                raise ToolchainError(
                    "special_file_forbidden",
                    f"toolchain path {protocol_path!r} is an unsupported reparse point",
                )
            if stat.S_ISDIR(mode):
                kind = "D"
                target = None
            elif stat.S_ISREG(mode):
                kind = "F"
                target = None
            elif stat.S_ISLNK(mode):
                kind = "L"
                target = _validated_link_target(
                    goroot,
                    native_path,
                    protocol_path,
                )
            else:
                raise ToolchainError(
                    "special_file_forbidden",
                    f"toolchain path {protocol_path!r} is not a directory, file, or link",
                )
            records.append(
                _TreeRecord(
                    protocol_path=protocol_path,
                    path_bytes=path_bytes,
                    native_path=native_path,
                    kind=kind,
                    initial_stat=info,
                    link_target=target,
                )
            )
            if kind == "D":
                descend(native_path, (*components, component))

    descend(goroot, ())
    return records


def _canonical_records(records: list[_TreeRecord]) -> list[_TreeRecord]:
    encoded: set[bytes] = set()
    for record in records:
        expected = _protocol_path_bytes(record.protocol_path)
        if expected != record.path_bytes:
            raise ToolchainError(
                "invalid_unicode",
                "toolchain record path bytes are inconsistent",
            )
        if record.path_bytes in encoded:
            raise ToolchainError(
                "duplicate_path",
                f"toolchain contains duplicate encoded path {record.protocol_path!r}",
            )
        encoded.add(record.path_bytes)
    return sorted(records, key=lambda record: record.path_bytes)


def _protocol_path_bytes(protocol_path: str) -> bytes:
    if not protocol_path or protocol_path == "." or "\x00" in protocol_path:
        raise ToolchainError(
            "invalid_unicode",
            "toolchain path is not valid protocol UTF-8",
        )
    components = protocol_path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ToolchainError(
            "invalid_unicode",
            "toolchain path is not valid protocol UTF-8",
        )
    try:
        return protocol_path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ToolchainError(
            "invalid_unicode",
            "toolchain path is not valid protocol UTF-8",
        ) from exc


def _validated_link_target(
    goroot: Path,
    native_path: Path,
    protocol_path: str,
) -> str:
    try:
        target = os.readlink(native_path)
    except OSError as exc:
        raise ToolchainError(
            "toolchain_link_dangling",
            f"cannot read toolchain link {protocol_path!r}",
        ) from exc
    if "\x00" in target:
        raise ToolchainError(
            "invalid_unicode",
            f"toolchain link {protocol_path!r} has an invalid target",
        )
    try:
        target.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ToolchainError(
            "invalid_unicode",
            f"toolchain link {protocol_path!r} has an invalid target",
        ) from exc
    if (
        posixpath.isabs(target)
        or ntpath.isabs(target)
        or bool(ntpath.splitdrive(target)[0])
        or target.startswith(("/", "\\"))
    ):
        raise ToolchainError(
            "toolchain_link_absolute",
            f"toolchain link {protocol_path!r} is absolute",
        )
    link_directory = posixpath.dirname(protocol_path)
    normalized_target = target.replace("\\", "/") if os.name == "nt" else target
    lexical = posixpath.normpath(posixpath.join(link_directory, normalized_target))
    if lexical == ".." or lexical.startswith("../") or lexical.startswith("/"):
        raise ToolchainError(
            "toolchain_link_escape",
            f"toolchain link {protocol_path!r} escapes GOROOT",
        )
    try:
        resolved = native_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ToolchainError(
            "toolchain_link_dangling",
            f"toolchain link {protocol_path!r} is dangling",
        ) from exc
    if not (_same_path(resolved, goroot) or _strictly_below(resolved, goroot)):
        raise ToolchainError(
            "toolchain_link_escape",
            f"toolchain link {protocol_path!r} escapes GOROOT",
        )
    return target


def _revalidate_directory(record: _TreeRecord) -> None:
    try:
        current = record.native_path.lstat()
    except OSError as exc:
        raise ToolchainError(
            "toolchain_mutated",
            f"toolchain directory {record.protocol_path!r} disappeared",
        ) from exc
    if not stat.S_ISDIR(current.st_mode) or not _same_stat(record.initial_stat, current):
        raise ToolchainError(
            "toolchain_mutated",
            f"toolchain directory {record.protocol_path!r} changed",
        )


def _revalidate_link(record: _TreeRecord) -> bytes:
    try:
        current = record.native_path.lstat()
        target = os.readlink(record.native_path)
    except OSError as exc:
        raise ToolchainError(
            "toolchain_mutated",
            f"toolchain link {record.protocol_path!r} changed",
        ) from exc
    if (
        not stat.S_ISLNK(current.st_mode)
        or not _same_stat(record.initial_stat, current)
        or target != record.link_target
    ):
        raise ToolchainError(
            "toolchain_mutated",
            f"toolchain link {record.protocol_path!r} changed",
        )
    try:
        return target.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ToolchainError(
            "toolchain_mutated",
            f"toolchain link {record.protocol_path!r} became invalid Unicode",
        ) from exc


def _digest_file(
    record: _TreeRecord,
    digest: hashlib._Hash,
    deadline: float,
) -> tuple[int, bytes]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(record.native_path, flags)
    except OSError as exc:
        raise ToolchainError(
            "toolchain_mutated",
            f"cannot open toolchain file {record.protocol_path!r}",
        ) from exc
    payload_digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _same_stat(record.initial_stat, opened)
            or opened.st_size < 0
        ):
            raise ToolchainError(
                "toolchain_mutated",
                f"toolchain file {record.protocol_path!r} changed while opening",
            )
        digest.update(
            _framed_record_header("F", record.path_bytes, opened.st_size)
        )
        while True:
            _check_deadline(deadline)
            chunk = os.read(descriptor, 128 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
            payload_digest.update(chunk)
        if total != opened.st_size:
            raise ToolchainError(
                "toolchain_mutated",
                f"toolchain file {record.protocol_path!r} changed while reading",
            )
    finally:
        os.close(descriptor)
    try:
        current = record.native_path.lstat()
    except OSError as exc:
        raise ToolchainError(
            "toolchain_mutated",
            f"toolchain file {record.protocol_path!r} disappeared",
        ) from exc
    if not stat.S_ISREG(current.st_mode) or not _same_stat(record.initial_stat, current):
        raise ToolchainError(
            "toolchain_mutated",
            f"toolchain file {record.protocol_path!r} changed",
        )
    return total, payload_digest.digest()


def _framed_record_header(kind: str, path: bytes, payload_length: int) -> bytes:
    if kind not in {"D", "F", "L", "V"} or payload_length < 0:
        raise ToolchainError(
            "special_file_forbidden",
            "invalid toolchain framing record",
        )
    return (
        kind.encode("ascii")
        + struct.pack(">Q", len(path))
        + path
        + struct.pack(">Q", payload_length)
    )


def _native_host() -> _Host:
    if sys.platform == "darwin":
        goos = "darwin"
    elif os.name == "nt":
        goos = "windows"
    elif sys.platform.startswith("linux"):
        goos = "linux"
    elif sys.platform.startswith("freebsd"):
        goos = "freebsd"
    elif sys.platform.startswith("openbsd"):
        goos = "openbsd"
    elif sys.platform.startswith("netbsd"):
        goos = "netbsd"
    else:
        raise ToolchainError(
            "target_mismatch",
            f"unsupported manager operating system {sys.platform!r}",
        )
    machine = platform.machine().lower()
    aliases = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "i386": "386",
        "i486": "386",
        "i586": "386",
        "i686": "386",
        "x86": "386",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv6l": "arm",
        "armv7l": "arm",
        "ppc64": "ppc64",
        "ppc64le": "ppc64le",
        "riscv64": "riscv64",
        "mips": "mips",
        "mipsel": "mipsle",
        "mips64": "mips64",
        "mips64el": "mips64le",
    }
    goarch = aliases.get(machine)
    if goarch is None or goarch not in TUNING_VARIABLES:
        raise ToolchainError(
            "target_mismatch",
            f"unsupported manager architecture {machine!r}",
        )
    return _Host(goos=goos, goarch=goarch, windows=os.name == "nt")


def _platform_go_name(host: _Host) -> str:
    return "go.exe" if host.windows else "go"


def _path_name_matches(actual: str, expected: str, host: _Host) -> bool:
    if host.windows:
        return actual.casefold() == expected.casefold()
    return actual == expected


def _canonical_forbidden_roots(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    if not roots:
        raise ToolchainError(
            "untrusted_go_executable",
            "repository/project forbidden roots must be supplied",
        )
    result: list[Path] = []
    for root in roots:
        if not root.is_absolute():
            raise ToolchainError(
                "untrusted_go_executable",
                "repository/project forbidden roots must be absolute",
            )
        try:
            result.append(root.resolve(strict=True))
        except (OSError, RuntimeError) as exc:
            raise ToolchainError(
                "untrusted_go_executable",
                f"forbidden root {root} is unavailable",
            ) from exc
    return tuple(result)


def _reject_forbidden_launcher(path: Path, forbidden: tuple[Path, ...]) -> None:
    absolute = path.absolute()
    try:
        physical_parent = path.parent.resolve(strict=True)
        physical = physical_parent / path.name
    except (OSError, RuntimeError):
        physical = absolute
    for root in forbidden:
        if (
            _same_path_or_lexical(absolute, root)
            or _strictly_below_lexical(absolute, root)
            or _same_path_or_lexical(physical, root)
            or _strictly_below_lexical(physical, root)
        ):
            raise ToolchainError(
                "untrusted_go_executable",
                "selected Go executable is under a repository or project-managed root",
            )


def _validate_private_base(path: Path, forbidden: tuple[Path, ...]) -> Path:
    if not path.is_absolute():
        raise ToolchainError(
            "private_probe_failed",
            "private probe base must be absolute",
        )
    try:
        physical = path.resolve(strict=True)
        info = physical.lstat()
    except (OSError, RuntimeError) as exc:
        raise ToolchainError(
            "private_probe_failed",
            "private probe base is unavailable",
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ToolchainError(
            "private_probe_failed",
            "private probe base is not a directory",
        )
    for root in forbidden:
        if _same_path_or_lexical(physical, root) or _strictly_below_lexical(
            physical, root
        ):
            raise ToolchainError(
                "private_probe_failed",
                "private probe base is under a repository or project-managed root",
            )
    return physical


def _canonical_directory(path: Path, code: str, detail: str) -> Path:
    if not path.is_absolute():
        raise ToolchainError(code, detail)
    try:
        physical = path.resolve(strict=True)
        info = physical.lstat()
    except (OSError, RuntimeError) as exc:
        raise ToolchainError(code, detail) from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ToolchainError(code, detail)
    return physical


def _remove_private_state(operation: Path, private_base: Path) -> ToolchainError | None:
    if not _strictly_below_lexical(operation, private_base):
        return ToolchainError(
            "private_probe_cleanup_failed",
            "refusing to remove a probe outside its private base",
        )
    try:
        shutil.rmtree(operation)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return ToolchainError(
            "private_probe_cleanup_failed",
            f"cannot remove operation-private probe: {exc}",
        )
    return None


def _restrict_directory(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)


def _is_real_directory(info: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(info, "st_file_attributes", 0)
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not bool(file_attributes & reparse_flag)
    )


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
    try:
        return os.path.samestat(left, right)
    except (AttributeError, OSError):
        return (
            left.st_dev,
            left.st_ino,
            stat.S_IFMT(left.st_mode),
        ) == (
            right.st_dev,
            right.st_ino,
            stat.S_IFMT(right.st_mode),
        )


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except (OSError, ValueError):
        return _same_path_or_lexical(left, right)


def _same_path_or_lexical(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _strictly_below(path: Path, root: Path) -> bool:
    try:
        return path != root and path.is_relative_to(root)
    except (OSError, ValueError):
        return False


def _strictly_below_lexical(path: Path, root: Path) -> bool:
    path_text = os.path.normcase(os.path.abspath(path))
    root_text = os.path.normcase(os.path.abspath(root))
    try:
        return (
            path_text != root_text
            and os.path.commonpath((path_text, root_text)) == root_text
        )
    except ValueError:
        return False


def _bounded_timeout(value: float, maximum: float) -> float:
    if value <= 0 or value > maximum:
        return maximum
    return value


def _elapsed() -> float:
    """Read the one clock this module measures every deadline with.

    ``time.monotonic()`` is not the same clock on every platform: Windows
    CPython before 3.13 backs it with ``GetTickCount64()``, whose tick is
    15.625 ms.  Two readings taken inside one tick are equal, so a deadline
    shorter than a tick is unobservable — an already-exhausted deadline
    reads as unreached and silently admits the work it was meant to refuse.
    ``time.perf_counter()`` is ``QueryPerformanceCounter()`` on every
    supported Windows CPython and ``CLOCK_MONOTONIC`` elsewhere, so a
    deadline means the same thing on every platform.  This is a refusal
    boundary, and a coarse clock must never round it into an admission.
    """

    return time.perf_counter()


def _deadline(timeout: float) -> float:
    if timeout <= 0:
        raise ToolchainError(
            "toolchain_timeout",
            "toolchain fingerprint timeout must be positive",
        )
    return _elapsed() + timeout


def _check_deadline(deadline: float) -> None:
    if _elapsed() > deadline:
        error = ToolchainError(
            "toolchain_timeout",
            "toolchain fingerprint deadline exceeded",
        )
        # The detail is the cross-implementation protocol string, byte for
        # byte; the operator remedy rides along as a note so it cannot drift
        # from that contract.  Install boundaries render notes through
        # ``installer.failure_text``, so the operator reads both.
        error.add_note(
            "hashing the Go toolchain did not finish in time; set "
            f"{FINGERPRINT_TIMEOUT_ENV} to a larger number of seconds "
            f"(default {DEFAULT_FINGERPRINT_TIMEOUT:g}, maximum "
            f"{MAX_FINGERPRINT_TIMEOUT:g}) on hosts where a cold GOROOT reads "
            "slowly, for example behind on-access antivirus"
        )
        raise error
