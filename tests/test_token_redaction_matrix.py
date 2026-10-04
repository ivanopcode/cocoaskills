"""Credential-option suppression (rev 3: suppress, do not redact).

Ground truth: the A/B corpus from spm TASK-261004-24ty6g (matrix.json, 1848
probes, committed as tests/fixtures/c1-redaction-matrix.json; extra.json, 28
probes; login.json, 2 probes: 1878 in total) plus panel reproductions across
all rounds (rev-5 TASK-261003-104dow/3a6epx/3gljgb, rev-1, rev-2
TASK-261004-fguh76/1qmehb/vj1er1).

Invariant (rev 3): if raw argv contains ANY credential-carrying option
(--token*, abbreviations, attached single-dash shorts, '=' forms, after
'--'), every usage/error diagnostic is ONE fixed message with no argv bytes,
exit 2. Successful output is unchanged; an accepted --token-env NAME may be
shown (C6.env-list). No length bound: even 1-character values suppress.

Production call sites: cli.argv_has_credential_option (one predicate), the
process-stderr choke point installed by cli.main plus _CskArgumentParser
error/exit for direct parser callers (one hook).
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

# The ONE fixed message, pinned literally (not via the implementation
# constant) so a drift in either fails.
SUPPRESSED = (
    "csk: invalid usage of a command with a credential option; "
    "the details are suppressed so that no secret is echoed. "
    "Run 'csk <command> --help'."
)


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
    # as 0-or-2: errors with a credential option suppress to exit 2, while
    # -h still wins (exit 0, safe help) when present.
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
    # by audit, so argparse fails and the hook suppresses.
    marker = f"SECRETMARK-C6-audit{mode}"
    assert cli.main(["audit", "--token-source", marker, mode]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert output.err == SUPPRESSED + "\n"
    assert output.out == ""


@pytest.mark.parametrize("mode", _C6_MODES)
def test_ground_truth_c6_https_invalid_selector_shaped(redaction_config, capsys, mode):
    # Rows C6.https--json/--verbose/--debug: an invalid --token-source value
    # fails the shaped validator, and the hook suppresses (broad: any
    # credential option present suppresses every error diagnostic).
    marker = f"SECRETMARK-C6-https{mode}"
    argv = ["config", "build-https", "add", "example.test/g", "--token-source", marker, mode]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert output.err == SUPPRESSED + "\n"
    assert output.out == ""


@pytest.mark.parametrize("mode", _C6_MODES)
def test_ground_truth_c6_audit_file_never_echoes_path(redaction_config, capsys, mode):
    # Rows C6.audit-file--json/--verbose/--debug: a --token-file path is
    # never echoed in errors, on the parse path or the dispatch path.
    marker = f"SECRETMARK-C6-audit-file{mode}"
    assert cli.main(["audit", "skill-a", "--token-file", marker, mode]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert output.err == SUPPRESSED + "\n"


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
    # Rows C2.invalid/oversize/nonutf8/crlf/spaces: --token-file is in argv,
    # so the dispatch failure suppresses (broad hook). Neither the file
    # content nor the path appears; the shaped fragment is replaced.
    del fragment
    marker = f"SECRETMARK-C2-{kind}"
    path = tmp_path / f"token-{kind}"
    path.write_bytes(builder(marker))
    path.chmod(0o600)
    argv = _publish_argv(publish_record) + ["--token-file", str(path)]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert str(path) not in output.out + output.err
    assert output.err == SUPPRESSED + "\n"


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
    # Rows C2.fifo/symlink/hardlink/device: --token-file is in argv, so the
    # refusal suppresses instead of naming the cause.
    del fragment
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
    assert output.err == SUPPRESSED + "\n"


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
    # A --token* name where it is not declared, or after --, fails in
    # argparse and suppresses to the ONE fixed message, exit 2.
    marker = "synthetic-refusal-marker"
    shaped = [piece.replace("MARK", marker) for piece in argv]
    assert cli.main(shaped) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert output.err == SUPPRESSED + "\n"
    assert output.out == ""


def test_redaction_dispatch_guard_redacts_consumed_value(redaction_config, capsys):
    # The hook also covers dispatch diagnostics: here the marker is both the
    # --token-file value and the project target, so the alias error must
    # suppress, proving the hook is active (not merely that the marker is
    # absent). Production call site: _SuppressingStderr over the audit alias
    # error. Kills the narrowing mutant hook-bypassed-on-dispatch.
    marker = "synthetic-dispatch-marker"
    argv = ["audit", "--token-file", marker, marker]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert output.err == SUPPRESSED + "\n"


_CASE_VARIANTS = ["--TOKEN-FILE", "--Token-Source", "--TOKEN-ENV", "--TOK", "--T"]
_DASH_VARIANTS = ["-token-file", "-token-source", "-token-env", "-tok", "-t"]


@pytest.mark.parametrize("name", _CASE_VARIANTS + _DASH_VARIANTS)
@pytest.mark.parametrize("form", ["separate", "equals"])
@pytest.mark.parametrize("prefix", [["audit"], ["check"], ["config", "build-https", "add", "example.test/g"]])
def test_redaction_matcher_covers_case_and_dash_variants(redaction_config, capsys, name, form, prefix):
    # Hardening beyond the A/B corpus: case variants and single-dash
    # spellings are credential options too (they echo verbatim in argparse
    # unrecognized-argument diagnostics), so errors with them suppress.
    # Production call site: cli.argv_has_credential_option.
    marker = "synthetic-variant-marker"
    suffix = [f"{name}={marker}"] if form == "equals" else [name, marker]
    assert cli.main([*prefix, *suffix]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert output.err == SUPPRESSED + "\n"


def test_redaction_single_character_value_suppresses(redaction_config, publish_record, capsys):
    # No length bound in rev 3: even a 1-character --token-file value counts,
    # so the dispatch failure suppresses instead of showing shaped text.
    argv = _publish_argv(publish_record) + ["--token-file", "-"]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert output.err == SUPPRESSED + "\n"


def test_redaction_separator_missing_value_suppresses(redaction_config, capsys):
    # A bare -- after --token-file is a missing value; argparse reports it
    # and the hook suppresses.
    assert cli.main(["audit", "--token-file", "--"]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert output.err == SUPPRESSED + "\n"


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
    # Escape-class values: argparse renders some of these with repr() in
    # invalid-choice diagnostics. Suppression replaces the whole diagnostic,
    # so neither raw nor repr may appear.
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
    # Required property: random positions (append/prepend/after --/repeat/
    # -h), forms (separate/equals, plus attached for single-dash),
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
# Suppression mechanism pins: predicate, hook, attached shorts, and the
# rev-2 repeat-of classes (attached-with-=, U+00A0/U+00AD, separator before
# the subcommand). Each repeat-of class has a named regression test plus a
# narrowing mutant (see tests/audit_secrets_mutants.py).
# ---------------------------------------------------------------------------

_PREDICATE_POSITIVE = [
    ["--token", "x"],
    ["--token=x"],
    ["--token-file", "x"],
    ["--token-file=x"],
    ["--token-source", "x"],
    ["--token-env", "x"],
    ["--tokenx", "x"],
    ["--token-f", "x"],
    ["--tok", "x"],
    ["--tok=x"],
    ["--to", "x"],
    ["--t", "x"],
    ["--toke", "x"],
    ["--TOKEN-FILE", "x"],
    ["-t", "x"],
    ["-t=x"],
    ["-to", "x"],
    ["-tok", "x"],
    ["-toke", "x"],
    ["-token-file", "x"],
    ["-token-file=x"],
    ["-T", "x"],
    ["audit", "-tVALUE1234"],
    ["audit", "-tVALUE1234== with pad"],
    ["audit", "-toSECRET=x"],
    ["audit", "-tokSECRET=x"],
    ["audit", "-tokeSECRET=x"],
    ["audit", "-tokenSECRET=x"],
    ["audit", "-TSECRET=x"],
    ["audit", "--", "--token-file", "x"],
    ["--", "--token-file=x"],
    ["--token-file", "--", "x", "audit"],
]

_PREDICATE_NEGATIVE = [
    [],
    ["audit"],
    ["--tag", "x"],
    ["--target", "x"],
    ["--tokn", "x"],
    ["--tokSECRET"],
    ["--help"],
    ["-h"],
    ["--"],
    ["--version"],
    ["check", "--all"],
]


@pytest.mark.parametrize("argv", _PREDICATE_POSITIVE)
def test_suppression_predicate_detects_credential_option(argv):
    assert cli.argv_has_credential_option(argv) is True


@pytest.mark.parametrize("argv", _PREDICATE_NEGATIVE)
def test_suppression_predicate_ignores_non_credential(argv):
    assert cli.argv_has_credential_option(argv) is False


def test_suppression_message_is_one_fixed_text(redaction_config, capsys):
    # The hook output is byte-exact: ONE fixed message on stderr, nothing on
    # stdout, exit 2, containing no argv bytes. Production call sites: parser
    # error via _SuppressingStderr.
    marker = "synthetic-shape-marker"
    argv = ["audit", "--token-file", marker, "--bogus-flag"]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert output.err == SUPPRESSED + "\n"
    assert output.out == ""
    assert marker not in output.err
    assert "--bogus-flag" not in output.err
    assert "usage:" not in output.err


_ATTACHED_SHORT_PREFIXES = ["-t", "-to", "-tok", "-toke", "-token", "-T"]


@pytest.mark.parametrize("prefix", _ATTACHED_SHORT_PREFIXES)
def test_redaction_attached_short_form_refused_without_echo(redaction_config, capsys, prefix):
    # Attached single-dash short forms (-tVALUE): the predicate holds, so the
    # argparse error suppresses. Kills predicate-misses-short-forms.
    # Production call site: cli.argv_has_credential_option + parser error hook.
    value = "ATTACHED-SECRET-1"
    assert cli.main(["audit", prefix + value]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    text = output.out + output.err
    assert value not in text
    assert prefix + value not in text
    assert output.err == SUPPRESSED + "\n"


@pytest.mark.parametrize("prefix", _ATTACHED_SHORT_PREFIXES)
def test_redaction_attached_short_form_guarded_in_direct_parser(capsys, prefix):
    # Same class through parser.parse_args directly: parse_known_args sets
    # the flag, error() suppresses. Production call site:
    # _CskArgumentParser error hook.
    value = "ATTACHED-SECRET-2"
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["audit", prefix + value])
    assert exc.value.code == 2
    output = capsys.readouterr()
    text = output.out + output.err
    assert value not in text
    assert output.err == SUPPRESSED + "\n"


def test_redaction_no_parser_defines_single_dash_token_shorts():
    # Assumption pin for the -t* handling: no parser defines a single-dash
    # -t* option, so treating every -t* spelling as credential-like
    # suppresses nothing legitimate. Fails if someone adds one.
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


_ATTACHED_EQUALS_VALUES = [
    "SECRETVALUE1234==",
    "SECRETVALUE1234=x",
    "=SECRETVALUE1234",
    "a=SECRETVALUE1234",
    "SECRETVALUE1234/+=",
]


@pytest.mark.parametrize("prefix", ["-t", "-to", "-tok", "-toke", "-T"])
@pytest.mark.parametrize("value", _ATTACHED_EQUALS_VALUES)
def test_rev2_attached_short_with_equals_suppresses(redaction_config, capsys, prefix, value):
    # Rev-2 F1 (repeat-of rev-1 attached/short-form class): attached
    # single-dash value containing '='. Panels fguh76/1qmehb/vj1er1.
    # Kills predicate-attached-equals-excluded.
    argv = ["audit", prefix + value]
    assert cli.argv_has_credential_option(argv) is True
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert value not in output.out + output.err
    assert prefix + value not in output.out + output.err
    assert output.err == SUPPRESSED + "\n"


@pytest.mark.parametrize(
    "argv_value",
    [
        "Ünïcode tokenx",
        "café­xxxxx",
        "ab'cd\"e fgh",
    ],
)
def test_rev2_nbsp_shy_value_suppresses(redaction_config, capsys, argv_value):
    # Rev-2 F3 (repeat-of rev-1 repr-form class): values with U+00A0/U+00AD,
    # which repr() escapes as \\xa0/\\xad. Panel vj1er1 F1. The whole
    # diagnostic suppresses, so no rendering can leak. Kills
    # hook-bypassed-on-parser-error.
    marker = argv_value
    argv = ["config", "build-https", "--token-env", marker, "add", "x"]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert repr(marker) not in output.out + output.err
    assert output.err == SUPPRESSED + "\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["--token-file", "--", "SECRETVALUE12", "audit"],
        ["config", "--token-env", "--", "SECRETVALUE12", "build-https", "add", "example.test/g"],
        ["config", "build-https", "--token-source", "--", "SECRETVALUE12", "add", "example.test/g"],
    ],
)
def test_rev2_separator_after_token_before_subcommand_suppresses(redaction_config, capsys, argv):
    # Rev-2 F2 (repeat-of rev-1 --/position class): --token-x before its
    # declaring subcommand with '-- VALUE'. On Python 3.13+ argparse echoes
    # VALUE as the command word; suppression replaces it. Panel 1qmehb F2.
    # Kills predicate-post-subcommand-only.
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "SECRETVALUE12" not in output.out + output.err
    assert output.err == SUPPRESSED + "\n"


def test_rev2_token_option_as_value_suppresses(redaction_config, capsys):
    # Rev-2 F5: --token-file followed by another token-like element. The
    # separate-form value is token-like; argparse consumes it and the
    # resulting error suppresses to ONE fixed message (no mangled text).
    assert cli.main(["audit", "--token-file", "--token-env", "x"]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert output.err == SUPPRESSED + "\n"
    assert "--token-env" not in output.err


def test_rev2_enum_usage_not_garbled(redaction_config, capsys):
    # Rev-2 F6: --token-source with a public enum value plus another error.
    # Suppression replaces usage+error wholesale, so the enum choices are
    # hidden, never rewritten. (False-positive cost: less detail.)
    argv = ["config", "build-https", "add", "example.test/g",
           "--token-source", "git-credentials", "--username"]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert output.err == SUPPRESSED + "\n"
    assert "git-credentials" not in output.err + output.out
    assert "usage:" not in output.err


@pytest.mark.parametrize(
    "name",
    ["MY_TOKEN", "_private", "A", "a1_", "SECRETMARK_C6_ENV", "X" * 128],
)
def test_token_env_valid_name_accepted(redaction_config, capsys, name):
    assert cli.main(["config", "build-https", "add", "example.test/g",
                     "--token-env", name]) == cli.EXIT_OK
    output = capsys.readouterr()
    assert output.err == ""


@pytest.mark.parametrize(
    "name",
    ["not a name!", "ghp-abcd", "a.b", "9INVALID", "", "X" * 129,
     "SECRETMARK-C6-x", "Ünïcode tokenx", "a=b", "-TSECRET=x"],
)
def test_token_env_invalid_name_refused_suppressed(redaction_config, capsys, name):
    # A --token-env value outside ^[A-Za-z_][A-Za-z0-9_]{0,127}$ is refused
    # at parse time; the hook suppresses (predicate holds).
    assert cli.main(["config", "build-https", "add", "example.test/g",
                     "--token-env", name]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    if name:
        assert name not in output.out + output.err
    assert output.err == SUPPRESSED + "\n"


def test_suppression_valid_credential_help_still_shows(redaction_config, capsys):
    # Help (exit 0) is not an error diagnostic: valid credential + -h shows
    # help with no secret. Invalid credential + -h also shows help (safe).
    assert cli.main(["audit", "--token-file", "/tmp/x", "-h"]) == cli.EXIT_OK
    output = capsys.readouterr()
    assert "usage:" in output.out
    assert output.err == ""


def test_suppression_error_without_credential_keeps_detail(redaction_config, capsys):
    # No credential option: detailed diagnostics are unchanged (no false
    # suppression cost).
    assert cli.main(["audit", "--bogus-flag"]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "unrecognized arguments: --bogus-flag" in output.err
    assert output.err != SUPPRESSED + "\n"


def test_redaction_stderr_hook_installed_in_main(redaction_config, monkeypatch, capsys):
    # The single output hook buffers BOTH streams for the whole main() call
    # when the predicate holds, and restores the real objects afterwards.
    # Without a credential option there is no wrapper (passthrough). Kills
    # suppression-stderr-wrapper-dropped together with the dispatch test.
    seen = {}

    def fake_dispatch(args):
        seen["stderr"] = sys.stderr
        seen["stdout"] = sys.stdout
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "_dispatch", fake_dispatch)
    assert cli.main(["audit", "--token-file", "x"]) == cli.EXIT_OK
    assert isinstance(seen["stderr"], cli._BufferingStream)
    assert isinstance(seen["stdout"], cli._BufferingStream)
    assert not isinstance(sys.stderr, cli._BufferingStream)
    assert not isinstance(sys.stdout, cli._BufferingStream)
    capsys.readouterr()
    assert cli.main(["audit"]) == cli.EXIT_OK
    assert not isinstance(seen["stderr"], cli._BufferingStream)
    capsys.readouterr()


# ---------------------------------------------------------------------------
# Generated test over random argv: no 8+ char credential-adjacent sequence on
# error (brief requirement). Positions, forms, unicode, control characters,
# '--', repeats; asserts against every Python behaviour by checking raw,
# repr, and casefold renderings rather than one version's exact text.
# ---------------------------------------------------------------------------

_SUPPRESSION_SPELLINGS = [
    "--token", "--token-file", "--token-source", "--token-env",
    "--tok", "--t", "--to", "--toke", "--token-f", "--tokenx",
    "--TOKEN-FILE", "-t", "-to", "-tok", "-toke", "-token", "-T",
    "-token-file",
]

_SUPPRESSION_VALUE_POOL = [
    "plain",
    "with space inside",
    "with=equals==",
    "base64+/==tail",
    "back\\slash",
    "new\nline",
    "tab\there",
    "cr\rhere",
    "both'quote\"kinds",
    "ünïcode-é-Ω",
    "nbsp inside",
    "shy­inside",
    "zero\x00byte",
    "del\x7fchar",
    "c1\x85control",
    "emoji-😀-tail",
    "pct-%(prog)s",
    "winpath-C:\\tok\\file",
    "quote'only",
    'dquote"only',
    "semi;colon",
    "dash-leading-value",
]


def _suppression_credential_runs(argv):
    # 8+ char sequences that follow a credential option name in raw argv:
    # the separate-form next element, the '=' remainder, and attached
    # single-dash remainders. The error output must contain none of them in
    # any rendering.
    runs = []
    for index, element in enumerate(argv):
        if not isinstance(element, str):
            continue
        name = element.split("=", 1)[0]
        lowered = name.lower()
        is_name = (
            lowered.startswith("--token")
            or lowered in ("--t", "--to", "--tok", "--toke")
            or lowered.startswith("-token")
            or lowered in ("-t", "-to", "-tok", "-toke")
        )
        if "=" in element and is_name:
            candidate = element.split("=", 1)[1]
            if len(candidate) >= 8:
                runs.append(candidate)
            continue
        if element.startswith("-") and not element.startswith("--"):
            lowered_element = element.lower()
            for prefix in ("-token", "-toke", "-tok", "-to", "-t"):
                if lowered_element.startswith(prefix) and len(element) > len(prefix):
                    candidate = element[len(prefix):]
                    if len(candidate) >= 8:
                        runs.append(candidate)
                    break
            else:
                if is_name and index + 1 < len(argv):
                    candidate = argv[index + 1]
                    if isinstance(candidate, str) and len(candidate) >= 8:
                        runs.append(candidate)
            continue
        if is_name and index + 1 < len(argv):
            candidate = argv[index + 1]
            if isinstance(candidate, str) and len(candidate) >= 8:
                runs.append(candidate)
    return runs


def _renderings(value):
    forms = {value, repr(value), repr(value)[1:-1]}
    try:
        forms.add(value.encode("unicode_escape").decode("ascii"))
    except Exception:
        pass
    forms.add(value.casefold())
    return {form for form in forms if form and len(form) >= 8}


def test_suppression_property_random_argv_no_credential_bytes_on_error(redaction_config, capsys):
    # Brief generated test: random argv over positions, forms, unicode,
    # control characters and '--'. On error (exit 2), no 8+ char sequence
    # following a credential option name appears in either stream in any
    # rendering. Seed: 261004. Runs through cli.main.
    prefixes = _all_prefixes()
    rng = random.Random(261004)
    failures = []
    for index in range(600):
        marker = (
            f"synth{index:04d}-"
            + _SUPPRESSION_VALUE_POOL[rng.randrange(len(_SUPPRESSION_VALUE_POOL))]
            + f"-{index:04d}"
        )
        prefix = list(prefixes[rng.randrange(len(prefixes))])
        spelling = _SUPPRESSION_SPELLINGS[rng.randrange(len(_SUPPRESSION_SPELLINGS))]
        single_dash = spelling.startswith("-") and not spelling.startswith("--")
        form = rng.randrange(3 if single_dash else 2)
        if form == 0:
            suffix = [spelling, marker]
        elif form == 1:
            suffix = [f"{spelling}={marker}"]
        else:
            suffix = [spelling + marker]
        placement = rng.randrange(6)
        if placement == 0:
            argv = prefix + suffix
        elif placement == 1:
            argv = suffix + prefix
        elif placement == 2:
            argv = prefix + ["--"] + suffix
        elif placement == 3:
            argv = prefix + suffix + suffix
        elif placement == 4:
            argv = prefix + suffix + ["-h"]
        else:
            argv = suffix + ["--"] + prefix
        code = cli.main(argv)
        output = capsys.readouterr()
        text = output.out + output.err
        if code not in (0, 2):
            failures.append((argv, code, "bad exit", output.err[-300:]))
            continue
        if code != 2:
            continue
        for run in _suppression_credential_runs(argv):
            for rendering in _renderings(run):
                if rendering in text:
                    failures.append((argv, code, rendering[:80], output.err[-300:]))
                    break
    assert not failures, f"{len(failures)} suppression cases leaked: {failures[:6]}"

