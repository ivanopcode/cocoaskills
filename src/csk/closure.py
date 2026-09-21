from __future__ import annotations

import stat
import tempfile
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from . import git_ops, manifest, skillspec, snapshot
from .config import GlobalConfig
from .dev_substitutions import Substitution
from .source_identity import (
    SourceIdentityError,
    canonical_source_identity,
    is_allowed,
)
from .sources.errors import (
    CODE_MEMBER_INVALID,
    CODE_MEMBER_MISSING,
    CODE_NAME_CONFLICT,
    CODE_SELECTION_INVALID,
    SourceError,
)


# The synthetic consumer name for direct Skillfile.json entries. A direct
# project skill behaves as a full requirement rooted in the project.
PROJECT_EDGE = "<project>"


class ClosureError(Exception):
    pass


@dataclass(frozen=True)
class ActivationEdge:
    consumer: str
    mode: str
    commands: tuple[str, ...] = ()


@dataclass
class ClosureNode:
    name: str
    decl: manifest.SkillDecl
    resolved: git_ops.ResolvedRef
    repo: Path
    snapshot: Path
    spec: skillspec.SkillSpec
    identity: str | None
    chains: list[str] = field(default_factory=list)
    substituted: str | None = None
    edges: list[ActivationEdge] = field(default_factory=list)

    @property
    def context_active(self) -> bool:
        return any(edge.mode in {"full", "context"} for edge in self.edges)

    def active_commands(self) -> set[str]:
        exported = {command.name for command in self.spec.commands.values() if command.type == "script"}
        if any(edge.mode == "full" for edge in self.edges):
            return exported
        active: set[str] = set()
        for edge in self.edges:
            if edge.mode != "runtime":
                continue
            active.update(edge.commands or exported)
        return active

    def consumers(self) -> list[str]:
        seen: list[str] = []
        for edge in self.edges:
            if edge.consumer not in seen:
                seen.append(edge.consumer)
        return seen


@dataclass(frozen=True)
class _Pending:
    name: str
    git: str | None
    ref: manifest.SkillRef
    source: str
    edge: ActivationEdge
    chain: str


def build_closure(
    config: GlobalConfig,
    project_manifest: manifest.ProjectManifest,
    substitutions: dict[str, Substitution],
    *,
    use_cache: bool = True,
    fetch_existing: bool = False,
    fetched_repos: set[Path] | None = None,
    stack: ExitStack | None = None,
    read_only: bool = False,
    node_resolver: Callable[[_Pending], ClosureNode] | None = None,
    error_factory: Callable[[str, str], BaseException] | None = None,
    ref_comparator: Callable[[ClosureNode, _Pending], str] | None = None,
) -> list[ClosureNode]:
    """Expand direct skills and their requirements into an ordered closure.

    Within one closure a skill name resolves to one commit and one canonical
    source; providers precede consumers in the returned order.

    ``node_resolver`` replaces the schema-1 node acquisition for every new
    name; the schema-2 full closure (:func:`build_source_closure`) supplies
    it so transitive requirements resolve through the bounded transport and
    the source-v1 store instead of the legacy cache layout. ``error_factory``
    maps ``(kind, message)`` to the raised error for the ``"unify"``,
    ``"requirement_commands"`` and ``"cycle"`` failures; ``None`` raises
    :class:`ClosureError` exactly as before. ``ref_comparator`` resolves a
    repeated requirement to its commit for the unification comparison; the
    schema-2 closure supplies it because its nodes carry materialized
    snapshot directories rather than git repositories. ``None`` resolves
    through the node's repository exactly as before.
    """
    nodes: dict[str, ClosureNode] = {}
    fetched_repos = fetched_repos if fetched_repos is not None else set()
    pending: list[_Pending] = [
        _Pending(
            name=decl.name,
            git=decl.git,
            ref=decl.ref,
            source=decl.source,
            edge=ActivationEdge(consumer=PROJECT_EDGE, mode="full"),
            chain=f"{PROJECT_EDGE} -> {decl.name}",
        )
        for decl in project_manifest.skills
    ]

    while pending:
        item = pending.pop(0)
        node = nodes.get(item.name)
        if node is None:
            if node_resolver is not None:
                node = node_resolver(item)
            else:
                node = _resolve_node(
                    config,
                    item,
                    substitutions.get(item.name),
                    use_cache=use_cache,
                    fetch_existing=fetch_existing,
                    fetched_repos=fetched_repos,
                    stack=stack,
                    read_only=read_only,
                )
            nodes[item.name] = node
            for requirement in node.spec.requirements.values():
                pending.append(
                    _Pending(
                        name=requirement.name,
                        git=requirement.git,
                        ref=manifest.SkillRef(requirement.ref_kind, requirement.ref_value),
                        source=requirement.name,
                        edge=ActivationEdge(
                            consumer=item.name,
                            mode=requirement.mode,
                            commands=requirement.commands,
                        ),
                        chain=f"{item.chain} -> {requirement.name}",
                    )
                )
        else:
            _unify(node, item, error_factory=error_factory, ref_comparator=ref_comparator)
        node.edges.append(item.edge)
        node.chains.append(item.chain)

    _validate_requirement_commands(nodes, error_factory=error_factory)
    return _topological_order(nodes, error_factory=error_factory)


def _closure_failure(
    error_factory: Callable[[str, str], BaseException] | None,
    kind: str,
    message: str,
) -> BaseException:
    if error_factory is not None:
        return error_factory(kind, message)
    return ClosureError(message)


def detect_active_command_collisions(nodes: list[ClosureNode]) -> None:
    owners: dict[str, str] = {}
    for node in nodes:
        for command in sorted(node.active_commands()):
            previous = owners.get(command)
            if previous:
                raise ClosureError(
                    f"Command collision for {command!r}: exported by {previous} and {node.name}"
                )
            owners[command] = node.name


def _unify(
    node: ClosureNode,
    item: _Pending,
    *,
    error_factory: Callable[[str, str], BaseException] | None = None,
    ref_comparator: Callable[[ClosureNode, _Pending], str] | None = None,
) -> None:
    if node.substituted is not None:
        # A development substitution replaces every requirement of this name.
        return
    if item.git:
        identity = canonical_source_identity(item.git)
        if identity is not None:
            if node.identity is None:
                node.identity = identity
            elif node.identity != identity:
                raise _closure_failure(
                    error_factory,
                    "unify",
                    f"Source conflict for {node.name}: {node.identity} (via {_best_chain(node.chains)}) "
                    f"and {identity} (via {item.chain}) name different repositories",
                )
    if (item.ref.kind, item.ref.value) == (node.resolved.kind, node.resolved.ref):
        return
    if ref_comparator is not None:
        other_commit = ref_comparator(node, item)
    else:
        try:
            other = git_ops.resolve_ref(node.repo, item.ref.kind, item.ref.value)
        except git_ops.GitError as exc:
            raise _closure_failure(
                error_factory,
                "unify",
                f"Cannot resolve {item.ref.kind} {item.ref.value!r} for {node.name} (via {item.chain}): {exc}",
            ) from exc
        other_commit = other.commit
    if other_commit != node.resolved.commit:
        raise _closure_failure(
            error_factory,
            "unify",
            f"Version conflict for {node.name}: {node.resolved.kind} {node.resolved.ref} "
            f"-> {node.resolved.commit[:12]} (via {_best_chain(node.chains)}) and {item.ref.kind} "
            f"{item.ref.value} -> {other_commit[:12]} (via {item.chain}); "
            "align the requirement refs at their declarations",
        )


def _best_chain(chains: list[str]) -> str:
    return min(chains, key=lambda chain: (chain.count(" -> "), chain))


def _resolve_node(
    config: GlobalConfig,
    item: _Pending,
    substitution: Substitution | None,
    *,
    use_cache: bool,
    fetch_existing: bool,
    fetched_repos: set[Path],
    stack: ExitStack | None,
    read_only: bool,
) -> ClosureNode:
    substituted: str | None = None
    if substitution is not None and substitution.path is not None:
        repo = substitution.path
        if not repo.exists() or not (repo / ".git").exists():
            raise ClosureError(
                f"Substitution for {item.name} points to {repo}, which is not a git repository"
            )
        resolved = git_ops.resolve_ref(repo, "revision", "HEAD")
        substituted = substitution.describe()
    elif substitution is not None:
        _gate_source(config, item.name, substitution.git or "", item.chain)
        repo = _ensure_dev_repo(
            config,
            item.name,
            substitution,
            use_cache=use_cache,
            stack=stack,
            read_only=read_only,
        )
        resolved = git_ops.resolve_ref(repo, substitution.ref_kind or "", substitution.ref_value or "")
        substituted = substitution.describe()
    else:
        repo = _ensure_repo(
            config,
            item,
            use_cache=use_cache,
            fetch_existing=fetch_existing,
            fetched_repos=fetched_repos,
            stack=stack,
            read_only=read_only,
        )
        try:
            resolved = git_ops.resolve_ref(repo, item.ref.kind, item.ref.value)
        except git_ops.GitError as exc:
            raise ClosureError(
                f"Cannot resolve {item.ref.kind} {item.ref.value!r} for {item.name} (via {item.chain}): {exc}"
            ) from exc

    snap = _snapshot_for(
        config,
        item.source,
        repo,
        resolved.commit,
        use_cache=use_cache,
        stack=stack,
        read_only=read_only,
    )
    if git_ops.repository_has_submodules(snap):
        raise ClosureError(f"Submodules are unsupported in MVP: {item.source}")
    try:
        spec = skillspec.load_skill_spec(snap)
    except skillspec.SkillSpecError as exc:
        # Name the closure node that carries the broken manifest: without the
        # provenance the error surfaces under the root declaration and reads
        # as if the declaring skill itself were invalid.
        raise ClosureError(
            f"Invalid skill manifest for {item.name} "
            f"{resolved.kind} {resolved.ref} (via {item.chain}): {exc}"
        ) from exc
    identity = canonical_source_identity(item.git) if item.git else None
    decl = manifest.SkillDecl(
        name=item.name,
        source=item.source,
        ref=manifest.SkillRef(resolved.kind, resolved.ref),
        git=item.git,
    )
    return ClosureNode(
        name=item.name,
        decl=decl,
        resolved=resolved,
        repo=repo,
        snapshot=snap,
        spec=spec,
        identity=identity,
        substituted=substituted,
    )


def _ensure_repo(
    config: GlobalConfig,
    item: _Pending,
    *,
    use_cache: bool,
    fetch_existing: bool,
    fetched_repos: set[Path],
    stack: ExitStack | None,
    read_only: bool,
) -> Path:
    repo = config.skills_root / item.source
    if repo.exists():
        if not (repo / ".git").exists():
            raise ClosureError(f"Local skill path exists but is not a git repository: {repo}")
        repo_key = repo.resolve()
        if (
            fetch_existing
            and use_cache
            and not read_only
            and repo_key not in fetched_repos
        ):
            try:
                git_ops.fetch_repo(repo)
            except git_ops.GitError as exc:
                raise ClosureError(f"Failed to fetch {item.name} at {repo} (via {item.chain}): {exc}") from exc
            fetched_repos.add(repo_key)
        return repo
    if not item.git:
        raise ClosureError(f"Skill repository not found for {item.name}: {repo} (via {item.chain})")
    if read_only:
        raise ClosureError(
            f"Skill repository not found for {item.name}: {repo} (read-only status does not clone sources)"
        )
    _gate_source(config, item.name, item.git, item.chain)
    destination = repo if use_cache else _temp_repo_dir(stack, item.source)
    try:
        git_ops.clone_repo(item.git, destination)
    except git_ops.GitError as exc:
        raise ClosureError(f"Failed to clone {item.name} from {item.git}: {exc}") from exc
    if use_cache and fetch_existing:
        fetched_repos.add(destination.resolve())
    return destination


def _ensure_dev_repo(
    config: GlobalConfig,
    name: str,
    substitution: Substitution,
    *,
    use_cache: bool,
    stack: ExitStack | None,
    read_only: bool,
) -> Path:
    git_url = substitution.git or ""
    if not use_cache:
        if read_only:
            raise ClosureError(
                "read-only closure resolution cannot clone a development substitution"
            )
        destination = _temp_repo_dir(stack, name)
        git_ops.clone_repo(git_url, destination)
        return destination
    # Dev clones live outside skills_root so a substitution never shadows the
    # declared source repository.
    repo = config.path.parent / "dev" / name
    if repo.exists() and (repo / ".git").exists():
        if not read_only:
            git_ops.fetch_repo(repo)
        return repo
    if read_only:
        raise ClosureError(
            f"Substitution repository not found for {name}: {repo} "
            "(read-only status does not clone sources)"
        )
    git_ops.clone_repo(git_url, repo)
    return repo


def _snapshot_for(
    config: GlobalConfig,
    source: str,
    repo: Path,
    commit: str,
    *,
    use_cache: bool,
    stack: ExitStack | None,
    read_only: bool,
) -> Path:
    if use_cache:
        if read_only:
            candidate = snapshot.snapshot_dir(
                config.path.parent,
                source,
                commit,
            )
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                if stack is None:
                    raise ClosureError(
                        "read-only closure resolution requires an ExitStack "
                        "when the persistent snapshot is absent"
                    )
                tmp_root = Path(
                    stack.enter_context(
                        tempfile.TemporaryDirectory(
                            prefix="csk-status-snapshot-"
                        )
                    )
                )
                temporary = tmp_root / source
                git_ops.archive(repo, commit, temporary)
                return temporary
            except OSError as exc:
                raise ClosureError(
                    f"Raw snapshot cannot be inspected for {source} at "
                    f"{commit}: {candidate}"
                ) from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ClosureError(
                    f"Raw snapshot is not a real directory for {source} at "
                    f"{commit}: {candidate}"
                )
            return candidate
        return snapshot.get_snapshot(config.path.parent, source, repo, commit)
    if stack is None:
        raise ClosureError("dry-run snapshot planning requires an ExitStack")
    tmp_root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="csk-dry-run-snapshot-")))
    snap = tmp_root / source
    git_ops.archive(repo, commit, snap)
    return snap


def _temp_repo_dir(stack: ExitStack | None, source: str) -> Path:
    if stack is None:
        raise ClosureError("dry-run source cloning requires an ExitStack")
    tmp_root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="csk-dry-run-source-")))
    return tmp_root / source


def _gate_source(config: GlobalConfig, name: str, git_url: str, chain: str) -> None:
    identity = canonical_source_identity(git_url)
    if not config.allowed_sources:
        return
    if is_allowed(identity, config.allowed_sources):
        return
    allowed = ", ".join(config.allowed_sources)
    raise ClosureError(
        f"Source not allowed for {name}: {git_url} (identity {identity or 'unknown'}); "
        f"allowed prefixes: {allowed}; required via {chain}"
    )


def _validate_requirement_commands(
    nodes: dict[str, ClosureNode],
    *,
    error_factory: Callable[[str, str], BaseException] | None = None,
) -> None:
    errors: list[str] = []
    for node in nodes.values():
        for requirement in node.spec.requirements.values():
            provider = nodes.get(requirement.name)
            if provider is None:
                continue
            for command in requirement.commands:
                provided = provider.spec.commands.get(command)
                if provided is None or provided.type != "script":
                    errors.append(
                        f"Requirement {node.name} -> {requirement.name} names command {command!r}, "
                        f"but {requirement.name} does not export a script command named {command!r}"
                    )
    if errors:
        raise _closure_failure(error_factory, "requirement_commands", "; ".join(errors))


def _topological_order(
    nodes: dict[str, ClosureNode],
    *,
    error_factory: Callable[[str, str], BaseException] | None = None,
) -> list[ClosureNode]:
    # Providers install before consumers: an edge provider -> consumer.
    dependents: dict[str, set[str]] = {name: set() for name in nodes}
    indegree: dict[str, int] = {name: 0 for name in nodes}
    for node in nodes.values():
        for edge in node.edges:
            if edge.consumer == PROJECT_EDGE or edge.consumer not in nodes:
                continue
            if edge.consumer not in dependents[node.name]:
                dependents[node.name].add(edge.consumer)
                indegree[edge.consumer] += 1

    ready = sorted(name for name, degree in indegree.items() if degree == 0)
    ordered: list[ClosureNode] = []
    while ready:
        name = ready.pop(0)
        ordered.append(nodes[name])
        for consumer in sorted(dependents[name]):
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                ready.append(consumer)
        ready.sort()
    if len(ordered) != len(nodes):
        remaining = sorted(name for name in nodes if nodes[name] not in ordered)
        raise _closure_failure(
            error_factory,
            "cycle",
            f"Dependency cycle between skills: {', '.join(remaining)}",
        )
    return ordered


#: The pseudo ref kind carried by local (path) members inside the schema-2
#: full closure. Local members never unify with a Git requirement: any
#: repeated requirement disagrees with this kind and fails closed.
SOURCE_LOCAL_REF_KIND = "local"


@dataclass(frozen=True)
class SourceClosureRoot:
    """One pre-resolved schema-2 root member entering the full closure.

    Roots are expanded, acquired and captured by the resolving caller before
    the closure runs; ``materialized`` is the member's frozen bytes directory
    (caller-owned lifetime, alive for the whole closure build). Local roots
    carry ``local=True``, ``identity=None`` and the ``"local"`` ref kind;
    Git roots carry the canonical repository identity and the alias-resolved
    ``(ref_kind, ref_value, commit)`` triple.
    """

    name: str
    from_alias: str
    directory: str
    local: bool
    materialized: Path
    identity: str | None
    ref_kind: str
    ref_value: str
    commit: str


@dataclass(frozen=True)
class SourceGitAcquisition:
    """One transport-acquired Git source for a transitive requirement.

    ``materialized`` is the acquired tree root (caller-owned lifetime); the
    required skill always sits at its root, exactly like a schema-1
    requirement resolves to its repository root.
    """

    commit: str
    object_format: str
    identity: str
    materialized: Path


def _source_closure_error(kind: str, message: str) -> SourceError:
    if kind == "unify":
        # Every _unify failure is a repeated requirement disagreeing with the
        # node one installed name already unified to: a different repository,
        # an unresolvable ref, or a different commit. A network-git package
        # identity includes the commit, so all three are conflicting
        # dependency identities over one installed name.
        return SourceError(CODE_NAME_CONFLICT, message)
    if kind == "requirement_commands":
        return SourceError(CODE_MEMBER_MISSING, message)
    return SourceError(CODE_MEMBER_INVALID, message)


def _refuse_transitive_ref(
    requirement: skillspec.SkillRequirement, *, chain: str
) -> None:
    """Refuse any transitive requirement that does not pin a revision.

    Branches are admitted only in the root project and the extension adds
    no floating transitive refs, so a transitive tag, branch or unknown ref
    kind fails here, before it can enter the pending queue.
    """
    if requirement.ref_kind == "revision":
        return
    if requirement.ref_kind == "branch":
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Requirement {requirement.name} (via {chain}) declares branch "
            f"{requirement.ref_value!r}, but branches are admitted only in "
            "the root project Skillfile",
        )
    raise SourceError(
        CODE_SELECTION_INVALID,
        f"Requirement {requirement.name} (via {chain}) declares "
        f"{requirement.ref_kind} {requirement.ref_value!r}, but transitive "
        "requirements must pin a revision",
    )


def _source_ref_commit(node: ClosureNode, item: _Pending) -> str:
    """Resolve one repeated schema-2 requirement to its commit, without I/O.

    Schema-2 nodes carry materialized snapshot directories, never git
    repositories, so the legacy resolver cannot run against them; and
    every transitive requirement pins a revision, so the pinned value
    already is the commit. Unification therefore compares resolved
    identities (commits), never ref spellings: a root declared
    ``tag: v1`` and a requirement pinning the same commit unify, while
    two different commits conflict however they are spelled. A
    non-revision requirement fails closed here, with the same code the
    outgoing transitive gate uses.
    """

    if item.ref.kind != "revision":
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Requirement {item.name} (via {item.chain}) declares "
            f"{item.ref.kind} {item.ref.value!r}, but transitive "
            "requirements must pin a revision",
        )
    return item.ref.value


def _gate_source_requirements(
    spec: skillspec.SkillSpec, *, chain: str
) -> None:
    """Refuse floating transitive refs in one newly loaded member spec.

    Every requirement enters the traversal from exactly one spec load, so
    gating each load gates the whole traversal, including requirements that
    unify with an already-resolved node without reaching the resolver.
    """
    for requirement in spec.requirements.values():
        _refuse_transitive_ref(requirement, chain=chain)


def _load_source_spec(materialized: Path, *, name: str, chain: str) -> skillspec.SkillSpec:
    try:
        spec = skillspec.load_skill_spec(materialized)
    except skillspec.SkillSpecError as exc:
        raise SourceError(
            CODE_MEMBER_INVALID,
            f"Invalid skill manifest for {name} (via {chain}): {exc}",
        ) from exc
    _gate_source_requirements(spec, chain=f"{chain} -> {name}")
    return spec


def _canonical_requirement_identity(git_url: str, *, name: str, chain: str) -> str:
    try:
        identity = canonical_source_identity(git_url)
    except SourceIdentityError as exc:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Requirement {name} (via {chain}) names malformed Git source {git_url!r}: {exc}",
        ) from exc
    if identity is None:
        raise SourceError(
            CODE_SELECTION_INVALID,
            f"Requirement {name} (via {chain}) names a local Git source, "
            "but transitive requirements carry no package-owned local bindings",
        )
    return identity


def build_source_closure(
    config: GlobalConfig,
    roots: Sequence[SourceClosureRoot],
    *,
    acquire_git: Callable[[str, str, str], SourceGitAcquisition],
) -> list[ClosureNode]:
    """Expand schema-2 root members and their requirements into one closure.

    This is the schema-2 full closure: the traversal, unification, command
    validation and ordering are :func:`build_closure` itself, so identical
    transitive requirements unify under the existing closure rules including
    diamonds, and cycles fail. ``acquire_git`` is the resolving capability
    ``(git_url, commit, chain) -> SourceGitAcquisition``; the frozen mode
    never calls this function because it holds no such capability. Every
    transitive requirement must pin a revision; every acquired root skill
    name must equal the requiring name.
    """
    by_name: dict[str, SourceClosureRoot] = {}
    for root in roots:
        if root.name in by_name:
            raise SourceError(
                CODE_NAME_CONFLICT,
                f"Repeated root selection of installed skill name {root.name!r}",
            )
        by_name[root.name] = root

    prebuilt: dict[str, ClosureNode] = {}
    for root in roots:
        spec = _load_source_spec(root.materialized, name=root.name, chain=PROJECT_EDGE)
        if root.local:
            decl = manifest.SkillDecl(
                name=root.name,
                source=f"{root.from_alias}:{root.directory}",
                ref=manifest.SkillRef(SOURCE_LOCAL_REF_KIND, root.directory),
                git=None,
            )
            resolved = git_ops.ResolvedRef(SOURCE_LOCAL_REF_KIND, root.directory, "")
        else:
            if root.identity is None:
                raise SourceError(
                    CODE_MEMBER_INVALID,
                    f"Skill {root.name!r} Git root carries no repository identity",
                )
            decl = manifest.SkillDecl(
                name=root.name,
                source=f"{root.from_alias}:{root.directory}",
                ref=manifest.SkillRef(root.ref_kind, root.ref_value),
                git=root.identity,
            )
            resolved = git_ops.ResolvedRef(root.ref_kind, root.ref_value, root.commit)
        prebuilt[root.name] = ClosureNode(
            name=root.name,
            decl=decl,
            resolved=resolved,
            repo=root.materialized,
            snapshot=root.materialized,
            spec=spec,
            identity=root.identity,
            substituted=None,
        )

    def resolve_item(item: _Pending) -> ClosureNode:
        prebuilt_node = prebuilt.get(item.name)
        if prebuilt_node is not None:
            return prebuilt_node
        if not item.git:
            raise SourceError(
                CODE_MEMBER_INVALID,
                f"Requirement {item.name} (via {item.chain}) carries no Git source",
            )
        # The outgoing gate pinned every queued requirement to a revision;
        # recheck here so a direct resolver caller cannot smuggle a float.
        if item.ref.kind != "revision":
            raise SourceError(
                CODE_SELECTION_INVALID,
                f"Requirement {item.name} (via {item.chain}) declares "
                f"{item.ref.kind} {item.ref.value!r}, but transitive "
                "requirements must pin a revision",
            )
        identity = _canonical_requirement_identity(item.git, name=item.name, chain=item.chain)
        acquired = acquire_git(item.git, item.ref.value, item.chain)
        if acquired.identity != identity:
            raise SourceError(
                CODE_NAME_CONFLICT,
                f"Requirement {item.name} (via {item.chain}) names {identity} "
                f"but acquisition served {acquired.identity}",
            )
        spec = _load_source_spec(acquired.materialized, name=item.name, chain=item.chain)
        # Imported lazily: selection is downstream of the closure and the
        # name check runs only for newly acquired transitive requirements.
        from .sources.selection import read_skill_md_name

        skill_name = read_skill_md_name(
            acquired.materialized,
            f"requirement {item.name}",
            resolved_root=acquired.materialized,
        )
        if skill_name != item.name:
            raise SourceError(
                CODE_MEMBER_MISSING,
                f"Requirement {item.name} (via {item.chain}) resolves to skill "
                f"{skill_name!r}, which does not satisfy it",
            )
        return ClosureNode(
            name=item.name,
            decl=manifest.SkillDecl(
                name=item.name,
                source=item.name,
                ref=manifest.SkillRef(item.ref.kind, item.ref.value),
                git=item.git,
            ),
            resolved=git_ops.ResolvedRef(item.ref.kind, item.ref.value, acquired.commit),
            repo=acquired.materialized,
            snapshot=acquired.materialized,
            spec=spec,
            identity=identity,
            substituted=None,
        )

    synthesized = manifest.ProjectManifest(
        path=Path("."),
        skills=[
            manifest.SkillDecl(
                name=root.name,
                source=f"{root.from_alias}:{root.directory}",
                ref=manifest.SkillRef(
                    SOURCE_LOCAL_REF_KIND if root.local else root.ref_kind,
                    root.directory if root.local else root.ref_value,
                ),
                git=None if root.local else root.identity,
            )
            for root in roots
        ],
    )
    return build_closure(
        config,
        synthesized,
        {},
        node_resolver=resolve_item,
        error_factory=_source_closure_error,
        ref_comparator=_source_ref_commit,
    )
