"""Value-based credential redaction (design-option-c S3 Amendment 2).

Ground truth: the A/B corpus from spm TASK-261004-24ty6g (matrix.json, 1848
probes, committed as tests/fixtures/c1-redaction-matrix.json; extra.json, 28
probes; login.json, 2 probes: 1878 in total) plus the three rev-5 R141 panel
reproductions (TASK-261003-104dow, TASK-261003-3a6epx, TASK-261003-3gljgb).

Refined invariant (task notes 2026-10-04, rev 2): no argv value of 8 or more
characters supplied after a --token* name appears in any usage, error or
diagnostic output, in any rendering argparse produces (raw, repr, quoted);
no secret read from the environment (CSK_REGISTRY_TOKEN) or from a token
file appears in any output. Stated bound: values shorter than 8 characters
are not treated as credentials, so short substrings never mangle unrelated
diagnostics. Explicitly allowed: successful normal output may show an
accepted --token-env NAME (C6.env-list pins this). Conservative: bytes
reused deliberately as another non-credential argument are redacted as the
credential rather than echoed as that argument (pinned, not allowed).

Production call sites: cli.main pre-parse refusal (one matcher), the
process-stderr choke point installed by cli.main plus _CskArgumentParser
error/exit for direct parser callers (one guard).
"""

from __future__ import annotations

import http.server
import io
import json
import os
import random
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from csk import build_https, cli, config
from test_audit_publish import _record

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "c1-redaction-matrix.json"
MATRIX_ROWS: list[dict[str, object]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def redaction_config(tmp_path, monkeypatch):
    """Isolated empty-projects config, matching the A/B collector setup."""
    cfg = config.GlobalConfig(
        path=tmp_path / "csk-config.json",
        skills_root=tmp_path / "skills",
        preferred_locale=None,
        default_agents=["codex_cli"],
        adapter_mode="auto",
        worktree_alias_pattern="[A-Z]+-[0-9]+",
        projects={},
    )
    config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)
    monkeypatch.delenv("CSK_REGISTRY_TOKEN", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    return cfg


def _check_no_marker(out, err, markers):
    text = out + err
    return [marker for marker in markers if marker and marker in text]


def test_c1_redaction_matrix_no_marker_in_output(redaction_config, capsys):
    # Generated test over the full A/B corpus: 1848 probes covering every
    # option family (O1--O5), position (P1--P7), form (F1--F5) and -h/--help
    # suffix (C5), through the production entry point cli.main. Every row
    # must hold: no marker in stdout or stderr. Exit codes are asserted only
    # as 0-or-2: refusal-first (Amendment 2) intentionally exits 2 where the
    # rev3 collector let -h win over an invalid credential spelling.
    assert len(MATRIX_ROWS) == 1848
    failures = []
    for record in MATRIX_ROWS:
        row = record["row"]
        argv = list(record["argv"])
        markers = list(record["markers"])
        code = cli.main(argv)
        output = capsys.readouterr()
        leaked = _check_no_marker(output.out, output.err, markers)
        if leaked or code not in (0, 2):
            failures.append((row, code, leaked, argv, output.out[-300:], output.err[-500:]))
    assert not failures, f"{len(failures)} matrix rows leaked: {failures[:8]}"


# ---------------------------------------------------------------------------
# C6: mode flags and the allowed persisted-NAME pair.
# ---------------------------------------------------------------------------

_C6_MODES = ["--json", "--verbose", "--debug"]


@pytest.mark.parametrize("mode", _C6_MODES)
def test_ground_truth_c6_audit_token_source_refused(redaction_config, capsys, mode):
    # Rows C6.audit--json/--verbose/--debug: --token-source is not declared
    # by audit, so the pre-parse refusal fires before argparse or dispatch.
    marker = f"SECRETMARK-C6-audit{mode}"
    assert cli.main(["audit", "--token-source", marker, mode]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert "--token is refused" in output.err


@pytest.mark.parametrize("mode", _C6_MODES)
def test_ground_truth_c6_https_invalid_selector_shaped(redaction_config, capsys, mode):
    # Rows C6.https--json/--verbose/--debug: the shaped selector validator
    # names only the allowed enum members, never the supplied value.
    marker = f"SECRETMARK-C6-https{mode}"
    argv = ["config", "build-https", "add", "example.test/g", "--token-source", marker, mode]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert "must be one of git-credentials, keyring" in output.err


@pytest.mark.parametrize("mode", _C6_MODES)
def test_ground_truth_c6_audit_file_never_echoes_path(redaction_config, capsys, mode):
    # Rows C6.audit-file--json/--verbose/--debug: a --token-file path is
    # never echoed in errors, on the parse path or the dispatch path.
    marker = f"SECRETMARK-C6-audit-file{mode}"
    assert cli.main(["audit", "skill-a", "--token-file", marker, mode]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err


def test_ground_truth_c6_env_list_may_show_accepted_name(redaction_config, capsys):
    # Rows C6.env-add (setup) and C6.env-list (ALLOWED): successful normal
    # output may show an accepted --token-env NAME, which is public metadata
    # (a variable name), not a secret. This is the former L3 pair, kept as
    # allowed cases by the refined invariant.
    name = "SECRETMARK_C6_ENV"
    assert cli.main(["config", "build-https", "add", "example.test/g", "--token-env", name]) == cli.EXIT_OK
    capsys.readouterr()
    assert cli.main(["config", "build-https", "list"]) == cli.EXIT_OK
    output = capsys.readouterr()
    assert output.err == ""
    assert f"token_env={name}" in output.out


# ---------------------------------------------------------------------------
# C1: secrets from the environment (GT1).
# ---------------------------------------------------------------------------

def _publish_argv(record_path, registry="http://127.0.0.1:1"):
    return ["audit", "--publish", str(record_path), "--registry", registry]


@pytest.fixture
def publish_record(tmp_path):
    record = tmp_path / "record.json"
    record.write_text(json.dumps(_record()), encoding="utf-8")
    return record


def test_ground_truth_c1_invalid_env_token_shaped(redaction_config, publish_record, monkeypatch, capsys):
    # Row C1.invalid: an env token outside the ASCII grammar is refused
    # before any request, without echoing the value.
    token = "SECRETMARK-C1-invalid bad"
    monkeypatch.setenv("CSK_REGISTRY_TOKEN", token)
    assert cli.main(_publish_argv(publish_record)) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "SECRETMARK-C1-invalid" not in output.out + output.err
    assert "must contain one ASCII token without spaces" in output.err


def test_ground_truth_c1_empty_env_token_shaped(redaction_config, publish_record, capsys):
    # Row C1.empty: no token anywhere names the safe sources, nothing else.
    assert cli.main(_publish_argv(publish_record)) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "--publish requires a non-empty token" in output.err


def test_ground_truth_c1_long_env_token_never_echoed(redaction_config, publish_record, monkeypatch, capsys):
    # Row C1.long: a valid long env token reaches the (unreachable) registry
    # and the shaped request failure carries no token bytes. Windows caps a
    # single environment variable at 32767 characters, so the padding is
    # sized per platform; the A/B row used 70k on POSIX.
    padding = 70000 if os.name != "nt" else 30000
    token = "SECRETMARK-C1-long" + "X" * padding
    monkeypatch.setenv("CSK_REGISTRY_TOKEN", token)
    assert cli.main(_publish_argv(publish_record)) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "SECRETMARK-C1-long" not in output.out + output.err
    assert "registry request failed" in output.err


# ---------------------------------------------------------------------------
# C4: HTTP response echo (GT4).
# ---------------------------------------------------------------------------

def test_ground_truth_c4_401_response_echo_never_echoes_token(
    redaction_config, publish_record, monkeypatch, capsys
):
    # Row C4.401-echo: a hostile registry repeating the Authorization header
    # in its 401 body still yields only the shaped request failure.
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.headers["Authorization"].encode()
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("CSK_REGISTRY_TOKEN", "SECRETMARK-C4-header")
        argv = _publish_argv(publish_record, f"http://127.0.0.1:{server.server_port}")
        assert cli.main(argv) == cli.EXIT_CONFIG
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    output = capsys.readouterr()
    assert "SECRETMARK-C4-header" not in output.out + output.err
    assert "registry request failed" in output.err


# ---------------------------------------------------------------------------
# C2: secrets from token files (GT2).
# ---------------------------------------------------------------------------

_C2_FILE_CASES = [
    # (row suffix, file bytes builder, expected shaped fragment)
    ("invalid", lambda m: m.encode() + b" bad", "must contain one ASCII token without spaces"),
    ("oversize", lambda m: m.encode() + b"X" * 65537, "must not exceed 64 KiB"),
    ("nonutf8", lambda m: m.encode() + b"\xff", "as a regular UTF-8 file"),
    ("crlf", lambda m: m.encode() + b"\r\n", "registry request failed"),
    ("spaces", lambda m: m.encode() + b" ", "must contain one ASCII token without spaces"),
]


@pytest.mark.parametrize("kind,builder,fragment", _C2_FILE_CASES)
def test_ground_truth_c2_token_file_content_never_echoed(
    redaction_config, publish_record, tmp_path, capsys, kind, builder, fragment
):
    # Rows C2.invalid/oversize/nonutf8/crlf/spaces: neither the file content
    # nor the --token-file path (itself a collected argv value) is echoed.
    marker = f"SECRETMARK-C2-{kind}"
    path = tmp_path / f"token-{kind}"
    path.write_bytes(builder(marker))
    path.chmod(0o600)
    argv = _publish_argv(publish_record) + ["--token-file", str(path)]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert str(path) not in output.out + output.err
    assert fragment in output.err


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only file shapes (fifo/symlink/permissions/device)")
@pytest.mark.parametrize(
    "kind,fragment",
    [
        ("fifo", "must be a regular file"),
        ("symlink", "must be a regular file"),
        ("hardlink", "must not be readable by group or others"),
        ("device", "must be a regular file"),
    ],
)
def test_ground_truth_c2_token_file_shape_refused_without_path(
    redaction_config, publish_record, tmp_path, capsys, kind, fragment
):
    # Rows C2.fifo/symlink/hardlink/device: refusal names the cause, never
    # the path. The path marker also serves the no-echo assertion.
    if kind == "fifo":
        path = tmp_path / "token-fifo"
        os.mkfifo(path)
    elif kind == "symlink":
        path = tmp_path / "token-symlink"
        path.symlink_to(publish_record)
    elif kind == "hardlink":
        path = tmp_path / "token-hardlink"
        os.link(publish_record, path)
        publish_record.chmod(0o644)
    else:
        path = Path("/dev/null")
    argv = _publish_argv(publish_record) + ["--token-file", str(path)]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert str(path) not in output.out + output.err
    assert "SECRETMARK-C2-path" not in output.out + output.err
    assert fragment in output.err


# ---------------------------------------------------------------------------
# C3: login flows (GT3).
# ---------------------------------------------------------------------------

def test_ground_truth_c3_malformed_stdin_never_echoed(redaction_config, capsys):
    # Row C3.malformed: a NUL-bearing token from stdin is stored through the
    # seam and the success line carries no token bytes.
    stored = []
    marker = "SECRETMARK-C3-malformed"
    with (
        patch.object(build_https, "store_namespaced_token", side_effect=lambda *a, **k: stored.append(a[2])),
        patch.object(cli, "resolve_tool", return_value="git"),
        patch.object(sys, "stdin", io.StringIO(marker + "\x00\n")),
    ):
        assert cli.main(["config", "build-https", "login", "example.test/g"]) == cli.EXIT_OK
    assert stored == [marker + "\x00"]
    output = capsys.readouterr()
    assert marker not in output.out + output.err


def test_ground_truth_c3_eof_is_shaped_refusal(redaction_config, capsys):
    # Row C3.eof: empty stdin refuses with the fixed message.
    with patch.object(sys, "stdin", io.StringIO("")):
        assert cli.main(["config", "build-https", "login", "example.test/g"]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "No token supplied" in output.err


def test_ground_truth_c3_list_never_prints_stored_secret(redaction_config, tmp_path, capsys):
    # Row C3.list: listing a keyring-backed scope reports only stored=yes.
    from dataclasses import replace

    cfg = config.load_config()
    rule = build_https.BuildHTTPSRule(scope="example.test/g", token="keyring", username="token")
    config.save_config(replace(cfg, build_https=(rule,)))
    with (
        patch.object(build_https, "read_namespaced_token", return_value="SECRETMARK-C3-stored"),
        patch.object(cli, "resolve_tool", return_value="git"),
    ):
        assert cli.main(["config", "build-https", "list"]) == cli.EXIT_OK
    output = capsys.readouterr()
    assert "SECRETMARK-C3-stored" not in output.out + output.err
    assert "stored=yes" in output.out


def test_ground_truth_c3_interrupt_carries_no_token(redaction_config, capsys):
    # Row C3.interrupt: a KeyboardInterrupt during the stdin read propagates
    # (the CLI installs no handler); nothing buffered is printed.
    class Interrupted(io.StringIO):
        def readline(self, *args):
            raise KeyboardInterrupt

    with (
        patch.object(sys, "stdin", Interrupted()),
        pytest.raises(KeyboardInterrupt),
    ):
        cli.main(["config", "build-https", "login", "example.test/g"])
    output = capsys.readouterr()
    assert output.out + output.err == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell credential-helper seam")
@pytest.mark.parametrize("name,stdin", [("malformed", "SECRETMARK-C3-real\x00\n"), ("eof", "")])
def test_ground_truth_c3_real_helper_never_echoes(redaction_config, tmp_path, name, stdin):
    # Rows C3.real-malformed/C3.real-eof (login.json): a real git credential
    # helper that leaks to stderr and fails still yields shaped output with
    # neither the token nor the helper marker, through python -m csk.
    helper = tmp_path / "review-helper"
    helper.write_text("#!/bin/sh\nprintf SECRETMARK-C3-helper >&2\nexit 1\n", encoding="utf-8")
    helper.chmod(0o700)
    env = dict(
        os.environ,
        PYTHONPATH=str(ROOT / "src"),
        CSK_CONFIG=str(redaction_config.path),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="credential.helper",
        GIT_CONFIG_VALUE_0=str(helper),
    )
    env.pop("CSK_SYSTEM_CONFIG", None)
    proc = subprocess.run(
        [sys.executable, "-m", "csk", "config", "build-https", "login", "example.test/g"],
        cwd=ROOT,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "SECRETMARK-C3-real" not in proc.stdout + proc.stderr
    assert "SECRETMARK-C3-helper" not in proc.stdout + proc.stderr


def _run_module_entry(argv, *, extra_env=None):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "csk", *argv],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Rev-5 R141 panel reproductions, frozen with original argv and markers.
# ---------------------------------------------------------------------------

_FULLA_MARKER = "synthetic-r141-private-marker"
_FULLA_CASES = [
    ["config", "--token-source=" + _FULLA_MARKER, "build-https", "add"],
    ["config", "--token-source", _FULLA_MARKER, "build-https", "add"],
    ["config", "build-https", "--token-env=" + _FULLA_MARKER, "add"],
    ["config", "build-https", "--token-env", _FULLA_MARKER, "add"],
    ["audit", "--", "--token-file=" + _FULLA_MARKER],
    ["audit", "--", "--token-file", _FULLA_MARKER],
    ["--token-file=" + _FULLA_MARKER + " EXTRA", "audit"],
    ["audit", "--token-file", "normal", "--", "--token-file", _FULLA_MARKER],
]


@pytest.mark.parametrize("argv", _FULLA_CASES)
def test_rev5_fulla_real_entry_never_echoes(argv):
    # TASK-261003-104dow test_real_entry_does_not_echo_credential_value: the
    # first four are misplaced-option positional-choice leaks (value guard),
    # the last four are separator-provenance leaks (fixed refusal).
    proc = _run_module_entry(argv)
    assert proc.returncode == 2
    assert _FULLA_MARKER not in proc.stdout + proc.stderr


_FULLB_MARKER = "synthetic-panel-secret-not-a-real-credential"
_FULLB_CASES = [
    ["--token-file", _FULLB_MARKER, "audit"],
    ["--token-file=" + _FULLB_MARKER, "audit"],
    ["config", "--token-env", _FULLB_MARKER, "build-https", "add", "example.com"],
    ["config", "build-https", "--token-source", _FULLB_MARKER, "add", "example.com"],
    ["audit", "--", "--token-file", _FULLB_MARKER],
    ["audit", "--", "--token-file=" + _FULLB_MARKER],
    ["audit", "--unknown", "--token-file=first " + _FULLB_MARKER],
    ["audit", "--unknown", "--token-file", "first " + _FULLB_MARKER],
    ["audit", "--unknown", "--token-file=first\n" + _FULLB_MARKER],
    ["audit", "--unknown", "--token-file", _FULLB_MARKER, "--token-file=" + _FULLB_MARKER],
    ["audit", "-h", "--token-file=" + _FULLB_MARKER],
    ["audit", "--tok=" + _FULLB_MARKER],
]
_FULLB_WHITESPACE_CASES = [
    ["--token-file=first " + _FULLB_MARKER, "audit"],
    ["config", "--token-source=first " + _FULLB_MARKER, "build-https", "add", "example.com"],
    ["audit", "--", "--token-file=first " + _FULLB_MARKER],
    ["audit", "--token-file=first " + _FULLB_MARKER, "--", "--token-file", _FULLB_MARKER],
]
_FULLB_SUBPROCESS_CASES = [
    ["--token-file", _FULLB_MARKER, "audit"],
    ["config", "--token-env", _FULLB_MARKER, "build-https", "add", "example.com"],
    ["config", "build-https", "--token-source", _FULLB_MARKER, "add", "example.com"],
    ["audit", "--", "--token-file", _FULLB_MARKER],
    ["audit", "--", "--token-file=" + _FULLB_MARKER],
    ["--token-file=first " + _FULLB_MARKER, "audit"],
]


@pytest.mark.parametrize("argv", _FULLB_CASES + _FULLB_WHITESPACE_CASES)
def test_rev5_fullb_entry_never_echoes(redaction_config, capsys, argv):
    # TASK-261003-3a6epx test_no_secret_echo / test_whitespace_and_separators,
    # through cli.main with an isolated config.
    cli.main(argv)
    output = capsys.readouterr()
    assert _FULLB_MARKER not in output.out + output.err


@pytest.mark.parametrize("argv", _FULLB_SUBPROCESS_CASES)
def test_rev5_fullb_subprocess_never_echoes(redaction_config, argv):
    # TASK-261003-3a6epx subprocess_repro.py, through python -m csk.
    proc = _run_module_entry(argv, extra_env={"CSK_CONFIG": str(redaction_config.path)})
    assert proc.returncode == 2
    assert _FULLB_MARKER not in proc.stdout + proc.stderr


_DELTA_MARKER = "synthetic-delta-marker"
_DELTA_SUBPROCESS_CASES = [
    ["--token-file", _DELTA_MARKER, "audit"],
    ["config", "--token-source", _DELTA_MARKER, "build-https", "add", "example.test/team"],
    ["config", "build-https", "--token-env", _DELTA_MARKER, "add", "example.test/team"],
    ["config", "--token-env", _DELTA_MARKER, "build-https", "add", "example.test/team"],
]
_DELTA_DIRECT_PREFIXES = [[], ["config"], ["config", "build-https"], ["shell-init"]]


@pytest.mark.parametrize("argv", _DELTA_SUBPROCESS_CASES)
def test_rev5_delta_value_before_declaring_parser_never_echoes(argv):
    # TASK-261003-3gljgb test_token_value_before_declaring_parser_does_not_echo.
    proc = _run_module_entry(argv)
    assert proc.returncode == 2
    assert _DELTA_MARKER not in proc.stdout + proc.stderr


@pytest.mark.parametrize("prefix", _DELTA_DIRECT_PREFIXES)
def test_rev5_delta_direct_parser_value_never_echoes(prefix, capsys):
    # TASK-261003-3gljgb test_public_parser_separate_value_not_absorbed: the
    # parser collects values from the argv it parses, so direct parse_args
    # callers are guarded without going through cli.main.
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([*prefix, "--token-file", _DELTA_MARKER])
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert _DELTA_MARKER not in output.out + output.err


# ---------------------------------------------------------------------------
# Refusal shape, dispatch guard, and matcher hardening.
# ---------------------------------------------------------------------------

_REFUSAL_CASES = [
    ["audit", "--", "--token-file", "MARK"],
    ["audit", "--", "--token-file=MARK"],
    ["audit", "--token-file", "normal", "--", "--token-file", "MARK"],
    ["config", "build-https", "add", "example.test/g", "--", "--token-env", "MARK"],
    ["check", "--token-file", "MARK"],
    ["install", "--token-file=MARK"],
    ["audit", "--token-source", "MARK"],
    ["audit", "--tokenx", "MARK"],
    ["audit", "--tok", "MARK"],
]


@pytest.mark.parametrize("argv", _REFUSAL_CASES)
def test_redaction_refusal_is_fixed_message(redaction_config, capsys, argv):
    # A --token* name where it is not declared, or after --, is refused with
    # the fixed message and exit 2; the value never appears.
    marker = "synthetic-refusal-marker"
    shaped = [piece.replace("MARK", marker) for piece in argv]
    assert cli.main(shaped) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert "--token is refused" in output.err
    assert "CSK_REGISTRY_TOKEN" in output.err


def test_redaction_dispatch_guard_redacts_consumed_value(redaction_config, capsys):
    # The value guard also covers dispatch diagnostics: here the marker is
    # both the --token-file value and the project target, so the alias error
    # must carry <redacted>, proving the guard is active (not merely that
    # the marker is absent). Production call site: cli.main dispatch-error
    # handler for the audit alias error.
    marker = "synthetic-dispatch-marker"
    argv = ["audit", "--token-file", marker, marker]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert "<redacted>" in output.err


_CASE_VARIANTS = ["--TOKEN-FILE", "--Token-Source", "--TOKEN-ENV", "--TOK", "--T"]
_DASH_VARIANTS = ["-token-file", "-token-source", "-token-env", "-tok", "-t"]


@pytest.mark.parametrize("name", _CASE_VARIANTS + _DASH_VARIANTS)
@pytest.mark.parametrize("form", ["separate", "equals"])
@pytest.mark.parametrize("prefix", [["audit"], ["check"], ["config", "build-https", "add", "example.test/g"]])
def test_redaction_matcher_covers_case_and_dash_variants(redaction_config, capsys, name, form, prefix):
    # Hardening beyond the A/B corpus: case variants and single-dash
    # spellings are token-like names too (they echo verbatim in argparse
    # unrecognized-argument diagnostics), so they are refused and their
    # values are collected. Production call site: cli.main pre-parse.
    marker = "synthetic-variant-marker"
    suffix = [f"{name}={marker}"] if form == "equals" else [name, marker]
    assert cli.main([*prefix, *suffix]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert "--token is refused" in output.err


def test_redaction_single_character_value_keeps_message_intact(redaction_config, publish_record, capsys):
    # Stated bound (8 characters, extreme case): single-character values are
    # not collected (a lone character carries no credential entropy and
    # occurs in every diagnostic). The shaped error keeps its exact text.
    argv = _publish_argv(publish_record) + ["--token-file", "-"]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "cannot read --token-file as a regular UTF-8 file" in output.err
    assert "<redacted>" not in output.err


def test_redaction_separator_is_never_collected_as_value(redaction_config, capsys):
    # A bare -- is never an option value (argparse reports a missing value),
    # so it is excluded from collection and the diagnostic stays exact.
    assert cli.main(["audit", "--token-file", "--"]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "expected one argument" in output.err
    assert "<redacted>" not in output.err


# ---------------------------------------------------------------------------
# Randomized property over parsers, spellings, forms, positions, --, repeats.
# ---------------------------------------------------------------------------

def _all_prefixes():
    found = [()]

    def walk(parser, prefix):
        for action in parser._actions:
            if isinstance(action, cli.argparse._SubParsersAction):
                for name, child in action.choices.items():
                    found.append(prefix + (name,))
                    walk(child, prefix + (name,))

    walk(cli.build_parser(), ())
    found.append(("check",))
    return found


_PROPERTY_SPELLINGS = [
    "--token", "--token-file", "--token-source", "--token-env",
    "--tok", "--t", "--to", "--toke", "--token-f", "--tokenx",
    "-t", "-token-file",
]

_PROPERTY_VALUE_SHAPES = ["plain", "backslash", "newline", "both-quotes", "winpath", "tab"]


def _property_value(index, shape):
    # Escape-class values (rev-2 finding guard-misses-repr-forms): argparse
    # renders some of these with repr() in invalid-choice diagnostics. Every
    # shape is at least 8 characters, so all are inside the redaction bound.
    if shape == "backslash":
        return f"SEC\\RET\\x-{index:04d}"
    if shape == "newline":
        return f"SEC\nRET-{index:04d}"
    if shape == "both-quotes":
        return f"SEC'RET\"-{index:04d}"
    if shape == "winpath":
        return f"C:\\redact\\prop-{index:04d}"
    if shape == "tab":
        return f"SEC\tRET-{index:04d}"
    return f"synthetic-redaction-prop-{index:04d}"


def test_redaction_property_random_positions_forms_separators(redaction_config, capsys):
    # Amendment 2 required property: random positions (append/prepend/after
    # --/repeat/-h), forms (separate/equals, plus attached for single-dash),
    # abbreviations and value shapes, through cli.main. Neither the marker
    # nor its repr rendering appears in either stream. Seed: 261004.
    prefixes = _all_prefixes()
    assert len(prefixes) == 44  # root + 42 subcommand prefixes + check
    rng = random.Random(261004)
    failures = []
    for index in range(600):
        shape = _PROPERTY_VALUE_SHAPES[rng.randrange(len(_PROPERTY_VALUE_SHAPES))]
        marker = _property_value(index, shape)
        assert len(marker) >= 8
        prefix = list(prefixes[rng.randrange(len(prefixes))])
        spelling = _PROPERTY_SPELLINGS[rng.randrange(len(_PROPERTY_SPELLINGS))]
        single_dash = spelling.startswith("-") and not spelling.startswith("--")
        form = rng.randrange(3 if single_dash else 2)
        if form == 0:
            suffix = [spelling, marker]
        elif form == 1:
            suffix = [f"{spelling}={marker}"]
        else:
            suffix = [spelling + marker]
        placement = rng.randrange(5)
        if placement == 0:
            argv = prefix + suffix
        elif placement == 1:
            argv = suffix + prefix
        elif placement == 2:
            argv = prefix + ["--"] + suffix
        elif placement == 3:
            argv = prefix + suffix + suffix
        else:
            argv = prefix + suffix + ["-h"]
        code = cli.main(argv)
        output = capsys.readouterr()
        text = output.out + output.err
        if marker in text or repr(marker)[1:-1] in text or code not in (0, 2):
            failures.append((argv, code, output.out[-200:], output.err[-300:]))
    assert not failures, f"{len(failures)} property cases leaked: {failures[:6]}"


# ---------------------------------------------------------------------------
# Rev-2 mechanism pins: repeats, longest-first, repr renderings, attached
# single-dash shorts, the 8-character bound, and the stderr choke point.
# ---------------------------------------------------------------------------

def test_redaction_repeated_distinct_values_both_redacted(redaction_config, capsys):
    # Two distinct values echoed in one diagnostic (before-subcommand =
    # form): the collector keeps every occurrence, so both are redacted.
    # Kills the narrowing mutant collection-first-value-only.
    # Production call sites: _collect_token_values + parser error guard.
    first = "SECRET-REPEAT-FIRST-1"
    second = "SECRET-REPEAT-SECOND-2"
    argv = [f"--token-file={first}", f"--token-file={second}", "audit"]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    text = output.out + output.err
    assert first not in text
    assert second not in text
    assert text.count("<redacted>") >= 2


def test_redaction_overlapping_values_longest_first(redaction_config, capsys):
    # One collected value extends the other: longest-first replacement keeps
    # the longer value's tail from leaking beside <redacted>.
    # Kills the narrowing mutant redaction-shortest-first.
    # Production call site: _redact_values via the parser error guard.
    short = "PREFIX-SECRET"
    long = "PREFIX-SECRET-SUFFIX"
    argv = [f"--token-file={short}", f"--token-file={long}", "audit"]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    text = output.out + output.err
    assert short not in text
    assert long not in text
    assert "-SUFFIX" not in text
    assert "<redacted>" in text


_REPR_ESCAPE_VALUES = [
    "SEC\\RET\\x-1",  # backslashes: repr doubles them
    "SEC\nRET-656",  # newline: repr shows backslash-n
    "SEC\tRET-661",  # tab: repr shows backslash-t
    "SEC\rRET-77",  # carriage return
    "SEC'RET\"-3",  # both quote kinds: repr escapes the single quote
    "C:\\tokens\\file-91",  # Windows path
    "SEC'RET-71",  # single quote only: repr uses double quotes
    'SEC"RET-72',  # double quote only
]


def _forbidden_forms(value):
    # Every rendering a diagnostic could plausibly carry, computed
    # independently of the implementation (repr, JSON, unicode_escape).
    forms = {value, repr(value), repr(value)[1:-1]}
    dumped = json.dumps(value)
    forms.add(dumped)
    forms.add(dumped[1:-1])
    forms.add(value.encode("unicode_escape").decode("ascii"))
    return {form for form in forms if form}


@pytest.mark.parametrize("value", _REPR_ESCAPE_VALUES)
def test_redaction_repr_escaped_values_never_echo(redaction_config, capsys, value):
    # Escape-class values reach argparse's invalid-choice diagnostic, which
    # renders the value with repr(): the guard replaces every rendering, not
    # just the raw bytes. Kills the narrowing mutant
    # guard-handles-raw-form-only (backslash, control and both-quotes cases).
    # Production call sites: _collect_token_values + parser error guard.
    assert len(value) >= 8
    assert cli.main(["--token-file", value, "audit"]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    text = output.out + output.err
    leaked = [form for form in _forbidden_forms(value) if form in text]
    assert not leaked
    assert "<redacted>" in text


_ATTACHED_SHORT_PREFIXES = ["-t", "-to", "-tok", "-toke", "-token", "-T"]


@pytest.mark.parametrize("prefix", _ATTACHED_SHORT_PREFIXES)
def test_redaction_attached_short_form_refused_without_echo(redaction_config, capsys, prefix):
    # Attached single-dash short forms (-tVALUE): the one matcher reads the
    # name, so the pre-parse refusal fires before argparse echoes the whole
    # element. Kills collection-attached-short-dropped.
    # Production call site: cli.main pre-parse refusal.
    value = "ATTACHED-SECRET-1"
    assert cli.main(["audit", prefix + value]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    text = output.out + output.err
    assert value not in text
    assert prefix + value not in text
    assert "--token is refused" in text


@pytest.mark.parametrize("prefix", _ATTACHED_SHORT_PREFIXES)
def test_redaction_attached_short_form_guarded_in_direct_parser(capsys, prefix):
    # Same class through parser.parse_args directly (bypassing the
    # pre-parse): the collector takes the attached remainder as the value,
    # so the unrecognized-arguments echo is redacted.
    # Production call site: _CskArgumentParser error guard.
    value = "ATTACHED-SECRET-2"
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["audit", prefix + value])
    assert exc.value.code == 2
    output = capsys.readouterr()
    text = output.out + output.err
    assert value not in text
    assert "<redacted>" in text


def test_redaction_no_parser_defines_single_dash_token_shorts():
    # Assumption pin for the -t* handling: no parser defines a single-dash
    # -t* option, so treating every -t* spelling as token-like refuses
    # nothing legitimate. Fails if someone adds one, forcing a revisit.
    stack = [cli.build_parser(), cli._build_check_parser()]
    seen = []
    while stack:
        parser = stack.pop()
        for action in parser._actions:
            for option in getattr(action, "option_strings", []):
                if option.startswith("-") and not option.startswith("--") and option[1:2].lower() == "t":
                    seen.append((parser.prog, option))
            if isinstance(action, cli.argparse._SubParsersAction):
                stack.extend(action.choices.values())
    assert not seen


def test_redaction_escaper_matches_repr():
    # The guard's quoting helper agrees with repr() on the escape classes it
    # claims (pins _py_quoted_inner against drift; behavior is proved by
    # test_redaction_repr_escaped_values_never_echo through cli.main).
    values = [
        "plain-abcdef-marker",
        "SEC\\RET\\x-1",
        "SEC\nRET-656",
        "a'b-quote-marker",
        'a"b-quote-marker',
        "a'b\"both-marker",
        "tab\tseparated-marker",
        "C:\\tok\\file-marker",
        "uni-\u00e9-\u00df-\u03a9-marker",
        "null\x00byte-marker",
        "del\x7fchar-marker",
        "c1\x85control-marker",
        "bell\x07back\x08vtab\x0bform\x0c-marker",
        "pct-%(prog)s-marker",
    ]
    for value in values:
        rendered = repr(value)
        assert rendered[0] in "'\""
        assert cli._py_quoted_inner(value, rendered[0]) == rendered[1:-1]


def test_redaction_length_bound_short_value_keeps_message_intact(redaction_config, capsys):
    # Stated bound, lower side: a 7-character value is not a credential, so
    # the diagnostic keeps its exact text instead of redacting a substring
    # that would also mangle unrelated words. Kills length-bound-admits-short.
    # Production call site: _collect_token_values length check.
    value = "Abc1234"
    assert cli.main(["--token-file", value, "audit"]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert f"invalid choice: '{value}'" in output.err
    assert "<redacted>" not in output.err


def test_redaction_length_bound_eight_char_value_is_redacted(redaction_config, capsys):
    # Stated bound, upper side: an 8-character value is a credential and is
    # redacted. Kills length-bound-drops-eight.
    value = "Abc12345"
    assert cli.main(["--token-file", value, "audit"]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    text = output.out + output.err
    assert value not in text
    assert "<redacted>" in text


def test_redaction_stderr_guard_installed_in_main(redaction_config, monkeypatch, capsys):
    # The single output guard is installed as the process-stderr choke point
    # for the whole main() call, so every current and future _cmd_*
    # diagnostic is covered with no per-site call. The real stderr object is
    # restored afterwards. Kills redaction-stderr-wrapper-dropped together
    # with the dispatch-guard test.
    seen = {}

    def fake_dispatch(args):
        seen["stderr"] = sys.stderr
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "_dispatch", fake_dispatch)
    assert cli.main(["audit"]) == cli.EXIT_OK
    assert isinstance(seen["stderr"], cli._RedactingStderr)
    assert not isinstance(sys.stderr, cli._RedactingStderr)
    capsys.readouterr()

