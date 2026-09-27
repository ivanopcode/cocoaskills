from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import make_config, make_project, run, write_skillfile
from test_builds_toolchain import RecordingRunner, _make_goroot
from test_install import _stub_trusted_toolchain
from test_install_external_repository import _external_repository, _git_tool, _skill_repository

from csk import cli, git_admission, installer
from csk.builds import toolchain as build_toolchain
from csk.sources import transport

pytestmark = pytest.mark.skipif(
    sys.platform not in {"darwin", "win32"},
    reason="go-repository-v1 is qualified only on macOS and Windows",
)


def _external_install_case(tmp_path: Path, *, annotated_tag_lock: bool):
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True)
    home = tmp_path / ".cocoaskills"
    installer.locking.provision_new_manager_home(home)
    project = make_project(tmp_path)
    external, commit = _external_repository(tmp_path)
    lock = commit
    if annotated_tag_lock:
        run(["git", "tag", "-a", "v1", "-m", "annotated"], external)
        lock = run(
            ["git", "show-ref", "--hash", "refs/tags/v1"], external
        ).stdout.strip()
        assert run(["git", "cat-file", "-t", lock], external).stdout.strip() == "tag"
    _skill_repository(skills_root, lock)
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [{"name": "external-skill", "tag": "v1"}],
        },
    )
    config = make_config(home, skills_root, project, agents=["codex_cli"])
    return config, project, external, lock


def _invoke_install(capsys, *, verbose: bool) -> tuple[int, str, str]:
    arguments = ["install", "app"]
    if verbose:
        arguments.append("--verbose")
    code = cli.main(arguments)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _use_fake_unsupported_go(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    cause: str | None = None,
) -> None:
    host = build_toolchain._native_host()
    goroot = _make_goroot(tmp_path / "fake-go" / "root")
    runner = RecordingRunner(
        goroot,
        version=f"go version go1.22.0 {host.goos}/{host.goarch}\n",
    )
    operator_search_path = build_toolchain.OperatorSearchPath((str(goroot / "bin"),))
    real_establish = build_toolchain.establish_toolchain

    def establish_with_fake_go(config):
        try:
            return real_establish(
                replace(
                    config,
                    operator_search_path=operator_search_path,
                    runner=runner,
                )
            )
        except build_toolchain.ToolchainError as exc:
            if cause is not None and exc.code == "unsupported_go_family":
                raise exc from RuntimeError(cause)
            raise

    monkeypatch.setattr(build_toolchain, "establish_toolchain", establish_with_fake_go)


def _stub_network_admission(
    monkeypatch: pytest.MonkeyPatch,
    *,
    external: Path,
    original_detail: str,
) -> None:
    git_tool = _git_tool()
    monkeypatch.setattr(
        installer,
        "_external_git_tool",
        lambda *_args, **_kwargs: git_tool,
    )
    real_acquire_plan = transport.acquire_plan

    def acquire_via_local_test_seam(plan, lock, tool=None, **kwargs):
        def attempt(*, endpoint, lock, tool, tag=None, limits=None):
            del endpoint, limits, tag
            if "tag-object" in original_detail:
                lock_type = run(
                    ["git", "cat-file", "-t", lock.hex], external
                ).stdout.strip()
                assert lock_type == "tag"
                snapshot = git_admission.admit_local(external, tool)
                assert snapshot.commit != lock.hex
                detail = "locked object is not the selected commit"
            else:
                detail = original_detail
            raise git_admission.GitAdmissionError(
                git_admission.INCOMPLETE_SOURCE,
                detail,
            )

        return real_acquire_plan(plan, lock, tool, attempt=attempt, **kwargs)

    monkeypatch.setattr(transport, "acquire_plan", acquire_via_local_test_seam)


def _assert_unsupported_go_reason(stderr: str) -> None:
    assert "unsupported_go_family" in stderr
    assert (
        "Go release is older than 1.23" in stderr
        or (
            "go version go1.22.0" in stderr
            and "family 1.22" in stderr
            and "tested families:" in stderr
        )
    )


def test_cli_unsupported_go_refusal_shows_code_reason_and_verbose_cause(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    config, _project, _external, _lock = _external_install_case(
        tmp_path, annotated_tag_lock=False
    )
    monkeypatch.setattr(cli.config, "load_config", lambda: config)
    _use_fake_unsupported_go(
        monkeypatch,
        tmp_path,
        cause="fake go probe produced unsupported release family 1.22",
    )

    code, _stdout, stderr = _invoke_install(capsys, verbose=False)
    assert code == cli.EXIT_PARTIAL_FAIL
    assert "go-v1 unsupported_go_family" in stderr
    _assert_unsupported_go_reason(stderr)

    code, _stdout, stderr = _invoke_install(capsys, verbose=True)
    assert code == cli.EXIT_PARTIAL_FAIL
    assert "go-v1 unsupported_go_family" in stderr
    _assert_unsupported_go_reason(stderr)
    assert "cause chain:" in stderr
    assert (
        "caused by: go-v1 unsupported_go_family:" in stderr
    )
    assert "fake go probe produced unsupported release family 1.22" not in stderr


def test_cli_annotated_tag_object_lock_refusal_shows_specific_reason_in_both_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    config, _project, external, _lock = _external_install_case(
        tmp_path, annotated_tag_lock=True
    )
    monkeypatch.setattr(cli.config, "load_config", lambda: config)
    _stub_trusted_toolchain(monkeypatch)
    _stub_network_admission(
        monkeypatch,
        external=external,
        original_detail="tag-object lock mismatch",
    )

    for verbose in (False, True):
        code, _stdout, stderr = _invoke_install(capsys, verbose=verbose)
        assert code == cli.EXIT_PARTIAL_FAIL
        assert "build_repository_incomplete_source" in stderr
        assert "locked object is not the selected commit" in stderr
        if verbose:
            assert "cause chain:" in stderr
            assert (
                "incomplete_source: locked object is not the selected commit"
                in stderr
            )


def test_cli_install_refusal_redacts_credential_url_in_both_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    config, _project, external, _lock = _external_install_case(
        tmp_path, annotated_tag_lock=False
    )
    monkeypatch.setattr(cli.config, "load_config", lambda: config)
    _stub_trusted_toolchain(monkeypatch)
    secret_url = "https://build-user:top-secret@example.test/private.git"
    _stub_network_admission(
        monkeypatch,
        external=external,
        original_detail=f"endpoint refused {secret_url}",
    )

    for verbose in (False, True):
        code, _stdout, stderr = _invoke_install(capsys, verbose=verbose)
        assert code == cli.EXIT_PARTIAL_FAIL
        assert "build_repository_incomplete_source" in stderr
        assert "build-user:top-secret@" not in stderr
        assert "https://***@example.test/private.git" in stderr
        if verbose:
            assert "cause chain:" in stderr


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("quote", ["'", '"', "`"])
def test_cli_install_redacts_each_url_in_a_shared_quoted_fragment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
    verbose: bool,
    quote: str,
) -> None:
    config, _project, external, _lock = _external_install_case(
        tmp_path, annotated_tag_lock=False
    )
    monkeypatch.setattr(cli.config, "load_config", lambda: config)
    _stub_trusted_toolchain(monkeypatch)
    first = "https://first-user:first-secret@example.test/one#KEEP_FRAGMENT"
    second = (
        "https://second-user:second-secret@example.test/two"
        "?token=second-query-secret&x=1"
    )
    _stub_network_admission(
        monkeypatch,
        external=external,
        original_detail=(
            f"missing revision {quote}{first}::{second}{quote} contact ops@example.test"
        ),
    )

    code, stdout, stderr = _invoke_install(capsys, verbose=verbose)

    assert code == cli.EXIT_PARTIAL_FAIL
    output = stdout + stderr
    for secret in (
        "first-user:first-secret@",
        "second-user:second-secret@",
        "second-query-secret",
    ):
        assert secret not in output
    assert "https://***@example.test/one#KEEP_FRAGMENT" in stderr
    assert "https://***@example.test/two?token=***&x=1" in stderr
    assert "missing revision" in stderr
    assert "contact ops@example.test" in stderr


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize(
    "token_value",
    [
        pytest.param("ab!c$d", id="required-punctuation-example"),
        pytest.param("a'b", id="apostrophe"),
        pytest.param("a;b", id="semicolon"),
        pytest.param("a)b", id="closing-parenthesis"),
        pytest.param("a!b", id="exclamation"),
        pytest.param("a$b", id="dollar"),
        pytest.param("a*b", id="asterisk"),
        pytest.param("a+b", id="plus"),
        pytest.param("a,b", id="comma"),
        pytest.param("a=b", id="equals"),
        pytest.param("a:b", id="colon"),
        pytest.param("a@b", id="at"),
        pytest.param("a/b", id="slash"),
        pytest.param("a?b", id="question-mark"),
    ],
)
def test_cli_install_refusal_redacts_full_punctuated_query_value_in_both_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
    verbose: bool,
    token_value: str,
) -> None:
    config, _project, external, _lock = _external_install_case(
        tmp_path, annotated_tag_lock=False
    )
    monkeypatch.setattr(cli.config, "load_config", lambda: config)
    _stub_trusted_toolchain(monkeypatch)
    _stub_network_admission(
        monkeypatch,
        external=external,
        original_detail=(
            "endpoint refused "
            f"https://example.test/repo.git?token={token_value}&x=1"
        ),
    )

    code, stdout, stderr = _invoke_install(capsys, verbose=verbose)

    assert code == cli.EXIT_PARTIAL_FAIL
    assert "?token=***&x=1" in stderr
    assert token_value not in stdout + stderr
    assert "x=1" in stderr


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize(
    ("url", "expected_url", "private_userinfo"),
    [
        pytest.param(
            "https://review-user:top-secret@example.test/repo.git",
            "https://***@example.test/repo.git",
            "review-user:top-secret@",
            id="userinfo-redacted-only",
        ),
        pytest.param(
            "https://example.test/repo.git",
            "https://example.test/repo.git",
            None,
            id="email-is-not-userinfo",
        ),
    ],
)
def test_cli_install_refusal_preserves_quoted_reason_after_url_and_email_in_both_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
    verbose: bool,
    url: str,
    expected_url: str,
    private_userinfo: str | None,
) -> None:
    config, _project, external, _lock = _external_install_case(
        tmp_path, annotated_tag_lock=False
    )
    monkeypatch.setattr(cli.config, "load_config", lambda: config)
    _stub_trusted_toolchain(monkeypatch)
    _stub_network_admission(
        monkeypatch,
        external=external,
        original_detail=(
            f"endpoint refused '{url} missing required revision; "
            "contact ops@example.test'"
        ),
    )

    code, stdout, stderr = _invoke_install(capsys, verbose=verbose)

    assert code == cli.EXIT_PARTIAL_FAIL
    assert "missing required revision; contact ops@example.test" in stderr
    assert expected_url in stderr
    if private_userinfo is not None:
        assert private_userinfo not in stdout + stderr


@pytest.mark.parametrize(
    ("userinfo", "credential_markers"),
    [
        (
            "user'name!$&()*+,;=:pass'word!$&()*+,;=",
            ("user'name!$&()*+,;=", "pass'word!$&()*+,;="),
        ),
        (
            "user%27name!$&()*+,;=:pass%27word!$&()*+,;=",
            ("user%27name!$&()*+,;=", "pass%27word!$&()*+,;="),
        ),
    ],
    ids=("literal-apostrophes", "encoded-apostrophes"),
)
@pytest.mark.parametrize("verbose", [False, True])
def test_cli_install_refusal_redacts_literal_apostrophe_userinfo_in_both_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
    userinfo: str,
    credential_markers: tuple[str, str],
    verbose: bool,
) -> None:
    config, _project, external, _lock = _external_install_case(
        tmp_path, annotated_tag_lock=False
    )
    monkeypatch.setattr(cli.config, "load_config", lambda: config)
    _stub_trusted_toolchain(monkeypatch)
    secret_url = f"https://{userinfo}@example.test/private.git"
    _stub_network_admission(
        monkeypatch,
        external=external,
        original_detail=f"endpoint refused {secret_url}",
    )

    code, stdout, stderr = _invoke_install(capsys, verbose=verbose)

    assert code == cli.EXIT_PARTIAL_FAIL
    output = stdout + stderr
    if any(marker in output for marker in credential_markers):
        pytest.fail(
            "credential-bearing apostrophe userinfo escaped CLI diagnostics",
            pytrace=False,
        )
    assert "https://***@example.test/private.git" in stderr
    if verbose:
        assert "cause chain:" in stderr


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize("shape", ["path", "token", "subprocess-stderr"])
def test_broker_failure_keeps_private_material_out(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
    verbose: bool,
    shape: str,
) -> None:
    from csk.build_https import BuildHTTPSRule

    config, _project, _external, _lock = _external_install_case(
        tmp_path, annotated_tag_lock=False
    )
    config = replace(
        config,
        build_https=(
            BuildHTTPSRule(scope="example.test/external-tool", token="keyring"),
        ),
    )
    policy = config.path.parent / "source-policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repositories": {
                    "example.test/external-tool": {
                        "endpoints": [
                            {
                                "url": "https://example.test/external-tool.git",
                                "authentication": "team-https",
                            }
                        ],
                        "fallback": "availability-auth",
                    }
                },
            }
        )
    )
    monkeypatch.setenv("CSK_SOURCE_POLICY", str(policy))
    monkeypatch.setattr(cli.config, "load_config", lambda: config)
    monkeypatch.setattr(installer, "_capture_operator_https_token", lambda: None)
    _stub_trusted_toolchain(monkeypatch)

    marker = (
        "/private/broker/vault-file"
        if shape == "path"
        else "SYNTHETIC_BROKER_CANARY"
    )
    reached: list[bool] = []

    def fail_broker_read(*_args, **_kwargs):
        reached.append(True)
        if shape == "subprocess-stderr":
            import subprocess

            raise subprocess.CalledProcessError(
                1, ["git", "credential", "fill"], stderr=marker
            )
        raise RuntimeError(f"credential broker failed: {marker}")

    monkeypatch.setattr(
        installer.build_https_module, "read_namespaced_token", fail_broker_read
    )
    code, stdout, stderr = _invoke_install(capsys, verbose=verbose)

    assert reached
    assert code == cli.EXIT_PARTIAL_FAIL
    if marker in stdout + stderr:
        pytest.fail(
            "broker private material escaped the CLI diagnostic boundary",
            pytrace=False,
        )


@pytest.mark.parametrize("verbose", [False, True])
def test_preexisting_redaction_marker_does_not_bypass_userinfo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
    verbose: bool,
) -> None:
    config, _project, external, _lock = _external_install_case(
        tmp_path, annotated_tag_lock=False
    )
    monkeypatch.setattr(cli.config, "load_config", lambda: config)
    _stub_trusted_toolchain(monkeypatch)
    marker_url = "https://review-user:***SYNTHETIC_CANARY@example.test/repo.git"
    _stub_network_admission(
        monkeypatch,
        external=external,
        original_detail=f"endpoint refused {marker_url}",
    )

    code, stdout, stderr = _invoke_install(capsys, verbose=verbose)

    assert code == cli.EXIT_PARTIAL_FAIL
    if "review-user" in stdout + stderr or "SYNTHETIC_CANARY" in stdout + stderr:
        pytest.fail(
            "userinfo escaped when its password contains the redaction marker",
            pytrace=False,
        )


def test_global_verbose_has_go_cause(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys,
) -> None:
    from test_global_install import _write_global_skillfile

    config, _project, _external, _lock = _external_install_case(
        tmp_path, annotated_tag_lock=False
    )
    _write_global_skillfile(
        config.path.parent,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [{"name": "external-skill", "tag": "v1"}],
        },
    )
    monkeypatch.setattr(cli.config, "load_config", lambda: config)
    _use_fake_unsupported_go(
        monkeypatch,
        tmp_path,
        cause="SYNTHETIC_GO_PROBE_PRIVATE_TEXT",
    )

    project_code, _project_stdout, project_stderr = _invoke_install(
        capsys, verbose=True
    )
    assert project_code == cli.EXIT_PARTIAL_FAIL

    code = cli.main(["global", "install", "--verbose"])
    stderr = capsys.readouterr().err

    assert code == cli.EXIT_PARTIAL_FAIL
    project_cause_lines = [
        line for line in project_stderr.splitlines() if line.startswith("  caused by:")
    ]
    global_cause_lines = [
        line for line in stderr.splitlines() if line.startswith("  caused by:")
    ]
    assert global_cause_lines == project_cause_lines
    _assert_unsupported_go_reason(stderr)
    assert "cause chain:" in stderr
    assert "go-v1 unsupported_go_family:" in stderr
    assert "SYNTHETIC_GO_PROBE_PRIVATE_TEXT" not in stderr


def test_unregistered_future_cause_type_is_not_rendered() -> None:
    class FutureCredentialBrokerError(RuntimeError):
        pass

    future_cause = FutureCredentialBrokerError("SYNTHETIC_FUTURE_CAUSE_SECRET")

    assert installer._render_failure_cause(future_cause) is None

    outer = installer.InstallError("stable refusal")
    outer.__cause__ = future_cause
    rendered = installer.failure_text(outer, verbose=True)
    assert "SYNTHETIC_FUTURE_CAUSE_SECRET" not in rendered
    assert "cause chain:" not in rendered


def test_public_install_failure_types_have_explicit_cause_rendering_rules() -> None:
    modules = (
        installer.build_toolchain,
        installer.build_planner,
        installer.build_source,
        git_admission,
        installer.source_errors,
        installer.source_transport,
        installer.repository_policy,
    )
    public_failure_types = {
        candidate
        for module in modules
        for name, candidate in vars(module).items()
        if not name.startswith("_")
        and isinstance(candidate, type)
        and candidate.__module__ == module.__name__
        and issubclass(candidate, BaseException)
    }

    assert set(installer._FAILURE_CAUSE_RENDERERS) == public_failure_types
