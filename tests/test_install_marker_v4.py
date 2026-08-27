"""Marker schema 4: marker-v3 meaning over a schema-8 manifest.

Protocol Core section 10 permits ``skill_schema_version`` 8 and otherwise
carries marker-v3 meaning unchanged. An enforced ``script-worker-v1`` command
produces no build entry and adds no marker member, so schema 8 changes which
manifests a marker may describe, not what a marker records.
"""

from __future__ import annotations

import json

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
        artifact_path="bin/modroot-tool",
    )


def _base(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "modroot-skill",
        "source": "modroot-skill",
        "ref_kind": "revision",
        "ref": SHA1,
        "commit": SHA1,
        "content_sha256": "sha256:" + "a" * 64,
        "locale": None,
        "agents": ("codex_cli",),
        "commands": ("modroot-tool",),
        "dependencies": (),
        "skill_schema_version": 8,
        "runtime_roots": (),
        "installed_at": "2000-01-01T00:00:00Z",
        "files": ("SKILL.md",),
        "build_roots": ("tools/cli",),
        "build_source": install_marker.BuildSourceIdentity(
            algorithm="curator-build-source-v1",
            content_sha256="sha256:" + "b" * 64,
        ),
        "builds": {"modroot-tool": _local_build()},
    }
    payload.update(changes)
    return payload


def test_marker_v4_round_trips_the_marker_v3_shape() -> None:
    marker = install_marker.InstallMarkerV4(**_base())
    raw = install_marker.serialize_install_marker(marker.to_json())
    decoded = json.loads(raw)

    assert decoded["schema_version"] == 4
    assert decoded["skill_schema_version"] == 8
    assert decoded["builds"]["modroot-tool"] == {
        "driver": "go-v1",
        "receipt_schema_version": 1,
        "execution_policy": "manager-worker-v1",
        "cache_key": "sha256:" + "1" * 64,
        "receipt_sha256": "sha256:" + "e" * 64,
        "artifact_sha256": "sha256:" + "d" * 64,
        "artifact_path": "bin/modroot-tool",
    }
    parsed = install_marker.read_install_marker(raw)
    assert isinstance(parsed, install_marker.InstallMarkerV4)
    assert parsed.to_json() == marker.to_json()
    assert raw.endswith(b"\n") and b"\r" not in raw


def test_marker_v4_shape_equals_the_marker_v3_shape() -> None:
    v4 = install_marker.InstallMarkerV4(**_base()).to_json()
    v3 = install_marker.InstallMarkerV3(
        **{**_base(), "skill_schema_version": 7}
    ).to_json()

    assert set(v4) == set(v3)


def test_marker_v4_binds_only_schema_eight() -> None:
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.InstallMarkerV4(**_base(skill_schema_version=7))


def test_marker_v3_never_describes_a_schema_eight_installation() -> None:
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.InstallMarkerV3(**_base())


def test_an_empty_schema_eight_installation_carries_no_build_source() -> None:
    marker = install_marker.InstallMarkerV4(
        **_base(builds={}, build_source=None, build_roots=())
    )

    payload = marker.to_json()

    assert payload["builds"] == {}
    assert "build_source" not in payload
    assert install_marker.read_install_marker(
        install_marker.serialize_install_marker(payload)
    ).to_json() == payload


def test_a_local_entry_still_requires_build_source_and_build_roots() -> None:
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.InstallMarkerV4(**_base(build_source=None))
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.InstallMarkerV4(**_base(build_roots=()))


def test_a_build_entry_must_name_an_installed_command() -> None:
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.InstallMarkerV4(**_base(commands=("other",)))


@pytest.mark.parametrize(
    ("schema_version", "skill_schema_version", "current"),
    [
        (4, 8, True),
        (4, 7, False),
        (3, 8, False),
        (3, 7, True),
    ],
)
def test_marker_currentness_bands_are_disjoint(
    schema_version: int, skill_schema_version: int, current: bool
) -> None:
    """Every marker keeps its frozen manifest band, so a schema-8 installation
    is recorded by marker v4 alone."""

    payload = _base(skill_schema_version=skill_schema_version)
    marker_type = (
        install_marker.InstallMarkerV4
        if schema_version == 4
        else install_marker.InstallMarkerV3
    )
    try:
        marker = marker_type(**payload)  # type: ignore[arg-type]
    except install_marker.InstallMarkerError:
        assert not current
        return
    assert (
        install_marker.marker_can_be_current(
            marker, skill_schema_version=skill_schema_version
        )
        is current
    )


def test_an_unknown_marker_schema_is_still_rejected() -> None:
    payload = install_marker.InstallMarkerV4(**_base()).to_json()
    payload["schema_version"] = 5

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.parse_install_marker(payload)

    assert raised.value.code == "unsupported_install_marker_schema"


def test_marker_v4_rejects_an_unknown_member() -> None:
    payload = install_marker.InstallMarkerV4(**_base()).to_json()
    payload["modules"] = ["pkg/board"]

    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.parse_install_marker(payload)
