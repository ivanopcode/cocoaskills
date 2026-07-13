from __future__ import annotations

import re
import unicodedata

# Skill names, source directory names, and command names become single
# filesystem path components (runtime dirs, shim filenames). Restrict them to
# a safe identifier alphabet so a third-party csk-skill.json can never write
# outside its designated directories.
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LOCALE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?$")

IDENTIFIER_RULE = (
    "must start with a letter or digit and contain only letters, digits, "
    "dots, underscores, or hyphens, be at most 128 characters, and be a "
    "portable filename"
)

_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def is_valid_identifier(value: str) -> bool:
    return len(value) <= 128 and bool(IDENTIFIER_RE.fullmatch(value)) and is_portable_component(value)


def is_valid_locale(value: str) -> bool:
    """Validate the protocol's safe BCP 47-compatible locale surface."""
    return len(value) <= 64 and LOCALE_RE.fullmatch(value) is not None


def is_portable_component(value: str) -> bool:
    """Return whether one path component is portable across supported hosts."""
    if (
        not value
        or value in {".", ".."}
        or value.endswith((" ", "."))
        or any(separator in value for separator in (":", "/", "\\"))
    ):
        return False
    if any(unicodedata.category(character) == "Cc" for character in value):
        return False
    basename = value.split(".", 1)[0].casefold()
    return basename not in _WINDOWS_RESERVED


def is_valid_portable_path(value: str) -> bool:
    """Validate a protocol relative path without normalizing its scalars."""
    if not value or len(value) > 4096 or value.startswith("/") or "\\" in value:
        return False
    parts = value.split("/")
    return all(is_portable_component(part) for part in parts)


# A source may be a nested directory under skills_root (for example
# "internal/skill-metrics"), so it is a POSIX-style relative path whose every
# segment is a safe identifier. That still rules out "..", absolute paths,
# backslashes, and option-like segments.
SOURCE_RULE = (
    "must be a relative path whose segments start with a letter or digit and "
    "contain only letters, digits, dots, underscores, or hyphens"
)


def is_valid_source_path(value: str) -> bool:
    if not value:
        return False
    return all(is_valid_identifier(segment) for segment in value.split("/"))
