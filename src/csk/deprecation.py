from __future__ import annotations

import sys


_WARNED: set[str] = set()


MESSAGES = {
    "bare-install": (
        "csk install: WARNING - semantics will change in v0.3.0\n"
        "  v0.3.0: 'csk install' without arguments installs only the current project\n"
        "  v0.3.0: for multi-project sync, use 'csk install --all'\n"
        "  This run uses legacy behavior over {count} configured projects."
    ),
    "bare-status": (
        "csk status: WARNING - semantics will change in v0.3.0\n"
        "  v0.3.0: 'csk status' without arguments shows only the current project\n"
        "  v0.3.0: for multi-project status, use 'csk status --all'\n"
        "  This run uses legacy behavior over {count} configured projects."
    ),
    "bare-upgrade": (
        "csk upgrade: WARNING - semantics will change in v0.3.0\n"
        "  v0.3.0: 'csk upgrade' without arguments updates skills and installs only the current project\n"
        "  v0.3.0: for multi-project sync, use 'csk upgrade --all'\n"
        "  This run uses legacy behavior over {count} configured projects."
    ),
    "auto-register": (
        "csk install .: WARNING - auto-register will be removed in v0.3.0\n"
        "  v0.3.0: this command no longer modifies ~/.cocoaskills/config.json\n"
        "  To pre-register this checkout for --all, run: csk project add <alias> <path>"
    ),
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
