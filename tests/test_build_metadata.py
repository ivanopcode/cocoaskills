from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from csk import install_marker, protocol_json
from csk.builds import (
    BuildMetadataError,
    cache_key,
    canonical_input_bytes,
    canonical_receipt_bytes,
    parse_build_input,
    parse_receipt,
    read_receipt,
    receipt_sha256,
    verify_receipt,
)
from csk.builds.toolchain import NativeTarget


# The 1.0.0-rc.6 candidate suite that publishes expected/marker-v2.json, the
# marker-v2 writer golden this manager's own marker output is compared against.
# The rc.5 suite (sha256:b6f56aac...) carries no writer golden, so no single root
# can satisfy both this digest and the conformance consumer.
EXPECTED_MANIFEST_SHA256 = (
    "sha256:12e58b82579645ba1ccafba49d3e2dd3216005ddf37ae63c68a9fafd46773071"
)
NON_UTF8_ENCODINGS = (
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "utf-32",
    "utf-32-le",
    "utf-32-be",
)
ROOT_TEXT = os.environ.get("CURATOR_CONFORMANCE_ROOT")
pytestmark = pytest.mark.skipif(
    not ROOT_TEXT,
    reason="CURATOR_CONFORMANCE_ROOT is not set",
)


def _root() -> Path:
    assert ROOT_TEXT is not None
    root = Path(ROOT_TEXT)
    manifest = root / "manifest.json"
    assert manifest.is_file(), f"invalid conformance root: {root}"
    digest = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert digest == EXPECTED_MANIFEST_SHA256
    return root


def _json(relative: str) -> Any:
    return json.loads((_root() / relative).read_text(encoding="utf-8"))


def _assert_build_error(expected: str, raised: pytest.ExceptionInfo[BuildMetadataError]) -> None:
    assert raised.value.code == expected


def _assert_marker_error(
    expected: str,
    raised: pytest.ExceptionInfo[install_marker.InstallMarkerError],
) -> None:
    assert raised.value.code == expected


def _canonical_sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(protocol_json.canonical_bytes(value)).hexdigest()


def test_rc5_candidate_manifest_digest_is_pinned_as_non_release_evidence() -> None:
    manifest = (_root() / "manifest.json").read_bytes()
    assert "sha256:" + hashlib.sha256(manifest).hexdigest() == EXPECTED_MANIFEST_SHA256


def test_go_v1_input_and_receipt_match_caller_supplied_rc5_bytes() -> None:
    expected_dir = _root() / "expected" / "build-driver"
    vector = _json("vectors/build-drivers.json")["portable_identity"]
    input_raw = (expected_dir / "build-input.ccj.json").read_bytes()
    receipt_raw = (expected_dir / "receipt.ccj.json").read_bytes()
    expected_cache_key = (expected_dir / "cache-key.txt").read_text(encoding="utf-8").strip()
    expected_receipt_hash = (
        expected_dir / "receipt-sha256.txt"
    ).read_text(encoding="utf-8").strip()

    build_input = parse_build_input(protocol_json.loads_canonical(input_raw))
    receipt = read_receipt(receipt_raw)

    assert build_input.to_json() == vector["build_input"]
    assert build_input.policy.execution_policy == "manager-worker-v1"
    assert canonical_input_bytes(build_input) == input_raw
    assert len(input_raw) == 869
    assert protocol_json.is_canonical(input_raw)
    assert not input_raw.startswith(b"\xef\xbb\xbf")
    assert not input_raw.endswith(b"\n")
    assert cache_key(build_input) == expected_cache_key == vector["cache_key"]
    assert expected_cache_key == (
        "sha256:529370122ae11e2e961d5265b1a020e046bcd43165b2eb96b05e73a51187ac9b"
    )

    assert receipt.to_json() == vector["stored_receipt"]
    assert receipt.input == build_input
    assert canonical_receipt_bytes(receipt) == receipt_raw
    assert len(receipt_raw) == 1120
    assert protocol_json.is_canonical(receipt_raw)
    assert not receipt_raw.startswith(b"\xef\xbb\xbf")
    assert not receipt_raw.endswith(b"\n")
    assert receipt_sha256(receipt_raw) == expected_receipt_hash == vector["receipt_sha256"]
    assert expected_receipt_hash == (
        "sha256:919fbbad8e6ce95532219fd952c2309d0d7026f85209650508fd6834af4020cd"
    )
    assert verify_receipt(
        receipt_raw,
        expected_input=build_input,
        expected_cache_key=expected_cache_key,
        expected_receipt_sha256=expected_receipt_hash,
    ) == receipt


def test_execution_policy_identities_are_exact_non_alias_negatives() -> None:
    identities = _json("vectors/build-drivers.json")["cache_identity"]
    assert identities["aliases"] is False

    expected = {
        "legacy_rc4_without_execution_policy": (
            None,
            "sha256:3fcd714a40e8918eb67dbd35d435875dcce6c9047da811a1fa26626e5e57be48",
            "build_input_invalid",
        ),
        "portable": (
            "manager-worker-v1",
            "sha256:529370122ae11e2e961d5265b1a020e046bcd43165b2eb96b05e73a51187ac9b",
            None,
        ),
        "reserved_hardened": (
            "hardened-worker-v1",
            "sha256:13736230d33ce59de7f7323dcd4cffd510655ad8dabd5ee9e8b6cb182ec70037",
            "build_execution_policy_unsupported",
        ),
    }

    derived: set[str] = set()
    for name, (execution_policy, expected_key, expected_error) in expected.items():
        identity = identities[name]
        assert identity["execution_policy"] == execution_policy
        assert identity["cache_key"] == expected_key
        assert _canonical_sha256(identity["input"]) == expected_key
        derived.add(expected_key)
        if expected_error is None:
            assert identity["schema_valid"] is True
            assert cache_key(parse_build_input(identity["input"])) == expected_key
        else:
            assert identity["schema_valid"] is False
            with pytest.raises(BuildMetadataError) as raised:
                parse_build_input(identity["input"])
            _assert_build_error(expected_error, raised)

    assert len(derived) == 3


RECEIPT_INVALID_CASES = (
    sorted(
        path.name
        for path in (
            Path(ROOT_TEXT) / "schema-cases" / "build-receipt-v1"
        ).glob("invalid*.json")
    )
    if ROOT_TEXT
    else []
)


@pytest.mark.parametrize("case_name", RECEIPT_INVALID_CASES)
def test_all_rc5_build_receipt_schema_negatives_are_rejected(case_name: str) -> None:
    payload = _json(f"schema-cases/build-receipt-v1/{case_name}")
    with pytest.raises(BuildMetadataError):
        parse_receipt(payload)


def test_receipt_reader_rejects_ambiguous_unsafe_and_noncanonical_bytes() -> None:
    raw = (_root() / "expected" / "build-driver" / "receipt.ccj.json").read_bytes()
    decoded = protocol_json.loads_canonical(raw)
    assert isinstance(decoded, dict)

    duplicate_key = raw.replace(
        b'{"artifact":',
        b'{"schema_version":1,"artifact":',
        1,
    )
    unsafe_integer = raw.replace(b'"size":1234567', b'"size":9007199254740992', 1)
    pretty = json.dumps(decoded, indent=2, ensure_ascii=False).encode("utf-8")
    variants = [
        duplicate_key,
        unsafe_integer,
        b"\xef\xbb\xbf" + raw,
        b" " + raw,
        raw + b"\n",
        pretty,
    ]

    for candidate in variants:
        with pytest.raises(BuildMetadataError) as raised:
            read_receipt(candidate)
        _assert_build_error("receipt_not_canonical", raised)


def test_receipt_reader_rejects_unknown_versions_keys_paths_and_identities() -> None:
    valid = _json("vectors/build-drivers.json")["portable_identity"]["stored_receipt"]

    mutations: list[tuple[str, Any, str]] = [
        ("unknown top-level", lambda body: body.__setitem__("cache_path", "/tmp/cache"), "build_receipt_invalid"),
        ("unsupported receipt", lambda body: body.__setitem__("schema_version", 2), "unsupported_receipt_schema"),
        (
            "mismatched cache key",
            lambda body: body.__setitem__("cache_key", "sha256:" + "0" * 64),
            "cache_key_mismatch",
        ),
        (
            "wrong artifact path",
            lambda body: body["artifact"].__setitem__("path", "bin/not-golden-tool"),
            "artifact_path_mismatch",
        ),
        (
            "bad build source",
            lambda body: body["input"]["build_source"].__setitem__(
                "content_sha256", "SHA256:" + "b" * 64
            ),
            "build_input_invalid",
        ),
        (
            "bad toolchain",
            lambda body: body["input"]["toolchain"].__setitem__(
                "algorithm", "curator-go-toolchain-v2"
            ),
            "build_input_invalid",
        ),
        (
            "missing execution policy",
            lambda body: body["input"]["policy"].pop("execution_policy"),
            "build_input_invalid",
        ),
        (
            "unknown execution policy",
            lambda body: body["input"]["policy"].__setitem__(
                "execution_policy", "hardened-worker-v1"
            ),
            "build_execution_policy_unsupported",
        ),
    ]

    for _name, mutate, expected_code in mutations:
        payload = copy.deepcopy(valid)
        mutate(payload)
        candidate = protocol_json.canonical_bytes(payload)
        with pytest.raises(BuildMetadataError) as raised:
            read_receipt(candidate)
        _assert_build_error(expected_code, raised)


def test_build_input_parser_rejects_malformed_native_identities_before_keying() -> None:
    valid = _json("vectors/build-drivers.json")["portable_identity"]["build_input"]

    def wrong_tuning(body: dict[str, Any]) -> None:
        body["target"]["tuning"] = {"GOAMD64": "v1"}

    def unsupported_architecture(body: dict[str, Any]) -> None:
        body["target"]["goarch"] = "loong64"
        body["target"]["tuning"] = {"GOAMD64": "v1"}
        body["toolchain"]["go_version"] = "go version go1.26.1 darwin/loong64"

    def mismatched_toolchain_target(body: dict[str, Any]) -> None:
        body["toolchain"]["go_version"] = "go version go1.26.1 linux/amd64"

    def malformed_go_version(body: dict[str, Any]) -> None:
        body["toolchain"]["go_version"] = "go1.26.1 darwin/arm64"

    def nonnormalized_go_version(body: dict[str, Any]) -> None:
        body["toolchain"]["go_version"] += "\r"

    for mutate in (
        wrong_tuning,
        unsupported_architecture,
        mismatched_toolchain_target,
        malformed_go_version,
        nonnormalized_go_version,
    ):
        payload = copy.deepcopy(valid)
        mutate(payload)
        with pytest.raises(BuildMetadataError) as raised:
            cache_key(parse_build_input(payload))
        _assert_build_error("build_input_invalid", raised)


def test_build_input_constructor_rejects_wrong_tuning_and_toolchain_target() -> None:
    valid = parse_build_input(
        _json("vectors/build-drivers.json")["portable_identity"]["build_input"]
    )

    with pytest.raises(BuildMetadataError) as raised:
        replace(
            valid,
            target=NativeTarget(
                goos=valid.target.goos,
                goarch=valid.target.goarch,
                tuning={"GOAMD64": "v1"},
            ),
        )
    _assert_build_error("build_input_invalid", raised)

    with pytest.raises(BuildMetadataError) as raised:
        replace(
            valid,
            toolchain=replace(
                valid.toolchain,
                go_version="go version go1.26.1 linux/amd64",
            ),
        )
    _assert_build_error("build_input_invalid", raised)


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        (("command",), "other-tool"),
        (("build_root",), "other-build"),
        (("source_dir",), "build/cmd/other-tool"),
        (("build_source", "content_sha256"), "sha256:" + "a" * 64),
        (("target", "tuning", "GOARM64"), "v8.1"),
        (("toolchain", "go_version"), "go version go1.26.2 darwin/arm64"),
    ],
)
def test_receipt_verification_rejects_every_expected_input_mismatch(
    field_path: tuple[str, ...],
    value: str,
) -> None:
    vector = _json("vectors/build-drivers.json")["portable_identity"]
    raw = (_root() / "expected" / "build-driver" / "receipt.ccj.json").read_bytes()
    expected = copy.deepcopy(vector["build_input"])
    target = expected
    for component in field_path[:-1]:
        target = target[component]
    target[field_path[-1]] = value
    mismatched = parse_build_input(expected)

    with pytest.raises(BuildMetadataError) as raised:
        verify_receipt(raw, expected_input=mismatched)
    _assert_build_error("cache_input_mismatch", raised)


def test_receipt_verification_rejects_lookup_key_and_receipt_hash_mismatches() -> None:
    vector = _json("vectors/build-drivers.json")["portable_identity"]
    raw = (_root() / "expected" / "build-driver" / "receipt.ccj.json").read_bytes()
    build_input = parse_build_input(vector["build_input"])

    with pytest.raises(BuildMetadataError) as raised:
        verify_receipt(
            raw,
            expected_input=build_input,
            expected_cache_key="sha256:" + "0" * 64,
        )
    _assert_build_error("cache_key_mismatch", raised)

    with pytest.raises(BuildMetadataError) as raised:
        verify_receipt(
            raw,
            expected_input=build_input,
            expected_receipt_sha256="sha256:" + "0" * 64,
        )
    _assert_build_error("receipt_hash_mismatch", raised)


MARKER_V1_VALID_CASES = (
    sorted(
        path.name
        for path in (
            Path(ROOT_TEXT) / "schema-cases" / "install-marker-v1"
        ).glob("valid*.json")
    )
    if ROOT_TEXT
    else []
)
MARKER_V2_VALID_CASES = (
    sorted(
        path.name
        for path in (
            Path(ROOT_TEXT) / "schema-cases" / "install-marker-v2"
        ).glob("valid*.json")
    )
    if ROOT_TEXT
    else []
)
MARKER_INVALID_CASES = (
    [
        (schema, path.name)
        for schema in ("install-marker-v1", "install-marker-v2")
        for path in sorted(
            (Path(ROOT_TEXT) / "schema-cases" / schema).glob("invalid*.json")
        )
    ]
    if ROOT_TEXT
    else []
)


@pytest.mark.parametrize("case_name", MARKER_V1_VALID_CASES)
def test_rc5_marker_v1_remains_readable_for_pre_v6_installs(case_name: str) -> None:
    raw = (_root() / "schema-cases" / "install-marker-v1" / case_name).read_bytes()
    marker = install_marker.read_install_marker(raw)

    assert isinstance(marker, install_marker.InstallMarkerV1)
    assert marker.to_json() == json.loads(raw)
    assert install_marker.marker_can_be_current(
        marker,
        skill_schema_version=marker.skill_schema_version,
    )
    assert not install_marker.marker_can_be_current(marker, skill_schema_version=6)


@pytest.mark.parametrize("case_name", MARKER_V2_VALID_CASES)
def test_all_rc5_marker_v2_schema_positives_are_typed(case_name: str) -> None:
    raw = (_root() / "schema-cases" / "install-marker-v2" / case_name).read_bytes()
    marker = install_marker.read_install_marker(raw)

    assert isinstance(marker, install_marker.InstallMarkerV2)
    assert marker.to_json() == json.loads(raw)
    assert install_marker.marker_can_be_current(
        marker,
        skill_schema_version=marker.skill_schema_version,
    )


@pytest.mark.parametrize(("schema", "case_name"), MARKER_INVALID_CASES)
def test_all_rc5_marker_v1_and_v2_schema_negatives_are_rejected(
    schema: str,
    case_name: str,
) -> None:
    raw = (_root() / "schema-cases" / schema / case_name).read_bytes()
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.read_install_marker(raw)


def test_marker_v2_matches_caller_supplied_golden_and_sorts_all_build_state() -> None:
    raw = (_root() / "expected" / "build-driver" / "marker.json").read_bytes()
    expected = json.loads(raw)
    vector = _json("vectors/build-drivers.json")["portable_identity"]["marker"]
    marker = install_marker.read_install_marker(raw)

    assert isinstance(marker, install_marker.InstallMarkerV2)
    assert marker.schema_version == 2
    assert marker.skill_schema_version == 6
    assert marker.to_json() == expected == vector
    assert marker.build_source is not None
    assert tuple(marker.builds) == ("golden-tool",)
    build = marker.builds["golden-tool"]
    assert build.driver == "go-v1"
    assert build.cache_key == (
        "sha256:529370122ae11e2e961d5265b1a020e046bcd43165b2eb96b05e73a51187ac9b"
    )
    assert build.receipt_sha256 == (
        "sha256:919fbbad8e6ce95532219fd952c2309d0d7026f85209650508fd6834af4020cd"
    )
    assert build.artifact_path == "bin/golden-tool"
    assert build.artifact_sha256 == "sha256:" + "d" * 64


def test_marker_v2_canonicalizes_set_arrays_and_build_key_order() -> None:
    payload = _json("schema-cases/install-marker-v2/valid-multiple-builds.json")
    payload["agents"] = ["zeta", "alpha"]
    payload["build_roots"].reverse()
    payload["commands"].reverse()
    payload["dependencies"] = ["zeta", "alpha"]
    payload["files"].reverse()
    payload["runtime_roots"] = ["zeta", "alpha"]
    payload["builds"] = dict(reversed(list(payload["builds"].items())))

    marker = install_marker.parse_install_marker(payload)
    assert isinstance(marker, install_marker.InstallMarkerV2)
    canonical = marker.to_json()
    for field in (
        "agents",
        "build_roots",
        "commands",
        "dependencies",
        "files",
        "runtime_roots",
    ):
        assert canonical[field] == sorted(canonical[field])
    assert list(canonical["builds"]) == ["alpha-tool", "golden-tool"]


def test_marker_v2_models_and_sorts_all_optional_schema_members() -> None:
    payload = _json("schema-cases/install-marker-v2/valid-empty-builds.json")
    payload.update(
        {
            "git": "https://example.invalid/golden-skill.git",
            "requirements": ["zeta", "alpha"],
            "mcp_servers": {
                "zeta": ["zeta", "alpha"],
                "alpha": ["codex_cli"],
            },
            "attestation": {
                "registry": "https://registry.example",
                "status": "audited",
                "key_id": "0123456789abcdef",
            },
            "substituted": "/operator/skill",
        }
    )

    marker = install_marker.parse_install_marker(payload)
    assert isinstance(marker, install_marker.InstallMarkerV2)
    canonical = marker.to_json()
    assert canonical["requirements"] == ["alpha", "zeta"]
    assert list(canonical["mcp_servers"]) == ["alpha", "zeta"]
    assert canonical["mcp_servers"]["zeta"] == ["alpha", "zeta"]
    assert canonical["attestation"] == payload["attestation"]
    assert canonical["git"] == payload["git"]
    assert canonical["substituted"] == payload["substituted"]


def test_marker_reader_rejects_duplicate_keys_unsafe_integers_and_marker_v3() -> None:
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.read_install_marker(
            b'{"schema_version":2,"schema_version":2}'
        )
    _assert_marker_error("install_marker_invalid", raised)

    payload = _json("schema-cases/install-marker-v2/valid-empty-builds.json")
    payload["skill_schema_version"] = protocol_json.MAX_SAFE_INTEGER + 1
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.read_install_marker(json.dumps(payload).encode("utf-8"))
    _assert_marker_error("install_marker_invalid", raised)

    marker_v3 = (
        _root() / "schema-cases" / "install-marker-v3" / "valid.json"
    ).read_bytes()
    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.read_install_marker(marker_v3)
    _assert_marker_error("unsupported_install_marker_schema", raised)


@pytest.mark.parametrize("encoding", NON_UTF8_ENCODINGS)
def test_marker_reader_rejects_non_utf8_byte_encodings(encoding: str) -> None:
    raw = (_root() / "expected" / "build-driver" / "marker.json").read_bytes()
    non_utf8 = raw.decode("utf-8").encode(encoding)

    with pytest.raises(install_marker.InstallMarkerError) as raised:
        install_marker.read_install_marker(non_utf8)
    _assert_marker_error("install_marker_invalid", raised)


def test_marker_v1_rejects_build_only_members_and_duplicate_set_values() -> None:
    payload = _json("schema-cases/install-marker-v1/valid.json")
    payload["build_roots"] = []
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.parse_install_marker(payload)

    payload = _json("schema-cases/install-marker-v2/valid-empty-builds.json")
    payload["commands"].append(payload["commands"][0])
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.parse_install_marker(payload)


def test_marker_v2_rejects_wrong_derived_artifact_path_and_build_name() -> None:
    payload = _json("schema-cases/install-marker-v2/valid.json")
    payload["builds"]["golden-tool"]["artifact_path"] = "bin/another-tool"
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.parse_install_marker(payload)

    payload = _json("schema-cases/install-marker-v2/valid.json")
    payload["commands"] = []
    with pytest.raises(install_marker.InstallMarkerError):
        install_marker.parse_install_marker(payload)


def test_external_receipt_v2_is_explicitly_out_of_scope() -> None:
    payload = _json("schema-cases/build-receipt-v2/valid.json")
    with pytest.raises(BuildMetadataError) as raised:
        parse_receipt(payload)
    _assert_build_error("unsupported_receipt_schema", raised)
