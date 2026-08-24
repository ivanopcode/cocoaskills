"""Substitution records in an external marker build, per Core section 4.2.

The marker reader used to admit any `tag` or `revision` ref and never checked
the record against the effective source it claims to describe. Three published
conformance cases disagreed with that: `branch` is an admitted structured ref,
a `local-path` substitution names an operator working tree rather than a
network remote, and a structured `revision` is a full object id *for the
effective repository object format*.
"""

from __future__ import annotations

import pytest

from csk import install_marker


SHA1 = "0123456789abcdef0123456789abcdef01234567"
SHA256 = "a" * 64


def _identity(kind: str = "network-git", value: str = "git.example.com/forks/tools"):
    return install_marker.MarkerRepositoryIdentity(kind, value)


def _substituted_build(**changes: object) -> install_marker.InstallMarkerBuildV3:
    fields: dict[str, object] = {
        "driver": "go-repository-v1",
        "receipt_schema_version": 2,
        "execution_policy": "manager-worker-v1",
        "repository": "golden-tools",
        "declared_identity": _identity("network-git", "github.com/example/golden-tools"),
        "declared_locked_commit": install_marker.MarkerRepositoryCommit("sha1", SHA1),
        "declared_tag": "v1.4.0",
        "effective_identity": _identity(),
        "object_format": "sha1",
        "commit": SHA1,
        "substituted": True,
        "substitution": install_marker.MarkerRepositorySubstitution(
            type="network-git",
            ref=install_marker.MarkerRepositoryRef("tag", "v1.4.0"),
        ),
        "build_source": install_marker.BuildSourceIdentity(
            algorithm="curator-build-source-v1",
            content_sha256="sha256:" + "b" * 64,
        ),
        "descriptor_target": "golden-tool",
        "cache_key": "sha256:" + "4" * 64,
        "receipt_sha256": "sha256:" + "0" * 64,
        "artifact_sha256": "sha256:" + "6" * 64,
        "artifact_path": "bin/golden-tool",
    }
    fields.update(changes)
    return install_marker.InstallMarkerBuildV3(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["revision", "tag", "branch"])
def test_every_structured_ref_kind_the_protocol_admits_is_accepted(kind: str) -> None:
    value = SHA1 if kind == "revision" else "release/v2"
    ref = install_marker.MarkerRepositoryRef(kind, value)

    assert ref.to_json() == {"kind": kind, "value": value}
    assert _substituted_build(
        substitution=install_marker.MarkerRepositorySubstitution(type="network-git", ref=ref)
    ).substitution is not None


@pytest.mark.parametrize("kind", ["", "head", "commit", "Branch", "refs/heads/main"])
def test_no_other_ref_kind_is_admitted(kind: str) -> None:
    with pytest.raises(install_marker.InstallMarkerError, match="ref kind"):
        install_marker.MarkerRepositoryRef(kind, "value")


def test_a_local_substitution_requires_an_operator_local_effective_identity() -> None:
    local = install_marker.MarkerRepositorySubstitution(type="local-path")

    with pytest.raises(install_marker.InstallMarkerError, match="effective identity kind"):
        _substituted_build(substitution=local)

    build = _substituted_build(
        substitution=local,
        effective_identity=_identity("operator-local-git", "sha256:" + "c" * 64),
    )
    assert build.substitution is local


def test_a_network_substitution_requires_a_network_effective_identity() -> None:
    with pytest.raises(install_marker.InstallMarkerError, match="effective identity kind"):
        _substituted_build(
            effective_identity=_identity("operator-local-git", "sha256:" + "c" * 64)
        )


@pytest.mark.parametrize(
    ("object_format", "commit", "revision"),
    [("sha1", SHA1, SHA256), ("sha256", SHA256, SHA1)],
)
def test_a_revision_ref_of_the_wrong_width_is_rejected(
    object_format: str, commit: str, revision: str
) -> None:
    with pytest.raises(install_marker.InstallMarkerError, match="full lowercase"):
        _substituted_build(
            object_format=object_format,
            commit=commit,
            declared_locked_commit=install_marker.MarkerRepositoryCommit(object_format, commit),
            substitution=install_marker.MarkerRepositorySubstitution(
                type="network-git",
                ref=install_marker.MarkerRepositoryRef("revision", revision),
            ),
        )


@pytest.mark.parametrize(
    ("object_format", "commit"), [("sha1", SHA1), ("sha256", SHA256)]
)
def test_a_revision_ref_matching_the_effective_object_format_is_accepted(
    object_format: str, commit: str
) -> None:
    build = _substituted_build(
        object_format=object_format,
        commit=commit,
        declared_locked_commit=install_marker.MarkerRepositoryCommit(object_format, commit),
        substitution=install_marker.MarkerRepositorySubstitution(
            type="network-git",
            ref=install_marker.MarkerRepositoryRef("revision", commit),
        ),
    )
    assert build.substitution is not None
    assert build.substitution.ref is not None
    assert build.substitution.ref.value == commit


def test_a_non_hex_revision_ref_is_rejected() -> None:
    with pytest.raises(install_marker.InstallMarkerError, match="full lowercase"):
        _substituted_build(
            substitution=install_marker.MarkerRepositorySubstitution(
                type="network-git",
                ref=install_marker.MarkerRepositoryRef("revision", "Z" + SHA1[1:]),
            )
        )


def test_a_tag_or_branch_ref_is_not_measured_against_the_object_format() -> None:
    """Only `revision` names an object id; a ref name has no width rule."""
    for kind in ("tag", "branch"):
        build = _substituted_build(
            substitution=install_marker.MarkerRepositorySubstitution(
                type="network-git",
                ref=install_marker.MarkerRepositoryRef(kind, "v1.4.0"),
            )
        )
        assert build.substitution is not None
