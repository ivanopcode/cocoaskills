"""Escape untrusted values at terminal presentation boundaries only."""
from __future__ import annotations

import unicodedata


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
    filesystem paths. Octal escapes preserve C0 bytes; Unicode escapes preserve
    other control code points in UTF-8 bash/zsh locales.
    """
    import shlex

    if terminal_text(value) == value:
        return shlex.quote(value)
    parts = []
    for char in value:
        if char == "\\":
            parts.append("\\\\")
        elif char == "'":
            parts.append("\\'")
        elif terminal_text(char) != char:
            code = ord(char)
            parts.append({10: r"\n", 13: r"\r", 9: r"\t"}.get(code, f"\\{code:03o}" if code < 128 else (f"\\u{code:04x}" if code <= 0xffff else f"\\U{code:08x}")))
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

    def __init__(self, stream):
        self.stream = stream
        self.machine_json = False

    def write(self, text: str) -> int:
        self.stream.write(text if self.machine_json else terminal_document(text))
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        return getattr(self.stream, name)
