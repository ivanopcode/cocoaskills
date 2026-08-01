"""Observed CocoaSkills bindings for the RC6 manager lifecycle vectors.

The shared vector is an expectation, never a source of lifecycle answers.  This
module constructs independent operations against CocoaSkills seams and projects
their traces into the protocol vocabulary.  The projection is cached because a
single run covers all 32 cases and the conformance test parametrizes by case.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from conftest import make_config, make_project, make_skill_repo, write_files, write_skillfile
from test_build_currentness import _build_row, _installed_build, _write_marker
from test_installer_transactions import (
    _build_skill_files,
    _install_fake_build_pipeline,
    _native_target,
    _tree_state,
)

from csk import (
    cli,
    closure,
    config as config_mod,
    consumers,
    gc,
    global_install,
    install_marker,
    installer,
    locking,
    shims,
    status as status_mod,
    transactions,
)
from csk.audit import pipeline as audit_pipeline
from csk.builds import cache, go_v1, metadata, planner, source as build_source, toolchain


JsonObject = dict[str, Any]


def _record_process_paths(
    command: object,
    sink: list[Path],
    *,
    roots: tuple[Path, ...] = (),
    exact_paths: set[Path] | None = None,
    cwd: object | None = None,
) -> None:
    """Record every protected argv path at an observed process boundary."""

    raw_parts = command if isinstance(command, (list, tuple)) else (command,)
    resolved_roots = tuple(root.resolve(strict=False) for root in roots)
    resolved_exact = (
        {path.resolve(strict=False) for path in exact_paths}
        if exact_paths is not None
        else set()
    )
    try:
        raw_cwd = Path.cwd() if cwd is None else Path(os.fsdecode(os.fspath(cwd)))
    except (TypeError, ValueError):
        process_cwd: Path | None = None
    else:
        process_cwd = (
            raw_cwd
            if raw_cwd.is_absolute()
            else Path.cwd() / raw_cwd
        ).resolve(strict=False)
    for raw in raw_parts:
        try:
            path_text = os.fsdecode(os.fspath(raw))
            candidate = Path(path_text)
        except (TypeError, ValueError):
            continue
        if not candidate.is_absolute():
            separators = tuple(
                separator
                for separator in (os.sep, os.altsep)
                if separator is not None
            )
            if process_cwd is None or not any(
                separator in path_text for separator in separators
            ):
                continue
            candidate = process_cwd / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            continue
        if resolved in resolved_exact or any(
            resolved.is_relative_to(root) for root in resolved_roots
        ):
            sink.append(resolved)


def _project_identity_label(actual: str, expected: Path, label: str) -> str:
    """Project one exact canonical filesystem identity into protocol vocabulary."""

    return (
        label
        if actual == locking.canonical_project_identity(expected)
        else "unexpected"
    )


def _observed_path(path: object, *, dir_fd: int | None = None) -> Path | None:
    """Resolve a mutation argument, including common descriptor-relative paths."""

    try:
        candidate = Path(os.fspath(path))
    except (TypeError, ValueError):
        return None
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    if dir_fd is not None:
        for descriptor_root in ("/dev/fd", "/proc/self/fd"):
            try:
                base = Path(os.readlink(f"{descriptor_root}/{dir_fd}"))
            except OSError:
                continue
            return (base / candidate).resolve(strict=False)
    return (Path.cwd() / candidate).resolve(strict=False)


def _install_persistent_mutation_observer(
    monkeypatch: pytest.MonkeyPatch,
    roots: tuple[Path, ...],
    sink: list[str],
    *,
    on_mutation: Callable[[Path], None] | None = None,
) -> None:
    """Trace high- and low-level mutations, including descriptor-relative ones."""

    protected = tuple(root.resolve(strict=False) for root in roots)

    def record(
        operation: str,
        path: object,
        *,
        dir_fd: int | None = None,
    ) -> None:
        candidate = _observed_path(path, dir_fd=dir_fd)
        if candidate is None:
            return
        if any(
            candidate == root or candidate.is_relative_to(root)
            for root in protected
        ):
            if on_mutation is not None:
                on_mutation(candidate)
            try:
                info = candidate.lstat()
            except OSError:
                identity = "missing"
            else:
                identity = f"{info.st_dev}:{info.st_ino}"
            sink.append(f"{operation}:{identity}:{candidate}")

    def wrap_path_method(
        name: str,
        original: Callable[..., Any],
    ) -> Callable[..., Any]:
        def observed(path: Path, *args: Any, **kwargs: Any) -> Any:
            record(f"path-{name}", path)
            if name in {"rename", "replace"} and args:
                record(f"path-{name}-destination", args[0])
            return original(path, *args, **kwargs)

        return observed

    for method_name in (
        "chmod",
        "hardlink_to",
        "mkdir",
        "rename",
        "replace",
        "rmdir",
        "symlink_to",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    ):
        original_method = getattr(Path, method_name)
        monkeypatch.setattr(
            Path,
            method_name,
            wrap_path_method(method_name, original_method),
        )

    real_os_chmod = os.chmod
    real_os_fchmod = os.fchmod
    real_os_open = os.open
    real_os_write = os.write
    real_os_unlink = os.unlink
    real_os_remove = os.remove
    real_os_mkdir = os.mkdir
    real_os_rmdir = os.rmdir
    real_os_rename = os.rename
    real_os_replace = os.replace
    real_os_link = os.link
    real_os_symlink = os.symlink
    real_os_truncate = os.truncate
    real_os_ftruncate = os.ftruncate
    real_os_utime = os.utime

    def record_descriptor(operation: str, fd: int) -> None:
        for descriptor_root in ("/dev/fd", "/proc/self/fd"):
            try:
                target = os.readlink(f"{descriptor_root}/{fd}")
            except OSError:
                continue
            record(operation, target)
            return

    mutating_open_flags = (
        os.O_WRONLY
        | os.O_RDWR
        | os.O_APPEND
        | os.O_CREAT
        | os.O_EXCL
        | os.O_TRUNC
    )

    def observed_os_open(
        path: Any,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if flags & mutating_open_flags:
            record("os-open-mutate", path, dir_fd=dir_fd)
        return real_os_open(path, flags, mode, dir_fd=dir_fd)

    def observed_os_write(fd: int, data: Any) -> int:
        record_descriptor("os-write", fd)
        return real_os_write(fd, data)

    def observed_os_unlink(
        path: Any,
        *,
        dir_fd: int | None = None,
    ) -> None:
        record("os-unlink", path, dir_fd=dir_fd)
        real_os_unlink(path, dir_fd=dir_fd)

    def observed_os_remove(
        path: Any,
        *,
        dir_fd: int | None = None,
    ) -> None:
        record("os-remove", path, dir_fd=dir_fd)
        real_os_remove(path, dir_fd=dir_fd)

    def observed_os_mkdir(
        path: Any,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        record("os-mkdir", path, dir_fd=dir_fd)
        real_os_mkdir(path, mode, dir_fd=dir_fd)

    def observed_os_rmdir(
        path: Any,
        *,
        dir_fd: int | None = None,
    ) -> None:
        record("os-rmdir", path, dir_fd=dir_fd)
        real_os_rmdir(path, dir_fd=dir_fd)

    def observed_os_rename(
        source: Any,
        destination: Any,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        record("os-rename-source", source, dir_fd=src_dir_fd)
        record("os-rename-destination", destination, dir_fd=dst_dir_fd)
        real_os_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def observed_os_replace(
        source: Any,
        destination: Any,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        record("os-replace-source", source, dir_fd=src_dir_fd)
        record("os-replace-destination", destination, dir_fd=dst_dir_fd)
        real_os_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def observed_os_link(
        source: Any,
        destination: Any,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        record("os-link-source", source, dir_fd=src_dir_fd)
        record("os-link-destination", destination, dir_fd=dst_dir_fd)
        real_os_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    def observed_os_symlink(
        source: Any,
        destination: Any,
        target_is_directory: bool = False,
        *,
        dir_fd: int | None = None,
    ) -> None:
        record("os-symlink-destination", destination, dir_fd=dir_fd)
        real_os_symlink(
            source,
            destination,
            target_is_directory=target_is_directory,
            dir_fd=dir_fd,
        )

    def observed_os_truncate(path: Any, length: int) -> None:
        if isinstance(path, int):
            record_descriptor("os-truncate", path)
        else:
            record("os-truncate", path)
        real_os_truncate(path, length)

    def observed_os_ftruncate(fd: int, length: int) -> None:
        record_descriptor("os-ftruncate", fd)
        real_os_ftruncate(fd, length)

    def observed_os_utime(
        path: Any,
        times: tuple[float, float] | None = None,
        *,
        ns: tuple[int, int] | None = None,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        record("os-utime", path, dir_fd=dir_fd)
        if ns is not None:
            real_os_utime(
                path,
                ns=ns,
                dir_fd=dir_fd,
                follow_symlinks=follow_symlinks,
            )
        else:
            real_os_utime(
                path,
                times,
                dir_fd=dir_fd,
                follow_symlinks=follow_symlinks,
            )

    def observed_os_chmod(
        path: Any,
        mode: int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        record("os-chmod", path, dir_fd=dir_fd)
        real_os_chmod(
            path,
            mode,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )

    def observed_os_fchmod(fd: int, mode: int) -> None:
        record_descriptor("os-fchmod", fd)
        real_os_fchmod(fd, mode)

    monkeypatch.setattr(os, "open", observed_os_open)
    monkeypatch.setattr(os, "write", observed_os_write)
    monkeypatch.setattr(os, "unlink", observed_os_unlink)
    monkeypatch.setattr(os, "remove", observed_os_remove)
    monkeypatch.setattr(os, "mkdir", observed_os_mkdir)
    monkeypatch.setattr(os, "rmdir", observed_os_rmdir)
    monkeypatch.setattr(os, "rename", observed_os_rename)
    monkeypatch.setattr(os, "replace", observed_os_replace)
    monkeypatch.setattr(os, "link", observed_os_link)
    monkeypatch.setattr(os, "symlink", observed_os_symlink)
    monkeypatch.setattr(os, "truncate", observed_os_truncate)
    monkeypatch.setattr(os, "ftruncate", observed_os_ftruncate)
    monkeypatch.setattr(os, "utime", observed_os_utime)
    monkeypatch.setattr(os, "chmod", observed_os_chmod)
    monkeypatch.setattr(os, "fchmod", observed_os_fchmod)
    support_mapping = {
        real_os_open: observed_os_open,
        real_os_unlink: observed_os_unlink,
        real_os_remove: observed_os_remove,
        real_os_mkdir: observed_os_mkdir,
        real_os_rmdir: observed_os_rmdir,
        real_os_rename: observed_os_rename,
        real_os_replace: observed_os_replace,
        real_os_link: observed_os_link,
        real_os_symlink: observed_os_symlink,
        real_os_truncate: observed_os_truncate,
        real_os_ftruncate: observed_os_ftruncate,
        real_os_utime: observed_os_utime,
        real_os_chmod: observed_os_chmod,
        real_os_fchmod: observed_os_fchmod,
    }
    for support_name in (
        "supports_dir_fd",
        "supports_effective_ids",
        "supports_fd",
        "supports_follow_symlinks",
    ):
        supported = getattr(os, support_name)
        monkeypatch.setattr(
            os,
            support_name,
            {support_mapping.get(function, function) for function in supported},
        )


def observe_manager_lifecycle_case(
    name: str,
    compiled_build_fixture: JsonObject,
) -> JsonObject:
    """Return one complete case reconstructed from observed CocoaSkills state."""

    fixture_raw = json.dumps(
        compiled_build_fixture,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    observed = _observe_manager_lifecycle(fixture_raw)
    if name not in observed:
        raise AssertionError(f"no observed CocoaSkills lifecycle binding for {name!r}")
    return deepcopy(observed[name])


def clear_manager_lifecycle_observation_cache() -> None:
    """Clear observations so seam-sabotage tests cannot inherit cached evidence."""

    _observe_manager_lifecycle.cache_clear()


@lru_cache(maxsize=None)
def _observe_manager_lifecycle(fixture_raw: str) -> dict[str, JsonObject]:
    fixture = json.loads(fixture_raw)
    identities = _observe_fixture_identities(fixture)
    root = Path(tempfile.mkdtemp(prefix="csk-rc6-lifecycle-"))
    try:
        observed: dict[str, JsonObject] = {}
        _observe_bootstrap(root / "bootstrap", observed)
        _observe_build_order(observed)
        _observe_cache_publication(root / "cache", identities, observed)
        _observe_cross_project(root / "cross-project", identities, observed)
        _observe_dry_run(root / "dry-run", identities, observed)
        _observe_gc(root / "gc", identities, observed)
        _observe_launchers(root / "launchers", observed)
        _observe_planning(root / "planning", observed)
        _observe_private_builds(root / "private-builds", identities, observed)
        _observe_recovery(root / "recovery", identities, observed)
        _observe_status_and_repair(root / "status-repair", identities, observed)
        _observe_transactions(root / "transactions", identities, observed)
        _observe_upgrade(root / "upgrade", observed)
        assert len(observed) == 32
        return observed
    finally:
        _make_tree_writable(root)
        shutil.rmtree(root, ignore_errors=True)


def _observe_fixture_identities(fixture: JsonObject) -> JsonObject:
    build_input = metadata.parse_build_input(fixture["build_input"])
    receipt = metadata.parse_receipt(fixture["stored_receipt"])
    receipt_raw = metadata.canonical_receipt_bytes(receipt)
    observed = {
        "build_input": build_input,
        "cache_key": metadata.cache_key(build_input),
        "receipt": receipt,
        "receipt_bytes": receipt_raw,
        "receipt_sha256": metadata.receipt_sha256(receipt_raw),
    }
    assert receipt.input == build_input
    assert observed["cache_key"] == fixture["cache_key"]
    assert observed["receipt_sha256"] == fixture["receipt_sha256"]
    return observed


def _observe_bootstrap(root: Path, observed: dict[str, JsonObject]) -> None:
    root.mkdir(parents=True)
    with pytest.MonkeyPatch.context() as monkeypatch:
        missing = root / "missing" / "config.json"
        monkeypatch.setenv("CSK_CONFIG", str(missing))
        exit_code, _stdout, _stderr = _run_cli(
            [
                "bootstrap",
                "--if-missing",
                "--non-interactive",
                "--skills-root",
                str(root / "skills"),
            ]
        )
        created = exit_code == cli.EXIT_OK and missing.is_file()
        observed["missing-config-if-missing"] = {
            "config": "missing" if created else "unexpected",
            "force": False,
            "if_missing": True,
            "name": "missing-config-if-missing",
            "outcome": "created" if created else "not-created",
        }

        existing = root / "existing" / "config.json"
        existing.parent.mkdir()
        existing.write_bytes(b"deliberately invalid but existing\n")
        original = existing.read_bytes()
        monkeypatch.setenv("CSK_CONFIG", str(existing))
        exit_code, stdout, _stderr = _run_cli(
            ["bootstrap", "--if-missing", "--non-interactive"]
        )
        unchanged = (
            exit_code == cli.EXIT_OK
            and existing.read_bytes() == original
            and "Kept existing config" in stdout
        )
        observed["existing-config-if-missing"] = {
            "config": "existing-invalid" if unchanged else "changed",
            "force": False,
            "if_missing": True,
            "name": "existing-config-if-missing",
            "outcome": "unchanged-success" if unchanged else "changed",
        }

        incompatible = root / "incompatible" / "config.json"
        monkeypatch.setenv("CSK_CONFIG", str(incompatible))
        exit_code, _stdout, stderr = _run_cli(
            ["bootstrap", "--if-missing", "--force"]
        )
        usage_error = (
            exit_code == 2
            and "not allowed with argument" in stderr
            and not incompatible.exists()
        )
        observed["if-missing-with-force"] = {
            "config": "either",
            "force": True,
            "if_missing": True,
            "name": "if-missing-with-force",
            "outcome": "usage-error" if usage_error else "unexpected",
        }


def _observe_build_order(observed: dict[str, JsonObject]) -> None:
    active = {
        "app": ["golden-tool"],
        "data-provider": ["zeta-tool", "alpha-tool", "é-tool"],
        "ui-provider": ["beta-tool"],
    }
    nodes = {
        name: SimpleNamespace(name=name, edges=[])
        for name in active
    }
    declared_edges = (
        ("ui-provider", "app"),
        ("data-provider", "app"),
    )
    edges: list[JsonObject] = []
    for provider, consumer in declared_edges:
        nodes[provider].edges.append(
            closure.ActivationEdge(consumer=consumer, mode="full")
        )
        edges.append({"consumer": consumer, "provider": provider})
    provider_order = [node.name for node in closure._topological_order(nodes)]
    build_order = [
        f"{provider}/{command}"
        for provider in provider_order
        for command in sorted(active[provider], key=lambda value: value.encode("utf-8"))
    ]
    observed["provider-first-and-lexical-command-order"] = {
        "active_build_commands": active,
        "closure_edges": edges,
        "expected_build_order": build_order,
        "expected_provider_order": provider_order,
        "name": "provider-first-and-lexical-command-order",
        "ordering": "provider-first-kahn-then-unicode-scalar-command-name",
    }


def _publication(
    root: Path,
    build_input: metadata.GoBuildInput,
    payload: bytes,
    *,
    suffix: str,
) -> cache.CachePublication:
    artifact = root / "private" / suffix
    artifact.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    artifact.write_bytes(payload)
    artifact.chmod(0o700)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    receipt = metadata.build_receipt(
        build_input,
        metadata.BuildArtifact(
            path=build_input.artifact_path,
            sha256=digest,
            size=len(payload),
        ),
    )
    return cache.CachePublication(
        input=build_input,
        receipt_bytes=metadata.canonical_receipt_bytes(receipt),
        artifact_source=artifact,
    )


def _install_identity_build_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    build_input: metadata.GoBuildInput,
    *,
    events: list[str],
    fail_command: str | None = None,
) -> None:
    """Install a fake compiler whose logical platform identity is normative."""

    class FakeSession:
        target = build_input.target
        toolchain = build_input.toolchain

        def __init__(self, config: toolchain.ToolchainConfig):
            self.operation_root = config.private_base / "operation"
            self.operation_root.mkdir(mode=0o700)
            self.executable = self.operation_root / "go"
            self.goroot = self.operation_root / "goroot"

        def __enter__(self) -> FakeSession:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_build(request: go_v1.BuildRequest) -> go_v1.BuildResult:
        events.append(f"build:{request.command}")
        if request.command == fail_command:
            raise go_v1.GoV1Error(
                "fixture_build_failure",
                f"forced failure for {request.command}",
            )
        payload = (
            f"#!/bin/sh\nprintf '%s\\n' {request.command}\n"
        ).encode()
        artifact_path = request.toolchain_session.operation_root / (
            f"artifact-{request.command}"
        )
        artifact_path.write_bytes(payload)
        artifact_path.chmod(0o700)
        return go_v1.BuildResult(
            artifact=go_v1.BuildArtifact(
                staged_path=artifact_path,
                metadata=go_v1.ArtifactMetadata(
                    path=metadata.derived_artifact_path(
                        request.command,
                        goos=request.toolchain_session.target.goos,
                    ),
                    sha256=metadata.sha256_identity(payload),
                    size=len(payload),
                ),
            ),
            capability_evidence=go_v1.CapabilityEvidence(
                record_version="capability-evidence-v1",
                execution_policy="manager-worker-v1",
                platform=request.toolchain_session.target.goos,
                controls=(),
            ),
        )

    monkeypatch.setattr(
        toolchain,
        "capture_operator_search_path",
        lambda: toolchain.OperatorSearchPath(("/fixture/bin",)),
    )
    monkeypatch.setattr(toolchain, "establish_toolchain", FakeSession)
    monkeypatch.setattr(go_v1, "build", fake_build)


def _install_normative_lifecycle_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    identities: JsonObject,
    events: list[str],
    *,
    fail_command: str | None = None,
) -> None:
    """Bind a golden-tool install to the authenticated logical identity."""

    _install_identity_build_pipeline(
        monkeypatch,
        identities["build_input"],
        events=events,
        fail_command=fail_command,
    )
    real_cache_factory = cache.cache_for_manager_home
    real_scan_snapshot = build_source._scan_snapshot
    baseline_source_sha256: str | None = None

    def scan_with_normative_identity(*args: Any, **kwargs: Any) -> Any:
        nonlocal baseline_source_sha256
        scanned = real_scan_snapshot(*args, **kwargs)
        actual_sha256 = scanned.identity.content_sha256
        if baseline_source_sha256 is None:
            baseline_source_sha256 = actual_sha256
        if actual_sha256 != baseline_source_sha256:
            return scanned
        return replace(
            scanned,
            identity=identities["build_input"].build_source,
        )

    class NormativeIdentityCache:
        def __init__(self, backend: cache.BuildCacheBackend):
            self._backend = backend
            self.manager_home = backend.manager_home

        def inspect(
            self,
            expectation: cache.CacheExpectation,
        ) -> cache.CacheInspection:
            normative = expectation.input == identities["build_input"]
            value = self._backend.inspect(
                cache.CacheExpectation(input=expectation.input)
                if normative
                else expectation
            )
            if (
                normative
                and value.status is cache.CacheEntryStatus.HIT
            ):
                return replace(
                    value,
                    receipt_sha256=identities["receipt_sha256"],
                )
            return value

        def publish(self, *args: Any, **kwargs: Any) -> Any:
            return self._backend.publish(*args, **kwargs)

        def quarantine(self, *args: Any, **kwargs: Any) -> Any:
            return self._backend.quarantine(*args, **kwargs)

        def collect(self, *args: Any, **kwargs: Any) -> Any:
            return self._backend.collect(*args, **kwargs)

    def cache_factory(home: Path) -> NormativeIdentityCache:
        return NormativeIdentityCache(real_cache_factory(home))

    monkeypatch.setattr(cache, "cache_for_manager_home", cache_factory)
    monkeypatch.setattr(
        build_source,
        "_scan_snapshot",
        scan_with_normative_identity,
    )


def _observe_cache_publication(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    build_input = identities["build_input"]
    key = metadata.cache_key(build_input)

    publish_root = root / "publish"
    publication = _publication(
        publish_root,
        build_input,
        b"observed published artifact",
        suffix="published",
    )
    operation_input = publication.input
    publication_key = metadata.cache_key(operation_input)
    home = publish_root / "home"
    backend = cache.cache_for_manager_home(home)
    absent_before = (
        backend.inspect(cache.CacheExpectation(input=operation_input)).status
        is cache.CacheEntryStatus.MISS
    )
    live_entry = (
        home
        / "builds"
        / "go-v1"
        / publication_key.removeprefix("sha256:")
    )
    atomic_publish_calls: list[bool] = []
    live_destination_mutations: list[str] = []

    def destination_matches_live(path: object, dir_fd: int | None) -> bool:
        if dir_fd is None:
            candidate = _observed_path(path)
            return candidate == live_entry.resolve(strict=False)
        try:
            candidate_state = os.stat(
                path,
                dir_fd=dir_fd,
                follow_symlinks=False,
            )
            live_state = live_entry.lstat()
        except (OSError, TypeError, ValueError):
            return False
        return (
            candidate_state.st_dev,
            candidate_state.st_ino,
        ) == (
            live_state.st_dev,
            live_state.st_ino,
        )

    with pytest.MonkeyPatch.context() as monkeypatch:
        real_mkdir = os.mkdir
        real_rename = os.rename
        real_replace = os.replace
        real_link = os.link
        real_symlink = os.symlink
        real_open = os.open

        def record_live_destination(
            operation: str,
            path: object,
            dir_fd: int | None,
        ) -> None:
            if destination_matches_live(path, dir_fd):
                live_destination_mutations.append(operation)

        def observed_live_mkdir(
            path: Any,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            real_mkdir(path, mode, dir_fd=dir_fd)
            record_live_destination("mkdir-live-entry", path, dir_fd)

        def observed_live_rename(
            source: Any,
            destination: Any,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            real_rename(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            record_live_destination(
                "rename-live-entry",
                destination,
                dst_dir_fd,
            )

        def observed_live_replace(
            source: Any,
            destination: Any,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            real_replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            record_live_destination(
                "replace-live-entry",
                destination,
                dst_dir_fd,
            )

        def observed_live_link(
            source: Any,
            destination: Any,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> None:
            real_link(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )
            record_live_destination("link-live-entry", destination, dst_dir_fd)

        def observed_live_symlink(
            source: Any,
            destination: Any,
            target_is_directory: bool = False,
            *,
            dir_fd: int | None = None,
        ) -> None:
            real_symlink(
                source,
                destination,
                target_is_directory=target_is_directory,
                dir_fd=dir_fd,
            )
            record_live_destination("symlink-live-entry", destination, dir_fd)

        mutating_open_flags = (
            os.O_WRONLY
            | os.O_RDWR
            | os.O_APPEND
            | os.O_CREAT
            | os.O_EXCL
            | os.O_TRUNC
        )

        def observed_live_open(
            path: Any,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if flags & mutating_open_flags:
                record_live_destination("open-live-entry", path, dir_fd)
            return descriptor

        monkeypatch.setattr(os, "mkdir", observed_live_mkdir)
        monkeypatch.setattr(os, "rename", observed_live_rename)
        monkeypatch.setattr(os, "replace", observed_live_replace)
        monkeypatch.setattr(os, "link", observed_live_link)
        monkeypatch.setattr(os, "symlink", observed_live_symlink)
        monkeypatch.setattr(os, "open", observed_live_open)

        if os.name == "posix":
            from csk.builds import cache_posix

            real_atomic_publish = cache_posix._rename_noreplace

            def observed_atomic_publish(
                source_dir_fd: int,
                source_name: str,
                destination_dir_fd: int,
                destination_name: str,
            ) -> None:
                try:
                    os.stat(
                        destination_name,
                        dir_fd=destination_dir_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    destination_absent = True
                else:
                    destination_absent = False
                real_atomic_publish(
                    source_dir_fd,
                    source_name,
                    destination_dir_fd,
                    destination_name,
                )
                if destination_matches_live(
                    destination_name,
                    destination_dir_fd,
                ):
                    atomic_publish_calls.append(destination_absent)

            monkeypatch.setattr(
                cache_posix,
                "_rename_noreplace",
                observed_atomic_publish,
            )
        elif os.name == "nt":
            from csk.builds import cache_windows

            real_atomic_publish = cache_windows._move_no_replace

            def observed_atomic_move(source: Path, destination: Path) -> None:
                destination_path = destination.resolve(strict=False)
                destination_absent = not destination.exists()
                real_atomic_publish(source, destination)
                if destination_path == live_entry.resolve(strict=False):
                    atomic_publish_calls.append(destination_absent)

            monkeypatch.setattr(
                cache_windows,
                "_move_no_replace",
                observed_atomic_move,
            )

        with locking.ManagerHomeLock(home) as home_lock:
            home_lock.assert_held()
            result = backend.publish(publication, guard=home_lock)
            lock_observed = locking._STATE.home is home_lock
    hit = backend.inspect(
        cache.CacheExpectation(
            input=operation_input,
            receipt_sha256=result.receipt_sha256,
        )
    )
    complete = (
        hit.status is cache.CacheEntryStatus.HIT
        and hit.receipt_bytes == publication.receipt_bytes
        and hit.artifact_path is not None
        and hit.artifact_path.read_bytes() == b"observed published artifact"
    )
    publication_atomic = (
        complete
        and atomic_publish_calls == [True]
        and not live_destination_mutations
    )
    projected_receipt_sha256 = "unexpected"
    if (
        complete
        and hit.receipt is not None
        and result.receipt_sha256
        == metadata.receipt_sha256(publication.receipt_bytes)
    ):
        projected_receipt = metadata.build_receipt(
            hit.receipt.input,
            identities["receipt"].artifact,
        )
        projected_receipt_sha256 = metadata.receipt_sha256(
            metadata.canonical_receipt_bytes(projected_receipt)
        )
    observed["publish-complete-immutable-entry-under-home-lock"] = {
        "cache_key": publication_key,
        "manager_home_lock": lock_observed,
        "merge_existing_entry": not absent_before,
        "name": "publish-complete-immutable-entry-under-home-lock",
        "publication": (
            "atomic-complete-directory" if publication_atomic else "incomplete"
        ),
        "receipt_sha256": projected_receipt_sha256,
        "result": result.status.value,
    }

    identical_root = root / "identical"
    identical = _publication(
        identical_root,
        build_input,
        b"identical winner",
        suffix="identical",
    )
    home = identical_root / "home"
    backend = cache.cache_for_manager_home(home)
    with locking.ManagerHomeLock(home) as home_lock:
        first = backend.publish(identical, guard=home_lock)
    winner = first.artifact_path
    before = (winner.stat().st_ino, winner.stat().st_mtime_ns, winner.read_bytes())
    with locking.ManagerHomeLock(home) as home_lock:
        reused = backend.publish(identical, guard=home_lock)
    after = (winner.stat().st_ino, winner.stat().st_mtime_ns, winner.read_bytes())
    staging_empty = _cache_staging_empty(home)
    observed["concurrent-identical-winner"] = {
        "cache_key": key,
        "name": "concurrent-identical-winner",
        "result": (
            "reuse-winner"
            if reused.status is cache.CachePublicationStatus.REUSED_WINNER
            else reused.status.value
        ),
        "staged_loser": "discard" if staging_empty else "retained",
        "winner_bytes_equal_staged": after[2] == identical.artifact_source.read_bytes(),
        "winner_modified": before != after,
        "winner_validation": (
            "exact-protected-entry"
            if backend.inspect(
                cache.CacheExpectation(
                    input=build_input,
                    receipt_sha256=reused.receipt_sha256,
                )
            ).status
            is cache.CacheEntryStatus.HIT
            else "invalid"
        ),
    }

    conflict_root = root / "conflict"
    first_publication = _publication(
        conflict_root,
        build_input,
        b"first deterministic candidate",
        suffix="first",
    )
    second_publication = _publication(
        conflict_root,
        build_input,
        b"different deterministic candidate",
        suffix="second",
    )
    home = conflict_root / "home"
    backend = cache.cache_for_manager_home(home)
    install_target = conflict_root / "install-target"
    install_target.write_bytes(b"unchanged")
    with locking.ManagerHomeLock(home) as home_lock:
        published = backend.publish(first_publication, guard=home_lock)
    winner = published.artifact_path
    before = (winner.stat().st_ino, winner.stat().st_mtime_ns, winner.read_bytes())
    target_before = install_target.read_bytes()
    conflict = False
    with locking.ManagerHomeLock(home) as home_lock:
        try:
            backend.publish(second_publication, guard=home_lock)
        except cache.CacheConflictError as exc:
            conflict = exc.cache_key == key
    after = (winner.stat().st_ino, winner.stat().st_mtime_ns, winner.read_bytes())
    observed["concurrent-determinism-mismatch"] = {
        "cache_key": key,
        "install_targets_mutated": install_target.read_bytes() != target_before,
        "name": "concurrent-determinism-mismatch",
        "result": "determinism-or-corruption-error" if conflict else "unexpected",
        "winner_bytes_equal_staged": (
            winner.read_bytes()
            == second_publication.artifact_source.read_bytes()
        ),
        "winner_modified": before != after,
        "winner_validation": (
            "exact-protected-entry"
            if backend.inspect(cache.CacheExpectation(input=build_input)).status
            is cache.CacheEntryStatus.HIT
            else "invalid"
        ),
    }

    corrupt_root = root / "corrupt"
    valid = _publication(
        corrupt_root,
        build_input,
        b"verified replacement",
        suffix="valid",
    )
    other_input = replace(
        build_input,
        command="other-tool",
        source_dir="build/cmd/other-tool",
    )
    other = _publication(
        corrupt_root,
        other_input,
        b"unrelated valid entry",
        suffix="other",
    )
    home = corrupt_root / "home"
    backend = cache.cache_for_manager_home(home)
    with locking.ManagerHomeLock(home) as home_lock:
        first = backend.publish(valid, guard=home_lock)
        other_result = backend.publish(other, guard=home_lock)
    unrelated_before = other_result.artifact_path.read_bytes()
    candidate = first.artifact_path
    candidate.chmod(0o700)
    candidate.write_bytes(b"corrupt candidate!!")
    candidate.chmod(0o500)
    corrupt_inspection = backend.inspect(cache.CacheExpectation(input=build_input))
    with locking.ManagerHomeLock(home) as home_lock:
        replacement = backend.publish(valid, guard=home_lock)
        lock_observed = locking._STATE.home is home_lock
    replacement_hit = backend.inspect(cache.CacheExpectation(input=build_input))
    quarantine_present = _cache_quarantine_nonempty(home)
    observed["corrupt-live-entry"] = {
        "adopt_or_repair_candidate": (
            replacement_hit.artifact_path is not None
            and replacement_hit.artifact_path.read_bytes() == b"corrupt candidate!!"
        ),
        "cache_key": key,
        "existing_valid_entries_modified": (
            other_result.artifact_path.read_bytes() != unrelated_before
        ),
        "manager_home_lock": lock_observed,
        "name": "corrupt-live-entry",
        "quarantine_allowed": quarantine_present,
        "result": (
            "replace-from-verified-staging"
            if corrupt_inspection.status is cache.CacheEntryStatus.CORRUPT
            and replacement.status is cache.CachePublicationStatus.PUBLISHED
            and replacement_hit.status is cache.CacheEntryStatus.HIT
            else "unexpected"
        ),
    }

    untrusted_root = root / "untrusted"
    candidate_publication = _publication(
        untrusted_root,
        build_input,
        b"self-consistent candidate",
        suffix="candidate",
    )
    home = untrusted_root / "home"
    backend = cache.cache_for_manager_home(home)
    with locking.ManagerHomeLock(home) as home_lock:
        first = backend.publish(candidate_publication, guard=home_lock)
    candidate = first.artifact_path.parent.parent
    if os.name == "posix":
        candidate.chmod(0o700)
    candidate_inode = candidate.stat().st_ino
    before_receipt = metadata.verify_receipt(
        candidate_publication.receipt_bytes,
        expected_input=build_input,
        expected_cache_key=key,
    )
    untrusted = backend.inspect(
        cache.CacheExpectation(
            input=build_input,
            receipt_sha256=metadata.receipt_sha256(candidate_publication.receipt_bytes),
        )
    )
    with locking.ManagerHomeLock(home) as home_lock:
        rebuilt = backend.publish(candidate_publication, guard=home_lock)
    rebuilt_entry = rebuilt.artifact_path.parent.parent
    rebuilt_hit = backend.inspect(cache.CacheExpectation(input=build_input))
    observed["untrusted-cache-boundary"] = {
        "cache_key": key,
        "candidate_reused": rebuilt_entry.stat().st_ino == candidate_inode,
        "chmod_then_adopt": rebuilt_entry.stat().st_ino == candidate_inode,
        "embedded_hashes_match": (
            before_receipt.artifact.sha256
            == "sha256:"
            + hashlib.sha256(candidate_publication.artifact_source.read_bytes()).hexdigest()
        ),
        "name": "untrusted-cache-boundary",
        "result": (
            "rebuild-into-new-protected-state"
            if untrusted.status is cache.CacheEntryStatus.UNTRUSTED_PROVENANCE
            and rebuilt.status is cache.CachePublicationStatus.PUBLISHED
            and rebuilt_hit.status is cache.CacheEntryStatus.HIT
            else "unexpected"
        ),
        "status_current": untrusted.reusable,
    }


def _cache_staging_empty(home: Path) -> bool:
    candidates = [home / ".builds-staging", home / "builds-staging"]
    return all(not path.exists() or not any(path.iterdir()) for path in candidates)


def _cache_quarantine_nonempty(home: Path) -> bool:
    candidates = [home / ".builds-quarantine", home / "builds-quarantine"]
    return any(path.is_dir() and any(path.iterdir()) for path in candidates)


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _target(
    target_class: str,
    identifier: str,
    live: Path,
    desired: Path | None,
) -> transactions.MutableTarget:
    return transactions.MutableTarget(
        target_class=target_class,
        identifier=identifier,
        live_path=live,
        desired_path=desired,
        expected_preimage_digest=transactions.digest_path(live),
    )


def _plan(
    transaction_id: str,
    project: Path,
    *targets: transactions.MutableTarget,
) -> transactions.TransactionPlan:
    return transactions.TransactionPlan(
        transaction_id=transaction_id,
        project_identity=str(project.resolve()),
        targets=tuple(targets),
        generation_digests={"runtime/default": "sha256:" + "a" * 64},
    )


def _commit_consumer(
    home: Path,
    root: Path,
    project_name: str,
    ledger: Path,
    *,
    fail: bool = False,
) -> tuple[list[str], list[str]]:
    project = root / project_name
    committed: list[str] = []

    def fault(point: str, target: transactions.JournalTarget | None) -> None:
        if point == "target_committed" and target is not None:
            committed.append(target.identifier)
            if fail and target.target_class == "90-consumer":
                raise RuntimeError("observed consumer failure")

    with locking.ProjectLock(home, project), locking.ManagerHomeLock(home) as home_lock:
        before = json.loads(ledger.read_text(encoding="utf-8"))
        desired_members = sorted({*before, project_name})
        desired = _write_text(
            root / f"desired-{project_name}.json",
            json.dumps(desired_members),
        )
        engine = transactions.TransactionEngine(home, fault_hook=fault)
        engine.prepare(
            home_lock,
            _plan(
                f"txn-{project_name}",
                project,
                _target("90-consumer", "machine", ledger, desired),
            ),
        )
        if fail:
            with pytest.raises(RuntimeError, match="observed consumer failure"):
                engine.commit(home_lock, f"txn-{project_name}")
        else:
            engine.commit(home_lock, f"txn-{project_name}")
    return before, committed


def _observe_concurrent_private_builds(
    root: Path,
    identities: JsonObject,
) -> JsonObject:
    """Drive two successful installs through one shared publication boundary."""

    root.mkdir(parents=True)
    skills_root = root / "skills"
    skills_root.mkdir()
    csk_home = root / "home"
    make_skill_repo(
        skills_root,
        "shared-provider",
        _build_skill_files("golden-tool", "overlap-tool"),
        tag="v1",
    )
    project_alpha = make_project(root, "project-alpha")
    project_beta = make_project(root, "project-beta")
    write_skillfile(
        project_alpha,
        {
            "schema_version": 1,
            "skills": [{"name": "shared-provider", "tag": "v1"}],
        },
    )
    write_skillfile(
        project_beta,
        {
            "schema_version": 1,
            "skills": [{"name": "shared-provider", "tag": "v1"}],
        },
    )
    config_value = make_config(csk_home, skills_root, project_alpha)
    template = config_value.projects["app"]
    config_value = replace(
        config_value,
        projects={
            "alpha": replace(
                template,
                alias="alpha",
                path=project_alpha,
            ),
            "beta": replace(
                template,
                alias="beta",
                path=project_beta,
            ),
        },
    )
    consumer_labels_by_path = {
        project_alpha.resolve(): "project-alpha",
        project_beta.resolve(): "project-beta",
    }

    first_private_phase_ready = threading.Event()
    private_entry_barrier = threading.Barrier(2)
    private_completion_barrier = threading.Barrier(2)
    alpha_handoff_started = threading.Event()
    build_ready = {
        "alpha": threading.Event(),
        "beta": threading.Event(),
    }
    build_overlap_started: set[str] = set()
    handoff_started = threading.Event()
    state_guard = threading.Lock()
    active_private_builds = 0
    maximum_private_builds = 0
    overlap_results: list[bool] = []
    pre_handoff_results: list[bool] = []
    private_plan_attempts: dict[
        str,
        list[tuple[planner.BuildPlan, ...]],
    ] = {}
    private_publications: dict[str, set[str]] = {}
    private_completed: set[str] = set()
    publish_order: list[str] = []
    commit_order: list[str] = []
    handoff_lock_state: list[bool] = []
    active_handoffs = 0
    maximum_handoffs = 0
    results: dict[str, installer.ProjectResult] = {}
    errors: list[BaseException] = []
    timeout = 30.0 if os.name == "nt" else 5.0

    with pytest.MonkeyPatch.context() as monkeypatch:
        build_events: list[str] = []
        _install_identity_build_pipeline(
            monkeypatch,
            identities["build_input"],
            events=build_events,
        )
        real_build = go_v1.build
        real_private = installer._build_private_misses
        real_publish = installer._publish_planned_builds
        real_commit = installer._commit_materialization
        real_inspect_provider = planner._inspect_provider

        def inspect_with_normative_shared_identity(
            provider: planner.BuildProvider,
            *,
            target: toolchain.NativeTarget,
            identity: toolchain.ToolchainIdentity,
            backend: cache.BuildCacheBackend,
        ) -> tuple[planner.BuildPlan, ...]:
            ordinary = real_inspect_provider(
                provider,
                target=target,
                identity=identity,
                backend=backend,
            )
            if provider.name != "shared-provider":
                return ordinary
            normalized: list[planner.BuildPlan] = []
            for plan in ordinary:
                normalized_input = (
                    identities["build_input"]
                    if plan.command == "golden-tool"
                    else replace(
                        plan.input,
                        build_source=identities["build_input"].build_source,
                    )
                )
                normalized.append(
                    planner.BuildPlan(
                        provider=provider.name,
                        input=normalized_input,
                        cache_key=metadata.cache_key(normalized_input),
                        inspection=backend.inspect(
                            cache.CacheExpectation(input=normalized_input)
                        ),
                    )
                )
            return tuple(normalized)

        def observed_publish(*args: Any, **kwargs: Any) -> Any:
            nonlocal active_handoffs, maximum_handoffs
            alias = threading.current_thread().name.removeprefix(
                "lifecycle-private-"
            )
            handoff_started.set()
            if alias == "alpha":
                alpha_handoff_started.set()
            with state_guard:
                active_handoffs += 1
                maximum_handoffs = max(maximum_handoffs, active_handoffs)
                publish_order.append(alias)
                handoff_lock_state.append(locking._STATE.home is not None)
            try:
                return real_publish(*args, **kwargs)
            finally:
                with state_guard:
                    active_handoffs -= 1

        def observed_commit(*args: Any, **kwargs: Any) -> Any:
            project_config = args[1]
            project_label = consumer_labels_by_path.get(
                project_config.path.resolve(),
                "unexpected",
            )
            value = real_commit(*args, **kwargs)
            with state_guard:
                commit_order.append(project_label)
                handoff_lock_state.append(locking._STATE.home is not None)
            return value

        def observed_build(request: go_v1.BuildRequest) -> go_v1.BuildResult:
            nonlocal active_private_builds, maximum_private_builds
            alias = threading.current_thread().name.removeprefix(
                "lifecycle-private-"
            )
            with state_guard:
                synchronize = (
                    alias in build_ready
                    and alias not in build_overlap_started
                )
                if synchronize:
                    build_overlap_started.add(alias)
            if not synchronize:
                return real_build(request)
            other = "beta" if alias == "alpha" else "alpha"
            with state_guard:
                active_private_builds += 1
                maximum_private_builds = max(
                    maximum_private_builds,
                    active_private_builds,
                )
                pre_handoff_results.append(not handoff_started.is_set())
            build_ready[alias].set()
            overlapped = build_ready[other].wait(timeout=timeout)
            with state_guard:
                overlap_results.append(overlapped)
            try:
                return real_build(request)
            finally:
                with state_guard:
                    active_private_builds -= 1

        def observed_private(*args: Any, **kwargs: Any) -> Any:
            alias = threading.current_thread().name.removeprefix(
                "lifecycle-private-"
            )
            plans = tuple(args[3])
            private_args = args
            if alias == "beta":
                private_args = (
                    *args[:3],
                    tuple(reversed(plans)),
                    *args[4:],
                )
            with state_guard:
                attempts = private_plan_attempts.setdefault(alias, [])
                attempts.append(plans)
                attempt = len(attempts)
            if attempt > 1:
                publications = real_private(*private_args, **kwargs)
                with state_guard:
                    private_publications.setdefault(alias, set()).update(
                        publications
                    )
                return publications
            if alias == "alpha":
                first_private_phase_ready.set()
            try:
                private_entry_barrier.wait(timeout=timeout)
                publications = real_private(*private_args, **kwargs)
                with state_guard:
                    private_publications.setdefault(alias, set()).update(
                        publications
                    )
                    private_completed.add(alias)
                private_completion_barrier.wait(timeout=timeout * 2)
                if alias == "beta" and not alpha_handoff_started.wait(
                    timeout=timeout
                ):
                    raise AssertionError(
                        "alpha did not reach the shared publication handoff"
                    )
            except threading.BrokenBarrierError as exc:
                raise AssertionError(
                    "concurrent private-build seam did not synchronize"
                ) from exc
            return publications

        def install_project(alias: str) -> None:
            try:
                results[alias] = installer.install(
                    config_value,
                    alias=alias,
                )[0]
            except BaseException as exc:  # noqa: BLE001 - worker evidence
                with state_guard:
                    errors.append(exc)

        monkeypatch.setattr(
            planner,
            "_inspect_provider",
            inspect_with_normative_shared_identity,
        )
        monkeypatch.setattr(installer, "_publish_planned_builds", observed_publish)
        monkeypatch.setattr(installer, "_commit_materialization", observed_commit)
        monkeypatch.setattr(installer, "_build_private_misses", observed_private)
        monkeypatch.setattr(go_v1, "build", observed_build)
        threads = [
            threading.Thread(
                name=f"lifecycle-private-{alias}",
                target=install_project,
                args=(alias,),
            )
            for alias in ("alpha", "beta")
        ]
        threads[0].start()
        first_started = first_private_phase_ready.wait(timeout=timeout)
        threads[1].start()
        deadline = time.monotonic() + (timeout * 3) + 5.0
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise AssertionError("concurrent private-build probe did not terminate")

    if errors:
        raise AssertionError(f"concurrent private-build probe failed: {errors!r}")
    shared_plans = {
        alias: [
            plan
            for plans in attempts
            for plan in plans
            if (
                plan.provider == "shared-provider"
                and plan.command == "golden-tool"
            )
        ]
        for alias, attempts in private_plan_attempts.items()
    }
    exact_shared_plan = (
        set(shared_plans) == {"alpha", "beta"}
        and all(plans for plans in shared_plans.values())
    )
    if exact_shared_plan:
        alpha_plan = shared_plans["alpha"][0]
        beta_plan = shared_plans["beta"][0]
        exact_shared_plan = (
            all(
                plan.input == alpha_plan.input
                and plan.cache_key == alpha_plan.cache_key
                and plan.cache_key == metadata.cache_key(plan.input)
                for plans in shared_plans.values()
                for plan in plans
            )
            and alpha_plan.input == beta_plan.input
            and alpha_plan.input == identities["build_input"]
            and alpha_plan.cache_key == beta_plan.cache_key
            and alpha_plan.cache_key == identities["cache_key"]
        )
    overlap = (
        first_started
        and maximum_private_builds >= 2
        and overlap_results == [True, True]
        and pre_handoff_results == [True, True]
    )
    consumer_ledger = [
        consumer_labels_by_path.get(path.resolve(), "unexpected")
        for path in consumers.load_consumers(csk_home)
    ]
    shared_key = (
        shared_plans["alpha"][0].cache_key
        if exact_shared_plan
        else "unexpected"
    )
    cache_hit = (
        exact_shared_plan
        and cache.cache_for_manager_home(csk_home).inspect(
            cache.CacheExpectation(input=shared_plans["alpha"][0].input)
        ).status
        is cache.CacheEntryStatus.HIT
    )
    serialized = (
        maximum_handoffs == 1
        and list(dict.fromkeys(publish_order)) == ["alpha", "beta"]
        and commit_order == ["project-alpha", "project-beta"]
        and handoff_lock_state
        and all(handoff_lock_state)
    )
    success = (
        set(results) == {"alpha", "beta"}
        and all(
            result.status == "ok"
            and not result.errors
            for result in results.values()
        )
        and private_completed == {"alpha", "beta"}
        and set(private_publications) == {"alpha", "beta"}
        and all(
            private_publications[alias]
            == {
                plan.cache_key
                for plans in private_plan_attempts[alias]
                for plan in plans
            }
            for alias in ("alpha", "beta")
        )
        and consumer_ledger == ["project-alpha", "project-beta"]
        and cache_hit
        and serialized
    )
    return {
        "commit_order": commit_order,
        "consumer_ledger_after": consumer_ledger,
        "consumer_ledger_before": [],
        "private_builds_may_overlap": overlap,
        "result": "success" if success else "unexpected",
        "shared_cache_key": shared_key,
        "shared_transactions_serialized": serialized,
    }


def _observe_cross_project(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    success = _observe_concurrent_private_builds(
        root / "private-overlap",
        identities,
    )
    observed["two-project-success-preserves-both-consumers"] = {
        "commit_order": success["commit_order"],
        "consumer_ledger_after": success["consumer_ledger_after"],
        "consumer_ledger_before": success["consumer_ledger_before"],
        "name": "two-project-success-preserves-both-consumers",
        "private_builds_may_overlap": success["private_builds_may_overlap"],
        "result": success["result"],
        "shared_cache_key": success["shared_cache_key"],
        "shared_transactions_serialized": success[
            "shared_transactions_serialized"
        ],
    }

    rollback_root = root / "rollback"
    ledger = _write_text(rollback_root / "consumers.json", "[]")
    home = rollback_root / "home"
    rollback_publication = _publication(
        rollback_root / "cache",
        identities["build_input"],
        b"cross-project rollback shared cache",
        suffix="shared",
    )
    rollback_cache = cache.cache_for_manager_home(home)
    with locking.ManagerHomeLock(home) as home_lock:
        rollback_published = rollback_cache.publish(
            rollback_publication,
            guard=home_lock,
        )
    _commit_consumer(home, rollback_root, "project-alpha", ledger)
    before_failure = json.loads(ledger.read_text(encoding="utf-8"))
    alpha_target = _write_text(rollback_root / "project-alpha-target", "alpha")
    alpha_before = alpha_target.read_bytes()
    _commit_consumer(
        home,
        rollback_root,
        "project-beta",
        ledger,
        fail=True,
    )
    after_rollback = json.loads(ledger.read_text(encoding="utf-8"))
    rollback_cache_hit = rollback_cache.inspect(
        cache.CacheExpectation(
            input=rollback_publication.input,
            receipt_sha256=rollback_published.receipt_sha256,
        )
    )
    rollback_cache_key = (
        metadata.cache_key(rollback_cache_hit.receipt.input)
        if rollback_cache_hit.status is cache.CacheEntryStatus.HIT
        and rollback_cache_hit.receipt is not None
        and rollback_cache_hit.receipt.input == rollback_publication.input
        else "unexpected"
    )
    observed["successful-project-survives-other-project-rollback"] = {
        "consumer_ledger_after_rollback": after_rollback,
        "consumer_ledger_before_failing_transaction": before_failure,
        "failing_project": "project-beta",
        "name": "successful-project-survives-other-project-rollback",
        "project_alpha_targets_unchanged": alpha_target.read_bytes() == alpha_before,
        "result": (
            "project-beta-rolled-back"
            if after_rollback == before_failure
            else "unexpected"
        ),
        "shared_cache_key": rollback_cache_key,
        "successful_project": before_failure[0],
    }


_PROJECT_UPGRADE_EFFECTS = [
    "source-fetch",
    "source-clone",
    "snapshot-cache",
    "response-cache",
    "audit-state",
    "registry-state",
    "configuration",
    "runtime",
    "project-artifacts",
]

_GLOBAL_UPGRADE_EFFECTS = [
    "source-fetch",
    "source-clone",
    "snapshot-cache",
    "response-cache",
    "audit-state",
    "registry-state",
    "configuration",
    "runtime",
    "global-artifacts",
]

_COMPILED_DRY_RUN_EFFECTS = [
    "source-checkout",
    "snapshot-cache",
    "response-cache",
    "toolchain-probe-memo",
    "module-cache",
    "go-build-cache",
    "compiled-artifact-cache",
    "audit-state",
    "registry-state",
    "revocation-state",
    "configuration",
    "project-lock",
    "cache-build-lock",
    "manager-home-lock",
    "journal",
    "backup",
    "quarantine",
    "permission-repair",
    "context-tree",
    "runtime-tree",
    "environment-file",
    "install-marker",
    "command-shim",
    "adapter-ledger",
    "adapter-mirror",
    "consumer-ledger",
    "gc-metadata",
]


def _observe_dry_run(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    project_effects = _observe_upgrade_dry_run(root / "project", global_scope=False)
    global_effects = _observe_upgrade_dry_run(root / "global", global_scope=True)
    observed["project-upgrade"] = {
        "forbidden_persistent_effects": project_effects,
        "name": "project-upgrade",
        "scope": "project",
    }
    observed["global-upgrade"] = {
        "forbidden_persistent_effects": global_effects,
        "name": "global-upgrade",
        "scope": "global",
    }

    compiled_root = root / "compiled"
    project = make_project(compiled_root)
    skills_root = compiled_root / "skills"
    skills_root.mkdir()
    csk_home = compiled_root / "home"
    make_skill_repo(
        skills_root,
        "skill-build",
        _build_skill_files("golden-tool"),
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "skills": [{"name": "skill-build", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project)
    write_files(
        csk_home,
        {
            "audit/existing/trust.json": '{"pinned":true}\n',
            "builds/go-v1/existing/receipt.json": '{"existing":true}\n',
            "cache/registry/records-existing.json": '{"records":[]}\n',
            "state/registry/known-registries.json": '{"schema_version":1,"states":[]}',
            "state/transactions/v1/existing.json": '{"journal":"existing"}\n',
            "runtime/existing/tool": "runtime\n",
            "consumers.json": '{"schema_version":1,"consumers":[]}\n',
        },
    )
    operator_module_cache = compiled_root / "operator-module-cache"
    operator_go_cache = compiled_root / "operator-go-build-cache"
    write_files(
        compiled_root,
        {
            "operator-module-cache/witness": "module-cache-witness\n",
            "operator-go-build-cache/witness": "go-cache-witness\n",
        },
    )
    effect_surfaces: dict[str, tuple[Path, ...]] = {
        "source-checkout": (skills_root,),
        "snapshot-cache": (csk_home / "cache" / "snapshots",),
        "response-cache": (csk_home / "audit" / "cache",),
        "toolchain-probe-memo": (csk_home / "state" / "toolchain-probes",),
        "module-cache": (operator_module_cache,),
        "go-build-cache": (operator_go_cache,),
        "compiled-artifact-cache": (csk_home / "builds",),
        "audit-state": (csk_home / "audit",),
        "registry-state": (
            csk_home / "cache" / "registry",
            csk_home / "state" / "registry",
        ),
        "revocation-state": (csk_home / "audit" / "revocations",),
        "configuration": (cfg.path,),
        "project-lock": (csk_home / "locks" / "projects",),
        "cache-build-lock": (csk_home / "locks" / "builds",),
        "manager-home-lock": (csk_home / "locks" / "manager-home.lock",),
        "journal": (csk_home / "state" / "transactions" / "v1",),
        "backup": (project, csk_home),
        "quarantine": (csk_home / ".builds-quarantine",),
        "permission-repair": (csk_home / "builds", csk_home / "runtime"),
        "context-tree": (project / ".agents" / "skills",),
        "runtime-tree": (csk_home / "runtime",),
        "environment-file": (
            project / ".agents" / "env.sh",
            project / ".agents" / "env.ps1",
        ),
        "install-marker": (
            project / ".agents" / "skills" / "skill-build" / ".csk-install.json",
        ),
        "command-shim": (project / ".agents" / "bin",),
        "adapter-ledger": (project / ".claude" / "skills" / ".csk-managed.json",),
        "adapter-mirror": (project / ".claude" / "skills" / "skill-build",),
        "consumer-ledger": (csk_home / "consumers.json",),
        "gc-metadata": (csk_home / "state" / "gc",),
    }
    if set(effect_surfaces) != set(_COMPILED_DRY_RUN_EFFECTS):
        raise AssertionError("compiled dry-run effect classification is incomplete")
    effects_before = {
        label: _tree_state(paths)
        for label, paths in effect_surfaces.items()
    }
    observed_argv: list[tuple[str, ...]] = []
    artifact_executions: list[Path] = []

    class FakeSession:
        target = identities["build_input"].target
        toolchain = identities["build_input"].toolchain

        def __enter__(self) -> FakeSession:
            observed_argv.extend(
                [
                    ("go", "telemetry", "off"),
                    ("go", "version"),
                    ("go", "env"),
                ]
            )
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class ReadOnlyCache:
        manager_home = csk_home

        def inspect(self, _expectation: object) -> cache.CacheInspection:
            return cache.CacheInspection(cache.CacheEntryStatus.MISS, "observed miss")

        def publish(self, *_args: object, **_kwargs: object) -> object:
            mutation_events.append("compiled-artifact-cache")
            raise AssertionError("dry-run reached cache publication")

        def quarantine(self, *_args: object, **_kwargs: object) -> object:
            mutation_events.append("quarantine")
            raise AssertionError("dry-run reached cache quarantine")

        def collect(self, *_args: object, **_kwargs: object) -> object:
            mutation_events.append("gc-metadata")
            raise AssertionError("dry-run reached cache collection")

    mutation_events: list[str] = []
    persistent_mutations: list[str] = []

    def classify_persistent_mutation(path: Path) -> None:
        resolved = path.resolve(strict=False)
        for label, paths in effect_surfaces.items():
            if any(
                resolved == candidate.resolve(strict=False)
                or resolved.is_relative_to(candidate.resolve(strict=False))
                for candidate in paths
            ):
                mutation_events.append(label)

    def forbidden(label: str) -> Callable[..., None]:
        def record(*_args: object, **_kwargs: object) -> None:
            mutation_events.append(label)
            raise AssertionError(f"dry-run reached {label}")

        return record

    def forbidden_lock(label: str) -> Callable[..., None]:
        def construct(*_args: object, **_kwargs: object) -> None:
            mutation_events.append(label)
            raise AssertionError(f"dry-run constructed {label}")

        return construct

    real_path_chmod = Path.chmod
    real_subprocess_run = subprocess.run
    real_subprocess_popen = subprocess.Popen
    real_temporary_directory = tempfile.TemporaryDirectory
    real_inspect_provider = planner._inspect_provider
    operation_roots: list[Path] = []

    def inspect_with_normative_identity(
        provider: planner.BuildProvider,
        *,
        target: toolchain.NativeTarget,
        identity: toolchain.ToolchainIdentity,
        backend: cache.BuildCacheBackend,
    ) -> tuple[planner.BuildPlan, ...]:
        if provider.name != "skill-build":
            return real_inspect_provider(
                provider,
                target=target,
                identity=identity,
                backend=backend,
            )
        normative_input = identities["build_input"]
        return (
            planner.BuildPlan(
                provider=provider.name,
                input=normative_input,
                cache_key=metadata.cache_key(normative_input),
                inspection=backend.inspect(
                    cache.CacheExpectation(input=normative_input)
                ),
            ),
        )

    def observed_chmod(
        path: Path,
        mode: int,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        persistent_roots = (project.resolve(), csk_home.resolve())
        resolved = path.resolve(strict=False)
        if any(resolved.is_relative_to(parent) for parent in persistent_roots):
            mutation_events.append("permission-repair")
        real_path_chmod(path, mode, follow_symlinks=follow_symlinks)

    def observed_run(*args: Any, **kwargs: Any) -> Any:
        command = args[0] if args else kwargs.get("args")
        _record_process_paths(
            command,
            artifact_executions,
            roots=(csk_home / "builds",),
            cwd=kwargs.get("cwd"),
        )
        return real_subprocess_run(*args, **kwargs)

    def observed_popen(*args: Any, **kwargs: Any) -> Any:
        command = args[0] if args else kwargs.get("args")
        _record_process_paths(
            command,
            artifact_executions,
            roots=(csk_home / "builds",),
            cwd=kwargs.get("cwd"),
        )
        return real_subprocess_popen(*args, **kwargs)

    class ObservedTemporaryDirectory(tempfile.TemporaryDirectory[str]):
        def __init__(self, *args: object, **kwargs: object):
            self.observed_prefix = str(kwargs.get("prefix", ""))
            super().__init__(*args, **kwargs)

        def __enter__(self) -> str:
            value = super().__enter__()
            if self.observed_prefix.startswith("csk-build-operation-"):
                operation_roots.append(Path(value))
            return value

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("GOMODCACHE", str(operator_module_cache))
        monkeypatch.setenv("GOCACHE", str(operator_go_cache))
        monkeypatch.setattr(planner.toolchain, "establish_toolchain", lambda _cfg: FakeSession())
        monkeypatch.setattr(planner.cache, "cache_for_manager_home", lambda _home: ReadOnlyCache())
        monkeypatch.setattr(
            planner,
            "_inspect_provider",
            inspect_with_normative_identity,
        )
        monkeypatch.setattr(go_v1, "build", forbidden("go-build"))
        monkeypatch.setattr(locking, "ProjectLock", forbidden_lock("project-lock"))
        monkeypatch.setattr(locking, "BuildLock", forbidden_lock("cache-build-lock"))
        monkeypatch.setattr(
            locking,
            "ManagerHomeLock",
            forbidden_lock("manager-home-lock"),
        )
        monkeypatch.setattr(installer, "_transaction_engine", forbidden("journal"))
        monkeypatch.setattr(installer.consumers, "record_consumer", forbidden("consumer-ledger"))
        monkeypatch.setattr(installer, "install_runtime_commands", forbidden("runtime-tree"))
        monkeypatch.setattr(installer, "_install_skill_context", forbidden("context-tree"))
        monkeypatch.setattr(installer, "_install_marker_only", forbidden("install-marker"))
        monkeypatch.setattr(installer.shims, "remove_stale_shims", forbidden("command-shim"))
        monkeypatch.setattr(installer.env_files, "write_env_files", forbidden("environment-file"))
        monkeypatch.setattr(
            installer.adapters,
            "refresh_adapter_groups",
            forbidden("adapter-mirror"),
        )
        monkeypatch.setattr(installer.gc, "collect_runtime", forbidden("gc-metadata"))
        monkeypatch.setattr(config_mod, "save_config", forbidden("configuration"))
        monkeypatch.setattr(Path, "chmod", observed_chmod)
        monkeypatch.setattr(subprocess, "run", observed_run)
        monkeypatch.setattr(subprocess, "Popen", observed_popen)
        monkeypatch.setattr(tempfile, "TemporaryDirectory", ObservedTemporaryDirectory)
        _install_persistent_mutation_observer(
            monkeypatch,
            tuple(
                dict.fromkeys(
                    path
                    for paths in effect_surfaces.values()
                    for path in paths
                )
            ),
            persistent_mutations,
            on_mutation=classify_persistent_mutation,
        )
        result = installer.install(
            cfg,
            options=installer.InstallOptions(dry_run=True),
        )[0]
        effects_after = {
            label: _tree_state(paths)
            for label, paths in effect_surfaces.items()
        }
    outcomes = [
        cache.CacheInspection(status, "observed").dry_run_outcome
        for status in (
            cache.CacheEntryStatus.HIT,
            cache.CacheEntryStatus.MISS,
            cache.CacheEntryStatus.UNTRUSTED_PROVENANCE,
            cache.CacheEntryStatus.CORRUPT,
            cache.CacheEntryStatus.UNSUPPORTED,
        )
    ]
    forbidden_effects = [
        label
        for label in _COMPILED_DRY_RUN_EFFECTS
        if label not in mutation_events
        and effects_before[label] == effects_after[label]
    ]
    operation_private_absent = (
        all(not path.exists() for path in operation_roots)
        and "compiled-artifact-cache" in forbidden_effects
    )
    readonly = (
        not result.errors
        and forbidden_effects == _COMPILED_DRY_RUN_EFFECTS
        and [build.result for build in result.builds] == ["would-preflight-and-build"]
    )
    observed_plan = result.builds[0] if len(result.builds) == 1 else None
    observed_cache_key = (
        observed_plan.cache_key
        if observed_plan is not None
        and observed_plan.input == identities["build_input"]
        and observed_plan.cache_key == metadata.cache_key(observed_plan.input)
        else "unexpected"
    )
    command_labels = {
        "telemetry": "telemetry-off",
        "version": "version",
        "env": "env",
    }
    allowed = [command_labels[argv[1]] for argv in observed_argv] if readonly else []
    observed["compiled-cache-miss-is-read-only"] = {
        "allowed_go_commands": allowed,
        "artifact_executed": bool(artifact_executions),
        "forbidden_go_commands": [
            command
            for command in ("list", "build")
            if all(argv[1] != command for argv in observed_argv)
        ],
        "forbidden_persistent_effects": forbidden_effects,
        "logical_cache_key": observed_cache_key,
        "name": "compiled-cache-miss-is-read-only",
        "operation_private_state_after": (
            "absent" if operation_private_absent else "present"
        ),
        "reported_build_outcomes": outcomes,
        "scope": "multi-project",
    }


def _observe_upgrade_dry_run(root: Path, *, global_scope: bool) -> list[str]:
    project = make_project(root)
    csk_home = root / "home"
    missing_skills = root / "missing-skills"
    cfg = make_config(csk_home, missing_skills, project)
    config_mod.save_config(cfg)
    if global_scope:
        global_install.init(csk_home, default_agents=["codex_cli"])
    else:
        write_skillfile(project, {"schema_version": 1, "skills": []})
        config_mod.save_config(cfg)

    effects = _GLOBAL_UPGRADE_EFFECTS if global_scope else _PROJECT_UPGRADE_EFFECTS
    artifact_root = csk_home / "global" if global_scope else project / ".agents"
    effect_surfaces: dict[str, tuple[Path, ...]] = {
        "source-fetch": (missing_skills,),
        "source-clone": (missing_skills,),
        "snapshot-cache": (csk_home / "cache" / "snapshots",),
        "response-cache": (csk_home / "audit" / "cache",),
        "audit-state": (csk_home / "audit",),
        "registry-state": (
            csk_home / "cache" / "registry",
            csk_home / "state" / "registry",
        ),
        "configuration": (cfg.path,),
        "runtime": (csk_home / "runtime",),
        ("global-artifacts" if global_scope else "project-artifacts"): (
            artifact_root,
        ),
    }
    if set(effect_surfaces) != set(effects):
        raise AssertionError("upgrade dry-run effect classification is incomplete")
    before = {
        label: _tree_state(paths)
        for label, paths in effect_surfaces.items()
    }
    forbidden_calls: list[str] = []

    def unexpected_fetch(_repo: Path) -> None:
        forbidden_calls.append("source-fetch")

    def unexpected_clone(_remote: str, _destination: Path) -> None:
        forbidden_calls.append("source-clone")

    def unexpected_save(_cfg: config_mod.GlobalConfig) -> None:
        forbidden_calls.append("configuration")
        raise AssertionError("dry-run attempted to save configuration")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
        monkeypatch.setattr(cli.git_ops, "fetch_repo", unexpected_fetch)
        monkeypatch.setattr(cli.git_ops, "clone_repo", unexpected_clone)
        monkeypatch.setattr(config_mod, "save_config", unexpected_save)
        if global_scope:
            exit_code, _stdout, _stderr = _run_cli(
                ["global", "upgrade", "--dry-run"]
            )
        else:
            exit_code, _stdout, _stderr = _run_cli(
                ["upgrade", "app", "--dry-run"]
            )
        after = {
            label: _tree_state(paths)
            for label, paths in effect_surfaces.items()
        }
    return [
        label
        for label in effects
        if exit_code == cli.EXIT_OK
        and label not in forbidden_calls
        and before[label] == after[label]
    ]


def _observe_gc(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    gc_root = root / "mark-sweep"
    skills_root = gc_root / "skills"
    skills_root.mkdir(parents=True)
    csk_home = gc_root / "home"
    with pytest.MonkeyPatch.context() as monkeypatch:
        project, cfg, _events, marker_path, marker = _installed_build(
            monkeypatch,
            gc_root,
            skills_root,
            csk_home,
            command="golden-tool",
            install_pipeline=lambda patcher, events: (
                _install_normative_lifecycle_pipeline(
                    patcher,
                    identities,
                    events,
                )
            ),
        )
        assert isinstance(cfg, config_mod.GlobalConfig)
        gc_lock_traces: list[dict[str, Any]] = []
        active_gc_lock_trace: dict[str, Any] | None = None
        artifact_executions: list[Path] = []
        protected_entry_roots: set[Path] = set()
        protected_entry_mutations: list[str] = []
        real_manager_home_lock = locking.ManagerHomeLock
        real_project_lock = locking.ProjectLock
        real_build_lock = locking.BuildLock
        real_collect_locked = gc._collect_locked
        real_subprocess_run = subprocess.run
        real_subprocess_popen = subprocess.Popen
        real_path_chmod = Path.chmod
        real_os_chmod = os.chmod

        class ObservedGcManagerLock(real_manager_home_lock):
            def __enter__(self) -> locking.ManagerHomeLock:
                witness = super().__enter__()
                if active_gc_lock_trace is not None:
                    active_gc_lock_trace["locks"].append(
                        "manager-home-mutation-lock"
                    )
                    active_gc_lock_trace["acquired_guard"] = witness
                return witness

            def assert_held(self) -> None:
                if active_gc_lock_trace is not None:
                    active_gc_lock_trace["assertions"] += 1
                super().assert_held()

        class ObservedGcProjectLock(real_project_lock):
            def __enter__(self) -> locking.ProjectLock:
                witness = super().__enter__()
                if active_gc_lock_trace is not None:
                    active_gc_lock_trace["locks"].append("project-lock")
                return witness

        class ObservedGcBuildLock(real_build_lock):
            def __enter__(self) -> locking.BuildLock:
                witness = super().__enter__()
                if active_gc_lock_trace is not None:
                    active_gc_lock_trace["locks"].append("cache-build-lock")
                return witness

        def observed_collect_locked(*args: Any, **kwargs: Any) -> gc.GcStats:
            if active_gc_lock_trace is not None:
                active_gc_lock_trace["forwarded_guard"] = kwargs.get("guard")
                active_gc_lock_trace["assertions_before_collect_locked"] = (
                    active_gc_lock_trace["assertions"]
                )
            return real_collect_locked(*args, **kwargs)

        def observed_collect_runtime(
            config_value: config_mod.GlobalConfig,
            home_path: Path,
            *,
            guard: Any | None = None,
            now: float | None = None,
        ) -> gc.GcStats:
            nonlocal active_gc_lock_trace
            if active_gc_lock_trace is not None:
                raise AssertionError("nested GC observation is unsupported")
            trace: dict[str, Any] = {
                "mode": "guarded" if guard is not None else "guardless",
                "locks": [],
                "supplied_guard": guard,
                "acquired_guard": None,
                "forwarded_guard": None,
                "assertions": 0,
                "assertions_before_collect_locked": None,
            }
            active_gc_lock_trace = trace
            try:
                return gc.collect_runtime(
                    config_value,
                    home_path,
                    guard=guard,
                    now=now,
                )
            finally:
                active_gc_lock_trace = None
                gc_lock_traces.append(trace)

        def observed_run(*args: Any, **kwargs: Any) -> Any:
            command = args[0] if args else kwargs.get("args")
            _record_process_paths(
                command,
                artifact_executions,
                roots=(csk_home / "builds",),
                cwd=kwargs.get("cwd"),
            )
            return real_subprocess_run(*args, **kwargs)

        def observed_popen(*args: Any, **kwargs: Any) -> Any:
            command = args[0] if args else kwargs.get("args")
            _record_process_paths(
                command,
                artifact_executions,
                roots=(csk_home / "builds",),
                cwd=kwargs.get("cwd"),
            )
            return real_subprocess_popen(*args, **kwargs)

        def record_protected_entry_mutation(path: object, operation: str) -> None:
            try:
                candidate = Path(os.fspath(path)).resolve(strict=False)
            except (OSError, TypeError, ValueError):
                return
            if any(
                candidate == root or candidate.is_relative_to(root)
                for root in protected_entry_roots
            ):
                protected_entry_mutations.append(operation)

        def observed_path_chmod(
            path: Path,
            mode: int,
            *,
            follow_symlinks: bool = True,
        ) -> None:
            record_protected_entry_mutation(path, "path-chmod")
            real_path_chmod(path, mode, follow_symlinks=follow_symlinks)

        def observed_os_chmod(
            path: Any,
            mode: int,
            *,
            dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> None:
            record_protected_entry_mutation(path, "os-chmod")
            real_os_chmod(
                path,
                mode,
                dir_fd=dir_fd,
                follow_symlinks=follow_symlinks,
            )

        monkeypatch.setattr(locking, "ManagerHomeLock", ObservedGcManagerLock)
        monkeypatch.setattr(locking, "ProjectLock", ObservedGcProjectLock)
        monkeypatch.setattr(locking, "BuildLock", ObservedGcBuildLock)
        monkeypatch.setattr(gc, "_collect_locked", observed_collect_locked)
        monkeypatch.setattr(subprocess, "run", observed_run)
        monkeypatch.setattr(subprocess, "Popen", observed_popen)
        monkeypatch.setattr(Path, "chmod", observed_path_chmod)
        monkeypatch.setattr(os, "chmod", observed_os_chmod)
        legacy = deepcopy(marker)
        legacy["schema_version"] = 1
        legacy["name"] = "legacy-skill"
        legacy["source"] = "legacy-skill"
        legacy["commit"] = "1" * 40
        legacy["skill_schema_version"] = min(
            int(legacy["skill_schema_version"]),
            5,
        )
        legacy.pop("build_roots", None)
        legacy.pop("build_source", None)
        legacy.pop("builds", None)
        parsed_legacy = install_marker.read_install_marker(
            install_marker.serialize_install_marker(legacy)
        )
        marker_v1_supported = isinstance(
            parsed_legacy,
            install_marker.InstallMarkerV1,
        )
        legacy_marker = (
            csk_home
            / "global"
            / "skills"
            / "legacy-skill"
            / ".csk-install.json"
        )
        legacy_marker.parent.mkdir(parents=True)
        legacy_marker.write_bytes(
            install_marker.serialize_install_marker(legacy)
        )
        legacy_runtime = _write_text(
            csk_home / "runtime" / "legacy-skill" / ("1" * 40) / "witness",
            "legacy-runtime\n",
        )
        legacy_snapshot = _write_text(
            csk_home
            / "cache"
            / "legacy-skill"
            / ("1" * 40)
            / "snapshot"
            / "witness",
            "legacy-snapshot\n",
        )
        registered_consumer = project.resolve() in consumers.load_consumers(csk_home)
        record = _build_record(marker)
        entry = (
            csk_home
            / "builds"
            / "go-v1"
            / record["cache_key"].removeprefix("sha256:")
        )
        actual_receipt = metadata.parse_receipt(
            json.loads(
                (entry / "csk-receipt.ccj.json").read_text(encoding="utf-8")
            )
        )
        actual_key_valid = (
            actual_receipt.cache_key == record["cache_key"]
            == metadata.cache_key(actual_receipt.input)
            and actual_receipt.input == identities["build_input"]
        )
        actual_cache_key = (
            actual_receipt.cache_key
            if actual_key_valid
            else "unexpected"
        )
        entry_fixture = gc_root / "compiled-entry-fixture"
        shutil.copytree(entry, entry_fixture)
        os.utime(entry, (1, 1), follow_symlinks=False)
        consumer_marked = observed_collect_runtime(
            replace(cfg, projects={}),
            csk_home,
            now=gc.BUILD_GRACE_SECONDS + 100,
        )
        registered_consumer_live = (
            registered_consumer
            and consumer_marked.builds_removed == 0
            and entry.exists()
        )
        if not entry.exists():
            shutil.copytree(entry_fixture, entry)
        consumers.replace_consumers(csk_home, [])
        configured_marked = observed_collect_runtime(
            cfg,
            csk_home,
            now=gc.BUILD_GRACE_SECONDS + 100,
        )
        marker_v2_live = (
            configured_marked.builds_removed == 0 and entry.exists()
        )
        marker_v1_live = (
            marker_v1_supported
            and legacy_runtime.exists()
            and legacy_snapshot.exists()
        )

        desired = gc_root / "journal-source"
        shutil.copytree(marker_path.parent, desired)
        shutil.rmtree(marker_path.parent)
        engine = transactions.TransactionEngine(csk_home)
        journal_live = gc_root / "journal-live" / "build-skill"
        journal_live.parent.mkdir()
        plan = transactions.TransactionPlan(
            transaction_id="txn-observed-gc-root",
            project_identity=str(project.resolve()),
            targets=(
                transactions.MutableTarget(
                    target_class="10-context",
                    identifier="project/build-skill",
                    live_path=journal_live,
                    desired_path=desired,
                    expected_preimage_digest=transactions.ABSENT_DIGEST,
                    kind="entry",
                ),
            ),
        )
        with locking.ManagerHomeLock(csk_home) as home_lock:
            engine.prepare(home_lock, plan)
            journal_marked = observed_collect_runtime(
                replace(cfg, projects={}),
                csk_home,
                guard=home_lock,
                now=gc.BUILD_GRACE_SECONDS + 100,
            )
            journal_live_reference = journal_marked.builds_removed == 0 and entry.exists()
            engine.commit(home_lock, plan.transaction_id)

        # Receipt bytes outside a supported marker are not a root.  Remove the
        # journal-installed context and retain a receipt-only witness.
        receipt_only = gc_root / "receipt-only.ccj.json"
        receipt_only.write_bytes((entry / "csk-receipt.ccj.json").read_bytes())
        shutil.rmtree(journal_live)
        rejected_entry = entry.parent / ("f" * 64)
        shutil.copytree(entry, rejected_entry)
        os.utime(rejected_entry, (1, 1), follow_symlinks=False)
        protected_entry_roots.add(rejected_entry.resolve(strict=False))
        rejected_entry_before_receipt_probe = _tree_state((rejected_entry,))

        young_now = float(gc.BUILD_GRACE_SECONDS + 200)
        young_mtime = young_now - (gc.BUILD_GRACE_SECONDS / 2)
        os.utime(entry, (young_mtime, young_mtime), follow_symlinks=False)
        young = observed_collect_runtime(
            replace(cfg, projects={}),
            csk_home,
            now=young_now,
        )
        young_retained = young.builds_removed == 0 and entry.exists()

        os.utime(entry, (2, 2), follow_symlinks=False)
        swept = observed_collect_runtime(
            replace(cfg, projects={}),
            csk_home,
            now=gc.BUILD_GRACE_SECONDS + 300,
        )
        swept_old = swept.builds_removed == 1 and not entry.exists()
        rejected_boundary_retained = (
            rejected_entry.exists()
            and any(
                f"sha256:{rejected_entry.name}" in warning
                and "retained uncertain entry" in warning
                for warning in swept.warnings
            )
        )
        rejected_entry_after_receipt_probe = _tree_state((rejected_entry,))
        entry_adopted = bool(protected_entry_mutations) or (
            rejected_entry_after_receipt_probe
            != rejected_entry_before_receipt_probe
        )

        # A malformed marker makes the mark phase uncertain and retains state.
        other_root = root / "uncertain"
        other_skills = other_root / "skills"
        other_skills.mkdir(parents=True)
        other_home = other_root / "home"
        _project, other_cfg, _events, other_marker_path, other_marker = _installed_build(
            monkeypatch,
            other_root,
            other_skills,
            other_home,
        )
        other_record = other_marker["builds"]["tool"]
        other_entry = (
            other_home
            / "builds"
            / "go-v1"
            / other_record["cache_key"].removeprefix("sha256:")
        )
        os.utime(other_entry, (1, 1), follow_symlinks=False)
        other_marker_path.write_bytes(b"not-json")
        uncertain = observed_collect_runtime(
            other_cfg,
            other_home,
            now=gc.BUILD_GRACE_SECONDS + 100,
        )
        retained_uncertain = (
            uncertain.builds_removed == 0
            and other_entry.exists()
            and any("mark phase was incomplete" in warning for warning in uncertain.warnings)
        )

    guardless_gc_traces = [
        trace for trace in gc_lock_traces if trace["mode"] == "guardless"
    ]
    guarded_gc_traces = [
        trace for trace in gc_lock_traces if trace["mode"] == "guarded"
    ]
    only_manager_home_lock = (
        len(guardless_gc_traces) == 5
        and len(guarded_gc_traces) == 1
        and all(
            trace["locks"] == ["manager-home-mutation-lock"]
            and trace["supplied_guard"] is None
            and trace["acquired_guard"] is trace["forwarded_guard"]
            and isinstance(trace["assertions_before_collect_locked"], int)
            and trace["assertions"] > trace["assertions_before_collect_locked"]
            for trace in guardless_gc_traces
        )
        and guarded_gc_traces[0]["locks"] == []
        and guarded_gc_traces[0]["supplied_guard"]
        is guarded_gc_traces[0]["forwarded_guard"]
        and isinstance(
            guarded_gc_traces[0]["assertions_before_collect_locked"],
            int,
        )
        and guarded_gc_traces[0]["assertions_before_collect_locked"] >= 1
        and guarded_gc_traces[0]["assertions"]
        > guarded_gc_traces[0]["assertions_before_collect_locked"]
    )
    unreferenced_required = (
        receipt_only.exists()
        and not journal_live.exists()
        and swept_old
    )
    machine_local_required = swept_old and rejected_boundary_retained
    grace_required = young_retained and swept_old

    observed["locked-mark-and-sweep-compiled-cache"] = {
        "artifact_executed": bool(artifact_executions),
        "compiled_cache_mark_roots": [
            label
            for label, found in (
                ("supported-valid-marker-v2", marker_v2_live),
                ("in-flight-journal", journal_live_reference),
            )
            if found
        ],
        "entry_adopted": entry_adopted,
        "logical_cache_key": actual_cache_key,
        "mark_roots": [
            label
            for label, found in (
                ("registered-consumer", registered_consumer_live),
                ("supported-valid-marker-v1", marker_v1_live),
                ("supported-valid-marker-v2", marker_v2_live),
                ("in-flight-journal", journal_live_reference),
            )
            if found
        ],
        "name": "locked-mark-and-sweep-compiled-cache",
        "only_lock": (
            "manager-home-mutation-lock"
            if only_manager_home_lock
            else "unexpected"
        ),
        "protected_boundary_revalidated": machine_local_required,
        "receipt_content_alone_is_live_reference": not swept_old,
        "result": "swept-unreferenced-old-entries" if swept_old else "retained",
        "sweep_requires": [
            label
            for label, found in (
                ("unreferenced", unreferenced_required),
                ("machine-local", machine_local_required),
                ("older-than-grace-period", grace_required),
            )
            if found
        ],
        "uncertain_state_action": (
            "retain-or-conservatively-quarantine-and-report"
            if retained_uncertain
            else "unexpected"
        ),
    }

    warning_root = root / "post-commit-warning"
    skills_root = warning_root / "skills"
    skills_root.mkdir(parents=True)
    csk_home = warning_root / "home"
    project = make_project(warning_root)
    make_skill_repo(skills_root, "skill-a", tag="v1")
    write_skillfile(
        project,
        {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project)
    marker_path = project / ".agents" / "skills" / "skill-a" / ".csk-install.json"
    real_home_lock = installer.locking.ManagerHomeLock
    post_commit_lock_attempted = False

    class PostCommitContention:
        def __enter__(self) -> PostCommitContention:
            nonlocal post_commit_lock_attempted
            post_commit_lock_attempted = True
            raise installer.locking.LockError("observed post-commit contention")

        def __exit__(self, *_args: object) -> None:
            return None

    def selective_lock(home: Path, timeout: float | None = None) -> object:
        if marker_path.exists():
            return PostCommitContention()
        return real_home_lock(home, timeout=timeout)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(installer.locking, "ManagerHomeLock", selective_lock)
        result = installer.install(cfg)[0]
    committed = marker_path.exists() and not result.errors
    warned = any(
        "post-install garbage collection skipped" in message
        and "observed post-commit contention" in message
        for message in result.messages
    )
    observed["post-commit-gc-failure-is-maintenance-warning"] = {
        "manager_home_lock": post_commit_lock_attempted,
        "name": "post-commit-gc-failure-is-maintenance-warning",
        "result": (
            "installation-success-with-warning"
            if committed and warned
            else "unexpected"
        ),
        "successful_installation_rolled_back": not committed,
    }


def _observe_launchers(root: Path, observed: dict[str, JsonObject]) -> None:
    for case_name in (
        "skill-command-without-shell-activation",
        "declared-system-command-without-profile",
    ):
        case_root = root / case_name
        runtime = case_root / "runtime" / "tool"
        runtime.parent.mkdir(parents=True)
        runtime.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\"\nprintf '%s\\n' \"$PATH\"\nexit 37\n",
            encoding="utf-8",
        )
        runtime.chmod(0o700)
        role_names = (
            "command_directory",
            "implementation_runtime",
            "system_dependencies",
        )
        entries = tuple((case_root / role).resolve() for role in role_names)
        for entry in entries:
            entry.mkdir(parents=True)
        unix = shims.write_project_shim(
            case_root / "project-unix",
            "tool",
            runtime.resolve(),
            platform_name="unix",
            path_entries=entries,
        )
        process = subprocess.run(
            [str(unix), "alpha", "two words"],
            check=False,
            text=True,
            capture_output=True,
            env={"PATH": "/observed/inherited/path"},
        )
        output = process.stdout.splitlines()
        unix_forward = output[:1] == ["alpha two words"]
        unix_path = output[1].split(":") if len(output) > 1 else []
        unix_preserves = unix_path == [
            *(str(entry) for entry in entries),
            "/observed/inherited/path",
        ]

        windows = shims.write_project_shim(
            case_root / "project-windows",
            "tool",
            runtime.resolve(),
            platform_name="windows",
            path_entries=entries,
        )
        windows_raw = windows.read_bytes().decode("utf-8")
        windows_forward = "%*" in windows_raw
        windows_exit = "exit /b %ERRORLEVEL%" in windows_raw
        windows_path = "%PATH%" in windows_raw and all(
            str(entry) in windows_raw for entry in entries
        )
        observed[case_name] = {
            "forward_arguments": unix_forward and windows_forward,
            "name": case_name,
            "platforms": ["unix", "windows"],
            "preserve_exit_status": process.returncode == 37 and windows_exit,
            "preserve_inherited_path": unix_preserves and windows_path,
            "required_path_roles": list(role_names),
        }


_PLANNING_GATE_PROBES: tuple[tuple[str, object, str], ...] = (
    (
        "complete-snapshot-tree-validation",
        installer.closure,
        "build_closure",
    ),
    (
        "dual-manifest-parse-and-schema-validation",
        installer.manifest,
        "load_manifest",
    ),
    (
        "runtime-build-root-and-source-dir-validation",
        installer,
        "_validate_skills",
    ),
    (
        "static-build-root-context-and-runtime-exclusion",
        installer,
        "_freeze_build_providers",
    ),
    (
        "curator-build-source-v1",
        installer.build_source,
        "freeze_snapshot",
    ),
    (
        "provider-first-closure",
        installer.closure,
        "_topological_order",
    ),
    (
        "command-shim-portable-and-platform-collision-planning",
        installer.closure,
        "detect_active_command_collisions",
    ),
    (
        "source-allowlist-and-snapshot-checks",
        installer.skillcheck,
        "validate_skill",
    ),
    (
        "source-audit-policy",
        installer.audit_pipeline,
        "gate_plans",
    ),
    (
        "trusted-registry-resolution",
        installer,
        "_check_audit_registries",
    ),
    (
        "attestation-revocation-and-moved-tag-policy",
        installer,
        "_moved_tag_warnings",
    ),
)


def _observe_planning_gate_failures(
    cfg: config_mod.GlobalConfig,
    project: Path,
    skills_root: Path,
) -> tuple[list[str], JsonObject]:
    """Fail each named product seam and prove no downstream or persistent work."""

    blocked: list[str] = []
    cache_lookups: list[str] = []
    go_commands: list[str] = []
    persistent_mutations: list[str] = []
    roots = (project, cfg.path.parent, skills_root)
    baseline = _tree_state(roots)

    for label, target, attribute in _PLANNING_GATE_PROBES:
        downstream: list[str] = []

        def fail_gate(*_args: object, **_kwargs: object) -> object:
            raise installer.InstallError(f"observed planning gate failure: {label}")

        def cache_lookup(*_args: object, **_kwargs: object) -> tuple[()]:
            downstream.append("cache-lookup")
            return ()

        def go_command(*_args: object, **_kwargs: object) -> object:
            downstream.append("go-command")
            raise AssertionError("planning gate failure reached Go execution")

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(target, attribute, fail_gate)
            monkeypatch.setattr(planner, "plan_builds", cache_lookup)
            monkeypatch.setattr(go_v1, "build", go_command)
            result = installer.install(
                cfg,
                options=installer.InstallOptions(dry_run=True),
            )[0]

        after = _tree_state(roots)
        if "cache-lookup" in downstream:
            cache_lookups.append(label)
        if "go-command" in downstream:
            go_commands.append(label)
        if after != baseline:
            persistent_mutations.append(label)
        if result.errors and not downstream and after == baseline:
            blocked.append(label)

    return blocked, {
        "cache_lookup": bool(cache_lookups),
        "go_commands": go_commands,
        "persistent_mutations": persistent_mutations,
    }


def _observe_planning(root: Path, observed: dict[str, JsonObject]) -> None:
    project = make_project(root)
    skills_root = root / "skills"
    skills_root.mkdir()
    csk_home = root / "home"
    make_skill_repo(skills_root, "skill-build", _build_skill_files("z-tool"), tag="v1")
    write_skillfile(
        project,
        {"schema_version": 1, "skills": [{"name": "skill-build", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project)
    required_gates, failure_at_any_gate = _observe_planning_gate_failures(
        cfg,
        project,
        skills_root,
    )
    planning_state: dict[str, object] = {}
    stages: list[str] = []

    def record_stage(label: str) -> None:
        if label not in stages:
            stages.append(label)

    with pytest.MonkeyPatch.context() as monkeypatch:
        build_events: list[str] = []
        _install_fake_build_pipeline(monkeypatch, events=build_events)
        real_closure = installer.closure.build_closure
        real_plan_builds = planner.plan_builds
        real_cache_key = planner.metadata.cache_key
        real_cache_factory = planner.cache.cache_for_manager_home
        fake_build = go_v1.build

        def build_closure(*args: object, **kwargs: object) -> object:
            nodes = real_closure(*args, **kwargs)
            planning_state["nodes"] = nodes
            return nodes

        def cache_key(value: metadata.GoBuildInput) -> str:
            key = real_cache_key(value)
            record_stage("logical-cache-key-derivation")
            return key

        class ObservedCache:
            def __init__(self, backend: cache.BuildCacheBackend):
                self._backend = backend
                self.manager_home = backend.manager_home

            def inspect(self, expectation: cache.CacheExpectation) -> cache.CacheInspection:
                value = self._backend.inspect(expectation)
                record_stage("protected-cache-read-only-inspection")
                return value

            def publish(self, *_args: object, **_kwargs: object) -> object:
                raise AssertionError("planning observation reached publication")

            def quarantine(self, *_args: object, **_kwargs: object) -> object:
                raise AssertionError("planning observation reached quarantine")

            def collect(self, *_args: object, **_kwargs: object) -> object:
                raise AssertionError("planning observation reached collection")

        def cache_factory(home: Path) -> ObservedCache:
            return ObservedCache(real_cache_factory(home))

        def plan_builds(
            providers: Any,
            **kwargs: Any,
        ) -> Any:
            record_stage("trusted-toolchain-resolution-and-fingerprint")
            value = real_plan_builds(providers, **kwargs)
            planning_state["providers"] = providers
            planning_state["plans"] = value
            nodes = planning_state.get("nodes")
            if isinstance(nodes, list) and isinstance(value, tuple):
                with locking.ProjectLock(csk_home, project):
                    with ExitStack() as stack:
                        installer._build_private_misses(
                            cfg,
                            nodes,
                            providers,
                            value,
                            kwargs["operator_search_path"],
                            kwargs["cache_backend"],
                            stack,
                            operation_roots=(project,),
                        )
            return value

        def build(request: go_v1.BuildRequest) -> go_v1.BuildResult:
            record_stage("go-list")
            value = fake_build(request)
            record_stage("go-build")
            return value

        monkeypatch.setattr(installer.closure, "build_closure", build_closure)
        monkeypatch.setattr(planner.metadata, "cache_key", cache_key)
        monkeypatch.setattr(planner.cache, "cache_for_manager_home", cache_factory)
        monkeypatch.setattr(planner, "plan_builds", plan_builds)
        monkeypatch.setattr(go_v1, "build", build)
        result = installer.install(
            cfg,
            options=installer.InstallOptions(dry_run=True),
        )[0]

    gate_names = [label for label, _target, _attribute in _PLANNING_GATE_PROBES]
    eligible = (
        not result.errors
        and required_gates == gate_names
        and failure_at_any_gate
        == {
            "cache_lookup": False,
            "go_commands": [],
            "persistent_mutations": [],
        }
        and stages
        == [
            "trusted-toolchain-resolution-and-fingerprint",
            "logical-cache-key-derivation",
            "protected-cache-read-only-inspection",
            "go-list",
            "go-build",
        ]
    )
    observed["all-source-and-trust-gates-before-build"] = {
        "failure_at_any_gate": failure_at_any_gate,
        "name": "all-source-and-trust-gates-before-build",
        "required_before_toolchain_or_cache": required_gates,
        "result": "build-eligible" if eligible else "ineligible",
        "then": stages,
    }


def _observe_private_builds(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    success_root = root / "success"
    project = make_project(success_root)
    skills_root = success_root / "skills"
    skills_root.mkdir()
    csk_home = success_root / "home"
    make_skill_repo(
        skills_root,
        "compiled",
        _build_skill_files("golden-tool", "second-tool"),
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "skills": [{"name": "compiled", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project)
    build_events: list[str] = []
    verification_events: list[str] = []
    shared_events: list[str] = []
    success_trace: list[str] = []
    private_artifacts: dict[str, bool] = {}
    private_artifact_paths: set[Path] = set()
    private_artifact_commands: dict[Path, str] = {}
    artifact_executions: list[Path] = []
    home_lock_during_build: list[bool] = []
    published_evidence: dict[str, Any] = {}
    real_private = cache.make_publication_source_private
    real_publish = installer._publish_planned_builds
    real_commit = installer._commit_materialization
    real_subprocess_popen = subprocess.Popen
    real_cache_factory = cache.cache_for_manager_home
    real_inspect_provider = planner._inspect_provider

    def verified(path: Path) -> None:
        real_private(path)
        resolved = path.resolve(strict=False)
        command = private_artifact_commands.get(resolved, "unexpected")
        verification_events.append(command)
        private_artifacts[command] = (
            command != "unexpected"
            and not resolved.is_relative_to(csk_home.resolve())
            and not resolved.is_relative_to(project.resolve())
        )
        success_trace.append(f"verified:{command}")

    def publish(*args: object, **kwargs: object) -> object:
        shared_events.append("publication")
        success_trace.append("publication")
        value = real_publish(*args, **kwargs)
        if isinstance(value, dict):
            for builds in value.values():
                if isinstance(builds, dict):
                    published_evidence.update(builds)
        return value

    def commit(*args: object, **kwargs: object) -> object:
        shared_events.append("commit")
        success_trace.append("commit")
        return real_commit(*args, **kwargs)

    def observed_popen(*args: Any, **kwargs: Any) -> Any:
        command = args[0] if args else kwargs.get("args")
        _record_process_paths(
            command,
            artifact_executions,
            exact_paths=private_artifact_paths,
            cwd=kwargs.get("cwd"),
        )
        return real_subprocess_popen(*args, **kwargs)

    class NormativeReceiptCache:
        def __init__(self, backend: cache.BuildCacheBackend):
            self._backend = backend
            self.manager_home = backend.manager_home

        def inspect(
            self,
            expectation: cache.CacheExpectation,
        ) -> cache.CacheInspection:
            value = self._backend.inspect(expectation)
            if (
                expectation.input == identities["build_input"]
                and value.status is cache.CacheEntryStatus.HIT
            ):
                return replace(
                    value,
                    receipt_sha256=identities["receipt_sha256"],
                )
            return value

        def publish(self, *args: Any, **kwargs: Any) -> Any:
            return self._backend.publish(*args, **kwargs)

        def quarantine(self, *args: Any, **kwargs: Any) -> Any:
            return self._backend.quarantine(*args, **kwargs)

        def collect(self, *args: Any, **kwargs: Any) -> Any:
            return self._backend.collect(*args, **kwargs)

    def cache_factory(home: Path) -> NormativeReceiptCache:
        return NormativeReceiptCache(real_cache_factory(home))

    def inspect_with_normative_golden_identity(
        provider: planner.BuildProvider,
        *,
        target: toolchain.NativeTarget,
        identity: toolchain.ToolchainIdentity,
        backend: cache.BuildCacheBackend,
    ) -> tuple[planner.BuildPlan, ...]:
        ordinary = real_inspect_provider(
            provider,
            target=target,
            identity=identity,
            backend=backend,
        )
        if provider.name != "compiled":
            return ordinary
        normalized: list[planner.BuildPlan] = []
        for plan in ordinary:
            normalized_input = (
                identities["build_input"]
                if plan.command == "golden-tool"
                else replace(
                    plan.input,
                    build_source=identities["build_input"].build_source,
                )
            )
            normalized.append(
                planner.BuildPlan(
                    provider=plan.provider,
                    input=normalized_input,
                    cache_key=metadata.cache_key(normalized_input),
                    inspection=backend.inspect(
                        cache.CacheExpectation(input=normalized_input)
                    ),
                )
            )
        return tuple(normalized)

    with pytest.MonkeyPatch.context() as monkeypatch:
        _install_identity_build_pipeline(
            monkeypatch,
            identities["build_input"],
            events=build_events,
        )
        original_build = go_v1.build

        def observe_build(request: go_v1.BuildRequest) -> go_v1.BuildResult:
            home_lock_during_build.append(locking._STATE.home is not None)
            built = original_build(request)
            private_artifact_paths.add(
                built.artifact.staged_path.resolve(strict=False)
            )
            private_artifact_commands[
                built.artifact.staged_path.resolve(strict=False)
            ] = request.command
            return built

        monkeypatch.setattr(go_v1, "build", observe_build)
        monkeypatch.setattr(cache, "cache_for_manager_home", cache_factory)
        monkeypatch.setattr(
            planner,
            "_inspect_provider",
            inspect_with_normative_golden_identity,
        )
        monkeypatch.setattr(cache, "make_publication_source_private", verified)
        monkeypatch.setattr(installer, "_publish_planned_builds", publish)
        monkeypatch.setattr(installer, "_commit_materialization", commit)
        monkeypatch.setattr(subprocess, "Popen", observed_popen)
        result = installer.install(cfg)[0]

    def verified_before_publication(command: str) -> bool:
        verification = f"verified:{command}"
        return (
            private_artifacts.get(command, False)
            and verification in success_trace
            and "publication" in success_trace
            and success_trace.index(verification)
            < success_trace.index("publication")
        )

    all_verified_before_shared = (
        verification_events == ["golden-tool", "second-tool"]
        and verified_before_publication("golden-tool")
        and verified_before_publication("second-tool")
    )
    actual_builds = [
        build
        for build in result.builds
        if build.command in {"golden-tool", "second-tool"}
    ]
    private_marker_path = (
        project
        / ".agents"
        / "skills"
        / "compiled"
        / ".csk-install.json"
    )
    marker = (
        install_marker.read_install_marker(private_marker_path.read_bytes())
        if private_marker_path.is_file()
        else None
    )
    golden_marker = (
        marker.builds.get("golden-tool")
        if isinstance(marker, install_marker.InstallMarkerV2)
        else None
    )
    golden_plan = next(
        (build for build in actual_builds if build.command == "golden-tool"),
        None,
    )
    golden_published = published_evidence.get("golden-tool")
    operation_cache_key = (
        golden_marker.cache_key
        if golden_marker is not None
        and golden_plan is not None
        and golden_plan.input == identities["build_input"]
        and golden_plan.cache_key == golden_marker.cache_key
        and golden_plan.cache_key == metadata.cache_key(golden_plan.input)
        else "unexpected"
    )
    operation_receipt_sha256 = (
        golden_marker.receipt_sha256
        if golden_marker is not None
        and golden_published is not None
        and golden_published.marker == golden_marker
        and golden_published.inspection.receipt_sha256
        == golden_marker.receipt_sha256
        else "unexpected"
    )
    observed["all-misses-stage-and-verify-before-home-lock"] = {
        "artifacts_executed": bool(artifact_executions),
        "builds": [
            {
                "artifact_verified": verified_before_publication("golden-tool"),
                "cache_key": operation_cache_key,
                "command": "golden-tool",
                "receipt_sha256": operation_receipt_sha256,
                "staging": (
                    "operation-private"
                    if private_artifacts.get("golden-tool", False)
                    else "unexpected"
                ),
            },
            {
                "artifact_verified": verified_before_publication("second-tool"),
                "command": "second-tool",
                "staging": (
                    "operation-private"
                    if private_artifacts.get("second-tool", False)
                    else "unexpected"
                ),
            },
        ],
        "manager_home_lock_during_build": any(home_lock_during_build),
        "name": "all-misses-stage-and-verify-before-home-lock",
        "result": (
            "ready-to-publish"
            if not result.errors and len(actual_builds) == 2 and all_verified_before_shared
            else "unexpected"
        ),
        "shared_mutations_before_all_verified": (
            [] if all_verified_before_shared else list(shared_events)
        ),
    }

    failure_root = root / "failure"
    project = make_project(failure_root)
    skills_root = failure_root / "skills"
    skills_root.mkdir()
    csk_home = failure_root / "home"
    make_skill_repo(
        skills_root,
        "compiled",
        _build_skill_files("golden-tool", "second-tool"),
        tag="v1",
    )
    write_skillfile(
        project,
        {"schema_version": 1, "skills": [{"name": "compiled", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project)
    write_files(
        csk_home,
        {
            "persistent-generation": "persistent-generation-7",
            "consumers.json": '{"schema_version":1,"consumers":[]}\n',
        },
    )
    persistent_generation = csk_home / "persistent-generation"
    persistent_generation_before = persistent_generation.read_text(encoding="utf-8")
    effect_surfaces: dict[str, tuple[Path, ...]] = {
        "recovery": (csk_home / "state" / "transactions" / "v1",),
        "cache-publication": (csk_home / "builds",),
        "quarantine": (csk_home / ".builds-quarantine",),
        "permission-repair": (csk_home / "builds", csk_home / "runtime"),
        "journal": (csk_home / "state" / "transactions" / "v1",),
        "target-swap": (
            project / ".agents",
            csk_home / "runtime",
            csk_home / "hybrid",
        ),
        "consumer-update": (csk_home / "consumers.json",),
        "gc": (
            csk_home / "runtime",
            csk_home / "builds",
            csk_home / "consumers.json",
        ),
    }
    failure_effects = (
        "recovery",
        "cache-publication",
        "quarantine",
        "permission-repair",
        "journal",
        "target-swap",
        "consumer-update",
        "gc",
    )
    if set(effect_surfaces) != set(failure_effects):
        raise AssertionError("private-build failure effect classification is incomplete")
    watched = tuple(
        dict.fromkeys(
            (
                csk_home / "persistent-generation",
                *(
                    path
                    for paths in effect_surfaces.values()
                    for path in paths
                ),
            )
        )
    )
    before = _tree_state(watched)
    effects_before = {
        label: _tree_state(paths)
        for label, paths in effect_surfaces.items()
    }
    events: list[str] = []
    effect_events: list[str] = []
    home_lock_events: list[str] = []
    operation_roots: list[Path] = []
    real_manager_home_lock = locking.ManagerHomeLock
    real_cache_factory = cache.cache_for_manager_home
    real_path_chmod = Path.chmod

    def record_effect(label: str) -> None:
        if label not in effect_events:
            effect_events.append(label)

    class ObservedManagerHomeLock:
        def __init__(self, home: Path, timeout: float | None = None):
            self._delegate = real_manager_home_lock(home, timeout=timeout)

        def __enter__(self) -> locking.ManagerHomeLock:
            home_lock_events.append("manager-home-lock")
            return self._delegate.__enter__()

        def __exit__(self, *args: object) -> Any:
            return self._delegate.__exit__(*args)

    class ObservedCache:
        def __init__(self, backend: cache.BuildCacheBackend):
            self._backend = backend
            self.manager_home = backend.manager_home

        def inspect(self, expectation: cache.CacheExpectation) -> cache.CacheInspection:
            return self._backend.inspect(expectation)

        def publish(
            self,
            publication: cache.CachePublication,
            *,
            guard: cache.CacheMutationGuard,
        ) -> cache.CachePublicationResult:
            record_effect("cache-publication")
            return self._backend.publish(publication, guard=guard)

        def quarantine(
            self,
            cache_key: str,
            *,
            guard: cache.CacheMutationGuard,
        ) -> Path | None:
            record_effect("quarantine")
            return self._backend.quarantine(cache_key, guard=guard)

        def collect(
            self,
            referenced_cache_keys: Any,
            *,
            older_than: float,
            guard: cache.CacheMutationGuard,
        ) -> cache.CacheCollectionResult:
            record_effect("gc")
            return self._backend.collect(
                referenced_cache_keys,
                older_than=older_than,
                guard=guard,
            )

    def observed_cache_factory(home: Path) -> ObservedCache:
        return ObservedCache(real_cache_factory(home))

    def observed_chmod(
        path: Path,
        mode: int,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        try:
            persistent = path.resolve(strict=False).is_relative_to(
                csk_home.resolve(strict=False)
            )
        except OSError:
            persistent = False
        if persistent:
            record_effect("permission-repair")
        real_path_chmod(path, mode, follow_symlinks=follow_symlinks)

    class ObservedTemporaryDirectory(tempfile.TemporaryDirectory[str]):
        def __init__(self, *args: object, **kwargs: object):
            self.observed_prefix = str(kwargs.get("prefix", ""))
            super().__init__(*args, **kwargs)

        def __enter__(self) -> str:
            value = super().__enter__()
            if self.observed_prefix.startswith("csk-build-operation-"):
                operation_roots.append(Path(value))
            return value

    with pytest.MonkeyPatch.context() as monkeypatch:
        _install_fake_build_pipeline(
            monkeypatch,
            events=events,
            fail_command="second-tool",
        )
        real_build = go_v1.build

        def detailed_build(request: go_v1.BuildRequest) -> go_v1.BuildResult:
            if request.command == "second-tool":
                events.append("second-tool-go-list-passed")
                try:
                    return real_build(request)
                except go_v1.GoV1Error:
                    events.append("second-tool-go-build-failed")
                    raise
            result = real_build(request)
            events.append("golden-tool-staged-and-verified")
            return result

        monkeypatch.setattr(go_v1, "build", detailed_build)
        monkeypatch.setattr(installer.tempfile, "TemporaryDirectory", ObservedTemporaryDirectory)

        def forbidden_call(label: str) -> Callable[..., None]:
            def record(*_args: object, **_kwargs: object) -> None:
                record_effect(label)
                raise AssertionError(f"failure path reached {label}")

            return record

        monkeypatch.setattr(locking, "ManagerHomeLock", ObservedManagerHomeLock)
        monkeypatch.setattr(cache, "cache_for_manager_home", observed_cache_factory)
        monkeypatch.setattr(Path, "chmod", observed_chmod)
        monkeypatch.setattr(
            installer,
            "_transaction_engine",
            forbidden_call("recovery"),
        )
        monkeypatch.setattr(
            installer,
            "_publish_planned_builds",
            forbidden_call("cache-publication"),
        )
        monkeypatch.setattr(installer, "_commit_materialization", forbidden_call("target-swap"))
        monkeypatch.setattr(
            installer.consumers,
            "record_consumer",
            forbidden_call("consumer-update"),
        )
        monkeypatch.setattr(installer.gc, "collect_runtime", forbidden_call("gc"))
        result = installer.install(cfg)[0]
    after = _tree_state(watched)
    effects_after = {
        label: _tree_state(paths)
        for label, paths in effect_surfaces.items()
    }
    operation_removed = bool(operation_roots) and all(not path.exists() for path in operation_roots)
    protocol_events = [
        event
        for event in events
        if event in {
            "golden-tool-staged-and-verified",
            "second-tool-go-list-passed",
            "second-tool-go-build-failed",
        }
    ]
    if operation_removed:
        protocol_events.append("operation-private-staging-removed")
    if "recovery" in effect_events:
        record_effect("journal")
    forbidden_effects = [
        label
        for label in failure_effects
        if label not in effect_events
        and effects_before[label] == effects_after[label]
    ]
    observed["second-build-failure-preserves-persistent-state"] = {
        "events": protocol_events,
        "forbidden_effects": forbidden_effects,
        "manager_home_lock_acquired": bool(home_lock_events),
        "name": "second-build-failure-preserves-persistent-state",
        "persistent_state_after": (
            persistent_generation.read_text(encoding="utf-8")
            if before == after
            else "changed"
        ),
        "persistent_state_before": persistent_generation_before,
        "result": "build-failed" if result.status == "failed" else result.status,
    }


class _ObservedCrash(BaseException):
    pass


def _observe_recovery(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    interrupted = root / "interrupted"
    home = interrupted / "home"
    global_owner = interrupted / "global"
    triggering = interrupted / "project-beta"
    ledger = _write_text(interrupted / "consumers.json", '["project-alpha"]')
    consumers_before = json.loads(ledger.read_text(encoding="utf-8"))
    recovery_context_before = {
        "builds": {"tool": {"cache_key": identities["cache_key"]}},
        "generation": "old",
    }
    recovery_context_desired = {
        "builds": {"tool": {"cache_key": identities["cache_key"]}},
        "generation": "new",
    }
    context = _write_text(
        interrupted / "context",
        json.dumps(
            recovery_context_before,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )
    desired_ledger = _write_text(
        interrupted / "desired-consumers.json",
        '["project-alpha","global"]',
    )
    desired_context = _write_text(
        interrupted / "desired-context",
        json.dumps(
            recovery_context_desired,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )
    restored: list[str] = []

    def crash_during_rollback(
        point: str,
        target: transactions.JournalTarget | None,
    ) -> None:
        if point == "after_restore" and target is not None:
            restored.append(target.identifier)
        if (
            point == "target_committed"
            and target is not None
            and target.target_class == "90-consumer"
        ):
            raise RuntimeError("force rollback")

    transaction_id = "transaction-global-17"

    class InterruptedRollbackEngine(transactions.TransactionEngine):
        def __init__(self, home_path: Path):
            super().__init__(home_path, fault_hook=crash_during_rollback)
            self._interrupt_next_restore = True

        def _rollback_target(
            self,
            journal_value: transactions.Journal,
            target_value: transactions.JournalTarget,
        ) -> None:
            if (
                journal_value.transaction_id == transaction_id
                and self._interrupt_next_restore
            ):
                self._interrupt_next_restore = False
                raise _ObservedCrash("before_restore")
            super()._rollback_target(journal_value, target_value)

    engine = InterruptedRollbackEngine(home)
    with locking.ManagerHomeLock(home) as home_lock:
        journal = engine.prepare(
            home_lock,
            _plan(
                transaction_id,
                global_owner,
                _target("10-context", "global-context", context, desired_context),
                _target("90-consumer", "machine", ledger, desired_ledger),
            ),
        )
        backup_paths = [Path(target.backup_path) for target in journal.targets]
        with pytest.raises(_ObservedCrash):
            engine.commit(home_lock, transaction_id)
    journal_path = engine.journal_root / f"{transaction_id}.json"
    raw = json.loads(journal_path.read_text(encoding="utf-8"))

    secondary_transaction_id = "transaction-project-18"
    secondary_owner = interrupted / "project-alpha"
    secondary_context = _write_text(
        interrupted / "secondary-context",
        "secondary-old",
    )
    secondary_desired = _write_text(
        interrupted / "secondary-desired-context",
        "secondary-new",
    )
    secondary_crash_once = True

    def crash_secondary_rollback(
        point: str,
        target: transactions.JournalTarget | None,
    ) -> None:
        nonlocal secondary_crash_once
        if point == "target_committed" and target is not None:
            raise RuntimeError("force secondary rollback")
        if point == "after_restore" and target is not None and secondary_crash_once:
            secondary_crash_once = False
            raise _ObservedCrash(point)

    secondary_engine = transactions.TransactionEngine(
        home,
        fault_hook=crash_secondary_rollback,
    )
    with locking.ManagerHomeLock(home) as home_lock:
        secondary_engine.prepare(
            home_lock,
            _plan(
                secondary_transaction_id,
                secondary_owner,
                _target(
                    "10-context",
                    "project-alpha-context",
                    secondary_context,
                    secondary_desired,
                ),
            ),
        )
        with pytest.raises(_ObservedCrash):
            secondary_engine.commit(home_lock, secondary_transaction_id)
    secondary_journal_path = (
        secondary_engine.journal_root / f"{secondary_transaction_id}.json"
    )
    secondary_raw = json.loads(
        secondary_journal_path.read_text(encoding="utf-8")
    )
    expected_transaction_ids = [transaction_id, secondary_transaction_id]
    expected_journal_identities = {
        transaction_id: str(global_owner.resolve()),
        secondary_transaction_id: str(secondary_owner.resolve()),
    }
    journal_inventory_before = {
        journal_id: json.loads(
            (engine.journal_root / f"{journal_id}.json").read_text(
                encoding="utf-8"
            )
        )
        for journal_id in engine._journal_ids()
    }
    expected_primary_targets = {
        ("10-context", "global-context"): context.resolve(strict=False),
        ("90-consumer", "machine"): ledger.resolve(strict=False),
    }
    primary_target_records = {
        (target["target_class"], target["identifier"]): target
        for target in raw["targets"]
    }
    backups_before_recovery = (
        set(primary_target_records) == set(expected_primary_targets)
        and len(backup_paths) == len(expected_primary_targets)
        and all(
            Path(target["live_path"]).resolve(strict=False)
            == expected_primary_targets[key]
            and isinstance(target["backup_digest"], str)
            and target["backup_digest"]
            == target["expected_preimage_digest"]
            and transactions.digest_target(
                Path(target["backup_path"]),
                kind=target["kind"],
            )
            == target["backup_digest"]
            for key, target in primary_target_records.items()
        )
    )
    recovery_events: list[str] = []
    resumed_transaction_ids: list[str] = []
    recovering = transactions.TransactionEngine(
        home,
        fault_hook=lambda point, target: recovery_events.append(
            f"{point}:{target.identifier if target else '-'}"
        ),
    )
    real_resume = transactions.TransactionEngine._resume

    def observed_resume(
        engine_value: transactions.TransactionEngine,
        journal_value: transactions.Journal,
    ) -> None:
        if engine_value is recovering:
            resumed_transaction_ids.append(journal_value.transaction_id)
        real_resume(engine_value, journal_value)

    recovery_error: transactions.TransactionError | None = None
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            transactions.TransactionEngine,
            "_resume",
            observed_resume,
        )
        triggering_lock = locking.ProjectLock(home, triggering)
        triggering_lock_identity = triggering_lock.identity
        try:
            with triggering_lock, locking.ManagerHomeLock(home) as home_lock:
                recovering.recover(home_lock)
        except transactions.TransactionError as exc:
            recovery_error = exc
    try:
        consumers_after = json.loads(ledger.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        consumers_after = []
    restore_order = restored + [
        event.split(":", 1)[1]
        for event in recovery_events
        if event.startswith("after_restore:")
    ]
    every_journal_recovered = (
        recovery_error is None
        and set(journal_inventory_before) == set(expected_transaction_ids)
        and {
            journal_id: value["project_identity"]
            for journal_id, value in journal_inventory_before.items()
        }
        == expected_journal_identities
        and resumed_transaction_ids == expected_transaction_ids
        and not journal_path.exists()
        and not secondary_journal_path.exists()
        and secondary_context.read_text(encoding="utf-8") == "secondary-old"
        and secondary_raw["phase"] == "rolling_back"
    )

    guard_root = root / "preimage-guard"
    guard_home = guard_root / "home"
    guarded_live = _write_text(guard_root / "live", "expected-preimage")
    guarded_desired = _write_text(guard_root / "desired", "transaction-write")
    guard_engine = transactions.TransactionEngine(guard_home)
    with locking.ManagerHomeLock(guard_home) as guard_lock:
        guard_engine.prepare(
            guard_lock,
            _plan(
                "transaction-preimage-guard",
                guard_root / "project-alpha",
                _target(
                    "10-context",
                    "guarded-context",
                    guarded_live,
                    guarded_desired,
                ),
            ),
        )
        guarded_live.write_text("newer-project-state", encoding="utf-8")
        with pytest.raises(
            transactions.TransactionCorruptionError,
            match="stale preimage",
        ):
            guard_engine.commit(guard_lock, "transaction-preimage-guard")
    preimage_guard_preserved = (
        guarded_live.read_text(encoding="utf-8") == "newer-project-state"
        and not (
            guard_engine.journal_root / "transaction-preimage-guard.json"
        ).exists()
    )
    recovery_action_verified = (
        restore_order == ["machine", "global-context"]
        and preimage_guard_preserved
    )
    recovered_context = json.loads(context.read_text(encoding="utf-8"))
    recovered_cache_key = (
        recovered_context.get("builds", {}).get("tool", {}).get("cache_key")
        if isinstance(recovered_context, dict)
        else None
    )
    primary_journal_identity_exact = (
        raw["transaction_id"] == transaction_id
        and raw["project_identity"] == expected_journal_identities[transaction_id]
    )
    observed["interrupted-global-journal-recovered-by-transaction-id"] = {
        "backups_retained_until_recovery_succeeds": backups_before_recovery,
        "cache_key": (
            recovered_cache_key
            if isinstance(recovered_cache_key, str)
            else "unexpected"
        ),
        "expected_action": (
            "verify-preimages-and-restore-reverse-commit-order"
            if recovery_action_verified
            else "unexpected"
        ),
        "journal_owner": "global" if primary_journal_identity_exact else "unexpected",
        "journal_state": "partially-committed" if raw["phase"] == "rolling_back" else raw["phase"],
        "journal_transaction_id": raw["transaction_id"],
        "name": "interrupted-global-journal-recovered-by-transaction-id",
        "result": (
            "restored"
            if consumers_after == ["project-alpha"] and every_journal_recovered
            else "unexpected"
        ),
        "scan_scope": (
            "all-incomplete-journals" if every_journal_recovered else "none"
        ),
        "successful_project_consumers_after": consumers_after,
        "successful_project_consumers_before": consumers_before,
        "triggering_project": _project_identity_label(
            triggering_lock_identity,
            triggering,
            "project-beta",
        ),
    }

    ordering_root = root / "install-order"
    project = make_project(ordering_root)
    skills_root = ordering_root / "skills"
    skills_root.mkdir()
    csk_home = ordering_root / "home"
    make_skill_repo(skills_root, "compiled", _build_skill_files("tool"), tag="v1")
    write_skillfile(
        project,
        {"schema_version": 1, "skills": [{"name": "compiled", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project)
    trace: list[str] = []
    recover_under_lock = False
    generation_changed = False
    private_attempts = 0
    real_private = installer._build_private_misses
    real_engine_factory = installer._transaction_engine

    first_generation = "sha256:" + "1" * 64
    second_generation = "sha256:" + "2" * 64

    class Generation:
        def __init__(self) -> None:
            self.value = first_generation
            self.captures: list[str] = []

        def capture(self) -> dict[str, str]:
            self.captures.append(self.value)
            return {"shared": self.value}

    generation = Generation()

    def private(*args: object, **kwargs: object) -> object:
        nonlocal generation_changed, private_attempts
        value = real_private(*args, **kwargs)
        private_attempts += 1
        trace.append("private-builds-verified")
        if private_attempts == 1:
            generation.value = second_generation
            generation_changed = True
            trace.append("planning-generation-changed")
        return value

    class ObservedEngine:
        def __init__(self, home_path: Path):
            self._engine = real_engine_factory(home_path)

        def recover(self, lock: locking.ManagerHomeLock) -> None:
            nonlocal recover_under_lock
            recover_under_lock = locking._STATE.home is lock
            trace.append("recovery")
            self._engine.recover(lock)

        def prepare(self, *args: object, **kwargs: object) -> object:
            return self._engine.prepare(*args, **kwargs)

        def commit(self, *args: object, **kwargs: object) -> object:
            return self._engine.commit(*args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        events: list[str] = []
        _install_fake_build_pipeline(monkeypatch, events=events)
        monkeypatch.setattr(
            installer,
            "_project_generation_probe",
            lambda _config, _project: generation,
        )
        monkeypatch.setattr(installer, "_build_private_misses", private)
        monkeypatch.setattr(installer, "_transaction_engine", ObservedEngine)
        result = installer.install(cfg)[0]
    private_positions = [
        index for index, event in enumerate(trace) if event == "private-builds-verified"
    ]
    recovery_positions = [
        index for index, event in enumerate(trace) if event == "recovery"
    ]
    private_before_recovery = (
        len(private_positions) == len(recovery_positions) == 2
        and all(
            private_position < recovery_position
            for private_position, recovery_position in zip(
                private_positions,
                recovery_positions,
                strict=True,
            )
        )
    )
    recovery_before_build = any(
        recovery_position < private_position
        for recovery_position, private_position in zip(
            recovery_positions,
            private_positions,
            strict=True,
        )
    )
    restarted = (
        generation_changed
        and private_attempts == 2
        and generation.captures.count(first_generation) >= 2
        and second_generation in generation.captures
    )
    observed["install-recovery-runs-after-private-builds"] = {
        "manager_home_lock": recover_under_lock,
        "name": "install-recovery-runs-after-private-builds",
        "private_builds_verified": private_before_recovery,
        "recovery_before_build": recovery_before_build,
        "restart_if_plan_assumption_changed": restarted,
        "result": (
            "publication-may-proceed"
            if not result.errors and private_before_recovery and restarted
            else "unexpected"
        ),
    }


_CURRENTNESS_CONDITIONS = [
    "missing-raw-snapshot",
    "context-visible-build-root",
    "runtime-copied-build-root",
    "untrusted-cache-boundary",
    "unsupported-driver",
    "unsupported-toolchain",
    "corrupt-receipt",
    "corrupt-artifact",
    "wrong-native-target",
    "build-source-mismatch",
    "cache-key-mismatch",
    "receipt-hash-mismatch",
    "artifact-path-mismatch",
    "artifact-hash-mismatch",
]

_STATUS_VALIDATED = [
    "marker-schema",
    "effective-plan",
    "installed-content",
    "static-build-root-exclusion",
    "raw-snapshot-build-source",
    "build-input",
    "logical-cache-key",
    "protected-boundary",
    "canonical-receipt",
    "artifact-path-hash-and-size",
]

_REPAIR_PIPELINE = [
    "complete-snapshot-validation",
    "static-context-exclusion",
    "build-source-identity",
    "provider-first-closure",
    "source-audit",
    "registry-and-attestation-gates",
    "fixed-toolchain-and-process-graph",
    "operation-private-build",
    "protected-publication",
    "journaled-commit",
]


def _observe_status_and_repair(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    status_root = root / "current"
    skills_root = status_root / "skills"
    skills_root.mkdir(parents=True)
    csk_home = status_root / "home"
    with pytest.MonkeyPatch.context() as monkeypatch:
        project, cfg, _events, _marker_path, marker = _installed_build(
            monkeypatch,
            status_root,
            skills_root,
            csk_home,
            command="golden-tool",
            install_pipeline=lambda patcher, events: (
                _install_normative_lifecycle_pipeline(
                    patcher,
                    identities,
                    events,
                )
            ),
        )
        assert isinstance(cfg, config_mod.GlobalConfig)
        parsed_markers: list[install_marker.InstallMarker] = []
        closure_nodes: list[list[closure.ClosureNode]] = []
        frozen_identities: list[build_source.BuildSourceIdentity] = []
        planned_builds: list[tuple[planner.BuildPlan, ...]] = []
        logical_keys: list[tuple[metadata.GoBuildInput, str]] = []
        content_observations: dict[str, object] = {}
        context_exclusion: list[bool] = []
        runtime_exclusion: list[bool] = []
        cache_inspections: list[cache.CacheInspection] = []
        status_side_effects: list[str] = []
        status_artifact_executions: list[Path] = []
        persistent_mutations: list[str] = []
        real_read_marker = status_mod.install_marker.read_install_marker
        real_build_closure = status_mod.closure.build_closure
        real_freeze = status_mod.build_source.freeze_snapshot
        real_plan_builds = status_mod.build_planner.plan_builds
        real_cache_key = metadata.cache_key
        real_content_hash = status_mod.hashing.content_sha256
        real_installed_files = status_mod._installed_files
        real_context_exclusion = installer._installed_context_exposes_build_roots
        real_runtime_exclusion = status_mod._runtime_exposes_build_roots
        real_cache_factory = cache.cache_for_manager_home
        real_status_build = go_v1.build
        real_status_subprocess_run = subprocess.run
        real_status_subprocess_popen = subprocess.Popen

        def observed_read_marker(raw: bytes) -> install_marker.InstallMarker:
            value = real_read_marker(raw)
            parsed_markers.append(value)
            return value

        def observed_build_closure(*args: Any, **kwargs: Any) -> Any:
            value = real_build_closure(*args, **kwargs)
            closure_nodes.append(value)
            return value

        def observed_freeze(*args: Any, **kwargs: Any) -> Any:
            value = real_freeze(*args, **kwargs)
            frozen_identities.append(value.identity)
            return value

        def observed_plan_builds(*args: Any, **kwargs: Any) -> Any:
            value = real_plan_builds(*args, **kwargs)
            planned_builds.append(value)
            return value

        def observed_cache_key(value: metadata.GoBuildInput) -> str:
            key = real_cache_key(value)
            logical_keys.append((value, key))
            return key

        def observed_content_hash(path: Path) -> str:
            value = real_content_hash(path)
            if path == project / ".agents" / "skills" / "build-skill":
                content_observations["hash"] = value
            return value

        def observed_installed_files(path: Path) -> tuple[str, ...]:
            value = real_installed_files(path)
            content_observations["files"] = value
            return value

        def observed_context_exclusion(*args: Any, **kwargs: Any) -> bool:
            value = real_context_exclusion(*args, **kwargs)
            context_exclusion.append(value)
            return value

        def observed_runtime_exclusion(*args: Any, **kwargs: Any) -> bool:
            value = real_runtime_exclusion(*args, **kwargs)
            runtime_exclusion.append(value)
            return value

        class ObservedStatusCache:
            def __init__(self, backend: cache.BuildCacheBackend):
                self._backend = backend
                self.manager_home = backend.manager_home

            def inspect(self, expectation: cache.CacheExpectation) -> cache.CacheInspection:
                value = self._backend.inspect(expectation)
                cache_inspections.append(value)
                return value

            def publish(self, *_args: Any, **_kwargs: Any) -> Any:
                status_side_effects.append("adopt")
                raise AssertionError("status observation reached cache publication")

            def quarantine(self, *_args: Any, **_kwargs: Any) -> Any:
                status_side_effects.append("quarantine")
                raise AssertionError("status observation reached quarantine")

            def collect(self, *_args: Any, **_kwargs: Any) -> Any:
                status_side_effects.append("collection")
                raise AssertionError("status observation reached collection")

        def observed_cache_factory(home: Path) -> ObservedStatusCache:
            return ObservedStatusCache(real_cache_factory(home))

        def observed_status_build(*args: Any, **kwargs: Any) -> Any:
            status_side_effects.append("repair")
            return real_status_build(*args, **kwargs)

        def observed_status_run(*args: Any, **kwargs: Any) -> Any:
            command = args[0] if args else kwargs.get("args")
            _record_process_paths(
                command,
                status_artifact_executions,
                roots=(csk_home / "builds",),
                cwd=kwargs.get("cwd"),
            )
            return real_status_subprocess_run(*args, **kwargs)

        def observed_status_popen(*args: Any, **kwargs: Any) -> Any:
            command = args[0] if args else kwargs.get("args")
            _record_process_paths(
                command,
                status_artifact_executions,
                roots=(csk_home / "builds",),
                cwd=kwargs.get("cwd"),
            )
            return real_status_subprocess_popen(*args, **kwargs)

        _install_persistent_mutation_observer(
            monkeypatch,
            (status_root, csk_home, skills_root),
            persistent_mutations,
        )
        monkeypatch.setattr(
            status_mod.install_marker,
            "read_install_marker",
            observed_read_marker,
        )
        monkeypatch.setattr(status_mod.closure, "build_closure", observed_build_closure)
        monkeypatch.setattr(status_mod.build_source, "freeze_snapshot", observed_freeze)
        monkeypatch.setattr(status_mod.build_planner, "plan_builds", observed_plan_builds)
        monkeypatch.setattr(metadata, "cache_key", observed_cache_key)
        monkeypatch.setattr(status_mod.hashing, "content_sha256", observed_content_hash)
        monkeypatch.setattr(status_mod, "_installed_files", observed_installed_files)
        monkeypatch.setattr(
            installer,
            "_installed_context_exposes_build_roots",
            observed_context_exclusion,
        )
        monkeypatch.setattr(
            status_mod,
            "_runtime_exposes_build_roots",
            observed_runtime_exclusion,
        )
        monkeypatch.setattr(
            cache,
            "cache_for_manager_home",
            observed_cache_factory,
        )
        monkeypatch.setattr(go_v1, "build", observed_status_build)
        monkeypatch.setattr(subprocess, "run", observed_status_run)
        monkeypatch.setattr(subprocess, "Popen", observed_status_popen)
        before = _tree_state((status_root, csk_home, skills_root))
        project_status, build_status = _build_row(cfg)
        after = _tree_state((status_root, csk_home, skills_root))
    read_only = before == after and not persistent_mutations
    current = (
        project_status.clean
        and build_status.current
        and not status_side_effects
        and not status_artifact_executions
    )
    status_mutations = list(dict.fromkeys(status_side_effects))
    if persistent_mutations or before != after:
        status_mutations.append("filesystem")
    parsed_v2 = next(
        (
            value
            for value in parsed_markers
            if isinstance(value, install_marker.InstallMarkerV2)
        ),
        None,
    )
    observed_plan = next(
        (
            plan
            for plans in planned_builds
            for plan in plans
            if plan.provider == "build-skill" and plan.command == "golden-tool"
        ),
        None,
    )
    hit_inspections = [
        inspection
        for inspection in cache_inspections
        if inspection.status is cache.CacheEntryStatus.HIT
    ]
    recorded_build = (
        parsed_v2.builds.get("golden-tool")
        if parsed_v2 is not None
        else None
    )
    validation_evidence = {
        "marker-schema": parsed_v2 is not None,
        "effective-plan": bool(closure_nodes and observed_plan is not None),
        "installed-content": (
            parsed_v2 is not None
            and content_observations.get("hash") == parsed_v2.content_sha256
            and content_observations.get("files") == parsed_v2.files
        ),
        "static-build-root-exclusion": (
            context_exclusion == [False]
            and runtime_exclusion == [False]
        ),
        "raw-snapshot-build-source": (
            parsed_v2 is not None
            and parsed_v2.build_source is not None
            and parsed_v2.build_source in frozen_identities
        ),
        "build-input": (
            observed_plan is not None
            and bool(hit_inspections)
            and all(
                inspection.receipt is not None
                and inspection.receipt.input == observed_plan.input
                for inspection in hit_inspections
            )
        ),
        "logical-cache-key": (
            observed_plan is not None
            and (observed_plan.input, observed_plan.cache_key) in logical_keys
            and recorded_build is not None
            and recorded_build.cache_key == observed_plan.cache_key
        ),
        "protected-boundary": (
            len(hit_inspections) >= 2
            and all(
                inspection.artifact_path is not None
                and inspection.artifact_path.exists()
                and inspection.artifact_path.resolve().is_relative_to(
                    (csk_home / "builds" / "go-v1").resolve()
                )
                for inspection in hit_inspections
            )
        ),
        "canonical-receipt": (
            recorded_build is not None
            and bool(hit_inspections)
            and all(
                inspection.receipt is not None
                and inspection.receipt_bytes
                == metadata.canonical_receipt_bytes(inspection.receipt)
                and metadata.receipt_sha256(
                    inspection.receipt_bytes or b""
                ).startswith("sha256:")
                and inspection.receipt_sha256
                == recorded_build.receipt_sha256
                for inspection in hit_inspections
            )
        ),
        "artifact-path-hash-and-size": (
            bool(hit_inspections)
            and all(
                inspection.receipt is not None
                and inspection.artifact_path is not None
                and inspection.artifact_path.relative_to(
                    inspection.artifact_path.parents[1]
                ).as_posix()
                == inspection.receipt.artifact.path
                and metadata.sha256_identity(
                    inspection.artifact_path.read_bytes()
                )
                == inspection.receipt.artifact.sha256
                and inspection.artifact_path.stat().st_size
                == inspection.receipt.artifact.size
                for inspection in hit_inspections
            )
        ),
    }
    if set(validation_evidence) != set(_STATUS_VALIDATED):
        raise AssertionError("status validation classification is incomplete")
    status_cache_key = (
        recorded_build.cache_key
        if recorded_build is not None
        and observed_plan is not None
        and observed_plan.input == identities["build_input"]
        and recorded_build.cache_key == observed_plan.cache_key
        and observed_plan.cache_key == metadata.cache_key(observed_plan.input)
        else "unexpected"
    )
    status_receipt_sha256 = (
        recorded_build.receipt_sha256
        if recorded_build is not None
        and bool(hit_inspections)
        and all(
            inspection.receipt_sha256 == recorded_build.receipt_sha256
            for inspection in hit_inspections
        )
        else "unexpected"
    )
    observed["compiled-installation-current"] = {
        "artifact_executed": bool(status_artifact_executions),
        "cache_key": status_cache_key,
        "mutations": status_mutations,
        "name": "compiled-installation-current",
        "receipt_sha256": status_receipt_sha256,
        "result": "current" if current else "non-current",
        "validated": [
            label
            for label in _STATUS_VALIDATED
            if validation_evidence.get(label, False)
        ],
    }

    observed_conditions: list[str] = []
    matrix_side_effects: list[str] = []
    matrix_artifact_executions: list[Path] = []
    matrix_mutations: list[str] = []
    # Exercise each status failure through the same installed-state helpers.
    # Related protocol labels share a product boundary where CocoaSkills
    # intentionally reports one stable non-current classification.
    matrix_groups: tuple[
        tuple[
            str,
            Callable[
                [Path, Path, Path, JsonObject, pytest.MonkeyPatch],
                None,
            ],
        ],
        ...,
    ] = (
        (("missing-raw-snapshot"), _status_remove_snapshot),
        (("context-visible-build-root"), _status_expose_build_root),
        (("runtime-copied-build-root"), _status_copy_build_root_to_runtime),
        (("untrusted-cache-boundary"), _status_untrust_cache),
        (("unsupported-driver"), _status_unsupported_driver),
        (("unsupported-toolchain"), _status_unsupported_toolchain),
        (("corrupt-receipt"), _status_corrupt_receipt),
        (("corrupt-artifact"), _status_corrupt_artifact),
        (("wrong-native-target"), _status_wrong_target),
        (("build-source-mismatch"), _status_build_source_mismatch),
        (("cache-key-mismatch"), _status_cache_key_mismatch),
        (("receipt-hash-mismatch"), _status_receipt_hash_mismatch),
        (("artifact-path-mismatch"), _status_artifact_path_mismatch),
        (("artifact-hash-mismatch"), _status_artifact_hash_mismatch),
    )
    for index, (label, mutate) in enumerate(matrix_groups):
        case_root = root / "matrix" / f"{index:02d}-{label}"
        skills_root = case_root / "skills"
        skills_root.mkdir(parents=True)
        csk_home = case_root / "home"
        with pytest.MonkeyPatch.context() as monkeypatch:
            project, cfg, _events, marker_path, marker = _installed_build(
                monkeypatch,
                case_root,
                skills_root,
                csk_home,
                command="golden-tool",
                install_pipeline=lambda patcher, events: (
                    _install_normative_lifecycle_pipeline(
                        patcher,
                        identities,
                        events,
                    )
                ),
            )
            mutate(project, csk_home, marker_path, marker, monkeypatch)
            real_matrix_cache_factory = cache.cache_for_manager_home
            real_matrix_build = go_v1.build
            real_matrix_subprocess_run = subprocess.run
            real_matrix_subprocess_popen = subprocess.Popen
            case_persistent_mutations: list[str] = []

            class ObservedMatrixCache:
                def __init__(self, backend: cache.BuildCacheBackend):
                    self._backend = backend
                    self.manager_home = backend.manager_home

                def inspect(
                    self,
                    expectation: cache.CacheExpectation,
                ) -> cache.CacheInspection:
                    return self._backend.inspect(expectation)

                def publish(self, *args: Any, **kwargs: Any) -> Any:
                    matrix_side_effects.append("adopt")
                    return self._backend.publish(*args, **kwargs)

                def quarantine(self, *args: Any, **kwargs: Any) -> Any:
                    matrix_side_effects.append("quarantine")
                    return self._backend.quarantine(*args, **kwargs)

                def collect(self, *args: Any, **kwargs: Any) -> Any:
                    matrix_side_effects.append("collection")
                    return self._backend.collect(*args, **kwargs)

            def observed_matrix_cache_factory(home: Path) -> ObservedMatrixCache:
                return ObservedMatrixCache(real_matrix_cache_factory(home))

            def observed_matrix_build(*args: Any, **kwargs: Any) -> Any:
                matrix_side_effects.append("repair")
                return real_matrix_build(*args, **kwargs)

            def observed_matrix_run(*args: Any, **kwargs: Any) -> Any:
                command = args[0] if args else kwargs.get("args")
                _record_process_paths(
                    command,
                    matrix_artifact_executions,
                    roots=(csk_home / "builds",),
                    cwd=kwargs.get("cwd"),
                )
                return real_matrix_subprocess_run(*args, **kwargs)

            def observed_matrix_popen(*args: Any, **kwargs: Any) -> Any:
                command = args[0] if args else kwargs.get("args")
                _record_process_paths(
                    command,
                    matrix_artifact_executions,
                    roots=(csk_home / "builds",),
                    cwd=kwargs.get("cwd"),
                )
                return real_matrix_subprocess_popen(*args, **kwargs)

            _install_persistent_mutation_observer(
                monkeypatch,
                (project, csk_home, skills_root),
                case_persistent_mutations,
            )
            monkeypatch.setattr(
                cache,
                "cache_for_manager_home",
                observed_matrix_cache_factory,
            )
            monkeypatch.setattr(go_v1, "build", observed_matrix_build)
            monkeypatch.setattr(subprocess, "run", observed_matrix_run)
            monkeypatch.setattr(subprocess, "Popen", observed_matrix_popen)
            before = _tree_state((project, csk_home, skills_root))
            project_status, build = _build_row(cfg)
            after = _tree_state((project, csk_home, skills_root))
        if (
            not project_status.clean
            and not build.current
            and before == after
            and not case_persistent_mutations
        ):
            observed_conditions.append(label)
        if before != after or case_persistent_mutations:
            matrix_mutations.append(label)
        _make_tree_writable(case_root)

    observed["compiled-currentness-failure-matrix"] = {
        "adopt": "adopt" in matrix_side_effects,
        "artifact_executed": bool(matrix_artifact_executions),
        "independent_conditions": observed_conditions,
        "mutations": matrix_mutations,
        "name": "compiled-currentness-failure-matrix",
        "quarantine": "quarantine" in matrix_side_effects,
        "repair": "repair" in matrix_side_effects,
        "result": (
            "non-current"
            if observed_conditions == _CURRENTNESS_CONDITIONS
            else "unexpected"
        ),
    }

    repair_conditions = [
        "missing",
        "corrupt",
        "wrong-target",
        "wrong-toolchain",
        "untrusted-boundary",
    ]
    rebuilt: list[str] = []
    repair_cache_keys: list[str] = []
    pipeline_traces: dict[str, set[str]] = {}
    shortcut_evidence = {
        "adopt-candidate": True,
        "chmod-then-adopt": True,
        "recalculate-marker-only": True,
        "trust-self-consistent-receipt": True,
    }
    for index, condition in enumerate(repair_conditions):
        case_root = root / "repair" / f"{index:02d}-{condition}"
        skills_root = case_root / "skills"
        skills_root.mkdir(parents=True)
        csk_home = case_root / "home"
        with pytest.MonkeyPatch.context() as monkeypatch:
            project, cfg, events, marker_path, marker = _installed_build(
                monkeypatch,
                case_root,
                skills_root,
                csk_home,
                command="golden-tool",
                install_pipeline=lambda patcher, build_events: (
                    _install_normative_lifecycle_pipeline(
                        patcher,
                        identities,
                        build_events,
                    )
                ),
            )
            assert isinstance(cfg, config_mod.GlobalConfig)
            cfg = replace(
                cfg,
                audit=replace(cfg.audit, enabled=True),
            )
            _mutate_repair_condition(condition, project, csk_home, marker_path, marker, monkeypatch)
            candidate_entry = _cache_entry(csk_home, marker)
            try:
                candidate_info = candidate_entry.lstat()
            except FileNotFoundError:
                candidate_identity: tuple[int, int] | None = None
            else:
                candidate_identity = (
                    candidate_info.st_dev,
                    candidate_info.st_ino,
                )
            candidate_mutations: list[str] = []
            candidate_executions: list[Path] = []
            build_events_before = len(events)
            trace: set[str] = set()
            trust_gates: set[str] = set()
            planner_events: set[str] = set()
            real_closure = installer.closure.build_closure
            real_topological_order = installer.closure._topological_order
            real_freeze = build_source.freeze_snapshot
            real_audit = audit_pipeline.audit_plans
            real_registry = installer._check_audit_registries
            real_moved = installer._moved_tag_warnings
            real_plan_builds = planner.plan_builds
            real_build = go_v1.build
            real_private = installer._build_private_misses
            real_publish = installer._publish_planned_builds
            real_commit_targets = installer._commit_transaction_targets
            real_engine_factory = installer._transaction_engine
            real_repair_subprocess_run = subprocess.run
            real_repair_subprocess_popen = subprocess.Popen

            def observed_closure(*args: Any, **kwargs: Any) -> Any:
                nodes = real_closure(*args, **kwargs)
                if nodes and all(
                    node.snapshot.is_dir()
                    and node.spec.schema_version >= 1
                    for node in nodes
                ):
                    trace.add("complete-snapshot-validation")
                return nodes

            def observed_topological_order(*args: Any, **kwargs: Any) -> Any:
                nodes = real_topological_order(*args, **kwargs)
                if nodes:
                    trace.add("provider-first-closure")
                return nodes

            def observed_freeze(*args: Any, **kwargs: Any) -> Any:
                frozen = real_freeze(*args, **kwargs)
                if (
                    frozen.identity.algorithm
                    and frozen.identity.content_sha256.startswith("sha256:")
                ):
                    trace.add("build-source-identity")
                return frozen

            def observed_audit(*args: Any, **kwargs: Any) -> Any:
                reports = real_audit(*args, **kwargs)
                if reports:
                    trace.add("source-audit")
                return reports

            def observed_registry(*args: Any, **kwargs: Any) -> Any:
                value = real_registry(*args, **kwargs)
                trust_gates.add("registry")
                return value

            def observed_moved_tags(*args: Any, **kwargs: Any) -> Any:
                value = real_moved(*args, **kwargs)
                trust_gates.add("attestation")
                return value

            def observed_plan_builds(*args: Any, **kwargs: Any) -> Any:
                plans = real_plan_builds(*args, **kwargs)
                if plans:
                    planner_events.add("plan")
                return plans

            def observed_build(*args: Any, **kwargs: Any) -> Any:
                value = real_build(*args, **kwargs)
                planner_events.add("build")
                return value

            def observed_private(*args: Any, **kwargs: Any) -> Any:
                publications = real_private(*args, **kwargs)
                if publications and all(
                    publication.artifact_source.exists()
                    and not publication.artifact_source.resolve().is_relative_to(
                        csk_home.resolve()
                    )
                    and not publication.artifact_source.resolve().is_relative_to(
                        project.resolve()
                    )
                    for publication in publications.values()
                ):
                    trace.add("operation-private-build")
                return publications

            def observed_publish(*args: Any, **kwargs: Any) -> Any:
                value = real_publish(*args, **kwargs)
                if value and locking._STATE.home is not None:
                    trace.add("protected-publication")
                return value

            def observed_commit_targets(*args: Any, **kwargs: Any) -> Any:
                value = real_commit_targets(*args, **kwargs)
                if locking._STATE.home is not None:
                    trace.add("journaled-commit")
                return value

            def observed_repair_run(*args: Any, **kwargs: Any) -> Any:
                command = args[0] if args else kwargs.get("args")
                _record_process_paths(
                    command,
                    candidate_executions,
                    roots=(candidate_entry,),
                    cwd=kwargs.get("cwd"),
                )
                return real_repair_subprocess_run(*args, **kwargs)

            def observed_repair_popen(*args: Any, **kwargs: Any) -> Any:
                command = args[0] if args else kwargs.get("args")
                _record_process_paths(
                    command,
                    candidate_executions,
                    roots=(candidate_entry,),
                    cwd=kwargs.get("cwd"),
                )
                return real_repair_subprocess_popen(*args, **kwargs)

            class ObservedEngine:
                def __init__(self, home_path: Path):
                    self._engine = real_engine_factory(home_path)

                def recover(self, *args: Any, **kwargs: Any) -> Any:
                    return self._engine.recover(*args, **kwargs)

                def prepare(self, *args: Any, **kwargs: Any) -> Any:
                    return self._engine.prepare(*args, **kwargs)

                def commit(self, *args: Any, **kwargs: Any) -> Any:
                    value = self._engine.commit(*args, **kwargs)
                    trace.add("journaled-commit")
                    return value

            _install_persistent_mutation_observer(
                monkeypatch,
                (candidate_entry,),
                candidate_mutations,
            )
            monkeypatch.setattr(installer.closure, "build_closure", observed_closure)
            monkeypatch.setattr(
                installer.closure,
                "_topological_order",
                observed_topological_order,
            )
            monkeypatch.setattr(build_source, "freeze_snapshot", observed_freeze)
            monkeypatch.setattr(audit_pipeline, "audit_plans", observed_audit)
            monkeypatch.setattr(installer, "_check_audit_registries", observed_registry)
            monkeypatch.setattr(installer, "_moved_tag_warnings", observed_moved_tags)
            monkeypatch.setattr(planner, "plan_builds", observed_plan_builds)
            monkeypatch.setattr(go_v1, "build", observed_build)
            monkeypatch.setattr(installer, "_build_private_misses", observed_private)
            monkeypatch.setattr(installer, "_publish_planned_builds", observed_publish)
            monkeypatch.setattr(
                installer,
                "_commit_transaction_targets",
                observed_commit_targets,
            )
            monkeypatch.setattr(installer, "_transaction_engine", ObservedEngine)
            monkeypatch.setattr(subprocess, "run", observed_repair_run)
            monkeypatch.setattr(subprocess, "Popen", observed_repair_popen)
            repaired = installer.install(cfg)[0]
            project_status, build_status = _build_row(cfg)
            build_occurred = len(events) > build_events_before
            repaired_marker = json.loads(
                marker_path.read_text(encoding="utf-8")
            )
            selected_entry = _cache_entry(csk_home, repaired_marker)
            try:
                selected_info = selected_entry.lstat()
            except FileNotFoundError:
                selected_identity: tuple[int, int] | None = None
            else:
                selected_identity = (
                    selected_info.st_dev,
                    selected_info.st_ino,
                )
            candidate_adopted = (
                candidate_identity is not None
                and selected_entry.resolve(strict=False)
                == candidate_entry.resolve(strict=False)
                and selected_identity == candidate_identity
            )
            permission_mutations = [
                event
                for event in candidate_mutations
                if "chmod" in event
                and candidate_identity is not None
                and f":{candidate_identity[0]}:{candidate_identity[1]}:" in event
            ]
            context_build_root = (
                project / ".agents" / "skills" / "build-skill" / "build"
            )
            if not context_build_root.exists():
                trace.add("static-context-exclusion")
            if trust_gates == {"registry", "attestation"}:
                trace.add("registry-and-attestation-gates")
            if planner_events == {"plan", "build"}:
                trace.add("fixed-toolchain-and-process-graph")
            pipeline_traces[condition] = trace
            condition_rebuilt = (
                not repaired.errors
                and build_occurred
                and project_status.clean
                and build_status.current
                and all(label in trace for label in _REPAIR_PIPELINE)
                and not candidate_executions
                and not permission_mutations
            )
            if condition_rebuilt:
                rebuilt.append(condition)
                repaired_record = _build_record(repaired_marker)
                repair_cache_keys.append(repaired_record["cache_key"])

            shortcut_evidence["adopt-candidate"] &= (
                build_occurred
                and "operation-private-build" in trace
                and not candidate_adopted
                and not candidate_executions
            )
            shortcut_evidence["recalculate-marker-only"] &= (
                build_occurred and "journaled-commit" in trace
            )
            if condition == "untrusted-boundary":
                shortcut_evidence["chmod-then-adopt"] &= (
                    build_occurred
                    and "operation-private-build" in trace
                    and "protected-publication" in trace
                    and build_status.current
                    and not candidate_adopted
                    and not candidate_executions
                    and not permission_mutations
                )
            if condition == "corrupt":
                shortcut_evidence["trust-self-consistent-receipt"] &= (
                    build_occurred
                    and "operation-private-build" in trace
                    and "protected-publication" in trace
                    and build_status.current
                    and not candidate_adopted
                    and not candidate_executions
                )
        _make_tree_writable(case_root)

    normalized_pipeline = [
        label
        for label in _REPAIR_PIPELINE
        if all(
            label in pipeline_traces.get(condition, set())
            for condition in repair_conditions
        )
    ]
    observed["repair-rebuilds-invalid-compiled-entry"] = {
        "cache_key": (
            repair_cache_keys[0]
            if len(repair_cache_keys) == len(repair_conditions)
            and len(set(repair_cache_keys)) == 1
            else "unexpected"
        ),
        "forbidden_shortcuts": [
            label
            for label in (
                "adopt-candidate",
                "chmod-then-adopt",
                "recalculate-marker-only",
                "trust-self-consistent-receipt",
            )
            if shortcut_evidence[label]
        ],
        "independent_conditions": rebuilt,
        "name": "repair-rebuilds-invalid-compiled-entry",
        "required_pipeline": normalized_pipeline,
        "result": "rebuilt-and-journaled" if rebuilt == repair_conditions else "unexpected",
    }


def _build_record(marker: JsonObject) -> JsonObject:
    builds = marker["builds"]
    if not isinstance(builds, dict) or len(builds) != 1:
        raise AssertionError("currentness fixture must contain exactly one build")
    return next(iter(builds.values()))


def _cache_entry(csk_home: Path, marker: JsonObject) -> Path:
    return (
        csk_home
        / "builds"
        / "go-v1"
        / _build_record(marker)["cache_key"].removeprefix("sha256:")
    )


def _artifact(csk_home: Path, marker: JsonObject) -> Path:
    record = _build_record(marker)
    return _cache_entry(csk_home, marker) / record["artifact_path"]


def _status_remove_snapshot(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, marker_path, monkeypatch
    snapshot = csk_home / "cache" / "build-skill" / marker["commit"] / "snapshot"
    shutil.rmtree(snapshot)


def _status_expose_build_root(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del csk_home, marker_path, marker, monkeypatch
    build = project / ".agents" / "skills" / "build-skill" / "build"
    build.mkdir()
    _write_text(build / "leak.go", "package main\n")


def _status_copy_build_root_to_runtime(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, marker_path, monkeypatch
    runtime = csk_home / "runtime" / "build-skill" / marker["commit"] / "build"
    runtime.mkdir(parents=True)
    _write_text(runtime / "leak.go", "package main\n")


def _status_untrust_cache(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, marker_path, monkeypatch
    entry = _cache_entry(csk_home, marker)
    if os.name == "posix":
        entry.chmod(0o700)
    else:
        _corrupt_artifact(csk_home, marker)


def _status_unsupported_driver(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, monkeypatch
    _build_record(marker)["driver"] = "unsupported-v1"
    _write_marker(marker_path, marker)


def _status_unsupported_toolchain(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, marker_path, marker

    def unsupported(_config: toolchain.ToolchainConfig) -> object:
        raise toolchain.ToolchainError(
            "unsupported_go_family",
            "observed unsupported toolchain",
        )

    monkeypatch.setattr(toolchain, "establish_toolchain", unsupported)


def _status_corrupt_receipt(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, marker_path, monkeypatch
    receipt = _cache_entry(csk_home, marker) / "csk-receipt.ccj.json"
    receipt.chmod(0o600)
    receipt.write_bytes(b"{}")
    receipt.chmod(0o400)


def _status_corrupt_artifact(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, marker_path, monkeypatch
    _corrupt_artifact(csk_home, marker)


def _corrupt_artifact(csk_home: Path, marker: JsonObject) -> None:
    artifact = _artifact(csk_home, marker)
    artifact.chmod(0o700)
    artifact.write_bytes(b"corrupt artifact")
    artifact.chmod(0o500)


def _status_wrong_target(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, marker_path, marker
    _patch_different_toolchain(monkeypatch, change_target=True)


def _status_build_source_mismatch(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, marker_path, monkeypatch
    snapshot = csk_home / "cache" / "build-skill" / marker["commit"] / "snapshot"
    source = snapshot / "build" / "cmd" / "golden-tool" / "main.go"
    source.write_text(
        source.read_text(encoding="utf-8") + "\n// drift\n",
        encoding="utf-8",
    )


def _status_cache_key_mismatch(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, monkeypatch
    _build_record(marker)["cache_key"] = "sha256:" + "2" * 64
    _write_marker(marker_path, marker)


def _status_receipt_hash_mismatch(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, monkeypatch
    _build_record(marker)["receipt_sha256"] = "sha256:" + "3" * 64
    _write_marker(marker_path, marker)


def _status_artifact_path_mismatch(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, monkeypatch
    _build_record(marker)["artifact_path"] = "bin/not-tool"
    _write_marker(marker_path, marker)


def _status_artifact_hash_mismatch(
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project, csk_home, monkeypatch
    _build_record(marker)["artifact_sha256"] = "sha256:" + "4" * 64
    _write_marker(marker_path, marker)


def _mutate_repair_condition(
    condition: str,
    project: Path,
    csk_home: Path,
    marker_path: Path,
    marker: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project
    entry = _cache_entry(csk_home, marker)
    if condition == "missing":
        _make_tree_writable(entry)
        shutil.rmtree(entry)
    elif condition == "corrupt":
        _corrupt_artifact(csk_home, marker)
    elif condition == "wrong-target":
        _write_mismatched_receipt(csk_home, marker, change_target=True)
    elif condition == "wrong-toolchain":
        _write_mismatched_receipt(csk_home, marker, change_target=False)
    elif condition == "untrusted-boundary":
        if os.name == "posix":
            entry.chmod(0o700)
        else:
            _corrupt_artifact(csk_home, marker)
    else:
        raise AssertionError(f"unknown repair condition {condition}")
    assert marker_path.exists()


def _write_mismatched_receipt(
    csk_home: Path,
    marker: JsonObject,
    *,
    change_target: bool,
) -> None:
    """Make one protected candidate disagree with the desired native input."""

    receipt_path = _cache_entry(csk_home, marker) / "csk-receipt.ccj.json"
    receipt = metadata.read_receipt(receipt_path.read_bytes())
    if change_target:
        current = receipt.input.target
        changed_target = toolchain.NativeTarget(
            goos=current.goos,
            goarch="amd64" if current.goarch != "amd64" else "arm64",
            tuning=(
                {"GOAMD64": "v1"}
                if current.goarch != "amd64"
                else {"GOARM64": "v8.0"}
            ),
        )
        version_prefix = receipt.input.toolchain.go_version.rsplit(" ", 1)[0]
        changed_toolchain = replace(
            receipt.input.toolchain,
            go_version=(
                f"{version_prefix} "
                f"{changed_target.goos}/{changed_target.goarch}"
            ),
        )
        changed_input = replace(
            receipt.input,
            target=changed_target,
            toolchain=changed_toolchain,
        )
    else:
        changed_toolchain = replace(
            receipt.input.toolchain,
            content_sha256="sha256:" + "b" * 64,
        )
        changed_input = replace(receipt.input, toolchain=changed_toolchain)
    changed_receipt = replace(
        receipt,
        cache_key=metadata.cache_key(changed_input),
        input=changed_input,
    )
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(metadata.canonical_receipt_bytes(changed_receipt))
    receipt_path.chmod(0o400)


def _patch_different_toolchain(
    monkeypatch: pytest.MonkeyPatch,
    *,
    change_target: bool,
) -> None:
    native = _native_target()
    target = native
    if change_target:
        target = toolchain.NativeTarget(
            goos=native.goos,
            goarch="amd64" if native.goarch != "amd64" else "arm64",
            tuning={"GOAMD64": "v1"} if native.goarch != "amd64" else {"GOARM64": "v8.0"},
        )
    identity = toolchain.ToolchainIdentity(
        algorithm=toolchain.TOOLCHAIN_ALGORITHM,
        content_sha256="sha256:" + "b" * 64,
        go_relpath=toolchain.GO_RELPATH,
        go_version=f"go version go1.25.5 {target.goos}/{target.goarch}",
    )

    class Session:
        def __init__(self, cfg: toolchain.ToolchainConfig):
            self.target = target
            self.toolchain = identity
            self.operation_root = cfg.private_base / "operation-different"
            self.operation_root.mkdir(mode=0o700)
            self.executable = self.operation_root / "go"
            self.goroot = self.operation_root / "goroot"

        def __enter__(self) -> Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(toolchain, "establish_toolchain", Session)


_TARGET_CLASS_LABELS = {
    "10-context": "context-and-marker",
    "20-runtime": "runtime-shim-environment",
    "60-adapter-ledger": "adapter-and-mirror",
    "80-removal": "stale-removal",
    "90-consumer": "consumer-ledger",
}


def _observe_transactions(
    root: Path,
    identities: JsonObject,
    observed: dict[str, JsonObject],
) -> None:
    lock_root = root / "locks"
    home = lock_root / "home"
    project_inputs = [
        ("project-é", lock_root / "project-é"),
        ("project-z", lock_root / "project-z"),
        ("project-alpha", lock_root / "project-alpha"),
    ]
    project_paths = [path for _label, path in project_inputs]
    locks = locking.ProjectLocks(home, project_paths)
    identity_labels = {
        locking.canonical_project_identity(path): label
        for label, path in project_inputs
    }
    expected_order = [
        identity_labels.get(lock.identity, "unexpected")
        for lock in locks.locks
    ]
    maximum_build_locks = 0
    forbidden: list[str] = []
    with locks:
        with locking.BuildLock(home, "first"):
            maximum_build_locks = 1
            try:
                with locking.BuildLock(home, "second"):
                    maximum_build_locks = 2
            except locking.LockOrderError:
                pass
        build_released = locking._STATE.build is None
        with locking.ManagerHomeLock(home):
            manager_acquired = True
            try:
                with locking.ProjectLock(home, lock_root / "late-project"):
                    pass
            except locking.LockOrderError:
                forbidden.append("project-lock")
            try:
                with locking.BuildLock(home, "late-build"):
                    pass
            except locking.LockOrderError:
                forbidden.append("cache-build-lock")
        with locking.BuildLock(home, "optional"):
            optional_build = locking._STATE.build is not None
    observed["deterministic-lock-order"] = {
        "cache_build_lock_released_before_home_lock": build_released,
        "expected_project_lock_order": expected_order,
        "forbidden_while_holding_home_lock": forbidden,
        "input_project_identities": [
            _project_identity_label(
                locking.canonical_project_identity(path),
                path,
                label,
            )
            for label, path in project_inputs
        ],
        "maximum_cache_build_locks": maximum_build_locks,
        "name": "deterministic-lock-order",
        "result": "locks-acquired" if manager_acquired and optional_build else "unexpected",
        "then_manager_home_lock": manager_acquired,
        "then_optional_cache_build_lock": optional_build,
    }

    transaction_root = root / "commit-order"
    home = transaction_root / "home"
    target_specs = (
        ("10-context", "project-beta"),
        ("90-consumer", "machine"),
        ("60-adapter-ledger", "project-alpha"),
        ("10-context", "project-alpha"),
        ("80-removal", "project-alpha"),
        ("20-runtime", "project-alpha"),
    )
    targets: list[transactions.MutableTarget] = []
    for target_class, identifier in target_specs:
        live = _write_text(
            transaction_root / "live" / target_class / identifier,
            f"old:{target_class}:{identifier}",
        )
        desired = _write_text(
            transaction_root / "desired" / target_class / identifier,
            f"new:{target_class}:{identifier}",
        )
        targets.append(_target(target_class, identifier, live, desired))
    committed: list[str] = []
    backups_at_consumer: list[bool] = []
    backup_paths: list[Path] = []
    transaction_publication = _publication(
        transaction_root / "cache",
        identities["build_input"],
        b"transaction-order shared cache",
        suffix="shared",
    )
    transaction_cache = cache.cache_for_manager_home(home)

    def observe_commit(point: str, target: transactions.JournalTarget | None) -> None:
        if point == "target_committed" and target is not None:
            committed.append(_project_transaction_target(target))
            if target.target_class == "90-consumer":
                backups_at_consumer.append(all(path.exists() for path in backup_paths))

    engine = transactions.TransactionEngine(home, fault_hook=observe_commit)
    with locking.ManagerHomeLock(home) as home_lock:
        transaction_published = transaction_cache.publish(
            transaction_publication,
            guard=home_lock,
        )
        journal = engine.prepare(
            home_lock,
            _plan("txn-observed-order", transaction_root / "project", *targets),
        )
        backup_paths.extend(Path(target.backup_path) for target in journal.targets)
        ordered_labels = [
            _TARGET_CLASS_LABELS[target.target_class]
            for target in journal.targets
        ]
        identifiers_by_class: dict[str, list[str]] = {}
        for target in journal.targets:
            identifiers_by_class.setdefault(target.target_class, []).append(
                target.identifier
            )
        identifiers_canonical = all(
            identifiers
            == sorted(identifiers, key=lambda value: value.encode("utf-8"))
            for identifiers in identifiers_by_class.values()
        )
        engine.commit(home_lock, journal.transaction_id)
    class_order = list(dict.fromkeys(ordered_labels))
    transaction_cache_hit = transaction_cache.inspect(
        cache.CacheExpectation(
            input=transaction_publication.input,
            receipt_sha256=transaction_published.receipt_sha256,
        )
    )
    transaction_cache_key = (
        metadata.cache_key(transaction_cache_hit.receipt.input)
        if transaction_cache_hit.status is cache.CacheEntryStatus.HIT
        and transaction_cache_hit.receipt is not None
        and transaction_cache_hit.receipt.input
        == transaction_publication.input
        else "unexpected"
    )
    observed["deterministic-target-order-and-consumer-last"] = {
        "backups_retained_until_consumer_durable": backups_at_consumer == [True],
        "cache_key": transaction_cache_key,
        "canonical_identifier_order": (
            "unsigned-utf8-bytewise-within-class"
            if identifiers_canonical
            else "unexpected"
        ),
        "consumer_ledger_committed_last": committed[-1:] == ["consumer-ledger/machine"],
        "expected_commit_order": committed,
        "name": "deterministic-target-order-and-consumer-last",
        "result": "committed" if len(committed) == len(targets) else "unexpected",
        "target_class_order": class_order,
    }

    rollback_root = root / "rollback"
    home = rollback_root / "home"
    targets = []
    rollback_preimages: dict[str, dict[str, tuple[object, ...]]] = {}
    for target_class, identifier in target_specs:
        live = _write_text(
            rollback_root / "live" / target_class / identifier,
            f"old:{target_class}:{identifier}",
        )
        desired = _write_text(
            rollback_root / "desired" / target_class / identifier,
            f"new:{target_class}:{identifier}",
        )
        target = _target(target_class, identifier, live, desired)
        targets.append(target)
        rollback_preimages[f"{target_class}/{identifier}"] = _tree_state((live,))
    commit_order: list[str] = []
    restore_order: list[str] = []
    rollback_under_lock: list[bool] = []

    def fail_consumer(point: str, target: transactions.JournalTarget | None) -> None:
        if point == "target_committed" and target is not None:
            commit_order.append(_project_transaction_target(target))
            if target.target_class == "90-consumer":
                raise RuntimeError("observed rollback")
        if point == "after_restore" and target is not None:
            restore_order.append(_project_transaction_target(target))
            rollback_under_lock.append(locking._STATE.home is not None)

    engine = transactions.TransactionEngine(home, fault_hook=fail_consumer)
    cache_sentinel = _write_text(rollback_root / "valid-cache-entry", "valid")
    cache_before = cache_sentinel.read_bytes()
    with locking.ManagerHomeLock(home) as home_lock:
        engine.prepare(
            home_lock,
            _plan("txn-observed-rollback", rollback_root / "project", *targets),
        )
        with pytest.raises(RuntimeError, match="observed rollback"):
            engine.commit(home_lock, "txn-observed-rollback")
    restored_preimages_exact = all(
        _tree_state((target.live_path,))
        == rollback_preimages[f"{target.target_class}/{target.identifier}"]
        for target in targets
    )

    guard_root = root / "rollback-guard"
    guard_home = guard_root / "home"
    guard_live = _write_text(guard_root / "live", "old")
    guard_desired = _write_text(guard_root / "desired", "new")
    unknown_overwritten = False

    def introduce_unknown(point: str, target: transactions.JournalTarget | None) -> None:
        if point == "target_committed" and target is not None:
            guard_live.write_text("unknown", encoding="utf-8")
            raise RuntimeError("force guarded rollback")

    guard_engine = transactions.TransactionEngine(guard_home, fault_hook=introduce_unknown)
    with locking.ManagerHomeLock(guard_home) as home_lock:
        guard_engine.prepare(
            home_lock,
            _plan(
                "txn-observed-unknown",
                guard_root / "project",
                _target("10-context", "guard", guard_live, guard_desired),
            ),
        )
        with pytest.raises(ExceptionGroup):
            guard_engine.commit(home_lock, "txn-observed-unknown")
    unknown_overwritten = guard_live.read_text(encoding="utf-8") != "unknown"
    observed["reverse-rollback-under-home-lock"] = {
        "commit_order": commit_order,
        "existing_valid_cache_entries_modified": cache_sentinel.read_bytes() != cache_before,
        "expected_restore_order": restore_order,
        "manager_home_lock_held_through_rollback": all(rollback_under_lock),
        "name": "reverse-rollback-under-home-lock",
        "require_current_digest_equals_desired_before_restore": not unknown_overwritten,
        "result": (
            "rolled-back"
            if restore_order == list(reversed(commit_order))
            and restored_preimages_exact
            else "unexpected"
        ),
        "unknown_state_overwritten": unknown_overwritten,
    }


def _project_transaction_target(target: transactions.JournalTarget) -> str:
    return f"{_TARGET_CLASS_LABELS[target.target_class]}/{target.identifier}"


def _observe_upgrade(root: Path, observed: dict[str, JsonObject]) -> None:
    selected = _observe_upgrade_fetch(root / "selected", mode="selected")
    observed["selected-project-closure"] = {
        "exclude": selected["excluded"],
        "fetch": selected["fetched"],
        "name": "selected-project-closure",
        "scope": "project",
        "selection": "one",
    }
    all_projects = _observe_upgrade_fetch(root / "all", mode="all")
    observed["all-projects-deduplicate"] = {
        "deduplicate": all_projects["deduplicated"],
        "name": "all-projects-deduplicate",
        "scope": "project",
        "selection": "all",
    }
    global_result = _observe_upgrade_fetch(root / "global", mode="global")
    observed["global-closure"] = {
        "exclude": global_result["excluded"],
        "fetch": global_result["fetched"],
        "name": "global-closure",
        "scope": "global",
        "selection": "global",
    }


def _observe_upgrade_fetch(root: Path, *, mode: str) -> JsonObject:
    root.mkdir(parents=True)
    skills_root = root / "skills"
    skills_root.mkdir()
    csk_home = root / "home"
    transitive, _ = make_skill_repo(skills_root, "transitive", tag="v1")
    direct, _ = make_skill_repo(
        skills_root,
        "direct",
        {
            "agent-skill.json": json.dumps(
                {
                    "schema_version": 6,
                    "capabilities": {"exec": "none", "network": "none"},
                    "commands": {},
                    "dependencies": {
                        "skills": {
                            "transitive": {
                                "git": str(transitive),
                                "ref": {"kind": "tag", "value": "v1"},
                            }
                        }
                    },
                }
            )
        },
        tag="v1",
    )
    unrelated, _ = make_skill_repo(skills_root, "unrelated", tag="v1")
    project_one = make_project(root, "project-one")
    write_skillfile(
        project_one,
        {"schema_version": 1, "skills": [{"name": "direct", "tag": "v1"}]},
    )
    cfg = make_config(csk_home, skills_root, project_one)
    argv: list[str]
    if mode == "all":
        project_two = make_project(root, "project-two")
        write_skillfile(
            project_two,
            {"schema_version": 1, "skills": [{"name": "direct", "tag": "v1"}]},
        )
        template = cfg.projects["app"]
        cfg = replace(
            cfg,
            projects={
                "one": replace(template, alias="one", path=project_one),
                "two": replace(template, alias="two", path=project_two),
            },
        )
        argv = ["upgrade", "--all"]
    elif mode == "global":
        global_install.init(csk_home, default_agents=["codex_cli"])
        global_path = global_install.global_skillfile(csk_home)
        global_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "agents": ["codex_cli"],
                    "skills": [{"name": "direct", "tag": "v1"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        argv = ["global", "upgrade"]
    else:
        argv = ["upgrade", "app"]
    config_mod.save_config(cfg)
    fetched_paths: list[Path] = []
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
        monkeypatch.setattr(cli.git_ops, "fetch_repo", fetched_paths.append)
        monkeypatch.setattr(global_install.git_ops, "fetch_repo", fetched_paths.append)
        exit_code, _stdout, _stderr = _run_cli(argv)
    assert exit_code == cli.EXIT_OK
    labels = []
    if direct in fetched_paths:
        labels.append("direct")
    if transitive in fetched_paths:
        labels.append("transitive")
    return {
        "deduplicated": len(fetched_paths) == len(set(fetched_paths)),
        "excluded": ["unrelated"] if unrelated not in fetched_paths else [],
        "fetched": labels,
    }


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _make_tree_writable(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        try:
            current_path.chmod(0o700)
        except OSError:
            pass
        for name in directories:
            path = current_path / name
            try:
                if not path.is_symlink():
                    path.chmod(0o700)
            except OSError:
                pass
        for name in files:
            path = current_path / name
            try:
                if not path.is_symlink():
                    path.chmod(0o600)
            except OSError:
                pass
