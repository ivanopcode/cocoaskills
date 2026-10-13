"""Line-aware TOML scanning for native MCP config surfaces.

The scanner understands bare, basic-quoted and literal-quoted keys, dotted
keys, multi-line basic and literal strings, ``[[array]]`` headers, comments
and CRLF, so a ``[`` inside a multi-line string is never read as a header.
It locates the standard-form table group csk owns for one entry and plans
single-span add/replace/remove edits over it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final


class TomlSpanError(ValueError):
    """The text is not the TOML document the caller parsed."""


_BARE_KEY: Final = re.compile(r"[A-Za-z0-9_-]+")

_BASIC_ESCAPES: Final = {
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    '"': '"',
    "\\": "\\",
}


@dataclass(frozen=True)
class TomlHeader:
    """One parsed ``[table]`` or ``[[array]]`` header."""

    path: tuple[str, ...]
    is_array: bool
    all_bare: bool


@dataclass(frozen=True)
class TomlLine:
    """One logical line: kind, character offsets and optional header."""

    kind: str  # "header" | "kv" | "blank" | "comment"
    start: int
    end: int
    header: TomlHeader | None
    has_comment: bool


@dataclass(frozen=True)
class TomlScan:
    """Scan result: logical lines plus whether any comment is present."""

    lines: tuple[TomlLine, ...]
    has_comment: bool


@dataclass(frozen=True)
class TomlEdit:
    """Planned edit: replacement text and the replaced span in the old text."""

    new_text: str
    span_start: int
    span_end: int


def scan(text: str) -> TomlScan:
    """Split text into logical lines, tracking strings and array depth.

    A header is only a line-leading ``[`` at depth 0 outside any string.
    Bracket depth covers multi-line arrays, nested included, and successive
    multi-line basic/literal strings (including inside arrays) are tracked,
    so a ``[`` inside either is never read as a header. Multi-line strings
    merge into one kv; array continuation lines stay separate so standalone
    comments inside arrays keep their kind.
    """
    physicals = _split_physical(text)
    lines: list[TomlLine] = []
    has_comment = False
    index = 0
    in_ml: str | None = None
    depth = 0
    kv_start = 0
    total = len(physicals)
    while index < total:
        start, end = physicals[index]
        body = _line_body(text, start, end)
        if in_ml is not None:
            close = _find_ml_close(body, 0, in_ml)
            if close is None:
                index += 1
                if index >= total:
                    raise TomlSpanError("unterminated multi-line string")
                continue
            new_opener, new_depth, trailing = _scan_value_content(body, close, depth)
            has_comment = has_comment or trailing
            lines.append(TomlLine("kv", kv_start, end, None, trailing))
            in_ml = new_opener
            depth = new_depth
            if new_opener is not None:
                kv_start = end
                index += 1
                if index >= total:
                    raise TomlSpanError("unterminated multi-line string")
                continue
            index += 1
            continue
        if depth > 0:
            stripped = body.lstrip(" \t")
            if stripped == "":
                lines.append(TomlLine("blank", start, end, None, False))
                index += 1
                continue
            if stripped.startswith("#"):
                lines.append(TomlLine("comment", start, end, None, True))
                has_comment = True
                index += 1
                continue
            new_opener, new_depth, trailing = _scan_value_content(body, 0, depth)
            has_comment = has_comment or trailing
            if new_opener is not None:
                in_ml = new_opener
                depth = new_depth
                kv_start = start
                index += 1
                if index >= total:
                    raise TomlSpanError("unterminated multi-line string")
                continue
            depth = new_depth
            lines.append(TomlLine("kv", start, end, None, trailing))
            index += 1
            continue
        stripped = body.lstrip(" \t")
        if stripped == "":
            lines.append(TomlLine("blank", start, end, None, False))
            index += 1
            continue
        if stripped.startswith("#"):
            lines.append(TomlLine("comment", start, end, None, True))
            has_comment = True
            index += 1
            continue
        if stripped.startswith("["):
            header, trailing = _parse_header_line(stripped)
            has_comment = has_comment or trailing
            lines.append(TomlLine("header", start, end, header, trailing))
            index += 1
            continue
        new_opener, new_depth, trailing = _scan_value_content(body, 0, 0)
        has_comment = has_comment or trailing
        if new_opener is not None:
            in_ml = new_opener
            depth = new_depth
            kv_start = start
            index += 1
            if index >= total:
                raise TomlSpanError("unterminated multi-line string")
            continue
        if new_depth > 0:
            depth = new_depth
            lines.append(TomlLine("kv", start, end, None, trailing))
            index += 1
            continue
        lines.append(TomlLine("kv", start, end, None, trailing))
        index += 1
    if in_ml is not None:
        raise TomlSpanError("unterminated multi-line string")
    if depth > 0:
        raise TomlSpanError("unterminated array")
    return TomlScan(tuple(lines), has_comment)


def find_group(lines: tuple[TomlLine, ...], name: str) -> tuple[int, int] | None:
    """Locate the owned group for one entry, or None when it is absent.

    The group is the standard header ``[mcp_servers.NAME]`` with both
    segments bare, plus the following lines up to (not including) the next
    header whose path does not start with ``("mcp_servers", NAME)``.
    """
    start_index: int | None = None
    for index, line in enumerate(lines):
        header = line.header
        if line.kind != "header" or header is None:
            continue
        if not header.is_array and header.all_bare and header.path == (
            "mcp_servers",
            name,
        ):
            start_index = index
            break
    if start_index is None:
        return None
    end_index = len(lines)
    for follow in range(start_index + 1, len(lines)):
        line = lines[follow]
        header = line.header
        if line.kind != "header" or header is None:
            continue
        path = header.path
        if len(path) >= 2 and path[0] == "mcp_servers" and path[1] == name:
            continue
        end_index = follow
        # A leading comment/blank block belongs to the next section.
        while end_index > start_index + 1 and lines[end_index - 1].kind in (
            "comment", "blank"
        ):
            end_index -= 1
        break
    return (lines[start_index].start, lines[end_index - 1].end)


def newline_of(text: str) -> str:
    """Infer the newline style from the first newline: CRLF or LF."""
    index = text.find("\n")
    if index > 0 and text[index - 1] == "\r":
        return "\r\n"
    return "\n"


def render_group(name: str, value: Mapping[str, object], *, nl: str) -> str:
    """Render the standard table group csk writes for one entry."""
    out: list[str] = [f"[mcp_servers.{name}]{nl}"]
    scalars = [(k, v) for k, v in value.items() if not isinstance(v, Mapping)]
    tables = [(k, v) for k, v in value.items() if isinstance(v, Mapping)]
    for key, item in scalars:
        out.append(f"{_render_key(key)} = {_render_scalar(item)}{nl}")
    for key, item in tables:
        assert isinstance(item, Mapping)
        out.append(f"[mcp_servers.{name}.{_render_key(key)}]{nl}")
        for sub_key, sub_item in item.items():
            out.append(f"{_render_key(sub_key)} = {_render_scalar(sub_item)}{nl}")
    return "".join(out)


def plan_add(text: str, group_text: str, nl: str) -> TomlEdit:
    """Append a group at EOF after a blank separator line."""
    if text == "":
        return TomlEdit(group_text, 0, 0)
    addition = ("" if text.endswith("\n") else nl) + nl + group_text
    return TomlEdit(text + addition, len(text), len(text))


def plan_replace(
    text: str,
    lines: tuple[TomlLine, ...],
    group: tuple[int, int],
    name: str,
    value: Mapping[str, object],
    nl: str,
) -> TomlEdit:
    """Substitute a group in place, keeping every standalone comment line.

    Headers are kept (verbatim when the section still exists, else rendered).
    Each section's comments stay in original order as a block directly after
    its header, followed by the new key/value lines. Comments from deleted
    subtables join the main block in original order. Blank lines are dropped.
    A missing final newline is preserved when the group reaches EOF.
    """
    start, end = group
    in_group = [ln for ln in lines if ln.start >= start and ln.end <= end]
    sections: list[tuple[TomlLine, tuple[str, ...], list[str]]] = []
    for ln in in_group:
        if ln.kind == "header" and ln.header is not None:
            sections.append((ln, ln.header.path, []))
        elif ln.kind == "comment" and sections:
            sections[-1][2].append(_line_body(text, ln.start, ln.end))
    if not sections:
        raise TomlSpanError("owned group has no header")
    main_header, main_path, main_comments = sections[0]
    if main_path != ("mcp_servers", name):
        raise TomlSpanError("owned group header mismatch")
    scalars = [(k, v) for k, v in value.items() if not isinstance(v, Mapping)]
    tables = [(k, v) for k, v in value.items() if isinstance(v, Mapping)]
    table_keys = {k for k, _v in tables}
    blocks: dict[str, list[str]] = {k: [] for k in table_keys}
    header_verbatim: dict[str, str] = {}
    orphans: list[str] = []
    for _hdr, path, comments in sections[1:]:
        if (
            len(path) == 3
            and path[0] == "mcp_servers"
            and path[1] == name
            and path[2] in table_keys
        ):
            key = path[2]
            blocks[key].extend(comments)
            if key not in header_verbatim:
                header_verbatim[key] = _line_body(text, _hdr.start, _hdr.end)
        elif (
            len(path) > 3
            and path[0] == "mcp_servers"
            and path[1] == name
            and path[2] in table_keys
        ):
            blocks[path[2]].extend(comments)
        else:
            orphans.extend(comments)
    out: list[str] = [_line_body(text, main_header.start, main_header.end) + nl]
    for comment in list(main_comments) + orphans:
        out.append(comment + nl)
    for key, item in scalars:
        out.append(f"{_render_key(key)} = {_render_scalar(item)}{nl}")
    for key, item in tables:
        assert isinstance(item, Mapping)
        if key in header_verbatim:
            out.append(header_verbatim[key] + nl)
        else:
            out.append(f"[mcp_servers.{name}.{_render_key(key)}]{nl}")
        for comment in blocks[key]:
            out.append(comment + nl)
        for sub_key, sub_item in item.items():
            out.append(f"{_render_key(sub_key)} = {_render_scalar(sub_item)}{nl}")
    group_text = "".join(out)
    new_text = text[:start] + group_text + text[end:]
    if text and not text.endswith("\n") and end == len(text):
        new_text = new_text[: -len(nl)]
    return TomlEdit(new_text, start, end)


def plan_remove(
    text: str, lines: tuple[TomlLine, ...], group: tuple[int, int]
) -> TomlEdit:
    """Delete a group's header and key/value lines, keeping comment lines.

    Pure span deletion: when the group reaches EOF the surviving prefix
    keeps its own final line break, so a missing final newline may become
    present. The byte postcondition takes precedence over that convention.
    """
    start, end = group
    kept = "".join(
        text[line.start : line.end]
        for line in lines
        if line.kind == "comment" and line.start >= start and line.end <= end
    )
    return TomlEdit(text[:start] + kept + text[end:], start, end)


def _render_key(key: object) -> str:
    if not isinstance(key, str):
        raise TomlSpanError("TOML key is not a string")
    if _BARE_KEY.fullmatch(key) is not None:
        return key
    return _render_basic_string(key)


def _render_scalar(item: object) -> str:
    if isinstance(item, str):
        return _render_basic_string(item)
    if isinstance(item, list):
        return "[" + ", ".join(_render_basic_string(v) for v in item) + "]"
    raise TomlSpanError("TOML value shape is not renderable")


def _render_basic_string(value: object) -> str:
    if not isinstance(value, str):
        raise TomlSpanError("TOML string value is not a string")
    out: list[str] = ['"']
    for char in value:
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\b":
            out.append("\\b")
        elif char == "\t":
            out.append("\\t")
        elif char == "\n":
            out.append("\\n")
        elif char == "\f":
            out.append("\\f")
        elif char == "\r":
            out.append("\\r")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _split_physical(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(text):
        if char == "\n":
            spans.append((start, index + 1))
            start = index + 1
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _line_body(text: str, start: int, end: int) -> str:
    body = text[start:end]
    if body.endswith("\n"):
        body = body[:-1]
    if body.endswith("\r"):
        body = body[:-1]
    return body


def _scan_value_content(
    body: str, pos: int, depth: int
) -> tuple[str | None, int, bool]:
    """Scan TOML value content; return unclosed opener, depth and trailing flag.

    Tracks bracket depth for multi-line arrays (nested included) and
    successive multi-line strings, skipping single-line strings so brackets
    inside them never affect depth. A ``#`` outside any string ends the scan
    as a trailing comment.
    """
    while pos < len(body):
        if body[pos] == "#":
            return None, depth, True
        if body.startswith('"""', pos) or body.startswith("'''", pos):
            quote = body[pos : pos + 3]
            close = _find_ml_close(body, pos + 3, quote)
            if close is None:
                return quote, depth, False
            pos = close
            continue
        if body[pos] == '"':
            pos = _skip_basic_string(body, pos)
            continue
        if body[pos] == "'":
            pos = _skip_literal_string(body, pos)
            continue
        if body[pos] == "[":
            depth += 1
            pos += 1
            continue
        if body[pos] == "]":
            if depth > 0:
                depth -= 1
            pos += 1
            continue
        pos += 1
    return None, depth, False


def _skip_basic_string(body: str, pos: int) -> int:
    pos += 1
    while True:
        if pos >= len(body):
            raise TomlSpanError("unterminated string on a TOML line")
        char = body[pos]
        if char == "\\":
            if pos + 1 >= len(body):
                raise TomlSpanError("unterminated escape on a TOML line")
            pos += 2
            continue
        if char == '"':
            return pos + 1
        pos += 1


def _skip_literal_string(body: str, pos: int) -> int:
    end = body.find("'", pos + 1)
    if end < 0:
        raise TomlSpanError("unterminated literal string on a TOML line")
    return end + 1


def _find_ml_close(body: str, pos: int, quote: str) -> int | None:
    """Find a multi-line closing delimiter; return the offset after it."""
    if quote == "'''":
        found = body.find("'''", pos)
        if found < 0:
            return None
        end = found + 3
        while end < len(body) and body[end] == "'" and end - found < 5:
            end += 1
        return end
    while pos < len(body):
        if body[pos] == "\\":
            pos += 2
            continue
        if body.startswith('"""', pos):
            end = pos + 3
            while end < len(body) and body[end] == '"' and end - pos < 5:
                end += 1
            return end
        pos += 1
    return None


def _parse_header_line(frag: str) -> tuple[TomlHeader, bool]:
    is_array = frag.startswith("[[")
    pos = 2 if is_array else 1
    pos = _skip_ws(frag, pos)
    segments: list[str] = []
    bares: list[bool] = []
    while True:
        segment, bare, pos = _parse_key(frag, pos)
        segments.append(segment)
        bares.append(bare)
        pos = _skip_ws(frag, pos)
        if pos < len(frag) and frag[pos] == ".":
            pos = _skip_ws(frag, pos + 1)
            continue
        break
    pos = _skip_ws(frag, pos)
    if is_array:
        if not frag.startswith("]]", pos):
            raise TomlSpanError("malformed TOML array header")
        pos += 2
    else:
        if pos >= len(frag) or frag[pos] != "]":
            raise TomlSpanError("malformed TOML table header")
        pos += 1
    rest = frag[pos:].lstrip(" \t")
    if rest != "" and not rest.startswith("#"):
        raise TomlSpanError("malformed TOML header line")
    return (
        TomlHeader(tuple(segments), is_array, all(bares)),
        rest.startswith("#"),
    )


def _skip_ws(frag: str, pos: int) -> int:
    while pos < len(frag) and frag[pos] in " \t":
        pos += 1
    return pos


def _parse_key(frag: str, pos: int) -> tuple[str, bool, int]:
    if pos >= len(frag):
        raise TomlSpanError("missing key in TOML header")
    char = frag[pos]
    if char == '"':
        return _parse_basic_key(frag, pos)
    if char == "'":
        end = frag.find("'", pos + 1)
        if end < 0:
            raise TomlSpanError("unterminated quoted key in TOML header")
        return frag[pos + 1 : end], False, end + 1
    match = _BARE_KEY.match(frag, pos)
    if match is None:
        raise TomlSpanError("invalid bare key in TOML header")
    return match.group(0), True, match.end()


def _parse_basic_key(frag: str, pos: int) -> tuple[str, bool, int]:
    out: list[str] = []
    pos += 1
    while True:
        if pos >= len(frag):
            raise TomlSpanError("unterminated quoted key in TOML header")
        char = frag[pos]
        if char == '"':
            return "".join(out), False, pos + 1
        if char != "\\":
            out.append(char)
            pos += 1
            continue
        if pos + 1 >= len(frag):
            raise TomlSpanError("unterminated escape in TOML header")
        escaped = frag[pos + 1]
        if escaped in _BASIC_ESCAPES:
            out.append(_BASIC_ESCAPES[escaped])
            pos += 2
        elif escaped == "u":
            out.append(_decode_hex_escape(frag, pos, 4))
            pos += 6
        elif escaped == "U":
            out.append(_decode_hex_escape(frag, pos, 8))
            pos += 10
        else:
            raise TomlSpanError("invalid escape in TOML header")


def _decode_hex_escape(frag: str, pos: int, width: int) -> str:
    digits = frag[pos + 2 : pos + 2 + width]
    if len(digits) != width or any(c not in "0123456789abcdefABCDEF" for c in digits):
        raise TomlSpanError("invalid unicode escape in TOML header")
    return chr(int(digits, 16))
