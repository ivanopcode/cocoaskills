"""Machine-local source audit bound to the existing assurance gates.

Covers TASK-260916-11yseo: the ``source-audit-v1`` production reader, the
persisted-report binding (never self-authorizing), operator pins that admit
only where already authorized, network-git registry evidence on exact match
plus live backing, receipt-3 build-input binding without a context-only
fallback, and audit-before-cache/compiler ordering for local packages.

Counter instruments reused, not rebuilt: the planner ordering test reuses the
recording cache/toolchain fakes and the events list from
``test_build_receipt_v3``; the registry fixtures reuse the signing helpers
from ``test_audit_registry``; the schema cases come from the authenticated
draft-sources suite via ``test_draft_sources_conformance``.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import random
import sys
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from conftest import make_project, make_skill_repo, write_skillfile
import test_draft_sources_conformance as draft_harness
from test_audit_registry import _make_key, _record_body, _sign_record
from test_build_receipt_v3 import (
    _install_v3,
    _package,
    _planning_provider,
    _RecordingCache,
    _RecordingToolchainSession,
)

from csk import audit_registry, cli, install_marker, installer
from csk.audit import canary as audit_canary
from csk.audit import pipeline as audit_pipeline
from csk.audit import trust as audit_trust
from csk.audit.backends.null_backend import NullBackend
from csk.audit.capabilities import CapabilityManifest
from csk.audit.model import Decision, Finding, Severity, Surface, TrustRecord
from csk.builds import metadata as build_metadata
from csk.builds import planner as build_planner
from csk.builds import source as build_source
from csk.config import AuditConfig, GlobalConfig, RegistryConfig, load_config
from csk import git_ops, hashing
from csk.installer import SkillPlan
from csk import manifest
from csk import skillspec
from csk.sources import _selection_fs
from csk.sources import package_identity as source_package
from csk.sources import snapshot as source_snapshot
from csk.sources import source_audit
from csk.sources.source_audit import SourceAuditError, SourceAuditPolicy

CONTENT_A = "sha256:" + "a" * 64
CONTENT_B = "sha256:" + "b" * 64
CONTENT_C = "sha256:" + "c" * 64
SNAPSHOT_A = "sha256:" + "1" * 64
SNAPSHOT_B = "sha256:" + "2" * 64
CONTEXT_A = "sha256:" + "c" * 64
CONTEXT_B = "sha256:" + "d" * 64
REPO = "github.com/example/golden-skills"
REPO_OTHER = "github.com/example/other-skills"
COMMIT_HEX = "0123456789abcdef0123456789abcdef01234567"
COMMIT_OTHER = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SCRIPT_POLICY = "manager-worker-v1"
CREATED_AT = "2026-09-10T00:00:00Z"
RAN_AT = "2026-09-10T00:00:00Z"
SOURCE_IDENTITY = "github.com/example/golden-skills"


def _policy(**overrides: Any) -> SourceAuditPolicy:
    values: dict[str, Any] = {
        "mode": "advisory",
        "fail_on": "high",
        "backend": "null",
        "registry_policy": "advisory",
        "revocations": (),
        "script_policy": SCRIPT_POLICY,
    }
    values.update(overrides)
    return SourceAuditPolicy(**values)


def _finding(
    *,
    severity: Severity = Severity.HIGH,
    verifiable: bool = True,
    finding_id: str = "test.finding",
) -> Finding:
    return Finding(
        id=finding_id,
        surface=Surface.CODE,
        category="test",
        severity=severity,
        location=None,
        evidence="test evidence",
        detector="test.detector",
        confidence="high",
        verifiable=verifiable,
    )


def _audit_report(
    content: str,
    decision: Decision,
    findings: tuple[Finding, ...] = (),
    *,
    schema_version: int = 3,
    source: str = "local:packages/golden",
    commit: str = COMMIT_HEX,
) -> audit_pipeline.AuditReport:
    return audit_pipeline.AuditReport(
        scope="test",
        skill="golden",
        source=source,
        ref_kind="branch",
        ref="main",
        commit=commit,
        schema_version=schema_version,
        source_file=None,
        runtime_roots=(),
        content_sha256=content,
        findings=findings,
        decision=decision,
        ran_at=RAN_AT,
    )


def _recorded(
    tmp_path: Path,
    *,
    content: str = CONTENT_A,
    package: source_package.PackageIdentity | None = None,
    decision: Decision = Decision.ALLOW,
    findings: tuple[Finding, ...] = (),
    schema_version: int = 3,
    source: str = "local:packages/golden",
    git: str | None = None,
    policy: SourceAuditPolicy | None = None,
) -> tuple[source_audit.SourceAuditRecord, Path, source_package.PackageIdentity, str, SourceAuditPolicy]:
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir(exist_ok=True)
    active_policy = policy if policy is not None else _policy()
    active_package = (
        package
        if package is not None
        else source_package.LocalSnapshot(snapshot=SNAPSHOT_A)
    )
    report = _audit_report(
        content, decision, findings, schema_version=schema_version, source=source
    )
    record = source_audit.record_source_audit(
        report,
        csk_home=csk_home,
        package=active_package,
        git=git,
        policy=active_policy,
        created_at=CREATED_AT,
    )
    return record, csk_home, active_package, content, active_policy


def _stored_envelope(csk_home: Path, content: str) -> dict[str, Any]:
    path = source_audit.source_audit_report_path(csk_home, content)
    return json.loads(path.read_bytes())


def _rewrite_envelope(csk_home: Path, content: str, mutate: Any) -> None:
    from csk import protocol_json

    path = source_audit.source_audit_report_path(csk_home, content)
    envelope = json.loads(path.read_bytes())
    mutate(envelope)
    path.write_bytes(protocol_json.canonical_bytes(envelope))


def _tamper_report(csk_home: Path, content: str) -> None:
    def mutate(envelope: dict[str, Any]) -> None:
        envelope["pins"]["reason"] = "tampered"
        envelope["ran_at"] = "2026-09-11T00:00:00Z"

    _rewrite_envelope(csk_home, content, mutate)


def _config(
    csk_home: Path,
    *,
    mode: str = "advisory",
    revocations: list[str] | None = None,
) -> GlobalConfig:
    return GlobalConfig(
        path=csk_home / "config.json",
        skills_root=csk_home / "skills",
        preferred_locale=None,
        default_agents=[],
        adapter_mode="auto",
        worktree_alias_pattern="[A-Z]+-[0-9]+",
        projects={},
        audit=AuditConfig(enabled=True, mode=mode, revocations=revocations or []),
    )


def _skill_plan(
    snapshot: Path,
    *,
    source: str = "local:packages/golden",
    git: str | None = None,
    schema_version: int = 3,
    commit: str = COMMIT_HEX,
) -> SkillPlan:
    return SkillPlan(
        decl=manifest.SkillDecl(
            name="golden",
            source=source,
            ref=manifest.SkillRef(kind="branch", value="main"),
            git=git,
        ),
        resolved=git_ops.ResolvedRef(kind="branch", ref="main", commit=commit),
        repo=snapshot,
        snapshot=snapshot,
        spec=skillspec.SkillSpec(
            commands={},
            source_file=None,
            schema_version=schema_version,
            capabilities=CapabilityManifest.implicit_none(),
        ),
    )


def _snapshot_dir(tmp_path: Path, name: str = "snap") -> Path:
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "SKILL.md").write_text("# golden\n", encoding="utf-8")
    return root


# --- The production reader: schema cases with expected polarity (S-POLICY). ---


def test_source_audit_schema_cases_through_production_reader() -> None:
    """Every source-audit-v1 schema case parses with its expected polarity.

    S-POLICY instrument: the cases run through the production reader
    (``source_audit.parse_source_audit``), and an audit hook proves parsing
    performs zero filesystem or network side effects.
    """
    if not draft_harness.ROOT_TEXT:
        pytest.skip("CSK_DRAFT_SOURCES_SUITE_ROOT is not set")
    cases = [
        entry
        for entry in draft_harness.SCHEMA_CASES
        if entry["schema"] == "source-audit-v1.schema.json"
    ]
    assert len(cases) == 4
    root = draft_harness._suite_root()
    values = []
    for entry in cases:
        relative = Path(entry["instance"])
        assert not relative.is_absolute() and ".." not in relative.parts
        values.append((entry, json.loads((root / relative).read_bytes())))
    # Warm up interpreter-lazy imports (notably _strptime) before arming the
    # counter, so the assertion below measures production behavior only.
    source_audit.parse_source_audit(dict(values[0][1]))
    events: list[str] = []
    state = {"armed": True}

    def hook(event: str, args: object) -> None:
        if state["armed"] and (event == "open" or event.startswith("socket.")):
            events.append(event)

    sys.addaudithook(hook)
    try:
        for entry, value in values:
            if entry["valid"]:
                record = source_audit.parse_source_audit(value)
                assert record.to_json() == value
            else:
                with pytest.raises(SourceAuditError) as excinfo:
                    source_audit.parse_source_audit(value)
                assert excinfo.value.code == source_audit.CODE_INVALID
    finally:
        state["armed"] = False
    assert events == []


def _valid_record_json() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "package": {"kind": "local-snapshot", "snapshot": SNAPSHOT_A},
        "content_sha256": CONTENT_A,
        "decision": "allow",
        "policy_sha256": CONTENT_B,
        "evidence_sha256": CONTENT_C,
        "created_at": CREATED_AT,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda v: v.pop("evidence_sha256"), id="missing-member"),
        pytest.param(lambda v: v.update(unexpected=True), id="unknown-member"),
        pytest.param(lambda v: v.update(schema_version=2), id="wrong-version"),
        pytest.param(lambda v: v.update(schema_version=True), id="bool-version"),
        pytest.param(lambda v: v.update(schema_version="1"), id="str-version"),
        pytest.param(lambda v: v.update(decision="trusted"), id="bad-decision"),
        pytest.param(lambda v: v.update(decision="ALLOW"), id="upper-decision"),
        pytest.param(lambda v: v.update(decision=None), id="null-decision"),
        pytest.param(
            lambda v: v.update(content_sha256="sha256:xyz"), id="bad-content-digest"
        ),
        pytest.param(
            lambda v: v.update(policy_sha256="a" * 64), id="unprefixed-policy-digest"
        ),
        pytest.param(
            lambda v: v.update(evidence_sha256="SHA256:" + "A" * 64),
            id="upper-evidence-digest",
        ),
        pytest.param(
            lambda v: v.update(created_at="2026-09-10 00:00:00"), id="bad-timestamp"
        ),
        pytest.param(
            lambda v: v.update(created_at="2026-13-10T00:00:00Z"), id="impossible-timestamp"
        ),
        pytest.param(lambda v: v.update(package={"kind": "nope"}), id="bad-package"),
        pytest.param(lambda v: v.update(package=None), id="null-package"),
    ],
)
def test_parse_source_audit_rejects_malformed_shapes(mutate: Any) -> None:
    value = _valid_record_json()
    mutate(value)
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.parse_source_audit(value)
    assert excinfo.value.code == source_audit.CODE_INVALID


def test_parse_source_audit_rejects_non_objects() -> None:
    for value in (None, True, 1, "x", [], b"{}"):
        with pytest.raises(SourceAuditError) as excinfo:
            source_audit.parse_source_audit(value)
        assert excinfo.value.code == source_audit.CODE_INVALID


@pytest.mark.parametrize(
    "package",
    [
        source_package.LocalSnapshot(snapshot=SNAPSHOT_A),
        source_package.NetworkGit(
            repository=REPO,
            commit=source_package.LockedCommit(object_format="sha1", hex=COMMIT_HEX),
        ),
        source_package.ConfiguredGit(
            source="golden",
            commit=source_package.LockedCommit(object_format="sha1", hex=COMMIT_HEX),
        ),
    ],
    ids=["local-snapshot", "network-git", "configured-git"],
)
@pytest.mark.parametrize("decision", ["allow", "warn", "block", "require_pin"])
def test_parse_source_audit_accepts_every_package_kind_and_decision(
    package: source_package.PackageIdentity, decision: str
) -> None:
    value = _valid_record_json()
    value["package"] = package.to_json()
    value["decision"] = decision
    record = source_audit.parse_source_audit(value)
    assert record.package == package
    assert record.decision == decision
    assert record.to_json() == value


# --- Record and validate: the binding round trip. ---


def test_record_validate_round_trip_admits(tmp_path: Path) -> None:
    record, csk_home, package, content, policy = _recorded(tmp_path)
    validated = source_audit.validate_source_audit(
        record,
        csk_home=csk_home,
        policy=policy,
        expected_package=package,
        expected_content_sha256=content,
    )
    assert validated.decision == Decision.ALLOW
    assert validated.findings == ()
    assert validated.warnings == ()
    assert validated.evidence_sha256 == record.evidence_sha256
    assert validated.policy_sha256 == record.policy_sha256


def test_record_embeds_complete_evidence(tmp_path: Path) -> None:
    from csk import protocol_json

    findings = (_finding(), _finding(severity=Severity.LOW, finding_id="test.low"))
    policy = _policy(
        mode="strict", revocations=["source:local:packages/*"], script_policy="custom-v1"
    )
    record, csk_home, _, content, _ = _recorded(
        tmp_path, decision=Decision.WARN, findings=findings, policy=policy
    )
    raw = source_audit.source_audit_report_path(csk_home, content).read_bytes()
    assert protocol_json.is_canonical(raw)
    envelope = json.loads(raw)
    assert [item["id"] for item in envelope["findings"]] == ["test.finding", "test.low"]
    assert envelope["pins"] == {"pinned": False, "pinned_by": None, "reason": None}
    assert envelope["revocations"] == ["source:local:packages/*"]
    assert envelope["script_policy"] == "custom-v1"
    assert envelope["assurance_policy"] == {
        "mode": "strict",
        "fail_on": "high",
        "backend": "null",
        "registry_policy": "advisory",
    }
    assert envelope["decision"] == "warn"
    assert record.evidence_sha256 == source_audit.evidence_digest(raw)


def test_record_refuses_confirm_decision(tmp_path: Path) -> None:
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    report = _audit_report(CONTENT_A, Decision.CONFIRM)
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.record_source_audit(
            report,
            csk_home=csk_home,
            package=source_package.LocalSnapshot(snapshot=SNAPSHOT_A),
            git=None,
            policy=_policy(),
        )
    assert excinfo.value.code == source_audit.CODE_INVALID


def _surrogate_finding(*, location: str | None, evidence: str) -> Finding:
    from csk.audit.model import Location

    return Finding(
        id="static.opaque.unanalyzable-artifact",
        surface=Surface.CODE,
        category="opaque",
        severity=Severity.HIGH,
        location=None if location is None else Location(location),
        evidence=evidence,
        detector="static.opaque",
        confidence="high",
        verifiable=True,
    )


@pytest.mark.parametrize(
    "field,finding",
    [
        pytest.param(
            "location",
            _surrogate_finding(location="scripts/\udcff.bin", evidence="binary content"),
            id="location",
        ),
        pytest.param(
            "evidence",
            _surrogate_finding(location="scripts/golden.bin", evidence="binary \udcff content"),
            id="evidence",
        ),
    ],
)
def test_record_with_surrogate_finding_string_is_structured(
    tmp_path: Path, field: str, finding: Finding
) -> None:
    """A lone surrogate from a pipeline-produced finding refuses typed (F5).

    Locations derive from real filenames and evidence from detector output,
    so neither may escape as a raw parser error; the refusal names the
    offending field. Production call site:
    ``source_audit.record_source_audit``.
    """
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    report = _audit_report(CONTENT_A, Decision.WARN, (finding,))
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.record_source_audit(
            report,
            csk_home=csk_home,
            package=source_package.LocalSnapshot(snapshot=SNAPSHOT_A),
            git=None,
            policy=_policy(),
            created_at=CREATED_AT,
        )
    assert excinfo.value.code == source_audit.CODE_INVALID
    assert field in excinfo.value.detail


@pytest.mark.parametrize(
    "seam",
    [
        pytest.param("mkdir", id="mkdir"),
        pytest.param("write", id="write"),
    ],
)
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError(errno.ENOSPC, "no space left"), id="enospc"),
        pytest.param(PermissionError(errno.EACCES, "denied"), id="eacces"),
        pytest.param(IsADirectoryError(errno.EISDIR, "is a directory"), id="eisdir"),
        pytest.param(NotADirectoryError(errno.ENOTDIR, "not a directory"), id="enotdir"),
    ],
)
def test_record_store_write_failure_is_structured(
    tmp_path: Path, seam: str, error: OSError
) -> None:
    """Every writer-seam failure is a typed refusal naming the store path.

    The class is the writer's failure surface, not one seam: ``mkdir`` and
    ``write_bytes`` are fault-injected at the real call site over the
    ``OSError`` family, each with a reached-flag proving the injected
    operation was reached. Production call site:
    ``source_audit.record_source_audit``.
    """
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    report = _audit_report(CONTENT_A, Decision.ALLOW)
    package = source_package.LocalSnapshot(snapshot=SNAPSHOT_A)
    policy = _policy()
    target = source_audit.source_audit_report_path(csk_home, CONTENT_A)
    reached: list[Path] = []
    if seam == "mkdir":
        original_mkdir = Path.mkdir

        def faulted_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
            if self == target.parent:
                reached.append(self)
                raise error
            original_mkdir(self, *args, **kwargs)

        ctx: Any = patch.object(Path, "mkdir", faulted_mkdir)
        expected_reached = [target.parent]
    else:
        assert seam == "write"
        original_write = Path.write_bytes

        def faulted_write(self: Path, data: bytes) -> int:
            if self == target:
                reached.append(self)
                raise error
            return original_write(self, data)

        ctx = patch.object(Path, "write_bytes", faulted_write)
        expected_reached = [target]
    with ctx:
        with pytest.raises(SourceAuditError) as excinfo:
            source_audit.record_source_audit(
                report,
                csk_home=csk_home,
                package=package,
                git=None,
                policy=policy,
                created_at=CREATED_AT,
            )
    assert excinfo.value.code == source_audit.CODE_STORE_UNWRITABLE
    assert str(target) in excinfo.value.detail
    assert reached == expected_reached


def test_record_store_namespace_is_a_file_is_structured(tmp_path: Path) -> None:
    """A file at the store namespace refuses typed, without mocks."""
    csk_home = tmp_path / "csk-home"
    (csk_home / "audit").mkdir(parents=True)
    (csk_home / "audit" / source_audit.STORE_NAMESPACE).write_bytes(b"not a directory")
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.record_source_audit(
            _audit_report(CONTENT_A, Decision.ALLOW),
            csk_home=csk_home,
            package=source_package.LocalSnapshot(snapshot=SNAPSHOT_A),
            git=None,
            policy=_policy(),
            created_at=CREATED_AT,
        )
    assert excinfo.value.code == source_audit.CODE_STORE_UNWRITABLE


def test_record_store_write_unfaulted_control(tmp_path: Path) -> None:
    """Positive control: the same writer fixture persists and validates."""
    record, csk_home, package, content, policy = _recorded(tmp_path)
    validated = source_audit.validate_source_audit(
        record,
        csk_home=csk_home,
        policy=policy,
        expected_package=package,
        expected_content_sha256=content,
    )
    assert validated.decision == Decision.ALLOW


def test_policy_digest_stable_and_order_insensitive() -> None:
    first = _policy(revocations=[CONTENT_A, "source:local:*"])
    second = _policy(revocations=["source:local:*", CONTENT_A])
    assert first.digest() == second.digest()
    assert first.digest() != _policy().digest()
    assert first.digest() != _policy(mode="strict").digest()
    assert first.digest() != _policy(script_policy="other-v1").digest()


def test_policy_dedupes_duplicate_revocation_spellings() -> None:
    """The digest is a function of the revocation identity set, not spellings."""
    single = _policy(revocations=["sha256:" + "a" * 64])
    duplicate = _policy(revocations=["sha256:" + "a" * 64, "A" * 64])
    assert duplicate.revocations == (CONTENT_A,)
    assert single.digest() == duplicate.digest()
    assert duplicate == single


def test_policy_from_config_bridges_current_policy(tmp_path: Path) -> None:
    config = _config(tmp_path, mode="strict", revocations=[CONTENT_A])
    policy = source_audit.policy_from_config(config, script_policy=SCRIPT_POLICY)
    assert policy == _policy(
        mode="strict",
        fail_on="high",
        backend="null",
        registry_policy="advisory",
        revocations=(CONTENT_A,),
        script_policy=SCRIPT_POLICY,
    )


@pytest.mark.parametrize(
    "revocations",
    [
        pytest.param(["not-a-hash"], id="garbage"),
        pytest.param(["sha256:xyz"], id="short-hash"),
        pytest.param(["source:"], id="empty-source-pattern"),
        pytest.param([None], id="non-string"),
    ],
)
def test_policy_rejects_malformed_revocations(revocations: Any) -> None:
    with pytest.raises(SourceAuditError) as excinfo:
        _policy(revocations=revocations)
    assert excinfo.value.code == source_audit.CODE_INVALID


def test_policy_accepts_bare_hex_revocations() -> None:
    policy = _policy(revocations=["a" * 64])
    assert policy.revocations == (CONTENT_A,)


@pytest.mark.parametrize(
    "raw,expected",
    [
        pytest.param("a" * 64, CONTENT_A, id="bare-lower"),
        pytest.param("A" * 64, CONTENT_A, id="bare-upper"),
        pytest.param("sha256:" + "a" * 64, CONTENT_A, id="prefixed-lower"),
        pytest.param("sha256:" + "A" * 64, CONTENT_A, id="prefixed-upper"),
        pytest.param("sha256:" + "aB" * 32, "sha256:" + "ab" * 32, id="prefixed-mixed"),
    ],
)
def test_policy_normalizes_revocation_case_and_form(raw: str, expected: str) -> None:
    """Every digest spelling denotes one canonical identity (F4 class)."""
    policy = _policy(revocations=[raw])
    assert policy.revocations == (expected,)
    assert _policy(revocations=[raw]).digest() == _policy(revocations=[expected]).digest()


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("mode", "Strict", id="mode-case"),
        pytest.param("mode", "permissive", id="mode-unknown"),
        pytest.param("fail_on", "HIGH", id="fail-on-case"),
        pytest.param("fail_on", "everything", id="fail-on-unknown"),
        pytest.param("registry_policy", "Strict", id="registry-policy-case"),
        pytest.param("registry_policy", "required", id="registry-policy-unknown"),
    ],
)
def test_policy_rejects_unknown_enum_values(field: str, value: str) -> None:
    """Unknown enum values refuse structurally instead of deciding silently."""
    with pytest.raises(SourceAuditError) as excinfo:
        _policy(**{field: value})
    assert excinfo.value.code == source_audit.CODE_INVALID


@pytest.mark.parametrize(
    "revocations,expected",
    [
        pytest.param(
            ["sha256:" + "A" * 64], ("sha256:" + "a" * 64,), id="upper-prefixed"
        ),
        pytest.param(["B" * 64], ("sha256:" + "b" * 64,), id="upper-bare"),
        pytest.param(
            ["sha256:" + "A" * 64, "B" * 64],
            ("sha256:" + "a" * 64, "sha256:" + "b" * 64),
            id="upper-pair",
        ),
    ],
)
def test_policy_from_config_with_uppercase_revocations(
    tmp_path: Path, revocations: list[str], expected: tuple[str, ...]
) -> None:
    """A policy bridged from a loadable config is constructible (F4).

    Production call site: ``source_audit.policy_from_config`` over
    ``csk.config.load_config`` output. The config loader admits upper-case
    digests and the revocation matcher normalizes case, so the bridge must
    normalize rather than fail every validation on the machine closed.
    """
    from csk import config as csk_config

    path = tmp_path / "config.json"
    path.write_bytes(
        json.dumps(
            {
                "schema_version": 1,
                "skills_root": str(tmp_path / "skills"),
                "projects": {},
                "audit": {"enabled": True, "revocations": revocations},
            }
        ).encode("utf-8")
    )
    loaded = csk_config.load_config(path)
    policy = source_audit.policy_from_config(loaded, script_policy=SCRIPT_POLICY)
    assert policy.revocations == expected


# --- The binding is not a credential: report-defect families. ---


def _decision_fixture(decision: str) -> tuple[Decision, tuple[Finding, ...]]:
    if decision == "allow":
        return Decision.ALLOW, ()
    if decision == "warn":
        return Decision.WARN, (_finding(severity=Severity.LOW, finding_id="test.low"),)
    if decision == "block":
        return Decision.BLOCK, (_finding(),)
    assert decision == "require_pin"
    return Decision.REQUIRE_PIN, ()


@pytest.mark.parametrize("decision", ["allow", "warn", "block", "require_pin"])
def test_missing_report_refuses_for_every_decision(tmp_path: Path, decision: str) -> None:
    """A record over an absent report is refused, whatever it claims.

    Production call site: ``source_audit.validate_source_audit``.
    """
    report_decision, findings = _decision_fixture(decision)
    record, csk_home, package, content, policy = _recorded(
        tmp_path, decision=report_decision, findings=findings
    )
    source_audit.source_audit_report_path(csk_home, content).unlink()
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=policy,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_REPORT_MISSING


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(PermissionError("denied"), id="permission"),
        pytest.param(IsADirectoryError("is a directory"), id="is-a-directory"),
        pytest.param(NotADirectoryError("not a directory"), id="not-a-directory"),
        pytest.param(OSError(errno.EACCES, "denied"), id="eacces"),
        pytest.param(OSError(errno.ELOOP, "loop"), id="eloop"),
    ],
)
def test_unreadable_report_refuses_with_distinct_code(
    tmp_path: Path, error: OSError
) -> None:
    """A read failure is never reported as an absence (S-ERRORS instrument).

    Fault injection at the real call site (``Path.read_bytes`` inside the
    report loader), with a reached-flag and the same fixture succeeding
    unfaulted as the positive control.
    """
    record, csk_home, package, content, policy = _recorded(tmp_path)
    target = source_audit.source_audit_report_path(csk_home, content)
    reached = {"read": False}
    original_read_bytes = Path.read_bytes

    def failing_read_bytes(self: Path) -> bytes:
        if self == target:
            reached["read"] = True
            raise error
        return original_read_bytes(self)

    with patch.object(Path, "read_bytes", failing_read_bytes):
        with pytest.raises(SourceAuditError) as excinfo:
            source_audit.validate_source_audit(
                record,
                csk_home=csk_home,
                policy=policy,
                expected_package=package,
                expected_content_sha256=content,
            )
    assert reached["read"] is True
    assert excinfo.value.code == source_audit.CODE_REPORT_UNREADABLE
    # Positive control: the same fixture validates unfaulted.
    validated = source_audit.validate_source_audit(
        record,
        csk_home=csk_home,
        policy=policy,
        expected_package=package,
        expected_content_sha256=content,
    )
    assert validated.decision == Decision.ALLOW


def test_dangling_symlink_report_is_unreadable_not_missing(tmp_path: Path) -> None:
    """A dangling link at the report path is a failure, never an absence (N3)."""
    record, csk_home, package, content, policy = _recorded(tmp_path)
    path = source_audit.source_audit_report_path(csk_home, content)
    path.unlink()
    path.symlink_to(tmp_path / "nowhere.json")
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=policy,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_REPORT_UNREADABLE


def _malformed_envelope_cases() -> list[Any]:
    from csk import protocol_json

    def raw_bytes(value: bytes) -> Any:
        return value

    cases: list[Any] = [
        pytest.param(raw_bytes(b"{oops"), id="not-json"),
        pytest.param(raw_bytes(b"\xff\xfe binary"), id="not-utf8"),
        pytest.param(raw_bytes(b"\"2026-09-10T00:00:00Z\""), id="not-envelope-json"),
    ]

    def mutate(cases_list: list[Any], name: str, fn: Any) -> None:
        cases_list.append(pytest.param(fn, id=name))

    mutate(cases, "missing-member", lambda e: e.pop("findings"))
    mutate(cases, "extra-member", lambda e: e.update(smuggled=True))
    mutate(cases, "bad-schema-version", lambda e: e.update(schema_version=2))
    mutate(cases, "bool-schema-version", lambda e: e.update(schema_version=True))
    mutate(
        cases,
        "bad-assurance-policy",
        lambda e: e.update(assurance_policy={"mode": "advisory"}),
    )
    mutate(
        cases,
        "non-string-assurance-label",
        lambda e: e["assurance_policy"].update(mode=1),
    )
    mutate(cases, "bad-revocations", lambda e: e.update(revocations=[None]))
    mutate(cases, "revocations-not-list", lambda e: e.update(revocations="x"))
    mutate(cases, "bad-decision", lambda e: e.update(decision="trusted"))
    mutate(cases, "bad-canary-flag", lambda e: e.update(canary_passed="yes"))
    mutate(cases, "bad-ran-at", lambda e: e.update(ran_at="yesterday"))
    mutate(
        cases, "bad-content-hash", lambda e: e.update(content_sha256="sha256:xyz")
    )
    mutate(cases, "findings-not-list", lambda e: e.update(findings={}))
    mutate(
        cases,
        "finding-not-object",
        lambda e: e.update(findings=["nope"]),
    )
    mutate(
        cases,
        "finding-bad-severity",
        lambda e: e["findings"].append(
            {
                "id": "test.bad",
                "surface": "code",
                "category": "test",
                "severity": "bogus",
                "evidence": "x",
                "detector": "test",
                "confidence": "high",
                "verifiable": True,
            }
        ),
    )
    mutate(cases, "bad-pins", lambda e: e.update(pins={"pinned": "yes"}))
    mutate(
        cases,
        "bad-summary-member",
        lambda e: e["report"].pop("schema_version"),
    )
    mutate(
        cases,
        "bad-skill-schema-version",
        lambda e: e["report"].update(schema_version="3"),
    )
    mutate(
        cases,
        "bad-summary-ran-at",
        lambda e: e["report"].update(ran_at="tomorrow"),
    )
    mutate(cases, "bad-script-policy", lambda e: e.update(script_policy=""))
    mutate(cases, "bad-git", lambda e: e.update(git=42))
    mutate(
        cases, "bad-package", lambda e: e.update(package={"kind": "local-snapshot"})
    )
    return cases


@pytest.mark.parametrize("corrupt", _malformed_envelope_cases())
def test_malformed_report_family_refuses(tmp_path: Path, corrupt: Any) -> None:
    record, csk_home, package, content, policy = _recorded(tmp_path)
    path = source_audit.source_audit_report_path(csk_home, content)
    if isinstance(corrupt, bytes):
        path.write_bytes(corrupt)
    else:
        _rewrite_envelope(csk_home, content, corrupt)
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=policy,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_REPORT_MALFORMED


@pytest.mark.parametrize("decision", ["allow", "warn", "require_pin"])
def test_tampered_report_refuses_for_every_decision(tmp_path: Path, decision: str) -> None:
    """The evidence digest is compared; a well-formed but altered report fails.

    Production call site: ``source_audit.validate_source_audit``.
    """
    report_decision, findings = _decision_fixture(decision)
    record, csk_home, package, content, policy = _recorded(
        tmp_path, decision=report_decision, findings=findings
    )
    if decision == "require_pin":
        audit_trust.pin_content_hash(csk_home, content, reason="test", pinned_by="test")
    _tamper_report(csk_home, content)
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=policy,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_EVIDENCE_MISMATCH


_POLICY_CHANGES = [
    pytest.param({"mode": "strict"}, id="mode"),
    pytest.param({"fail_on": "low"}, id="fail-on"),
    pytest.param({"backend": "command"}, id="backend"),
    pytest.param({"registry_policy": "strict"}, id="registry-policy"),
    pytest.param({"revocations": (CONTENT_B,)}, id="revocations"),
    pytest.param({"script_policy": "other-v1"}, id="script-policy"),
]


@pytest.mark.parametrize("change", _POLICY_CHANGES)
@pytest.mark.parametrize("decision", ["allow", "warn", "require_pin"])
def test_stale_policy_refuses_for_every_member_and_decision(
    tmp_path: Path, change: dict[str, Any], decision: str
) -> None:
    """A record taken under another policy is refused under the current one.

    Production call site: ``source_audit.validate_source_audit``.
    """
    report_decision, findings = _decision_fixture(decision)
    record, csk_home, package, content, policy = _recorded(
        tmp_path, decision=report_decision, findings=findings
    )
    if decision == "require_pin":
        audit_trust.pin_content_hash(csk_home, content, reason="test", pinned_by="test")
    current = replace(policy, **change)
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=current,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_POLICY_MISMATCH


_STORED_POLICY_CHANGES = [
    pytest.param({"backend": "openai"}, id="backend"),
    pytest.param({"registry_policy": "strict"}, id="registry-policy"),
    pytest.param({"script_policy": "other-v1"}, id="script-policy"),
]


@pytest.mark.parametrize("change", _STORED_POLICY_CHANGES)
@pytest.mark.parametrize("decision", ["allow", "warn", "require_pin"])
def test_stored_report_refuses_stale_non_recomputed_labels(
    tmp_path: Path, change: dict[str, Any], decision: str
) -> None:
    """The stored path refuses the same stale-label class as the record path (F3).

    Backend, registry policy and script policy are persisted labels the
    recomputation does not re-derive, so a report taken under other labels
    refuses here too. Production call site:
    ``source_audit.validate_stored_report``.
    """
    report_decision, findings = _decision_fixture(decision)
    _, csk_home, package, content, policy = _recorded(
        tmp_path, decision=report_decision, findings=findings
    )
    if decision == "require_pin":
        audit_trust.pin_content_hash(csk_home, content, reason="test", pinned_by="test")
    current = replace(policy, **change)
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_stored_report(
            package=package,
            content_sha256=content,
            csk_home=csk_home,
            policy=current,
        )
    assert excinfo.value.code == source_audit.CODE_POLICY_MISMATCH


def test_stored_report_refuses_recorded_canary_failure(tmp_path: Path) -> None:
    """A persisted report recording canary_passed=false refuses (F3)."""
    _, csk_home, package, content, policy = _recorded(tmp_path)
    _rewrite_envelope(csk_home, content, lambda e: e.update(canary_passed=False))
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_stored_report(
            package=package,
            content_sha256=content,
            csk_home=csk_home,
            policy=policy,
        )
    assert excinfo.value.code == source_audit.CODE_CANARY_FAILED


def test_record_persists_observed_canary_outcome(tmp_path: Path) -> None:
    """The canary label is observed at record time, not a constant (N11)."""
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    package = source_package.LocalSnapshot(snapshot=SNAPSHOT_A)
    with patch.object(audit_canary, "run_static_canary", return_value=False):
        source_audit.record_source_audit(
            _audit_report(CONTENT_A, Decision.ALLOW),
            csk_home=csk_home,
            package=package,
            git=None,
            policy=_policy(),
            created_at=CREATED_AT,
        )
    assert _stored_envelope(csk_home, CONTENT_A)["canary_passed"] is False
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_stored_report(
            package=package,
            content_sha256=CONTENT_A,
            csk_home=csk_home,
            policy=_policy(),
        )
    assert excinfo.value.code == source_audit.CODE_CANARY_FAILED


def test_record_binding_mismatch_family(tmp_path: Path) -> None:
    """The record must bind the expected package, content and decision."""
    record, csk_home, package, content, policy = _recorded(tmp_path)
    other_package = source_package.LocalSnapshot(snapshot=SNAPSHOT_B)
    swapped_package = source_audit.SourceAuditRecord(
        schema_version=record.schema_version,
        package=other_package,
        content_sha256=record.content_sha256,
        decision=record.decision,
        policy_sha256=record.policy_sha256,
        evidence_sha256=record.evidence_sha256,
        created_at=record.created_at,
    )
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            swapped_package,
            csk_home=csk_home,
            policy=policy,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_BINDING_MISMATCH

    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=policy,
            expected_package=package,
            expected_content_sha256=CONTENT_B,
        )
    assert excinfo.value.code == source_audit.CODE_BINDING_MISMATCH

    flipped = source_audit.SourceAuditRecord(
        schema_version=record.schema_version,
        package=record.package,
        content_sha256=record.content_sha256,
        decision="warn",
        policy_sha256=record.policy_sha256,
        evidence_sha256=record.evidence_sha256,
        created_at=record.created_at,
    )
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            flipped,
            csk_home=csk_home,
            policy=policy,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_BINDING_MISMATCH


def test_record_expected_package_swap_refuses(tmp_path: Path) -> None:
    """An expected package the record was not taken for refuses.

    The stored-vs-expected check duplicates this refusal behind the
    record-vs-expected check (defense in depth): dropping the record-side
    disjunct is masked here and killed on the record-swapped fixture.
    """
    record, csk_home, package, content, policy = _recorded(tmp_path)
    other = source_package.LocalSnapshot(snapshot=SNAPSHOT_B)
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=policy,
            expected_package=other,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_BINDING_MISMATCH


def test_stored_report_binding_mismatch_family(tmp_path: Path) -> None:
    """A stored report naming another package or content is refused."""
    record, csk_home, package, content, policy = _recorded(tmp_path)
    _rewrite_envelope(
        csk_home, content, lambda e: e.update(package=source_package.LocalSnapshot(snapshot=SNAPSHOT_B).to_json())
    )
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=policy,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_BINDING_MISMATCH

    record, csk_home, package, content, policy = _recorded(tmp_path)
    _rewrite_envelope(csk_home, content, lambda e: e.update(content_sha256=CONTENT_B))
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=policy,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_BINDING_MISMATCH


# --- Decision recomputation under the current policy. ---


@pytest.mark.parametrize(
    "revocation",
    [
        pytest.param(CONTENT_A, id="content-hash"),
        pytest.param("a" * 64, id="content-hash-bare"),
        pytest.param("source:local:packages/golden", id="source-exact"),
        pytest.param("source:local:packages/*", id="source-glob"),
        pytest.param("source:*example*", id="git-glob"),
        pytest.param("source:github.com", id="source-normalized-host"),
    ],
)
def test_revocation_recheck_refuses_for_every_revocation_kind(
    tmp_path: Path, revocation: str
) -> None:
    """Current revocations refuse, whatever the record claims.

    Production call site: ``source_audit.validate_source_audit`` via the one
    revocation matcher in ``audit.pipeline``.
    """
    record, csk_home, package, content, _ = _recorded(
        tmp_path, git="https://github.com/example/golden"
    )
    current = _policy(revocations=(revocation,))
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=current,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_DECISION_REFUSED


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param({"git": "http://[::1"}, id="git"),
        pytest.param({"source": "http://[::1"}, id="source"),
    ],
)
def test_malformed_stored_source_with_revocation_is_structured(
    tmp_path: Path, stored: dict[str, str]
) -> None:
    """A stored source the matcher cannot parse refuses typed (N5).

    The malformed string comes from the store, so the refusal names the
    malformed report rather than escaping as the matcher's raw ValueError.
    """
    _, csk_home, package, content, _ = _recorded(tmp_path, **stored)  # type: ignore[arg-type]
    revoking = _policy(revocations=["source:evil.example*"])
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_stored_report(
            package=package,
            content_sha256=content,
            csk_home=csk_home,
            policy=revoking,
        )
    assert excinfo.value.code == source_audit.CODE_REPORT_MALFORMED


def test_non_matching_revocation_admits(tmp_path: Path) -> None:
    record, csk_home, package, content, _ = _recorded(tmp_path)
    validated = source_audit.validate_source_audit(
        record,
        csk_home=csk_home,
        policy=_policy(),
        expected_package=package,
        expected_content_sha256=content,
    )
    assert validated.decision == Decision.ALLOW


def test_require_pin_unsatisfied_refuses(tmp_path: Path) -> None:
    strict = _policy(mode="strict")
    record, csk_home, package, content, _ = _recorded(
        tmp_path, decision=Decision.REQUIRE_PIN, schema_version=2, policy=strict
    )
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=strict,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_DECISION_REFUSED


def test_require_pin_satisfied_admits_where_authorized(tmp_path: Path) -> None:
    """A pin satisfies require_pin: the one outcome pins already authorize."""
    strict = _policy(mode="strict")
    record, csk_home, package, content, _ = _recorded(
        tmp_path, decision=Decision.REQUIRE_PIN, schema_version=2, policy=strict
    )
    audit_trust.pin_content_hash(csk_home, content, reason="test", pinned_by="test")
    validated = source_audit.validate_source_audit(
        record,
        csk_home=csk_home,
        policy=strict,
        expected_package=package,
        expected_content_sha256=content,
    )
    assert validated.decision == Decision.ALLOW


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"[]", id="list"),
        pytest.param(b'"pinned"', id="string"),
        pytest.param(b"1", id="int"),
        pytest.param(b"null", id="null"),
        pytest.param(b"\xff\xfe\x00bad", id="non-utf8"),
    ],
)
def test_non_object_trust_file_is_fail_closed_on_both_paths(
    tmp_path: Path, payload: bytes
) -> None:
    """A trust file that is not an object fails closed, never raw.

    The trust reader takes the same fail-closed branch malformed trust
    files take: no pin. (Unreadable trust refuses since rev4; malformed
    still means no pin because garbage never counts as a pin.) In advisory
    mode over an allow report both validators therefore admit exactly as
    with an absent trust file, and no raw ``AttributeError`` escapes
    either path.
    Production call sites: ``source_audit.validate_stored_report`` and
    ``source_audit.validate_source_audit`` via ``audit.trust.load_trust_record``.
    """
    record, csk_home, package, content, policy = _recorded(tmp_path)
    trust_file = audit_trust.trust_path(csk_home, content)
    trust_file.parent.mkdir(parents=True, exist_ok=True)
    trust_file.write_bytes(payload)
    stored = source_audit.validate_stored_report(
        package=package, content_sha256=content, csk_home=csk_home, policy=policy
    )
    assert stored.decision == Decision.ALLOW
    admitted = source_audit.validate_source_audit(
        record,
        csk_home=csk_home,
        policy=policy,
        expected_package=package,
        expected_content_sha256=content,
    )
    assert admitted.decision == Decision.ALLOW


def test_non_object_trust_file_through_plan_hook(tmp_path: Path) -> None:
    """The hook path fails a non-object trust file closed, never raw.

    Production call site: ``source_audit.source_audit_plan_hook`` through
    ``builds.planner.plan_builds(audit=...)``.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.txt").write_text("source", encoding="utf-8")
    manager_home = tmp_path / "manager"
    manager_home.mkdir()
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    policy = _policy()
    content = hashing.content_sha256(root)
    source_audit.record_source_audit(
        _audit_report(content, Decision.ALLOW),
        csk_home=csk_home,
        package=_package(),
        git=None,
        policy=policy,
        created_at=CREATED_AT,
    )
    trust_file = audit_trust.trust_path(csk_home, content)
    trust_file.parent.mkdir(parents=True, exist_ok=True)
    trust_file.write_bytes(b"[]")
    events: list[str] = []
    backend = _RecordingCache(manager_home, events)

    def establish(config: Any) -> _RecordingToolchainSession:
        events.append("toolchain")
        return _RecordingToolchainSession(events)

    def audit(providers: tuple[build_planner.BuildProvider, ...]) -> None:
        events.append("audit")
        source_audit.source_audit_plan_hook(providers, csk_home=csk_home, policy=policy)

    with build_source.freeze_snapshot(root) as frozen:
        build_planner.plan_builds(
            (_planning_provider(frozen, _package()),),
            manager_home=manager_home,
            operator_search_path=(),
            cache_backend=backend,  # type: ignore[arg-type]
            establish_toolchain=establish,  # type: ignore[arg-type]
            audit=audit,
        )
    assert events[0] == "audit"


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("warn", id="stored-warn"),
        pytest.param("require_pin", id="stored-require-pin"),
    ],
)
def test_stale_allow_over_blocking_findings_refuses(tmp_path: Path, stored: str) -> None:
    """Findings that block under the current policy refuse stale records."""
    findings = (_finding(),)
    if stored == "warn":
        record, csk_home, package, content, _ = _recorded(
            tmp_path, decision=Decision.WARN, findings=findings
        )
        current = _policy(mode="strict")
    else:
        strict = _policy(mode="strict")
        record, csk_home, package, content, _ = _recorded(
            tmp_path,
            decision=Decision.REQUIRE_PIN,
            findings=findings,
            schema_version=2,
            policy=strict,
        )
        audit_trust.pin_content_hash(csk_home, content, reason="test", pinned_by="test")
        current = strict
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=current,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_DECISION_REFUSED


def test_require_pin_record_needs_pin(tmp_path: Path) -> None:
    """A require_pin record without a pin never authorizes, in any mode."""
    for findings in ((), (_finding(severity=Severity.LOW, finding_id="test.low"),)):
        record, csk_home, package, content, _ = _recorded(
            tmp_path, decision=Decision.REQUIRE_PIN, findings=findings
        )
        with pytest.raises(SourceAuditError) as excinfo:
            source_audit.validate_source_audit(
                record,
                csk_home=csk_home,
                policy=_policy(),
                expected_package=package,
                expected_content_sha256=content,
            )
        assert excinfo.value.code == source_audit.CODE_DECISION_REFUSED


def test_strict_unpinned_schema2_refuses_allow_record(tmp_path: Path) -> None:
    strict = _policy(mode="strict")
    record, csk_home, package, content, _ = _recorded(
        tmp_path, decision=Decision.ALLOW, schema_version=2, policy=strict
    )
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=strict,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_DECISION_REFUSED


@pytest.mark.parametrize("pinned", [False, True], ids=["unpinned", "pinned"])
def test_block_record_never_authorizes(tmp_path: Path, pinned: bool) -> None:
    record, csk_home, package, content, policy = _recorded(
        tmp_path, decision=Decision.BLOCK, findings=(_finding(),)
    )
    if pinned:
        audit_trust.pin_content_hash(csk_home, content, reason="test", pinned_by="test")
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=policy,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_DECISION_REFUSED


@pytest.mark.parametrize("mode", ["advisory", "strict"])
def test_failing_canary_refuses_valid_binding(tmp_path: Path, mode: str) -> None:
    record, csk_home, package, content, _ = _recorded(tmp_path, policy=_policy(mode=mode))
    policy = _policy(mode=mode)
    with patch.object(audit_canary, "run_static_canary", return_value=False):
        with pytest.raises(SourceAuditError) as excinfo:
            source_audit.validate_source_audit(
                record,
                csk_home=csk_home,
                policy=policy,
                expected_package=package,
                expected_content_sha256=content,
            )
    assert excinfo.value.code == source_audit.CODE_CANARY_FAILED


# --- A pin is not a master key: one named test per gate. ---


@pytest.mark.parametrize("canary", ["static", "backend"])
def test_pin_does_not_satisfy_canary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, canary: str
) -> None:
    """A pin cannot satisfy the canary gate (production: gate_plans)."""
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    snapshot = _snapshot_dir(tmp_path)
    content = hashing.content_sha256(snapshot)
    audit_trust.pin_content_hash(csk_home, content, reason="test", pinned_by="test")
    config = _config(csk_home)
    plans = [_skill_plan(snapshot)]
    healthy = audit_pipeline.gate_plans(plans, config, scope="test", record=False)
    assert not healthy.blocked
    if canary == "static":
        monkeypatch.setattr(audit_canary, "run_static_canary", lambda: False)
    else:
        monkeypatch.setattr(NullBackend, "run_canary", lambda self: False)
    gated = audit_pipeline.gate_plans(plans, config, scope="test", record=False)
    assert gated.blocked
    assert "canary" in "; ".join(gated.errors)


@pytest.mark.parametrize("canary", ["static", "backend"])
def test_canary_failure_blocks_without_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, canary: str
) -> None:
    """Positive control: the canary gate exists without any pin."""
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    snapshot = _snapshot_dir(tmp_path)
    config = _config(csk_home)
    if canary == "static":
        monkeypatch.setattr(audit_canary, "run_static_canary", lambda: False)
    else:
        monkeypatch.setattr(NullBackend, "run_canary", lambda self: False)
    gated = audit_pipeline.gate_plans([_skill_plan(snapshot)], config, scope="test", record=False)
    assert gated.blocked
    assert "canary" in "; ".join(gated.errors)


def test_pin_does_not_satisfy_revocation(tmp_path: Path) -> None:
    """A pin cannot satisfy the revocation gate (production: gate_plans)."""
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    snapshot = _snapshot_dir(tmp_path)
    content = hashing.content_sha256(snapshot)
    audit_trust.pin_content_hash(csk_home, content, reason="test", pinned_by="test")
    clean = audit_pipeline.gate_plans(
        [_skill_plan(snapshot)], _config(csk_home), scope="test", record=False
    )
    assert not clean.blocked
    revoked = audit_pipeline.gate_plans(
        [_skill_plan(snapshot)],
        _config(csk_home, revocations=[content]),
        scope="test",
        record=False,
    )
    assert revoked.blocked
    assert "revoked" in "; ".join(revoked.errors)


def test_pin_does_not_satisfy_required_source_audit_gate(tmp_path: Path) -> None:
    """A pin cannot satisfy the required source-audit gate.

    Production call site: ``source_audit.validate_source_audit``. The
    content is pinned, yet a missing report and a mismatching report both
    refuse: the pin authorizes require_pin only, never evidence.
    """
    record, csk_home, package, content, policy = _recorded(tmp_path)
    audit_trust.pin_content_hash(csk_home, content, reason="test", pinned_by="test")
    source_audit.source_audit_report_path(csk_home, content).unlink()
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            record,
            csk_home=csk_home,
            policy=policy,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_REPORT_MISSING

    warn_record, csk_home, package, content, policy = _recorded(
        tmp_path,
        decision=Decision.WARN,
        findings=(_finding(severity=Severity.LOW, finding_id="test.low"),),
    )
    audit_trust.pin_content_hash(csk_home, content, reason="test", pinned_by="test")
    _tamper_report(csk_home, content)
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.validate_source_audit(
            warn_record,
            csk_home=csk_home,
            policy=policy,
            expected_package=package,
            expected_content_sha256=content,
        )
    assert excinfo.value.code == source_audit.CODE_EVIDENCE_MISMATCH


# --- Network-git registry evidence: exact match plus live backing. ---


def _registry_fixture() -> tuple[Any, RegistryConfig]:
    priv, pinned = _make_key()
    registry = RegistryConfig(
        name="trusted", url="https://registry.example/v1", public_keys=(pinned,)
    )
    return priv, registry


def _signed_live_record(priv: Any, **overrides: Any) -> dict[str, Any]:
    # The live record attests the claimed context by default: content and
    # context are one quantity, and the binding requires the evidence to name
    # the live record's content hash exactly.
    body = _record_body(
        name="golden",
        source_identity=SOURCE_IDENTITY,
        commit=COMMIT_HEX,
        content_sha256=CONTEXT_A,
    )
    body.update(overrides)
    return _sign_record(priv, body)


def _live_fetch(
    records: list[dict[str, Any]], *, stale_urls: set[str] | None = None
) -> Any:
    def fetch(url: str, source: str, commit: str, content: str) -> list[dict[str, Any]]:
        return records

    fetch.stale_urls = stale_urls if stale_urls is not None else set()  # type: ignore[attr-defined]
    return fetch


def _expectation(key_id: str) -> install_marker.AttestationExpectation:
    return install_marker.AttestationExpectation(
        name="golden",
        repository=REPO,
        commit=source_package.LockedCommit(object_format="sha1", hex=COMMIT_HEX),
        context_sha256=CONTEXT_A,
        key_id=key_id,
    )


def _write_evidence(tmp_path: Path, key_id: str, **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        "name": "golden",
        "repository": REPO,
        "commit": {"object_format": "sha1", "hex": COMMIT_HEX},
        "context_sha256": CONTEXT_A,
        "key_id": key_id,
    }
    payload.update(overrides)
    path = tmp_path / "evidence.json"
    path.write_bytes(json.dumps(payload).encode("utf-8"))
    return path


def test_network_git_evidence_admits_on_exact_match(tmp_path: Path) -> None:
    """Exact name, repository, commit and context admit with live backing.

    The live record attests the claimed context: content and context are one
    quantity, and the evidence must name the live record's content hash. An
    earlier revision of this fixture had the live record attest CONTENT_A
    while the evidence claimed CONTEXT_A, which certified the unbound fourth
    member instead of the binding.

    Production call site: ``source_audit.admit_network_git_evidence``.
    """
    priv, registry = _registry_fixture()
    live = _signed_live_record(priv)
    assert live["content_sha256"] == CONTEXT_A
    key_id = live["sig"]["key_id"]
    evidence_path = _write_evidence(tmp_path, key_id)
    admitted = source_audit.admit_network_git_evidence(
        evidence_path,
        _expectation(key_id),
        registries=(registry,),
        fetch=_live_fetch([live]),
    )
    assert admitted.evidence.name == "golden"
    assert admitted.evidence.repository == REPO
    assert admitted.evidence.context_sha256 == CONTEXT_A
    assert admitted.warnings == ()
    assert admitted.attestation.registry == "trusted"
    assert admitted.attestation.status == "audited"
    assert admitted.attestation.key_id == key_id


def test_network_git_evidence_carries_live_warnings(tmp_path: Path) -> None:
    priv, registry = _registry_fixture()
    live = _signed_live_record(priv)
    key_id = live["sig"]["key_id"]
    evidence_path = _write_evidence(tmp_path, key_id)
    admitted = source_audit.admit_network_git_evidence(
        evidence_path,
        _expectation(key_id),
        registries=(registry,),
        fetch=_live_fetch([live, {"bogus": True}]),
    )
    assert admitted.evidence.name == "golden"
    assert any("malformed" in warning for warning in admitted.warnings)


_EVIDENCE_DEFECTS = [
    "absent",
    "unreadable",
    "malformed",
    "stale",
    "revoked",
    "wrong-name",
    "wrong-repository",
    "wrong-commit",
    "wrong-context",
    "wrong-key",
]

_EXPECTED_EVIDENCE_CODES = {
    "absent": "attestation_evidence_missing",
    "unreadable": "attestation_evidence_unreadable",
    "malformed": "attestation_evidence_malformed",
    "stale": "attestation_evidence_stale",
    "revoked": "attestation_evidence_revoked",
    "wrong-name": "attestation_evidence_mismatch",
    "wrong-repository": "attestation_evidence_mismatch",
    "wrong-commit": "attestation_evidence_mismatch",
    "wrong-context": "attestation_evidence_mismatch",
    "wrong-key": "attestation_evidence_mismatch",
}

_WRONG_VALUE = {
    "wrong-name": {"name": "other"},
    "wrong-repository": {"repository": REPO_OTHER},
    "wrong-commit": {"commit": {"object_format": "sha1", "hex": COMMIT_OTHER}},
    "wrong-context": {"context_sha256": CONTEXT_B},
    "wrong-key": {"key_id": "ffffffffffffffff"},
}


@pytest.mark.parametrize("defect", _EVIDENCE_DEFECTS)
def test_network_git_evidence_defect_family_refuses(tmp_path: Path, defect: str) -> None:
    """The ten attestation-evidence defects refuse through the binding.

    This extends the sibling ``attestation-evidence-*`` drivers (which drive
    the validator directly) through ``admit_network_git_evidence``, where
    freshness and revocation come from a live signed registry instead of
    injected booleans.
    """
    priv, registry = _registry_fixture()
    status = "revoked" if defect == "revoked" else "audited"
    live = _signed_live_record(priv, status=status)
    key_id = live["sig"]["key_id"]
    stale = {registry.url} if defect == "stale" else set()
    evidence_path = tmp_path / "evidence.json"
    if defect == "malformed":
        evidence_path.write_bytes(b"{oops")
    elif defect != "absent":
        mutation = dict(_WRONG_VALUE.get(defect, {}))
        kid = mutation.pop("key_id", key_id)
        _write_evidence(tmp_path, kid, **mutation)
    if defect == "unreadable":
        original_read_bytes = Path.read_bytes

        def refusing_read_bytes(self: Path) -> bytes:
            if self == evidence_path:
                raise PermissionError("evidence store denied the read")
            return original_read_bytes(self)

        ctx: Any = patch.object(Path, "read_bytes", refusing_read_bytes)
    else:
        import contextlib

        ctx = contextlib.nullcontext()
    with ctx:
        with pytest.raises(install_marker.InstallMarkerError) as excinfo:
            source_audit.admit_network_git_evidence(
                evidence_path,
                _expectation(key_id),
                registries=(registry,),
                fetch=_live_fetch([live], stale_urls=stale),
            )
    assert excinfo.value.code == _EXPECTED_EVIDENCE_CODES[defect]


def test_network_git_evidence_without_live_attestation_refuses(tmp_path: Path) -> None:
    """Matching evidence without a live attestation is refused, not adopted."""
    priv, registry = _registry_fixture()
    key_id = _signed_live_record(priv)["sig"]["key_id"]
    evidence_path = _write_evidence(tmp_path, key_id)
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.admit_network_git_evidence(
            evidence_path,
            _expectation(key_id),
            registries=(registry,),
            fetch=_live_fetch([]),
        )
    assert excinfo.value.code == source_audit.CODE_REGISTRY_UNATTESTED


def test_network_git_evidence_excluded_registry_refuses(tmp_path: Path) -> None:
    """A snapshot-excluded registry cannot back required evidence."""
    priv, registry = _registry_fixture()
    live = _signed_live_record(priv)
    key_id = live["sig"]["key_id"]
    evidence_path = _write_evidence(tmp_path, key_id)
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.admit_network_git_evidence(
            evidence_path,
            _expectation(key_id),
            registries=(registry,),
            fetch=_live_fetch([live]),
            excluded_registry_urls=frozenset({registry.url}),
        )
    assert excinfo.value.code == source_audit.CODE_REGISTRY_UNATTESTED


@pytest.mark.parametrize(
    "member",
    [
        pytest.param("name", id="live-name"),
        pytest.param("repository", id="live-repository"),
        pytest.param("commit", id="live-commit"),
        pytest.param("context", id="live-context"),
        pytest.param("key", id="live-key"),
    ],
)
def test_live_registry_record_mismatch_family_refuses(tmp_path: Path, member: str) -> None:
    """Evidence matching the expectation but not the live record refuses (F1).

    The live registry record is the independent side: the evidence and the
    expectation agree with each other on the wrong member while the only
    live record says otherwise. Every member isolates: the live record
    attests the claimed value on all other members, so a mutant dropping
    exactly the named leg admits. Production call site:
    ``source_audit.admit_network_git_evidence``.
    """
    priv, registry = _registry_fixture()
    live = _signed_live_record(priv)
    live_key = live["sig"]["key_id"]
    bogus_key = "ffffffffffffffff"
    assert bogus_key != live_key
    evidence_kw: dict[str, Any] = {}
    if member == "name":
        evidence_kw = {"name": "other"}
        expectation = install_marker.AttestationExpectation(
            name="other",
            repository=REPO,
            commit=source_package.LockedCommit(object_format="sha1", hex=COMMIT_HEX),
            context_sha256=CONTEXT_A,
            key_id=live_key,
        )
    elif member == "repository":
        evidence_kw = {"repository": REPO_OTHER}
        expectation = install_marker.AttestationExpectation(
            name="golden",
            repository=REPO_OTHER,
            commit=source_package.LockedCommit(object_format="sha1", hex=COMMIT_HEX),
            context_sha256=CONTEXT_A,
            key_id=live_key,
        )
    elif member == "commit":
        evidence_kw = {"commit": {"object_format": "sha1", "hex": COMMIT_OTHER}}
        expectation = install_marker.AttestationExpectation(
            name="golden",
            repository=REPO,
            commit=source_package.LockedCommit(object_format="sha1", hex=COMMIT_OTHER),
            context_sha256=CONTEXT_A,
            key_id=live_key,
        )
    elif member == "context":
        evidence_kw = {"context_sha256": CONTEXT_B}
        expectation = install_marker.AttestationExpectation(
            name="golden",
            repository=REPO,
            commit=source_package.LockedCommit(object_format="sha1", hex=COMMIT_HEX),
            context_sha256=CONTEXT_B,
            key_id=live_key,
        )
    else:
        assert member == "key"
        expectation = _expectation(bogus_key)
        evidence_path = _write_evidence(tmp_path, bogus_key)
        with pytest.raises(SourceAuditError) as excinfo:
            source_audit.admit_network_git_evidence(
                evidence_path,
                expectation,
                registries=(registry,),
                fetch=_live_fetch([live]),
            )
        assert excinfo.value.code == source_audit.CODE_REGISTRY_UNATTESTED
        return
    evidence_path = _write_evidence(tmp_path, live_key, **evidence_kw)
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.admit_network_git_evidence(
            evidence_path,
            expectation,
            registries=(registry,),
            fetch=_live_fetch([live]),
        )
    assert excinfo.value.code == source_audit.CODE_REGISTRY_UNATTESTED


def test_live_record_with_key_refuses_keyless_evidence(tmp_path: Path) -> None:
    """Key absence on the evidence side does not match a signed live record."""
    priv, registry = _registry_fixture()
    live = _signed_live_record(priv)
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_bytes(
        json.dumps(
            {
                "name": "golden",
                "repository": REPO,
                "commit": {"object_format": "sha1", "hex": COMMIT_HEX},
                "context_sha256": CONTEXT_A,
            }
        ).encode("utf-8")
    )
    expectation = install_marker.AttestationExpectation(
        name="golden",
        repository=REPO,
        commit=source_package.LockedCommit(object_format="sha1", hex=COMMIT_HEX),
        context_sha256=CONTEXT_A,
        key_id=None,
    )
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.admit_network_git_evidence(
            evidence_path,
            expectation,
            registries=(registry,),
            fetch=_live_fetch([live]),
        )
    assert excinfo.value.code == source_audit.CODE_REGISTRY_UNATTESTED


def test_admit_derives_live_query_from_expectation(tmp_path: Path) -> None:
    """The live resolve queries the expected identity, including its context.

    There is no separate content parameter to point elsewhere: repository,
    commit and content all derive from the expectation.
    """
    priv, registry = _registry_fixture()
    live = _signed_live_record(priv)
    key_id = live["sig"]["key_id"]
    seen: dict[str, str] = {}

    def fetch(url: str, source: str, commit: str, content: str) -> list[dict[str, Any]]:
        seen["source_identity"] = source
        seen["commit"] = commit
        seen["content_sha256"] = content
        return [live]

    fetch.stale_urls = set()  # type: ignore[attr-defined]
    evidence_path = _write_evidence(tmp_path, key_id)
    source_audit.admit_network_git_evidence(
        evidence_path,
        _expectation(key_id),
        registries=(registry,),
        fetch=fetch,
    )
    assert seen == {
        "source_identity": REPO,
        "commit": COMMIT_HEX,
        "content_sha256": CONTEXT_A,
    }


def test_admit_refuses_non_digest_expectation_context(tmp_path: Path) -> None:
    """A live query is never built from a non-digest expectation context."""
    priv, registry = _registry_fixture()
    live = _signed_live_record(priv)
    key_id = live["sig"]["key_id"]
    evidence_path = _write_evidence(tmp_path, key_id)
    expectation = install_marker.AttestationExpectation(
        name="golden",
        repository=REPO,
        commit=source_package.LockedCommit(object_format="sha1", hex=COMMIT_HEX),
        context_sha256="not-a-digest",
        key_id=key_id,
    )
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.admit_network_git_evidence(
            evidence_path,
            expectation,
            registries=(registry,),
            fetch=_live_fetch([live]),
        )
    assert excinfo.value.code == source_audit.CODE_INVALID


@pytest.mark.parametrize(
    "repository,commit",
    [
        pytest.param("https://github.com/example/golden-skills", None, id="url-repository"),
        pytest.param("Golden-Skills", None, id="non-canonical-repository"),
        pytest.param(REPO, "not-a-commit", id="non-commit"),
    ],
)
def test_admit_refuses_non_canonical_expectation(
    tmp_path: Path, repository: str, commit: Any
) -> None:
    """A live query is never built from a non-canonical expectation."""
    priv, registry = _registry_fixture()
    live = _signed_live_record(priv)
    key_id = live["sig"]["key_id"]
    evidence_path = _write_evidence(tmp_path, key_id)
    expectation = install_marker.AttestationExpectation(
        name="golden",
        repository=repository,
        commit=(
            commit
            if commit is not None
            else source_package.LockedCommit(object_format="sha1", hex=COMMIT_HEX)
        ),
        context_sha256=CONTEXT_A,
        key_id=key_id,
    )
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.admit_network_git_evidence(
            evidence_path,
            expectation,
            registries=(registry,),
            fetch=_live_fetch([live]),
        )
    assert excinfo.value.code == source_audit.CODE_INVALID


def _fetch_by_url(records_by_url: dict[str, list[dict[str, Any]]], stale: set[str]) -> Any:
    def fetch(url: str, source: str, commit: str, content: str) -> list[dict[str, Any]]:
        return records_by_url.get(url, [])

    fetch.stale_urls = set(stale)  # type: ignore[attr-defined]
    return fetch


@pytest.mark.parametrize(
    "order",
    [
        pytest.param("attesting-first", id="attesting-first"),
        pytest.param("attesting-last", id="attesting-last"),
        pytest.param("unique-name", id="unique-name"),
    ],
)
def test_stale_attestation_under_name_collision_refuses(tmp_path: Path, order: str) -> None:
    """Freshness is deny-wins over every URL carrying the attestation name.

    Registry names are not unique in a loadable config, so the verdict must
    not depend on which same-named registry is listed last: a stale serving
    registry refuses whatever the order. Production call site:
    ``source_audit.admit_network_git_evidence``.
    """
    priv, registry = _registry_fixture()
    live = _signed_live_record(priv)
    key_id = live["sig"]["key_id"]
    attesting = RegistryConfig(
        name="trusted", url="https://one.example/v1", public_keys=registry.public_keys
    )
    other = RegistryConfig(
        name="trusted", url="https://two.example/v1", public_keys=registry.public_keys
    )
    fetch = _fetch_by_url({attesting.url: [live]}, stale={attesting.url})
    evidence_path = _write_evidence(tmp_path, key_id)
    if order == "attesting-first":
        registries = (attesting, other)
    elif order == "attesting-last":
        registries = (other, attesting)
    else:
        assert order == "unique-name"
        registries = (attesting,)
    with pytest.raises(install_marker.InstallMarkerError) as excinfo:
        source_audit.admit_network_git_evidence(
            evidence_path,
            _expectation(key_id),
            registries=registries,
            fetch=fetch,
        )
    assert excinfo.value.code == "attestation_evidence_stale"


def test_fresh_name_collision_admits(tmp_path: Path) -> None:
    """Deny-wins refuses only on stale data, never on the collision itself."""
    priv, registry = _registry_fixture()
    live = _signed_live_record(priv)
    key_id = live["sig"]["key_id"]
    attesting = RegistryConfig(
        name="trusted", url="https://one.example/v1", public_keys=registry.public_keys
    )
    other = RegistryConfig(
        name="trusted", url="https://two.example/v1", public_keys=registry.public_keys
    )
    fetch = _fetch_by_url({attesting.url: [live]}, stale=set())
    evidence_path = _write_evidence(tmp_path, key_id)
    admitted = source_audit.admit_network_git_evidence(
        evidence_path,
        _expectation(key_id),
        registries=(attesting, other),
        fetch=fetch,
    )
    assert admitted.evidence.name == "golden"
    assert admitted.attestation.registry == "trusted"


def test_deprecated_live_record_warns(tmp_path: Path) -> None:
    """A deprecated live attestation admits with a deprecation warning."""
    priv, registry = _registry_fixture()
    live = _signed_live_record(priv, status="deprecated")
    key_id = live["sig"]["key_id"]
    evidence_path = _write_evidence(tmp_path, key_id)
    admitted = source_audit.admit_network_git_evidence(
        evidence_path,
        _expectation(key_id),
        registries=(registry,),
        fetch=_live_fetch([live]),
    )
    assert admitted.evidence.name == "golden"
    assert any("deprecat" in warning for warning in admitted.warnings)
    assert admitted.attestation.status == "deprecated"


# --- Local content has no network identity. ---


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for entry in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = entry.relative_to(root).as_posix().encode("utf-8")
        if entry.is_symlink() or not entry.is_file():
            digest.update(b"S" + relative)
        else:
            digest.update(b"F" + relative + entry.read_bytes())
    return digest.hexdigest()


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param("00", id="digest-00"),
        pytest.param("01", id="digest-01"),
        pytest.param("ff", id="digest-ff"),
    ],
)
def test_local_network_attestation_requirement_refuses_without_publication(
    tmp_path: Path, tail: str
) -> None:
    """Local content fails where policy needs a network attestation.

    Production call site: ``install_marker.check_local_registry_requirement``.
    No audit-record-v1 is forged for local content: the gate raises and both
    trees stay byte-identical.
    """
    csk_home = tmp_path / "csk-home"
    project = tmp_path / "project"
    csk_home.mkdir()
    project.mkdir()
    (csk_home / "lock.json").write_text("{}")
    (project / "Skillfile.json").write_text("{}")
    before_home = _tree_hash(csk_home)
    before_project = _tree_hash(project)
    package = source_package.LocalSnapshot(snapshot="sha256:" + "a" * 62 + tail)
    with pytest.raises(install_marker.InstallMarkerError) as excinfo:
        install_marker.check_local_registry_requirement(
            package, network_attestation_required=True
        )
    assert excinfo.value.code == "local_registry_attestation_required"
    assert _tree_hash(csk_home) == before_home
    assert _tree_hash(project) == before_project


def test_network_package_passes_local_registry_requirement() -> None:
    package = source_package.NetworkGit(
        repository=REPO,
        commit=source_package.LockedCommit(object_format="sha1", hex=COMMIT_HEX),
    )
    install_marker.check_local_registry_requirement(package, network_attestation_required=True)


# --- Assurance bindings carry the exact receipt-3 build input digest. ---


def test_build_input_binds_exact_receipt_digest(tmp_path: Path) -> None:
    """The binder recomputes the exact receipt-3 input digest.

    Production call site: ``source_audit.bind_assurance_build_input`` over
    real pipeline receipt bytes.
    """
    _, _, result = _install_v3(tmp_path)
    assert result.receipt is not None
    bound = source_audit.bind_assurance_build_input(
        result.receipt, context_sha256=CONTEXT_A
    )
    assert bound == result.cache_key
    receipt = build_metadata.read_receipt_v3(result.receipt)
    assert bound == build_metadata.source_aware_cache_key(receipt.input)


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param("absent", id="absent"),
        pytest.param("malformed", id="malformed"),
        pytest.param("truncated", id="truncated"),
        pytest.param("wrong-schema", id="wrong-schema"),
    ],
)
def test_unbindable_build_input_refuses_without_fallback(
    tmp_path: Path, corrupt: str
) -> None:
    """An unbindable input rejects execution; the context hash never fills in."""
    from csk import protocol_json

    if corrupt == "absent":
        raw: bytes | None = None
    elif corrupt == "malformed":
        raw = b"{oops"
    elif corrupt == "truncated":
        _, _, result = _install_v3(tmp_path)
        assert result.receipt is not None
        raw = result.receipt[:-10]
    else:
        raw = protocol_json.canonical_bytes({"schema_version": 3})
    with pytest.raises(SourceAuditError) as excinfo:
        source_audit.bind_assurance_build_input(raw, context_sha256=CONTEXT_A)
    assert excinfo.value.code == source_audit.CODE_BUILD_INPUT_UNAVAILABLE
    assert CONTEXT_A in excinfo.value.detail


# --- Audit before cache and compiler for local packages (AC f). ---


def _hook(events: list[str], csk_home: Path, policy: SourceAuditPolicy) -> Any:
    def audit(providers: tuple[build_planner.BuildProvider, ...]) -> None:
        events.append("audit")
        source_audit.source_audit_plan_hook(providers, csk_home=csk_home, policy=policy)

    return audit


def test_local_audit_runs_before_cache_and_compiler(tmp_path: Path) -> None:
    """The source-audit hook orders before cache reads and toolchain probes.

    Reuses the counter instrument from TASK-260916-341a6q: the recording
    cache, the recording toolchain session and the shared events list.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.txt").write_text("source", encoding="utf-8")
    manager_home = tmp_path / "manager"
    manager_home.mkdir()
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    policy = _policy()
    package = _package()
    # The hook observes the content hash from the frozen tree while the
    # package stays the provider claim checked against the stored binding,
    # so the fixture records the real content hash of the planned tree.
    content = hashing.content_sha256(root)
    report = _audit_report(content, Decision.ALLOW)
    source_audit.record_source_audit(
        report,
        csk_home=csk_home,
        package=package,
        git=None,
        policy=policy,
        created_at=CREATED_AT,
    )
    events: list[str] = []
    backend = _RecordingCache(manager_home, events)

    def establish(config: Any) -> _RecordingToolchainSession:
        return _RecordingToolchainSession(events)

    with build_source.freeze_snapshot(root) as frozen:
        plans = build_planner.plan_builds(
            (_planning_provider(frozen, package),),
            manager_home=manager_home,
            operator_search_path=(),
            cache_backend=backend,  # type: ignore[arg-type]
            establish_toolchain=establish,  # type: ignore[arg-type]
            audit=_hook(events, csk_home, policy),
        )
    assert len(plans) == 1
    assert events == ["audit", "toolchain", "cache:golden-tool"]


def test_local_audit_refusal_reads_no_cache_and_runs_no_compiler(tmp_path: Path) -> None:
    """A refused audit performs zero cache reads and zero compiler probes."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.txt").write_text("source", encoding="utf-8")
    manager_home = tmp_path / "manager"
    manager_home.mkdir()
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    events: list[str] = []
    backend = _RecordingCache(manager_home, events)

    def establish(config: Any) -> _RecordingToolchainSession:
        events.append("toolchain")
        return _RecordingToolchainSession(events)

    with build_source.freeze_snapshot(root) as frozen:
        with pytest.raises(SourceAuditError) as excinfo:
            build_planner.plan_builds(
                (_planning_provider(frozen, _package()),),
                manager_home=manager_home,
                operator_search_path=(),
                cache_backend=backend,  # type: ignore[arg-type]
                establish_toolchain=establish,  # type: ignore[arg-type]
                audit=_hook(events, csk_home, _policy()),
            )
    assert excinfo.value.code == source_audit.CODE_REPORT_MISSING
    assert events == ["audit"]


def test_plan_hook_gates_every_provider(tmp_path: Path) -> None:
    """The hook validates each provider, not just the first one."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.txt").write_text("source", encoding="utf-8")
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    policy = _policy()
    package = _package()
    content = hashing.content_sha256(root)
    report = _audit_report(content, Decision.ALLOW)
    source_audit.record_source_audit(
        report,
        csk_home=csk_home,
        package=package,
        git=None,
        policy=policy,
        created_at=CREATED_AT,
    )
    with build_source.freeze_snapshot(root) as frozen:
        good = _planning_provider(frozen, package, name="first")
        bad_package = source_package.LocalSnapshot(snapshot=SNAPSHOT_B)
        bad = build_planner.BuildProvider(
            name="second",
            snapshot=frozen,
            commands=good.commands,
            build_roots=good.build_roots,
            runtime_roots=good.runtime_roots,
            package=bad_package,
        )
        with pytest.raises(SourceAuditError) as excinfo:
            source_audit.source_audit_plan_hook(
                (good, bad), csk_home=csk_home, policy=policy
            )
    # Same tree, so the store lookup by observed content finds the first
    # provider's report; the second provider's package claim then fails the
    # stored binding instead of missing the store.
    assert excinfo.value.code == source_audit.CODE_BINDING_MISMATCH


def test_plan_hook_skips_legacy_packageless_providers(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.txt").write_text("source", encoding="utf-8")
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    with build_source.freeze_snapshot(root) as frozen:
        provider = _planning_provider(frozen, None)
        source_audit.source_audit_plan_hook((provider,), csk_home=csk_home, policy=_policy())


def test_plan_hook_refuses_network_packages(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.txt").write_text("source", encoding="utf-8")
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    package = source_package.NetworkGit(
        repository=REPO,
        commit=source_package.LockedCommit(object_format="sha1", hex=COMMIT_HEX),
    )
    with build_source.freeze_snapshot(root) as frozen:
        provider = build_planner.BuildProvider(
            name="provider",
            snapshot=frozen,
            commands=(),
            package=package,  # type: ignore[arg-type]
        )
        with pytest.raises(SourceAuditError) as excinfo:
            source_audit.source_audit_plan_hook(
                (provider,), csk_home=csk_home, policy=_policy()
            )
    assert excinfo.value.code == source_audit.CODE_INVALID


def _real_tree_identities(tmp_path: Path) -> tuple[Path, str, str]:
    """Build one real tree with its real inventory digest and content hash.

    The capture below runs schema-2 source selection, which is
    POSIX-only (operational note 8): the guard lives here, on the
    one helper that needs selection, so the rest of the module
    keeps running where traversal is unavailable.
    """
    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    (root / "source.txt").write_text("source", encoding="utf-8")
    (root / "SKILL.md").write_text("# golden\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    captured = source_snapshot.capture_package_snapshot(root, ".", home=home)
    snapshot = captured.inventory["snapshot"]
    assert isinstance(snapshot, str)
    content = hashing.content_sha256(root)
    assert content != snapshot, "the two identities coincide; test is void"
    return root, content, snapshot


def test_hook_revocation_of_real_content_hash_refuses_through_plan_builds(
    tmp_path: Path,
) -> None:
    """A revocation of the real content hash refuses on the hook path (F2).

    One real tree, its real inventory digest as the package identity and its
    real section-8 content hash as the report key; the operator revokes the
    content hash csk audit prints. Production call site:
    ``builds.planner.plan_builds(audit=source_audit.source_audit_plan_hook)``.
    The refusal code proves the matcher ran on the content hash rather than
    missing the store on the inventory digest.
    """
    root, content, snapshot = _real_tree_identities(tmp_path)
    manager_home = tmp_path / "manager"
    manager_home.mkdir()
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    package = source_package.LocalSnapshot(snapshot=snapshot)
    policy = _policy()
    source_audit.record_source_audit(
        _audit_report(content, Decision.ALLOW),
        csk_home=csk_home,
        package=package,
        git=None,
        policy=policy,
        created_at=CREATED_AT,
    )
    revoking = _policy(revocations=[content])
    events: list[str] = []
    backend = _RecordingCache(manager_home, events)

    def establish(config: Any) -> _RecordingToolchainSession:
        return _RecordingToolchainSession(events)

    with build_source.freeze_snapshot(root) as frozen:
        with pytest.raises(SourceAuditError) as excinfo:
            build_planner.plan_builds(
                (_planning_provider(frozen, package),),
                manager_home=manager_home,
                operator_search_path=(),
                cache_backend=backend,  # type: ignore[arg-type]
                establish_toolchain=establish,  # type: ignore[arg-type]
                audit=_hook(events, csk_home, revoking),
            )
    assert excinfo.value.code == source_audit.CODE_DECISION_REFUSED
    assert events == ["audit"]


def test_hook_admits_pipeline_recorded_report_for_real_tree(tmp_path: Path) -> None:
    """A report recorded under the pipeline content hash admits (F2)."""
    root, content, snapshot = _real_tree_identities(tmp_path)
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    package = source_package.LocalSnapshot(snapshot=snapshot)
    policy = _policy()
    source_audit.record_source_audit(
        _audit_report(content, Decision.ALLOW),
        csk_home=csk_home,
        package=package,
        git=None,
        policy=policy,
        created_at=CREATED_AT,
    )
    with build_source.freeze_snapshot(root) as frozen:
        source_audit.source_audit_plan_hook(
            (_planning_provider(frozen, package),), csk_home=csk_home, policy=policy
        )


def test_hook_path_refuses_stale_label_and_canary_flag(tmp_path: Path) -> None:
    """The hook path refuses the stale-policy class, not just the record path (F3)."""
    root, content, snapshot = _real_tree_identities(tmp_path)
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    package = source_package.LocalSnapshot(snapshot=snapshot)
    policy = _policy()
    source_audit.record_source_audit(
        _audit_report(content, Decision.ALLOW),
        csk_home=csk_home,
        package=package,
        git=None,
        policy=policy,
        created_at=CREATED_AT,
    )
    with build_source.freeze_snapshot(root) as frozen:
        providers = (_planning_provider(frozen, package),)
        with pytest.raises(SourceAuditError) as stale:
            source_audit.source_audit_plan_hook(
                providers, csk_home=csk_home, policy=_policy(backend="openai")
            )
        assert stale.value.code == source_audit.CODE_POLICY_MISMATCH
        _rewrite_envelope(csk_home, content, lambda e: e.update(canary_passed=False))
        with pytest.raises(SourceAuditError) as canary:
            source_audit.source_audit_plan_hook(
                providers, csk_home=csk_home, policy=policy
            )
        assert canary.value.code == source_audit.CODE_CANARY_FAILED


# --- Identity instruments (S-IDENTITY) and store paths. ---


def test_evidence_digest_is_raw_bytes_hash(tmp_path: Path) -> None:
    """Identity is a function of the raw bytes (S-IDENTITY instrument)."""
    assert (
        source_audit.evidence_digest(b"abc")
        == "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    rng = random.Random(260916)
    for size in (0, 1, 63, 64, 65, 1024, 4096):
        for _ in range(8):
            blob = bytes(rng.randrange(256) for _ in range(size))
            assert source_audit.evidence_digest(blob) == "sha256:" + hashlib.sha256(blob).hexdigest()
    record, csk_home, _, content, _ = _recorded(tmp_path)
    stored = source_audit.source_audit_report_path(csk_home, content).read_bytes()
    assert record.evidence_sha256 == source_audit.evidence_digest(stored)


def test_report_path_derives_from_content_only(tmp_path: Path) -> None:
    csk_home = tmp_path / "csk-home"
    path = source_audit.source_audit_report_path(csk_home, CONTENT_A)
    assert path == csk_home / "audit" / "source-audit-v1" / ("sha256-" + "a" * 64) / "report.json"
    for bad in ("", "../escape", "sha256:xyz", "a" * 64, "sha256:" + "A" * 64):
        with pytest.raises(SourceAuditError) as excinfo:
            source_audit.source_audit_report_path(csk_home, bad)
        assert excinfo.value.code == source_audit.CODE_INVALID


# --- Trust-reader I/O surface (rev4, S-ERRORS): absent vs unreadable. ---

_TRUST_SEAM_ERRORS = [
    pytest.param(PermissionError(errno.EACCES, "denied"), id="eacces"),
    pytest.param(OSError(errno.EIO, "i/o error"), id="eio"),
    pytest.param(OSError(errno.ENAMETOOLONG, "name too long"), id="enametoolong"),
    pytest.param(IsADirectoryError(errno.EISDIR, "is a directory"), id="eisdir"),
    pytest.param(NotADirectoryError(errno.ENOTDIR, "not a directory"), id="enotdir"),
]


def test_absent_trust_record_is_no_pin_on_both_paths(tmp_path: Path) -> None:
    """Absent trust is the empty record; both validators admit over it.

    The absent side of the rev4 distinction: no trust file exists, the
    reader returns ``TrustRecord()``, and both validators admit an allow
    report in advisory mode exactly as before.
    """
    record, csk_home, package, content, policy = _recorded(tmp_path)
    assert not audit_trust.trust_path(csk_home, content).exists()
    assert audit_trust.load_trust_record(csk_home, content) == TrustRecord()
    stored = source_audit.validate_stored_report(
        package=package, content_sha256=content, csk_home=csk_home, policy=policy
    )
    assert stored.decision == Decision.ALLOW
    admitted = source_audit.validate_source_audit(
        record,
        csk_home=csk_home,
        policy=policy,
        expected_package=package,
        expected_content_sha256=content,
    )
    assert admitted.decision == Decision.ALLOW


@pytest.mark.parametrize("seam", [pytest.param("stat", id="stat"), pytest.param("read", id="read")])
@pytest.mark.parametrize("error", _TRUST_SEAM_ERRORS)
@pytest.mark.parametrize(
    "validator", [pytest.param("stored", id="stored"), pytest.param("record", id="record")]
)
def test_trust_reader_seam_failure_refuses_structured(
    tmp_path: Path, seam: str, error: OSError, validator: str
) -> None:
    """A trust-reader I/O failure is a typed refusal on both validators.

    The class test for the rev3 finding (stat seam escaped raw), rewritten
    for the TASK-260921-qe62bu survivor: ``load_trust_record`` attempts the
    read directly and maps only ``FileNotFoundError`` to absence, so there
    is no existence probe to fault. The ``read`` param keeps the original
    promise — fault injection at ``Path.read_text`` refuses structured,
    naming the trust path. The ``stat`` param pins the survivor's side of
    the decision: a faulted ``Path.exists`` is never consulted (no probe,
    no TOCTOU, nothing for ``exists()`` to swallow into a silent absence),
    so validation still succeeds and the reached-flag stays empty. The
    real paths (EACCES at open, EISDIR at read) are proved mock-free
    below; the same fixture validates unfaulted as the positive control.
    Production call sites: ``source_audit.validate_stored_report`` and
    ``source_audit.validate_source_audit`` via ``_load_trust_record``.
    """
    record, csk_home, package, content, policy = _recorded(tmp_path)
    target = audit_trust.trust_path(csk_home, content)
    # A present trust file, so the read seam is reached when the stat seam
    # is not faulted; a pin changes nothing over an allow report in advisory.
    audit_trust.pin_content_hash(csk_home, content, reason="test", pinned_by="test")
    reached: list[Path] = []

    def validate() -> None:
        if validator == "stored":
            source_audit.validate_stored_report(
                package=package,
                content_sha256=content,
                csk_home=csk_home,
                policy=policy,
            )
        else:
            source_audit.validate_source_audit(
                record,
                csk_home=csk_home,
                policy=policy,
                expected_package=package,
                expected_content_sha256=content,
            )

    if seam == "stat":
        original_exists = Path.exists

        def faulted_exists(self: Path) -> bool:
            if self == target:
                reached.append(self)
                raise error
            return original_exists(self)

        ctx: Any = patch.object(Path, "exists", faulted_exists)
    else:
        assert seam == "read"
        original_read_text = Path.read_text

        def faulted_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == target:
                reached.append(self)
                raise error
            return original_read_text(self, *args, **kwargs)

        ctx = patch.object(Path, "read_text", faulted_read_text)
    if seam == "stat":
        # Survivor contract (TASK-260921-qe62bu): no existence probe, so a
        # faulted Path.exists is never consulted and validation succeeds.
        with ctx:
            validate()
        assert reached == []
    else:
        with ctx:
            with pytest.raises(SourceAuditError) as excinfo:
                validate()
        assert excinfo.value.code == source_audit.CODE_TRUST_UNREADABLE
        assert str(target) in excinfo.value.detail
        assert reached == [target]
    # Positive control: the same fixture validates unfaulted.
    validate()


@pytest.mark.parametrize("seam", [pytest.param("stat", id="stat"), pytest.param("read", id="read")])
@pytest.mark.parametrize("error", _TRUST_SEAM_ERRORS)
def test_trust_reader_seam_failure_refuses_through_plan_hook(
    tmp_path: Path, seam: str, error: OSError
) -> None:
    """The hook path refuses a trust-reader I/O failure before any side effect.

    Same seam family as above, driven through the production entry point
    ``builds.planner.plan_builds(audit=source_audit.source_audit_plan_hook)``:
    the ``read`` param refuses with the trust code and the events list holds
    exactly ``["audit"]`` — no cache read, no toolchain. The ``stat`` param
    pins the TASK-260921-qe62bu survivor (no existence probe): a faulted
    ``Path.exists`` is never consulted, so the plan succeeds with the
    reached-flag empty and audit first. The unfaulted control runs the same
    plan through ``plan_builds`` with fresh instruments.
    """
    root, content, snapshot = _real_tree_identities(tmp_path)
    manager_home = tmp_path / "manager"
    manager_home.mkdir()
    csk_home = tmp_path / "csk-home"
    csk_home.mkdir()
    package = source_package.LocalSnapshot(snapshot=snapshot)
    policy = _policy()
    source_audit.record_source_audit(
        _audit_report(content, Decision.ALLOW),
        csk_home=csk_home,
        package=package,
        git=None,
        policy=policy,
        created_at=CREATED_AT,
    )
    target = audit_trust.trust_path(csk_home, content)
    # A present trust file, so the read seam is reached when the stat seam
    # is not faulted; a pin changes nothing over an allow report in advisory.
    audit_trust.pin_content_hash(csk_home, content, reason="test", pinned_by="test")
    reached: list[Path] = []

    def plan(events: list[str]) -> None:
        backend = _RecordingCache(manager_home, events)

        def establish(config: Any) -> _RecordingToolchainSession:
            return _RecordingToolchainSession(events)

        with build_source.freeze_snapshot(root) as frozen:
            build_planner.plan_builds(
                (_planning_provider(frozen, package),),
                manager_home=manager_home,
                operator_search_path=(),
                cache_backend=backend,  # type: ignore[arg-type]
                establish_toolchain=establish,  # type: ignore[arg-type]
                audit=_hook(events, csk_home, policy),
            )

    if seam == "stat":
        original_exists = Path.exists

        def faulted_exists(self: Path) -> bool:
            if self == target:
                reached.append(self)
                raise error
            return original_exists(self)

        ctx: Any = patch.object(Path, "exists", faulted_exists)
    else:
        assert seam == "read"
        original_read_text = Path.read_text

        def faulted_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == target:
                reached.append(self)
                raise error
            return original_read_text(self, *args, **kwargs)

        ctx = patch.object(Path, "read_text", faulted_read_text)
    events: list[str] = []
    if seam == "stat":
        # Survivor contract (TASK-260921-qe62bu): no existence probe, so a
        # faulted Path.exists is never consulted and the plan succeeds.
        with ctx:
            plan(events)
        assert reached == []
        assert events[0] == "audit"
    else:
        with ctx:
            with pytest.raises(SourceAuditError) as excinfo:
                plan(events)
        assert excinfo.value.code == source_audit.CODE_TRUST_UNREADABLE
        assert str(target) in excinfo.value.detail
        assert reached == [target]
        assert events == ["audit"]
    # Positive control: the same plan succeeds unfaulted, audit first.
    control_events: list[str] = []
    plan(control_events)
    assert control_events[0] == "audit"


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("unsearchable-dir", id="unsearchable-dir"),
        pytest.param("directory-at-file", id="directory-at-file"),
    ],
)
@pytest.mark.parametrize(
    "path", [pytest.param("stored", id="stored"), pytest.param("record", id="record"), pytest.param("hook", id="hook")]
)
def test_trust_reader_failure_refuses_mock_free(tmp_path: Path, shape: str, path: str) -> None:
    """Mock-free trust failures refuse typed on all three paths (rev3 repro).

    ``unsearchable-dir`` is the reviewer's reproduction: ``chmod 0`` on
    ``csk_home/audit/<hash>/`` with the report itself readable, so the
    stat seam raises EACCES for real. ``directory-at-file`` puts a
    directory where ``trust.json`` belongs, so the read seam raises
    EISDIR for real. Both refuse ``source_audit_trust_unreadable``
    naming the trust path; the hook records exactly ``["audit"]``.
    The chmod shape probes the live seam and skips with a named bound
    where mode 0 stays searchable (root/Windows) or the interpreter
    swallows the failure.
    """
    if path == "hook":
        root, content, snapshot = _real_tree_identities(tmp_path)
        manager_home = tmp_path / "manager"
        manager_home.mkdir()
        csk_home = tmp_path / "csk-home"
        csk_home.mkdir()
        package = source_package.LocalSnapshot(snapshot=snapshot)
        policy = _policy()
        report = _audit_report(content, Decision.ALLOW)
    else:
        record, csk_home, package, content, policy = _recorded(tmp_path)
        report = None
        root = None
        manager_home = None
    if report is not None:
        assert root is not None
        record = source_audit.record_source_audit(
            report,
            csk_home=csk_home,
            package=package,
            git=None,
            policy=policy,
            created_at=CREATED_AT,
        )
    target = audit_trust.trust_path(csk_home, content)
    # The report reads fine; only the trust store is broken.
    assert source_audit.source_audit_report_path(csk_home, content).is_file()
    events: list[str] = []

    def drive() -> None:
        if path == "stored":
            source_audit.validate_stored_report(
                package=package,
                content_sha256=content,
                csk_home=csk_home,
                policy=policy,
            )
        elif path == "record":
            source_audit.validate_source_audit(
                record,
                csk_home=csk_home,
                policy=policy,
                expected_package=package,
                expected_content_sha256=content,
            )
        else:
            assert root is not None and manager_home is not None
            backend = _RecordingCache(manager_home, events)

            def establish(config: Any) -> _RecordingToolchainSession:
                return _RecordingToolchainSession(events)

            with build_source.freeze_snapshot(root) as frozen:
                build_planner.plan_builds(
                    (_planning_provider(frozen, package),),
                    manager_home=manager_home,
                    operator_search_path=(),
                    cache_backend=backend,  # type: ignore[arg-type]
                    establish_toolchain=establish,  # type: ignore[arg-type]
                    audit=_hook(events, csk_home, policy),
                )

    if shape == "directory-at-file":
        target.mkdir(parents=True)
        with pytest.raises(SourceAuditError) as excinfo:
            drive()
    else:
        assert shape == "unsearchable-dir"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.parent.chmod(0)
        try:
            try:
                swallowed = target.exists()
            except OSError:
                swallowed = None
            if swallowed is not None:
                pytest.skip(
                    "unreadable-dir bound: mode 0 stays searchable here "
                    "(root/Windows) or exists() swallows the failure"
                )
            with pytest.raises(SourceAuditError) as excinfo:
                drive()
        finally:
            target.parent.chmod(0o755)
    assert excinfo.value.code == source_audit.CODE_TRUST_UNREADABLE
    assert str(target) in excinfo.value.detail
    if path == "hook":
        assert events == ["audit"]


# --- Legacy caller boundary (rev5, S-ERRORS): the third trust-reader caller. ---


def _legacy_audit_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    csk_home: Path,
    skills_root: Path,
) -> tuple[GlobalConfig, Path]:
    """A schema-1 project with a saved config, as the legacy audit sees it.

    A tagged skill, a project declaring it, and a config file selected
    through ``CSK_CONFIG`` — the same shape the audit CLI tests build.
    """
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
                "audit": {"enabled": True, "mode": "advisory", "fail_on": "high"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CSK_CONFIG", str(cfg_path))
    return load_config(cfg_path), project


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("directory-at-file", id="directory-at-file"),
        pytest.param("unreadable-file", id="unreadable-file"),
        pytest.param("unsearchable-dir", id="unsearchable-dir"),
    ],
)
@pytest.mark.parametrize(
    "caller",
    [
        pytest.param("cli-audit", id="cli-audit"),
        pytest.param("gate-advisory", id="gate-advisory"),
        pytest.param("gate-strict", id="gate-strict"),
    ],
)
def test_legacy_trust_seam_is_structured_on_every_caller(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    csk_home: Path,
    skills_root: Path,
    capsys: pytest.CaptureFixture[str],
    shape: str,
    caller: str,
) -> None:
    """The third trust-reader caller refuses structured on every shape (rev4 F-1).

    ``load_trust_record`` raises ``TrustRecordError``; its third production
    caller ``audit.pipeline.audit_plans`` reaches the user through ``csk
    audit`` and through ``gate_plans``. All three mock-free unreadable-trust
    shapes refuse structured on all three reaches: ``cli.main`` returns
    ``EXIT_CONFIG`` with the trust code on stderr and no laundered report on
    stdout (via the ``ValueError`` base, the ``SourceAuditError``
    convention), and ``gate_plans`` returns a blocking ``GateResult`` naming
    the code in errors in BOTH modes — never a warning, never "unpinned".
    The chmod shapes probe the live seam and skip with the named
    unreadable-trust bound where mode 0 does not bind. Production call
    sites: ``cli.main(["audit", ...])`` and ``audit.pipeline.gate_plans``.
    """
    cfg, project = _legacy_audit_fixture(monkeypatch, tmp_path, csk_home, skills_root)
    cli.main(["audit", "app", "--json"])
    content = str(json.loads(capsys.readouterr().out)["reports"][0]["content_sha256"])
    target = audit_trust.trust_path(csk_home, content)
    audit_trust.pin_content_hash(csk_home, content, reason="test", pinned_by="test")
    restore: list[tuple[Path, int]] = []
    if shape == "directory-at-file":
        target.unlink()
        target.mkdir()
    else:
        victim = target if shape == "unreadable-file" else target.parent
        victim.chmod(0)
        restore.append((victim, 0o600 if shape == "unreadable-file" else 0o700))
        try:
            if shape == "unreadable-file":
                victim.read_bytes()
            else:
                target.exists()
        except OSError:
            pass
        else:
            pytest.skip("unreadable-trust bound: mode 0 does not bind here (root/Windows)")
    try:
        if caller == "cli-audit":
            code = cli.main(["audit", "app", "--json"])
            captured = capsys.readouterr()
            assert code == cli.EXIT_CONFIG
            assert audit_trust.CODE_TRUST_UNREADABLE in captured.err
            assert captured.out == ""
        else:
            gated = replace(cfg, audit=replace(cfg.audit, enabled=True, mode="strict" if caller == "gate-strict" else "advisory"))
            loaded = manifest.load_manifest(project)
            assert loaded is not None
            with ExitStack() as stack:
                plans = installer._build_plans(gated, loaded, use_cache=False, stack=stack)
                result = audit_pipeline.gate_plans(plans, gated, scope="app", record=False)
            assert result.blocked
            assert any(audit_trust.CODE_TRUST_UNREADABLE in error for error in result.errors)
            assert result.warnings == ()
    finally:
        for victim, mode_bits in restore:
            victim.chmod(mode_bits)
