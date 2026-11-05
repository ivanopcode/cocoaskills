"""Portable diagnostic seams; no network, real keys or credential stores."""
import subprocess

import pytest

from csk import git_admission, installer
from csk.build_repository import LockedCommit
from csk.sources import diagnostics, transport


# Helper text may add a fixed hint, but only terminal evidence sets the class.
@pytest.mark.parametrize("terminal, expected", [
    ("git@example.org: Permission denied (publickey).", "ssh-auth-rejected"),
    ("", "unclassified"),
    ("ssh: connect to host example.org port 22: Connection refused", "connection-refused"),
])
@pytest.mark.parametrize("resolve", [False, True])
def test_runner_carries_safe_hint_without_reclassifying(monkeypatch, tmp_path, terminal, expected, resolve):
    output = ('sign_and_send_pubkey: signing failed for ED25519 "PRIVATE_CANARY" from agent: refused\n' + terminal).encode()
    monkeypatch.setattr(git_admission, "_operation_trust_environment", lambda *args: {})
    monkeypatch.setattr(git_admission.git_trust, "pinned_command", lambda argv, env: argv)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(128, "git", stderr=output)
    from test_git_admission_ssh import _private_paths, _tool
    tool = _tool(tmp_path / "ssh", git_admission.OperatorSSHCredentials())
    paths = _private_paths(tmp_path / "private")
    monkeypatch.setattr(git_admission.subprocess, "run", fail)
    with pytest.raises(git_admission.GitAdmissionError) as caught:
        if resolve:
            git_admission._run_git_ls_remote(tool, paths, {}, ("ls-remote",), limits=git_admission.Limits())
        else:
            git_admission._run_git(tool, paths, {}, "fetch", limits=git_admission.Limits())
    assert caught.value.failure_class == expected
    assert caught.value.hints == ("ssh-signing-failed",)
    rendered = installer.failure_text(caught.value, verbose=True)
    assert "next:" in rendered and "hint: SSH reported a signing failure" in rendered
    assert "PRIVATE_CANARY" not in rendered


# Remote payload and injected controls cannot become echoed hint prose.
@pytest.mark.parametrize("output", [None, b"remote: sign_and_send_pubkey: signing failed for CANARY", b"remote: x\rsign_and_send_pubkey: signing failed for CANARY", b"CANARY", b"-----BEGIN OPENSSH PRIVATE KEY-----"])
def test_untrusted_output_has_no_hint(output):
    assert git_admission._git_failure_hints(output) == ()
    assert diagnostics.transport_next_steps("unclassified", ("CANARY",)).startswith("next: ")
    assert "CANARY" not in diagnostics.transport_next_steps("unclassified", ("CANARY",))


# Both acquisition and resolution preserve hints without changing plan verdicts.
@pytest.mark.parametrize("resolve", [False, True])
def test_attempt_hints_survive_transport_and_deepest_cause(resolve):
    plan = transport.plan_attempts("example.org/kit", "ssh://git@example.org/kit.git")
    def fail(**kwargs):
        raise git_admission.GitAdmissionError(git_admission.SOURCE_UNAVAILABLE, "private Git operation failed", failure_class="ssh-auth-rejected", hints=("ssh-signing-failed",))
    with pytest.raises(transport.TransportResolutionError) as caught:
        if resolve:
            transport.resolve_plan(plan, "tag", "v1", attempt=fail)
        else:
            transport.acquire_plan(plan, LockedCommit("sha1", "0" * 40), attempt=fail)
    error = caught.value
    assert error.attempts[0].classification == "ssh-auth-rejected"
    assert error.attempts[0].hints == ("ssh-signing-failed",)
    error.__cause__ = git_admission.GitAdmissionError(git_admission.SOURCE_UNAVAILABLE, "private Git operation failed")
    for verbose in (False, True):
        rendered = installer.failure_text(error, verbose=verbose)
        assert "attempt 1=ssh-auth-rejected" in rendered
        assert "ssh://example.org/kit.git" in rendered
        assert "next:" in rendered and "hint:" in rendered
        assert rendered.splitlines()[0] != "repository_endpoint_unavailable: private Git operation failed"


# Every known outcome has a concrete action, and unknown classes fail safely.
def test_class_actions_cover_policy_outcomes():
    from csk.sources import repository_policy
    assert (repository_policy.AVAILABILITY_AUTH_FAILURES | repository_policy.FORBIDDEN_FALLBACK_FAILURES) <= diagnostics.NEXT_BY_FAILURE_CLASS.keys()
    for failure_class in diagnostics.NEXT_BY_FAILURE_CLASS:
        assert diagnostics.transport_next_steps(failure_class).startswith("next: ")
    assert "CANARY" not in diagnostics.transport_next_steps("CANARY")


# Key-loading hints carry no path, key bytes or unsupported diagnostic claims.
@pytest.mark.parametrize("record", [
    'Load key "/sensitive/CANARY": incorrect passphrase supplied to decrypt private key',
    'Load key "/sensitive/CANARY": invalid format',
])
def test_key_load_hint_is_fixed_and_not_a_failure_class(record):
    assert git_admission._git_failure_hints(record) == ("ssh-key-load-failed",)
    assert git_admission._classify_git_failure_output(record) == "unclassified"
    text = diagnostics.transport_next_steps("unclassified", git_admission._git_failure_hints(record))
    assert "hint: SSH reported a key-loading failure" in text
    assert "CANARY" not in text and "/sensitive" not in text
