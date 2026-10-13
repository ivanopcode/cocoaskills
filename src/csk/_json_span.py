"""Span-aware strict-JSON editing for native MCP config surfaces.

The scanner locates the top-level object, one container member and one target
member as character spans. The planners rewrite exactly one span per edit and
report it, so the caller can verify the byte postcondition. This module never
parses for semantics; the caller strict-parses before and after every edit.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final


class JsonSpanError(ValueError):
    """The text is not the strict JSON document the caller parsed."""


_WS: Final = " \t\n\r"


@dataclass(frozen=True)
class JsonMember:
    """One object member with key and value spans (character offsets)."""

    key: str
    key_start: int
    key_end: int
    value_start: int
    value_end: int
    value_is_object: bool


@dataclass(frozen=True)
class JsonObjectSpan:
    """One JSON object with brace offsets and member spans."""

    open_brace: int
    close_brace: int
    members: tuple[JsonMember, ...]


@dataclass(frozen=True)
class ScannedJson:
    """Scan result: top object plus every nested object by open-brace offset."""

    text: str
    top: JsonObjectSpan
    objects: dict[int, JsonObjectSpan] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class JsonEdit:
    """Planned edit: replacement text and the replaced span in the old text."""

    new_text: str
    span_start: int
    span_end: int


def scan(text: str) -> ScannedJson:
    """Tokenize strict JSON and record every object span.

    Raises :class:`JsonSpanError` on anything that is not exactly one
    top-level object: comments, trailing commas and trailing bytes all fail.
    """
    parser = _Parser(text)
    top = parser.parse_top()
    return ScannedJson(text=text, top=top, objects=parser.objects)


def infer_indent_unit(text: str) -> str:
    """Infer the indent unit from the first indented line.

    A leading tab means a tab unit, else spaces of the observed width.
    Files without an indented line fall back to two spaces.
    """
    for line in text.split("\n"):
        end = 0
        while end < len(line) and line[end] in " \t":
            end += 1
        if end == 0:
            continue
        run = line[:end]
        if run[0] == "\t":
            return "\t"
        return " " * len(run)
    return "  "


def infer_newline(text: str) -> str:
    """Infer the newline style from the first newline: CRLF or LF."""
    index = text.find("\n")
    if index > 0 and text[index - 1] == "\r":
        return "\r\n"
    return "\n"


def render_value(
    value: Mapping[str, object], *, base_indent: str, unit: str, nl: str
) -> str:
    """Render an entry value as indented JSON for one member position."""
    raw = json.dumps(value, indent=unit, ensure_ascii=False)
    lines = raw.split("\n")
    return lines[0] + "".join(nl + base_indent + line for line in lines[1:])


def render_member(
    name: str, value: Mapping[str, object], *, base_indent: str, unit: str, nl: str
) -> str:
    """Render one `"name": value` member for one member position."""
    return (
        json.dumps(name, ensure_ascii=False)
        + ": "
        + render_value(value, base_indent=base_indent, unit=unit, nl=nl)
    )


def render_absent_file(container: str, name: str, value: Mapping[str, object]) -> bytes:
    """Render the fixed template for an Add when no file exists (LF)."""
    member = render_member(name, value, base_indent="    ", unit="  ", nl="\n")
    text = (
        "{\n  " + json.dumps(container, ensure_ascii=False) + ": {\n    " + member + "\n  }\n}\n"
    )
    return text.encode("utf-8")


def plan_add_member(
    scan_result: ScannedJson, container_key: str, name: str, value: Mapping[str, object]
) -> JsonEdit:
    """Append a member at the end of an existing container object.

    Pure insertion: exactly one contiguous string at one zero-width offset;
    every original byte survives, including the gap before the closing brace.
    """
    text = scan_result.text
    unit = infer_indent_unit(text)
    nl = infer_newline(text)
    obj = _container_object(scan_result, container_key)
    member_indent = unit * 2
    member = render_member(
        name, value, base_indent=member_indent, unit=unit, nl=nl
    )
    if obj.members:
        offset = obj.members[-1].value_end
        insertion = "," + nl + member_indent + member
        return JsonEdit(text[:offset] + insertion + text[offset:], offset, offset)
    close_indent = _line_indent(text, obj.close_brace)
    insertion = nl + member_indent + member + nl + close_indent
    offset = obj.open_brace + 1
    return JsonEdit(text[:offset] + insertion + text[offset:], offset, offset)


def plan_add_container(
    scan_result: ScannedJson, container_key: str, name: str, value: Mapping[str, object]
) -> JsonEdit:
    """Append a new container member at the end of the top-level object.

    Pure insertion: exactly one contiguous string at one zero-width offset;
    every original byte survives, including the gap before the closing brace.
    """
    text = scan_result.text
    unit = infer_indent_unit(text)
    nl = infer_newline(text)
    top = scan_result.top
    outer_indent = unit
    member_indent = unit * 2
    member = render_member(
        name, value, base_indent=member_indent, unit=unit, nl=nl
    )
    block = (
        json.dumps(container_key, ensure_ascii=False)
        + ": {"
        + nl
        + member_indent
        + member
        + nl
        + outer_indent
        + "}"
    )
    if top.members:
        offset = top.members[-1].value_end
        insertion = "," + nl + outer_indent + block
        return JsonEdit(text[:offset] + insertion + text[offset:], offset, offset)
    close_indent = _line_indent(text, top.close_brace)
    insertion = nl + outer_indent + block + nl + close_indent
    offset = top.open_brace + 1
    return JsonEdit(text[:offset] + insertion + text[offset:], offset, offset)


def plan_replace_member(
    scan_result: ScannedJson, container_key: str, name: str, value: Mapping[str, object]
) -> JsonEdit:
    """Rewrite only the value span of one container member."""
    text = scan_result.text
    unit = infer_indent_unit(text)
    nl = infer_newline(text)
    obj = _container_object(scan_result, container_key)
    target = _find_member(obj, name)
    base_indent = _line_indent(text, target.key_start)
    replacement = render_value(value, base_indent=base_indent, unit=unit, nl=nl)
    start = target.value_start
    end = target.value_end
    return JsonEdit(text[:start] + replacement + text[end:], start, end)


def plan_remove_member(
    scan_result: ScannedJson, container_key: str, name: str
) -> JsonEdit:
    """Delete one container member and exactly one separating comma."""
    text = scan_result.text
    obj = _container_object(scan_result, container_key)
    index = _find_member_index(obj, name)
    target = obj.members[index]
    if index + 1 < len(obj.members):
        start = target.key_start
        end = obj.members[index + 1].key_start
        return JsonEdit(text[:start] + text[end:], start, end)
    if len(obj.members) > 1:
        start = obj.members[index - 1].value_end
        end = target.value_end
        return JsonEdit(text[:start] + text[end:], start, end)
    start = obj.open_brace + 1
    end = obj.close_brace
    if "\n" not in text[start:end]:
        return JsonEdit(text[:start] + text[end:], start, end)
    close_indent = _line_indent(text, obj.close_brace)
    replacement = infer_newline(text) + close_indent
    return JsonEdit(text[:start] + replacement + text[end:], start, end)


def _container_object(scan_result: ScannedJson, container_key: str) -> JsonObjectSpan:
    member = _find_member(scan_result.top, container_key)
    if not member.value_is_object:
        raise JsonSpanError(f"container {container_key!r} is not an object")
    obj = scan_result.objects.get(member.value_start)
    if obj is None:
        raise JsonSpanError(f"container {container_key!r} span is inconsistent")
    return obj


def _find_member(obj: JsonObjectSpan, key: str) -> JsonMember:
    for member in obj.members:
        if member.key == key:
            return member
    raise JsonSpanError(f"member {key!r} is not present")


def _find_member_index(obj: JsonObjectSpan, key: str) -> int:
    for index, member in enumerate(obj.members):
        if member.key == key:
            return index
    raise JsonSpanError(f"member {key!r} is not present")


def _line_indent(text: str, pos: int) -> str:
    line_start = text.rfind("\n", 0, pos) + 1
    end = line_start
    while end < len(text) and text[end] in " \t":
        end += 1
    return text[line_start:end]


class _Parser:
    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0
        self.objects: dict[int, JsonObjectSpan] = {}

    def parse_top(self) -> JsonObjectSpan:
        self._skip_ws()
        if self._peek() != "{":
            raise JsonSpanError("top-level JSON value is not an object")
        top = self._parse_object()
        self._skip_ws()
        if self._pos != len(self._text):
            raise JsonSpanError("trailing bytes after the top-level JSON value")
        return top

    def _peek(self) -> str:
        if self._pos >= len(self._text):
            raise JsonSpanError("unexpected end of JSON input")
        return self._text[self._pos]

    def _skip_ws(self) -> None:
        text = self._text
        pos = self._pos
        while pos < len(text) and text[pos] in _WS:
            pos += 1
        self._pos = pos

    def _parse_object(self) -> JsonObjectSpan:
        text = self._text
        if text[self._pos] != "{":
            raise JsonSpanError("expected an object")
        open_brace = self._pos
        self._pos += 1
        members: list[JsonMember] = []
        self._skip_ws()
        if self._peek() == "}":
            self._pos += 1
            span = JsonObjectSpan(open_brace, self._pos - 1, ())
            self.objects[open_brace] = span
            return span
        while True:
            self._skip_ws()
            key, key_start, key_end = self._parse_string()
            self._skip_ws()
            if self._peek() != ":":
                raise JsonSpanError("expected a colon after an object key")
            self._pos += 1
            self._skip_ws()
            value_start = self._pos
            is_object = self._peek() == "{"
            value_end = self._parse_value()
            members.append(
                JsonMember(key, key_start, key_end, value_start, value_end, is_object)
            )
            self._skip_ws()
            char = self._peek()
            if char == ",":
                self._pos += 1
                continue
            if char == "}":
                self._pos += 1
                span = JsonObjectSpan(open_brace, self._pos - 1, tuple(members))
                self.objects[open_brace] = span
                return span
            raise JsonSpanError("expected a comma or closing brace in an object")

    def _parse_value(self) -> int:
        char = self._peek()
        if char == "{":
            span = self._parse_object()
            return span.close_brace + 1
        if char == "[":
            return self._parse_array()
        if char == '"':
            _, start, end = self._parse_string()
            _ = start
            return end
        if char == "t":
            return self._parse_literal("true")
        if char == "f":
            return self._parse_literal("false")
        if char == "n":
            return self._parse_literal("null")
        if char == "-" or char.isdigit():
            return self._parse_number()
        raise JsonSpanError(f"unexpected character {char!r} in JSON input")

    def _parse_array(self) -> int:
        self._pos += 1
        self._skip_ws()
        if self._peek() == "]":
            self._pos += 1
            return self._pos
        while True:
            self._skip_ws()
            self._parse_value()
            self._skip_ws()
            char = self._peek()
            if char == ",":
                self._pos += 1
                continue
            if char == "]":
                self._pos += 1
                return self._pos
            raise JsonSpanError("expected a comma or closing bracket in an array")

    def _parse_string(self) -> tuple[str, int, int]:
        text = self._text
        if text[self._pos] != '"':
            raise JsonSpanError("expected a string")
        start = self._pos
        pos = start + 1
        while True:
            if pos >= len(text):
                raise JsonSpanError("unterminated string in JSON input")
            char = text[pos]
            if char == '"':
                end = pos + 1
                self._pos = end
                return json.loads(text[start:end]), start, end
            if char == "\\":
                if pos + 1 >= len(text):
                    raise JsonSpanError("unterminated escape in JSON input")
                escaped = text[pos + 1]
                if escaped == "u":
                    digits = text[pos + 2 : pos + 6]
                    if len(digits) != 4 or any(
                        c not in "0123456789abcdefABCDEF" for c in digits
                    ):
                        raise JsonSpanError("invalid unicode escape in JSON input")
                    pos += 6
                elif escaped in '"\\/bfnrt':
                    pos += 2
                else:
                    raise JsonSpanError(f"invalid escape {escaped!r} in JSON input")
                continue
            if ord(char) < 0x20:
                raise JsonSpanError("unescaped control character in JSON input")
            pos += 1

    def _parse_literal(self, word: str) -> int:
        if self._text.startswith(word, self._pos):
            self._pos += len(word)
            return self._pos
        raise JsonSpanError(f"invalid literal in JSON input near {self._pos}")

    def _parse_number(self) -> int:
        text = self._text
        pos = self._pos
        if pos < len(text) and text[pos] == "-":
            pos += 1
        if pos >= len(text):
            raise JsonSpanError("invalid number in JSON input")
        if text[pos] == "0":
            pos += 1
        elif text[pos].isdigit():
            while pos < len(text) and text[pos].isdigit():
                pos += 1
        else:
            raise JsonSpanError("invalid number in JSON input")
        if pos < len(text) and text[pos] == ".":
            pos += 1
            digits = pos
            while pos < len(text) and text[pos].isdigit():
                pos += 1
            if pos == digits:
                raise JsonSpanError("invalid number in JSON input")
        if pos < len(text) and text[pos] in "eE":
            pos += 1
            if pos < len(text) and text[pos] in "+-":
                pos += 1
            digits = pos
            while pos < len(text) and text[pos].isdigit():
                pos += 1
            if pos == digits:
                raise JsonSpanError("invalid number in JSON input")
        self._pos = pos
        return pos
