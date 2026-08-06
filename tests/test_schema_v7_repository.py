from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from csk import build_repository, dev_substitutions, skillspec


RC5_MANIFEST_SHA256 = "b6f56aacc0e37dcc6692f73f641bff761e89b645adfe20a47a06d81c6fda204c"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _prepare_snapshot(snapshot: Path) -> None:
    for directory in ("scripts", "build/cmd/tool", "build/cmd/helper", "runtime"):
        (snapshot / directory).mkdir(parents=True, exist_ok=True)
    (snapshot / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (snapshot / "scripts" / "tool.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (snapshot / "build" / "go.mod").write_text("module example.com/tool\n", encoding="utf-8")


def _repository_manifest(**command_overrides: object) -> dict[str, object]:
    command: dict[str, object] = {
        "type": "build",
        "driver": "go-repository-v1",
        "repository": "golden-tools",
        "target": "golden-tool",
    }
    command.update(command_overrides)
    return {
        "schema_version": 7,
        "capabilities": {},
        "build_repositories": {
            "golden-tools": {
                "git": "https://GIT.example.com/Skills/golden-tools.git",
                "locked_commit": {"object_format": "sha1", "hex": "a" * 40},
                "tag": "v1.4.0",
            }
        },
        "commands": {"golden-tool": command},
    }


def test_rc5_contract_pin() -> None:
    assert build_repository.PROTOCOL_VERSION == "1.0.0-rc.5"
    assert build_repository.CONFORMANCE_MANIFEST_SHA256 == RC5_MANIFEST_SHA256


def test_schema_v7_parses_declared_repository_and_command(tmp_path: Path) -> None:
    _write_json(tmp_path / "agent-skill.json", _repository_manifest())

    spec = skillspec.load_skill_spec(tmp_path)

    repository = spec.build_repositories["golden-tools"]
    assert repository.identity == "git.example.com/Skills/golden-tools"
    assert repository.transport == "https"
    assert repository.locked_commit == build_repository.LockedCommit("sha1", "a" * 40)
    assert repository.tag == "v1.4.0"
    assert spec.commands["golden-tool"].repository == "golden-tools"
    assert spec.commands["golden-tool"].target == "golden-tool"


@pytest.mark.parametrize(
    "controlled_field",
    ["argv", "env", "output", "name", "credentials", "signing", "hooks", "plugins", "generator", "fallback"],
)
def test_schema_v7_rejects_package_controlled_command_fields(
    tmp_path: Path, controlled_field: str
) -> None:
    _write_json(tmp_path / "agent-skill.json", _repository_manifest(**{controlled_field: []}))

    with pytest.raises(skillspec.SkillSpecError, match=controlled_field):
        skillspec.load_skill_spec(tmp_path)


def test_schema_v7_requires_declared_and_selected_repositories(tmp_path: Path) -> None:
    missing = _repository_manifest(repository="missing")
    _write_json(tmp_path / "agent-skill.json", missing)
    with pytest.raises(skillspec.SkillSpecError, match="undeclared"):
        skillspec.load_skill_spec(tmp_path)

    unselected = _repository_manifest()
    unselected["commands"] = {}
    _write_json(tmp_path / "agent-skill.json", unselected)
    with pytest.raises(skillspec.SkillSpecError, match="not selected"):
        skillspec.load_skill_spec(tmp_path)


def test_schema_v7_preserves_local_go_v1(tmp_path: Path) -> None:
    _prepare_snapshot(tmp_path)
    payload = _repository_manifest()
    payload["build_roots"] = ["build"]
    commands = payload["commands"]
    assert isinstance(commands, dict)
    commands["local"] = {"type": "build", "driver": "go-v1", "source_dir": "build/cmd/tool"}
    _write_json(tmp_path / "agent-skill.json", payload)

    spec = skillspec.load_skill_spec(tmp_path)

    assert spec.commands["local"].driver == "go-v1"
    assert spec.commands["local"].source_dir == "build/cmd/tool"


@pytest.mark.parametrize(
    ("raw", "identity", "transport"),
    [
        ("git@git.example.com:skills/a.git", "git.example.com/skills/a", "ssh"),
        ("https://GIT.example.com/Skills/A.git", "git.example.com/Skills/A", "https"),
        ("ssh://git@git.example.com/skills/a", "git.example.com/skills/a", "ssh"),
        ("https://git.example.com/文書/工具.git", "git.example.com/文書/工具", "https"),
    ],
)
def test_repository_source_canonicalization(raw: str, identity: str, transport: str) -> None:
    parsed = build_repository.parse_repository_source(raw)
    assert parsed.identity == identity
    assert parsed.transport == transport


@pytest.mark.parametrize(
    "raw",
    [
        "file:///tmp/a",
        "http://git.example.com/a",
        "https://user@git.example.com/a",
        "https://git.example.com:8443/a",
        "https://git.example.com/a%2Fb",
        "https://git.example.com/a/../b",
        "git@git.example.com:a b",
        "ssh://git@git.example.com/文書/a",
        "https://git.example.com/a\u00a0b",
        "https://git.example.com/a\u0085b",
    ],
)
def test_repository_source_rejects_non_rc5_forms(raw: str) -> None:
    with pytest.raises(build_repository.BuildRepositoryError):
        build_repository.parse_repository_source(raw)


def test_ref_grammar_uses_released_byte_limit() -> None:
    assert build_repository.is_valid_ref_name("界" * 85)
    assert not build_repository.is_valid_ref_name("界" * 86)
    for invalid in ("@", ".hidden", "a..b", "a@{b", "a.lock", "a/b.lock", "a b", "a~b"):
        assert not build_repository.is_valid_ref_name(invalid)


def test_skill_build_targets_are_closed_and_contained() -> None:
    descriptor = build_repository.parse_skill_build(
        json.dumps(
            {
                "schema_version": 1,
                "targets": {
                    "root": {"driver": "go-repository-v1", "build_root": ".", "source_dir": "cmd/tool"},
                    "root-package": {
                        "driver": "go-repository-v1",
                        "build_root": ".",
                        "source_dir": ".",
                    },
                    "nested": {
                        "driver": "go-repository-v1",
                        "build_root": "tools/admin",
                        "source_dir": "tools/admin/cmd/admin",
                    },
                },
            }
        )
    )
    assert set(descriptor.targets) == {"nested", "root", "root-package"}

    for extra in ("argv", "environment", "output", "name", "hook", "plugin", "signing"):
        target = {"driver": "go-repository-v1", "build_root": ".", "source_dir": ".", extra: []}
        with pytest.raises(build_repository.BuildRepositoryError, match=extra):
            build_repository.parse_skill_build(json.dumps({"schema_version": 1, "targets": {"tool": target}}))

    with pytest.raises(build_repository.BuildRepositoryError, match="contained"):
        build_repository.parse_skill_build(
            json.dumps(
                {
                    "schema_version": 1,
                    "targets": {
                        "tool": {
                            "driver": "go-repository-v1",
                            "build_root": "tools/admin",
                            "source_dir": "cmd/tool",
                        }
                    },
                }
            )
        )


def test_schema2_build_repository_substitution_selection_and_identity(tmp_path: Path) -> None:
    manifest = dev_substitutions.parse_manifest(
        json.dumps(
            {
                "schema_version": 2,
                "substitutions": {},
                "build_repository_substitutions": {
                    "golden-skill": {
                        "local": {"path": "tools/tmp/../golden"},
                        "network": {
                            "git": "ssh://git@git.example.com/tools/golden.git",
                            "ref": {"kind": "branch", "value": "release/v2"},
                        },
                    }
                },
            }
        ),
        tmp_path,
    )

    local = manifest.build_repository_substitution("golden-skill", "local")
    assert local is not None
    assert local.selector == "tools/golden"
    assert local.path == tmp_path / "tools" / "golden"
    assert local.effective_identity("project-123") == (
        "sha256:4c006e6f2d8c9ede6e6d5bc3bce3edea9780e2dfd3b442ef358f51a77b921969"
    )
    network = manifest.build_repository_substitution("golden-skill", "network")
    assert network is not None
    assert network.effective_identity("unused") == "git.example.com/tools/golden"
    assert manifest.build_repository_substitution("other", "network") is None


@pytest.mark.parametrize("field", ["command", "driver", "target", "output", "credentials", "signing", "hooks"])
def test_schema2_substitution_rejects_package_owned_fields(tmp_path: Path, field: str) -> None:
    payload = {
        "schema_version": 2,
        "substitutions": {},
        "build_repository_substitutions": {
            "skill": {"repo": {"path": "../repo", field: "owned"}}
        },
    }
    with pytest.raises(dev_substitutions.DevSubstitutionError, match=field):
        dev_substitutions.parse_manifest(json.dumps(payload), tmp_path)


def test_schema2_bounds_do_not_change_schema1(tmp_path: Path) -> None:
    oversized_path = "界" * 8193
    oversized_git = "界" * 8193
    oversized_ref = "界" * 1025
    schema1 = dev_substitutions.parse_manifest(
        json.dumps(
            {
                "substitutions": {
                    "local": {"path": oversized_path},
                    "network": {
                        "git": oversized_git,
                        "ref": {"kind": "branch", "value": oversized_ref},
                    },
                }
            }
        ),
        tmp_path,
    )
    assert schema1.substitutions["local"].path == tmp_path / oversized_path
    assert schema1.substitutions["network"].git == oversized_git
    assert schema1.substitutions["network"].ref_value == oversized_ref

    boundary = dev_substitutions.parse_manifest(
        json.dumps(
            {
                "schema_version": 2,
                "substitutions": {
                    "local": {"path": "界" * 8192},
                    "network": {
                        "git": "界" * 8192,
                        "ref": {"kind": "branch", "value": "界" * 1024},
                    },
                },
            }
        ),
        tmp_path,
    )
    boundary_path = boundary.substitutions["local"].path
    assert boundary_path is not None
    assert len(os.fspath(boundary_path)) >= 8192
    assert len(boundary.substitutions["network"].git or "") == 8192
    assert len(boundary.substitutions["network"].ref_value or "") == 1024

    for entry in (
        {"path": oversized_path},
        {"git": oversized_git, "ref": {"kind": "branch", "value": "main"}},
        {"git": "repo", "ref": {"kind": "branch", "value": oversized_ref}},
    ):
        with pytest.raises(dev_substitutions.DevSubstitutionError):
            dev_substitutions.parse_manifest(
                json.dumps({"schema_version": 2, "substitutions": {"skill": entry}}), tmp_path
            )


def test_local_selector_normalization_is_idempotent() -> None:
    seeds = [".", "tools/./golden", "tools/tmp/../golden", "../../tools", "a/../../b", "文書/工具"]
    for prefix in ("a", "tools", "文書"):
        for depth in range(1, 20):
            seeds.append("/".join([prefix, *("tmp" for _ in range(depth)), *(".." for _ in range(depth))]))
    for selector in seeds:
        normalized = build_repository.normalize_local_selector(selector)
        assert build_repository.normalize_local_selector(normalized) == normalized
        assert "//" not in normalized


def test_released_rc5_schema_cases(tmp_path: Path) -> None:
    root_text = os.environ.get("CURATOR_SCHEMA_V7_ROOT")
    if root_text is None:
        pytest.skip("CURATOR_SCHEMA_V7_ROOT is not set")
    root = Path(root_text)
    manifest = root / "manifest.json"
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == RC5_MANIFEST_SHA256

    case_count = 0
    for suite, manifest_name in (("agent-skill-v7", "agent-skill.json"), ("csk-skill-v7", "csk-skill.json")):
        for case_path in sorted((root / "schema-cases" / suite).glob("*.json")):
            case_count += 1
            snapshot = tmp_path / suite / case_path.stem
            _prepare_snapshot(snapshot)
            (snapshot / manifest_name).write_bytes(case_path.read_bytes())
            if case_path.name.startswith("valid"):
                skillspec.load_skill_spec(snapshot)
            else:
                with pytest.raises(skillspec.SkillSpecError):
                    skillspec.load_skill_spec(snapshot)

    for case_path in sorted((root / "schema-cases" / "skill-build-v1").glob("*.json")):
        case_count += 1
        if case_path.name.startswith("valid"):
            build_repository.parse_skill_build(case_path.read_bytes())
        else:
            with pytest.raises(build_repository.BuildRepositoryError):
                build_repository.parse_skill_build(case_path.read_bytes())

    for case_path in sorted((root / "schema-cases" / "skillfile-dev-v2").glob("*.json")):
        case_count += 1
        if case_path.name.startswith("valid"):
            dev_substitutions.parse_manifest(case_path.read_bytes(), tmp_path)
        else:
            with pytest.raises(dev_substitutions.DevSubstitutionError):
                dev_substitutions.parse_manifest(case_path.read_bytes(), tmp_path)
    assert case_count == 95


def test_released_schemas_1_through_6_do_not_accept_v7_fields(tmp_path: Path) -> None:
    root_text = os.environ.get("CURATOR_SCHEMA_V7_ROOT")
    if root_text is None:
        pytest.skip("CURATOR_SCHEMA_V7_ROOT is not set")
    cases_root = Path(root_text) / "schema-cases"
    case_count = 0
    for version in range(1, 7):
        for prefix, manifest_name in (("agent-skill", "agent-skill.json"), ("csk-skill", "csk-skill.json")):
            suite = cases_root / f"{prefix}-v{version}"
            selected = [suite / "valid.json", *sorted(suite.glob("invalid-v7-*.json"))]
            for case_path in selected:
                case_count += 1
                snapshot = tmp_path / f"{prefix}-v{version}" / case_path.stem
                _prepare_snapshot(snapshot)
                (snapshot / manifest_name).write_bytes(case_path.read_bytes())
                if case_path.name == "valid.json":
                    skillspec.load_skill_spec(snapshot)
                else:
                    with pytest.raises(skillspec.SkillSpecError):
                        skillspec.load_skill_spec(snapshot)
    assert case_count == 96


def test_released_source_identity_vectors() -> None:
    root_text = os.environ.get("CURATOR_SCHEMA_V7_ROOT")
    if root_text is None:
        pytest.skip("CURATOR_SCHEMA_V7_ROOT is not set")
    vectors = json.loads((Path(root_text) / "vectors" / "source-identities.json").read_bytes())
    for vector in vectors:
        identity = vector.get("identity")
        if identity is None:
            with pytest.raises(build_repository.BuildRepositoryError):
                build_repository.parse_repository_source(vector["input"])
        else:
            assert build_repository.parse_repository_source(vector["input"]).identity == identity
