"""Single source of truth for the floor Python version.

The ``floor_syntax`` job's resolve step executes this file::

    run: python3 .github/scripts/floor_resolve.py

and ``floor_gate.py`` resolves through :func:`resolve_floor` from this same
file, so the value that selects the interpreter and the value the gate
checks against the running interpreter cannot drift apart (revision-1
finding ``floor-source-self-agreeing``: the gate used to compare ``FLOOR``
against the interpreter ``FLOOR`` itself selected, satisfied by construction
whenever the resolve output was wrong). ``FLOOR`` survives only as a
cross-check inside the gate.

The floor is the ``>=`` lower bound of the PEP 621 ``[project]``
``requires-python``, resolved with a plain regex so this step runs on any
preinstalled ``python3`` (no ``tomllib`` needed). Anything else -- a missing
file, a missing bound, a non-``>=`` specifier -- fails closed, so raising
the floor moves the gate with it and rewriting the specifier shape cannot
silently disarm it. The read is scoped to ``[project]``: a
``requires-python`` line under any other table is ignored.

On success the script appends ``version=<major.minor>`` to the
``$GITHUB_OUTPUT`` file itself, so the step's ``run`` value carries no shell
at all -- no heredoc, no redirect, no text around which an extra line, a
pipe, or an echo could hide.
"""

from __future__ import annotations

import os
import re

_PROJECT_TABLE = re.compile(r"(?ms)^\[project\][^\n]*\n(.*?)(?=^\[.*\]|\Z)")
_FLOOR_BOUND = re.compile(
    r'^\s*requires-python\s*=\s*"[^"]*?>=\s*(\d+)\.(\d+)', re.MULTILINE
)


def resolve_floor(text: str) -> str:
    """Resolve ``major.minor`` from ``pyproject.toml`` text, or fail closed."""
    project = _PROJECT_TABLE.search(text)
    section = project.group(1) if project is not None else ""
    match = _FLOOR_BOUND.search(section)
    if match is None:
        raise ValueError(
            "cannot resolve a >= floor from requires-python in pyproject.toml"
        )
    return f"{match.group(1)}.{match.group(2)}"


def main() -> int:
    try:
        with open("pyproject.toml", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise SystemExit(f"cannot read pyproject.toml: {exc}")
    try:
        version = resolve_floor(text)
    except ValueError as exc:
        raise SystemExit(str(exc))
    output = os.environ.get("GITHUB_OUTPUT", "")
    if not output:
        raise SystemExit("GITHUB_OUTPUT is empty: resolve step has no output file")
    with open(output, "a", encoding="utf-8") as handle:
        handle.write(f"version={version}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
