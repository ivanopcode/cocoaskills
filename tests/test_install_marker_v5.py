"""Install marker schema 5 and schema-2 currentness.

Schema-2 installations write marker 5 regardless of skill manifest version:
``package`` replaces the legacy ``source``/``git``/``ref_kind``/``ref``/
``commit`` identity and ``lock_sha256`` binds the installed selection. Status
compares package, lock, attestation and substitution against the effective
plan in addition to every retained comparison, and required registry
evidence that is missing, unreadable, malformed, stale or mismatching is
never an unattested success and never current.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from csk import (
    closure,
    dev_substitutions,
    gc,
    git_ops,
    global_install,
    install_marker,
    installer,
    manifest,
    skillspec,
)
from csk.build_repository_pipeline import EffectiveState, snapshot_key
from csk.builds.source import BuildSourceIdentity
from csk.sources import package_identity as source_package

SHA1 = "0123456789abcdef0123456789abcdef01234567"
SHA1_OTHER = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
REPO = "github.com/example/golden-skills"
REPO_OTHER = "github.com/example/other-skills"
LOCK = "sha256:" + "0" * 64
LOCK_OTHER = "sha256:" + "1" * 64
CONTEXT = "sha256:" + "c" * 64
CONTEXT_OTHER = "sha256:" + "d" * 64
CONTENT = "sha256:" + "a" * 64
KEY = "0123456789abcdef"
KEY_OTHER = "fedcba9876543210"


def _commit(hex_value: str = SHA1) -> source_package.LockedCommit:
    return source_package.LockedCommit(object_format="sha1", hex=hex_value)


def _local() -> source_package.LocalSnapshot:
    return source_package.LocalSnapshot(snapshot="sha256:" + "1" * 64)


def _network(
    repository: str = REPO, hex_value: str = SHA1
) -> source_package.NetworkGit:
    return source_package.NetworkGit(repository=repository, commit=_commit(hex_value))


def _configured(
    source: str = "golden-skill", hex_value: str = SHA1
) -> source_package.ConfiguredGit:
    return source_package.ConfiguredGit(source=source, commit=_commit(hex_value))


def _build_source() -> BuildSourceIdentity:
    return BuildSourceIdentity(
        algorithm="curator-build-source-v1",
        content_sha256="sha256:" + "b" * 64,
    )


def _local_record_fields(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "driver": "go-v1",
        "receipt_schema_version": 3,
        "execution_policy": "manager-worker-v1",
        "cache_key": "sha256:" + "1" * 64,
        "receipt_sha256": "sha256:" + "e" * 64,
        "artifact_sha256": "sha256:" + "d" * 64,
        "artifact_path": "bin/local-helper",
    }
    values.update(changes)
    return values


def _local_record() -> install_marker.InstallMarkerBuildV5:
    return install_marker.InstallMarkerBuildV5(**_local_record_fields())


def _external_record_fields(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "driver": "go-repository-v1",
        "receipt_schema_version": 3,
        "execution_policy": "manager-worker-v1",
        "cache_key": "sha256:" + "4" * 64,
        "receipt_sha256": "sha256:" + "0" * 64,
        "artifact_sha256": "sha256:" + "6" * 64,
        "artifact_path": "bin/golden-tool",
        "repository": "golden-tools",
        "declared_identity": install_marker.MarkerRepositoryIdentity(
            kind="network-git", value="github.com/example/golden-tools"
        ),
        "declared_locked_commit": install_marker.MarkerRepositoryCommit(
            object_format="sha1", hex=SHA1
        ),
        "declared_tag": "v1.4.0",
        "effective_identity": install_marker.MarkerRepositoryIdentity(
            kind="network-git", value="github.com/example/golden-tools"
        ),
        "object_format": "sha1",
        "commit": SHA1,
        "substituted": False,
        "substitution": None,
        "build_source": _build_source(),
        "descriptor_target": "golden-tool",
    }
    values.update(changes)
    return values


def _external_record(
    **changes: Any,
) -> install_marker.InstallMarkerBuildV5:
    return install_marker.InstallMarkerBuildV5(**_external_record_fields(**changes))


def _attestation(
    registry: str = "trusted", status: str = "audited", key_id: str | None = KEY
) -> install_marker.MarkerAttestation:
    return install_marker.MarkerAttestation(
        registry=registry, status=status, key_id=key_id
    )


def _base_marker(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "golden-skill",
        "package": _network(),
        "lock_sha256": LOCK,
        "content_sha256": CONTENT,
        "locale": None,
        "agents": ("codex_cli",),
        "commands": ("golden-tool", "local-helper"),
        "dependencies": (),
        "skill_schema_version": 8,
        "runtime_roots": (),
        "build_roots": ("build",),
        "installed_at": "2000-01-01T00:00:00Z",
        "files": ("SKILL.md",),
        "builds": {
            "golden-tool": _external_record(),
            "local-helper": _local_record(),
        },
        "build_source": _build_source(),
        "attestation": _attestation(),
    }
    payload.update(changes)
    return payload


def _base_plan(**changes: Any) -> install_marker.MarkerPlan:
    values: dict[str, Any] = {
        "name": "golden-skill",
        "package": _network(),
        "lock_sha256": LOCK,
        "context_sha256": CONTEXT,
        "content_sha256": CONTENT,
        "locale": None,
        "agents": ("codex_cli",),
        "commands": ("golden-tool", "local-helper"),
        "dependencies": (),
        "skill_schema_version": 8,
        "runtime_roots": (),
        "build_roots": ("build",),
        "files": ("SKILL.md",),
        "builds": {
            "golden-tool": _external_record(),
            "local-helper": _local_record(),
        },
        "requirements": None,
        "mcp_servers": None,
        "activation": None,
        "requirers": None,
        "attestation": _attestation(),
        "substituted": None,
        "build_source": _build_source(),
    }
    values.update(changes)
    return install_marker.MarkerPlan(**values)


def _write_marker(directory: Path, payload: dict[str, Any]) -> Path:
    path = directory / ".csk-install.json"
    path.write_bytes(install_marker.serialize_install_marker(payload))
    return path


def _tree_hash(root: Path) -> str:
    """Hash a directory tree portably: relative posix paths plus raw bytes."""

    digest = hashlib.sha256()
    if not root.exists():
        return "missing:" + digest.hexdigest()
    entries = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    for entry in entries:
        relative = entry.relative_to(root).as_posix().encode("utf-8")
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"L" + relative + os.readlink(entry).encode("utf-8"))
        elif stat.S_ISDIR(info.st_mode):
            digest.update(b"D" + relative)
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"F" + relative + entry.read_bytes())
        else:
            digest.update(b"S" + relative)
    return digest.hexdigest()


# --- Migration table: v4 fields to the v5 representation --------------------


def test_marker_v5_round_trips_through_bytes() -> None:
    marker = install_marker.InstallMarkerV5(**_base_marker())

    raw = install_marker.serialize_install_marker(marker.to_json())
    parsed = install_marker.read_install_marker(raw)

    assert isinstance(parsed, install_marker.InstallMarkerV5)
    assert parsed.to_json() == marker.to_json()
    assert install_marker.serialize_install_marker(parsed.to_json()) == raw


@pytest.mark.parametrize(
    "legacy_member", ["source", "git", "ref_kind", "ref", "commit"]
)
def test_marker_v5_refuses_every_legacy_identity_member(legacy_member: str) -> None:
    payload = install_marker.InstallMarkerV5(**_base_marker()).to_json()
    payload[legacy_member] = "legacy-identity"

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.parse_install_marker(payload)

    assert raised.value.code == "install_marker_invalid"


@pytest.mark.parametrize("manifest_version", [1, 2, 3, 4, 5, 6, 7, 8])
def test_marker_v5_covers_every_manifest_version(manifest_version: int) -> None:
    """Marker 5 crosses manifest versions 1-8 instead of moving with them."""

    marker = install_marker.InstallMarkerV5(
        **_base_marker(skill_schema_version=manifest_version)
    )

    assert marker.to_json()["schema_version"] == 5
    assert marker.to_json()["skill_schema_version"] == manifest_version
    assert install_marker.marker_can_be_current(
        marker, skill_schema_version=manifest_version
    ) is True
    parsed = install_marker.read_install_marker(
        install_marker.serialize_install_marker(marker.to_json())
    )
    assert isinstance(parsed, install_marker.InstallMarkerV5)


@pytest.mark.parametrize("manifest_version", [0, 9, -1, 99])
def test_marker_v5_rejects_manifest_versions_outside_one_to_eight(
    manifest_version: int,
) -> None:
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.InstallMarkerV5(
            **_base_marker(skill_schema_version=manifest_version)
        )

    assert raised.value.code == "install_marker_invalid"


@pytest.mark.parametrize(
    "required_member",
    [
        "name",
        "package",
        "lock_sha256",
        "content_sha256",
        "locale",
        "agents",
        "commands",
        "dependencies",
        "skill_schema_version",
        "runtime_roots",
        "build_roots",
        "installed_at",
        "files",
        "builds",
    ],
)
def test_marker_v5_requires_every_retained_and_new_member(required_member: str) -> None:
    payload = install_marker.InstallMarkerV5(**_base_marker()).to_json()
    del payload[required_member]

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.parse_install_marker(payload)

    assert raised.value.code == "install_marker_invalid"


def test_marker_v5_accepts_absent_conditional_members() -> None:
    marker = install_marker.InstallMarkerV5(
        **_base_marker(
            package=_local(),
            builds={},
            build_source=None,
            attestation=None,
            requirements=None,
            mcp_servers=None,
            activation=None,
            requirers=None,
            substituted=None,
        )
    )

    decoded = marker.to_json()

    for member in (
        "build_source",
        "requirements",
        "mcp_servers",
        "attestation",
        "activation",
        "requirers",
        "substituted",
    ):
        assert member not in decoded
    assert install_marker.read_install_marker(
        install_marker.serialize_install_marker(decoded)
    ).to_json() == decoded


def test_marker_v5_accepts_every_conditional_member() -> None:
    marker = install_marker.InstallMarkerV5(
        **_base_marker(
            requirements=("helper",),
            mcp_servers={"docs": ("codex_cli",)},
            activation=install_marker.MarkerActivation(
                context=True, commands=("golden-tool",)
            ),
            requirers=("<project>",),
            substituted="operator-development",
        )
    )

    decoded = marker.to_json()

    assert decoded["requirements"] == ["helper"]
    assert decoded["mcp_servers"] == {"docs": ["codex_cli"]}
    assert decoded["activation"] == {"context": True, "commands": ["golden-tool"]}
    assert decoded["requirers"] == ["<project>"]
    assert decoded["substituted"] == "operator-development"
    assert install_marker.read_install_marker(
        install_marker.serialize_install_marker(decoded)
    ).to_json() == decoded


@pytest.mark.parametrize(
    "package_factory",
    [_network, lambda: _configured()],
    ids=["network-git", "configured-git"],
)
def test_marker_v5_admits_attestation_for_git_packages(package_factory: Any) -> None:
    marker = install_marker.InstallMarkerV5(
        **_base_marker(package=package_factory(), builds={}, build_source=None)
    )

    assert marker.to_json()["attestation"] == {
        "registry": "trusted",
        "status": "audited",
        "key_id": KEY,
    }


def test_marker_v5_forbids_attestation_for_local_snapshot() -> None:
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.InstallMarkerV5(
            **_base_marker(package=_local(), builds={}, build_source=None)
        )

    assert raised.value.code == "install_marker_invalid"

    payload = install_marker.InstallMarkerV5(
        **_base_marker(package=_local(), builds={}, build_source=None, attestation=None)
    ).to_json()
    payload["attestation"] = {"registry": "trusted", "status": "audited"}
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.parse_install_marker(payload)


def test_marker_v5_forbids_substituted_for_local_snapshot() -> None:
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.InstallMarkerV5(
            **_base_marker(
                package=_local(),
                builds={},
                build_source=None,
                attestation=None,
                substituted="operator-development",
            )
        )

    assert raised.value.code == "install_marker_invalid"

    payload = install_marker.InstallMarkerV5(
        **_base_marker(package=_local(), builds={}, build_source=None, attestation=None)
    ).to_json()
    payload["substituted"] = "operator-development"
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.parse_install_marker(payload)


def test_marker_v5_freezes_canonical_set_ordering() -> None:
    marker = install_marker.InstallMarkerV5(
        **_base_marker(
            agents=("zeta", "alpha"),
            commands=("local-helper", "golden-tool"),
            dependencies=("b-dep", "a-dep"),
            runtime_roots=("runtime/b", "runtime/a"),
            build_roots=("build/b", "build/a"),
            files=("b.md", "a.md"),
            requirements=("r2", "r1"),
            requirers=("z", "a"),
            mcp_servers={"zebra": ("c2", "c1"), "apple": ("c1",)},
        )
    )

    decoded = marker.to_json()

    assert decoded["agents"] == ["alpha", "zeta"]
    assert decoded["commands"] == ["golden-tool", "local-helper"]
    assert decoded["dependencies"] == ["a-dep", "b-dep"]
    assert decoded["runtime_roots"] == ["runtime/a", "runtime/b"]
    assert decoded["build_roots"] == ["build/a", "build/b"]
    assert decoded["files"] == ["a.md", "b.md"]
    assert decoded["requirements"] == ["r1", "r2"]
    assert decoded["requirers"] == ["a", "z"]
    assert list(decoded["mcp_servers"]) == ["apple", "zebra"]
    assert decoded["mcp_servers"]["zebra"] == ["c1", "c2"]
    assert list(decoded["builds"]) == ["golden-tool", "local-helper"]


# --- Builds bind receipt version 3 ------------------------------------------


def test_marker_v5_build_records_require_receipt_version_three() -> None:
    assert _local_record().to_json()["receipt_schema_version"] == 3
    assert _external_record().to_json()["receipt_schema_version"] == 3


@pytest.mark.parametrize("receipt_version", [1, 2, 4, 0])
def test_marker_v5_refuses_legacy_receipt_versions(receipt_version: int) -> None:
    local_fields = _local_record_fields(receipt_schema_version=receipt_version)
    external_fields = _external_record_fields(receipt_schema_version=receipt_version)

    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.InstallMarkerBuildV5(**local_fields)
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.InstallMarkerBuildV5(**external_fields)


@pytest.mark.parametrize(
    "required_member",
    [
        "repository",
        "declared_identity",
        "declared_locked_commit",
        "effective_identity",
        "object_format",
        "commit",
        "substituted",
        "build_source",
        "descriptor_target",
        "cache_key",
        "receipt_sha256",
        "artifact_sha256",
        "artifact_path",
    ],
)
def test_marker_v5_refuses_external_records_missing_a_required_field(
    required_member: str,
) -> None:
    payload = install_marker.InstallMarkerV5(**_base_marker()).to_json()
    del payload["builds"]["golden-tool"][required_member]

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.parse_install_marker(payload)

    assert raised.value.code == "install_marker_invalid"


def test_marker_v5_keeps_external_cross_field_rules() -> None:
    with pytest.raises(install_marker.InstallMarkerError):
        _external_record(substituted=True, substitution=None)
    with pytest.raises(install_marker.InstallMarkerError):
        _external_record(
            substituted=False,
            substitution=install_marker.MarkerRepositorySubstitution(
                type="network-git",
                ref=install_marker.MarkerRepositoryRef(kind="tag", value="v1"),
            ),
        )
    with pytest.raises(install_marker.InstallMarkerError):
        _external_record(object_format="sha1", commit="a" * 64)


def test_marker_v5_external_declared_tag_requires_valid_git_ref_name() -> None:
    """A present receipt tag must satisfy the shared Git ref-name grammar."""

    assert _external_record(declared_tag="release.candidate").declared_tag == (
        "release.candidate"
    )
    with pytest.raises(install_marker.InstallMarkerError) as refused:
        _external_record(declared_tag="release..candidate")

    assert refused.value.code == "install_marker_invalid"
    assert "valid Git ref name" in refused.value.detail


def test_marker_v5_reader_accepts_a_build_source_without_recorded_builds() -> None:
    """The pinned corpus carries build_source with empty builds (valid.json).

    Presence of the top-level build source is a plan comparison, not a reader
    gate: the reader validates its shape only.
    """

    marker = install_marker.InstallMarkerV5(
        **_base_marker(
            package=_local(), builds={}, attestation=None, build_source=_build_source()
        )
    )

    assert marker.to_json()["build_source"]["algorithm"] == "curator-build-source-v1"
    assert marker.to_json()["builds"] == {}


# --- Legacy lanes stay readable; unknown versions fail closed ----------------


@pytest.mark.parametrize(
    "unknown_version", [0, 6, 7, 99, -1, "5", 5.0, True, None]
)
def test_unknown_marker_versions_fail_closed(unknown_version: Any) -> None:
    payload = install_marker.InstallMarkerV5(**_base_marker()).to_json()
    payload["schema_version"] = unknown_version

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.parse_install_marker(payload)

    assert raised.value.code == "unsupported_install_marker_schema"


def test_legacy_markers_stay_readable_on_legacy_lanes() -> None:
    legacy = install_marker.InstallMarkerV4(
        name="golden-skill",
        source="golden-skill",
        ref_kind="revision",
        ref=SHA1,
        commit=SHA1,
        content_sha256=CONTENT,
        locale=None,
        agents=(),
        commands=(),
        dependencies=(),
        skill_schema_version=8,
        runtime_roots=(),
        installed_at="2000-01-01T00:00:00Z",
        files=(),
        build_roots=(),
        builds={},
    )

    parsed = install_marker.read_install_marker(
        install_marker.serialize_install_marker(legacy.to_json())
    )

    assert isinstance(parsed, install_marker.InstallMarkerV4)
    assert install_marker.marker_can_be_current(parsed, skill_schema_version=8) is True


def test_legacy_markers_never_attest_schema2_currency(tmp_path: Path) -> None:
    legacy = install_marker.InstallMarkerV4(
        name="golden-skill",
        source="golden-skill",
        ref_kind="revision",
        ref=SHA1,
        commit=SHA1,
        content_sha256=CONTENT,
        locale=None,
        agents=(),
        commands=(),
        dependencies=(),
        skill_schema_version=8,
        runtime_roots=(),
        installed_at="2000-01-01T00:00:00Z",
        files=(),
        build_roots=(),
        builds={},
    )
    marker_path = _write_marker(tmp_path, legacy.to_json())

    verdict = install_marker.evaluate_marker_status(marker_path, _base_plan())

    assert verdict.current is False
    assert verdict.exit_code == 1
    assert "legacy" in verdict.detail


# --- No endpoint, host, mirror or alias string can reach a marker -------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://github.com/example/golden-skills",
        "https://github.com:443/example/golden-skills",
        "http://github.com/example/golden-skills",
        "ssh://git@github.com/example/golden-skills.git",
        "git@github.com:example/golden-skills.git",
        "github.com:22/example/golden-skills",
        "github.com/example/golden-skills?ref=main",
        "github.com/example/golden-skills#main",
        "file:///tmp/golden-skills",
        "GITHUB.COM/example/golden-skills",
        "github.com/example/../escape",
        "github.com/example/golden skills",
        "github.com\\example\\golden-skills",
    ],
)
def test_marker_v5_refuses_endpoint_shaped_package_identities(endpoint: str) -> None:
    payload = install_marker.InstallMarkerV5(**_base_marker()).to_json()
    payload["package"] = {
        "kind": "network-git",
        "repository": endpoint,
        "commit": {"object_format": "sha1", "hex": SHA1},
        "directory": ".",
    }

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.parse_install_marker(payload)

    assert raised.value.code == "install_marker_invalid"


def _bad_packages() -> list[Any]:
    return [
        {"kind": "local-snapshot", "snapshot": "sha256:" + "z" * 64},
        {"kind": "local-snapshot"},
        {"kind": "local-snapshot", "snapshot": "sha256:" + "1" * 64, "extra": 1},
        {
            "kind": "network-git",
            "repository": REPO,
            "commit": {"object_format": "sha1", "hex": "xyz"},
            "directory": ".",
        },
        {
            "kind": "network-git",
            "repository": REPO,
            "commit": {"object_format": "sha1", "hex": SHA1},
        },
        {
            "kind": "configured-git",
            "source": "golden-skill",
            "commit": {"object_format": "sha1", "hex": SHA1_OTHER},
            "directory": "elsewhere",
        },
        {
            "kind": "configured-git",
            "source": "../escape",
            "commit": {"object_format": "sha1", "hex": SHA1},
            "directory": ".",
        },
        {"kind": "tarball", "url": "https://example.com/skill.tar.gz"},
        {"kind": "local-snapshot", "snapshot": None},
        "github.com/example/golden-skills",
        None,
        [],
    ]


@pytest.mark.parametrize("package", _bad_packages())
def test_marker_v5_refuses_malformed_package_unions(package: Any) -> None:
    payload = install_marker.InstallMarkerV5(**_base_marker()).to_json()
    payload["package"] = package

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.parse_install_marker(payload)

    assert raised.value.code == "install_marker_invalid"


def test_marker_v5_carries_no_transport_member() -> None:
    decoded = install_marker.InstallMarkerV5(**_base_marker()).to_json()

    assert set(decoded) <= install_marker._V5_MEMBERS
    for forbidden in ("endpoint", "url", "mirror", "mirror_of", "alias", "host"):
        assert forbidden not in decoded
        assert forbidden not in decoded["package"]


def test_marker_v5_writer_takes_no_endpoint_string() -> None:
    """The writer derives the package from the typed plan: no stringly path."""

    parameters = inspect.signature(install_marker.build_install_marker_v5).parameters

    assert "package" not in parameters
    assert "endpoint" not in parameters
    assert "repository" not in parameters


# --- The v5 writer derives every summary from the plan ------------------------


def test_writer_derives_package_lock_attestation_and_substitution() -> None:
    plan = _base_plan(substituted="operator-development")

    marker = install_marker.build_install_marker_v5(
        plan,
        content_sha256=CONTENT,
        files=("SKILL.md",),
        locale=None,
        agents=["codex_cli"],
        commands=["golden-tool", "local-helper"],
        dependencies=[],
        skill_schema_version=8,
        runtime_roots=[],
        build_roots=["build"],
        installed_at="2000-01-01T00:00:00Z",
        builds={
            "golden-tool": _external_record(),
            "local-helper": _local_record(),
        },
    )

    assert marker.package == plan.package
    assert marker.lock_sha256 == plan.lock_sha256
    assert marker.attestation == plan.attestation
    assert marker.substituted == "operator-development"
    assert marker.build_source == plan.build_source
    assert marker.name == plan.name
    assert install_marker.compare_marker_plan(marker, plan) == ()


@pytest.mark.parametrize("key_id", [KEY, None], ids=["key-present", "key-absent"])
def test_writer_records_key_absence_as_a_compared_value(key_id: str | None) -> None:
    plan = _base_plan(
        attestation=install_marker.MarkerAttestation(
            registry="trusted", status="audited", key_id=key_id
        ),
        agents=(),
        commands=(),
        build_roots=(),
        builds={},
    )

    marker = install_marker.build_install_marker_v5(
        plan,
        content_sha256=CONTENT,
        files=("SKILL.md",),
        locale=None,
        agents=[],
        commands=[],
        dependencies=[],
        skill_schema_version=8,
        runtime_roots=[],
        build_roots=[],
        installed_at="2000-01-01T00:00:00Z",
        builds={},
    )

    assert ("key_id" in marker.to_json().get("attestation", {})) is (key_id is not None)
    assert install_marker.compare_marker_plan(marker, plan) == ()


def test_writer_emits_no_attestation_or_substitution_for_local_plans() -> None:
    plan = install_marker.MarkerPlan(
        name="golden-skill",
        package=_local(),
        lock_sha256=LOCK,
        context_sha256=CONTEXT,
        content_sha256=CONTENT,
        locale=None,
        agents=(),
        commands=(),
        dependencies=(),
        skill_schema_version=1,
        runtime_roots=(),
        build_roots=(),
        files=("SKILL.md",),
        builds={},
        requirements=None,
        mcp_servers=None,
        activation=None,
        requirers=None,
    )

    marker = install_marker.build_install_marker_v5(
        plan,
        content_sha256=CONTENT,
        files=("SKILL.md",),
        locale=None,
        agents=[],
        commands=[],
        dependencies=[],
        skill_schema_version=1,
        runtime_roots=[],
        build_roots=[],
        installed_at="2000-01-01T00:00:00Z",
        builds={},
    )

    decoded = marker.to_json()
    assert "attestation" not in decoded
    assert "substituted" not in decoded


@pytest.mark.parametrize("manifest_version", [1, 2, 3, 4, 5, 6, 7, 8])
def test_writer_crosses_marker_five_with_every_manifest_version(
    manifest_version: int,
) -> None:
    marker = install_marker.build_install_marker_v5(
        _base_plan(),
        content_sha256=CONTENT,
        files=("SKILL.md",),
        locale=None,
        agents=[],
        commands=[],
        dependencies=[],
        skill_schema_version=manifest_version,
        runtime_roots=[],
        build_roots=[],
        installed_at="2000-01-01T00:00:00Z",
        builds={},
    )

    decoded = marker.to_json()
    assert decoded["schema_version"] == 5
    assert decoded["skill_schema_version"] == manifest_version


# --- One field table drives marker-plan comparison ----------------------------


def test_marker_plan_comparison_table_is_exact() -> None:
    assert install_marker.MARKER_PLAN_COMPARISON_FIELDS == (
        "name",
        "package",
        "lock_sha256",
        "content_sha256",
        "locale",
        "agents",
        "commands",
        "dependencies",
        "skill_schema_version",
        "runtime_roots",
        "build_roots",
        "files",
        "builds",
        "build_source",
        "requirements",
        "mcp_servers",
        "registry",
        "status",
        "key_id",
        "activation",
        "requirers",
        "substituted",
    )


def test_marker_plan_comparison_covers_every_record_member() -> None:
    """Growth test: the comparison derives from the record's own members.

    A member added to InstallMarkerV5 without a plan expectation fails here:
    the row table pins the attestation expansion over record-minus-exclusions,
    and the plan member set pins record-minus-exclusions plus the
    evidence-bound context hash. The only members status never compares are
    the constant schema version and the install timestamp.
    """

    record_members = {
        field.name for field in dataclasses.fields(install_marker.InstallMarkerV5)
    }
    assert set(install_marker.MARKER_PLAN_COMPARISON_FIELDS) == (
        record_members - {"schema_version", "installed_at", "attestation"}
    ) | {"registry", "status", "key_id"}
    plan_members = {
        field.name for field in dataclasses.fields(install_marker.MarkerPlan)
    }
    assert plan_members == (record_members - {"schema_version", "installed_at"}) | {
        "context_sha256"
    }


_MISMATCH_SIX = (
    "package",
    "lock_sha256",
    "registry",
    "status",
    "key_id",
    "substituted",
)


def _marker_kwargs_for_mismatch(field: str) -> dict[str, Any]:
    if field == "package":
        return {"package": _network(repository=REPO_OTHER)}
    if field == "lock_sha256":
        return {"lock_sha256": LOCK_OTHER}
    if field == "registry":
        return {"attestation": _attestation(registry="elsewhere")}
    if field == "status":
        return {"attestation": _attestation(status="deprecated")}
    if field == "key_id":
        return {"attestation": _attestation(key_id=KEY_OTHER)}
    if field == "substituted":
        return {"substituted": "another-operator"}
    if field == "build_source":
        return {"build_source": None}
    if field == "name":
        return {"name": "other-skill"}
    if field == "content_sha256":
        return {"content_sha256": "sha256:" + "f" * 64}
    if field == "locale":
        return {"locale": "en-US"}
    if field == "agents":
        return {"agents": ("codex_cli", "other_agent")}
    if field == "commands":
        return {"commands": ("golden-tool", "local-helper", "extra-command")}
    if field == "dependencies":
        return {"dependencies": ("other-skill",)}
    if field == "skill_schema_version":
        return {"skill_schema_version": 7}
    if field == "runtime_roots":
        return {"runtime_roots": ("runtime",)}
    if field == "build_roots":
        return {"build_roots": ("build", "extra-build")}
    if field == "files":
        return {"files": ("SKILL.md", "EXTRA.md")}
    if field == "builds":
        return {"builds": {"golden-tool": _external_record()}}
    if field == "requirements":
        return {"requirements": ("safety",)}
    if field == "mcp_servers":
        return {"mcp_servers": {"search": ("codex_cli",)}}
    if field == "activation":
        return {
            "activation": install_marker.MarkerActivation(
                context=True, commands=("golden-tool",)
            )
        }
    if field == "requirers":
        return {"requirers": ("other-skill",)}
    raise AssertionError(f"no mismatch builder for {field!r}")


@pytest.mark.parametrize("field", list(_MISMATCH_SIX))
def test_marker_plan_mismatch_flags_exactly_its_field(field: str, tmp_path: Path) -> None:
    """The six mismatch cases: one table, one comparison, nonzero status."""

    plan = _base_plan()
    marker = install_marker.InstallMarkerV5(
        **_base_marker(**_marker_kwargs_for_mismatch(field))
    )

    assert install_marker.compare_marker_plan(marker, plan) == (field,)
    assert install_marker.marker_plan_is_current(marker, plan) is False

    marker_path = _write_marker(tmp_path, marker.to_json())
    verdict = install_marker.evaluate_marker_status(marker_path, plan)
    assert verdict.current is False
    assert verdict.exit_code == 1
    assert verdict.differences == (field,)


@pytest.mark.parametrize(
    "plan_key,marker_key",
    [(None, KEY), (KEY, None)],
    ids=["plan-absent-marker-present", "plan-present-marker-absent"],
)
def test_marker_plan_key_absence_mismatches(
    plan_key: str | None, marker_key: str | None
) -> None:
    plan = _base_plan(
        attestation=install_marker.MarkerAttestation(
            registry="trusted", status="audited", key_id=plan_key
        )
    )
    marker = install_marker.InstallMarkerV5(
        **_base_marker(
            attestation=install_marker.MarkerAttestation(
                registry="trusted", status="audited", key_id=marker_key
            )
        )
    )

    assert install_marker.compare_marker_plan(marker, plan) == ("key_id",)


def test_marker_plan_presence_mismatch_flags_the_whole_triple() -> None:
    plan = _base_plan(attestation=None)
    marker = install_marker.InstallMarkerV5(**_base_marker())

    assert install_marker.compare_marker_plan(marker, plan) == (
        "registry",
        "status",
        "key_id",
    )


def test_marker_plan_agreement_is_current(tmp_path: Path) -> None:
    plan = _base_plan()
    marker = install_marker.InstallMarkerV5(**_base_marker())

    assert install_marker.compare_marker_plan(marker, plan) == ()
    assert install_marker.marker_plan_is_current(marker, plan) is True

    marker_path = _write_marker(tmp_path, marker.to_json())
    verdict = install_marker.evaluate_marker_status(marker_path, plan)
    assert verdict.current is True
    assert verdict.exit_code == 0
    assert verdict.differences == ()


_RETAINED_ROWS = (
    "build_source",
    "name",
    "content_sha256",
    "locale",
    "agents",
    "commands",
    "dependencies",
    "skill_schema_version",
    "runtime_roots",
    "build_roots",
    "files",
    "builds",
    "requirements",
    "mcp_servers",
    "activation",
    "requirers",
)


@pytest.mark.parametrize("field", list(_RETAINED_ROWS))
def test_marker_plan_retained_rows_still_compare(field: str, tmp_path: Path) -> None:
    """Every retained row refuses reuse: drift anywhere is non-current.

    Parametrised over the literal retained family — deliberately not over the
    production table, so a mutant that drops a row fails here by assertion
    instead of vanishing by collection. The completeness test below pins the
    literal against the table, so a new row still cannot slip in untested.
    """

    plan = _base_plan()
    marker = install_marker.InstallMarkerV5(
        **_base_marker(**_marker_kwargs_for_mismatch(field))
    )

    assert install_marker.compare_marker_plan(marker, plan) == (field,)
    assert install_marker.marker_plan_is_current(marker, plan) is False

    marker_path = _write_marker(tmp_path, marker.to_json())
    verdict = install_marker.evaluate_marker_status(marker_path, plan)
    assert verdict.current is False
    assert verdict.exit_code == 1
    assert verdict.differences == (field,)


def test_marker_plan_row_families_cover_the_table_exactly() -> None:
    """The two literal families equal the comparison table: no silent drift."""

    assert set(_MISMATCH_SIX).isdisjoint(_RETAINED_ROWS)
    assert set(_MISMATCH_SIX) | set(_RETAINED_ROWS) == set(
        install_marker.MARKER_PLAN_COMPARISON_FIELDS
    )
    for field in install_marker.MARKER_PLAN_COMPARISON_FIELDS:
        _marker_kwargs_for_mismatch(field)


def test_marker_plan_lock_identity_is_exact() -> None:
    """S-IDENTITY: the lock binding compares exact strings, never normalised."""

    plan = _base_plan()
    marker = install_marker.InstallMarkerV5(**_base_marker(lock_sha256=LOCK_OTHER))

    assert install_marker.compare_marker_plan(marker, plan) == ("lock_sha256",)


@pytest.mark.parametrize(
    "mutated_lock",
    [
        "SHA256:" + "0" * 64,
        "sha256:" + "0" * 64 + " ",
        " " + "sha256:" + "0" * 64,
        "sha256:" + "0" * 63,
        "sha256:" + "g" * 64,
        "0" * 64,
    ],
    ids=["upper-scheme", "trailing-space", "leading-space", "truncated", "non-hex", "unprefixed"],
)
def test_marker_v5_lock_digest_shape_is_exact(mutated_lock: str) -> None:
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.InstallMarkerV5(**_base_marker(lock_sha256=mutated_lock))

    assert raised.value.code == "install_marker_invalid"
    with pytest.raises(install_marker.InstallMarkerError):
        _base_plan(lock_sha256=mutated_lock)


@pytest.mark.parametrize(
    "other_package",
    [
        _local(),
        source_package.LocalSnapshot(snapshot="sha256:" + "2" * 64),
        _configured(),
        _configured(source="other-source"),
        _network(repository=REPO_OTHER),
        _network(hex_value=SHA1_OTHER),
        source_package.NetworkGit(
            repository=REPO,
            commit=source_package.LockedCommit(
                object_format="sha256", hex="b" * 64
            ),
        ),
        source_package.NetworkGit(
            repository=REPO, commit=_commit(), directory="sub/dir"
        ),
    ],
    ids=[
        "local-snapshot",
        "other-snapshot-digest",
        "configured-git",
        "other-configured-source",
        "other-repository",
        "other-commit",
        "other-object-format",
        "other-directory",
    ],
)
def test_marker_plan_package_arm_change_mismatches(other_package: Any) -> None:
    plan = _base_plan()
    marker = install_marker.InstallMarkerV5(
        **_base_marker(package=other_package, attestation=None)
        if isinstance(other_package, source_package.LocalSnapshot)
        else _base_marker(package=other_package)
    )

    assert "package" in install_marker.compare_marker_plan(marker, plan)


# --- One defect table drives required-evidence validation ---------------------


def test_attestation_evidence_defect_table_is_exact() -> None:
    assert install_marker.ATTESTATION_EVIDENCE_DEFECTS == (
        "absent",
        "unreadable",
        "malformed",
        "stale",
        "revoked",
        "wrong-name",
        "wrong-repository",
        "wrong-commit",
        "wrong-context",
        "wrong-key",
    )


def _expectation(**changes: Any) -> install_marker.AttestationExpectation:
    values: dict[str, Any] = {
        "name": "golden-skill",
        "repository": REPO,
        "commit": _commit(),
        "context_sha256": CONTEXT,
        "key_id": KEY,
    }
    values.update(changes)
    return install_marker.AttestationExpectation(**values)


def _evidence_payload(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "golden-skill",
        "repository": REPO,
        "commit": {"object_format": "sha1", "hex": SHA1},
        "context_sha256": CONTEXT,
        "key_id": KEY,
    }
    payload.update(changes)
    return payload


MALFORMED_EVIDENCE_VARIANTS: tuple[Any, ...] = (
    b"not json at all",
    b"[1, 2, 3]",
    b"null",
    {"name": "golden-skill"},
    {**_evidence_payload(), "extra": True},
    {**_evidence_payload(), "commit": "0123456789abcdef0123456789abcdef01234567"},
    {**_evidence_payload(), "commit": {"object_format": "sha1", "hex": "xyz"}},
    {**_evidence_payload(), "key_id": "not-hex"},
    {**_evidence_payload(), "context_sha256": "sha256:zzz"},
    {**_evidence_payload(), "repository": "https://github.com/example/golden-skills"},
    {**_evidence_payload(), "name": "not an identifier!"},
)


def _defect_fixture(
    defect: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, install_marker.AttestationExpectation, bool, bool, str]:
    """Build one defect fixture: path, expectation, fresh, revoked, code."""

    expectation = _expectation()
    evidence_path = tmp_path / "evidence.json"
    if defect == "absent":
        return evidence_path, expectation, True, False, "attestation_evidence_missing"
    if defect == "unreadable":
        evidence_path.write_bytes(json.dumps(_evidence_payload()).encode("utf-8"))
        return evidence_path, expectation, True, False, "attestation_evidence_unreadable"
    if defect == "malformed":
        evidence_path.write_bytes(b"{oops")
        return evidence_path, expectation, True, False, "attestation_evidence_malformed"
    if defect == "stale":
        evidence_path.write_bytes(json.dumps(_evidence_payload()).encode("utf-8"))
        return evidence_path, expectation, False, False, "attestation_evidence_stale"
    if defect == "revoked":
        evidence_path.write_bytes(json.dumps(_evidence_payload()).encode("utf-8"))
        return evidence_path, expectation, True, True, "attestation_evidence_revoked"
    mutations = {
        "wrong-name": {"name": "other-skill"},
        "wrong-repository": {"repository": REPO_OTHER},
        "wrong-commit": {"commit": {"object_format": "sha1", "hex": SHA1_OTHER}},
        "wrong-context": {"context_sha256": CONTEXT_OTHER},
        "wrong-key": {"key_id": KEY_OTHER},
    }
    evidence_path.write_bytes(json.dumps(_evidence_payload(**mutations[defect])).encode("utf-8"))
    return evidence_path, expectation, True, False, "attestation_evidence_mismatch"


def _refuse_evidence_reads(monkeypatch: pytest.MonkeyPatch, evidence_path: Path) -> None:
    """Fail exactly the evidence read: the marker seam must keep working."""

    original = Path.read_bytes

    def _selective(self: Path) -> bytes:
        if self == evidence_path:
            raise PermissionError("evidence store denied the read")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", _selective)


@pytest.mark.parametrize("defect", list(install_marker.ATTESTATION_EVIDENCE_DEFECTS))
def test_attestation_evidence_defect_refuses(
    defect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_path, expectation, fresh, revoked, code = _defect_fixture(
        defect, tmp_path, monkeypatch
    )
    if defect == "unreadable":
        _refuse_evidence_reads(monkeypatch, evidence_path)

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.validate_attestation_evidence(
            evidence_path, expectation, evidence_fresh=fresh, evidence_revoked=revoked
        )

    assert raised.value.code == code


@pytest.mark.parametrize("variant", MALFORMED_EVIDENCE_VARIANTS)
def test_attestation_evidence_malformed_class_refuses(
    variant: Any, tmp_path: Path
) -> None:
    evidence_path = tmp_path / "evidence.json"
    if isinstance(variant, bytes):
        evidence_path.write_bytes(variant)
    else:
        evidence_path.write_bytes(json.dumps(variant).encode("utf-8"))

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.validate_attestation_evidence(
            evidence_path, _expectation(), evidence_fresh=True, evidence_revoked=False
        )

    assert raised.value.code == "attestation_evidence_malformed"


def test_attestation_evidence_valid_record_passes(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_bytes(json.dumps(_evidence_payload()).encode("utf-8"))

    record = install_marker.validate_attestation_evidence(
        evidence_path, _expectation(), evidence_fresh=True, evidence_revoked=False
    )

    assert record.to_json() == _evidence_payload()


@pytest.mark.parametrize(
    "expected_key,evidence_key",
    [(None, KEY), (KEY, None)],
    ids=["plan-absent-evidence-present", "plan-present-evidence-absent"],
)
def test_attestation_evidence_key_absence_mismatches(
    expected_key: str | None, evidence_key: str | None, tmp_path: Path
) -> None:
    payload = _evidence_payload()
    if evidence_key is None:
        del payload["key_id"]
    else:
        payload["key_id"] = evidence_key
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_bytes(json.dumps(payload).encode("utf-8"))

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.validate_attestation_evidence(
            evidence_path,
            _expectation(key_id=expected_key),
            evidence_fresh=True,
            evidence_revoked=False,
        )

    assert raised.value.code == "attestation_evidence_mismatch"


def test_attestation_evidence_key_absence_agrees(tmp_path: Path) -> None:
    payload = _evidence_payload()
    del payload["key_id"]
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_bytes(json.dumps(payload).encode("utf-8"))

    record = install_marker.validate_attestation_evidence(
        evidence_path,
        _expectation(key_id=None),
        evidence_fresh=True,
        evidence_revoked=False,
    )

    assert record.key_id is None


@pytest.mark.parametrize(
    "verdict",
    ["stale", "revoked"],
    ids=["stale", "revoked"],
)
@pytest.mark.parametrize(
    "key_id",
    [KEY, None],
    ids=["key-present", "key-absent"],
)
def test_attestation_evidence_freshness_verdicts_apply_whatever_the_key(
    verdict: str, key_id: str | None, tmp_path: Path
) -> None:
    """Freshness verdicts are crossed with key absence: stale stays stale."""

    payload = _evidence_payload()
    if key_id is None:
        del payload["key_id"]
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_bytes(json.dumps(payload).encode("utf-8"))

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.validate_attestation_evidence(
            evidence_path,
            _expectation(key_id=key_id),
            evidence_fresh=verdict != "stale",
            evidence_revoked=verdict == "revoked",
        )

    assert raised.value.code == f"attestation_evidence_{verdict}"


def test_marker_summaries_never_authorize_evidence(tmp_path: Path) -> None:
    """The validator takes no marker input: an apparently-valid summary changes nothing."""

    assert "marker" not in inspect.signature(
        install_marker.validate_attestation_evidence
    ).parameters
    assert "summary" not in inspect.signature(
        install_marker.validate_attestation_evidence
    ).parameters
    missing = tmp_path / "missing.json"

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.validate_attestation_evidence(
            missing, _expectation(), evidence_fresh=True, evidence_revoked=False
        )

    assert raised.value.code == "attestation_evidence_missing"


@pytest.mark.parametrize(
    "error",
    [
        PermissionError("denied"),
        IsADirectoryError("is a directory"),
        OSError("loop"),
    ],
    ids=["permission", "is-a-directory", "os-error"],
)
def test_attestation_evidence_read_failures_are_unreadable_not_absent(
    error: OSError, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-ERRORS: every read failure at the real seam refuses as unreadable."""

    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_bytes(json.dumps(_evidence_payload()).encode("utf-8"))
    expectation = _expectation()
    control = install_marker.validate_attestation_evidence(
        evidence_path, expectation, evidence_fresh=True, evidence_revoked=False
    )
    assert control.name == "golden-skill"

    reached = {"count": 0}

    def _fail(self: Path) -> bytes:
        reached["count"] += 1
        raise error

    monkeypatch.setattr(Path, "read_bytes", _fail)
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.validate_attestation_evidence(
            evidence_path, expectation, evidence_fresh=True, evidence_revoked=False
        )

    assert reached["count"] == 1
    assert raised.value.code == "attestation_evidence_unreadable"


def test_attestation_evidence_absent_stays_missing(tmp_path: Path) -> None:
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.validate_attestation_evidence(
            tmp_path / "no-such-file.json",
            _expectation(),
            evidence_fresh=True,
            evidence_revoked=False,
        )

    assert raised.value.code == "attestation_evidence_missing"


# --- Substitution admission: the planning gate --------------------------------


@pytest.mark.parametrize("strict_audit", [False, True])
def test_from_selectors_never_gain_substitution(strict_audit: bool) -> None:
    with pytest.raises(dev_substitutions.DevSubstitutionError) as raised:
        dev_substitutions.check_source_substitution_admission(
            selector_kind="from",
            operator_substitution=True,
            strict_audit=strict_audit,
        )

    assert "forbidden" in str(raised.value)


@pytest.mark.parametrize("marker_present", [False, True], ids=["marker-absent", "marker-present"])
def test_strict_audit_rejects_legacy_substitution_whatever_the_marker_says(
    marker_present: bool,
) -> None:
    """Omitting the marker field cannot bypass the planning gate.

    Both branches build a real marker — with and without the substituted
    member — and the planning verdict is identical, because the gate takes no
    marker input at all.
    """

    marker = install_marker.InstallMarkerV5(
        **_base_marker(
            substituted="some-operator" if marker_present else None,
        )
    )
    assert (marker.substituted is not None) is marker_present
    assert "marker" not in inspect.signature(
        dev_substitutions.check_source_substitution_admission
    ).parameters
    with pytest.raises(dev_substitutions.DevSubstitutionError) as raised:
        dev_substitutions.check_source_substitution_admission(
            selector_kind="legacy",
            operator_substitution=True,
            strict_audit=True,
        )

    assert "strict audit" in str(raised.value)


def test_strict_audit_rejects_external_substitution() -> None:
    with pytest.raises(dev_substitutions.DevSubstitutionError) as raised:
        dev_substitutions.check_source_substitution_admission(
            selector_kind="legacy",
            operator_substitution=False,
            external_substitution=True,
            strict_audit=True,
        )

    assert "strict audit" in str(raised.value)


@pytest.mark.parametrize(
    "selector_kind,operator,external,strict",
    [
        ("legacy", False, False, False),
        ("legacy", True, False, False),
        ("legacy", False, True, False),
        ("legacy", False, False, True),
        ("from", False, False, False),
        ("from", False, False, True),
        ("from", False, True, False),
    ],
    ids=[
        "legacy-plain",
        "legacy-substituted",
        "legacy-external",
        "legacy-strict-plain",
        "from-plain",
        "from-strict-plain",
        "from-external-nonstrict",
    ],
)
def test_substitution_admission_matrix(
    selector_kind: str, operator: bool, external: bool, strict: bool
) -> None:
    dev_substitutions.check_source_substitution_admission(
        selector_kind=selector_kind,
        operator_substitution=operator,
        external_substitution=external,
        strict_audit=strict,
    )


@pytest.mark.parametrize(
    "selector_kind", ["", "collection", "individual", "LEGACY", "legacy ", None]
)
def test_substitution_gate_fails_closed_on_unknown_kinds(selector_kind: Any) -> None:
    with pytest.raises(dev_substitutions.DevSubstitutionError):
        dev_substitutions.check_source_substitution_admission(
            selector_kind=selector_kind,
            operator_substitution=False,
            strict_audit=False,
        )


def test_substitution_gate_performs_no_io() -> None:
    """S-POLICY: rejection precedes cache reads because the gate cannot read."""

    dev_substitutions.check_source_substitution_admission(
        selector_kind="legacy", operator_substitution=True, strict_audit=False
    )
    events: list[str] = []
    state = {"armed": True}

    def hook(event: str, args: object) -> None:
        if state["armed"] and (event == "open" or event.startswith("socket.")):
            events.append(event)

    sys.addaudithook(hook)
    try:
        dev_substitutions.check_source_substitution_admission(
            selector_kind="legacy", operator_substitution=True, strict_audit=False
        )
        with pytest.raises(dev_substitutions.DevSubstitutionError):
            dev_substitutions.check_source_substitution_admission(
                selector_kind="from", operator_substitution=True, strict_audit=False
            )
        with pytest.raises(dev_substitutions.DevSubstitutionError):
            dev_substitutions.check_source_substitution_admission(
                selector_kind="legacy",
                operator_substitution=True,
                strict_audit=True,
            )
    finally:
        state["armed"] = False
    assert events == []


# --- Local content where policy requires a network attestation -----------------


def test_local_snapshot_with_required_registry_attestation_refuses() -> None:
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.check_local_registry_requirement(
            _local(), network_attestation_required=True
        )

    assert raised.value.code == "local_registry_attestation_required"


@pytest.mark.parametrize(
    "package,required",
    [
        (_local(), False),
        (_network(), False),
        (_network(), True),
        (_configured(), False),
        (_configured(), True),
    ],
    ids=["local-unrequired", "network-unrequired", "network-required", "configured-unrequired", "configured-required"],
)
def test_local_registry_requirement_admits_git_and_unrequired_local(
    package: Any, required: bool
) -> None:
    install_marker.check_local_registry_requirement(
        package, network_attestation_required=required
    )


# --- External-only build state -------------------------------------------------


def _external_only_marker(**changes: Any) -> install_marker.InstallMarkerV5:
    values = _base_marker(
        package=_local(),
        attestation=None,
        builds={"golden-tool": _external_record()},
        build_source=None,
        commands=("golden-tool",),
    )
    values.update(changes)
    return install_marker.InstallMarkerV5(**values)


def test_external_only_current_shape_passes() -> None:
    marker = _external_only_marker()

    install_marker.check_external_only_build_state(
        marker, receipt_package=_local()
    )


@pytest.mark.parametrize(
    "mutation",
    ["network-package", "build-source-present", "local-record", "receipt-differs"],
)
def test_external_only_mismatches_refuse(mutation: str) -> None:
    if mutation == "network-package":
        marker = _external_only_marker(package=_network(), attestation=_attestation())
        receipt: Any = _network()
    elif mutation == "build-source-present":
        marker = _external_only_marker(build_source=_build_source())
        receipt = _local()
    elif mutation == "local-record":
        marker = _external_only_marker(
            builds={"local-helper": _local_record()},
            commands=("local-helper",),
        )
        receipt = _local()
    else:
        marker = _external_only_marker()
        receipt = source_package.LocalSnapshot(snapshot="sha256:" + "2" * 64)

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.check_external_only_build_state(marker, receipt_package=receipt)

    assert raised.value.code == "external_only_build_mismatch"


# --- Schema-2 status: marker, then required evidence ---------------------------


def _write_evidence(directory: Path, payload: dict[str, Any]) -> Path:
    path = directory / "evidence.json"
    path.write_bytes(json.dumps(payload).encode("utf-8"))
    return path


def test_schema2_status_current_when_marker_and_evidence_agree(
    tmp_path: Path,
) -> None:
    plan = _base_plan()
    marker_path = _write_marker(
        tmp_path, install_marker.InstallMarkerV5(**_base_marker()).to_json()
    )
    evidence_path = _write_evidence(
        tmp_path, _evidence_payload(context_sha256=CONTENT)
    )

    verdict = install_marker.evaluate_schema2_status(
        marker_path,
        plan,
        evidence_path=evidence_path,
        evidence_fresh=True,
        evidence_revoked=False,
    )

    assert verdict.current is True
    assert verdict.exit_code == 0


def test_schema2_status_binds_evidence_to_raw_package_tree_hash(
    tmp_path: Path,
) -> None:
    """A lock-projected context hash cannot replace the raw package-tree hash."""

    plan = _base_plan(context_sha256=CONTEXT_OTHER, content_sha256=CONTENT)
    marker_path = _write_marker(
        tmp_path, install_marker.InstallMarkerV5(**_base_marker()).to_json()
    )
    evidence_path = _write_evidence(
        tmp_path, _evidence_payload(context_sha256=CONTENT)
    )

    accepted = install_marker.evaluate_schema2_status(
        marker_path,
        plan,
        evidence_path=evidence_path,
        evidence_fresh=True,
        evidence_revoked=False,
    )

    assert accepted.current is True
    assert accepted.exit_code == 0

    evidence_path.write_bytes(
        json.dumps(_evidence_payload(context_sha256=CONTEXT_OTHER)).encode("utf-8")
    )
    refused = install_marker.evaluate_schema2_status(
        marker_path,
        plan,
        evidence_path=evidence_path,
        evidence_fresh=True,
        evidence_revoked=False,
    )

    assert refused.current is False
    assert refused.exit_code == 1
    assert "evidence context hash differs" in refused.detail


def test_schema2_status_unattested_plans_need_no_evidence(tmp_path: Path) -> None:
    plan = install_marker.MarkerPlan(
        name="golden-skill",
        package=_local(),
        lock_sha256=LOCK,
        context_sha256=CONTEXT,
        content_sha256=CONTENT,
        locale=None,
        agents=(),
        commands=(),
        dependencies=(),
        skill_schema_version=8,
        runtime_roots=(),
        build_roots=(),
        files=("SKILL.md",),
        builds={},
        requirements=None,
        mcp_servers=None,
        activation=None,
        requirers=None,
    )
    marker = install_marker.build_install_marker_v5(
        plan,
        content_sha256=CONTENT,
        files=["SKILL.md"],
        locale=None,
        agents=[],
        commands=[],
        dependencies=[],
        skill_schema_version=8,
        runtime_roots=[],
        build_roots=[],
        installed_at="2000-01-01T00:00:00Z",
        builds={},
    )
    marker_path = _write_marker(tmp_path, marker.to_json())

    verdict = install_marker.evaluate_schema2_status(
        marker_path,
        plan,
        evidence_path=None,
        evidence_fresh=True,
        evidence_revoked=False,
    )

    assert verdict.current is True


def test_schema2_status_missing_evidence_path_is_noncurrent(tmp_path: Path) -> None:
    plan = _base_plan()
    marker_path = _write_marker(
        tmp_path, install_marker.InstallMarkerV5(**_base_marker()).to_json()
    )

    verdict = install_marker.evaluate_schema2_status(
        marker_path,
        plan,
        evidence_path=None,
        evidence_fresh=True,
        evidence_revoked=False,
    )

    assert verdict.current is False
    assert verdict.exit_code == 1
    assert "absent" in verdict.detail


@pytest.mark.parametrize("defect", list(install_marker.ATTESTATION_EVIDENCE_DEFECTS))
def test_schema2_status_defective_evidence_is_noncurrent(
    defect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _base_plan()
    marker_path = _write_marker(
        tmp_path, install_marker.InstallMarkerV5(**_base_marker()).to_json()
    )
    evidence_path, _, fresh, revoked, _ = _defect_fixture(defect, tmp_path, monkeypatch)
    if defect == "unreadable":
        _refuse_evidence_reads(monkeypatch, evidence_path)

    verdict = install_marker.evaluate_schema2_status(
        marker_path,
        plan,
        evidence_path=evidence_path,
        evidence_fresh=fresh,
        evidence_revoked=revoked,
    )

    assert verdict.current is False
    assert verdict.exit_code == 1


def test_schema2_status_configured_git_uses_the_explicit_repository(
    tmp_path: Path,
) -> None:
    plan = _base_plan(package=_configured(), attestation=_attestation())
    marker = install_marker.InstallMarkerV5(**_base_marker(package=_configured()))
    marker_path = _write_marker(tmp_path, marker.to_json())
    evidence_path = _write_evidence(
        tmp_path, _evidence_payload(context_sha256=CONTENT)
    )

    verdict = install_marker.evaluate_schema2_status(
        marker_path,
        plan,
        evidence_path=evidence_path,
        evidence_fresh=True,
        evidence_revoked=False,
        expected_repository=REPO,
    )

    assert verdict.current is True


def test_schema2_status_configured_git_without_repository_is_a_caller_error(
    tmp_path: Path,
) -> None:
    plan = _base_plan(package=_configured(), attestation=_attestation())
    marker_path = _write_marker(
        tmp_path, install_marker.InstallMarkerV5(**_base_marker(package=_configured())).to_json()
    )
    evidence_path = _write_evidence(tmp_path, _evidence_payload())

    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.evaluate_schema2_status(
            marker_path,
            plan,
            evidence_path=evidence_path,
            evidence_fresh=True,
            evidence_revoked=False,
        )


def test_schema2_status_network_override_must_match_the_package(
    tmp_path: Path,
) -> None:
    plan = _base_plan()
    marker_path = _write_marker(
        tmp_path, install_marker.InstallMarkerV5(**_base_marker()).to_json()
    )
    evidence_path = _write_evidence(tmp_path, _evidence_payload())

    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.evaluate_schema2_status(
            marker_path,
            plan,
            evidence_path=evidence_path,
            evidence_fresh=True,
            evidence_revoked=False,
            expected_repository=REPO_OTHER,
        )


def test_marker_status_missing_marker_is_noncurrent(tmp_path: Path) -> None:
    verdict = install_marker.evaluate_marker_status(
        tmp_path / "no-such-marker.json", _base_plan()
    )

    assert verdict.current is False
    assert verdict.exit_code == 1
    assert "missing" in verdict.detail


def test_marker_status_unreadable_marker_is_noncurrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker_path = _write_marker(
        tmp_path, install_marker.InstallMarkerV5(**_base_marker()).to_json()
    )

    def _fail(self: Path) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", _fail)
    verdict = install_marker.evaluate_marker_status(marker_path, _base_plan())

    assert verdict.current is False
    assert verdict.exit_code == 1
    assert "unreadable" in verdict.detail


def test_marker_status_unknown_version_fails_closed(tmp_path: Path) -> None:
    payload = install_marker.InstallMarkerV5(**_base_marker()).to_json()
    payload["schema_version"] = 99
    marker_path = tmp_path / ".csk-install.json"
    marker_path.write_bytes(json.dumps(payload).encode("utf-8"))

    verdict = install_marker.evaluate_marker_status(marker_path, _base_plan())

    assert verdict.current is False
    assert verdict.exit_code == 1


# --- Malformed markers are verdicts, never tracebacks ---------------------------


_MALFORMED_MEMBER_VALUES: tuple[tuple[str, Any], ...] = (
    ("int", 5),
    ("bool", True),
    ("float", 1.5),
    ("list", []),
    ("dict", {}),
)

_BUILD_RECORD_MEMBERS: tuple[str, ...] = (
    "driver",
    "receipt_schema_version",
    "execution_policy",
    "cache_key",
    "receipt_sha256",
    "artifact_sha256",
    "artifact_path",
    "repository",
    "declared_identity.kind",
    "declared_identity.value",
    "declared_locked_commit.object_format",
    "declared_locked_commit.hex",
    "declared_tag",
    "effective_identity.kind",
    "effective_identity.value",
    "object_format",
    "commit",
    "substituted",
    "substitution",
    "substitution.type",
    "substitution.ref.kind",
    "build_source",
    "descriptor_target",
)


def _set_dotted_build_member(
    record: dict[str, Any], dotted: str, value: Any
) -> None:
    if dotted == "substitution.type":
        record["substitution"] = {"type": value}
        return
    if dotted == "substitution.ref.kind":
        record["substitution"] = {
            "type": "network-git",
            "ref": {"kind": value, "value": "v9.9.9"},
        }
        return
    if "." in dotted:
        head, tail = dotted.split(".", 1)
        nested = dict(record[head])
        nested[tail] = value
        record[head] = nested
        return
    record[dotted] = value


@pytest.mark.parametrize("member", list(_BUILD_RECORD_MEMBERS))
@pytest.mark.parametrize(
    "label,value",
    [pytest.param(label, value, id=label) for label, value in _MALFORMED_MEMBER_VALUES],
)
def test_malformed_build_record_member_is_noncurrent_never_a_crash(
    member: str, label: str, value: Any, tmp_path: Path
) -> None:
    """S-ERRORS: every malformed build record is a verdict, never a traceback.

    Parametrised over every build-record member crossed with non-string and
    unhashable values, through the status entry point: a malformed marker is
    non-current with a structured refusal detail.
    """

    plan = _base_plan()
    payload = install_marker.InstallMarkerV5(**_base_marker()).to_json()
    assert "golden-tool" in payload["builds"]
    _set_dotted_build_member(payload["builds"]["golden-tool"], member, value)
    marker_path = _write_marker(tmp_path, payload)

    verdict = install_marker.evaluate_marker_status(marker_path, plan)

    assert verdict.current is False
    assert verdict.exit_code == 1
    assert "not usable" in verdict.detail


@pytest.mark.parametrize(
    "bad_commit",
    [pytest.param(value, id=label) for label, value in _MALFORMED_MEMBER_VALUES],
)
def test_malformed_build_commit_refusal_names_the_member(bad_commit: Any) -> None:
    """The commit gate refuses non-strings itself; downstream never sees them.

    Layered validation needs a gate-level assertion: without it, deleting the
    commit check would still refuse downstream, and no verdict-level test
    could tell the gate was gone.
    """

    payload = install_marker.InstallMarkerV5(**_base_marker()).to_json()
    payload["builds"]["golden-tool"]["commit"] = bad_commit

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.parse_install_marker(payload)

    assert raised.value.code == "install_marker_invalid"
    assert "commit must be a string" in raised.value.detail


@pytest.mark.parametrize("identity", ["declared_identity", "effective_identity"])
@pytest.mark.parametrize(
    "bad_kind",
    [pytest.param([], id="list"), pytest.param({}, id="dict")],
)
def test_malformed_identity_kind_refusal_names_the_member(
    identity: str, bad_kind: Any
) -> None:
    """The identity gate refuses unhashable kinds itself, before any set test."""

    payload = install_marker.InstallMarkerV5(**_base_marker()).to_json()
    payload["builds"]["golden-tool"][identity]["kind"] = bad_kind

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.parse_install_marker(payload)

    assert raised.value.code == "install_marker_invalid"
    assert "repository identity kind is invalid" in raised.value.detail


# --- Status is read-only; refusals preserve prior state ------------------------


def _project_with_marker(tmp_path: Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    csk_home = tmp_path / "csk-home"
    project = tmp_path / "project"
    (csk_home / "source-v1").mkdir(parents=True)
    (csk_home / "source-v1" / "record.json").write_text("{}")
    skills = project / ".agents" / "skills" / "golden-skill"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# golden\n")
    marker_path = _write_marker(skills, payload)
    return csk_home, marker_path


@pytest.mark.parametrize("field", list(_MISMATCH_SIX) + list(_RETAINED_ROWS))
def test_status_mismatch_mutates_nothing(field: str, tmp_path: Path) -> None:
    """S-TXN: before/after tree hashes around every non-current status call."""

    plan = _base_plan()
    marker = install_marker.InstallMarkerV5(
        **_base_marker(**_marker_kwargs_for_mismatch(field))
    )
    csk_home, marker_path = _project_with_marker(tmp_path, marker.to_json())
    project = tmp_path / "project"
    before_home = _tree_hash(csk_home)
    before_project = _tree_hash(project)

    verdict = install_marker.evaluate_marker_status(marker_path, plan)

    assert verdict.current is False
    assert verdict.exit_code == 1
    assert _tree_hash(csk_home) == before_home
    assert _tree_hash(project) == before_project


@pytest.mark.parametrize("defect", list(install_marker.ATTESTATION_EVIDENCE_DEFECTS))
def test_evidence_refusal_preserves_prior_state(
    defect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install, repair and refresh refuse through the one validator; the trees stay put."""

    csk_home = tmp_path / "csk-home"
    project = tmp_path / "project"
    csk_home.mkdir()
    project.mkdir()
    (csk_home / "lock.json").write_text("{}")
    (project / "Skillfile.json").write_text("{}")
    evidence_path, expectation, fresh, revoked, _ = _defect_fixture(
        defect, project, monkeypatch
    )
    before_home = _tree_hash(csk_home)
    before_project = _tree_hash(project)
    if defect == "unreadable":
        _refuse_evidence_reads(monkeypatch, evidence_path)

    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.validate_attestation_evidence(
            evidence_path, expectation, evidence_fresh=fresh, evidence_revoked=revoked
        )

    # The refusal is recorded; lift fault injection before measuring so the
    # tree hash reads through the real seam.
    monkeypatch.undo()
    assert _tree_hash(csk_home) == before_home
    assert _tree_hash(project) == before_project


# --- Legacy lanes meet v5 markers without crashing ------------------------------


def _legacy_closure_node() -> closure.ClosureNode:
    return closure.ClosureNode(
        name="golden-skill",
        decl=manifest.SkillDecl(
            name="golden-skill",
            source="golden-skill",
            ref=manifest.SkillRef(kind="revision", value=SHA1),
        ),
        resolved=git_ops.ResolvedRef(kind="revision", ref=SHA1, commit=SHA1),
        repo=Path("/nonexistent/repo"),
        snapshot=Path("/nonexistent/snapshot"),
        spec=skillspec.SkillSpec(commands={}, source_file=None, schema_version=8),
        identity=None,
    )


def test_legacy_status_reports_manifest_drift_for_v5_markers(tmp_path: Path) -> None:
    from csk import status as status_module

    installed = tmp_path / "golden-skill"
    installed.mkdir()
    _write_marker(
        installed, install_marker.InstallMarkerV5(**_base_marker()).to_json()
    )

    inspection = status_module._inspect_node_marker(
        _legacy_closure_node(),
        installed,
        runtime_dir=tmp_path / "runtime",
        effective_locale=None,
        agents=[],
        expected_build_source=None,
    )

    assert inspection.status.label == "manifest-drift"
    assert inspection.status.installed_commit is None
    assert inspection.build_boundary_error is not None


def test_legacy_basic_status_reports_update_for_v5_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from csk import config as config_module
    from csk import status as status_module

    node = _legacy_closure_node()
    skills_root = tmp_path / "skills"
    installed = skills_root / "golden-skill"
    installed.mkdir(parents=True)
    _write_marker(
        installed, install_marker.InstallMarkerV5(**_base_marker()).to_json()
    )
    monkeypatch.setattr(
        git_ops,
        "resolve_ref",
        lambda *args: git_ops.ResolvedRef(kind="revision", ref=SHA1, commit=SHA1),
    )
    config = config_module.GlobalConfig(
        path=tmp_path / "config.json",
        skills_root=tmp_path,
        preferred_locale=None,
        default_agents=[],
        adapter_mode="none",
        worktree_alias_pattern="*",
        projects={},
    )

    result = status_module._basic_skill_status(config, skills_root, node.decl)

    assert result.label == "update-available"
    assert result.installed_commit is None
    assert result.resolved_commit == SHA1


def test_legacy_installer_currentness_rejects_v5_markers(tmp_path: Path) -> None:
    node = _legacy_closure_node()
    plan = installer.SkillPlan(
        decl=node.decl,
        resolved=node.resolved,
        repo=node.repo,
        snapshot=node.snapshot,
        spec=node.spec,
    )
    target = tmp_path / "golden-skill"
    target.mkdir()
    marker_dict = install_marker.InstallMarkerV5(**_base_marker()).to_json()

    assert (
        installer._marker_is_current(
            marker_dict, target, plan, None, [], activation=None
        )
        is False
    )


def test_gc_collects_build_but_no_legacy_state_references_from_v5_markers(
    tmp_path: Path,
) -> None:
    # Contract change ordered by the TASK-260916-341a6q review round 1 (F1):
    # v5 build records mark their receipt-3 references, or the sweep
    # destroys live entries by construction. Only legacy runtime/snapshot
    # state stays unmarked.
    entry = tmp_path / "golden-skill"
    entry.mkdir()
    _write_marker(entry, install_marker.InstallMarkerV5(**_base_marker()).to_json())
    references = gc._References()

    found, warning = gc._collect_marker_directory(entry, references)

    assert (found, warning) == (True, None)
    assert references.runtime == set()
    assert references.snapshots == set()
    assert references.builds == {"sha256:" + "1" * 64}
    assert references.external_builds == {"sha256:" + "4" * 64}
    assert references.external_snapshots == {
        snapshot_key(
            EffectiveState(
                identity_kind="network-git",
                identity="github.com/example/golden-tools",
                transport=None,
                object_format="sha1",
                commit=SHA1,
                substituted=False,
            ),
            "sha256:" + "b" * 64,
        )
    }


def test_legacy_global_retention_skips_v5_runtime_references(tmp_path: Path) -> None:
    csk_home = tmp_path / "home"
    skills_root = global_install.global_skills_root(csk_home)
    entry = skills_root / "golden-skill"
    entry.mkdir(parents=True)
    _write_marker(entry, install_marker.InstallMarkerV5(**_base_marker()).to_json())

    retained = global_install._collect_retained(csk_home, [])

    assert "golden-skill" in retained.names
    assert retained.references == frozenset()
