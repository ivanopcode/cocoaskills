"""Fail-closed release routing and artifact verification for CocoaSkills."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from typing import Any

PROJECT = "cocoaskills"
TAG_RE = re.compile(
    r"^v(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<kind>rc|alpha|beta)\.(?P<number>[1-9][0-9]*))?$"
)
STABLE_VERSION_RE = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)$"
)


class ContractError(RuntimeError):
    """The release state does not match the requested immutable route."""


@dataclass(frozen=True)
class ReleaseRoute:
    tag: str
    version: str
    base_version: str
    prerelease: bool

    @property
    def distribution_names(self) -> tuple[str, str]:
        return (
            f"{PROJECT}-{self.version}-py3-none-any.whl",
            f"{PROJECT}-{self.version}.tar.gz",
        )

    @property
    def attestation_names(self) -> tuple[str, str]:
        return tuple(f"{name}.publish.attestation" for name in self.distribution_names)

    @property
    def asset_names(self) -> tuple[str, str, str, str, str]:
        return (*self.distribution_names, *self.attestation_names, "SHA256SUMS")


def parse_release_tag(raw: str) -> ReleaseRoute:
    tag = raw.strip()
    if not tag.startswith("v"):
        tag = f"v{tag}"
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise ContractError(
            "release tag must be vMAJOR.MINOR.PATCH or "
            "vMAJOR.MINOR.PATCH-(rc|alpha|beta).N"
        )

    base = ".".join(match.group(name) for name in ("major", "minor", "patch"))
    kind = match.group("kind")
    number = match.group("number")
    if kind is None:
        version = base
    else:
        marker = {"rc": "rc", "alpha": "a", "beta": "b"}[kind]
        version = f"{base}{marker}{number}"
    return ReleaseRoute(
        tag=tag, version=version, base_version=base, prerelease=kind is not None
    )


def _metadata_fields(payload: bytes, source: str) -> tuple[str, str]:
    message = BytesParser().parsebytes(payload)
    name = message.get("Name")
    version = message.get("Version")
    if not name or not version:
        raise ContractError(f"{source} is missing Name or Version metadata")
    return name, version


def _wheel_metadata(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        members = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(members) != 1:
            raise ContractError(
                f"{path.name} must contain exactly one dist-info/METADATA"
            )
        return _metadata_fields(archive.read(members[0]), path.name)


def _sdist_metadata(path: Path) -> tuple[str, str]:
    with tarfile.open(path, mode="r:gz") as archive:
        root_metadata = f"{path.name.removesuffix('.tar.gz')}/PKG-INFO"
        members = [
            member for member in archive.getmembers() if member.name == root_metadata
        ]
        if len(members) != 1:
            raise ContractError(f"{path.name} must contain exactly one root PKG-INFO")
        stream = archive.extractfile(members[0])
        if stream is None:
            raise ContractError(f"cannot read metadata from {path.name}")
        return _metadata_fields(stream.read(), path.name)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def checksum_payload(distributions: Mapping[str, bytes]) -> bytes:
    lines = [
        f"{sha256_bytes(distributions[name])}  {name}" for name in sorted(distributions)
    ]
    return ("\n".join(lines) + "\n").encode()


def parse_checksums(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("SHA256SUMS must be UTF-8") from exc
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if match is None:
            raise ContractError(f"invalid SHA256SUMS row: {line!r}")
        digest, name = match.groups()
        if name in result:
            raise ContractError(f"duplicate SHA256SUMS row for {name}")
        result[name] = digest
    if not result:
        raise ContractError("SHA256SUMS is empty")
    return result


def verify_dist(
    route: ReleaseRoute, dist: Path, *, write_checksums: bool
) -> dict[str, str]:
    if not dist.is_dir():
        raise ContractError(f"distribution directory does not exist: {dist}")
    found = sorted(
        path.name for path in dist.iterdir() if path.name.endswith((".whl", ".tar.gz"))
    )
    expected = sorted(route.distribution_names)
    if found != expected:
        raise ContractError(
            f"distribution files mismatch: expected {expected}, got {found}"
        )

    wheel = dist / route.distribution_names[0]
    sdist = dist / route.distribution_names[1]
    for path, metadata in (
        (wheel, _wheel_metadata(wheel)),
        (sdist, _sdist_metadata(sdist)),
    ):
        if metadata != (PROJECT, route.version):
            raise ContractError(
                f"{path.name} metadata mismatch: expected {(PROJECT, route.version)}, got {metadata}"
            )

    payloads = {name: (dist / name).read_bytes() for name in route.distribution_names}
    checksums = checksum_payload(payloads)
    checksum_path = dist / "SHA256SUMS"
    if write_checksums:
        checksum_path.write_bytes(checksums)
    elif not checksum_path.is_file() or checksum_path.read_bytes() != checksums:
        raise ContractError("SHA256SUMS does not exactly match wheel and sdist bytes")
    return {name: sha256_bytes(payload) for name, payload in payloads.items()}


def latest_stable_version(project_json: Mapping[str, Any]) -> str:
    releases = project_json.get("releases")
    if not isinstance(releases, Mapping):
        raise ContractError("PyPI project response has no releases map")
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for version, files in releases.items():
        match = STABLE_VERSION_RE.fullmatch(str(version))
        if match is None or not isinstance(files, Sequence):
            continue
        if not any(
            isinstance(item, Mapping) and not item.get("yanked", False)
            for item in files
        ):
            continue
        key = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
        candidates.append((key, str(version)))
    if not candidates:
        raise ContractError("PyPI has no non-yanked stable CocoaSkills release")
    return max(candidates)[1]


def verify_publication_snapshot(
    route: ReleaseRoute,
    release: Mapping[str, Any],
    latest_release: Mapping[str, Any],
    pypi_version: Mapping[str, Any],
    pypi_project: Mapping[str, Any],
    asset_payloads: Mapping[str, bytes],
) -> str:
    if release.get("tag_name") != route.tag:
        raise ContractError(
            f"GitHub tag mismatch: expected {route.tag}, got {release.get('tag_name')}"
        )
    if release.get("draft") is not False:
        raise ContractError("GitHub release must be published, not draft")
    if release.get("prerelease") is not route.prerelease:
        raise ContractError("GitHub prerelease flag does not match the tag route")

    assets = release.get("assets")
    if not isinstance(assets, Sequence):
        raise ContractError("GitHub release has no asset list")
    assets_by_name: dict[str, Mapping[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, Mapping) or not isinstance(asset.get("name"), str):
            raise ContractError("GitHub release contains an invalid asset record")
        name = str(asset["name"])
        if name in assets_by_name:
            raise ContractError(f"GitHub release contains duplicate asset {name}")
        assets_by_name[name] = asset
    if sorted(assets_by_name) != sorted(route.asset_names):
        raise ContractError(
            f"GitHub assets mismatch: expected {sorted(route.asset_names)}, "
            f"got {sorted(assets_by_name)}"
        )
    if sorted(asset_payloads) != sorted(route.asset_names):
        raise ContractError("downloaded GitHub asset set does not match the release")

    actual_digests = {
        name: sha256_bytes(payload) for name, payload in asset_payloads.items()
    }
    for name, asset in assets_by_name.items():
        expected_digest = f"sha256:{actual_digests[name]}"
        if asset.get("digest") != expected_digest:
            raise ContractError(
                f"GitHub digest mismatch for {name}: expected {expected_digest}, got {asset.get('digest')}"
            )

    checksums = parse_checksums(asset_payloads["SHA256SUMS"])
    expected_checksum_names = sorted(route.distribution_names)
    if sorted(checksums) != expected_checksum_names:
        raise ContractError(
            f"SHA256SUMS names mismatch: expected {expected_checksum_names}, got {sorted(checksums)}"
        )
    for name in route.distribution_names:
        if checksums[name] != actual_digests[name]:
            raise ContractError(f"SHA256SUMS digest mismatch for {name}")

    urls = pypi_version.get("urls")
    if not isinstance(urls, Sequence):
        raise ContractError("PyPI version response has no urls list")
    pypi_by_name: dict[str, Mapping[str, Any]] = {}
    for item in urls:
        if not isinstance(item, Mapping) or not isinstance(item.get("filename"), str):
            raise ContractError("PyPI contains an invalid file record")
        name = str(item["filename"])
        if name in pypi_by_name:
            raise ContractError(f"PyPI contains duplicate file {name}")
        pypi_by_name[name] = item
    if sorted(pypi_by_name) != expected_checksum_names:
        raise ContractError(
            f"PyPI files mismatch: expected {expected_checksum_names}, got {sorted(pypi_by_name)}"
        )
    for name, item in pypi_by_name.items():
        digests = item.get("digests")
        pypi_digest = digests.get("sha256") if isinstance(digests, Mapping) else None
        if pypi_digest != actual_digests[name]:
            raise ContractError(f"PyPI/GitHub digest mismatch for {name}")

    stable_latest = latest_stable_version(pypi_project)
    stable_key = tuple(int(part) for part in stable_latest.split("."))
    base_key = tuple(int(part) for part in route.base_version.split("."))
    if route.prerelease and stable_key >= base_key:
        raise ContractError(
            f"prerelease {route.version} must not replace stable latest {stable_latest}"
        )
    if not route.prerelease and stable_latest != route.version:
        raise ContractError(
            f"stable release {route.version} is not stable latest (got {stable_latest})"
        )

    expected_latest_tag = f"v{stable_latest}" if route.prerelease else route.tag
    if latest_release.get("tag_name") != expected_latest_tag:
        raise ContractError(
            "GitHub latest release mismatch: "
            f"expected {expected_latest_tag}, got {latest_release.get('tag_name')}"
        )
    if (
        latest_release.get("draft") is not False
        or latest_release.get("prerelease") is not False
    ):
        raise ContractError("GitHub latest release must be a published stable release")
    return stable_latest


def _request(
    url: str, *, token: str | None = None, accept: str = "application/json"
) -> bytes:
    headers = {"Accept": accept, "User-Agent": "cocoaskills-release-contract/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _json(url: str, *, token: str | None = None) -> Mapping[str, Any]:
    payload = json.loads(_request(url, token=token))
    if not isinstance(payload, Mapping):
        raise ContractError(f"expected JSON object from {url}")
    return payload


def verify_published(route: ReleaseRoute, repo: str, token: str) -> str:
    release = _json(
        f"https://api.github.com/repos/{repo}/releases/tags/{route.tag}", token=token
    )
    latest_release = _json(
        f"https://api.github.com/repos/{repo}/releases/latest", token=token
    )
    assets = release.get("assets")
    if not isinstance(assets, Sequence):
        raise ContractError("GitHub release has no asset list")
    payloads: dict[str, bytes] = {}
    for asset in assets:
        if not isinstance(asset, Mapping):
            raise ContractError("GitHub release contains an invalid asset record")
        name = asset.get("name")
        url = asset.get("url")
        if not isinstance(name, str) or not isinstance(url, str):
            raise ContractError("GitHub asset is missing name or API URL")
        payloads[name] = _request(url, token=token, accept="application/octet-stream")
    pypi_version = _json(f"https://pypi.org/pypi/{PROJECT}/{route.version}/json")
    pypi_project = _json(f"https://pypi.org/pypi/{PROJECT}/json")
    return verify_publication_snapshot(
        route, release, latest_release, pypi_version, pypi_project, payloads
    )


def _write_outputs(values: Mapping[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as stream:
            stream.writelines(f"{key}={value}\n" for key, value in values.items())


def _resolve_command(args: argparse.Namespace) -> None:
    route = parse_release_tag(args.tag)
    values = {
        "tag": route.tag,
        "version": route.version,
        "base_version": route.base_version,
        "prerelease": str(route.prerelease).lower(),
    }
    _write_outputs(values)
    print(json.dumps(values, sort_keys=True))


def _verify_dist_command(args: argparse.Namespace) -> None:
    route = parse_release_tag(args.tag)
    digests = verify_dist(route, Path(args.dist), write_checksums=args.write_checksums)
    print(
        json.dumps(
            {"tag": route.tag, "version": route.version, "digests": digests},
            sort_keys=True,
        )
    )


def _verify_published_command(args: argparse.Namespace) -> None:
    route = parse_release_tag(args.tag)
    token = os.environ.get(args.token_env)
    if not token:
        raise ContractError(
            f"required token environment variable is unset: {args.token_env}"
        )
    last_error: Exception | None = None
    for attempt in range(1, args.attempts + 1):
        try:
            stable_latest = verify_published(route, args.repo, token)
            values = {"stable_latest": stable_latest}
            _write_outputs(values)
            print(
                json.dumps(
                    {"tag": route.tag, "version": route.version, **values},
                    sort_keys=True,
                )
            )
            return
        except (ContractError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == args.attempts:
                break
            print(
                f"publication verification attempt {attempt} failed: {exc}", flush=True
            )
            time.sleep(args.delay)
    raise ContractError(f"published release did not converge: {last_error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--tag", required=True)
    resolve.set_defaults(func=_resolve_command)

    verify_dist_parser = subparsers.add_parser("verify-dist")
    verify_dist_parser.add_argument("--tag", required=True)
    verify_dist_parser.add_argument("--dist", default="dist")
    verify_dist_parser.add_argument("--write-checksums", action="store_true")
    verify_dist_parser.set_defaults(func=_verify_dist_command)

    verify_published_parser = subparsers.add_parser("verify-published")
    verify_published_parser.add_argument("--tag", required=True)
    verify_published_parser.add_argument("--repo", required=True)
    verify_published_parser.add_argument("--token-env", default="GITHUB_TOKEN")
    verify_published_parser.add_argument("--attempts", type=int, default=5)
    verify_published_parser.add_argument("--delay", type=int, default=15)
    verify_published_parser.set_defaults(func=_verify_published_command)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except ContractError as exc:
        raise SystemExit(f"release contract failed: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
