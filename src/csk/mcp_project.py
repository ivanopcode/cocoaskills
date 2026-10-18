"""Project MCP planning, consent presentation and transaction inputs.

Planning reads native surfaces and protected manager state without modifying
anything. Native bytes never enter reports: only declarations and action rows
are displayed. The installer publishes staged outputs with the skill targets.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from . import locking, mcp_configs, protocol_json, mcp_declarations as declarations
from . import mcp_native as native, mcp_ownership as ownership, transactions
from .skillspec import McpServerRequirement

AGENTS = ("claude_code", "codex_cli", "cursor")
_ACTIONS = {
    "add": "added",
    "replace": "updated",
    "keep": "unchanged",
    "restore": "restored",
    "remove": "removed",
    "skip_drift": "skipped: drift",
    "release": "released",
    "forget": "removed",
}


class McpProjectError(ValueError):
    """A sanitized refusal with an operator remedy for each blocked contract."""


def refusal(code: str, detail: str) -> McpProjectError:
    remedies = {
        "mcp_plan_unapproved": "run csk install in a terminal to review and approve.",
        "mcp_entry_conflict": "remove the declaration or move the existing native entry before retrying.",
        "mcp_ownership_unknown": "repair the protected manager MCP state before retrying.",
        "mcp_native_config_unreadable": "make the native path a readable regular file without links.",
        "mcp_native_config_unsupported_syntax": "repair the native config syntax before retrying.",
        "mcp_native_config_too_large": "reduce the native config below 4 MiB before retrying.",
        "mcp_windows_batch_unsupported": "select a native executable instead of a batch launcher.",
    }
    return McpProjectError(
        f"{code}: {detail}\nRemedy: {remedies.get(code, 'correct the MCP declaration and retry.')}"
    )


def refused_report(
    plan: ProjectPlan | None,
    error: BaseException,
    *,
    dry_run: bool = False,
    detail: str | None = None,
) -> str:
    text = str(error) if detail is None else detail
    if any(line.startswith("mcp ") for line in text.splitlines()):
        result = text
    else:
        code = getattr(error, "code", None)
        if not isinstance(code, str):
            match = re.search(r"\bmcp_[a-z_]+", text)
            code = match.group() if match else "mcp_declaration_invalid"
        rows = plan.rows if plan is not None else ()
        result = "\n".join(
            [
                *(
                    f"mcp {r.server} -> {r.agent} {r.path} (refused: {code})"
                    for r in rows
                ),
                text,
            ]
        )
    if dry_run:
        result = "\n".join(
            "would: " + line if line.startswith("mcp ") else line
            for line in result.splitlines()
        )
    return result


def _raw_refusal_rows(
    raw: object,
    agents: Sequence[str],
    code: str,
    owned: Sequence[ownership.EntryAddress] = (),
) -> list[str]:
    pairs = {(address.name, address.agent, address.relpath) for address in owned}
    if isinstance(raw, dict):
        for name in sorted(
            k
            for k in raw
            if isinstance(k, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", k)
        ):
            body = raw[name]
            selected = agents
            if isinstance(body, dict) and isinstance(body.get("server"), dict):
                environments = body["server"].get("environments")
                if isinstance(environments, list) and all(
                    isinstance(a, str) for a in environments
                ):
                    selected = environments
            for agent in AGENTS:
                if agent in selected and agent in agents:
                    pairs.add((name, agent, native.project_surface(agent).relpath))
    return [
        f"mcp {name} -> {agent} {path} (refused: {code})"
        for name, agent, path in sorted(pairs, key=lambda p: (p[0], AGENTS.index(p[1])))
    ]


@dataclass(frozen=True)
class SurfaceRead:
    surface: native.NativeSurface
    read: native.NativeRead
    raw: bytes | None
    transaction_digest: str | None
    mode: int


@dataclass(frozen=True)
class ReportRow:
    server: str
    agent: str
    path: str
    action: str

    def render(self, *, dry_run: bool = False) -> str:
        prefix = "would: " if dry_run else ""
        return f"{prefix}mcp {self.server} -> {self.agent} {self.path} ({self.action})"


@dataclass(frozen=True)
class ProjectPlan:
    root: Path
    home: Path
    manifest_sha256: str
    consumer: str
    declarations: Mapping[str, declarations.McpDeclaration]
    desired: tuple[ownership.Desired, ...]
    surfaces: Mapping[str, SurfaceRead]
    observed: Mapping[ownership.EntryAddress, ownership.Observed]
    ledger: ownership.Ledger | ownership.LedgerAbsent
    reconciliation: ownership.ReconcilePlan
    subjects: tuple[ownership.ConsentSubject, ...]
    pending: tuple[ownership.ConsentSubject, ...]
    rows: tuple[ReportRow, ...]
    edits: Mapping[str, bytes | None]
    created_files: frozenset[tuple[str, str]]
    warnings: tuple[str, ...]

    def report(self, *, dry_run: bool = False) -> list[str]:
        prefix = "would: " if dry_run else ""
        lines = [row.render(dry_run=dry_run) for row in self.rows]
        if dry_run:
            pending = {s.name for s in self.pending}
            for name, decl in sorted(self.declarations.items()):
                approval = (
                    ("required" if name in pending else "approved")
                    if declarations.authority(decl).consent_required
                    else "not-required"
                )
                lines.append(f"would: mcp {name} approval: {approval}")
        lines.extend(f"{prefix}{warning}" for warning in self.warnings)
        if self.rows:
            lines.append(
                f"{prefix}MCP configuration installed. Authentication not checked."
            )
            written = {
                a.address.agent
                for a in self.reconciliation.actions
                if a.kind in ("add", "restore", "replace", "remove")
            }
            for agent in AGENTS:
                if agent not in written:
                    continue
                if agent == "claude_code":
                    lines.append(
                        f"{prefix}run /mcp in Claude Code to review and sign in"
                    )
                elif agent == "cursor":
                    lines.append(f"{prefix}open Cursor Settings > MCP")
                else:
                    for name, decl in sorted(self.declarations.items()):
                        if agent in decl.agents and decl.auth.kind == "oauth":
                            lines.append(f"{prefix}codex mcp login {name}")
        return lines


def _read(root: Path, surface: native.NativeSurface) -> SurfaceRead:
    read, raw = native.read_surface(root, surface)
    digest: str | None = None
    mode = 0o600
    if read.state in ("present", "absent"):
        digest = transactions.digest_target(root / surface.relpath)
        checked, _ = native.read_surface(root, surface)
        if checked != read:
            raise refusal(
                "mcp_native_config_unreadable",
                f"{surface.relpath} changed during planning",
            )
        if read.state == "present":
            mode = stat.S_IMODE((root / surface.relpath).stat().st_mode)
    return SurfaceRead(surface, read, raw, digest, mode)


def plan_project(
    root: Path, target_agents: Sequence[str], raw: object, home: Path
) -> ProjectPlan:
    """Return a typed, entirely pre-write plan for one project consumer."""
    owned: list[ownership.EntryAddress] = []
    try:
        return _plan_project(root, target_agents, raw, home, owned)
    except McpProjectError as exc:
        if any(line.startswith("mcp ") for line in str(exc).splitlines()):
            raise
        match = re.search(r"\bmcp_[a-z_]+", str(exc))
        code = match.group() if match else "mcp_declaration_invalid"
        raise McpProjectError(
            "\n".join([*_raw_refusal_rows(raw, target_agents, code, owned), str(exc)])
        ) from exc


@dataclass(frozen=True)
class _EntryRead:
    consumer: str
    ledger: ownership.Ledger | ownership.LedgerAbsent
    desired: tuple[ownership.Desired, ...]
    values: Mapping[ownership.EntryAddress, Mapping[str, object]]
    surfaces: Mapping[str, SurfaceRead]
    observed: Mapping[ownership.EntryAddress, ownership.Observed]


def _read_ledger(
    root: Path, home: Path
) -> tuple[Path, Path, str, ownership.Ledger | ownership.LedgerAbsent]:
    root = root.resolve()
    home = home.resolve()
    if home.is_relative_to(root):
        raise refusal(
            "mcp_ownership_unknown", "manager home must be outside the project"
        )
    consumer = ownership.consumer_id(
        locking.canonical_project_identity(root), "project"
    )
    ledger = ownership.load_ledger(home, consumer)
    if isinstance(ledger, ownership.LedgerUnknown):
        raise refusal("mcp_ownership_unknown", "protected ledger cannot be verified")
    if isinstance(ledger, ownership.Ledger):
        for entry in ledger.entries:
            if entry.address.agent not in AGENTS:
                raise refusal("mcp_ownership_unknown", "ledger agent is unsupported")
            surface = native.project_surface(entry.address.agent)
            if entry.capability != surface.capability:
                raise refusal(
                    "mcp_ownership_unknown", "ledger native profile is unsupported"
                )
            if (
                entry.address.relpath != surface.relpath
                or entry.address.container != surface.container
            ):
                raise refusal(
                    "mcp_ownership_unknown",
                    "ledger address is not a supported native surface",
                )
    return root, home, consumer, ledger


def _read_declarations(
    root: Path, target_agents: Sequence[str], raw: object
) -> tuple[str, dict[str, declarations.McpDeclaration]]:
    manifest_bytes = (root / "Skillfile.json").read_bytes()
    current = protocol_json.loads(manifest_bytes)
    if (
        not isinstance(current, dict)
        or current.get("mcp_servers", {}) != raw
        or (current.get("agents") and current["agents"] != list(target_agents))
    ):
        raise refusal(
            "mcp_declaration_invalid", "Skillfile changed before MCP planning"
        )
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    try:
        parsed = declarations.parse_mcp_servers(
            raw, invocation_scope="project", target_agents=target_agents
        )
    except declarations.McpDeclarationError as exc:
        raise refusal(exc.code, str(exc)) from exc
    return manifest_sha256, parsed


def _read_entries(
    root: Path,
    target_agents: Sequence[str],
    parsed: Mapping[str, declarations.McpDeclaration],
    consumer: str,
    ledger: ownership.Ledger | ownership.LedgerAbsent,
) -> _EntryRead:
    desired: list[ownership.Desired] = []
    values: dict[ownership.EntryAddress, Mapping[str, object]] = {}
    for decl in parsed.values():
        for agent in decl.agents:
            surface = native.project_surface(agent)
            address = ownership.EntryAddress(
                agent, surface.relpath, surface.container, decl.name
            )
            value = declarations.render_native_entry(agent, decl)
            values[address] = value
            entry_sha = declarations.entry_digest(value)
            assert entry_sha is not None
            desired.append(
                ownership.Desired(
                    address,
                    entry_sha,
                    declarations.declaration_digest(decl),
                    surface.capability,
                )
            )
    addresses = set(values)
    if isinstance(ledger, ownership.Ledger):
        addresses.update(e.address for e in ledger.entries)
    surfaces = {
        agent: _read(root, native.project_surface(agent))
        for agent in set(target_agents) & set(AGENTS)
    }
    for address in addresses:
        surface = native.project_surface(address.agent)
        if address.agent not in surfaces:
            surfaces[address.agent] = _read(root, surface)
    observed: dict[ownership.EntryAddress, ownership.Observed] = {}
    for address in addresses:
        read = surfaces[address.agent].read
        state: ownership.ObservedState
        digest = None
        if read.state == "absent":
            state = "surface_absent"
        elif read.state in ("malformed", "unreadable"):
            state = read.state
        elif address.name not in read.entries:
            state = "absent"
        else:
            state = "present"
            try:
                observed_value = read.entries[address.name]
                digest = declarations.entry_digest(observed_value)
            except (ValueError, TypeError):
                pass  # An unrepresentable foreign value is still present, never absent.
        observed[address] = ownership.Observed(address, state, digest)
    return _EntryRead(consumer, ledger, tuple(desired), values, surfaces, observed)


def _plan_project(
    root: Path,
    target_agents: Sequence[str],
    raw: object,
    home: Path,
    owned: list[ownership.EntryAddress],
) -> ProjectPlan:
    root, home, consumer, ledger = _read_ledger(root, home)
    if isinstance(ledger, ownership.Ledger):
        # Keep verified owned pairs even when declaration validation or native edits fail.
        owned.extend(entry.address for entry in ledger.entries)
    manifest_sha256, parsed = _read_declarations(root, target_agents, raw)
    if sys.platform == "win32":
        for decl in parsed.values():
            if decl.server.transport == "stdio":
                command = decl.server.command or ""
                resolved = shutil.which(command)
                if (
                    command.lower() in ("npx", "npm", "pnpm", "yarn")
                    or command.lower().endswith((".cmd", ".bat"))
                    or (resolved and resolved.lower().endswith((".cmd", ".bat")))
                ):
                    raise refusal(
                        "mcp_windows_batch_unsupported",
                        f"{decl.name} uses a batch launcher",
                    )
    entry_read = _read_entries(root, target_agents, parsed, consumer, ledger)
    consumer, ledger = entry_read.consumer, entry_read.ledger
    desired, values = entry_read.desired, entry_read.values
    surfaces, observed = entry_read.surfaces, entry_read.observed
    addresses = set(observed)
    reconciliation = ownership.reconcile(desired, observed, ledger)
    errors = []
    if reconciliation.refusals:
        refused_by_address = {
            r.address: (surfaces[r.address.agent].read.reason or r.code)
            if surfaces[r.address.agent].read.state == "malformed"
            else r.code
            for r in reconciliation.refusals
        }
        blocking_code = refused_by_address[reconciliation.refusals[0].address]
        for address in sorted(addresses, key=lambda a: (a.name, AGENTS.index(a.agent))):
            code = refused_by_address.get(address, blocking_code)
            report_action = (
                "conflict: foreign entry"
                if code == "mcp_entry_conflict" and address in refused_by_address
                else f"refused: {code}"
            )
            errors.append(
                f"mcp {address.name} -> {address.agent} {address.relpath} ({report_action})"
            )
    for item in reconciliation.refusals:
        read = surfaces[item.address.agent].read
        code = (read.reason if read.state == "malformed" else item.code) or item.code
        errors.append(
            str(
                refusal(
                    code,
                    f"{item.address.name} -> {item.address.agent} {item.address.relpath}",
                )
            )
        )
    if errors:
        raise McpProjectError("\n".join(errors))
    edits: dict[str, bytes | None] = {}
    created: set[tuple[str, str]] = set()
    created_before = (
        {(e.address.agent, e.address.relpath) for e in ledger.entries if e.created_file}
        if isinstance(ledger, ownership.Ledger)
        else set()
    )
    for action in reconciliation.actions:
        if action.kind not in ("add", "restore", "replace", "remove"):
            continue
        address = action.address
        snapshot = surfaces[address.agent]
        before = edits.get(address.agent, snapshot.raw)
        if action.kind == "remove":
            op: native.AddEntry | native.ReplaceEntry | native.RemoveEntry = (
                native.RemoveEntry(address.name)
            )
        elif action.kind == "replace":
            op = native.ReplaceEntry(address.name, values[address])
        else:
            op = native.AddEntry(address.name, values[address])
        try:
            edits[address.agent] = native.edit_surface_bytes(
                snapshot.surface, before, op
            )
        except native.McpNativeError as exc:
            raise refusal(exc.code, snapshot.surface.relpath) from exc
        if snapshot.read.state == "absent":
            created.add((address.agent, address.relpath))
    for agent, data in list(edits.items()):
        surface = surfaces[agent].surface
        if (
            data is not None
            and (agent, surface.relpath) in created_before
            and native.is_effectively_empty(surface, data)
        ):
            edits[agent] = None
    subjects = []
    for decl in parsed.values():
        if declarations.authority(decl).consent_required and any(
            a.address.name == decl.name and a.kind in ("add", "restore", "replace")
            for a in reconciliation.actions
        ):
            subjects.append(
                _consent_subject(decl)
            )
    try:
        pending = (
            ownership.unapproved(
                consumer, subjects, ownership.load_consent(home, consumer)
            )
            if subjects
            else ()
        )
    except ownership.McpOwnershipError as exc:
        raise refusal(exc.code, "protected consent store cannot be verified") from exc
    rows = tuple(
        ReportRow(a.address.name, a.address.agent, a.address.relpath, _ACTIONS[a.kind])
        for a in sorted(
            reconciliation.actions,
            key=lambda a: (a.address.name, AGENTS.index(a.address.agent)),
        )
        if a.kind != "forget"
    )
    warnings = (
        [
            f"mcp_capability_unsupported: {agent}"
            for agent in sorted(set(target_agents) - set(AGENTS))
        ]
        if parsed
        else []
    )
    warnings.extend(
        f"{w.code}: {w.address.name} -> {w.address.agent}; {w.detail}. Remove the declaration, or delete the native entry so csk restores it."
        for w in reconciliation.warnings
    )
    return ProjectPlan(
        root,
        home,
        manifest_sha256,
        consumer,
        parsed,
        tuple(desired),
        surfaces,
        observed,
        ledger,
        reconciliation,
        tuple(subjects),
        pending,
        rows,
        edits,
        frozenset(created),
        tuple(warnings),
    )


def _consent_subject(decl: declarations.McpDeclaration) -> ownership.ConsentSubject:
    return ownership.ConsentSubject(
        decl.name,
        decl.agents,
        "project",
        declarations.declaration_digest(decl),
        tuple(native.project_surface(agent).capability for agent in decl.agents),
    )


def status_project(
    root: Path, target_agents: Sequence[str], raw: object, home: Path
) -> tuple[ReportRow, ...]:
    """Observe every desired/owned pair without planning edits or recording consent."""
    root, home, consumer, ledger = _read_ledger(root, home)
    _, parsed = _read_declarations(root, target_agents, raw)
    read = _read_entries(root, target_agents, parsed, consumer, ledger)
    reconciliation = ownership.reconcile(read.desired, read.observed, read.ledger)
    wanted = {item.address for item in read.desired}
    actions = {item.address: item.kind for item in reconciliation.actions}
    conflicts = {
        item.address for item in reconciliation.refusals
        if item.code == "mcp_entry_conflict"
    }
    subjects = tuple(
        _consent_subject(decl)
        for decl in parsed.values()
        if declarations.authority(decl).consent_required
    )
    try:
        pending = {
            subject.name
            for subject in ownership.unapproved(
                read.consumer, subjects, ownership.load_consent(home, read.consumer)
            )
        }
    except ownership.McpOwnershipError as exc:
        raise refusal(exc.code, "protected consent store cannot be verified") from exc
    rows = []
    # Precedence: unreadable > orphaned > conflict/drift > unapproved > outdated > missing > current.
    # Ownership problems stay visible even when a declaration also lacks consent.
    ordered = sorted(
        read.observed.items(), key=lambda item: (item[0].name, AGENTS.index(item[0].agent))
    )
    for address, seen in ordered:
        if seen.state in ("unreadable", "malformed"):
            state = "unreadable"
        elif address not in wanted:
            state = "orphaned"
        elif address in conflicts:
            state = "conflict"
        elif actions.get(address) == "skip_drift":
            state = "drift"
        elif address.name in pending:
            state = "unapproved"
        elif actions.get(address) == "replace":
            state = "outdated"
        elif seen.state != "present":
            state = "missing"
        else:
            state = "current"
        rows.append(ReportRow(address.name, address.agent, address.relpath, state))
    return tuple(rows)


def review_consent(
    plan: ProjectPlan, *, interactive: bool, dry_run: bool, project: str
) -> None:
    """Only an affirmative terminal response records operator consent."""
    if dry_run:
        return
    for name, decl in sorted(plan.declarations.items()):
        server = decl.server
        intent = (
            f"{server.command} {' '.join(server.args)}"
            if server.transport == "stdio"
            else server.url
        )
        env = sorted(set(server.env_names) | {header.env for header in server.headers})
        print(
            f"MCP plan {name}: {server.transport} {intent}; agents={','.join(decl.agents)}; env={','.join(env) or 'none'}; auth={decl.auth.kind}"
        )
    if not plan.pending:
        return
    if not interactive or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise refusal("mcp_plan_unapproved", "operator approval is required")
    try:
        answer = input(
            f"Approve {len(plan.pending)} MCP server(s) for {project}? [y/N] "
        )
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        raise refusal("mcp_plan_unapproved", "operator did not approve")
    ownership.record_consent(
        plan.home,
        plan.consumer,
        [ownership.consent_key(plan.consumer, subject) for subject in plan.pending],
    )


def assert_current(plan: ProjectPlan) -> None:
    """Compare whole-file bytes and permissions, never only the owned subtree."""
    if (
        hashlib.sha256((plan.root / "Skillfile.json").read_bytes()).hexdigest()
        != plan.manifest_sha256
    ):
        raise refusal("mcp_declaration_invalid", "Skillfile changed since MCP planning")
    for agent in plan.edits:
        snapshot = plan.surfaces[agent]
        current, _ = native.read_surface(plan.root, snapshot.surface)
        if (
            current != snapshot.read
            or transactions.digest_target(plan.root / snapshot.surface.relpath)
            != snapshot.transaction_digest
        ):
            raise refusal(
                "mcp_native_config_unreadable",
                f"{snapshot.surface.relpath} changed since planning",
            )
    if ownership.load_ledger(plan.home, plan.consumer) != plan.ledger:
        raise refusal(
            "mcp_ownership_unknown", "protected ledger changed since planning"
        )


def check_requirements(
    plan: ProjectPlan,
    requirements: Mapping[str, Mapping[str, McpServerRequirement]],
    agents: Sequence[str],
) -> declarations.RequirementReport:
    """Check planned entries together with external native configuration."""
    if not any(requirements.values()):
        return declarations.RequirementReport({}, [])
    view: list[declarations.EffectiveServer] = []
    commands: dict[tuple[str, str], str] = {}
    project_names: set[tuple[str, str]] = set()
    disabled = mcp_configs._claude_disabled_servers(plan.root)
    for agent in agents:
        snapshot = plan.surfaces.get(agent)
        if snapshot is not None:
            data = plan.edits.get(agent, snapshot.raw)
            entries = (
                native.parse_surface_bytes(snapshot.surface, data).entries
                if data is not None
                else {}
            )
        else:
            entries = mcp_configs._project_entries(plan.root, agent)
        external = mcp_configs._home_entries(agent)
        for name, value in {**external, **entries}.items():
            entry = value if isinstance(value, Mapping) else {}
            state: Literal["active", "disabled", "rejected"] = "active"
            if entry.get("enabled") is False:
                state = "disabled"
            if agent == "claude_code" and name in disabled:
                state = "rejected"
            transport: Literal["stdio", "http", "sse", "unknown"] = "unknown"
            command = mcp_configs._stdio_command(entry)
            if command is not None:
                transport = "stdio"
                commands[(agent, name)] = command
            elif isinstance(entry.get("url"), str) or isinstance(
                entry.get("serverUrl"), str
            ):
                transport = "sse" if entry.get("type") == "sse" else "http"
            managed = any(
                d.address.agent == agent and d.address.name == name
                for d in plan.desired
            )
            view.append(
                declarations.EffectiveServer(
                    name, agent, "planned" if managed else "external", transport, state
                )
            )
            if name in entries and name not in external:
                project_names.add((agent, name))
    report = declarations.check_requirements(requirements, agents, view)
    warnings = []
    for found in report.found.values():
        for name, available in found.items():
            for agent in available:
                command = commands.get((agent, name))
                if command and shutil.which(command) is None:
                    warnings.append(
                        f"MCP server {name!r} for {agent} runs {command!r}, which is not on PATH"
                    )
            gated = [agent for agent in available if (agent, name) in project_names]
            if gated:
                warnings.append(
                    f"MCP server {name!r} is declared only in project-level config for {', '.join(gated)}; agents keep project servers pending until the checkout is trusted"
                )
    return replace(report, warnings=warnings)


def stage_targets(
    plan: ProjectPlan, staging_root: Path, transaction_id: str
) -> tuple[transactions.MutableTarget, ...]:
    """Stage private native bytes and the next protected ledger for one commit."""
    assert_current(plan)
    targets = []
    for agent, data in sorted(plan.edits.items()):
        snapshot = plan.surfaces[agent]
        desired = None
        if data is not None:
            desired = staging_root / f"mcp-{agent}"
            desired.write_bytes(data)
            desired.chmod(snapshot.mode)
        assert snapshot.transaction_digest is not None
        targets.append(
            transactions.MutableTarget(
                target_class="mcp-native",
                identifier=agent,
                live_path=plan.root / snapshot.surface.relpath,
                desired_path=desired,
                expected_preimage_digest=snapshot.transaction_digest,
            )
        )
    created = set(plan.created_files)
    if isinstance(plan.ledger, ownership.Ledger):
        created.update(
            (e.address.agent, e.address.relpath)
            for e in plan.ledger.entries
            if e.created_file
        )
    updated = ownership.next_ledger(
        plan.consumer,
        "project",
        plan.ledger,
        plan.reconciliation,
        plan.desired,
        transaction_id,
        created,
    )
    if updated.entries or isinstance(plan.ledger, ownership.Ledger):
        data = ownership.encode_ledger(updated)
        if not isinstance(
            plan.ledger, ownership.Ledger
        ) or data != ownership.encode_ledger(plan.ledger):
            ownership._ensure_private_directory(plan.home)
            path = ownership.ledger_path(plan.home, plan.consumer)
            desired = staging_root / "mcp-ledger"
            desired.write_bytes(data)
            desired.chmod(0o600)
            targets.append(
                transactions.MutableTarget(
                    target_class="mcp-ledger",
                    identifier=plan.consumer,
                    live_path=path,
                    desired_path=desired,
                    expected_preimage_digest=transactions.digest_target(path),
                )
            )
    return tuple(targets)
