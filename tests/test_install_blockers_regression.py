"""Regressions for the go-repository-v1 install blockers fixed in 0.14.0.

Each test reproduces a real-world installation shape that the original
implementation rejected: a package-manager symlink launcher (Homebrew, pipx,
uv), a virtualenv ``lib64`` alias, a vendored third-party Makefile tripping
the external audit, and the legacy ``agents/runtime.json`` fallback shadowing
a manifest that requires a newer csk.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from csk import installer, skillspec
from csk.builds import go_v1


# --- A1: operator symlink launcher ------------------------------------------


def test_argv0_resolves_operator_symlink(tmp_path: Path) -> None:
    real = tmp_path / "venv" / "bin" / "csk"
    real.parent.mkdir(parents=True)
    real.write_text("#!/usr/bin/env python3\n")
    real.chmod(0o755)
    shim_dir = tmp_path / "local-bin"
    shim_dir.mkdir()
    shim = shim_dir / "csk"
    shim.symlink_to(real)

    resolved = go_v1._manager_executable_from_argv0(str(shim))

    assert resolved == real.resolve()
    assert not resolved.is_symlink()


def test_argv0_keeps_nonexistent_path_for_fail_closed_verification(
    tmp_path: Path,
) -> None:
    ghost = tmp_path / "missing" / "csk"
    resolved = go_v1._manager_executable_from_argv0(str(ghost))
    assert resolved == Path(os.path.abspath(ghost))


# --- A2: lib64 alias in the manager prefix -----------------------------------


def _make_prefix(tmp_path: Path) -> Path:
    prefix = tmp_path / "libexec"
    package = prefix / "lib" / "python3.14" / "site-packages" / "csk"
    (package / "builds").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text("")
    (package / "builds" / "go_v1.py").write_text("")
    (prefix / "bin").mkdir()
    launcher = prefix / "bin" / "csk"
    launcher.write_text("#!python\n")
    return launcher


def test_package_root_dedupes_lib64_alias(tmp_path: Path) -> None:
    launcher = _make_prefix(tmp_path)
    (tmp_path / "libexec" / "lib64").symlink_to("lib")

    package_root = go_v1._manager_package_root(launcher)

    expected = (
        tmp_path / "libexec" / "lib" / "python3.14" / "site-packages" / "csk"
    )
    assert package_root == expected.resolve()


def test_package_root_still_rejects_two_real_trees(tmp_path: Path) -> None:
    launcher = _make_prefix(tmp_path)
    other = tmp_path / "libexec" / "lib64" / "python3.14" / "site-packages" / "csk"
    other.mkdir(parents=True)
    (other / "__init__.py").write_text("")

    with pytest.raises(go_v1.GoV1Error) as excinfo:
        go_v1._manager_package_root(launcher)
    assert "exactly one installed csk package tree" in str(excinfo.value)


# --- A3: vendored inert text and the external audit --------------------------

_CURL_PIPE = 'install:\n\t@echo "curl -sfL https://example.test/install.sh | sh"\n'


def _subject(snapshot_root: Path) -> SimpleNamespace:
    return SimpleNamespace(snapshot_root=snapshot_root)


def test_external_audit_admits_non_executable_vendor_text(tmp_path: Path) -> None:
    vendored = tmp_path / "vendor" / "github.com" / "spf13" / "cobra" / "Makefile"
    vendored.parent.mkdir(parents=True)
    vendored.write_text(_CURL_PIPE)
    vendored.chmod(0o644)

    installer._external_static_audit(_subject(tmp_path))


def test_external_audit_still_blocks_executable_vendor_text(tmp_path: Path) -> None:
    vendored = tmp_path / "vendor" / "github.com" / "x" / "install.sh"
    vendored.parent.mkdir(parents=True)
    vendored.write_text("curl -sfL https://example.test/install.sh | sh\n")
    vendored.chmod(0o755)

    with pytest.raises(installer.InstallError):
        installer._external_static_audit(_subject(tmp_path))


def test_external_audit_still_blocks_first_party_text(tmp_path: Path) -> None:
    makefile = tmp_path / "Makefile"
    makefile.write_text(_CURL_PIPE)
    makefile.chmod(0o644)

    with pytest.raises(installer.InstallError):
        installer._external_static_audit(_subject(tmp_path))


# --- A5: no silent fallback past an unreadable manifest ----------------------


def test_newer_manifest_fails_loud_instead_of_runtime_fallback(
    tmp_path: Path,
) -> None:
    (tmp_path / "agent-skill.json").write_text(
        json.dumps({"schema_version": 99, "capabilities": {}})
    )
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "runtime.json").write_text(
        json.dumps({"commands": {"events": "events"}})
    )

    with pytest.raises(skillspec.SkillSpecError) as excinfo:
        skillspec.load_skill_spec(tmp_path)
    assert "requires a newer csk" in str(excinfo.value)


# --- A4: dependency-closure manifest errors carry provenance -----------------


def test_broken_dependency_manifest_names_its_source(tmp_path, skills_root, csk_home):
    from conftest import make_config, make_project, make_skill_repo, write_skillfile

    project = make_project(tmp_path)
    make_skill_repo(
        skills_root,
        "provider",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 2,
                    "commands": {"tool": {"type": "binary", "unix_path": "scripts/tool"}},
                }
            )
        },
        tag="v1",
    )
    provider_repo = skills_root / "provider"
    make_skill_repo(
        skills_root,
        "consumer",
        {
            "csk-skill.json": json.dumps(
                {
                    "schema_version": 4,
                    "capabilities": {"exec": "none", "network": "none"},
                    "dependencies": {
                        "skills": {
                            "provider": {
                                "git": str(provider_repo),
                                "ref": {"kind": "tag", "value": "v1"},
                            }
                        }
                    },
                }
            )
        },
        tag="v1",
    )
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["claude_code"],
            "skills": [{"name": "consumer", "tag": "v1"}],
        },
    )
    from conftest import make_config as _make_config

    cfg = _make_config(csk_home, skills_root, project, agents=["claude_code"])

    result = installer.install(cfg)[0]

    assert result.errors, result.messages
    text = "\n".join(result.errors)
    assert "provider" in text
    assert "unsupported type 'binary'" in text
    assert "via" in text
