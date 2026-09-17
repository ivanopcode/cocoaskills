"""Pure local-snapshot-v1 inventory and digest coverage."""

from __future__ import annotations

import ast
import hashlib
import json
import locale
import random
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from csk import protocol_json
from csk.sources import local_snapshot
from csk.sources.errors import (
    CODE_INVENTORY_INVALID,
    CODE_PATH_CONFLICT,
    CODE_PATH_EQUIVALENCE_INVALID,
    SourceError,
    SourcePathConflictError,
)


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _identity(_left: str, _right: str) -> bool:
    return False


def _vector_entries(utf8_files: dict[str, str]) -> list[tuple[str, str, bool]]:
    # Reverse the caller order intentionally: traversal order is not an input
    # to the resulting identity.
    return [
        (path, _sha256(content.encode("utf-8")), path == "scripts/run.sh")
        for path, content in reversed(list(utf8_files.items()))
    ]


SNAPSHOT_VECTORS: tuple[dict[str, Any], ...] = (
    {
        "id": "base",
        "utf8_files": {
            "SKILL.md": "name: review\n",
            "scripts/run.sh": "echo one\n",
            "build/main.go": "package main\n",
        },
        "expected": "sha256:481f362d0c82cdf8c32ed854edfc125d21a7c470c0128086f7eebb1956d832a9",
    },
    {
        "id": "runtime-edit",
        "utf8_files": {
            "SKILL.md": "name: review\n",
            "scripts/run.sh": "echo two\n",
            "build/main.go": "package main\n",
        },
        "expected": "sha256:15ac234d51e71847165d42e2695df3d91ca49314bd5e5811eb17cad6ce8cea80",
    },
    {
        "id": "build-edit",
        "utf8_files": {
            "SKILL.md": "name: review\n",
            "scripts/run.sh": "echo one\n",
            "build/main.go": "package changed\n",
        },
        "expected": "sha256:2aa1bc2a835d6fd931de8f032761c17288bfefeaba492fe229158c602b8750ac",
    },
)


@pytest.mark.parametrize("vector", SNAPSHOT_VECTORS, ids=[item["id"] for item in SNAPSHOT_VECTORS])
def test_conformance_snapshot_vectors_use_the_production_function(vector: dict[str, Any]) -> None:
    inventory = local_snapshot.build_inventory(
        _vector_entries(vector["utf8_files"]), equivalent=_identity
    )

    assert inventory["snapshot"] == vector["expected"]
    assert local_snapshot.inventory_digest(inventory) == vector["expected"]
    assert inventory["files"] == [
        {
            "path": "SKILL.md",
            "sha256": _sha256(b"name: review\n"),
            "executable": False,
        },
        {
            "path": "build/main.go",
            "sha256": _sha256(vector["utf8_files"]["build/main.go"].encode("utf-8")),
            "executable": False,
        },
        {
            "path": "scripts/run.sh",
            "sha256": _sha256(vector["utf8_files"]["scripts/run.sh"].encode("utf-8")),
            "executable": True,
        },
    ]


def test_conformance_vectors_change_only_for_the_runtime_or_build_edit() -> None:
    inventories = {
        vector["id"]: local_snapshot.build_inventory(
            _vector_entries(vector["utf8_files"]), equivalent=_identity
        )
        for vector in SNAPSHOT_VECTORS
    }

    assert inventories["runtime-edit"]["files"][0] == inventories["build-edit"]["files"][0]
    assert inventories["runtime-edit"]["snapshot"] != inventories["base"]["snapshot"]
    assert inventories["build-edit"]["snapshot"] != inventories["base"]["snapshot"]
    assert inventories["runtime-edit"]["files"][1] == inventories["base"]["files"][1]
    assert inventories["build-edit"]["files"][2] == inventories["base"]["files"][2]


def test_empty_file_is_a_real_inventory_entry_and_execute_bit_is_not_normalized() -> None:
    empty = ("empty", _sha256(b""), False)
    executable = ("empty-executable", _sha256(b""), True)

    inventory = local_snapshot.build_inventory([empty, executable], equivalent=_identity)

    assert inventory["files"] == [
        {"path": "empty", "sha256": _sha256(b""), "executable": False},
        {"path": "empty-executable", "sha256": _sha256(b""), "executable": True},
    ]
    changed_bit = local_snapshot.build_inventory(
        [("empty", _sha256(b""), True)], equivalent=_identity
    )
    unchanged_bit = local_snapshot.build_inventory(
        [("empty", _sha256(b""), False)], equivalent=_identity
    )
    assert changed_bit["snapshot"] != unchanged_bit["snapshot"]


def test_raw_content_variants_remain_distinct_at_the_supplied_sha256_boundary() -> None:
    newline = local_snapshot.build_inventory(
        [("content", _sha256(b"line\n"), False)], equivalent=_identity
    )
    carriage_return_newline = local_snapshot.build_inventory(
        [("content", _sha256(b"line\r\n"), False)], equivalent=_identity
    )

    assert newline["files"][0]["sha256"] != carriage_return_newline["files"][0]["sha256"]
    assert newline["snapshot"] != carriage_return_newline["snapshot"]


def test_ordering_uses_utf8_bytes_and_keeps_nfc_nfd_and_case_spellings() -> None:
    nfc = "café"
    nfd = "cafe\u0301"
    paths = ["a/b", "z", nfc, "a", "A", "a-file", nfd, "ä"]
    inventory = local_snapshot.build_inventory(
        [(path, _sha256(path.encode("utf-8")), False) for path in reversed(paths)],
        equivalent=_identity,
    )

    actual = [entry["path"] for entry in inventory["files"]]
    assert actual == sorted(paths, key=lambda path: path.encode("utf-8"))
    assert actual.index(nfd) < actual.index(nfc)
    assert actual.index("A") < actual.index("a")
    assert actual.index("a") < actual.index("a/b")
    assert nfc != nfd


def _find_locale_disagreement(paths: list[str]) -> tuple[str, list[str], list[str]] | None:
    original = locale.setlocale(locale.LC_COLLATE)
    try:
        for candidate in (
            "en_US.UTF-8",
            "en_US.utf8",
            "sv_SE.UTF-8",
            "de_DE.UTF-8",
            "C.UTF-8",
            "C",
        ):
            try:
                locale.setlocale(locale.LC_COLLATE, candidate)
                collated = sorted(paths, key=locale.strxfrm)
            except (locale.Error, OSError, UnicodeError):
                continue
            byte_order = sorted(paths, key=lambda path: path.encode("utf-8"))
            if collated != byte_order:
                return candidate, byte_order, collated
    finally:
        locale.setlocale(locale.LC_COLLATE, original)
    return None


def test_ordering_disagrees_with_locale_collation_when_the_host_exposes_one() -> None:
    paths = ["z", "ä", "A", "a", "a/b"]
    disagreement = _find_locale_disagreement(paths)
    if disagreement is None:
        pytest.skip(
            "BOUND: no available host collation disagrees with UTF-8 byte order; "
            "the production key remains explicitly path.encode('utf-8')"
        )
    locale_name, byte_order, collated = disagreement

    original = locale.setlocale(locale.LC_COLLATE)
    try:
        locale.setlocale(locale.LC_COLLATE, locale_name)
        inventory = local_snapshot.build_inventory(
            [(path, _sha256(path.encode("utf-8")), False) for path in paths],
            equivalent=_identity,
        )
        assert [entry["path"] for entry in inventory["files"]] == byte_order
        assert collated != byte_order
        assert locale.strxfrm("z") != locale.strxfrm("ä")
    finally:
        locale.setlocale(locale.LC_COLLATE, original)


def test_trailing_separator_pair_is_rejected_by_the_portable_path_surface() -> None:
    with pytest.raises(SourceError) as excinfo:
        local_snapshot.build_inventory(
            [("a", _sha256(b"a"), False), ("a/", _sha256(b"a"), False)],
            equivalent=_identity,
        )

    assert excinfo.value.code == CODE_INVENTORY_INVALID
    assert "a/" in str(excinfo.value)


def test_duplicate_paths_raise_a_typed_error_naming_both_paths() -> None:
    with pytest.raises(SourcePathConflictError) as excinfo:
        local_snapshot.build_inventory(
            [
                ("same/path", _sha256(b"one"), False),
                ("same/path", _sha256(b"two"), True),
            ],
            equivalent=_identity,
        )

    error = excinfo.value
    assert isinstance(error, SourceError)
    assert error.code == CODE_PATH_CONFLICT
    assert error.first_path == "same/path"
    assert error.second_path == "same/path"
    assert "'same/path'" in str(error)


@pytest.mark.parametrize(
    ("left", "right"),
    [("Readme", "README"), ("café", "cafe\u0301")],
    ids=["case-equivalent", "nfc-nfd-equivalent"],
)
def test_injected_filesystem_equivalence_rejects_colliding_spellings(
    left: str, right: str
) -> None:
    calls: list[tuple[str, str]] = []

    def equivalent(first: str, second: str) -> bool:
        calls.append((first, second))
        return {first, second} == {left, right}

    with pytest.raises(SourcePathConflictError) as excinfo:
        local_snapshot.build_inventory(
            [(left, _sha256(b"left"), False), (right, _sha256(b"right"), False)],
            equivalent=equivalent,
        )

    error = excinfo.value
    assert {error.first_path, error.second_path} == {left, right}
    assert {calls[0][0], calls[0][1]} == {left, right}
    assert left in str(error) and right in str(error)


def test_equivalence_predicate_can_keep_distinct_nfc_and_nfd_paths() -> None:
    nfc = "café"
    nfd = "cafe\u0301"
    inventory = local_snapshot.build_inventory(
        [(nfc, _sha256(b"nfc"), False), (nfd, _sha256(b"nfd"), False)],
        equivalent=_identity,
    )

    assert [entry["path"] for entry in inventory["files"]] == [nfd, nfc]


def test_invalid_equivalence_result_is_a_structured_refusal() -> None:
    def invalid_equivalence(_left: str, _right: str) -> bool:
        return 1  # type: ignore[return-value]

    with pytest.raises(SourceError) as excinfo:
        local_snapshot.build_inventory(
            [("a", _sha256(b"a"), False), ("b", _sha256(b"b"), False)],
            equivalent=invalid_equivalence,
        )

    assert excinfo.value.code == CODE_PATH_EQUIVALENCE_INVALID


@pytest.mark.parametrize(
    "entry",
    [
        ("/absolute", _sha256(b"x"), False),
        ("has\\backslash", _sha256(b"x"), False),
        ("has/../parent", _sha256(b"x"), False),
        ("not-a-digest", "sha256:ABC", False),
        ("upper-digest", "sha256:" + "F" * 64, False),
        ("short-digest", "sha256:" + "ab" * 31, False),
        ("unprefixed-digest", "0" * 64, False),
        ("wrong-bit-one", _sha256(b"x"), 1),
        ("wrong-bit-zero", _sha256(b"x"), 0),
        ("wrong-bit-str", _sha256(b"x"), "true"),
        ("wrong-bit-none", _sha256(b"x"), None),
        ("\ud800", _sha256(b"x"), False),
    ],
    ids=[
        "absolute",
        "backslash",
        "parent",
        "bad-sha256",
        "uppercase-sha256",
        "short-sha256",
        "unprefixed-sha256",
        "non-bool-executable-one",
        "non-bool-executable-zero",
        "non-bool-executable-str",
        "non-bool-executable-none",
        "lone-surrogate-path",
    ],
)
def test_entry_contract_rejects_non_admitted_values(entry: tuple[object, object, object]) -> None:
    with pytest.raises(SourceError) as excinfo:
        local_snapshot.build_inventory([entry], equivalent=_identity)  # type: ignore[list-item]
    assert excinfo.value.code == CODE_INVENTORY_INVALID


@pytest.mark.parametrize(
    "entry",
    [
        "just-a-string",
        b"just-bytes",
        ("only", "two"),
        ("one", "two", "three", "four"),
        42,
        None,
    ],
    ids=["string", "bytes", "two-tuple", "four-tuple", "int", "none"],
)
def test_malformed_entry_shapes_are_structured_refusals(entry: object) -> None:
    with pytest.raises(SourceError) as excinfo:
        local_snapshot.build_inventory([entry], equivalent=_identity)  # type: ignore[list-item]
    assert excinfo.value.code == CODE_INVENTORY_INVALID


def test_non_callable_equivalence_is_a_structured_refusal() -> None:
    with pytest.raises(SourceError) as excinfo:
        local_snapshot.build_inventory([], equivalent=None)  # type: ignore[arg-type]
    assert excinfo.value.code == CODE_PATH_EQUIVALENCE_INVALID


def test_named_entry_objects_build_the_same_inventory_as_tuples() -> None:
    triples = [
        ("b-file", _sha256(b"b"), True),
        ("a-file", _sha256(b"a"), False),
    ]
    from_tuples = local_snapshot.build_inventory(triples, equivalent=_identity)
    from_named = local_snapshot.build_inventory(
        [local_snapshot.InventoryEntry(*triple) for triple in triples],
        equivalent=_identity,
    )

    assert from_named == from_tuples


def test_collision_naming_is_independent_of_caller_order() -> None:
    first = ("b-path", _sha256(b"b"), False)
    second = ("a-path", _sha256(b"a"), False)

    def equivalent(left: str, right: str) -> bool:
        assert (left, right) == ("a-path", "b-path")
        return True

    for entries in ([first, second], [second, first]):
        with pytest.raises(SourcePathConflictError) as excinfo:
            local_snapshot.build_inventory(entries, equivalent=equivalent)
        assert (excinfo.value.first_path, excinfo.value.second_path) == (
            "a-path",
            "b-path",
        )


def test_git_paths_are_ordinary_admitted_entries_without_silent_pruning() -> None:
    without_git = local_snapshot.build_inventory(
        [("SKILL.md", _sha256(b"name: review\n"), False)], equivalent=_identity
    )
    with_git = local_snapshot.build_inventory(
        [
            ("SKILL.md", _sha256(b"name: review\n"), False),
            (".git", _sha256(b"gitdir"), False),
        ],
        equivalent=_identity,
    )

    assert [entry["path"] for entry in with_git["files"]] == [".git", "SKILL.md"]
    assert with_git["snapshot"] != without_git["snapshot"]


def _generated_entry_sets() -> list[list[tuple[str, str, bool]]]:
    generator = random.Random(260917)
    generated: list[list[tuple[str, str, bool]]] = []
    for set_index in range(32):
        entries: list[tuple[str, str, bool]] = []
        for entry_index in range(1 + generator.randrange(7)):
            path = f"generated-{set_index}/file-{entry_index}"
            payload = generator.randbytes(generator.randrange(0, 17))
            entries.append((path, _sha256(payload), bool(generator.randrange(2))))
        generated.append(entries)
    return generated


def test_generated_digest_property_depends_only_on_sorted_triples() -> None:
    generator = random.Random(260918)
    for entries in _generated_entry_sets():
        baseline = local_snapshot.build_inventory(entries, equivalent=_identity)
        baseline_digest = baseline["snapshot"]

        for _ in range(5):
            permutation = generator.sample(entries, k=len(entries))
            assert (
                local_snapshot.build_inventory(permutation, equivalent=_identity)["snapshot"]
                == baseline_digest
            )

        for entry_index, (path, sha256, executable) in enumerate(entries):
            changed = list(entries)
            if entry_index % 3 == 0:
                changed[entry_index] = (path + "-changed", sha256, executable)
            elif entry_index % 3 == 1:
                replacement = "0" if sha256[-1] != "0" else "1"
                changed[entry_index] = (path, sha256[:-1] + replacement, executable)
            else:
                changed[entry_index] = (path, sha256, not executable)
            assert (
                local_snapshot.build_inventory(changed, equivalent=_identity)["snapshot"]
                != baseline_digest
            )


def test_inventory_schema_validation_and_ccj1_read_then_write_are_byte_identical() -> None:
    schema_path = Path(__file__).parent / "fixtures" / "local-snapshot-v1.schema.json"
    schema = json.loads(schema_path.read_bytes())
    Draft202012Validator.check_schema(schema)
    inventory = local_snapshot.build_inventory(
        [("empty", _sha256(b""), False)], equivalent=_identity
    )

    Draft202012Validator(schema).validate(inventory)
    raw = protocol_json.canonical_bytes(inventory)
    decoded = protocol_json.loads_canonical(raw)
    rewritten = protocol_json.canonical_bytes(decoded)

    assert rewritten == raw
    assert decoded == inventory
    assert local_snapshot.inventory_digest(decoded) == inventory["snapshot"]


def test_inventory_digest_ignores_only_snapshot_and_refuses_out_of_band_fields() -> None:
    inventory = local_snapshot.build_inventory(
        [("file", _sha256(b"content"), False)], equivalent=_identity
    )
    tampered_snapshot = dict(inventory)
    tampered_snapshot["snapshot"] = "sha256:" + "0" * 64
    assert local_snapshot.inventory_digest(tampered_snapshot) == inventory["snapshot"]

    with pytest.raises(SourceError) as excinfo:
        local_snapshot.inventory_digest({**inventory, "owner_id": 42})
    assert excinfo.value.code == CODE_INVENTORY_INVALID


def test_inventory_digest_is_insensitive_to_file_order() -> None:
    inventory = local_snapshot.build_inventory(
        [
            ("b-file", _sha256(b"b"), False),
            ("a-file", _sha256(b"a"), True),
        ],
        equivalent=_identity,
    )
    shuffled = dict(inventory)
    shuffled["files"] = list(reversed(inventory["files"]))

    assert local_snapshot.inventory_digest(shuffled) == inventory["snapshot"]


def _digest_refusal_cases() -> dict[str, dict[str, Any]]:
    good_file = {"path": "file", "sha256": _sha256(b"content"), "executable": False}
    base = {
        "schema_version": 1,
        "algorithm": "curator-local-snapshot-v1",
        "files": [dict(good_file)],
        "snapshot": "sha256:" + "0" * 64,
    }

    def variant(**overrides: object) -> dict[str, Any]:
        mutated = dict(base)
        mutated.update(overrides)
        return mutated  # type: ignore[return-value]

    missing_files = dict(base)
    del missing_files["files"]
    missing_executable = variant(files=[{k: v for k, v in good_file.items() if k != "executable"}])
    extra_file_key = variant(files=[{**good_file, "owner_id": 42}])
    return {
        "missing-files": missing_files,
        "unknown-top-level": variant(captured_at="2026-09-17T00:00:00Z"),
        "schema-version-two": variant(schema_version=2),
        "schema-version-str": variant(schema_version="1"),
        "schema-version-bool": variant(schema_version=True),
        "algorithm-wrong": variant(algorithm="curator-local-snapshot-v2"),
        "algorithm-non-str": variant(algorithm=1),
        "files-not-a-list": variant(files={"path": "file"}),
        "file-not-an-object": variant(files=["file"]),
        "file-missing-key": missing_executable,
        "file-extra-key": extra_file_key,
        "file-path-non-str": variant(files=[{**good_file, "path": 42}]),
        "file-path-non-portable": variant(files=[{**good_file, "path": "/absolute"}]),
        "file-sha256-non-str": variant(files=[{**good_file, "sha256": 42}]),
        "file-sha256-invalid": variant(files=[{**good_file, "sha256": "sha256:XYZ"}]),
        "file-executable-non-bool": variant(files=[{**good_file, "executable": 1}]),
    }


@pytest.mark.parametrize("case", _digest_refusal_cases(), ids=list(_digest_refusal_cases()))
def test_inventory_digest_refuses_malformed_preimages(case: dict[str, Any]) -> None:
    with pytest.raises(SourceError) as excinfo:
        local_snapshot.inventory_digest(case)
    assert excinfo.value.code == CODE_INVENTORY_INVALID


def test_empty_inventory_builds_and_validates_against_the_schema() -> None:
    schema_path = Path(__file__).parent / "fixtures" / "local-snapshot-v1.schema.json"
    schema = json.loads(schema_path.read_bytes())
    inventory = local_snapshot.build_inventory([], equivalent=_identity)

    Draft202012Validator(schema).validate(inventory)
    assert inventory["files"] == []
    expected = "sha256:" + hashlib.sha256(
        protocol_json.canonical_bytes(
            {
                "schema_version": 1,
                "algorithm": "curator-local-snapshot-v1",
                "files": [],
            }
        )
    ).hexdigest()
    assert inventory["snapshot"] == expected
    assert local_snapshot.inventory_digest(inventory) == expected


_AUDIT_ARMED = False
_AUDIT_EVENTS: list[str] = []


def _audit_hook(event: str, _args: object) -> None:
    if _AUDIT_ARMED:
        _AUDIT_EVENTS.append(event)


sys.addaudithook(_audit_hook)


def test_production_calls_emit_no_audit_event() -> None:
    global _AUDIT_ARMED
    entries = [
        ("b-file", _sha256(b"b"), False),
        ("a-file", _sha256(b"a"), True),
    ]
    del _AUDIT_EVENTS[:]
    _AUDIT_ARMED = True
    try:
        inventory = local_snapshot.build_inventory(entries, equivalent=_identity)
        local_snapshot.inventory_digest(inventory)
    finally:
        _AUDIT_ARMED = False

    assert _AUDIT_EVENTS == []


def _ast_call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def test_local_snapshot_module_static_ast_reaches_no_io_primitive() -> None:
    module_path = Path(local_snapshot.__file__ or "")
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    forbidden_modules = {"os", "time", "locale", "mmap", "socket", "urllib"}
    forbidden_calls = {
        "open",
        "read",
        "read_bytes",
        "read_text",
        "write",
        "write_bytes",
        "write_text",
        "scandir",
        "listdir",
        "iterdir",
        "glob",
        "rglob",
        "mmap",
        "stat",
        "lstat",
        "time",
        "monotonic",
        "perf_counter",
        "setlocale",
        "strxfrm",
        "getenv",
    }
    # Non-call attribute uses such as os.environ carry no Call node, so the
    # call-name check above cannot see them. Obfuscated construction
    # (getattr/__import__ string assembly) evades this walk by design; the
    # behavioral test_production_calls_emit_no_audit_event covers obfuscated
    # construction of AUDITED operations (open, listdir, scandir, mmap).
    # BOUND: obfuscated construction of non-audited primitives (os.stat,
    # time, locale, getenv, environ reads) evades both instruments; only
    # their direct references are refused here.
    forbidden_attributes = {"environ"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".", 1)[0] not in forbidden_modules for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".", 1)[0] not in forbidden_modules
        elif isinstance(node, ast.Call):
            assert _ast_call_name(node.func) not in forbidden_calls
        elif isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_attributes


def test_named_entry_form_is_limited_to_identity_fields() -> None:
    fields = tuple(local_snapshot.InventoryEntry.__dataclass_fields__)
    assert fields == ("path", "sha256", "executable")


def test_build_inventory_does_not_call_equivalence_for_an_exact_duplicate() -> None:
    calls: list[tuple[str, str]] = []

    def equivalent(left: str, right: str) -> bool:
        calls.append((left, right))
        return False

    with pytest.raises(SourcePathConflictError):
        local_snapshot.build_inventory(
            [("duplicate", _sha256(b"one"), False), ("duplicate", _sha256(b"two"), False)],
            equivalent=equivalent,
        )
    assert calls == []


class _PlainStrSubclass(str):
    """Benign str subclass without overrides (F1 class member)."""


class _EqChaos(str):
    """str subclass whose equality always lies (F1 repros 1-2)."""

    def __eq__(self, other: object) -> bool:
        return False

    def __ne__(self, other: object) -> bool:
        return True

    __hash__ = str.__hash__


class _EncodeLiar(str):
    """str subclass whose UTF-8 encoding lies for one spelling (F1 repro 3)."""

    def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
        if str.__str__(self) == "b":
            return b"A"
        return super().encode(encoding, errors)


class _StrConstantLiar(str):
    """__str__ always returns a fixed unrelated spelling (F2 A5)."""

    def __str__(self) -> str:
        return "DIFFERENT"


class _StrSmuggleEq(str):
    """__str__ returns a still-hostile __eq__-lying subclass (F2 A1-A2)."""

    def __str__(self) -> str:
        return _EqChaos(str.__str__(self))


class _StrSmuggleEncode(str):
    """__str__ returns a still-hostile encode-lying subclass (F2 A3)."""

    def __str__(self) -> str:
        return _EncodeLiar(str.__str__(self))


class _StrStatefulFlip(str):
    """__str__ answers an evil spelling once, then the true value (F2 family).

    Evil-first so that any single read through str() observes the lie; a fix
    that bypasses __str__ entirely always reads the true value instead.
    """

    _remaining_lies: int

    def __new__(cls, value: str) -> _StrStatefulFlip:
        instance = super().__new__(cls, value)
        instance._remaining_lies = 1
        return instance

    def __str__(self) -> str:
        if self._remaining_lies > 0:
            self._remaining_lies -= 1
            return "evil-flip"
        return str.__str__(self)


@pytest.mark.parametrize(
    "spell",
    [
        _PlainStrSubclass,
        _EqChaos,
        _EncodeLiar,
        _StrConstantLiar,
        _StrSmuggleEq,
        _StrSmuggleEncode,
        _StrStatefulFlip,
    ],
    ids=[
        "plain-subclass",
        "eq-lying",
        "encode-lying",
        "str-constant-lying",
        "str-smuggle-eq",
        "str-smuggle-encode",
        "str-stateful-flip",
    ],
)
def test_str_subclass_spellings_normalize_to_exact_str_by_value(spell: type[str]) -> None:
    """F1/F2 CLASS test: every str-subclass spelling normalizes on admission.

    Each family member is driven through ``build_inventory`` (tuple and
    ``InventoryEntry`` forms) and through ``inventory_digest``'s mapping path.
    Every member must refuse value-duplicates, digest exactly like plain
    ``str``, and emit true UTF-8 byte order with exact-``str`` fields. The
    F2 __str__ members additionally prove str() itself is not the normalizer:
    constant-lying redefines the value, smuggling resurrects F1's hostile
    subclasses, and the stateful flip poisons any single read through str().
    """
    digest_a = _sha256(b"a")
    digest_b = _sha256(b"b")
    digest_one = _sha256(b"one")
    digest_two = _sha256(b"two")

    duplicate_tuple_entries = [
        (spell("dup"), spell(digest_one), False),
        ("dup", digest_two, False),
    ]
    duplicate_named_entries = [
        local_snapshot.InventoryEntry(spell("dup"), spell(digest_one), False),
        local_snapshot.InventoryEntry("dup", digest_two, False),
    ]
    for entries in (duplicate_tuple_entries, duplicate_named_entries):
        with pytest.raises(SourcePathConflictError) as excinfo:
            local_snapshot.build_inventory(entries, equivalent=_identity)
        assert excinfo.value.first_path == "dup"
        assert excinfo.value.second_path == "dup"
        assert type(excinfo.value.first_path) is str
        assert type(excinfo.value.second_path) is str

    plain = local_snapshot.build_inventory(
        [("b", digest_b, False), ("a", digest_a, False)], equivalent=_identity
    )
    order_tuple_entries = [
        (spell("b"), spell(digest_b), False),
        (spell("a"), spell(digest_a), False),
    ]
    order_named_entries = [
        local_snapshot.InventoryEntry(spell("b"), spell(digest_b), False),
        local_snapshot.InventoryEntry(spell("a"), spell(digest_a), False),
    ]
    for entries in (order_tuple_entries, order_named_entries):
        inventory = local_snapshot.build_inventory(entries, equivalent=_identity)
        assert [entry["path"] for entry in inventory["files"]] == ["a", "b"]
        assert inventory == plain
        assert inventory["snapshot"] == plain["snapshot"]
        assert type(inventory["algorithm"]) is str
        for entry in inventory["files"]:
            assert type(entry["path"]) is str
            assert type(entry["sha256"]) is str

    mapping: dict[str, object] = {
        "schema_version": 1,
        "algorithm": local_snapshot.LOCAL_SNAPSHOT_ALGORITHM,
        "files": [
            {"path": spell("b"), "sha256": spell(digest_b), "executable": False},
            {"path": spell("a"), "sha256": spell(digest_a), "executable": False},
        ],
        "snapshot": "sha256:" + "0" * 64,
    }
    assert local_snapshot.inventory_digest(mapping) == plain["snapshot"]
    preimage = local_snapshot._preimage(mapping)
    assert type(preimage["algorithm"]) is str
    preimage_files = preimage["files"]
    assert isinstance(preimage_files, list)
    for preimage_entry in preimage_files:
        assert isinstance(preimage_entry, dict)
        assert type(preimage_entry["path"]) is str
        assert type(preimage_entry["sha256"]) is str


class _AlgorithmHonestThenEvil(str):
    """Stateful __str__: honest algorithm once, then evil (F2 A4 TOCTOU)."""

    _remaining_honest: int

    def __new__(cls) -> _AlgorithmHonestThenEvil:
        instance = super().__new__(cls, "curator-local-snapshot-v1")
        instance._remaining_honest = 1
        return instance

    def __str__(self) -> str:
        if self._remaining_honest > 0:
            self._remaining_honest -= 1
            return "curator-local-snapshot-v1"
        return "evil-algorithm"


class _AlgorithmEvilThenHonest(str):
    """Stateful __str__: evil algorithm once, then honest (F2 A4 mirror)."""

    _remaining_evil: int

    def __new__(cls) -> _AlgorithmEvilThenHonest:
        instance = super().__new__(cls, "curator-local-snapshot-v1")
        instance._remaining_evil = 1
        return instance

    def __str__(self) -> str:
        if self._remaining_evil > 0:
            self._remaining_evil -= 1
            return "evil-algorithm"
        return "curator-local-snapshot-v1"


@pytest.mark.parametrize(
    "algorithm_spell",
    [_AlgorithmHonestThenEvil, _AlgorithmEvilThenHonest],
    ids=["honest-then-evil", "evil-then-honest"],
)
def test_stateful_algorithm_spelling_reads_one_canonical_value(
    algorithm_spell: type[str],
) -> None:
    """F2 TOCTOU regression: the algorithm gate and the preimage read one value.

    A compare-then-store that normalizes twice lets honest-then-evil pass the
    gate and hash the evil value, and lets evil-then-honest refuse honest
    input. Normalizing once and comparing/storing the same canonical value
    closes both directions; the admitted value is the exact-str value.
    """
    honest = local_snapshot.build_inventory(
        [("a-file", _sha256(b"a"), False)], equivalent=_identity
    )
    mapping: dict[str, object] = {
        "schema_version": 1,
        "algorithm": algorithm_spell(),
        "files": [dict(entry) for entry in honest["files"]],
        "snapshot": "sha256:" + "0" * 64,
    }

    assert local_snapshot.inventory_digest(mapping) == honest["snapshot"]
    preimage = local_snapshot._preimage(mapping)
    assert preimage["algorithm"] == "curator-local-snapshot-v1"
    assert type(preimage["algorithm"]) is str
