from __future__ import annotations

import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from . import git_ops, manifest, skillspec, snapshot
from .config import GlobalConfig
from .dev_substitutions import Substitution
from .source_identity import canonical_source_identity, is_allowed


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
) -> list[ClosureNode]:
    """Expand direct skills and their requirements into an ordered closure.

    Within one closure a skill name resolves to one commit and one canonical
    source; providers precede consumers in the returned order.
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
            node = _resolve_node(
                config,
                item,
                substitutions.get(item.name),
                use_cache=use_cache,
                fetch_existing=fetch_existing,
                fetched_repos=fetched_repos,
                stack=stack,
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
            _unify(node, item)
        node.edges.append(item.edge)
        node.chains.append(item.chain)

    _validate_requirement_commands(nodes)
    return _topological_order(nodes)


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


def _unify(node: ClosureNode, item: _Pending) -> None:
    if node.substituted is not None:
        # A development substitution replaces every requirement of this name.
        return
    if item.git:
        identity = canonical_source_identity(item.git)
        if identity is not None:
            if node.identity is None:
                node.identity = identity
            elif node.identity != identity:
                raise ClosureError(
                    f"Source conflict for {node.name}: {node.identity} (via {_best_chain(node.chains)}) "
                    f"and {identity} (via {item.chain}) name different repositories"
                )
    if (item.ref.kind, item.ref.value) == (node.resolved.kind, node.resolved.ref):
        return
    try:
        other = git_ops.resolve_ref(node.repo, item.ref.kind, item.ref.value)
    except git_ops.GitError as exc:
        raise ClosureError(
            f"Cannot resolve {item.ref.kind} {item.ref.value!r} for {node.name} (via {item.chain}): {exc}"
        ) from exc
    if other.commit != node.resolved.commit:
        raise ClosureError(
            f"Version conflict for {node.name}: {node.resolved.kind} {node.resolved.ref} "
            f"-> {node.resolved.commit[:12]} (via {_best_chain(node.chains)}) and {item.ref.kind} "
            f"{item.ref.value} -> {other.commit[:12]} (via {item.chain}); "
            "align the requirement refs at their declarations"
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
        repo = _ensure_dev_repo(config, item.name, substitution, use_cache=use_cache, stack=stack)
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
        )
        try:
            resolved = git_ops.resolve_ref(repo, item.ref.kind, item.ref.value)
        except git_ops.GitError as exc:
            raise ClosureError(
                f"Cannot resolve {item.ref.kind} {item.ref.value!r} for {item.name} (via {item.chain}): {exc}"
            ) from exc

    snap = _snapshot_for(config, item.source, repo, resolved.commit, use_cache=use_cache, stack=stack)
    if git_ops.repository_has_submodules(snap):
        raise ClosureError(f"Submodules are unsupported in MVP: {item.source}")
    spec = skillspec.load_skill_spec(snap)
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
) -> Path:
    repo = config.skills_root / item.source
    if repo.exists():
        if not (repo / ".git").exists():
            raise ClosureError(f"Local skill path exists but is not a git repository: {repo}")
        repo_key = repo.resolve()
        if fetch_existing and use_cache and repo_key not in fetched_repos:
            try:
                git_ops.fetch_repo(repo)
            except git_ops.GitError as exc:
                raise ClosureError(f"Failed to fetch {item.name} at {repo} (via {item.chain}): {exc}") from exc
            fetched_repos.add(repo_key)
        return repo
    if not item.git:
        raise ClosureError(f"Skill repository not found for {item.name}: {repo} (via {item.chain})")
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
) -> Path:
    git_url = substitution.git or ""
    if not use_cache:
        destination = _temp_repo_dir(stack, name)
        git_ops.clone_repo(git_url, destination)
        return destination
    # Dev clones live outside skills_root so a substitution never shadows the
    # declared source repository.
    repo = config.path.parent / "dev" / name
    if repo.exists() and (repo / ".git").exists():
        git_ops.fetch_repo(repo)
        return repo
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
) -> Path:
    if use_cache:
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


def _validate_requirement_commands(nodes: dict[str, ClosureNode]) -> None:
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
        raise ClosureError("; ".join(errors))


def _topological_order(nodes: dict[str, ClosureNode]) -> list[ClosureNode]:
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
        raise ClosureError(f"Dependency cycle between skills: {', '.join(remaining)}")
    return ordered
