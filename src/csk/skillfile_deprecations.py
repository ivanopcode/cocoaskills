"""Input-only legacy forms shared by the two explicit Skillfile migrations."""
from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

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
    if migration in {'ignore_mode', 'cursor'}:
        return
    if forms(data):
        raise SourceError('skillfile_deprecated_form', '; '.join(hints(data)))
