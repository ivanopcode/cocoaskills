"""Resolving and frozen source modes as capability types (draft, opt-in).

There are exactly two modes, and the code must not be able to confuse
them. Resolving mode (``csk install`` with no lock, ``csk upgrade``) may
enumerate members, resolve refs, capture snapshots, write the source-v1
store and write a lock. Frozen mode (``csk install`` with a lock, launch,
status) reads the lock and the store, and is forbidden four operations:
enumerate a collection, advance a ref, write the store, write a lock.

Frozen mode does capture, but only on one source arm, and it never
stages. :func:`csk.sources.publish._capture_locked_member` dispatches on
the locked package identity, and the two arms differ:

* ``LocalSnapshot``: the store is read first, and then the live bytes
  are re-captured exactly once for that member. That capture is
  load-bearing rather than an exception to the rule: re-reading the live
  bytes is how ``source_snapshot_changed`` is produced at all, so a
  local lane unable to capture would be a lane unable to detect drift.
  The capture is for comparison only and is never staged.
* ``NetworkGit``: served from the source-v1 store alone, through
  :func:`csk.sources.publish._serve_locked_git_member`. A Git member has
  no live bytes to compare against — the store entry addressed by
  ``(skill name, SHA-256(CCJ-1(package)))`` *is* the locked snapshot, and
  a missing or mismatching entry is unavailable — so nothing is captured
  and the network is not touched. Drift detection is not weaker here; it
  is answered by the identity instead of by a re-read.

Both arms are measured, not assumed. Counting
:func:`csk.sources.snapshot.capture_package_snapshot` and
:func:`csk.sources.store.stage_snapshot` across one locked install per
arm gives frozen ``capture=1 stage=0`` local and ``capture=0 stage=0``
Git, each against resolving ``1/1``.

The four forbidden operations are not refused by the same mechanism, and
the difference is what a new call site has to know:

* Advance a ref: refused by type. :func:`resolve_git_alias`,
  :func:`acquire_git_alias` and :func:`acquire_git_commit` take a
  :class:`ResolvingSources`, the only carrier of the transport grant.
* Enumerate a collection: refused by type at the entry point. Selector
  expansion is reachable only through
  :func:`csk.sources.publish.resolve_schema2_source_roots` and
  :func:`csk.sources.publish.resolve_schema2_members`, both typed
  :class:`ResolvingSources`. The re-enumeration guard
  :func:`csk.sources.publish.collection_membership` takes no mode and is
  confined instead: only the resolving lane records a membership to
  compare against, and the frozen lane passes ``None``.
* Write the store: refused by call-graph confinement, not by type.
  :func:`csk.sources.snapshot.capture_package_snapshot` and
  :func:`csk.sources.store.stage_snapshot` take a plain
  :class:`~pathlib.Path` and would run under either mode.
  :func:`csk.sources.publish.stage_schema2_store` is unreachable because
  its only caller is
  :func:`csk.sources.publish._install_schema2_resolving`, and the healing
  write sits in
  :func:`csk.sources.publish._serve_resolving_publication_files`, which
  the shared pipeline reaches only from the ``else`` branch of its mode
  check.
* Write a lock: refused by an explicit runtime mode branch. Both
  :func:`csk.sources.publish.plan_schema2_targets` and
  :func:`csk.sources.publish.stage_schema2_desired` take the union type
  and gate the lock target and the lock bytes on
  ``isinstance(mode, ResolvingSources)``. Frozen mode still rebuilds the
  lock in memory for the determinism comparison; it never stages it.

The modes are two disjoint types, not a boolean a later call site can
forget. :class:`ResolvingSources` carries the transport grant (the Git
tool factory, the policy path, the acquisition workspace and the
operation-scoped resolution memo); :class:`FrozenSources` carries only
the home directory and the validated lock. :func:`select` is the single
mode-decision point; everything downstream dispatches on the type. Where
an operation is refused by type, the capability is simply not in scope
and a frozen call site cannot spell it. Where it is refused by
confinement or by a mode branch, the guarantee lives in the call graph
above it, and a new call site can break it without a type error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import git_admission
from ..build_repository import LockedCommit
from . import repository_policy
from . import skillfile_v2
from . import transport as source_transport
from .errors import (
    CODE_MEMBER_INVALID,
    CODE_MEMBER_MISSING,
    CODE_SELECTION_INVALID,
    SourceError,
)
from .lock import SkillfileLock

#: Memo key for one alias resolution: the declared source spelling plus the
#: declared ref. Two aliases sharing one declaration resolve once; two
#: spellings of one repository resolve separately because each resolves
#: through its own declared endpoint.
MemoKey = tuple[str, str, str]


@dataclass(frozen=True)
class AliasResolution:
    """One Git alias resolved to an immutable commit for this operation."""

    identity: str
    commit: str
    object_format: str

    def __post_init__(self) -> None:
        expected = 40 if self.object_format == "sha1" else 64 if self.object_format == "sha256" else 0
        if not expected or len(self.commit) != expected or any(
            character not in "0123456789abcdef" for character in self.commit
        ):
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Git alias resolves to an invalid {self.object_format} commit",
            )


@dataclass(frozen=True)
class ResolvingSources:
    """The resolving-mode capability: enumerate, resolve, capture, lock.

    ``tool_for_endpoint`` provisions the operator Git tool per resolved
    endpoint; it runs only when a Git alias actually needs the network,
    so local-only installs never probe for Git. ``None`` means no tool
    is available and every Git alias refuses. The memos are
    operation-scoped: one alias resolves once per operation, and one
    ``(identity, commit)`` pair is acquired once.
    """

    home: Path
    workspace: Path
    tool_for_endpoint: source_transport.ToolProvider | None
    policy_path: Path | None
    alias_commits: dict[MemoKey, AliasResolution] = field(default_factory=dict)
    acquisitions: dict[tuple[str, str], git_admission.Snapshot] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class FrozenSources:
    """The frozen-mode capability: the home directory and the lock.

    Deliberately disconnected from everything resolving mode holds: no
    tool provider, no policy path, no workspace, no memo. A frozen call
    site therefore cannot advance a ref or enumerate a collection: those
    operations are typed against :class:`ResolvingSources` and this type
    offers them nothing. It does capture on one arm — once per locked
    ``LocalSnapshot`` member, for drift detection — while locked
    ``NetworkGit`` members are served from the store and capture
    nothing. The store write and the lock write are held off by
    call-graph confinement and an explicit mode branch rather than by
    this type. See the module docstring for which is which.
    """

    home: Path
    lock: SkillfileLock


def select(
    *,
    home: Path,
    old_lock: SkillfileLock | None,
    fetch: bool,
    workspace: Path,
    tool_for_endpoint: source_transport.ToolProvider | None,
    policy_path: Path | None,
) -> ResolvingSources | FrozenSources:
    """Decide the mode for one install from the lock and the fetch flag.

    This is the single mode-decision point. An install with a lock and
    without fetch runs frozen; initial installs and explicit refreshes
    resolve. Everything downstream dispatches on the returned type.
    """
    if old_lock is not None and not fetch:
        return FrozenSources(home=home, lock=old_lock)
    return ResolvingSources(
        home=home,
        workspace=workspace,
        tool_for_endpoint=tool_for_endpoint,
        policy_path=policy_path,
    )


def _memo_key(acquisition: skillfile_v2.GitSource | skillfile_v2.RepositorySource) -> MemoKey:
    if isinstance(acquisition, skillfile_v2.GitSource):
        return (acquisition.git, acquisition.ref_kind, acquisition.ref_value)
    return (
        f"repository:{acquisition.repository}",
        acquisition.ref_kind,
        acquisition.ref_value,
    )


def _alias_identity(
    alias: str, acquisition: skillfile_v2.GitSource | skillfile_v2.RepositorySource
) -> str:
    if isinstance(acquisition, skillfile_v2.GitSource):
        return acquisition.identity
    return acquisition.repository


def _declared_url(
    acquisition: skillfile_v2.GitSource | skillfile_v2.RepositorySource,
) -> str | None:
    if isinstance(acquisition, skillfile_v2.GitSource):
        return acquisition.git
    return None


def _transport_failure(
    label: str, error: source_transport.TransportError
) -> SourceError:
    code = error.code
    if code == repository_policy.CODE_POLICY_INVALID:
        return SourceError(
            code,
            f"{label} cannot be acquired: the machine source policy "
            f"is invalid: {error}",
        )
    failure_class = getattr(error, "failure_class", None)
    if failure_class == "ref-missing":
        return SourceError(
            CODE_MEMBER_MISSING,
            f"{label} declares a ref the remote does not advertise: {error}",
        )
    return SourceError(
        CODE_MEMBER_INVALID,
        f"{label} cannot be acquired: {error}",
    )


def resolve_git_alias(
    mode: ResolvingSources,
    alias: str,
    acquisition: skillfile_v2.GitSource | skillfile_v2.RepositorySource,
) -> AliasResolution:
    """Resolve one Git alias to its commit for this operation.

    ``revision`` pins resolve without I/O; tags and branches resolve
    through the bounded transport. One declaration resolves once: the
    memo serves every further member from the same alias, so every
    member from one Git alias uses the same resolved commit even when
    the remote ref moves mid-operation.
    """
    if not isinstance(acquisition, (skillfile_v2.GitSource, skillfile_v2.RepositorySource)):
        raise TypeError(f"Source {alias!r} is not a Git acquisition")
    key = _memo_key(acquisition)
    cached = mode.alias_commits.get(key)
    if cached is not None:
        return cached
    identity = _alias_identity(alias, acquisition)
    if acquisition.ref_kind == "revision":
        resolved = AliasResolution(
            identity=identity,
            commit=acquisition.ref_value,
            object_format="sha1" if len(acquisition.ref_value) == 40 else "sha256",
        )
        mode.alias_commits[key] = resolved
        return resolved
    try:
        result = source_transport.resolve_ref(
            identity,
            acquisition.ref_kind,
            acquisition.ref_value,
            None,
            declaration=_declared_url(acquisition),
            declared_url=_declared_url(acquisition),
            policy_path=mode.policy_path,
            tool_for_endpoint=mode.tool_for_endpoint,
        )
    except source_transport.TransportError as exc:
        raise _transport_failure(f"Source {alias!r}", exc) from exc
    resolved = AliasResolution(
        identity=identity, commit=result.lock.hex, object_format=result.lock.object_format
    )
    mode.alias_commits[key] = resolved
    return resolved


def acquire_git_commit(
    mode: ResolvingSources,
    *,
    identity: str,
    commit: str,
    object_format: str,
    declared_url: str | None,
    label: str,
) -> git_admission.Snapshot:
    """Acquire one immutable commit through the bounded transport.

    One ``(identity, commit)`` pair is fetched once per operation;
    the returned snapshot is transport-verified and safe to share
    between members, which each materialize their own copy. Used by
    Git aliases and transitive requirements alike.
    """

    expected_length = 40 if object_format == "sha1" else 64 if object_format == "sha256" else 0
    if (
        not expected_length
        or len(commit) != expected_length
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"{label} pins malformed revision {commit!r}",
        )
    cached = mode.acquisitions.get((identity, commit))
    if cached is not None:
        return cached
    lock = LockedCommit(object_format, commit)
    try:
        result = source_transport.acquire_network(
            identity,
            lock,
            None,
            declaration=declared_url,
            declared_url=declared_url,
            policy_path=mode.policy_path,
            tool_for_endpoint=mode.tool_for_endpoint,
        )
    except source_transport.TransportError as exc:
        raise _transport_failure(label, exc) from exc
    if result.snapshot.commit != commit:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"{label} acquisition served {result.snapshot.commit}, "
            f"not the resolved {commit}",
        )
    mode.acquisitions[(identity, commit)] = result.snapshot
    return result.snapshot


def acquire_git_alias(
    mode: ResolvingSources,
    alias: str,
    acquisition: skillfile_v2.GitSource | skillfile_v2.RepositorySource,
    resolution: AliasResolution,
) -> git_admission.Snapshot:
    """Acquire the bytes for one resolved Git alias, memoized per commit.

    Every Git member reaches the network through
    :func:`csk.sources.transport.acquire_network` and nothing else. One
    ``(identity, commit)`` pair is fetched once per operation; the
    returned snapshot is transport-verified and safe to share between
    members, which each materialize their own copy.
    """
    if not isinstance(acquisition, (skillfile_v2.GitSource, skillfile_v2.RepositorySource)):
        raise TypeError(f"Source {alias!r} is not a Git acquisition")
    if resolution.identity != _alias_identity(alias, acquisition):
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Source {alias!r} resolution names {resolution.identity}, "
            "which does not match the declared alias",
        )
    return acquire_git_commit(
        mode,
        identity=resolution.identity,
        commit=resolution.commit,
        object_format=resolution.object_format,
        declared_url=_declared_url(acquisition),
        label=f"Source {alias!r}",
    )
