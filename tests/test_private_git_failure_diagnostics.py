"""Regression tests for naming the canonical repository on private Git failures."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import make_config, make_project, write_skillfile
from test_git_admission_ssh import _tool
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
from csk.sources import _selection_fs, diagnostics, errors as source_errors, transport


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
    # Subprocess private Git failures through acquire_network and resolve_network_ref attach the canonical repository identity and fixed hints while keeping raw child stderr hidden.
    raw_output = (
        'RAW_STDERR_CANARY_TOKEN\n'
        'sign_and_send_pubkey: signing failed for ED25519 "PRIVATE_KEY_CANARY" from agent: refused\n'
        + terminal_stderr
    ).encode("utf-8")
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text("", encoding="utf-8")
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("placeholder", encoding="utf-8")
    tool = _tool(
        fake_ssh,
        git_admission.OperatorSSHCredentials(identity=str(key_file)),
    )
    source = git_admission.RepositorySource(
        git="ssh://git@example.org/team/kit.git",
        identity="example.org/team/kit",
        transport="ssh",
    )
    monkeypatch.setattr(git_admission, "validate_git_tool", lambda *_a, **_kw: None)
    monkeypatch.setattr(git_admission, "_preflight_ssh", lambda *_a, **_kw: None)
    monkeypatch.setattr(git_admission, "_materialize_ssh_wrapper", lambda *_a, **_kw: ("ssh",))
    monkeypatch.setattr(git_admission, "_operation_trust_environment", lambda *_a: {})
    monkeypatch.setattr(git_admission.git_trust, "pinned_command", lambda argv, _env: argv)

    def fail_run(*_args: object, **_kwargs: object) -> Any:
        raise subprocess.CalledProcessError(128, "git", stderr=raw_output)

    monkeypatch.setattr(git_admission.subprocess, "run", fail_run)

    with pytest.raises(git_admission.GitAdmissionError) as caught:
        if operation == "ls-remote":
            git_admission.resolve_network_ref(
                source,
                "tag",
                "v1.0.0",
                tool,
                limits=git_admission.Limits(),
            )
        else:
            git_admission.acquire_network(
                source,
                LockedCommit("sha1", "1" * 40),
                tool,
                limits=git_admission.Limits(),
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
    # Negative + positive control: credential-bearing URLs (which plan-time parsing already rejects in production) never leak userinfo or query tokens in rendered diagnostics while still naming the canonical repository identity; bare-token scp strings without ':' are not matched by free-text redaction, so production relies on never echoing the raw endpoint URL.
    # 1) TransportResolutionError (ssh-auth-rejected) path refuses unparseable credentialed URLs and falls back to the validated canonical identity.
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
    "failure_class",
    ["ssh-auth-rejected", "auth-unavailable", "unclassified"],
)
def test_locked_recovery_transport_cause_renders_repository_and_next_lines(
    failure_class: str,
) -> None:
    # Locked-snapshot recovery failures wrapped in source_snapshot_unavailable render canonical repository identity plus class/next/hint lines from _transport_cause_next_lines, with a non-transport cause as negative control.
    git_cause = git_admission.GitAdmissionError(
        git_admission.SOURCE_UNAVAILABLE,
        "trusted Git admission failed for example.org/team/kit",
        failure_class=failure_class,
        hints=("ssh-signing-failed",),
        repository="example.org/team/kit",
    )
    wrapped_git = source_errors.SourceError(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        f"Skill 'demo' locked commit {'1' * 40} from Git source 'example.org/team/kit' is unavailable: {git_cause}",
    )
    wrapped_git.__cause__ = git_cause

    rendered_git = installer._schema2_failure_text(wrapped_git)
    assert "source_snapshot_unavailable:" in rendered_git, rendered_git
    assert "example.org/team/kit" in rendered_git, rendered_git
    assert f"failure class: {failure_class}" in rendered_git, rendered_git
    assert "next:" in rendered_git, rendered_git
    assert "csk config build-ssh doctor" in rendered_git, rendered_git
    assert "csk config build-https" in rendered_git, rendered_git
    assert "hint: SSH reported a signing failure" in rendered_git, rendered_git

    resolution_cause = transport.TransportResolutionError(
        (
            replace(
                _attempt(1, failure_class, "ssh://git@example.org/team/kit.git"),
                hints=("ssh-signing-failed",),
            ),
        ),
        identity="example.org/team/kit",
    )
    wrapped_resolution = source_errors.SourceError(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        f"Skill 'demo' locked commit {'1' * 40} from Git source 'example.org/team/kit' is unavailable: {resolution_cause}",
    )
    wrapped_resolution.__cause__ = resolution_cause

    rendered_resolution = installer._schema2_failure_text(wrapped_resolution)
    assert "source_snapshot_unavailable:" in rendered_resolution, rendered_resolution
    assert "example.org/team/kit" in rendered_resolution, rendered_resolution
    assert "next: attempt 1:" in rendered_resolution, rendered_resolution
    assert "csk config build-ssh doctor" in rendered_resolution, rendered_resolution
    assert "csk config build-https" in rendered_resolution, rendered_resolution
    assert "hint: attempt 1: SSH reported a signing failure" in rendered_resolution, rendered_resolution

    # Negative control: non-transport causes emit no transport next lines.
    wrapped_plain = source_errors.SourceError(
        source_errors.CODE_SNAPSHOT_UNAVAILABLE,
        "Skill 'demo' locked snapshot is unavailable: missing directory",
    )
    wrapped_plain.__cause__ = ValueError("missing directory")
    rendered_plain = installer._schema2_failure_text(wrapped_plain)
    assert installer._transport_cause_next_lines(ValueError("missing directory")) == []
    assert "next:" not in rendered_plain
    assert "csk config build-ssh doctor" not in rendered_plain


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
