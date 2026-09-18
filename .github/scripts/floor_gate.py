"""Floor syntax gate: resolve the floor, prove the interpreter IS it, then compile.

The ``floor_syntax`` job in ``.github/workflows/ci.yml`` runs this file as
its compile step's only command::

    run: python -I .github/scripts/floor_gate.py

One process does everything, so the check and the compile have no "between":
there is no second command whose status could replace the check's, no point
at which a PATH change could swap the compiling interpreter, and no
dependence on shell ``-e`` semantics. This script's exit status IS the step
status (TASK-260918-16r0fm closes BUG-260917-3txerf rev-2 finding F3).

The floor is resolved from ``pyproject.toml`` ``requires-python`` inside
this process, through the shared resolver (``floor_resolve.py`` in this
directory), never trusted from the environment: ``FLOOR`` is only a
cross-check that must equal the resolved floor. A job that resolves,
installs, and reports the wrong interpreter cannot satisfy this gate by
construction (revision-1 finding ``floor-source-self-agreeing``).

Each target is proved present by a named sentinel read before compiling,
and every directory under each target is proved listable by a walk that
fails closed on any listing error, because ``compileall`` treats a target
it cannot list as empty and exits 0 -- a relocated checkout, a sparse
checkout, a moved package, or an unlistable directory would otherwise pass
a gate that proves nothing (rev-2 finding F4 and its read-failure
residual). Compilation goes through :func:`compileall.compile_dir` with its
return value checked, after the walk has proved every directory readable.

Isolated mode (``-I``) keeps this process immune to inherited ``PYTHON*``
variables, the user site, and modules smuggled next to this file: the
script directory is not on ``sys.path``, so the resolver is loaded by file
location below, never by import.

Exit codes: 0 only when the resolved floor equals both ``FLOOR`` and the
running interpreter, and every target is present, listable, and compiling.
Any failure exits 1 with the reason on stderr.
"""

from __future__ import annotations

import compileall
import importlib.util
import os
import sys
from pathlib import Path

TARGETS = {
    "src/csk": "src/csk/__init__.py",
    "tests": "tests/conftest.py",
}

_RESOLVER_PATH = Path(__file__).with_name("floor_resolve.py")


def _resolve_floor_from_pyproject() -> str:
    """Resolve the floor from ./pyproject.toml via the shared resolver.

    Loaded by file location: under ``python -I`` the script directory is
    not on ``sys.path`` (that absence is what defeats import hijack), so a
    plain ``import floor_resolve`` cannot work here.
    """
    spec = importlib.util.spec_from_file_location(
        "floor_resolve", os.fspath(_RESOLVER_PATH)
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"floor resolver missing: {_RESOLVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        with open("pyproject.toml", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise SystemExit(f"cannot read pyproject.toml: {exc}")
    try:
        return module.resolve_floor(text)
    except ValueError as exc:
        raise SystemExit(str(exc))


def _fail_on_list_error(exc: OSError) -> None:
    raise SystemExit(f"compile target unlistable: {exc.filename or exc}")


def main() -> int:
    floor = os.environ.get("FLOOR", "")
    resolved = _resolve_floor_from_pyproject()
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    if not floor:
        raise SystemExit("FLOOR is empty: floor resolution did not run")
    if floor != resolved:
        raise SystemExit(f"floor mismatch: FLOOR={floor!r} resolved={resolved!r}")
    if resolved != running:
        raise SystemExit(f"floor mismatch: resolved={resolved!r} running={running!r}")
    for target, sentinel in TARGETS.items():
        if not os.path.isdir(target) or not os.path.isfile(sentinel):
            raise SystemExit(
                f"compile target missing: {target!r} (sentinel {sentinel!r})"
            )
        # Prove listability before compiling: compileall reports an
        # unlistable directory as "Can't list" and treats it as empty, so
        # the walk below must fail closed on any listing error first.
        for _root, _dirs, _files in os.walk(target, onerror=_fail_on_list_error):
            pass
        try:
            with open(sentinel, "rb") as handle:
                handle.read()
        except OSError as exc:
            raise SystemExit(f"compile target unreadable: {sentinel!r} ({exc})")
    # Every target compiles before any decision: a short-circuiting `all()`
    # would leave later targets uncompiled (state unknown) after an early
    # failure, so collect per-target results first, then decide.
    failed = [
        target
        for target in TARGETS
        if not compileall.compile_dir(target, quiet=1)
    ]
    if failed:
        raise SystemExit(f"floor compile failed: {', '.join(failed)}")
    print(f"floor_compiled={running} targets={sorted(TARGETS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
