"""Schema-8 first-party module roots for the local ``go-v1`` driver.

Protocol Core section 4.2.3 lets one schema-8 build command declare the
first-party Go modules its build root replaces. The package states a claim and
the manager checks it: nothing here reads a replacement as an instruction,
discovers a directory, or lets package data select which paths are trusted.

The module owns three separable steps so that the manager profile's fixed order
survives refactoring:

1. :func:`validate_declaration` runs against the frozen snapshot alone and
   completes before the fixed ``go list``.
2. :func:`parse_effective_replacements` reads ``<build root>/vendor/modules.txt``
   after ``go list`` returns. That file is the only surface the effective
   replace set is read from.
3. :func:`resolve_bijection` checks the one-to-one correspondence between the
   declared directories and the effective directives, and returns the module
   paths whose vendor copies may carry a replacement.
"""

from __future__ import annotations

import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from ..identifiers import is_valid_portable_path


CODE_DECLARATION_INVALID: Final = "build_module_root_declaration_invalid"
CODE_CONTAINMENT_INVALID: Final = "build_module_root_containment_invalid"
CODE_DIRECTIVE_FORM_UNSUPPORTED: Final = "build_module_root_directive_form_unsupported"
CODE_DIRECTIVE_UNDECLARED: Final = "build_module_root_directive_undeclared"
CODE_DECLARATION_UNUSED: Final = "build_module_root_declaration_unused"

MODULE_ROOT_DIAGNOSTICS: Final[frozenset[str]] = frozenset(
    {
        CODE_DECLARATION_INVALID,
        CODE_CONTAINMENT_INVALID,
        CODE_DIRECTIVE_FORM_UNSUPPORTED,
        CODE_DIRECTIVE_UNDECLARED,
        CODE_DECLARATION_UNUSED,
    }
)

_ANNOTATION_PREFIX: Final = "# "
_ANNOTATION_ARROW: Final = " => "
_MODULES_TXT: Final = "vendor/modules.txt"


class ModuleRootError(Exception):
    """One stable ``phase: preflight`` module-root diagnostic."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Replacement:
    """One effective, unversioned-left replacement directive."""

    module_path: str
    target: str


def platform_path_key(value: str) -> str:
    """Fold one protocol path the way a case-insensitive host would map it."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", value).casefold())


def validate_declaration(
    snapshot: Path,
    modules: tuple[str, ...],
    *,
    build_root: str,
    build_roots: tuple[str, ...],
    runtime_roots: tuple[str, ...],
    label: str,
) -> None:
    """Validate a declared module list against the frozen snapshot alone.

    ``Module.Replace.Dir`` and ``Module.Replace.GoMod`` are never evidence that
    a path exists, so this step never looks at a ``go list`` stream.
    """

    seen: set[str] = set()
    for index, value in enumerate(modules):
        field = f"{label}.modules[{index}]"
        if not isinstance(value, str) or not value:
            raise ModuleRootError(
                CODE_DECLARATION_INVALID,
                f"{field} must be a non-empty portable relative path",
            )
        if value == "." or not is_valid_portable_path(value):
            raise ModuleRootError(
                CODE_DECLARATION_INVALID,
                f"{field} must be a portable relative path other than '.': {value!r}",
            )
        if PurePosixPath(value).as_posix() != value or ".." in PurePosixPath(value).parts:
            raise ModuleRootError(
                CODE_DECLARATION_INVALID,
                f"{field} must be a normalized relative path: {value!r}",
            )
        if value in seen:
            raise ModuleRootError(
                CODE_DECLARATION_INVALID,
                f"{field} declares a duplicate module directory: {value!r}",
            )
        seen.add(value)
        _require_link_free_directory(snapshot, value, field=field)
        _require_direct_go_mod(snapshot, value, field=field)

    _reject_overlaps(modules, build_root, build_roots, runtime_roots)


def _require_link_free_directory(snapshot: Path, relative: str, *, field: str) -> None:
    current = snapshot
    for component in PurePosixPath(relative).parts:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise ModuleRootError(
                CODE_DECLARATION_INVALID,
                f"{field} module directory does not exist: {relative}",
            ) from exc
        except OSError as exc:
            raise ModuleRootError(
                CODE_DECLARATION_INVALID,
                f"{field} cannot inspect module directory {relative}: {exc}",
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise ModuleRootError(
                CODE_DECLARATION_INVALID,
                f"{field} module directory must be link-free: {relative}",
            )
        if not stat.S_ISDIR(info.st_mode):
            raise ModuleRootError(
                CODE_DECLARATION_INVALID,
                f"{field} module directory must be a directory: {relative}",
            )


def _require_direct_go_mod(snapshot: Path, relative: str, *, field: str) -> None:
    go_mod = snapshot.joinpath(*PurePosixPath(relative).parts, "go.mod")
    try:
        info = go_mod.lstat()
    except FileNotFoundError as exc:
        raise ModuleRootError(
            CODE_DECLARATION_INVALID,
            f"{field} module directory has no go.mod directly inside it: {relative}",
        ) from exc
    except OSError as exc:
        raise ModuleRootError(
            CODE_DECLARATION_INVALID,
            f"{field} cannot inspect {relative}/go.mod: {exc}",
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ModuleRootError(
            CODE_DECLARATION_INVALID,
            f"{field} module go.mod must be a real regular file: {relative}/go.mod",
        )


def _reject_overlaps(
    modules: tuple[str, ...],
    build_root: str,
    build_roots: tuple[str, ...],
    runtime_roots: tuple[str, ...],
) -> None:
    others: list[tuple[str, str]] = [("build root", root) for root in dict.fromkeys((build_root, *build_roots))]
    others.extend(("runtime root", root) for root in runtime_roots)
    ordered = sorted(modules)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            _reject_pair("declared module directory", left, "declared module directory", right)
        for noun, root in others:
            _reject_pair("declared module directory", left, noun, root)


def _reject_pair(left_noun: str, left: str, right_noun: str, right: str) -> None:
    if not _overlaps(left, right):
        return
    raise ModuleRootError(
        CODE_CONTAINMENT_INVALID,
        f"{left_noun} {left!r} overlaps {right_noun} {right!r}",
    )


def _overlaps(left: str, right: str) -> bool:
    return _contains(left, right) or _contains(right, left)


def _contains(root: str, path: str) -> bool:
    """Return containment under exact and platform-path comparison."""
    root_parts = PurePosixPath(root).parts
    path_parts = PurePosixPath(path).parts
    if len(path_parts) < len(root_parts):
        return False
    prefix = path_parts[: len(root_parts)]
    if prefix == root_parts:
        return True
    return tuple(platform_path_key(part) for part in prefix) == tuple(
        platform_path_key(part) for part in root_parts
    )


def read_vendor_modules_text(build_root: Path) -> str:
    """Read ``<build root>/vendor/modules.txt`` as the single effective surface."""
    path = build_root.joinpath("vendor", "modules.txt")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ModuleRootError(
            CODE_DIRECTIVE_FORM_UNSUPPORTED,
            f"cannot inspect {_MODULES_TXT}: {exc}",
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ModuleRootError(
            CODE_DIRECTIVE_FORM_UNSUPPORTED,
            f"{_MODULES_TXT} must be a real regular file below the build root",
        )
    try:
        return path.read_bytes().decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise ModuleRootError(
            CODE_DIRECTIVE_FORM_UNSUPPORTED,
            f"{_MODULES_TXT} is unreadable: {exc}",
        ) from exc


def parse_effective_replacements(text: str) -> tuple[Replacement, ...]:
    """Extract the effective replace set from ``vendor/modules.txt`` bytes.

    Only a line whose first two bytes are exactly ``"# "`` and which contains
    the exact bytes ``" => "`` is a replacement annotation. A one-token-left
    annotation is the effective directive; a two-token-left annotation is
    selection metadata that MUST reconcile against an identical one-token-left
    directive, which is what rejects a versioned left side without parsing
    ``go.mod``.
    """

    directives: list[Replacement] = []
    selections: list[tuple[str, tuple[str, ...]]] = []
    directive_keys: set[tuple[str, tuple[str, ...]]] = set()
    for raw_line in text.splitlines():
        if not raw_line.startswith(_ANNOTATION_PREFIX) or _ANNOTATION_ARROW not in raw_line:
            continue
        head, _, tail = raw_line.partition(_ANNOTATION_ARROW)
        left_tokens = tuple(head[len(_ANNOTATION_PREFIX) :].split())
        right_tokens = tuple(tail.split())
        if not 1 <= len(left_tokens) <= 2 or not 1 <= len(right_tokens) <= 2:
            raise ModuleRootError(
                CODE_DIRECTIVE_FORM_UNSUPPORTED,
                f"{_MODULES_TXT} carries an unreadable replacement annotation: {raw_line!r}",
            )
        if len(left_tokens) == 2:
            selections.append((left_tokens[0], right_tokens))
            continue
        if len(right_tokens) != 1:
            raise ModuleRootError(
                CODE_DIRECTIVE_FORM_UNSUPPORTED,
                "a module-to-module redirect is not a directory replacement: "
                f"{raw_line!r}",
            )
        directive_keys.add((left_tokens[0], right_tokens))
        directives.append(Replacement(module_path=left_tokens[0], target=right_tokens[0]))

    for module_path, right_tokens in selections:
        if (module_path, right_tokens) not in directive_keys:
            raise ModuleRootError(
                CODE_DIRECTIVE_FORM_UNSUPPORTED,
                "a versioned replacement side has no matching unversioned "
                f"directive: {module_path!r}",
            )
    return tuple(directives)


def resolve_bijection(
    modules: tuple[str, ...],
    replacements: tuple[Replacement, ...],
    *,
    build_root: str,
) -> frozenset[str]:
    """Check the declaration against the effective replace set both ways.

    Returns the module paths whose vendored packages are permitted to carry a
    ``Module.Replace`` record.
    """

    declared = set(modules)
    claimed: dict[str, str] = {}
    for replacement in replacements:
        resolved = _resolve_target(replacement.target, build_root=build_root)
        if resolved is None or resolved not in declared:
            raise ModuleRootError(
                CODE_DIRECTIVE_UNDECLARED,
                f"replacement {replacement.module_path!r} => {replacement.target!r} "
                "names no declared module directory",
            )
        previous = claimed.get(resolved)
        if previous is not None:
            raise ModuleRootError(
                CODE_DIRECTIVE_UNDECLARED,
                f"declared module directory {resolved!r} is named by both "
                f"{previous!r} and {replacement.module_path!r}",
            )
        claimed[resolved] = replacement.module_path

    unused = sorted(declared - set(claimed))
    if unused:
        raise ModuleRootError(
            CODE_DECLARATION_UNUSED,
            f"declared module directory {unused[0]!r} is named by no replacement",
        )
    return frozenset(claimed.values())


def _resolve_target(target: str, *, build_root: str) -> str | None:
    """Resolve one right-hand directory token against the build root."""
    if not target or target.startswith("/") or "\\" in target:
        return None
    if PurePosixPath(target).is_absolute():
        return None
    parts: list[str] = list(PurePosixPath(build_root).parts)
    for component in PurePosixPath(target).parts:
        if component == ".":
            continue
        if component == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(component)
    if not parts:
        return None
    resolved = PurePosixPath(*parts).as_posix()
    if not is_valid_portable_path(resolved):
        return None
    return resolved
