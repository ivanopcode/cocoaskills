"""Atomic publication of schema-2 source installs and locks.

Implements the install side of protocol skillfile-sources sections 2
through 4 for local ``path`` sources: one recoverable transaction
publishes the lock, marker v5 records, runtime store entries, context
projection and adapter mirrors, with the physical boundary rechecked
immediately before each publication write. Status, locked install
(repair) and explicit refresh all revalidate the exact locked source
and required evidence; marker summaries never authorize anything and
locks are never silently refreshed.

The transaction itself is the existing :class:`csk.transactions`
engine, extended with a per-target recheck payload and a pre-write
hook: this module builds the payloads and the hook, never a second
engine, journal, or staging layout. Every filesystem failure that
reaches a public entry point of this module is a structured
``SourceError`` naming the offending subject.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from .. import adapters, hashing, identifiers, install_marker, locale, locking, manifest
from .. import protocol_json, skillspec, whitelist
from ..builds import planner as build_planner
from ..transactions import (
    ABSENT_DIGEST,
    JournalTarget,
    MutableTarget,
    PreWriteHook,
    TargetKind,
    TransactionCorruptionError,
    TransactionEngine,
    TransactionError,
    TransactionPlan,
    digest_target,
)
from . import boundaries, consumers, skillfile_v2, snapshot, store
from .errors import (
    CODE_LOCK_STALE,
    CODE_MEMBER_INVALID,
    CODE_MEMBER_MISSING,
    CODE_NAME_CONFLICT,
    CODE_OUTPUT_OVERLAP,
    CODE_SELECTION_INVALID,
    CODE_SNAPSHOT_CHANGED,
    CODE_SNAPSHOT_UNAVAILABLE,
    SourceError,
)
from .lock import (
    LockMember,
    MachinePrivateBinding,
    SkillfileLock,
    create_lock,
    read_lock,
    read_machine_private_binding,
    serialize_lock,
    serialize_machine_private_binding,
    validate_lock,
)
from .package_identity import LocalSnapshot, package_identity_sha256
from .selection import (
    SelectedSkill,
    destination_key,
    expand_collection,
    expand_selectors,
)

__all__ = [
    "BINDINGS_DIRNAME",
    "CLASS_ADAPTER_LEDGER",
    "CLASS_BINDINGS",
    "CLASS_CONTEXT",
    "CLASS_LOCK",
    "CLASS_REMOVAL",
    "CLASS_RUNTIME",
    "RUNTIME_DIRNAME",
    "SKILLFILE_LOCK_NAME",
    "STAGE_BOUNDARIES",
    "CapturedMember",
    "MemberVerdict",
    "ResolvedMember",
    "Schema2InstallResult",
    "Schema2Status",
    "StagedMember",
    "TargetSpec",
    "binding_path",
    "build_recheck_payloads",
    "capture_schema2_members",
    "evaluate_schema2_installation",
    "freeze_publication_record",
    "install_schema2",
    "live_context_managed",
    "make_publication_hook",
    "materialize_frozen",
    "plan_schema2_targets",
    "project_slug",
    "read_schema2_lock",
    "resolve_schema2_members",
    "resolve_source_root",
    "runtime_entry_path",
    "stage_schema2_desired",
    "validate_live_managed_tree",
]

SKILLFILE_LOCK_NAME: Final = "Skillfile.lock.json"
RUNTIME_DIRNAME: Final = "runtime"
BINDINGS_DIRNAME: Final = "bindings"
TRANSACTION_PREFIX: Final = "source-install"

CLASS_BINDINGS: Final = "05-bindings"
CLASS_LOCK: Final = "07-lock"
CLASS_CONTEXT: Final = "10-context"
CLASS_RUNTIME: Final = "20-runtime"
# Adapter classes mirror csk.adapters._plan_adapter_targets, the single
# planner for both install lanes; the enumeration test fails if they drift.
CLASS_ADAPTER_LEDGER: Final = "60-adapter-ledger"
CLASS_REMOVAL: Final = "80-removal"

#: Stage boundaries of one schema-2 publication, in pipeline order. The
#: fault matrix injects at each one and the enumeration test pins this
#: tuple, so a boundary cannot silently leave the matrix.
STAGE_BOUNDARIES: Final[tuple[str, ...]] = (
    "selection",
    "capture",
    "re-enumeration",
    "snapshot-store",
    "lock-create",
    "prepare",
    "commit-05-bindings",
    "commit-07-lock",
    "commit-10-context",
    "commit-20-runtime",
    "commit-60-adapter-ledger",
    "commit-80-removal",
    "cleanup",
)

_FS_ERRORS: Final[tuple[type[Exception], ...]] = (
    OSError,
    RuntimeError,
    ValueError,
    UnicodeError,
)

_RECHECK_MANAGED: Final = "managed-output"
_RECHECK_ROOT_FILE: Final = "project-root-file"
_RECHECK_HOME_ENTRY: Final = "home-store-entry"


@dataclass(frozen=True)
class ResolvedMember:
    """One selected skill with its root-selection ordinal and source root.

    ``selection_ordinal`` is the member's index in expansion order: the
    lock schema requires every root member selection to be unique and
    zero-based dense, which a selector index cannot satisfy when one
    collection selects several members.
    """

    name: str
    from_alias: str
    directory: str
    selection_ordinal: int
    source_root: Path


@dataclass(frozen=True)
class CapturedMember:
    """One resolved member with its frozen capture and package identity.

    ``captured`` is None only on the locked-repair path, where live
    bytes cannot be captured and the verified store copy serves
    instead; ``package`` always carries the authoritative snapshot
    digest either way.
    """

    member: ResolvedMember
    captured: snapshot.CapturedPackage | None
    package: LocalSnapshot
    package_key: str


@dataclass(frozen=True)
class StagedMember:
    """One captured member with its staged context and content hash."""

    captured: CapturedMember
    spec: skillspec.SkillSpec
    content_sha256: str
    files: tuple[str, ...]
    context_dir: Path


@dataclass(frozen=True)
class TargetSpec:
    """One planned publication target: live path plus staged desire."""

    target_class: str
    identifier: str
    live_path: Path
    kind: TargetKind
    staged: Path | None


@dataclass(frozen=True)
class Schema2InstallResult:
    """Outcome of one schema-2 install call."""

    messages: tuple[str, ...]
    installed: tuple[str, ...]
    up_to_date: tuple[str, ...]
    removed: tuple[str, ...]
    lock_replaced: bool
    transaction_id: str | None


@dataclass(frozen=True)
class MemberVerdict:
    """Read-only currentness verdict for one lock member."""

    name: str
    current: bool
    label: str
    detail: str
    locked_snapshot: str | None
    current_snapshot: str | None
    marker_snapshot: str | None


@dataclass(frozen=True)
class Schema2Status:
    """Read-only currentness verdict for one schema-2 installation."""

    members: tuple[MemberVerdict, ...]
    errors: tuple[str, ...]
    lock_sha256: str | None


def project_slug(project_identity: str) -> str:
    """Return the machine-private binding namespace for one project identity."""

    if type(project_identity) is not str:
        raise SourceError(
            CODE_MEMBER_INVALID, "project identity must be a string"
        )
    try:
        raw = project_identity.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"project identity is not valid Unicode: {exc}"
        ) from exc
    return hashlib.sha256(raw).hexdigest()[:16]


def _key_hex(value: str, *, kind: str) -> str:
    """Hash one namespace key exactly like the snapshot store does."""

    if type(value) is not str:
        raise TypeError(f"snapshot {kind} must be a string")
    try:
        raw = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"snapshot {kind} is not valid Unicode",
        ) from exc
    return hashlib.sha256(raw).hexdigest()


def runtime_entry_path(home: Path, skill: str, package_key: str) -> Path:
    """Return the runtime entry for one (skill, package) key (pure).

    Keys are ``(skill name, SHA-256(CCJ-1(package)))`` per protocol
    section 4, under the distinct ``source-v1`` namespace; the hashed
    components keep hostile names from escaping or conflating on any
    host, exactly like the snapshot store layout.
    """

    return (
        store.store_root(home)
        / RUNTIME_DIRNAME
        / _key_hex(skill, kind="skill name")
        / _key_hex(package_key, kind="package identity")
    )


def binding_path(home: Path, slug: str, alias: str) -> Path:
    """Return the machine-private binding path for one source alias (pure)."""

    return store.store_root(home) / BINDINGS_DIRNAME / slug / f"{alias}.json"


def resolve_source_root(
    project_path: Path, alias: str, acquisition: skillfile_v2.SourceAcquisition
) -> Path:
    """Resolve one source alias to its local source root.

    Only ``path`` acquisitions install through the atomic publisher: a
    literal native path, resolved against the project directory when
    relative. Git and repository acquisitions refuse here; their
    acquisition pipeline belongs to a later leaf, and silently dropping
    them would install an incomplete closure.
    """

    if isinstance(acquisition, skillfile_v2.PathSource):
        declared = acquisition.path
        candidate = Path(declared)
        if candidate.is_absolute():
            return candidate
        return project_path / declared
    raise SourceError(
        CODE_SELECTION_INVALID,
        f"Source {alias!r} uses a network acquisition, which atomic source "
        "install does not acquire; only local path sources install here",
    )


def _selector_admits_member(
    selector: skillfile_v2.SkillSelector, *, name: str, directory: str
) -> bool:
    """Decide whether one selector admits a member, without filesystem reads.

    Individuals match by exact directory and installed name. Collections
    match when the member directory sits exactly one folder below the
    collection base and the child folder passes the include/exclude
    admission rule: a listed literal or ``"*"`` selects it, and any
    exclusion vetoes it. Managed-output pruning is a filesystem property
    a pure matcher cannot see, but an installed member survived it by
    construction, so admission here is exact for installed members.
    """

    if isinstance(selector, skillfile_v2.IndividualSelector):
        return selector.directory == directory and selector.name == name
    base = selector.directory
    if base == ".":
        if "/" in directory:
            return False
        folder = directory
    else:
        prefix = base + "/"
        if not directory.startswith(prefix):
            return False
        folder = directory[len(prefix):]
        if not folder or "/" in folder:
            return False
    if folder in selector.exclude:
        return False
    return folder in selector.include or "*" in selector.include


def _attribute_unique_selector(
    selectors: list[skillfile_v2.SkillSelector],
    *,
    name: str,
    directory: str,
    from_alias: str | None = None,
) -> int | None:
    """Return the unique admitting selector index, or None when not unique.

    Expansion refused every repeated selection, so a successfully
    installed member is admitted by exactly one selector; zero or
    several matches mean the lock (or manifest) no longer describes an
    expansion outcome. ``from_alias`` narrows the search when the
    caller's member already carries its source alias.
    """

    found: int | None = None
    for index, selector in enumerate(selectors):
        if from_alias is not None and selector.from_alias != from_alias:
            continue
        if not _selector_admits_member(selector, name=name, directory=directory):
            continue
        if found is not None:
            return None
        found = index
    return found


def resolve_schema2_members(
    project_path: Path, manifest_value: manifest.ProjectManifest
) -> tuple[ResolvedMember, ...]:
    """Expand and validate every schema-2 selector against local roots.

    Refusals are explicit and never silent: network acquisitions, legacy
    declarations, root packages (which need admitted-subset capture the
    snapshot leaf does not provide) and members with transitive
    requirements (which need closure resolution) all fail here, before
    any filesystem work beyond expansion itself.
    """

    if manifest_value.skills:
        names = sorted(decl.name for decl in manifest_value.skills)
        raise SourceError(
            CODE_SELECTION_INVALID,
            "Legacy skill declarations are not installable from a "
            f"schema-2 Skillfile by atomic install: {names[0]!r}",
        )
    selectors = list(manifest_value.selectors)
    if any(selector.directory == "." for selector in selectors):
        raise SourceError(
            CODE_SELECTION_INVALID,
            "Root package publication needs admitted-subset capture, which "
            "atomic install does not provide; select a nested package",
        )
    source_roots = {
        alias: resolve_source_root(project_path, alias, acquisition)
        for alias, acquisition in manifest_value.sources.items()
    }
    try:
        selected = expand_selectors(selectors, source_roots)
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Skill selection cannot be expanded: {exc}",
        ) from exc
    resolved: list[ResolvedMember] = []
    for ordinal, member in enumerate(selected):
        if member.requirements:
            missing = sorted({requirement.name for requirement in member.requirements})
            raise SourceError(
                CODE_MEMBER_MISSING,
                f"Skill {member.name!r} requires {missing[0]!r}, which needs "
                "transitive closure resolution",
            )
        if (
            _attribute_unique_selector(
                selectors,
                name=member.name,
                directory=member.directory,
                from_alias=member.from_alias,
            )
            is None
        ):
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Selected skill {member.name!r} cannot be attributed "
                "to a manifest selector",
            )
        resolved.append(
            ResolvedMember(
                name=member.name,
                from_alias=member.from_alias,
                directory=member.directory,
                selection_ordinal=ordinal,
                source_root=source_roots[member.from_alias],
            )
        )
    return tuple(resolved)


def locked_schema2_members(
    old_lock: SkillfileLock,
    selectors: list[skillfile_v2.SkillSelector],
    source_roots: dict[str, Path],
) -> tuple[ResolvedMember, ...]:
    """Derive install members from the lock without expanding selectors.

    A locked install or status check consumes the frozen membership:
    each lock member is attributed to its source by admission matching
    (no filesystem expansion), so a deleted member directory surfaces
    as an unavailable snapshot at capture time instead of a selection
    failure. Non-local packages, root packages, unknown sources,
    unattributable members and filesystem-equivalent name collisions
    (the selection leaf's own folding rule) refuse explicitly.
    """

    seen_folded: dict[str, str] = {}
    for lock_member in old_lock.members:
        folded = destination_key(lock_member.name)
        previous = seen_folded.get(folded)
        if previous is not None and previous != lock_member.name:
            raise SourceError(
                CODE_NAME_CONFLICT,
                f"Filesystem-equivalent installed skill names {previous!r} "
                f"and {lock_member.name!r} collide in the lock",
            )
        seen_folded.setdefault(folded, lock_member.name)
    members: list[ResolvedMember] = []
    for lock_member in old_lock.members:
        if not isinstance(lock_member.package, LocalSnapshot):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Skill {lock_member.name!r} lock member is not a local "
                "snapshot, which atomic install does not serve",
            )
        if lock_member.directory == ".":
            raise SourceError(
                CODE_SELECTION_INVALID,
                "Root package publication needs admitted-subset capture, "
                "which atomic install does not provide; select a nested package",
            )
        index = _attribute_unique_selector(
            selectors, name=lock_member.name, directory=lock_member.directory
        )
        if index is None or lock_member.selection is None:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {lock_member.name!r} lock member cannot be "
                "attributed to a manifest selector",
            )
        from_alias = selectors[index].from_alias
        if from_alias not in source_roots:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {lock_member.name!r} lock member names source "
                f"{from_alias!r}, which has no resolved source root",
            )
        members.append(
            ResolvedMember(
                name=lock_member.name,
                from_alias=from_alias,
                directory=lock_member.directory,
                selection_ordinal=lock_member.selection,
                source_root=source_roots[from_alias],
            )
        )
    return tuple(members)


def capture_schema2_members(
    members: tuple[ResolvedMember, ...], *, home: Path
) -> dict[str, snapshot.CapturedPackage]:
    """Capture every resolved member as an immutable snapshot.

    Capture reads working-tree bytes with revalidation inside; failures
    carry the snapshot leaf's codes unchanged.
    """

    captured: dict[str, snapshot.CapturedPackage] = {}
    for member in members:
        try:
            package = snapshot.capture_package_snapshot(
                member.source_root, member.directory, home=home
            )
        except SourceError:
            raise
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_SNAPSHOT_CHANGED,
                f"Skill {member.name!r} cannot be captured: {exc}",
            ) from exc
        captured[member.name] = package
    return captured


def collection_membership(
    selectors: list[skillfile_v2.SkillSelector],
    source_roots: dict[str, Path],
) -> dict[int, tuple[str, ...]]:
    """Expand every collection selector to its ordered member names."""

    membership: dict[int, tuple[str, ...]] = {}
    for index, selector in enumerate(selectors):
        if isinstance(selector, skillfile_v2.IndividualSelector):
            continue
        try:
            members = expand_collection(source_roots[selector.from_alias], selector)
        except SourceError:
            raise
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_SNAPSHOT_CHANGED,
                f"Collection {selector.directory!r} cannot be re-enumerated: {exc}",
            ) from exc
        membership[index] = tuple(member.name for member in members)
    return membership


def require_membership_unchanged(
    before: dict[int, tuple[str, ...]],
    after: dict[int, tuple[str, ...]],
) -> None:
    """Refuse any admitted-membership change observed during capture."""

    if before != after:
        raise SourceError(
            CODE_SNAPSHOT_CHANGED,
            "Admitted membership changed during capture; run an explicit refresh",
        )


def stage_schema2_store(
    home: Path, captured: dict[str, CapturedMember]
) -> dict[str, store.StoredSnapshot]:
    """Stage every capture into the source-v1 snapshot store.

    Keys are ``(skill name, package identity hash)``; staging is
    idempotent for identical bytes and atomically supersedes on
    verified change. Failures carry the store's codes unchanged.
    """

    staged: dict[str, store.StoredSnapshot] = {}
    for name in sorted(captured):
        item = captured[name]
        if item.captured is None:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"Skill {name!r} has no captured bytes to stage",
            )
        try:
            staged[name] = store.stage_snapshot(
                home, name, item.package_key, item.captured
            )
        except SourceError:
            raise
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"Skill {name!r} snapshot cannot be staged: {exc}",
            ) from exc
    return staged


def materialize_frozen(
    files: Mapping[str, snapshot.FrozenFile], destination: Path, *, subject: str
) -> None:
    """Write frozen in-memory bytes to a private staging directory.

    Paths are revalidated as portable relative paths even though the
    inventory already validated them: staging never trusts its input.
    The executable bit is applied on POSIX hosts only, matching the
    inventory rule for filesystems without that metadata.
    """

    try:
        items = list(files.items())
    except (AttributeError, TypeError) as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"{subject} has no frozen file mapping: {exc}"
        ) from exc
    try:
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        for path, frozen in items:
            if type(path) is not str or not identifiers.is_valid_portable_path(path):
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"{subject} carries a non-portable frozen path {path!r}",
                )
            data = getattr(frozen, "data", None)
            if type(data) is not bytes:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"{subject} file {path!r} has no frozen bytes",
                )
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if getattr(frozen, "executable", False) and os.name == "posix":
                mode = target.stat().st_mode
                target.chmod(mode | 0o111)
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"{subject} cannot be staged: {exc}"
        ) from exc


def load_member_spec(frozen_dir: Path, *, name: str) -> skillspec.SkillSpec:
    """Load one member skill spec from its frozen materialization."""

    try:
        return skillspec.load_skill_spec(frozen_dir)
    except skillspec.SkillSpecError as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Skill {name!r} is not a valid package: {exc}"
        ) from exc
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Skill {name!r} manifest cannot be read: {exc}"
        ) from exc


def require_context_only(spec: skillspec.SkillSpec, *, name: str) -> None:
    """Refuse members needing materialization this leaf does not publish.

    Exported commands need runtime activation and shims, owned by the
    runtime-materialization story, and skill requirements need closure
    resolution: both refuse explicitly here rather than installing an
    incomplete skill.
    """

    if spec.commands:
        first = sorted(spec.commands)[0]
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill {name!r} exports command {first!r}, which needs command "
            "materialization that atomic install does not publish",
        )
    if spec.requirements:
        first = sorted(spec.requirements)[0]
        raise SourceError(
            CODE_MEMBER_MISSING,
            f"Skill {name!r} requires {first!r}, which needs transitive "
            "closure resolution",
        )


def stage_member_context(
    frozen_dir: Path,
    context_dir: Path,
    spec: skillspec.SkillSpec,
    *,
    name: str,
    locale_value: str | None,
) -> tuple[list[str], str]:
    """Project one member context from frozen bytes and hash it.

    The projection reuses the existing context rules unchanged:
    ``scripts/`` is context only when no commands are exported (always
    here, since commands refuse above), runtime and build roots stay
    excluded, and the content hash runs after locale rendering.
    """

    try:
        files = whitelist.copy_context(
            frozen_dir,
            context_dir,
            include_scripts=not spec.commands,
            exclude_roots=spec.runtime_roots,
            build_roots=spec.build_roots,
        )
    except whitelist.WhitelistError as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Skill {name!r} context cannot be projected: {exc}"
        ) from exc
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Skill {name!r} context cannot be staged: {exc}"
        ) from exc
    try:
        locale.render_locale(
            frozen_dir, context_dir, locale_value, exclude_roots=spec.build_roots
        )
    except locale.LocaleError as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Skill {name!r} locale cannot be rendered: {exc}"
        ) from exc
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Skill {name!r} locale cannot be rendered: {exc}"
        ) from exc
    try:
        content = hashing.content_sha256(context_dir)
    except hashing.HashingError as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Skill {name!r} context cannot be hashed: {exc}"
        ) from exc
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Skill {name!r} context cannot be hashed: {exc}"
        ) from exc
    return files, content


def build_marker_plan(
    *,
    name: str,
    package: LocalSnapshot,
    lock_sha256: str,
    content_sha256: str,
    spec: skillspec.SkillSpec,
    files: tuple[str, ...],
    agents: tuple[str, ...],
    locale_value: str | None,
) -> install_marker.MarkerPlan:
    """Build the marker-comparable plan projection for one member."""

    try:
        return install_marker.MarkerPlan(
            name=name,
            package=package,
            lock_sha256=lock_sha256,
            context_sha256=content_sha256,
            content_sha256=content_sha256,
            locale=locale_value,
            agents=agents,
            commands=(),
            dependencies=tuple(spec.dependencies),
            skill_schema_version=spec.schema_version,
            runtime_roots=spec.runtime_roots,
            build_roots=spec.build_roots,
            files=files,
            builds={},
            requirements=None,
            mcp_servers=None,
            activation=install_marker.MarkerActivation(context=True, commands=()),
            requirers=None,
        )
    except install_marker.InstallMarkerError as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Skill {name!r} plan is not valid: {exc}"
        ) from exc


def build_marker(
    plan: install_marker.MarkerPlan, *, installed_at: str
) -> install_marker.InstallMarkerV5:
    """Build one marker v5 record from its plan projection."""

    try:
        return install_marker.InstallMarkerV5(
            name=plan.name,
            package=plan.package,
            lock_sha256=plan.lock_sha256,
            content_sha256=plan.content_sha256,
            locale=plan.locale,
            agents=plan.agents,
            commands=plan.commands,
            dependencies=plan.dependencies,
            skill_schema_version=plan.skill_schema_version,
            runtime_roots=plan.runtime_roots,
            build_roots=plan.build_roots,
            installed_at=installed_at,
            files=plan.files,
            builds=dict(plan.builds),
            requirements=plan.requirements,
            mcp_servers=plan.mcp_servers,
            activation=plan.activation,
            requirers=plan.requirers,
        )
    except install_marker.InstallMarkerError as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Skill {plan.name!r} marker is not valid: {exc}"
        ) from exc


def read_schema2_lock(project_path: Path) -> SkillfileLock | None:
    """Read the project lock, distinguishing absence from failure.

    A missing lock is a legitimate ``None`` (initial install). A link, a
    directory, an unreadable file, or malformed bytes all refuse: the
    lock destination is never followed through a link, and a failure to
    read is never treated as an absent lock.
    """

    path = project_path / SKILLFILE_LOCK_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Lock {path} cannot be inspected: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"Lock {path} is a link; newly introduced links are never followed",
        )
    if not stat.S_ISREG(info.st_mode):
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"Lock {path} is not a regular file"
        )
    try:
        raw = path.read_bytes()
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Lock {path} is unreadable: {exc}"
        ) from exc
    try:
        return read_lock(raw)
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Lock {path} is not usable: {exc}"
        ) from exc


def validate_live_managed_tree(path: Path, *, subject: str) -> None:
    """Refuse any link inside a live managed tree.

    Managed context and runtime trees never contain links: frozen
    captures refuse them and staging never creates them. A link found
    here was introduced after publication, so the boundary moved and
    the destination refuses rather than digesting through it.
    """

    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} cannot be inspected: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{subject} is a link; newly introduced links are never followed",
        )
    if not stat.S_ISDIR(info.st_mode):
        return
    try:
        entries = sorted(path.rglob("*"))
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} cannot be inspected: {exc}"
        ) from exc
    for entry in entries:
        try:
            entry_info = entry.lstat()
        except FileNotFoundError:
            continue
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP, f"{subject} cannot be inspected: {exc}"
            ) from exc
        if stat.S_ISLNK(entry_info.st_mode):
            relative = entry.relative_to(path).as_posix()
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{subject} entry {relative!r} is a link; newly introduced "
                "links are never followed",
            )


def live_context_managed(live: Path, name: str) -> bool:
    """Return whether a live context directory is csk-managed.

    Managed means a parseable install marker whose recorded name equals
    the directory name, at any marker version: csk wrote it, so the
    transaction may replace it. Anything else is unmanaged and the
    publisher refuses rather than overwriting it.
    """

    marker_path = live / ".csk-install.json"
    try:
        raw = marker_path.read_bytes()
    except _FS_ERRORS:
        return False
    try:
        marker = install_marker.read_install_marker(raw)
    except install_marker.InstallMarkerError:
        return False
    return marker.name == name


def freeze_publication_record(project_path: Path, home: Path) -> boundaries.BoundaryRecord:
    """Freeze the publication boundary record for one project and home."""

    return boundaries.freeze_boundaries(project_path, home)


def _managed_children(root: Path, *, subject: str) -> list[str]:
    """List one managed root's children, refusing on inspection failure."""

    try:
        info = root.lstat()
    except FileNotFoundError:
        return []
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} cannot be inspected: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{subject} is a link; newly introduced links are never followed",
        )
    if not stat.S_ISDIR(info.st_mode):
        raise SourceError(CODE_OUTPUT_OVERLAP, f"{subject} is not a directory")
    try:
        return sorted(child.name for child in root.iterdir())
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} cannot be listed: {exc}"
        ) from exc


def _referenced_runtime_keys(
    lock: SkillfileLock,
) -> set[tuple[str, str]]:
    """Return the runtime hex key pairs one lock references."""

    keys: set[tuple[str, str]] = set()
    for member in lock.members:
        package_key = package_identity_sha256(member.package)
        keys.add(
            (
                _key_hex(member.name, kind="skill name"),
                _key_hex(package_key, kind="package identity"),
            )
        )
    return keys


def runtime_hex_pairs(pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Hash (skill, package-key) pairs to runtime namespace components."""

    return {
        (
            _key_hex(skill, kind="skill name"),
            _key_hex(package_key, kind="package identity"),
        )
        for skill, package_key in pairs
    }


def _refuse_unless_absent_or_regular(path: Path, *, subject: str) -> None:
    """Refuse an adapter ledger live path that is not absent or regular.

    The shared adapters planner emits the ledger unconditionally and
    its reader maps every foreign shape to "no ledger", so without
    this check a directory (or link) at the ledger path would be moved
    aside and replaced, deleting user bytes. The schema-2 translation
    refuses those shapes here; the shared planner and the legacy lane
    are deliberately untouched.
    """

    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} cannot be inspected: {exc}"
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{subject} is a link; newly introduced links are never followed",
        )
    if not stat.S_ISREG(info.st_mode):
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{subject} is not a regular file and is never overwritten",
        )


def plan_schema2_targets(
    *,
    home: Path,
    project_path: Path,
    slug: str,
    alias: str,
    agents: list[str],
    members: tuple[ResolvedMember, ...],
    package_keys: dict[str, str],
    runtime_roots: dict[str, tuple[str, ...]],
    new_runtime_refs: set[tuple[str, str]],
    old_runtime_refs: set[tuple[str, str]],
) -> tuple[tuple[TargetSpec, ...], tuple[adapters.AdapterTarget, ...], list[str]]:
    """Plan every publication target for one schema-2 install.

    The plan covers machine bindings, the lock, one context target per
    member, one runtime target per member with runtime roots, adapter
    mirrors with their ledger, and removals for dropped skills and
    stale managed entries. The runtime removal set is the lock diff:
    keys the old lock referenced and the new lock does not. Nothing
    else is removable by an install; an entry referenced by no lock
    this project owns stays for ``gc``. The planner never lists the
    live home runtime tree and never reads another project's lock, so
    cleanup works with the source member directory deleted and never
    touches another installation. Unmanaged destinations refuse here,
    before any write.
    """

    messages: list[str] = []
    skills_root = project_path / ".agents" / "skills"
    member_names = {member.name for member in members}
    used_aliases = {member.from_alias for member in members}
    specs: list[TargetSpec] = []

    bindings_root = store.store_root(home) / BINDINGS_DIRNAME / slug
    for binding_alias in sorted(used_aliases):
        specs.append(
            TargetSpec(
                target_class=CLASS_BINDINGS,
                identifier=f"{slug}/{binding_alias}",
                live_path=binding_path(home, slug, binding_alias),
                kind="bytes",
                staged=None,
            )
        )
    for stale in _managed_children(bindings_root, subject=f"Bindings {bindings_root}"):
        if not stale.endswith(".json"):
            continue
        stale_alias = stale[: -len(".json")]
        if stale_alias in {member.from_alias for member in members}:
            continue
        if not identifiers.is_valid_identifier(stale_alias):
            continue
        specs.append(
            TargetSpec(
                target_class=CLASS_REMOVAL,
                identifier=f"bindings/{slug}/{stale_alias}",
                live_path=bindings_root / stale,
                kind="bytes",
                staged=None,
            )
        )

    specs.append(
        TargetSpec(
            target_class=CLASS_LOCK,
            identifier="Skillfile.lock.json",
            live_path=project_path / SKILLFILE_LOCK_NAME,
            kind="bytes",
            staged=None,
        )
    )

    for member in members:
        live = skills_root / member.name
        try:
            info = live.lstat()
        except FileNotFoundError:
            info = None
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"Skill {member.name!r} destination cannot be inspected: {exc}",
            ) from exc
        if info is not None:
            if stat.S_ISLNK(info.st_mode):
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Skill {member.name!r} destination is a link; newly "
                    "introduced links are never followed",
                )
            if not stat.S_ISDIR(info.st_mode):
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Skill {member.name!r} destination is not a directory",
                )
            validate_live_managed_tree(live, subject=f"Skill {member.name!r} destination")
            if not live_context_managed(live, member.name):
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Skill {member.name!r} destination is not managed by csk "
                    "and is never overwritten",
                )
        specs.append(
            TargetSpec(
                target_class=CLASS_CONTEXT,
                identifier=f"project/{member.name}",
                live_path=live,
                kind="entry",
                staged=None,
            )
        )
        if runtime_roots.get(member.name):
            specs.append(
                TargetSpec(
                    target_class=CLASS_RUNTIME,
                    identifier=f"{member.name}/{package_keys[member.name]}",
                    live_path=runtime_entry_path(home, member.name, package_keys[member.name]),
                    kind="entry",
                    staged=None,
                )
            )

    for stale in _managed_children(skills_root, subject=f"Context {skills_root}"):
        if stale in member_names or stale.startswith("."):
            continue
        live = skills_root / stale
        if not live_context_managed(live, stale):
            # Foreign to this installation: referenced by no lock this
            # project owns, addressed by no publication target, so it
            # is neither removed nor a conflict. Refusing here would
            # block every install on an unrelated sibling (on a
            # case-sensitive host a case variant is exactly such a
            # sibling); where the filesystem conflates the spelling
            # with a member destination, that destination's own check
            # above already refused.
            continue
        validate_live_managed_tree(live, subject=f"Stale skill {stale!r}")
        specs.append(
            TargetSpec(
                target_class=CLASS_REMOVAL,
                identifier=f"context/project/{stale}",
                live_path=live,
                kind="entry",
                staged=None,
            )
        )

    runtime_root = store.store_root(home) / RUNTIME_DIRNAME
    for skill_hex, package_hex in sorted(old_runtime_refs - new_runtime_refs):
        entry = runtime_root / skill_hex / package_hex
        try:
            entry_info = entry.lstat()
        except FileNotFoundError:
            continue
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"Runtime entry {skill_hex}/{package_hex} cannot be inspected: {exc}",
            ) from exc
        if stat.S_ISLNK(entry_info.st_mode):
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"Runtime entry {skill_hex}/{package_hex} is a link; newly "
                "introduced links are never followed",
            )
        if not stat.S_ISDIR(entry_info.st_mode):
            continue
        specs.append(
            TargetSpec(
                target_class=CLASS_REMOVAL,
                identifier=f"runtime/{skill_hex}/{package_hex}",
                live_path=entry,
                kind="entry",
                staged=None,
            )
        )

    try:
        adapter_targets = adapters.plan_project_adapter_targets(
            project_path,
            agents,
            [
                adapters.AdapterGroup(
                    canonical_root=skills_root,
                    skill_names=tuple(sorted(member_names)),
                )
            ],
        )
    except adapters.AdapterError as exc:
        raise SourceError(CODE_OUTPUT_OVERLAP, str(exc)) from exc
    for target in adapter_targets:
        if target.desired_kind == "ledger":
            _refuse_unless_absent_or_regular(
                target.live_path,
                subject=f"Adapter ledger {target.live_path}",
            )
        specs.append(
            TargetSpec(
                target_class=target.target_class,
                identifier=target.identifier,
                live_path=target.live_path,
                kind=target.kind,
                staged=None,
            )
        )

    keys = [(spec.target_class, spec.identifier) for spec in specs]
    if len(keys) != len(set(keys)):
        raise SourceError(
            CODE_MEMBER_INVALID, "schema-2 materialization plan has duplicate targets"
        )
    return tuple(specs), adapter_targets, messages


def used_source_aliases(members: tuple[ResolvedMember, ...]) -> tuple[str, ...]:
    """Return the sorted source aliases bindings are written for."""

    return tuple(sorted({member.from_alias for member in members}))


def _stat_identity(path: str, *, subject: str) -> os.stat_result:
    """stat one path through stable links, refusing failures as overlap.

    Following is safe here because the caller compares the resolved
    identity against the frozen one: a stable link root resolves to the
    frozen directory, while any swap or retarget resolves elsewhere and
    refuses. This matches the boundary recheck, which tolerates frozen
    link roots the same way.
    """

    try:
        return os.stat(path)
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} cannot be inspected: {exc}"
        ) from exc


def _freeze_managed_ancestors(
    record: boundaries.BoundaryRecord,
    root_spelling: str,
    components: list[str],
    *,
    subject: str,
) -> list[list[int]] | None:
    """Freeze the ancestor identities from the root down to a parent chain.

    Returns the top-down identities aligned with ``components``, or None
    when the managed root itself is a frozen link: link-valued roots
    defer to the boundary recheck, which resolves them authoritatively.
    Anything else unexpected (missing entries, new links, swapped types)
    refuses immediately, exactly as the recheck would.
    """

    root_stat = _stat_identity(root_spelling, subject=f"{subject} source root")
    if (root_stat.st_dev, root_stat.st_ino) != record.source_identity:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} source root changed since planning"
        )
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} source root is not a directory"
        )
    frozen: list[list[int]] = []
    current = root_spelling
    for depth, part in enumerate(components):
        probe = current + os.sep + part
        try:
            info = os.lstat(probe)
        except FileNotFoundError as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{subject} ancestor {part!r} does not exist",
            ) from exc
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP, f"{subject} cannot be inspected: {exc}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            if depth == 0:
                frozen_root = next(
                    (root for root in record.managed_roots if root.name == part), None
                )
                if frozen_root is not None and frozen_root.target is not None:
                    return None
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{subject} ancestor {part!r} is a link; newly introduced "
                "links are never followed",
            )
        if not stat.S_ISDIR(info.st_mode):
            raise SourceError(
                CODE_OUTPUT_OVERLAP, f"{subject} ancestor {part!r} is not a directory"
            )
        frozen.append([info.st_dev, info.st_ino])
        current = probe
    return frozen


def _freeze_home_ancestors(
    home_spelling: str,
    components: list[str],
    *,
    subject: str,
) -> list[list[int]]:
    """Freeze the ancestor identities below the csk home.

    Home-namespace chains never contain frozen links: any link refuses.
    Parents are created before freezing, so a missing entry is a
    concurrent change and refuses too.
    """

    frozen: list[list[int]] = []
    current = home_spelling
    for part in components:
        probe = current + os.sep + part
        try:
            info = os.lstat(probe)
        except FileNotFoundError as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{subject} ancestor {part!r} does not exist",
            ) from exc
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP, f"{subject} cannot be inspected: {exc}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{subject} ancestor {part!r} is a link; newly introduced "
                "links are never followed",
            )
        if not stat.S_ISDIR(info.st_mode):
            raise SourceError(
                CODE_OUTPUT_OVERLAP, f"{subject} ancestor {part!r} is not a directory"
            )
        frozen.append([info.st_dev, info.st_ino])
        current = probe
    return frozen


def _live_link_target(live: Path, *, subject: str) -> str | None:
    """Freeze the live readlink spelling, or None when not a link."""

    try:
        info = live.lstat()
    except FileNotFoundError:
        return None
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} cannot be inspected: {exc}"
        ) from exc
    if not stat.S_ISLNK(info.st_mode):
        return None
    try:
        return os.readlink(live)
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} link cannot be read: {exc}"
        ) from exc


def _staged_link_target(staged: Path | None, *, subject: str) -> str | None:
    """Freeze the staged readlink spelling, or None when not a link."""

    if staged is None:
        return None
    try:
        info = staged.lstat()
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} staged state cannot be read: {exc}"
        ) from exc
    if not stat.S_ISLNK(info.st_mode):
        return None
    try:
        return os.readlink(staged)
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} staged link cannot be read: {exc}"
        ) from exc


def build_recheck_payloads(
    *,
    record: boundaries.BoundaryRecord,
    project_path: Path,
    home: Path,
    staged_specs: tuple[TargetSpec, ...],
    live_digests: dict[tuple[str, str], str],
    admitted: frozenset[tuple[int, int]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Build one journal-carried recheck payload per publication target.

    Payloads are self-contained: the frozen record plus the plan-time
    live digest, ancestor identities and link spellings, so crash
    recovery rechecks with the same frozen answers. Project-tree
    destinations reuse the boundary recheck; the project-root lock and
    home-namespace entries carry the equivalent identity checks.
    """

    root_spelling = os.fspath(project_path)
    home_spelling = os.fspath(home)
    record_json = boundaries.boundary_record_to_json(record)
    admitted_json = [[dev, ino] for dev, ino in sorted(admitted)]
    payloads: dict[tuple[str, str], dict[str, Any]] = {}
    for spec in staged_specs:
        key = (spec.target_class, spec.identifier)
        subject = f"Publication destination {spec.live_path}"
        expected_live = live_digests[key]
        try:
            relative = spec.live_path.relative_to(project_path)
        except ValueError:
            relative = None
        if spec.target_class == CLASS_LOCK:
            payloads[key] = {
                "kind": _RECHECK_ROOT_FILE,
                "record": record_json,
                "root": root_spelling,
                "leaf": SKILLFILE_LOCK_NAME,
                "expected_live": expected_live,
            }
            continue
        if relative is not None and spec.target_class in {
            CLASS_CONTEXT,
            CLASS_ADAPTER_LEDGER,
            CLASS_REMOVAL,
        }:
            destination = relative.as_posix()
            parent_parts = destination.split("/")[:-1]
            planned_output = "/".join(parent_parts)
            ancestors = _freeze_managed_ancestors(
                record, root_spelling, parent_parts, subject=subject
            )
            payloads[key] = {
                "kind": _RECHECK_MANAGED,
                "record": record_json,
                "root": root_spelling,
                "planned_output": planned_output,
                "destination": destination,
                "expected_live": expected_live,
                "expected_link": _live_link_target(spec.live_path, subject=subject),
                "desired_link": _staged_link_target(spec.staged, subject=subject),
                "ancestors": ancestors,
                "admitted": admitted_json,
            }
            continue
        try:
            home_relative = spec.live_path.relative_to(home)
        except ValueError as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{subject} is neither under the project nor under the csk home",
            ) from exc
        home_parts = home_relative.parts
        if len(home_parts) < 2:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{subject} does not name a home-namespace entry",
            )
        ancestors = _freeze_home_ancestors(
            home_spelling, list(home_parts[:-1]), subject=subject
        )
        payloads[key] = {
            "kind": _RECHECK_HOME_ENTRY,
            "record": record_json,
            "home": home_spelling,
            "components": list(home_parts[:-1]),
            "leaf": home_parts[-1],
            "expected_live": expected_live,
            "ancestors": ancestors,
        }
    return payloads


def _payload_str(payload: dict[str, Any], field: str, *, subject: str) -> str:
    value = payload[field]
    if type(value) is not str:
        raise ValueError(f"{subject} field {field!r} must be a string")
    return value


def _payload_identities(
    payload: dict[str, Any], field: str, *, subject: str
) -> list[tuple[int, int]]:
    value = payload[field]
    if not isinstance(value, list):
        raise ValueError(f"{subject} field {field!r} must be a list")
    identities: list[tuple[int, int]] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(type(part) is int for part in item)
        ):
            raise ValueError(
                f"{subject} field {field!r}[{index}] must be an integer pair"
            )
        identities.append((item[0], item[1]))
    return identities


def _hook_live_digest(target: JournalTarget, *, subject: str) -> str:
    """Digest the live destination, mapping every failure to the boundary."""

    try:
        return digest_target(Path(target.live_path), kind=target.kind)
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{subject} changed since planning and cannot be verified: {exc}",
        ) from exc
    except TransactionError as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{subject} changed since planning and cannot be verified: {exc}",
        ) from exc


def _hook_require_recognized_state(
    target: JournalTarget, live_digest: str, expected_live: str, *, subject: str
) -> None:
    """Require the live destination in a state this transaction explains.

    A pending target was never written and a rolled-back target was
    restored, so in both states live must still be the plan-time
    preimage. A backed-up target was moved aside, so live must be
    absent or, on crash replay, already the desired bytes. A rollback
    interrupted after restoring live but before marking rolled_back
    resumes with live back at the preimage and no backup left; the
    engine re-marks it below. Anything else changed after planning
    and refuses. Ancestor and recheck verification still run after
    this return, so recognition never waives the boundary.
    """

    if target.state in {"pending", "rolled_back"}:
        recognized = live_digest == expected_live
    else:
        recognized = live_digest in {ABSENT_DIGEST, target.desired_digest}
        if not recognized and live_digest == expected_live:
            try:
                backup_digest = digest_target(
                    Path(target.backup_path), kind=target.kind
                )
            except (TransactionError, OSError, RuntimeError, ValueError, UnicodeError):
                backup_digest = None
            recognized = backup_digest == ABSENT_DIGEST
    if not recognized:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} changed since planning"
        )


def _hook_verify_ancestors(
    base_spelling: str,
    components: list[str],
    ancestors: list[tuple[int, int]],
    *,
    subject: str,
) -> None:
    """Re-walk a frozen ancestor chain, refusing links and swaps.

    Each step stats through the verified prefix only: a link refuses
    before it can redirect the walk, and an identity mismatch refuses a
    same-path directory replacement that string prefixes would miss.
    """

    if len(components) != len(ancestors):
        raise ValueError(f"{subject} ancestor chain does not match its path")
    current = base_spelling
    for part, frozen in zip(components, ancestors):
        probe = current + os.sep + part
        try:
            info = os.lstat(probe)
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP, f"{subject} cannot be inspected: {exc}"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{subject} ancestor {part!r} is a link; newly introduced "
                "links are never followed",
            )
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != frozen:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"{subject} ancestor {part!r} changed since planning",
            )
        current = probe


def _hook_managed(target: JournalTarget, payload: dict[str, Any], *, subject: str) -> None:
    record = boundaries.boundary_record_from_json(payload["record"])
    root = _payload_str(payload, "root", subject=subject)
    planned_output = _payload_str(payload, "planned_output", subject=subject)
    destination = _payload_str(payload, "destination", subject=subject)
    expected_live = _payload_str(payload, "expected_live", subject=subject)
    live_digest = _hook_live_digest(target, subject=subject)
    _hook_require_recognized_state(
        target, live_digest, expected_live, subject=subject
    )
    ancestors_raw = payload["ancestors"]
    if ancestors_raw is not None:
        if not isinstance(ancestors_raw, list):
            raise ValueError(f"{subject} ancestors must be a list or null")
        ancestors = _payload_identities(payload, "ancestors", subject=subject)
        parent_parts = destination.split("/")[:-1]
        _hook_verify_ancestors(root, parent_parts, ancestors, subject=subject)
    admitted = frozenset(_payload_identities(payload, "admitted", subject=subject))
    if live_digest == target.desired_digest and target.state != "pending":
        link_field = "desired_link"
    else:
        link_field = "expected_link"
    expected_link = payload[link_field]
    if expected_link is not None and type(expected_link) is not str:
        raise ValueError(f"{subject} link spelling must be a string or null")
    boundaries.recheck_publication_destination(
        record,
        Path(root),
        planned_output,
        destination,
        admitted=admitted,
        expected_link_target=expected_link,
    )


def _hook_root_file(target: JournalTarget, payload: dict[str, Any], *, subject: str) -> None:
    record = boundaries.boundary_record_from_json(payload["record"])
    root = _payload_str(payload, "root", subject=subject)
    leaf = _payload_str(payload, "leaf", subject=subject)
    expected_live = _payload_str(payload, "expected_live", subject=subject)
    root_stat = _stat_identity(root, subject=f"{subject} source root")
    if (root_stat.st_dev, root_stat.st_ino) != record.source_identity or not stat.S_ISDIR(
        root_stat.st_mode
    ):
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} source root changed since planning"
        )
    try:
        final = os.lstat(root + os.sep + leaf)
    except FileNotFoundError:
        final = None
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} cannot be inspected: {exc}"
        ) from exc
    if final is not None and stat.S_ISLNK(final.st_mode):
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{subject} is a link; newly introduced links are never followed",
        )
    live_digest = _hook_live_digest(target, subject=subject)
    _hook_require_recognized_state(
        target, live_digest, expected_live, subject=subject
    )


def _hook_home_entry(target: JournalTarget, payload: dict[str, Any], *, subject: str) -> None:
    record = boundaries.boundary_record_from_json(payload["record"])
    home = _payload_str(payload, "home", subject=subject)
    expected_live = _payload_str(payload, "expected_live", subject=subject)
    components_raw = payload["components"]
    if not isinstance(components_raw, list) or not all(
        type(part) is str for part in components_raw
    ):
        raise ValueError(f"{subject} components must be a list of strings")
    components: list[str] = list(components_raw)
    leaf = _payload_str(payload, "leaf", subject=subject)
    if record.home_identity is not None:
        home_stat = _stat_identity(home, subject=f"{subject} csk home")
        if (home_stat.st_dev, home_stat.st_ino) != record.home_identity or not stat.S_ISDIR(
            home_stat.st_mode
        ):
            raise SourceError(
                CODE_OUTPUT_OVERLAP, f"{subject} csk home changed since planning"
            )
    ancestors = _payload_identities(payload, "ancestors", subject=subject)
    _hook_verify_ancestors(home, components, ancestors, subject=subject)
    try:
        final = os.lstat(home + os.sep + os.sep.join([*components, leaf]))
    except FileNotFoundError:
        final = None
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP, f"{subject} cannot be inspected: {exc}"
        ) from exc
    if final is not None and stat.S_ISLNK(final.st_mode):
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"{subject} is a link; newly introduced links are never followed",
        )
    live_digest = _hook_live_digest(target, subject=subject)
    _hook_require_recognized_state(
        target, live_digest, expected_live, subject=subject
    )


def make_publication_hook() -> PreWriteHook:
    """Build the engine pre-write hook for schema-2 publication targets.

    The hook interprets the journal-carried payload per target: the
    live destination must be in a state this transaction explains, the
    frozen ancestor chain must be unchanged, and the boundary recheck
    must pass. Every boundary failure is ``source_output_overlap`` and
    the engine rolls back unpublished state. Targets without a payload
    proceed untouched, and malformed payloads fail as transaction
    errors, since journal corruption is not a boundary change.
    """

    def hook(target: JournalTarget) -> None:
        payload = target.publication_recheck
        if payload is None:
            return
        subject = (
            f"Publication destination {target.target_class}/{target.identifier}"
        )
        if not isinstance(payload, dict):
            raise TransactionError(f"{subject} recheck payload is not an object")
        kind = payload.get("kind")
        try:
            if kind == _RECHECK_MANAGED:
                _hook_managed(target, payload, subject=subject)
            elif kind == _RECHECK_ROOT_FILE:
                _hook_root_file(target, payload, subject=subject)
            elif kind == _RECHECK_HOME_ENTRY:
                _hook_home_entry(target, payload, subject=subject)
            else:
                raise TransactionError(
                    f"{subject} has an unknown publication recheck kind {kind!r}"
                )
        except SourceError:
            raise
        except TransactionError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise TransactionError(
                f"{subject} recheck payload is invalid: {exc}"
            ) from exc

    return hook


@dataclass(frozen=True)
class StagedDesired:
    """Staged desire for every planned target plus the lock and bindings."""

    members: dict[str, StagedMember]
    new_lock: SkillfileLock
    lock_bytes: bytes
    bindings: dict[str, bytes]
    desired: dict[tuple[str, str], Path | None]
    up_to_date: tuple[str, ...]


def _utc_now_stamp() -> str:
    """Return an install timestamp in marker format (UTC, no microseconds)."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def collect_admitted_identities(
    members: tuple[ResolvedMember, ...],
) -> frozenset[tuple[int, int]]:
    """Collect the admitted source-package identities for the recheck.

    A member directory that disappeared after capture is a concurrent
    mutation and fails ``source_snapshot_changed``; a member path that
    became a link is a boundary change and fails
    ``source_output_overlap``. Neither is ever treated as absent.
    """

    admitted: set[tuple[int, int]] = set()
    for member in members:
        path = member.source_root / member.directory
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise SourceError(
                CODE_SNAPSHOT_CHANGED,
                f"Skill {member.name!r} disappeared after capture; run an "
                "explicit refresh",
            ) from exc
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"Skill {member.name!r} source cannot be inspected: {exc}",
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"Skill {member.name!r} source is a link; newly introduced "
                "links are never followed",
            )
        if not stat.S_ISDIR(info.st_mode):
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"Skill {member.name!r} source is not a directory",
            )
        admitted.add((info.st_dev, info.st_ino))
    return frozenset(admitted)


def digest_live_targets(
    specs: tuple[TargetSpec, ...],
) -> dict[tuple[str, str], str]:
    """Digest every live destination, mapping links to the boundary.

    An unsafe tree (links where none belong) refuses as a boundary
    change here, before the engine, so the refusal carries the
    ``source_output_overlap`` code instead of a preimage failure.
    """

    digests: dict[tuple[str, str], str] = {}
    for spec in specs:
        key = (spec.target_class, spec.identifier)
        try:
            digests[key] = digest_target(spec.live_path, kind=spec.kind)
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"Publication destination {spec.live_path} cannot be "
                f"inspected: {exc}",
            ) from exc
        except TransactionError:
            validate_live_managed_tree(
                spec.live_path,
                subject=f"Publication destination {spec.live_path}",
            )
            raise
    return digests


def ensure_live_parents(
    specs: tuple[TargetSpec, ...], *, staged: dict[tuple[str, str], Path | None]
) -> list[Path]:
    """Create every live parent directory, tracking what was created.

    Parents are created after the locks are held and before payloads
    freeze, so ancestor identities are authoritative. A link or a
    non-directory parent refuses; created directories are returned for
    removal when publication fails.
    """

    created: list[Path] = []
    for spec in specs:
        if staged.get((spec.target_class, spec.identifier)) is None:
            continue
        parent = spec.live_path.parent
        try:
            info = parent.lstat()
        except FileNotFoundError:
            info = None
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"Publication parent {parent} cannot be inspected: {exc}",
            ) from exc
        if info is not None:
            if stat.S_ISLNK(info.st_mode):
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Publication parent {parent} is a link; newly introduced "
                    "links are never followed",
                )
            if not stat.S_ISDIR(info.st_mode):
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Publication parent {parent} is not a directory",
                )
            continue
        chain: list[Path] = []
        probe: Path | None = parent
        while probe is not None:
            try:
                probe_info = probe.lstat()
            except FileNotFoundError:
                chain.append(probe)
                probe = probe.parent if probe.parent != probe else None
                continue
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Publication parent {probe} cannot be inspected: {exc}",
                ) from exc
            if stat.S_ISLNK(probe_info.st_mode):
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Publication parent {probe} is a link; newly introduced "
                    "links are never followed",
                )
            if not stat.S_ISDIR(probe_info.st_mode):
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Publication parent {probe} is not a directory",
                )
            break
        for directory in reversed(chain):
            try:
                directory.mkdir(mode=0o755)
            except FileExistsError:
                try:
                    raced = directory.lstat()
                except _FS_ERRORS as exc:
                    raise SourceError(
                        CODE_OUTPUT_OVERLAP,
                        f"Publication parent {directory} cannot be inspected: {exc}",
                    ) from exc
                if stat.S_ISLNK(raced.st_mode) or not stat.S_ISDIR(raced.st_mode):
                    raise SourceError(
                        CODE_OUTPUT_OVERLAP,
                        f"Publication parent {directory} is not a real directory",
                    )
                continue
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Publication parent {directory} cannot be created: {exc}",
                ) from exc
            created.append(directory)
    return created


def remove_created_parents(created: list[Path]) -> None:
    """Remove created parent directories, deepest first, best effort."""

    for directory in reversed(created):
        try:
            directory.rmdir()
        except _FS_ERRORS:
            continue


def _marker_reusable(
    live: Path,
    plan: install_marker.MarkerPlan,
    content_sha256: str,
    *,
    name: str,
) -> bool:
    """Return whether the live marker already matches the staged plan.

    Reuse keeps the live bytes (including the install timestamp) so an
    unchanged install stages a digest-identical tree and the target
    skips publication. Only a schema-5 marker that compares equal on
    every compared member with a matching live content hash reuses;
    anything else stages fresh.
    """

    marker_path = live / ".csk-install.json"
    try:
        raw = marker_path.read_bytes()
    except _FS_ERRORS:
        return False
    try:
        marker = install_marker.read_install_marker(raw)
    except install_marker.InstallMarkerError:
        return False
    if not isinstance(marker, install_marker.InstallMarkerV5):
        return False
    if install_marker.compare_marker_plan(marker, plan):
        return False
    validate_live_managed_tree(live, subject=f"Skill {name!r} destination")
    try:
        live_content = hashing.content_sha256(live)
    except hashing.HashingError:
        return False
    except _FS_ERRORS:
        return False
    return live_content == content_sha256


def stage_schema2_desired(
    staging_root: Path,
    *,
    home: Path,
    project_path: Path,
    alias: str,
    agents: tuple[str, ...],
    locale_value: str | None,
    adapter_mode: str,
    manifest_data: dict[str, Any],
    members: tuple[ResolvedMember, ...],
    captured: dict[str, CapturedMember],
    frozen_files: dict[str, Mapping[str, snapshot.FrozenFile]],
    specs_map: dict[str, skillspec.SkillSpec],
    source_roots: dict[str, Path],
    specs: tuple[TargetSpec, ...],
    adapter_targets: tuple[adapters.AdapterTarget, ...],
    slug: str,
) -> StagedDesired:
    """Stage the desired bytes for every planned target.

    Context is projected from frozen bytes, hashed, and bound into a
    new lock created only after all gates succeed; markers are written
    into the staged contexts, runtime entries and bindings are staged,
    and adapters mirror the staged canonical roots. Members whose live
    state already matches reuse the live bytes so the target skips.
    """

    staged_members: dict[str, StagedMember] = {}
    staged_skills = staging_root / "skills"
    staged_skills.mkdir(parents=True, exist_ok=True)
    member_by_name = {member.name: member for member in members}
    for name in sorted(captured):
        member = member_by_name[name]
        frozen_dir = staging_root / "frozen" / name
        materialize_frozen(
            frozen_files[name], frozen_dir, subject=f"Skill {name!r}"
        )
        spec = specs_map[name]
        context_dir = staged_skills / name
        files, content = stage_member_context(
            frozen_dir, context_dir, spec, name=name, locale_value=locale_value
        )
        staged_members[name] = StagedMember(
            captured=captured[name],
            spec=spec,
            content_sha256=content,
            files=tuple(files),
            context_dir=context_dir,
        )

    lock_members = [
        LockMember(
            name=name,
            selection=member_by_name[name].selection_ordinal,
            directory=member_by_name[name].directory,
            package=captured[name].package,
            content_sha256=staged_members[name].content_sha256,
        )
        for name in sorted(captured)
    ]
    try:
        new_lock = create_lock(manifest_data, lock_members)
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Lock for {alias} cannot be created: {exc}"
        ) from exc
    try:
        lock_bytes = serialize_lock(new_lock)
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Lock for {alias} cannot be serialized: {exc}"
        ) from exc

    if new_lock.lock_sha256 is None:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Lock for {alias} has no lock digest"
        )
    lock_digest = new_lock.lock_sha256
    installed_at = _utc_now_stamp()
    up_to_date: list[str] = []
    for name in sorted(staged_members):
        staged = staged_members[name]
        plan = build_marker_plan(
            name=name,
            package=staged.captured.package,
            lock_sha256=lock_digest,
            content_sha256=staged.content_sha256,
            spec=staged.spec,
            files=staged.files,
            agents=agents,
            locale_value=locale_value,
        )
        live = project_path / ".agents" / "skills" / name
        if _marker_reusable(live, plan, staged.content_sha256, name=name):
            try:
                if staged.context_dir.exists():
                    shutil.rmtree(staged.context_dir)
                shutil.copytree(live, staged.context_dir, symlinks=True)
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill {name!r} installed state cannot be restaged: {exc}",
                ) from exc
            up_to_date.append(name)
            continue
        marker = build_marker(plan, installed_at=installed_at)
        try:
            payload = install_marker.serialize_install_marker(marker.to_json())
        except (TypeError, ValueError, OSError, RuntimeError, UnicodeError) as exc:
            raise SourceError(
                CODE_MEMBER_INVALID, f"Skill {name!r} marker cannot be rendered: {exc}"
            ) from exc
        try:
            (staged.context_dir / ".csk-install.json").write_bytes(payload)
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_MEMBER_INVALID, f"Skill {name!r} marker cannot be staged: {exc}"
            ) from exc

    bindings: dict[str, bytes] = {}
    for binding_alias in sorted({member.from_alias for member in members}):
        binding_root = source_roots[binding_alias]
        try:
            location = os.path.realpath(os.fspath(binding_root))
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"Machine binding for {binding_alias!r} cannot resolve its source: {exc}",
            ) from exc
        try:
            binding = MachinePrivateBinding(source_location=location, root_inputs=())
            bindings[binding_alias] = serialize_machine_private_binding(binding)
        except SourceError:
            raise
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"Machine binding for {binding_alias!r} cannot be rendered: {exc}",
            ) from exc

    desired: dict[tuple[str, str], Path | None] = {}
    by_key = {(planned.target_class, planned.identifier): planned for planned in specs}
    for key, planned_spec in by_key.items():
        if planned_spec.target_class == CLASS_REMOVAL:
            desired[key] = None
    lock_staged = staging_root / SKILLFILE_LOCK_NAME
    try:
        lock_staged.write_bytes(lock_bytes)
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Lock for {alias} cannot be staged: {exc}"
        ) from exc
    desired[(CLASS_LOCK, "Skillfile.lock.json")] = lock_staged
    bindings_root = staging_root / "bindings"
    bindings_root.mkdir(parents=True, exist_ok=True)
    for binding_alias, binding_bytes in bindings.items():
        staged_binding = bindings_root / f"{binding_alias}.json"
        try:
            staged_binding.write_bytes(binding_bytes)
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"Machine binding for {binding_alias!r} cannot be staged: {exc}",
            ) from exc
        desired[(CLASS_BINDINGS, f"{slug}/{binding_alias}")] = staged_binding
    for name, staged in staged_members.items():
        desired[(CLASS_CONTEXT, f"project/{name}")] = staged.context_dir
        if staged.spec.runtime_roots:
            runtime_staged = staging_root / "runtime" / name
            try:
                if runtime_staged.exists():
                    shutil.rmtree(runtime_staged)
                runtime_staged.mkdir(parents=True)
                frozen_dir = staging_root / "frozen" / name
                for root in staged.spec.runtime_roots:
                    shutil.copytree(frozen_dir / root, runtime_staged / root, symlinks=True)
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill {name!r} runtime cannot be staged: {exc}",
                ) from exc
            desired[(CLASS_RUNTIME, f"{name}/{captured[name].package_key}")] = runtime_staged
    desired.update(
        adapters.stage_project_adapter_targets(
            staging_root / "adapters",
            adapter_targets,
            source_roots={project_path / ".agents" / "skills": staged_skills},
            mode=adapter_mode,
        )
    )
    for key in by_key:
        if key not in desired:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Target {key[0]}/{key[1]} has no staged state",
            )
    return StagedDesired(
        members=staged_members,
        new_lock=new_lock,
        lock_bytes=lock_bytes,
        bindings=bindings,
        desired=desired,
        up_to_date=tuple(sorted(up_to_date)),
    )


def _read_fresh_manifest(
    project_path: Path,
) -> tuple[dict[str, Any], manifest.ProjectManifest]:
    """Re-read and re-parse the Skillfile, mirroring the manifest loader."""

    path = project_path / manifest.MANIFEST_NAME
    try:
        raw = path.read_bytes()
    except _FS_ERRORS as exc:
        raise manifest.ManifestError(f"Skillfile cannot be read: {exc}") from exc
    try:
        data = protocol_json.loads(raw)
    except protocol_json.ProtocolJSONError as exc:
        raise manifest.ManifestError(f"Malformed JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise manifest.ManifestError(f"{path} must contain a JSON object")
    parsed = manifest.parse_manifest(data, path, allow_schema_2=True, scope="project")
    return data, parsed


def _concurrent_state_change(detail: str) -> build_planner.BuildPlanningError:
    return build_planner.BuildPlanningError("concurrent_state_change", detail)


def _capture_locked_member(
    member: ResolvedMember,
    package: LocalSnapshot,
    *,
    home: Path,
    heal: bool,
) -> tuple[snapshot.CapturedPackage | None, store.StoredSnapshot | None]:
    """Capture one locked member or serve its locked snapshot from the store.

    A successful capture must digest-match the lock or the install
    refuses ``source_snapshot_changed`` without writing anything, and
    the locked snapshot is never silently recreated from current
    bytes. When live bytes cannot be captured, the verified store copy
    serves instead; when neither serves, the snapshot is unavailable.
    With ``heal`` false (dry run) the store is never written.
    """

    try:
        captured = snapshot.capture_package_snapshot(
            member.source_root, member.directory, home=home
        )
    except SourceError as capture_error:
        return _serve_locked_fallback(member, package, home=home, cause=capture_error)
    except _FS_ERRORS as capture_error:
        return _serve_locked_fallback(member, package, home=home, cause=capture_error)
    if captured.inventory["snapshot"] != package.snapshot:
        raise SourceError(
            CODE_SNAPSHOT_CHANGED,
            f"Skill {member.name!r} changed since the lock was written; "
            "run an explicit refresh",
        )
    if not heal:
        return captured, None
    key = package_identity_sha256(package)
    try:
        stored = consumers.open_for_install(home, member.name, key)
    except SourceError:
        stored = None
    if stored is not None and stored.snapshot == package.snapshot:
        return captured, stored
    healed = store.stage_snapshot(home, member.name, key, captured)
    if healed.snapshot != package.snapshot:
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"Skill {member.name!r} locked snapshot cannot be served",
        )
    return captured, healed


def _serve_locked_fallback(
    member: ResolvedMember,
    package: LocalSnapshot,
    *,
    home: Path,
    cause: BaseException,
) -> tuple[None, store.StoredSnapshot]:
    """Serve one locked member from the store when live capture fails."""

    key = package_identity_sha256(package)
    try:
        stored = consumers.open_for_install(home, member.name, key)
    except SourceError as exc:
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"Skill {member.name!r} locked snapshot is unavailable: {cause}",
        ) from exc
    if stored.snapshot != package.snapshot:
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"Skill {member.name!r} locked snapshot cannot be served: {cause}",
        ) from cause
    return None, stored


def _commit_schema2(
    engine: TransactionEngine,
    home_lock: locking.ManagerHomeLock,
    *,
    transaction_id: str,
    project_identity: str,
    specs: tuple[TargetSpec, ...],
    desired: dict[tuple[str, str], Path | None],
    expected: dict[tuple[str, str], str],
    payloads: dict[tuple[str, str], dict[str, Any]],
    created_parents: list[Path],
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """Commit one schema-2 publication transaction, skipping identical targets.

    Returns the committed and skipped target keys. Identical targets
    never enter the transaction, so an unchanged install publishes
    nothing. Live parents are already created by the caller; they are
    removed again when publication fails.
    """

    targets: list[MutableTarget] = []
    skipped: list[tuple[str, str]] = []
    for spec in specs:
        key = (spec.target_class, spec.identifier)
        wanted = desired[key]
        wanted_digest = ABSENT_DIGEST
        if wanted is not None:
            try:
                wanted_digest = digest_target(wanted, kind=spec.kind)
            except TransactionError:
                validate_live_managed_tree(
                    spec.live_path,
                    subject=f"Publication destination {spec.live_path}",
                )
                raise
        if wanted_digest == expected[key]:
            skipped.append(key)
            continue
        targets.append(
            MutableTarget(
                target_class=spec.target_class,
                identifier=spec.identifier,
                live_path=spec.live_path,
                desired_path=wanted,
                expected_preimage_digest=expected[key],
                kind=spec.kind,
                publication_recheck=payloads[key],
            )
        )
    if not targets:
        return (), tuple(skipped)
    committed = False
    try:
        plan = TransactionPlan(
            transaction_id=transaction_id,
            project_identity=project_identity,
            targets=tuple(targets),
        )
        engine.prepare(home_lock, plan)
        engine.commit(home_lock, transaction_id)
        committed = True
    except ExceptionGroup as group:
        raise _fold_engine_group(group) from group
    finally:
        if not committed:
            remove_created_parents(created_parents)
    return tuple((target.target_class, target.identifier) for target in targets), tuple(skipped)


def _fold_engine_group(group: ExceptionGroup) -> BaseException:
    """Fold a same-condition engine group into its single diagnostic.

    When commit and rollback fail on one moved boundary, every leaf
    carries the same source code; the operator gets that code once,
    with the deferred rollback recorded, instead of an opaque group.
    Mixed leaves stay grouped: genuinely distinct failures remain
    observable.
    """

    leaves: list[BaseException] = []

    def collect(error: BaseException) -> None:
        if isinstance(error, BaseExceptionGroup):
            for sub in error.exceptions:
                collect(sub)
        else:
            leaves.append(error)

    collect(group)
    sources = [leaf for leaf in leaves if isinstance(leaf, SourceError)]
    if sources and len(sources) == len(leaves) and len({item.code for item in sources}) == 1:
        return SourceError(
            sources[0].code,
            f"{sources[0].detail} (rollback is deferred while the "
            "boundary is moved; restore it and rerun csk install to recover)",
        )
    return group


def install_schema2(
    *,
    home: Path,
    project_path: Path,
    alias: str,
    agents: list[str],
    locale_value: str | None,
    adapter_mode: str,
    fetch: bool,
    dry_run: bool,
) -> Schema2InstallResult:
    """Install one schema-2 project: resolve, freeze, publish atomically.

    Initial install creates the lock after all gates succeed; locked
    install consumes the locked snapshots and refuses drift instead of
    re-resolving; explicit refresh re-runs every gate and atomically
    replaces the lock and markers only after success. Every failure
    preserves the prior lock, markers and installed state.
    """

    project_identity = locking.canonical_project_identity(project_path)
    slug = project_slug(project_identity)
    manifest_data, fresh = _read_fresh_manifest(project_path)
    if fresh.schema_version != 2 or fresh.manifest_sha256 is None:
        raise _concurrent_state_change("Skillfile changed during install planning")
    manifest_sha = fresh.manifest_sha256
    old_lock = read_schema2_lock(project_path)
    refresh = fetch and old_lock is not None

    source_roots = {
        source_alias: resolve_source_root(project_path, source_alias, acquisition)
        for source_alias, acquisition in fresh.sources.items()
    }
    selectors = list(fresh.selectors)

    captured_members: dict[str, CapturedMember] = {}
    pred_files: dict[str, Mapping[str, snapshot.FrozenFile]] = {}
    frozen_membership: dict[int, tuple[str, ...]] | None = None
    if old_lock is not None and not fetch:
        validate_lock(old_lock, current_manifest_sha256=manifest_sha)
        members = locked_schema2_members(old_lock, selectors, source_roots)
        lock_packages = {
            lock_member.name: lock_member.package
            for lock_member in old_lock.members
            if isinstance(lock_member.package, LocalSnapshot)
        }
        for member in members:
            package = lock_packages[member.name]
            raw, served = _capture_locked_member(
                member, package, home=home, heal=not dry_run
            )
            captured_members[member.name] = CapturedMember(
                member=member,
                captured=raw,
                package=package,
                package_key=package_identity_sha256(package),
            )
            if raw is not None:
                pred_files[member.name] = raw.frozen_files()
            elif served is not None:
                pred_files[member.name] = served.files
            else:
                raise SourceError(
                    CODE_SNAPSHOT_UNAVAILABLE,
                    f"Skill {member.name!r} locked snapshot cannot be served",
                )
    else:
        members = resolve_schema2_members(project_path, fresh)
        frozen_membership = collection_membership(selectors, source_roots)
        raw_captures = capture_schema2_members(members, home=home)
        require_membership_unchanged(
            frozen_membership, collection_membership(selectors, source_roots)
        )
        for member in members:
            raw = raw_captures[member.name]
            package = LocalSnapshot(snapshot=raw.inventory["snapshot"])
            captured_members[member.name] = CapturedMember(
                member=member,
                captured=raw,
                package=package,
                package_key=package_identity_sha256(package),
            )
            pred_files[member.name] = raw.frozen_files()
        if not dry_run:
            stage_schema2_store(home, captured_members)

    with tempfile.TemporaryDirectory(prefix=".csk-source-install-") as staging_tmp:
        staging_root = Path(staging_tmp)
        specs_map: dict[str, skillspec.SkillSpec] = {}
        for name in sorted(pred_files):
            frozen_dir = staging_root / "predspec" / name
            materialize_frozen(
                pred_files[name],
                frozen_dir,
                subject=f"Skill {name!r}",
            )
            spec = load_member_spec(frozen_dir, name=name)
            require_context_only(spec, name=name)
            specs_map[name] = spec

        package_keys = {name: item.package_key for name, item in captured_members.items()}
        runtime_roots_map = {
            name: specs_map[name].runtime_roots for name in captured_members
        }
        new_runtime_refs = runtime_hex_pairs(
            {(name, package_keys[name]) for name in captured_members}
        )
        old_runtime_refs = (
            _referenced_runtime_keys(old_lock) if old_lock is not None else set()
        )
        planned, adapter_targets, plan_messages = plan_schema2_targets(
            home=home,
            project_path=project_path,
            slug=slug,
            alias=alias,
            agents=agents,
            members=members,
            package_keys=package_keys,
            runtime_roots=runtime_roots_map,
            new_runtime_refs=new_runtime_refs,
            old_runtime_refs=old_runtime_refs,
        )
        try:
            preimages = digest_live_targets(planned)
        except TransactionError as exc:
            raise _concurrent_state_change(
                "shared install state changed before the atomic commit"
            ) from exc

        if dry_run:
            messages = list(plan_messages)
            for member in members:
                messages.append(f"{alias}: {member.name} (planned)")
            messages.append(f"{alias}: dry-run; no files modified")
            return Schema2InstallResult(
                messages=tuple(messages),
                installed=(),
                up_to_date=(),
                removed=(),
                lock_replaced=False,
                transaction_id=None,
            )

        with locking.ManagerHomeLock(home) as home_lock:
            engine = TransactionEngine(home, pre_write_hook=make_publication_hook())
            engine.recover(home_lock)
            try:
                current = digest_live_targets(planned)
            except TransactionError as exc:
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Install destinations changed before the atomic commit: {exc}",
                ) from exc
            if current != preimages:
                raise _concurrent_state_change(
                    "shared install state changed before the atomic commit"
                )
            # The manifest is re-read under the lock; the hash must match
            # the planning read or the plan restarts. Members, selectors
            # and source roots below are the planning-read values, valid
            # because the hashes are equal.
            manifest_data, reread = _read_fresh_manifest(project_path)
            if reread.schema_version != 2 or reread.manifest_sha256 != manifest_sha:
                raise _concurrent_state_change("Skillfile changed during install")
            # Locked installs consume the frozen lock membership without
            # expanding selectors, so only fresh resolves re-enumerate.
            if frozen_membership is not None:
                require_membership_unchanged(
                    frozen_membership,
                    collection_membership(selectors, source_roots),
                )
            admitted = collect_admitted_identities(members)

            # Publication consumes the frozen store copy, rehashed here.
            # A superseded record heals from verified captured bytes.
            frozen_files: dict[str, Mapping[str, snapshot.FrozenFile]] = {}
            for name, captured_member in captured_members.items():
                served = consumers.open_for_install(
                    home, name, captured_member.package_key
                )
                if served.snapshot == captured_member.package.snapshot:
                    frozen_files[name] = served.files
                    continue
                if captured_member.captured is None:
                    raise SourceError(
                        CODE_SNAPSHOT_UNAVAILABLE,
                        f"Skill {name!r} locked snapshot cannot be served",
                    )
                healed = store.stage_snapshot(
                    home,
                    name,
                    captured_member.package_key,
                    captured_member.captured,
                )
                if healed.snapshot != captured_member.package.snapshot:
                    raise SourceError(
                        CODE_SNAPSHOT_UNAVAILABLE,
                        f"Skill {name!r} locked snapshot cannot be served",
                    )
                frozen_files[name] = healed.files

            staged = stage_schema2_desired(
                staging_root,
                home=home,
                project_path=project_path,
                alias=alias,
                agents=tuple(agents),
                locale_value=locale_value,
                adapter_mode=adapter_mode,
                manifest_data=manifest_data,
                members=members,
                captured=captured_members,
                frozen_files=frozen_files,
                specs_map=specs_map,
                source_roots=source_roots,
                specs=planned,
                adapter_targets=adapter_targets,
                slug=slug,
            )
            created_parents = ensure_live_parents(planned, staged=staged.desired)
            # The record freezes after our own parents exist, so the
            # frozen managed bindings already include them; anything
            # that changes afterwards refuses at write time.
            record = freeze_publication_record(project_path, home)
            try:
                live_digests = digest_live_targets(planned)
            except TransactionCorruptionError as exc:
                remove_created_parents(created_parents)
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Install destinations changed before the atomic commit: {exc}",
                ) from exc
            payloads = build_recheck_payloads(
                record=record,
                project_path=project_path,
                home=home,
                staged_specs=tuple(
                    TargetSpec(
                        target_class=spec.target_class,
                        identifier=spec.identifier,
                        live_path=spec.live_path,
                        kind=spec.kind,
                        staged=staged.desired[(spec.target_class, spec.identifier)],
                    )
                    for spec in planned
                ),
                live_digests=live_digests,
                admitted=admitted,
            )
            transaction_id = f"{TRANSACTION_PREFIX}-{slug}-{uuid.uuid4().hex}"
            try:
                committed, _skipped = _commit_schema2(
                    engine,
                    home_lock,
                    transaction_id=transaction_id,
                    project_identity=project_identity,
                    specs=planned,
                    desired=staged.desired,
                    expected=live_digests,
                    payloads=payloads,
                    created_parents=created_parents,
                )
            except BaseException:
                # _commit_schema2 already removed created parents on failure;
                # a failure here (including payload faults injected above)
                # must not leave them behind either.
                remove_created_parents(created_parents)
                raise

    installed = _touched_members(committed)
    removed = _removed_names(old_lock, staged.new_lock)
    up_to_date = tuple(
        sorted(set(captured_members) - set(installed) - set(removed))
    )
    lock_replaced = (CLASS_LOCK, "Skillfile.lock.json") in committed
    messages = list(plan_messages)
    for name in sorted(installed):
        messages.append(f"{alias}: {name} installed")
    for name in up_to_date:
        messages.append(f"{alias}: {name} up-to-date")
    for name in removed:
        messages.append(f"{alias}: {name} removed")
    if lock_replaced:
        messages.append(
            f"{alias}: lock {'replaced' if old_lock is not None else 'created'}"
        )
    if any(key[0] == CLASS_BINDINGS for key in committed):
        messages.append(f"{alias}: machine bindings updated")
    if any(key[0] == CLASS_ADAPTER_LEDGER for key in committed):
        messages.append(f"{alias}: adapters updated")
    if refresh:
        messages.extend(
            _binding_move_messages(
                home=home, slug=slug, alias=alias, members=members, source_roots=source_roots
            )
        )
    return Schema2InstallResult(
        messages=tuple(messages),
        installed=tuple(sorted(installed)),
        up_to_date=up_to_date,
        removed=removed,
        lock_replaced=lock_replaced,
        transaction_id=transaction_id if committed else None,
    )


def _removed_names(
    old_lock: SkillfileLock | None, new_lock: SkillfileLock
) -> tuple[str, ...]:
    if old_lock is None:
        return ()
    old_names = {member.name for member in old_lock.members}
    new_names = {member.name for member in new_lock.members}
    return tuple(sorted(old_names - new_names))


def _touched_members(
    committed: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    """Map committed target keys to the member names they publish."""

    touched: set[str] = set()
    for target_class, identifier in committed:
        if target_class == CLASS_CONTEXT and identifier.startswith("project/"):
            touched.add(identifier[len("project/"):])
        elif target_class == CLASS_RUNTIME and "/" in identifier:
            touched.add(identifier.split("/", 1)[0])
        elif target_class == CLASS_ADAPTER_LEDGER and "/entry/" in identifier:
            touched.add(identifier.split("/entry/", 1)[1])
    return tuple(sorted(touched))


def _binding_move_messages(
    *,
    home: Path,
    slug: str,
    alias: str,
    members: tuple[ResolvedMember, ...],
    source_roots: dict[str, Path],
) -> list[str]:
    """Report source relocations on refresh; bindings are diagnostic only."""

    messages: list[str] = []
    for source_alias in sorted({member.from_alias for member in members}):
        try:
            location = os.path.realpath(os.fspath(source_roots[source_alias]))
        except _FS_ERRORS:
            continue
        try:
            prior = read_machine_private_binding(
                binding_path(home, slug, source_alias).read_bytes()
            )
            prior_location = prior.canonical_source_location
        except SourceError:
            continue
        except _FS_ERRORS:
            continue
        if prior_location != location:
            messages.append(
                f"{alias}: source {source_alias!r} moved {prior_location} -> "
                f"{location}; re-resolved"
            )
    return messages


def _marker_snapshot(marker_path: Path) -> str | None:
    """Read the installed snapshot a marker claims, or None when unusable."""

    try:
        raw = marker_path.read_bytes()
    except _FS_ERRORS:
        return None
    try:
        marker = install_marker.read_install_marker(raw)
    except install_marker.InstallMarkerError:
        return None
    if not isinstance(marker, install_marker.InstallMarkerV5):
        return None
    if not isinstance(marker.package, LocalSnapshot):
        return None
    return marker.package.snapshot


def _evaluate_locked_member(
    staging_root: Path,
    *,
    home: Path,
    project_path: Path,
    lock_member: LockMember,
    lock_sha256: str,
    source_root: Path,
    agents: tuple[str, ...],
    locale_value: str | None,
) -> MemberVerdict:
    """Evaluate one lock member against live source and installed marker.

    The live source is re-captured at the locked directory (frozen, no
    collection expansion) and must digest-match the lock; the plan is
    then derived from those verified bytes and the installed marker is
    compared against it. Marker summaries never authorize currency:
    the plan comes from the lock and the re-captured bytes alone.
    Read-only: staging stays in the system temporary directory.
    """

    name = lock_member.name
    if not isinstance(lock_member.package, LocalSnapshot):
        return MemberVerdict(
            name=name,
            current=False,
            label="error",
            detail=f"Skill {name!r} lock member is not a local snapshot",
            locked_snapshot=None,
            current_snapshot=None,
            marker_snapshot=_marker_snapshot(
                project_path / ".agents" / "skills" / name / ".csk-install.json"
            ),
        )
    locked_snapshot = lock_member.package.snapshot
    try:
        captured = snapshot.capture_package_snapshot(
            source_root, lock_member.directory, home=home
        )
    except SourceError as exc:
        return MemberVerdict(
            name=name,
            current=False,
            label="snapshot-unavailable",
            detail=f"Skill {name!r} locked source cannot be revalidated: {exc}",
            locked_snapshot=locked_snapshot,
            current_snapshot=None,
            marker_snapshot=_marker_snapshot(
                project_path / ".agents" / "skills" / name / ".csk-install.json"
            ),
        )
    except _FS_ERRORS as exc:
        return MemberVerdict(
            name=name,
            current=False,
            label="snapshot-unavailable",
            detail=f"Skill {name!r} locked source cannot be revalidated: {exc}",
            locked_snapshot=locked_snapshot,
            current_snapshot=None,
            marker_snapshot=_marker_snapshot(
                project_path / ".agents" / "skills" / name / ".csk-install.json"
            ),
        )
    current_snapshot = captured.inventory["snapshot"]
    marker_path = project_path / ".agents" / "skills" / name / ".csk-install.json"
    claimed = _marker_snapshot(marker_path)
    if current_snapshot != locked_snapshot:
        return MemberVerdict(
            name=name,
            current=False,
            label="source-changed",
            detail=(
                f"Skill {name!r} changed since the lock was written; "
                "run an explicit refresh"
            ),
            locked_snapshot=locked_snapshot,
            current_snapshot=current_snapshot,
            marker_snapshot=claimed,
        )
    if not marker_path.exists():
        return MemberVerdict(
            name=name,
            current=False,
            label="missing-marker",
            detail=f"Skill {name!r} install marker is missing",
            locked_snapshot=locked_snapshot,
            current_snapshot=current_snapshot,
            marker_snapshot=None,
        )
    try:
        frozen_dir = staging_root / "status" / name
        materialize_frozen(
            captured.frozen_files(), frozen_dir, subject=f"Skill {name!r}"
        )
        spec = load_member_spec(frozen_dir, name=name)
        context_dir = staging_root / "status-context" / name
        files, content = stage_member_context(
            frozen_dir, context_dir, spec, name=name, locale_value=locale_value
        )
        plan = build_marker_plan(
            name=name,
            package=lock_member.package,
            lock_sha256=lock_sha256,
            content_sha256=content,
            spec=spec,
            files=tuple(files),
            agents=agents,
            locale_value=locale_value,
        )
    except SourceError as exc:
        return MemberVerdict(
            name=name,
            current=False,
            label="error",
            detail=str(exc),
            locked_snapshot=locked_snapshot,
            current_snapshot=current_snapshot,
            marker_snapshot=claimed,
        )
    try:
        verdict = install_marker.evaluate_schema2_status(
            marker_path,
            plan,
            evidence_path=None,
            evidence_fresh=False,
            evidence_revoked=False,
        )
    except install_marker.InstallMarkerError as exc:
        return MemberVerdict(
            name=name,
            current=False,
            label="error",
            detail=f"Skill {name!r} install marker is not usable: {exc}",
            locked_snapshot=locked_snapshot,
            current_snapshot=current_snapshot,
            marker_snapshot=claimed,
        )
    except _FS_ERRORS as exc:
        return MemberVerdict(
            name=name,
            current=False,
            label="error",
            detail=f"Skill {name!r} install marker cannot be read: {exc}",
            locked_snapshot=locked_snapshot,
            current_snapshot=current_snapshot,
            marker_snapshot=claimed,
        )
    if verdict.current:
        return MemberVerdict(
            name=name,
            current=True,
            label="up-to-date",
            detail=f"Skill {name!r} is up-to-date",
            locked_snapshot=locked_snapshot,
            current_snapshot=current_snapshot,
            marker_snapshot=claimed,
        )
    differences = "; ".join(verdict.differences) if verdict.differences else "unknown"
    return MemberVerdict(
        name=name,
        current=False,
        label="marker-mismatch",
        detail=f"Skill {name!r} installed state does not match the lock: {differences}",
        locked_snapshot=locked_snapshot,
        current_snapshot=current_snapshot,
        marker_snapshot=claimed,
    )


def evaluate_schema2_installation(
    *,
    home: Path,
    project_path: Path,
    manifest_value: manifest.ProjectManifest,
    agents: tuple[str, ...],
    locale_value: str | None,
    alias: str,
    adapter_mode: str,
) -> Schema2Status:
    """Evaluate one schema-2 installation without writing anything.

    Members enumerate from the lock (frozen membership); collections
    are never expanded, since status never rescans. A stale lock marks
    every member; otherwise each member revalidates its locked source
    and marker, and then every live publication target is compared
    against the locked desired state through the install's own
    staging, so marker fields alone never attest currency. All staging
    stays in the system temporary directory, so project and home trees
    are byte-identical afterwards.
    """

    try:
        lock = read_schema2_lock(project_path)
    except SourceError as exc:
        return Schema2Status(members=(), errors=(str(exc),), lock_sha256=None)
    if lock is None:
        members: list[MemberVerdict] = []
        for selector in manifest_value.selectors:
            if isinstance(selector, skillfile_v2.IndividualSelector):
                members.append(
                    MemberVerdict(
                        name=selector.name,
                        current=False,
                        label="not-installed",
                        detail=(
                            f"Skill {selector.name!r} is not installed; "
                            "run csk install"
                        ),
                        locked_snapshot=None,
                        current_snapshot=None,
                        marker_snapshot=_marker_snapshot(
                            project_path
                            / ".agents"
                            / "skills"
                            / selector.name
                            / ".csk-install.json"
                        ),
                    )
                )
        return Schema2Status(
            members=tuple(members),
            errors=("schema-2 installation has no lock; run csk install",),
            lock_sha256=None,
        )
    if manifest_value.manifest_sha256 is None:
        return Schema2Status(
            members=(),
            errors=("schema-2 manifest digest is missing",),
            lock_sha256=lock.lock_sha256,
        )
    try:
        validate_lock(lock, current_manifest_sha256=manifest_value.manifest_sha256)
    except SourceError as exc:
        stale = tuple(
            MemberVerdict(
                name=member.name,
                current=False,
                label="lock-stale" if exc.code == CODE_LOCK_STALE else "error",
                detail=str(exc),
                locked_snapshot=(
                    member.package.snapshot
                    if isinstance(member.package, LocalSnapshot)
                    else None
                ),
                current_snapshot=None,
                marker_snapshot=_marker_snapshot(
                    project_path
                    / ".agents"
                    / "skills"
                    / member.name
                    / ".csk-install.json"
                ),
            )
            for member in lock.members
        )
        return Schema2Status(
            members=stale, errors=(str(exc),), lock_sha256=lock.lock_sha256
        )
    source_roots: dict[str, Path] = {}
    for source_alias, acquisition in manifest_value.sources.items():
        if isinstance(acquisition, skillfile_v2.PathSource):
            source_roots[source_alias] = resolve_source_root(
                project_path, source_alias, acquisition
            )
    if lock.lock_sha256 is None:
        return Schema2Status(
            members=(),
            errors=("schema-2 lock digest is missing",),
            lock_sha256=None,
        )
    lock_digest = lock.lock_sha256
    verdicts: list[MemberVerdict] = []
    with tempfile.TemporaryDirectory(prefix=".csk-source-status-") as staging_tmp:
        staging_root = Path(staging_tmp)
        for lock_member in lock.members:
            member_alias = _lock_member_alias(
                lock_member, manifest_value, source_roots
            )
            if member_alias is None:
                verdicts.append(
                    MemberVerdict(
                        name=lock_member.name,
                        current=False,
                        label="error",
                        detail=(
                            f"Skill {lock_member.name!r} lock member cannot be "
                            "attributed to a manifest source"
                        ),
                        locked_snapshot=(
                            lock_member.package.snapshot
                            if isinstance(lock_member.package, LocalSnapshot)
                            else None
                        ),
                        current_snapshot=None,
                        marker_snapshot=_marker_snapshot(
                            project_path
                            / ".agents"
                            / "skills"
                            / lock_member.name
                            / ".csk-install.json"
                        ),
                    )
                )
                continue
            verdicts.append(
                _evaluate_locked_member(
                    staging_root,
                    home=home,
                    project_path=project_path,
                    lock_member=lock_member,
                    lock_sha256=lock_digest,
                    source_root=source_roots[member_alias],
                    agents=agents,
                    locale_value=locale_value,
                )
            )
        if all(verdict.current for verdict in verdicts):
            return _evaluate_live_outputs(
                staging_root,
                home=home,
                project_path=project_path,
                manifest_value=manifest_value,
                agents=agents,
                locale_value=locale_value,
                alias=alias,
                adapter_mode=adapter_mode,
                lock=lock,
                lock_digest=lock_digest,
                source_roots=source_roots,
                verdicts=tuple(verdicts),
            )
    return Schema2Status(
        members=tuple(verdicts), errors=(), lock_sha256=lock_digest
    )


def _evaluate_live_outputs(
    staging_root: Path,
    *,
    home: Path,
    project_path: Path,
    manifest_value: manifest.ProjectManifest,
    agents: tuple[str, ...],
    locale_value: str | None,
    alias: str,
    adapter_mode: str,
    lock: SkillfileLock,
    lock_digest: str,
    source_roots: dict[str, Path],
    verdicts: tuple[MemberVerdict, ...],
) -> Schema2Status:
    """Compare every live target against the locked desired state.

    Desired state derives from the lock and the revalidated locked
    bytes through the install's own staging (temporary directory
    only, never healed into the store), so status and install-skip
    decide by one mechanism: any live difference — tampered context,
    retargeted mirror, stale outputs — is non-current. Planning and
    staging failures report as status errors, never as crashes, and a
    concurrent source change mid-evaluation reports non-current rather
    than a stale clean.
    """

    try:
        members = locked_schema2_members(
            lock, list(manifest_value.selectors), source_roots
        )
        packages = {
            lock_member.name: lock_member.package
            for lock_member in lock.members
            if isinstance(lock_member.package, LocalSnapshot)
        }
        captured_members: dict[str, CapturedMember] = {}
        pred_files: dict[str, Mapping[str, snapshot.FrozenFile]] = {}
        for member in members:
            package = packages[member.name]
            raw, served = _capture_locked_member(
                member, package, home=home, heal=False
            )
            captured_members[member.name] = CapturedMember(
                member=member,
                captured=raw,
                package=package,
                package_key=package_identity_sha256(package),
            )
            if raw is not None:
                pred_files[member.name] = raw.frozen_files()
            elif served is not None:
                pred_files[member.name] = served.files
            else:
                raise SourceError(
                    CODE_SNAPSHOT_UNAVAILABLE,
                    f"Skill {member.name!r} locked snapshot cannot be served",
                )
        specs_map: dict[str, skillspec.SkillSpec] = {}
        for name in sorted(pred_files):
            frozen_dir = staging_root / "status-spec" / name
            materialize_frozen(
                pred_files[name], frozen_dir, subject=f"Skill {name!r}"
            )
            specs_map[name] = load_member_spec(frozen_dir, name=name)
        package_keys = {
            name: item.package_key for name, item in captured_members.items()
        }
        lock_runtime_refs = _referenced_runtime_keys(lock)
        planned, adapter_targets, _plan_messages = plan_schema2_targets(
            home=home,
            project_path=project_path,
            slug=project_slug(locking.canonical_project_identity(project_path)),
            alias=alias,
            agents=list(agents),
            members=tuple(members),
            package_keys=package_keys,
            runtime_roots={
                name: specs_map[name].runtime_roots for name in captured_members
            },
            new_runtime_refs=lock_runtime_refs,
            old_runtime_refs=lock_runtime_refs,
        )
        manifest_data, _reread = _read_fresh_manifest(project_path)
        staged = stage_schema2_desired(
            staging_root / "status-desired",
            home=home,
            project_path=project_path,
            alias=alias,
            agents=agents,
            locale_value=locale_value,
            adapter_mode=adapter_mode,
            manifest_data=manifest_data,
            members=tuple(members),
            captured=captured_members,
            frozen_files=pred_files,
            specs_map=specs_map,
            source_roots=source_roots,
            specs=planned,
            adapter_targets=adapter_targets,
            slug=project_slug(locking.canonical_project_identity(project_path)),
        )
    except SourceError as exc:
        return Schema2Status(
            members=verdicts, errors=(str(exc),), lock_sha256=lock_digest
        )
    except manifest.ManifestError as exc:
        return Schema2Status(
            members=verdicts,
            errors=(f"schema-2 manifest cannot be re-read: {exc}",),
            lock_sha256=lock_digest,
        )
    except _FS_ERRORS as exc:
        return Schema2Status(
            members=verdicts,
            errors=(f"schema-2 installation cannot be inspected: {exc}",),
            lock_sha256=lock_digest,
        )
    drifted: dict[str, str] = {}
    global_errors: list[str] = []
    for spec in planned:
        key = (spec.target_class, spec.identifier)
        wanted = staged.desired[key]
        try:
            live_digest = digest_target(spec.live_path, kind=spec.kind)
        except _FS_ERRORS as exc:
            live_digest = f"unreadable: {exc}"
        except TransactionError as exc:
            live_digest = f"unreadable: {exc}"
        if wanted is None:
            if live_digest != ABSENT_DIGEST:
                global_errors.append(
                    f"stale installed outputs remain at {spec.live_path}; "
                    "run csk install to clean up"
                )
            continue
        try:
            wanted_digest = digest_target(wanted, kind=spec.kind)
        except TransactionError as exc:
            global_errors.append(
                f"staged target cannot be hashed for {spec.live_path}: {exc}"
            )
            continue
        if live_digest == wanted_digest:
            continue
        touched = _touched_members((key,))
        if touched:
            for name in touched:
                drifted.setdefault(
                    name,
                    f"Skill {name!r} installed outputs differ from the lock; "
                    "run csk install to repair",
                )
        else:
            global_errors.append(
                f"installed outputs at {spec.live_path} differ from the "
                "lock; run csk install to repair"
            )
    if not drifted and not global_errors:
        return Schema2Status(
            members=verdicts, errors=(), lock_sha256=lock_digest
        )
    final: list[MemberVerdict] = []
    for verdict in verdicts:
        detail = drifted.get(verdict.name)
        if detail is None:
            final.append(verdict)
        else:
            final.append(
                MemberVerdict(
                    name=verdict.name,
                    current=False,
                    label="drifted",
                    detail=detail,
                    locked_snapshot=verdict.locked_snapshot,
                    current_snapshot=verdict.current_snapshot,
                    marker_snapshot=verdict.marker_snapshot,
                )
            )
    return Schema2Status(
        members=tuple(final),
        errors=tuple(global_errors),
        lock_sha256=lock_digest,
    )


def _lock_member_alias(
    lock_member: LockMember,
    manifest_value: manifest.ProjectManifest,
    source_roots: dict[str, Path],
) -> str | None:
    """Attribute one lock member to its source alias via admission matching.

    The lock selection is a root-selection ordinal, not a selector
    index, so attribution replays the admission predicate: the member's
    unique admitting selector names its source. Transitive members
    (null selection) belong to closure resolution and stay
    unattributable here.
    """

    selectors = list(manifest_value.selectors)
    if lock_member.selection is None:
        return None
    index = _attribute_unique_selector(
        selectors, name=lock_member.name, directory=lock_member.directory
    )
    if index is None:
        return None
    selector = selectors[index]
    if selector.from_alias not in source_roots:
        return None
    return selector.from_alias
