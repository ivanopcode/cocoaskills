"""Credential values never reach usage-error output (value-based redaction, rev 5).

Ground truth: the A/B corpus from spm TASK-261004-24ty6g (matrix.json, 1848
probes, committed as tests/fixtures/c1-redaction-matrix.json; extra.json, 28
probes; login.json, 2 probes: 1878 in total) plus the panel reproductions of
every round (rev-5 TASK-261003-104dow/3a6epx/3gljgb, rev-1, rev-2
TASK-261004-fguh76/1qmehb/vj1er1, rev-3 TASK-261005-24m2zb/2vt4q6/1c7lyk,
rev-4 TASK-261013-3hvd29/3f63mn/2j8txz).

Invariant (rev 5): usage errors never show argv values.  Every parser and
subparser prints the usage-error text rebuilt by ``usage_errors.closed_message``
from an allowlist of argparse templates; every argv-derived part is the fixed
text ``<value not shown>``.  There is no credential predicate: a typo, a
confusable spelling or an abbreviation is not special.  Credential option
values (``--token-file`` PATH, ``--token-env`` NAME, ``--token-source``) are
also never echoed by a command's own diagnostics.  Successful ``config ...
list`` output may show a configured NAME.

Documented bound: positional arguments are command data.  A value passed as a
positional, including one after ``--``, may appear in that command's own
errors.  ``usage_closure.marker_is_command_data`` decides, with the real
parser, which rows fall under the bound.

The closure properties themselves (generated fuzz, one test per argparse error
kind, imitation values, panel shapes) live in test_usage_error_closure.py.
"""

from __future__ import annotations

import http.server
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from csk import build_https, cli, config
from test_audit_publish import _record
import usage_closure
from usage_closure import is_closed_usage_error, marker_is_command_data, usage_failed

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


def assert_entry_output(argv, marker, code, out, err):
    """The rev-5 oracle for one real-entry run carrying ``marker``.

    A usage error (the parser rejected argv) exits 2 with a closed message and
    no marker.  A parse that succeeded may show the marker only when it landed
    in command data (the documented positional bound); a marker that was only
    ever a credential-option value is never shown.
    """
    text = out + err
    if usage_failed(argv):
        assert code == 2
        assert is_closed_usage_error(err), err
        assert marker not in text
    elif not marker_is_command_data(argv, marker):
        assert marker not in text


def test_c1_redaction_matrix_no_marker_in_output(redaction_config, capsys):
    # Generated test over the full A/B corpus: 1848 probes covering every
    # option family (O1--O5), position (P1--P7), form (F1--F5) and -h/--help
    # suffix (C5), through the production entry point cli.main.  No marker in
    # stdout or stderr, except where the marker is command data (the
    # documented positional bound), which the oracle decides.
    assert len(MATRIX_ROWS) == 1848
    failures = []
    bound = []
    for record in MATRIX_ROWS:
        row = record["row"]
        argv = list(record["argv"])
        markers = list(record["markers"])
        code = cli.main(argv)
        output = capsys.readouterr()
        leaked = _check_no_marker(output.out, output.err, markers)
        if code not in (0, 1, 2):
            failures.append((row, code, argv))
        if usage_failed(argv):
            if leaked or code != 2 or not is_closed_usage_error(output.err):
                failures.append((row, code, leaked, argv, output.err[-400:]))
        elif leaked:
            if any(marker_is_command_data(argv, marker) for marker in leaked):
                bound.append((row, argv, output.err))
            else:
                failures.append((row, code, leaked, argv, output.out[-300:], output.err[-500:]))
    assert not failures, f"{len(failures)} matrix rows leaked: {failures[:8]}"
    # Pinned from the committed fixture: every bound row is `audit -- --tok…=M`,
    # a positional shown by the command's own "Unknown project alias" error.
    assert len(bound) == 10
    for row, argv, err in bound:
        assert "--" in argv, row
        assert err.startswith("error: Unknown project alias: "), (row, err)


# ---------------------------------------------------------------------------
# C6: mode flags and the allowed persisted-NAME pair.
# ---------------------------------------------------------------------------

_C6_MODES = ["--json", "--verbose", "--debug"]


@pytest.mark.parametrize("mode", _C6_MODES)
def test_ground_truth_c6_audit_token_source_refused(redaction_config, capsys, mode):
    # Rows C6.audit--json/--verbose/--debug: --token-source is not declared
    # by audit, so argparse fails with a closed usage error.
    marker = f"SECRETMARK-C6-audit{mode}"
    assert cli.main(["audit", "--token-source", marker, mode]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert is_closed_usage_error(output.err)
    assert output.out == ""


@pytest.mark.parametrize("mode", _C6_MODES)
def test_ground_truth_c6_https_invalid_selector_shaped(redaction_config, capsys, mode):
    # Rows C6.https--json/--verbose/--debug: an invalid --token-source value
    # fails the shaped validator; the message names the allowed members only.
    marker = f"SECRETMARK-C6-https{mode}"
    argv = ["config", "build-https", "add", "example.test/g", "--token-source", marker, mode]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert "must be one of git-credentials, keyring" in output.err
    assert is_closed_usage_error(output.err)
    assert output.out == ""


@pytest.mark.parametrize("mode", _C6_MODES)
def test_ground_truth_c6_audit_file_never_echoes_path(redaction_config, capsys, mode):
    # Rows C6.audit-file--json/--verbose/--debug: a --token-file path is
    # never echoed.  --verbose/--debug are undeclared by audit (closed usage
    # error); --json parses and the unknown-alias dispatch diagnostic runs.
    marker = f"SECRETMARK-C6-audit-file{mode}"
    assert cli.main(["audit", "skill-a", "--token-file", marker, mode]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    if mode == "--json":
        assert "Unknown project alias: skill-a" in output.err
    else:
        assert is_closed_usage_error(output.err)


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
    # Rows C2.invalid/oversize/nonutf8/crlf/spaces: the token-file read fails
    # in dispatch with a shaped diagnostic (rev 4: execution diagnostics are
    # shown, never suppressed). Neither the file content nor the path appears.
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
    assert not is_closed_usage_error(output.err)


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
    # Rows C2.fifo/symlink/hardlink/device: the refusal names the shaped
    # cause (rev 4: execution diagnostics are shown), never the path.
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
    assert not is_closed_usage_error(output.err)


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
# Panel reproductions of rounds 1-5, frozen with original argv and markers.
# Each runs through the real entry point; the rev-5 oracle decides whether a
# marker may appear (positional command data) or must not.
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
    # TASK-261003-104dow test_real_entry_does_not_echo_credential_value.
    proc = _run_module_entry(argv)
    assert proc.returncode == 2
    assert_entry_output(argv, _FULLA_MARKER, proc.returncode, proc.stdout, proc.stderr)


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
    code = cli.main(argv)
    output = capsys.readouterr()
    assert_entry_output(argv, _FULLB_MARKER, code, output.out, output.err)


@pytest.mark.parametrize("argv", _FULLB_SUBPROCESS_CASES)
def test_rev5_fullb_subprocess_never_echoes(redaction_config, argv):
    # TASK-261003-3a6epx subprocess_repro.py, through python -m csk.
    proc = _run_module_entry(argv, extra_env={"CSK_CONFIG": str(redaction_config.path)})
    assert proc.returncode == 2
    assert_entry_output(argv, _FULLB_MARKER, proc.returncode, proc.stdout, proc.stderr)


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
    assert_entry_output(argv, _DELTA_MARKER, proc.returncode, proc.stdout, proc.stderr)


@pytest.mark.parametrize("prefix", _DELTA_DIRECT_PREFIXES)
def test_rev5_delta_direct_parser_value_never_echoes(prefix, capsys):
    # TASK-261003-3gljgb test_public_parser_separate_value_not_absorbed: the
    # error path lives on the parser, so direct parse_args callers get the
    # closed text without going through cli.main.
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([*prefix, "--token-file", _DELTA_MARKER])
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert _DELTA_MARKER not in output.out + output.err
    assert is_closed_usage_error(output.err)


# ---------------------------------------------------------------------------
# Refusal shape, positional bound, and the allowed NAME.
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
def test_redaction_refusal_is_closed_usage_error(redaction_config, capsys, argv):
    # A --token* name where it is not declared, or after --, is refused by
    # argparse with a closed message and exit 2 -- or, when it parsed as a
    # positional after --, it is command data and the oracle says so.
    marker = "synthetic-refusal-marker"
    shaped = [piece.replace("MARK", marker) for piece in argv]
    code = cli.main(shaped)
    output = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert_entry_output(shaped, marker, code, output.out, output.err)


def test_redaction_dispatch_reuse_echoes_target_not_credential(redaction_config, capsys):
    # Here the marker is both the --token-file value and the project target.
    # The positional is command data (documented bound): the alias error
    # echoes it AS THE TARGET.  Production call site: the dispatch-error
    # handler in cli.main.
    marker = "synthetic-dispatch-marker"
    argv = ["audit", "--token-file", marker, marker]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert output.err == f"error: Unknown project alias: {marker}\n"
    assert output.out == ""


def test_redaction_single_character_value_shaped_error(redaction_config, publish_record, capsys):
    # A 1-character --token-file value fails the file read with the shaped
    # cause (which never echoes the path); execution diagnostics are shown.
    argv = _publish_argv(publish_record) + ["--token-file", "-"]
    assert cli.main(argv) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "cannot read --token-file as a regular UTF-8 file" in output.err
    assert not is_closed_usage_error(output.err)


def test_redaction_separator_missing_value_is_closed(redaction_config, capsys):
    # A bare -- after --token-file is a missing value; argparse reports it
    # and the error path prints the closed text.
    assert cli.main(["audit", "--token-file", "--"]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "argument --token-file: expected one argument" in output.err
    assert is_closed_usage_error(output.err)


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
     "SECRETMARK-C6-x", "Ünïcode tokenx", "a=b", "-TSECRET=x",
     "FOO\n", "9", "-lead", "with space"],
)
def test_token_env_invalid_name_refused_without_echo(redaction_config, capsys, name):
    # A --token-env value outside [A-Za-z_][A-Za-z0-9_]{0,127} (fullmatch) is
    # refused at parse time with the static shape message.  "FOO\n" pins the
    # $-vs-fullmatch hole; digit-first and dash names pin the narrowing
    # mutants env-regex-allows-digits-first/dash.  A leading-dash value is an
    # option to argparse, so it is a missing value instead.
    assert cli.main(["config", "build-https", "add", "example.test/g",
                     "--token-env", name]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    message = usage_closure.error_message(output.err)
    assert message is not None
    if name.startswith("-"):
        assert message == "argument --token-env: expected one argument"
    else:
        assert message.startswith("argument --token-env: must be an environment variable name")
    if len(name) > 1:
        assert name not in output.out + output.err
    assert is_closed_usage_error(output.err)


def test_valid_credential_help_still_shows(redaction_config, capsys):
    # Help (exit 0) is not an error diagnostic and carries only parser-defined text.
    assert cli.main(["audit", "--token-file", "/tmp/x", "-h"]) == cli.EXIT_OK
    output = capsys.readouterr()
    assert "usage:" in output.out
    assert output.err == ""


def test_usage_error_without_credential_option_is_closed_too(redaction_config, capsys):
    # No credential option anywhere: the same closed text (there is no
    # predicate to be inside or outside of).
    assert cli.main(["audit", "--bogus-flag"]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "unrecognized arguments: <value not shown>" in output.err
    assert "--bogus-flag" not in output.err
    assert is_closed_usage_error(output.err)


def test_scope_nonzero_result_keeps_output_and_exit_code(redaction_config, capsys):
    # Rev-4 scope (rev-3 panels TASK-261005-24m2zb F1 / TASK-261005-2vt4q6
    # F1): command execution output and exit codes are never buffered,
    # replaced or remapped. An audit that legitimately reports BLOCK (exit 1)
    # keeps its report on stdout and its exit code even with a declared
    # credential option present. Production call site: cli.main (no hook on
    # the result path). Any reintroduced output buffering fails this test.
    from csk.audit import pipeline as audit_pipeline
    from csk.audit import runner as audit_runner
    from csk.audit.model import Decision

    with (
        patch.object(audit_runner, "audit_global",
                     return_value=[SimpleNamespace(decision=Decision.BLOCK)]),
        patch.object(audit_pipeline, "render_reports",
                     return_value="REPORT: BLOCK foo"),
    ):
        assert cli.main(["audit", "--global"]) == cli.EXIT_PARTIAL_FAIL
        plain = capsys.readouterr()
        assert cli.main(["audit", "--global", "--token-file", "tok"]) == cli.EXIT_PARTIAL_FAIL
        guarded = capsys.readouterr()
    assert plain.out == guarded.out == "REPORT: BLOCK foo\n"
    assert plain.err == guarded.err == ""
    assert "suppressed" not in guarded.out + guarded.err
