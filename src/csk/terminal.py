"""Escape untrusted values at terminal presentation boundaries only."""
from __future__ import annotations

import unicodedata
import traceback
from collections.abc import Iterable
from typing import Any, TextIO


def terminal_text(value: object) -> str:
    """Keep ordinary Unicode; render controls and line separators visibly.

    Apply to data before adding trusted layout, never to machine payloads.
    Backslashes remain unchanged, making repeated display escaping harmless.
    """
    result = []
    for char in str(value):
        code = ord(char)
        if char not in "\u200c\u200d" and (unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} or unicodedata.bidirectional(char) in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}):
            result.append({10: r"\n", 13: r"\r", 9: r"\t"}.get(
                code, f"\\x{code:02x}" if code < 256 else (f"\\u{code:04x}" if code <= 0xffff else f"\\U{code:08x}")
            ))
        else:
            result.append(char)
    return "".join(result)


def shell_word(value: str) -> str:
    """Copyable bash/zsh word; preserve shlex output when no controls occur.

    ANSI-C quoting is not promised for POSIX sh or fish. NUL cannot occur in
    filesystem paths. Octal escapes preserve filesystem bytes (including surrogateescaped names)
    even in bash 3.2. Apostrophes use octal to avoid its history-expansion bug.
    """
    import shlex
    import os

    if terminal_text(value) == value:
        return shlex.quote(value)
    parts = []
    for char in value:
        if char == "\\":
            parts.append("\\\\")
        elif char == "'":
            parts.append(r"\047")
        elif terminal_text(char) != char:
            code = ord(char)
            parts.append({10: r"\n", 13: r"\r", 9: r"\t"}.get(
                code, "".join(f"\\{byte:03o}" for byte in os.fsencode(char))
            ))
        else:
            parts.append(char)
    return "$'" + "".join(parts) + "'"


def terminal_document(value: str) -> str:
    """Escape document characters while retaining owned JSON whitespace layout."""
    return "".join(char if char in "\n\t" else terminal_text(char) for char in value)


class TerminalWriter:
    """Text-only safety net; source renderers must escape untrusted newlines.

    JSON stdout can opt out after successful argument parsing. Stderr never
    opts out. Delegate stream capabilities (isatty, encoding, flush) unchanged.
    """

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.machine_json = False

    def write(self, text: str) -> int:
        self.stream.write(text if self.machine_json else terminal_document(text))
        return len(text)

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


def exception_text(exc: BaseException) -> str:
    """Render traceback layout separately from exception, note and frame data."""
    lines: list[str] = []
    seen: set[int] = set()

    def render(error: BaseException) -> None:
        if id(error) in seen:
            return
        seen.add(id(error))
        cause = error.__cause__
        context = error.__context__
        if cause is not None:
            render(cause)
            lines.append("\nThe above exception was the direct cause of the following exception:\n")
        elif context is not None and not error.__suppress_context__:
            render(context)
            lines.append("\nDuring handling of the above exception, another exception occurred:\n")
        if error.__traceback__ is not None:
            lines.append("Traceback (most recent call last):")
            for frame in traceback.extract_tb(error.__traceback__):
                lines.append(f'  File "{terminal_text(frame.filename)}", line {frame.lineno}, in {terminal_text(frame.name)}')
                if frame.line:
                    lines.append("    " + terminal_text(frame.line))
        lines.append(f"{terminal_text(type(error).__qualname__)}: {terminal_text(error)}")
        for note in getattr(error, "__notes__", ()):
            lines.append(terminal_text(note))
        if isinstance(error, BaseExceptionGroup):
            for index, child in enumerate(error.exceptions, 1):
                lines.append(f"  Exception group member {index}:")
                render(child)

    render(exc)
    return "\n".join(lines)
