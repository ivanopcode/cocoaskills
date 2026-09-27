"""Atomic publication of schema-2 source installs and locks.

Implements the install side of protocol skillfile-sources sections 2
through 4 for local ``path`` and Git sources: one recoverable
transaction publishes the lock, marker v5 records, runtime store
entries, context projection and adapter mirrors, with the physical
boundary rechecked immediately before each publication write. Status,
locked install (repair) and explicit refresh all revalidate the exact
locked source and required evidence; marker summaries never authorize
anything and locks are never silently refreshed.

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
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from .. import adapters, build_repository_pipeline, closure, git_admission, git_ops, hashing
from .. import identifiers, install_marker, locale, locking, manifest, shims
from ..source_identity import canonical_source_identity
from ..build_repository import (
    GO_REPOSITORY_V1_DRIVER,
    LockedCommit as BuildLockedCommit,
)
from .. import protocol_json, skillspec, whitelist
from ..builds import cache as build_cache
from ..builds import currentness as build_currentness
from ..builds import metadata as build_metadata
from ..builds import planner as build_planner
from ..builds import source as build_source

if TYPE_CHECKING:
    from ..audit.pipeline import GateResult
    from ..builds.toolchain import OperatorSearchPath
    from ..config import GlobalConfig
    from ..dev_substitutions import DevManifest
    from ..git_admission import OperatorSSHCredentials
    from ..installer import OperatorHTTPSToken
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
from . import boundaries, consumers, modes, skillfile_v2, snapshot, source_audit, store
from . import transport as source_transport
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
    sort_lock_members_by_utf8,
    validate_lock,
)
from .package_identity import (
    ConfiguredGit,
    LocalSnapshot,
    LockedCommit,
    NetworkGit,
    PackageIdentity,
    package_identity_sha256,
)
from .selection import (
    SelectedSkill,
    destination_key,
    expand_collection,
    expand_selectors,
)

__all__ = [
    "BINDINGS_DIRNAME",
    "CLASS_ADAPTER_LEDGER",
    "CLASS_BIN",
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
# One command owns one launcher path, whatever produced it: the class string
# is the legacy lane's own vocabulary for project command shims.
CLASS_BIN: Final = "30-shim-canonical"
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
    "commit-30-shim-canonical",
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
    collection selects several members. Transitive closure members
    carry no selector, so ``from_alias``, ``selection_ordinal`` and
    ``source_root`` are all ``None`` for them: no binding is written and
    the lock selection is null.
    """

    name: str
    from_alias: str | None
    directory: str
    selection_ordinal: int | None
    source_root: Path | None


@dataclass(frozen=True)
class CapturedMember:
    """One resolved member with its frozen capture and package identity.

    ``captured`` is None only on the locked-repair path, where live
    bytes cannot be captured and the verified store copy serves
    instead; ``package`` always carries the authoritative identity —
    a local snapshot digest or a Git repository, commit and directory —
    either way.
    """

    member: ResolvedMember
    captured: snapshot.CapturedPackage | None
    package: PackageIdentity
    package_key: str
    store_missing: bool = False


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

    Only ``path`` acquisitions resolve here: a literal native path,
    resolved against the project directory when relative. Git and
    repository acquisitions resolve through
    :func:`acquire_git_source_root`, which needs the resolving mode;
    reaching this function with one is a caller error.
    """

    if isinstance(acquisition, skillfile_v2.PathSource):
        declared = acquisition.path
        candidate = Path(declared)
        if candidate.is_absolute():
            return candidate
        return project_path / declared
    raise SourceError(
        CODE_SELECTION_INVALID,
        f"Source {alias!r} uses a network acquisition, which resolves "
        "through the bounded transport, not through local source roots",
    )


def acquire_git_source_root(
    mode: modes.ResolvingSources,
    alias: str,
    acquisition: skillfile_v2.GitSource | skillfile_v2.RepositorySource,
) -> tuple[Path, modes.AliasResolution]:
    """Resolve and materialize one Git alias inside the resolving workspace.

    Resolution is memoized per declaration, so every member from one Git
    alias uses the same resolved commit. Acquisition goes through the
    bounded transport only; the verified snapshot materializes into the
    private workspace, which expansion then treats exactly like a local
    source root. Frozen mode cannot reach this function: it holds no
    resolving mode to pass.
    """

    resolution = modes.resolve_git_alias(mode, alias, acquisition)
    acquired = modes.acquire_git_alias(mode, alias, acquisition, resolution)
    materialized = mode.workspace / "acquired" / alias
    if materialized.exists():
        # One alias materializes once per operation: the workspace is a
        # fresh private directory, so an existing tree holds exactly the
        # bytes this operation acquired for this alias.
        return materialized, resolution
    try:
        acquired.materialize(materialized)
    except git_admission.GitAdmissionError as exc:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Source {alias!r} acquired bytes cannot be staged: {exc}",
        ) from exc
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Source {alias!r} acquired bytes cannot be staged: {exc}",
        ) from exc
    return materialized, resolution


def resolve_schema2_source_roots(
    project_path: Path,
    manifest_value: manifest.ProjectManifest,
    *,
    mode: modes.ResolvingSources,
) -> tuple[dict[str, Path], dict[str, modes.AliasResolution]]:
    """Resolve every referenced alias: paths directly, Git via transport.

    Only aliases a selector references are resolved; unreferenced Git
    aliases cost no network. Git aliases resolve and materialize into
    the resolving workspace; path aliases resolve against the project
    directory as before.
    """

    referenced = {selector.from_alias for selector in manifest_value.selectors}
    source_roots: dict[str, Path] = {}
    git_resolutions: dict[str, modes.AliasResolution] = {}
    for alias in sorted(referenced):
        acquisition = manifest_value.sources.get(alias)
        if acquisition is None:
            # Unknown aliases refuse at expansion with the admission
            # error; resolving nothing here keeps that refusal exact.
            continue
        if isinstance(acquisition, skillfile_v2.PathSource):
            source_roots[alias] = resolve_source_root(
                project_path, alias, acquisition
            )
        else:
            materialized, resolution = acquire_git_source_root(
                mode, alias, acquisition
            )
            source_roots[alias] = materialized
            git_resolutions[alias] = resolution
    return source_roots, git_resolutions


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
    project_path: Path,
    manifest_value: manifest.ProjectManifest,
    *,
    mode: modes.ResolvingSources,
    source_roots: dict[str, Path] | None = None,
    git_resolutions: dict[str, modes.AliasResolution] | None = None,
) -> tuple[ResolvedMember, ...]:
    """Expand and validate every schema-2 selector against resolved roots.

    Refusals are explicit and never silent: legacy declarations and
    project-root packages fail here, before any filesystem work beyond
    expansion itself. Git aliases resolve through the bounded transport
    (see :func:`resolve_schema2_source_roots`); members with transitive
    requirements no longer refuse — the schema-2 full closure resolves
    them after expansion. A ``directory: "."`` selector over a Git alias
    selects the repository root as the package, which is a complete
    tree and needs no admitted subset; over a path alias it still
    refuses.
    """

    if manifest_value.skills:
        names = sorted(decl.name for decl in manifest_value.skills)
        raise SourceError(
            CODE_SELECTION_INVALID,
            "Legacy skill declarations are not installable from a "
            f"schema-2 Skillfile by atomic install: {names[0]!r}",
        )
    selectors = list(manifest_value.selectors)
    for selector in selectors:
        if selector.directory != ".":
            continue
        acquisition = manifest_value.sources.get(selector.from_alias)
        if acquisition is None or isinstance(acquisition, skillfile_v2.PathSource):
            raise SourceError(
                CODE_SELECTION_INVALID,
                "Root package publication needs admitted-subset capture, which "
                "atomic install does not provide; select a nested package",
            )
    if source_roots is None or git_resolutions is None:
        source_roots, git_resolutions = resolve_schema2_source_roots(
            project_path, manifest_value, mode=mode
        )
    # Root-input enforcement is a local-path rule: a materialized Git root
    # is a complete tree and needs no admitted subset, so only path aliases
    # are in scope (path roots already refused above; this preserves the
    # Git-root lane through expansion).
    path_aliases = frozenset(
        alias
        for alias, acquisition in manifest_value.sources.items()
        if isinstance(acquisition, skillfile_v2.PathSource)
    )
    try:
        selected = expand_selectors(
            selectors, source_roots, root_inputs_aliases=path_aliases
        )
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Skill selection cannot be expanded: {exc}",
        ) from exc
    resolved: list[ResolvedMember] = []
    for ordinal, member in enumerate(selected):
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
    mode: modes.FrozenSources,
    selectors: list[skillfile_v2.SkillSelector],
    source_roots: dict[str, Path],
) -> tuple[ResolvedMember, ...]:
    """Derive install members from the lock without expanding selectors.

    A locked install or status check consumes the frozen membership:
    each lock member is attributed to its source by admission matching
    (no filesystem expansion), so a deleted member directory surfaces
    as an unavailable snapshot at capture time instead of a selection
    failure. Git members serve store-only; transitive members (null
    selection) carry no selector. Configured-git packages, local root
    packages, unknown sources, unattributable members and
    filesystem-equivalent name collisions (the selection leaf's own
    folding rule) refuse explicitly.
    """

    old_lock = mode.lock
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
        package = lock_member.package
        if isinstance(package, ConfiguredGit):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Skill {lock_member.name!r} lock member is a legacy "
                "configured-git package, which schema-2 install does not serve",
            )
        if not isinstance(package, (LocalSnapshot, NetworkGit)):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Skill {lock_member.name!r} lock member carries an "
                "unsupported package identity",
            )
        if lock_member.directory == "." and isinstance(package, LocalSnapshot):
            raise SourceError(
                CODE_SELECTION_INVALID,
                "Root package publication needs admitted-subset capture, "
                "which atomic install does not provide; select a nested package",
            )
        if lock_member.selection is None:
            # Transitive closure members carry no selector: they serve
            # from the store alone, with no source root to verify.
            if not isinstance(package, NetworkGit):
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill {lock_member.name!r} transitive lock member is "
                    "not a Git package",
                )
            members.append(
                ResolvedMember(
                    name=lock_member.name,
                    from_alias=None,
                    directory=lock_member.directory,
                    selection_ordinal=None,
                    source_root=None,
                )
            )
            continue
        index = _attribute_unique_selector(
            selectors, name=lock_member.name, directory=lock_member.directory
        )
        if index is None:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {lock_member.name!r} lock member cannot be "
                "attributed to a manifest selector",
            )
        from_alias = selectors[index].from_alias
        if from_alias not in source_roots:
            if isinstance(package, NetworkGit):
                # Git root members serve from the store alone; no local
                # source root exists for them.
                members.append(
                    ResolvedMember(
                        name=lock_member.name,
                        from_alias=from_alias,
                        directory=lock_member.directory,
                        selection_ordinal=lock_member.selection,
                        source_root=None,
                    )
                )
                continue
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
    """Capture every resolved root member as an immutable snapshot.

    Capture reads working-tree bytes with revalidation inside; failures
    carry the snapshot leaf's codes unchanged. Every member needs a
    source root: transitive closure members capture from their
    acquisition materialization instead (see
    :func:`capture_transitive_member`).
    """

    captured: dict[str, snapshot.CapturedPackage] = {}
    for member in members:
        if member.source_root is None:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {member.name!r} has no source root to capture",
            )
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


def capture_transitive_member(
    materialized: Path, *, name: str, home: Path
) -> snapshot.CapturedPackage:
    """Capture one transitive member from its acquisition materialization.

    The materialized directory holds exactly the acquired skill bytes
    (the skill sits at the acquisition root, like a schema-1
    requirement), so capturing ``"."`` admits precisely the package
    with no subset problem. Reuses the snapshot leaf's capture,
    including its revalidation; failures carry its codes unchanged.
    """

    try:
        return snapshot.capture_package_snapshot(materialized, ".", home=home)
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_SNAPSHOT_CHANGED,
            f"Skill {name!r} cannot be captured: {exc}",
        ) from exc


def collection_membership(
    selectors: list[skillfile_v2.SkillSelector],
    source_roots: dict[str, Path],
    skip_aliases: frozenset[str] = frozenset(),
) -> dict[int, tuple[str, ...]]:
    """Expand every collection selector to its ordered member names.

    Aliases in ``skip_aliases`` are omitted: Git aliases enumerate
    private acquisition workspace paths that no concurrent writer can
    reach, so there is no membership race to detect for them.
    """

    membership: dict[int, tuple[str, ...]] = {}
    for index, selector in enumerate(selectors):
        if isinstance(selector, skillfile_v2.IndividualSelector):
            continue
        if selector.from_alias in skip_aliases:
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


def script_command_relative_path(
    command: skillspec.CommandSpec, *, name: str
) -> str:
    """Select one script command's platform path, like the shim layer.

    Mirrors the legacy lane's selection exactly: the Windows path on
    Windows, the Unix path elsewhere, refused when the member declares
    no path for the activating platform.
    """

    relative = command.win_path if os.name == "nt" else command.unix_path
    if not relative:
        platform = "windows" if os.name == "nt" else "unix"
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill {name!r} command {command.name!r} has no path for {platform}",
        )
    if not identifiers.is_valid_portable_path(relative):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill {name!r} command {command.name!r} path is not a "
            "portable relative path",
        )
    return relative


def script_bin_filename(command_name: str) -> str:
    """Return the runtime ``bin/`` filename for one rootless script command.

    Mirrors the legacy lane's single-file activation layout: the
    command name, with the launcher suffix on Windows.
    """

    if os.name == "nt" and not command_name.endswith(".cmd"):
        return f"{command_name}.cmd"
    return command_name


def _grant_staged_execute(path: Path, *, name: str, command: str) -> None:
    """Add execute bits to one staged command file on POSIX hosts.

    Mirrors the shim layer's grant exactly: through a descriptor that
    follows no link, only onto a regular file. Windows activates
    commands through ``.cmd`` launchers instead.
    """

    if os.name != "posix":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill {name!r} command {command!r} staged state cannot be "
            f"opened: {exc}",
        ) from exc
    try:
        try:
            info = os.fstat(descriptor)
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {command!r} staged state cannot be "
                f"inspected: {exc}",
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {command!r} staged state is not a "
                "regular file",
            )
        mode = stat.S_IMODE(info.st_mode)
        execute = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        if mode | execute != mode:
            try:
                os.fchmod(descriptor, mode | execute)
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill {name!r} command {command!r} staged state cannot "
                    f"be made executable: {exc}",
                ) from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _relative_within_root(root: str, relative: str) -> bool:
    """Return whether one portable path sits at or below a portable root."""

    root_parts = tuple(root.split("/"))
    path_parts = tuple(relative.split("/"))
    return len(path_parts) >= len(root_parts) and path_parts[: len(root_parts)] == root_parts


def refuse_build_root_script(
    command: skillspec.CommandSpec, relative: str, spec: skillspec.SkillSpec, *, name: str
) -> None:
    """Refuse a script command file that sits below a build root.

    Build roots are compiled inputs; they never enter installed
    script runtime. Members with runtime roots exclude build roots
    structurally (the manifest grammar keeps the two disjoint and
    every command path inside a runtime root), so this gate bites
    for rootless members whose script may name any package file.
    """

    for root in spec.build_roots:
        if _relative_within_root(root, relative):
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {command.name!r} path {relative!r} "
                f"is below build root {root!r}, which never enters "
                "installed script runtime",
            )


def stage_member_runtime(
    frozen_dir: Path,
    runtime_staged: Path,
    spec: skillspec.SkillSpec,
    *,
    name: str,
) -> None:
    """Stage one member's script runtime from its frozen bytes.

    Members with runtime roots stage those roots; members without
    roots but with script commands stage each command file into the
    legacy lane's ``bin/`` single-file layout. Every byte comes from
    the frozen materialization, never from the live authored tree;
    command files gain owner execute bits on POSIX hosts, and a
    command file below a build root refuses.
    """

    try:
        if runtime_staged.exists():
            shutil.rmtree(runtime_staged)
        runtime_staged.mkdir(parents=True)
        if spec.runtime_roots:
            for root in spec.runtime_roots:
                if not identifiers.is_valid_portable_path(root):
                    raise SourceError(
                        CODE_MEMBER_INVALID,
                        f"Skill {name!r} carries a non-portable runtime root "
                        f"{root!r}",
                    )
                shutil.copytree(frozen_dir / root, runtime_staged / root, symlinks=True)
        script_names = active_script_commands(spec)
        for command_name in script_names:
            command = spec.commands[command_name]
            relative = script_command_relative_path(command, name=name)
            refuse_build_root_script(command, relative, spec, name=name)
            if spec.runtime_roots:
                staged_file = runtime_staged / relative
            else:
                staged_file = runtime_staged / "bin" / script_bin_filename(command_name)
                source = frozen_dir / relative
                try:
                    data = source.read_bytes()
                except _FS_ERRORS as exc:
                    raise SourceError(
                        CODE_MEMBER_INVALID,
                        f"Skill {name!r} command {command_name!r} frozen file "
                        f"{relative!r} cannot be read: {exc}",
                    ) from exc
                staged_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    staged_file.write_bytes(data)
                except _FS_ERRORS as exc:
                    raise SourceError(
                        CODE_MEMBER_INVALID,
                        f"Skill {name!r} command {command_name!r} cannot be "
                        f"staged: {exc}",
                    ) from exc
            _grant_staged_execute(staged_file, name=name, command=command_name)
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Skill {name!r} runtime cannot be staged: {exc}"
        ) from exc


def schema2_shim_path_entries(
    spec: skillspec.SkillSpec, *, final_bin: Path
) -> tuple[Path, ...]:
    """Derive the launcher PATH entries for one member's shims.

    Mirrors the legacy lane exactly: the final project bin dir
    first (so a consumer script reaches provider commands by bare
    name), then the running interpreter's directory, then the
    resolved directory of every declared system dependency,
    deduplicated by normalized spelling. Entries are absolute
    final paths, so staged and live launchers carry identical
    bytes.
    """

    candidates = [final_bin.absolute()]
    if sys.executable:
        candidates.append(Path(sys.executable).resolve().parent)
    for dependency in spec.dependencies.values():
        if dependency.type != "system" or not dependency.command:
            continue
        executable = shutil.which(dependency.command)
        if executable:
            candidates.append(Path(executable).resolve().parent)
    entries: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        entries.append(candidate)
    return tuple(entries)


def schema2_script_target(
    home: Path,
    name: str,
    package_key: str,
    command: skillspec.CommandSpec,
    spec: skillspec.SkillSpec,
) -> Path:
    """Return the protected-store file one script shim launches.

    Members with runtime roots address the command path inside the
    runtime entry; rootless members address the single-file
    ``bin/`` layout. The entry is keyed by ``(skill name,
    SHA-256(CCJ-1(package)))`` in the ``source-v1`` namespace and
    never resolves into the authored directory.
    """

    entry = runtime_entry_path(home, name, package_key)
    relative = script_command_relative_path(command, name=name)
    if spec.runtime_roots:
        return entry / relative
    return entry / "bin" / script_bin_filename(command.name)


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


def require_installable_member(
    spec: skillspec.SkillSpec, *, name: str, satisfied_by: frozenset[str]
) -> None:
    """Refuse members whose skill requirements are not in the closure.

    Script commands materialize from the frozen snapshot into the
    protected runtime store with shims in ``.agents/bin``; build
    commands materialize through the existing build pipeline with the
    member package bound. Every skill requirement
    (``dependencies.skills``) must name a member of the resolved
    closure (or the consumed lock): a requirement the closure did not
    satisfy refuses here rather than installing an incomplete skill.
    """

    for requirement in sorted(spec.requirements):
        if requirement not in satisfied_by:
            raise SourceError(
                CODE_MEMBER_MISSING,
                f"Skill {name!r} requires {requirement!r}, which is not a "
                "member of the resolved closure",
            )


def check_schema2_requirement_commands(
    specs_map: Mapping[str, skillspec.SkillSpec],
) -> None:
    """Refuse requirements naming commands the provider does not export.

    Mirrors the closure's own requirement-command validation for the
    frozen path, where no traversal runs: every requirement command
    must name a script command its closure member exports.
    """

    errors: list[str] = []
    for name in sorted(specs_map):
        spec = specs_map[name]
        for requirement in spec.requirements.values():
            provider = specs_map.get(requirement.name)
            if provider is None:
                continue
            for command in requirement.commands:
                provided = provider.commands.get(command)
                if provided is None or provided.type != "script":
                    errors.append(
                        f"Requirement {name} -> {requirement.name} names command {command!r}, "
                        f"but {requirement.name} does not export a script command named {command!r}"
                    )
    if errors:
        raise SourceError(CODE_MEMBER_MISSING, "; ".join(errors))


def active_script_commands(spec: skillspec.SkillSpec) -> tuple[str, ...]:
    """Return the sorted script commands one member activates.

    Every selected member is fully active: schema-2 selection has no
    partial activation edges, so each member activates all the script
    commands it exports. Build commands activate through the build
    providers instead.
    """

    return tuple(
        sorted(
            command.name
            for command in spec.commands.values()
            if command.type == "script"
        )
    )


def active_build_commands(spec: skillspec.SkillSpec) -> tuple[str, ...]:
    """Return the sorted build commands one member activates."""

    return tuple(
        sorted(
            command.name
            for command in spec.commands.values()
            if command.type == "build"
        )
    )


def claim_schema2_command_owner(
    owners: dict[str, str], command: str, owner: str
) -> None:
    """Claim one command name for its owning member, refusing collisions.

    The single collision predicate for both the plan-time script
    gate and the planning-time owner map, so the two cannot drift:
    one name owns one launcher path, whoever reports it.
    """

    previous = owners.get(command)
    if previous is not None:
        raise SourceError(
            CODE_NAME_CONFLICT,
            f"Command collision for {command!r}: exported by "
            f"{previous} and {owner}",
        )
    owners[command] = owner


def check_schema2_command_collisions(
    members: tuple[ResolvedMember, ...],
    specs_map: dict[str, skillspec.SkillSpec],
) -> None:
    """Refuse script commands two members export under one name.

    One name owns one launcher path, so a repeated script command
    name refuses before any staging. Build collisions are refused by
    the build planner's own detector once providers exist.
    """

    owners: dict[str, str] = {}
    for member in members:
        for command in active_script_commands(specs_map[member.name]):
            claim_schema2_command_owner(owners, command, member.name)


def check_schema2_system_commands(
    members: tuple[ResolvedMember, ...],
    specs_map: dict[str, skillspec.SkillSpec],
) -> None:
    """Refuse members whose required system commands are not ready.

    Mirrors the legacy lane's readiness check exactly: every declared
    system dependency names an executable that must resolve on the
    operator PATH before any shim is published.
    """

    for member in members:
        spec = specs_map[member.name]
        for dependency in spec.dependencies.values():
            if dependency.type != "system":
                continue
            if not dependency.command or shutil.which(dependency.command) is None:
                hint = f" Hint: {dependency.hint}" if dependency.hint else ""
                raise SourceError(
                    CODE_MEMBER_MISSING,
                    f"Missing system command {dependency.command!r} for "
                    f"{member.name}.{hint}",
                )


def check_schema2_skill_dependencies(
    members: tuple[ResolvedMember, ...],
    specs_map: dict[str, skillspec.SkillSpec],
) -> None:
    """Refuse members whose skill command dependencies are unsatisfied.

    A skill dependency is satisfied exactly when a selected member
    exports the named script command. Members outside the selected
    set cannot satisfy a dependency: locked installs and status
    derive membership from the lock, so filesystem-side siblings
    never count.
    """

    for member in members:
        spec = specs_map[member.name]
        for dependency in spec.dependencies.values():
            if dependency.type != "skill":
                continue
            if not dependency.skill or not dependency.command:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Invalid skill dependency for {member.name}: "
                    f"{dependency.name}",
                )
            provider = specs_map.get(dependency.skill)
            if provider is None:
                hint = f" Hint: {dependency.hint}" if dependency.hint else ""
                raise SourceError(
                    CODE_MEMBER_MISSING,
                    f"Missing skill dependency {dependency.skill!r} for "
                    f"{member.name}; add {dependency.skill} to "
                    f"Skillfile.json.{hint}",
                )
            provided = provider.commands.get(dependency.command)
            if provided is None or provided.type != "script":
                raise SourceError(
                    CODE_MEMBER_MISSING,
                    f"Skill dependency {member.name} requires "
                    f"{dependency.skill}.{dependency.command}, but "
                    f"{dependency.skill} does not export a script command "
                    f"named {dependency.command!r}",
                )


def check_schema2_script_execution_policy(
    members: tuple[ResolvedMember, ...],
    specs_map: dict[str, skillspec.SkillSpec],
) -> None:
    """Refuse members selecting a script policy this manager cannot run.

    The check is the legacy lane's own fail-closed call at the single
    shim publication point: a manager that does not implement the
    selected script execution policy never publishes the shim, not
    even declared-only.
    """

    for member in members:
        spec = specs_map[member.name]
        if not active_script_commands(spec):
            continue
        rejection = skillspec.script_execution_policy_rejection(spec)
        if rejection is not None:
            raise SourceError(
                CODE_MEMBER_INVALID, f"{member.name}: {rejection}"
            )


def check_schema2_build_root_scripts(
    members: tuple[ResolvedMember, ...],
    specs_map: dict[str, skillspec.SkillSpec],
) -> None:
    """Refuse script commands whose files sit below a build root.

    Build roots are compiled inputs; they never enter installed
    script runtime. The check is pure spec, so it refuses here at
    plan time, before audit, compilation, or staging; the staging
    seam rechecks through the same predicate.
    """

    for member in members:
        spec = specs_map[member.name]
        for command_name in active_script_commands(spec):
            command = spec.commands[command_name]
            relative = script_command_relative_path(command, name=member.name)
            refuse_build_root_script(command, relative, spec, name=member.name)


def run_schema2_command_gates(
    members: tuple[ResolvedMember, ...],
    specs_map: dict[str, skillspec.SkillSpec],
) -> None:
    """Run every plan-time command gate over the selected members.

    Capabilities are mandatory at skill-spec parse time
    (``load_member_spec`` refuses schema-3 members without them);
    collisions, system-command readiness, skill command
    dependencies, the script execution policy and build-root
    script placement are refused here, before any staging. Build
    collisions join once the build providers exist.
    """

    check_schema2_command_collisions(members, specs_map)
    check_schema2_system_commands(members, specs_map)
    check_schema2_skill_dependencies(members, specs_map)
    check_schema2_script_execution_policy(members, specs_map)
    check_schema2_build_root_scripts(members, specs_map)


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
    ``scripts/`` is context only when no commands are exported,
    runtime and build roots stay excluded, and the content hash runs
    after locale rendering.
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
    package: PackageIdentity,
    lock_sha256: str,
    content_sha256: str,
    spec: skillspec.SkillSpec,
    files: tuple[str, ...],
    agents: tuple[str, ...],
    locale_value: str | None,
    builds: Mapping[str, install_marker.InstallMarkerBuildV5] | None = None,
    build_source: build_source.BuildSourceIdentity | None = None,
) -> install_marker.MarkerPlan:
    """Build the marker-comparable plan projection for one member.

    Every selected member is fully active, so the plan carries all
    the script and build commands the member exports. Build records
    arrive from the build publication that just ran; members without
    builds carry the empty map and no top-level build source. The
    top-level build source is exact here: present exactly with active
    local go-v1 records, in both directions.
    """

    try:
        install_marker.check_top_level_build_source(dict(builds or {}), build_source)
    except install_marker.InstallMarkerError as exc:
        raise SourceError(
            CODE_MEMBER_INVALID, f"Skill {name!r} plan is not valid: {exc}"
        ) from exc
    active = tuple(
        sorted(
            command.name
            for command in spec.commands.values()
            if command.type in {"script", "build"}
        )
    )
    try:
        return install_marker.MarkerPlan(
            name=name,
            package=package,
            lock_sha256=lock_sha256,
            context_sha256=content_sha256,
            content_sha256=content_sha256,
            locale=locale_value,
            agents=agents,
            commands=active,
            dependencies=tuple(spec.dependencies),
            skill_schema_version=spec.schema_version,
            runtime_roots=spec.runtime_roots,
            build_roots=spec.build_roots,
            files=files,
            builds=dict(builds or {}),
            requirements=None,
            mcp_servers=None,
            activation=install_marker.MarkerActivation(context=True, commands=active),
            requirers=None,
            build_source=build_source,
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
            build_source=plan.build_source,
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


def plan_schema2_targets(
    *,
    mode: modes.ResolvingSources | modes.FrozenSources,
    home: Path,
    project_path: Path,
    slug: str,
    alias: str,
    agents: list[str],
    members: tuple[ResolvedMember, ...],
    package_keys: dict[str, str],
    specs_map: dict[str, skillspec.SkillSpec],
    new_runtime_refs: set[tuple[str, str]],
    old_runtime_refs: set[tuple[str, str]],
    git_aliases: frozenset[str] = frozenset(),
) -> tuple[tuple[TargetSpec, ...], tuple[adapters.AdapterTarget, ...], list[str]]:
    """Plan every publication target for one schema-2 install.

    The plan covers machine bindings, the lock, one context target per
    member, one runtime target per member with a script runtime, one
    command shim per active command, adapter mirrors with their
    ledger, and removals for dropped skills and stale managed
    entries. The runtime removal set is the lock diff: keys the old
    lock referenced and the new lock does not. Nothing else is
    removable by an install; an entry referenced by no lock this
    project owns stays for ``gc``. The planner never lists the live
    home runtime tree and never reads another project's lock, so
    cleanup works with the source member directory deleted and never
    touches another installation. Unmanaged destinations refuse here,
    before any write. Bindings are written for path aliases only: a
    binding records a physical source location, and Git aliases
    refresh through their declared endpoint instead. The lock target
    is planned only in resolving mode: frozen mode cannot write a
    lock, so the lock target never enters its commit set.
    """

    messages: list[str] = []
    skills_root = project_path / ".agents" / "skills"
    project_bin = project_path / ".agents" / "bin"
    member_names = {member.name for member in members}
    used_aliases = {
        member.from_alias
        for member in members
        if member.from_alias is not None and member.from_alias not in git_aliases
    }
    specs: list[TargetSpec] = []
    command_owners = schema2_command_owners(members, specs_map)

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
        if stale_alias in used_aliases:
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

    if isinstance(mode, modes.ResolvingSources):
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
        spec = specs_map[member.name]
        if spec.runtime_roots or active_script_commands(spec):
            specs.append(
                TargetSpec(
                    target_class=CLASS_RUNTIME,
                    identifier=f"{member.name}/{package_keys[member.name]}",
                    live_path=runtime_entry_path(home, member.name, package_keys[member.name]),
                    kind="entry",
                    staged=None,
                )
            )
        for command_name in sorted(
            set(active_script_commands(spec)) | set(active_build_commands(spec))
        ):
            try:
                shim_live = shims.shim_path(project_bin, command_name)
            except shims.ShimError as exc:
                raise SourceError(CODE_MEMBER_INVALID, str(exc)) from exc
            try:
                shim_info = shim_live.lstat()
            except FileNotFoundError:
                shim_info = None
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Command {command_name!r} destination cannot be "
                    f"inspected: {exc}",
                ) from exc
            if shim_info is not None and stat.S_ISDIR(shim_info.st_mode):
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Command {command_name!r} destination is a directory "
                    "and is never overwritten",
                )
            specs.append(
                TargetSpec(
                    target_class=CLASS_BIN,
                    identifier=command_name,
                    live_path=shim_live,
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

    expected_shims = {
        shims.shim_path(project_bin, command_name)
        for command_name in command_owners
    }
    try:
        bin_info = project_bin.lstat()
    except FileNotFoundError:
        bin_info = None
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_OUTPUT_OVERLAP,
            f"Command directory {project_bin} cannot be inspected: {exc}",
        ) from exc
    if bin_info is not None:
        if stat.S_ISLNK(bin_info.st_mode):
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"Command directory {project_bin} is a link; newly "
                "introduced links are never followed",
            )
        if not stat.S_ISDIR(bin_info.st_mode):
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"Command directory {project_bin} is not a directory",
            )
        try:
            bin_children = sorted(project_bin.iterdir(), key=lambda p: p.name)
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"Command directory {project_bin} cannot be listed: {exc}",
            ) from exc
        for child in bin_children:
            if child in expected_shims:
                continue
            try:
                child_info = child.lstat()
            except FileNotFoundError:
                continue
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Command directory entry {child.name!r} cannot be "
                    f"inspected: {exc}",
                ) from exc
            if not stat.S_ISREG(child_info.st_mode) and not stat.S_ISLNK(
                child_info.st_mode
            ):
                continue
            specs.append(
                TargetSpec(
                    target_class=CLASS_REMOVAL,
                    identifier=f"shim/{child.name}",
                    live_path=child,
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
    # Ledger adoption is decided inside the shared planner: it refuses
    # any live ledger bytes csk did not write before a target is
    # emitted, identically for the schema-1 and schema-2 lanes.
    for target in adapter_targets:
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

    return tuple(
        sorted({member.from_alias for member in members if member.from_alias is not None})
    )


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
) -> list[tuple[int, int]] | None:
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
    frozen: list[tuple[int, int]] = []
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
        frozen.append((info.st_dev, info.st_ino))
        current = probe
    return frozen


def _freeze_home_ancestors(
    home_spelling: str,
    components: list[str],
    *,
    subject: str,
) -> list[tuple[int, int]]:
    """Freeze the ancestor identities below the csk home.

    Home-namespace chains never contain frozen links: any link refuses.
    Parents are created before freezing, so a missing entry is a
    concurrent change and refuses too.
    """

    frozen: list[tuple[int, int]] = []
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
        frozen.append((info.st_dev, info.st_ino))
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
    admitted_json = _identity_pairs_to_json(sorted(admitted))
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
            CLASS_BIN,
            CLASS_CONTEXT,
            CLASS_ADAPTER_LEDGER,
            CLASS_REMOVAL,
        }:
            destination = relative.as_posix()
            parent_parts = destination.split("/")[:-1]
            planned_output = "/".join(parent_parts)
            frozen_ancestors = _freeze_managed_ancestors(
                record, root_spelling, parent_parts, subject=subject
            )
            ancestors = (
                None
                if frozen_ancestors is None
                else _identity_pairs_to_json(frozen_ancestors)
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
        frozen_ancestors = _freeze_home_ancestors(
            home_spelling, list(home_parts[:-1]), subject=subject
        )
        ancestors = _identity_pairs_to_json(frozen_ancestors)
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
        identity = boundaries._identity_from_json(
            item, subject=f"{subject} field {field!r}[{index}]"
        )
        if identity is None:
            raise ValueError(
                f"{subject} field {field!r}[{index}] must be an integer pair"
            )
        identities.append(identity)
    return identities


def _identity_pairs_to_json(
    identities: list[tuple[int, int]] | tuple[tuple[int, int], ...],
) -> list[list[int | str]]:
    """Encode journal identities without narrowing Windows' 128-bit FileIds."""

    encoded: list[list[int | str]] = []
    for identity in identities:
        pair = boundaries._identity_to_json(identity)
        assert pair is not None
        encoded.append(pair)
    return encoded


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
    git_aliases: frozenset[str] = frozenset(),
) -> frozenset[tuple[int, int]]:
    """Collect the admitted source-package identities for the recheck.

    A member directory that disappeared after capture is a concurrent
    mutation and fails ``source_snapshot_changed``; a member path that
    became a link is a boundary change and fails
    ``source_output_overlap``. Neither is ever treated as absent.
    Git members resolve from private acquisition workspace paths that
    no concurrent writer can reach, so there is nothing to recheck
    for them.
    """

    admitted: set[tuple[int, int]] = set()
    for member in members:
        if member.source_root is None:
            # Transitive members serve from the store alone; there is no
            # live directory to recheck.
            continue
        if member.from_alias is not None and member.from_alias in git_aliases:
            continue
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
            remove_created_parents(created)
            raise SourceError(
                CODE_OUTPUT_OVERLAP,
                f"Publication parent {parent} cannot be inspected: {exc}",
            ) from exc
        if info is not None:
            if stat.S_ISLNK(info.st_mode):
                remove_created_parents(created)
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Publication parent {parent} is a link; newly introduced "
                    "links are never followed",
                )
            if not stat.S_ISDIR(info.st_mode):
                remove_created_parents(created)
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
                remove_created_parents(created)
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Publication parent {probe} cannot be inspected: {exc}",
                ) from exc
            if stat.S_ISLNK(probe_info.st_mode):
                remove_created_parents(created)
                raise SourceError(
                    CODE_OUTPUT_OVERLAP,
                    f"Publication parent {probe} is a link; newly introduced "
                    "links are never followed",
                )
            if not stat.S_ISDIR(probe_info.st_mode):
                remove_created_parents(created)
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
                    remove_created_parents(created)
                    raise SourceError(
                        CODE_OUTPUT_OVERLAP,
                        f"Publication parent {directory} cannot be inspected: {exc}",
                    ) from exc
                if stat.S_ISLNK(raced.st_mode) or not stat.S_ISDIR(raced.st_mode):
                    remove_created_parents(created)
                    raise SourceError(
                        CODE_OUTPUT_OVERLAP,
                        f"Publication parent {directory} is not a real directory",
                    )
                continue
            except _FS_ERRORS as exc:
                remove_created_parents(created)
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
    mode: modes.ResolvingSources | modes.FrozenSources,
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
    build_outputs: Schema2BuildOutputs | None = None,
    git_aliases: frozenset[str] = frozenset(),
) -> StagedDesired:
    """Stage the desired bytes for every planned target.

    Context is projected from frozen bytes, hashed, and bound into a
    new lock created only after all gates succeed; markers are written
    into the staged contexts, runtime entries, command shims and
    bindings are staged, and adapters mirror the staged canonical
    roots. Members whose live state already matches reuse the live
    bytes so the target skips. Build records and build shims derive
    from the published or reconstructed build bundle. The lock bytes
    are staged only in resolving mode; in frozen mode the rebuilt lock
    is computed for the determinism comparison only and never staged.
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
    bundle = build_outputs if build_outputs is not None else empty_build_outputs()
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
            builds=bundle.marker_builds.get(name),
            build_source=bundle.build_sources.get(name),
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
    for binding_alias in sorted(
        {
            member.from_alias
            for member in members
            if member.from_alias is not None and member.from_alias not in git_aliases
        }
    ):
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
    if isinstance(mode, modes.ResolvingSources):
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
        if staged.spec.runtime_roots or active_script_commands(staged.spec):
            runtime_staged = staging_root / "runtime" / name
            frozen_dir = staging_root / "frozen" / name
            stage_member_runtime(frozen_dir, runtime_staged, staged.spec, name=name)
            desired[(CLASS_RUNTIME, f"{name}/{captured[name].package_key}")] = runtime_staged
    staged_bin = staging_root / "bin"
    final_bin = project_path / ".agents" / "bin"
    for name, staged in staged_members.items():
        entries = schema2_shim_path_entries(staged.spec, final_bin=final_bin)
        for command_name in active_script_commands(staged.spec):
            command = staged.spec.commands[command_name]
            target = schema2_script_target(
                home, name, captured[name].package_key, command, staged.spec
            )
            try:
                staged_shim = shims.write_bin_shim(
                    staged_bin, command_name, target, path_entries=entries
                )
            except shims.ShimError as exc:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill {name!r} command {command_name!r} launcher "
                    f"cannot be staged: {exc}",
                ) from exc
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill {name!r} command {command_name!r} launcher "
                    f"cannot be staged: {exc}",
                ) from exc
            desired[(CLASS_BIN, command_name)] = staged_shim
        for command_name in active_build_commands(staged.spec):
            activation = bundle.activations.get(command_name)
            if activation is None:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill {name!r} command {command_name!r} has no staged "
                    "build activation",
                )
            try:
                staged_shim = shims.activate_build_command(
                    staged_bin, activation, path_entries=entries
                )
            except shims.ShimError as exc:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill {name!r} command {command_name!r} launcher "
                    f"cannot be staged: {exc}",
                ) from exc
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill {name!r} command {command_name!r} launcher "
                    f"cannot be staged: {exc}",
                ) from exc
            desired[(CLASS_BIN, command_name)] = staged_shim
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


def _serve_locked_git_member(
    member: ResolvedMember,
    package: NetworkGit,
    *,
    home: Path,
    recovery: modes.LockedGitRecovery | None = None,
    declared_url: str | None = None,
) -> tuple[snapshot.CapturedPackage | None, store.StoredSnapshot | None]:
    """Serve a locked Git member or capture its exact commit on a store miss.

    A present entry always wins and is never replaced. A missing entry
    may be recovered only with the locked-commit grant. The grant fetches
    ``package.commit`` directly and captures ``package.directory`` through
    the confined snapshot selection.
    """

    key = package_identity_sha256(package)
    stored: store.StoredSnapshot | None
    try:
        if recovery is None:
            stored = consumers.open_for_install(home, member.name, key)
        else:
            stored = consumers.open_for_install(
                home, member.name, key, allow_missing=True
            )
    except SourceError as exc:
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"Skill {member.name!r} snapshot for Git source "
            f"{package.repository!r} is unavailable: {exc}",
        ) from exc
    if stored is not None:
        return None, stored
    if recovery is None:
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"Skill {member.name!r} locked snapshot for Git source "
            f"{package.repository!r} is unavailable: no locked snapshot is stored",
        )

    label = f"Git source {package.repository!r}"
    if member.from_alias is not None:
        label += f" (alias {member.from_alias!r})"
    try:
        acquired = modes.acquire_locked_git_commit(
            recovery,
            identity=package.repository,
            commit=BuildLockedCommit(
                package.commit.object_format, package.commit.hex
            ),
            declared_url=declared_url,
            label=label,
        )
        with tempfile.TemporaryDirectory(prefix=".csk-locked-source-") as raw_tmp:
            repository_root = Path(raw_tmp) / "repository"
            acquired.materialize(repository_root)
            captured = snapshot.capture_package_snapshot(
                repository_root, package.directory, home=home
            )
    except SourceError as exc:
        if exc.code == source_transport.CODE_POLICY_INVALID:
            raise
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"Skill {member.name!r} locked commit {package.commit.hex} from "
            f"{label} is unavailable: {exc}",
        ) from exc
    except git_admission.GitAdmissionError as exc:
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"Skill {member.name!r} locked commit {package.commit.hex} from "
            f"{label} is unavailable: {exc}",
        ) from exc
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"Skill {member.name!r} locked commit {package.commit.hex} from "
            f"{label} is unavailable: {exc}",
        ) from exc
    return captured, None


def _capture_locked_member(
    member: ResolvedMember,
    package: PackageIdentity,
    *,
    home: Path,
    recovery: modes.LockedGitRecovery | None = None,
    declared_url: str | None = None,
) -> tuple[snapshot.CapturedPackage | None, store.StoredSnapshot | None]:
    """Serve one locked member's frozen bytes, revalidating live drift.

    The store is consulted FIRST. Present entries keep the existing
    drift checks and are never replaced. On an absent entry, install
    mode captures a path member or fetches the exact locked Git commit;
    it compares the lock before staging the recovered bytes. Read
    failures and present-but-invalid entries remain unavailable.
    """

    if isinstance(package, NetworkGit):
        return _serve_locked_git_member(
            member,
            package,
            home=home,
            recovery=recovery,
            declared_url=declared_url,
        )
    if not isinstance(package, LocalSnapshot):
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Skill {member.name!r} lock member carries an unsupported "
            "package identity",
        )
    if member.source_root is None:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill {member.name!r} lock member has no source root to verify",
        )
    key = package_identity_sha256(package)
    stored: store.StoredSnapshot | None
    try:
        if recovery is None:
            stored = consumers.open_for_install(home, member.name, key)
        else:
            stored = consumers.open_for_install(
                home, member.name, key, allow_missing=True
            )
    except SourceError as exc:
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"Skill {member.name!r} locked snapshot is unavailable: {exc}",
        ) from exc
    if stored is None:
        source_label = (
            f"path source {member.from_alias!r}"
            if member.from_alias is not None
            else f"path directory {member.directory!r}"
        )
        if member.source_root is None:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"Skill {member.name!r} from {source_label} has no source path",
            )
        try:
            captured = snapshot.capture_package_snapshot(
                member.source_root, member.directory, home=home
            )
        except SourceError as exc:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"Skill {member.name!r} source path {source_label} directory "
                f"{member.directory!r} is unavailable: {exc}",
            ) from exc
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"Skill {member.name!r} source path {source_label} directory "
                f"{member.directory!r} is unavailable: {exc}",
            ) from exc
        if captured.inventory["snapshot"] != package.snapshot:
            raise SourceError(
                CODE_SNAPSHOT_CHANGED,
                f"Skill {member.name!r} from {source_label} changed since the lock; "
                "run csk upgrade",
            )
        return captured, None
    if stored.snapshot != package.snapshot:
        raise SourceError(
            CODE_SNAPSHOT_UNAVAILABLE,
            f"Skill {member.name!r} locked snapshot cannot be served",
        )
    try:
        captured = snapshot.capture_package_snapshot(
            member.source_root, member.directory, home=home
        )
    except SourceError:
        return None, stored
    except _FS_ERRORS:
        return None, stored
    if captured.inventory["snapshot"] != package.snapshot:
        raise SourceError(
            CODE_SNAPSHOT_CHANGED,
            f"Skill {member.name!r} changed since the lock was written; "
            "run an explicit refresh",
        )
    return captured, stored


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


def _transitive_git_acquirer(
    mode: modes.ResolvingSources,
) -> Callable[[str, str, str], closure.SourceGitAcquisition]:
    """Build the closure's transitive Git acquisition over the mode.

    One ``(identity, commit)`` pair materializes once per operation
    into the resolving workspace; the shared acquisition memo serves
    every further requirement of the same pair without re-fetching.
    Every fetch goes through the bounded transport.
    """

    materialized_by_commit: dict[tuple[str, str], Path] = {}

    def acquire(
        git_url: str, commit: str, chain: str
    ) -> closure.SourceGitAcquisition:
        identity = canonical_source_identity(git_url)
        if identity is None:
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Git source {git_url!r} (via {chain}) has no network identity",
            )
        object_format = (
            "sha1" if len(commit) == 40 else "sha256" if len(commit) == 64 else ""
        )
        acquired = modes.acquire_git_commit(
            mode,
            identity=identity,
            commit=commit,
            object_format=object_format,
            declared_url=git_url,
            label=f"Requirement {git_url}@{commit[:12]}",
        )
        key = (identity, commit)
        materialized = materialized_by_commit.get(key)
        if materialized is None:
            materialized = mode.workspace / "transitive" / str(
                len(materialized_by_commit)
            )
            try:
                acquired.materialize(materialized)
            except git_admission.GitAdmissionError as exc:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Requirement {git_url}@{commit[:12]} acquired bytes "
                    f"cannot be staged: {exc}",
                ) from exc
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Requirement {git_url}@{commit[:12]} acquired bytes "
                    f"cannot be staged: {exc}",
                ) from exc
            materialized_by_commit[key] = materialized
        return closure.SourceGitAcquisition(
            commit=commit,
            object_format=object_format,
            identity=identity,
            materialized=materialized,
        )

    return acquire


def resolve_source_closure(
    config: GlobalConfig,
    members: tuple[ResolvedMember, ...],
    raw_captures: Mapping[str, snapshot.CapturedPackage],
    manifest_value: manifest.ProjectManifest,
    git_resolutions: Mapping[str, modes.AliasResolution],
    *,
    mode: modes.ResolvingSources,
) -> list[closure.ClosureNode]:
    """Expand resolved roots and their requirements into one closure.

    The traversal, unification, command validation and ordering are
    the existing closure itself; transitive requirements resolve
    through the bounded transport and fail on floating refs, name
    mismatches and conflicting identities. Command collisions are
    enforced across the full closure including transitive members.
    Runs in resolving mode only: frozen installs consume the lock
    membership without traversing.
    """

    closure_workspace = mode.workspace / "closure"
    roots: list[closure.SourceClosureRoot] = []
    for member in members:
        if member.from_alias is None or member.source_root is None:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {member.name!r} root member carries no selector",
            )
        acquisition = manifest_value.sources.get(member.from_alias)
        if acquisition is None:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {member.name!r} names source {member.from_alias!r}, "
                "which the manifest does not declare",
            )
        materialized = closure_workspace / member.name
        try:
            materialize_frozen(
                raw_captures[member.name].frozen_files(),
                materialized,
                subject=f"Skill {member.name!r}",
            )
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {member.name!r} frozen bytes cannot be staged: {exc}",
            ) from exc
        if isinstance(acquisition, skillfile_v2.PathSource):
            roots.append(
                closure.SourceClosureRoot(
                    name=member.name,
                    from_alias=member.from_alias,
                    directory=member.directory,
                    local=True,
                    materialized=materialized,
                    identity=None,
                    ref_kind=closure.SOURCE_LOCAL_REF_KIND,
                    ref_value=member.directory,
                    commit="",
                )
            )
        else:
            resolution = git_resolutions[member.from_alias]
            roots.append(
                closure.SourceClosureRoot(
                    name=member.name,
                    from_alias=member.from_alias,
                    directory=member.directory,
                    local=False,
                    materialized=materialized,
                    identity=resolution.identity,
                    ref_kind=acquisition.ref_kind,
                    ref_value=acquisition.ref_value,
                    commit=resolution.commit,
                )
            )
    nodes = closure.build_source_closure(
        config, roots, acquire_git=_transitive_git_acquirer(mode)
    )
    try:
        closure.detect_active_command_collisions(nodes)
    except closure.ClosureError as exc:
        raise SourceError(CODE_NAME_CONFLICT, str(exc)) from exc
    return nodes


def _require_frozen_lock_unchanged(
    old_lock: SkillfileLock,
    rebuilt: SkillfileLock,
    *,
    recovered_names: frozenset[str] = frozenset(),
) -> None:
    """Determinism guard: a frozen rebuild must describe the same closure.

    The comparison is order-insensitive (locks sort by UTF-8 name
    bytes, but readers accept any member order): what must match is
    the manifest digest and every member's name, selection,
    directory, package identity and content digest. A mismatch is a
    determinism violation and refuses; frozen mode never writes.
    """

    if old_lock.manifest_sha256 != rebuilt.manifest_sha256:
        raise SourceError(
            CODE_MEMBER_INVALID,
            "Frozen install rebuilt a lock for a different manifest; refusing",
        )
    old_members = sort_lock_members_by_utf8(old_lock.members)
    new_members = sort_lock_members_by_utf8(rebuilt.members)
    if old_members != new_members:
        rebuilt_by_name = {member.name: member for member in new_members}
        for old_member in old_members:
            rebuilt_member = rebuilt_by_name.get(old_member.name)
            if (
                old_member.name in recovered_names
                and rebuilt_member is not None
                and old_member.package == rebuilt_member.package
                and old_member.content_sha256 != rebuilt_member.content_sha256
            ):
                raise SourceError(
                    CODE_SNAPSHOT_CHANGED,
                    f"Skill {old_member.name!r} content from the recovered "
                    "locked source differs from the lock; run csk upgrade",
                )
        raise SourceError(
            CODE_MEMBER_INVALID,
            "Frozen install rebuilt a different lock closure; refusing",
        )


@dataclass(frozen=True)
class _Schema2Capture:
    """One install's capture outputs plus the planning reads behind them.

    Both lanes produce this bundle and the shared pipeline consumes
    it; it carries data only — members, captures, roots, the planning
    manifest reads — and no capability. The frozen lane fills
    ``node_specs`` and ``frozen_membership`` with ``None`` (no
    traversal ran, no membership was enumerated) and ``old_lock``
    with the frozen lock.
    """

    members: tuple[ResolvedMember, ...]
    captured_members: dict[str, CapturedMember]
    pred_files: dict[str, Mapping[str, snapshot.FrozenFile]]
    source_roots: dict[str, Path]
    node_specs: dict[str, skillspec.SkillSpec] | None
    frozen_membership: dict[int, tuple[str, ...]] | None
    old_lock: SkillfileLock | None
    manifest_sha: str
    selectors: list[skillfile_v2.SkillSelector]
    git_aliases: frozenset[str]


def _read_planning_inputs(
    project_path: Path,
) -> tuple[
    manifest.ProjectManifest, str, list[skillfile_v2.SkillSelector], frozenset[str]
]:
    """Read the manifest once for planning: manifest, digest, selectors, Git aliases."""

    _manifest_data, fresh = _read_fresh_manifest(project_path)
    if fresh.schema_version != 2 or fresh.manifest_sha256 is None:
        raise _concurrent_state_change("Skillfile changed during install planning")
    manifest_sha = fresh.manifest_sha256
    selectors = list(fresh.selectors)
    git_aliases = frozenset(
        source_alias
        for source_alias, acquisition in fresh.sources.items()
        if not isinstance(acquisition, skillfile_v2.PathSource)
    )
    return fresh, manifest_sha, selectors, git_aliases


def _serve_frozen_publication_files(
    captured_members: dict[str, CapturedMember], *, home: Path
) -> dict[str, Mapping[str, snapshot.FrozenFile]]:
    """Prepare frozen publication bytes, including verified store misses.

    Existing snapshots are re-read and verified from the store. A
    recovered missing entry uses its confined capture temporarily;
    the caller stages that capture only after rebuilt lock equality.
    """

    frozen_files: dict[str, Mapping[str, snapshot.FrozenFile]] = {}
    for name, captured_member in captured_members.items():
        if captured_member.store_missing:
            if captured_member.captured is None:
                raise SourceError(
                    CODE_SNAPSHOT_UNAVAILABLE,
                    f"Skill {name!r} has no captured bytes for its missing snapshot",
                )
            frozen_files[name] = captured_member.captured.frozen_files()
            continue
        try:
            served = consumers.open_for_install(
                home, name, captured_member.package_key
            )
        except SourceError as exc:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"Skill {name!r} locked snapshot cannot be served: {exc}",
            ) from exc
        if (
            isinstance(captured_member.package, LocalSnapshot)
            and served.snapshot != captured_member.package.snapshot
        ):
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"Skill {name!r} locked snapshot cannot be served",
            )
        frozen_files[name] = served.files
    return frozen_files


def _stage_recovered_locked_snapshots(
    captured_members: dict[str, CapturedMember],
    *,
    home: Path,
    home_lock: locking.ManagerHomeLock,
) -> None:
    """Store missing locked snapshots after the rebuilt lock matches.

    The caller holds the manager-home lock. The store checks absence again
    under that same lock and refuses to replace an entry that appeared
    after capture.
    """

    for name, captured_member in captured_members.items():
        if not captured_member.store_missing:
            continue
        captured = captured_member.captured
        if captured is None:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"Skill {name!r} has no captured bytes for its missing snapshot",
            )
        try:
            served = store.stage_snapshot_if_missing(
                home,
                name,
                captured_member.package_key,
                captured,
                home_lock=home_lock,
            )
        except SourceError:
            raise
        if served.snapshot != captured.inventory["snapshot"]:
            raise SourceError(
                CODE_SNAPSHOT_UNAVAILABLE,
                f"Skill {name!r} locked snapshot appeared with different stored bytes",
            )


def _serve_resolving_publication_files(
    captured_members: dict[str, CapturedMember], *, home: Path
) -> dict[str, Mapping[str, snapshot.FrozenFile]]:
    """Serve resolving publication bytes, healing superseded records.

    Publication consumes the frozen store copy, rehashed here. A
    superseded record heals from verified captured bytes, which the
    resolving lane captured moments ago from the admitted sources.
    Git members serve key-bound from the store with no live bytes to
    heal from. Only the resolving lane calls this function.
    """

    frozen_files: dict[str, Mapping[str, snapshot.FrozenFile]] = {}
    for name, captured_member in captured_members.items():
        if not isinstance(captured_member.package, LocalSnapshot):
            try:
                served_git = consumers.open_for_install(
                    home, name, captured_member.package_key
                )
            except SourceError as exc:
                raise SourceError(
                    CODE_SNAPSHOT_UNAVAILABLE,
                    f"Skill {name!r} locked snapshot cannot be "
                    f"served: {exc}",
                ) from exc
            frozen_files[name] = served_git.files
            continue
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
    return frozen_files


def install_schema2(
    *,
    mode: modes.ResolvingSources | modes.FrozenSources,
    home: Path,
    project_path: Path,
    alias: str,
    agents: list[str],
    locale_value: str | None,
    adapter_mode: str,
    dry_run: bool,
    config: GlobalConfig,
    operator_search_path: OperatorSearchPath | None,
    substitutions: DevManifest,
    ssh_credentials: OperatorSSHCredentials | None = None,
    https_token: OperatorHTTPSToken | None = None,
    interactive: bool = False,
) -> Schema2InstallResult:
    """Install one schema-2 project: resolve, freeze, publish atomically.

    Initial install creates the lock after all gates succeed; locked
    install consumes the locked snapshots and refuses drift instead of
    re-resolving; explicit refresh re-runs every gate and atomically
    replaces the lock and markers only after success. Every failure
    preserves the prior lock, markers and installed state.

    Members with script commands materialize their frozen runtime
    into the protected store with shims in ``.agents/bin``; members
    with build commands additionally run the existing build lanes
    with the member package bound, and their receipt-3 records join
    the markers. Capabilities, dependencies, system-command
    readiness, the script execution policy, toolchain admission and
    the assurance gate all run before any write.

    The caller decides the mode with :func:`modes.select` and passes
    only the mode: this function takes no fetch flag, no transport
    grant and no policy path, and each lane below receives only the
    mode type it runs under.
    """

    if isinstance(mode, modes.FrozenSources):
        return _install_schema2_frozen(
            mode=mode,
            home=home,
            project_path=project_path,
            alias=alias,
            agents=agents,
            locale_value=locale_value,
            adapter_mode=adapter_mode,
            dry_run=dry_run,
            config=config,
            operator_search_path=operator_search_path,
            substitutions=substitutions,
            ssh_credentials=ssh_credentials,
            https_token=https_token,
            interactive=interactive,
        )
    return _install_schema2_resolving(
        mode=mode,
        home=home,
        project_path=project_path,
        alias=alias,
        agents=agents,
        locale_value=locale_value,
        adapter_mode=adapter_mode,
        dry_run=dry_run,
        config=config,
        operator_search_path=operator_search_path,
        substitutions=substitutions,
        ssh_credentials=ssh_credentials,
        https_token=https_token,
        interactive=interactive,
    )


def _install_schema2_frozen(
    *,
    mode: modes.FrozenSources,
    home: Path,
    project_path: Path,
    alias: str,
    agents: list[str],
    locale_value: str | None,
    adapter_mode: str,
    dry_run: bool,
    config: GlobalConfig,
    operator_search_path: OperatorSearchPath | None,
    substitutions: DevManifest,
    ssh_credentials: OperatorSSHCredentials | None = None,
    https_token: OperatorHTTPSToken | None = None,
    interactive: bool = False,
) -> Schema2InstallResult:
    """Run one locked install: frozen bytes in, published outputs out.

    This lane receives the home directory, lock and narrow recovery
    grant. It derives members from the lock and never expands selectors
    or resolves refs. A missing store entry is captured from the path or
    exact locked Git commit, then compared before recovery is staged.
    The shared pipeline plans no lock target for frozen mode.
    """

    fresh, manifest_sha, selectors, git_aliases = _read_planning_inputs(project_path)
    validate_lock(mode.lock, current_manifest_sha256=manifest_sha)
    source_roots = {
        source_alias: resolve_source_root(
            project_path, source_alias, acquisition
        )
        for source_alias, acquisition in fresh.sources.items()
        if isinstance(acquisition, skillfile_v2.PathSource)
    }
    members = locked_schema2_members(mode, selectors, source_roots)
    lock_packages = {
        lock_member.name: lock_member.package for lock_member in mode.lock.members
    }
    captured_members: dict[str, CapturedMember] = {}
    pred_files: dict[str, Mapping[str, snapshot.FrozenFile]] = {}
    for member in members:
        package = lock_packages[member.name]
        acquisition = fresh.sources.get(member.from_alias or "")
        declared_url = (
            acquisition.git
            if isinstance(acquisition, skillfile_v2.GitSource)
            else None
        )
        raw, served = _capture_locked_member(
            member,
            package,
            home=home,
            recovery=mode.git_recovery,
            declared_url=declared_url,
        )
        captured_members[member.name] = CapturedMember(
            member=member,
            captured=raw,
            package=package,
            package_key=package_identity_sha256(package),
            store_missing=served is None,
        )
        if served is None:
            if raw is None:
                raise SourceError(
                    CODE_SNAPSHOT_UNAVAILABLE,
                    f"Skill {member.name!r} has no recovered locked snapshot",
                )
            pred_files[member.name] = raw.frozen_files()
        else:
            pred_files[member.name] = served.files
    capture = _Schema2Capture(
        members=members,
        captured_members=captured_members,
        pred_files=pred_files,
        source_roots=source_roots,
        node_specs=None,
        frozen_membership=None,
        old_lock=mode.lock,
        manifest_sha=manifest_sha,
        selectors=selectors,
        git_aliases=git_aliases,
    )
    return _plan_and_publish_schema2(
        mode=mode,
        home=home,
        project_path=project_path,
        alias=alias,
        agents=agents,
        locale_value=locale_value,
        adapter_mode=adapter_mode,
        dry_run=dry_run,
        config=config,
        operator_search_path=operator_search_path,
        substitutions=substitutions,
        ssh_credentials=ssh_credentials,
        https_token=https_token,
        interactive=interactive,
        capture=capture,
    )


def _install_schema2_resolving(
    *,
    mode: modes.ResolvingSources,
    home: Path,
    project_path: Path,
    alias: str,
    agents: list[str],
    locale_value: str | None,
    adapter_mode: str,
    dry_run: bool,
    config: GlobalConfig,
    operator_search_path: OperatorSearchPath | None,
    substitutions: DevManifest,
    ssh_credentials: OperatorSSHCredentials | None = None,
    https_token: OperatorHTTPSToken | None = None,
    interactive: bool = False,
) -> Schema2InstallResult:
    """Run one initial resolve or explicit refresh, then publish.

    This lane holds the resolving capability: it re-enumerates the
    admitted member set, resolves refs, captures snapshots,
    re-resolves the closure, re-runs every gate, and atomically
    replaces the lock and markers only after success. The acquisition
    workspace lives on the mode; the caller owns its lifetime.
    """

    fresh, manifest_sha, selectors, git_aliases = _read_planning_inputs(project_path)
    old_lock = read_schema2_lock(project_path)
    source_roots, git_resolutions = resolve_schema2_source_roots(
        project_path, fresh, mode=mode
    )
    members = resolve_schema2_members(
        project_path,
        fresh,
        mode=mode,
        source_roots=source_roots,
        git_resolutions=git_resolutions,
    )
    frozen_membership = collection_membership(
        selectors, source_roots, git_aliases
    )
    raw_captures = capture_schema2_members(members, home=home)
    require_membership_unchanged(
        frozen_membership,
        collection_membership(selectors, source_roots, git_aliases),
    )
    closure_nodes = resolve_source_closure(
        config,
        members,
        raw_captures,
        fresh,
        git_resolutions,
        mode=mode,
    )
    node_specs = {node.name: node.spec for node in closure_nodes}
    captured_members: dict[str, CapturedMember] = {}
    pred_files: dict[str, Mapping[str, snapshot.FrozenFile]] = {}
    for member in members:
        raw = raw_captures[member.name]
        acquisition = fresh.sources.get(member.from_alias or "")
        if acquisition is not None and not isinstance(
            acquisition, skillfile_v2.PathSource
        ):
            resolution = git_resolutions[member.from_alias or ""]
            assert resolution.object_format in ("sha1", "sha256")
            resolved_format: Literal["sha1", "sha256"] = (
                "sha1" if resolution.object_format == "sha1" else "sha256"
            )
            root_package: PackageIdentity = NetworkGit(
                repository=resolution.identity,
                commit=LockedCommit(
                    resolved_format, resolution.commit
                ),
                directory=member.directory,
            )
        else:
            root_package = LocalSnapshot(snapshot=raw.inventory["snapshot"])
        captured_members[member.name] = CapturedMember(
            member=member,
            captured=raw,
            package=root_package,
            package_key=package_identity_sha256(root_package),
        )
        pred_files[member.name] = raw.frozen_files()
    root_names = {member.name for member in members}
    transitive: list[ResolvedMember] = []
    for node in closure_nodes:
        if node.name in root_names:
            continue
        raw = capture_transitive_member(
            node.snapshot, name=node.name, home=home
        )
        assert node.identity is not None
        transitive_package = NetworkGit(
            repository=node.identity,
            commit=LockedCommit(
                "sha1" if len(node.resolved.commit) == 40 else "sha256",
                node.resolved.commit,
            ),
            directory=".",
        )
        transitive_member = ResolvedMember(
            name=node.name,
            from_alias=None,
            directory=".",
            selection_ordinal=None,
            source_root=None,
        )
        transitive.append(transitive_member)
        captured_members[node.name] = CapturedMember(
            member=transitive_member,
            captured=raw,
            package=transitive_package,
            package_key=package_identity_sha256(transitive_package),
        )
        pred_files[node.name] = raw.frozen_files()
    members = (*members, *transitive)
    if not dry_run:
        stage_schema2_store(home, captured_members)
    capture = _Schema2Capture(
        members=members,
        captured_members=captured_members,
        pred_files=pred_files,
        source_roots=source_roots,
        node_specs=node_specs,
        frozen_membership=frozen_membership,
        old_lock=old_lock,
        manifest_sha=manifest_sha,
        selectors=selectors,
        git_aliases=git_aliases,
    )
    return _plan_and_publish_schema2(
        mode=mode,
        home=home,
        project_path=project_path,
        alias=alias,
        agents=agents,
        locale_value=locale_value,
        adapter_mode=adapter_mode,
        dry_run=dry_run,
        config=config,
        operator_search_path=operator_search_path,
        substitutions=substitutions,
        ssh_credentials=ssh_credentials,
        https_token=https_token,
        interactive=interactive,
        capture=capture,
    )


def _plan_and_publish_schema2(
    *,
    mode: modes.ResolvingSources | modes.FrozenSources,
    home: Path,
    project_path: Path,
    alias: str,
    agents: list[str],
    locale_value: str | None,
    adapter_mode: str,
    dry_run: bool,
    config: GlobalConfig,
    operator_search_path: OperatorSearchPath | None,
    substitutions: DevManifest,
    ssh_credentials: OperatorSSHCredentials | None,
    https_token: OperatorHTTPSToken | None,
    interactive: bool,
    capture: _Schema2Capture,
) -> Schema2InstallResult:
    """Run the shared gates, builds, planning and atomic publication.

    Both lanes converge here once their captures are frozen bytes:
    specs, command gates, audit, builds, target planning, and the
    single atomic commit. The only mode dispatch left is on the mode
    TYPE — the lock target is planned for resolving modes only, and
    publication bytes heal superseded records for resolving modes
    only. This function takes no fetch flag, no transport grant and
    no policy path.
    """

    members = capture.members
    captured_members = capture.captured_members
    pred_files = capture.pred_files
    source_roots = capture.source_roots
    node_specs = capture.node_specs
    frozen_membership = capture.frozen_membership
    old_lock = capture.old_lock
    manifest_sha = capture.manifest_sha
    selectors = capture.selectors
    git_aliases = capture.git_aliases
    project_identity = locking.canonical_project_identity(project_path)
    slug = project_slug(project_identity)
    refresh = old_lock is not None and isinstance(mode, modes.ResolvingSources)

    with (
        tempfile.TemporaryDirectory(prefix=".csk-source-install-") as staging_tmp,
        ExitStack() as build_stack,
    ):
        staging_root = Path(staging_tmp)
        specs_map: dict[str, skillspec.SkillSpec] = {}
        satisfied_by = frozenset(pred_files)
        for name in sorted(pred_files):
            frozen_dir = staging_root / "predspec" / name
            materialize_frozen(
                pred_files[name],
                frozen_dir,
                subject=f"Skill {name!r}",
            )
            if node_specs is not None and name in node_specs:
                # The closure already loaded this spec from the same
                # captured bytes the store verified; reuse it instead of
                # parsing the same bytes twice.
                spec = node_specs[name]
            else:
                spec = load_member_spec(frozen_dir, name=name)
            require_installable_member(spec, name=name, satisfied_by=satisfied_by)
            specs_map[name] = spec
        check_schema2_requirement_commands(specs_map)
        run_schema2_command_gates(members, specs_map)
        check_schema2_registry_requirement(members, captured_members, config)
        adapted = tuple(
            adapt_schema2_member(
                member,
                specs_map[member.name],
                staging_root / "predspec" / member.name,
                captured_members[member.name].package,
            )
            for member in members
        )
        gate = run_schema2_audit_gate(adapted, config, alias=alias, dry_run=dry_run)
        audit_policy = source_audit.policy_from_config(
            config, script_policy=skillspec.SCRIPT_WORKER_V1_POLICY
        )
        # Frozen providers, compiler sessions and operation roots live on
        # the install's stack, like the legacy lane: compilation happens
        # before the home lock but its operation roots must survive until
        # cache publication under the lock.
        providers = local_build_providers(
            tuple(
                FrozenBuildMember(
                    name=member.name,
                    spec=specs_map[member.name],
                    frozen_dir=staging_root / "predspec" / member.name,
                    package=captured_members[member.name].package,
                )
                for member in members
            ),
            stack=build_stack,
        )
        if providers and not config.audit.enabled:
            raise SourceError(
                CODE_MEMBER_INVALID,
                "local builds need audit enabled: the source audit "
                "binding cannot be recorded while audit is disabled",
            )
        record_home = home
        if dry_run and providers:
            record_home = staging_root / "audit-dry-run"
            try:
                record_home.mkdir(parents=True, exist_ok=True)
            except _FS_ERRORS as exc:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"dry-run audit binding cannot be staged: {exc}",
                ) from exc
        record_schema2_source_audits(
            gate,
            adapted,
            captured_members,
            record_home=record_home,
            policy=audit_policy,
        )
        hook = partial(
            source_audit.source_audit_plan_hook,
            csk_home=record_home,
            policy=audit_policy,
        )
        script_owners = {
            command: member.name
            for member in members
            for command in active_script_commands(specs_map[member.name])
        }
        cache_backend = build_cache.cache_for_manager_home(home)
        plans = plan_schema2_local_builds(
            providers,
            home=home,
            operator_search_path=operator_search_path,
            forbidden_roots=(
                project_path,
                config.skills_root,
                *sorted(source_roots.values()),
            ),
            cache_backend=cache_backend,
            occupied=script_owners,
            audit=hook,
        )
        provider_identities = {
            provider.name: provider.snapshot.identity for provider in providers
        }

        package_keys = {name: item.package_key for name, item in captured_members.items()}
        new_runtime_refs = runtime_hex_pairs(
            {(name, package_keys[name]) for name in captured_members}
        )
        old_runtime_refs = (
            _referenced_runtime_keys(old_lock) if old_lock is not None else set()
        )
        planned, adapter_targets, plan_messages = plan_schema2_targets(
            mode=mode,
            home=home,
            project_path=project_path,
            slug=slug,
            alias=alias,
            agents=agents,
            members=members,
            package_keys=package_keys,
            specs_map=specs_map,
            new_runtime_refs=new_runtime_refs,
            old_runtime_refs=old_runtime_refs,
            git_aliases=git_aliases,
        )
        try:
            preimages = digest_live_targets(planned)
        except TransactionError as exc:
            raise _concurrent_state_change(
                "shared install state changed before the atomic commit"
            ) from exc

        external_outputs, external_messages = publish_schema2_external_builds(
            config,
            project_path,
            [item.node for item in adapted],
            specs_map,
            captured_members,
            substitutions,
            operator_search_path,
            build_stack,
            home=home,
            dry_run=dry_run,
            ssh_credentials=ssh_credentials,
            https_token=https_token,
            interactive=interactive,
        )

        if dry_run:
            messages = list(plan_messages)
            messages.extend(gate.warnings)
            messages.extend(external_messages)
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

        publications: dict[str, build_cache.CachePublication] = {}
        if plans:
            if operator_search_path is None:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    "local builds need the captured operator search path",
                )
            publications = compile_schema2_builds(
                config,
                [item.node for item in adapted],
                providers,
                plans,
                operator_search_path,
                cache_backend,
                build_stack,
                project_path=project_path,
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
                    collection_membership(selectors, source_roots, git_aliases),
                )
            admitted = collect_admitted_identities(members, git_aliases)

            # Publication consumes verified stored bytes, or the captured
            # candidate for a missing frozen entry. Recovered candidates
            # remain unstaged until the rebuilt lock matches below.
            if isinstance(mode, modes.FrozenSources):
                frozen_files = _serve_frozen_publication_files(
                    captured_members, home=home
                )
            else:
                frozen_files = _serve_resolving_publication_files(
                    captured_members, home=home
                )

            local_outputs = publish_schema2_local_builds(
                home,
                plans,
                publications,
                cache_backend,
                home_lock,
                specs_map,
                provider_identities=provider_identities,
            )
            build_outputs = merge_build_outputs(local_outputs, external_outputs)
            staged = stage_schema2_desired(
                staging_root,
                mode=mode,
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
                build_outputs=build_outputs,
                git_aliases=git_aliases,
            )
            if isinstance(mode, modes.FrozenSources):
                # The planner excluded the lock target structurally. The
                # rebuilt lock must still describe the same closure before
                # a missing snapshot is stored.
                recovered_names = frozenset(
                    name
                    for name, captured_member in captured_members.items()
                    if captured_member.store_missing
                )
                _require_frozen_lock_unchanged(
                    mode.lock,
                    staged.new_lock,
                    recovered_names=recovered_names,
                )
                _stage_recovered_locked_snapshots(
                    captured_members,
                    home=home,
                    home_lock=home_lock,
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

    installed = _touched_members(
        committed, schema2_command_owners(members, specs_map)
    )
    removed = _removed_names(old_lock, staged.new_lock)
    up_to_date = tuple(
        sorted(set(captured_members) - set(installed) - set(removed))
    )
    lock_replaced = (CLASS_LOCK, "Skillfile.lock.json") in committed
    messages = list(plan_messages)
    messages.extend(gate.warnings)
    messages.extend(external_messages)
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
                home=home,
                slug=slug,
                alias=alias,
                members=members,
                source_roots=source_roots,
                git_aliases=git_aliases,
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
    command_owners: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Map committed target keys to the member names they publish.

    Command shims attribute through the caller-supplied owner map;
    without one a shim key touches no member and surfaces as a
    global output instead.
    """

    touched: set[str] = set()
    for target_class, identifier in committed:
        if target_class == CLASS_CONTEXT and identifier.startswith("project/"):
            touched.add(identifier[len("project/"):])
        elif target_class == CLASS_RUNTIME and "/" in identifier:
            touched.add(identifier.split("/", 1)[0])
        elif target_class == CLASS_BIN and command_owners is not None:
            owner = command_owners.get(identifier)
            if owner is not None:
                touched.add(owner)
        elif target_class == CLASS_ADAPTER_LEDGER and "/entry/" in identifier:
            touched.add(identifier.split("/entry/", 1)[1])
    return tuple(sorted(touched))


def schema2_command_owners(
    members: tuple[ResolvedMember, ...],
    specs_map: dict[str, skillspec.SkillSpec],
) -> dict[str, str]:
    """Map every active command to its owning member.

    Command gates run before planning, so ownership here is unique;
    a repeated name fails closed instead of attributing either
    owner.
    """

    owners: dict[str, str] = {}
    for member in members:
        spec = specs_map[member.name]
        for command_name in sorted(
            set(active_script_commands(spec)) | set(active_build_commands(spec))
        ):
            claim_schema2_command_owner(owners, command_name, member.name)
    return owners


# ---------------------------------------------------------------------------
# Schema-2 builds: frozen members feeding the existing build lanes.
#
# Local acquisition feeds the complete existing package pipeline. Members
# adapt to closure nodes and skill plans over their frozen bytes (never the
# live authored tree); local go-v1 commands plan through the build planner
# with the source-audit hook, compile on cache misses, and publish receipt-3
# artifacts into the immutable cache; external go-repository-v1 commands run
# the existing repository pipeline with the member package bound. Markers
# carry receipt-3 build records and shims launch the protected artifacts.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Schema2BuildOutputs:
    """Published or reconstructed build evidence for one install.

    Install assembles this from compilation and cache publication;
    status reconstructs it read-only from re-planning and protected
    store reads. Either way the marker plans and the staged shims
    derive from the same bundle.
    """

    marker_builds: Mapping[str, Mapping[str, install_marker.InstallMarkerBuildV5]]
    activations: Mapping[str, shims.BuildCommandActivation]
    build_sources: Mapping[str, build_source.BuildSourceIdentity | None]


def empty_build_outputs() -> Schema2BuildOutputs:
    """Return the build bundle for members without build commands."""

    return Schema2BuildOutputs(marker_builds={}, activations={}, build_sources={})


@dataclass(frozen=True)
class AdaptedSchema2Member:
    """One member adapted to the existing closure/audit/build lanes."""

    member: ResolvedMember
    spec: skillspec.SkillSpec
    node: closure.ClosureNode
    # installer.SkillPlan under TYPE_CHECKING would still be a runtime
    # import cycle; the plan is built by a lazy installer import below and
    # only audit/build lanes consume it.
    plan: Any


def adapt_schema2_member(
    member: ResolvedMember,
    spec: skillspec.SkillSpec,
    frozen_dir: Path,
    package: PackageIdentity,
) -> AdaptedSchema2Member:
    """Adapt one member to a closure node and skill plan over frozen bytes.

    The node carries a full activation edge: schema-2 selection has no
    partial activation, so every member activates all its commands.
    Provenance labels name the selection (``local:<alias>/<dir>`` for
    path members, ``git:<alias>/<dir>`` for Git roots,
    ``transitive:<name>`` for closure members) and the frozen
    identity; no git identity is forged for local content, and Git
    members carry their canonical repository identity.
    """

    from ..installer import SkillPlan

    if isinstance(package, LocalSnapshot):
        label = f"local:{member.from_alias}/{member.directory}"
        ref = manifest.SkillRef(kind="snapshot", value=package.snapshot)
        resolved = git_ops.ResolvedRef(
            kind="snapshot",
            ref=package.snapshot,
            commit=package.snapshot.removeprefix("sha256:"),
        )
        git: str | None = None
        identity: str | None = None
    elif isinstance(package, NetworkGit):
        if member.from_alias is None:
            label = f"transitive:{member.name}"
        else:
            label = f"git:{member.from_alias}/{member.directory}"
        ref = manifest.SkillRef(kind="revision", value=package.commit.hex)
        resolved = git_ops.ResolvedRef(
            kind="revision", ref=package.commit.hex, commit=package.commit.hex
        )
        git = package.repository
        identity = package.repository
    else:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Skill {member.name!r} carries an unsupported package identity",
        )
    decl = manifest.SkillDecl(
        name=member.name,
        source=label,
        ref=ref,
        git=git,
    )
    # Local members adapt over their live source root; Git source roots
    # are private acquisition workspace paths that do not outlive
    # resolution, so Git members adapt over their frozen bytes, which
    # the audit gate consumes.
    if isinstance(package, LocalSnapshot) and member.source_root is not None:
        repo = member.source_root
    else:
        repo = frozen_dir
    node = closure.ClosureNode(
        name=member.name,
        decl=decl,
        resolved=resolved,
        repo=repo,
        snapshot=frozen_dir,
        spec=spec,
        identity=identity,
        chains=[],
        substituted=None,
        edges=[
            closure.ActivationEdge(
                consumer="project", mode="full", commands=()
            )
        ],
    )
    plan = SkillPlan(
        decl=decl, resolved=resolved, repo=repo,
        snapshot=frozen_dir, spec=spec,
    )
    return AdaptedSchema2Member(member=member, spec=spec, node=node, plan=plan)


def check_schema2_registry_requirement(
    members: tuple[ResolvedMember, ...],
    captured: Mapping[str, CapturedMember],
    config: GlobalConfig,
) -> None:
    """Refuse local members where policy requires a network attestation.

    Mirrors the legacy lane's trigger exactly: configured trusted
    registries under a strict registry policy require an attestation
    local content cannot supply. The checker's own structured error
    propagates unchanged so the conformance case keeps its code.
    """

    required = (
        config.audit.registry_policy == "strict"
        and bool(config.trusted_registries())
    )
    for member in members:
        install_marker.check_local_registry_requirement(
            captured[member.name].package,
            network_attestation_required=required,
        )


def run_schema2_audit_gate(
    adapted: tuple[AdaptedSchema2Member, ...],
    config: GlobalConfig,
    *,
    alias: str,
    dry_run: bool,
) -> GateResult:
    """Run the existing assurance gate over the selected members.

    The gate is the legacy lane's own call over the adapted plans:
    static detectors, pins, revocations and backends run over the
    frozen bytes, and a block refuses with the gate's message. Audit
    records persist exactly when this is not a dry run.
    """

    # Imported lazily: the audit pipeline imports installer, which imports
    # this module; a top-level import here would fail on the partially
    # initialized installer. The builds/metadata module lazy-imports the
    # sources package for the same reason.
    from ..audit import pipeline as audit_pipeline

    gate = audit_pipeline.gate_plans(
        [item.plan for item in adapted],
        config,
        scope=alias,
        record=not dry_run,
    )
    if gate.blocked:
        raise SourceError(CODE_MEMBER_INVALID, "; ".join(gate.errors))
    return gate


def record_schema2_source_audits(
    gate: GateResult,
    adapted: tuple[AdaptedSchema2Member, ...],
    captured: Mapping[str, CapturedMember],
    *,
    record_home: Path,
    policy: source_audit.SourceAuditPolicy,
) -> None:
    """Persist one source audit per member with local builds.

    The stored report is what the planning hook re-validates before
    any toolchain probe or cache read. Members without local builds
    record nothing: the gate above is their whole preflight.
    """

    reports = {report.skill: report for report in gate.reports}
    for item in adapted:
        if not _local_build_command_names(item.spec):
            continue
        report = reports.get(item.member.name)
        if report is None:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {item.member.name!r} produced no audit report; "
                "local builds cannot be bound without one",
            )
        member_package = captured[item.member.name].package
        source_audit.record_source_audit(
            report,
            csk_home=record_home,
            package=member_package,
            git=(
                member_package.repository
                if isinstance(member_package, NetworkGit)
                else None
            ),
            policy=policy,
        )


def _local_build_command_names(spec: skillspec.SkillSpec) -> tuple[str, ...]:
    """Return the sorted local go-v1 build commands one member activates."""

    return tuple(
        sorted(
            command.name
            for command in spec.commands.values()
            if command.type == "build"
            and command.driver == build_metadata.GO_V1_DRIVER
        )
    )


def _external_build_command_names(spec: skillspec.SkillSpec) -> tuple[str, ...]:
    """Return the sorted external build commands one member activates."""

    return tuple(
        sorted(
            command.name
            for command in spec.commands.values()
            if command.type == "build"
            and command.driver != build_metadata.GO_V1_DRIVER
        )
    )


@dataclass(frozen=True)
class FrozenBuildMember:
    """The frozen inputs one local build provider freezes."""

    name: str
    spec: skillspec.SkillSpec
    frozen_dir: Path
    package: PackageIdentity


def local_build_providers(
    frozen_members: tuple[FrozenBuildMember, ...],
    *,
    stack: ExitStack,
) -> tuple[build_planner.BuildProvider, ...]:
    """Freeze one build provider per member with local builds.

    Each provider freezes the member's frozen bytes (never the live
    tree) and carries the member package, which selects receipt-3
    planning, cache namespaces and markers downstream.
    """

    providers: list[build_planner.BuildProvider] = []
    for item in frozen_members:
        names = _local_build_command_names(item.spec)
        if not names:
            continue
        try:
            frozen = stack.enter_context(
                build_source.freeze_snapshot(item.frozen_dir)
            )
        except build_planner.BuildPlanningError as exc:
            raise SourceError(CODE_MEMBER_INVALID, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {item.name!r} frozen source cannot be used "
                f"for builds: {exc}",
            ) from exc
        try:
            providers.append(
                build_planner.provider_from_spec(
                    item.name,
                    frozen,
                    item.spec,
                    active_commands=names,
                    package=item.package,
                )
            )
        except build_planner.BuildPlanningError as exc:
            raise SourceError(CODE_MEMBER_INVALID, str(exc)) from exc
    return tuple(providers)


def plan_schema2_local_builds(
    providers: tuple[build_planner.BuildProvider, ...],
    *,
    home: Path,
    operator_search_path: OperatorSearchPath | None,
    forbidden_roots: tuple[Path, ...],
    cache_backend: build_cache.BuildCacheBackend,
    occupied: Mapping[str, str],
    audit: Callable[[tuple[build_planner.BuildProvider, ...]], None],
    read_only: bool = False,
) -> tuple[build_planner.BuildPlan, ...]:
    """Plan local builds with the source-audit hook first.

    Command collisions refuse before planning; the audit hook then
    runs inside planning before any toolchain probe or cache read.
    ``concurrent_state_change`` propagates as a planning error for
    the installer's retry protocol, everything else becomes a
    structured source refusal.
    """

    if not providers:
        return ()
    if operator_search_path is None:
        raise SourceError(
            CODE_MEMBER_INVALID,
            "local builds need the captured operator search path",
        )
    try:
        build_planner.detect_command_collisions(providers, occupied=occupied)
    except build_planner.BuildPlanningError as exc:
        raise SourceError(CODE_NAME_CONFLICT, str(exc)) from exc
    try:
        return build_planner.plan_builds(
            providers,
            manager_home=home,
            operator_search_path=operator_search_path,
            forbidden_roots=forbidden_roots,
            cache_backend=cache_backend,
            audit=audit,
            read_only_preflight=read_only,
        )
    except build_planner.BuildPlanningError as exc:
        if exc.code == "concurrent_state_change":
            raise
        raise SourceError(CODE_MEMBER_INVALID, str(exc)) from exc


def compile_schema2_builds(
    config: GlobalConfig,
    nodes: list[closure.ClosureNode],
    providers: tuple[build_planner.BuildProvider, ...],
    plans: tuple[build_planner.BuildPlan, ...],
    operator_search_path: OperatorSearchPath,
    cache_backend: build_cache.BuildCacheBackend,
    stack: ExitStack,
    *,
    project_path: Path,
) -> dict[str, build_cache.CachePublication]:
    """Compile planned local builds that miss the cache.

    The worker boundary is the legacy lane's own: per-key worker
    locks, a private native session, and receipt-3 construction from
    the wrapped input. Legacy inputs cannot reach this lane and
    refuse if they do.
    """

    # Imported lazily for the installer cycle documented above.
    from ..installer import InstallError, _build_private_misses

    try:
        return _build_private_misses(
            config,
            nodes,
            providers,
            plans,
            operator_search_path,
            cache_backend,
            stack,
            operation_roots=(project_path,),
            allow_source_aware=True,
        )
    except build_planner.BuildPlanningError:
        raise
    except InstallError as exc:
        raise SourceError(CODE_MEMBER_INVALID, str(exc)) from exc


def marker_build_v5_from_inspection(
    provider_name: str,
    command_name: str,
    plan: build_planner.BuildPlan,
    inspection: build_cache.CacheInspection,
) -> install_marker.InstallMarkerBuildV5:
    """Build one local receipt-3 marker record from a cache HIT.

    Mirrors the legacy lane's winner checks: the inspection must
    carry a receipt-3 record, its hash, and a physical artifact
    path, and the record fields come from the receipt itself.
    """

    receipt = inspection.receipt
    if not isinstance(receipt, build_metadata.BuildReceiptV3):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill {provider_name!r} command {command_name!r} cache entry "
            "is not a receipt-3 record",
        )
    if inspection.receipt_sha256 is None or inspection.artifact_path is None:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill {provider_name!r} command {command_name!r} cache did "
            "not yield a verified winner",
        )
    try:
        return install_marker.InstallMarkerBuildV5(
            driver=build_metadata.GO_V1_DRIVER,
            receipt_schema_version=3,
            execution_policy=build_metadata.PORTABLE_EXECUTION_POLICY,
            cache_key=plan.cache_key,
            receipt_sha256=inspection.receipt_sha256,
            artifact_sha256=receipt.artifact.sha256,
            artifact_path=receipt.artifact.path,
        )
    except install_marker.InstallMarkerError as exc:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill {provider_name!r} command {command_name!r} build record "
            f"is not valid: {exc}",
        ) from exc


def publish_schema2_local_builds(
    home: Path,
    plans: tuple[build_planner.BuildPlan, ...],
    publications: Mapping[str, build_cache.CachePublication],
    cache_backend: build_cache.BuildCacheBackend,
    home_lock: locking.ManagerHomeLock,
    specs_map: Mapping[str, skillspec.SkillSpec],
    *,
    provider_identities: Mapping[str, build_source.BuildSourceIdentity],
) -> Schema2BuildOutputs:
    """Publish planned local builds to the immutable cache.

    Mirrors the legacy lane's publication exactly: each plan's cache
    winner is inspected or published under the home lock, the HIT
    inspection becomes the marker record, and the activation binds
    the marker to the protected artifact. Runs under the install's
    home lock alongside the atomic publication.
    """

    marker_builds: dict[str, dict[str, install_marker.InstallMarkerBuildV5]] = {}
    activations: dict[str, shims.BuildCommandActivation] = {}
    build_sources: dict[str, build_source.BuildSourceIdentity | None] = dict(
        provider_identities
    )
    for plan in plans:
        try:
            inspection = cache_backend.inspect(
                build_cache.CacheExpectation(input=plan.input)
            )
            if inspection.status is not build_cache.CacheEntryStatus.HIT:
                publication = publications.get(plan.cache_key)
                if publication is None:
                    raise _concurrent_state_change(
                        "cache winner changed before commit: "
                        f"{plan.provider}.{plan.command}"
                    )
                cache_backend.publish(publication, guard=home_lock)
                inspection = cache_backend.inspect(
                    build_cache.CacheExpectation(input=plan.input)
                )
            if inspection.status is not build_cache.CacheEntryStatus.HIT:
                raise _concurrent_state_change(
                    f"local build {plan.provider}.{plan.command} changed "
                    "during publication"
                )
        except build_planner.BuildPlanningError:
            raise
        except SourceError:
            raise
        except (OSError, ValueError) as exc:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {plan.provider!r} command {plan.command!r} cache "
                f"publication failed: {exc}",
            ) from exc
        marker = marker_build_v5_from_inspection(
            plan.provider, plan.command, plan, inspection
        )
        command = specs_map[plan.provider].commands[plan.command]
        try:
            activation = shims.select_build_activation(
                csk_home=home,
                command=command,
                marker_build=marker,
                inspection=inspection,
            )
        except shims.ShimError as exc:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {plan.provider!r} command {plan.command!r} "
                f"activation failed: {exc}",
            ) from exc
        marker_builds.setdefault(plan.provider, {})[plan.command] = marker
        activations[plan.command] = activation
    return Schema2BuildOutputs(
        marker_builds=marker_builds,
        activations=activations,
        build_sources=build_sources,
    )


def publish_schema2_external_builds(
    config: GlobalConfig,
    project_path: Path,
    nodes: list[closure.ClosureNode],
    specs_map: Mapping[str, skillspec.SkillSpec],
    captured: Mapping[str, CapturedMember],
    substitutions: DevManifest,
    operator_search_path: OperatorSearchPath | None,
    stack: ExitStack,
    *,
    home: Path,
    dry_run: bool,
    ssh_credentials: OperatorSSHCredentials | None,
    https_token: OperatorHTTPSToken | None,
    interactive: bool,
) -> tuple[Schema2BuildOutputs, list[str]]:
    """Run external builds through the existing repository pipeline.

    Each member package binds its pipeline request, which selects
    receipt-3 lineage and the receipt-3 cache namespace; results
    become marker-v5 records and validated activations. Manager
    section 11 governs throughout: acquisition admits the effective
    state (committed HEAD under a substitution, never dirty bytes),
    and local acquisition never snapshots the external repository.
    """

    # Imported lazily for the installer cycle documented above.
    from ..installer import InstallError, _publish_external_builds

    packages = {
        node.name: captured[node.name].package
        for node in nodes
        if _external_build_command_names(specs_map[node.name])
    }
    if not packages:
        return empty_build_outputs(), []
    if operator_search_path is None:
        raise SourceError(
            CODE_MEMBER_INVALID,
            "external builds need the captured operator search path",
        )
    marker_roots = (project_path / ".agents" / "skills",)
    try:
        published, messages = _publish_external_builds(
            config,
            project_root=project_path,
            nodes=nodes,
            substitutions=substitutions,
            operator_search_path=operator_search_path,
            stack=stack,
            dry_run=dry_run,
            marker_roots=marker_roots,
            ssh_credentials=ssh_credentials,
            https_token=https_token,
            interactive=interactive,
            packages=packages,
        )
    except build_planner.BuildPlanningError:
        raise
    except InstallError as exc:
        raise SourceError(CODE_MEMBER_INVALID, str(exc)) from exc
    marker_builds: dict[str, dict[str, install_marker.InstallMarkerBuildV5]] = {}
    activations: dict[str, shims.BuildCommandActivation] = {}
    for provider_name, commands in published.items():
        for command_name, build in commands.items():
            marker = build.marker
            receipt_bytes = build.receipt_bytes
            artifact_path = build.artifact_path
            if (
                not isinstance(marker, install_marker.InstallMarkerBuildV5)
                or receipt_bytes is None
                or artifact_path is None
            ):
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill {provider_name!r} command {command_name!r} "
                    "external result is not a receipt-3 publication",
                )
            command = specs_map[provider_name].commands[command_name]
            try:
                activation = shims.select_external_build_activation_v3(
                    csk_home=home,
                    command=command,
                    marker_build=marker,
                    receipt_bytes=receipt_bytes,
                    artifact_path=artifact_path,
                    expected_package=captured[provider_name].package,
                )
            except shims.ShimError as exc:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill {provider_name!r} command {command_name!r} "
                    f"activation failed: {exc}",
                ) from exc
            marker_builds.setdefault(provider_name, {})[command_name] = marker
            activations[command_name] = activation
    return (
        Schema2BuildOutputs(
            marker_builds=marker_builds,
            activations=activations,
            build_sources={},
        ),
        messages,
    )


def merge_build_outputs(
    *bundles: Schema2BuildOutputs,
) -> Schema2BuildOutputs:
    """Merge local and external build bundles into one install bundle."""

    marker_builds: dict[str, dict[str, install_marker.InstallMarkerBuildV5]] = {}
    activations: dict[str, shims.BuildCommandActivation] = {}
    build_sources: dict[str, build_source.BuildSourceIdentity | None] = {}
    for bundle in bundles:
        for provider_name, commands in bundle.marker_builds.items():
            marker_builds.setdefault(provider_name, {}).update(commands)
        activations.update(bundle.activations)
        build_sources.update(bundle.build_sources)
    return Schema2BuildOutputs(
        marker_builds=marker_builds,
        activations=activations,
        build_sources=build_sources,
    )


def reconstruct_schema2_local_builds(
    *,
    home: Path,
    project_path: Path,
    name: str,
    spec: skillspec.SkillSpec,
    frozen_dir: Path,
    package: PackageIdentity,
    source_root: Path | None,
    config: GlobalConfig | None,
) -> Schema2BuildOutputs:
    """Reconstruct one member's local build bundle read-only.

    Re-planning runs the audit hook over the stored reports, the
    toolchain probe and the cache reads, but never compiles,
    records or publishes: a cache miss means the install evidence
    is gone and the member cannot be current. The marker is never
    an input here; the plan comparison downstream decides.
    """

    if not _local_build_command_names(spec):
        return empty_build_outputs()
    if config is None:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill {name!r} build evidence cannot be verified without "
            "the machine policy",
        )
    from ..builds import toolchain as build_toolchain

    policy = source_audit.policy_from_config(
        config, script_policy=skillspec.SCRIPT_WORKER_V1_POLICY
    )
    hook = partial(
        source_audit.source_audit_plan_hook, csk_home=home, policy=policy
    )
    with ExitStack() as stack:
        providers = local_build_providers(
            (
                FrozenBuildMember(
                    name=name, spec=spec, frozen_dir=frozen_dir,
                    package=package,
                ),
            ),
            stack=stack,
        )
        cache_backend = build_cache.cache_for_manager_home(home)
        plans = plan_schema2_local_builds(
            providers,
            home=home,
            operator_search_path=build_toolchain.capture_operator_search_path(),
            forbidden_roots=(
                (project_path, config.skills_root, source_root)
                if source_root is not None
                else (project_path, config.skills_root)
            ),
            cache_backend=cache_backend,
            occupied={
                command: name for command in active_script_commands(spec)
            },
            audit=hook,
            read_only=True,
        )
        identities = {
            provider.name: provider.snapshot.identity for provider in providers
        }
    marker_builds: dict[str, install_marker.InstallMarkerBuildV5] = {}
    activations: dict[str, shims.BuildCommandActivation] = {}
    for plan in plans:
        try:
            inspection = cache_backend.inspect(
                build_cache.CacheExpectation(input=plan.input)
            )
        except (OSError, ValueError) as exc:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {plan.command!r} build evidence "
                f"cannot be read: {exc}",
            ) from exc
        if inspection.status is not build_cache.CacheEntryStatus.HIT:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {plan.command!r} build evidence "
                "is missing from the cache; run csk install to repair",
            )
        marker = marker_build_v5_from_inspection(
            name, plan.command, plan, inspection
        )
        command = spec.commands[plan.command]
        try:
            activation = shims.select_build_activation(
                csk_home=home,
                command=command,
                marker_build=marker,
                inspection=inspection,
            )
        except shims.ShimError as exc:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {plan.command!r} build evidence "
                f"does not validate: {exc}",
            ) from exc
        marker_builds[plan.command] = marker
        activations[plan.command] = activation
    return Schema2BuildOutputs(
        marker_builds={name: marker_builds} if marker_builds else {},
        activations=activations,
        build_sources={name: identities[name]} if marker_builds else {},
    )


def reconstruct_schema2_external_builds(
    *,
    home: Path,
    name: str,
    spec: skillspec.SkillSpec,
    package: PackageIdentity,
    live_marker_path: Path,
) -> Schema2BuildOutputs:
    """Reconstruct one member's external build bundle read-only.

    The live marker supplies cache-key locators only; every expected
    record derives from the verified protected-store receipt bytes
    and the member spec, and the plan comparison downstream decides
    whether the marker matches. The recorded marker record is also
    compared field by field against the receipt-3 evidence and the
    protected artifact bytes, so a record the evidence no longer
    supports refuses here instead of comparing current. No network,
    no acquisition, no toolchain probe.
    """

    external = _external_build_command_names(spec)
    if not external:
        return empty_build_outputs()
    try:
        raw = live_marker_path.read_bytes()
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill {name!r} install marker cannot be read: {exc}",
        ) from exc
    try:
        marker = install_marker.read_install_marker(raw)
    except install_marker.InstallMarkerError as exc:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill {name!r} install marker is not usable: {exc}",
        ) from exc
    if not isinstance(marker, install_marker.InstallMarkerV5):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill {name!r} install marker is not a schema-2 marker",
        )
    store = build_repository_pipeline.DiskProtectedStore(home / "external-builds")
    marker_builds: dict[str, install_marker.InstallMarkerBuildV5] = {}
    activations: dict[str, shims.BuildCommandActivation] = {}
    for command_name in external:
        recorded = marker.builds.get(command_name)
        if recorded is None:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {command_name!r} build evidence "
                "is missing from the marker; run csk install to repair",
            )
        try:
            hit = store.inspect_artifact_v3(recorded.cache_key)
        except build_repository_pipeline.ExternalBuildError as exc:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {command_name!r} build evidence "
                f"is not usable: {exc}",
            ) from exc
        if hit is None:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {command_name!r} build evidence "
                "is missing from the protected store; run csk install "
                "to repair",
            )
        command = spec.commands[command_name]
        try:
            receipt = build_metadata.read_receipt_v3(hit.receipt)
        except build_metadata.BuildMetadataError as exc:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {command_name!r} build evidence "
                f"receipt is not usable: {exc}",
            ) from exc
        expected = _expected_external_v5(
            name, command_name, command, spec, package, receipt, hit.receipt
        )
        artifact_relative = expected.artifact_path
        artifact_path = (
            home
            / "external-builds"
            / "artifacts-v3"
            / expected.cache_key.removeprefix("sha256:")
            / build_metadata.derived_cache_artifact_name(artifact_relative)
        )
        try:
            artifact_bytes = artifact_path.read_bytes()
        except FileNotFoundError as exc:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {command_name!r} build evidence "
                "is missing from the protected store; run csk install "
                "to repair",
            ) from exc
        except _FS_ERRORS as exc:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {command_name!r} build evidence "
                f"artifact cannot be read: {exc}",
            ) from exc
        try:
            differing = build_currentness.compare_external_build_evidence(
                recorded,
                receipt.input,
                marker_package=package,
                receipt_bytes=hit.receipt,
                artifact_bytes=artifact_bytes,
            )
        except build_currentness.BuildCurrentnessError as exc:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {command_name!r} build evidence "
                f"does not match the installed record: {exc}",
            ) from exc
        if differing:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {command_name!r} installed build "
                "record differs from receipt evidence: "
                + ", ".join(differing)
                + "; run csk install to repair",
            )
        try:
            activation = shims.select_external_build_activation_v3(
                csk_home=home,
                command=command,
                marker_build=expected,
                receipt_bytes=hit.receipt,
                artifact_path=artifact_path,
                expected_package=package,
            )
        except shims.ShimError as exc:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Skill {name!r} command {command_name!r} build evidence "
                f"does not validate: {exc}",
            ) from exc
        marker_builds[command_name] = expected
        activations[command_name] = activation
    return Schema2BuildOutputs(
        marker_builds={name: marker_builds},
        activations=activations,
        build_sources={},
    )


def _expected_external_v5(
    member_name: str,
    command_name: str,
    command: skillspec.CommandSpec,
    spec: skillspec.SkillSpec,
    package: PackageIdentity,
    receipt: build_metadata.BuildReceiptV3,
    receipt_bytes: bytes,
) -> install_marker.InstallMarkerBuildV5:
    """Derive one expected external record from verified receipt bytes.

    The receipt was validated self-consistent by the protected store
    read; every field here is either recomputed from those bytes or
    cross-checked against the member spec and package. The live
    marker contributes nothing but the locator key its caller used.
    """

    def _refuse(reason: str) -> install_marker.InstallMarkerBuildV5:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Skill {member_name!r} command {command_name!r} build evidence "
            f"{reason}",
        )

    if (
        build_metadata.canonical_receipt_v3_bytes(receipt) != receipt_bytes
        or build_metadata.source_aware_cache_key(receipt.input)
        != receipt.cache_key
    ):
        return _refuse("receipt does not authenticate its cache key")
    if receipt.input.package != package:
        return _refuse("receipt package differs from the installing member")
    build = receipt.input.build
    if not isinstance(build, build_metadata.GoRepositoryBuildInput):
        return _refuse("receipt input is not an external build input")
    if build.command != command_name or build.driver != command.driver:
        return _refuse("receipt identity differs from the member command")
    repository_name = build.source.repository
    repository = spec.build_repositories.get(repository_name)
    if repository is None or command.repository != repository_name:
        return _refuse("receipt repository differs from the member command")
    declared = build.source.declared
    if (
        declared.identity.value != repository.identity
        or declared.transport != repository.transport
        or declared.locked_commit.object_format
        != repository.locked_commit.object_format
        or declared.locked_commit.hex != repository.locked_commit.hex
        or declared.tag != repository.tag
    ):
        return _refuse("receipt declared source differs from the member")
    if build.source.descriptor.target != command.target:
        return _refuse("receipt descriptor target differs from the member")
    effective = build.source.effective
    if effective.build_source.algorithm != "curator-build-source-v1":
        return _refuse("receipt build source algorithm differs from the manager")
    artifact = receipt.artifact
    try:
        expected_relative = build_metadata.derived_artifact_path(
            command_name, goos=build.target.goos
        )
    except shims.ShimError as exc:
        return _refuse(f"receipt target is not usable: {exc}")
    if artifact.path != expected_relative:
        return _refuse("receipt artifact is not manager-derived")
    substitution = None
    if effective.substitution is not None:
        raw_substitution = effective.substitution
        ref = None
        if raw_substitution.ref is not None:
            ref = install_marker.MarkerRepositoryRef(
                raw_substitution.ref.kind, raw_substitution.ref.value
            )
        substitution = install_marker.MarkerRepositorySubstitution(
            type=raw_substitution.type, ref=ref
        )
    try:
        return install_marker.InstallMarkerBuildV5(
            driver=GO_REPOSITORY_V1_DRIVER,
            receipt_schema_version=3,
            execution_policy=build_metadata.PORTABLE_EXECUTION_POLICY,
            cache_key=receipt.cache_key,
            receipt_sha256="sha256:"
            + hashlib.sha256(receipt_bytes).hexdigest(),
            artifact_sha256=artifact.sha256,
            artifact_path=artifact.path,
            repository=repository_name,
            declared_identity=install_marker.MarkerRepositoryIdentity(
                "network-git", declared.identity.value
            ),
            declared_locked_commit=install_marker.MarkerRepositoryCommit(
                declared.locked_commit.object_format,
                declared.locked_commit.hex,
            ),
            declared_tag=declared.tag,
            effective_identity=install_marker.MarkerRepositoryIdentity(
                effective.identity.kind, effective.identity.value
            ),
            object_format=effective.object_format,
            commit=effective.commit,
            substituted=effective.substituted,
            substitution=substitution,
            build_source=build_source.BuildSourceIdentity(
                "curator-build-source-v1",
                effective.build_source.content_sha256,
            ),
            descriptor_target=build.source.descriptor.target,
        )
    except install_marker.InstallMarkerError as exc:
        return _refuse(f"record is not valid: {exc}")


def _binding_move_messages(
    *,
    home: Path,
    slug: str,
    alias: str,
    members: tuple[ResolvedMember, ...],
    source_roots: dict[str, Path],
    git_aliases: frozenset[str] = frozenset(),
) -> list[str]:
    """Report source relocations on refresh; bindings are diagnostic only."""

    messages: list[str] = []
    for source_alias in sorted(
        {
            member.from_alias
            for member in members
            if member.from_alias is not None and member.from_alias not in git_aliases
        }
    ):
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
    if isinstance(marker.package, LocalSnapshot):
        return marker.package.snapshot
    if isinstance(marker.package, NetworkGit):
        return marker.package.commit.hex
    return None


def _evaluate_locked_member(
    staging_root: Path,
    *,
    home: Path,
    project_path: Path,
    lock_member: LockMember,
    lock_sha256: str,
    source_root: Path | None,
    agents: tuple[str, ...],
    locale_value: str | None,
    config: GlobalConfig | None,
) -> MemberVerdict:
    """Evaluate one lock member against live source and installed marker.

    The live source is re-captured at the locked directory (frozen, no
    collection expansion) and must digest-match the lock; the plan is
    then derived from those verified bytes and the installed marker is
    compared against it. Git members serve from the store alone and
    skip live re-capture. Marker summaries never authorize currency:
    the plan comes from the lock and the verified bytes alone.
    Build records reconstruct read-only from re-planning and
    protected store reads. Read-only: staging stays in the system
    temporary directory.
    """

    name = lock_member.name
    if isinstance(lock_member.package, NetworkGit):
        return _evaluate_locked_git_member(
            staging_root,
            home=home,
            project_path=project_path,
            lock_member=lock_member,
            lock_sha256=lock_sha256,
            agents=agents,
            locale_value=locale_value,
            config=config,
        )
    if not isinstance(lock_member.package, LocalSnapshot):
        return MemberVerdict(
            name=name,
            current=False,
            label="error",
            detail=f"Skill {name!r} lock member carries an unsupported package identity",
            locked_snapshot=None,
            current_snapshot=None,
            marker_snapshot=_marker_snapshot(
                project_path / ".agents" / "skills" / name / ".csk-install.json"
            ),
        )
    if source_root is None:
        return MemberVerdict(
            name=name,
            current=False,
            label="error",
            detail=f"Skill {name!r} lock member has no source root to revalidate",
            locked_snapshot=lock_member.package.snapshot,
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
    return _evaluate_member_plan(
        staging_root,
        home=home,
        project_path=project_path,
        name=name,
        package=lock_member.package,
        lock_sha256=lock_sha256,
        frozen_files=captured.frozen_files(),
        source_root=source_root,
        agents=agents,
        locale_value=locale_value,
        config=config,
        locked_snapshot=locked_snapshot,
        current_snapshot=current_snapshot,
    )


def _evaluate_member_plan(
    staging_root: Path,
    *,
    home: Path,
    project_path: Path,
    name: str,
    package: PackageIdentity,
    lock_sha256: str,
    frozen_files: Mapping[str, snapshot.FrozenFile],
    source_root: Path | None,
    agents: tuple[str, ...],
    locale_value: str | None,
    config: GlobalConfig | None,
    locked_snapshot: str | None,
    current_snapshot: str | None,
) -> MemberVerdict:
    """Derive the install plan from verified bytes and compare the marker.

    Shared by the local and Git status paths: the plan comes from the
    verified bytes alone, build records reconstruct read-only, and the
    installed marker is compared against the plan. Read-only.
    """

    marker_path = project_path / ".agents" / "skills" / name / ".csk-install.json"
    claimed = _marker_snapshot(marker_path)
    try:
        frozen_dir = staging_root / "status" / name
        materialize_frozen(
            frozen_files, frozen_dir, subject=f"Skill {name!r}"
        )
        spec = load_member_spec(frozen_dir, name=name)
        context_dir = staging_root / "status-context" / name
        files, content = stage_member_context(
            frozen_dir, context_dir, spec, name=name, locale_value=locale_value
        )
        bundle = merge_build_outputs(
            reconstruct_schema2_local_builds(
                home=home,
                project_path=project_path,
                name=name,
                spec=spec,
                frozen_dir=frozen_dir,
                package=package,
                source_root=source_root,
                config=config,
            ),
            reconstruct_schema2_external_builds(
                home=home,
                name=name,
                spec=spec,
                package=package,
                live_marker_path=marker_path,
            ),
        )
        plan = build_marker_plan(
            name=name,
            package=package,
            lock_sha256=lock_sha256,
            content_sha256=content,
            spec=spec,
            files=tuple(files),
            agents=agents,
            locale_value=locale_value,
            builds=bundle.marker_builds.get(name),
            build_source=bundle.build_sources.get(name),
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


def _evaluate_locked_git_member(
    staging_root: Path,
    *,
    home: Path,
    project_path: Path,
    lock_member: LockMember,
    lock_sha256: str,
    agents: tuple[str, ...],
    locale_value: str | None,
    config: GlobalConfig | None,
) -> MemberVerdict:
    """Evaluate one locked Git member from the store alone.

    Git members have no live bytes to re-capture: the store entry
    addressed by the package key is the verified frozen copy, and a
    missing entry is unavailable. The plan then derives from those
    bytes exactly like the local path.
    """

    name = lock_member.name
    package = lock_member.package
    assert isinstance(package, NetworkGit)
    commit = package.commit.hex
    marker_path = project_path / ".agents" / "skills" / name / ".csk-install.json"
    try:
        served = consumers.open_for_install(
            home, name, package_identity_sha256(package)
        )
    except SourceError as exc:
        return MemberVerdict(
            name=name,
            current=False,
            label="snapshot-unavailable",
            detail=f"Skill {name!r} locked snapshot is unavailable: {exc}",
            locked_snapshot=commit,
            current_snapshot=None,
            marker_snapshot=_marker_snapshot(marker_path),
        )
    if not marker_path.exists():
        return MemberVerdict(
            name=name,
            current=False,
            label="missing-marker",
            detail=f"Skill {name!r} install marker is missing",
            locked_snapshot=commit,
            current_snapshot=commit,
            marker_snapshot=None,
        )
    return _evaluate_member_plan(
        staging_root,
        home=home,
        project_path=project_path,
        name=name,
        package=package,
        lock_sha256=lock_sha256,
        frozen_files=served.files,
        source_root=None,
        agents=agents,
        locale_value=locale_value,
        config=config,
        locked_snapshot=commit,
        current_snapshot=commit,
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
    config: GlobalConfig | None = None,
) -> Schema2Status:
    """Evaluate one schema-2 installation without writing anything.

    Members enumerate from the lock (frozen membership); collections
    are never expanded, since status never rescans. A stale lock marks
    every member; otherwise each member revalidates its locked source
    and marker, and then every live publication target is compared
    against the locked desired state through the install's own
    staging, so marker fields alone never attest currency. All staging
    stays in the system temporary directory, so project and home trees
    are byte-identical afterwards. Build evidence reconstructs
    read-only; without the machine policy it cannot be verified.
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
            if lock_member.selection is None and isinstance(
                lock_member.package, NetworkGit
            ):
                # Transitive closure members serve from the store alone.
                verdicts.append(
                    _evaluate_locked_member(
                        staging_root,
                        home=home,
                        project_path=project_path,
                        lock_member=lock_member,
                        lock_sha256=lock_digest,
                        source_root=None,
                        agents=agents,
                        locale_value=locale_value,
                        config=config,
                    )
                )
                continue
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
                    source_root=source_roots.get(member_alias),
                    agents=agents,
                    locale_value=locale_value,
                    config=config,
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
                config=config,
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
    config: GlobalConfig | None,
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
        frozen_mode = modes.FrozenSources(home=home, lock=lock)
        members = locked_schema2_members(
            frozen_mode,
            list(manifest_value.selectors),
            source_roots,
        )
        packages = {lock_member.name: lock_member.package for lock_member in lock.members}
        captured_members: dict[str, CapturedMember] = {}
        pred_files: dict[str, Mapping[str, snapshot.FrozenFile]] = {}
        for member in members:
            package = packages[member.name]
            raw, served = _capture_locked_member(member, package, home=home)
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
                    f"Skill {member.name!r} has no served locked snapshot",
                )
        specs_map: dict[str, skillspec.SkillSpec] = {}
        for name in sorted(pred_files):
            frozen_dir = staging_root / "status-spec" / name
            materialize_frozen(
                pred_files[name], frozen_dir, subject=f"Skill {name!r}"
            )
            specs_map[name] = load_member_spec(frozen_dir, name=name)
        bundles = []
        for member in members:
            member_spec = specs_map[member.name]
            member_package = packages[member.name]
            bundles.append(
                reconstruct_schema2_local_builds(
                    home=home,
                    project_path=project_path,
                    name=member.name,
                    spec=member_spec,
                    frozen_dir=staging_root / "status-spec" / member.name,
                    package=member_package,
                    source_root=member.source_root,
                    config=config,
                )
            )
            bundles.append(
                reconstruct_schema2_external_builds(
                    home=home,
                    name=member.name,
                    spec=member_spec,
                    package=member_package,
                    live_marker_path=project_path
                    / ".agents"
                    / "skills"
                    / member.name
                    / ".csk-install.json",
                )
            )
        build_outputs = merge_build_outputs(*bundles)
        package_keys = {
            name: item.package_key for name, item in captured_members.items()
        }
        lock_runtime_refs = _referenced_runtime_keys(lock)
        git_aliases = frozenset(
            source_alias
            for source_alias, acquisition in manifest_value.sources.items()
            if not isinstance(acquisition, skillfile_v2.PathSource)
        )
        planned, adapter_targets, _plan_messages = plan_schema2_targets(
            mode=frozen_mode,
            home=home,
            project_path=project_path,
            slug=project_slug(locking.canonical_project_identity(project_path)),
            alias=alias,
            agents=list(agents),
            members=tuple(members),
            package_keys=package_keys,
            specs_map=specs_map,
            new_runtime_refs=lock_runtime_refs,
            old_runtime_refs=lock_runtime_refs,
            git_aliases=git_aliases,
        )
        manifest_data, _reread = _read_fresh_manifest(project_path)
        staged = stage_schema2_desired(
            staging_root / "status-desired",
            mode=frozen_mode,
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
            build_outputs=build_outputs,
            git_aliases=git_aliases,
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
        touched = _touched_members(
            (key,), schema2_command_owners(tuple(members), specs_map)
        )
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
        # Git roots have no local source root; they attribute by
        # package kind and serve from the store alone.
        if isinstance(lock_member.package, NetworkGit):
            return selector.from_alias
        return None
    return selector.from_alias
