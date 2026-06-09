from __future__ import annotations

import re

# Skill names, source directory names, and command names become single
# filesystem path components (runtime dirs, shim filenames). Restrict them to
# a safe identifier alphabet so a third-party csk-skill.json can never write
# outside its designated directories.
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

IDENTIFIER_RULE = (
    "must start with a letter or digit and contain only letters, digits, "
    "dots, underscores, or hyphens"
)


def is_valid_identifier(value: str) -> bool:
    return bool(IDENTIFIER_RE.match(value))


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
