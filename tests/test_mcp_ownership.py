from __future__ import annotations

import hashlib
import os
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from csk import locking, mcp_ownership, protocol_json
from csk.mcp_ownership import (
    Action,
    ConsentSubject,
    Desired,
    EntryAddress,
    Ledger,
    LedgerAbsent,
    LedgerEntry,
    LedgerUnknown,
    McpOwnershipError,
    Observed,
    ReconcilePlan,
    Refusal,
    consent_key,
    consent_path,
    consumer_id,
    encode_consent,
    encode_ledger,
    ledger_path,
    load_consent,
    load_ledger,
    next_ledger,
    parse_consent,
    parse_ledger,
    reconcile,
    record_consent,
    unapproved,
)

CONSUMER = consumer_id("/work/project", "project")
OTHER_CONSUMER = consumer_id("/work/other", "project")
POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX file modes do not apply on Windows; the manager home gets its "
    "private state from locking.provision_new_manager_home",
)


def sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def addr(
    name: str = "docs",
    agent: str = "claude_code",
    relpath: str = ".mcp.json",
    container: str = "mcpServers",
) -> EntryAddress:
    return EntryAddress(agent=agent, relpath=relpath, container=container, name=name)


def want(address: EntryAddress, entry: str = "new", capability: str = "cap-1") -> Desired:
    return Desired(
        address=address,
        entry_sha256=sha(entry),
        declaration_sha256=sha("decl-" + entry),
        capability=capability,
    )


def owned(
    address: EntryAddress,
    written: str = "old",
    *,
    capability: str = "cap-1",
    transaction_id: str = "tx-old",
    created_file: bool = False,
) -> LedgerEntry:
    return LedgerEntry(
        address=address,
        written_sha256=sha(written),
        declaration_sha256=sha("decl-" + written),
        capability=capability,
        transaction_id=transaction_id,
        created_file=created_file,
    )


def ledger_of(*entries: LedgerEntry) -> Ledger:
    return Ledger(consumer=CONSUMER, scope="project", entries=entries)


def seen(address: EntryAddress, state: str, digest: str | None = None) -> Observed:
    return Observed(address=address, state=state, entry_sha256=sha(digest) if digest else None)  # type: ignore[arg-type]


# --- reconcile truth table ---------------------------------------------

A = addr()

# (id, desired entry label or None, owned written label or None, observed state,
#  observed digest label, expected action kind or None, expected refusal code or None,
#  expects drift warning)
TRUTH_ROWS: list[tuple[str, str | None, str | None, str, str | None, str | None, str | None, bool]] = [
    # desired, not owned
    ("d-absent-unowned", "new", None, "absent", None, "add", None, False),
    ("d-surface-absent-unowned", "new", None, "surface_absent", None, "add", None, False),
    ("d-present-unowned-different", "new", None, "present", "other", None, "mcp_entry_conflict", False),
    ("d-present-unowned-equal", "new", None, "present", "new", None, "mcp_entry_conflict", False),
    ("d-present-unowned-unrepresentable", "new", None, "present", None, None, "mcp_entry_conflict", False),
    ("d-unreadable-unowned", "new", None, "unreadable", None, None, "mcp_native_config_unreadable", False),
    ("d-malformed-unowned", "new", None, "malformed", None, None, "mcp_native_config_unreadable", False),
    # desired, owned
    ("d-absent-owned", "new", "old", "absent", None, "restore", None, False),
    ("d-surface-absent-owned", "new", "old", "surface_absent", None, "restore", None, False),
    ("d-present-owned-same-desired-same", "old", "old", "present", "old", "keep", None, False),
    ("d-present-owned-same-desired-new", "new", "old", "present", "old", "replace", None, False),
    ("d-present-owned-drift", "new", "old", "present", "edited", "skip_drift", None, True),
    ("d-present-owned-drift-equals-desired", "new", "old", "present", "new", "skip_drift", None, True),
    ("d-present-owned-drift-unrepresentable", "new", "old", "present", None, "skip_drift", None, True),
    ("d-unreadable-owned", "new", "old", "unreadable", None, None, "mcp_native_config_unreadable", False),
    ("d-malformed-owned", "new", "old", "malformed", None, None, "mcp_native_config_unreadable", False),
    # owned, no longer desired
    ("u-present-unchanged", None, "old", "present", "old", "remove", None, False),
    ("u-present-edited", None, "old", "present", "edited", "release", None, True),
    ("u-present-unrepresentable", None, "old", "present", None, "release", None, True),
    ("u-absent", None, "old", "absent", None, "forget", None, False),
    ("u-surface-absent", None, "old", "surface_absent", None, "forget", None, False),
    ("u-unreadable", None, "old", "unreadable", None, None, "mcp_native_config_unreadable", False),
    ("u-malformed", None, "old", "malformed", None, None, "mcp_native_config_unreadable", False),
]


@pytest.mark.parametrize(
    "row", TRUTH_ROWS, ids=[row[0] for row in TRUTH_ROWS]
)
def test_reconcile_truth_table(
    row: tuple[str, str | None, str | None, str, str | None, str | None, str | None, bool],
) -> None:
    _, desired_label, owned_label, state, digest, kind, refusal, drift = row
    desired = [want(A, desired_label)] if desired_label else []
    ledger: Ledger | LedgerAbsent = ledger_of(owned(A, owned_label)) if owned_label else LedgerAbsent()
    plan = reconcile(desired, {A: seen(A, state, digest)}, ledger)

    assert tuple(action.kind for action in plan.actions) == ((kind,) if kind else ())
    assert all(action.address == A for action in plan.actions)
    assert tuple(item.code for item in plan.refusals) == ((refusal,) if refusal else ())
    assert tuple(item.code for item in plan.warnings) == (("mcp_entry_drift",) if drift else ())
    assert all(item.address == A for item in (*plan.refusals, *plan.warnings))
    assert plan.executable is (refusal is None)


def test_reconcile_unowned_address_in_a_nonempty_ledger_is_still_unowned() -> None:
    other = addr("other")
    plan = reconcile(
        [want(A)], {A: seen(A, "present", "new"), other: seen(other, "present", "old")},
        ledger_of(owned(other)),
    )
    assert [r.code for r in plan.refusals] == ["mcp_entry_conflict"]
    assert [(a.kind, a.address) for a in plan.actions] == [("remove", other)]


def test_reconcile_same_name_in_a_different_file_or_agent_is_a_different_address() -> None:
    elsewhere = addr(agent="cursor", relpath=".cursor/mcp.json")
    plan = reconcile(
        [want(A)], {A: seen(A, "absent"), elsewhere: seen(elsewhere, "present", "old")},
        ledger_of(owned(elsewhere)),
    )
    assert sorted((a.kind, a.address.agent) for a in plan.actions) == [
        ("add", "claude_code"),
        ("remove", "cursor"),
    ]


def test_reconcile_ledger_absent_owns_nothing() -> None:
    plan = reconcile([want(A)], {A: seen(A, "present", "new")}, LedgerAbsent())
    assert plan.actions == ()
    assert [r.code for r in plan.refusals] == ["mcp_entry_conflict"]


def test_reconcile_empty_everything_is_an_empty_executable_plan() -> None:
    plan = reconcile([], {}, LedgerAbsent())
    assert plan == ReconcilePlan(actions=(), refusals=(), warnings=())
    assert plan.executable


def test_reconcile_unknown_ledger_refuses_every_desired_address_and_plans_no_action() -> None:
    b = addr("b")
    desired = [want(A), want(b)]
    observed = {A: seen(A, "absent"), b: seen(b, "present", "old")}
    plan = reconcile(desired, observed, LedgerUnknown(reason="unreadable"))
    assert plan.actions == ()
    assert plan.warnings == ()
    assert [(r.code, r.address) for r in plan.refusals] == [
        ("mcp_ownership_unknown", b),  # sorted by name: "b" < "docs"
        ("mcp_ownership_unknown", A),
    ]
    assert not plan.executable


def test_reconcile_unknown_ledger_with_nothing_desired_has_nothing_to_refuse() -> None:
    plan = reconcile([], {}, LedgerUnknown(reason="unreadable"))
    assert plan == ReconcilePlan(actions=(), refusals=(), warnings=())


def test_reconcile_missing_observation_is_a_programming_error() -> None:
    with pytest.raises(KeyError):
        reconcile([want(A)], {}, LedgerAbsent())
    with pytest.raises(KeyError):
        reconcile([], {}, ledger_of(owned(A)))


def test_reconcile_inconsistent_observations_raise() -> None:
    with pytest.raises(ValueError):
        reconcile([want(A)], {A: Observed(A, "absent", sha("x"))}, LedgerAbsent())
    with pytest.raises(ValueError):
        reconcile([want(A)], {A: seen(addr("other"), "absent")}, LedgerAbsent())
    with pytest.raises(McpOwnershipError):
        reconcile([want(A), want(A, "again")], {A: seen(A, "absent")}, LedgerAbsent())


def test_reconcile_orders_by_agent_relpath_name_regardless_of_input_order() -> None:
    addresses = [
        addr("z", "cursor", ".cursor/mcp.json"),
        addr("b", "claude_code", ".mcp.json"),
        addr("a", "claude_code", ".mcp.json"),
        addr("a", "codex_cli", ".codex/config.toml", "mcp_servers"),
        addr("a", "claude_code", ".claude/other.json"),
    ]
    observed = {a: seen(a, "absent") for a in addresses}
    forward = reconcile([want(a) for a in addresses], observed, LedgerAbsent())
    backward = reconcile([want(a) for a in reversed(addresses)], observed, LedgerAbsent())
    expected = sorted(addresses, key=lambda a: (a.agent, a.relpath, a.name))
    assert [a.address for a in forward.actions] == expected
    assert forward == backward


def test_reconcile_mixed_plan_reports_actions_refusals_and_warnings_together() -> None:
    to_add, conflict, drifted, gone = addr("add"), addr("conflict"), addr("drift"), addr("gone")
    plan = reconcile(
        [want(to_add), want(conflict), want(drifted)],
        {
            to_add: seen(to_add, "absent"),
            conflict: seen(conflict, "present", "foreign"),
            drifted: seen(drifted, "present", "edited"),
            gone: seen(gone, "present", "old"),
        },
        ledger_of(owned(drifted), owned(gone)),
    )
    assert sorted((a.kind, a.address.name) for a in plan.actions) == [
        ("add", "add"),
        ("remove", "gone"),
        ("skip_drift", "drift"),
    ]
    assert [(r.code, r.address.name) for r in plan.refusals] == [("mcp_entry_conflict", "conflict")]
    assert [(w.code, w.address.name) for w in plan.warnings] == [("mcp_entry_drift", "drift")]
    # Caller contract: a plan with any refusal performs no action and no ledger update.
    assert not plan.executable
    with pytest.raises(McpOwnershipError) as caught:
        next_ledger(CONSUMER, "project", ledger_of(owned(drifted), owned(gone)), plan, [], "tx-1", [])
    assert caught.value.code == "mcp_entry_conflict"


@pytest.mark.parametrize("code", ["mcp_entry_conflict", "mcp_native_config_unreadable", "mcp_ownership_unknown"])
def test_plan_is_not_executable_for_any_refusal_code(code: str) -> None:
    plan = ReconcilePlan(actions=(Action("add", A),), refusals=(Refusal(code, A, "x"),), warnings=())
    assert not plan.executable
    with pytest.raises(McpOwnershipError) as caught:
        next_ledger(CONSUMER, "project", LedgerAbsent(), plan, [want(A)], "tx-1", [])
    assert caught.value.code == code


def test_reconcile_capability_difference_is_decided_by_digests_only() -> None:
    same = reconcile(
        [want(A, "old", "cap-2")], {A: seen(A, "present", "old")}, ledger_of(owned(A, capability="cap-1"))
    )
    assert [a.kind for a in same.actions] == ["keep"]
    changed = reconcile(
        [want(A, "new", "cap-2")], {A: seen(A, "present", "old")}, ledger_of(owned(A, capability="cap-1"))
    )
    assert [a.kind for a in changed.actions] == ["replace"]
    edited = reconcile(
        [want(A, "new", "cap-2")], {A: seen(A, "present", "edited")}, ledger_of(owned(A, capability="cap-1"))
    )
    assert [a.kind for a in edited.actions] == ["skip_drift"]


# --- next_ledger -------------------------------------------------------

OLD_CREATED = owned(A, "old", created_file=True)
OLD_PLAIN = owned(A, "old", created_file=False)


def _entry(kind_desired: Desired, tx: str, created: bool) -> LedgerEntry:
    return LedgerEntry(
        address=kind_desired.address,
        written_sha256=kind_desired.entry_sha256,
        declaration_sha256=kind_desired.declaration_sha256,
        capability=kind_desired.capability,
        transaction_id=tx,
        created_file=created,
    )


NEXT_ROWS: list[tuple[str, str, LedgerEntry | None, list[tuple[str, str]], LedgerEntry | None]] = [
    ("add-created-file", "add", None, [("claude_code", ".mcp.json")], _entry(want(A), "tx-new", True)),
    ("add-existing-file", "add", None, [], _entry(want(A), "tx-new", False)),
    ("add-other-file-created", "add", None, [("cursor", ".mcp.json")], _entry(want(A), "tx-new", False)),
    ("restore-carries-created", "restore", OLD_CREATED, [], _entry(want(A), "tx-new", True)),
    ("restore-carries-not-created", "restore", OLD_PLAIN, [], _entry(want(A), "tx-new", False)),
    ("restore-file-recreated", "restore", OLD_PLAIN, [("claude_code", ".mcp.json")], _entry(want(A), "tx-new", True)),
    ("replace-carries-created", "replace", OLD_CREATED, [], _entry(want(A), "tx-new", True)),
    (
        "replace-takes-new-capability",
        "replace",
        owned(A, "old", capability="cap-0"),
        [],
        _entry(want(A, capability="cap-9"), "tx-new", False),
    ),
    ("keep-unchanged", "keep", OLD_CREATED, [("claude_code", ".mcp.json")], OLD_CREATED),
    ("skip-drift-unchanged", "skip_drift", OLD_CREATED, [], OLD_CREATED),
    ("remove-drops", "remove", OLD_CREATED, [], None),
    ("release-drops", "release", OLD_CREATED, [], None),
    ("forget-drops", "forget", OLD_CREATED, [], None),
]


@pytest.mark.parametrize("row", NEXT_ROWS, ids=[row[0] for row in NEXT_ROWS])
def test_next_ledger_table(
    row: tuple[str, str, LedgerEntry | None, list[tuple[str, str]], LedgerEntry | None],
) -> None:
    _, kind, previous_entry, created, expected = row
    bystander = owned(addr("bystander"), "kept", transaction_id="tx-bystander")
    previous: Ledger | LedgerAbsent = (
        ledger_of(bystander, previous_entry) if previous_entry else LedgerAbsent()
    )
    desired_item = want(A, capability="cap-9") if row[0] == "replace-takes-new-capability" else want(A)
    plan = ReconcilePlan(actions=(Action(kind, A),), refusals=(), warnings=())  # type: ignore[arg-type]
    result = next_ledger(CONSUMER, "project", previous, plan, [desired_item], "tx-new", created)
    expected_entries = ([bystander] if previous_entry else []) + ([expected] if expected else [])
    assert result.entries == tuple(sorted(expected_entries, key=lambda e: (e.address.agent, e.address.relpath, e.address.name)))
    assert (result.consumer, result.scope) == (CONSUMER, "project")
    assert parse_ledger(encode_ledger(result), expected_consumer=CONSUMER) == result


def test_next_ledger_follows_a_reconcile_plan_end_to_end() -> None:
    added, kept, edited, removed, released, forgotten = (
        addr(n) for n in ("added", "kept", "edited", "removed", "released", "forgotten")
    )
    previous = ledger_of(
        owned(kept, "k"), owned(edited, "e"), owned(removed, "r"), owned(released, "p"), owned(forgotten, "f")
    )
    desired = [want(added), want(kept, "k"), want(edited, "changed")]
    observed = {
        added: seen(added, "absent"),
        kept: seen(kept, "present", "k"),
        edited: seen(edited, "present", "user-edit"),
        removed: seen(removed, "present", "r"),
        released: seen(released, "present", "user-edit"),
        forgotten: seen(forgotten, "surface_absent"),
    }
    plan = reconcile(desired, observed, previous)
    result = next_ledger(CONSUMER, "project", previous, plan, desired, "tx-new", [])
    by_name = {e.address.name: e for e in result.entries}
    assert sorted(by_name) == ["added", "edited", "kept"]
    assert by_name["added"].transaction_id == "tx-new"
    assert by_name["kept"] == owned(kept, "k")
    # skip_drift keeps the ledger digest at what csk last wrote, not what the user edited
    assert by_name["edited"] == owned(edited, "e")


def test_next_ledger_refuses_unknown_previous_and_foreign_previous() -> None:
    plan = ReconcilePlan(actions=(), refusals=(), warnings=())
    with pytest.raises(McpOwnershipError) as caught:
        next_ledger(CONSUMER, "project", LedgerUnknown("x"), plan, [], "tx", [])  # type: ignore[arg-type]
    assert caught.value.code == "mcp_ownership_unknown"
    foreign = Ledger(consumer=OTHER_CONSUMER, scope="project", entries=())
    with pytest.raises(McpOwnershipError):
        next_ledger(CONSUMER, "project", foreign, plan, [], "tx", [])


def test_next_ledger_refuses_plans_that_disagree_with_the_ledger() -> None:
    with pytest.raises(McpOwnershipError):
        next_ledger(CONSUMER, "project", LedgerAbsent(), ReconcilePlan((Action("remove", A),), (), ()), [], "tx", [])
    with pytest.raises(McpOwnershipError):
        next_ledger(CONSUMER, "project", LedgerAbsent(), ReconcilePlan((Action("keep", A),), (), ()), [], "tx", [])
    with pytest.raises(McpOwnershipError):
        next_ledger(CONSUMER, "project", LedgerAbsent(), ReconcilePlan((Action("add", A),), (), ()), [], "tx", [])
    with pytest.raises(McpOwnershipError):
        next_ledger(CONSUMER, "project", LedgerAbsent(), ReconcilePlan((Action("add", A),), (), ()), [want(A)], "", [])


# --- ids and paths -----------------------------------------------------


def test_consumer_id_matches_the_documented_derivation(tmp_path: Path) -> None:
    identity = "/Users/someone/project"
    expected = "p-" + hashlib.sha256(b"csk-mcp-consumer-v1\0project\0" + identity.encode()).hexdigest()[:40]
    assert consumer_id(identity, "project") == expected
    assert consumer_id(identity, "project") != consumer_id(identity + "/", "project")
    real = consumer_id(locking.canonical_project_identity(tmp_path), "project")
    assert real.startswith("p-") and len(real) == 42


def test_consumer_id_refuses_other_scopes_and_unencodable_identity() -> None:
    with pytest.raises(McpOwnershipError) as caught:
        consumer_id("/x", "user")  # type: ignore[arg-type]
    assert caught.value.code == "mcp_scope_mismatch"
    with pytest.raises(McpOwnershipError):
        consumer_id("/x\ud800", "project")


def test_store_paths_and_malformed_consumer_ids(tmp_path: Path) -> None:
    assert ledger_path(tmp_path, CONSUMER) == tmp_path / "state/mcp/ledger" / f"{CONSUMER}.json"
    assert consent_path(tmp_path, CONSUMER) == tmp_path / "state/mcp/consent" / f"{CONSUMER}.json"
    for bad in ("../escape", "p-xyz", "", CONSUMER + "/x", CONSUMER.upper(), "q-" + CONSUMER[2:]):
        with pytest.raises(McpOwnershipError):
            ledger_path(tmp_path, bad)
        with pytest.raises(McpOwnershipError):
            consent_path(tmp_path, bad)
        assert isinstance(load_ledger(tmp_path, bad), LedgerUnknown)


# --- consent keys ------------------------------------------------------

SUBJECT = ConsentSubject(
    name="tools",
    agents=("claude_code", "cursor"),
    scope="project",
    declaration_sha256=sha("declaration"),
    capabilities=("claude_code/1", "cursor/1"),
)


def _variant(**changes: Any) -> ConsentSubject:
    fields: dict[str, Any] = {
        "name": SUBJECT.name,
        "agents": SUBJECT.agents,
        "scope": SUBJECT.scope,
        "declaration_sha256": SUBJECT.declaration_sha256,
        "capabilities": SUBJECT.capabilities,
    }
    fields.update(changes)
    return ConsentSubject(**fields)


@pytest.mark.parametrize(
    "subject",
    [
        _variant(name="other"),
        _variant(agents=("claude_code", "cursor", "codex_cli")),
        _variant(agents=("claude_code",)),
        _variant(agents=("claude_code", "codex_cli")),
        _variant(scope="user"),
        _variant(declaration_sha256=sha("changed declaration")),
        _variant(capabilities=("claude_code/1", "cursor/2")),
        _variant(capabilities=("claude_code/1",)),
    ],
    ids=["name", "agent-added", "agent-removed", "agent-replaced", "scope", "digest", "capability-changed", "capability-removed"],
)
def test_consent_key_changes_with_every_subject_component(subject: ConsentSubject) -> None:
    assert consent_key(CONSUMER, subject) != consent_key(CONSUMER, SUBJECT)


def test_consent_key_changes_with_the_consumer_and_is_order_independent() -> None:
    assert consent_key(OTHER_CONSUMER, SUBJECT) != consent_key(CONSUMER, SUBJECT)
    reordered = _variant(agents=("cursor", "claude_code"), capabilities=("cursor/1", "claude_code/1"))
    assert consent_key(CONSUMER, reordered) == consent_key(CONSUMER, SUBJECT)
    key = consent_key(CONSUMER, SUBJECT)
    assert len(key) == 64 and key == key.lower()


def test_consent_key_refuses_malformed_input() -> None:
    with pytest.raises(McpOwnershipError):
        consent_key(CONSUMER, _variant(declaration_sha256="ABC"))
    with pytest.raises(McpOwnershipError):
        consent_key("not-a-consumer", SUBJECT)


def test_widening_or_changing_a_subject_makes_it_unapproved_again() -> None:
    approved = frozenset({consent_key(CONSUMER, SUBJECT)})
    assert unapproved(CONSUMER, [SUBJECT], approved) == ()
    widened = _variant(agents=(*SUBJECT.agents, "codex_cli"))
    changed = _variant(declaration_sha256=sha("new bytes"))
    assert unapproved(CONSUMER, [SUBJECT, widened, changed], approved) == (widened, changed)
    assert unapproved(OTHER_CONSUMER, [SUBJECT], approved) == (SUBJECT,)
    assert unapproved(CONSUMER, [SUBJECT], frozenset()) == (SUBJECT,)


# --- ledger bytes ------------------------------------------------------

B = addr("b", "codex_cli", ".codex/config.toml", "mcp_servers")


def sample_ledger() -> Ledger:
    return ledger_of(owned(A, created_file=True), owned(B, "other"))


def sample_document() -> dict[str, Any]:
    return protocol_json.loads(encode_ledger(sample_ledger()))  # type: ignore[no-any-return]


def line(document: Any) -> bytes:
    return protocol_json.canonical_bytes(document) + b"\n"


def test_ledger_round_trip_is_deterministic() -> None:
    ledger = sample_ledger()
    data = encode_ledger(ledger)
    assert data.endswith(b"}\n") and not data.endswith(b"\n\n")
    assert parse_ledger(data, expected_consumer=CONSUMER) == ledger
    reversed_entries = Ledger(CONSUMER, "project", tuple(reversed(ledger.entries)))
    assert encode_ledger(reversed_entries) == data
    assert parse_ledger(encode_ledger(Ledger(CONSUMER, "project", ())), expected_consumer=CONSUMER).entries == ()


def _with(mutate: Callable[[dict[str, Any]], None]) -> bytes:
    document = sample_document()
    mutate(document)
    return line(document)


def _unsorted_entries() -> bytes:
    document = sample_document()
    document["entries"].reverse()
    return line(document)


LEDGER_NEGATIVES: list[tuple[str, bytes]] = [
    ("unknown-top-field", _with(lambda d: d.update(extra=1))),
    ("unknown-entry-field", _with(lambda d: d["entries"][0].update(extra=1))),
    ("unknown-address-field", _with(lambda d: d["entries"][0]["address"].update(extra=1))),
    ("missing-field", _with(lambda d: d.pop("scope"))),
    ("missing-entry-field", _with(lambda d: d["entries"][0].pop("capability"))),
    ("wrong-consumer", _with(lambda d: d.update(consumer=OTHER_CONSUMER))),
    ("malformed-consumer", _with(lambda d: d.update(consumer="p-1"))),
    # the copy sits in sorted position, so only the duplicate check can refuse it
    ("duplicate-address", _with(lambda d: d["entries"].insert(1, dict(d["entries"][0])))),
    ("uppercase-digest", _with(lambda d: d["entries"][0].update(written_sha256=sha("x").upper()))),
    ("short-digest", _with(lambda d: d["entries"][0].update(declaration_sha256="ab"))),
    ("non-string-digest", _with(lambda d: d["entries"][0].update(written_sha256=7))),
    ("wrong-schema-version", _with(lambda d: d.update(schema_version=2))),
    ("bool-schema-version", _with(lambda d: d.update(schema_version=True))),
    ("int-created-file", _with(lambda d: d["entries"][0].update(created_file=1))),
    ("string-created-file", _with(lambda d: d["entries"][0].update(created_file="true"))),
    ("user-scope", _with(lambda d: d.update(scope="user"))),
    ("non-string-scope", _with(lambda d: d.update(scope=1))),
    ("entries-not-list", _with(lambda d: d.update(entries={}))),
    ("empty-name", _with(lambda d: d["entries"][0]["address"].update(name=""))),
    ("empty-transaction-id", _with(lambda d: d["entries"][0].update(transaction_id=""))),
    ("control-character", _with(lambda d: d["entries"][0].update(capability="a\nb"))),
    ("parent-relpath", _with(lambda d: d["entries"][0]["address"].update(relpath="../.mcp.json"))),
    ("absolute-relpath", _with(lambda d: d["entries"][0]["address"].update(relpath="/etc/x"))),
    ("backslash-relpath", _with(lambda d: d["entries"][0]["address"].update(relpath="a\\b"))),
    ("float", encode_ledger(sample_ledger()).replace(b'"schema_version":1', b'"schema_version":1.5')),
    ("bom", b"\xef\xbb\xbf" + encode_ledger(sample_ledger())),
    ("no-trailing-newline", encode_ledger(sample_ledger())[:-1]),
    ("crlf", encode_ledger(sample_ledger())[:-1] + b"\r\n"),
    ("double-newline", encode_ledger(sample_ledger()) + b"\n"),
    ("indented", protocol_json.canonical_bytes(sample_document()).replace(b",", b", ") + b"\n"),
    ("unsorted-keys", b'{"schema_version":1,"consumer":"' + CONSUMER.encode() + b'","scope":"project","entries":[]}\n'),
    ("unsorted-entries", _unsorted_entries()),
    ("not-an-object", b"[]\n"),
    ("not-json", b"nope\n"),
    ("empty", b""),
    ("invalid-utf8", b"\xff\xfe\n"),
    ("duplicate-json-key", b'{"consumer":"a","consumer":"b"}\n'),
]


@pytest.mark.parametrize("name,data", LEDGER_NEGATIVES, ids=[n for n, _ in LEDGER_NEGATIVES])
def test_parse_ledger_refuses(name: str, data: bytes) -> None:
    with pytest.raises(McpOwnershipError) as caught:
        parse_ledger(data, expected_consumer=CONSUMER)
    assert caught.value.code == "mcp_ownership_unknown"


def test_parse_ledger_refuses_a_valid_ledger_for_another_consumer() -> None:
    data = encode_ledger(sample_ledger())
    with pytest.raises(McpOwnershipError) as caught:
        parse_ledger(data, expected_consumer=OTHER_CONSUMER)
    assert caught.value.code == "mcp_ownership_unknown"


def test_encode_ledger_refuses_what_it_could_not_parse_back() -> None:
    with pytest.raises(McpOwnershipError):
        encode_ledger(ledger_of(owned(A), owned(A, "again")))
    with pytest.raises(McpOwnershipError):
        encode_ledger(Ledger("bad", "project", ()))


def test_ledger_never_holds_more_than_names_addresses_digests_and_capabilities() -> None:
    text = encode_ledger(sample_ledger()).decode()
    assert set(protocol_json.loads(text)) == {"schema_version", "consumer", "scope", "entries"}


# --- ledger on disk ----------------------------------------------------


def put(home: Path, store: str, consumer: str, data: bytes) -> Path:
    path = home / "state/mcp" / store / f"{consumer}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def symlink(target: Path, link: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows without privilege
        pytest.skip(f"symlinks are unavailable here: {exc}")


def test_load_ledger_round_trip_and_absence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert load_ledger(home, CONSUMER) == LedgerAbsent()  # the home itself does not exist
    (home / "state/mcp/ledger").mkdir(parents=True)
    assert load_ledger(home, CONSUMER) == LedgerAbsent()  # the store exists, the file does not
    ledger = sample_ledger()
    put(home, "ledger", CONSUMER, encode_ledger(ledger))
    assert load_ledger(home, CONSUMER) == ledger


@pytest.mark.parametrize("name,data", LEDGER_NEGATIVES[:6] + LEDGER_NEGATIVES[25:28], ids=lambda v: v if isinstance(v, str) else "")
def test_load_ledger_reports_invalid_content_as_unknown_not_absent(tmp_path: Path, name: str, data: bytes) -> None:
    put(tmp_path, "ledger", CONSUMER, data)
    result = load_ledger(tmp_path, CONSUMER)
    assert isinstance(result, LedgerUnknown) and result.reason


def test_load_ledger_refuses_another_consumers_ledger_under_this_name(tmp_path: Path) -> None:
    other = Ledger(OTHER_CONSUMER, "project", ())
    put(tmp_path, "ledger", CONSUMER, encode_ledger(other))
    assert isinstance(load_ledger(tmp_path, CONSUMER), LedgerUnknown)


def test_load_ledger_refuses_a_symlinked_file(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    real.write_bytes(encode_ledger(sample_ledger()))
    directory = tmp_path / "home/state/mcp/ledger"
    directory.mkdir(parents=True)
    symlink(real, directory / f"{CONSUMER}.json")
    result = load_ledger(tmp_path / "home", CONSUMER)
    assert isinstance(result, LedgerUnknown) and "link" in result.reason


def test_load_ledger_refuses_a_dangling_symlinked_file(tmp_path: Path) -> None:
    directory = tmp_path / "state/mcp/ledger"
    directory.mkdir(parents=True)
    symlink(tmp_path / "missing.json", directory / f"{CONSUMER}.json")
    assert isinstance(load_ledger(tmp_path, CONSUMER), LedgerUnknown)


@pytest.mark.parametrize("linked", ["state", "state/mcp", "state/mcp/ledger"])
def test_load_ledger_refuses_a_symlinked_directory_component(tmp_path: Path, linked: str) -> None:
    real_home = tmp_path / "real"
    put(real_home, "ledger", CONSUMER, encode_ledger(sample_ledger()))
    assert load_ledger(real_home, CONSUMER) == sample_ledger()
    home = tmp_path / "home"
    target = real_home / linked
    link = home / linked
    link.parent.mkdir(parents=True)
    symlink(target, link)
    result = load_ledger(home, CONSUMER)
    assert isinstance(result, LedgerUnknown) and "link" in result.reason


def test_load_ledger_treats_wrong_file_types_as_unknown(tmp_path: Path) -> None:
    (tmp_path / "state/mcp/ledger" / f"{CONSUMER}.json").mkdir(parents=True)
    assert isinstance(load_ledger(tmp_path, CONSUMER), LedgerUnknown)
    other = tmp_path / "other"
    (other / "state").mkdir(parents=True)
    (other / "state/mcp").write_bytes(b"x")
    assert isinstance(load_ledger(other, CONSUMER), LedgerUnknown)


def test_load_ledger_refuses_an_oversized_file(tmp_path: Path) -> None:
    put(tmp_path, "ledger", CONSUMER, b"x" * (4 * 1024 * 1024 + 1))
    result = load_ledger(tmp_path, CONSUMER)
    assert isinstance(result, LedgerUnknown) and "size" in result.reason


@POSIX_ONLY
def test_load_ledger_refuses_an_unreadable_file(tmp_path: Path) -> None:
    path = put(tmp_path, "ledger", CONSUMER, encode_ledger(sample_ledger()))
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):  # pragma: no cover - running as root
            pytest.skip("file permissions do not restrict this user")
        assert isinstance(load_ledger(tmp_path, CONSUMER), LedgerUnknown)
    finally:
        path.chmod(0o600)


# --- consent store -----------------------------------------------------

KEY_A, KEY_B, KEY_C = sha("approval-a"), sha("approval-b"), sha("approval-c")


def test_consent_bytes_round_trip_and_are_canonical() -> None:
    data = encode_consent(CONSUMER, [KEY_B, KEY_A, KEY_A])
    assert data == line({"schema_version": 1, "consumer": CONSUMER, "approved": sorted([KEY_A, KEY_B])})
    assert parse_consent(data, expected_consumer=CONSUMER) == frozenset({KEY_A, KEY_B})
    assert parse_consent(encode_consent(CONSUMER, []), expected_consumer=CONSUMER) == frozenset()


def _consent(**changes: Any) -> bytes:
    document: dict[str, Any] = {"schema_version": 1, "consumer": CONSUMER, "approved": sorted([KEY_A, KEY_B])}
    document.update(changes)
    return line(document)


CONSENT_NEGATIVES: list[tuple[str, bytes]] = [
    ("unknown-field", _consent(extra=True)),
    ("wrong-consumer", _consent(consumer=OTHER_CONSUMER)),
    ("wrong-version", _consent(schema_version=2)),
    ("bad-key", _consent(approved=[KEY_A.upper()])),
    ("non-string-key", _consent(approved=[1])),
    ("duplicate-key", _consent(approved=[KEY_A, KEY_A])),
    ("unsorted", _consent(approved=[KEY_B, KEY_A])),
    ("approved-not-list", _consent(approved="x")),
    ("bom", b"\xef\xbb\xbf" + _consent()),
    ("no-newline", _consent()[:-1]),
    ("not-canonical", _consent().replace(b",", b", ")),
    ("not-json", b"{\n"),
]


@pytest.mark.parametrize("name,data", CONSENT_NEGATIVES, ids=[n for n, _ in CONSENT_NEGATIVES])
def test_parse_consent_refuses(name: str, data: bytes) -> None:
    with pytest.raises(McpOwnershipError) as caught:
        parse_consent(data, expected_consumer=CONSUMER)
    assert caught.value.code == "mcp_ownership_unknown"


def test_load_consent_missing_is_empty(tmp_path: Path) -> None:
    assert load_consent(tmp_path / "no-home", CONSUMER) == frozenset()
    (tmp_path / "state/mcp/consent").mkdir(parents=True)
    assert load_consent(tmp_path, CONSUMER) == frozenset()


@pytest.mark.parametrize("name,data", CONSENT_NEGATIVES[:3], ids=[n for n, _ in CONSENT_NEGATIVES[:3]])
def test_load_consent_raises_for_invalid_content(tmp_path: Path, name: str, data: bytes) -> None:
    put(tmp_path, "consent", CONSUMER, data)
    with pytest.raises(McpOwnershipError):
        load_consent(tmp_path, CONSUMER)


def test_record_consent_round_trip_and_merge_never_drops_keys(tmp_path: Path) -> None:
    home = tmp_path / "home"
    record_consent(home, CONSUMER, [KEY_A])
    assert load_consent(home, CONSUMER) == {KEY_A}
    record_consent(home, CONSUMER, [KEY_B])
    assert load_consent(home, CONSUMER) == {KEY_A, KEY_B}
    record_consent(home, CONSUMER, [])
    record_consent(home, CONSUMER, [KEY_A])
    record_consent(home, CONSUMER, [KEY_C, KEY_B])
    assert load_consent(home, CONSUMER) == {KEY_A, KEY_B, KEY_C}
    # A different consumer has its own store.
    assert load_consent(home, OTHER_CONSUMER) == frozenset()
    assert not [p for p in consent_path(home, CONSUMER).parent.iterdir() if p.name.endswith(".tmp")]


def test_record_consent_with_nothing_to_record_writes_nothing(tmp_path: Path) -> None:
    record_consent(tmp_path / "home", CONSUMER, [])
    assert not consent_path(tmp_path / "home", CONSUMER).exists()


@POSIX_ONLY
def test_record_consent_creates_private_files_and_directories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    previous = os.umask(0)  # a permissive umask must not widen what is created
    try:
        record_consent(home, CONSUMER, [KEY_A])
        record_consent(home, CONSUMER, [KEY_B])
    finally:
        os.umask(previous)
    path = consent_path(home, CONSUMER)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for directory in (home / "state", home / "state/mcp", path.parent, home / "state/mcp/ledger"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory


def test_record_consent_rejects_malformed_keys_before_touching_disk(tmp_path: Path) -> None:
    for bad in ("", "abc", KEY_A.upper(), KEY_A + "0"):
        with pytest.raises(McpOwnershipError):
            record_consent(tmp_path / "home", CONSUMER, [KEY_A, bad])
    assert not consent_path(tmp_path / "home", CONSUMER).exists()
    with pytest.raises(McpOwnershipError):
        record_consent(tmp_path / "home", "../x", [KEY_A])


def test_record_consent_refuses_an_invalid_existing_store_and_leaves_it_alone(tmp_path: Path) -> None:
    path = put(tmp_path, "consent", CONSUMER, b"garbage\n")
    with pytest.raises(McpOwnershipError):
        record_consent(tmp_path, CONSUMER, [KEY_A])
    assert path.read_bytes() == b"garbage\n"


@pytest.mark.parametrize("linked", ["state", "state/mcp", "state/mcp/consent"])
def test_record_consent_refuses_linked_directories_and_writes_nothing_through_them(
    tmp_path: Path, linked: str
) -> None:
    real = tmp_path / "real"
    (real / "state/mcp/consent").mkdir(parents=True)
    home = tmp_path / "home"
    link = home / linked
    link.parent.mkdir(parents=True)
    symlink(real / linked, link)
    with pytest.raises(McpOwnershipError):
        record_consent(home, CONSUMER, [KEY_A])
    assert list((real / "state/mcp/consent").iterdir()) == []


def test_record_consent_refuses_a_symlinked_store_file(tmp_path: Path) -> None:
    victim = tmp_path / "victim.json"
    victim.write_bytes(encode_consent(CONSUMER, [KEY_B]))
    home = tmp_path / "home"
    consent_dir = home / "state/mcp/consent"
    consent_dir.mkdir(parents=True)
    symlink(victim, consent_dir / f"{CONSUMER}.json")
    with pytest.raises(McpOwnershipError):
        record_consent(home, CONSUMER, [KEY_A])
    assert victim.read_bytes() == encode_consent(CONSUMER, [KEY_B])
    with pytest.raises(McpOwnershipError):
        load_consent(home, CONSUMER)


def test_record_consent_does_not_lose_concurrent_approvals(tmp_path: Path) -> None:
    home = tmp_path / "home"
    keys = [sha(f"approval-{i}") for i in range(8)]
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(keys))

    def worker(key: str) -> None:
        try:
            barrier.wait()
            record_consent(home, CONSUMER, [key])
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(key,)) for key in keys]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors
    assert load_consent(home, CONSUMER) == frozenset(keys)


def test_record_consent_takes_the_manager_home_lock_and_so_cannot_nest_inside_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with locking.ManagerHomeLock(home):
        with pytest.raises(locking.LockOrderError):
            record_consent(home, CONSUMER, [KEY_A])
    assert not consent_path(home, CONSUMER).exists()
    record_consent(home, CONSUMER, [KEY_A])
    assert load_consent(home, CONSUMER) == {KEY_A}


# --- size bound ------------------------------------------------------

def test_parse_ledger_refuses_input_over_the_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    data = encode_ledger(sample_ledger())
    monkeypatch.setattr(mcp_ownership, "_STORE_LIMIT", len(data))
    assert parse_ledger(data, expected_consumer=CONSUMER) == sample_ledger()
    monkeypatch.setattr(mcp_ownership, "_STORE_LIMIT", len(data) - 1)
    with pytest.raises(McpOwnershipError) as caught:
        parse_ledger(data, expected_consumer=CONSUMER)
    assert caught.value.code == "mcp_ownership_unknown"


def test_parse_consent_refuses_input_over_the_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    data = encode_consent(CONSUMER, [KEY_A, KEY_B])
    monkeypatch.setattr(mcp_ownership, "_STORE_LIMIT", len(data))
    assert parse_consent(data, expected_consumer=CONSUMER) == frozenset({KEY_A, KEY_B})
    monkeypatch.setattr(mcp_ownership, "_STORE_LIMIT", len(data) - 1)
    with pytest.raises(McpOwnershipError) as caught:
        parse_consent(data, expected_consumer=CONSUMER)
    assert caught.value.code == "mcp_ownership_unknown"


def test_record_consent_refuses_a_store_over_the_size_limit_and_leaves_the_existing_file_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Scaled-down full-a F1: N keys load, the N+1st is refused, old bytes stay loadable.
    keys = [sha(f"scale-key-{i}") for i in range(4)]
    bound = len(encode_consent(CONSUMER, keys[:3]))
    assert len(encode_consent(CONSUMER, keys)) > bound
    monkeypatch.setattr(mcp_ownership, "_STORE_LIMIT", bound)
    home = tmp_path / "home"
    record_consent(home, CONSUMER, keys[:3])
    path = consent_path(home, CONSUMER)
    before = path.read_bytes()
    assert len(before) == bound
    assert load_consent(home, CONSUMER) == frozenset(keys[:3])
    with pytest.raises(McpOwnershipError) as caught:
        record_consent(home, CONSUMER, [keys[3]])
    assert caught.value.code == "mcp_consent_store_full"
    assert path.read_bytes() == before
    assert load_consent(home, CONSUMER) == frozenset(keys[:3])


@pytest.mark.parametrize("count", [0, 1, 2, 16])
def test_record_consent_accepts_only_stores_the_loader_reads_back(tmp_path: Path, count: int) -> None:
    home = tmp_path / "home"
    keys = [sha(f"agreement-{count}-{i}") for i in range(count)]
    record_consent(home, CONSUMER, keys)
    assert load_consent(home, CONSUMER) == frozenset(keys)


def test_record_consent_accepted_writes_stay_loadable_under_a_small_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Writer/reader agreement: every store record_consent accepts is loadable,
    # and the first store it refuses leaves the previous one loadable too.
    monkeypatch.setattr(mcp_ownership, "_STORE_LIMIT", 512)
    home = tmp_path / "home"
    approved: set[str] = set()
    full = False
    for i in range(20):
        key = sha(f"small-bound-{i}")
        try:
            record_consent(home, CONSUMER, [key])
        except McpOwnershipError as caught:
            assert caught.code == "mcp_consent_store_full"
            full = True
            break
        approved.add(key)
        assert load_consent(home, CONSUMER) == frozenset(approved)
    assert full and approved
    assert load_consent(home, CONSUMER) == frozenset(approved)


# --- MCP-owned directory modes ----------------------------------------

MCP_OWNED_DIRECTORIES = ("state/mcp", "state/mcp/consent", "state/mcp/ledger")


@POSIX_ONLY
def test_record_consent_narrows_existing_mcp_owned_directories_and_leaves_state_alone(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    for rel in ("state", *MCP_OWNED_DIRECTORIES):
        directory = home / rel
        directory.mkdir()
        directory.chmod(0o755)
    record_consent(home, CONSUMER, [KEY_A])
    assert stat.S_IMODE((home / "state").stat().st_mode) == 0o755
    for rel in MCP_OWNED_DIRECTORIES:
        assert stat.S_IMODE((home / rel).stat().st_mode) == 0o700, rel
    assert stat.S_IMODE(consent_path(home, CONSUMER).stat().st_mode) == 0o600
    assert load_consent(home, CONSUMER) == {KEY_A}


@POSIX_ONLY
def test_record_consent_refuses_mcp_owned_directories_owned_by_someone_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "state/mcp/consent").mkdir(parents=True)
    stranger = os.geteuid() + 1
    monkeypatch.setattr(os, "geteuid", lambda: stranger)
    with pytest.raises(McpOwnershipError) as caught:
        record_consent(home, CONSUMER, [KEY_A])
    assert caught.value.code == "mcp_ownership_unknown"
    assert "not owned by the current user" in str(caught.value)
    assert not consent_path(home, CONSUMER).exists()
