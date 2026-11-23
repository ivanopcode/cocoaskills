"""Regression tests for naming the canonical repository on private Git failures."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import make_config, make_project, write_skillfile
from test_git_admission_ssh import _private_paths, _tool
from test_install import _stub_trusted_toolchain
from test_install_external_repository import (
    _external_repository,
    _git_tool,
    _skill_repository,
)
from test_source_diagnostics import _attempt

from csk import cli, git_admission, installer, locking
from csk.build_repository import LockedCommit
from csk.build_ssh import BuildSSHRule
from csk.sources import _selection_fs, diagnostics, transport


def _require_selection() -> None:
    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cfg: Any,
    args: list[str],
) -> tuple[int, str, str]:
    monkeypatch.setattr(cli.config, "load_config", lambda: cfg)
    code = cli.main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("source_mode", ["resolve-tag", "acquire-revision"])
@pytest.mark.parametrize(
    "failure_class",
    ["ssh-auth-rejected", "auth-unavailable", "unclassified"],
)
@pytest.mark.parametrize("verbose", [False, True])
def test_cli_skill_source_private_git_failure_names_repository_and_next_steps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills_root: Path,
    csk_home: Path,
    capsys: pytest.CaptureFixture[str],
    source_mode: str,
    failure_class: str,
    verbose: bool,
) -> None:
    # Failing private Git skill-source fetches render the canonical repository identity, failure class, and fixed doctor/HTTPS next steps.
    _require_selection()
    project = make_project(tmp_path)
    source_spec: dict[str, object] = {"git": "ssh://git@example.org/team/kit.git"}
    if source_mode == "resolve-tag":
        source_spec["tag"] = "v1.0.0"
    else:
        source_spec["revision"] = "1" * 40
    write_skillfile(
        project,
        {
            "schema_version": 2,
            "sources": {"kit": source_spec},
            "skills": [{"name": "demo", "from": "kit", "directory": "."}],
        },
    )
    cfg = make_config(csk_home, skills_root, project, agents=["codex_cli"])
    cfg = replace(cfg, build_ssh=(BuildSSHRule(scope="example.org/team", agent="auto"),))
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text("", encoding="utf-8")
    tool = replace(_git_tool(), ssh=fake_ssh)
    monkeypatch.setattr(installer, "_external_git_tool", lambda *_a, **_kw: tool)
    monkeypatch.setattr(
        installer,
        "_endpoint_ssh_credentials",
        lambda *_a, **_kw: git_admission.OperatorSSHCredentials(),
    )

    def fail_git(*_args: object, **_kwargs: object) -> Any:
        raise git_admission.GitAdmissionError(
            git_admission.SOURCE_UNAVAILABLE,
            "private Git operation failed",
            failure_class=failure_class,
            hints=("ssh-signing-failed",),
        )

    if source_mode == "resolve-tag":
        monkeypatch.setattr(git_admission, "resolve_network_ref", fail_git)
    else:
        monkeypatch.setattr(git_admission, "acquire_network", fail_git)

    args = ["install", "app"] + (["--verbose"] if verbose else [])
    code, _stdout, stderr = _run_cli(monkeypatch, capsys, cfg, args)

    assert code == cli.EXIT_PARTIAL_FAIL
    assert "example.org/team/kit" in stderr, stderr
    assert failure_class in stderr, stderr
    assert "next:" in stderr, stderr
    assert "csk config build-ssh doctor" in stderr, stderr
    assert "csk config build-https" in stderr, stderr
    assert "hint:" in stderr and "SSH reported a signing failure" in stderr, stderr


@pytest.mark.skipif(
    not installer.supports_external_builds(),
    reason="go-repository-v1 is qualified only on macOS and Windows",
)
@pytest.mark.parametrize(
    "failure_class",
    ["ssh-auth-rejected", "auth-unavailable", "unclassified"],
)
@pytest.mark.parametrize("verbose", [False, True])
def test_cli_external_build_source_private_git_failure_names_repository_and_next_steps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure_class: str,
    verbose: bool,
) -> None:
    # Failing external build-source private Git fetches render the canonical repository identity, build subject, failure class, and fixed doctor/HTTPS next steps.
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True)
    home = tmp_path / ".cocoaskills"
    locking.provision_new_manager_home(home)
    project = make_project(tmp_path)
    _external, commit = _external_repository(tmp_path)
    _skill_repository(skills_root, commit, git="ssh://git@example.org/team/tools.git")
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [{"name": "external-skill", "tag": "v1"}],
        },
    )
    cfg = make_config(home, skills_root, project, agents=["codex_cli"])
    cfg = replace(cfg, build_ssh=(BuildSSHRule(scope="example.org/team", agent="auto"),))
    _stub_trusted_toolchain(monkeypatch)
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text("", encoding="utf-8")
    tool = replace(_git_tool(), ssh=fake_ssh)
    monkeypatch.setattr(installer, "_external_git_tool", lambda *_a, **_kw: tool)
    monkeypatch.setattr(
        installer,
        "_rule_credentials",
        lambda *_a, **_kw: git_admission.OperatorSSHCredentials(),
    )

    def fail_acquire(*_args: object, **_kwargs: object) -> Any:
        raise git_admission.GitAdmissionError(
            git_admission.SOURCE_UNAVAILABLE,
            "private Git operation failed",
            failure_class=failure_class,
            hints=("ssh-signing-failed",),
        )

    monkeypatch.setattr(git_admission, "acquire_network", fail_acquire)

    args = ["install", "app"] + (["--verbose"] if verbose else [])
    code, _stdout, stderr = _run_cli(monkeypatch, capsys, cfg, args)

    assert code == cli.EXIT_PARTIAL_FAIL
    assert "example.org/team/tools" in stderr, stderr
    assert "command 'external-tool' of skill 'external-skill'" in stderr, stderr
    assert failure_class in stderr, stderr
    assert "next:" in stderr, stderr
    assert "csk config build-ssh doctor" in stderr, stderr
    assert "csk config build-https" in stderr, stderr
    assert "hint:" in stderr and "SSH reported a signing failure" in stderr, stderr


@pytest.mark.parametrize(
    "failure_class",
    ["ssh-auth-rejected", "auth-unavailable", "unclassified"],
)
def test_external_build_transport_lane_names_repository_on_all_platforms(
    failure_class: str,
) -> None:
    # Portable seam: the external-build transport lane attaches the canonical repository identity so failure_text renders identity, class, and next steps on every OS.
    plan = transport.plan_attempts(
        "example.org/team/tools",
        "ssh://git@example.org/team/tools.git",
    )

    def fail_attempt(**_kwargs: object) -> Any:
        raise git_admission.GitAdmissionError(
            git_admission.SOURCE_UNAVAILABLE,
            "private Git operation failed",
            failure_class=failure_class,
        )

    with pytest.raises(git_admission.GitAdmissionError) as caught:
        transport.acquire_plan(
            plan,
            LockedCommit("sha1", "0" * 40),
            attempt=fail_attempt,
            lane=transport.LANE_EXTERNAL_BUILD,
        )

    for verbose in (False, True):
        rendered = installer.failure_text(caught.value, verbose=verbose)
        assert "example.org/team/tools" in rendered, rendered
        assert failure_class in rendered, rendered
        assert "next:" in rendered, rendered
        assert "csk config build-ssh doctor" in rendered, rendered
        assert "csk config build-https" in rendered, rendered


@pytest.mark.parametrize("operation", ["fetch", "ls-remote"])
@pytest.mark.parametrize(
    ("terminal_stderr", "expected_class"),
    [
        ("git@example.org: Permission denied (publickey).", "ssh-auth-rejected"),
        ("fatal: unexpected private transport error", "unclassified"),
    ],
)
def test_subprocess_private_git_failure_names_repository_and_hides_raw_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    terminal_stderr: str,
    expected_class: str,
) -> None:
    # Subprocess private Git failures name the canonical repository identity and fixed hints while keeping raw child stderr hidden.
    raw_output = (
        'RAW_STDERR_CANARY_TOKEN\n'
        'sign_and_send_pubkey: signing failed for ED25519 "PRIVATE_KEY_CANARY" from agent: refused\n'
        + terminal_stderr
    ).encode("utf-8")
    tool = _tool(tmp_path / "ssh", git_admission.OperatorSSHCredentials())
    paths = _private_paths(tmp_path / "private")
    monkeypatch.setattr(git_admission, "_operation_trust_environment", lambda *_a: {})
    monkeypatch.setattr(git_admission.git_trust, "pinned_command", lambda argv, _env: argv)

    def fail_run(*_args: object, **_kwargs: object) -> Any:
        raise subprocess.CalledProcessError(128, "git", stderr=raw_output)

    monkeypatch.setattr(git_admission.subprocess, "run", fail_run)

    with pytest.raises(git_admission.GitAdmissionError) as caught:
        if operation == "ls-remote":
            git_admission._run_git_ls_remote(
                tool,
                paths,
                {},
                ("ls-remote",),
                limits=git_admission.Limits(),
                repository="example.org/team/kit",
            )
        else:
            git_admission._run_git(
                tool,
                paths,
                {},
                "fetch",
                limits=git_admission.Limits(),
                repository="example.org/team/kit",
            )

    rendered = installer.failure_text(caught.value, verbose=True)
    assert "example.org/team/kit" in rendered, rendered
    assert expected_class in rendered, rendered
    assert "csk config build-ssh doctor" in rendered, rendered
    assert "csk config build-https" in rendered, rendered
    assert "hint: SSH reported a signing failure" in rendered, rendered
    assert "RAW_STDERR_CANARY_TOKEN" not in rendered, rendered
    assert "PRIVATE_KEY_CANARY" not in rendered, rendered
    assert "unexpected private transport error" not in rendered, rendered


@pytest.mark.parametrize(
    ("credentialed_url", "secrets", "redactable_in_free_text"),
    [
        (
            "https://bot-user:TOP_SECRET_TOKEN@example.org/team/kit.git",
            ("bot-user", "TOP_SECRET_TOKEN"),
            True,
        ),
        (
            "https://bot-user:TOP_SECRET_TOKEN@example.org/team/kit.git?private_token=QUERY_SECRET",
            ("bot-user", "TOP_SECRET_TOKEN", "QUERY_SECRET"),
            True,
        ),
        (
            "ssh://token-user:SSH_SECRET_VALUE@example.org/team/kit.git",
            ("token-user", "SSH_SECRET_VALUE"),
            True,
        ),
        (
            "bot-user:SCP_SECRET_TOKEN@example.org:team/kit.git",
            ("bot-user", "SCP_SECRET_TOKEN"),
            True,
        ),
        (
            "ghp_SCP_SECRET_TOKEN@example.org:team/kit.git",
            ("ghp_SCP_SECRET_TOKEN",),
            False,
        ),
    ],
)
def test_credential_bearing_url_redacts_secret_while_keeping_canonical_identity(
    credentialed_url: str,
    secrets: tuple[str, ...],
    redactable_in_free_text: bool,
) -> None:
    # Negative + positive control: credential-bearing URLs render without userinfo or query tokens while still naming the canonical repository identity.
    # 1) TransportResolutionError (ssh-auth-rejected) path drops every URL userinfo/query form structurally.
    resolution_error = transport.TransportResolutionError(
        (_attempt(1, "ssh-auth-rejected", credentialed_url),),
        identity="example.org/team/kit",
    )
    rendered_resolution = installer.failure_text(resolution_error, verbose=True)
    for secret in secrets:
        assert secret not in rendered_resolution, (secret, rendered_resolution)
    assert "example.org/team/kit" in rendered_resolution, rendered_resolution
    assert "ssh-auth-rejected" in rendered_resolution, rendered_resolution
    assert "csk config build-ssh doctor" in rendered_resolution, rendered_resolution
    assert "csk config build-https" in rendered_resolution, rendered_resolution

    # 2) Unclassified transport path: production never echoes the endpoint URL, and any free-text userinfo/query credential is redacted.
    plan = transport.plan_attempts(
        "example.org/team/kit",
        "ghp_SCP_SECRET_TOKEN@example.org:team/kit.git"
        if not redactable_in_free_text
        else "ssh://git@example.org/team/kit.git",
    )

    def fail_unclassified(**_kwargs: object) -> Any:
        detail = (
            f"private Git operation failed against {credentialed_url}"
            if redactable_in_free_text
            else "private Git operation failed"
        )
        raise git_admission.GitAdmissionError(
            git_admission.SOURCE_UNAVAILABLE,
            detail,
            failure_class="unclassified",
        )

    with pytest.raises(git_admission.GitAdmissionError) as caught:
        transport.acquire_plan(
            plan,
            LockedCommit("sha1", "0" * 40),
            attempt=fail_unclassified,
        )
    rendered_unclassified = installer.failure_text(caught.value, verbose=True)
    for secret in secrets:
        assert secret not in rendered_unclassified, (secret, rendered_unclassified)
    assert "example.org/team/kit" in rendered_unclassified, rendered_unclassified
    assert "unclassified" in rendered_unclassified, rendered_unclassified
    assert "csk config build-ssh doctor" in rendered_unclassified, rendered_unclassified
    assert "csk config build-https" in rendered_unclassified, rendered_unclassified


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "https://mirror.example.net/kit.git",
        "ssh://git@example.org:2222/team/kit.git",
    ],
)
def test_mirror_and_explicit_port_transport_failures_still_name_canonical_identity(
    endpoint_url: str,
) -> None:
    # A mirror endpoint or explicit port still renders the canonical repository identity alongside the endpoint subject.
    error = transport.TransportResolutionError(
        (_attempt(1, "ssh-auth-rejected", endpoint_url),),
        identity="example.org/team/kit",
    )
    rendered = installer.failure_text(error)
    assert "example.org/team/kit" in rendered, rendered
    assert "ssh-auth-rejected" in rendered, rendered


def test_next_steps_for_ssh_auth_and_unclassified_mention_doctor_and_https() -> None:
    # Fixed next steps for ssh-auth-rejected, auth-unavailable and unclassified always mention csk config build-ssh doctor and csk config build-https, never raw hints.
    for failure_class in ("ssh-auth-rejected", "auth-unavailable", "unclassified"):
        rendered = diagnostics.transport_next_steps(failure_class, ("UNTRUSTED_RAW_HINT",))
        assert rendered.startswith("next: "), rendered
        assert "csk config build-ssh doctor" in rendered, (failure_class, rendered)
        assert "csk config build-https" in rendered, (failure_class, rendered)
        assert "UNTRUSTED_RAW_HINT" not in rendered, rendered

    # Positive/negative control: unrelated classes keep their own fixed advice rather than blanket SSH text.
    dns_rendered = diagnostics.transport_next_steps("dns", ("UNTRUSTED_RAW_HINT",))
    assert "DNS" in dns_rendered
    assert "csk config build-ssh doctor" not in dns_rendered
    assert "UNTRUSTED_RAW_HINT" not in dns_rendered
