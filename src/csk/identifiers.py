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
