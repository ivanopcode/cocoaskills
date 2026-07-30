from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from csk.builds import metadata, planner, source, toolchain
from csk.builds.cache import (
    CacheEntryStatus,
    CacheExpectation,
    CacheInspection,
)


class _FakeToolchainSession:
    def __init__(self, events: list[str]):
        self.target = toolchain.NativeTarget(
            goos="darwin",
            goarch="arm64",
            tuning={"GOARM64": "v8.0"},
        )
        self.toolchain = toolchain.ToolchainIdentity(
            algorithm=toolchain.TOOLCHAIN_ALGORITHM,
            content_sha256="sha256:" + "a" * 64,
            go_relpath=toolchain.GO_RELPATH,
            go_version="go version go1.25.5 darwin/arm64",
        )
        self._events = events

    def __enter__(self) -> _FakeToolchainSession:
        self._events.append("toolchain-enter")
        return self

    def __exit__(self, *args: object) -> None:
        self._events.append("toolchain-exit")


class _FakeCache:
    def __init__(
        self,
        manager_home: Path,
        statuses: Mapping[str, CacheEntryStatus],
        events: list[str],
    ):
        self.manager_home = manager_home
        self._statuses = statuses
        self._events = events

    def inspect(self, expectation: CacheExpectation) -> CacheInspection:
        command = expectation.input.command
        self._events.append(f"cache:{command}")
        return CacheInspection(
            status=self._statuses[command],
            reason=f"{command} fixture",
        )

    def publish(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("planning must not publish cache entries")

    def quarantine(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("planning must not quarantine cache entries")


class _SequenceGeneration:
    def __init__(self, values: list[Mapping[str, str]]):
        self._values = values
        self.calls = 0

    def capture(self) -> Mapping[str, str]:
        index = min(self.calls, len(self._values) - 1)
        self.calls += 1
        return self._values[index]


def _command(name: str) -> planner.BuildCommand:
    return planner.BuildCommand(
        name=name,
        driver="go-v1",
        build_root="build",
        source_dir=f"build/cmd/{name}",
    )


def test_plan_builds_is_provider_first_command_lexical_and_records_all_outcomes(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "source.txt").write_text("first", encoding="utf-8")
    (second_root / "source.txt").write_text("second", encoding="utf-8")
    manager_home = tmp_path / "manager"
    manager_home.mkdir()
    events: list[str] = []
    statuses = {
        "z-hit": CacheEntryStatus.HIT,
        "a-miss": CacheEntryStatus.MISS,
        "b-untrusted": CacheEntryStatus.UNTRUSTED_PROVENANCE,
        "c-corrupt": CacheEntryStatus.CORRUPT,
        "d-unsupported": CacheEntryStatus.UNSUPPORTED,
    }
    cache = _FakeCache(manager_home, statuses, events)
    configs: list[toolchain.ToolchainConfig] = []

    def establish(config: toolchain.ToolchainConfig) -> _FakeToolchainSession:
        assert config.private_base.is_dir()
        configs.append(config)
        events.append("toolchain")
        return _FakeToolchainSession(events)

    with source.freeze_snapshot(first_root) as first, source.freeze_snapshot(second_root) as second:
        providers = (
            planner.BuildProvider(
                name="provider-first",
                snapshot=first,
                commands=(_command("z-hit"),),
            ),
            planner.BuildProvider(
                name="provider-second",
                snapshot=second,
                commands=(
                    _command("d-unsupported"),
                    _command("c-corrupt"),
                    _command("b-untrusted"),
                    _command("a-miss"),
                ),
            ),
        )
        plans = planner.plan_builds(
            providers,
            manager_home=manager_home,
            operator_search_path=toolchain.OperatorSearchPath(("/trusted/bin",)),
            cache_backend=cache,
            establish_toolchain=establish,
        )

    assert [(plan.provider, plan.command) for plan in plans] == [
        ("provider-first", "z-hit"),
        ("provider-second", "a-miss"),
        ("provider-second", "b-untrusted"),
        ("provider-second", "c-corrupt"),
        ("provider-second", "d-unsupported"),
    ]
    assert [plan.result for plan in plans] == [
        "cache-hit",
        "would-preflight-and-build",
        "would-rebuild-untrusted-cache",
        "corrupt",
        "unsupported",
    ]
    assert events == [
        "toolchain",
        "toolchain-enter",
        "cache:z-hit",
        "cache:a-miss",
        "cache:b-untrusted",
        "cache:c-corrupt",
        "cache:d-unsupported",
        "toolchain-exit",
    ]
    assert len(configs) == 1
    assert not configs[0].private_base.exists()
    assert first_root in configs[0].forbidden_roots
    assert second_root in configs[0].forbidden_roots
    assert manager_home in configs[0].forbidden_roots
    for plan in plans:
        assert plan.cache_key == metadata.cache_key(plan.input)
        assert plan.target == plan.input.target
        assert plan.artifact_path == plan.input.artifact_path
        assert plan.to_json() == {
            "build_root": plan.input.build_root,
            "build_source": {
                "algorithm": plan.input.build_source.algorithm,
                "content_sha256": plan.input.build_source.content_sha256,
            },
            "cache_key": plan.cache_key,
            "command": plan.input.command,
            "driver": plan.input.driver,
            "result": plan.result,
            "source_dir": plan.input.source_dir,
            "target": {
                "goarch": "arm64",
                "goos": "darwin",
                "tuning": {"GOARM64": "v8.0"},
            },
        }


def test_plan_builds_retries_a_changed_generation(tmp_path: Path) -> None:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "source.txt").write_text("source", encoding="utf-8")
    manager_home = tmp_path / "manager"
    manager_home.mkdir()
    events: list[str] = []
    cache = _FakeCache(
        manager_home,
        {"tool": CacheEntryStatus.MISS},
        events,
    )
    generation = _SequenceGeneration(
        [
            {"shared": "sha256:" + "0" * 64},
            {"shared": "sha256:" + "1" * 64},
            {"shared": "sha256:" + "1" * 64},
            {"shared": "sha256:" + "1" * 64},
        ]
    )

    def establish(_config: toolchain.ToolchainConfig) -> _FakeToolchainSession:
        events.append("toolchain")
        return _FakeToolchainSession(events)

    with source.freeze_snapshot(snapshot_root) as frozen:
        plans = planner.plan_builds(
            (
                planner.BuildProvider(
                    name="provider",
                    snapshot=frozen,
                    commands=(_command("tool"),),
                ),
            ),
            manager_home=manager_home,
            operator_search_path=toolchain.OperatorSearchPath(()),
            cache_backend=cache,
            establish_toolchain=establish,
            generation_probe=generation,
            max_generation_attempts=2,
        )

    assert [plan.result for plan in plans] == ["would-preflight-and-build"]
    assert generation.calls == 4
    assert events.count("toolchain") == 2
    assert events.count("cache:tool") == 2


def test_plan_builds_reports_concurrent_state_change_after_retry_limit(
    tmp_path: Path,
) -> None:
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    (snapshot_root / "source.txt").write_text("source", encoding="utf-8")
    manager_home = tmp_path / "manager"
    manager_home.mkdir()
    events: list[str] = []
    cache = _FakeCache(
        manager_home,
        {"tool": CacheEntryStatus.MISS},
        events,
    )
    generation = _SequenceGeneration(
        [
            {"shared": "sha256:" + "0" * 64},
            {"shared": "sha256:" + "1" * 64},
            {"shared": "sha256:" + "2" * 64},
            {"shared": "sha256:" + "3" * 64},
        ]
    )

    with source.freeze_snapshot(snapshot_root) as frozen:
        with pytest.raises(planner.BuildPlanningError) as error:
            planner.plan_builds(
                (
                    planner.BuildProvider(
                        name="provider",
                        snapshot=frozen,
                        commands=(_command("tool"),),
                    ),
                ),
                manager_home=manager_home,
                operator_search_path=toolchain.OperatorSearchPath(()),
                cache_backend=cache,
                establish_toolchain=lambda _config: _FakeToolchainSession(events),
                generation_probe=generation,
                max_generation_attempts=2,
            )

    assert error.value.code == "concurrent_state_change"
    assert generation.calls == 4
    assert events.count("cache:tool") == 2


def test_plan_builds_with_no_commands_never_probes_or_inspects(
    tmp_path: Path,
) -> None:
    manager_home = tmp_path / "manager"
    manager_home.mkdir()

    def unexpected_toolchain(_config: toolchain.ToolchainConfig) -> _FakeToolchainSession:
        raise AssertionError("no build commands must not probe Go")

    assert planner.plan_builds(
        (),
        manager_home=manager_home,
        operator_search_path=toolchain.OperatorSearchPath(()),
        establish_toolchain=unexpected_toolchain,
    ) == ()


def test_filesystem_generation_probe_is_deterministic_and_read_only(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    payload = shared / "state.json"
    payload.write_text('{"generation":1}\n', encoding="utf-8")
    probe = planner.FilesystemGenerationProbe((shared, tmp_path / "missing"))

    before = probe.capture()
    after = probe.capture()

    assert before == after
    assert list(before) == sorted(before, key=lambda value: value.encode("utf-8"))
    assert not (tmp_path / "missing").exists()
    payload.write_text('{"generation":2}\n', encoding="utf-8")
    assert probe.capture() != before
