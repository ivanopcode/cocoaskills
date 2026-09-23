from __future__ import annotations

import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path, PurePosixPath
from typing import Any, Final

from . import protocol_json
from .audit.capabilities import CapabilityManifest, CapabilityParseError, parse_capabilities
from .build_repository import (
    GO_REPOSITORY_V1_DRIVER,
    BuildRepository,
    BuildRepositoryError,
    is_valid_ref_name,
    parse_locked_commit,
    parse_repository_source,
)
from .builds import GO_V1_DRIVER
from .builds.module_roots import ModuleRootError, validate_declaration
from .identifiers import IDENTIFIER_RULE, is_valid_identifier, is_valid_portable_path


SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3, 4, 5, 6, 7, 8}
CANONICAL_MANIFEST = "agent-skill.json"
LEGACY_MANIFEST = "csk-skill.json"
RUNTIME_FALLBACK = "agents/runtime.json"
# Grammar labels for the two manifest grammars: the validated schema grammar
# (``schema_version`` plus the closed command schemata) and the unversioned
# runtime fallback grammar (``commands`` mapping names to script paths).
MANIFEST_GRAMMAR_SCHEMA: Final = "schema"
MANIFEST_GRAMMAR_RUNTIME: Final = "runtime"
# The registry carries what each spelling IS, not just its name: grammar per
# spelling, in resolution order. The snapshot loader, the diagnostics
# selector, and the root-input probe in sources.selection all iterate the
# derived order below, and the loader and the gate both call
# select_effective_manifest for the effective manifest plus its grammar, so
# no caller reimplements precedence or grammar dispatch. A spelling absent
# from this map (a monkeypatched growth entry, or a future version's new
# spelling before its grammar is registered) infers its grammar from content
# inside the one shared function, never per caller.
MANIFEST_GRAMMAR: Final[dict[str, str]] = {
    CANONICAL_MANIFEST: MANIFEST_GRAMMAR_SCHEMA,
    LEGACY_MANIFEST: MANIFEST_GRAMMAR_SCHEMA,
    RUNTIME_FALLBACK: MANIFEST_GRAMMAR_RUNTIME,
}
# The manifest spellings load_skill_spec resolves, in resolution order,
# derived from the registry above.
MANIFEST_PROBE_ORDER: Final[tuple[str, ...]] = tuple(MANIFEST_GRAMMAR)
UPGRADE_HINT = (
    "Upgrade with: pipx upgrade cocoaskills, brew upgrade cocoaskills, "
    "or mise upgrade pipx:cocoaskills."
)

REQUIREMENT_MODES = {"full", "runtime", "context"}
REQUIREMENT_REF_KINDS = {"tag", "revision"}
_RANGE_MARKERS = ("^", "~", ">", "<", "*", " ")
_SCHEMA_V1_RESERVED_TOP_LEVEL_FIELDS = frozenset(
    {
        "build_roots",
        "build_repositories",
        "driver",
        "repository",
        "target",
        "modules",
        "execution_policy",
        "interpreter",
    }
)
_SCHEMA_V1_RESERVED_COMMAND_FIELDS = frozenset(
    {
        "driver",
        "source_dir",
        "repository",
        "target",
        "modules",
        "execution_policy",
        "interpreter",
    }
)

# Protocol 1.0 defines exactly one script execution policy and exactly two
# interpreter identifiers. Both value spaces are closed: a manifest that names
# anything else is invalid, never a forward-compatible extension.
SCRIPT_WORKER_V1_POLICY = "script-worker-v1"
SCRIPT_EXECUTION_POLICIES = frozenset({SCRIPT_WORKER_V1_POLICY})
SCRIPT_INTERPRETERS = frozenset({"node-v1", "python3-v1"})

# csk does not implement ``script-worker-v1``. Protocol Core section 4.1.1
# requires such a manager to reject an enforced command outright: installing it
# declared-only, downgrading it, or ignoring the field would publish a shim
# that runs package code the manifest says is contained.
SCRIPT_EXECUTION_POLICIES_IMPLEMENTED: frozenset[str] = frozenset()
SCRIPT_EXECUTION_POLICY_UNSUPPORTED = "script_execution_policy_unsupported"

MCP_TRANSPORTS = {"stdio", "http"}
MCP_REQUIRED_IN = {"any", "all"}


class SkillSpecError(Exception):
    pass


@dataclass(frozen=True)
class CommandSpec:
    name: str
    type: str
    command: str | None = None
    unix_path: str | None = None
    win_path: str | None = None
    hint: str | None = None
    source: str = CANONICAL_MANIFEST
    driver: str | None = None
    source_dir: str | None = None
    repository: str | None = None
    target: str | None = None
    execution_policy: str | None = None
    interpreter: str | None = None
    modules: tuple[str, ...] = ()


@dataclass(frozen=True)
class DependencySpec:
    name: str
    type: str
    command: str | None = None
    skill: str | None = None
    hint: str | None = None
    source: str = CANONICAL_MANIFEST


@dataclass(frozen=True)
class SkillRequirement:
    """A self-contained skill-to-skill requirement (dependencies.skills)."""

    name: str
    git: str
    ref_kind: str
    ref_value: str
    mode: str = "full"
    commands: tuple[str, ...] = ()
    source: str = CANONICAL_MANIFEST


@dataclass(frozen=True)
class McpServerRequirement:
    """A declared dependency on an MCP server configured in agent environments."""

    name: str
    hint: str
    transport: str | None = None
    required_in: str = "any"
    source: str = CANONICAL_MANIFEST


@dataclass(frozen=True)
class SkillSpec:
    commands: dict[str, CommandSpec]
    source_file: str | None
    schema_version: int = SCHEMA_VERSION
    runtime_roots: tuple[str, ...] = ()
    capabilities: CapabilityManifest = field(default_factory=CapabilityManifest.implicit_none)
    dependencies: dict[str, DependencySpec] = field(default_factory=dict)
    requirements: dict[str, SkillRequirement] = field(default_factory=dict)
    mcp_servers: dict[str, McpServerRequirement] = field(default_factory=dict)
    build_roots: tuple[str, ...] = ()
    build_repositories: dict[str, BuildRepository] = field(default_factory=dict)


# Members of SkillSpec that never name a source-relative input: the schema
# version integer; the manifest filename metadata (the effective manifest is
# required via the descriptor probe in selection, not via this property);
# capabilities (host capability grants, not source inputs); dependencies,
# requirements, MCP servers and build repositories (identifiers, URLs and
# refs, never source-relative paths). Every other record member can name a
# source-relative input. The growth test pins this exclusion set, so a new
# record member is a required input by construction unless it is consciously
# added here with a justification.
_SKILLSPEC_NONPATH_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "source_file",
        "schema_version",
        "capabilities",
        "dependencies",
        "requirements",
        "mcp_servers",
        "build_repositories",
    }
)

# Members of CommandSpec that never name a source-relative input: the command
# name, type, system command identifier, hint, manifest filename metadata,
# build driver, repository/target identifiers, and the schema-8 execution
# policy pair. Every other record member can name a source-relative input.
_COMMANDSPEC_NONPATH_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "type",
        "command",
        "hint",
        "source",
        "driver",
        "repository",
        "target",
        "execution_policy",
        "interpreter",
    }
)

# The one member tables driving required-input derivation, in record order:
# every SkillSpec/CommandSpec member except the exclusions above. The tables
# derive from the records' own fields, so a new record member extends the
# derivation by construction, and the growth test fails unless the member is
# either handled below or consciously excluded above.
SKILLSPEC_PATH_MEMBERS: Final[tuple[str, ...]] = tuple(
    member.name
    for member in dataclass_fields(SkillSpec)
    if member.name not in _SKILLSPEC_NONPATH_MEMBERS
)
COMMANDSPEC_PATH_MEMBERS: Final[tuple[str, ...]] = tuple(
    member.name
    for member in dataclass_fields(CommandSpec)
    if member.name not in _COMMANDSPEC_NONPATH_MEMBERS
)


def _command_declared_paths(command: CommandSpec) -> tuple[str, ...]:
    """Return every source-relative path one command declares, in field order."""

    paths: list[str] = []
    for member in COMMANDSPEC_PATH_MEMBERS:
        value = getattr(command, member)
        if value is None:
            continue
        if isinstance(value, str):
            paths.append(value)
        elif isinstance(value, tuple):
            paths.extend(value)
        else:  # pragma: no cover - fail closed on an unhandled member shape
            raise AssertionError(f"unhandled CommandSpec path member: {member!r}")
    return tuple(paths)


def declared_required_inputs(spec: SkillSpec) -> tuple[str, ...]:
    """Return every source-relative path the manifest declares, deduped.

    Runtime roots, build roots, and every command path (script ``unix_path``
    / ``win_path`` on every schema including schema 1, build ``source_dir``
    and schema-8 ``modules``), in record order with commands in ascending
    name order. The caller prepends the effective manifest filename(s) and
    the gate adds ``SKILL.md``; coverage (the required path at-or-below an
    admitted entry) is decided by the shared boundary validator, never here.
    """

    paths: list[str] = []
    for member in SKILLSPEC_PATH_MEMBERS:
        if member == "runtime_roots":
            paths.extend(spec.runtime_roots)
        elif member == "build_roots":
            paths.extend(spec.build_roots)
        elif member == "commands":
            for name in sorted(spec.commands):
                paths.extend(_command_declared_paths(spec.commands[name]))
        else:  # pragma: no cover - fail closed on an unhandled member
            raise AssertionError(f"unhandled SkillSpec path member: {member!r}")
    return tuple(dict.fromkeys(paths))


def infer_manifest_grammar(raw: bytes) -> str:
    """Infer the grammar for a spelling unknown to this version.

    The one inference implementation: ``schema_version`` present means the
    schema grammar, otherwise the runtime grammar. Malformed JSON infers
    schema, so a corrupt unknown manifest takes schema precedence and
    refuses loudly instead of being silently outranked by a valid runtime
    spelling.
    """

    try:
        data = protocol_json.loads(raw)
    except protocol_json.ProtocolJSONError:
        return MANIFEST_GRAMMAR_SCHEMA
    if isinstance(data, dict) and "schema_version" in data:
        return MANIFEST_GRAMMAR_SCHEMA
    return MANIFEST_GRAMMAR_RUNTIME


def manifest_grammar(name: str, raw: bytes) -> str:
    """Return the grammar for one spelling: registry entry, else inferred."""

    known = MANIFEST_GRAMMAR.get(name)
    if known is not None:
        return known
    return infer_manifest_grammar(raw)


@dataclass(frozen=True)
class EffectiveManifestDecision:
    """The one effective-manifest decision, shared by loader and gate."""

    name: str
    grammar: str
    schema_names: tuple[str, ...]
    runtime_names: tuple[str, ...]


def select_effective_manifest(
    present_names: Sequence[str],
    read_bytes: Callable[[str], bytes],
) -> EffectiveManifestDecision | None:
    """Decide the effective manifest and its grammar: the one selection rule.

    Both :func:`load_skill_spec` and the root-input gate in
    ``sources.selection`` call this; no caller reimplements precedence or
    grammar dispatch. ``present_names`` is in registry order. Known
    spellings use the registry without reading (so a runtime fallback that
    is a directory or unreadable is still ignored when a schema manifest
    is present, exactly as before); unknown spellings are read once via
    ``read_bytes`` and inferred by :func:`infer_manifest_grammar`.
    Schema-grammar manifests win over runtime regardless of registry
    position (the historical precedence); within a grammar, registry
    order wins.
    """

    classified: list[tuple[str, str]] = []
    for name in present_names:
        known = MANIFEST_GRAMMAR.get(name)
        if known is not None:
            classified.append((name, known))
        else:
            classified.append((name, infer_manifest_grammar(read_bytes(name))))
    schema_names = tuple(name for name, grammar in classified if grammar == MANIFEST_GRAMMAR_SCHEMA)
    runtime_names = tuple(name for name, grammar in classified if grammar == MANIFEST_GRAMMAR_RUNTIME)
    if schema_names:
        return EffectiveManifestDecision(
            name=schema_names[0],
            grammar=MANIFEST_GRAMMAR_SCHEMA,
            schema_names=schema_names,
            runtime_names=runtime_names,
        )
    if runtime_names:
        return EffectiveManifestDecision(
            name=runtime_names[0],
            grammar=MANIFEST_GRAMMAR_RUNTIME,
            schema_names=schema_names,
            runtime_names=runtime_names,
        )
    return None


def load_skill_spec(snapshot: Path) -> SkillSpec:
    """Load the effective skill manifest from a snapshot directory.

    The registry controls loading: the effective manifest and its grammar
    come from :func:`select_effective_manifest`, the same decision the
    root-input gate uses, so the loader and the gate cannot disagree about
    which manifest is effective or which grammar parses it. The
    canonical/legacy conflict check is preserved without a private copy
    of the order: among all schema-grammar manifests present, differing
    JSON values refuse. A spelling unknown to this version infers its
    grammar from content inside the shared selector, so a newly
    registered spelling is honoured, not silently ignored.
    """

    present = [name for name in MANIFEST_PROBE_ORDER if (snapshot / Path(name)).exists()]
    if not present:
        return SkillSpec(commands={}, source_file=None)
    cache: dict[str, bytes] = {}

    def _read(name: str) -> bytes:
        if name not in cache:
            cache[name] = (snapshot / Path(name)).read_bytes()
        return cache[name]

    decision = select_effective_manifest(present, _read)
    assert decision is not None
    if decision.schema_names:
        specs: list[SkillSpec] = []
        datas: list[dict[str, Any]] = []
        for name in decision.schema_names:
            path = snapshot / Path(name)
            if name == CANONICAL_MANIFEST or name == LEGACY_MANIFEST:
                spec, data = _load_skill_manifest(path)
            else:
                raw = _read(name)
                try:
                    data = protocol_json.loads(raw)
                except protocol_json.ProtocolJSONError as exc:
                    raise SkillSpecError(f"Malformed JSON in {path}: {exc}") from exc
                if not isinstance(data, dict):
                    raise SkillSpecError(f"{path} must contain a JSON object")
                spec = _parse_validated_manifest(data, snapshot, name)
            specs.append(spec)
            datas.append(data)
        for other in datas[1:]:
            if not _json_values_equal(datas[0], other):
                if set(decision.schema_names) == {CANONICAL_MANIFEST, LEGACY_MANIFEST} and len(
                    decision.schema_names
                ) == 2:
                    raise SkillSpecError(
                        f"conflicting_skill_manifests: {CANONICAL_MANIFEST} and "
                        f"{LEGACY_MANIFEST} contain different JSON values"
                    )
                joined = ", ".join(decision.schema_names)
                raise SkillSpecError(
                    f"conflicting_skill_manifests: {joined} contain different JSON values"
                )
        return specs[0]
    name = decision.name
    path = snapshot / Path(name)
    return parse_runtime_fallback_bytes(_read(name), name, label=path)


def manifest_source_path(snapshot: Path) -> str:
    """Return the shared effective-manifest decision's path for diagnostics."""

    present = tuple(
        name for name in MANIFEST_PROBE_ORDER if (snapshot / Path(name)).exists()
    )
    if not present:
        return ""
    decision = select_effective_manifest(
        present, lambda name: (snapshot / Path(name)).read_bytes()
    )
    return decision.name if decision is not None else ""


def _json_values_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_json_values_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_values_equal(a, b) for a, b in zip(left, right, strict=True))
    return bool(left == right)


def _load_skill_manifest(path: Path) -> tuple[SkillSpec, dict[str, Any]]:
    try:
        data = protocol_json.loads(path.read_bytes())
    except protocol_json.ProtocolJSONError as exc:
        raise SkillSpecError(f"Malformed JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillSpecError(f"{path} must contain a JSON object")
    return _parse_validated_manifest(data, path.parent, path.name), data


def parse_manifest_bytes(raw: bytes, snapshot: Path, source_file: str) -> SkillSpec:
    """Parse and fully validate one manifest document against a snapshot.

    The one manifest implementation, shared by the snapshot loader and the
    root-input gate: the gate validates the descriptor-read bytes against
    the source root to derive the required inputs, and the snapshot loader
    validates the admitted bytes the same way. A malformed document or a
    manifest invalid against the given snapshot raises ``SkillSpecError``;
    the gate treats that as no derived inputs here and lets the snapshot
    validation refuse precisely later.
    """

    try:
        data = protocol_json.loads(raw)
    except protocol_json.ProtocolJSONError as exc:
        raise SkillSpecError(f"Malformed JSON in {source_file}: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillSpecError(f"{source_file} must contain a JSON object")
    return _parse_validated_manifest(data, snapshot, source_file)


def _parse_validated_manifest(
    data: dict[str, Any], snapshot: Path, source_file: str
) -> SkillSpec:
    schema = data.get("schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise SkillSpecError(f"{source_file} field 'schema_version' must be an integer")
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise SkillSpecError(
            f"Unsupported {source_file} schema_version {schema!r}; this skill requires a newer csk. "
            f"{UPGRADE_HINT}"
        )
    if schema == 1:
        reserved_fields = sorted(data.keys() & _SCHEMA_V1_RESERVED_TOP_LEVEL_FIELDS)
        if reserved_fields:
            joined = ", ".join(repr(key) for key in reserved_fields)
            raise SkillSpecError(f"{source_file} has unsupported field(s): {joined}")
    if schema >= 2:
        allowed_fields = {"schema_version", "runtime_roots", "commands", "dependencies"}
        if schema >= 3:
            allowed_fields.add("capabilities")
        if schema >= 6:
            allowed_fields.add("build_roots")
        if schema >= 7:
            allowed_fields.add("build_repositories")
        _reject_unknown_fields(data, allowed_fields, source_file)
    if schema >= 3 and "capabilities" not in data:
        raise SkillSpecError(f"{source_file} schema v{schema} requires 'capabilities'")
    try:
        capabilities = (
            parse_capabilities(data.get("capabilities")) if schema >= 3 else CapabilityManifest.implicit_none()
        )
    except CapabilityParseError as exc:
        raise SkillSpecError(str(exc)) from exc
    runtime_roots_raw = data["runtime_roots"] if schema >= 2 and "runtime_roots" in data else []
    runtime_roots = (
        _parse_runtime_roots(runtime_roots_raw, snapshot=snapshot, source_file=source_file)
        if schema >= 2
        else ()
    )
    build_roots_raw = data["build_roots"] if schema >= 6 and "build_roots" in data else []
    build_roots = (
        _parse_build_roots(
            build_roots_raw,
            snapshot=snapshot,
            runtime_roots=runtime_roots,
            source_file=source_file,
        )
        if schema >= 6
        else ()
    )
    build_repositories = _parse_build_repositories(data.get("build_repositories"), schema=schema)
    commands_raw = data.get("commands", {})
    if not isinstance(commands_raw, dict):
        raise SkillSpecError(f"{source_file} field 'commands' must be an object")
    commands: dict[str, CommandSpec] = {}
    command_items = sorted(commands_raw.items()) if schema >= 6 else commands_raw.items()
    for name, raw in command_items:
        if not isinstance(name, str) or not name:
            raise SkillSpecError("Command names must be non-empty strings")
        if not is_valid_identifier(name):
            raise SkillSpecError(f"Command name {name!r} {IDENTIFIER_RULE}")
        if not isinstance(raw, dict):
            raise SkillSpecError(f"Command {name!r} must be an object")
        if schema == 1:
            reserved_fields = sorted(raw.keys() & _SCHEMA_V1_RESERVED_COMMAND_FIELDS)
            if reserved_fields:
                joined = ", ".join(repr(key) for key in reserved_fields)
                raise SkillSpecError(f"commands.{name} has unsupported field(s): {joined}")
        command_type = raw.get("type")
        if command_type == "script":
            if schema >= 2:
                allowed_script_fields = {"type", "unix_path", "win_path"}
                if schema >= 8:
                    allowed_script_fields |= {"execution_policy", "interpreter"}
                _reject_unknown_fields(raw, allowed_script_fields, f"commands.{name}")
            execution_policy, interpreter = _parse_script_execution_policy(
                raw, schema=schema, label=f"commands.{name}"
            )
            unix_path = raw.get("unix_path")
            win_path = raw.get("win_path")
            if schema >= 2 and unix_path is None and win_path is None:
                raise SkillSpecError(f"Script command {name!r} requires 'unix_path' or 'win_path'")
            if unix_path is not None:
                unix_path = _validate_relative_path(
                    unix_path,
                    field=f"commands.{name}.unix_path",
                    strict_posix=schema >= 2,
                )
                if schema >= 2:
                    _validate_v2_script_path(snapshot, unix_path, runtime_roots, field=f"commands.{name}.unix_path")
            if win_path is not None:
                win_path = _validate_relative_path(
                    win_path,
                    field=f"commands.{name}.win_path",
                    strict_posix=schema >= 2,
                )
                if schema >= 2:
                    _validate_v2_script_path(snapshot, win_path, runtime_roots, field=f"commands.{name}.win_path")
            commands[name] = CommandSpec(
                name=name,
                type="script",
                unix_path=unix_path,
                win_path=win_path,
                execution_policy=execution_policy,
                interpreter=interpreter,
                source=source_file,
            )
        elif command_type == "system":
            if schema >= 2:
                _reject_unknown_fields(raw, {"type", "command", "hint"}, f"commands.{name}")
            command = raw.get("command")
            if not isinstance(command, str) or not command:
                raise SkillSpecError(f"System command {name!r} requires non-empty 'command'")
            if schema >= 6 and not is_valid_identifier(command):
                raise SkillSpecError(f"commands.{name}.command system command {command!r} {IDENTIFIER_RULE}")
            hint = raw.get("hint")
            if hint is not None and not isinstance(hint, str):
                raise SkillSpecError(f"System command {name!r} field 'hint' must be a string")
            if schema >= 6 and "hint" in raw and hint == "":
                raise SkillSpecError(f"commands.{name}.hint must be a non-empty string")
            commands[name] = CommandSpec(
                name=name,
                type="system",
                command=command,
                hint=hint,
                source=source_file,
            )
        elif command_type == "build" and schema >= 6 and raw.get("driver") == GO_V1_DRIVER:
            allowed_build_fields = {"type", "driver", "source_dir"}
            if schema >= 8:
                allowed_build_fields.add("modules")
            _reject_unknown_fields(raw, allowed_build_fields, f"commands.{name}")
            driver = raw.get("driver")
            source_dir = _validate_relative_path(
                raw.get("source_dir"),
                field=f"commands.{name}.source_dir",
                strict_posix=True,
            )
            modules = (
                _parse_declared_modules(raw["modules"], label=f"commands.{name}")
                if "modules" in raw
                else ()
            )
            commands[name] = CommandSpec(
                name=name,
                type="build",
                driver=driver,
                source_dir=source_dir,
                modules=modules,
                source=source_file,
            )
        elif command_type == "build" and schema >= 7 and raw.get("driver") == GO_REPOSITORY_V1_DRIVER:
            _reject_unknown_fields(raw, {"type", "driver", "repository", "target"}, f"commands.{name}")
            repository = raw.get("repository")
            target = raw.get("target")
            if not isinstance(repository, str) or not is_valid_identifier(repository):
                raise SkillSpecError(f"commands.{name}.repository must be a portable repository identifier")
            if not isinstance(target, str) or not is_valid_identifier(target):
                raise SkillSpecError(f"commands.{name}.target must be a portable target identifier")
            commands[name] = CommandSpec(
                name=name,
                type="build",
                driver=GO_REPOSITORY_V1_DRIVER,
                repository=repository,
                target=target,
                source=source_file,
            )
        elif command_type == "build" and schema >= 6:
            expected = (
                f"{GO_V1_DRIVER!r} or {GO_REPOSITORY_V1_DRIVER!r}"
                if schema >= 7
                else repr(GO_V1_DRIVER)
            )
            raise SkillSpecError(f"Command {name!r} field 'driver' must be {expected}")
        else:
            raise SkillSpecError(f"Command {name!r} has unsupported type {command_type!r}")
    if schema >= 6:
        _validate_build_layout(snapshot, build_roots, runtime_roots, commands, schema=schema)
    if schema >= 7:
        _validate_repository_commands(build_repositories, commands)
    dependencies, requirements, mcp_servers = _parse_dependencies(
        data.get("dependencies"), schema=schema, source_file=source_file
    )
    return SkillSpec(
        commands=commands,
        source_file=source_file,
        schema_version=schema,
        runtime_roots=runtime_roots,
        build_roots=build_roots,
        capabilities=capabilities,
        dependencies=dependencies,
        requirements=requirements,
        mcp_servers=mcp_servers,
        build_repositories=build_repositories,
    )


def _parse_build_repositories(raw: Any, *, schema: int) -> dict[str, BuildRepository]:
    if schema < 7:
        return {}
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not raw:
        raise SkillSpecError("build_repositories must be a non-empty object when present")
    repositories: dict[str, BuildRepository] = {}
    for name in sorted(raw):
        label = f"build_repositories.{name}"
        if not isinstance(name, str) or not is_valid_identifier(name):
            raise SkillSpecError(f"{label} repository name {IDENTIFIER_RULE}")
        entry = raw[name]
        if not isinstance(entry, dict):
            raise SkillSpecError(f"{label} must be an object")
        _reject_unknown_fields(entry, {"git", "locked_commit", "tag"}, label)
        git = entry.get("git")
        if not isinstance(git, str):
            raise SkillSpecError(f"{label}.git must be an HTTPS or SSH repository URL")
        try:
            source = parse_repository_source(git)
            locked_commit = parse_locked_commit(entry.get("locked_commit"), field=f"{label}.locked_commit")
        except BuildRepositoryError as exc:
            raise SkillSpecError(str(exc)) from exc
        tag = entry.get("tag")
        if tag is not None and (not isinstance(tag, str) or not is_valid_ref_name(tag)):
            raise SkillSpecError(f"{label}.tag must be a safe immutable Git tag name")
        repositories[name] = BuildRepository(
            name=name,
            git=git,
            identity=source.identity,
            transport=source.transport,
            locked_commit=locked_commit,
            tag=tag,
        )
    return repositories


def _validate_repository_commands(
    repositories: dict[str, BuildRepository], commands: dict[str, CommandSpec]
) -> None:
    selected: set[str] = set()
    for name in sorted(commands):
        command = commands[name]
        if command.driver != GO_REPOSITORY_V1_DRIVER:
            continue
        assert command.repository is not None
        if command.repository not in repositories:
            raise SkillSpecError(
                f"commands.{name}.repository selects undeclared build repository {command.repository!r}"
            )
        selected.add(command.repository)
    for name in sorted(repositories):
        if name not in selected:
            raise SkillSpecError(
                f"build_repositories.{name} is not selected by any {GO_REPOSITORY_V1_DRIVER} command"
            )


def parse_runtime_fallback_bytes(
    raw: bytes,
    source_file: str = RUNTIME_FALLBACK,
    *,
    label: str | Path | None = None,
) -> SkillSpec:
    """Parse one runtime-fallback document from bytes.

    The one runtime-grammar implementation, shared by the snapshot
    loader and the root-input gate: the gate derives the fallback's
    declared command paths from the descriptor-read bytes, and the
    snapshot loader validates the admitted bytes the same way. A
    malformed document raises ``SkillSpecError``; the gate treats that
    as no derived inputs here and lets the snapshot validation refuse
    precisely later.

    ``source_file`` is the source identity stored on the spec (the
    manifest spelling); ``label`` is the diagnostic label shown in the
    malformed-JSON message. The loader passes the full snapshot path
    as the label, preserving the historical message bytes; callers
    that swallow the error (the gate) leave the label defaulting to
    the spelling.
    """

    display = str(label) if label is not None else source_file
    try:
        data = protocol_json.loads(raw)
    except protocol_json.ProtocolJSONError as exc:
        raise SkillSpecError(f"Malformed JSON in {display}: {exc}") from exc
    commands_raw = data.get("commands", {}) if isinstance(data, dict) else {}
    if not isinstance(commands_raw, dict):
        raise SkillSpecError(f"{source_file} field 'commands' must be an object")
    commands: dict[str, CommandSpec] = {}
    for name, rel_path in commands_raw.items():
        if not isinstance(name, str) or not name:
            raise SkillSpecError("Runtime command names must be non-empty strings")
        if not is_valid_identifier(name):
            raise SkillSpecError(f"Runtime command name {name!r} {IDENTIFIER_RULE}")
        if not isinstance(rel_path, str) or not rel_path:
            raise SkillSpecError(f"Runtime command {name!r} path must be a non-empty string")
        _validate_relative_path(rel_path, field=f"commands.{name}")
        commands[name] = CommandSpec(
            name=name,
            type="script",
            unix_path=rel_path,
            win_path=rel_path if rel_path.endswith(".cmd") else None,
            source=source_file,
        )
    return SkillSpec(commands=commands, source_file=source_file)


def _load_runtime_fallback(path: Path) -> SkillSpec:
    return parse_runtime_fallback_bytes(path.read_bytes(), RUNTIME_FALLBACK, label=path)


def _parse_dependencies(
    raw: Any, *, schema: int, source_file: str = CANONICAL_MANIFEST
) -> tuple[dict[str, DependencySpec], dict[str, SkillRequirement], dict[str, McpServerRequirement]]:
    if raw is None:
        return {}, {}, {}
    if schema < 2:
        raise SkillSpecError(f"{source_file} field 'dependencies' requires schema_version 2 or newer")
    if not isinstance(raw, dict):
        raise SkillSpecError(f"{source_file} field 'dependencies' must be an object")
    if schema < 4 and "skills" in raw:
        raise SkillSpecError(f"{source_file} field 'dependencies.skills' requires schema_version 4")
    if schema < 5 and "mcp_servers" in raw:
        raise SkillSpecError(f"{source_file} field 'dependencies.mcp_servers' requires schema_version 5")
    allowed = {"commands"}
    if schema >= 4:
        allowed.add("skills")
    if schema >= 5:
        allowed.add("mcp_servers")
    _reject_unknown_fields(raw, allowed, "dependencies")
    requirements = _parse_requirements(raw.get("skills"), schema=schema, source_file=source_file)
    mcp_servers = _parse_mcp_servers(raw.get("mcp_servers"), source_file=source_file)
    commands_raw = raw.get("commands", {})
    if not isinstance(commands_raw, dict):
        raise SkillSpecError("dependencies.commands must be an object")

    dependencies: dict[str, DependencySpec] = {}
    for name, entry in commands_raw.items():
        if not isinstance(name, str) or not name:
            raise SkillSpecError("Dependency command names must be non-empty strings")
        if not is_valid_identifier(name):
            raise SkillSpecError(f"Dependency command name {name!r} {IDENTIFIER_RULE}")
        if not isinstance(entry, dict):
            raise SkillSpecError(f"dependencies.commands.{name} must be an object")
        dependency_type = entry.get("type")
        hint = entry.get("hint")
        if hint is not None and not isinstance(hint, str):
            raise SkillSpecError(f"dependencies.commands.{name}.hint must be a string")
        if dependency_type == "system":
            _reject_unknown_fields(entry, {"type", "command", "hint"}, f"dependencies.commands.{name}")
            command = entry.get("command")
            if not isinstance(command, str) or not command:
                raise SkillSpecError(f"System dependency {name!r} requires non-empty 'command'")
            dependencies[name] = DependencySpec(
                name=name,
                type="system",
                command=command,
                hint=hint,
                source=source_file,
            )
        elif dependency_type == "skill":
            _reject_unknown_fields(entry, {"type", "skill", "command", "hint"}, f"dependencies.commands.{name}")
            skill = entry.get("skill")
            if not isinstance(skill, str) or not skill:
                raise SkillSpecError(f"Skill dependency {name!r} requires non-empty 'skill'")
            if not is_valid_identifier(skill):
                raise SkillSpecError(f"Skill dependency name {skill!r} {IDENTIFIER_RULE}")
            command = entry.get("command")
            if not isinstance(command, str) or not command:
                raise SkillSpecError(f"Skill dependency {name!r} requires non-empty 'command'")
            if not is_valid_identifier(command):
                raise SkillSpecError(f"Skill dependency command {command!r} {IDENTIFIER_RULE}")
            dependencies[name] = DependencySpec(
                name=name,
                type="skill",
                command=command,
                skill=skill,
                hint=hint,
                source=source_file,
            )
        else:
            raise SkillSpecError(f"Dependency command {name!r} has unsupported type {dependency_type!r}")
    return dependencies, requirements, mcp_servers


def _parse_mcp_servers(
    raw: Any, *, source_file: str = CANONICAL_MANIFEST
) -> dict[str, McpServerRequirement]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SkillSpecError("dependencies.mcp_servers must be an object")
    servers: dict[str, McpServerRequirement] = {}
    for name, entry in raw.items():
        label = f"dependencies.mcp_servers.{name}"
        if not isinstance(name, str) or not name:
            raise SkillSpecError("MCP server names must be non-empty strings")
        if not is_valid_identifier(name):
            raise SkillSpecError(f"MCP server name {name!r} {IDENTIFIER_RULE}")
        if not isinstance(entry, dict):
            raise SkillSpecError(f"{label} must be an object")
        _reject_unknown_fields(entry, {"hint", "transport", "required_in"}, label)
        hint = entry.get("hint")
        if not isinstance(hint, str) or not hint:
            raise SkillSpecError(f"{label} requires a non-empty 'hint' describing how to connect the server")
        transport = entry.get("transport")
        if transport is not None and transport not in MCP_TRANSPORTS:
            raise SkillSpecError(f"{label}.transport must be 'stdio' or 'http'")
        required_in = entry.get("required_in", "any")
        if required_in not in MCP_REQUIRED_IN:
            raise SkillSpecError(f"{label}.required_in must be 'any' or 'all'")
        servers[name] = McpServerRequirement(
            name=name,
            hint=hint,
            transport=transport,
            required_in=required_in,
            source=source_file,
        )
    return servers


def _parse_requirements(
    raw: Any, *, schema: int, source_file: str = CANONICAL_MANIFEST
) -> dict[str, SkillRequirement]:
    if raw is None:
        return {}
    if schema < 4:
        raise SkillSpecError(f"{source_file} field 'dependencies.skills' requires schema_version 4")
    if not isinstance(raw, dict):
        raise SkillSpecError("dependencies.skills must be an object")
    requirements: dict[str, SkillRequirement] = {}
    for name, entry in raw.items():
        label = f"dependencies.skills.{name}"
        if not isinstance(name, str) or not name:
            raise SkillSpecError("Skill requirement names must be non-empty strings")
        if not is_valid_identifier(name):
            raise SkillSpecError(f"Skill requirement name {name!r} {IDENTIFIER_RULE}")
        if not isinstance(entry, dict):
            raise SkillSpecError(f"{label} must be an object")
        if "version" in entry:
            raise SkillSpecError(
                f"{label} declares 'version'; version ranges are not supported. "
                "Pin an exact ref: {\"kind\": \"tag\" | \"revision\", \"value\": ...}"
            )
        _reject_unknown_fields(entry, {"git", "ref", "mode", "commands"}, label)

        git = entry.get("git")
        if not isinstance(git, str) or not git:
            raise SkillSpecError(f"{label} requires a non-empty 'git' source URL")

        ref = entry.get("ref")
        if not isinstance(ref, dict):
            raise SkillSpecError(f"{label} requires a 'ref' object with 'kind' and 'value'")
        _reject_unknown_fields(ref, {"kind", "value"}, f"{label}.ref")
        kind = ref.get("kind")
        if kind == "branch":
            raise SkillSpecError(
                f"{label}.ref pins a branch; skill requirements accept exact 'tag' or 'revision' refs only"
            )
        if kind not in REQUIREMENT_REF_KINDS:
            raise SkillSpecError(f"{label}.ref.kind must be 'tag' or 'revision'")
        value = ref.get("value")
        if not isinstance(value, str) or not value:
            raise SkillSpecError(f"{label}.ref.value must be a non-empty string")
        if any(marker in value for marker in _RANGE_MARKERS):
            raise SkillSpecError(
                f"{label}.ref.value {value!r} looks like a version range; "
                "skill requirements accept exact tags or revisions only"
            )

        mode = entry.get("mode", "full")
        if mode not in REQUIREMENT_MODES:
            raise SkillSpecError(f"{label}.mode must be one of full, runtime, or context")

        commands_raw = entry.get("commands")
        commands: tuple[str, ...] = ()
        if commands_raw is not None:
            if mode != "runtime":
                raise SkillSpecError(f"{label}.commands applies to runtime requirements only")
            if not isinstance(commands_raw, list) or not commands_raw:
                raise SkillSpecError(f"{label}.commands must be a non-empty list of command names")
            seen: list[str] = []
            for item in commands_raw:
                if not isinstance(item, str) or not item:
                    raise SkillSpecError(f"{label}.commands entries must be non-empty strings")
                if not is_valid_identifier(item):
                    raise SkillSpecError(f"{label}.commands entry {item!r} {IDENTIFIER_RULE}")
                if item not in seen:
                    seen.append(item)
            commands = tuple(seen)

        requirements[name] = SkillRequirement(
            name=name,
            git=git,
            ref_kind=kind,
            ref_value=value,
            mode=mode,
            commands=commands,
            source=source_file,
        )
    return requirements


def _validate_relative_path(value: Any, *, field: str, strict_posix: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise SkillSpecError(f"{field} must be a non-empty string")
    if strict_posix and ("\\" in value or "//" in value):
        raise SkillSpecError(f"{field} must be a POSIX-style relative path inside the skill repository")
    if strict_posix and any(part in {"", "."} for part in value.split("/")):
        raise SkillSpecError(f"{field} must be a POSIX-style relative path inside the skill repository")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise SkillSpecError(f"{field} must be a relative path inside the skill repository")
    if not path.parts:
        raise SkillSpecError(f"{field} must be a relative path inside the skill repository")
    if path.as_posix() != value or not is_valid_portable_path(value):
        raise SkillSpecError(f"{field} must be a portable relative path inside the skill repository")
    return value


def _parse_runtime_roots(
    raw: Any, *, snapshot: Path, source_file: str = CANONICAL_MANIFEST
) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise SkillSpecError(f"{source_file} field 'runtime_roots' must be a list")
    roots: list[str] = []
    for index, value in enumerate(raw):
        root = _validate_relative_path(value, field=f"runtime_roots[{index}]", strict_posix=True)
        root_path = snapshot / root
        if not root_path.exists():
            raise SkillSpecError(f"runtime root does not exist: {root}")
        if not root_path.is_dir():
            raise SkillSpecError(f"runtime root must be a directory: {root}")
        roots.append(root)

    if len(set(roots)) != len(roots):
        raise SkillSpecError("runtime roots must be unique after normalization")

    sorted_roots = sorted(roots)
    for left_index, left in enumerate(sorted_roots):
        for right in sorted_roots[left_index + 1 :]:
            if _path_contains(left, right) or _path_contains(right, left):
                container, contained = (left, right) if _path_contains(left, right) else (right, left)
                raise SkillSpecError(f"runtime roots must be disjoint: {container} contains {contained}")
    return tuple(roots)


def _parse_build_roots(
    raw: Any,
    *,
    snapshot: Path,
    runtime_roots: tuple[str, ...],
    source_file: str = CANONICAL_MANIFEST,
) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise SkillSpecError(f"{source_file} field 'build_roots' must be a list")
    roots: list[str] = []
    for index, value in enumerate(raw):
        field = f"build_roots[{index}]"
        root = _validate_relative_path(value, field=field, strict_posix=True)
        _validate_link_free_directory(snapshot, root, field=field, noun="build root")
        roots.append(root)

    if len(set(roots)) != len(roots):
        raise SkillSpecError("build roots must be unique after normalization")

    overlap = _overlapping_roots(roots)
    if overlap is not None:
        left, right = overlap
        raise SkillSpecError(f"build roots must be disjoint: {left} overlaps {right}")

    for build_root in roots:
        for runtime_root in runtime_roots:
            if _path_contains(build_root, runtime_root) or _path_contains(runtime_root, build_root):
                raise SkillSpecError(
                    f"build roots must not overlap runtime roots: {build_root} overlaps {runtime_root}"
                )
    return tuple(roots)


def _overlapping_roots(roots: list[str] | tuple[str, ...]) -> tuple[str, str] | None:
    sorted_roots = sorted(roots)
    for index, left in enumerate(sorted_roots):
        for right in sorted_roots[index + 1 :]:
            if _path_contains(left, right) or _path_contains(right, left):
                return left, right
    return None


def _parse_script_execution_policy(
    raw: dict[str, Any], *, schema: int, label: str
) -> tuple[str | None, str | None]:
    """Parse the schema-8 opt-in into the enforced script execution policy.

    ``execution_policy`` and ``interpreter`` are co-required. A manifest that
    declares one without the other is invalid, and a manager must not resolve
    the missing field to a default or install the command declared-only.
    """

    if schema < 8:
        return None, None
    has_policy = "execution_policy" in raw
    has_interpreter = "interpreter" in raw
    if not has_policy and not has_interpreter:
        return None, None
    if not has_policy or not has_interpreter:
        missing = "interpreter" if not has_interpreter else "execution_policy"
        raise SkillSpecError(
            f"{label} declares an execution policy without {missing!r}; "
            "the two fields are co-required"
        )
    execution_policy = raw["execution_policy"]
    interpreter = raw["interpreter"]
    if not isinstance(execution_policy, str) or execution_policy not in SCRIPT_EXECUTION_POLICIES:
        admitted = ", ".join(repr(value) for value in sorted(SCRIPT_EXECUTION_POLICIES))
        raise SkillSpecError(
            f"{label}.execution_policy must be {admitted}, got {execution_policy!r}"
        )
    if not isinstance(interpreter, str) or interpreter not in SCRIPT_INTERPRETERS:
        admitted = ", ".join(repr(value) for value in sorted(SCRIPT_INTERPRETERS))
        raise SkillSpecError(
            f"{label}.interpreter must be one of {admitted}, got {interpreter!r}"
        )
    return execution_policy, interpreter


def _parse_declared_modules(raw: Any, *, label: str) -> tuple[str, ...]:
    """Parse a present schema-8 ``modules`` member before snapshot validation.

    Absence is the default; an explicit ``null`` is not a spelling of absence
    and never reaches here.
    """

    if not isinstance(raw, list):
        raise SkillSpecError(f"{label}.modules must be a list of portable relative paths")
    modules: list[str] = []
    for index, value in enumerate(raw):
        modules.append(
            _validate_relative_path(
                value,
                field=f"{label}.modules[{index}]",
                strict_posix=True,
            )
        )
    return tuple(modules)


def _validate_build_layout(
    snapshot: Path,
    build_roots: tuple[str, ...],
    runtime_roots: tuple[str, ...],
    commands: dict[str, CommandSpec],
    *,
    schema: int,
) -> None:
    used_roots: set[str] = set()
    for name in sorted(commands):
        command = commands[name]
        if command.type != "build" or command.driver != GO_V1_DRIVER:
            continue
        source_dir = command.source_dir
        if source_dir is None:
            raise SkillSpecError(f"Command {name!r} field 'source_dir' must be a non-empty string")
        containing_roots = [root for root in build_roots if _path_contains(root, source_dir)]
        if len(containing_roots) != 1:
            raise SkillSpecError(
                f"commands.{name}.source_dir must be below exactly one build_roots entry"
            )
        build_root = containing_roots[0]
        field = f"commands.{name}.source_dir"
        _validate_link_free_directory(snapshot, source_dir, field=field, noun="source directory")
        _validate_nearest_go_module(snapshot, build_root, source_dir, field=field)
        if schema >= 8:
            try:
                validate_declaration(
                    snapshot,
                    command.modules,
                    build_root=build_root,
                    build_roots=build_roots,
                    runtime_roots=runtime_roots,
                    label=f"commands.{name}",
                )
            except ModuleRootError as exc:
                raise SkillSpecError(str(exc)) from exc
        used_roots.add(build_root)

    for index, root in enumerate(build_roots):
        if root not in used_roots:
            raise SkillSpecError(f"build_roots[{index}] build root {root!r} is not used by any build command")


def _validate_link_free_directory(snapshot: Path, rel_path: str, *, field: str, noun: str) -> None:
    current = snapshot
    for component in PurePosixPath(rel_path).parts:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise SkillSpecError(f"{field} {noun} does not exist: {rel_path}") from exc
        except OSError as exc:
            raise SkillSpecError(f"{field} cannot inspect {noun} {rel_path}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise SkillSpecError(f"{field} {noun} must be link-free: {rel_path}")
        if not stat.S_ISDIR(info.st_mode):
            raise SkillSpecError(f"{field} {noun} must be a directory: {rel_path}")


def _validate_nearest_go_module(snapshot: Path, build_root: str, source_dir: str, *, field: str) -> None:
    root_path = PurePosixPath(build_root)
    current = PurePosixPath(source_dir)
    while True:
        module_path = snapshot.joinpath(*current.parts, "go.mod")
        try:
            info = module_path.lstat()
        except FileNotFoundError:
            info = None
        except OSError as exc:
            raise SkillSpecError(f"{field} cannot inspect nearest go.mod: {exc}") from exc
        if info is not None:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise SkillSpecError(
                    f"{field} nearest go.mod must be a real regular file in build root {build_root}"
                )
            if current != root_path:
                raise SkillSpecError(
                    f"{field} intervening module {current.as_posix()}/go.mod is below build root {build_root}"
                )
            return
        if current == root_path:
            raise SkillSpecError(
                f"{field} build root {build_root} must contain the nearest go.mod directly"
            )
        current = current.parent


def _validate_v2_script_path(snapshot: Path, rel_path: str, runtime_roots: tuple[str, ...], *, field: str) -> None:
    script_path = snapshot / rel_path
    if not script_path.exists():
        raise SkillSpecError(f"{field} source file not found: {rel_path}")
    if not script_path.is_file():
        raise SkillSpecError(f"{field} must point to a file: {rel_path}")
    if runtime_roots and not any(_path_contains(root, rel_path) for root in runtime_roots):
        raise SkillSpecError(f'command path "{rel_path}" is not inside any runtime_roots')


def _path_contains(root: str, rel_path: str) -> bool:
    root_parts = PurePosixPath(root).parts
    path_parts = PurePosixPath(rel_path).parts
    return len(path_parts) >= len(root_parts) and path_parts[: len(root_parts)] == root_parts


def _reject_unknown_fields(data: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        joined = ", ".join(repr(item) for item in unknown)
        raise SkillSpecError(f"{label} has unsupported field(s): {joined}")


def enforced_script_commands(spec: SkillSpec) -> tuple[CommandSpec, ...]:
    """Return every script command selecting an unimplemented execution policy."""
    return tuple(
        command
        for _, command in sorted(spec.commands.items())
        if command.type == "script"
        and command.execution_policy is not None
        and command.execution_policy not in SCRIPT_EXECUTION_POLICIES_IMPLEMENTED
    )


def script_execution_policy_rejection(spec: SkillSpec) -> str | None:
    """Return the fail-closed diagnostic text, or ``None`` when installable."""
    unsupported = enforced_script_commands(spec)
    if not unsupported:
        return None
    listed = ", ".join(
        f"{command.name} ({command.execution_policy})" for command in unsupported
    )
    return (
        f"{SCRIPT_EXECUTION_POLICY_UNSUPPORTED}: this manager does not implement "
        f"the selected script execution policy, so it refuses to install "
        f"{listed}. The command is not downgraded to a declared-only shim."
    )
