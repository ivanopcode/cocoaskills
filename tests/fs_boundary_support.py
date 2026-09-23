"""Boundary support: entry-point enumeration and broad fault injection.

The invariant is "No raw OSError escapes a public entry point", tested
without knowing which calls a module makes. Entry points are derived from
the CLI command table (argparse choices at runtime) and the modules'
exported API (inspect), so a command or public function added later fails
the pin test. Faults are injected broadly at the os/io layer (every os
function derived via dir(os), plus io.open/builtins.open) when any path
argument touches the marker, rather than at named call sites.

Ordinal sweep (BUG-260921-hv5upg rev 2): ``broad_fault`` accepts ``nth``
to fail the Nth touching call for N = 1..K, letting calls 1..N-1 through,
so a seam behind an already-guarded one gets its own firing.
``count_touches`` dry-runs an entry point with no fault to observe the
touch sequence per (entry point, marker, fixture). Every breadth pair
has a sweep driver firing every (ordinal, errno) combination and
asserting the pinned per-ordinal outcome, plus a K+1 run asserting no
firing, so touching more than K is visible instead of silently
truncated. The breadth drivers themselves remain first-touch samples
(four errnos at the first touch); only the sweep drivers sweep, and the
linkage test fails a breadth pair without its sweep.

Below-swallow injection: on Python 3.14 ``Path.exists/is_dir/is_file/is_mount``
(and the ``os.path`` predicates) swallow every OSError from the underlying
``os.stat`` and return False, so an os-layer fault never reaches the boundary.
``broad_fault`` additionally wraps those predicates to raise directly when the
receiver touches the marker, with the same ordinal accounting. High-level
wrappers suspend counting of nested calls while passing through so one
logical call counts once; the dry run and the firing runs share the same
suspension accounting, so their ordinals align.

Coverage statement (measured on 3.11/3.12/3.13/3.14; BUG-260921-hv5upg rev 2).

27 of 27 (entry point, marker, fixture) pairs swept, 0 unswept: the 23
breadth pairs below each have a same-marker sweep, plus 4 branch/marker
extras (bootstrap ``--if-missing``, draft mocked, draft unmocked, parent
probe). Per pair: K, then per ordinal ``call@module`` with R = structured
refusal, C = contained non-refusal (guard degrades by design), D =
declared non-named outcome. A declared ordinal pins its outcome; it is
not skipped.

* bootstrap create (K=4): open@config load C(exit 0, draft unknown),
  stat@cli draft C(exit 0), stat@cli bootstrap R, replace@config save
  D(raw OSError).
* bootstrap --if-missing (K=1): stat@cli bootstrap R.
* init target (K=8 on 3.11/3.12, K=7 on 3.13/3.14): resolve
  internals@cli C(exit 0; on 3.11/3.12 ordinal 2 with ELOOP refuses),
  guard stat@cli R, require/present/write@manifest RRR,
  exists+write@gitignore_gate D(raw OSError x2).
* config show (K=2): open@config load C(exit 0, draft unknown),
  read@cli R.
* hybrid status (K=1): read@cli C([unreadable marker], exit 0).
* list (K=1), status --all (K=1): read@manifest R.
* list --paths (K=2): stat@cli suffix C("(unreadable)", exit 0),
  read@manifest R.
* project resolve (K=3 on 3.11/3.12, K=2 on 3.13/3.14): stat@cli R,
  resolve internals@project_resolver C(exit 0; on 3.11/3.12 ordinal 3
  with ELOOP escapes raw RuntimeError, declared).
* project add (K=3), ensure_empty (K=3), ensure_project (K=3):
  require/present/write@manifest RRR.
* add, remove, remove_decl (K=2 each): read/write@manifest RR.
* audit publish (K=1): read@cli R (dry/K+1 exit 2 on record validation).
* audit trust (K=4): trust-read/verdict-read@trust RR(unreadable),
  mkdir/write@trust RR(unwritable).
* audit allow (K=2): mkdir/write@trust RR(unwritable).
* trust record, cached verdict, load_manifest (K=1 each): read R.
* store, pin (K=3 each): mkdir/mkdir/write@trust RRR(unwritable).
* draft mocked (K=1): stat@cli draft R. draft unmocked (K=3):
  read@config load R(via dispatch ConfigError), stat@cli draft R,
  read@config load D(raw OSError: the config.py:236 bound, pinned live).
* parent probe (K=1): stat@cli parent-probe R.

Ordinal relativity (review N3): an ordinal counts touching calls from
entry start in the fixture's path, including draft/config/resolver
touches; every sweep pins the per-ordinal (call, module, func) sequence,
so a fixture change that shifts ordinals fails the pin instead of
silently reassigning expectations. Two pairs have version-split
sequences (``resolve()`` is lstat+stat on 3.11/3.12, one lstat on
3.13/3.14); both branches are pinned from measurement.

Errno by ordinal (review N4): every (ordinal, errno) combination is
exercised (EACCES/EIO/ENAMETOOLONG/ELOOP at every ordinal of every
sweep); no combination is assumed. The matrix found two errno-sensitive
ordinals, both pinned per combination: init resolve-stat ELOOP refuses
on 3.11/3.12, and project-resolve hash-stat ELOOP escapes raw
``RuntimeError`` (``realpath`` loop mapping) on 3.11/3.12.

Glob (review N2, measured per version): a glob resolving to the marker
is visible except where noted, and a fault there is observable (raw
raise or a listing that differs from the unfaulted one), so an
unguarded glob seam on a marked path fails its sweep. Blind spots,
each pinned by a test: a glob matching unmarked entries only (all
versions, no touching call); ``dir.glob`` on 3.13
(``glob._Globber.scandir`` captures ``os.scandir`` at import time);
``parent.glob(name)`` on 3.12/3.13 (no candidate stat).

Per-module reach: ``csk.audit.trust`` all seams swept via the trust and
audit pairs; ``csk.manifest`` all seams swept via the manifest and CLI
pairs; ``csk.cli`` covered-leaf seams swept via the CLI pairs, and the
25 out-of-scope leaves census-pinned (each mapped handler holds no
``cli.py`` seam; the mapping is verified against the dispatch branches,
and ``()`` leaves reach no ``_cmd_*``). ``csk.config`` and every other
non-named module are out of contract per AC (a) "in the named modules";
their ordinals appear above as C/D with pinned outcomes, never as
refusals owed.

Still NOT exercised: seams on paths no driver marks (position C);
flag/option variants inside covered leaves that take a different
filesystem path (``status`` with a target or ``--attest``,
``add``/``remove`` via resolver path search, ``audit --all/--global``);
import-time bindings captured before the patch (package idiom census:
0 such sites; 3.13 ``glob._Globber.scandir`` is the measured instance)
and non-``os`` syscallers (``io.FileIO``, fd-relative ``dir_fd``,
``DirEntry`` methods); predicate swallow on interpreters other than the
measured 3.11/3.12/3.13/3.14 set.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import inspect
import io
import os
import traceback
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from csk import cli

#: Pure path-string conversions with no syscall (verified against CPython
#: source: os.fspath/fsencode/fsdecode perform no I/O). Excluded from
#: broad patching so pure path manipulation on the marker never faults.
#: Everything else in os is wrapped; non-path calls pass through.
_PURE_OS_NAMES: frozenset[str] = frozenset({"fspath", "_fspath", "fsencode", "fsdecode"})


def _os_patch_names() -> list[str]:
    """Derive the os functions to wrap (dir(os), pure-string ops excluded)."""
    names: list[str] = []
    for name in dir(os):
        if name in _PURE_OS_NAMES:
            continue
        try:
            obj = getattr(os, name)
        except AttributeError:
            continue
        if isinstance(obj, (types.BuiltinFunctionType, types.FunctionType)):
            names.append(name)
    return sorted(names)


OS_PATCH_NAMES: tuple[str, ...] = tuple(_os_patch_names())

#: Path predicates that swallow OSError from the underlying stat on 3.14 and
#: return False instead of reaching the boundary. Wrapped to raise directly
#: when the receiver touches the marker (below-swallow injection).
PATH_PREDICATE_NAMES: tuple[str, ...] = (
    "exists",
    "is_dir",
    "is_file",
    "is_symlink",
    "is_mount",
    "is_junction",
)

#: os.path predicates with the same swallow shape. Wrapped to raise directly.
OS_PATH_PREDICATE_NAMES: tuple[str, ...] = (
    "exists",
    "isdir",
    "isfile",
    "islink",
    "lexists",
    "getsize",
    "ismount",
    "isjunction",
)

#: Modules whose seams the boundary family owns (the same set as the seam
#: family's ``IN_SCOPE_MODULES``, restated so this module stays importable
#: without the seam support). A touch whose innermost ``csk`` frame is in
#: one of these must produce a structured refusal; touches elsewhere are
#: declared per ordinal in the sweep driver, never silently skipped.
NAMED_MODULES: frozenset[str] = frozenset(
    {
        "csk.audit.trust",
        "csk.manifest",
        "csk.cli",
    }
)


class Touch(str):
    """One touching call observed by :func:`count_touches`, with its site.

    Behaves as the call name (``"os.stat"``, ``"Path.exists"``), and
    carries the innermost ``csk`` production frame that issued the call:
    :attr:`module` (dotted, ``""`` when no ``csk`` frame is on the stack),
    :attr:`func`, and :attr:`lineno`. :attr:`site` renders the triple for
    pins (``"csk.cli:_cmd_init:1187"`` or ``"<no csk frame>"``).
    """

    module: str
    func: str
    lineno: int

    def __new__(cls, name: str, *, module: str = "", func: str = "", lineno: int = 0) -> "Touch":
        self = super().__new__(cls, name)
        self.module = module
        self.func = func
        self.lineno = lineno
        return self

    @property
    def site(self) -> str:
        if not self.module:
            return "<no csk frame>"
        return f"{self.module}:{self.func}:{self.lineno}"

    @property
    def is_named(self) -> bool:
        return self.module in NAMED_MODULES


def _dotted_csk_module(filename: str) -> str:
    """Map a frame filename to its dotted ``csk`` module, or ``""``."""
    parts = filename.split(os.sep)
    if "csk" not in parts:
        return ""
    idx = len(parts) - 1 - parts[::-1].index("csk")
    leaf = parts[-1]
    if not leaf.endswith(".py"):
        return ""
    return ".".join([*parts[idx:-1], leaf[:-3]])


def _innermost_csk_frame() -> tuple[str, str, int]:
    """Innermost ``csk`` frame of the current stack as (module, func, lineno).

    ``("", "", 0)`` when the call comes from the harness itself (a
    synthetic entry or a predicate self-test) rather than production.
    """
    for frame in reversed(traceback.extract_stack()):
        module = _dotted_csk_module(frame.filename)
        if module:
            return module, frame.name, frame.lineno or 0
    return "", "", 0


def enumerate_cli_leaves(*, draft: bool) -> set[tuple[str, ...]]:
    """Derive every CLI leaf command from the built parser's choices."""
    parser = cli.build_parser(draft=draft)
    leaves: set[tuple[str, ...]] = set()

    def walk(sub: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        found = False
        for action in sub._actions:
            if isinstance(action, argparse._SubParsersAction):
                found = True
                for name, child in action.choices.items():
                    walk(child, prefix + (name,))
        if not found and prefix:
            leaves.add(prefix)

    walk(parser, ())
    return leaves


def enumerate_public_api(module: types.ModuleType) -> set[str]:
    """Derive public functions defined in one module (inspect, not listed)."""
    return {
        name
        for name, func in inspect.getmembers(module, inspect.isfunction)
        if not name.startswith("_") and func.__module__ == module.__name__
    }


def _arg_touches(value: object, norm_target: str) -> bool:
    try:
        raw = os.fspath(value)  # type: ignore[arg-type]
    except TypeError:
        return False
    if isinstance(raw, bytes):
        try:
            raw = os.fsdecode(raw)
        except (UnicodeDecodeError, ValueError):
            return False
    if not isinstance(raw, str):
        return False
    try:
        norm = os.path.normpath(raw)
    except (TypeError, ValueError):
        return False
    return norm == norm_target or norm.startswith(norm_target + os.sep)


def _call_touches(args: tuple[object, ...], kwargs: dict[str, object], norm_target: str) -> bool:
    for value in args:
        if _arg_touches(value, norm_target):
            return True
    for value in kwargs.values():
        if _arg_touches(value, norm_target):
            return True
    return False


@contextlib.contextmanager
def broad_fault(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: Path,
    err: int,
    nth: int | None = None,
) -> Iterator[list[tuple[str, int]]]:
    """Raise OSError(err) from a touching call, else pass through.

    With ``nth=None`` (default) every touching call raises, so the entry
    executes exactly its first touching seam: the historical first-touch
    sample, kept for the breadth drivers. With ``nth=N`` (1-indexed) only
    the Nth touching call raises and calls 1..N-1 pass through, so each
    ordinal gets its own firing.

    Yields the firing log; drivers assert it is non-empty, proving the
    fault hit the entry point's path and the test is not vacuous. Sweep
    drivers assert exactly one firing at the requested ordinal.
    """
    if nth is not None and nth < 1:
        raise ValueError(f"nth must be >= 1, got {nth!r}")
    norm_target = os.path.normpath(os.fspath(target))
    firings: list[tuple[str, int]] = []
    state = {"touches": 0, "suspended": False}

    def _should_fire(name: str) -> bool:
        if state["suspended"]:
            return False
        state["touches"] += 1
        if nth is None or state["touches"] == nth:
            firings.append((name, err))
            return True
        return False

    def _fire() -> None:
        raise OSError(err, os.strerror(err))

    for name in OS_PATCH_NAMES:
        original = getattr(os, name, None)
        if original is None:
            continue

        def _wrapper(
            *args: object,
            __original: object = original,
            __name: str = name,
            **kwargs: object,
        ) -> object:
            if state["suspended"]:
                func = __original
                assert callable(func)
                return func(*args, **kwargs)
            if _call_touches(args, kwargs, norm_target):
                if _should_fire(f"os.{__name}"):
                    _fire()
            func = __original
            assert callable(func)
            return func(*args, **kwargs)

        monkeypatch.setattr(os, name, _wrapper)

    original_io_open = io.open
    original_builtin_open = builtins.open

    def _io_open_wrapper(file: object, *args: object, **kwargs: object) -> object:
        if _arg_touches(file, norm_target):
            if _should_fire("io.open"):
                _fire()
            state["suspended"] = True
            try:
                return original_io_open(file, *args, **kwargs)  # type: ignore[arg-type]
            finally:
                state["suspended"] = False
        return original_io_open(file, *args, **kwargs)  # type: ignore[arg-type]

    def _builtin_open_wrapper(file: object, *args: object, **kwargs: object) -> object:
        if _arg_touches(file, norm_target):
            if _should_fire("builtins.open"):
                _fire()
            state["suspended"] = True
            try:
                return original_builtin_open(file, *args, **kwargs)  # type: ignore[arg-type]
            finally:
                state["suspended"] = False
        return original_builtin_open(file, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(io, "open", _io_open_wrapper)
    monkeypatch.setattr(builtins, "open", _builtin_open_wrapper)

    # Below-swallow predicates: Path.exists/is_* and os.path.* swallow the
    # os-layer fault on 3.14, so raise directly at the predicate instead.
    for pred in PATH_PREDICATE_NAMES:
        original_pred = getattr(Path, pred, None)
        if original_pred is None:
            continue

        def _pred_wrapper(
            self: Path,
            *args: object,
            __original: object = original_pred,
            __pred: str = pred,
            **kwargs: object,
        ) -> object:
            if _arg_touches(self, norm_target):
                if _should_fire(f"Path.{__pred}"):
                    _fire()
                state["suspended"] = True
                try:
                    func = __original
                    assert callable(func)
                    return func(self, *args, **kwargs)
                finally:
                    state["suspended"] = False
            func = __original
            assert callable(func)
            return func(self, *args, **kwargs)

        monkeypatch.setattr(Path, pred, _pred_wrapper)

    for pred in OS_PATH_PREDICATE_NAMES:
        original_pred = getattr(os.path, pred, None)
        if original_pred is None or not callable(original_pred):
            continue

        def _os_path_wrapper(
            *args: object,
            __original: object = original_pred,
            __pred: str = pred,
            **kwargs: object,
        ) -> object:
            if _call_touches(args, kwargs, norm_target):
                if _should_fire(f"os.path.{__pred}"):
                    _fire()
                state["suspended"] = True
                try:
                    func = __original
                    assert callable(func)
                    return func(*args, **kwargs)
                finally:
                    state["suspended"] = False
            func = __original
            assert callable(func)
            return func(*args, **kwargs)

        monkeypatch.setattr(os.path, pred, _os_path_wrapper)

    yield firings


@contextlib.contextmanager
def count_touches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: Path,
) -> Iterator[list[Touch]]:
    """Dry-run helper: record every touching call without faulting.

    Yields the touch log; its length is K for the sweep over this
    (entry point, marker). Each entry is a :class:`Touch`: the call
    name plus the innermost ``csk`` frame that issued it, so the sweep
    driver can pin which ordinals land in named modules and which are
    declared non-named. Uses the same patch points and the same
    suspension accounting as :func:`broad_fault`, so the count and the
    order match the ordinals the sweep will fire at: a firing run with
    ``nth=k`` passes calls 1..k-1 through untouched, so its k-th touch
    is the dry run's k-th touch.
    """
    norm_target = os.path.normpath(os.fspath(target))
    touches: list[Touch] = []
    state = {"suspended": False}

    def _record(name: str) -> None:
        module, func, lineno = _innermost_csk_frame()
        touches.append(Touch(name, module=module, func=func, lineno=lineno))

    for name in OS_PATCH_NAMES:
        original = getattr(os, name, None)
        if original is None:
            continue

        def _wrapper(
            *args: object,
            __original: object = original,
            __name: str = name,
            **kwargs: object,
        ) -> object:
            if not state["suspended"] and _call_touches(args, kwargs, norm_target):
                _record(f"os.{__name}")
            func = __original
            assert callable(func)
            return func(*args, **kwargs)

        monkeypatch.setattr(os, name, _wrapper)

    original_io_open = io.open
    original_builtin_open = builtins.open

    def _io_open_wrapper(file: object, *args: object, **kwargs: object) -> object:
        if _arg_touches(file, norm_target):
            if not state["suspended"]:
                _record("io.open")
            state["suspended"] = True
            try:
                return original_io_open(file, *args, **kwargs)  # type: ignore[arg-type]
            finally:
                state["suspended"] = False
        return original_io_open(file, *args, **kwargs)  # type: ignore[arg-type]

    def _builtin_open_wrapper(file: object, *args: object, **kwargs: object) -> object:
        if _arg_touches(file, norm_target):
            if not state["suspended"]:
                _record("builtins.open")
            state["suspended"] = True
            try:
                return original_builtin_open(file, *args, **kwargs)  # type: ignore[arg-type]
            finally:
                state["suspended"] = False
        return original_builtin_open(file, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(io, "open", _io_open_wrapper)
    monkeypatch.setattr(builtins, "open", _builtin_open_wrapper)

    for pred in PATH_PREDICATE_NAMES:
        original_pred = getattr(Path, pred, None)
        if original_pred is None:
            continue

        def _pred_wrapper(
            self: Path,
            *args: object,
            __original: object = original_pred,
            __pred: str = pred,
            **kwargs: object,
        ) -> object:
            if _arg_touches(self, norm_target):
                if not state["suspended"]:
                    _record(f"Path.{__pred}")
                state["suspended"] = True
                try:
                    func = __original
                    assert callable(func)
                    return func(self, *args, **kwargs)
                finally:
                    state["suspended"] = False
            func = __original
            assert callable(func)
            return func(self, *args, **kwargs)

        monkeypatch.setattr(Path, pred, _pred_wrapper)

    for pred in OS_PATH_PREDICATE_NAMES:
        original_pred = getattr(os.path, pred, None)
        if original_pred is None or not callable(original_pred):
            continue

        def _os_path_wrapper_fixed(
            *args: object,
            __original: object = original_pred,
            __pred: str = pred,
            **kwargs: object,
        ) -> object:
            if _call_touches(args, kwargs, norm_target):
                if not state["suspended"]:
                    _record(f"os.path.{__pred}")
                state["suspended"] = True
                try:
                    func = __original
                    assert callable(func)
                    return func(*args, **kwargs)
                finally:
                    state["suspended"] = False
            func = __original
            assert callable(func)
            return func(*args, **kwargs)

        monkeypatch.setattr(os.path, pred, _os_path_wrapper_fixed)

    yield touches
