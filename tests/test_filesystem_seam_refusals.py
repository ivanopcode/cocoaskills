"""SEAM family: raw OS errors never escape the filesystem seams.

Every filesystem call site in the in-scope modules
(``csk.audit.trust``, ``csk.manifest``, ``csk.cli``) is derived by
inspecting the modules (see ``fs_seam_support``), fault-injected with
EACCES, EIO, ENAMETOOLONG and ELOOP, and required to produce a
structured, domain-typed refusal. Absence stays absence in the other
direction: every reader is also driven with a genuinely missing file.

Completeness is derived, not hand-listed:

* the static tests enumerate the seams and require each to be lexically
  guarded and multiplicity-singleton per (module, function, op);
* each driver fault-injects its seam through the fault helper, which
  records every firing;
* the linkage test at the end of this module (it must stay last: the
  suite runs single-process in file order) requires every enumerated
  (module, function, op) to have been faulted with all four errnos.

A seam added later fails either the guard test, the multiplicity test,
or the linkage test instead of going unguarded.
"""

from __future__ import annotations

import errno
import io
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import make_config, make_project, make_skill_repo, write_skillfile
from fs_seam_support import (
    FAULT_ERRNOS,
    FIRED,
    IN_SCOPE_MODULES,
    enumerate_all_seams,
    enumerate_source_snippet,
    fault_at,
)

from csk import cli, hybrid, manifest
from csk import config as csk_config
from csk.audit import pipeline as audit_pipeline
from csk.audit import trust as audit_trust
from csk.audit.model import Decision, TrustRecord, Verdict

ERRNO_CASES = [pytest.param(err, id=name) for err, name in FAULT_ERRNOS]

_TRUST_HASH = "sha256:" + "ab" * 32

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits required")
_NOT_ROOT = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root bypasses permission bits"
)


def _configured(tmp_path: Path, skills_root: Path, csk_home: Path, monkeypatch) -> Path:
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    csk_config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    monkeypatch.delenv("CSK_SYSTEM_CONFIG", raising=False)
    return project


def _verdict(content_sha256: str) -> Verdict:
    return Verdict(
        schema_version=audit_trust.SCHEMA_VERSION,
        content_sha256=content_sha256,
        skill="skill-a",
        source="skill-a",
        commit="c0ffee",
        backend="null",
        model=None,
        cloud=False,
        prompt_version=audit_trust.PROMPT_VERSION,
        ruleset_version=audit_trust.RULESET_VERSION,
        canary_passed=True,
        findings=(),
        decision=Decision.ALLOW,
        ran_at="2026-09-18T00:00:00+00:00",
        trust=TrustRecord(),
    )


# ---------------------------------------------------------------------------
# Static derivation: guards, multiplicity, pins, checker self-test.
# ---------------------------------------------------------------------------


def test_static_every_seam_lexically_guarded():
    """Every derived seam sits inside a try that covers OSError.

    Guards are lexical and function-local by design: a caller-side catch
    is defense in depth, never the seam's own guard, so each module owns
    its failures where a reader can see them.
    """

    unguarded = [seam for seam in enumerate_all_seams() if not seam.guarded]
    assert not unguarded, unguarded


def test_static_seam_multiplicity_singleton():
    """No (module, function, op) group holds more than one seam.

    The linkage test matches faults to seams per (module, function, op);
    this invariant is what makes that granularity airtight: a second
    same-op site in one function fails here instead of hiding behind the
    first site's driver.
    """

    counts: dict[tuple[str, str, str], int] = {}
    for seam in enumerate_all_seams():
        key = (seam.module, seam.func, seam.op)
        counts[key] = counts.get(key, 0) + 1
    duplicated = {key: count for key, count in counts.items() if count > 1}
    assert not duplicated, duplicated


def test_enumeration_pins_known_seams():
    """Concrete members pin the derivation walk across modules and ops.

    The pins verify the enumerator is not vacuous or broken; they do not
    replace the derivation. The count tripwire fails on any added or
    removed seam so the change is conscious.
    """

    groups = {(seam.module, seam.func, seam.op) for seam in enumerate_all_seams()}
    assert ("csk.audit.trust", "load_trust_record", "read_text") in groups
    assert ("csk.audit.trust", "_confirm_missing_trust_path", "stat") in groups
    assert ("csk.audit.trust", "store_verdict", "mkdir") in groups
    assert ("csk.audit.trust", "store_verdict", "write_text") in groups
    assert ("csk.audit.trust", "pin_content_hash", "write_text") in groups
    assert ("csk.manifest", "load_manifest", "read_bytes") in groups
    assert ("csk.manifest", "_require_project_dir", "stat") in groups
    assert ("csk.manifest", "_write_skillfile_text", "write_text") in groups
    assert ("csk.cli", "_cmd_audit_publish", "read_bytes") in groups
    assert ("csk.cli", "_nearest_parent_manifest", "stat") in groups
    assert len(groups) == 20, sorted(groups)


def test_checker_flags_synthetic_unguarded_seam():
    """The checker convicts unguarded seams and acquits guarded ones."""

    flagged = enumerate_source_snippet(
        "from pathlib import Path\n"
        "import os\n"
        "def f(p: Path):\n"
        "    if p.exists():\n"
        "        return p.read_bytes()\n"
        "    return os.stat(p)\n"
        "def g(p: Path):\n"
        "    try:\n"
        "        return p.read_bytes()\n"
        "    except OSError:\n"
        "        return None\n"
    )
    by_func = {}
    for seam in flagged:
        by_func.setdefault(seam.func, []).append(seam)
    assert {seam.op for seam in by_func["f"]} == {"exists", "read_bytes", "os.stat"}
    assert all(not seam.guarded for seam in by_func["f"])
    assert len(by_func["g"]) == 1 and by_func["g"][0].guarded


def test_checker_arity_rule_distinguishes_path_replace_from_str_replace():
    flagged = enumerate_source_snippet(
        "def f(p, s: str):\n"
        "    p.replace(p.with_suffix('.bak'))\n"
        "    return s.replace(':', '-')\n"
    )
    assert [(seam.op, seam.guarded) for seam in flagged] == [("replace", False)]


def test_resolve_never_raises_oserror(tmp_path):
    """Justification for excluding resolve() from the enumeration.

    Non-strict ``Path.resolve`` swallows per-component errors and returns
    a path, so it cannot produce the four errnos this family injects. The
    ELOOP-as-RuntimeError variant on Python <= 3.13 is a distinct
    non-OSError corner, hardened and tested at the init entry only.
    """

    missing = tmp_path / "no-such-dir" / "leaf"
    assert missing.resolve() == missing.absolute()
    long_name = tmp_path / ("x" * 300)
    assert long_name.resolve().name == "x" * 300


@_POSIX_ONLY
@_NOT_ROOT
def test_resolve_never_raises_oserror_under_eacces(tmp_path):
    blocked = tmp_path / "nosearch"
    blocked.mkdir()
    (blocked / "leaf").write_text("x", encoding="utf-8")
    blocked.chmod(0)
    try:
        assert (blocked / "leaf").resolve() == (blocked / "leaf").absolute()
    finally:
        blocked.chmod(0o700)


# ---------------------------------------------------------------------------
# Canonical mock-free reproduction: chmod 0 on the trust directory.
# ---------------------------------------------------------------------------


@_POSIX_ONLY
@_NOT_ROOT
def test_canonical_chmod0_trust_dir_refuses(tmp_path):
    """Unreadable trust store refuses; restored store reads the pin back."""

    csk_home = tmp_path / "home"
    csk_home.mkdir()
    path = audit_trust.trust_path(csk_home, _TRUST_HASH)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"schema_version": 1, "pinned": True, "reason": "reviewed"}), encoding="utf-8"
    )
    assert audit_trust.load_trust_record(csk_home, _TRUST_HASH).pinned

    path.parent.chmod(0)
    try:
        with pytest.raises(audit_trust.TrustRecordError) as excinfo:
            audit_trust.load_trust_record(csk_home, _TRUST_HASH)
    finally:
        path.parent.chmod(0o700)
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNREADABLE
    assert str(path) in excinfo.value.detail

    assert audit_trust.load_trust_record(csk_home, _TRUST_HASH) == TrustRecord(
        pinned=True, reason="reviewed"
    )


@_POSIX_ONLY
@_NOT_ROOT
def test_canonical_chmod0_trust_dir_refuses_csk_audit(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    """The canonical seam driven through the public entry point."""

    make_skill_repo(skills_root, "skill-a", tag="v1")
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": [{"name": "skill-a", "tag": "v1"}]})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))

    first_code = cli.main(["audit", "app"])
    assert first_code in (0, 1)
    capsys.readouterr()
    store_dirs = list((csk_home / "audit").iterdir())
    assert len(store_dirs) == 1
    store_dirs[0].chmod(0)
    try:
        code = cli.main(["audit", "app"])
    finally:
        store_dirs[0].chmod(0o700)
    captured = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert audit_trust.CODE_TRUST_UNREADABLE in captured.err


# ---------------------------------------------------------------------------
# Trust store drivers.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_trust_record_read_refuses(monkeypatch, tmp_path, err):
    csk_home = tmp_path / "home"
    path = audit_trust.trust_path(csk_home, _TRUST_HASH)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 1, "pinned": True, "reason": "r"}))
    with fault_at(
        monkeypatch, module="csk.audit.trust", func="load_trust_record", op="read_text",
        target=path, err=err,
    ) as firings, pytest.raises(audit_trust.TrustRecordError) as excinfo:
        audit_trust.load_trust_record(csk_home, _TRUST_HASH)
    assert firings == [err]
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNREADABLE


def test_trust_record_absence_is_empty_record(tmp_path):
    """Absent stays absent: a fresh home has no pin and refuses nothing."""

    csk_home = tmp_path / "home"
    csk_home.mkdir()
    assert audit_trust.load_trust_record(csk_home, _TRUST_HASH) == TrustRecord()
    audit_trust.trust_path(csk_home, _TRUST_HASH).parent.mkdir(parents=True)
    assert audit_trust.load_trust_record(csk_home, _TRUST_HASH) == TrustRecord()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("{not json", id="malformed"),
        pytest.param("[1, 2]", id="non-object"),
        pytest.param(json.dumps({"schema_version": 999, "pinned": True}), id="wrong-schema"),
    ],
)
def test_trust_record_malformed_is_empty_record(tmp_path, payload):
    csk_home = tmp_path / "home"
    path = audit_trust.trust_path(csk_home, _TRUST_HASH)
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")
    assert audit_trust.load_trust_record(csk_home, _TRUST_HASH) == TrustRecord()


def test_trust_record_non_utf8_is_empty_record(tmp_path):
    csk_home = tmp_path / "home"
    path = audit_trust.trust_path(csk_home, _TRUST_HASH)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00bad")
    assert audit_trust.load_trust_record(csk_home, _TRUST_HASH) == TrustRecord()


def test_trust_record_file_as_parent_refuses(tmp_path):
    """A file where the trust directory belongs is refusal, not absence.

    Mock-free ENOTDIR at the stat seam: ``Path.exists`` swallows it into
    ``False`` on every interpreter (classic ``pathlib`` ignores ENOTDIR;
    3.14 delegates to ``os.path.exists``), so the existence-probe spelling
    reads this sick store as "no pin". The direct-read survivor refuses
    typed, naming the trust path. Added by TASK-260921-qe62bu to pin the
    replay decision on all supported Pythons.
    """

    csk_home = tmp_path / "home"
    path = audit_trust.trust_path(csk_home, _TRUST_HASH)
    path.parent.parent.mkdir(parents=True)
    path.parent.write_text("not a directory", encoding="utf-8")
    with pytest.raises(audit_trust.TrustRecordError) as excinfo:
        audit_trust.load_trust_record(csk_home, _TRUST_HASH)
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNREADABLE
    assert str(path) in excinfo.value.detail


def test_trust_record_windows_path_not_found_file_as_parent_refuses(
    monkeypatch, tmp_path
):
    """Windows ERROR_PATH_NOT_FOUND at open cannot turn a file parent into no pin."""

    csk_home = tmp_path / "home"
    path = audit_trust.trust_path(csk_home, _TRUST_HASH)
    path.parent.parent.mkdir(parents=True)
    path.parent.write_text("not a directory", encoding="utf-8")

    real_open = io.open
    opened: list[Path] = []

    def windows_path_not_found(file, *args, **kwargs):
        if os.fspath(file) == os.fspath(path):
            opened.append(path)
            error = FileNotFoundError(
                errno.ENOENT, "Windows ERROR_PATH_NOT_FOUND", os.fspath(path)
            )
            error.winerror = 3
            raise error
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", windows_path_not_found)
    with pytest.raises(audit_trust.TrustRecordError) as excinfo:
        audit_trust.load_trust_record(csk_home, _TRUST_HASH)

    assert opened == [path]
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNREADABLE
    assert str(path.parent) in excinfo.value.detail


def test_trust_record_parent_recheck_os_error_refuses(monkeypatch, tmp_path):
    """A failed parent re-check after an absent read is still a refusal."""

    csk_home = tmp_path / "home"
    csk_home.mkdir()
    path = audit_trust.trust_path(csk_home, _TRUST_HASH)
    real_open = io.open
    real_stat = Path.stat
    opened: list[Path] = []

    def windows_path_not_found(file, *args, **kwargs):
        if os.fspath(file) == os.fspath(path):
            opened.append(path)
            error = FileNotFoundError(
                errno.ENOENT, "Windows ERROR_PATH_NOT_FOUND", os.fspath(path)
            )
            error.winerror = 3
            raise error
        return real_open(file, *args, **kwargs)

    def denied_parent_stat(self, *args, **kwargs):
        if self == path.parent:
            raise PermissionError(errno.EACCES, "re-check denied", os.fspath(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(io, "open", windows_path_not_found)
    monkeypatch.setattr(Path, "stat", denied_parent_stat)
    with pytest.raises(audit_trust.TrustRecordError) as excinfo:
        audit_trust.load_trust_record(csk_home, _TRUST_HASH)

    assert opened == [path]
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNREADABLE
    assert "re-check denied" in excinfo.value.detail


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_trust_record_missing_parent_recheck_fault_refuses(monkeypatch, tmp_path, err):
    """The new parent-stat seam refuses all filesystem faults."""

    csk_home = tmp_path / "home"
    csk_home.mkdir()
    path = audit_trust.trust_path(csk_home, _TRUST_HASH)
    with (
        fault_at(
            monkeypatch,
            module="csk.audit.trust",
            func="_confirm_missing_trust_path",
            op="stat",
            target=path.parent,
            err=err,
        ) as firings,
        pytest.raises(audit_trust.TrustRecordError) as excinfo,
    ):
        audit_trust.load_trust_record(csk_home, _TRUST_HASH)
    assert firings == [err]
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNREADABLE


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_cached_verdict_read_refuses(monkeypatch, tmp_path, err):
    csk_home = tmp_path / "home"
    verdict = _verdict(_TRUST_HASH)
    stored = audit_trust.store_verdict(csk_home, verdict)
    assert audit_trust.load_cached_verdict(csk_home, _TRUST_HASH, "null", None) is not None
    with (
        fault_at(
            monkeypatch, module="csk.audit.trust", func="load_cached_verdict", op="read_text",
            target=stored, err=err,
        ) as firings,
        pytest.raises(audit_trust.TrustRecordError) as excinfo,
    ):
        audit_trust.load_cached_verdict(csk_home, _TRUST_HASH, "null", None)
    assert firings == [err]
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNREADABLE


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("{not json", id="malformed"),
        pytest.param("[1, 2]", id="non-object"),
        pytest.param(b"\xff\xfe\x00bad", id="non-utf8"),
    ],
)
def test_cached_verdict_malformed_is_miss(tmp_path, payload):
    csk_home = tmp_path / "home"
    verdict = _verdict(_TRUST_HASH)
    stored = audit_trust.store_verdict(csk_home, verdict)
    if isinstance(payload, bytes):
        stored.write_bytes(payload)
    else:
        stored.write_text(payload, encoding="utf-8")
    assert audit_trust.load_cached_verdict(csk_home, _TRUST_HASH, "null", None) is None


def test_cached_verdict_absence_is_miss(tmp_path):
    csk_home = tmp_path / "home"
    csk_home.mkdir()
    assert audit_trust.load_cached_verdict(csk_home, _TRUST_HASH, "null", None) is None


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_store_verdict_mkdir_refuses(monkeypatch, tmp_path, err):
    csk_home = tmp_path / "home"
    verdict = _verdict(_TRUST_HASH)
    parent = audit_trust.verdict_path(
        csk_home, _TRUST_HASH, "null", None,
        audit_trust.PROMPT_VERSION, audit_trust.RULESET_VERSION,
    ).parent
    with fault_at(
        monkeypatch, module="csk.audit.trust", func="store_verdict", op="mkdir",
        target=parent, err=err,
    ) as firings, pytest.raises(audit_trust.TrustRecordError) as excinfo:
        audit_trust.store_verdict(csk_home, verdict)
    assert firings == [err]
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNWRITABLE


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_store_verdict_write_refuses(monkeypatch, tmp_path, err):
    csk_home = tmp_path / "home"
    verdict = _verdict(_TRUST_HASH)
    stored = audit_trust.store_verdict(csk_home, verdict)
    with fault_at(
        monkeypatch, module="csk.audit.trust", func="store_verdict", op="write_text",
        target=stored, err=err,
    ) as firings, pytest.raises(audit_trust.TrustRecordError) as excinfo:
        audit_trust.store_verdict(csk_home, verdict)
    assert firings == [err]
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNWRITABLE


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_pin_mkdir_refuses(monkeypatch, tmp_path, err):
    csk_home = tmp_path / "home"
    parent = audit_trust.trust_path(csk_home, _TRUST_HASH).parent
    with fault_at(
        monkeypatch, module="csk.audit.trust", func="pin_content_hash", op="mkdir",
        target=parent, err=err,
    ) as firings, pytest.raises(audit_trust.TrustRecordError) as excinfo:
        audit_trust.pin_content_hash(csk_home, _TRUST_HASH, reason="r")
    assert firings == [err]
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNWRITABLE


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_pin_write_refuses(monkeypatch, tmp_path, err):
    csk_home = tmp_path / "home"
    path = audit_trust.trust_path(csk_home, _TRUST_HASH)
    path.parent.mkdir(parents=True)
    with fault_at(
        monkeypatch, module="csk.audit.trust", func="pin_content_hash", op="write_text",
        target=path, err=err,
    ) as firings, pytest.raises(audit_trust.TrustRecordError) as excinfo:
        audit_trust.pin_content_hash(csk_home, _TRUST_HASH, reason="r")
    assert firings == [err]
    assert excinfo.value.code == audit_trust.CODE_TRUST_UNWRITABLE


# ---------------------------------------------------------------------------
# Manifest drivers.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_load_manifest_read_refuses(monkeypatch, tmp_path, err):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    path = project / manifest.MANIFEST_NAME
    with (
        fault_at(
            monkeypatch, module="csk.manifest", func="load_manifest", op="read_bytes",
            target=path, err=err,
        ) as firings,
        pytest.raises(manifest.ManifestError, match="Cannot read Skillfile at") as excinfo,
    ):
        manifest.load_manifest(project)
    assert firings == [err]
    assert str(path) in str(excinfo.value)


def test_load_manifest_absence_is_none(tmp_path):
    project = make_project(tmp_path)
    assert manifest.load_manifest(project) is None


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_read_payload_read_refuses(monkeypatch, tmp_path, err):
    """The add/remove reader refuses through its public entry point."""

    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    path = project / manifest.MANIFEST_NAME
    with (
        fault_at(
            monkeypatch, module="csk.manifest", func="_read_payload", op="read_bytes",
            target=path, err=err,
        ) as firings,
        pytest.raises(manifest.ManifestError, match="Cannot read Skillfile at"),
    ):
        manifest.add_skill_decl(project, name="skill-a", ref_kind="tag", ref="v1")
    assert firings == [err]


def test_read_payload_missing_keeps_not_found_message(tmp_path):
    project = make_project(tmp_path)
    with pytest.raises(manifest.ManifestError) as excinfo:
        manifest.add_skill_decl(project, name="skill-a", ref_kind="tag", ref="v1")
    assert str(excinfo.value) == (
        f"Skillfile.json not found at {project / manifest.MANIFEST_NAME}; run 'csk init' first"
    )


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_write_payload_write_refuses(monkeypatch, tmp_path, err):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    path = project / manifest.MANIFEST_NAME
    with (
        fault_at(
            monkeypatch, module="csk.manifest", func="_write_skillfile_text", op="write_text",
            target=path, err=err,
        ) as firings,
        pytest.raises(manifest.ManifestError, match="Cannot write Skillfile at"),
    ):
        manifest.add_skill_decl(project, name="skill-a", ref_kind="tag", ref="v1")
    assert firings == [err]


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_require_project_dir_stat_refuses(monkeypatch, tmp_path, err):
    project = make_project(tmp_path)
    with (
        fault_at(
            monkeypatch, module="csk.manifest", func="_require_project_dir", op="stat",
            target=project, err=err,
        ) as firings,
        pytest.raises(manifest.ManifestError, match="Cannot access project path"),
    ):
        manifest.ensure_empty_manifest(project)
    assert firings == [err]


@pytest.mark.parametrize(
    "shape",
    [pytest.param("missing", id="missing"), pytest.param("file", id="file-as-root")],
)
def test_require_project_dir_absence_reports_does_not_exist(tmp_path, shape):
    target = tmp_path / "project"
    if shape == "file":
        target.write_text("x", encoding="utf-8")
    with pytest.raises(manifest.ManifestError) as excinfo:
        manifest.ensure_empty_manifest(target)
    assert str(excinfo.value) == f"project path does not exist: {target}"


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_skillfile_present_stat_refuses(monkeypatch, tmp_path, err):
    project = make_project(tmp_path)
    path = project / manifest.MANIFEST_NAME
    with (
        fault_at(
            monkeypatch, module="csk.manifest", func="_skillfile_present", op="stat",
            target=path, err=err,
        ) as firings,
        pytest.raises(manifest.ManifestError, match="Cannot read Skillfile at"),
    ):
        manifest.ensure_empty_manifest(project)
    assert firings == [err]


def test_ensure_empty_manifest_present_is_untouched(tmp_path):
    project = make_project(tmp_path)
    sentinel = '{"schema_version": 1, "skills": [], "custom": true}\n'
    (project / manifest.MANIFEST_NAME).write_text(sentinel, encoding="utf-8")
    manifest.ensure_empty_manifest(project)
    assert (project / manifest.MANIFEST_NAME).read_text(encoding="utf-8") == sentinel


def test_ensure_project_manifest_caller_proof(monkeypatch, tmp_path):
    """The shared project probe refuses through the second ensure entry."""

    project = make_project(tmp_path)
    with fault_at(
        monkeypatch, module="csk.manifest", func="_require_project_dir", op="stat",
        target=project, err=errno.EACCES,
    ), pytest.raises(manifest.ManifestError, match="Cannot access project path"):
        manifest.ensure_project_manifest(project, alias="app", agents=["codex_cli"])


# ---------------------------------------------------------------------------
# CLI drivers.
# ---------------------------------------------------------------------------


def _hybrid_status_setup(tmp_path, skills_root, csk_home, monkeypatch):
    project = _configured(tmp_path, skills_root, csk_home, monkeypatch)
    hybrid.add_hybrid_decl(
        csk_home, name="skill-conventions", ref_kind="tag", ref="v1", git=None, targets=["app"]
    )
    return project


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_hybrid_status_marker_read_degrades(monkeypatch, tmp_path, skills_root, csk_home, capsys, err):
    _hybrid_status_setup(tmp_path, skills_root, csk_home, monkeypatch)
    marker = hybrid.hybrid_skills_root(csk_home) / "skill-conventions" / ".csk-install.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"commit": "abcdef123456"}), encoding="utf-8")
    with fault_at(
        monkeypatch, module="csk.cli", func="_cmd_hybrid", op="read_text",
        target=marker, err=err,
    ) as firings:
        assert cli.main(["hybrid", "status"]) == cli.EXIT_OK
    assert firings == [err]
    assert "[unreadable marker]" in capsys.readouterr().out


def test_hybrid_status_marker_states(monkeypatch, tmp_path, skills_root, csk_home, capsys):
    _hybrid_status_setup(tmp_path, skills_root, csk_home, monkeypatch)
    marker = hybrid.hybrid_skills_root(csk_home) / "skill-conventions" / ".csk-install.json"

    assert cli.main(["hybrid", "status"]) == cli.EXIT_OK
    assert "[missing]" in capsys.readouterr().out

    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"commit": "abcdef123456"}), encoding="utf-8")
    assert cli.main(["hybrid", "status"]) == cli.EXIT_OK
    assert "[installed abcdef1]" in capsys.readouterr().out

    marker.write_text("{not json", encoding="utf-8")
    assert cli.main(["hybrid", "status"]) == cli.EXIT_OK
    assert "[unreadable marker]" in capsys.readouterr().out

    marker.write_text("[1, 2]", encoding="utf-8")
    assert cli.main(["hybrid", "status"]) == cli.EXIT_OK
    assert "[unreadable marker]" in capsys.readouterr().out


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_bootstrap_stat_refuses(monkeypatch, tmp_path, capsys, err):
    cfg_path = tmp_path / "cfg" / "config.json"
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    with fault_at(
        monkeypatch, module="csk.cli", func="_cmd_bootstrap", op="stat",
        target=cfg_path, err=err,
    ) as firings:
        code = cli.main(
            ["bootstrap", "--non-interactive", "--skills-root", str(tmp_path / "skills")]
        )
    assert firings == [err]
    assert code == cli.EXIT_CONFIG
    assert "cannot inspect config" in capsys.readouterr().err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_init_stat_refuses(monkeypatch, tmp_path, capsys, err):
    # resolve() is bypassed so the fault can only fire at the stat probe:
    # on <= 3.13 resolve() stats internally, swallows the fault, and for
    # ELOOP even raises RuntimeError itself, which would misattribute the
    # firing. resolve() never raises OSError (see the exclusion probe), so
    # bypassing it changes nothing about the stat seam under test.
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: self)
    target = tmp_path / "project"
    target.mkdir()
    with fault_at(
        monkeypatch, module="csk.cli", func="_cmd_init", op="stat",
        target=target, err=err,
    ) as firings:
        code = cli.main(["init", str(target)])
    assert firings == [err]
    assert code == cli.EXIT_CONFIG
    assert f"cannot access target path {target}" in capsys.readouterr().err


@pytest.mark.parametrize(
    "shape",
    [pytest.param("missing", id="missing"), pytest.param("file", id="file-as-target")],
)
def test_init_absence_reports_does_not_exist(tmp_path, capsys, shape):
    target = tmp_path / "project"
    if shape == "file":
        target.write_text("x", encoding="utf-8")
    assert cli.main(["init", str(target)]) == cli.EXIT_CONFIG
    assert f"target path does not exist: {target.resolve()}" in capsys.readouterr().err


def test_init_symlink_loop_is_structured_refusal(tmp_path, capsys):
    """ELOOP through init refuses on every version (RuntimeError <= 3.13)."""

    first = tmp_path / "loopa"
    second = tmp_path / "loopb"
    try:
        first.symlink_to(second)
        second.symlink_to(first)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    assert cli.main(["init", str(first)]) == cli.EXIT_CONFIG
    assert capsys.readouterr().err.startswith("error: ")


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_config_show_read_refuses(monkeypatch, tmp_path, skills_root, csk_home, capsys, err):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    csk_config.save_config(cfg)
    monkeypatch.setenv("CSK_CONFIG", str(cfg.path))
    with fault_at(
        monkeypatch, module="csk.cli", func="_cmd_config_show", op="read_text",
        target=cfg.path, err=err,
    ) as firings:
        code = cli.main(["config", "show"])
    assert firings == [err]
    assert code == cli.EXIT_CONFIG
    captured = capsys.readouterr()
    assert f"Config path: {cfg.path}" in captured.out
    assert "cannot read config" in captured.err


def test_config_show_missing_reports_does_not_exist(monkeypatch, tmp_path, capsys):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    assert cli.main(["config", "show"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert f"Config path: {cfg_path}" in captured.out
    assert "Config does not exist" in captured.out


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_audit_publish_read_refuses(monkeypatch, tmp_path, skills_root, csk_home, capsys, err):
    _configured(tmp_path, skills_root, csk_home, monkeypatch)
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with fault_at(
        monkeypatch, module="csk.cli", func="_cmd_audit_publish", op="read_bytes",
        target=record, err=err,
    ) as firings:
        code = cli.main(
            ["audit", "--publish", str(record), "--registry", "https://r.example",
             "--token", "t0ken"]
        )
    assert firings == [err]
    assert code == cli.EXIT_CONFIG
    assert "cannot read audit record file" in capsys.readouterr().err


def test_audit_publish_missing_is_structured_refusal(
    monkeypatch, tmp_path, skills_root, csk_home, capsys
):
    _configured(tmp_path, skills_root, csk_home, monkeypatch)
    record = tmp_path / "record.json"
    code = cli.main(
        ["audit", "--publish", str(record), "--registry", "https://r.example", "--token", "t0ken"]
    )
    assert code == cli.EXIT_CONFIG
    assert "cannot read audit record file" in capsys.readouterr().err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_list_suffix_stat_degrades(monkeypatch, tmp_path, csk_home, skills_root, capsys, err):
    project = make_project(tmp_path)
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": []}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    with fault_at(
        monkeypatch, module="csk.cli", func="_project_path_suffix", op="stat",
        target=project, err=err,
    ) as firings:
        assert cli.main(["list", "--paths"]) == cli.EXIT_OK
    assert firings == [err]
    assert f"path={project} (unreadable)" in capsys.readouterr().out


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_parent_manifest_stat_refuses(monkeypatch, tmp_path, capsys, err):
    target = tmp_path / "project"
    target.mkdir()
    probed = tmp_path / manifest.MANIFEST_NAME
    with fault_at(
        monkeypatch, module="csk.cli", func="_nearest_parent_manifest", op="stat",
        target=probed, err=err,
    ) as firings:
        code = cli.main(["init", str(target)])
    assert firings == [err]
    assert code == cli.EXIT_CONFIG
    assert "Cannot inspect" in capsys.readouterr().err


@pytest.mark.parametrize("err", ERRNO_CASES)
def test_configured_resolution_stat_refuses(
    monkeypatch, tmp_path, csk_home, skills_root, capsys, err
):
    project = make_project(tmp_path)
    write_skillfile(project, {"schema_version": 1, "skills": []})
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"app": {"path": str(project), "agents": ["codex_cli"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    with fault_at(
        monkeypatch, module="csk.cli", func="_render_configured_project_resolution",
        op="stat", target=project, err=err,
    ) as firings:
        code = cli.main(["project", "resolve", "app"])
    assert firings == [err]
    assert code == cli.EXIT_CONFIG
    assert "Cannot access project path" in capsys.readouterr().err


def test_configured_resolution_missing_hash_is_empty(
    monkeypatch, tmp_path, csk_home, skills_root, capsys
):
    missing = tmp_path / "missing-project"
    cfg_path = csk_home / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(skills_root),
                "projects": {"ghost": {"path": str(missing), "agents": []}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    assert cli.main(["project", "resolve", "ghost"]) == cli.EXIT_OK
    assert "path_hash: \n" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Install-hook boundary: the trust refusal blocks in every audit mode.
# ---------------------------------------------------------------------------


def test_gate_plans_trust_refusal_blocks_without_warning(
    monkeypatch, tmp_path, csk_home, skills_root
):
    project = make_project(tmp_path)
    cfg = make_config(csk_home, skills_root, project)
    cfg = replace(cfg, audit=csk_config.AuditConfig(enabled=True))

    def failing_plans(*args, **kwargs):
        raise audit_trust.TrustRecordError(
            audit_trust.CODE_TRUST_UNREADABLE, "trust store unavailable"
        )

    monkeypatch.setattr(audit_pipeline, "audit_plans", failing_plans)
    result = audit_pipeline.gate_plans([], cfg, scope="app")
    assert result.blocked
    assert result.warnings == ()
    assert result.errors == (
        f"audit blocked: {audit_trust.CODE_TRUST_UNREADABLE}: trust store unavailable",
    )


# ---------------------------------------------------------------------------
# Linkage: every derived seam was fault-injected with every errno.
# This test must stay last in this module.
# ---------------------------------------------------------------------------


def test_linkage_every_enumerated_seam_faulted():
    """The derived enumeration and the fired faults cover each other.

    Every (module, function, op) found by inspection must have been
    fault-injected with all four errnos by its driver, and every firing
    must name a real enumerated seam, so neither a new seam nor a stale
    driver passes silently.
    """

    assert set(IN_SCOPE_MODULES) == {"csk.audit.trust", "csk.manifest", "csk.cli"}
    enumerated = {(seam.module, seam.func, seam.op) for seam in enumerate_all_seams()}
    assert enumerated, "empty enumeration proves nothing"
    fired = {(module, func, op, err) for module, func, op, err in FIRED}
    assert fired, "no fault fired; drivers did not run before the linkage test"

    missing = [
        (module, func, op, name)
        for module, func, op in sorted(enumerated)
        for err, name in FAULT_ERRNOS
        if (module, func, op, err) not in fired
    ]
    assert not missing, missing

    stale = [
        (module, func, op)
        for module, func, op, _ in sorted(fired)
        if (module, func, op) not in enumerated
    ]
    assert not stale, stale
