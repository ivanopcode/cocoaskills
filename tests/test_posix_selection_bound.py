"""The descriptor-traversal capability gate for schema-2 selection.

Schema-2 selection descends from an opened source-root descriptor by bare
component names (``os.open`` with ``O_DIRECTORY`` plus ``dir_fd=`` descent).
Where the runtime cannot provide that mechanism selection refuses with a
typed refusal instead of falling back to a path-based walk.

Every test in this module RUNS on every platform: the capability is either
faked at the runtime inputs (``os.supports_dir_fd``, ``O_DIRECTORY``) or
injected at the predicate, so the no-capability branch is exercised on this
host as well. Nothing here may skip.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from csk.sources import _selection_fs
from csk.sources import errors as source_errors
from csk.sources.selection import (
    expand_collection,
    expand_selectors,
    resolve_individual,
    resolve_selector_directory,
)
from csk.sources.skillfile_v2 import CollectionSelector, IndividualSelector
from test_manifest import _assert_matches_baseline, _load_v1_baseline


def _capable_dir_fd_set() -> set[object]:
    """A ``supports_dir_fd`` value carrying exactly the gated functions."""

    return {os.open, os.stat, os.readlink}


def _fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    supports_dir_fd: object,
    has_o_directory: bool,
) -> None:
    """Replace the two runtime inputs the predicate reads, nothing else."""

    monkeypatch.setattr(
        os, "supports_dir_fd", supports_dir_fd, raising=False
    )
    if has_o_directory:
        monkeypatch.setattr(os, "O_DIRECTORY", 0o200000, raising=False)
    else:
        monkeypatch.delattr(os, "O_DIRECTORY", raising=False)


def test_predicate_agrees_with_runtime_inputs() -> None:
    """The live predicate equals the manual reading of its two inputs.

    This pins the wiring on whatever host runs it (capable or not) without
    pinning the value: a hardcoded predicate fails on one side or the other.
    """

    supported = getattr(os, "supports_dir_fd", None)
    if os.name == "nt":
        expected = not _selection_fs._missing_traversal_mechanisms()
    else:
        expected = (
            hasattr(os, "O_DIRECTORY")
            and supported is not None
            and all(
                getattr(os, name, None) in supported
                for name in ("open", "stat", "readlink")
            )
        )
    assert _selection_fs.supports_descriptor_traversal() == expected


def test_predicate_windows_shaped_runtime_is_not_capable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``supports_dir_fd`` plus no ``O_DIRECTORY`` refuses (Windows)."""

    _fake_runtime(monkeypatch, supports_dir_fd=frozenset(), has_o_directory=False)
    if os.name == "nt":
        assert _selection_fs.supports_descriptor_traversal() == (
            not _selection_fs._missing_traversal_mechanisms()
        )
    else:
        assert _selection_fs.supports_descriptor_traversal() is False
        assert _selection_fs._missing_traversal_mechanisms() == [
            "O_DIRECTORY",
            "dir_fd support for os.open",
            "dir_fd support for os.stat",
            "dir_fd support for os.readlink",
        ]


@pytest.mark.parametrize("with_lstat", [False, True], ids=["py311", "py314"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd backend predicate")
def test_predicate_posix_shaped_runtime_is_capable(
    monkeypatch: pytest.MonkeyPatch, with_lstat: bool
) -> None:
    """A POSIX ``supports_dir_fd`` shape is capable with or without lstat.

    macOS on Python 3.11/3.12 omits ``os.lstat`` from ``os.supports_dir_fd``
    while the ``dir_fd=`` call itself works; requiring lstat membership
    would refuse a healthy POSIX lane. Killer for the require-lstat
    regression mutant.
    """

    supported = _capable_dir_fd_set()
    if with_lstat:
        supported.add(os.lstat)
    _fake_runtime(
        monkeypatch, supports_dir_fd=supported, has_o_directory=True
    )
    assert _selection_fs.supports_descriptor_traversal() is True
    assert _selection_fs._missing_traversal_mechanisms() == []


@pytest.mark.parametrize(
    "missing",
    ["O_DIRECTORY", "open", "stat", "readlink", "supports_dir_fd"],
    ids=[
        "missing-O_DIRECTORY",
        "missing-open",
        "missing-stat",
        "missing-readlink",
        "missing-supports_dir_fd",
    ],
)
@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd backend predicate")
def test_predicate_missing_single_mechanism_is_not_capable(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """Losing any one required mechanism refuses (narrowing killers).

    Each row kills the narrowing mutant that reports the capability present
    for exactly the platform shape lacking that mechanism.
    """

    supported: object = _capable_dir_fd_set()
    has_o_directory = True
    if missing == "O_DIRECTORY":
        has_o_directory = False
    elif missing == "supports_dir_fd":
        supported = None
    else:
        supported = set(_capable_dir_fd_set())
        supported.remove(getattr(os, missing))
    _fake_runtime(
        monkeypatch,
        supports_dir_fd=supported,
        has_o_directory=has_o_directory,
    )
    assert _selection_fs.supports_descriptor_traversal() is False
    assert _selection_fs._missing_traversal_mechanisms() != []


def test_predicate_ignores_in_process_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrapping ``os`` functions never changes the capability answer.

    Fault injection and tracing legitimately replace ``os.open`` /
    ``os.stat`` / ``os.readlink`` in-process (this repository's own
    observation harness does); the predicate compares import-time
    identities against the live ``supports_dir_fd`` set, so the answer
    before wrapping equals the answer after on every host. Killer for
    the live-getattr regression mutant.
    """

    before = _selection_fs.supports_descriptor_traversal()
    for name in ("open", "stat", "readlink"):
        original = getattr(os, name)

        def passthrough(
            *args: object, _original: object = original, **kwargs: object
        ) -> object:
            return _original(*args, **kwargs)  # type: ignore[operator]

        monkeypatch.setattr(os, name, passthrough)
    assert _selection_fs.supports_descriptor_traversal() == before


def _assert_posix_only_refusal(
    excinfo: pytest.ExceptionInfo[source_errors.SourceError],
) -> None:
    """Assert the structured early refusal shape, never a rescued error."""

    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID
    assert "descriptor-relative traversal is unavailable" in excinfo.value.detail
    if os.name == "nt":
        assert "required Windows NT filesystem APIs" in excinfo.value.detail
    else:
        assert "POSIX-only" in excinfo.value.detail
    assert excinfo.value.__cause__ is None


@pytest.mark.parametrize(
    "entry", ["selector", "collection", "individual", "selectors"]
)
def test_schema2_selection_refuses_without_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Every selection entry refuses through the production path.

    The capability is injected as absent on this host; the source root does
    not even exist, which additionally proves the refusal precedes any
    traversal attempt (a traversal would fail as "cannot be opened").
    Production call sites: ``resolve_selector_directory``,
    ``expand_collection``, ``resolve_individual``, ``expand_selectors``,
    each via ``selection._open_session`` into ``SelectionSession.open``.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    monkeypatch.setattr(
        _selection_fs, "supports_descriptor_traversal", lambda: False
    )
    root = tmp_path / "does-not-exist"
    with pytest.raises(source_errors.SourceError) as excinfo:
        if entry == "selector":
            resolve_selector_directory(root, ".")
        elif entry == "collection":
            expand_collection(
                root,
                CollectionSelector(
                    from_alias="local", directory=".", include=("*",), exclude=()
                ),
            )
        elif entry == "individual":
            resolve_individual(
                root,
                IndividualSelector(
                    name="review", from_alias="local", directory="pkg"
                ),
            )
        else:
            expand_selectors(
                [
                    IndividualSelector(
                        name="review", from_alias="local", directory="pkg"
                    )
                ],
                {"local": root},
            )
    _assert_posix_only_refusal(excinfo)


def test_refusal_precedes_any_traversal_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate fires before the first descriptor open of either phase.

    Phase A starts with ``_open_root`` (home probe and root open both go
    through ``_open_descriptor``), so zero seam calls means neither phase
    began. Killer for the gate-after-preflight reorder mutant.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    monkeypatch.setattr(
        _selection_fs, "supports_descriptor_traversal", lambda: False
    )
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    real_open = _selection_fs._open_descriptor

    def spy(*args: object, **kwargs: object) -> int:
        calls.append((args, kwargs))
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(_selection_fs, "_open_descriptor", spy)
    root = tmp_path / "does-not-exist"
    with pytest.raises(source_errors.SourceError) as first:
        expand_collection(
            root,
            CollectionSelector(
                from_alias="local", directory=".", include=("*",), exclude=()
            ),
        )
    _assert_posix_only_refusal(first)
    with pytest.raises(source_errors.SourceError) as second:
        resolve_individual(
            root,
            IndividualSelector(name="review", from_alias="local", directory="pkg"),
        )
    _assert_posix_only_refusal(second)
    assert calls == []


@pytest.mark.parametrize("root_shape", ["missing", "file", "nul"])
def test_refusal_is_structured_never_rescued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_shape: str
) -> None:
    """Inputs that would explode in ``os`` calls still get the clean refusal.

    A missing root (ENOENT), a file root (ENOTDIR) and an embedded-NUL root
    (ValueError) each refuse with the capability ``SourceError`` carrying
    no chained cause: the refusal is raised, never rescued.
    """

    monkeypatch.setenv("CSK_CONFIG", str(tmp_path / "home" / "config.json"))
    monkeypatch.setattr(
        _selection_fs, "supports_descriptor_traversal", lambda: False
    )
    if root_shape == "missing":
        root = tmp_path / "does-not-exist"
    elif root_shape == "file":
        root = tmp_path / "file-root"
        root.write_text("not a directory", encoding="utf-8")
    else:
        root = tmp_path / "bad\0name"
    with pytest.raises(source_errors.SourceError) as excinfo:
        expand_collection(
            root,
            CollectionSelector(
                from_alias="local", directory=".", include=("*",), exclude=()
            ),
        )
    _assert_posix_only_refusal(excinfo)


def test_v1_byte_identity_holds_without_descriptor_traversal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Released v1 behaviour is unchanged where traversal is absent.

    The full v1 byte-identity corpus (captured from base ``53638fa``)
    parses identically with the capability injected as absent, through
    the production ``csk.manifest.parse_manifest`` entry point. The
    capability injection is asserted active so the test is not vacuous.
    """

    monkeypatch.setattr(
        _selection_fs, "supports_descriptor_traversal", lambda: False
    )
    assert _selection_fs.supports_descriptor_traversal() is False
    monkeypatch.delenv("CSK_EXPERIMENTAL_SKILLFILE_SOURCES", raising=False)
    path = Path("Skillfile.json")
    for entry in _load_v1_baseline():
        _assert_matches_baseline(
            entry["payload"], entry["expected"], path, allow_schema_2=False
        )
