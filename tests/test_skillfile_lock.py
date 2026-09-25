from __future__ import annotations

import hashlib
import json
import locale
import os
import random
import sys
import unicodedata
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from csk import protocol_json
from csk.sources import errors as source_errors
from csk.sources import lock as source_lock
from csk.sources import package_identity


SHA1 = "0123456789abcdef0123456789abcdef01234567"
SHA256 = "0123456789abcdef" * 4
SNAPSHOT = "sha256:" + "1" * 64
CONTENT = "sha256:" + "2" * 64


def _commit(object_format: str = "sha1") -> package_identity.LockedCommit:
    return package_identity.LockedCommit(
        object_format=object_format, hex=SHA1 if object_format == "sha1" else SHA256
    )


def _manifest(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 2,
        "agents": ["codex"],
        "sources": {"team": {"repository": "example.org/kit", "revision": SHA1}},
        "skills": [{"name": "review", "from": "team", "directory": "."}],
    }
    value.update(changes)
    return value


def _local_member(name: str = "review", selection: int | None = 0) -> source_lock.LockMember:
    return source_lock.LockMember(
        name=name,
        selection=selection,
        directory=".",
        package=package_identity.LocalSnapshot(SNAPSHOT),
        content_sha256=CONTENT,
    )


def _network_member(
    name: str = "review",
    selection: int | None = 0,
    directory: str = "skills/review",
) -> source_lock.LockMember:
    return source_lock.LockMember(
        name=name,
        selection=selection,
        directory=directory,
        package=package_identity.NetworkGit(
            repository="example.org/kit",
            commit=_commit(),
            directory=directory,
        ),
        content_sha256=CONTENT,
    )


def _configured_member(
    name: str = "review",
    selection: int | None = 0,
    directory: str = ".",
) -> source_lock.LockMember:
    return source_lock.LockMember(
        name=name,
        selection=selection,
        directory=directory,
        package=package_identity.ConfiguredGit(source="team/review", commit=_commit()),
        content_sha256=CONTENT,
    )


def _lock_payload(
    manifest: dict[str, Any] | None = None,
    members: tuple[source_lock.LockMember, ...] = (_local_member(),),
) -> source_lock.SkillfileLock:
    return source_lock.create_lock(manifest or _manifest(), members)


def _payload_with_valid_digest(lock: source_lock.SkillfileLock) -> dict[str, Any]:
    payload = lock.to_json()
    payload["lock_sha256"] = lock.computed_lock_sha256()
    return payload


def _payload_with_manual_digest(payload: dict[str, Any]) -> dict[str, Any]:
    preimage = {key: value for key, value in payload.items() if key != "lock_sha256"}
    payload["lock_sha256"] = "sha256:" + hashlib.sha256(
        protocol_json.canonical_bytes(preimage)
    ).hexdigest()
    return payload


def _generated_json(rng: random.Random, atoms: list[str], depth: int) -> Any:
    if depth >= 3:
        return rng.choice([rng.choice(atoms), rng.randrange(-1000, 1000), True, False, None])
    choice = rng.randrange(5)
    if choice == 0:
        return {
            rng.choice(atoms) or "k": _generated_json(rng, atoms, depth + 1)
            for _ in range(rng.randrange(4))
        }
    if choice == 1:
        return [_generated_json(rng, atoms, depth + 1) for _ in range(rng.randrange(4))]
    return rng.choice([rng.choice(atoms), rng.randrange(-1000, 1000), True, False, None])


def _reversed_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _reversed_json(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reversed_json(item) for item in value]
    return value


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found: list[str] = []
        for key in value:
            found.extend(_walk_strings(key))
            found.extend(_walk_strings(value[key]))
        return found
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            items.extend(_walk_strings(item))
        return items
    return []


def test_package_identity_arms_are_frozen_dataclasses_and_have_one_wire_shape() -> None:
    identities: tuple[package_identity.PackageIdentity, ...] = (
        package_identity.LocalSnapshot(SNAPSHOT),
        package_identity.NetworkGit("example.org/kit", _commit(), "skills/review"),
        package_identity.ConfiguredGit("team/review", _commit()),
    )
    assert all(is_dataclass(identity) for identity in identities)
    assert all(identity.__dataclass_params__.frozen for identity in identities)
    assert [identity.kind for identity in identities] == [
        "local-snapshot",
        "network-git",
        "configured-git",
    ]
    assert package_identity.package_identity_to_json(identities[0]) == {
        "kind": "local-snapshot",
        "snapshot": SNAPSHOT,
    }
    assert package_identity.package_identity_to_json(identities[1]) == {
        "kind": "network-git",
        "repository": "example.org/kit",
        "commit": {"object_format": "sha1", "hex": SHA1},
        "directory": "skills/review",
    }
    assert package_identity.package_identity_to_json(identities[2]) == {
        "kind": "configured-git",
        "source": "team/review",
        "commit": {"object_format": "sha1", "hex": SHA1},
        "directory": ".",
    }
    with pytest.raises(FrozenInstanceError):
        identities[0].snapshot = SNAPSHOT  # type: ignore[misc]


def test_legacy_configured_identity_defaults_source_to_skill_name() -> None:
    identity = package_identity.configured_git_for_legacy("review", _commit())
    assert identity.source == "review"
    assert identity.directory == "."


@pytest.mark.parametrize(
    "repository",
    [
        "/private/repository",
        "https://example.org/kit",
        "alice@example.org/kit",
        "example.org:2222/kit",
        "example.org/kit\\nested",
    ],
)
def test_package_identity_rejects_machine_paths_and_endpoint_spellings(repository: str) -> None:
    with pytest.raises(source_errors.SourceError):
        package_identity.NetworkGit(repository, _commit())


def test_machine_private_binding_is_separate_and_rebinding_preserves_identity(tmp_path: Path) -> None:
    identity = package_identity.LocalSnapshot(SNAPSHOT)
    first = source_lock.MachinePrivateBinding(
        source_location=tmp_path / "one" / "source",
        root_inputs=("SKILL.md", "runtime/tool"),
    )
    second = source_lock.MachinePrivateBinding(
        source_location=tmp_path / "two" / "source",
        root_inputs=("SKILL.md", "runtime/tool"),
    )
    assert first.source_location != second.source_location
    assert first.root_inputs == second.root_inputs
    assert source_lock.serialize_machine_private_binding(first) != source_lock.serialize_machine_private_binding(
        second
    )
    assert package_identity.package_identity_bytes(identity) == package_identity.package_identity_bytes(
        identity
    )
    assert package_identity.package_identity_sha256(identity) == package_identity.package_identity_sha256(
        identity
    )
    binding_bytes = source_lock.serialize_machine_private_binding(first)
    decoded_binding = json.loads(binding_bytes.decode("utf-8"))
    assert decoded_binding["source_location"] == str(first.source_location)

    lock_bytes = source_lock.serialize_lock(_lock_payload())
    lock_strings = _walk_strings(json.loads(lock_bytes.decode("utf-8")))
    assert all(str(first.source_location) not in value for value in lock_strings)
    # "https://" and "alice@" contain no JSON-escapable character, so unlike a
    # path these byte sequences appear verbatim whenever the value does.
    assert b"https://" not in lock_bytes
    assert b"alice@" not in lock_bytes


def test_machine_binding_location_with_json_escapes_round_trips_by_decoded_value(
    tmp_path: Path,
) -> None:
    # A backslash is a legal POSIX filename character and is always escaped by
    # JSON, so this spelling diverges under a raw-bytes comparison on every
    # host -- the same divergence a drive-lettered path shows only on Windows.
    location = tmp_path / "sour\\ce"
    binding = source_lock.MachinePrivateBinding(
        source_location=location, root_inputs=("SKILL.md",)
    )
    raw = source_lock.serialize_machine_private_binding(binding)
    assert json.loads(raw.decode("utf-8"))["source_location"] == str(location)
    assert str(location).encode("utf-8") not in raw


def test_manifest_digest_is_ccj1_and_ignores_only_key_order() -> None:
    first = _manifest()
    second = {
        "skills": first["skills"],
        "sources": first["sources"],
        "agents": first["agents"],
        "schema_version": first["schema_version"],
    }
    expected = "sha256:" + hashlib.sha256(protocol_json.canonical_bytes(first)).hexdigest()
    assert source_lock.manifest_sha256(first) == expected
    assert source_lock.manifest_sha256(first) == source_lock.manifest_sha256(second)


def test_create_lock_sorts_utf8_names_and_assigns_root_selection_indices() -> None:
    members = (
        _local_member("zulu", selection=None),
        _local_member("alpha", selection=1),
        _local_member("beta", selection=0),
    )
    lock = _lock_payload(members=members)
    assert [member.name for member in lock.members] == ["alpha", "beta", "zulu"]
    assert [member.selection for member in lock.members] == [1, 0, None]
    assert [member.name for member in lock.members] == sorted(
        (member.name for member in members), key=lambda name: name.encode("utf-8")
    )


def test_utf8_order_key_covers_non_ascii_nfc_nfd_case_and_shared_prefix_families() -> None:
    # The schema's identifier definition is ASCII-only, so these values are
    # exercised at the production ordering seam and separately refused by the
    # lock shape gate.  The ordering seam itself must not fall back to locale
    # or case-folded order.
    names = ["é", "e\u0301", "a", "A", "a-", "a_", "aa", "Ω", "z", "Z"]
    expected = sorted(names, key=lambda name: name.encode("utf-8"))
    assert source_lock.sort_member_names_by_utf8(names) == expected
    assert unicodedata.normalize("NFC", "e\u0301") == "é"
    assert expected == sorted(names)
    assert expected != sorted(names, key=str.casefold)
    locale_orders: list[list[str]] = []
    original_locale = locale.setlocale(locale.LC_COLLATE)
    try:
        for locale_name in ("en_US.UTF-8", "de_DE.UTF-8", "sv_SE.UTF-8"):
            try:
                locale.setlocale(locale.LC_COLLATE, locale_name)
            except locale.Error:
                continue
            locale_orders.append(sorted(names, key=locale.strxfrm))
    finally:
        locale.setlocale(locale.LC_COLLATE, original_locale)
    if locale_orders:
        assert any(order != expected for order in locale_orders)
    with pytest.raises(source_errors.SourceError):
        _local_member("é")


def test_lock_digests_recompute_and_read_write_is_byte_identical() -> None:
    lock = _lock_payload()
    raw = source_lock.serialize_lock(lock)
    decoded = protocol_json.loads(raw)
    assert decoded["manifest_sha256"] == source_lock.manifest_sha256(_manifest())
    assert decoded["lock_sha256"] == source_lock.compute_lock_sha256(lock)
    reread = source_lock.read_lock(raw, current_manifest=_manifest(), resolved_members=["review"])
    assert source_lock.serialize_lock(reread) == raw
    assert raw == protocol_json.canonical_bytes(decoded)


def test_configured_git_is_strict_on_creation_but_tolerant_on_read() -> None:
    lock = _lock_payload(members=(_configured_member(directory="legacy/review"),))
    assert lock.members[0].directory == "."

    external = _payload_with_valid_digest(
        source_lock.SkillfileLock(
            manifest_sha256=lock.manifest_sha256,
            members=(_configured_member(directory="agents/skills/review"),),
            lock_sha256=None,
        )
    )
    external["lock_sha256"] = "sha256:" + hashlib.sha256(
        protocol_json.canonical_bytes({key: value for key, value in external.items() if key != "lock_sha256"})
    ).hexdigest()
    raw = protocol_json.canonical_bytes(external)
    parsed = source_lock.read_lock(raw, current_manifest=_manifest())
    assert parsed.members[0].directory == "agents/skills/review"
    assert source_lock.serialize_lock(parsed) == raw


def test_network_git_directory_mismatch_is_refused_by_full_reader() -> None:
    lock = _lock_payload(members=(_network_member(directory="skills/review"),))
    payload = lock.to_json()
    payload["members"][0]["directory"] = "."
    payload["lock_sha256"] = "sha256:" + hashlib.sha256(
        protocol_json.canonical_bytes({key: value for key, value in payload.items() if key != "lock_sha256"})
    ).hexdigest()
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.read_lock(protocol_json.canonical_bytes(payload))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda value: value["members"].append(
                {**value["members"][0], "selection": None}
            ),
            source_errors.CODE_NAME_CONFLICT,
        ),
        (lambda value: value["members"].reverse(), source_errors.CODE_MEMBER_INVALID),
    ],
)
def test_duplicate_or_unsorted_members_fail_before_digest_validation(mutate, expected_code: str) -> None:
    lock = _lock_payload(members=(_local_member("alpha", 0), _local_member("beta", None)))
    payload = lock.to_json()
    mutate(payload)
    payload["lock_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.read_lock(json.dumps(payload))
    assert excinfo.value.code == expected_code


@pytest.mark.parametrize(
    "names",
    [
        ["beta", "alpha"],
        ["beta", "gamma", "alpha"],
        ["alpha", "gamma", "beta"],
    ],
    ids=["two-reversal", "three-rotation", "three-tail-swap"],
)
def test_unsorted_members_are_refused_even_with_a_matching_digest(names: list[str]) -> None:
    members = tuple(
        _local_member(name, selection=0 if index == 0 else None)
        for index, name in enumerate(names)
    )
    lock = source_lock.SkillfileLock(
        manifest_sha256=source_lock.manifest_sha256(_manifest()),
        members=members,
        lock_sha256=None,
    )
    payload = _payload_with_manual_digest(lock.to_json())
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.read_lock(protocol_json.canonical_bytes(payload))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_exact_duplicate_pair_is_refused_as_name_conflict() -> None:
    members = (_local_member("alpha", 0), _local_member("alpha", None))
    lock = source_lock.SkillfileLock(
        manifest_sha256=source_lock.manifest_sha256(_manifest()),
        members=members,
        lock_sha256=None,
    )
    payload = _payload_with_manual_digest(lock.to_json())
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.read_lock(protocol_json.canonical_bytes(payload))
    assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT


def test_wrong_lock_digest_including_itself_is_refused() -> None:
    lock = _lock_payload()
    payload = lock.to_json()
    payload["lock_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.read_lock(protocol_json.canonical_bytes(payload))
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_malformed_json_never_reaches_digest_computation(monkeypatch) -> None:
    called = False

    def fail_if_called(value: object) -> bytes:
        nonlocal called
        called = True
        raise AssertionError("digest computation reached malformed JSON")

    monkeypatch.setattr(source_lock.protocol_json, "canonical_bytes", fail_if_called)
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.read_lock(b"{ malformed")
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID
    assert called is False


def test_json_reader_fault_at_production_callsite_is_structured(monkeypatch) -> None:
    lock = _lock_payload()
    raw = source_lock.serialize_lock(lock)
    assert source_lock.read_lock(raw).members == lock.members
    called = False

    def fail_at_reader(value: object) -> Any:
        nonlocal called
        called = True
        raise PermissionError("injected protocol reader failure")

    monkeypatch.setattr(source_lock.protocol_json, "loads", fail_at_reader)
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.read_lock(raw)
    assert called is True
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2},
        {"schema_version": 1, "manifest_sha256": SNAPSHOT, "members": [], "lock_sha256": SNAPSHOT, "extra": True},
        {"schema_version": 1, "manifest_sha256": "NOT-A-DIGEST", "members": [], "lock_sha256": SNAPSHOT},
        {"schema_version": 1, "manifest_sha256": SNAPSHOT, "members": [], "lock_sha256": "0" * 64},
        {"schema_version": True},
        {"schema_version": 1, "manifest_sha256": SNAPSHOT, "members": {}, "lock_sha256": SNAPSHOT},
    ],
)
def test_wrong_schema_and_unknown_members_fail_closed(payload: dict[str, Any]) -> None:
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.read_lock(json.dumps(payload))
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


@pytest.mark.parametrize(
    "members",
    [
        ("alpha", "beta"),
        ("alpha",),
    ],
    ids=["duplicate-index", "gap-index"],
)
def test_non_dense_root_selections_are_refused(members: tuple[str, ...]) -> None:
    if len(members) == 2:
        pair = (_local_member("alpha", 0), _local_member("beta", 0))
    else:
        pair = (_local_member("alpha", 1),)
    lock = source_lock.SkillfileLock(
        manifest_sha256=source_lock.manifest_sha256(_manifest()),
        members=pair,
        lock_sha256=None,
    )
    payload = _payload_with_manual_digest(lock.to_json())
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.read_lock(protocol_json.canonical_bytes(payload))
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_parse_lock_is_tolerant_by_default_and_strict_on_request() -> None:
    # F3 decision: the whole read surface is tolerant; strict configured-git
    # directory agreement is an explicit opt-in for write-path validation.
    member = _configured_member("review", 0, directory="agents/skills/review")
    lock = source_lock.SkillfileLock(
        manifest_sha256=source_lock.manifest_sha256(_manifest()),
        members=(member,),
        lock_sha256=None,
    )
    payload = _payload_with_manual_digest(lock.to_json())
    blob = protocol_json.canonical_bytes(payload)
    decoded = protocol_json.loads(blob)
    assert source_lock.parse_lock(decoded).members[0].directory == "agents/skills/review"
    assert source_lock.read_lock(blob).members[0].directory == "agents/skills/review"
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.parse_lock(decoded, strict=True)
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_membership_refuses_record_divergence_and_duplicate_input() -> None:
    lock = _lock_payload()
    raw = source_lock.serialize_lock(lock)
    diverged = source_lock.LockMember(
        name="review",
        selection=0,
        directory=".",
        package=package_identity.LocalSnapshot(SNAPSHOT),
        content_sha256="sha256:" + "9" * 64,
    )
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.read_lock(raw, resolved_members=[diverged])
    assert excinfo.value.code == source_errors.CODE_MEMBER_MISSING
    with pytest.raises(source_errors.SourceError) as duplicate:
        source_lock.read_lock(raw, resolved_members=["review", "review"])
    assert duplicate.value.code == source_errors.CODE_NAME_CONFLICT


def test_machine_binding_refuses_malformed_shape_duplicate_inputs_and_relative_location(
    tmp_path: Path,
) -> None:
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.parse_machine_private_binding(
            {"schema_version": 1, "source_location": "relative/path", "root_inputs": []}
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID
    with pytest.raises(source_errors.SourceError) as duplicate:
        source_lock.parse_machine_private_binding(
            {
                "schema_version": 1,
                "source_location": str(tmp_path / "source"),
                "root_inputs": ["SKILL.md", "SKILL.md"],
            }
        )
    assert duplicate.value.code == source_errors.CODE_NAME_CONFLICT
    with pytest.raises(source_errors.SourceError) as malformed:
        source_lock.read_machine_private_binding(b"{ malformed")
    assert malformed.value.code == source_errors.CODE_SELECTION_INVALID


def test_machine_binding_drive_relative_location_refusal_is_platform_native() -> None:
    # "/canonical/source" is absolute under POSIX semantics but drive-relative
    # under Windows semantics, where pathlib requires a drive letter plus a
    # root for absoluteness. The binding validates the location with the
    # platform-native pathlib before it checks root_inputs, so this document
    # stops at the location gate on Windows and reaches the duplicate-inputs
    # gate on POSIX. (ntpath.isabs is deliberately not the oracle here: it
    # disagrees with pathlib on 3.12 and changed across versions.)
    assert PureWindowsPath("/canonical/source").is_absolute() is False
    assert PurePosixPath("/canonical/source").is_absolute() is True
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.parse_machine_private_binding(
            {
                "schema_version": 1,
                "source_location": "/canonical/source",
                "root_inputs": ["SKILL.md", "SKILL.md"],
            }
        )
    if os.name == "nt":
        assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID
    else:
        assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT


def test_stale_manifest_and_membership_mismatch_have_typed_refusals() -> None:
    lock = _lock_payload()
    raw = source_lock.serialize_lock(lock)
    with pytest.raises(source_errors.SourceError) as stale:
        source_lock.read_lock(raw, current_manifest={**_manifest(), "locale": "en-US"})
    assert stale.value.code == source_errors.CODE_LOCK_STALE

    with pytest.raises(source_errors.SourceError) as membership:
        source_lock.read_lock(raw, resolved_members=["other"])
    assert membership.value.code == source_errors.CODE_MEMBER_MISSING


def test_schema_reader_drives_schema_shape_without_using_synthetic_digests() -> None:
    payload = {
        "schema_version": 1,
        "manifest_sha256": SNAPSHOT,
        "members": [
            {
                "name": "review",
                "selection": 0,
                "directory": "agents/skills/review",
                "package": {
                    "kind": "network-git",
                    "repository": "example.org/kit",
                    "commit": {"object_format": "sha1", "hex": "0" * 40},
                    "directory": ".",
                },
                "content_sha256": CONTENT,
            }
        ],
        "lock_sha256": "sha256:" + "0" * 64,
    }
    parsed = source_lock.read_lock_schema(json.dumps(payload))
    assert parsed.members[0].name == "review"
    with pytest.raises(source_errors.SourceError):
        source_lock.read_lock(protocol_json.canonical_bytes(payload), verify_digests=True)


def test_source_types_reject_wrong_union_shapes_and_commit_lengths() -> None:
    with pytest.raises(source_errors.SourceError):
        package_identity.parse_package_identity(
            {"kind": "local-snapshot", "snapshot": SNAPSHOT, "commit": SHA1}
        )
    with pytest.raises(source_errors.SourceError):
        package_identity.parse_package_identity(
            {
                "kind": "network-git",
                "repository": "example.org/kit",
                "commit": {"object_format": "sha1", "hex": "0" * 64},
                "directory": ".",
            }
        )
    with pytest.raises(source_errors.SourceError):
        package_identity.ConfiguredGit(source=".", commit=_commit())


@pytest.mark.parametrize("fixture_name", ["local", "network", "hand-authored"])
def test_committed_fixture_recomputes_digests_and_round_trips_exactly(fixture_name: str) -> None:
    fixture = Path(__file__).parent / "fixtures" / "skillfile-v2" / fixture_name
    manifest = json.loads((fixture / "Skillfile.json").read_bytes())
    lock_raw = (fixture / "Skillfile.lock.json").read_bytes()
    parsed = source_lock.read_lock(lock_raw, current_manifest=manifest)
    assert source_lock.compute_lock_sha256(parsed) == json.loads(lock_raw)["lock_sha256"]
    assert source_lock.manifest_sha256(manifest) == json.loads(lock_raw)["manifest_sha256"]
    assert source_lock.serialize_lock(parsed) == lock_raw


def test_committed_fixture_locks_validate_against_pinned_json_schema() -> None:
    suite_text = os.environ.get("CSK_DRAFT_SOURCES_SUITE_ROOT")
    if not suite_text:
        pytest.skip("CSK_DRAFT_SOURCES_SUITE_ROOT is not set")
    suite_root = Path(suite_text)
    repository_root = suite_root.parent.parent
    schema_path = repository_root / "schemas" / "skillfile-sources-v1" / "skillfile-lock-v1.schema.json"
    if not schema_path.is_file():
        pytest.skip(f"pinned skillfile-lock schema is not available at {schema_path}")
    schema = json.loads(schema_path.read_bytes())
    resources: list[tuple[str, Resource[Any]]] = []
    for directory in (repository_root / "schemas" / "v1", repository_root / "schemas" / "skillfile-sources-v1"):
        for path in directory.glob("*.json"):
            document = json.loads(path.read_bytes())
            resources.append((document["$id"], Resource.from_contents(document)))
    validator = Draft202012Validator(schema, registry=Registry().with_resources(resources))
    fixture_root = Path(__file__).parent / "fixtures" / "skillfile-v2"
    for fixture_name in ("local", "network", "hand-authored"):
        value = json.loads((fixture_root / fixture_name / "Skillfile.lock.json").read_bytes())
        assert validator.is_valid(value), fixture_name


def test_machine_private_binding_round_trips_without_becoming_a_lock_field(tmp_path: Path) -> None:
    binding = source_lock.MachinePrivateBinding(
        source_location=tmp_path / "canonical" / "source",
        root_inputs=("SKILL.md", "build/input.txt"),
    )
    raw = source_lock.serialize_machine_private_binding(binding)
    assert source_lock.read_machine_private_binding(raw) == binding
    assert set(protocol_json.loads(raw)) == {"schema_version", "source_location", "root_inputs"}


@pytest.mark.parametrize(
    "spelling",
    ["/abs/legacy", "/abs/pkg", "C:/team/kit", "C:\\team\\kit", "\\\\host\\share", "/"],
    ids=["posix-legacy", "posix-pkg", "drive-slash", "drive-backslash", "unc", "root"],
)
@pytest.mark.parametrize(
    "target",
    ["member-directory", "network-git-directory", "configured-git-source"],
)
def test_absolute_paths_are_refused_in_every_lock_directory_field(
    target: str, spelling: str
) -> None:
    member: dict[str, Any] = {
        "name": "review",
        "selection": 0,
        "directory": ".",
        "package": {"kind": "local-snapshot", "snapshot": SNAPSHOT},
        "content_sha256": CONTENT,
    }
    if target == "member-directory":
        member["directory"] = spelling
    elif target == "network-git-directory":
        member["directory"] = "skills/review"
        member["package"] = {
            "kind": "network-git",
            "repository": "example.org/kit",
            "commit": {"object_format": "sha1", "hex": SHA1},
            "directory": spelling,
        }
    else:
        member["package"] = {
            "kind": "configured-git",
            "source": spelling,
            "commit": {"object_format": "sha1", "hex": SHA1},
            "directory": ".",
        }
    manifest = _manifest()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "manifest_sha256": source_lock.manifest_sha256(manifest),
        "members": [member],
        "lock_sha256": "sha256:" + "0" * 64,
    }
    # The digest is correct for the absolute-path document, so a reader that
    # admits the spelling would accept the lock instead of refusing it.
    payload = _payload_with_manual_digest(payload)
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.read_lock(
            protocol_json.canonical_bytes(payload), current_manifest=manifest
        )
    assert excinfo.value.code == source_errors.CODE_SELECTION_INVALID


def test_stale_manifest_differing_in_final_character_is_refused() -> None:
    lock = _lock_payload()
    raw = source_lock.serialize_lock(lock)
    stored = lock.manifest_sha256
    assert stored is not None
    flipped = stored[:-1] + ("0" if stored[-1] != "0" else "1")
    assert flipped != stored
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.read_lock(raw, current_manifest_sha256=flipped)
    assert excinfo.value.code == source_errors.CODE_LOCK_STALE


def test_validation_order_is_structural_schema_semantic_digest_stale_membership() -> None:
    manifest = _manifest()
    lock = _lock_payload(members=(_local_member("alpha", 0), _local_member("beta", None)))

    def read(payload: dict[str, Any], **kwargs: Any) -> source_errors.SourceError:
        with pytest.raises(source_errors.SourceError) as excinfo:
            source_lock.read_lock(protocol_json.canonical_bytes(payload), **kwargs)
        return excinfo.value

    # Malformed JSON never reaches any later phase.
    with pytest.raises(source_errors.SourceError) as malformed:
        source_lock.read_lock(
            b"{ malformed",
            current_manifest={**manifest, "locale": "en-US"},
            resolved_members=["other"],
        )
    assert malformed.value.code == source_errors.CODE_SELECTION_INVALID

    # Schema shape beats cross-field semantics: unknown top-level field plus
    # duplicate members is a shape refusal, not a name conflict.
    shape_payload = lock.to_json()
    shape_payload["members"].append({**shape_payload["members"][0]})
    shape_payload["extra"] = True
    assert read(shape_payload).code == source_errors.CODE_SELECTION_INVALID

    # Semantics beats digests: duplicate members plus a wrong digest is still
    # a name conflict.
    semantic_payload = lock.to_json()
    semantic_payload["members"].append({**semantic_payload["members"][0]})
    semantic_payload["lock_sha256"] = "sha256:" + "0" * 64
    assert read(semantic_payload).code == source_errors.CODE_NAME_CONFLICT

    # Digests beat staleness and membership.
    digest_payload = lock.to_json()
    digest_payload["lock_sha256"] = "sha256:" + "0" * 64
    assert (
        read(
            digest_payload,
            current_manifest={**manifest, "locale": "en-US"},
            resolved_members=["other"],
        ).code
        == source_errors.CODE_SELECTION_INVALID
    )

    # Staleness beats membership on a digest-valid document.
    stale_payload = lock.to_json()
    assert (
        read(
            stale_payload,
            current_manifest={**manifest, "locale": "en-US"},
            resolved_members=["other"],
        ).code
        == source_errors.CODE_LOCK_STALE
    )


def test_lock_digest_preimage_is_exactly_the_lock_without_itself() -> None:
    lock = _lock_payload(
        members=(
            _local_member("alpha", 0),
            _network_member("beta", None),
            _configured_member("gamma", None),
        )
    )
    stored = protocol_json.loads(source_lock.serialize_lock(lock))
    assert set(stored) == {"schema_version", "manifest_sha256", "members", "lock_sha256"}
    preimage = {key: value for key, value in stored.items() if key != "lock_sha256"}
    assert set(preimage) == {"schema_version", "manifest_sha256", "members"}
    expected = "sha256:" + hashlib.sha256(
        protocol_json.canonical_bytes(preimage)
    ).hexdigest()
    assert stored["lock_sha256"] == expected
    assert source_lock.compute_lock_sha256(lock) == expected


def test_identity_is_a_function_of_raw_bytes_over_generated_manifests() -> None:
    rng = random.Random(260916)
    atoms = ["a", "review", "é", "Ω", "e\u0301", "", "x" * 200, "0", "-1", "true", "null"]
    assert unicodedata.normalize("NFC", "e\u0301") == "é"
    for _ in range(64):
        manifest = _generated_json(rng, atoms, 0)
        if not isinstance(manifest, dict):
            manifest = {"value": manifest}
        expected = "sha256:" + hashlib.sha256(
            protocol_json.canonical_bytes(manifest)
        ).hexdigest()
        assert source_lock.manifest_sha256(manifest) == expected
        assert source_lock.manifest_sha256(_reversed_json(manifest)) == expected
        mutated = dict(manifest)
        mutated["zz-new-key"] = 1
        assert source_lock.manifest_sha256(mutated) != expected


def test_pinned_valid_lock_digest_recomputes_exactly() -> None:
    suite_text = os.environ.get("CSK_DRAFT_SOURCES_SUITE_ROOT")
    if not suite_text:
        pytest.skip("CSK_DRAFT_SOURCES_SUITE_ROOT is not set")
    cases = Path(suite_text) / "schema-cases" / "skillfile-lock-v1"
    valid = json.loads((cases / "valid.json").read_bytes())
    parsed = source_lock.read_lock_schema((cases / "valid.json").read_bytes())
    assert source_lock.compute_lock_sha256(parsed) == valid["lock_sha256"]
    # The other two valid cases reuse valid.json's digest verbatim in the
    # corpus, so they are schema-validity fixtures, not digest vectors.
    for name in ("valid-git.json", "valid-configured-git.json"):
        other = json.loads((cases / name).read_bytes())
        reparsed = source_lock.read_lock_schema((cases / name).read_bytes())
        assert other["lock_sha256"] == valid["lock_sha256"]
        assert source_lock.compute_lock_sha256(reparsed) != other["lock_sha256"]


def test_lock_operations_emit_no_filesystem_or_network_audit_events(tmp_path: Path) -> None:
    manifest = _manifest()
    members = (_local_member("alpha", 0), _network_member("beta", None))
    lock = source_lock.create_lock(manifest, members)
    raw = source_lock.serialize_lock(lock)
    binding = source_lock.MachinePrivateBinding(
        source_location=tmp_path / "source",
        root_inputs=("SKILL.md",),
    )
    binding_raw = source_lock.serialize_machine_private_binding(binding)
    decoded = protocol_json.loads(raw)
    # Warm up every lazy import before the hook is installed.
    source_lock.read_lock(raw)

    events: list[str] = []
    state = {"armed": True}

    def hook(event: str, args: object) -> None:
        if state["armed"] and (event == "open" or event.startswith("socket.")):
            events.append(event)

    sys.addaudithook(hook)
    try:
        source_lock.create_lock(manifest, members)
        source_lock.serialize_lock(lock)
        source_lock.read_lock(
            raw, current_manifest=manifest, resolved_members=["alpha", "beta"]
        )
        source_lock.parse_lock(decoded)
        source_lock.compute_lock_sha256(lock)
        source_lock.manifest_sha256(manifest)
        package_identity.parse_package_identity(
            package_identity.package_identity_to_json(members[0].package)
        )
        source_lock.read_machine_private_binding(binding_raw)
        source_lock.serialize_machine_private_binding(binding)
    finally:
        state["armed"] = False
    assert events == []


# --- F1 CLASS: the hostile str family, generated across all five gates -------
#
# Every liar below carries an HONEST value at construction and lies through a
# different overridable method. Each gate must decide from the honest value.
# A cell passes only when the outcome equals the honest outcome: order emits
# UTF-8 byte order, duplicates conflict, absolute directories refuse, foreign
# membership is missing, and foreign digests are stale.


class _EncodeLiar(str):
    """encode() returns the bytes of a fixed decoy name (reviewer A2.1)."""

    def encode(self, encoding: str = "utf-8", errors: str = "strict"):  # type: ignore[override]
        return b"zzz-decoy"


class _UnequalLiar(str):
    """Claims to be unequal to everything, with a unique hash."""

    def __eq__(self, other: object) -> bool:
        return False

    def __ne__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return id(self)


class _EqualLiar(str):
    """Claims to equal any string, with the hash of a fixed target."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, str)

    def __ne__(self, other: object) -> bool:
        return not isinstance(other, str)

    def __hash__(self) -> int:
        return hash("alpha")


class _DotLiar(str):
    """Claims to equal '.' whatever the content (reviewer A2.3 shape)."""

    def __eq__(self, other: object) -> bool:
        return other == "." or str.__str__(self) == other

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return str.__hash__(str.__str__(self))


class _LtLiar(str):
    """Claims to sort before everything (reviewer A2b.2 shape)."""

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        return True

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        return False


class _SmuggledSub(str):
    """A second subclass used only as a str() smuggling payload."""


class _ConstStrLiar(str):
    """__str__ answers a constant wrong value."""

    def __str__(self) -> str:
        return "DIFFERENT"


class _SmuggleStrLiar(str):
    """__str__ smuggles a still-hostile subclass instance out of str()."""

    def __str__(self):  # type: ignore[override]
        return _SmuggledSub("smuggled")


class _FlipStrLiar(str):
    """__str__ answers honestly once, then evil (stateful flip)."""

    def __init__(self, value: str) -> None:
        self._str_calls = 0

    def __str__(self) -> str:
        self._str_calls += 1
        if self._str_calls == 1:
            return str.__str__(self)
        return "evil-after-first"


class _NonStrDot:
    """A non-str whose __eq__ claims everything (reviewer A2b.1 shape)."""

    def __init__(self, value: object) -> None:
        self._value = value

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    def __hash__(self) -> int:
        return 0

    def __repr__(self) -> str:
        return f"_NonStrDot({self._value!r})"


class _BenignSub(str):
    """A subclass with no overrides: must normalize by value, not refuse."""


_LIARS: dict[str, Any] = {
    "lying-encode": _EncodeLiar,
    "always-unequal": _UnequalLiar,
    "always-equal": _EqualLiar,
    "dot-eq": _DotLiar,
    "lt-liar": _LtLiar,
    "str-constant": _ConstStrLiar,
    "str-smuggling": _SmuggleStrLiar,
    "str-stateful": _FlipStrLiar,
    "non-str-dot": lambda honest: _NonStrDot(honest),
    "benign-subclass": _BenignSub,
}
_NON_STR_LIARS = frozenset({"non-str-dot"})
_CLASS_GATES = (
    "order",
    "duplicate",
    "directory-abs",
    "directory-dot",
    "membership",
    "stale",
    "manifest",
)


@pytest.mark.parametrize("liar_name", sorted(_LIARS))
@pytest.mark.parametrize("gate", _CLASS_GATES)
def test_str_subclass_family_is_canonicalized_at_every_gate(liar_name: str, gate: str) -> None:
    wrap = _LIARS[liar_name]
    non_str = liar_name in _NON_STR_LIARS
    manifest = _manifest()
    if gate == "order":
        if non_str:
            with pytest.raises(source_errors.SourceError):
                _local_member(wrap("beta"), selection=None)
            return
        first = _local_member(wrap("beta"), selection=None)
        second = _local_member(wrap("alpha"), selection=0)
        assert type(first.name) is str and type(second.name) is str
        lock = source_lock.create_lock(manifest, (first, second))
        assert [member.name for member in lock.members] == ["alpha", "beta"]
        wire = protocol_json.loads(source_lock.serialize_lock(lock))
        assert [member["name"] for member in wire["members"]] == ["alpha", "beta"]
    elif gate == "duplicate":
        if non_str:
            with pytest.raises(source_errors.SourceError) as excinfo:
                _local_member(wrap("alpha"))
            assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
            return
        first = _local_member(wrap("alpha"), selection=0)
        second = _local_member(wrap("alpha"), selection=None)
        with pytest.raises(source_errors.SourceError) as excinfo:
            source_lock.create_lock(manifest, (first, second))
        assert excinfo.value.code == source_errors.CODE_NAME_CONFLICT
    elif gate == "directory-abs":
        with pytest.raises(source_errors.SourceError) as excinfo:
            _configured_member("review", 0, directory=wrap("/abs/smuggled"))
        assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    elif gate == "directory-dot":
        if non_str:
            with pytest.raises(source_errors.SourceError):
                _configured_member("review", 0, directory=wrap("."))
            return
        member = _configured_member("review", 0, directory=wrap("."))
        assert member.directory == "." and type(member.directory) is str
    elif gate == "membership":
        lock = _lock_payload(members=(_local_member("alpha", 0),))
        raw = source_lock.serialize_lock(lock)
        with pytest.raises(source_errors.SourceError) as excinfo:
            source_lock.read_lock(raw, resolved_members=[wrap("other")])
        assert excinfo.value.code == (
            source_errors.CODE_MEMBER_INVALID if non_str else source_errors.CODE_MEMBER_MISSING
        )
    elif gate == "stale":
        lock = _lock_payload(members=(_local_member("alpha", 0),))
        raw = source_lock.serialize_lock(lock)
        other = "sha256:" + "9" * 64
        assert other != lock.manifest_sha256
        with pytest.raises(source_errors.SourceError) as excinfo:
            source_lock.read_lock(raw, current_manifest_sha256=wrap(other))
        assert excinfo.value.code == (
            source_errors.CODE_SELECTION_INVALID if non_str else source_errors.CODE_LOCK_STALE
        )
    elif gate == "manifest":
        if non_str:
            with pytest.raises(source_errors.SourceError):
                source_lock.manifest_sha256({wrap("zzz"): 1, "aaa": 2})
            return
        honest: dict[str, Any] = {"zzz": 1, "aaa": 2, "nested": {"b": "x", "a": "y"}}
        evil: dict[str, Any] = {
            wrap("zzz"): 1,
            "aaa": 2,
            "nested": {"b": wrap("x"), "a": "y"},
        }
        # Incidental immunity via the C sorter/encoder: pinned, not relied on.
        assert source_lock.manifest_sha256(evil) == source_lock.manifest_sha256(honest)
    else:  # pragma: no cover - the gate list is fixed above
        raise AssertionError(f"unknown gate {gate}")


def test_ordering_seam_canonicalizes_direct_liar_input() -> None:
    liars: list[object] = [
        _EncodeLiar("beta"),
        _UnequalLiar("gamma"),
        _EqualLiar("delta"),
        _DotLiar("epsilon"),
        _SmuggleStrLiar("zeta"),
        _FlipStrLiar("eta"),
    ]
    ordered = source_lock.sort_member_names_by_utf8(liars)  # type: ignore[arg-type]
    assert [str.__str__(name) for name in ordered] == [
        "beta",
        "delta",
        "epsilon",
        "eta",
        "gamma",
        "zeta",
    ]
    assert source_lock._utf8_key(_EncodeLiar("alpha")) == b"alpha"


def test_exact_str_bypasses_overridden_dunder_but_str_does_not() -> None:
    assert str(_ConstStrLiar("abc")) == "DIFFERENT"
    smuggled = str(_SmuggleStrLiar("x"))
    assert smuggled == "smuggled" and type(smuggled) is not str
    flip = _FlipStrLiar("real")
    assert (str(flip), str(flip)) == ("real", "evil-after-first")
    for liar_name in sorted(_LIARS):
        if liar_name in _NON_STR_LIARS:
            continue
        liar = _LIARS[liar_name]("abc")
        first = package_identity.exact_str(liar)
        second = package_identity.exact_str(liar)
        assert type(first) is str and first == "abc"
        assert type(second) is str and second == "abc"
    assert package_identity.exact_str(5) == 5
    assert package_identity.exact_str(None) is None


class _EvilInt(int):
    """Reviewer A2b.1 shape: a non-str int whose __eq__ claims everything."""

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    def __hash__(self) -> int:
        return 0


@pytest.mark.parametrize(
    "evil",
    [_EvilInt(5), _NonStrDot("."), None, b".", True, 5],
    ids=["evil-int", "fake-dot", "none", "bytes", "bool", "int"],
)
def test_dot_shortcut_refuses_every_non_str_shape(evil: object) -> None:
    assert package_identity.is_valid_identity_directory(evil) is False
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.LockMember(
            name="alpha",
            selection=0,
            directory=evil,  # type: ignore[arg-type]
            package=package_identity.LocalSnapshot(SNAPSHOT),
            content_sha256=CONTENT,
        )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


@pytest.mark.parametrize(
    ("make_resolved", "expect_code"),
    [
        (lambda diverged: [diverged[0], "beta"], source_errors.CODE_MEMBER_MISSING),
        (lambda diverged: ["alpha", diverged[1]], source_errors.CODE_MEMBER_MISSING),
        (lambda diverged: [diverged[0], diverged[1]], source_errors.CODE_MEMBER_MISSING),
        (lambda diverged: [diverged[0], _local_member("beta", None)], source_errors.CODE_MEMBER_MISSING),
        (lambda diverged: [_local_member("alpha", 0), "beta"], None),
        (lambda diverged: ["alpha", _local_member("beta", None)], None),
    ],
    ids=[
        "diverged-first",
        "diverged-last",
        "all-diverged",
        "homogeneous-diverged",
        "matching-mixed-first",
        "matching-mixed-last",
    ],
)
def test_mixed_membership_compares_each_record(make_resolved: Any, expect_code: str | None) -> None:
    lock = _lock_payload(members=(_local_member("alpha", 0), _local_member("beta", None)))
    raw = source_lock.serialize_lock(lock)
    diverged = (
        source_lock.LockMember(
            name="alpha",
            selection=0,
            directory=".",
            package=package_identity.LocalSnapshot(SNAPSHOT),
            content_sha256="sha256:" + "9" * 64,
        ),
        source_lock.LockMember(
            name="beta",
            selection=None,
            directory=".",
            package=package_identity.LocalSnapshot(SNAPSHOT),
            content_sha256="sha256:" + "8" * 64,
        ),
    )
    resolved = make_resolved(diverged)
    if expect_code is None:
        source_lock.read_lock(raw, resolved_members=resolved)
    else:
        with pytest.raises(source_errors.SourceError) as excinfo:
            source_lock.read_lock(raw, resolved_members=resolved)
        assert excinfo.value.code == expect_code


@pytest.mark.parametrize(
    "bad_members",
    [
        ("x",),
        (None,),
        (123,),
        ({"name": "alpha"},),
        (_local_member("alpha", 0), "beta"),
        (b"alpha",),
        123,
        None,
    ],
    ids=["str", "none", "int", "dict", "mixed", "bytes", "non-iterable", "null"],
)
def test_lock_constructor_refuses_non_member_elements(bad_members: Any) -> None:
    with pytest.raises(source_errors.SourceError) as excinfo:
        source_lock.SkillfileLock(
            manifest_sha256=source_lock.manifest_sha256(_manifest()),
            members=bad_members,
            lock_sha256=None,
        )
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
