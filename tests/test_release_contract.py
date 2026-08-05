from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
import zipfile
from copy import deepcopy
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "release_contract.py"


def _load_contract() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_contract", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


contract = _load_contract()


@pytest.mark.parametrize(
    ("raw", "tag", "version", "prerelease"),
    [
        ("0.13.0", "v0.13.0", "0.13.0", False),
        ("v0.13.0-rc.4", "v0.13.0-rc.4", "0.13.0rc4", True),
        ("v1.2.3-alpha.2", "v1.2.3-alpha.2", "1.2.3a2", True),
        ("v1.2.3-beta.7", "v1.2.3-beta.7", "1.2.3b7", True),
    ],
)
def test_parse_release_tag_routes_exact_versions(
    raw: str, tag: str, version: str, prerelease: bool
) -> None:
    route = contract.parse_release_tag(raw)
    assert (route.tag, route.version, route.prerelease) == (tag, version, prerelease)


@pytest.mark.parametrize(
    "tag",
    [
        "v0.13",
        "v0.13.0-rc",
        "v0.13.0-rc.0",
        "v0.13.0-preview.1",
        "v0.13.0-rc.4-extra",
        "v00.13.0",
    ],
)
def test_parse_release_tag_rejects_ambiguous_or_unsupported_tags(tag: str) -> None:
    with pytest.raises(contract.ContractError):
        contract.parse_release_tag(tag)


def _write_fake_dist(
    dist: Path, route: object, *, metadata_version: str | None = None
) -> None:
    dist.mkdir()
    wheel_name, sdist_name = route.distribution_names
    version = metadata_version or route.version
    archive_version = route.version
    metadata = (
        f"Metadata-Version: 2.1\nName: cocoaskills\nVersion: {version}\n\n".encode()
    )
    with zipfile.ZipFile(dist / wheel_name, "w") as archive:
        archive.writestr(f"cocoaskills-{archive_version}.dist-info/METADATA", metadata)
    with tarfile.open(dist / sdist_name, "w:gz") as archive:
        info = tarfile.TarInfo(f"cocoaskills-{archive_version}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
        nested = tarfile.TarInfo(
            f"cocoaskills-{archive_version}/src/cocoaskills.egg-info/PKG-INFO"
        )
        nested.size = len(metadata)
        archive.addfile(nested, io.BytesIO(metadata))


def test_verify_dist_generates_and_rechecks_exact_checksums(tmp_path: Path) -> None:
    route = contract.parse_release_tag("v0.13.0-rc.4")
    dist = tmp_path / "dist"
    _write_fake_dist(dist, route)

    digests = contract.verify_dist(route, dist, write_checksums=True)

    assert sorted(digests) == sorted(route.distribution_names)
    assert contract.parse_checksums((dist / "SHA256SUMS").read_bytes()) == digests
    assert contract.verify_dist(route, dist, write_checksums=False) == digests


def test_verify_dist_rejects_tag_metadata_mismatch(tmp_path: Path) -> None:
    route = contract.parse_release_tag("v0.13.0-rc.4")
    dist = tmp_path / "dist"
    _write_fake_dist(dist, route, metadata_version="0.13.0rc3")

    with pytest.raises(contract.ContractError, match="metadata mismatch"):
        contract.verify_dist(route, dist, write_checksums=True)


def test_verify_dist_rejects_extra_distribution(tmp_path: Path) -> None:
    route = contract.parse_release_tag("v0.13.0")
    dist = tmp_path / "dist"
    _write_fake_dist(dist, route)
    (dist / "cocoaskills-0.12.5.tar.gz").write_bytes(b"stale")

    with pytest.raises(contract.ContractError, match="distribution files mismatch"):
        contract.verify_dist(route, dist, write_checksums=True)


def _publication_snapshot(
    tag: str,
) -> tuple[object, dict, dict, dict, dict, dict[str, bytes]]:
    route = contract.parse_release_tag(tag)
    wheel_name, sdist_name = route.distribution_names
    payloads = {
        wheel_name: b"wheel-bytes",
        sdist_name: b"sdist-bytes",
    }
    payloads["SHA256SUMS"] = contract.checksum_payload(payloads)
    payloads.update(
        {name: f"attestation:{name}".encode() for name in route.attestation_names}
    )
    release = {
        "tag_name": route.tag,
        "draft": False,
        "prerelease": route.prerelease,
        "assets": [
            {
                "name": name,
                "digest": f"sha256:{contract.sha256_bytes(payload)}",
            }
            for name, payload in payloads.items()
        ],
    }
    pypi_version = {
        "urls": [
            {
                "filename": name,
                "digests": {"sha256": contract.sha256_bytes(payloads[name])},
            }
            for name in route.distribution_names
        ]
    }
    stable = route.version if not route.prerelease else "0.12.5"
    latest_release = {
        "tag_name": f"v{stable}",
        "draft": False,
        "prerelease": False,
    }
    pypi_project = {
        "releases": {
            stable: [{"yanked": False}],
            route.version: [{"yanked": False}],
        }
    }
    return route, release, latest_release, pypi_version, pypi_project, payloads


def test_verify_publication_accepts_rc_without_changing_stable_latest() -> None:
    route, release, latest_release, pypi_version, pypi_project, payloads = (
        _publication_snapshot("v0.13.0-rc.4")
    )

    assert (
        contract.verify_publication_snapshot(
            route, release, latest_release, pypi_version, pypi_project, payloads
        )
        == "0.12.5"
    )


def test_verify_publication_accepts_stable_as_latest() -> None:
    route, release, latest_release, pypi_version, pypi_project, payloads = (
        _publication_snapshot("v0.13.0")
    )

    assert (
        contract.verify_publication_snapshot(
            route, release, latest_release, pypi_version, pypi_project, payloads
        )
        == "0.13.0"
    )


@pytest.mark.parametrize(
    "mutation",
    ["tag", "prerelease", "asset", "github_digest", "pypi_digest", "latest"],
)
def test_verify_publication_fails_closed_on_routing_asset_or_digest_mismatch(
    mutation: str,
) -> None:
    route, release, latest_release, pypi_version, pypi_project, payloads = (
        _publication_snapshot("v0.13.0-rc.4")
    )
    release = deepcopy(release)
    latest_release = deepcopy(latest_release)
    pypi_version = deepcopy(pypi_version)
    payloads = dict(payloads)
    if mutation == "tag":
        release["tag_name"] = "v0.13.0-rc.3"
    elif mutation == "prerelease":
        release["prerelease"] = False
    elif mutation == "asset":
        release["assets"].pop()
        payloads.pop("SHA256SUMS")
    elif mutation == "github_digest":
        release["assets"][0]["digest"] = "sha256:" + "0" * 64
    elif mutation == "pypi_digest":
        pypi_version["urls"][0]["digests"]["sha256"] = "0" * 64
    else:
        latest_release["tag_name"] = route.tag

    with pytest.raises(contract.ContractError):
        contract.verify_publication_snapshot(
            route, release, latest_release, pypi_version, pypi_project, payloads
        )


def test_prerelease_rejects_stable_latest_at_same_base_version() -> None:
    route, release, latest_release, pypi_version, pypi_project, payloads = (
        _publication_snapshot("v0.13.0-rc.4")
    )
    pypi_project["releases"]["0.13.0"] = [{"yanked": False}]

    with pytest.raises(contract.ContractError, match="must not replace stable latest"):
        contract.verify_publication_snapshot(
            route, release, latest_release, pypi_version, pypi_project, payloads
        )


def test_workflows_keep_rc_and_stable_routes_explicit() -> None:
    release_workflow = (SCRIPT.parents[1] / "workflows" / "release.yml").read_text()
    smoke_workflow = (
        SCRIPT.parents[1] / "workflows" / "distribution-smoke.yml"
    ).read_text()

    assert "needs.build.outputs.prerelease == 'true'" in release_workflow
    assert "prerelease: ${{ needs.build.outputs.prerelease }}" in release_workflow
    assert (
        "make_latest: ${{ needs.build.outputs.prerelease == 'true' && 'false' || 'true' }}"
        in release_workflow
    )
    assert "draft: ${{ needs.build.outputs.prerelease }}" in release_workflow
    assert "Publish GitHub prerelease after immutable asset upload" in release_workflow
    assert "github.event.workflow_run.conclusion == 'success'" in smoke_workflow
    assert "!contains(github.event.workflow_run.head_branch, '-')" not in smoke_workflow
    assert "PIP_INDEX_URL: https://pypi.org/simple" in smoke_workflow
    assert "UV_DEFAULT_INDEX: https://pypi.org/simple" in smoke_workflow
    assert "MISE_PIPX_REGISTRY_URL: https://pypi.org/simple/{}" in smoke_workflow
    assert "needs.resolve-version.result == 'success'" in smoke_workflow
    assert release_workflow.count("attestations: true") == 2
