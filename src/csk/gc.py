from __future__ import annotations

import math
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import consumers, install_marker, locking, protocol_json, transactions
from .build_repository_pipeline import EffectiveState, DiskProtectedStore, snapshot_key
from .builds import cache as build_cache
from .config import GlobalConfig
from .locking import _pid_alive


# Interrupted installs leave exact PID-owned temporary generations behind.
# Current runtime staging adds a numeric allocation index; legacy project and
# global staging does not.  Match only those two closed shapes so unrelated
# dotfiles are never treated as manager-owned garbage.
_ORPHAN_RE = re.compile(
    r"^\..+\.(?:(?:tmp|backup)-([1-9]\d*)(?:-(?:0|[1-9]\d*))?"
    r"|stale-([1-9]\d*)-(?:0|[1-9]\d*))$"
)
BUILD_GRACE_SECONDS = 24 * 60 * 60


class _GcLockWitness(
    build_cache.CacheMutationGuard,
    transactions.HomeLockWitness,
    Protocol,
):
    pass


@dataclass
class GcStats:
    runtime_removed: int = 0
    snapshots_removed: int = 0
    builds_removed: int = 0
    external_builds_removed: int = 0
    external_snapshots_removed: int = 0
    consumers_pruned: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class _References:
    runtime: set[tuple[str, str]] = field(default_factory=set)
    snapshots: set[tuple[str, str]] = field(default_factory=set)
    builds: set[str] = field(default_factory=set)
    external_builds: set[str] = field(default_factory=set)
    external_snapshots: set[str] = field(default_factory=set)


def collect_runtime(
    config: GlobalConfig,
    csk_home: Path,
    *,
    guard: _GcLockWitness | None = None,
    now: float | None = None,
    build_grace_seconds: float = BUILD_GRACE_SECONDS,
) -> GcStats:
    """Collect manager state under exactly one manager-home mutation lock.

    Existing callers that do not already hold the lock acquire it here.  The
    installer passes its witness explicitly, avoiding lock recursion while
    making the build-cache mutation authority visible to the backend.
    """

    if guard is None:
        with locking.ManagerHomeLock(csk_home) as acquired:
            return _collect_locked(
                config,
                csk_home,
                guard=acquired,
                now=now,
                build_grace_seconds=build_grace_seconds,
            )
    guard.assert_held()
    return _collect_locked(
        config,
        csk_home,
        guard=guard,
        now=now,
        build_grace_seconds=build_grace_seconds,
    )


def _collect_locked(
    config: GlobalConfig,
    csk_home: Path,
    *,
    guard: _GcLockWitness,
    now: float | None,
    build_grace_seconds: float,
) -> GcStats:
    guard.assert_held()
    timestamp = time.time() if now is None else now
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
    ):
        raise ValueError("GC time must be a finite timestamp")
    if (
        isinstance(build_grace_seconds, bool)
        or not isinstance(build_grace_seconds, (int, float))
        or not math.isfinite(build_grace_seconds)
        or build_grace_seconds < 0
    ):
        raise ValueError("build GC grace must be a finite non-negative duration")

    stats = GcStats()
    references = _References()
    uncertain = False
    roots = [
        csk_home / "global" / "skills",
        csk_home / "hybrid" / "skills",
        *(project.path / ".agents" / "skills" for project in config.projects.values()),
    ]
    for root in roots:
        found, warnings = _collect_marker_root(root, references)
        del found
        if warnings:
            uncertain = True
            stats.warnings.extend(warnings)
        sweep_orphans(root)

    registry_valid = True
    try:
        known = _load_consumers_strict(csk_home)
    except ValueError as exc:
        registry_valid = False
        uncertain = True
        known = []
        stats.warnings.append(
            f"consumer registry is uncertain; retaining referenced state: {exc}"
        )

    alive: list[Path] = []
    for consumer in known:
        try:
            consumer_info = consumer.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            uncertain = True
            alive.append(consumer)
            stats.warnings.append(
                f"consumer {consumer} is uncertain; retaining it: {exc}"
            )
            continue
        if stat.S_ISLNK(consumer_info.st_mode) or not stat.S_ISDIR(
            consumer_info.st_mode
        ):
            uncertain = True
            alive.append(consumer)
            stats.warnings.append(
                f"consumer {consumer} is not a real directory; retaining it"
            )
            continue
        root = consumer / ".agents" / "skills"
        found, warnings = _collect_marker_root(root, references)
        if warnings:
            uncertain = True
            alive.append(consumer)
            stats.warnings.extend(warnings)
        elif found:
            alive.append(consumer)
            sweep_orphans(root)

    try:
        journal_groups = transactions.TransactionEngine(
            csk_home
        ).referenced_install_marker_groups(guard)
        for group in journal_groups:
            warnings = _collect_journal_marker_group(group, references)
            if warnings:
                uncertain = True
                stats.warnings.extend(warnings)
    except (transactions.TransactionError, OSError) as exc:
        uncertain = True
        stats.warnings.append(
            f"transaction journals are uncertain; retaining referenced state: {exc}"
        )

    if uncertain:
        stats.warnings.append(
            "GC retained runtime, snapshot, and build entries because the mark phase was incomplete"
        )
        return stats

    if registry_valid:
        stats.consumers_pruned = len(known) - len(alive)
        consumers.replace_consumers(csk_home, alive)

    stats.snapshots_removed = _collect_snapshots(
        csk_home,
        references.snapshots,
    )
    runtime_removed, runtime_warnings = _collect_runtime_entries(
        csk_home,
        references.runtime,
    )
    stats.runtime_removed = runtime_removed
    stats.warnings.extend(runtime_warnings)

    try:
        collected = build_cache.cache_for_manager_home(csk_home).collect(
            references.builds,
            older_than=float(timestamp - build_grace_seconds),
            guard=guard,
        )
    except (build_cache.BuildCacheError, AttributeError) as exc:
        stats.warnings.append(
            f"build cache retained because collection could not prove its boundary: {exc}"
        )
    else:
        stats.builds_removed = collected.removed
        stats.warnings.extend(collected.warnings)
    try:
        (
            stats.external_builds_removed,
            stats.external_snapshots_removed,
        ) = _collect_external_cache(csk_home, references)
    except (OSError, ValueError) as exc:
        stats.warnings.append(
            f"external build cache retained because collection could not prove its boundary: {exc}"
        )
    return stats


def _load_consumers_strict(csk_home: Path) -> list[Path]:
    path = consumers.registry_path(csk_home)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ValueError(f"cannot inspect {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{path} is not a regular non-link file")
    try:
        value = protocol_json.loads(path.read_bytes())
    except (OSError, protocol_json.ProtocolJSONError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "consumers",
    }:
        raise ValueError(f"{path} has an invalid object shape")
    if value.get("schema_version") != consumers.SCHEMA_VERSION:
        raise ValueError(f"{path} has an unsupported schema_version")
    raw = value.get("consumers")
    if (
        not isinstance(raw, list)
        or not all(isinstance(item, str) and item for item in raw)
        or raw != sorted(set(raw))
    ):
        raise ValueError(f"{path} has an invalid consumers list")
    result: list[Path] = []
    for item in raw:
        candidate = Path(item)
        if not candidate.is_absolute() or Path(os.path.abspath(candidate)) != candidate:
            raise ValueError(f"{path} contains a non-canonical consumer path")
        result.append(candidate)
    return result


def _collect_marker_root(
    skills_root: Path,
    references: _References,
) -> tuple[bool, list[str]]:
    try:
        before = skills_root.lstat()
    except FileNotFoundError:
        return False, []
    except OSError as exc:
        return False, [f"cannot inspect skill store {skills_root}: {exc}"]
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        return False, [f"skill store is not a real directory: {skills_root}"]
    try:
        children = sorted(
            skills_root.iterdir(),
            key=lambda item: item.name.encode("utf-8"),
        )
    except OSError as exc:
        return False, [f"cannot list skill store {skills_root}: {exc}"]
    found = False
    warnings: list[str] = []
    for child in children:
        if _ORPHAN_RE.match(child.name):
            continue
        try:
            info = child.lstat()
        except OSError as exc:
            warnings.append(f"cannot inspect skill entry {child}: {exc}")
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            warnings.append(f"skill entry is not a real directory: {child}")
            continue
        marker_found, warning = _collect_marker_directory(
            child,
            references,
            expected_name=child.name,
        )
        if warning is None:
            found = found or marker_found
        else:
            warnings.append(f"uncertain install marker at {child}: {warning}")
    try:
        after = skills_root.lstat()
    except OSError as exc:
        warnings.append(f"skill store changed while scanning {skills_root}: {exc}")
    else:
        if _path_state(before) != _path_state(after):
            warnings.append(f"skill store changed while scanning: {skills_root}")
    return found, warnings


def _collect_marker_directory(
    directory: Path,
    references: _References,
    *,
    expected_name: str | None = None,
) -> tuple[bool, str | None]:
    try:
        directory_info = directory.lstat()
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        return False, str(exc)
    if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(
        directory_info.st_mode
    ):
        return False, "marker parent is not a real directory"
    marker_path = directory / ".csk-install.json"
    try:
        before = marker_path.lstat()
    except FileNotFoundError:
        return False, "marker is missing"
    except OSError as exc:
        return False, str(exc)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        return False, "marker is not a regular non-link file"
    try:
        raw = marker_path.read_bytes()
        after = marker_path.lstat()
        marker = install_marker.read_install_marker(raw)
    except (OSError, install_marker.InstallMarkerError) as exc:
        return False, str(exc)
    if _path_state(before) != _path_state(after):
        return False, "marker changed while it was read"
    if expected_name is not None and marker.name != expected_name:
        return False, f"marker name {marker.name!r} does not match store entry"
    references.runtime.add((marker.name, marker.commit))
    references.snapshots.add((marker.source, marker.commit))
    if isinstance(marker, install_marker.InstallMarkerV2):
        references.builds.update(build.cache_key for build in marker.builds.values())
    elif isinstance(marker, install_marker.InstallMarkerV3):
        for build in marker.builds.values():
            if build.driver == "go-v1":
                references.builds.add(build.cache_key)
                continue
            references.external_builds.add(build.cache_key)
            assert build.effective_identity is not None
            assert build.object_format is not None and build.commit is not None
            assert build.build_source is not None
            references.external_snapshots.add(
                snapshot_key(
                    EffectiveState(
                        identity_kind=build.effective_identity.kind,
                        identity=build.effective_identity.value,
                        transport=None,
                        object_format=build.object_format,
                        commit=build.commit,
                        substituted=bool(build.substituted),
                    ),
                    build.build_source.content_sha256,
                )
            )
    return True, None


def _collect_journal_marker_group(
    group: transactions.InstallMarkerGenerationGroup,
    references: _References,
) -> list[str]:
    """Mark every safe generation and require one valid marker per target."""

    before, state_warnings = _generation_states(group.paths)
    candidate_references = _References()
    found = False
    warnings = list(state_warnings)
    for path in group.paths:
        marker_found, warning = _collect_marker_directory(
            path,
            candidate_references,
        )
        found = found or marker_found
        if warning is not None:
            warnings.append(f"{path}: {warning}")
    after, after_warnings = _generation_states(group.paths)
    warnings.extend(after_warnings)
    if before != after:
        warnings.append("generation paths changed while they were scanned")
    if not found:
        warnings.append("no valid install marker generation remains")
    if warnings:
        prefix = (
            f"journal {group.transaction_id} context target "
            f"{group.target_identifier!r} is uncertain"
        )
        return [f"{prefix}: {warning}" for warning in warnings]
    references.runtime.update(candidate_references.runtime)
    references.snapshots.update(candidate_references.snapshots)
    references.builds.update(candidate_references.builds)
    references.external_builds.update(candidate_references.external_builds)
    references.external_snapshots.update(candidate_references.external_snapshots)
    return []


def _collect_external_cache(csk_home: Path, references: _References) -> tuple[int, int]:
    root = csk_home / "external-builds"
    if not root.exists():
        return 0, 0
    # Reuse the store's boundary proof before deleting anything. GC does not
    # repair permissions or infer liveness from receipt contents.
    DiskProtectedStore(root)._prepare(mutate=False)
    removed: list[int] = []
    for kind, live in (
        ("artifacts", references.external_builds),
        ("snapshots", references.external_snapshots),
    ):
        parent = root / kind
        if not parent.exists():
            removed.append(0)
            continue
        DiskProtectedStore(root)._protected_dir(parent, create=False)
        count = 0
        for entry in parent.iterdir():
            key = "sha256:" + entry.name
            if key in live:
                continue
            DiskProtectedStore(root)._protected_dir(entry, create=False)
            entry.chmod(0o700)
            shutil.rmtree(entry)
            count += 1
        removed.append(count)
    return removed[0], removed[1]


def _generation_states(
    paths: tuple[Path, ...],
) -> tuple[dict[Path, tuple[int, int, int, int, int, int] | None], list[str]]:
    states: dict[Path, tuple[int, int, int, int, int, int] | None] = {}
    warnings: list[str] = []
    for path in paths:
        try:
            states[path] = _path_state(path.lstat())
        except FileNotFoundError:
            states[path] = None
        except OSError as exc:
            warnings.append(f"cannot inspect generation {path}: {exc}")
    return states, warnings


def _collect_snapshots(
    csk_home: Path,
    referenced: set[tuple[str, str]],
) -> int:
    cache_root = csk_home / "cache"
    if not cache_root.exists() or cache_root.is_symlink():
        return 0
    removed = 0
    # Layout: cache/<source>/<commit>/snapshot, where <source> may be nested.
    for snapshot_dir in sorted(cache_root.rglob("snapshot")):
        if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
            continue
        commit_dir = snapshot_dir.parent
        source = commit_dir.parent.relative_to(cache_root).as_posix()
        if (source, commit_dir.name) in referenced:
            continue
        shutil.rmtree(commit_dir)
        removed += 1
        parent = commit_dir.parent
        while parent != cache_root and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return removed


def _collect_runtime_entries(
    csk_home: Path,
    referenced: set[tuple[str, str]],
) -> tuple[int, list[str]]:
    runtime_root = csk_home / "runtime"
    try:
        root_info = runtime_root.lstat()
    except FileNotFoundError:
        return 0, []
    except OSError as exc:
        return 0, [f"runtime store retained: {exc}"]
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return 0, [f"runtime store retained because it is not a real directory: {runtime_root}"]
    removed = 0
    warnings: list[str] = []
    for skill_dir in runtime_root.iterdir():
        try:
            skill_info = skill_dir.lstat()
        except OSError as exc:
            warnings.append(f"runtime entry retained at {skill_dir}: {exc}")
            continue
        if stat.S_ISLNK(skill_info.st_mode) or not stat.S_ISDIR(skill_info.st_mode):
            warnings.append(f"runtime entry retained because it is not a real directory: {skill_dir}")
            continue
        sweep_orphans(skill_dir)
        for commit_dir in skill_dir.iterdir():
            if _ORPHAN_RE.match(commit_dir.name):
                continue
            try:
                commit_info = commit_dir.lstat()
            except OSError as exc:
                warnings.append(f"runtime generation retained at {commit_dir}: {exc}")
                continue
            if stat.S_ISLNK(commit_info.st_mode) or not stat.S_ISDIR(
                commit_info.st_mode
            ):
                warnings.append(
                    f"runtime generation retained because it is not a real directory: {commit_dir}"
                )
                continue
            if (skill_dir.name, commit_dir.name) not in referenced:
                shutil.rmtree(commit_dir)
                removed += 1
    return removed, warnings


def sweep_orphans(directory: Path) -> None:
    try:
        info = directory.lstat()
    except OSError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return
    try:
        children = list(directory.iterdir())
    except OSError:
        return
    for child in children:
        match = _ORPHAN_RE.match(child.name)
        if not match:
            continue
        pid = match.group(1) or match.group(2)
        assert pid is not None
        if _pid_alive(int(pid)):
            continue
        try:
            child_info = child.lstat()
        except OSError:
            continue
        if stat.S_ISDIR(child_info.st_mode) and not stat.S_ISLNK(
            child_info.st_mode
        ):
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


def _path_state(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
