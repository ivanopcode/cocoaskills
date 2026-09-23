"""Shared support for the filesystem-seam refusal family.

A seam is a ``pathlib``/``os`` call that performs a filesystem syscall on an
operational path (the trust store, a Skillfile, a config path, a project
path). The enumeration below derives them by inspecting the modules, so a
seam added later is found without anyone hand-listing it.

Deliberately NOT seams (stated bounds, each with its reason):

* ``resolve()``: non-strict ``Path.resolve`` performs no failing syscall
  observable as EACCES/EIO/ENAMETOOLONG/ELOOP; it swallows per-component
  errors and returns a path (proven by
  ``test_resolve_never_raises_oserror`` in the seam suite). The one
  exception is ELOOP surfacing as ``RuntimeError`` on Python <= 3.13, a
  distinct non-OSError variant hardened at the init entry only.
* ``cwd()``/``home()``/``expanduser()``/``getuser()``: process-environment
  lookups, not operations on caller-controlled seam paths.
* ``str.replace`` and friends: non-filesystem methods sharing a name are
  excluded by the arity rule below, never by a hand list of line numbers.
"""

from __future__ import annotations

import ast
import contextlib
import errno
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

import csk

SRC_ROOT = Path(csk.__file__).resolve().parent

#: Modules whose seams this family owns, as dotted names.
IN_SCOPE_MODULES: tuple[str, ...] = (
    "csk.audit.trust",
    "csk.manifest",
    "csk.cli",
)

#: ``pathlib.Path`` method names that perform a filesystem syscall.
#:
#: ``owner``/``group`` are deliberately absent: ``re.Match.group`` shares
#: the name and takes zero arguments too, so no arity rule separates them.
#: No ``Path.owner``/``Path.group`` seam exists in scope; a future one is a
#: stated bound of this enumeration, caught by review rather than by AST.
PATH_OPS: frozenset[str] = frozenset(
    {
        "exists",
        "is_dir",
        "is_file",
        "is_symlink",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "mkdir",
        "open",
        "stat",
        "lstat",
        "iterdir",
        "glob",
        "rglob",
        "unlink",
        "rename",
        "replace",
        "symlink_to",
        "hardlink_to",
        "touch",
        "chmod",
        "samefile",
        "link_to",
    }
)

#: ``os``/``os.path`` functions that perform a filesystem syscall.
OS_OPS: frozenset[str] = frozenset(
    {
        "stat",
        "lstat",
        "open",
        "listdir",
        "scandir",
        "mkdir",
        "makedirs",
        "remove",
        "unlink",
        "rename",
        "replace",
        "chmod",
    }
)

OS_PATH_OPS: frozenset[str] = frozenset({"exists", "isdir", "isfile", "islink", "lexists", "getsize"})

#: Handler names that cover OSError (plus bare ``except:``).
_OSERROR_COVERING_NAMES: frozenset[str] = frozenset({"OSError", "Exception", "BaseException"})

#: The fault-injection matrix: every seam refuses every one of these.
FAULT_ERRNOS: tuple[tuple[int, str], ...] = (
    (errno.EACCES, "EACCES"),
    (errno.EIO, "EIO"),
    (errno.ENAMETOOLONG, "ENAMETOOLONG"),
    (errno.ELOOP, "ELOOP"),
)


@dataclass(frozen=True)
class Seam:
    module: str
    func: str
    lineno: int
    op: str
    guarded: bool


def module_path(dotted: str) -> Path:
    return SRC_ROOT / (dotted.removeprefix("csk.").replace(".", "/") + ".py")


def _call_op(node: ast.Call) -> str | None:
    """Return the seam op for one call node, or None when not a seam."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id == "os" and func.attr in OS_OPS:
            return f"os.{func.attr}"
        if (
            isinstance(func.value, ast.Attribute)
            and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "os"
            and func.attr in OS_PATH_OPS
        ):
            return f"os.path.{func.attr}"
        if func.attr in PATH_OPS:
            if func.attr == "replace" and not (len(node.args) == 1 and not node.keywords):
                # str.replace(old, new) shares the name; Path.replace takes
                # exactly one target. Invalid one-argument str.replace calls
                # cannot survive a green suite, so the arity rule is sound.
                return None
            return func.attr
        return None
    if isinstance(func, ast.Name) and func.id == "open":
        return "open"
    return None


def _handler_covers_oserror(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    candidates = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(
        isinstance(item, ast.Name) and item.id in _OSERROR_COVERING_NAMES for item in candidates
    )


class _SeamVisitor(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.func_stack: list[str] = []
        self.try_stack: list[ast.Try] = []
        self.seams: list[Seam] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.func_stack.append(node.name)
        saved_try_stack = self.try_stack
        self.try_stack = []
        self.generic_visit(node)
        self.try_stack = saved_try_stack
        self.func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Try(self, node: ast.Try) -> None:
        self.try_stack.append(node)
        self.generic_visit(node)
        self.try_stack.pop()

    visit_TryStar = visit_Try

    def visit_Call(self, node: ast.Call) -> None:
        op = _call_op(node)
        if op is not None and self.func_stack:
            guarded = any(
                any(_handler_covers_oserror(handler) for handler in frame.handlers)
                for frame in self.try_stack
            )
            self.seams.append(
                Seam(
                    module=self.module,
                    func=".".join(self.func_stack),
                    lineno=node.lineno,
                    op=op,
                    guarded=guarded,
                )
            )
        self.generic_visit(node)


def enumerate_seams(dotted: str) -> list[Seam]:
    """Derive the filesystem seams of one in-scope module by inspection."""
    path = module_path(dotted)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = _SeamVisitor(dotted)
    visitor.visit(tree)
    return visitor.seams


def enumerate_all_seams() -> list[Seam]:
    seams: list[Seam] = []
    for dotted in IN_SCOPE_MODULES:
        seams.extend(enumerate_seams(dotted))
    return seams


def enumerate_source_snippet(source: str) -> list[Seam]:
    """Enumerate one synthetic snippet (checker self-test, not shipped code)."""
    tree = ast.parse(source)
    visitor = _SeamVisitor("snippet")
    visitor.visit(tree)
    return visitor.seams


#: Fault firings observed by drivers: (module, func, op, errno).
FIRED: list[tuple[str, str, str, int]] = []


def _matches(candidate: object, target: str) -> bool:
    """Lexical path equality with no I/O (safe inside patched wrappers)."""

    try:
        return os.path.normpath(os.fspath(candidate)) == target  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


@contextlib.contextmanager
def fault_at(
    monkeypatch: pytest.MonkeyPatch,
    *,
    module: str,
    func: str,
    op: str,
    target: Path,
    err: int,
) -> Iterator[list[int]]:
    """Raise ``OSError(err)`` when ``op`` touches ``target``, pass through else.

    Yields the per-case firing log; drivers assert it fired exactly once,
    proving the fault hit the intended seam and nothing else. Every firing
    is also recorded in :data:`FIRED` for the linkage test.
    """

    normalized = os.path.normpath(os.fspath(target))
    firings: list[int] = []

    def _fire() -> None:
        firings.append(err)
        FIRED.append((module, func, op, err))
        raise OSError(err, os.strerror(err))

    if op == "os.stat":
        original = os.stat

        def os_stat_fault(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if _matches(path, normalized):
                _fire()
            return original(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "stat", os_stat_fault)
    elif op in {"stat", "read_text", "read_bytes", "write_text", "mkdir"}:
        original_method = getattr(Path, op)

        def path_fault(self: Path, *args: object, **kwargs: object) -> object:
            if _matches(self, normalized):
                _fire()
            return original_method(self, *args, **kwargs)

        monkeypatch.setattr(Path, op, path_fault)
    else:  # pragma: no cover - enumerator admits no other op in scope today
        raise AssertionError(f"fault injection has no patch point for op {op!r}")
    yield firings
