"""Physical boundary enforcement for local path sources.

Implements protocol skillfile-sources section 2: before any traversal or
copy, selected packages and effective inputs are resolved to physical
identity (symlinks plus the filesystem's actual case-equivalence rules);
managed outputs, ``.git`` metadata and snapshot/staging trees are pruned
deterministically; a project-root package requires the operator's
``root_inputs`` allowlist; and destination separation is rechecked
immediately before each publication write.

Every boundary decision in this module is a Phase A decision: :func:`freeze
boundaries` answers every question that requires looking outside the
source root (the source root identity, the csk home identity, each managed
output root identity, the root's ancestry) and freezes the answers in a
:class:`BoundaryRecord`. Later phases consult the frozen record and never
re-derive a boundary from a path spelling. No containment, ancestry or
pruning decision in this module compares path strings: containment is an
ancestry walk that asks the filesystem at every ancestor whether it IS the
root, and case-equivalence is a same-file probe against the live
filesystem, never a casefold of names.

The publication-time :func:`recheck_publication_destination` is exported
for the serialized publication transaction (TASK-260916-17x3o1), which
calls it per write with the frozen record in hand.
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from .. import adapters, identifiers
from .errors import (
    CODE_ALIAS_UNKNOWN,
    CODE_MEMBER_INVALID,
    CODE_MEMBER_MISSING,
    CODE_OUTPUT_OVERLAP,
    CODE_PATH_CONFLICT,
    CODE_SELECTION_INVALID,
    SourceError,
    SourcePathConflictError,
)
from .skillfile_v2 import is_valid_selector_directory

if TYPE_CHECKING:
    from .repository_policy import RepositoryPolicy

__all__ = [
    "AdmittedInput",
    "AdmittedMember",
    "BoundaryRecord",
    "ManagedOutputMember",
    "ManagedRoot",
    "SKILL_MD_NAME",
    "check_declared_inputs",
    "check_selected_package",
    "freeze_boundaries",
    "managed_output_first_components",
    "managed_output_table",
    "prune_discovery_candidates",
    "recheck_publication_destination",
    "validate_root_inputs",
]


SKILL_MD_NAME: Final = "SKILL.md"

_GIT_NAME: Final = ".git"

_FS_ERRORS: Final[tuple[type[Exception], ...]] = (
    OSError,
    RuntimeError,
    ValueError,
    UnicodeError,
)

# Cap on symlink expansions during one physical resolution. A visited set of
# ((device, inode), remaining components) refuses true loops; the cap bounds
# adversarial link chains that never repeat a state.
_MAX_LINK_EXPANSIONS: Final = 40

_MISSING_ERRNOS: Final = frozenset({errno.ENOENT, errno.ENOTDIR})


def managed_output_first_components() -> frozenset[str]:
    """Return the managed-output first components csk itself writes.

    The single source is ``csk.adapters`` (every adapter root first
    component plus the native-discovery ``.agents`` root) with ``.git``
    metadata added; this module keeps no second copy of the adapter list.
    The source snapshot store, transaction staging, runtime stores and
    build caches live under the csk home, so they are covered by the
    csk-home containment rule rather than by extra names here.
    """

    names = {_GIT_NAME}
    for relative in (
        *adapters.AGENT_PATHS.values(),
        adapters.NATIVE_DISCOVERY_HOME_PATH,
    ):
        first = relative.split("/", 1)[0]
        if first:
            names.add(first)
    return frozenset(names)


@dataclass(frozen=True)
class ManagedOutputMember:
    """One row of the closed managed-output table (no filesystem access)."""

    key: str
    display: str
    kind: str
    origin: str


def managed_output_table(source_root: Path, csk_home: Path) -> tuple[ManagedOutputMember, ...]:
    """Return the closed managed-output set as named rows.

    Pure function of the two root spellings: it performs no filesystem
    access and its rows are the contract section 2 enumerates, each with
    the existing csk constant or resolver it came from.
    """

    root_text = os.fspath(source_root)
    home_text = os.fspath(csk_home)
    members: list[ManagedOutputMember] = []
    for name in sorted(managed_output_first_components(), key=lambda value: value.encode("utf-8")):
        if name == _GIT_NAME:
            origin = "protocol skillfile-sources section 2 (.git metadata pruning)"
        elif name == adapters.NATIVE_DISCOVERY_HOME_PATH.split("/", 1)[0]:
            origin = (
                "csk.adapters.NATIVE_DISCOVERY_HOME_PATH "
                "(.agents/skills canonical installs, .agents/bin command "
                "links, .agents/env.* via csk.env_files, install markers "
                "via csk.install_marker)"
            )
        else:
            owners = sorted(
                agent
                for agent, relative in adapters.AGENT_PATHS.items()
                if relative.split("/", 1)[0] == name
            )
            origin = (
                "csk.adapters.AGENT_PATHS["
                + ",".join(owners)
                + "] ("
                + ", ".join(adapters.AGENT_PATHS[agent] for agent in owners)
                + ")"
            )
        members.append(
            ManagedOutputMember(
                key=name,
                display=f"{root_text}/{name}",
                kind="source-root first component",
                origin=origin,
            )
        )
    members.append(
        ManagedOutputMember(
            key="<csk-home>",
            display=home_text,
            kind="csk-home containment",
            origin=(
                "csk.config.DEFAULT_CONFIG_PATH parent (source snapshot "
                "store source-v1 namespace per epic decision 4, "
                "csk.transactions staging trees, "
                "csk.global_install.global_root/global_skills_root/global_bin_dir "
                "manager stores, csk.builds.cache_* build caches, "
                "csk.snapshot.snapshot_dir legacy cache layout)"
            ),
        )
    )
    return tuple(members)


@dataclass(frozen=True)
class ManagedRoot:
    """One frozen managed-output root at the source root (Phase A)."""

    name: str
    identity: tuple[int, int] | None
    entry_identity: tuple[int, int] | None
    target: str | None = None


@dataclass(frozen=True)
class BoundaryRecord:
    """Frozen Phase-A boundary answers for one source root and csk home."""

    source_root: str
    source_identity: tuple[int, int]
    home_root: str | None
    home_identity: tuple[int, int] | None
    home_contains_source: bool
    source_inside_managed: str | None
    managed_roots: tuple[ManagedRoot, ...]

    def managed_identities(self) -> dict[tuple[int, int], str]:
        """Map each present managed-root identity to its root name."""

        result: dict[tuple[int, int], str] = {}
        for root in self.managed_roots:
            if root.identity is not None and root.identity not in result:
                result[root.identity] = root.name
        return result


@dataclass(frozen=True)
class AdmittedMember:
    """One admitted file or directory below a root_inputs entry."""

    path: str
    is_dir: bool
    identity: tuple[int, int]


@dataclass(frozen=True)
class AdmittedInput:
    """One validated root_inputs entry with its recursive members."""

    path: str
    resolved: str
    is_dir: bool
    identity: tuple[int, int]
    members: tuple[AdmittedMember, ...]


@dataclass(frozen=True)
class _PhysicalPath:
    """One component-wise physical resolution result."""

    path: str
    identity: tuple[int, int] | None
    exists: bool
    is_dir: bool
    final_stat: os.stat_result | None = None


def _identity_from_stat(value: os.stat_result) -> tuple[int, int]:
    return (value.st_dev, value.st_ino)


def _refusal(code: str, context: str, cause: Exception) -> SourceError:
    return SourceError(code, f"{context}: {cause}")


def _undetermined(code: str, context: str, cause: Exception) -> SourceError:
    return SourceError(code, f"{context}: boundary undetermined: {cause}")


def _lstat(path: str, *, code: str, context: str) -> os.stat_result | None:
    """lstat one path, returning None only for legitimate absence."""

    try:
        return os.lstat(path)
    except OSError as exc:
        if exc.errno in _MISSING_ERRNOS:
            return None
        raise _undetermined(code, context, exc) from exc
    except _FS_ERRORS as exc:
        raise _undetermined(code, context, exc) from exc


def _physical_path(path: str, *, code: str, context: str) -> _PhysicalPath:
    """Resolve one path component by component without string collapsing.

    Every component is inspected with ``lstat``; symlinks expand with a
    visited set plus an expansion cap, so loops refuse with the caller's
    code instead of leaking ``RuntimeError``. The first missing component
    ends resolution with the tail appended literally, so absent managed
    roots and fresh homes stay representable. No ``..`` is collapsed
    lexically: ``..`` inside a link target pops one resolved component,
    which is the physical parent by construction.
    """

    try:
        absolute = os.path.abspath(path)
    except _FS_ERRORS as exc:
        raise _undetermined(code, context, exc) from exc
    drive, pending = _split_absolute(absolute)
    # ``resolved`` is the physical stack: every element was inspected with
    # lstat and every link already expanded. ``pending`` holds the consumed
    # prefix plus the unscanned tail; ``index`` points at the next part.
    resolved: list[str] = []
    visited: set[tuple[tuple[int, int], tuple[str, ...]]] = set()
    expansions = 0
    index = 0
    while index < len(pending):
        part = pending[index]
        if part == "..":
            if resolved:
                resolved.pop()
            index += 1
            continue
        candidate = _join_absolute(drive, [*resolved, part])
        entry = _lstat(candidate, code=code, context=f"{context} at {part!r}")
        if entry is None:
            tail = os.sep.join(pending[index + 1 :])
            literal = candidate + os.sep + tail if tail else candidate
            return _PhysicalPath(path=literal, identity=None, exists=False, is_dir=False)
        if not stat.S_ISLNK(entry.st_mode):
            resolved.append(part)
            index += 1
            continue
        key = (_identity_from_stat(entry), tuple(pending[index + 1 :]))
        if key in visited:
            raise SourceError(code, f"{context}: symlink loop at {candidate!r}")
        visited.add(key)
        expansions += 1
        if expansions > _MAX_LINK_EXPANSIONS:
            raise SourceError(code, f"{context}: too many symlink expansions at {candidate!r}")
        try:
            target = os.readlink(candidate)
        except OSError as exc:
            if exc.errno in _MISSING_ERRNOS:
                return _PhysicalPath(path=candidate, identity=None, exists=False, is_dir=False)
            raise _undetermined(code, f"{context} at {part!r}", exc) from exc
        except _FS_ERRORS as exc:
            raise _undetermined(code, f"{context} at {part!r}", exc) from exc
        if os.path.isabs(target):
            drive, target_parts = _split_absolute(target)
            resolved = []
            pending = [*target_parts, *pending[index + 1 :]]
            index = 0
        else:
            target_parts = _split_relative_target(target)
            pending = [*pending[:index], *target_parts, *pending[index + 1 :]]
    full_path = _join_absolute(drive, resolved)
    final = _lstat(full_path, code=code, context=context)
    if final is None:
        return _PhysicalPath(path=full_path, identity=None, exists=False, is_dir=False)
    return _PhysicalPath(
        path=full_path,
        identity=_identity_from_stat(final),
        exists=True,
        is_dir=stat.S_ISDIR(final.st_mode),
        final_stat=final,
    )


def _split_absolute(path: str) -> tuple[str, list[str]]:
    """Split an absolute path into its drive and significant components."""

    drive, rest = os.path.splitdrive(path)
    parts = [part for part in rest.split(os.sep) if part not in ("", ".")]
    return (drive, parts)


def _split_relative_target(target: str) -> list[str]:
    """Split a relative link target on this platform's separators.

    A backslash is a separator only on Windows; on POSIX it is an
    ordinary filename character and is never rewritten.
    """

    if os.sep == "\\":
        target = target.replace("/", os.sep)
    return [part for part in target.split(os.sep) if part not in ("", ".")]


def _join_absolute(drive: str, parts: list[str]) -> str:
    if not parts:
        return drive + os.sep if drive else os.sep
    return drive + os.sep + os.sep.join(parts) if drive else os.sep + os.sep.join(parts)


def _identities_equal(
    first: tuple[int, int] | None,
    second: tuple[int, int] | None,
) -> bool:
    """Compare two identities where inode numbers are meaningful."""

    if first is None or second is None:
        return False
    if first[1] == 0 or second[1] == 0:
        return False
    return first == second


def _stats_same_file(
    first_path: str,
    first_stat: os.stat_result,
    second_path: str,
    second_stat: os.stat_result,
    *,
    code: str,
    context: str,
) -> bool:
    """Return whether two statted paths name one filesystem object.

    Identity comparison is by ``(st_dev, st_ino)``; where inode numbers
    are not meaningful the decision falls back to ``os.path.samefile``.
    Every failure refuses with the caller's code, never a guess.
    """

    first_identity = _identity_from_stat(first_stat)
    second_identity = _identity_from_stat(second_stat)
    if first_identity[1] != 0 and second_identity[1] != 0:
        return first_identity == second_identity
    try:
        return os.path.samefile(first_path, second_path)
    except OSError as exc:
        if exc.errno in _MISSING_ERRNOS:
            raise SourceError(
                code, f"{context}: {first_path!r} disappeared during inspection"
            ) from exc
        raise _undetermined(code, context, exc) from exc
    except _FS_ERRORS as exc:
        raise _undetermined(code, context, exc) from exc


def _ancestry_stats(
    resolved_path: str,
    *,
    code: str,
    context: str,
    stop_at: tuple[int, int] | None = None,
) -> list[tuple[str, os.stat_result]]:
    """Walk the physical ancestry asking the filesystem at every level.

    Each ancestor is re-statted fresh; a disappeared ancestor or an
    ancestor that became a link is a concurrent change and refuses. The
    walk stops after the ancestor matching ``stop_at`` (when given) so a
    containment check never looks above the root it proves.
    """

    drive, parts = _split_absolute(resolved_path)
    ancestry: list[tuple[str, os.stat_result]] = []
    for depth in range(len(parts), -1, -1):
        ancestor = _join_absolute(drive, parts[:depth])
        try:
            entry = os.lstat(ancestor)
        except OSError as exc:
            if exc.errno in _MISSING_ERRNOS:
                raise SourceError(
                    code, f"{context}: ancestor {ancestor!r} disappeared during inspection"
                ) from exc
            raise _undetermined(code, f"{context} at {ancestor!r}", exc) from exc
        except _FS_ERRORS as exc:
            raise _undetermined(code, f"{context} at {ancestor!r}", exc) from exc
        if stat.S_ISLNK(entry.st_mode):
            raise SourceError(
                code, f"{context}: ancestor {ancestor!r} became a link during inspection"
            )
        ancestry.append((ancestor, entry))
        if stop_at is not None and _identities_equal(
            _identity_from_stat(entry), stop_at
        ):
            break
    return ancestry


def _is_within(
    resolved_path: str,
    *,
    root_path: str,
    root_identity: tuple[int, int],
    code: str,
    context: str,
) -> bool:
    """Return whether one resolved path is at or below one root.

    The ancestry walk asks the filesystem at every ancestor whether it
    IS the root; no path string is compared. Where inode numbers are
    not meaningful the per-ancestor decision falls back to samefile.
    """

    try:
        root_stat = os.lstat(root_path)
    except OSError as exc:
        if exc.errno in _MISSING_ERRNOS:
            raise SourceError(
                code, f"{context}: root {root_path!r} disappeared during inspection"
            ) from exc
        raise _undetermined(code, context, exc) from exc
    except _FS_ERRORS as exc:
        raise _undetermined(code, context, exc) from exc
    drive, parts = _split_absolute(resolved_path)
    for depth in range(len(parts), -1, -1):
        ancestor = _join_absolute(drive, parts[:depth])
        try:
            entry = os.lstat(ancestor)
        except OSError as exc:
            if exc.errno in _MISSING_ERRNOS:
                raise SourceError(
                    code, f"{context}: ancestor {ancestor!r} disappeared during inspection"
                ) from exc
            raise _undetermined(code, f"{context} at {ancestor!r}", exc) from exc
        except _FS_ERRORS as exc:
            raise _undetermined(code, f"{context} at {ancestor!r}", exc) from exc
        if stat.S_ISLNK(entry.st_mode):
            raise SourceError(
                code, f"{context}: ancestor {ancestor!r} became a link during inspection"
            )
        if _identities_equal(_identity_from_stat(entry), root_identity):
            return True
        if _stats_same_file(
            ancestor, entry, root_path, root_stat, code=code, context=context
        ):
            return True
    return False


def _managed_match_at_parent(
    parent_path: str,
    candidate_path: str,
    candidate_stat: os.stat_result,
    managed_names: tuple[str, ...],
    *,
    code: str,
    context: str,
) -> str | None:
    """Probe whether one directory IS a managed root by filesystem rules.

    The canonical managed spelling is statted under the same parent and
    compared by identity, so a case-variant alias on a case-insensitive
    filesystem matches while a near-miss name never does. No name is ever
    compared as a string.
    """

    for name in managed_names:
        if parent_path.endswith(os.sep):
            spelling = parent_path + name
        else:
            spelling = parent_path + os.sep + name
        try:
            entry = os.lstat(spelling)
        except OSError as exc:
            if exc.errno in _MISSING_ERRNOS:
                continue
            raise _undetermined(code, f"{context} at {name!r}", exc) from exc
        except _FS_ERRORS as exc:
            raise _undetermined(code, f"{context} at {name!r}", exc) from exc
        if stat.S_ISLNK(entry.st_mode):
            try:
                resolved_stat = os.stat(spelling)
            except OSError as exc:
                if exc.errno in _MISSING_ERRNOS:
                    continue
                raise _undetermined(code, f"{context} at {name!r}", exc) from exc
            except _FS_ERRORS as exc:
                raise _undetermined(code, f"{context} at {name!r}", exc) from exc
            if not stat.S_ISDIR(resolved_stat.st_mode):
                continue
            if _stats_same_file(
                spelling,
                resolved_stat,
                candidate_path,
                candidate_stat,
                code=code,
                context=context,
            ):
                return name
            continue
        if not stat.S_ISDIR(entry.st_mode):
            continue
        if _stats_same_file(
            spelling, entry, candidate_path, candidate_stat, code=code, context=context
        ):
            return name
    return None


def _managed_names_sorted() -> tuple[str, ...]:
    return tuple(sorted(managed_output_first_components(), key=lambda value: value.encode("utf-8")))


def _freeze_managed_root(
    source_display: str,
    name: str,
    *,
    code: str,
    context: str,
) -> ManagedRoot:
    spelling = source_display + os.sep + name
    entry = _lstat(spelling, code=code, context=f"{context} at {name!r}")
    if entry is None:
        return ManagedRoot(name=name, identity=None, entry_identity=None)
    entry_identity = _identity_from_stat(entry)
    if stat.S_ISLNK(entry.st_mode):
        try:
            target = os.readlink(spelling)
        except OSError as exc:
            if exc.errno in _MISSING_ERRNOS:
                return ManagedRoot(name=name, identity=None, entry_identity=None)
            raise _undetermined(code, f"{context} at {name!r}", exc) from exc
        except _FS_ERRORS as exc:
            raise _undetermined(code, f"{context} at {name!r}", exc) from exc
        try:
            resolved_stat = os.stat(spelling)
        except OSError as exc:
            if exc.errno in _MISSING_ERRNOS:
                return ManagedRoot(
                    name=name, identity=None, entry_identity=entry_identity, target=target
                )
            raise _undetermined(code, f"{context} at {name!r}", exc) from exc
        except _FS_ERRORS as exc:
            raise _undetermined(code, f"{context} at {name!r}", exc) from exc
        if not stat.S_ISDIR(resolved_stat.st_mode):
            return ManagedRoot(
                name=name, identity=None, entry_identity=entry_identity, target=target
            )
        return ManagedRoot(
            name=name,
            identity=_identity_from_stat(resolved_stat),
            entry_identity=entry_identity,
            target=target,
        )
    if not stat.S_ISDIR(entry.st_mode):
        return ManagedRoot(name=name, identity=None, entry_identity=entry_identity)
    return ManagedRoot(name=name, identity=entry_identity, entry_identity=entry_identity)


def freeze_boundaries(source_root: Path, csk_home: Path) -> BoundaryRecord:
    """Freeze every outside question for one source root and csk home.

    Phase A: resolve the source root and the csk home to physical
    identity, freeze each managed-output root at the source root, and
    decide home containment plus above-root managed ancestry. All later
    boundary checks consult the returned record.
    """

    try:
        return _freeze_boundaries(os.fspath(source_root), os.fspath(csk_home))
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"Source root {source_root}: boundary undetermined: {exc}",
        ) from exc


def _freeze_boundaries(source_text: str, home_text: str) -> BoundaryRecord:
    source_context = f"Source root {source_text}"
    resolved_source = _physical_path(
        source_text, code=CODE_SELECTION_INVALID, context=source_context
    )
    if not resolved_source.exists:
        raise SourceError(CODE_SELECTION_INVALID, f"{source_context} does not exist")
    if not resolved_source.is_dir or resolved_source.identity is None:
        raise SourceError(CODE_SELECTION_INVALID, f"{source_context} is not a directory")
    source_identity = resolved_source.identity
    source_display = resolved_source.path

    home_context = f"CSK home {home_text}"
    resolved_home = _physical_path(home_text, code=CODE_OUTPUT_OVERLAP, context=home_context)
    home_root: str | None = None
    home_identity: tuple[int, int] | None = None
    if resolved_home.exists:
        if not resolved_home.is_dir or resolved_home.identity is None:
            raise SourceError(
                CODE_OUTPUT_OVERLAP, f"{home_context}: boundary undetermined: not a directory"
            )
        home_root = resolved_home.path
        home_identity = resolved_home.identity

    home_contains_source = False
    if home_identity is not None and home_root is not None:
        home_contains_source = _is_within(
            source_display,
            root_path=home_root,
            root_identity=home_identity,
            code=CODE_OUTPUT_OVERLAP,
            context=f"{source_context}: home containment",
        )

    managed_names = _managed_names_sorted()
    source_inside_managed: str | None = None
    ancestry = _ancestry_stats(
        source_display, code=CODE_OUTPUT_OVERLAP, context=f"{source_context}: ancestry"
    )
    # ancestry[0] is the source root itself; managed names are evaluated
    # at the source root below, so only strict ancestors are probed here.
    for depth, (ancestor, entry) in enumerate(ancestry):
        if depth == 0:
            continue
        parent = _join_absolute(*_parent_split(ancestor))
        match = _managed_match_at_parent(
            parent,
            ancestor,
            entry,
            managed_names,
            code=CODE_OUTPUT_OVERLAP,
            context=f"{source_context}: ancestry",
        )
        if match is not None:
            source_inside_managed = match
            break

    managed_roots = tuple(
        _freeze_managed_root(
            source_display,
            name,
            code=CODE_OUTPUT_OVERLAP,
            context=f"{source_context}: managed outputs",
        )
        for name in managed_names
    )
    return BoundaryRecord(
        source_root=source_display,
        source_identity=source_identity,
        home_root=home_root,
        home_identity=home_identity,
        home_contains_source=home_contains_source,
        source_inside_managed=source_inside_managed,
        managed_roots=managed_roots,
    )


def _parent_split(path: str) -> tuple[str, list[str]]:
    drive, parts = _split_absolute(path)
    return (drive, parts[:-1])


def _check_record_root(
    record: BoundaryRecord,
    source_text: str,
    *,
    code: str,
    context: str,
) -> _PhysicalPath:
    """Resolve the caller's source root and bind it to the frozen record."""

    resolved = _physical_path(source_text, code=code, context=context)
    if not resolved.exists:
        raise SourceError(code, f"{context} does not exist")
    if not resolved.is_dir or resolved.identity is None or resolved.final_stat is None:
        raise SourceError(code, f"{context} is not a directory")
    frozen_stat = _lstat_known(
        record.source_root,
        code=code,
        missing_code=code,
        context=f"{context}: frozen record",
    )
    if not _stats_same_file(
        resolved.path,
        resolved.final_stat,
        record.source_root,
        frozen_stat,
        code=code,
        context=context,
    ):
        raise SourceError(code, f"{context} does not match the frozen boundary record")
    return resolved


def _managed_reason(
    record: BoundaryRecord,
    resolved_path: str,
    *,
    code: str,
    context: str,
) -> str | None:
    """Return why one resolved path is managed output, or None.

    The single predicate behind explicit selection, discovery pruning,
    declared-input checks and root_inputs validation: frozen managed
    identities first, then the same-parent case-equivalence probe at
    every ancestor, then csk-home containment. The walk stops at the
    frozen source root.
    """

    if record.source_inside_managed is not None:
        return f"inside managed output {record.source_inside_managed!r}"
    if record.home_contains_source:
        return "inside the csk home"
    managed_identities = record.managed_identities()
    managed_names = _managed_names_sorted()
    ancestry = _ancestry_stats(
        resolved_path, code=code, context=context, stop_at=record.source_identity
    )
    for ancestor, entry in ancestry:
        identity = _identity_from_stat(entry)
        hit = managed_identities.get(identity)
        if hit is not None:
            return f"inside managed output {hit!r}"
        if _identities_equal(identity, record.source_identity):
            continue
        parent = _join_absolute(*_parent_split(ancestor))
        probe = _managed_match_at_parent(
            parent, ancestor, entry, managed_names, code=code, context=context
        )
        if probe is not None:
            return f"inside managed output {probe!r}"
    if (
        record.home_identity is not None
        and record.home_root is not None
        and _is_within(
            resolved_path,
            root_path=record.home_root,
            root_identity=record.home_identity,
            code=code,
            context=context,
        )
    ):
        return "inside the csk home"
    return None


def _refuse_managed_source(record: BoundaryRecord, context: str) -> None:
    """Refuse any operation whose source root is itself managed output."""

    if record.source_inside_managed is not None:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{context} is inside managed output {record.source_inside_managed!r}",
        )
    if record.home_contains_source:
        raise SourceError(CODE_OUTPUT_OVERLAP, f"{context} is inside the csk home")


def check_selected_package(
    record: BoundaryRecord,
    source_root: Path,
    directory: str,
    *,
    alias: str,
    policy: RepositoryPolicy,
    required_inputs: tuple[str, ...] = (),
) -> Path:
    """Resolve one selector directory to its physical package path.

    Escape from the source root fails ``source_selection_invalid``; a
    package within any managed output fails ``source_output_overlap``; a
    root selection without ``root_inputs`` for the alias fails
    ``source_output_overlap`` because separation cannot be proved. A root
    selection with an allowlist additionally validates its entries.
    """

    try:
        return _check_selected_package(
            record,
            os.fspath(source_root),
            directory,
            alias=alias,
            policy=policy,
            required_inputs=required_inputs,
        )
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"Selected package {directory!r}: boundary undetermined: {exc}",
        ) from exc


def _check_selected_package(
    record: BoundaryRecord,
    source_text: str,
    directory: str,
    *,
    alias: str,
    policy: RepositoryPolicy,
    required_inputs: tuple[str, ...],
) -> Path:
    context = f"Selected package {directory!r}"
    if not is_valid_selector_directory(directory):
        raise SourceError(
            CODE_SELECTION_INVALID, f"{context} is not a portable contained path"
        )
    resolved_root = _check_record_root(
        record, source_text, code=CODE_SELECTION_INVALID, context=f"Source root {source_text}"
    )
    _refuse_managed_source(record, context)
    if directory == ".":
        package_path = resolved_root.path
    else:
        joined = resolved_root.path + os.sep + directory.replace("/", os.sep)
        resolved = _physical_path(joined, code=CODE_SELECTION_INVALID, context=context)
        if not resolved.exists:
            raise SourceError(CODE_MEMBER_MISSING, f"{context} does not exist")
        if not resolved.is_dir or resolved.identity is None:
            raise SourceError(CODE_SELECTION_INVALID, f"{context} is not a directory")
        package_path = resolved.path
    if not _is_within(
        package_path,
        root_path=record.source_root,
        root_identity=record.source_identity,
        code=CODE_SELECTION_INVALID,
        context=context,
    ):
        raise SourceError(CODE_SELECTION_INVALID, f"{context} escapes the source root")
    reason = _managed_reason(
        record, package_path, code=CODE_OUTPUT_OVERLAP, context=context
    )
    if reason is not None:
        raise SourceError(CODE_OUTPUT_OVERLAP, f"{context} is {reason}")
    if directory == ".":
        entries = policy.root_inputs.get(alias)
        if not entries:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{context} selects the project root without root_inputs for {alias!r}: "
                "separation cannot be proved",
            )
        validate_root_inputs(
            record, Path(resolved_root.path), alias, policy, required_inputs
        )
    return Path(package_path)


def prune_discovery_candidates(
    record: BoundaryRecord,
    source_root: Path,
    names: tuple[str, ...],
) -> tuple[str, ...]:
    """Prune managed outputs from ``*`` discovery candidates.

    Silent, deterministic, order-preserving: a candidate that IS managed
    output (by frozen identity or the same-parent equivalence probe) is
    dropped; every other candidate is kept for downstream validation.
    Legitimate absence keeps the name so the downstream existence check
    fails precisely; an indeterminate probe refuses instead of guessing.
    """

    try:
        return _prune_discovery_candidates(record, os.fspath(source_root), names)
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"Discovery pruning below {source_root}: boundary undetermined: {exc}",
        ) from exc


def _prune_discovery_candidates(
    record: BoundaryRecord,
    source_text: str,
    names: tuple[str, ...],
) -> tuple[str, ...]:
    context = f"Discovery pruning below {source_text}"
    resolved_root = _check_record_root(
        record, source_text, code=CODE_OUTPUT_OVERLAP, context=f"Source root {source_text}"
    )
    if record.source_inside_managed is not None or record.home_contains_source:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{context}: source root is { 'inside managed output ' + repr(record.source_inside_managed) if record.source_inside_managed is not None else 'inside the csk home'}",
        )
    kept: list[str] = []
    for name in names:
        candidate_context = f"{context}: candidate {name!r}"
        if not identifiers.is_portable_component(name):
            kept.append(name)
            continue
        joined = resolved_root.path + os.sep + name
        resolved = _physical_path(
            joined, code=CODE_OUTPUT_OVERLAP, context=candidate_context
        )
        if not resolved.exists or resolved.identity is None:
            kept.append(name)
            continue
        if not resolved.is_dir:
            kept.append(name)
            continue
        reason = _managed_reason(
            record,
            resolved.path,
            code=CODE_OUTPUT_OVERLAP,
            context=candidate_context,
        )
        if reason is None:
            kept.append(name)
    return tuple(kept)


def check_declared_inputs(
    record: BoundaryRecord,
    source_root: Path,
    declared: tuple[str, ...],
    *,
    label: str,
) -> None:
    """Refuse a declared runtime/build input the prune set intersects.

    Pruning a declared input is an error naming the path and the output
    root, never permission to install an incomplete package.
    """

    try:
        _check_declared_inputs(record, os.fspath(source_root), declared, label=label)
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"Declared {label} inputs below {source_root}: boundary undetermined: {exc}",
        ) from exc


def _check_declared_inputs(
    record: BoundaryRecord,
    source_text: str,
    declared: tuple[str, ...],
    *,
    label: str,
) -> None:
    resolved_root = _check_record_root(
        record, source_text, code=CODE_SELECTION_INVALID, context=f"Source root {source_text}"
    )
    _refuse_managed_source(record, f"Declared {label} inputs below {source_text}")
    for entry in declared:
        context = f"Declared {label} input {entry!r}"
        if not identifiers.is_valid_portable_path(entry):
            raise SourceError(
                CODE_SELECTION_INVALID, f"{context} is not a portable relative path"
            )
        joined = resolved_root.path + os.sep + entry.replace("/", os.sep)
        resolved = _physical_path(joined, code=CODE_MEMBER_INVALID, context=context)
        if not resolved.exists or resolved.identity is None:
            raise SourceError(CODE_MEMBER_MISSING, f"{context} does not exist")
        if not _is_within(
            resolved.path,
            root_path=record.source_root,
            root_identity=record.source_identity,
            code=CODE_SELECTION_INVALID,
            context=context,
        ):
            raise SourceError(CODE_SELECTION_INVALID, f"{context} escapes the source root")
        reason = _managed_reason(
            record,
            resolved.path,
            code=CODE_OUTPUT_OVERLAP,
            context=context,
        )
        if reason is not None:
            raise SourceError(CODE_OUTPUT_OVERLAP, f"{context} is {reason}")


def _reject_lexical_overlap(entries: tuple[str, ...], *, context: str) -> None:
    ordered = sorted((tuple(entry.split("/")), entry) for entry in entries)
    for index, (left_parts, left) in enumerate(ordered):
        for right_parts, right in ordered[index + 1 :]:
            if _parts_contain(left_parts, right_parts) or _parts_contain(
                right_parts, left_parts
            ):
                raise SourceError(
                    CODE_SELECTION_INVALID,
                    f"{context} contains overlapping paths {left!r} and {right!r}",
                )


def _parts_contain(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) <= len(right) and right[: len(left)] == left


def _check_entry_structure(entries: tuple[str, ...], *, context: str) -> None:
    """Check portable spelling, duplicates and lexical overlap (no fs)."""

    seen: set[str] = set()
    for entry in entries:
        if not identifiers.is_valid_portable_path(entry):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"{context} entry {entry!r} is not a portable relative path",
            )
        if entry in seen:
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"{context} contains duplicate path {entry!r}",
            )
        seen.add(entry)
    _reject_lexical_overlap(entries, context=context)


def _check_required_structure(required: tuple[str, ...], *, context: str) -> None:
    for entry in required:
        if not identifiers.is_valid_portable_path(entry):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"{context} required input {entry!r} is not a portable relative path",
            )


def _readable_file(path: str, *, context: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise SourceError(CODE_MEMBER_INVALID, f"{context} is not readable: {exc}") from exc
    except _FS_ERRORS as exc:
        raise SourceError(CODE_MEMBER_INVALID, f"{context} is not readable: {exc}") from exc
    try:
        try:
            os.read(fd, 1)
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_MEMBER_INVALID, f"{context} is not readable: {exc}"
            ) from exc
    finally:
        try:
            os.close(fd)
        except _FS_ERRORS:
            pass


def _expand_entry_directory(
    record: BoundaryRecord,
    entry_path: str,
    entry_identity: tuple[int, int],
    *,
    context: str,
) -> tuple[AdmittedMember, ...]:
    """Recursively admit one directory entry's contents.

    Validation-only walk: every member is inspected with lstat, links
    and special files refuse, and no file content is ever read. Members
    enumerate in ascending UTF-8 byte order of their relative path.
    """

    members: list[AdmittedMember] = []
    visited: set[tuple[int, int]] = {entry_identity}
    stack: list[str] = [""]
    while stack:
        relative = stack.pop()
        if relative:
            current = entry_path + os.sep + relative.replace("/", os.sep)
        else:
            current = entry_path
        try:
            with os.scandir(current) as iterator:
                child_names = sorted(
                    (child.name for child in iterator),
                    key=lambda name: os.fsencode(name),
                )
        except OSError as exc:
            if exc.errno in _MISSING_ERRNOS:
                raise SourceError(
                    CODE_MEMBER_MISSING, f"{context} disappeared during inspection"
                ) from exc
            raise SourceError(
                CODE_MEMBER_INVALID, f"{context} is not readable: {exc}"
            ) from exc
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_MEMBER_INVALID, f"{context} is not readable: {exc}"
            ) from exc
        for child_name in child_names:
            child_context = f"{context}: member {child_name!r}"
            child_path = current + os.sep + child_name
            child_stat = _lstat_known(
                child_path,
                code=CODE_MEMBER_INVALID,
                missing_code=CODE_MEMBER_MISSING,
                context=child_context,
            )
            if stat.S_ISLNK(child_stat.st_mode):
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"{child_context} is a link; root inputs are link-free",
                )
            if not stat.S_ISREG(child_stat.st_mode) and not stat.S_ISDIR(
                child_stat.st_mode
            ):
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"{child_context} is not a regular file or directory",
                )
            child_identity = _identity_from_stat(child_stat)
            child_relative = child_name if not relative else relative + "/" + child_name
            reason = _managed_reason(
                record,
                child_path,
                code=CODE_OUTPUT_OVERLAP,
                context=child_context,
            )
            if reason is not None:
                raise SourceError(CODE_OUTPUT_OVERLAP, f"{child_context} is {reason}")
            is_dir = stat.S_ISDIR(child_stat.st_mode)
            if not is_dir:
                _readable_file(child_path, context=child_context)
            else:
                if child_identity in visited:
                    raise SourceError(
                        CODE_MEMBER_INVALID,
                        f"{child_context} revisits an admitted directory",
                    )
                visited.add(child_identity)
            members.append(
                AdmittedMember(path=child_relative, is_dir=is_dir, identity=child_identity)
            )
            if is_dir:
                stack.append(child_relative)
    members.sort(key=lambda member: member.path.encode("utf-8"))
    return tuple(members)


def _lstat_known(path: str, *, code: str, missing_code: str, context: str) -> os.stat_result:
    entry = _lstat(path, code=code, context=context)
    if entry is None:
        raise SourceError(missing_code, f"{context} disappeared during inspection")
    return entry


def validate_root_inputs(
    record: BoundaryRecord,
    source_root: Path,
    alias: str,
    policy: RepositoryPolicy,
    required_inputs: tuple[str, ...] = (),
) -> tuple[AdmittedInput, ...]:
    """Validate the operator's ``root_inputs`` allowlist for one alias.

    Structural rules (portable, duplicate-free, non-overlapping) are
    decided before any filesystem access. Every entry must then exist,
    be link-free, be disjoint from outputs, and be readable; a directory
    selects its recursive contents. The admitted set must cover
    ``SKILL.md`` and every required input. Unknown aliases fail
    ``source_alias_unknown``.
    """

    try:
        return _validate_root_inputs(
            record, os.fspath(source_root), alias, policy, required_inputs
        )
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"Root inputs for {alias!r}: boundary undetermined: {exc}",
        ) from exc


def _validate_root_inputs(
    record: BoundaryRecord,
    source_text: str,
    alias: str,
    policy: RepositoryPolicy,
    required_inputs: tuple[str, ...],
) -> tuple[AdmittedInput, ...]:
    context = f"Root inputs for {alias!r}"
    if alias not in policy.root_inputs:
        raise SourceError(CODE_ALIAS_UNKNOWN, f"{context} names an unknown alias")
    entries = policy.root_inputs[alias]
    if not entries:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{context} is empty: separation cannot be proved",
        )
    _check_entry_structure(entries, context=context)
    _check_required_structure(required_inputs, context=context)
    resolved_root = _check_record_root(
        record, source_text, code=CODE_SELECTION_INVALID, context=f"Source root {source_text}"
    )
    _refuse_managed_source(record, context)
    admitted: list[AdmittedInput] = []
    for entry in entries:
        entry_context = f"{context}: entry {entry!r}"
        joined = resolved_root.path + os.sep + entry.replace("/", os.sep)
        _reject_entry_links(resolved_root.path, entry, context=entry_context)
        top = _lstat(joined, code=CODE_MEMBER_INVALID, context=entry_context)
        if top is None:
            raise SourceError(CODE_MEMBER_MISSING, f"{entry_context} does not exist")
        resolved = _physical_path(joined, code=CODE_MEMBER_INVALID, context=entry_context)
        if not resolved.exists or resolved.identity is None:
            raise SourceError(CODE_MEMBER_MISSING, f"{entry_context} does not exist")
        if not _is_within(
            resolved.path,
            root_path=record.source_root,
            root_identity=record.source_identity,
            code=CODE_SELECTION_INVALID,
            context=entry_context,
        ):
            raise SourceError(
                CODE_SELECTION_INVALID, f"{entry_context} escapes the source root"
            )
        reason = _managed_reason(
            record,
            resolved.path,
            code=CODE_OUTPUT_OVERLAP,
            context=entry_context,
        )
        if reason is not None:
            raise SourceError(CODE_OUTPUT_OVERLAP, f"{entry_context} is {reason}")
        is_dir = stat.S_ISDIR(top.st_mode)
        if not is_dir and not stat.S_ISREG(top.st_mode):
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"{entry_context} is not a regular file or directory",
            )
        members: tuple[AdmittedMember, ...] = ()
        if is_dir:
            members = _expand_entry_directory(
                record, joined, resolved.identity, context=entry_context
            )
        else:
            _readable_file(joined, context=entry_context)
        admitted.append(
            AdmittedInput(
                path=entry,
                resolved=resolved.path,
                is_dir=is_dir,
                identity=resolved.identity,
                members=members,
            )
        )
    _reject_physical_overlap(admitted, context=context)
    _require_covered_inputs(
        resolved_root.path, admitted, (SKILL_MD_NAME, *required_inputs), context=context
    )
    return tuple(admitted)


def _reject_entry_links(source_display: str, entry: str, *, context: str) -> None:
    """Refuse a root_inputs entry traversing any link, including mid-path."""

    parts = entry.split("/")
    for depth in range(1, len(parts) + 1):
        probe = source_display + os.sep + os.sep.join(parts[:depth])
        probe_stat = _lstat(probe, code=CODE_MEMBER_INVALID, context=context)
        if probe_stat is None:
            raise SourceError(CODE_MEMBER_MISSING, f"{context} does not exist")
        if stat.S_ISLNK(probe_stat.st_mode):
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"{context} traverses a link at {'/'.join(parts[:depth])!r}; "
                "root inputs are link-free",
            )


def _reject_physical_overlap(admitted: list[AdmittedInput], *, context: str) -> None:
    """Refuse entries resolving onto or below each other by identity.

    Lexical overlap was already rejected structurally; this is the
    physical remainder (case aliases, hard links, bind mounts). Either
    direction refuses: no admitted entry may select another entry's
    bytes twice.
    """

    for index, outer in enumerate(admitted):
        for inner in admitted[index + 1 :]:
            if _is_within(
                inner.resolved,
                root_path=outer.resolved,
                root_identity=outer.identity,
                code=CODE_PATH_CONFLICT,
                context=context,
            ):
                raise SourcePathConflictError(outer.path, inner.path)
            if _is_within(
                outer.resolved,
                root_path=inner.resolved,
                root_identity=inner.identity,
                code=CODE_PATH_CONFLICT,
                context=context,
            ):
                raise SourcePathConflictError(inner.path, outer.path)


def _require_covered_inputs(
    source_display: str,
    admitted: list[AdmittedInput],
    required: tuple[str, ...],
    *,
    context: str,
) -> None:
    for entry in required:
        required_context = f"{context}: required input {entry!r}"
        joined = source_display + os.sep + entry.replace("/", os.sep)
        resolved = _physical_path(joined, code=CODE_MEMBER_MISSING, context=required_context)
        if not resolved.exists or resolved.identity is None:
            raise SourceError(CODE_MEMBER_MISSING, f"{required_context} does not exist")
        covered = False
        for item in admitted:
            if _is_within(
                resolved.path,
                root_path=item.resolved,
                root_identity=item.identity,
                code=CODE_MEMBER_MISSING,
                context=required_context,
            ):
                covered = True
                break
        if not covered:
            raise SourceError(
                CODE_MEMBER_MISSING, f"{required_context} is not covered by the allowlist"
            )


def recheck_publication_destination(
    record: BoundaryRecord,
    source_root: Path,
    planned_output: str,
    destination: str,
    *,
    admitted: frozenset[tuple[int, int]] = frozenset(),
) -> None:
    """Recheck destination separation immediately before a publication write.

    The serialized publication transaction (TASK-260916-17x3o1) calls this
    per write with the frozen record in hand: the planned managed output
    must be unchanged since planning, every destination parent must exist
    as a directory with no newly introduced link, the destination must
    stay within the planned output, and it must never overwrite an
    admitted input. Every failure is ``source_output_overlap`` and the
    caller rolls back unpublished state.
    """

    try:
        _recheck_publication_destination(
            record,
            os.fspath(source_root),
            planned_output,
            destination,
            admitted=admitted,
        )
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"Publication destination {destination!r}: boundary undetermined: {exc}",
        ) from exc


def _recheck_publication_destination(
    record: BoundaryRecord,
    source_text: str,
    planned_output: str,
    destination: str,
    *,
    admitted: frozenset[tuple[int, int]],
) -> None:
    context = f"Publication destination {destination!r}"
    if not identifiers.is_valid_portable_path(planned_output):
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{context}: planned output is not a portable path"
        )
    if not identifiers.is_valid_portable_path(destination):
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{context} is not a portable relative path"
        )
    resolved_root = _check_record_root(
        record, source_text, code=CODE_OUTPUT_OVERLAP, context=f"Source root {source_text}"
    )
    planned_first = planned_output.split("/", 1)[0]
    frozen = next(
        (root for root in record.managed_roots if root.name == planned_first), None
    )
    if frozen is None:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{context}: planned output {planned_output!r} is not a managed output",
        )
    _reverify_managed_binding(resolved_root.path, frozen, context=context)
    effective_base = _effective_managed_base(
        resolved_root.path, frozen, context=context
    )
    planned_display = _resolve_effective_chain(
        resolved_root.path,
        effective_base,
        planned_output,
        frozen,
        context=f"{context}: planned output {planned_output!r}",
        chain_label="planned output",
    )
    planned_stat = _lstat_known(
        planned_display,
        code=CODE_OUTPUT_OVERLAP,
        missing_code=CODE_OUTPUT_OVERLAP,
        context=f"{context}: planned output {planned_output!r}",
    )
    if not stat.S_ISDIR(planned_stat.st_mode):
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{context}: planned output {planned_output!r} is not a directory",
        )
    planned_identity = _identity_from_stat(planned_stat)
    if not _is_within(
        planned_display,
        root_path=effective_base,
        root_identity=_identity_from_stat(
            _lstat_known(
                effective_base,
                code=CODE_OUTPUT_OVERLAP,
                missing_code=CODE_OUTPUT_OVERLAP,
                context=context,
            )
        ),
        code=CODE_OUTPUT_OVERLAP,
        context=context,
    ):
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{context}: planned output {planned_output!r} is outside its managed root",
        )
    parent_display = _resolve_effective_chain(
        resolved_root.path,
        effective_base,
        destination.rsplit("/", 1)[0] if "/" in destination else "",
        frozen,
        context=context,
        chain_label="parent",
    )
    if not _is_within(
        parent_display,
        root_path=planned_display,
        root_identity=planned_identity,
        code=CODE_OUTPUT_OVERLAP,
        context=context,
    ):
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{context} is outside planned output {planned_output!r}",
        )
    destination_display = resolved_root.path + os.sep + destination.replace("/", os.sep)
    final = _lstat(destination_display, code=CODE_OUTPUT_OVERLAP, context=context)
    if final is not None:
        if stat.S_ISLNK(final.st_mode):
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{context} is a link; newly introduced links are never followed",
            )
        if _identity_from_stat(final) in admitted:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{context} would overwrite an admitted input",
            )


def _reverify_managed_binding(
    source_display: str,
    frozen: ManagedRoot,
    *,
    context: str,
) -> None:
    """Refuse a managed-output binding changed since planning."""

    current = _freeze_managed_root(
        source_display,
        frozen.name,
        code=CODE_OUTPUT_OVERLAP,
        context=context,
    )
    if (
        current.identity != frozen.identity
        or current.entry_identity != frozen.entry_identity
        or current.target != frozen.target
    ):
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{context}: managed output {frozen.name!r} changed since planning",
        )


def _effective_managed_base(
    source_display: str,
    frozen: ManagedRoot,
    *,
    context: str,
) -> str:
    """Return the resolved base a frozen managed root writes through.

    A managed root frozen as a link resolves through its unchanged
    binding; anything else about the binding refuses. The binding was
    just reverified, so this resolves but never re-derives.
    """

    binding_path = source_display + os.sep + frozen.name
    if frozen.target is None:
        return binding_path
    if frozen.identity is None:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{context}: managed output {frozen.name!r} is not a directory",
        )
    resolved = _physical_path(binding_path, code=CODE_OUTPUT_OVERLAP, context=context)
    if (
        not resolved.exists
        or resolved.identity is None
        or not _identities_equal(resolved.identity, frozen.identity)
    ):
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{context}: managed output {frozen.name!r} changed since planning",
        )
    return resolved.path


def _resolve_effective_chain(
    source_display: str,
    effective_base: str,
    relative: str,
    frozen: ManagedRoot,
    *,
    context: str,
    chain_label: str,
) -> str:
    """Walk one publication chain to its effective display path.

    Every component must exist as a directory with no newly introduced
    link; the frozen managed root itself is entered through its
    effective base. An empty chain selects the source root.
    """

    if not relative:
        return source_display
    parts = relative.split("/")
    current = source_display
    for depth, part in enumerate(parts, start=1):
        if depth == 1 and part == frozen.name:
            probe = source_display + os.sep + part
            probe_stat = _lstat(probe, code=CODE_OUTPUT_OVERLAP, context=context)
            if probe_stat is None:
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"{context}: {chain_label} {relative!r} does not exist",
                )
            if stat.S_ISLNK(probe_stat.st_mode):
                if frozen.target is None:
                    raise SourceError(
                        CODE_OUTPUT_OVERLAP,
                        f"{context}: {chain_label} component {part!r} is a link; "
                        "newly introduced links are never followed",
                    )
                current = effective_base
                continue
            if not stat.S_ISDIR(probe_stat.st_mode):
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"{context}: {chain_label} {relative!r} is not a directory",
                )
            current = probe
            continue
        probe = current + os.sep + part
        probe_stat = _lstat(probe, code=CODE_OUTPUT_OVERLAP, context=context)
        if probe_stat is None:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{context}: {chain_label} {relative!r} does not exist",
            )
        if stat.S_ISLNK(probe_stat.st_mode):
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{context}: {chain_label} component {part!r} is a link; "
                "newly introduced links are never followed",
            )
        if not stat.S_ISDIR(probe_stat.st_mode):
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{context}: {chain_label} {relative!r} is not a directory",
            )
        current = probe
    return current


