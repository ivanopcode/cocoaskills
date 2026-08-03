"""Pure provider-ordered planning for declarative compiled commands.

The planner consumes already resolved, validated, frozen providers.  It owns
only the package-independent toolchain probe, logical input/key derivation,
and read-only protected-cache inspection.  Compilation, publication,
quarantine, launchers, markers, and target swaps are deliberately outside this
module.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import tempfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, Protocol

from .. import skillspec
from . import cache, metadata, source, toolchain


class BuildPlanningError(RuntimeError):
    """Stable failure at the read-only build-planning boundary."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class BuildCommand:
    """One statically validated compiled command below exactly one build root."""

    name: str
    driver: str
    build_root: str
    source_dir: str

    def __post_init__(self) -> None:
        if not self.name:
            raise BuildPlanningError(
                "build_plan_invalid",
                "build command name must be non-empty",
            )
        if self.driver != metadata.GO_V1_DRIVER:
            raise BuildPlanningError(
                "unsupported_build_driver",
                f"unsupported build driver {self.driver!r}",
            )
        if not self.build_root or not self.source_dir:
            raise BuildPlanningError(
                "build_plan_invalid",
                f"build command {self.name!r} has an incomplete source layout",
            )


@dataclass(frozen=True)
class BuildProvider:
    """One provider in dependency-closure order with a retained snapshot."""

    name: str
    snapshot: source.FrozenSnapshot
    commands: tuple[BuildCommand, ...]

    def __post_init__(self) -> None:
        if not self.name:
            raise BuildPlanningError(
                "build_plan_invalid",
                "build provider name must be non-empty",
            )
        if not isinstance(self.snapshot, source.FrozenSnapshot):
            raise BuildPlanningError(
                "build_plan_invalid",
                f"build provider {self.name!r} has no frozen snapshot",
            )
        names = [command.name for command in self.commands]
        if len(names) != len(set(names)):
            raise BuildPlanningError(
                "command_collision",
                f"build provider {self.name!r} declares a command more than once",
            )


@dataclass(frozen=True)
class BuildPlan:
    """Exact logical input and read-only cache outcome for one command."""

    provider: str
    input: metadata.GoBuildInput
    cache_key: str
    inspection: cache.CacheInspection

    @property
    def command(self) -> str:
        return self.input.command

    @property
    def driver(self) -> str:
        return self.input.driver

    @property
    def target(self) -> toolchain.NativeTarget:
        return self.input.target

    @property
    def artifact_path(self) -> str:
        return self.input.artifact_path

    @property
    def result(self) -> str:
        return self.inspection.dry_run_outcome

    def to_json(self) -> dict[str, Any]:
        """Return the stable user-facing dry-run record from the protocol."""

        return {
            "build_root": self.input.build_root,
            "build_source": {
                "algorithm": self.input.build_source.algorithm,
                "content_sha256": self.input.build_source.content_sha256,
            },
            "cache_key": self.cache_key,
            "command": self.input.command,
            "driver": self.input.driver,
            "result": self.result,
            "source_dir": self.input.source_dir,
            "target": {
                "goarch": self.input.target.goarch,
                "goos": self.input.target.goos,
                "tuning": dict(self.input.target.tuning),
            },
        }


class GenerationProbe(Protocol):
    """Read-only optimistic generation source for every shared target read."""

    def capture(self) -> Mapping[str, str]:
        """Return stable target-name to SHA-256 generation observations."""


class _ToolchainSession(Protocol):
    @property
    def target(self) -> toolchain.NativeTarget: ...

    @property
    def toolchain(self) -> toolchain.ToolchainIdentity: ...

    def __enter__(self) -> _ToolchainSession: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> object: ...


ToolchainFactory = Callable[[toolchain.ToolchainConfig], _ToolchainSession]


class FilesystemGenerationProbe:
    """Hash explicit shared paths without creating, repairing, or following them."""

    def __init__(self, paths: Sequence[Path]):
        by_name: dict[str, Path] = {}
        for value in paths:
            path = Path(os.path.abspath(os.fspath(value)))
            by_name[str(path)] = path
        self._paths = tuple(
            by_name[name]
            for name in sorted(by_name, key=lambda item: item.encode("utf-8"))
        )

    @property
    def paths(self) -> tuple[Path, ...]:
        return self._paths

    def capture(self) -> Mapping[str, str]:
        observations: dict[str, str] = {}
        for path in self._paths:
            observations[str(path)] = _generation_digest(path)
        return MappingProxyType(observations)


def provider_from_spec(
    name: str,
    snapshot: source.FrozenSnapshot,
    spec: skillspec.SkillSpec,
    *,
    active_commands: Collection[str] | None = None,
) -> BuildProvider:
    """Project active build declarations into one frozen provider."""

    selected = (
        {
            command.name
            for command in spec.commands.values()
            if command.type == "build"
        }
        if active_commands is None
        else set(active_commands)
    )
    commands: list[BuildCommand] = []
    for command_name in sorted(selected, key=lambda value: value.encode("utf-8")):
        command = spec.commands.get(command_name)
        if command is None:
            raise BuildPlanningError(
                "build_plan_invalid",
                f"provider {name!r} activates unknown command {command_name!r}",
            )
        if command.type != "build":
            continue
        if command.driver is None or command.source_dir is None:
            raise BuildPlanningError(
                "build_plan_invalid",
                f"provider {name!r} build command {command_name!r} is incomplete",
            )
        containing = [
            root
            for root in spec.build_roots
            if _portable_path_contains(root, command.source_dir)
        ]
        if len(containing) != 1:
            raise BuildPlanningError(
                "build_plan_invalid",
                f"provider {name!r} build command {command_name!r} is not below "
                "exactly one build root",
            )
        commands.append(
            BuildCommand(
                name=command.name,
                driver=command.driver,
                build_root=containing[0],
                source_dir=command.source_dir,
            )
        )
    return BuildProvider(name=name, snapshot=snapshot, commands=tuple(commands))


def detect_command_collisions(
    providers: Sequence[BuildProvider],
    *,
    occupied: Mapping[str, str] | None = None,
) -> None:
    """Reject build/build and script/build collisions before any Go probe."""

    owners = dict(occupied or {})
    for provider in providers:
        for command in sorted(
            provider.commands,
            key=lambda item: item.name.encode("utf-8"),
        ):
            previous = owners.get(command.name)
            if previous is not None:
                raise BuildPlanningError(
                    "command_collision",
                    f"Command collision for {command.name!r}: exported by "
                    f"{previous} and {provider.name}",
                )
            owners[command.name] = provider.name


def plan_builds(
    providers: Sequence[BuildProvider],
    *,
    manager_home: Path,
    operator_search_path: toolchain.OperatorSearchPath,
    forbidden_roots: Sequence[Path] = (),
    cache_backend: cache.BuildCacheBackend | None = None,
    establish_toolchain: ToolchainFactory | None = None,
    generation_probe: GenerationProbe | None = None,
    expected_generation: Mapping[str, str] | None = None,
    max_generation_attempts: int = 2,
    read_only_preflight: bool = False,
) -> tuple[BuildPlan, ...]:
    """Produce a complete immutable plan without source-aware Go or mutation.

    When a generation probe is supplied, a changed observation retries the
    toolchain/cache portion.  A caller that needs to retry validation and trust
    gates as well supplies ``expected_generation`` and one attempt, catches
    ``concurrent_state_change``, and repeats its complete read-only operation.
    """

    active = tuple(provider for provider in providers if provider.commands)
    if not active and generation_probe is None:
        return ()
    if max_generation_attempts < 1:
        raise ValueError("max_generation_attempts must be at least one")
    if expected_generation is not None and generation_probe is None:
        raise ValueError("expected_generation requires a generation_probe")

    baseline = (
        dict(expected_generation)
        if expected_generation is not None
        else None
    )
    for attempt in range(max_generation_attempts):
        before = (
            baseline
            if baseline is not None
            else _capture_generation(generation_probe)
        )
        plans = _plan_once(
            active,
            manager_home=manager_home,
            operator_search_path=operator_search_path,
            forbidden_roots=forbidden_roots,
            cache_backend=cache_backend,
            establish_toolchain=establish_toolchain,
            read_only_preflight=read_only_preflight,
        )
        after = _capture_generation(generation_probe)
        if before == after:
            return plans
        baseline = None
        if attempt + 1 == max_generation_attempts:
            raise BuildPlanningError(
                "concurrent_state_change",
                "shared planning state changed during the read-only build plan",
            )
    raise AssertionError("unreachable generation retry state")


def _plan_once(
    providers: Sequence[BuildProvider],
    *,
    manager_home: Path,
    operator_search_path: toolchain.OperatorSearchPath,
    forbidden_roots: Sequence[Path],
    cache_backend: cache.BuildCacheBackend | None,
    establish_toolchain: ToolchainFactory | None,
    read_only_preflight: bool,
) -> tuple[BuildPlan, ...]:
    if not providers:
        return ()
    home = Path(os.path.abspath(os.fspath(manager_home)))
    backend = (
        cache.cache_for_manager_home(home)
        if cache_backend is None
        else cache_backend
    )
    establish = (
        toolchain.establish_toolchain
        if establish_toolchain is None
        else establish_toolchain
    )
    forbidden = tuple(
        path
        for path in _unique_paths(
            (
                home,
                *(provider.snapshot.path for provider in providers),
                *forbidden_roots,
            )
        )
        if path.exists()
    )
    if read_only_preflight:
        toolchain.preflight_toolchain(
            toolchain.ToolchainConfig(
                private_base=home,
                operator_search_path=operator_search_path,
                forbidden_roots=forbidden,
            )
        )
    plans: list[BuildPlan] = []
    with tempfile.TemporaryDirectory(prefix="csk-build-plan-") as private:
        config = toolchain.ToolchainConfig(
            private_base=Path(private),
            operator_search_path=operator_search_path,
            forbidden_roots=forbidden,
        )
        with establish(config) as session:
            for provider in providers:
                def inspect_current(
                    _snapshot: source.FrozenSnapshot,
                ) -> tuple[BuildPlan, ...]:
                    return _inspect_provider(
                        provider,
                        target=session.target,
                        identity=session.toolchain,
                        backend=backend,
                    )

                plans.extend(
                    provider.snapshot.use(inspect_current)
                )
    return tuple(plans)


def _inspect_provider(
    provider: BuildProvider,
    *,
    target: toolchain.NativeTarget,
    identity: toolchain.ToolchainIdentity,
    backend: cache.BuildCacheBackend,
) -> tuple[BuildPlan, ...]:
    plans: list[BuildPlan] = []
    for command in sorted(
        provider.commands,
        key=lambda item: item.name.encode("utf-8"),
    ):
        build_input = metadata.GoBuildInput(
            build_source=provider.snapshot.identity,
            build_root=command.build_root,
            command=command.name,
            source_dir=command.source_dir,
            target=target,
            toolchain=identity,
            driver=command.driver,
        )
        key = metadata.cache_key(build_input)
        inspection = backend.inspect(cache.CacheExpectation(input=build_input))
        plans.append(
            BuildPlan(
                provider=provider.name,
                input=build_input,
                cache_key=key,
                inspection=inspection,
            )
        )
    return tuple(plans)


def _capture_generation(
    probe: GenerationProbe | None,
) -> dict[str, str] | None:
    if probe is None:
        return None
    captured = probe.capture()
    observations: dict[str, str] = {}
    for name in sorted(captured, key=lambda item: item.encode("utf-8")):
        digest = captured[name]
        if not isinstance(name, str) or not isinstance(digest, str):
            raise BuildPlanningError(
                "generation_invalid",
                "generation observations must map strings to strings",
            )
        observations[name] = digest
    return observations


def _generation_digest(path: Path) -> str:
    digest = hashlib.sha256()
    _frame(digest, b"csk-filesystem-generation-v1")
    _visit_generation_path(digest, path, b".")
    return "sha256:" + digest.hexdigest()


def _visit_generation_path(
    digest: Any,
    path: Path,
    relative: bytes,
) -> None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        _generation_record(digest, b"M", relative, ())
        return
    except OSError as exc:
        raise BuildPlanningError(
            "generation_unreadable",
            f"cannot inspect shared planning state {path}: {exc}",
        ) from exc

    metadata_fields = _stat_fields(before)
    if stat.S_ISLNK(before.st_mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise BuildPlanningError(
                "generation_unreadable",
                f"cannot read shared-state link {path}: {exc}",
            ) from exc
        _generation_record(
            digest,
            b"L",
            relative,
            (*metadata_fields, os.fsencode(target)),
        )
        _require_stable_lstat(path, before)
        return
    if stat.S_ISDIR(before.st_mode):
        _generation_record(digest, b"D", relative, metadata_fields)
        for name in _directory_names_noatime(path):
            encoded = os.fsencode(name)
            child_relative = (
                encoded
                if relative == b"."
                else relative + b"/" + encoded
            )
            _visit_generation_path(
                digest,
                path / name,
                child_relative,
            )
        _require_stable_lstat(path, before)
        return
    if stat.S_ISREG(before.st_mode):
        content = _hash_regular_file_noatime(path, before)
        _require_stable_lstat(path, before)
        _generation_record(
            digest,
            b"F",
            relative,
            (*metadata_fields, content),
        )
        return
    _generation_record(digest, b"S", relative, metadata_fields)
    _require_stable_lstat(path, before)


def _directory_names_noatime(path: Path) -> list[str]:
    descriptor = -1
    try:
        if os.name == "posix" and os.listdir in os.supports_fd:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOATIME", 0)
            )
            descriptor = _open_with_noatime_fallback(path, flags)
            return sorted(
                os.listdir(descriptor),
                key=lambda value: os.fsencode(value),
            )
        with os.scandir(path) as entries:
            return sorted(
                (entry.name for entry in entries),
                key=lambda value: os.fsencode(value),
            )
    except OSError as exc:
        raise BuildPlanningError(
            "generation_unreadable",
            f"cannot list shared planning state {path}: {exc}",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _hash_regular_file_noatime(
    path: Path,
    expected: os.stat_result,
) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOATIME", 0)
    )
    descriptor = -1
    try:
        descriptor = _open_with_noatime_fallback(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _stable_open_stat(expected, opened)
        ):
            raise BuildPlanningError(
                "concurrent_state_change",
                f"shared planning file changed while opening: {path}",
            )
        digest = hashlib.sha256()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise BuildPlanningError(
                    "concurrent_state_change",
                    f"shared planning file shrank while reading: {path}",
                )
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise BuildPlanningError(
                "concurrent_state_change",
                f"shared planning file grew while reading: {path}",
            )
        after = os.fstat(descriptor)
        if not _stable_stat(opened, after):
            raise BuildPlanningError(
                "concurrent_state_change",
                f"shared planning file changed while reading: {path}",
            )
        return digest.digest()
    except BuildPlanningError:
        raise
    except OSError as exc:
        raise BuildPlanningError(
            "generation_unreadable",
            f"cannot read shared planning state {path}: {exc}",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_with_noatime_fallback(path: Path, flags: int) -> int:
    noatime = getattr(os, "O_NOATIME", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        if not noatime or not flags & noatime:
            raise
        if exc.errno not in {
            errno.EPERM,
            errno.EACCES,
            errno.EINVAL,
            errno.ENOTSUP,
        }:
            raise
        return os.open(path, flags & ~noatime)


def _require_stable_lstat(path: Path, expected: os.stat_result) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise BuildPlanningError(
            "concurrent_state_change",
            f"shared planning state changed while reading: {path}",
        ) from exc
    if not _stable_stat(expected, current):
        raise BuildPlanningError(
            "concurrent_state_change",
            f"shared planning state changed while reading: {path}",
        )


def _stable_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return _stat_fields(left) == _stat_fields(right)


def _stable_open_stat(
    path_stat: os.stat_result,
    descriptor_stat: os.stat_result,
) -> bool:
    """Compare path and descriptor views without platform-specific ctime."""

    # Windows Python 3.12+ retains creation time in a pathname stat's
    # st_ctime while descriptor stat exposes metadata change time.  Identity
    # and content fields are comparable across the two APIs; full metadata is
    # still checked within each API before/after the read.
    return (
        os.path.samestat(path_stat, descriptor_stat)
        and stat.S_IFMT(path_stat.st_mode) == stat.S_IFMT(descriptor_stat.st_mode)
        and path_stat.st_size == descriptor_stat.st_size
        and path_stat.st_mtime_ns == descriptor_stat.st_mtime_ns
    )


def _stat_fields(value: os.stat_result) -> tuple[bytes, ...]:
    return tuple(
        str(item).encode("ascii")
        for item in (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            getattr(value, "st_uid", 0),
            getattr(value, "st_gid", 0),
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
    )


def _generation_record(
    digest: Any,
    kind: bytes,
    relative: bytes,
    fields: Sequence[bytes],
) -> None:
    _frame(digest, kind)
    _frame(digest, relative)
    for field in fields:
        _frame(digest, field)


def _frame(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _portable_path_contains(root: str, path: str) -> bool:
    root_parts = root.split("/")
    path_parts = path.split("/")
    return (
        len(path_parts) >= len(root_parts)
        and path_parts[: len(root_parts)] == root_parts
    )


def _unique_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    by_name: dict[str, Path] = {}
    for value in paths:
        path = Path(os.path.abspath(os.fspath(value)))
        by_name[str(path)] = path
    return tuple(
        by_name[name]
        for name in sorted(by_name, key=lambda item: item.encode("utf-8"))
    )


__all__ = [
    "BuildCommand",
    "BuildPlan",
    "BuildPlanningError",
    "BuildProvider",
    "FilesystemGenerationProbe",
    "GenerationProbe",
    "detect_command_collisions",
    "plan_builds",
    "provider_from_spec",
]
