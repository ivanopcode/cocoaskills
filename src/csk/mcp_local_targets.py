"""Project-bound operator configuration and independent Cursor approvals.

This file is manager-local state, never a shared Skillfile extension or a
canonical MCP carrier. No operation starts a declared command.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from collections.abc import Mapping
import re

from . import locking, protocol_json, mcp_declarations as d, mcp_ownership as o


def consumer(root: Path) -> str:
    return o.consumer_id(locking.canonical_project_identity(root), "project")


def path(root: Path, home: Path) -> Path:
    return home / "state" / "mcp" / "local-targets" / (consumer(root) + ".json")


def read(root: Path, home: Path) -> dict[str, dict[str, str]]:
    try:
        raw = o._read_store(home, "local-targets", consumer(root))
        if raw is None:
            return {}
        data = protocol_json.loads(raw)
        if (
            not isinstance(data, dict)
            or set(data) != {"schema_version", "consumer", "targets"}
            or type(data["schema_version"]) is not int
            or data["schema_version"] != 1
            or data["consumer"] != consumer(root)
        ):
            raise ValueError()
        targets = data["targets"]
        if not isinstance(targets, dict):
            raise ValueError()
        for target, grants in targets.items():
            if target != "*" and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", target
            ):
                raise ValueError()
            if not isinstance(grants, dict):
                raise ValueError()
            for name, key in grants.items():
                if (
                    not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", name)
                    or not isinstance(key, str)
                    or not re.fullmatch("[0-9a-f]{64}", key)
                ):
                    raise ValueError()
        return targets
    except (
        ValueError,
        TypeError,
        o.McpOwnershipError,
        protocol_json.ProtocolJSONError,
    ):
        from .mcp_project import refusal

        raise refusal(
            "mcp_local_target_invalid",
            "protected local-target configuration is invalid",
        ) from None


def encode(root: Path, targets: Mapping[str, Mapping[str, str]]) -> bytes:
    return (
        protocol_json.canonical_bytes(
            {"schema_version": 1, "consumer": consumer(root), "targets": dict(targets)}
        )
        + b"\n"
    )


def ensure_directory(home: Path) -> None:
    o._ensure_private_directory(home)
    o._ensure_component(home / "state/mcp/local-targets", "local-targets", owned=True)


def selected_target(
    decl: d.McpDeclaration, targets: Mapping[str, Mapping[str, str]]
) -> str | None:
    if decl.environments is None:
        return None  # Absence already selects every writer; local settings apply only to explicit selectors.
    return decl.name if decl.name in targets else "*" if "*" in targets else None


def apply(
    root: Path, home: Path, parsed: Mapping[str, d.McpDeclaration]
) -> dict[str, d.McpDeclaration]:
    targets = read(root, home)
    result = {}
    for name, decl in parsed.items():
        selected = selected_target(decl, targets)
        if selected is not None and "cursor" not in decl.agents:
            decl = replace(
                decl,
                agents=tuple(dict.fromkeys((*decl.agents, "cursor"))),
                local_agents=("cursor",),
            )
        result[name] = decl
    return result


def subject(decl: d.McpDeclaration, target: str) -> o.ConsentSubject:
    from . import mcp_project, mcp_native

    return replace(
        mcp_project._consent_subject(decl),
        agents=("cursor",),
        scope="project-local:" + target,
        capabilities=(mcp_native.project_surface("cursor").capability,),
    )


def pending(
    root: Path, home: Path, parsed: Mapping[str, d.McpDeclaration]
) -> tuple[o.ConsentSubject, ...]:
    targets = read(root, home)
    result = []
    for decl in parsed.values():
        target = selected_target(decl, targets)
        if target is not None:
            approval = subject(decl, target)
            if o.consent_key(consumer(root), approval) != targets[target].get(
                decl.name
            ):
                result.append(approval)
    return tuple(result)


def approved_payload(
    root: Path, home: Path, subjects: tuple[o.ConsentSubject, ...]
) -> bytes | None:
    local = [s for s in subjects if s.scope.startswith("project-local:")]
    if not local:
        return None
    targets = read(root, home)
    for approval in local:
        target = approval.scope.removeprefix("project-local:")
        targets[target][approval.name] = o.consent_key(consumer(root), approval)
    return encode(root, targets)
