from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
import pytest

from csk import protocol_json
from csk.build_repository_pipeline import (
    AUDIT_BLOCKED,
    ARTIFACT_INVALID,
    CompilerIdentity,
    DeclaredState,
    DiskProtectedStore,
    EffectiveState,
    ExistingGoV1Session,
    ExternalBuildError,
    OBJECT_SEMANTICS_INVALID,
    Operation,
    PipelineRequest,
    SubstitutionState,
    _frame_snapshot,
    _read_tree,
    _validate_materialized,
    run_pipeline,
    snapshot_key,
)
from csk.git_admission import (
    INCOMPLETE_SOURCE,
    SOURCE_UNAVAILABLE,
    GitAdmissionError,
    Snapshot,
    SnapshotFile,
)


COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _snapshot(*, outside: bytes = b"not-visible", tag_verified: bool = False) -> Snapshot:
    files = tuple(sorted((
        SnapshotFile("docs/secret.txt", outside),
        SnapshotFile("repo/go.mod", b"module example.test/tool\n\ngo 1.25\n"),
        SnapshotFile("repo/cmd/tool/main.go", b"package main\nfunc main() {}\n"),
        SnapshotFile(
            "skill-build.json",
            protocol_json.canonical_bytes(
                {
                    "schema_version": 1,
                    "targets": {
                        "tool": {
                            "driver": "go-repository-v1",
                            "build_root": "repo",
                            "source_dir": "repo/cmd/tool",
                        }
                    },
                }
            ),
        ),
    ), key=lambda item: item.path))
    canonical = _frame(files)
    return Snapshot(
        object_format="sha1",
        commit=COMMIT,
        files=files,
        canonical_bytes=canonical,
        digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        tag_verified=tag_verified,
    )


def _frame(files: tuple[SnapshotFile, ...]) -> bytes:
    result = bytearray(b"curator-build-source-v1\0")
    for item in files:
        path = item.path.encode()
        result.extend(b"F")
        result.extend(len(path).to_bytes(8, "big"))
        result.extend(path)
        result.extend(len(item.content).to_bytes(8, "big"))
        result.extend(item.content)
    return bytes(result)


def _declared(*, tag: str | None = None) -> DeclaredState:
    return DeclaredState(
        repository="tools",
        identity="github.com/example/tools",
        transport="https",
        object_format="sha1",
        commit=COMMIT,
        tag=tag,
    )


def _effective() -> EffectiveState:
    return EffectiveState(
        identity_kind="network-git",
        identity="github.com/example/tools",
        transport="https",
        object_format="sha1",
        commit=COMMIT,
    )


@dataclass
class _Compiler:
    events: list[str]
    calls: int = 0
    visible: tuple[str, ...] = ()
    source_dir: str = ""
    identity: CompilerIdentity = field(
        default_factory=lambda: CompilerIdentity(
            content_sha256="sha256:" + "a" * 64,
            go_version="go1.25.1",
            go_relpath="bin/go",
            goos="darwin",
            goarch="arm64",
            tuning={"GOARM64": "v8.0"},
        )
    )

    def compile(self, root: Path, source_dir: str, command: str) -> bytes:
        self.calls += 1
        self.events.append("compile-call")
        self.visible = tuple(
            path.relative_to(root).as_posix() for path in sorted(root.rglob("*"))
        )
        self.source_dir = source_dir
        assert command == "tool"
        return b"compiled-tool"


def _request(
    tmp_path: Path,
    operation: Operation,
    events: list[str],
    compiler: _Compiler,
    *,
    snapshot: Snapshot | None = None,
    store: DiskProtectedStore | None = None,
) -> PipelineRequest:
    selected = snapshot or _snapshot()
    return PipelineRequest(
        operation=operation,
        command="tool",
        target="tool",
        declared=_declared(),
        effective=_effective(),
        acquire=lambda: selected,
        audit=lambda subject: events.append(f"audit-call:{subject.build_source}"),
        store=store or DiskProtectedStore(tmp_path / "external-cache"),
        compiler=compiler,
        trace=events.append,
    )


@pytest.mark.parametrize(
    ("operation", "expects_cache", "expects_compile"),
    [
        (Operation.INSTALL, True, True),
        (Operation.DRY_RUN, True, False),
        (Operation.REPAIR, True, True),
        (Operation.AUDIT, False, False),
    ],
)
def test_external_audit_precedes_cache_and_compiler_for_all_modes(
    tmp_path: Path,
    operation: Operation,
    expects_cache: bool,
    expects_compile: bool,
) -> None:
    events: list[str] = []
    compiler = _Compiler(events)
    result = run_pipeline(_request(tmp_path, operation, events, compiler))

    audit_index = next(index for index, event in enumerate(events) if event.startswith("audit-call:"))
    assert events.index("whole-snapshot-validation") < audit_index
    assert events.index("build-source-digest") < audit_index
    assert ("artifact-cache-lookup" in events) is expects_cache
    assert ("compile-call" in events) is expects_compile
    if expects_cache:
        assert audit_index < events.index("artifact-cache-lookup")
    if expects_compile:
        assert audit_index < events.index("compile-call")
    assert result.build_source == _snapshot().digest


def test_cache_hit_reacquires_revalidates_and_reaudits_before_lookup(
    tmp_path: Path,
) -> None:
    store = DiskProtectedStore(tmp_path / "external-cache")
    first_events: list[str] = []
    compiler = _Compiler(first_events)
    first = run_pipeline(
        _request(tmp_path, Operation.INSTALL, first_events, compiler, store=store)
    )
    assert first.artifact == b"compiled-tool"
    assert compiler.calls == 1

    events: list[str] = []
    compiler.events = events
    second = run_pipeline(
        _request(tmp_path, Operation.INSTALL, events, compiler, store=store)
    )
    assert second.state == "cache-hit"
    assert compiler.calls == 1
    assert events.index("independent-external-audit") < events.index("artifact-cache-lookup")
    assert next(i for i, item in enumerate(events) if item.startswith("audit-call:")) < events.index("artifact-cache-lookup")


def test_only_descriptor_build_root_is_compiler_visible(tmp_path: Path) -> None:
    events: list[str] = []
    compiler = _Compiler(events)
    run_pipeline(_request(tmp_path, Operation.INSTALL, events, compiler))

    assert "go.mod" in compiler.visible
    assert "cmd/tool/main.go" in compiler.visible
    assert "docs/secret.txt" not in compiler.visible
    assert "skill-build.json" not in compiler.visible
    assert compiler.source_dir == "cmd/tool"


def test_repository_root_build_with_nested_source_reaches_compiler(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    files = tuple(
        SnapshotFile(
            item.path.removeprefix("repo/"),
            item.content,
        )
        for item in snapshot.files
        if item.path != "skill-build.json"
    )
    descriptor = SnapshotFile(
        "skill-build.json",
        protocol_json.canonical_bytes(
            {
                "schema_version": 1,
                "targets": {
                    "tool": {
                        "driver": "go-repository-v1",
                        "build_root": ".",
                        "source_dir": "cmd/tool",
                    }
                },
            }
        ),
    )
    files = tuple(sorted((*files, descriptor), key=lambda item: item.path))
    canonical = _frame(files)
    root_snapshot = Snapshot(
        object_format=snapshot.object_format,
        commit=snapshot.commit,
        files=files,
        canonical_bytes=canonical,
        digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
        tag_verified=snapshot.tag_verified,
    )
    events: list[str] = []
    compiler = _Compiler(events)

    run_pipeline(
        _request(
            tmp_path,
            Operation.INSTALL,
            events,
            compiler,
            snapshot=root_snapshot,
        )
    )

    assert "go.mod" in compiler.visible
    assert "cmd/tool/main.go" in compiler.visible
    assert compiler.source_dir == "cmd/tool"


def test_exact_protected_snapshot_supports_untagged_offline_reinstall(
    tmp_path: Path,
) -> None:
    store = DiskProtectedStore(tmp_path / "external-cache")
    events: list[str] = []
    compiler = _Compiler(events)
    first = run_pipeline(
        _request(tmp_path, Operation.INSTALL, events, compiler, store=store)
    )
    assert first.snapshot_key is not None

    offline_events: list[str] = []
    compiler.events = offline_events
    request = _request(
        tmp_path, Operation.INSTALL, offline_events, compiler, store=store
    )
    request = PipelineRequest(
        **{
            **request.__dict__,
            "acquire": lambda: (_ for _ in ()).throw(
                GitAdmissionError(SOURCE_UNAVAILABLE, "offline")
            ),
            "offline_snapshot_key": first.snapshot_key,
        }
    )
    result = run_pipeline(request)
    assert result.state == "cache-hit"
    assert any(item.startswith("audit-call:") for item in offline_events)
    assert compiler.calls == 1


def test_incomplete_source_cannot_fall_back_to_protected_snapshot(
    tmp_path: Path,
) -> None:
    store = DiskProtectedStore(tmp_path / "external-cache")
    compiler = _Compiler([])
    first = run_pipeline(
        _request(tmp_path, Operation.INSTALL, [], compiler, store=store)
    )
    assert first.snapshot_key is not None

    request = _request(tmp_path, Operation.INSTALL, [], compiler, store=store)
    request = PipelineRequest(
        **{
            **request.__dict__,
            "acquire": lambda: (_ for _ in ()).throw(
                GitAdmissionError(INCOMPLETE_SOURCE, "malformed fetched graph")
            ),
            "offline_snapshot_key": first.snapshot_key,
        }
    )
    with pytest.raises(GitAdmissionError) as raised:
        run_pipeline(request)

    assert raised.value.code == INCOMPLETE_SOURCE
    assert compiler.calls == 1


def test_tagged_source_cannot_use_offline_snapshot_without_fresh_tag_proof(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tag_verified=True)
    store = DiskProtectedStore(tmp_path / "external-cache")
    key = snapshot_key(_effective(), snapshot.digest)
    store.store_snapshot(key, snapshot)
    compiler = _Compiler([])
    with pytest.raises(ExternalBuildError) as raised:
        run_pipeline(
            PipelineRequest(
                operation=Operation.INSTALL,
                command="tool",
                target="tool",
                declared=_declared(tag="v1.0.0"),
                effective=_effective(),
                acquire=lambda: (_ for _ in ()).throw(
                    GitAdmissionError(SOURCE_UNAVAILABLE, "offline")
                ),
                audit=lambda _subject: None,
                store=store,
                compiler=compiler,
                offline_snapshot_key=key,
            )
        )
    assert raised.value.code == "build_repository_source_unavailable"
    assert compiler.calls == 0


def test_substitution_has_distinct_snapshot_and_artifact_identity(tmp_path: Path) -> None:
    base_events: list[str] = []
    compiler = _Compiler(base_events)
    base = _request(tmp_path, Operation.DRY_RUN, base_events, compiler)
    plain = run_pipeline(base)
    substituted = PipelineRequest(
        **{
            **base.__dict__,
            "effective": EffectiveState(
                identity_kind="operator-local-git",
                identity="sha256:" + "b" * 64,
                transport=None,
                object_format="sha1",
                commit=COMMIT,
                substituted=True,
                substitution=SubstitutionState(type="local-path"),
            ),
        }
    )
    changed = run_pipeline(substituted)
    assert plain.snapshot_key != changed.snapshot_key
    assert plain.cache_key != changed.cache_key


@pytest.mark.parametrize(
    ("goos", "goarch", "stored_name", "receipt_path"),
    [
        ("darwin", "arm64", "artifact", "bin/tool"),
        ("linux", "amd64", "artifact", "bin/tool"),
        ("windows", "amd64", "artifact.exe", "bin/tool.exe"),
    ],
)
def test_stored_artifact_carries_the_suffix_its_receipt_declares(
    tmp_path: Path,
    goos: str,
    goarch: str,
    stored_name: str,
    receipt_path: str,
) -> None:
    # A Windows launcher can only run a name Windows recognizes as executable,
    # so the stored file has to follow the receipt rather than a fixed literal.
    store = DiskProtectedStore(tmp_path / "external-cache")
    compiler = _Compiler(
        [],
        identity=CompilerIdentity(
            content_sha256="sha256:" + "a" * 64,
            go_version="go1.25.1",
            go_relpath="bin/go",
            goos=goos,
            goarch=goarch,
            tuning={},
        ),
    )
    first = run_pipeline(
        _request(tmp_path, Operation.INSTALL, [], compiler, store=store)
    )

    assert first.cache_key is not None and first.receipt is not None
    entry = store.root / "artifacts" / first.cache_key.removeprefix("sha256:")
    assert sorted(path.name for path in entry.iterdir()) == [
        stored_name,
        "receipt.json",
    ]
    assert (entry / stored_name).read_bytes() == b"compiled-tool"
    receipt = protocol_json.loads_canonical(first.receipt)
    assert isinstance(receipt, dict)
    assert receipt["artifact"] == {
        "path": receipt_path,
        "sha256": "sha256:" + hashlib.sha256(b"compiled-tool").hexdigest(),
        "size": len(b"compiled-tool"),
    }

    second = run_pipeline(
        _request(tmp_path, Operation.INSTALL, [], compiler, store=store)
    )
    assert second.state == "cache-hit"
    assert compiler.calls == 1
    assert not (store.root / "quarantine").exists()


def test_corrupt_artifact_is_quarantined_before_rebuild(tmp_path: Path) -> None:
    store = DiskProtectedStore(tmp_path / "external-cache")
    events: list[str] = []
    compiler = _Compiler(events)
    first = run_pipeline(
        _request(tmp_path, Operation.INSTALL, events, compiler, store=store)
    )
    assert first.cache_key is not None
    entry = store.root / "artifacts" / first.cache_key.removeprefix("sha256:")
    artifact = entry / "artifact"
    artifact.chmod(0o700)
    artifact.write_bytes(b"corrupt")

    second = run_pipeline(
        _request(tmp_path, Operation.REPAIR, [], compiler, store=store)
    )
    assert second.state == "would-rebuild-untrusted-cache"
    assert second.code == ARTIFACT_INVALID
    assert compiler.calls == 2
    assert any((store.root / "quarantine").iterdir())


def test_dry_run_detects_corruption_without_quarantine(tmp_path: Path) -> None:
    store = DiskProtectedStore(tmp_path / "external-cache")
    compiler = _Compiler([])
    first = run_pipeline(
        _request(tmp_path, Operation.INSTALL, [], compiler, store=store)
    )
    assert first.cache_key is not None
    entry = store.root / "artifacts" / first.cache_key.removeprefix("sha256:")
    artifact = entry / "artifact"
    artifact.chmod(0o700)
    artifact.write_bytes(b"corrupt")

    result = run_pipeline(
        _request(tmp_path, Operation.DRY_RUN, [], compiler, store=store)
    )
    assert result.state == "corrupt"
    assert result.code == ARTIFACT_INVALID
    assert entry.exists()
    assert not (store.root / "quarantine").exists()


def test_corrupt_offline_snapshot_is_quarantined_and_cannot_compile(
    tmp_path: Path,
) -> None:
    store = DiskProtectedStore(tmp_path / "external-cache")
    snapshot = _snapshot()
    key = snapshot_key(_effective(), snapshot.digest)
    store.store_snapshot(key, snapshot)
    entry = store.root / "snapshots" / key.removeprefix("sha256:")
    selected = entry / "files" / "repo" / "go.mod"
    selected.chmod(0o600)
    selected.write_bytes(b"corrupt")
    compiler = _Compiler([])

    with pytest.raises(ExternalBuildError) as raised:
        run_pipeline(
            PipelineRequest(
                operation=Operation.INSTALL,
                command="tool",
                target="tool",
                declared=_declared(),
                effective=_effective(),
                acquire=lambda: (_ for _ in ()).throw(
                    GitAdmissionError(SOURCE_UNAVAILABLE, "offline")
                ),
                audit=lambda _subject: None,
                store=store,
                compiler=compiler,
                offline_snapshot_key=key,
            )
        )
    assert raised.value.code == "build_repository_source_unavailable"
    assert compiler.calls == 0
    assert not entry.exists()
    assert any((store.root / "quarantine").iterdir())


def test_corrupt_snapshot_is_quarantined_and_republished_from_online_source(
    tmp_path: Path,
) -> None:
    store = DiskProtectedStore(tmp_path / "external-cache")
    compiler = _Compiler([])
    first = run_pipeline(
        _request(tmp_path, Operation.INSTALL, [], compiler, store=store)
    )
    assert first.snapshot_key is not None
    entry = store.root / "snapshots" / first.snapshot_key.removeprefix("sha256:")
    selected = entry / "files" / "repo" / "go.mod"
    selected.chmod(0o600)
    selected.write_bytes(b"corrupt")

    second = run_pipeline(
        _request(tmp_path, Operation.INSTALL, [], compiler, store=store)
    )

    assert second.state == "cache-hit"
    assert compiler.calls == 1
    assert entry.is_dir()
    assert (entry / "files" / "repo" / "go.mod").read_bytes() == (
        b"module example.test/tool\n\ngo 1.25\n"
    )
    assert any((store.root / "quarantine").iterdir())


def test_external_compiler_adapter_reuses_exact_go_v1_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"binary")
    native_session = SimpleNamespace(
        toolchain=SimpleNamespace(
            content_sha256="sha256:" + "c" * 64,
            go_version="go1.25.1",
            go_relpath="bin/go",
        ),
        target=SimpleNamespace(
            goos="darwin", goarch="arm64", tuning={"GOARM64": "v8.0"}
        ),
    )
    observed: list[object] = []

    def fake_build(request: object) -> object:
        observed.append(request)
        return SimpleNamespace(
            artifact=SimpleNamespace(staged_path=artifact)
        )

    monkeypatch.setattr("csk.build_repository_pipeline.go_v1.build", fake_build)
    root = tmp_path / "root"
    root.mkdir()
    (root / "go.mod").write_text("module example.test/tool\n", encoding="utf-8")
    (root / "cmd").mkdir()
    adapter = ExistingGoV1Session(native_session)  # type: ignore[arg-type]
    result = adapter.compile(root, "cmd", "tool")

    assert result == b"binary"
    assert len(observed) == 1
    assert observed[0].toolchain_session is native_session  # type: ignore[union-attr]
    assert observed[0].command_object["driver"] == "go-v1"  # type: ignore[union-attr]


def test_audit_failure_blocks_store_lookup_and_compiler(tmp_path: Path) -> None:
    compiler = _Compiler([])
    request = _request(tmp_path, Operation.INSTALL, [], compiler)
    request = PipelineRequest(
        **{
            **request.__dict__,
            "audit": lambda _subject: (_ for _ in ()).throw(RuntimeError("deny")),
        }
    )
    with pytest.raises(ExternalBuildError) as raised:
        run_pipeline(request)
    assert raised.value.code == AUDIT_BLOCKED
    assert compiler.calls == 0
    assert not (tmp_path / "external-cache").exists()


def test_receipt_binds_policy_declared_and_effective_identity(tmp_path: Path) -> None:
    events: list[str] = []
    compiler = _Compiler(events)
    result = run_pipeline(_request(tmp_path, Operation.INSTALL, events, compiler))
    assert result.receipt is not None
    receipt = protocol_json.loads_canonical(result.receipt)
    source = receipt["input"]["source"]
    assert source["declared"]["identity"] == {
        "kind": "network-git",
        "value": "github.com/example/tools",
    }
    assert source["effective"]["build_source"] == {
        "algorithm": "curator-build-source-v1",
        "content_sha256": result.build_source,
    }
    assert receipt["input"]["policy"]["source_kind"] == "locked-external-git-v1"


# Regression fixture for BUG-260807.  Names chosen so that both orderings the
# fix removes are observable in one tree:
#   * "README.md" vs "cmd..." separates only under Windows case folding;
#   * "cmd.go" vs "cmd/tool.go" separates only under Path component ordering,
#     which puts the directory "cmd" — and therefore "cmd/tool.go" — before the
#     sibling file "cmd.go" on every platform.
# The admitted order is the one git_admission._prove_repository frames: sorted
# on the UTF-8 bytes of the relative POSIX path.
_ORDERING_TREE = (
    ("README.md", b"readme\n"),
    ("cmd.go", b"package main\n"),
    ("cmd/tool.go", b"package cmd\n"),
    ("go.mod", b"module example.test/tool\n"),
)
_ADMITTED_ORDER = ["README.md", "cmd.go", "cmd/tool.go", "go.mod"]
_PATH_COMPONENT_ORDER = ["README.md", "cmd/tool.go", "cmd.go", "go.mod"]
_WINDOWS_CASE_FOLDED_ORDER = ["cmd/tool.go", "cmd.go", "go.mod", "README.md"]


def _ordering_snapshot(order: list[str] | None = None) -> Snapshot:
    contents = dict(_ORDERING_TREE)
    if order is None:
        order = sorted(contents, key=lambda path: path.encode("utf-8"))
    files = tuple(SnapshotFile(path, contents[path]) for path in order)
    canonical = _frame(files)
    return Snapshot(
        object_format="sha1",
        commit=COMMIT,
        files=files,
        canonical_bytes=canonical,
        digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
    )


def test_materialized_order_matches_admitted_order_for_colliding_names(
    tmp_path: Path,
) -> None:
    """Regression for BUG-260807: reading a materialized tree must not reorder it."""
    snapshot = _ordering_snapshot()
    assert [item.path for item in snapshot.files] == _ADMITTED_ORDER
    root = tmp_path / "snapshot"
    snapshot.materialize(root)

    read = _read_tree(root, lambda _info, _directory: True, protected=False)

    assert [item.path for item in read] == _ADMITTED_ORDER
    assert _frame_snapshot(read) == snapshot.canonical_bytes
    _validate_materialized(root, snapshot)


def test_path_object_ordering_would_break_the_materialized_digest(
    tmp_path: Path,
) -> None:
    """Both orderings the fix removes reproduce the reported install signature."""
    root = tmp_path / "snapshot"
    _ordering_snapshot().materialize(root)

    # What sorting Path objects yields, spelled out rather than taken from
    # sorted(Path): PurePath compares _parts_normcase, and which of these two
    # the running flavour picks is itself the platform dependency under test.
    def ordering(fold: bool) -> list[str]:
        def key(item: Path) -> tuple[str, ...]:
            parts = item.relative_to(root).parts
            return tuple(part.lower() for part in parts) if fold else parts

        return [
            item.relative_to(root).as_posix()
            for item in sorted(root.rglob("*"), key=key)
            if item.is_file()
        ]

    component_order = ordering(fold=False)
    case_folded_order = ordering(fold=True)
    native_order = [
        item.relative_to(root).as_posix()
        for item in sorted(root.rglob("*"))
        if item.is_file()
    ]
    assert component_order == _PATH_COMPONENT_ORDER
    assert case_folded_order == _WINDOWS_CASE_FOLDED_ORDER
    # Whichever flavour is running, sorting Path objects is the wrong order.
    assert native_order in (_PATH_COMPONENT_ORDER, _WINDOWS_CASE_FOLDED_ORDER)
    assert native_order != _ADMITTED_ORDER

    for order in (component_order, case_folded_order):
        with pytest.raises(ExternalBuildError) as captured:
            _validate_materialized(root, _ordering_snapshot(order))
        assert captured.value.code == OBJECT_SEMANTICS_INVALID
