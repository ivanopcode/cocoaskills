from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import urllib.request

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from csk import audit_registry, closure, config, git_ops, hashing, identifiers, installer, manifest, skillspec, whitelist
from csk.config import RegistryConfig
from csk.source_identity import SourceIdentityError, parse_source_identity


ROOT_TEXT = os.environ.get("CURATOR_CONFORMANCE_ROOT")
pytestmark = pytest.mark.skipif(not ROOT_TEXT, reason="CURATOR_CONFORMANCE_ROOT is not set")


def _root() -> Path:
    assert ROOT_TEXT is not None
    root = Path(ROOT_TEXT)
    assert (root / "manifest.json").is_file(), f"invalid conformance root: {root}"
    return root


def _json(relative: str) -> Any:
    return json.loads((_root() / relative).read_text(encoding="utf-8"))


def test_shared_fixture_context_hash_and_marker(tmp_path: Path) -> None:
    fixture = _root() / "fixtures" / "skill"
    expected_files = _json("expected/context_files.json")
    expected_hash = (_root() / "expected" / "context_sha256.txt").read_text(encoding="utf-8").strip()
    expected_marker = _json("expected/marker.json")

    spec = skillspec.load_skill_spec(fixture)
    destination = tmp_path / "context"
    files = whitelist.copy_context(
        fixture,
        destination,
        include_scripts=not any(command.type == "script" for command in spec.commands.values()),
        exclude_roots=spec.runtime_roots,
    )
    assert files == expected_files
    assert hashing.content_sha256(destination) == expected_hash

    commit = expected_marker["commit"]
    plan = installer.SkillPlan(
        decl=manifest.SkillDecl(
            name="golden-skill",
            source="golden-skill",
            ref=manifest.SkillRef("revision", commit),
        ),
        resolved=git_ops.ResolvedRef("revision", commit, commit),
        repo=fixture,
        snapshot=fixture,
        spec=spec,
    )
    marker = installer._marker_payload(
        plan,
        None,
        ["codex_cli"],
        content_hash=expected_hash,
        files=list(reversed(files)),
        activation={"context": True, "commands": ["golden-tool"]},
        requirers=["<project>"],
        substituted=None,
    )
    marker["installed_at"] = expected_marker["installed_at"]
    assert marker == expected_marker


@pytest.mark.parametrize("case", _json("vectors/canonical-valid.json") if ROOT_TEXT else [])
def test_ccj_positive_vectors(case: dict[str, Any]) -> None:
    assert audit_registry.canonical_bytes(case["input"]).decode("utf-8") == case["canonical_utf8"]


@pytest.mark.parametrize("case", _json("vectors/canonical-invalid.json") if ROOT_TEXT else [])
def test_ccj_rejection_vectors(case: dict[str, str]) -> None:
    with pytest.raises(audit_registry.RegistryError):
        audit_registry.load_protocol_json(case["input_text"])


@pytest.mark.parametrize("case", _json("vectors/source-identities.json") if ROOT_TEXT else [])
def test_source_identity_vectors(case: dict[str, Any]) -> None:
    if "error" in case:
        with pytest.raises(SourceIdentityError):
            parse_source_identity(case["input"])
    else:
        assert parse_source_identity(case["input"]) == case["identity"]


@pytest.mark.parametrize("case", _json("vectors/identifiers.json") if ROOT_TEXT else [])
def test_identifier_vectors(case: dict[str, Any]) -> None:
    assert identifiers.is_valid_identifier(case["input"]) is case["valid"]


@pytest.mark.parametrize("case", _json("vectors/locale-selectors.json") if ROOT_TEXT else [])
def test_locale_selector_vectors(case: dict[str, Any]) -> None:
    assert identifiers.is_valid_locale(case["input"]) is case["valid"]


@pytest.mark.parametrize("case", _json("vectors/manager-config.json") if ROOT_TEXT else [])
def test_manager_config_vectors(case: dict[str, Any], tmp_path: Path) -> None:
    if not case["valid"]:
        with pytest.raises(config.ConfigError):
            config.parse_config(case["input"], tmp_path / "config.json")
        return
    parsed = config.parse_config(case["input"], tmp_path / "config.json")
    expected = case["expected"]
    assert parsed.default_agents == expected["default_agents"]
    assert parsed.adapter_mode == expected["adapter_mode"]
    assert [item.url for item in parsed.audit_registries] == expected["registry_urls"]
    if "project_alias" in expected:
        assert parsed.projects["app"].project_alias == expected["project_alias"]
        assert parsed.projects["app"].checkout_alias == expected["checkout_alias"]
    assert parsed.audit.snapshot_max_age_seconds == expected["snapshot_max_age_seconds"]
    assert parsed.audit.snapshot_clock_skew_seconds == expected["snapshot_clock_skew_seconds"]
    assert parsed.audit.cache_ttl_seconds == expected["cache_ttl_seconds"]
    assert parsed.audit.offline_grace_seconds == expected["offline_grace_seconds"]
    assert parsed.audit.max_request_bytes == expected["max_request_bytes"]


@pytest.mark.parametrize("case", _json("vectors/portable-paths.json") if ROOT_TEXT else [])
def test_portable_path_vectors(case: dict[str, Any]) -> None:
    assert identifiers.is_valid_portable_path(case["input"]) is case["valid"]


def test_closure_order_and_cycle_vectors() -> None:
    cases = {case["name"]: case for case in _json("vectors/closures.json")}
    diamond = cases["deterministic-diamond"]
    nodes = {name: SimpleNamespace(name=name, edges=[]) for name in diamond["nodes"]}
    for consumer, provider in diamond["edges"]:
        nodes[provider].edges.append(closure.ActivationEdge(consumer=consumer, mode="full"))
    assert [node.name for node in closure._topological_order(nodes)] == diamond["expected_provider_order"]

    cycle = cases["cycle"]
    cyclic = {name: SimpleNamespace(name=name, edges=[]) for name in {item for edge in cycle["edges"] for item in edge}}
    for consumer, provider in cycle["edges"]:
        cyclic[provider].edges.append(closure.ActivationEdge(consumer=consumer, mode="full"))
    with pytest.raises(closure.ClosureError, match="cycle"):
        closure._topological_order(cyclic)


def test_shared_registry_signatures_and_deny_wins() -> None:
    key = (_root() / "expected" / "registry" / "pinned_key.txt").read_text(encoding="utf-8").strip()
    audited_payload = _json("expected/registry/record_audited.json")
    revoked_payload = _json("expected/registry/record_revoked.json")
    forged_payload = _json("expected/registry/record_forged.json")
    wrong_key_payload = _json("expected/registry/record_wrong_key_id.json")
    audited = audit_registry.parse_record(audited_payload)
    revoked = audit_registry.parse_record(revoked_payload)
    assert audit_registry.verify_record(audited, (key,))
    assert audit_registry.verify_record(revoked, (key,))
    assert not audit_registry.verify_record(audit_registry.parse_record(forged_payload), (key,))
    assert not audit_registry.verify_record(audit_registry.parse_record(wrong_key_payload), (key,))

    registries = (
        RegistryConfig("first", "https://one.example", (key,)),
        RegistryConfig("second", "https://two.example", (key,)),
    )

    def fetch(url: str, source_identity: str, commit: str, content_sha256: str) -> list[dict[str, Any]]:
        return [audited_payload] if url == "https://one.example" else [revoked_payload]

    result = audit_registry.resolve(
        registries,
        source_identity=audited.source_identity,
        commit=audited.commit,
        content_sha256=audited.content_sha256,
        fetch=fetch,
    )
    assert result.result == audit_registry.RESULT_REVOKED


def test_shared_registry_snapshot_signature() -> None:
    key = (_root() / "expected" / "registry" / "pinned_key.txt").read_text(encoding="utf-8").strip()
    snapshot = _json("expected/registry/snapshot.json")
    assert audit_registry.verify_snapshot(snapshot, (key,))


REGISTRY_CLIENT_VECTOR = _json("vectors/registry-client.json") if ROOT_TEXT else {}


@pytest.mark.parametrize("case", REGISTRY_CLIENT_VECTOR.get("retry_cases", []))
def test_registry_client_retry_vectors(case: dict[str, Any]) -> None:
    assert (
        audit_registry.retry_permitted(case["method"], case["outcome"], case["idempotency_key"])
        is case["retry_permitted"]
    )


def test_registry_client_retry_execution_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = REGISTRY_CLIENT_VECTOR["retry_policy"]
    assert audit_registry.MAX_HTTP_ATTEMPTS == policy["max_attempts"]
    assert audit_registry.GET_TOTAL_DEADLINE_SECONDS == policy["get_total_deadline_seconds"]
    assert audit_registry.POST_TOTAL_DEADLINE_SECONDS == policy["post_total_deadline_seconds"]
    assert policy["follow_redirects"] is False
    assert audit_registry._RejectRegistryRedirect().redirect_request() is None

    class UnavailableResponse:
        status = 503
        headers = {"Content-Type": "application/json", "Retry-After": "0"}

        def __enter__(self) -> UnavailableResponse:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    requests: list[urllib.request.Request] = []

    def unavailable(request: urllib.request.Request, *, timeout: float) -> UnavailableResponse:
        assert timeout > 0
        requests.append(request)
        return UnavailableResponse()

    monkeypatch.setattr(audit_registry, "_open_registry_request", unavailable)
    monkeypatch.setattr(audit_registry.time, "sleep", lambda _seconds: None)
    with pytest.raises(audit_registry.RegistryError, match="HTTP 503"):
        audit_registry.http_get_snapshot("https://registry.example")
    assert len(requests) == policy["max_attempts"]

    record = (_root() / "expected" / "registry" / "record_audited.json").read_bytes()
    requests.clear()
    with pytest.raises(audit_registry.RegistryError, match="HTTP 503"):
        audit_registry.http_publish_record("https://registry.example", "secret-token", record)
    assert len(requests) == policy["max_attempts"]
    bodies = [request.data for request in requests]
    keys = [
        next(
            value
            for name, value in request.header_items()
            if name.lower() == "idempotency-key"
        )
        for request in requests
    ]
    assert all(body == record for body in bodies)
    assert keys[0] and all(key == keys[0] for key in keys)


def _conformance_key() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return private, "ed25519:" + base64.b64encode(public).decode("ascii")


def _conformance_snapshot(version: int, head: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "merkle_root": "b" * 64,
        "log_size": version,
        "head": head,
        "version": version,
        "created_at": "2026-07-13T00:00:00Z",
    }


def _sign_conformance_snapshot(
    private: Ed25519PrivateKey, body: dict[str, Any]
) -> dict[str, Any]:
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    signed = dict(body)
    signed["sig"] = {
        "key_id": hashlib.sha256(public).hexdigest()[:16],
        "algorithm": "ed25519",
        "signature": base64.b64encode(private.sign(audit_registry.canonical_bytes(body))).decode("ascii"),
    }
    return signed


@pytest.mark.parametrize("case", REGISTRY_CLIENT_VECTOR.get("snapshot_transitions", []))
def test_registry_client_snapshot_transition_vectors(case: dict[str, Any], tmp_path: Path) -> None:
    assert REGISTRY_CLIENT_VECTOR["state_key"] == "canonical_registry_url"
    assert REGISTRY_CLIENT_VECTOR["key_rotation_resets_state"] is False
    old_private, old_pin = _conformance_key()
    new_private, new_pin = _conformance_key()
    registry_url = "https://registry.example/curator"
    initial = RegistryConfig("before-rotation", registry_url, (old_pin, new_pin))
    stored = _sign_conformance_snapshot(
        old_private, _conformance_snapshot(case["stored_version"], "a" * 64)
    )
    now = audit_registry._parse_iso8601("2026-07-13T01:00:00Z")
    assert now is not None
    unavailable, warnings = audit_registry.check_snapshots(
        (initial,), tmp_path, fetch_snapshot=lambda _url: stored, now=now
    )
    assert unavailable == set()
    assert warnings == []

    candidate_body = _conformance_snapshot(case["candidate_version"], "a" * 64)
    if not case["same_body"] and case["candidate_version"] == case["stored_version"]:
        candidate_body["head"] = "c" * 64
    candidate = _sign_conformance_snapshot(new_private, candidate_body)
    rotated = RegistryConfig("after-rotation", registry_url, (old_pin, new_pin))
    unavailable, _ = audit_registry.check_snapshots(
        (rotated,), tmp_path, fetch_snapshot=lambda _url: candidate, now=now
    )
    assert (registry_url not in unavailable) is case["accepted"]


@pytest.mark.parametrize("case", REGISTRY_CLIENT_VECTOR.get("pagination_rejections", []))
def test_registry_client_pagination_rejection_vectors(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, body: bytes):
            self.body = body

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return self.body if size < 0 else self.body[:size]

    def fake_urlopen(request: urllib.request.Request, timeout: int = 0) -> FakeResponse:
        nonlocal calls
        del request, timeout
        calls += 1
        if case["error"] == "pagination_cycle":
            body = json.dumps({"records": [], "next_cursor": "same"}).encode()
        elif case["error"] == "invalid_cursor":
            body = json.dumps(
                {"records": [], "next_cursor": "x" * case["characters"]}
            ).encode()
        elif case["error"] == "record_limit":
            remaining = case["records"] - (calls - 1) * 1000
            page_size = min(remaining, 1000)
            next_cursor = f"page-{calls + 1}" if remaining > page_size else None
            body = json.dumps(
                {"records": [{} for _ in range(page_size)], "next_cursor": next_cursor}
            ).encode()
        elif case["error"] == "body_limit":
            body = b"x" * case["bytes"]
        else:
            raise AssertionError(f"unknown pagination vector error {case['error']!r}")
        return FakeResponse(body)

    monkeypatch.setattr(audit_registry, "_open_registry_request", fake_urlopen)
    expected = {
        "pagination_cycle": "repeated a pagination cursor",
        "invalid_cursor": "next_cursor",
        "record_limit": "10000-record limit",
        "body_limit": "exceeds 16 MiB",
    }[case["error"]]
    with pytest.raises(audit_registry.RegistryError, match=expected):
        audit_registry._http_get_records("https://registry.example/v1/records?limit=1000")


@pytest.mark.parametrize("case", REGISTRY_CLIENT_VECTOR.get("rollback_state_cases", []))
def test_registry_client_rollback_state_vectors(case: dict[str, Any], tmp_path: Path) -> None:
    private, pin = _conformance_key()
    registry_url = "https://registry.example/state"
    registry = RegistryConfig("state-case", registry_url, (pin,))
    snapshot = _sign_conformance_snapshot(private, _conformance_snapshot(8, "a" * 64))
    state_dir = tmp_path / "state"
    now = audit_registry._parse_iso8601("2026-07-13T01:00:00Z")
    assert now is not None
    if case["state"] == "unavailable":
        state_dir.write_text("not a directory", encoding="utf-8")
    if case["state"] in {"malformed", "deleted"}:
        unavailable, warnings = audit_registry.check_snapshots(
            (registry,), state_dir, fetch_snapshot=lambda _url: snapshot, now=now
        )
        assert unavailable == set()
        assert warnings == []
        state_files = list(state_dir.glob("snapshot-*.json"))
        assert len(state_files) == 1
        if case["state"] == "malformed":
            state_files[0].write_text("{broken", encoding="utf-8")
        else:
            state_files[0].unlink()
    unavailable, _ = audit_registry.check_snapshots(
        (registry,), state_dir, fetch_snapshot=lambda _url: snapshot, now=now
    )
    assert (registry_url not in unavailable) is case["accepted"]
