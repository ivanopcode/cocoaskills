"""Released and current CLI journals recover without invented launcher approval."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from cli_golden_v1_support import extract_release_source
from test_mcp_install_acceptance import _case, _assert_seed_installed, SURFACES, STDIO, URL

RELEASE = "92409a9d621b8eafcc74f5aeaa85cf2f6e54d6fc"
CURRENT = Path(__file__).resolve().parents[1] / "src"
CHILD = r'''
import json, os, signal, sys
from unittest.mock import patch
from csk import cli
from csk.sources import publish
request = json.loads(sys.argv[1])
print("WRITER_IMPLEMENTATION", cli.__file__, flush=True)
original = publish.TransactionEngine
def factory(home, **kwargs):
    def fault(event, target):
        if request.get("kill") and event == request["point"] and (request.get("target") is None or target is not None and target.target_class == request["target"]):
            print("REAL_SIGKILL", event, flush=True)
            os.kill(os.getpid(), signal.SIGKILL)
    return original(home, fault_hook=fault, **kwargs)
with patch.object(publish, "TransactionEngine", factory), patch("builtins.input", lambda _: "yes"), patch.object(sys.stdin, "isatty", lambda: True), patch.object(sys.stdout, "isatty", lambda: True):
    sys.exit(cli.main(request["argv"]))
'''


@pytest.fixture(scope="module")
def release_source(tmp_path_factory):
    return extract_release_source(RELEASE, tmp_path_factory.mktemp("released-cli"), cwd=CURRENT.parent)


def invoke(case, source, **fault):
    result = subprocess.run(
        [sys.executable, "-c", CHILD, json.dumps(dict(argv=["install", "app", "--fix-gitignore"], **fault))],
        cwd=case.project, env=dict(case.env, PYTHONPATH=str(source)), capture_output=True, text=True, timeout=120,
    )
    assert str(source / "csk/cli.py") in result.stdout, result.stdout + result.stderr
    return result


def journals(case):
    return list((case.home / "state/transactions/v1").glob("*.json*"))


# Real released source writes its own unedited journal before SIGKILL. Current
# source is the neighboring control. Both command/HTTP transports recover at
# prepared and first-native boundaries; no launcher observation is fabricated.
@pytest.mark.skipif(os.name == "nt", reason="POSIX process-death control")
@pytest.mark.parametrize("writer", ["release", "current"])
@pytest.mark.parametrize("kind", ["command", "http"])
@pytest.mark.parametrize("point", ["prepared", "after_install"])
@pytest.mark.parametrize("changed_manifest", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_nonlauncher_journals_recover(tmp_path, release_source, writer, kind, point, changed_manifest, existing):
    case = _case(tmp_path / "case", ignore_mode=None if writer == "release" else "gitignore")
    source = release_source if writer == "release" else CURRENT
    if existing:
        case.declare({"docs": {"server": {**STDIO["server"], "args": ["pkg==0.9.0"]}}})
        installed = invoke(case, source)
        assert installed.returncode == 0, installed.stdout + installed.stderr
    case.declare({"docs": STDIO if kind == "command" else URL})
    killed = invoke(case, source,
                    kill=True, point=point, target="mcp-native" if point == "after_install" else None)
    assert killed.returncode == -signal.SIGKILL and "REAL_SIGKILL" in killed.stdout
    pending, = journals(case)
    raw = json.loads(pending.read_bytes())
    guards = [t["publication_recheck"]["commit_guard"] for t in raw["targets"]
              if "commit_guard" in (t.get("publication_recheck") or {})]
    assert guards == ([] if writer == "release" else [{"kind": "mcp-launchers-v1", "observations": []}])
    # A valid current launcher declaration cannot reclassify the saved ordinary
    # plan. Its later resolution refusal leaves the recovered native entries
    # visible instead of replacing them with a fresh plan.
    original = (case.project / "Skillfile.json").read_bytes()
    if changed_manifest:
        case.declare({"later": {"server": {"transport": "stdio", "launcher": {"macos": {
            "bundle_id": "invalid.example.missing", "team_id": "ABCDE12345", "executable": "Contents/MacOS/helper",
        }}}}})
    resumed = invoke(case, CURRENT)
    assert resumed.returncode == (1 if changed_manifest else 0), resumed.stdout + resumed.stderr
    if changed_manifest:
        expected = "mcp_launcher_not_found" if sys.platform == "darwin" else "mcp_launcher_platform_missing"
        assert expected in resumed.stdout + resumed.stderr
    assert not journals(case), resumed.stdout + resumed.stderr
    _assert_seed_installed(case)
    assert all((case.project / path).is_file() for _, path in SURFACES)
    assert "mcp_recovery_observation_missing" not in resumed.stdout + resumed.stderr
    (case.project / "Skillfile.json").write_bytes(original)
    assert invoke(case, CURRENT).returncode == 0


# Home-wide replay also preserves ordinary schema-2/3 transactions that never
# planned any native MCP target, with actual old and current CLI writers.
@pytest.mark.skipif(os.name == "nt", reason="POSIX process-death control")
@pytest.mark.parametrize("writer", ["release", "current"])
@pytest.mark.parametrize("schema", [2, 3])
def test_non_mcp_journals_recover(tmp_path, release_source, writer, schema):
    case = _case(tmp_path / "case", schema=schema,
                 ignore_mode=None if writer == "release" else "gitignore")
    killed = invoke(case, release_source if writer == "release" else CURRENT, kill=True, point="prepared")
    assert killed.returncode == -signal.SIGKILL
    resumed = invoke(case, CURRENT)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert not journals(case)
    _assert_seed_installed(case)
    assert not any((case.project / path).exists() for _, path in SURFACES)


# An opaque native plan cannot be classified as old command/HTTP. The emitted
# command really rolls back its pinned preimages without running forward trust
# checks, then a normal CLI install succeeds. Unknown ids leave state intact.
def test_unclassifiable_native_journal_has_supported_rollback(tmp_path):
    from csk.locking import ManagerHomeLock
    from csk.transactions import MutableTarget, TransactionEngine, TransactionPlan, digest_target
    from test_mcp_install_acceptance import _snapshot

    case = _case(tmp_path / "case")
    live = case.project / ".mcp.json"
    live.write_text('{"mcpServers":{}}\n')
    original = live.read_bytes()
    desired = tmp_path / "desired"
    desired.write_text('{"mcpServers":{"opaque":{"command":"example"}}}\n')
    with ManagerHomeLock(case.home) as lock:
        TransactionEngine(case.home).prepare(lock, TransactionPlan(
            "unknown-native", str(case.project),
            (MutableTarget("mcp-native", "claude_code", live, desired,
                           expected_preimage_digest=digest_target(live)),),
        ))
    before = _snapshot(case.project, case.home)
    refused = invoke(case, CURRENT)
    assert refused.returncode == 1 and "mcp_recovery_observation_missing" in refused.stdout + refused.stderr
    assert "csk recover --rollback unknown-native" in refused.stdout + refused.stderr
    assert _snapshot(case.project, case.home) == before
    def rollback(identifier):
        return subprocess.run([sys.executable, "-m", "csk", "recover", "--rollback", identifier],
                              cwd=case.project, env=case.env, text=True, capture_output=True, timeout=60)
    wrong = rollback("missing")
    assert wrong.returncode == 1 and "cannot read journal missing" in wrong.stderr and journals(case)
    assert _snapshot(case.project, case.home) == before
    recovered = rollback("unknown-native")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert live.read_bytes() == original and not journals(case)
    assert invoke(case, CURRENT).returncode == 0
