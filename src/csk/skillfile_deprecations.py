"""Input-only legacy forms shared by the two explicit Skillfile migrations."""
from __future__ import annotations

import sys
import json
from pathlib import Path
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from . import protocol_json
from .sources.errors import SourceError

IGNORE_MIGRATION = 'csk ignore-mode set exclude'
CURSOR_MIGRATION = 'csk mcp migrate'
_warned: ContextVar[set[str] | None] = ContextVar('skillfile_deprecation_warnings', default=None)


@contextmanager
def command_warnings() -> Iterator[None]:
    """Deduplicate internal reloads while keeping separate CLI invocations independent."""
    token = _warned.set(set())
    try:
        yield
    finally:
        _warned.reset(token)


def forms(data: Mapping[str, Any]) -> dict[str, str]:
    found = {}
    if not isinstance(data, Mapping):
        return found
    if 'ignore_mode' in data:
        found['ignore_mode'] = IGNORE_MIGRATION
    servers = data.get('mcp_servers')
    if isinstance(servers, dict):
        for declaration in servers.values():
            if not isinstance(declaration, dict):
                continue
            server = declaration.get('server')
            if isinstance(server, dict) and isinstance(server.get('environments'), list) and 'cursor' in server['environments']:
                found['cursor'] = CURSOR_MIGRATION
    return found


def hints(data: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(f'{name} is not portable: other managers reject this Skillfile; run `{command}`'
                  + (' (or `csk ignore-mode set gitignore`)' if name == 'ignore_mode' else '')
                 for name, command in forms(data).items())


def warn(data: Mapping[str, Any]) -> None:
    seen = _warned.get()
    for name, message in zip(forms(data), hints(data)):
        if seen is None or name not in seen:
            print('warning: ' + message, file=sys.stderr)
            if seen is not None:
                seen.add(name)


def require_writable(data: Mapping[str, Any], *, migration: str | None = None) -> None:
    """Ordinary writers refuse; a migration removes only its own form.

    Explicit migrations may preserve the other form byte-for-byte (never
    serialize it), so two legacy forms cannot deadlock each other's migration.
    """
    from . import mcp_declarations

    if isinstance(data, Mapping):
        mcp_declarations.validate_environment_selectors(
            data.get('mcp_servers', {}), allow_deprecated=True
        )
    if migration in {'ignore_mode', 'cursor'}:
        return
    if forms(data):
        raise refusal('; '.join(hints(data)))


def refusal(detail: str) -> SourceError:
    """Construct the shared deprecated-form diagnostic for writers and migrations."""
    return SourceError("skillfile_deprecated_form", detail)


def check_path(path: Path) -> None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    except OSError as exc:
        from .manifest import ManifestError

        raise ManifestError(f"Cannot read Skillfile at {path}: {exc}") from exc
    require_writable(protocol_json.loads(raw))


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
    require_writable(data, migration="cursor")
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
            raise refusal(
                "fully local declarations are not supported before 0.22; choose csk mcp migrate --targets claude_code,..."
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
        raise refusal(
            "selected server has no deprecated cursor form"
        )
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    return text.encode("utf-8"), tuple(names)
