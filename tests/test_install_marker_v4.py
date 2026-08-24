"""Install marker schema 4: marker-v3 meaning over a schema-8 manifest."""

from __future__ import annotations

import json
from typing import Any

import pytest

from csk import install_marker


SHA1 = "0123456789abcdef0123456789abcdef01234567"


def _local_build() -> install_marker.InstallMarkerBuildV3:
    return install_marker.InstallMarkerBuildV3(
        driver="go-v1",
        receipt_schema_version=1,
        execution_policy="manager-worker-v1",
        cache_key="sha256:" + "1" * 64,
        receipt_sha256="sha256:" + "e" * 64,
        artifact_sha256="sha256:" + "d" * 64,
        artifact_path="bin/local-helper",
    )


def _base(**changes: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "golden-skill",
        "source": "golden-skill",
        "ref_kind": "revision",
        "ref": SHA1,
        "commit": SHA1,
        "content_sha256": "sha256:" + "a" * 64,
        "locale": None,
        "agents": ("codex_cli",),
        "commands": ("helper", "local-helper"),
        "dependencies": (),
        "skill_schema_version": 8,
        "runtime_roots": ("scripts",),
        "installed_at": "2000-01-01T00:00:00Z",
        "files": ("SKILL.md", "scripts/helper"),
        "build_roots": ("tools/cli",),
        "build_source": install_marker.BuildSourceIdentity(
            algorithm="curator-build-source-v1",
            content_sha256="sha256:" + "b" * 64,
        ),
        "builds": {"local-helper": _local_build()},
    }
    payload.update(changes)
    return payload


def test_marker_v4_round_trips_a_schema_eight_installation() -> None:
    marker = install_marker.InstallMarkerV4(**_base())

    decoded = json.loads(install_marker.serialize_install_marker(marker.to_json()))

    assert decoded["schema_version"] == 4
    assert decoded["skill_schema_version"] == 8
    assert decoded["builds"]["local-helper"] == {
        "driver": "go-v1",
        "receipt_schema_version": 1,
        "execution_policy": "manager-worker-v1",
        "cache_key": "sha256:" + "1" * 64,
        "receipt_sha256": "sha256:" + "e" * 64,
        "artifact_sha256": "sha256:" + "d" * 64,
        "artifact_path": "bin/local-helper",
    }
    assert install_marker.parse_install_marker(decoded).to_json() == marker.to_json()


def test_marker_v4_reads_back_as_the_v4_model() -> None:
    raw = install_marker.serialize_install_marker(install_marker.InstallMarkerV4(**_base()).to_json())

    parsed = install_marker.read_install_marker(raw)

    assert isinstance(parsed, install_marker.InstallMarkerV4)
    assert parsed.schema_version == 4


def test_marker_v4_carries_no_script_execution_member() -> None:
    decoded = install_marker.InstallMarkerV4(**_base()).to_json()

    assert "execution_policy" not in decoded
    assert "interpreter" not in decoded
    assert set(decoded) <= (
        install_marker._V4_MEMBERS | {"schema_version"}
    )


@pytest.mark.parametrize("skill_schema", [6, 7, 9])
def test_marker_v4_binds_exactly_skill_schema_eight(skill_schema: int) -> None:
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.InstallMarkerV4(**_base(skill_schema_version=skill_schema))

    assert raised.value.code == "install_marker_invalid"


def test_marker_v3_still_binds_exactly_skill_schema_seven() -> None:
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.InstallMarkerV3(**_base())

    assert raised.value.code == "install_marker_invalid"


def test_marker_v4_requires_build_source_exactly_with_local_builds() -> None:
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.InstallMarkerV4(**_base(build_source=None))

    assert raised.value.code == "install_marker_invalid"

    empty = install_marker.InstallMarkerV4(**_base(builds={}, build_source=None))

    assert empty.to_json()["builds"] == {}
    assert "build_source" not in empty.to_json()


def test_marker_v4_requires_build_roots_for_a_local_build() -> None:
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.InstallMarkerV4(**_base(build_roots=()))

    assert raised.value.code == "install_marker_invalid"


def test_marker_v4_rejects_a_build_that_is_not_an_installed_command() -> None:
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.InstallMarkerV4(**_base(commands=("helper",)))

    assert raised.value.code == "install_marker_invalid"


def test_marker_v4_sorts_every_set_like_array() -> None:
    marker = install_marker.InstallMarkerV4(
        **_base(
            agents=("gemini_cli", "codex_cli"),
            files=("scripts/helper", "SKILL.md"),
            runtime_roots=("scripts",),
        )
    )

    decoded = marker.to_json()

    assert decoded["agents"] == ["codex_cli", "gemini_cli"]
    assert decoded["files"] == ["SKILL.md", "scripts/helper"]


@pytest.mark.parametrize(
    ("marker_schema", "skill_schema", "expected"),
    [
        (4, 8, True),
        (4, 7, False),
        (3, 7, True),
        (3, 8, False),
        (2, 6, True),
        (2, 8, False),
    ],
)
def test_currentness_bands_are_one_marker_per_manifest_version(
    marker_schema: int, skill_schema: int, expected: bool
) -> None:
    if marker_schema == 2:
        marker: install_marker.InstallMarker = install_marker.InstallMarkerV2(
            **_base(
                skill_schema_version=6,
                builds={
                    "local-helper": install_marker.InstallMarkerBuild(
                        driver="go-v1",
                        cache_key="sha256:" + "1" * 64,
                        receipt_sha256="sha256:" + "e" * 64,
                        artifact_sha256="sha256:" + "d" * 64,
                        artifact_path="bin/local-helper",
                    )
                },
            )
        )
    elif marker_schema == 3:
        marker = install_marker.InstallMarkerV3(**_base(skill_schema_version=7))
    else:
        marker = install_marker.InstallMarkerV4(**_base())

    assert (
        install_marker.marker_can_be_current(marker, skill_schema_version=skill_schema) is expected
    )


def test_schema_version_four_is_a_supported_marker_schema() -> None:
    assert install_marker.SUPPORTED_INSTALL_MARKER_SCHEMA_VERSIONS == frozenset({1, 2, 3, 4})


def test_an_unsupported_marker_schema_is_still_rejected() -> None:
    payload = install_marker.InstallMarkerV4(**_base()).to_json()
    payload["schema_version"] = 5

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.parse_install_marker(payload)

    assert raised.value.code == "unsupported_install_marker_schema"
