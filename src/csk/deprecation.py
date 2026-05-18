from __future__ import annotations

import sys


_WARNED: set[str] = set()


MESSAGES = {
    "fix-gitignore": (
        "--fix-gitignore: WARNING - deprecated for regular install flows\n"
        "  v0.3.0+: prefer 'csk init' once per project to set up gitignore\n"
        "  --fix-gitignore is scheduled for removal in a future release."
    ),
}


def warn_once(category: str, **values: object) -> None:
    if category in _WARNED:
        return
    message = MESSAGES[category].format(**values)
    print(message, file=sys.stderr)
    _WARNED.add(category)


def reset_for_tests() -> None:
    _WARNED.clear()
