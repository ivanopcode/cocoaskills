"""Consume the schema-8 families the candidate protocol suite publishes.

Presence in a conformance root was never evidence that anything read it. The
released-pin lanes bind the rc.6 manager/build corpus, and until this module
existed no lane read a single schema-8 schema case or either schema-8
behavioural vector: a candidate suite could be checked out, authenticated,
digest-matched and reported green while the whole schema-8 surface sat unread.

This module is the consumer that closes it. Its reads are unguarded on purpose.
`.github/ci/candidate-artifacts.tsv` declares the artefacts they require, and
the partition that follows is derived from the root, never from the lane:

* a root that publishes the declared surface serves this consumer, and every
  published case decides a real cocoaskills behaviour here;
* a root that publishes none of it -- the released suite pin, for instance --
  defers this consumer, which skips at module level and names what was absent;
* the candidate lane additionally runs `candidate_consumption.py require`, and
  sets ``CSK_REQUIRE_FULL_CANDIDATE_ROOT``, so a *partly* published surface is
  a failure rather than a quieter run.

Every byte this module reads is authenticated against the candidate manifest
before it is parsed, so a case that was edited in the checkout fails on its
digest rather than on its content.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from candidate_consumption_support import load_candidate_consumption

from csk import install_marker, installer, skillcheck, skillspec
from csk.builds import go_v1, module_roots


CONSUMER = "tests/test_schema8_candidate_conformance.py"
ROOT_TEXT = os.environ.get("CURATOR_CONFORMANCE_ROOT")
REQUIRE_FULL_ROOT = bool(os.environ.get("CSK_REQUIRE_FULL_CANDIDATE_ROOT"))

MANIFEST_FAMILIES = {
    "agent-skill-v8.schema.json": "agent-skill.json",
    "csk-skill-v8.schema.json": "csk-skill.json",
}
MARKER_FAMILY = "install-marker-v4.schema.json"
MODULE_ROOT_VECTOR = "vectors/module-roots.json"
SCRIPT_POLICY_VECTOR = "vectors/script-host-execution-policy.json"

# Section classifications for the two behavioural families. Every top-level
# section a family publishes carries exactly one of these, and the
# classification is asserted against the file's real key set in both directions
# rather than trusted, so a section the protocol adds fails on its first run
# instead of being ignored by a reader that only names what it wants.
CONSUMED_HERE = "consumed by this module"
# The section describes worker behaviour only a manager implementing
# script-worker-v1 can exhibit. cocoaskills implements no script worker and
# refuses every enforced command before any install mutation, so no run of this
# build reaches it. test_a_refusal_precedes_every_worker_surface asserts the
# refusal that makes this true instead of leaving the section unread.
REFUSED_BEFORE_REACHED = (
    "unreachable: this manager refuses enforced script commands before any worker surface"
)
# Real surface this build does not carry. Named with its owner so the gap is
# declared rather than silent. The owner is the story that scopes the missing
# surface -- declared-only audit labeling for legacy schemas -- not whichever
# story happened to add this consumer; a gap parked on a story that never owned
# it and is about to close is a silent gap wearing a label.
DECLARED_GAP_PREFIX = "not implemented: "
DECLARED_GAP_OWNER = re.compile(r"\bSTORY-\d{6}-[0-9a-z]{6}\b")
NOT_IMPLEMENTED_YET = (
    "not implemented: script-command audit warning classes, owned by STORY-260822-2evh3p"
)

SCRIPT_POLICY_SECTIONS = {
    "schema_version": CONSUMED_HERE,
    "protocol_version": CONSUMED_HERE,
    "execution_policy": CONSUMED_HERE,
    "interpreters": CONSUMED_HERE,
    "opt_in_cases": CONSUMED_HERE,
    "audit_label_cases": NOT_IMPLEMENTED_YET,
    "capability_derivation_cases": REFUSED_BEFORE_REACHED,
    "capability_evidence_cases": REFUSED_BEFORE_REACHED,
    "capability_evidence_record": REFUSED_BEFORE_REACHED,
    "mandatory_controls": REFUSED_BEFORE_REACHED,
    "native_control_inventory": REFUSED_BEFORE_REACHED,
    "preflight_cases": REFUSED_BEFORE_REACHED,
}
MODULE_ROOT_SECTIONS = {
    "schema_version": CONSUMED_HERE,
    "protocol_version": CONSUMED_HERE,
    "evaluation_order": CONSUMED_HERE,
    "cases": CONSUMED_HERE,
}

# The manager's own fixed order, named the way the vector's own
# `fails_before` field names it.
GO_LIST_PHASE = "go-list"
GO_BUILD_PHASE = "go-build"


def _defer(reason: str) -> None:
    """Skip or fail, depending on whether this run requires a serving root."""
    if REQUIRE_FULL_ROOT:
        raise RuntimeError(
            f"this run requires a fully serving candidate root, but {reason}. "
            f"A lane that continued here would report success while a published "
            f"schema-8 family went unread."
        )
    pytest.skip(reason, allow_module_level=True)


if not ROOT_TEXT:
    _defer("CURATOR_CONFORMANCE_ROOT is not set")

_ROOT = Path(ROOT_TEXT) if ROOT_TEXT else Path()
_CONSUMPTION = load_candidate_consumption()
_LEDGER = _CONSUMPTION.load_artifact_ledger()
_DECLARED = next(row.artifacts for row in _LEDGER if row.consumer == CONSUMER)
try:
    _INVENTORY: dict[str, str] = dict(_CONSUMPTION.manifest_inventory(_ROOT))
except _CONSUMPTION.LedgerError as _error:
    # A root this consumer cannot even read its inventory from is not a schema-8
    # candidate. Say so and defer, unless the run requires a serving root.
    _defer(str(_error))
    raise
_MISSING = _CONSUMPTION.missing_artifacts(_DECLARED, _INVENTORY)
if _MISSING:
    _defer(f"this conformance root publishes no {', '.join(_MISSING)}")


def _authenticated_bytes(relative: str) -> bytes:
    """Read one published file only after its manifest digest agrees.

    Membership and bytes are checked together: a file the candidate manifest
    does not publish is not part of the candidate, and a published file whose
    bytes moved is a different file.
    """
    assert relative in _INVENTORY, f"candidate manifest publishes no {relative}"
    relative_path = PurePosixPath(relative)
    assert not relative_path.is_absolute() and ".." not in relative_path.parts
    path = _ROOT / relative_path
    assert path.is_file(), f"candidate suite publishes no file at {relative}"
    raw = path.read_bytes()
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    assert actual == _INVENTORY[relative], f"candidate digest mismatch for {relative}"
    return raw


def _json(relative: str) -> Any:
    return json.loads(_authenticated_bytes(relative))


@lru_cache(maxsize=1)
def _index() -> list[dict[str, Any]]:
    entries = _json("schema-cases/index.json")
    assert isinstance(entries, list) and entries
    return entries


def _family(schema: str) -> list[dict[str, Any]]:
    return [entry for entry in _index() if entry["schema"] == schema]


MANIFEST_CASES = [
    (schema, entry)
    for schema in sorted(MANIFEST_FAMILIES)
    for entry in _family(schema)
]
MARKER_CASES = _family(MARKER_FAMILY)
MODULE_ROOT_VECTORS = _json(MODULE_ROOT_VECTOR)
SCRIPT_POLICY_VECTORS = _json(SCRIPT_POLICY_VECTOR)


# --- presence --------------------------------------------------------------


def test_every_declared_schema8_artefact_is_published() -> None:
    """Decide the presence ledger inside the run that consumes it.

    The workflow gate answers the same question before pytest starts. Asking it
    again here means the evidence and the consumption travel together: a result
    stream that carries this case carries the proof that the root it ran
    against published the whole declared surface.
    """
    assert _DECLARED, f"{CONSUMER} declares no artefact in the presence ledger"
    assert _CONSUMPTION.missing_artifacts(_DECLARED, _INVENTORY) == ()
    for family in (*MANIFEST_FAMILIES, MARKER_FAMILY):
        assert _family(family), f"the candidate index publishes no {family} case"


# --- schema-8 manifest families -------------------------------------------


def test_both_manifest_families_are_consumed_in_full() -> None:
    """Both halves of the schema bump are read, and every case is collected."""
    for schema in MANIFEST_FAMILIES:
        published = _family(schema)
        assert published, f"the candidate index publishes no {schema} case"
        collected = [entry for name, entry in MANIFEST_CASES if name == schema]
        assert collected == published
        assert any(entry["valid"] for entry in published)
        assert any(not entry["valid"] for entry in published)


def _safe_relative(value: Any) -> str | None:
    """Return a case-declared path only when it is safe to materialize."""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("/") or "\\" in value or ":" in value:
        return None
    if ".." in PurePosixPath(value).parts:
        return None
    return value


def _materialize_manifest_case(
    snapshot: Path, manifest_name: str, raw: bytes, document: Any
) -> None:
    """Lay out everything the case names so it fails for its own rule.

    A case that is invalid for a missing directory would prove nothing about
    schema 8, so every path the manifest declares is created first.
    """
    if isinstance(document, dict):
        for root in document.get("build_roots") or []:
            relative = _safe_relative(root)
            if relative:
                (snapshot / relative).mkdir(parents=True, exist_ok=True)
                (snapshot / relative / "go.mod").write_text(
                    "module example.com/conformance\n", encoding="utf-8"
                )
        for root in document.get("runtime_roots") or []:
            relative = _safe_relative(root)
            if relative:
                (snapshot / relative).mkdir(parents=True, exist_ok=True)
        commands = document.get("commands")
        for command in (commands or {}).values() if isinstance(commands, dict) else ():
            if not isinstance(command, dict):
                continue
            source_dir = _safe_relative(command.get("source_dir"))
            if source_dir:
                (snapshot / source_dir).mkdir(parents=True, exist_ok=True)
                (snapshot / source_dir / "main.go").write_text(
                    "package main\nfunc main() {}\n", encoding="utf-8"
                )
            for module in command.get("modules") or []:
                relative = _safe_relative(module)
                if relative:
                    (snapshot / relative).mkdir(parents=True, exist_ok=True)
                    (snapshot / relative / "go.mod").write_text(
                        "module example.com/module\n", encoding="utf-8"
                    )
            for key in ("unix_path", "win_path"):
                relative = _safe_relative(command.get(key))
                if relative:
                    (snapshot / relative).parent.mkdir(parents=True, exist_ok=True)
                    (snapshot / relative).write_text("#!/bin/sh\n", encoding="utf-8")
    (snapshot / manifest_name).write_bytes(raw)


@pytest.mark.parametrize(
    ("schema", "entry"),
    MANIFEST_CASES,
    ids=[entry["instance"] for _, entry in MANIFEST_CASES],
)
def test_schema8_manifest_case(
    schema: str, entry: dict[str, Any], tmp_path: Path
) -> None:
    """Every published schema-8 manifest case decides acceptance here."""
    raw = _authenticated_bytes(f"schema-cases/{entry['instance']}")
    document = json.loads(raw)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    try:
        _materialize_manifest_case(snapshot, MANIFEST_FAMILIES[schema], raw, document)
    except OSError:
        # Some invalid cases name a path the host itself refuses -- a Windows
        # reserved device, for instance. The manifest still has to be rejected
        # for its own rule, and it is rejected before any of these paths is
        # consulted, so an unmaterializable INVALID case still decides here. An
        # unmaterializable VALID case is a real host problem and stays loud.
        if entry["valid"]:
            raise
        (snapshot / MANIFEST_FAMILIES[schema]).write_bytes(raw)

    if entry["valid"]:
        spec = skillspec.load_skill_spec(snapshot)
        assert spec.source_file == MANIFEST_FAMILIES[schema]
        assert spec.schema_version == 8
    else:
        with pytest.raises(skillspec.SkillSpecError):
            skillspec.load_skill_spec(snapshot)


# --- install marker v4 -----------------------------------------------------


def test_the_marker_v4_family_is_consumed_in_full() -> None:
    published = _family(MARKER_FAMILY)
    assert published, f"the candidate index publishes no {MARKER_FAMILY} case"
    assert MARKER_CASES == published
    assert any(entry["valid"] for entry in published)
    assert any(not entry["valid"] for entry in published)


@pytest.mark.parametrize(
    "entry", MARKER_CASES, ids=[entry["instance"] for entry in MARKER_CASES]
)
def test_marker_v4_case(entry: dict[str, Any]) -> None:
    """Every published marker-v4 case is read back through the real reader."""
    raw = _authenticated_bytes(f"schema-cases/{entry['instance']}")
    if not entry["valid"]:
        with pytest.raises(install_marker.InstallMarkerError):
            install_marker.read_install_marker(raw)
        return
    marker = install_marker.read_install_marker(raw)
    assert isinstance(marker, install_marker.InstallMarkerV4)
    assert marker.skill_schema_version == 8
    assert marker.to_json() == json.loads(raw)


# --- module roots ----------------------------------------------------------


def test_module_root_sections_are_all_classified() -> None:
    """The file this build reads is the file the suite publishes."""
    published = set(MODULE_ROOT_VECTORS)
    assert published, "the module-roots family published no sections"
    assert published == set(MODULE_ROOT_SECTIONS), (
        f"unclassified: {sorted(published - set(MODULE_ROOT_SECTIONS))}; "
        f"stale: {sorted(set(MODULE_ROOT_SECTIONS) - published)}"
    )
    assert MODULE_ROOT_VECTORS["cases"], "the module-roots family published no case"
    order = MODULE_ROOT_VECTORS["evaluation_order"]
    assert order == [
        "declaration-and-containment-before-go-list",
        "go-list-vendor-consistency",
        "directive-form-and-bijection-before-go-build",
    ], "the manager's fixed order is bound to this published order"
    phases = {
        case["fails_before"]
        for case in MODULE_ROOT_VECTORS["cases"]
        if case["fails_before"] is not None
    }
    assert phases <= {GO_LIST_PHASE, GO_BUILD_PHASE}


def _request(root: Path, case: dict[str, Any]) -> go_v1.BuildRequest:
    declaration = case["declaration"]
    snapshot = type("Snapshot", (), {"path": root})()
    return go_v1.BuildRequest(
        toolchain_session=None,
        source_snapshot=snapshot,
        command_object={
            "type": "build",
            "driver": "go-v1",
            "source_dir": declaration["build_root"],
        },
        build_root=declaration["build_root"],
        source_dir=declaration["build_root"],
        command="cli",
        modules=tuple(declaration["modules"]),
        build_roots=tuple(declaration["build_roots"]),
        runtime_roots=tuple(declaration["runtime_roots"]),
    )


def _materialize_module_root_case(root: Path, case: dict[str, Any]) -> None:
    snapshot = case["snapshot"]
    assert not snapshot["link_paths"], (
        "this case declares a link the fixture does not materialize; "
        "materialize it before consuming the case"
    )
    for relative in snapshot["directories"]:
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative in snapshot["go_mod_files"]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("module example.com/conformance\n\ngo 1.24\n", encoding="utf-8")
    vendor = root / case["declaration"]["build_root"] / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    (vendor / "modules.txt").write_text(
        "".join(f"{line}\n" for line in case["vendor_module_annotations"]),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "case",
    MODULE_ROOT_VECTORS["cases"],
    ids=[case["name"] for case in MODULE_ROOT_VECTORS["cases"]],
)
def test_module_root_case(case: dict[str, Any], tmp_path: Path) -> None:
    """Drive one published case to the driver's own declared failure boundary.

    The case is not merely required to produce the right diagnostic: it must
    produce it in the right phase. A manager that rejected everything before
    `go list` would answer every code correctly and still violate section
    4.2.3's failure boundary.
    """
    root = tmp_path / "snapshot"
    root.mkdir()
    _materialize_module_root_case(root, case)
    request = _request(root, case)

    phase: str | None = None
    try:
        phase = GO_LIST_PHASE
        go_v1._validate_declared_module_roots(request, root)
        phase = GO_BUILD_PHASE
        admitted = go_v1._resolve_module_root_bijection(
            request, root / case["declaration"]["build_root"]
        )
    except go_v1.GoV1Error as error:
        assert case["expected_error"] is not None, f"unexpected rejection: {error}"
        assert error.code == case["expected_error"]
        assert phase == case["fails_before"]
        return

    assert case["expected_error"] is None
    assert case["fails_before"] is None
    assert case["build_permitted"] is True
    # The admitted set names module paths, not directories, so the bijection is
    # checked in the direction the declaration makes a claim about: every
    # declared directory is named by exactly one admitted replacement.
    declared = set(case["declaration"]["modules"])
    replacements = module_roots.parse_effective_replacements(
        module_roots.read_vendor_modules_text(root / case["declaration"]["build_root"])
    )
    expected = {
        replacement.module_path
        for replacement in replacements
        if posixpath.normpath(
            posixpath.join(case["declaration"]["build_root"], replacement.target)
        )
        in declared
    }
    assert set(admitted) == expected
    assert len(admitted) == len(declared)


# --- script execution policy ----------------------------------------------


def test_script_policy_sections_are_all_classified() -> None:
    """Every published section is classified, consumed or declared unreachable."""
    published = set(SCRIPT_POLICY_VECTORS)
    assert published, "the script-host-execution-policy family published no sections"
    unclassified = sorted(published - set(SCRIPT_POLICY_SECTIONS))
    stale = sorted(set(SCRIPT_POLICY_SECTIONS) - published)
    assert not unclassified, (
        f"the root publishes sections this build does not classify: {unclassified}. "
        f"Classify each one and consume it, or record why it is unreachable."
    )
    assert not stale, (
        f"this build classifies sections the root no longer publishes: {stale}. "
        f"A classification that names nothing proves nothing."
    )
    assert any(
        reason == CONSUMED_HERE for reason in SCRIPT_POLICY_SECTIONS.values()
    )
    unowned = sorted(
        {
            reason
            for reason in SCRIPT_POLICY_SECTIONS.values()
            if reason.startswith(DECLARED_GAP_PREFIX)
            and not DECLARED_GAP_OWNER.search(reason)
        }
    )
    assert not unowned, (
        f"these classifications declare a gap without naming the story that owns "
        f"the missing surface: {unowned}. An unowned gap is never scheduled, so "
        f"it is a silent gap with extra ceremony."
    )


def test_script_policy_identity_matches_the_suite() -> None:
    """Bind the two closed identities this build hard-codes to the suite's bytes."""
    assert SCRIPT_POLICY_VECTORS["execution_policy"] == skillspec.SCRIPT_WORKER_V1_POLICY
    published = sorted(SCRIPT_POLICY_VECTORS["interpreters"])
    assert published, "the suite published no interpreter identity"
    assert published == sorted(skillspec.SCRIPT_INTERPRETERS)
    assert skillspec.SCRIPT_EXECUTION_POLICIES == frozenset(
        {SCRIPT_POLICY_VECTORS["execution_policy"]}
    )
    # This manager implements no script worker, which is what makes every
    # worker-side section of this family unreachable.
    assert skillspec.SCRIPT_EXECUTION_POLICIES_IMPLEMENTED == frozenset()


def _script_manifest(
    snapshot: Path, schema: int, command: dict[str, Any]
) -> Path:
    """Write a one-command script skill at the requested manifest schema."""
    (snapshot / "scripts").mkdir(parents=True, exist_ok=True)
    (snapshot / "scripts" / "tool").write_text("#!/bin/sh\n", encoding="utf-8")
    (snapshot / "SKILL.md").write_text("---\nname: tool\n---\n\n# Tool\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "schema_version": schema,
        "runtime_roots": [],
        "commands": {"tool": {"type": "script", "unix_path": "scripts/tool", **command}},
    }
    if schema >= 3:
        payload["capabilities"] = {}
    (snapshot / "agent-skill.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    return snapshot


def _refuses_publication(spec: skillspec.SkillSpec, tmp_path: Path) -> str:
    """Reach the single shim publication point and require it to refuse."""
    plan = installer.SkillPlan(
        decl=type("Decl", (), {"name": "skill-tool", "git": None, "source": "skill-tool"})(),
        resolved=type("Resolved", (), {"kind": "tag", "ref": "v1", "commit": "0" * 40})(),
        repo=tmp_path,
        snapshot=tmp_path,
        spec=spec,
    )
    bin_dir = tmp_path / "bin"
    with pytest.raises(installer.InstallError) as raised:
        installer.install_runtime_commands(tmp_path / "home", bin_dir, plan)
    assert not bin_dir.exists(), "a refused command still published a shim"
    return str(raised.value)


@pytest.mark.parametrize(
    "case",
    SCRIPT_POLICY_VECTORS["opt_in_cases"],
    ids=[case["name"] for case in SCRIPT_POLICY_VECTORS["opt_in_cases"]],
)
def test_script_policy_opt_in_case(case: dict[str, Any], tmp_path: Path) -> None:
    """Every published opt-in case decides the two questions this manager owns.

    Whether the manifest parses at all, and -- when it does -- whether the
    command is enforced or declared-only. An enforced command must additionally
    be refused with the closed section 4.1.1 diagnostic, because this manager
    has no worker and must not downgrade the command to a declared-only shim.
    """
    declared: dict[str, Any] = {}
    for field in ("execution_policy", "interpreter"):
        if case.get(field) is not None:
            declared[field] = case[field]
    snapshot = _script_manifest(tmp_path / "snapshot", case["manifest_schema"], declared)

    if not case["accepted"]:
        assert case.get("mode") is None, "the suite records a mode for a rejected case"
        with pytest.raises(skillspec.SkillSpecError):
            skillspec.load_skill_spec(snapshot)
        return

    spec = skillspec.load_skill_spec(snapshot)
    assert spec.schema_version == case["manifest_schema"]
    command = spec.commands["tool"]
    mode = case["mode"]
    assert mode is not None, "the suite accepts this case but records no mode"
    enforced = skillspec.enforced_script_commands(spec)

    if mode != "enforced":
        assert enforced == ()
        assert skillspec.script_execution_policy_rejection(spec) is None
        assert command.execution_policy is None
        return

    assert command.execution_policy == SCRIPT_POLICY_VECTORS["execution_policy"]
    assert command.interpreter in set(SCRIPT_POLICY_VECTORS["interpreters"])
    assert enforced == (command,)
    rejection = skillspec.script_execution_policy_rejection(spec)
    assert rejection is not None
    assert skillspec.SCRIPT_EXECUTION_POLICY_UNSUPPORTED in rejection
    assert skillspec.SCRIPT_EXECUTION_POLICY_UNSUPPORTED in _refuses_publication(
        spec, tmp_path / "install"
    )


@pytest.mark.parametrize("interpreter", sorted(SCRIPT_POLICY_VECTORS["interpreters"]))
def test_a_refusal_precedes_every_worker_surface(
    interpreter: str, tmp_path: Path
) -> None:
    """The assertion the ``refused before reached`` classifications rest on.

    Every enforced shape the suite itself declares -- the closed policy identity
    against each published interpreter -- is refused before any install
    mutation, so no control probe, capability derivation, evidence record or
    preflight this family publishes is reachable from this build. When a worker
    lands, this is the test that has to change first.
    """
    snapshot = _script_manifest(
        tmp_path / "snapshot",
        8,
        {
            "execution_policy": SCRIPT_POLICY_VECTORS["execution_policy"],
            "interpreter": interpreter,
        },
    )
    spec = skillspec.load_skill_spec(snapshot)
    assert skillspec.enforced_script_commands(spec) == (spec.commands["tool"],)
    assert skillspec.SCRIPT_EXECUTION_POLICY_UNSUPPORTED in _refuses_publication(
        spec, tmp_path / "install"
    )
    issues = skillcheck.validate_skill(snapshot)
    assert skillcheck.has_errors(issues)
    reported = [
        issue
        for issue in issues
        if issue.code == "skill.script_execution_policy_unsupported"
    ]
    assert len(reported) == 1 and reported[0].severity == "error"
