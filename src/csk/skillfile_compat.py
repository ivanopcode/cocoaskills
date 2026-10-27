"""Input-only deprecated forms and the shared Skillfile writer boundary.

Migration commands may exempt both forms, but remove only their own form.
Other writers must call refuse_write before doing any work, not after staging.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import protocol_json

MIGRATIONS = {
    "ignore_mode": "csk ignore-mode set <exclude|gitignore>",
    "cursor": "csk mcp migrate",
}
_warned: set[str] = set()


def reset_warnings() -> None:
    _warned.clear()


def forms(data: object) -> tuple[str, ...]:
    if not isinstance(data, dict):
        return ()
    found = ["ignore_mode"] if "ignore_mode" in data else []
    servers = data.get("mcp_servers")
    if isinstance(servers, dict):
        for body in servers.values():
            if isinstance(body, dict) and isinstance(body.get("server"), dict):
                selected = body["server"].get("environments")
                if isinstance(selected, list) and "cursor" in selected:
                    found.append("cursor")
                    break
    return tuple(found)


def warn(data: object) -> None:
    for form in forms(data):
        if form not in _warned:
            print(
                f"warning: {form} is not portable: other managers reject this Skillfile; run `{MIGRATIONS[form]}` (legacy reading ends in 0.22)",
                file=sys.stderr,
            )
            _warned.add(form)


def refuse_write(data: object, *, exempt: tuple[str, ...] = ()) -> None:
    pending = [f for f in forms(data) if f not in exempt]
    if pending:
        from .sources.errors import SourceError

        raise SourceError(
            "skillfile_deprecated_form",
            "not portable: other managers reject this Skillfile; run "
            + "; ".join(MIGRATIONS[f] for f in pending),
        )


def check_path(path: Path) -> None:
    if path.exists():
        refuse_write(protocol_json.loads(path.read_bytes()))


def value_spans(text: str) -> dict[tuple[str | int, ...], tuple[int, int]]:
    """Locate JSON value tokens without reserializing surrounding bytes."""
    protocol_json.loads(
        text.encode()
    )  # Reject duplicate keys and noncanonical JSON values.
    decoder = json.JSONDecoder()
    spans: dict[tuple[str | int, ...], tuple[int, int]] = {}

    def space(i: int) -> int:
        while i < len(text) and text[i] in " \r\n\t":
            i += 1
        return i

    def walk(i: int, path: tuple[str | int, ...]) -> int:
        i = space(i)
        _, end = decoder.raw_decode(text, i)
        spans[path] = (i, end)
        if text[i] == "{":
            at = space(i + 1)
            while text[at] != "}":
                key, at = decoder.raw_decode(text, at)
                at = space(at)
                assert text[at] == ":"
                at = space(walk(at + 1, (*path, key)))
                if text[at] == "}":
                    break
                at = space(at + 1)
        elif text[i] == "[":
            at, index = space(i + 1), 0
            while text[at] != "]":
                at = space(walk(at, (*path, index)))
                index += 1
                if text[at] == "]":
                    break
                at = space(at + 1)
        return end

    walk(0, ())
    return spans


def migrate_cursor(
    raw: bytes, *, server: str | None, targets: tuple[str, ...] | None
) -> tuple[bytes, tuple[str, ...]]:
    from .mcp_declarations import CANONICAL_ENVIRONMENTS

    if targets is not None and (
        not targets
        or len(set(targets)) != len(targets)
        or any(a not in CANONICAL_ENVIRONMENTS for a in targets)
    ):
        raise ValueError(
            "mcp_declaration_invalid: --targets requires unique canonical environment ids"
        )
    data: Any = protocol_json.loads(raw)
    text = raw.decode("utf-8")
    spans = value_spans(text)
    edits = []
    names = []
    for name, body in data.get("mcp_servers", {}).items():
        if server is not None and name != server:
            continue
        selected = (
            body.get("server", {}).get("environments")
            if isinstance(body, dict)
            else None
        )
        if not isinstance(selected, list) or "cursor" not in selected:
            continue
        if len(selected) == 1 and targets is None:
            raise ValueError(
                "skillfile_deprecated_form: fully local declarations are not supported before 0.22; choose csk mcp migrate --targets claude_code,..."
            )
        path = ("mcp_servers", name, "server", "environments")
        start, end = spans[path]
        if len(selected) == 1:
            edits.append((start, end, json.dumps(targets)))
        else:
            index = selected.index("cursor")
            first, last = spans[(*path, index)]
            if index == len(selected) - 1:
                first = spans[(*path, index - 1)][1]
            else:
                last = spans[(*path, index + 1)][0]
            edits.append((first, last, ""))
        names.append(name)
    if server is not None and not names:
        raise ValueError(
            "skillfile_deprecated_form: selected server has no deprecated cursor form"
        )
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    return text.encode("utf-8"), tuple(names)
