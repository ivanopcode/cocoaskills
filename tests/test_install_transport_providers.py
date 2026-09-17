"""Per-endpoint operator provider selection through the production installer.

Every test drives ``installer.install`` with a source policy on disk: no test
supplies its own provider callback, so the production
``installer`` -> ``acquire_plan`` -> ``endpoint_tool`` -> broker chain always
runs.  Acquisition itself is observed at the ``git_admission.acquire_network``
seam with a classified injected failure, which keeps every run free of
outbound network while proving which endpoint was attempted with which
credentials.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from conftest import make_config, make_project, write_skillfile
from test_git_admission_ssh import (
    _identity as _ssh_identity,
)
from test_git_admission_ssh import (
    _known_hosts as _ssh_known_hosts,
)
from test_install import _stub_trusted_toolchain
from test_install_external_repository import (
    _external_repository,
    _git_tool,
    _skill_repository,
)

from csk import git_admission, installer
from csk.build_https import BuildHTTPSRule
from csk.build_ssh import BuildSSHRule

pytestmark = pytest.mark.skipif(
    sys.platform not in {"darwin", "win32"},
    reason="go-repository-v1 is qualified only on macOS and Windows",
)

IDENTITY = "example.org/kit"
HTTPS_URL = "https://example.org/kit.git"
SSH_URL = "git@example.org:kit.git"


@dataclass(frozen=True)
class _ObservedCall:
    transport: str
    ssh_credentials: git_admission.OperatorSSHCredentials | None
    https_credentials: git_admission.OperatorHTTPSCredentials | None


def _run_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: dict[str, Any],
    *,
    ssh_selected: git_admission.OperatorSSHCredentials | None = None,
    https_token: installer.OperatorHTTPSToken | None = None,
    build_ssh: tuple[BuildSSHRule, ...] = (),
    build_https: tuple[BuildHTTPSRule, ...] = (),
    failure_class: str = "connection-refused",
) -> tuple[Any, list[_ObservedCall]]:
    """Run one production install with ``policy`` and record acquisitions."""

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    monkeypatch.setenv("CSK_CONFIG", str(home / "config.json"))
    monkeypatch.setenv("CSK_SOURCE_POLICY", str(home / "source-policy.json"))
    project = make_project(tmp_path)
    _external, commit = _external_repository(tmp_path)
    _skill_repository(skills, commit, git=HTTPS_URL)
    write_skillfile(
        project,
        {
            "schema_version": 1,
            "agents": ["codex_cli"],
            "skills": [{"name": "external-skill", "tag": "v1"}],
        },
    )
    config = make_config(home, skills, project, agents=["codex_cli"])
    config = replace(config, build_ssh=build_ssh, build_https=build_https)
    (home / "source-policy.json").write_text(json.dumps(policy), encoding="utf-8")
    _stub_trusted_toolchain(monkeypatch)
    selected = (
        ssh_selected
        if ssh_selected is not None
        else git_admission.OperatorSSHCredentials()
    )
    monkeypatch.setattr(
        installer, "_capture_operator_ssh_credentials", lambda options: selected
    )
    monkeypatch.setattr(
        installer, "_capture_operator_https_token", lambda: https_token
    )
    tool = _git_tool()

    def get_tool(*args: object, **kwargs: Any) -> git_admission.GitTool:
        return replace(
            tool,
            ssh=Path("/usr/bin/ssh"),
            ssh_credentials=kwargs.get("ssh_credentials"),
            https_credentials=kwargs.get("https_credentials"),
        )

    monkeypatch.setattr(installer, "_external_git_tool", get_tool)
    calls: list[_ObservedCall] = []

    def fetch(
        source: Any, lock: Any, tool: git_admission.GitTool, **kwargs: Any
    ) -> Any:
        calls.append(
            _ObservedCall(
                source.transport, tool.ssh_credentials, tool.https_credentials
            )
        )
        raise git_admission.GitAdmissionError(
            git_admission.SOURCE_UNAVAILABLE,
            "injected classified failure",
            failure_class=failure_class,
        )

    monkeypatch.setattr(git_admission, "acquire_network", fetch)
    result = installer.install(config)[0]
    return result, calls


def _policy(endpoints: list[dict[str, Any]], *, schema: int = 1) -> dict[str, Any]:
    entry: dict[str, Any] = {"endpoints": endpoints, "fallback": "availability-auth"}
    return {"schema_version": schema, "repositories": {IDENTITY: entry}}


def _rendered_errors(result: Any) -> str:
    return "\n".join(result.errors)


@pytest.mark.parametrize("order", ["https-first", "ssh-first"])
def test_unknown_named_provider_refuses_before_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, order: str
) -> None:
    """A named provider with no operator selection never reaches acquisition.

    The unknown HTTPS endpoint is refused with the lane's identity code in
    both orders; the SSH run-wide selection stays available for the cases
    where the SSH endpoint itself is resolvable, which proves the refusal
    comes from admission rather than from missing SSH material.
    """

    first, second = (
        (HTTPS_URL, SSH_URL) if order == "https-first" else (SSH_URL, HTTPS_URL)
    )
    policy = _policy(
        [
            {"url": first, "authentication": "unknown-provider-alpha"},
            {"url": second, "authentication": "unknown-provider-beta"},
        ]
    )
    run_wide = git_admission.OperatorSSHCredentials(
        identity=tmp_path / "operator-identity",
        known_hosts=tmp_path / "known-hosts",
    )
    result, calls = _run_install(
        monkeypatch,
        tmp_path,
        policy,
        ssh_selected=run_wide if order == "https-first" else None,
    )
    assert calls == []
    rendered = _rendered_errors(result)
    assert git_admission.IDENTITY_INVALID in rendered
    assert "unknown-provider-alpha" not in rendered
    assert "unknown-provider-beta" not in rendered


@pytest.mark.parametrize("direction", ["https-unavailable", "ssh-unavailable"])
def test_unavailable_configured_provider_preserves_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, direction: str
) -> None:
    """Configured-but-unreadable material opens the independent alternate."""

    run_wide_ssh = git_admission.OperatorSSHCredentials(
        identity=tmp_path / "operator-identity",
        known_hosts=tmp_path / "known-hosts",
    )
    run_wide_https = installer.OperatorHTTPSToken(username="token", token="synthetic")
    if direction == "https-unavailable":
        policy = _policy(
            [
                {"url": HTTPS_URL, "authentication": "team-https"},
                {"url": SSH_URL, "authentication": "team-ssh"},
            ]
        )
        env_name = "CSK_TEST_MISSING_PROVIDER_TOKEN"
        monkeypatch.delenv(env_name, raising=False)
        result, calls = _run_install(
            monkeypatch,
            tmp_path,
            policy,
            ssh_selected=run_wide_ssh,
            build_https=(BuildHTTPSRule(scope=IDENTITY, token_env=env_name),),
        )
        assert len(calls) == 1
        assert calls[0].transport == "ssh"
        assert calls[0].ssh_credentials == run_wide_ssh
        rendered = _rendered_errors(result)
        assert "repository_endpoint_unavailable" in rendered
        assert env_name not in rendered
    else:
        policy = _policy(
            [
                {"url": SSH_URL, "authentication": "team-ssh"},
                {"url": HTTPS_URL, "authentication": "team-https"},
            ]
        )
        result, calls = _run_install(
            monkeypatch,
            tmp_path,
            policy,
            https_token=run_wide_https,
            build_ssh=(
                BuildSSHRule(
                    scope=IDENTITY,
                    identity=os.fspath(tmp_path / "absent-identity"),
                    known_hosts=os.fspath(tmp_path / "known-hosts"),
                ),
            ),
        )
        assert len(calls) == 1
        assert calls[0].transport == "https"
        assert calls[0].https_credentials is not None
        assert calls[0].https_credentials.token_value == "synthetic"
        rendered = _rendered_errors(result)
        assert "repository_endpoint_unavailable" in rendered
        assert "absent-identity" not in rendered


@pytest.mark.parametrize(
    ("direction", "selection"),
    [
        ("https-first", "run-wide"),
        ("ssh-first", "run-wide"),
        ("https-first", "rules"),
        ("ssh-first", "rules"),
    ],
)
def test_installer_endpoints_receive_independent_provider_selections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    direction: str,
    selection: str,
) -> None:
    """Each endpoint attempt carries its own transport's operator selection."""

    first, second = (
        (HTTPS_URL, SSH_URL) if direction == "https-first" else (SSH_URL, HTTPS_URL)
    )
    policy = _policy(
        [
            {"url": first, "authentication": "team-first"},
            {"url": second, "authentication": "team-second"},
        ]
    )
    run_wide_ssh = git_admission.OperatorSSHCredentials(
        identity=tmp_path / "operator-identity",
        known_hosts=tmp_path / "known-hosts",
    )
    run_wide_https = installer.OperatorHTTPSToken(username="token", token="synthetic")
    kwargs: dict[str, Any] = {}
    if selection == "run-wide":
        kwargs = {"ssh_selected": run_wide_ssh, "https_token": run_wide_https}
    else:
        env_name = "CSK_TEST_PROVIDER_TOKEN"
        monkeypatch.setenv(env_name, "synthetic-rule-token")
        key = _ssh_identity(tmp_path / "operator")
        known_hosts = _ssh_known_hosts(tmp_path / "operator")
        kwargs = {
            "build_ssh": (
                BuildSSHRule(
                    scope=IDENTITY,
                    identity=os.fspath(key),
                    known_hosts=os.fspath(known_hosts),
                ),
            ),
            "build_https": (BuildHTTPSRule(scope=IDENTITY, token_env=env_name),),
        }
    _result, calls = _run_install(monkeypatch, tmp_path, policy, **kwargs)
    assert [call.transport for call in calls] == (
        ["https", "ssh"] if direction == "https-first" else ["ssh", "https"]
    )
    https_call = next(call for call in calls if call.transport == "https")
    ssh_call = next(call for call in calls if call.transport == "ssh")
    assert https_call.https_credentials is not None
    assert https_call.https_credentials.host == "example.org"
    assert ssh_call.ssh_credentials is not None
    assert ssh_call.ssh_credentials.selected
    if selection == "run-wide":
        assert ssh_call.ssh_credentials == run_wide_ssh
        assert https_call.https_credentials.token_value == "synthetic"
    else:
        assert https_call.https_credentials.token_value == "synthetic-rule-token"


@pytest.mark.parametrize("alternate", ["port", "alias"])
def test_installer_refuses_forbidden_alternate_before_any_acquisition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, alternate: str
) -> None:
    """Whole-plan lane admission precedes the first external-build fetch."""

    valid = "https://mirror.example.net/kit.git"
    if alternate == "port":
        policy = _policy(
            [
                {
                    "url": valid,
                    "authentication": "team-https",
                    "mirror_of": IDENTITY,
                },
                {
                    "url": "https://mirror.example.net:8443/kit.git",
                    "authentication": "team-https",
                    "mirror_of": IDENTITY,
                },
            ],
            schema=2,
        )
    else:
        policy = _policy(
            [
                {
                    "url": valid,
                    "authentication": "team-https",
                    "mirror_of": IDENTITY,
                },
                {
                    "url": HTTPS_URL,
                    "authentication": "team-https",
                    "alias": "corp",
                    "mirror_of": IDENTITY,
                },
            ],
            schema=2,
        )
        policy["aliases"] = {
            "corp": {"host": "mirror.example.net", "authentication": "team-https"}
        }
    run_wide_https = installer.OperatorHTTPSToken(username="token", token="synthetic")
    result, calls = _run_install(
        monkeypatch, tmp_path, policy, https_token=run_wide_https
    )
    assert calls == []
    assert "build_repository_identity_invalid" in _rendered_errors(result)


def test_installer_mirror_endpoints_resolve_per_endpoint_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mirror endpoints authenticate against the mirror host, not the key."""

    mirror_https = "https://mirror.example.net/kit.git"
    mirror_ssh = "ssh://git@mirror.example.net/kit.git"
    policy = _policy(
        [
            {
                "url": mirror_https,
                "authentication": "team-https",
                "mirror_of": IDENTITY,
            },
            {
                "url": mirror_ssh,
                "authentication": "team-ssh",
                "mirror_of": IDENTITY,
            },
        ],
        schema=2,
    )
    run_wide_ssh = git_admission.OperatorSSHCredentials(
        identity=tmp_path / "operator-identity",
        known_hosts=tmp_path / "known-hosts",
    )
    run_wide_https = installer.OperatorHTTPSToken(username="token", token="synthetic")
    _result, calls = _run_install(
        monkeypatch,
        tmp_path,
        policy,
        ssh_selected=run_wide_ssh,
        https_token=run_wide_https,
    )
    assert [call.transport for call in calls] == ["https", "ssh"]
    assert calls[0].https_credentials is not None
    assert calls[0].https_credentials.host == "mirror.example.net"
    assert calls[1].ssh_credentials == run_wide_ssh


@pytest.mark.parametrize(
    "case",
    ["https-anonymous", "ssh-anonymous-falls-back", "ssh-mismatched", "https-mismatched"],
)
def test_installer_anonymous_and_mismatched_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    """Explicit anonymous is honored; mismatched transport material refuses."""

    run_wide_ssh = git_admission.OperatorSSHCredentials(
        identity=tmp_path / "operator-identity",
        known_hosts=tmp_path / "known-hosts",
    )
    run_wide_https = installer.OperatorHTTPSToken(username="token", token="synthetic")
    if case == "https-anonymous":
        policy = _policy([{"url": HTTPS_URL, "authentication": "anonymous"}])
        policy["repositories"][IDENTITY]["fallback"] = "none"
        _result, calls = _run_install(
            monkeypatch, tmp_path, policy, https_token=run_wide_https
        )
        assert len(calls) == 1
        assert calls[0].transport == "https"
        assert calls[0].https_credentials is None
    elif case == "ssh-anonymous-falls-back":
        policy = _policy(
            [
                {"url": SSH_URL, "authentication": "anonymous"},
                {"url": HTTPS_URL, "authentication": "team-https"},
            ]
        )
        result, calls = _run_install(
            monkeypatch, tmp_path, policy, https_token=run_wide_https
        )
        assert len(calls) == 1
        assert calls[0].transport == "https"
        assert calls[0].https_credentials is not None
        assert "repository_endpoint_unavailable" in _rendered_errors(result)
    elif case == "ssh-mismatched":
        policy = _policy([{"url": SSH_URL, "authentication": "team-ssh"}])
        policy["repositories"][IDENTITY]["fallback"] = "none"
        result, calls = _run_install(
            monkeypatch, tmp_path, policy, https_token=run_wide_https
        )
        assert calls == []
        assert git_admission.IDENTITY_INVALID in _rendered_errors(result)
    else:
        policy = _policy([{"url": HTTPS_URL, "authentication": "team-https"}])
        policy["repositories"][IDENTITY]["fallback"] = "none"
        result, calls = _run_install(
            monkeypatch, tmp_path, policy, ssh_selected=run_wide_ssh
        )
        assert calls == []
        assert git_admission.IDENTITY_INVALID in _rendered_errors(result)
