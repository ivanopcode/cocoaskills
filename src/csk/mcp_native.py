"""Native MCP config surfaces: typed reads and lossless span edits.

Slice-1 pure-bytes seam for the three project surfaces (``.mcp.json``,
``.cursor/mcp.json``, ``.codex/config.toml``). Reads are typed
(absent/present/unreadable/malformed) and never follow links. Edits change
one entry under one container key as a single span over the existing bytes,
with an enforced semantic (P1) and byte (P2) postcondition. Nothing calls
this module yet; leaf M3b wires it in.

TOML standalone comment lines are retained. Trailing inline comments on
removed key/value lines go with those lines. Container nesting is bounded
at 256 levels, counting the root document as level one.
"""

from __future__ import annotations

import errno
import hashlib
import math
import os
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple

from . import _json_span, _toml_span, protocol_json
from .sources import _selection_fs

__all__ = [
    "AddEntry",
    "McpNativeError",
    "NativeRead",
    "NativeSurface",
    "RemoveEntry",
    "ReplaceEntry",
    "edit_surface_bytes",
    "is_effectively_empty",
    "parse_surface_bytes",
    "project_surface",
    "read_surface",
]


class McpNativeError(ValueError):
    """Native config refusal with a stable code and surface relpath.

    ``code`` is one of the A8 refusal codes; ``surface`` is the affected
    surface relpath, or "" when no surface applies (for example an unknown
    agent name). The message carries only the code and surface, never file
    content.
    """

    def __init__(self, code: str, surface: str = "") -> None:
        self.code = code
        self.surface = surface
        super().__init__(f"{code}: {surface}" if surface else code)


@dataclass(frozen=True)
class NativeSurface:
    """One native file surface: agent, scope, path, syntax and container."""

    agent: str
    scope: Literal["project"]
    relpath: str
    syntax: Literal["json", "toml"]
    container: str
    capability: str


@dataclass(frozen=True)
class NativeRead:
    """One typed read: state, entries, content hash and sanitized reason."""

    state: Literal["absent", "present", "unreadable", "malformed"]
    entries: Mapping[str, object]
    raw_sha256: str | None
    reason: str | None


@dataclass(frozen=True)
class AddEntry:
    """Add one entry that must be absent."""

    name: str
    value: Mapping[str, object]


@dataclass(frozen=True)
class ReplaceEntry:
    """Replace one entry that must be present."""

    name: str
    value: Mapping[str, object]


@dataclass(frozen=True)
class RemoveEntry:
    """Remove one entry that must be present."""

    name: str


_SURFACES: Final = {
    "claude_code": NativeSurface(
        agent="claude_code",
        scope="project",
        relpath=".mcp.json",
        syntax="json",
        container="mcpServers",
        capability="claude_code/project/v1",
    ),
    "cursor": NativeSurface(
        agent="cursor",
        scope="project",
        relpath=".cursor/mcp.json",
        syntax="json",
        container="mcpServers",
        capability="cursor/project/v1",
    ),
    "codex_cli": NativeSurface(
        agent="codex_cli",
        scope="project",
        relpath=".codex/config.toml",
        syntax="toml",
        container="mcp_servers",
        capability="codex_cli/project/v1",
    ),
}

_MAX_SURFACE_BYTES: Final = 4 * 1024 * 1024
_MAX_DEPTH: Final = 256
_UTF8_BOM: Final = b"\xef\xbb\xbf"
_REPARSE_POINT: Final = 0x400
_READ_CHUNK: Final = 64 * 1024

_TOO_LARGE: Final = "mcp_native_config_too_large"
_UNSUPPORTED_SYNTAX: Final = "mcp_native_config_unsupported_syntax"

_BARE_KEY: Final = re.compile(r"[A-Za-z0-9_-]+")


class _SpanEdit(NamedTuple):
    new_text: str
    span_start: int
    span_end: int


def project_surface(agent: str) -> NativeSurface:
    """Return the project surface for one agent, else capability_unsupported."""
    try:
        return _SURFACES[agent]
    except KeyError:
        raise McpNativeError("mcp_capability_unsupported") from None


def parse_surface_bytes(surface: NativeSurface, data: bytes) -> NativeRead:
    """Parse surface bytes into a typed read without touching the disk."""
    _require_canonical_surface(surface)
    digest = hashlib.sha256(data).hexdigest()
    if len(data) > _MAX_SURFACE_BYTES:
        return NativeRead("malformed", {}, digest, _TOO_LARGE)
    try:
        doc = _parse_full(surface, data)
    except McpNativeError as exc:
        return NativeRead("malformed", {}, digest, exc.code)
    container = doc.get(surface.container, {})
    if not isinstance(container, dict):
        return NativeRead("malformed", {}, digest, _UNSUPPORTED_SYNTAX)
    return NativeRead("present", dict(container), digest, None)


def read_surface(
    project_root: Path, surface: NativeSurface
) -> tuple[NativeRead, bytes | None]:
    """Read one surface without following links at any component below root.

    A link or reparse point at the file or at a parent component (``.cursor``,
    ``.codex``) is ``unreadable`` with reason ``link``; a dangling link is
    unreadable, never absent. Files over 4 MiB are ``malformed`` with reason
    ``mcp_native_config_too_large``; their bytes are not returned.
    """
    _require_canonical_surface(surface)
    outcome, payload = _read_nofollow_bytes(project_root, surface.relpath)
    if outcome == "absent":
        return NativeRead("absent", {}, None, None), None
    if outcome == "unreadable":
        assert isinstance(payload, str)
        return NativeRead("unreadable", {}, None, payload), None
    if outcome == "too_large":
        return NativeRead("malformed", {}, None, _TOO_LARGE), None
    assert isinstance(payload, bytes)
    return parse_surface_bytes(surface, payload), payload


def edit_surface_bytes(
    surface: NativeSurface,
    data: bytes | None,
    op: AddEntry | ReplaceEntry | RemoveEntry,
) -> bytes:
    """Apply one single-entry change as a span edit over the old bytes.

    Enforces the semantic (P1) and byte (P2) postconditions inside: on
    violation it raises and never returns bytes. ``data is None`` means the
    file is absent, where only Add applies.
    """
    _require_canonical_surface(surface)
    _validate_op(surface, op)
    if data is None:
        if not isinstance(op, AddEntry):
            raise McpNativeError("mcp_entry_conflict", surface.relpath)
        return _add_to_absent(surface, op)
    if len(data) > _MAX_SURFACE_BYTES:
        raise McpNativeError(_TOO_LARGE, surface.relpath)
    read = parse_surface_bytes(surface, data)
    if read.state != "present":
        raise McpNativeError(read.reason or _UNSUPPORTED_SYNTAX, surface.relpath)
    if isinstance(op, AddEntry):
        if op.name in read.entries:
            raise McpNativeError("mcp_entry_conflict", surface.relpath)
    elif op.name not in read.entries:
        raise McpNativeError("mcp_entry_conflict", surface.relpath)
    old_doc = _parse_full(surface, data)
    old_text = _decode_text(surface, data)
    try:
        if surface.syntax == "json":
            edit = _plan_json_edit(surface, old_doc, old_text, op)
        else:
            edit = _plan_toml_edit(surface, old_text, op)
    except (_json_span.JsonSpanError, _toml_span.TomlSpanError, RecursionError) as exc:
        raise McpNativeError(_UNSUPPORTED_SYNTAX, surface.relpath) from exc
    new_bytes = edit.new_text.encode("utf-8")
    if len(new_bytes) > _MAX_SURFACE_BYTES:
        raise McpNativeError(_TOO_LARGE, surface.relpath)
    _verify_p2(data, new_bytes, old_text, edit, surface.relpath, op)
    _verify_p1(surface, old_doc, new_bytes, op)
    return new_bytes


def is_effectively_empty(surface: NativeSurface, data: bytes) -> bool:
    """Return whether a file holds only an empty container and nothing else.

    JSON: the document is an object whose only member is an empty container.
    TOML: no keys or tables other than an empty or absent ``mcp_servers``,
    and no comment anywhere. Anything malformed or oversized is not empty.
    """
    _require_canonical_surface(surface)
    if len(data) > _MAX_SURFACE_BYTES:
        return False
    if parse_surface_bytes(surface, data).state != "present":
        return False
    try:
        doc = _parse_full(surface, data)
    except McpNativeError:
        return False
    if surface.syntax == "json":
        return set(doc) == {surface.container} and doc[surface.container] == {}
    if set(doc) - {surface.container}:
        return False
    if doc.get(surface.container, {}) != {}:
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    try:
        return not _toml_span.scan(text).has_comment
    except _toml_span.TomlSpanError:
        return False


def _require_canonical_surface(surface: NativeSurface) -> None:
    expected = project_surface(surface.agent)
    if surface != expected:
        raise McpNativeError("mcp_capability_unsupported", surface.relpath)


def _parse_full(surface: NativeSurface, data: bytes) -> dict[str, Any]:
    try:
        if surface.syntax == "json":
            doc = protocol_json.loads(data)
        else:
            if data.startswith(_UTF8_BOM):
                raise McpNativeError(_UNSUPPORTED_SYNTAX, surface.relpath)
            doc = tomllib.loads(data.decode("utf-8"))
    except McpNativeError:
        raise
    except Exception as exc:
        raise McpNativeError(_UNSUPPORTED_SYNTAX, surface.relpath) from exc
    if not isinstance(doc, dict):
        raise McpNativeError(_UNSUPPORTED_SYNTAX, surface.relpath)
    stack: list[tuple[object, int]] = [(doc, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, (dict, list)):
            if depth > _MAX_DEPTH:
                raise McpNativeError(_UNSUPPORTED_SYNTAX, surface.relpath)
            children = node.values() if isinstance(node, dict) else node
            stack.extend((child, depth + 1) for child in children)
    return doc


def _decode_text(surface: NativeSurface, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise McpNativeError(_UNSUPPORTED_SYNTAX, surface.relpath) from exc


def _is_link(mode: int, file_attributes: int | None) -> bool:
    """Return whether stat data describes a symlink or reparse point.

    The reparse-point bit is honored wherever present (Windows); POSIX
    stat results carry no file-attributes word.
    """
    if stat.S_ISLNK(mode):
        return True
    return file_attributes is not None and bool(file_attributes & _REPARSE_POINT)


def _file_attributes_of(value: os.stat_result) -> int | None:
    raw = getattr(value, "st_file_attributes", None)
    return int(raw) if isinstance(raw, int) else None


def _read_nofollow_bytes(
    root: Path, relpath: str
) -> tuple[str, bytes | str | None]:
    """Read one file held through directory descriptors, never following links.

    Walks from ``project_root`` with ``O_NOFOLLOW | O_DIRECTORY`` per parent
    component and an openat-style final open with ``O_NOFOLLOW``, reusing the
    owned ``csk.sources._selection_fs`` seam (with its Windows counterpart).
    Descriptors are held until the bounded read completes, so a parent link
    introduced between check and open cannot be followed. Typed results match
    the previous contract: absent, unreadable(link/...), too_large, ok.
    """
    parts = relpath.split("/")
    fds: list[int] = []
    try:
        try:
            root_fd = _selection_fs._open_descriptor(
                os.fspath(root), _selection_fs._directory_flags(nofollow=False)
            )
        except FileNotFoundError:
            return ("absent", None)
        except OSError as exc:
            if _is_permission(exc):
                return ("unreadable", "permission_denied")
            if getattr(exc, "errno", None) == errno.ENOENT:
                return ("absent", None)
            return ("unreadable", "io_error")
        fds.append(root_fd)
        parent_fd = root_fd
        parent_path = root
        for part in parts[:-1]:
            try:
                child_fd = _selection_fs._open_child_directory(
                    parent_fd, part, parent_path=parent_path
                )
            except FileNotFoundError:
                return ("absent", None)
            except OSError as exc:
                err = getattr(exc, "errno", None)
                if err == errno.ELOOP:
                    return ("unreadable", "link")
                if err == errno.ENOENT:
                    return ("absent", None)
                if err == errno.ENOTDIR:
                    # macOS reports a symlink-to-dir opened with
                    # O_NOFOLLOW|O_DIRECTORY as ENOTDIR, same as a file.
                    # Disambiguate with a dir_fd-relative lstat.
                    if _is_link_at(parent_fd, part, parent_path):
                        return ("unreadable", "link")
                    return ("unreadable", "parent_not_directory")
                if _is_permission(exc):
                    return ("unreadable", "permission_denied")
                return ("unreadable", "io_error")
            fds.append(child_fd)
            parent_fd = child_fd
            parent_path = parent_path / part
        leaf = parts[-1]
        try:
            file_fd = _selection_fs._open_child_file(
                parent_fd, leaf, parent_path=parent_path
            )
        except FileNotFoundError:
            return ("absent", None)
        except OSError as exc:
            err = getattr(exc, "errno", None)
            if err == errno.ELOOP:
                return ("unreadable", "link")
            if err == errno.ENOENT:
                return ("absent", None)
            if err == errno.EISDIR:
                return ("unreadable", "not_regular_file")
            if _is_permission(exc):
                return ("unreadable", "permission_denied")
            return ("unreadable", "io_error")
        fds.append(file_fd)
        try:
            if os.name == "nt":
                from .sources import _selection_fs_windows

                opened: Any = _selection_fs_windows.stat_handle(file_fd)
            else:
                opened = os.fstat(file_fd)
        except OSError as exc:
            if _is_permission(exc):
                return ("unreadable", "permission_denied")
            return ("unreadable", "io_error")
        mode = int(getattr(opened, "st_mode", 0))
        attrs = getattr(opened, "st_file_attributes", None)
        file_attrs = int(attrs) if isinstance(attrs, int) else None
        if _is_link(mode, file_attrs):
            return ("unreadable", "link")
        if not stat.S_ISREG(mode):
            return ("unreadable", "not_regular_file")
        size = getattr(opened, "st_size", 0)
        if isinstance(size, int) and size > _MAX_SURFACE_BYTES:
            return ("too_large", None)
        chunks: list[bytes] = []
        remaining = _MAX_SURFACE_BYTES + 1
        while remaining > 0:
            try:
                chunk = _selection_fs._read_descriptor(
                    file_fd, min(_READ_CHUNK, remaining), parent_fd=parent_fd, name=leaf
                )
            except OSError:
                return ("unreadable", "io_error")
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                return ("unreadable", "io_error")
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _MAX_SURFACE_BYTES:
            return ("too_large", None)
        return ("ok", data)
    finally:
        for fd in reversed(fds):
            try:
                _selection_fs._close_quietly(fd)
            except Exception:
                pass


def _is_permission(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (
        errno.EACCES,
        errno.EPERM,
    )


def _is_link_at(parent_fd: int, name: str, parent_path: Path) -> bool:
    """Return whether a child that failed ENOTDIR is a link (fail-closed)."""
    if os.name == "nt":
        return False
    try:
        st = os.lstat(name, dir_fd=parent_fd)
    except (TypeError, NotImplementedError):
        try:
            st = os.lstat(parent_path / name)
        except OSError:
            return True
    except OSError:
        return True
    return _is_link(st.st_mode, _file_attributes_of(st))


def _validate_op(
    surface: NativeSurface, op: AddEntry | ReplaceEntry | RemoveEntry
) -> None:
    if not isinstance(op, (AddEntry, ReplaceEntry, RemoveEntry)):
        raise McpNativeError("mcp_declaration_invalid", surface.relpath)
    if (
        not isinstance(op.name, str)
        or not op.name
        or _has_lone_surrogate(op.name)
    ):
        raise McpNativeError("mcp_declaration_invalid", surface.relpath)
    if surface.syntax == "toml" and _BARE_KEY.fullmatch(op.name) is None:
        raise McpNativeError("mcp_declaration_invalid", surface.relpath)
    if isinstance(op, RemoveEntry):
        return
    _validate_value(op.value, surface.relpath)


def _validate_value(value: Mapping[str, object], relpath: str) -> None:
    if not isinstance(value, Mapping):
        raise McpNativeError("mcp_declaration_invalid", relpath)
    for key, item in value.items():
        if not isinstance(key, str) or _has_lone_surrogate(key):
            raise McpNativeError("mcp_declaration_invalid", relpath)
        if isinstance(item, str):
            if _has_lone_surrogate(item):
                raise McpNativeError("mcp_declaration_invalid", relpath)
        elif isinstance(item, list):
            for entry in item:
                if not isinstance(entry, str) or _has_lone_surrogate(entry):
                    raise McpNativeError("mcp_declaration_invalid", relpath)
        elif isinstance(item, Mapping):
            for sub_key, sub_item in item.items():
                if not isinstance(sub_key, str) or _has_lone_surrogate(sub_key):
                    raise McpNativeError("mcp_declaration_invalid", relpath)
                if not isinstance(sub_item, str) or _has_lone_surrogate(sub_item):
                    raise McpNativeError("mcp_declaration_invalid", relpath)
        else:
            raise McpNativeError("mcp_declaration_invalid", relpath)


def _has_lone_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _freeze(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_freeze(item) for item in value]
    return value


def _plan_json_edit(
    surface: NativeSurface,
    old_doc: dict[str, Any],
    old_text: str,
    op: AddEntry | ReplaceEntry | RemoveEntry,
) -> _SpanEdit:
    scan_result = _json_span.scan(old_text)
    if isinstance(op, AddEntry):
        if surface.container in old_doc:
            planned = _json_span.plan_add_member(
                scan_result, surface.container, op.name, op.value
            )
        else:
            planned = _json_span.plan_add_container(
                scan_result, surface.container, op.name, op.value
            )
        return _SpanEdit(planned.new_text, planned.span_start, planned.span_end)
    if isinstance(op, ReplaceEntry):
        planned = _json_span.plan_replace_member(
            scan_result, surface.container, op.name, op.value
        )
        return _SpanEdit(planned.new_text, planned.span_start, planned.span_end)
    if isinstance(op, RemoveEntry):
        planned = _json_span.plan_remove_member(
            scan_result, surface.container, op.name
        )
        return _SpanEdit(planned.new_text, planned.span_start, planned.span_end)
    raise McpNativeError("mcp_declaration_invalid", surface.relpath)


def _plan_toml_edit(
    surface: NativeSurface, old_text: str, op: AddEntry | ReplaceEntry | RemoveEntry
) -> _SpanEdit:
    toml_scan = _toml_span.scan(old_text)
    nl = _toml_span.newline_of(old_text)
    if isinstance(op, AddEntry):
        group_text = _toml_span.render_group(op.name, op.value, nl=nl)
        planned = _toml_span.plan_add(old_text, group_text, nl)
        return _SpanEdit(planned.new_text, planned.span_start, planned.span_end)
    group = _toml_span.find_group(toml_scan.lines, op.name)
    if group is None:
        raise McpNativeError(_UNSUPPORTED_SYNTAX, surface.relpath)
    if isinstance(op, ReplaceEntry):
        planned = _toml_span.plan_replace(
            old_text, toml_scan.lines, group, op.name, op.value, nl
        )
        return _SpanEdit(planned.new_text, planned.span_start, planned.span_end)
    if isinstance(op, RemoveEntry):
        planned = _toml_span.plan_remove(old_text, toml_scan.lines, group)
        return _SpanEdit(planned.new_text, planned.span_start, planned.span_end)
    raise McpNativeError("mcp_declaration_invalid", surface.relpath)


def _add_to_absent(surface: NativeSurface, op: AddEntry) -> bytes:
    if surface.syntax == "json":
        new_bytes = _json_span.render_absent_file(
            surface.container, op.name, op.value
        )
    else:
        new_bytes = _toml_span.render_group(op.name, op.value, nl="\n").encode("utf-8")
    if len(new_bytes) > _MAX_SURFACE_BYTES:
        raise McpNativeError(_TOO_LARGE, surface.relpath)
    _verify_p1(surface, {}, new_bytes, op)
    return new_bytes


def _verify_p2(
    old_bytes: bytes,
    new_bytes: bytes,
    old_text: str,
    edit: _SpanEdit,
    relpath: str,
    op: AddEntry | ReplaceEntry | RemoveEntry | None = None,
) -> None:
    start = edit.span_start
    end = edit.span_end
    new_text = edit.new_text
    if not 0 <= start <= end <= len(old_text):
        raise McpNativeError(_UNSUPPORTED_SYNTAX, relpath)
    if isinstance(op, AddEntry) and start != end:
        raise McpNativeError(_UNSUPPORTED_SYNTAX, relpath)
    if isinstance(op, (ReplaceEntry, RemoveEntry)) and start == end:
        raise McpNativeError(_UNSUPPORTED_SYNTAX, relpath)
    if new_text[:start] != old_text[:start]:
        raise McpNativeError(_UNSUPPORTED_SYNTAX, relpath)
    tail = len(old_text) - end
    if tail:
        if new_text[len(new_text) - tail :] != old_text[end:]:
            raise McpNativeError(_UNSUPPORTED_SYNTAX, relpath)
        middle = new_text[start : len(new_text) - tail]
    else:
        middle = new_text[start:]
    if len(new_text) < start + tail:
        raise McpNativeError(_UNSUPPORTED_SYNTAX, relpath)
    byte_start = len(old_text[:start].encode("utf-8"))
    byte_end = len(old_text[:end].encode("utf-8"))
    expected = old_bytes[:byte_start] + middle.encode("utf-8") + old_bytes[byte_end:]
    if new_bytes != expected:
        raise McpNativeError(_UNSUPPORTED_SYNTAX, relpath)
    if isinstance(op, AddEntry):
        inserted_length = len(new_bytes) - len(old_bytes)
        if inserted_length <= 0 or old_bytes != (
            new_bytes[:byte_start] + new_bytes[byte_start + inserted_length:]
        ):
            raise McpNativeError(_UNSUPPORTED_SYNTAX, relpath)


def _values_equal(left: object, right: object) -> bool:
    """Type-exact deep equality for the P1 semantic postcondition.

    Dicts need the exact key set, lists need exact order, and every scalar
    needs the same type and value: bool, int and float are distinct, and
    TOML date/time values compare by type and value. NaN equals NaN so a
    foreign NaN survives a round trip.
    """
    stack = [(left, right)]
    while stack:
        left, right = stack.pop()
        if type(left) is not type(right):
            return False
        if isinstance(left, dict) and isinstance(right, dict):
            if set(left.keys()) != set(right.keys()):
                return False
            stack.extend((left[key], right[key]) for key in left)
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                return False
            stack.extend(zip(left, right))
        elif isinstance(left, float) and isinstance(right, float):
            if math.isnan(left) and math.isnan(right):
                continue
            if left != right or math.copysign(1.0, left) != math.copysign(1.0, right):
                return False
        elif left != right:
            return False
    return True


def _verify_p1(
    surface: NativeSurface,
    old_doc: dict[str, Any],
    new_bytes: bytes,
    op: AddEntry | ReplaceEntry | RemoveEntry,
) -> None:
    try:
        new_doc = _parse_full(surface, new_bytes)
    except McpNativeError as exc:
        raise McpNativeError(_UNSUPPORTED_SYNTAX, surface.relpath) from exc
    expected = dict(old_doc)
    container = expected.get(surface.container, {})
    if not isinstance(container, dict):
        raise McpNativeError(_UNSUPPORTED_SYNTAX, surface.relpath)
    container = dict(container)
    expected[surface.container] = container
    if isinstance(op, AddEntry):
        if op.name in container:
            raise McpNativeError(_UNSUPPORTED_SYNTAX, surface.relpath)
        container[op.name] = _freeze(op.value)
    elif isinstance(op, ReplaceEntry):
        if op.name not in container:
            raise McpNativeError(_UNSUPPORTED_SYNTAX, surface.relpath)
        container[op.name] = _freeze(op.value)
    elif isinstance(op, RemoveEntry):
        if op.name not in container:
            raise McpNativeError(_UNSUPPORTED_SYNTAX, surface.relpath)
        del container[op.name]
    else:
        raise McpNativeError("mcp_declaration_invalid", surface.relpath)
    if surface.syntax == "toml":
        if expected.get(surface.container) == {}:
            del expected[surface.container]
        if new_doc.get(surface.container) == {}:
            del new_doc[surface.container]
    if not _values_equal(new_doc, expected):
        raise McpNativeError(_UNSUPPORTED_SYNTAX, surface.relpath)
