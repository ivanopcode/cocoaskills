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
        if unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} or unicodedata.bidirectional(char) in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}:
            result.append({10: r"\n", 13: r"\r", 9: r"\t"}.get(
                code, f"\\x{code:02x}" if code < 256 else f"\\u{code:04x}"
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
            parts.append({10: r"\n", 13: r"\r", 9: r"\t"}.get(code, f"\\{code:03o}" if code < 128 else f"\\u{code:04x}"))
        else:
            parts.append(char)
    return "$'" + "".join(parts) + "'"
