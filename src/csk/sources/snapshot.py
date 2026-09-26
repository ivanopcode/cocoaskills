"""Local package snapshots: capture admitted bytes, freeze, revalidate.

A local ``path`` always means admitted filesystem bytes — dirty, staged and
untracked — whether or not ``.git`` exists. It never means Git HEAD, and no
Git process is spawned anywhere on this path. Capture reads regular files
into a private staged snapshot through the confined descriptor layer
(:mod:`csk.sources._selection_fs`); admission of each file is decided on its
opened descriptor (``fstat`` the fd), never on the directory entry inspected
earlier. Links, special files, hard links and cross-device entries inside
admitted inputs are structured refusals naming their package-relative path.

The inventory and digest are owned by :mod:`csk.sources.local_snapshot` and
are called here, never reimplemented. This module supplies the two inputs
that seam cannot produce itself: the real executable bit read from each
opened descriptor (any POSIX execute bit) and the real
filesystem-equivalence predicate for the host, probed at capture time from
the capture filesystem itself (see :func:`probe_filesystem_equivalence`).

The three revalidations of protocol skillfile-sources section 3 are kept
distinct. This module owns the first two; the third is named, not claimed:

1. After capture, before the digest is trusted
   (:func:`revalidate_capture`): file identities, contents, executable bits
   AND the complete admitted path set are re-established by a fresh
   descriptor capture. An added or removed admitted file is a mutation even
   when every captured file is untouched. Any mismatch fails
   ``source_snapshot_changed`` and requires a new explicit attempt: no retry
   loop, no silent re-capture inside the same operation.
2. Before publication, over the frozen copy (:func:`verify_frozen_copy`):
   the frozen bytes are rehashed and the recomputed digest is compared with
   the audited digest. Any difference fails ``source_snapshot_changed``.
3. At each publication write, the boundary recheck: owned by
   ``TASK-260916-100uew`` (``csk.sources.boundaries``) and called by
   ``TASK-260916-17x3o1``. Not this module; named here so the table is
   complete.

Storage of the frozen copy and ``source_snapshot_unavailable`` belong to
``TASK-260917-34g2lq``. This module produces the frozen copy
(:class:`FrozenFile`) and hands it off; it writes nothing to the snapshot
store and performs no mutation of any kind — capture is read-only.

This module is deliberately NOT re-exported from ``csk.sources.__init__``:
the selection import-closure pin (``test_closure_matches_reviewed_selection_tree``)
must keep reviewing exactly the selection path, and nothing on that path
imports this module.
"""

from __future__ import annotations

import errno
import hashlib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ._selection_fs import (
    CapturedTree,
    ConflationProbe,
    Directory,
    PreflightPath,
    PreflightRequest,
    SelectionSession,
    _close_quietly,
    _fstat,
    _file_flags,
    _identity_from_stat,
    _open_descriptor,
)
from .errors import (
    CODE_MEMBER_INVALID,
    CODE_SELECTION_INVALID,
    CODE_SNAPSHOT_CHANGED,
    SourceError,
)
from .local_snapshot import Inventory, PathEquivalence, build_inventory
from .selection import PRUNED_CHILD_NAMES
from .skillfile_v2 import is_valid_selector_directory

#: Failures that must never leak as raw Python exceptions from the public
#: snapshot entry points. Mirrors the tuple the descriptor layer catches.
_FS_ERRORS: Final[tuple[type[Exception], ...]] = (
    OSError,
    RuntimeError,
    ValueError,
    UnicodeError,
)

#: Capture-time admission code: a stable link, special file, hard link or
#: cross-device entry inside admitted inputs, mirroring member validation.
CODE_CAPTURE: Final = CODE_MEMBER_INVALID

#: An entry that disappears mid-capture is a concurrent mutation, not proof
#: that the package was always smaller. Absence mid-walk refuses with this.
CODE_CAPTURE_ABSENT: Final = CODE_SNAPSHOT_CHANGED

#: An entry the opened descriptor reports differently from its listing is a
#: concurrent mutation. Listing-to-open skew refuses with this.
CODE_CAPTURE_CHANGED: Final = CODE_SNAPSHOT_CHANGED


@dataclass(frozen=True)
class FilesystemEquivalence:
    """The host filesystem's probed name-conflation relations.

    ``case_conflates`` is true only when a probe proved that two
    case-variant spellings name one file; ``normalization_conflates`` only
    when an NFC/NFD-variant pair proved the same. Each ``*_known`` flag
    records whether its axis was decided at all: an axis with no usable
    probe spelling, or whose every probe failed inconclusively, is unknown
    and its relation is treated as exact. Unknown therefore fails toward
    the sensitive predicate, never toward folding, and the bound stays
    visible on the value instead of dissolving into a bare boolean.
    """

    case_conflates: bool
    normalization_conflates: bool
    case_known: bool
    normalization_known: bool

    def equivalent(self, left: str, right: str) -> bool:
        """Return whether two spellings name one file on the probed host.

        Folding applies only relations the probe proved: NFC normalisation
        when the filesystem conflates normalisation forms, then case folding
        when it conflates case. Identical spellings are never the
        predicate's verdict — the inventory seam reports exact duplicates
        before consulting it — so they return False here.
        """

        if left == right:
            return False
        first, second = left, right
        # Each fold requires its known flag as well as the conflation fact:
        # the probe never reports conflation without knowledge, and the
        # conjunction keeps an off-contract construction exact instead of
        # folding.
        if self.normalization_conflates and self.normalization_known:
            first = unicodedata.normalize("NFC", first)
            second = unicodedata.normalize("NFC", second)
        if self.case_conflates and self.case_known:
            first = first.casefold()
            second = second.casefold()
        return first == second


def _case_variant(name: str) -> str:
    lowered = name.lower()
    if lowered != name:
        return lowered
    return name.upper()


def _normalization_variants(name: str) -> tuple[str, ...]:
    variants: list[str] = []
    for form in ("NFC", "NFD"):
        candidate = unicodedata.normalize(form, name)
        if candidate != name and candidate not in variants:
            variants.append(candidate)
    return tuple(variants)


def _probe_vote(probe: ConflationProbe, variant: str) -> bool | None:
    """Open one variant spelling and vote on conflation.

    True means the variant names the probed file (conflation proved);
    False means the filesystem distinguishes the spellings (the variant is
    absent, names a different file, or names a link — on a conflating
    filesystem the variant would name the same entry, so a link there
    proves distinction); None means the probe was inconclusive and votes
    nothing. Every failure mode that is not positive evidence votes None:
    a failure is never treated as distinction, and only an opened
    descriptor with an equal identity can vote True, so injected faults
    can only push the axis toward unknown, never toward folding.
    """

    try:
        fd = _open_descriptor(
            variant, _file_flags(nofollow=True), dir_fd=probe.parent.fd
        )
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return False
        return None
    except _FS_ERRORS:
        return None
    try:
        try:
            value = _fstat(
                fd,
                code=CODE_SELECTION_INVALID,
                context="Filesystem-equivalence probe",
                path=variant,
            )
        except SourceError:
            return None
        return _identity_from_stat(value) == probe.identity
    finally:
        _close_quietly(fd)


def probe_filesystem_equivalence(
    probes: tuple[ConflationProbe, ...] | list[ConflationProbe],
) -> FilesystemEquivalence:
    """Probe the capture filesystem's real name-conflation relations.

    Each probe opens a variant spelling of a captured entry relative to
    the still-open parent descriptor and compares identities, so the
    answer describes the filesystem the capture read, not the platform
    name and not a temporary directory elsewhere. Probing is read-only.
    Axes stop as soon as conflation is proved; an axis with no deciding
    probe stays unknown and its relation is treated as exact.
    """

    case_conflates = False
    case_decided = False
    normalization_conflates = False
    normalization_decided = False
    for probe in probes:
        if "case" in probe.axes and not case_conflates:
            vote = _probe_vote(probe, _case_variant(probe.name))
            if vote is True:
                case_conflates = True
                case_decided = True
            elif vote is False:
                case_decided = True
        if "normalization" in probe.axes and not normalization_conflates:
            for variant in _normalization_variants(probe.name):
                vote = _probe_vote(probe, variant)
                if vote is True:
                    normalization_conflates = True
                    normalization_decided = True
                    break
                if vote is False:
                    normalization_decided = True
        if case_conflates and normalization_conflates:
            break
    return FilesystemEquivalence(
        case_conflates=case_conflates,
        normalization_conflates=normalization_conflates,
        case_known=case_decided,
        normalization_known=normalization_decided,
    )


@dataclass(frozen=True)
class FrozenFile:
    """One frozen package file: portable path, raw bytes, executable bit."""

    path: str
    data: bytes
    executable: bool


@dataclass(frozen=True)
class CapturedPackage:
    """One captured package: staged tree, inventory and equivalence facts."""

    root: Path
    tree: CapturedTree
    inventory: Inventory
    equivalence: FilesystemEquivalence

    def frozen_files(self) -> dict[str, FrozenFile]:
        """Render the staged bytes as portable-path frozen files."""

        frozen: dict[str, FrozenFile] = {}
        for components, captured in self.tree.files.items():
            path = _portable_path(components)
            frozen[path] = FrozenFile(
                path=path, data=captured.data, executable=captured.executable
            )
        return frozen


def _portable_path(components: tuple[str, ...]) -> str:
    return "/".join(components)


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def captured_to_inventory(
    captured: CapturedTree, *, equivalent: PathEquivalence
) -> Inventory:
    """Build the local-snapshot-v1 inventory for one captured tree.

    Entry digests are SHA-256 over the captured raw bytes — nothing
    normalises before hashing — and the executable bit is the bit read
    from each opened descriptor. Ordering, collision checks and the digest
    are :func:`csk.sources.local_snapshot.build_inventory`'s, called here
    with the host's probed equivalence predicate.
    """

    entries = [
        (
            _portable_path(components),
            _sha256(item.data),
            item.executable,
        )
        for components, item in captured.files.items()
    ]
    return build_inventory(entries, equivalent=equivalent)


def revalidate_capture(
    session: SelectionSession,
    member: Directory,
    captured: CapturedTree,
    *,
    label: str,
) -> None:
    """Revalidate a capture over the complete admitted path set.

    This is revalidation 1 (after capture, before the digest is trusted).
    A fresh descriptor capture is compared with ``captured``: the file
    path set, the directory path set, every file identity, every file's
    bytes and every executable bit. An added or removed admitted file is
    a mutation even when every captured file is untouched. Any mismatch —
    or any anomaly the fresh walk meets, including a newly appeared link
    or special file — fails ``source_snapshot_changed``. There is no
    retry and no re-capture inside this operation; the caller must start
    a new explicit attempt.

    Identity is ``(st_dev, st_ino)``. A filesystem that reuses the inode
    number for a replacement file cannot surface a byte-identical,
    same-mode replacement: with identity, bytes and mode all unchanged
    there is no observable difference to catch. Such a replacement is
    integrity-neutral — the digest still describes the frozen bytes
    exactly — so it is a declared bound, not a missed mutation: nothing
    content-bearing goes unreported on any host.
    """

    context = f"Snapshot revalidation {label}"
    fresh = session.capture_tree(
        member,
        label=label,
        code=CODE_SNAPSHOT_CHANGED,
        missing_code=CODE_SNAPSHOT_CHANGED,
        changed_code=CODE_SNAPSHOT_CHANGED,
    )
    old_files = set(captured.files)
    new_files = set(fresh.files)
    for added in sorted(new_files - old_files):
        raise SourceError(
            CODE_SNAPSHOT_CHANGED,
            f"{context}: admitted file {_portable_path(added)!r} "
            "appeared after capture",
        )
    for removed in sorted(old_files - new_files):
        raise SourceError(
            CODE_SNAPSHOT_CHANGED,
            f"{context}: admitted file {_portable_path(removed)!r} "
            "disappeared after capture",
        )
    if fresh.directories != captured.directories:
        only_new = sorted(set(fresh.directories) - set(captured.directories))
        only_old = sorted(set(captured.directories) - set(fresh.directories))
        if only_new:
            changed_dir = _portable_path(only_new[0]) or "."
            detail = f"admitted directory {changed_dir!r} appeared after capture"
        else:
            changed_dir = _portable_path(only_old[0]) or "."
            detail = f"admitted directory {changed_dir!r} disappeared after capture"
        raise SourceError(CODE_SNAPSHOT_CHANGED, f"{context}: {detail}")
    for relative in sorted(old_files):
        old_item = captured.files[relative]
        new_item = fresh.files[relative]
        shown = _portable_path(relative)
        if new_item.identity != old_item.identity:
            raise SourceError(
                CODE_SNAPSHOT_CHANGED,
                f"{context}: {shown!r} changed after capture",
            )
        if new_item.data != old_item.data:
            raise SourceError(
                CODE_SNAPSHOT_CHANGED,
                f"{context}: {shown!r} changed after capture",
            )
        if new_item.executable != old_item.executable:
            raise SourceError(
                CODE_SNAPSHOT_CHANGED,
                f"{context}: {shown!r} changed after capture",
            )


def verify_frozen_copy(
    frozen: Mapping[str, FrozenFile],
    expected_snapshot: str,
    *,
    equivalent: PathEquivalence,
) -> str:
    """Rehash one frozen copy and compare it with the audited digest.

    This is revalidation 2 (before publication, over the frozen copy).
    Entry digests are recomputed from the frozen bytes and the inventory
    digest is recomputed through the production inventory function; a
    recomputed digest that differs from ``expected_snapshot`` fails
    ``source_snapshot_changed``. The recomputed digest is returned so the
    caller can carry it without re-deriving it. Digest equality is
    predicate-independent for admitted sets: ``equivalent`` only decides
    collisions, which refuse as such.
    """

    entries = [
        (item.path, _sha256(item.data), item.executable)
        for item in frozen.values()
    ]
    inventory = build_inventory(entries, equivalent=equivalent)
    recomputed = inventory["snapshot"]
    if recomputed != expected_snapshot:
        raise SourceError(
            CODE_SNAPSHOT_CHANGED,
            "frozen copy changed before publication: "
            f"audited {expected_snapshot}, rehashed {recomputed}",
        )
    return recomputed


def capture_package(
    session: SelectionSession,
    member: Directory,
    *,
    label: str,
) -> CapturedPackage:
    """Capture, inventory and revalidate one package inside a session.

    The session carries the frozen Phase-A boundary record, so managed
    outputs prune exactly as the boundary leaf decided and nothing here
    re-decides admission. Capture reads working-tree bytes (never Git
    HEAD), the inventory is built through the production inventory
    function with the probed host predicate, and revalidation 1 runs
    before the package is returned.
    """

    context = f"Snapshot capture {label}"
    try:
        captured = session.capture_tree(
            member,
            label=label,
            code=CODE_CAPTURE,
            missing_code=CODE_CAPTURE_ABSENT,
            changed_code=CODE_CAPTURE_CHANGED,
        )
    except SourceError:
        raise
    except _FS_ERRORS as exc:
        raise SourceError(
            CODE_CAPTURE, f"{context} cannot be inspected: {exc}"
        ) from exc
    equivalence = probe_filesystem_equivalence(captured.conflation_probes)
    inventory = captured_to_inventory(captured, equivalent=equivalence.equivalent)
    revalidate_capture(session, member, captured, label=label)
    return CapturedPackage(
        root=captured.root,
        tree=captured,
        inventory=inventory,
        equivalence=equivalence,
    )


def _capture_components(directory: str, *, context: str) -> list[str]:
    if directory == ".":
        return []
    if not is_valid_selector_directory(directory):
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"{context}: {directory!r} must be '.' or a portable contained path",
        )
    return directory.split("/")


def capture_package_snapshot(
    source_root: Path, directory: str, *, home: Path
) -> CapturedPackage:
    """Capture one package directory as an immutable snapshot.

    ``source_root`` is the local source root and ``directory`` is ``"."``
    or a portable contained path within it. ``home`` is the csk home used
    for the managed-output boundary. The session, descent and containment
    checks mirror the selection entry points; the capture itself runs
    through :func:`capture_package`.
    """

    context = f"Snapshot directory {directory!r} cannot be resolved"
    components = tuple(_capture_components(directory, context=context))
    session = SelectionSession.open(
        source_root,
        home,
        managed_names=PRUNED_CHILD_NAMES,
        preflight=PreflightRequest(
            paths=(
                PreflightPath(
                    components,
                    code=CODE_SELECTION_INVALID,
                    context=context,
                ),
            )
        ),
    )
    with session:
        member = session.descend(
            session.root,
            list(components),
            code=CODE_SELECTION_INVALID,
            missing_code=None,
            context=context,
        ).directory
        if not any(
            item.identity == session.root.identity for item in member.ancestry()
        ):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Snapshot directory {directory!r} escapes the source root",
            )
        return capture_package(session, member, label=directory)


__all__ = [
    "CODE_CAPTURE",
    "CODE_CAPTURE_ABSENT",
    "CODE_CAPTURE_CHANGED",
    "CapturedPackage",
    "FilesystemEquivalence",
    "FrozenFile",
    "capture_package",
    "capture_package_snapshot",
    "captured_to_inventory",
    "probe_filesystem_equivalence",
    "revalidate_capture",
    "verify_frozen_copy",
]
