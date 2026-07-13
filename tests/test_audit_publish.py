from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from csk import audit_registry, cli, config as csk_config
from conftest import make_config, make_project


def _configured(tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch) -> None:
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    csk_config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)


def _record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "name": "skill-x",
        "source_identity": "git.example.com/skills/skill-x",
        "commit": "ab" * 20,
        "content_sha256": "sha256:" + "cd" * 32,
        "status": "audited",
        "audit": {},
        "sig": {
            "algorithm": "ed25519",
            "key_id": "0123456789abcdef",
            "signature": "A" * 86 + "==",
        },
    }


def test_publish_posts_record(tmp_path, skills_root, csk_home, monkeypatch, capsys):
    _configured(tmp_path, skills_root, csk_home, monkeypatch)
    record = tmp_path / "record.json"
    record.write_text(json.dumps(_record()), encoding="utf-8")

    captured = {}

    class FakeResponse:
        status = 201
        headers = {"Content-Type": "application/json; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, size=-1):
            return json.dumps({"seq": 1, "entry_hash": "de" * 32}).encode()

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["auth"] = request.headers.get("Authorization")
        captured["idempotency"] = request.get_header("Idempotency-key")
        captured["data"] = request.data
        return FakeResponse()

    monkeypatch.setattr(audit_registry, "_open_registry_request", fake_urlopen)

    code = cli.main(
        ["audit", "--publish", str(record), "--registry", "https://r.example", "--token", "t0ken"]
    )
    assert code == 0
    assert captured["url"] == "https://r.example/v1/records"
    assert captured["auth"] == "Bearer t0ken"
    assert json.loads(captured["data"])["name"] == "skill-x"
    assert captured["idempotency"] == hashlib.sha256(
        audit_registry.canonical_bytes(json.loads(captured["data"]))
    ).hexdigest()
    assert "dedede" in capsys.readouterr().out


def test_publish_reads_token_from_env(tmp_path, skills_root, csk_home, monkeypatch):
    _configured(tmp_path, skills_root, csk_home, monkeypatch)
    monkeypatch.setenv("CSK_REGISTRY_TOKEN", "env-token")
    record = tmp_path / "record.json"
    record.write_text(json.dumps(_record()), encoding="utf-8")

    captured = {}

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, size=-1):
            return json.dumps({"seq": 1, "entry_hash": "de" * 32}).encode()

    def fake_urlopen(request, timeout=0):
        captured["auth"] = request.headers.get("Authorization")
        return FakeResponse()

    monkeypatch.setattr(audit_registry, "_open_registry_request", fake_urlopen)
    assert cli.main(["audit", "--publish", str(record), "--registry", "https://r.example"]) == 0
    assert captured["auth"] == "Bearer env-token"


def test_publish_requires_registry(tmp_path, skills_root, csk_home, monkeypatch):
    _configured(tmp_path, skills_root, csk_home, monkeypatch)
    record = tmp_path / "record.json"
    record.write_text("{}", encoding="utf-8")
    # No --registry, exits configuration error.
    assert cli.main(["audit", "--publish", str(record), "--token", "t"]) == cli.EXIT_CONFIG


def test_publish_requires_token(tmp_path, skills_root, csk_home, monkeypatch):
    _configured(tmp_path, skills_root, csk_home, monkeypatch)
    monkeypatch.delenv("CSK_REGISTRY_TOKEN", raising=False)
    record = tmp_path / "record.json"
    record.write_text("{}", encoding="utf-8")
    assert cli.main(["audit", "--publish", str(record), "--registry", "https://r.example"]) == cli.EXIT_CONFIG
