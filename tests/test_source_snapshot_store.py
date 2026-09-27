"""The source-v1 snapshot store and its four consumer readers.

Covers TASK-260917-34g2lq: the ``source-v1`` namespace under the csk home
(proven disjoint from the legacy ``cache/<source>/<commit>`` layout, with no
``commit`` field anywhere near a snapshot digest), lookup by (skill name,
package identity) that refuses a missing locked snapshot with
``source_snapshot_unavailable`` and never recreates it, an opt-in install
absence probe for the publisher's locked recovery path, the refusal as a
class test over default consumer reads (audit, build, projection, install),
transactional staging with before/after tree hashes around every injected
fault, and host-specific bounds probed at runtime.

Every test here is portable except the ones carrying an explicit runtime
probe skip (symlink/permission creation, descriptor capture): no test
branches on the operating system's name.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import os
import shutil
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from csk import snapshot as legacy_snapshot
from csk.locking import ManagerHomeLock
from csk.sources import _selection_fs, consumers, local_snapshot
from csk.sources import errors as source_errors
from csk.sources import store as store_module
from csk.sources._selection_fs import CapturedFile, CapturedTree
from csk.sources.snapshot import CapturedPackage, FilesystemEquivalence

SKILL = "review"
PACKAGE = "local:packages/review"


def _capture(
    tmp_path: Path, files: Mapping[str, tuple[bytes, bool]]
) -> CapturedPackage:
    """Hand-build one captured package from in-memory bytes (no traversal)."""
    entries = [
        (path, "sha256:" + hashlib.sha256(data).hexdigest(), executable)
        for path, (data, executable) in files.items()
    ]
    inventory = local_snapshot.build_inventory(entries, equivalent=lambda a, b: False)
    tree_files = {
        tuple(path.split("/")): CapturedFile(
            path=tuple(path.split("/")),
            data=data,
            executable=executable,
            identity=(7, index + 1),
            nlink=1,
        )
        for index, (path, (data, executable)) in enumerate(files.items())
    }
    root = tmp_path / "src"
    tree = CapturedTree(
        root=root, files=tree_files, directories=frozenset({()}), conflation_probes=()
    )
    return CapturedPackage(
        root=root,
        tree=tree,
        inventory=inventory,
        equivalence=FilesystemEquivalence(False, False, True, True),
    )


def _store_tree_hash(store_root: Path) -> str:
    """Strict hash of the store tree: dirs, files, link targets and bytes."""
    if not store_root.exists() and not store_root.is_symlink():
        return "absent"
    digest = hashlib.sha256()
    for current, dirs, files in os.walk(store_root, followlinks=False):
        for name in sorted(dirs):
            relative = os.fspath((Path(current) / name).relative_to(store_root))
            digest.update(b"d\0" + relative.encode("utf-8") + b"\0")
        for name in sorted(files):
            path = Path(current) / name
            relative = os.fspath(path.relative_to(store_root)).encode("utf-8")
            if path.is_symlink():
                digest.update(b"l\0" + relative + b"\0")
                digest.update(os.readlink(path).encode("utf-8") + b"\0")
            else:
                digest.update(b"f\0" + relative + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _raises(code: str, func: Callable[[], Any]) -> source_errors.SourceError:
    with pytest.raises(source_errors.SourceError) as excinfo:
        func()
    assert excinfo.value.code == code
    return excinfo.value


@contextmanager
def _faulty_call(
    module: Any, func_name: str, predicate: Callable[..., bool], error: Exception
) -> Iterator[list[str]]:
    """Replace one module-level call with a failing wrapper inside the window."""
    real = getattr(module, func_name)
    fired: list[str] = []

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if predicate(*args, **kwargs):
            fired.append(repr(args[0]) if args else func_name)
            raise error
        return real(*args, **kwargs)

    setattr(module, func_name, wrapper)
    try:
        yield fired
    finally:
        setattr(module, func_name, real)


def _can_create_symlink(tmp_path: Path) -> bool:
    probe = tmp_path / "symlink-probe-target"
    link = tmp_path / "symlink-probe-link"
    try:
        probe.write_text("x", encoding="utf-8")
        link.symlink_to(probe)
        return link.is_symlink()
    except OSError:
        return False
    finally:
        try:
            link.unlink()
        except OSError:
            pass
        try:
            probe.unlink()
        except OSError:
            pass


def _can_set_executable(tmp_path: Path) -> bool:
    probe = tmp_path / "exec-probe"
    try:
        probe.write_text("x", encoding="utf-8")
        probe.chmod(0o755)
        return bool(probe.stat().st_mode & 0o111)
    except OSError:
        return False
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def _keys_conflate(first: Path, second: Path) -> bool | None:
    try:
        first.write_bytes(b"1")
        second.write_bytes(b"2")
        return first.read_bytes() == b"2"
    except OSError:
        return None
    finally:
        for candidate in (first, second):
            try:
                candidate.unlink()
            except OSError:
                pass


# Namespace: source-v1 under the csk home, disjoint from the legacy layout.


def test_store_root_is_source_v1_under_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert store_module.store_root(home) == home / "source-v1"
    assert store_module.STORE_NAMESPACE == "source-v1"


def test_entry_dir_is_deterministic_hex_under_snapshots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = store_module.entry_dir(home, SKILL, PACKAGE)
    second = store_module.entry_dir(home, SKILL, PACKAGE)
    assert first == second
    relative = first.relative_to(home)
    assert relative.parts[0] == "source-v1"
    assert relative.parts[1] == "snapshots"
    assert len(relative.parts) == 4
    for component in relative.parts[2:]:
        assert len(component) == 64
        int(component, 16)


@pytest.mark.parametrize(
    "first, second",
    [
        (("review", "local:a"), ("review", "local:b")),
        (("review", "local:a"), ("other", "local:a")),
        (("Review", "local:a"), ("review", "local:a")),
        (("review", "LOCAL:A"), ("review", "local:a")),
        (("review", ""), ("review", "local:a")),
        (("", ""), ("review", "local:a")),
    ],
)
def test_distinct_keys_map_to_distinct_entries(
    tmp_path: Path, first: tuple[str, str], second: tuple[str, str]
) -> None:
    home = tmp_path / "home"
    assert store_module.entry_dir(home, *first) != store_module.entry_dir(home, *second)


@pytest.mark.parametrize(
    "skill, package",
    [
        ("..", "local:a"),
        ("review", ".."),
        ("../cache", "../../x"),
        ("a/b", "c/d"),
        ("cache", "snapshots"),
        ("source-v1", "staging"),
        ("C:\\Windows", "D:\\x"),
        ("", ""),
        ("x" * 5000, "y" * 5000),
        ("réview ✓", "local:pâth ✓"),
        ("sha256:" + "ab" * 32, "sha256:" + "cd" * 32),
        (".", "."),
        ("record.json", "trees"),
    ],
)
def test_hostile_keys_stay_under_the_store_root(
    tmp_path: Path, skill: str, package: str
) -> None:
    home = tmp_path / "home"
    entry = store_module.entry_dir(home, skill, package)
    assert entry.relative_to(store_module.store_root(home))
    assert store_module.STORE_NAMESPACE not in entry.relative_to(home).parts[1:]
    _raises(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        lambda: store_module.lookup_snapshot(home, skill, package),
    )
    assert _store_tree_hash(store_module.store_root(home)) == "absent"


@pytest.mark.parametrize(
    "source, commit",
    [
        ("github.com/acme/skills", "a" * 40),
        ("github.com/acme/skills", "b" * 64),
        ("source-v1", "c" * 40),
        ("cache", "sha256:" + "ab" * 32),
        ("host/path", "19" * 20),
    ],
)
def test_store_and_legacy_cache_layouts_are_disjoint(
    tmp_path: Path, source: str, commit: str
) -> None:
    home = tmp_path / "home"
    legacy = legacy_snapshot.snapshot_dir(home, source, commit)
    stored = store_module.entry_dir(home, SKILL, PACKAGE)
    assert legacy.relative_to(home).parts[0] == "cache"
    assert stored.relative_to(home).parts[0] == "source-v1"
    with pytest.raises(ValueError):
        stored.relative_to(legacy)
    with pytest.raises(ValueError):
        legacy.relative_to(stored)


@pytest.mark.parametrize("bad", [None, 7, b"review", ("review",), {"k": "v"}])
def test_non_string_keys_raise_typeerror(tmp_path: Path, bad: Any) -> None:
    home = tmp_path / "home"
    with pytest.raises(TypeError):
        store_module.entry_dir(home, bad, PACKAGE)
    with pytest.raises(TypeError):
        store_module.entry_dir(home, SKILL, bad)
    with pytest.raises(TypeError):
        store_module.lookup_snapshot(home, bad, PACKAGE)
    with pytest.raises(TypeError):
        store_module.lookup_snapshot(home, SKILL, bad)


def test_surrogate_key_lookup_refuses_without_writing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    error = _raises(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        lambda: store_module.lookup_snapshot(home, "a\ud800", PACKAGE),
    )
    assert "review" not in error.detail or "a" in error.detail
    assert not home.exists()


# No snapshot digest ever lands in a Git commit field.


def _json_key_values(value: Any, pairs: list[tuple[str, Any]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            pairs.append((str(key), item))
            _json_key_values(item, pairs)
    elif isinstance(value, list):
        for item in value:
            _json_key_values(item, pairs)


def test_stored_records_carry_no_commit_field(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = _capture(tmp_path, {"SKILL.md": (b"one", False)})
    second = _capture(tmp_path, {"SKILL.md": (b"two", True), "bin/run": (b"x", True)})
    store_module.stage_snapshot(home, SKILL, PACKAGE, first)
    store_module.stage_snapshot(home, "other", "local:other", second)
    digests = {first.inventory["snapshot"], second.inventory["snapshot"]}
    documents = sorted(store_module.store_root(home).rglob("*.json"))
    assert len(documents) == 2
    for document in documents:
        pairs: list[tuple[str, Any]] = []
        _json_key_values(json.loads(document.read_bytes().decode("utf-8")), pairs)
        assert [key for key, _ in pairs if key == "commit"] == []
        for key, item in pairs:
            if isinstance(item, str) and item in digests:
                assert key in {"snapshot", "sha256"}, (document, key)


def test_store_modules_reach_no_commit_field_owner() -> None:
    """Neither module imports the legacy ``csk.snapshot`` nor the pipeline.

    The sibling ``csk.sources.snapshot`` (capture) is expected and allowed;
    only the top-level commit-field owners are refused.
    """
    for module_name in ("store.py", "consumers.py"):
        path = Path(store_module.__file__).parent / module_name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        owners: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                level = node.level
                module = node.module or ""
                names = [module, *(alias.name for alias in node.names)]
                for name in names:
                    short = name.split(".")[-1]
                    if short in {"snapshot", "build_repository_pipeline"} and (
                        level != 1 or short != "snapshot"
                    ):
                        owners.append(f"{module_name}:{node.lineno}:{short}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[-1] in {
                        "snapshot",
                        "build_repository_pipeline",
                    }:
                        owners.append(f"{module_name}:{node.lineno}:{alias.name}")
        assert owners == []
        names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        assert "commit" not in names, module_name


# Staging happy path.


STAGED_FILES: dict[str, tuple[bytes, bool]] = {
    "SKILL.md": (b"# review\n", False),
    "bin/run": (b"#!/bin/sh\necho hi\n", True),
    "docs/nested/notes.md": (b"notes", False),
}


def test_stage_then_lookup_serves_exact_bytes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    captured = _capture(tmp_path, STAGED_FILES)
    returned = store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    served = store_module.lookup_snapshot(home, SKILL, PACKAGE)
    assert returned.snapshot == captured.inventory["snapshot"] == served.snapshot
    assert served.skill == SKILL
    assert served.package == PACKAGE
    assert served.inventory == captured.inventory
    assert set(served.files) == set(STAGED_FILES)
    for path, (data, executable) in STAGED_FILES.items():
        assert served.files[path].data == data
        assert served.files[path].executable is executable
        assert served.files[path].path == path


def test_stage_is_idempotent_for_identical_bytes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    captured = _capture(tmp_path, STAGED_FILES)
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    before = _store_tree_hash(store_module.store_root(home))
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    assert _store_tree_hash(store_module.store_root(home)) == before
    served = store_module.lookup_snapshot(home, SKILL, PACKAGE)
    assert served.snapshot == captured.inventory["snapshot"]


def test_lookup_if_present_returns_none_only_for_absent_entry(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert store_module.lookup_snapshot_if_present(home, SKILL, PACKAGE) is None
    assert not home.exists()

    captured = _capture(tmp_path, {"SKILL.md": (b"stored", False)})
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    record = store_module.entry_dir(home, SKILL, PACKAGE) / store_module.RECORD_FILENAME
    record.write_bytes(b"not json")
    _raises(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        lambda: store_module.lookup_snapshot_if_present(home, SKILL, PACKAGE),
    )


def test_stage_if_missing_preserves_a_snapshot_that_appeared(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old = _capture(tmp_path, {"SKILL.md": (b"stored", False)})
    new = _capture(tmp_path, {"SKILL.md": (b"recovered", False)})
    store_module.stage_snapshot(home, SKILL, PACKAGE, old)
    before = _store_tree_hash(store_module.store_root(home))

    served = store_module.stage_snapshot_if_missing(home, SKILL, PACKAGE, new)

    assert served.snapshot == old.inventory["snapshot"]
    assert served.files["SKILL.md"].data == b"stored"
    assert _store_tree_hash(store_module.store_root(home)) == before


def test_stage_supersedes_for_same_key(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old = _capture(tmp_path, {"SKILL.md": (b"old", False)})
    new = _capture(tmp_path, {"SKILL.md": (b"new", False)})
    store_module.stage_snapshot(home, SKILL, PACKAGE, old)
    store_module.stage_snapshot(home, SKILL, PACKAGE, new)
    served = store_module.lookup_snapshot(home, SKILL, PACKAGE)
    assert served.snapshot == new.inventory["snapshot"]
    assert served.files["SKILL.md"].data == b"new"
    trees = store_module.entry_dir(home, SKILL, PACKAGE) / "trees"
    assert len(list(trees.iterdir())) == 2


def test_stage_empty_package(tmp_path: Path) -> None:
    home = tmp_path / "home"
    captured = _capture(tmp_path, {})
    stored = store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    served = store_module.lookup_snapshot(home, SKILL, PACKAGE)
    assert dict(served.files) == {}
    assert served.snapshot == stored.snapshot == captured.inventory["snapshot"]


def test_stage_rejects_capture_whose_bytes_differ_from_its_digest(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    honest = _capture(tmp_path, {"SKILL.md": (b"audited", False)})
    tampered_tree = CapturedTree(
        root=honest.root,
        files={
            ("SKILL.md",): CapturedFile(
                path=("SKILL.md",),
                data=b"mutated",
                executable=False,
                identity=(7, 1),
                nlink=1,
            )
        },
        directories=frozenset({()}),
        conflation_probes=(),
    )
    tampered = CapturedPackage(
        root=honest.root,
        tree=tampered_tree,
        inventory=honest.inventory,
        equivalence=honest.equivalence,
    )
    _raises(
        source_errors.CODE_SNAPSHOT_CHANGED,
        lambda: store_module.stage_snapshot(home, SKILL, PACKAGE, tampered),
    )
    assert _store_tree_hash(store_module.store_root(home)) == "absent"


def test_stage_reports_capture_collisions_as_such(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folded = _capture(tmp_path, {"a.txt": (b"1", False), "A.txt": (b"2", False)})
    folding = CapturedPackage(
        root=folded.root,
        tree=folded.tree,
        inventory=folded.inventory,
        equivalence=FilesystemEquivalence(True, False, True, True),
    )
    _raises(
        source_errors.CODE_PATH_CONFLICT,
        lambda: store_module.stage_snapshot(home, SKILL, PACKAGE, folding),
    )
    assert _store_tree_hash(store_module.store_root(home)) == "absent"


# The refusal: missing locked snapshots, over every consumer.


def test_missing_snapshot_refuses_for_every_consumer(tmp_path: Path) -> None:
    """Live bytes B exist, the store is absent: every consumer refuses.

    The store stays absent afterwards: nothing recreates the snapshot from
    the live bytes, and lookup does not even create the home directory.
    """
    home = tmp_path / "home"
    live = tmp_path / "live"
    (live / "pkg").mkdir(parents=True)
    (live / "pkg" / "SKILL.md").write_text("B", encoding="utf-8")
    assert {opener.__name__ for opener in consumers.ALL_CONSUMERS} == {
        "open_for_audit",
        "open_for_build",
        "open_for_projection",
        "open_for_install",
    }
    for opener in consumers.ALL_CONSUMERS:
        error = _raises(
            source_errors.CODE_SNAPSHOT_UNAVAILABLE,
            lambda: opener(home, SKILL, PACKAGE),
        )
        assert SKILL in error.detail
        assert PACKAGE in error.detail
        assert opener.__name__.removeprefix("open_for_") in error.detail
    assert _store_tree_hash(store_module.store_root(home)) == "absent"
    assert not home.exists()
    assert (live / "pkg" / "SKILL.md").read_text(encoding="utf-8") == "B"


def test_consumer_set_matches_public_readers() -> None:
    assert {opener.__name__ for opener in consumers.ALL_CONSUMERS} == {
        name for name in consumers.__all__ if name.startswith("open_for_")
    }
    assert len(consumers.ALL_CONSUMERS) == 4


def test_every_consumer_serves_staged_bytes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    captured = _capture(tmp_path, STAGED_FILES)
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    for opener in consumers.ALL_CONSUMERS:
        served = opener(home, SKILL, PACKAGE)
        assert served.snapshot == captured.inventory["snapshot"]
        assert set(served.files) == set(STAGED_FILES)
        for path, (data, executable) in STAGED_FILES.items():
            assert served.files[path].data == data
            assert served.files[path].executable is executable


def test_every_consumer_calls_store_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    calls: list[tuple[Path, str, str]] = []
    sentinel = store_module.StoredSnapshot(
        skill=SKILL,
        package=PACKAGE,
        snapshot="sha256:" + "00" * 32,
        inventory={
            "schema_version": 1,
            "algorithm": "curator-local-snapshot-v1",
            "files": [],
            "snapshot": "sha256:" + "00" * 32,
        },
        files={},
    )

    def _spy(entry_home: Path, skill: str, package: str) -> store_module.StoredSnapshot:
        calls.append((entry_home, skill, package))
        return sentinel

    monkeypatch.setattr(consumers, "lookup_snapshot", _spy)
    for opener in consumers.ALL_CONSUMERS:
        assert opener(home, SKILL, PACKAGE) is sentinel
    assert calls == [(home, SKILL, PACKAGE)] * 4


def test_lookup_wrong_package_serves_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = _capture(tmp_path, {"SKILL.md": (b"first", False)})
    second = _capture(tmp_path, {"SKILL.md": (b"second", False)})
    store_module.stage_snapshot(home, SKILL, "local:first", first)
    store_module.stage_snapshot(home, SKILL, "local:second", second)
    assert store_module.lookup_snapshot(home, SKILL, "local:first").files["SKILL.md"].data == b"first"
    assert (
        store_module.lookup_snapshot(home, SKILL, "local:second").files["SKILL.md"].data
        == b"second"
    )
    for opener in consumers.ALL_CONSUMERS:
        _raises(
            source_errors.CODE_SNAPSHOT_UNAVAILABLE,
            lambda: opener(home, SKILL, "local:third"),
        )
        first_bytes = opener(home, SKILL, "local:first").files["SKILL.md"].data
        assert first_bytes == b"first"


def test_lookup_case_variant_keys_are_distinct(tmp_path: Path) -> None:
    home = tmp_path / "home"
    upper = _capture(tmp_path, {"SKILL.md": (b"upper", False)})
    lower = _capture(tmp_path, {"SKILL.md": (b"lower", False)})
    store_module.stage_snapshot(home, "Review", PACKAGE, upper)
    store_module.stage_snapshot(home, "review", PACKAGE, lower)
    for opener in consumers.ALL_CONSUMERS:
        assert opener(home, "Review", PACKAGE).files["SKILL.md"].data == b"upper"
        assert opener(home, "review", PACKAGE).files["SKILL.md"].data == b"lower"


def test_served_bytes_survive_deletion_of_the_live_tree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    source = tmp_path / "src"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "SKILL.md").write_text("live", encoding="utf-8")
    captured = _capture(tmp_path, {"SKILL.md": (b"frozen", False)})
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    shutil.rmtree(source)
    assert not source.exists()
    for opener in consumers.ALL_CONSUMERS:
        assert opener(home, SKILL, PACKAGE).files["SKILL.md"].data == b"frozen"


def test_consumer_reads_stay_inside_the_store(tmp_path: Path) -> None:
    home = tmp_path / "home"
    captured = _capture(tmp_path, STAGED_FILES)
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    store_module.lookup_snapshot(home, SKILL, PACKAGE)
    root_text = os.path.abspath(store_module.store_root(home))
    events: list[str] = []
    active = True

    def _hook(event: str, args: tuple[Any, ...]) -> None:
        # Audit hooks cannot be removed; the flag keeps the leftover inert.
        if not active:
            return
        if event == "open" and args:
            events.append(os.fsdecode(args[0]) if isinstance(args[0], (str, bytes)) else "")

    sys.addaudithook(_hook)
    try:
        for opener in consumers.ALL_CONSUMERS:
            opener(home, SKILL, PACKAGE)
    finally:
        active = False
    opened = [event for event in events if event]
    assert opened, "the audit hook observed no file opens during consumer reads"
    for event in opened:
        assert os.path.abspath(event).startswith(root_text + os.sep), event


# Lookup fails closed over every tamper shape.


def _tamper_record(home: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    path = store_module.entry_dir(home, SKILL, PACKAGE) / "record.json"
    record = json.loads(path.read_bytes().decode("utf-8"))
    mutate(record)
    path.write_bytes(json.dumps(record, sort_keys=True).encode("utf-8"))


def _stored_tree(home: Path) -> Path:
    entry = store_module.entry_dir(home, SKILL, PACKAGE)
    record = json.loads((entry / "record.json").read_bytes().decode("utf-8"))
    digest = str(record["snapshot"])
    return entry / "trees" / digest[len("sha256:") :]


def _assert_tamper_refuses(home: Path) -> None:
    error = _raises(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        lambda: store_module.lookup_snapshot(home, SKILL, PACKAGE),
    )
    assert error.detail
    for opener in consumers.ALL_CONSUMERS:
        _raises(
            source_errors.CODE_SNAPSHOT_UNAVAILABLE,
            lambda: opener(home, SKILL, PACKAGE),
        )


def _seed_single_file(home: Path, tmp_path: Path) -> CapturedPackage:
    captured = _capture(tmp_path, {"SKILL.md": (b"sealed", False)})
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    assert (
        store_module.lookup_snapshot(home, SKILL, PACKAGE).snapshot
        == captured.inventory["snapshot"]
    )
    return captured


@pytest.mark.parametrize("version", [2, 99, "1", None])
def test_tamper_record_version_refuses(
    tmp_path: Path, version: Any
) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    _tamper_record(home, lambda record: record.update(schema_version=version))
    _assert_tamper_refuses(home)


@pytest.mark.parametrize(
    "schema", ["", "csk-source-snapshot-record-v2", "Xcsk-source-snapshot-record-v1"]
)
def test_tamper_record_schema_refuses(tmp_path: Path, schema: str) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    _tamper_record(home, lambda record: record.update(schema=schema))
    _assert_tamper_refuses(home)


def test_tamper_record_substring_schema_refuses(tmp_path: Path) -> None:
    """The schema gate is equality, not containment: padding refuses."""
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    padded = "X" + store_module.RECORD_SCHEMA + "X"
    _tamper_record(home, lambda record: record.update(schema=padded))
    _assert_tamper_refuses(home)


def test_tamper_record_extra_key_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    _tamper_record(home, lambda record: record.update(commit="ab" * 20))
    _assert_tamper_refuses(home)


def test_tamper_record_missing_key_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    _tamper_record(home, lambda record: record.pop("inventory"))
    _assert_tamper_refuses(home)


@pytest.mark.parametrize("field, value", [("skill", "other"), ("package", "local:other")])
def test_tamper_record_key_swap_refuses(
    tmp_path: Path, field: str, value: str
) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    _tamper_record(home, lambda record: record.update({field: value}))
    _assert_tamper_refuses(home)


@pytest.mark.parametrize("digest", ["", "sha256:xyz", "ab" * 32, "sha256:" + "ab" * 31])
def test_tamper_record_digest_refuses(tmp_path: Path, digest: str) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    _tamper_record(home, lambda record: record.update(snapshot=digest))
    _assert_tamper_refuses(home)


def test_tamper_record_inventory_digest_disagreement_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    captured = _seed_single_file(home, tmp_path)
    other = _capture(tmp_path, {"SKILL.md": (b"other", False)})
    assert other.inventory["snapshot"] != captured.inventory["snapshot"]
    _tamper_record(
        home, lambda record: record.update(snapshot=other.inventory["snapshot"])
    )
    _assert_tamper_refuses(home)


def test_tamper_record_file_entry_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    _tamper_record(
        home, lambda record: record["inventory"]["files"].append({"path": "evil.md"})
    )
    _assert_tamper_refuses(home)


def test_tamper_record_not_json_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    (store_module.entry_dir(home, SKILL, PACKAGE) / "record.json").write_bytes(b"{nope")
    _assert_tamper_refuses(home)


def test_tamper_record_not_utf8_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    (store_module.entry_dir(home, SKILL, PACKAGE) / "record.json").write_bytes(b"\xff\xfe")
    _assert_tamper_refuses(home)


def test_tamper_missing_record_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    (store_module.entry_dir(home, SKILL, PACKAGE) / "record.json").unlink()
    _assert_tamper_refuses(home)


def test_tamper_missing_tree_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    shutil.rmtree(_stored_tree(home))
    _assert_tamper_refuses(home)


def test_tamper_tree_replaced_by_file_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    tree = _stored_tree(home)
    shutil.rmtree(tree)
    tree.write_bytes(b"not a directory")
    _assert_tamper_refuses(home)


def test_tamper_modified_byte_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    (_stored_tree(home) / "SKILL.md").write_bytes(b"sealed!")
    _assert_tamper_refuses(home)


def test_tamper_removed_file_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    (_stored_tree(home) / "SKILL.md").unlink()
    _assert_tamper_refuses(home)


def test_tamper_extra_file_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    (_stored_tree(home) / "extra.md").write_bytes(b"extra")
    _assert_tamper_refuses(home)


def test_tamper_extra_empty_directory_refuses(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    (_stored_tree(home) / "empty-dir").mkdir()
    _assert_tamper_refuses(home)


def test_tamper_planted_link_refuses(tmp_path: Path) -> None:
    if not _can_create_symlink(tmp_path):
        pytest.skip("cannot create symlinks on this host (store link-plant bound)")
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    (_stored_tree(home) / "planted").symlink_to(_stored_tree(home) / "SKILL.md")
    _assert_tamper_refuses(home)


def test_tamper_tree_replaced_by_link_refuses(tmp_path: Path) -> None:
    if not _can_create_symlink(tmp_path):
        pytest.skip("cannot create symlinks on this host (store link-plant bound)")
    home = tmp_path / "home"
    captured = _seed_single_file(home, tmp_path)
    tree = _stored_tree(home)
    spare = tmp_path / "spare"
    spare.mkdir()
    (spare / "SKILL.md").write_bytes(b"spare")
    shutil.rmtree(tree)
    tree.symlink_to(spare, target_is_directory=True)
    _assert_tamper_refuses(home)
    assert captured.inventory["snapshot"]


def test_tamper_record_replaced_by_link_refuses(tmp_path: Path) -> None:
    if not _can_create_symlink(tmp_path):
        pytest.skip("cannot create symlinks on this host (store link-plant bound)")
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    record = store_module.entry_dir(home, SKILL, PACKAGE) / "record.json"
    spare = tmp_path / "spare-record.json"
    spare.write_bytes(record.read_bytes())
    record.unlink()
    record.symlink_to(spare)
    _assert_tamper_refuses(home)


# Staging is a transaction: faults leave the store byte-identical.


FAULT_FILES: dict[str, tuple[bytes, bool]] = {
    "SKILL.md": (b"fault-new", False),
    "second.md": (b"second-new", False),
    "third.md": (b"third-new", True),
}


def _seed_old(home: Path, tmp_path: Path) -> CapturedPackage:
    old = _capture(tmp_path, {"SKILL.md": (b"fault-old", False)})
    store_module.stage_snapshot(home, SKILL, PACKAGE, old)
    return old


def _assert_old_served(home: Path, old: CapturedPackage) -> None:
    served = store_module.lookup_snapshot(home, SKILL, PACKAGE)
    assert served.snapshot == old.inventory["snapshot"]
    assert served.files["SKILL.md"].data == b"fault-old"


def _faulty_stage(
    tmp_path: Path,
    fault: Callable[[], Any],
    *,
    seed: bool,
) -> tuple[Path, CapturedPackage | None, list[str]]:
    home = tmp_path / "home"
    old = _seed_old(home, tmp_path) if seed else None
    before = _store_tree_hash(store_module.store_root(home))
    new = _capture(tmp_path, FAULT_FILES)
    with fault() as fired:
        _raises(
            source_errors.CODE_SNAPSHOT_UNAVAILABLE,
            lambda: store_module.stage_snapshot(home, SKILL, PACKAGE, new),
        )
    assert fired, "the injected fault was never reached"
    assert _store_tree_hash(store_module.store_root(home)) == before
    if old is not None:
        _assert_old_served(home, old)
    else:
        _raises(
            source_errors.CODE_SNAPSHOT_UNAVAILABLE,
            lambda: store_module.lookup_snapshot(home, SKILL, PACKAGE),
        )
    return home, old, fired


def _is_record_tmp_open(*args: Any, **kwargs: Any) -> bool:
    if not args or kwargs.get("mode", args[1] if len(args) > 1 else "") not in ("xb",):
        return False
    return ".record-" in os.fspath(args[0])


@pytest.mark.parametrize("seed", [False, True], ids=["fresh", "seeded"])
def test_fault_before_record_write_leaves_store_identical(
    tmp_path: Path, seed: bool
) -> None:
    """The tree is renamed but the record write fails: rollback is total."""

    def _fault() -> Any:
        return _faulty_call(
            builtins, "open", _is_record_tmp_open, PermissionError("injected")
        )

    _faulty_stage(tmp_path, _fault, seed=seed)


@pytest.mark.parametrize("seed", [False, True], ids=["fresh", "seeded"])
def test_fault_at_tree_publish_leaves_store_identical(
    tmp_path: Path, seed: bool
) -> None:
    def _fault() -> Any:
        return _faulty_call(
            os,
            "rename",
            lambda *args, **kwargs: True,
            OSError("injected"),
        )

    _faulty_stage(tmp_path, _fault, seed=seed)


@pytest.mark.parametrize("seed", [False, True], ids=["fresh", "seeded"])
def test_fault_at_record_replace_leaves_store_identical(
    tmp_path: Path, seed: bool
) -> None:
    """The tree is published but the record replace fails: rollback is total."""

    def _fault() -> Any:
        return _faulty_call(
            os,
            "replace",
            lambda *args, **kwargs: str(args[1]).endswith("record.json"),
            PermissionError("injected"),
        )

    _faulty_stage(tmp_path, _fault, seed=seed)


def test_record_replace_retries_transient_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient sharing denial at the record replace is retried, not refused.

    Windows has no replace-while-open: a lock-free reader holding
    ``record.json`` open makes the writer's ``os.replace`` fail with
    ``PermissionError``. The replace is retried within a short bound, so a
    concurrent reader delays publication instead of failing it; a still-held
    file keeps refusing afterwards (see the record-replace S-TXN tests).

    Production call site: ``store._stage_locked`` record replace.
    """
    home = tmp_path / "home"
    new = _capture(tmp_path, FAULT_FILES)
    real_replace = os.replace
    attempts: list[str] = []

    def _flaky_replace(first: Any, second: Any) -> None:
        if str(second).endswith("record.json") and len(attempts) < 2:
            attempts.append("denied")
            raise PermissionError("[WinError 5] Access is denied")
        real_replace(first, second)

    monkeypatch.setattr(os, "replace", _flaky_replace)
    served = store_module.stage_snapshot(home, SKILL, PACKAGE, new)
    assert served.snapshot == new.inventory["snapshot"]
    assert len(attempts) == 2
    assert (
        store_module.lookup_snapshot(home, SKILL, PACKAGE).snapshot == served.snapshot
    )


@pytest.mark.parametrize("seed", [False, True], ids=["fresh", "seeded"])
def test_fault_mid_copy_leaves_store_identical(tmp_path: Path, seed: bool) -> None:
    """The second staged file fails: the half-written staging tree is gone."""
    seen: list[str] = []

    def _second_staged_write(*args: Any, **kwargs: Any) -> bool:
        if not args or ".record-" in os.fspath(args[0]):
            return False
        mode = kwargs.get("mode", args[1] if len(args) > 1 else "")
        if mode != "xb":
            return False
        seen.append(os.fspath(args[0]))
        return len(seen) == 2

    def _fault() -> Any:
        return _faulty_call(builtins, "open", _second_staged_write, OSError("injected"))

    _, _, fired = _faulty_stage(tmp_path, _fault, seed=seed)
    assert len(seen) == 2


@pytest.mark.parametrize("seed", [False, True], ids=["fresh", "seeded"])
def test_stage_while_home_lock_held_refuses_without_writing(
    tmp_path: Path, seed: bool
) -> None:
    home = tmp_path / "home"
    old = _seed_old(home, tmp_path) if seed else None
    before = _store_tree_hash(store_module.store_root(home))
    new = _capture(tmp_path, FAULT_FILES)
    with ManagerHomeLock(home, timeout=5):
        _raises(
            source_errors.CODE_SNAPSHOT_UNAVAILABLE,
            lambda: store_module.stage_snapshot(home, SKILL, PACKAGE, new),
        )
    assert _store_tree_hash(store_module.store_root(home)) == before
    if old is not None:
        _assert_old_served(home, old)


def test_stage_behind_a_contended_lock_times_out_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second thread holds the home lock: staging waits, then refuses."""
    home = tmp_path / "home"
    old = _seed_old(home, tmp_path)
    before = _store_tree_hash(store_module.store_root(home))
    monkeypatch.setenv("CSK_LOCK_TIMEOUT", "0.3")
    entered = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        with ManagerHomeLock(home, timeout=10):
            entered.set()
            assert release.wait(timeout=30)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    try:
        assert entered.wait(timeout=30)
        new = _capture(tmp_path, FAULT_FILES)
        error = _raises(
            source_errors.CODE_SNAPSHOT_UNAVAILABLE,
            lambda: store_module.stage_snapshot(home, SKILL, PACKAGE, new),
        )
        assert "lock" in error.detail
    finally:
        release.set()
        holder.join(timeout=30)
    assert _store_tree_hash(store_module.store_root(home)) == before
    _assert_old_served(home, old)


def test_cleanup_fault_publishes_completely_but_reports_it(
    tmp_path: Path,
) -> None:
    """Post-commit staging removal fails: bytes are served, the fault raises."""
    home = tmp_path / "home"
    old = _seed_old(home, tmp_path)
    new = _capture(tmp_path, FAULT_FILES)
    with _faulty_call(shutil, "rmtree", lambda *args, **kwargs: True, OSError("injected")):
        error = _raises(
            source_errors.CODE_SNAPSHOT_UNAVAILABLE,
            lambda: store_module.stage_snapshot(home, SKILL, PACKAGE, new),
        )
    assert "cleanup" in error.detail
    served = store_module.lookup_snapshot(home, SKILL, PACKAGE)
    assert served.snapshot == new.inventory["snapshot"]
    assert served.snapshot != old.inventory["snapshot"]
    residue = list((store_module.store_root(home) / "staging").iterdir())
    assert len(residue) == 1


def test_restage_heals_a_corrupt_published_tree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    captured = _capture(tmp_path, STAGED_FILES)
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    (_stored_tree(home) / "SKILL.md").write_bytes(b"corrupt")
    _raises(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        lambda: store_module.lookup_snapshot(home, SKILL, PACKAGE),
    )
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    served = store_module.lookup_snapshot(home, SKILL, PACKAGE)
    assert served.snapshot == captured.inventory["snapshot"]
    assert served.files["SKILL.md"].data == STAGED_FILES["SKILL.md"][0]
    leftovers = [
        path for path in (store_module.entry_dir(home, SKILL, PACKAGE) / "trees").iterdir()
    ]
    assert len(leftovers) == 1


def test_restage_heals_a_non_directory_tree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    captured = _capture(tmp_path, {"SKILL.md": (b"sealed", False)})
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    tree = _stored_tree(home)
    shutil.rmtree(tree)
    tree.write_bytes(b"not a directory")
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    assert store_module.lookup_snapshot(home, SKILL, PACKAGE).files["SKILL.md"].data == b"sealed"


def test_restage_heals_an_extra_empty_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    captured = _capture(tmp_path, {"SKILL.md": (b"sealed", False)})
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    (_stored_tree(home) / "planted-dir").mkdir()
    _raises(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        lambda: store_module.lookup_snapshot(home, SKILL, PACKAGE),
    )
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    assert store_module.lookup_snapshot(home, SKILL, PACKAGE).files["SKILL.md"].data == b"sealed"


@pytest.mark.parametrize("fail_on", [1, 2], ids=["first-rename", "second-rename"])
def test_fault_during_heal_restores_the_prior_tree(
    tmp_path: Path, fail_on: int
) -> None:
    """A fault mid-heal puts the prior (corrupt) bytes back; nothing publishes."""
    home = tmp_path / "home"
    captured = _capture(tmp_path, {"SKILL.md": (b"sealed", False)})
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    (_stored_tree(home) / "SKILL.md").write_bytes(b"corrupt")
    before = _store_tree_hash(store_module.store_root(home))
    seen: list[str] = []

    def _nth_rename(*args: Any, **kwargs: Any) -> bool:
        seen.append(str(args[0]))
        return len(seen) == fail_on

    with _faulty_call(os, "rename", _nth_rename, OSError("injected")) as fired:
        _raises(
            source_errors.CODE_SNAPSHOT_UNAVAILABLE,
            lambda: store_module.stage_snapshot(home, SKILL, PACKAGE, captured),
        )
    assert fired, "the injected fault was never reached"
    assert _store_tree_hash(store_module.store_root(home)) == before
    _raises(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        lambda: store_module.lookup_snapshot(home, SKILL, PACKAGE),
    )


def test_crash_residue_is_inert_and_never_served(tmp_path: Path) -> None:
    """Hand-planted staging residue and record tmps change nothing served."""
    home = tmp_path / "home"
    old = _seed_old(home, tmp_path)
    root = store_module.store_root(home)
    (root / "staging" / "deadbeef" / "trees").mkdir(parents=True)
    (root / "staging" / "deadbeef" / "trees" / "junk").write_bytes(b"junk")
    entry = store_module.entry_dir(home, SKILL, PACKAGE)
    (entry / ".record-deadbeef.tmp").write_bytes(b"half-written")
    _assert_old_served(home, old)
    new = _capture(tmp_path, FAULT_FILES)
    store_module.stage_snapshot(home, SKILL, PACKAGE, new)
    served = store_module.lookup_snapshot(home, SKILL, PACKAGE)
    assert served.snapshot == new.inventory["snapshot"]
    # Foreign crash residue is never swept and never served.
    assert (entry / ".record-deadbeef.tmp").exists()
    assert (root / "staging" / "deadbeef" / "trees" / "junk").exists()


def test_concurrent_readers_always_serve_a_complete_snapshot(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    versions = [
        _capture(tmp_path, {"SKILL.md": (f"version-{index}".encode(), False)})
        for index in range(6)
    ]
    digests = {version.inventory["snapshot"] for version in versions}
    assert len(digests) == 6
    store_module.stage_snapshot(home, SKILL, PACKAGE, versions[0])
    failures: list[str] = []
    served_digests: set[str] = set()
    guard = threading.Lock()

    def _read_many() -> None:
        try:
            for _ in range(30):
                served = store_module.lookup_snapshot(home, SKILL, PACKAGE)
                rebuilt = local_snapshot.build_inventory(
                    [
                        (path, "sha256:" + hashlib.sha256(item.data).hexdigest(), item.executable)
                        for path, item in served.files.items()
                    ],
                    equivalent=lambda a, b: False,
                )
                assert rebuilt["snapshot"] == served.snapshot
                with guard:
                    served_digests.add(served.snapshot)
        except Exception as exc:
            with guard:
                failures.append(repr(exc))

    def _write_many() -> None:
        try:
            for version in versions[1:]:
                store_module.stage_snapshot(home, SKILL, PACKAGE, version)
        except Exception as exc:
            with guard:
                failures.append(repr(exc))

    readers = [threading.Thread(target=_read_many) for _ in range(2)]
    writer = threading.Thread(target=_write_many)
    for thread in (*readers, writer):
        thread.start()
    for thread in (*readers, writer):
        thread.join(timeout=120)
    assert failures == []
    assert served_digests <= digests
    assert served_digests, "no reader served anything"


# The failure surface: every fault becomes the structured refusal.


@pytest.mark.parametrize(
    "error",
    [
        PermissionError("injected"),
        FileNotFoundError("injected"),
        NotADirectoryError("injected"),
        IsADirectoryError("injected"),
        OSError("injected"),
        RuntimeError("injected"),
    ],
    ids=["permission", "not-found", "not-a-dir", "is-a-dir", "os", "runtime"],
)
@pytest.mark.parametrize("site", ["open", "scandir"], ids=["open", "scandir"])
def test_lookup_fault_matrix_refuses_structured(
    tmp_path: Path, site: str, error: Exception
) -> None:
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    module, name = (builtins, "open") if site == "open" else (os, "scandir")
    with _faulty_call(module, name, lambda *args, **kwargs: True, error) as fired:
        detail = _raises(
            source_errors.CODE_SNAPSHOT_UNAVAILABLE,
            lambda: store_module.lookup_snapshot(home, SKILL, PACKAGE),
        ).detail
    assert fired, "the injected fault was never reached"
    assert SKILL in detail
    assert store_module.lookup_snapshot(home, SKILL, PACKAGE).snapshot


@pytest.mark.parametrize(
    "site, error",
    [
        ("write", PermissionError("injected")),
        ("write", OSError("injected")),
        ("rename", OSError("injected")),
        ("replace", PermissionError("injected")),
        ("mkdir", OSError("injected")),
        ("lock-mkdir", OSError("injected")),
    ],
    ids=[
        "write-permission",
        "write-os",
        "rename-os",
        "replace-permission",
        "mkdir-os",
        "lock-mkdir-os",
    ],
)
def test_stage_fault_matrix_refuses_structured(
    tmp_path: Path, site: str, error: Exception
) -> None:
    home = tmp_path / "home"
    control = tmp_path / "control-home"
    new = _capture(tmp_path, FAULT_FILES)
    store_module.stage_snapshot(control, SKILL, PACKAGE, new)
    before = _store_tree_hash(store_module.store_root(home))
    if site == "write":
        fault = _faulty_call(
            builtins,
            "open",
            lambda *args, **kwargs: (args[1] if len(args) > 1 else kwargs.get("mode"))
            == "xb",
            error,
        )
    elif site == "rename":
        fault = _faulty_call(os, "rename", lambda *args, **kwargs: True, error)
    elif site == "replace":
        fault = _faulty_call(os, "replace", lambda *args, **kwargs: True, error)
    elif site == "mkdir":
        fault = _faulty_call(
            os,
            "mkdir",
            lambda *args, **kwargs: "source-v1" in str(args[0]),
            error,
        )
    else:
        fault = _faulty_call(os, "mkdir", lambda *args, **kwargs: True, error)
    with fault as fired:
        _raises(
            source_errors.CODE_SNAPSHOT_UNAVAILABLE,
            lambda: store_module.stage_snapshot(home, SKILL, PACKAGE, new),
        )
    assert fired, "the injected fault was never reached"
    assert _store_tree_hash(store_module.store_root(home)) == before


# Host-specific bounds, probed at runtime.


def test_case_pair_files_follow_the_host_filesystem(tmp_path: Path) -> None:
    """A case-variant file pair stores on sensitive hosts, refuses elsewhere.

    The probe writes both spellings into scratch: when they conflate, the
    second staged write collides and staging refuses structured; when they
    stay distinct, both files store and serve.
    """
    first = tmp_path / "probe-CASE-sensitivity.txt"
    second = tmp_path / "probe-case-sensitivity.txt"
    conflates = _keys_conflate(first, second)
    if conflates is None:
        pytest.skip("cannot probe case behavior on this host (store case bound)")
    home = tmp_path / "home"
    pair = _capture(tmp_path, {"A.txt": (b"upper", False), "a.txt": (b"lower", False)})
    if conflates:
        _raises(
            source_errors.CODE_SNAPSHOT_UNAVAILABLE,
            lambda: store_module.stage_snapshot(home, SKILL, PACKAGE, pair),
        )
        assert _store_tree_hash(store_module.store_root(home)) == "absent"
    else:
        store_module.stage_snapshot(home, SKILL, PACKAGE, pair)
        served = store_module.lookup_snapshot(home, SKILL, PACKAGE)
        assert served.files["A.txt"].data == b"upper"
        assert served.files["a.txt"].data == b"lower"


def test_on_disk_mode_is_not_part_of_the_frozen_identity(tmp_path: Path) -> None:
    if not _can_set_executable(tmp_path):
        pytest.skip("cannot set executable bits on this host (store mode bound)")
    home = tmp_path / "home"
    captured = _capture(tmp_path, {"SKILL.md": (b"sealed", False)})
    store_module.stage_snapshot(home, SKILL, PACKAGE, captured)
    stored = _stored_tree(home) / "SKILL.md"
    stored.chmod(0o755)
    assert stored.stat().st_mode & 0o111
    served = store_module.lookup_snapshot(home, SKILL, PACKAGE)
    assert served.files["SKILL.md"].executable is False
    assert served.snapshot == captured.inventory["snapshot"]


def test_capture_to_serve_end_to_end(tmp_path: Path) -> None:
    """Real capture stages and serves through every consumer backend."""
    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)
    from csk.sources import snapshot as snapshot_module

    home = tmp_path / "home"
    home.mkdir(parents=True)
    root = tmp_path / "src"
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("A", encoding="utf-8")
    (root / "tracked.txt").write_text("B", encoding="utf-8")
    (root / "untracked.txt").write_text("C", encoding="utf-8")
    package = snapshot_module.capture_package_snapshot(root, ".", home=home)
    store_module.stage_snapshot(home, SKILL, PACKAGE, package)
    for opener in consumers.ALL_CONSUMERS:
        served = opener(home, SKILL, PACKAGE)
        assert served.snapshot == package.inventory["snapshot"]
        assert set(served.files) == {"tracked.txt", "untracked.txt"}
    shutil.rmtree(root)
    for opener in consumers.ALL_CONSUMERS:
        assert opener(home, SKILL, PACKAGE).files["tracked.txt"].data == b"B"


# BUG-260922-1ulkcl: the reader retries the transient sharing denial the
# writer already guards, and absence is never decided by re-probing.


def _is_record_read_open(*args: Any, **kwargs: Any) -> bool:
    if not args:
        return False
    try:
        name = os.fspath(args[0])
    except TypeError:
        return False
    if not name.endswith("record.json") or ".record-" in name:
        return False
    mode = kwargs.get("mode", args[1] if len(args) > 1 else "")
    return mode == "rb"


def test_record_read_retries_transient_sharing_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient denial at the reader's open is retried, not refused.

    Mirror of the writer's replace retry: on Windows a record replace in
    flight denies a concurrent reader's open with PermissionError. The
    reader retries within the same bound and serves the record.

    Production call site: ``store.lookup_snapshot`` -> ``store._read_record`` open.
    """
    home = tmp_path / "home"
    captured = _seed_single_file(home, tmp_path)
    real_open = builtins.open
    attempts: list[str] = []
    monkeypatch.setattr(store_module, "_RECORD_REPLACE_BACKOFF_SECONDS", 0)

    def _flaky_open(*args: Any, **kwargs: Any) -> Any:
        if _is_record_read_open(*args, **kwargs) and len(attempts) < 2:
            attempts.append("denied")
            raise PermissionError("[WinError 5] Access is denied")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", _flaky_open)
    served = store_module.lookup_snapshot(home, SKILL, PACKAGE)
    assert served.snapshot == captured.inventory["snapshot"]
    assert len(attempts) == 2


def test_record_read_refuses_immediately_on_non_transient_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-transient read failure is never retried: one attempt, then refuse.

    Only PermissionError is transient. A mutant that widens the retried
    class (e.g. retries OSError) would retry here and serve; correct code
    refuses on the first failure. The record is also held at
    ``exists() == False``: a re-probing mutant would report absence here,
    correct code still reports failure without probing.

    Production call site: ``store.lookup_snapshot`` -> ``store._read_record`` open.
    """
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    real_open = builtins.open
    real_exists = Path.exists
    attempts: list[str] = []
    probed: list[str] = []
    monkeypatch.setattr(store_module, "_RECORD_REPLACE_BACKOFF_SECONDS", 0)

    def _tracking_exists(self: Path) -> bool:
        if self.name == "record.json":
            probed.append(str(self))
            return False
        return real_exists(self)

    def _once_failing_open(*args: Any, **kwargs: Any) -> Any:
        if _is_record_read_open(*args, **kwargs):
            attempts.append("open")
            if len(attempts) == 1:
                raise OSError("injected non-transient")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(Path, "exists", _tracking_exists)
    monkeypatch.setattr(builtins, "open", _once_failing_open)
    detail = _raises(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        lambda: store_module.lookup_snapshot(home, SKILL, PACKAGE),
    ).detail
    assert len(attempts) == 1, "a non-transient failure must not be retried"
    assert probed == [], "failure must not be re-probed as absence"
    assert "cannot be read" in detail
    assert "no locked snapshot is stored" not in detail


def test_record_read_shares_writer_retry_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader's retry bound IS the writer's constant, not a second one.

    Patching ``_RECORD_REPLACE_ATTEMPTS`` changes the reader: with bound 3,
    two denials then success serves, three denials then success refuses. A
    separately spelled reader constant would ignore the patch (still 6)
    and serve in both cases, failing this test.

    Production call site: ``store.lookup_snapshot`` -> ``store._read_record`` open.
    """
    home = tmp_path / "home"
    captured = _seed_single_file(home, tmp_path)
    monkeypatch.setattr(store_module, "_RECORD_REPLACE_ATTEMPTS", 3)
    monkeypatch.setattr(store_module, "_RECORD_REPLACE_BACKOFF_SECONDS", 0)
    real_open = builtins.open

    first: list[str] = []

    def _two_denials(*args: Any, **kwargs: Any) -> Any:
        if _is_record_read_open(*args, **kwargs) and len(first) < 2:
            first.append("denied")
            raise PermissionError("[WinError 5] Access is denied")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", _two_denials)
    served = store_module.lookup_snapshot(home, SKILL, PACKAGE)
    assert served.snapshot == captured.inventory["snapshot"]
    assert len(first) == 2

    second: list[str] = []

    def _three_denials(*args: Any, **kwargs: Any) -> Any:
        if _is_record_read_open(*args, **kwargs) and len(second) < 3:
            second.append("denied")
            raise PermissionError("[WinError 5] Access is denied")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(builtins, "open", _three_denials)
    _raises(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        lambda: store_module.lookup_snapshot(home, SKILL, PACKAGE),
    )
    assert len(second) == 3


def test_record_read_retry_uses_writer_constant() -> None:
    """Static guard: _read_record spells the writer's bound, not a second one.

    The behavioral bound test proves the two cannot disagree at runtime;
    this names the shared spelling so a separately introduced constant
    fails here too.
    """
    import inspect
    import re

    reader_source = inspect.getsource(store_module._read_record)
    writer_source = inspect.getsource(store_module._replace_record)
    assert "_RECORD_REPLACE_ATTEMPTS" in writer_source
    attempts_names = set(re.findall(r"_[A-Z0-9_]*ATTEMPTS\b", reader_source))
    assert attempts_names == {"_RECORD_REPLACE_ATTEMPTS"}, attempts_names
    backoff_names = set(re.findall(r"_[A-Z0-9_]*BACKOFF[A-Z0-9_]*\b", reader_source))
    assert backoff_names == {"_RECORD_REPLACE_BACKOFF_SECONDS"}, backoff_names


def test_denied_record_read_is_never_reported_as_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A denied read is a failure, never 'no locked snapshot is stored'.

    The old shape re-probed with ``path.exists()`` after a failure: a
    transient False turned an I/O failure into the positive claim of
    absence. Correct code distinguishes where each occurs
    (FileNotFoundError vs other errors) and never re-probes.

    Production call site: ``store.lookup_snapshot`` -> ``store._read_record`` open.
    """
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    monkeypatch.setattr(store_module, "_RECORD_REPLACE_BACKOFF_SECONDS", 0)
    real_open = builtins.open
    real_exists = Path.exists
    probed: list[str] = []

    def _tracking_exists(self: Path) -> bool:
        if self.name == "record.json":
            probed.append(str(self))
            return False
        return real_exists(self)

    def _always_denied(*args: Any, **kwargs: Any) -> Any:
        if _is_record_read_open(*args, **kwargs):
            raise PermissionError("[WinError 5] Access is denied")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(Path, "exists", _tracking_exists)
    monkeypatch.setattr(builtins, "open", _always_denied)
    detail = _raises(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        lambda: store_module.lookup_snapshot(home, SKILL, PACKAGE),
    ).detail
    assert "no locked snapshot is stored" not in detail
    assert "cannot be read" in detail
    assert probed == [], "absence must not be decided by re-probing after failure"


def test_absent_record_reports_no_locked_snapshot(tmp_path: Path) -> None:
    """A truly absent record still reports 'no locked snapshot is stored'.

    Companion to the denied-read test: absence (FileNotFoundError at the
    open) keeps the absence message, while any other failure reports
    'cannot be read'. Removing the absence branch fails this test.

    Production call site: ``store.lookup_snapshot`` -> ``store._read_record`` open.
    """
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    (store_module.entry_dir(home, SKILL, PACKAGE) / "record.json").unlink()
    detail = _raises(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        lambda: store_module.lookup_snapshot(home, SKILL, PACKAGE),
    ).detail
    assert "no locked snapshot is stored" in detail


# BUG-260922-1ulkcl revision 2 (H-1): only the OPEN is classified for
# retry/absence. A failure from read() or close() after a successful open
# refuses as a read failure - never retried, never reported as absence.


class _FailingRecordHandle:
    """Wrap a real record handle, failing exactly one post-open operation."""

    def __init__(
        self, real: Any, *, method: str, error: type[BaseException]
    ) -> None:
        self._real = real
        self._method = method
        self._error = error

    def read(self, *args: Any, **kwargs: Any) -> Any:
        if self._method == "read":
            raise self._error("injected post-open read failure")
        return self._real.read(*args, **kwargs)

    def close(self) -> None:
        try:
            self._real.close()
        finally:
            if self._method == "close":
                raise self._error("injected post-open close failure")

    def __enter__(self) -> _FailingRecordHandle:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _inject_post_open_failure(
    monkeypatch: pytest.MonkeyPatch, *, method: str, error: type[BaseException]
) -> list[str]:
    """Fail one post-open operation on the first record open; count opens."""
    real_open = builtins.open
    attempts: list[str] = []
    monkeypatch.setattr(store_module, "_RECORD_REPLACE_BACKOFF_SECONDS", 0)

    def _post_open_failing(*args: Any, **kwargs: Any) -> Any:
        handle = real_open(*args, **kwargs)
        if _is_record_read_open(*args, **kwargs):
            attempts.append("open")
            if len(attempts) == 1:
                return _FailingRecordHandle(handle, method=method, error=error)
        return handle

    monkeypatch.setattr(builtins, "open", _post_open_failing)
    return attempts


@pytest.mark.parametrize("method", ["read", "close"])
def test_record_post_open_permission_error_is_read_failure_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """A PermissionError after a successful open is a read failure, not a retry.

    Only an open-time sharing denial is transient. A mutant that widens the
    open-only try back over read()/close() would retry here and serve on the
    second attempt, hiding the real I/O failure; correct code refuses on the
    first attempt without retrying and without reporting absence.

    Production call site: ``store.lookup_snapshot`` -> ``store._read_record`` read/close.
    """
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    attempts = _inject_post_open_failure(
        monkeypatch, method=method, error=PermissionError
    )
    detail = _raises(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        lambda: store_module.lookup_snapshot(home, SKILL, PACKAGE),
    ).detail
    assert len(attempts) == 1, "a post-open failure must not be retried"
    assert "cannot be read" in detail
    assert "no locked snapshot is stored" not in detail


@pytest.mark.parametrize("method", ["read", "close"])
def test_record_post_open_not_found_is_read_failure_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """A FileNotFoundError after a successful open never asserts absence.

    Absence is established only by FileNotFoundError AT the open. A failure
    after a successful open means the record existed and could not be
    served: reporting 'no locked snapshot is stored' would be a wrong
    answer, not an error. A mutant that widens the open-only try back over
    read()/close() reports absence here and fails this test.

    Production call site: ``store.lookup_snapshot`` -> ``store._read_record`` read/close.
    """
    home = tmp_path / "home"
    _seed_single_file(home, tmp_path)
    attempts = _inject_post_open_failure(
        monkeypatch, method=method, error=FileNotFoundError
    )
    detail = _raises(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        lambda: store_module.lookup_snapshot(home, SKILL, PACKAGE),
    ).detail
    assert len(attempts) == 1, "a post-open failure must not be retried"
    assert "cannot be read" in detail
    assert "no locked snapshot is stored" not in detail
