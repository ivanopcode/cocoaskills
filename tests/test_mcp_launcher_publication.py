"""Complete install refusal preserves source snapshots and every managed surface."""
from __future__ import annotations

import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from csk import cli, mcp_launchers as launchers, mcp_project
from csk.sources import publish
from publication_capture import PublicationCapture
from test_mcp_install_acceptance import _case, _snapshot, _assert_seed_installed, SURFACES
from test_mcp_launchers import bundle, declaration, good_verification


class _View:
    def __init__(self, original, **values):
        self._original = original
        self.__dict__.update(values)

    def __getattr__(self, name):
        return getattr(self._original, name)


def _approve_publication(monkeypatch):
    # Module-local stream/input views leave pytest and shared stdlib untouched.
    streams = _View(sys, stdin=_View(sys.stdin, isatty=lambda: True),
                    stdout=_View(sys.stdout, isatty=lambda: True))
    monkeypatch.setattr(cli, "sys", streams)
    monkeypatch.setattr(mcp_project, "sys", streams)
    monkeypatch.setattr(mcp_project, "input", lambda prompt: "yes", raising=False)


def _publication_capture(case, tmp_path, monkeypatch):
    temporary = tmp_path / "publication-temporary"
    temporary.mkdir()
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(name, str(temporary))
    original = publish.tempfile.TemporaryDirectory

    def workspace(*args, **kwargs):
        kwargs.setdefault("dir", str(temporary))
        return original(*args, **kwargs)

    monkeypatch.setattr(publish, "tempfile", _View(publish.tempfile, TemporaryDirectory=workspace))
    return PublicationCapture(tmp_path / "publication-capture", roots=(case.project, case.home), output_anchor=tmp_path)


@pytest.fixture
def local_app(tmp_path, monkeypatch):
    case = _case(tmp_path / "case", ignore_mode=None)
    # Keep gitignore publication under the documented existing-block policy,
    # without a deprecated Skillfile field that would refuse upgrade preflight.
    (case.project / ".gitignore").write_text("# CocoaSkill\n", encoding="utf-8")
    case.declare(declaration())
    _, helper = bundle(tmp_path / "Applications")
    for key, value in case.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(launchers, "macos_roots", lambda: (helper.parents[3],))
    monkeypatch.setattr(launchers, "_platform", lambda: "macos")
    return case


# The reviewer's complete CLI witness includes a real local skill: a verifier
# flip during approval must leave project/store bytes, modes and file mtimes
# unchanged; the identical flow succeeds when verification stays valid.
def test_post_consent_verification_refusal_preserves_all_state(local_app, monkeypatch, capsys):
    case = local_app
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    approved = False
    calls = []

    def verify(*args):
        calls.append(approved)
        return replace(good_verification(), signature_valid=not approved)

    def consent(prompt):
        nonlocal approved
        approved = True
        return "yes"

    monkeypatch.setattr(launchers, "verify_macos", verify)
    monkeypatch.setattr("builtins.input", consent)
    before = _snapshot(case.project, case.home)
    assert cli.main(["install", "app", "--fix-gitignore"]) == 1
    output = capsys.readouterr()
    assert "mcp_launcher_untrusted" in output.out + output.err
    assert calls[0] is False and calls[-1] is True and approved
    assert _snapshot(case.project, case.home) == before

    monkeypatch.setattr(launchers, "verify_macos", lambda *a: good_verification())
    assert cli.main(["install", "app", "--fix-gitignore"]) == 0
    capsys.readouterr()
    _assert_seed_installed(case)
    assert all((case.project / path).is_file() for _, path in SURFACES)
    assert list((case.home / "source-v1").rglob("record.json"))


# Enumerate every target from an actual successful full CLI transaction, then
# fail once at each publication boundary. Rollback must restore the whole state,
# including pre-existing snapshots/approval and scaffolding. The final unmodified
# run is a positive control for exactly the flow probed by every refusal.
@pytest.mark.parametrize("point", ["prepared", "after_backup", "after_install", "target_committed"])
@pytest.mark.parametrize("existing", [False, True])
def test_each_publication_refusal_rolls_back_full_state(local_app, tmp_path, monkeypatch, capsys, point, existing):
    case = local_app
    capture = _publication_capture(case, tmp_path, monkeypatch)
    _approve_publication(monkeypatch)
    monkeypatch.setattr(launchers, "verify_macos", lambda *a: good_verification())

    def run():
        result = None
        try:
            result = cli.main(["upgrade" if existing else "install", "app", "--fix-gitignore"])
        finally:
            output = capsys.readouterr()
            capture.cli(result, output.out, output.err)
        return result, output.out + output.err

    if existing:
        code, output = run()
        assert code == 0, output
        source = case.project / "source/seed/SKILL.md"
        source.write_bytes(source.read_bytes() + b"\nUpdated local source.\n")
        raw = declaration()
        raw["app"]["server"]["args"] = ["--updated"]
        case.declare(raw)
        # Exercise restoration of pre-existing broader MCP directory modes too.
        if sys.platform != "win32":
            (case.home / "state/mcp/consent").chmod(0o750)

    engine_type = publish.TransactionEngine
    observed = []

    # Discover targets in preparation without publishing the successful plan.
    def capture_factory(home, **kwargs):
        def fault(event, target):
            if event == "staging_entry_created" and target is not None:
                key = (target.target_class, target.identifier)
                if key not in observed:
                    observed.append(key)
            if event == "prepared":
                raise OSError("publication table capture")
        return capture.observe(engine_type(home, fault_hook=fault, **kwargs))

    before = _snapshot(case.project, case.home)
    with monkeypatch.context() as patch:
        patch.setattr(publish, "TransactionEngine", capture_factory)
        code, output = run()
    assert code == 1 and "publication table capture" in output
    assert _snapshot(case.project, case.home) == before
    classes = {key[0] for key in observed}
    required = {"03-source-tree", "04-source-record", "mcp-native", "mcp-consent", "mcp-ledger", publish.CLASS_CONTEXT}
    if not existing:
        required.add("mcp-gitignore")
    assert required <= classes
    assert sum(key[0].startswith(("03-source-", "04-source-")) for key in observed) == 2

    for selected in ([None] if point == "prepared" else observed):
        reached = []

        def factory(home, **kwargs):
            def fault(event, target):
                key = None if target is None else (target.target_class, target.identifier)
                if event == point and key == selected and not reached:
                    reached.append((event, key))
                    raise OSError("injected publication refusal")
            return capture.observe(engine_type(home, fault_hook=fault, **kwargs))

        with monkeypatch.context() as patch:
            patch.setattr(publish, "TransactionEngine", factory)
            code, output = run()
        assert reached == [(point, selected)], (point, selected, output)
        assert code == 1 and "injected publication refusal" in output
        assert _snapshot(case.project, case.home) == before, (point, selected)

    code, output = run()
    assert code == 0, output
    _assert_seed_installed(case)
    assert all((case.project / path).is_file() for _, path in SURFACES)
    # The old store survives an update; the new entry is installed beside it.
    assert len(list((case.home / "source-v1").rglob("record.json"))) == (2 if existing else 1)

    assert capture.complete, "publication observation incomplete; inspect private capture artifacts"


# This smallest real CLI pair qualifies the table's recorder attachment, not the
# full target table or native signature verification. Private output is saved
# before any result/output assertion; the same flow then publishes unchanged.
def test_publication_capture_cli_preparation_and_positive(local_app, tmp_path, monkeypatch, capsys):
    case = local_app
    _approve_publication(monkeypatch)
    monkeypatch.setattr(launchers, "verify_macos", lambda *a: good_verification())
    capture = _publication_capture(case, tmp_path, monkeypatch)
    original = publish.TransactionEngine
    applied = []
    refusal = OSError("publication capture control")
    fail = True

    def factory(home, **kwargs):
        def hook(point, target):
            if fail and point == "prepared":
                applied.append(point)
                raise refusal
        return capture.observe(original(home, fault_hook=hook, **kwargs))

    monkeypatch.setattr(publish, "TransactionEngine", factory)
    before = _snapshot(case.project, case.home)
    code = cli.main(["install", "app", "--fix-gitignore"])
    output = capsys.readouterr()
    capture.cli(code, output.out, output.err)
    assert (capture.directory / "cli-001.stdout.private").read_text() == output.out
    assert (capture.directory / "cli-001.stderr.private").read_text() == output.err
    assert code == 1 and "publication capture control" in output.out + output.err
    assert applied == ["prepared"] and capture.complete
    assert _snapshot(case.project, case.home) == before
    assert any(row.get("call") == "prepare_error" for row in capture.rows)
    assert (capture.directory / "journal-001.private").is_file()
    fail = False
    code = cli.main(["install", "app", "--fix-gitignore"])
    output = capsys.readouterr()
    capture.cli(code, output.out, output.err)
    assert code == 0, output.out + output.err
    assert capture.complete and not capture.errors
    _assert_seed_installed(case)
    assert all((case.project / path).is_file() for _, path in SURFACES)
    assert not list((case.home / "state/transactions/v1").glob("*.json"))


# Production codesign exercises the reviewer's real-app interleaving. Only a
# disposable copy is tampered; the original is neither modified nor launched.
# Restoring the copy then installs the same local skill and native configs.
@pytest.mark.csk_e2e_native
@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS signature check")
def test_native_post_consent_tamper_preserves_all_state(local_app, tmp_path, monkeypatch, capsys):
    app = Path("/Applications/Proxyman.app")
    if not app.is_dir():
        pytest.skip("Proxyman is required for the native positive control")
    case = local_app
    raw = declaration({"macos": {"bundle_id": "com.proxyman.NSProxy", "team_id": "3X57WP8E8V", "executable": "Contents/MacOS/mcp-server"}})
    case.declare(raw)
    from csk.mcp_declarations import parse_mcp_servers
    spec = parse_mcp_servers(raw, invocation_scope="project", target_agents=["claude_code"])["app"].server.launcher
    original = launchers.resolve_launcher(spec, roots=(app.parent,))
    apps = tmp_path / "copied"
    apps.mkdir()
    copied = apps / app.name
    shutil.copytree(app, copied, symlinks=True)
    assert launchers.resolve_launcher(spec, roots=(apps,)).content_sha256 == original.content_sha256
    helper = copied / "Contents/MacOS/mcp-server"
    monkeypatch.setattr(launchers, "macos_roots", lambda: (apps,))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with helper.open("rb") as stream:
        stream.seek(4096)
        byte = stream.read(1)
    prompts = []

    def approve_and_tamper(prompt):
        prompts.append(prompt)
        with helper.open("r+b") as stream:
            stream.seek(4096)
            stream.write(bytes([byte[0] ^ 1]))
        return "yes"

    monkeypatch.setattr("builtins.input", approve_and_tamper)
    before = _snapshot(case.project, case.home)
    assert cli.main(["install", "app", "--fix-gitignore"]) == 1
    output = capsys.readouterr()
    assert "mcp_launcher_untrusted" in output.out + output.err and len(prompts) == 1
    assert _snapshot(case.project, case.home) == before
    with helper.open("r+b") as stream:
        stream.seek(4096)
        stream.write(byte)
    monkeypatch.setattr("builtins.input", lambda p: "yes")
    assert cli.main(["install", "app", "--fix-gitignore"]) == 0
    capsys.readouterr()
    _assert_seed_installed(case)
    assert all((case.project / path).is_file() for _, path in SURFACES)
    assert launchers.resolve_launcher(spec, roots=(app.parent,)) == original


# Frozen recovery of a missing snapshot also shares rollback: refusal during
# approval or after source-record publication preserves the prior lock/native
# state and leaves recovery uncommitted; retry restores the exact locked skill.
@pytest.mark.parametrize("phase", ["approval", "source-record"])
def test_frozen_snapshot_recovery_refusal(local_app, monkeypatch, capsys, phase):
    case = local_app
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda p: "yes")
    monkeypatch.setattr(launchers, "verify_macos", lambda *a: good_verification())
    argv = ["install", "app", "--fix-gitignore"]
    assert cli.main(argv) == 0
    capsys.readouterr()
    record = next((case.home / "source-v1").rglob("record.json"))
    shutil.rmtree(record.parent)
    raw = declaration()
    raw["app"]["server"]["args"] = ["--repair"]
    case.declare(raw)
    before = _snapshot(case.project, case.home)
    reached = []
    engine_type = publish.TransactionEngine

    def factory(home, **kwargs):
        def fault(event, target):
            if event == "target_committed" and target.target_class == "04-source-record" and not reached:
                reached.append(event)
                raise OSError("injected recovery refusal")
        return engine_type(home, fault_hook=fault, **kwargs)

    with monkeypatch.context() as patch:
        if phase == "approval":
            def consent(prompt):
                reached.append("approved")
                patch.setattr(launchers, "verify_macos", lambda *a: replace(good_verification(), signature_valid=False))
                return "yes"
            patch.setattr("builtins.input", consent)
        else:
            patch.setattr(publish, "TransactionEngine", factory)
        assert cli.main(argv) == 1
        output = capsys.readouterr()
    assert reached == (["approved"] if phase == "approval" else ["target_committed"])
    assert ("mcp_launcher_untrusted" if phase == "approval" else "injected recovery refusal") in output.out + output.err
    assert _snapshot(case.project, case.home) == before
    assert cli.main(argv) == 0
    capsys.readouterr()
    assert record.is_file()
    _assert_seed_installed(case)
