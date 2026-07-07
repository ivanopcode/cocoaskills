from __future__ import annotations

import json
from pathlib import Path

import pytest

from csk import config as csk_config


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _user_config(tmp_path: Path, extra: dict) -> Path:
    path = tmp_path / "user" / "config.json"
    base = {"schema_version": 1, "skills_root": str(tmp_path / "skills"), "projects": {}}
    base.update(extra)
    _write(path, base)
    return path


def test_locked_key_overrides_user_value(tmp_path, monkeypatch):
    system_path = tmp_path / "system.json"
    _write(
        system_path,
        {
            "schema_version": 1,
            "locked": ["allowed_sources"],
            "allowed_sources": ["gitlab.corp/skills/"],
        },
    )
    user_path = _user_config(tmp_path, {"allowed_sources": ["evil.example/"]})
    monkeypatch.setenv("CSK_SYSTEM_CONFIG", str(system_path))

    cfg = csk_config.load_config(user_path)
    assert cfg.allowed_sources == ("gitlab.corp/skills/",)


def test_locked_override_attempt_warns(tmp_path, monkeypatch, capsys):
    system_path = tmp_path / "system.json"
    _write(
        system_path,
        {"schema_version": 1, "locked": ["disable_builtin_registries"], "disable_builtin_registries": True},
    )
    user_path = _user_config(tmp_path, {"disable_builtin_registries": False})
    monkeypatch.setenv("CSK_SYSTEM_CONFIG", str(system_path))

    cfg = csk_config.load_config(user_path)
    assert cfg.disable_builtin_registries is True
    assert "locked" in capsys.readouterr().err


def test_unlocked_system_key_is_a_default(tmp_path, monkeypatch):
    system_path = tmp_path / "system.json"
    _write(
        system_path,
        {"schema_version": 1, "audit_registries": [{"name": "corp", "url": "https://corp.example"}]},
    )
    # User does not set audit_registries, so the system default applies.
    user_path = _user_config(tmp_path, {})
    monkeypatch.setenv("CSK_SYSTEM_CONFIG", str(system_path))

    cfg = csk_config.load_config(user_path)
    assert [r.name for r in cfg.audit_registries] == ["corp"]


def test_unlocked_system_key_yields_to_user(tmp_path, monkeypatch):
    system_path = tmp_path / "system.json"
    _write(
        system_path,
        {"schema_version": 1, "audit_registries": [{"name": "corp", "url": "https://corp.example"}]},
    )
    user_path = _user_config(
        tmp_path, {"audit_registries": [{"name": "mine", "url": "https://mine.example"}]}
    )
    monkeypatch.setenv("CSK_SYSTEM_CONFIG", str(system_path))

    cfg = csk_config.load_config(user_path)
    assert [r.name for r in cfg.audit_registries] == ["mine"]


def test_locked_key_must_be_set_in_system_config(tmp_path, monkeypatch):
    system_path = tmp_path / "system.json"
    _write(system_path, {"schema_version": 1, "locked": ["allowed_sources"]})
    user_path = _user_config(tmp_path, {})
    monkeypatch.setenv("CSK_SYSTEM_CONFIG", str(system_path))

    with pytest.raises(csk_config.ConfigError, match="locks 'allowed_sources' but does not set it"):
        csk_config.load_config(user_path)


def test_no_system_config_is_transparent(tmp_path, monkeypatch):
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)
    user_path = _user_config(tmp_path, {"allowed_sources": ["mine.example/"]})
    # Point the system path at a nonexistent file via env to avoid reading /etc.
    monkeypatch.setenv("CSK_SYSTEM_CONFIG", str(tmp_path / "absent.json"))
    cfg = csk_config.load_config(user_path)
    assert cfg.allowed_sources == ("mine.example/",)


def test_registry_policy_parsing_and_roundtrip(tmp_path):
    cfg = csk_config.parse_config(
        {
            "schema_version": 1,
            "skills_root": str(tmp_path / "skills"),
            "projects": {},
            "audit": {"registry_policy": "strict"},
        },
        tmp_path / "config.json",
    )
    assert cfg.audit.registry_policy == "strict"
    csk_config.save_config(cfg)
    reloaded = csk_config.load_config(tmp_path / "config.json")
    assert reloaded.audit.registry_policy == "strict"


def test_registry_policy_rejects_unknown(tmp_path):
    with pytest.raises(csk_config.ConfigError, match="registry_policy"):
        csk_config.parse_config(
            {
                "schema_version": 1,
                "skills_root": str(tmp_path / "skills"),
                "projects": {},
                "audit": {"registry_policy": "loose"},
            },
            tmp_path / "config.json",
        )
