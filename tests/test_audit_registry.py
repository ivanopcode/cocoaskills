from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from csk import _ed25519, audit_registry
from csk.config import RegistryConfig


def _make_key() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pinned = "ed25519:" + base64.b64encode(raw).decode("ascii")
    return priv, pinned


def _sign_record(priv: Ed25519PrivateKey, body: dict[str, Any], key_id: str = "k1") -> dict[str, Any]:
    message = audit_registry.canonical_bytes(body)
    signature = priv.sign(message)
    record = dict(body)
    record["sig"] = {
        "key_id": key_id,
        "algorithm": "ed25519",
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    return record


def _record_body(status: str = "audited", **overrides: Any) -> dict[str, Any]:
    body = {
        "schema_version": 1,
        "name": "skill-tracker",
        "source_identity": "gitlab.example.com/skills/skill-tracker",
        "commit": "8c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d",
        "content_sha256": "sha256:1f2e3d",
        "status": status,
        "audit": {"ruleset_version": "csk-audit/1"},
    }
    body.update(overrides)
    return body


def test_vendored_verify_accepts_valid_signature():
    priv, pinned = _make_key()
    record = _sign_record(priv, _record_body())
    message = audit_registry.canonical_bytes(record)
    signature = base64.b64decode(record["sig"]["signature"])
    public_key = audit_registry.parse_public_key(pinned)
    assert _ed25519.verify(public_key, message, signature)


def test_parse_public_key_roundtrip_and_errors():
    _, pinned = _make_key()
    assert len(audit_registry.parse_public_key(pinned)) == 32
    with pytest.raises(audit_registry.RegistryError):
        audit_registry.parse_public_key("ed25519:not-base64!!")
    with pytest.raises(audit_registry.RegistryError):
        audit_registry.parse_public_key("ed25519:" + base64.b64encode(b"short").decode())


def test_verify_record_rejects_tampered_body():
    priv, pinned = _make_key()
    record = _sign_record(priv, _record_body())
    parsed = audit_registry.parse_record(record)
    assert audit_registry.verify_record(parsed, (pinned,))

    tampered = dict(record)
    tampered["content_sha256"] = "sha256:evil"
    assert not audit_registry.verify_record(audit_registry.parse_record(tampered), (pinned,))


def test_verify_record_rejects_wrong_key():
    priv, _ = _make_key()
    _, other_pinned = _make_key()
    record = _sign_record(priv, _record_body())
    assert not audit_registry.verify_record(audit_registry.parse_record(record), (other_pinned,))


def _fetch_from(records: list[dict[str, Any]]) -> audit_registry.FetchFn:
    def fetch(url: str, source_identity: str, commit: str, content_sha256: str) -> list[dict[str, Any]]:
        return records

    return fetch


ARTIFACT = {
    "source_identity": "gitlab.example.com/skills/skill-tracker",
    "commit": "8c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d",
    "content_sha256": "sha256:1f2e3d",
}


def test_resolve_audited_authorizes():
    priv, pinned = _make_key()
    registry = RegistryConfig(name="central", url="https://r.example", public_keys=(pinned,))
    record = _sign_record(priv, _record_body("audited"))
    resolution = audit_registry.resolve((registry,), fetch=_fetch_from([record]), **ARTIFACT)
    assert resolution.result == audit_registry.RESULT_AUDITED
    assert resolution.attestation is not None
    assert resolution.attestation.registry == "central"


def test_resolve_revoked_wins_over_audited():
    priv_a, pinned_a = _make_key()
    priv_b, pinned_b = _make_key()
    audited = RegistryConfig(name="audited-reg", url="https://a.example", public_keys=(pinned_a,))
    revoking = RegistryConfig(name="revoking-reg", url="https://b.example", public_keys=(pinned_b,))
    rec_a = _sign_record(priv_a, _record_body("audited"))
    rec_b = _sign_record(priv_b, _record_body("revoked"))

    def fetch(url: str, source_identity: str, commit: str, content_sha256: str) -> list[dict[str, Any]]:
        return [rec_a] if url == "https://a.example" else [rec_b]

    resolution = audit_registry.resolve((audited, revoking), fetch=fetch, **ARTIFACT)
    assert resolution.result == audit_registry.RESULT_REVOKED
    assert resolution.attestation is not None
    assert resolution.attestation.registry == "revoking-reg"


def test_resolve_unknown_when_no_match():
    _, pinned = _make_key()
    registry = RegistryConfig(name="central", url="https://r.example", public_keys=(pinned,))
    resolution = audit_registry.resolve((registry,), fetch=_fetch_from([]), **ARTIFACT)
    assert resolution.result == audit_registry.RESULT_UNKNOWN
    assert resolution.attestation is None


def test_resolve_ignores_unverified_record_with_warning():
    priv, _ = _make_key()
    _, other_pinned = _make_key()
    registry = RegistryConfig(name="central", url="https://r.example", public_keys=(other_pinned,))
    record = _sign_record(priv, _record_body("audited"))
    resolution = audit_registry.resolve((registry,), fetch=_fetch_from([record]), **ARTIFACT)
    assert resolution.result == audit_registry.RESULT_UNKNOWN
    assert any("failed signature verification" in w for w in resolution.warnings)


def test_resolve_warns_on_registry_without_pinned_keys():
    registry = RegistryConfig(name="central", url="https://r.example", public_keys=())
    resolution = audit_registry.resolve((registry,), fetch=_fetch_from([]), **ARTIFACT)
    assert resolution.result == audit_registry.RESULT_UNKNOWN
    assert any("no pinned keys" in w for w in resolution.warnings)


def test_resolve_matches_by_content_hash_across_source():
    priv, pinned = _make_key()
    registry = RegistryConfig(name="central", url="https://r.example", public_keys=(pinned,))
    # Different source_identity and commit, same content hash still matches.
    record = _sign_record(
        priv, _record_body("audited", source_identity="other.example/x", commit="0" * 40)
    )
    resolution = audit_registry.resolve((registry,), fetch=_fetch_from([record]), **ARTIFACT)
    assert resolution.result == audit_registry.RESULT_AUDITED


def test_http_fetch_caches_and_offline_grace(tmp_path):
    calls = {"n": 0}

    payload = json.dumps({"records": [{"name": "x"}]}).encode()

    def fake_get(endpoint: str) -> list[dict[str, Any]]:
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"name": "x"}]
        raise audit_registry.RegistryError("offline")

    import csk.audit_registry as mod

    original = mod._http_get_records
    mod._http_get_records = fake_get  # type: ignore[assignment]
    try:
        fetch = mod.make_http_fetch(tmp_path, ttl_seconds=0, grace_seconds=1000, now=1000.0)
        first = fetch("https://r.example", "s", "c", "h")
        assert first == [{"name": "x"}]
        # ttl=0 forces a refresh; the registry is now offline, grace serves cache.
        second = fetch("https://r.example", "s", "c", "h")
        assert second == [{"name": "x"}]
        assert calls["n"] == 2
    finally:
        mod._http_get_records = original  # type: ignore[assignment]


# --- config parsing and merge ---

from dataclasses import replace  # noqa: E402
from pathlib import Path  # noqa: E402

from csk import config as csk_config  # noqa: E402


def _base_config(tmp_path: Path, extra: dict[str, Any]) -> dict[str, Any]:
    data = {
        "schema_version": 1,
        "skills_root": str(tmp_path / "skills"),
        "projects": {},
    }
    data.update(extra)
    return data


def test_config_parses_audit_registries(tmp_path):
    cfg = csk_config.parse_config(
        _base_config(
            tmp_path,
            {
                "audit_registries": [
                    {"name": "internal", "url": "https://r.example", "public_keys": ["ed25519:AAAA"]}
                ]
            },
        ),
        tmp_path / "config.json",
    )
    assert len(cfg.audit_registries) == 1
    assert cfg.audit_registries[0].name == "internal"
    assert cfg.audit_registries[0].enabled is True


def test_config_rejects_registry_without_http_url(tmp_path):
    with pytest.raises(csk_config.ConfigError, match="url"):
        csk_config.parse_config(
            _base_config(tmp_path, {"audit_registries": [{"name": "x", "url": "ftp://r"}]}),
            tmp_path / "config.json",
        )


def test_config_rejects_duplicate_registry_url(tmp_path):
    with pytest.raises(csk_config.ConfigError, match="duplicate"):
        csk_config.parse_config(
            _base_config(
                tmp_path,
                {
                    "audit_registries": [
                        {"name": "a", "url": "https://r.example"},
                        {"name": "b", "url": "https://r.example"},
                    ]
                },
            ),
            tmp_path / "config.json",
        )


def test_config_roundtrips_registries(tmp_path):
    cfg = csk_config.parse_config(
        _base_config(
            tmp_path,
            {
                "audit_registries": [{"name": "internal", "url": "https://r.example", "public_keys": ["ed25519:AAAA"]}],
                "disable_builtin_registries": True,
            },
        ),
        tmp_path / "config.json",
    )
    csk_config.save_config(cfg)
    reloaded = csk_config.load_config(tmp_path / "config.json")
    assert reloaded.audit_registries[0].url == "https://r.example"
    assert reloaded.disable_builtin_registries is True


def test_trusted_registries_disabled_excludes_inactive(tmp_path):
    cfg = csk_config.parse_config(
        _base_config(
            tmp_path,
            {
                "audit_registries": [
                    {"name": "on", "url": "https://on.example", "public_keys": ["ed25519:AAAA"]},
                    {"name": "off", "url": "https://off.example", "enabled": False},
                ]
            },
        ),
        tmp_path / "config.json",
    )
    trusted = cfg.trusted_registries()
    assert [entry.name for entry in trusted] == ["on"]


# --- installer end-to-end ---

from conftest import make_config, make_project, make_skill_repo, write_skillfile  # noqa: E402
from csk import audit_registry as registry_mod, installer  # noqa: E402


def _install_with_registry(tmp_path, skills_root, csk_home, monkeypatch, *, status: str):
    priv, pinned = _make_key()
    project = make_project(tmp_path)
    _, commit = make_skill_repo(skills_root, "skill-tracker", tag="v1")
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [
                {
                    "name": "skill-tracker",
                    "git": "git@gitlab.example.com:skills/skill-tracker.git",
                    "tag": "v1",
                }
            ],
        },
    )
    record = _sign_record(
        priv,
        _record_body(
            status,
            source_identity="gitlab.example.com/skills/skill-tracker",
            commit=commit,
            content_sha256="sha256:ignored",
        ),
    )

    def fake_make_fetch(cache_dir, **kwargs):
        def fetch(url, source_identity, commit_arg, content_sha256):
            return [record]

        return fetch

    monkeypatch.setattr(registry_mod, "make_http_fetch", fake_make_fetch)

    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    cfg = replace(
        cfg,
        audit_registries=(RegistryConfig(name="central", url="https://r.example", public_keys=(pinned,)),),
    )
    return project, installer.install(cfg)[0]


def test_revoked_skill_blocks_install(tmp_path, skills_root, csk_home, monkeypatch):
    _, result = _install_with_registry(tmp_path, skills_root, csk_home, monkeypatch, status="revoked")
    assert result.errors
    assert "revoked by central" in result.errors[0]


def test_audited_skill_records_attestation(tmp_path, skills_root, csk_home, monkeypatch):
    project, result = _install_with_registry(tmp_path, skills_root, csk_home, monkeypatch, status="audited")
    assert not result.errors, result.errors
    marker = json.loads(
        (project / ".agents" / "skills" / "skill-tracker" / ".csk-install.json").read_text(encoding="utf-8")
    )
    assert marker["attestation"]["registry"] == "central"
    assert marker["attestation"]["status"] == "audited"


# --- strict policy and attest ---

from csk import attest as attest_mod  # noqa: E402
from csk.config import AuditConfig  # noqa: E402


def test_strict_policy_fails_unknown_skill(tmp_path, skills_root, csk_home, monkeypatch):
    _, pinned = _make_key()
    project = make_project(tmp_path)
    make_skill_repo(skills_root, "skill-tracker", tag="v1")
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [
                {"name": "skill-tracker", "git": "git@gitlab.example.com:skills/skill-tracker.git", "tag": "v1"}
            ],
        },
    )

    def fake_make_fetch(cache_dir, **kwargs):
        def fetch(url, source_identity, commit_arg, content_sha256):
            return []

        return fetch

    monkeypatch.setattr(registry_mod, "make_http_fetch", fake_make_fetch)
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    cfg = replace(
        cfg,
        audit=AuditConfig(registry_policy="strict"),
        audit_registries=(RegistryConfig(name="central", url="https://r.example", public_keys=(pinned,)),),
    )
    result = installer.install(cfg)[0]
    assert result.errors
    assert "not audited by any trusted registry" in result.errors[0]


def test_attest_detects_post_install_revocation(tmp_path, skills_root, csk_home, monkeypatch):
    project, result = _install_with_registry(tmp_path, skills_root, csk_home, monkeypatch, status="audited")
    assert not result.errors, result.errors

    # A revocation is issued after install; attest re-checks the marker.
    priv2, pinned2 = _make_key()
    marker = json.loads(
        (project / ".agents" / "skills" / "skill-tracker" / ".csk-install.json").read_text(encoding="utf-8")
    )
    revoked = _sign_record(
        priv2,
        _record_body(
            "revoked",
            source_identity="gitlab.example.com/skills/skill-tracker",
            commit=marker["commit"],
            content_sha256=marker["content_sha256"],
        ),
    )

    def fake_make_fetch(cache_dir, **kwargs):
        def fetch(url, source_identity, commit_arg, content_sha256):
            return [revoked]

        return fetch

    monkeypatch.setattr(registry_mod, "make_http_fetch", fake_make_fetch)
    cfg = make_config(csk_home, skills_root, project, agents=["claude_code"])
    cfg = replace(
        cfg,
        audit_registries=(RegistryConfig(name="central", url="https://r.example", public_keys=(pinned2,)),),
    )
    results = attest_mod.attest_projects(cfg, alias="app")
    tracker = [r for r in results if r.skill == "skill-tracker"]
    assert tracker and tracker[0].result == registry_mod.RESULT_REVOKED
    assert attest_mod.has_revocation(results)
