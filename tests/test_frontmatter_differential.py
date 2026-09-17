"""Differential frontmatter oracle: csk selection parser vs PyYAML (TEST-ONLY).

Compares the production frontmatter reader in :mod:`csk.sources.selection`
(:func:`_parse_frontmatter`, :func:`read_skill_md_name`, and both public
entry points) against ``yaml.safe_load`` over a deterministic generated
corpus (seed 260916, several hundred SKILL.md documents).

Oracle rule (strict documents): the oracle input is the frontmatter YAML
source; if ``yaml.safe_load`` raises, yields a non-dict, or yields a dict
whose ``name``/``description`` is missing, null, or not a string, csk must
refuse with ``source_member_invalid``; otherwise csk must accept and return
EXACTLY PyYAML's ``name`` and ``description`` strings (byte-identical,
including blank-only values at the ``_parse_frontmatter`` level; the
non-blank gate lives in :func:`read_skill_md_name` and is asserted at the
gate level with the same oracle).

Two documented divergence tables keep the oracle honest:

* ``TYPE_ALLOWLIST``: YAML 1.1 (PyYAML) vs YAML 1.2 core schema (csk) typing.
  Each entry pins the 1.1 oracle outcome and csk's 1.2 behaviour.
* ``BOUNDARY``: intended csk-subset strictness or prior-brief mandates where
  csk deliberately differs from PyYAML (duplicates, tab separation,
  below-baseline comment lines, tag/anchor values, single-line quoted and
  flow forms, mid-text BOM, Cc names, and the SB1/SB2 YAML 1.1-vs-1.2
  separator-break divergences with spec citations). Each entry pins BOTH
  outcomes with its mandate.

Any discrepancy outside those tables is a real parser bug. The runtime
package stays standard-library-only: this module is the sole PyYAML
consumer, enforced by :func:`test_runtime_never_imports_yaml`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from csk.sources import errors as source_errors
from csk.sources import selection
from csk.sources.selection import expand_collection, resolve_individual
from csk.sources.skillfile_v2 import CollectionSelector, IndividualSelector

SEED = 260916

# ---------------------------------------------------------------------------
# Corpus document model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Doc:
    """One generated corpus document.

    ``skill_text`` is the full SKILL.md file; ``yaml_body`` is the frontmatter
    YAML source fed to the oracle (for standard framing the lines between the
    fences; for framing variants the logical body). ``kind`` selects the
    assertion rule: ``strict`` (oracle rule), ``allow`` (TYPE_ALLOWLIST),
    ``bound`` (BOUNDARY), ``frame-ok`` (framing accepted, oracle values must
    match), ``frame-no`` (framing refused by csk, oracle outcome documented).
    """

    id: str
    skill_text: str
    yaml_body: str
    kind: str
    note: str = ""
    # For allow-accept / bound-accept / frame-ok docs: exact expected csk
    # description value (None means csk must refuse at parse level).
    csk_value: str | None = None
    # For bound docs where the oracle accepts: pinned oracle description.
    oracle_value: str | None = None
    # For bound docs probing the name field: pinned oracle/csk names.
    oracle_name: str | None = None
    csk_name: str | None = None
    # For bound docs that parse but refuse at the gate (non-portable names).
    gate_raise: bool = False
    # "raise" or "accept": documented oracle outcome for bound/frame-no docs.
    oracle_expect: str = ""
    # Required message fragment when csk must refuse (bound/frame-no docs).
    csk_fragment: str = ""
    # For tab-header bound docs: the space-separated oracle body.
    paired_body: str = ""
    # For allow docs: pinned oracle type ("bool", "int", "float", "str").
    oracle_kind: str = ""


def _wrap(body: str) -> str:
    """Wrap one frontmatter body in standard SKILL.md framing."""
    return f"---\n{body}---\n# body is ignored\n"


def _oracle(body: str) -> tuple[str, dict[str, object] | None]:
    """Run the PyYAML oracle: ("raise"|"non-dict"|"ok", dict-or-None)."""
    try:
        loaded = yaml.safe_load(body)
    except Exception:  # noqa: BLE001 - any oracle failure is a refuse signal
        return "raise", None
    if not isinstance(loaded, dict):
        return "non-dict", None
    return "ok", loaded


def _triggers_invalid(value: object) -> bool:
    """Mirror the required ``triggers`` gate: non-empty list of blanks-free strings.

    Blank means ASCII-blank (spaces, tabs, line breaks) exactly like the
    production gate; any other character (including Unicode whitespace) is
    content.
    """
    if not isinstance(value, list) or not value:
        return True
    return any(not isinstance(item, str) or not item.strip(" \t\n") for item in value)


# ---------------------------------------------------------------------------
# Assertion rules
# ---------------------------------------------------------------------------


def assert_parse_level(doc: Doc) -> None:
    """Assert one document at the ``_parse_frontmatter`` level."""
    status, loaded = _oracle(doc.yaml_body)
    if doc.kind in ("strict", "frame-ok"):
        _assert_strict_parse(doc, status, loaded)
    elif doc.kind == "allow":
        _assert_allow_parse(doc, status, loaded)
    elif doc.kind in ("bound", "frame-no"):
        _assert_bound_parse(doc, status, loaded)
    else:  # pragma: no cover - corpus construction guards kinds
        raise AssertionError(f"unknown kind {doc.kind}")


def _assert_strict_parse(
    doc: Doc, status: str, loaded: dict[str, object] | None
) -> None:
    try:
        fields = selection._parse_frontmatter(doc.skill_text, f"'{doc.id}'")
    except source_errors.SourceError as exc:
        assert exc.code == source_errors.CODE_MEMBER_INVALID, doc.id
        # A parse refusal needs an oracle-visible reason: structural failure,
        # a missing/null/non-string required field, or invalid triggers.
        assert _strict_refuse_expected(status, loaded), doc.id
        return
    assert status == "ok" and loaded is not None, doc.id
    name = loaded.get("name")
    description = loaded.get("description")
    assert isinstance(name, str) and isinstance(description, str), doc.id
    assert fields.get("name") == name, doc.id
    assert fields.get("description") == description, doc.id
    if "triggers" in loaded:
        assert fields.get("triggers") == loaded["triggers"], doc.id


def _strict_refuse_expected(status: str, loaded: dict[str, object] | None) -> bool:
    if status != "ok" or loaded is None:
        return True
    name = loaded.get("name")
    description = loaded.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        return True
    if "triggers" in loaded and _triggers_invalid(loaded["triggers"]):
        return True
    return False


def _assert_allow_parse(
    doc: Doc, status: str, loaded: dict[str, object] | None
) -> None:
    assert status == "ok" and loaded is not None, doc.id
    probed = "name" if doc.note.endswith("-in-name") else "description"
    value = loaded.get(probed)
    if doc.csk_value is None:
        # 1.2 non-string, 1.1 string: oracle keeps the spelling, csk refuses.
        assert doc.oracle_kind == "str", doc.id
        assert value == doc.oracle_value, doc.id
        with pytest.raises(source_errors.SourceError) as excinfo:
            selection._parse_frontmatter(doc.skill_text, f"'{doc.id}'")
        assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID, doc.id
        return
    # 1.1 non-string, 1.2 string: oracle types it, csk keeps the spelling.
    assert doc.oracle_kind in ("bool", "int", "float"), doc.id
    if doc.oracle_kind == "bool":
        assert isinstance(value, bool), doc.id
    elif doc.oracle_kind == "int":
        assert isinstance(value, int) and not isinstance(value, bool), doc.id
    else:
        assert isinstance(value, float), doc.id
    fields = selection._parse_frontmatter(doc.skill_text, f"'{doc.id}'")
    assert fields.get(probed) == doc.csk_value, doc.id


def _assert_bound_parse(
    doc: Doc, status: str, loaded: dict[str, object] | None
) -> None:
    assert doc.oracle_expect in ("raise", "accept"), doc.id
    if doc.kind == "bound" and doc.paired_body:
        # Tab-header docs: the oracle cannot scan tabs, so the pinned value
        # comes from the space-separated twin body.
        assert status == "raise", doc.id
        twin_status, twin = _oracle(doc.paired_body)
        assert twin_status == "ok" and twin is not None, doc.id
        assert twin.get("description") == doc.oracle_value, doc.id
        fields = selection._parse_frontmatter(doc.skill_text, f"'{doc.id}'")
        assert fields.get("description") == doc.csk_value, doc.id
        return
    if doc.oracle_expect == "raise":
        assert status in ("raise", "non-dict"), doc.id
    else:
        assert status == "ok" and loaded is not None, doc.id
        if doc.oracle_value is not None:
            assert loaded.get("description") == doc.oracle_value, doc.id
        else:
            assert isinstance(loaded.get("description"), str), doc.id
        if doc.oracle_name is not None:
            assert loaded.get("name") == doc.oracle_name, doc.id
    if doc.csk_value is None:
        with pytest.raises(source_errors.SourceError) as excinfo:
            selection._parse_frontmatter(doc.skill_text, f"'{doc.id}'")
        assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID, doc.id
        if doc.csk_fragment:
            assert doc.csk_fragment in str(excinfo.value), doc.id
    else:
        fields = selection._parse_frontmatter(doc.skill_text, f"'{doc.id}'")
        assert fields.get("description") == doc.csk_value, doc.id
        if doc.csk_name is not None:
            assert fields.get("name") == doc.csk_name, doc.id


def gate_expects_raise(doc: Doc) -> bool:
    """Oracle-driven gate expectation for :func:`read_skill_md_name`.

    Derived from the oracle outcome plus the documented gate rules
    (non-blank required fields, non-empty string-list triggers, portable
    names by construction), never from csk's own parse outcome.
    """
    status, loaded = _oracle(doc.yaml_body)
    if doc.kind == "allow":
        if doc.csk_value is None:
            return True
        # The 1.2 value is pinned in the corpus (asserted at parse level);
        # the gate sees the pinned spelling, never the 1.1-typed oracle value.
        assert doc.csk_value.strip(" \t\n"), doc.id
        if doc.note.endswith("-in-name"):
            assert selection.is_installable_name(doc.csk_value.strip(" \t\n")), doc.id
        return False
    if doc.kind == "bound":
        if doc.csk_value is None:
            return True
        if doc.gate_raise:
            return True
        assert doc.csk_value.strip(" \t\n"), doc.id
        return False
    if doc.kind == "frame-no":
        return True
    if doc.kind == "frame-ok":
        return _gate_tail_refuse(doc, status, loaded)
    assert doc.kind == "strict", doc.id
    if status != "ok" or loaded is None:
        return True
    name = loaded.get("name")
    description = loaded.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        return True
    return _gate_tail_refuse(doc, status, loaded)


def _gate_tail_refuse(
    doc: Doc, status: str, loaded: dict[str, object] | None
) -> bool:
    if status != "ok" or loaded is None:
        return True
    name = loaded.get("name")
    description = loaded.get("description")
    # The required-field gate refuses ASCII-blank (spaces/tabs/line-breaks
    # only) strings; the oracle rule mirrors it instead of the literal
    # "non-empty" reading.
    if not isinstance(name, str) or not name.strip(" \t\n"):
        return True
    if not isinstance(description, str) or not description.strip(" \t\n"):
        return True
    if "triggers" in loaded and _triggers_invalid(loaded["triggers"]):
        return True
    # Corpus accept-path names are portable by construction (guarded below).
    assert selection.is_installable_name(name.strip(" \t\n")), doc.id
    assert len(name.strip(" \t\n")) <= 128, doc.id
    return False


def gate_expected_name(doc: Doc) -> str:
    """Expected installed name when the gate accepts (oracle-driven)."""
    if doc.note.endswith("-in-name") and doc.csk_value is not None:
        # Allow rows pin the probed name in csk_value; bound rows pin the
        # description in csk_value and the name in csk_name.
        pinned = doc.csk_name if doc.csk_name is not None else doc.csk_value
        return pinned.strip(" \t\n")
    status, loaded = _oracle(doc.yaml_body)
    if status == "ok" and loaded is not None:
        name = loaded.get("name")
        if isinstance(name, str):
            return name.strip(" \t\n")
    # Bound-accept docs whose oracle raises (tabs): name is review by construction.
    assert doc.csk_value is not None, doc.id
    assert doc.kind == "bound", doc.id
    return "review"


# ---------------------------------------------------------------------------
# Systematic corpus: plain scalars
# ---------------------------------------------------------------------------
#
# Strict documents are tab-free by construction (any tab makes the 1.1 oracle
# raise; tab shapes live in the T1 boundary section with pinned csk values).


def _strict_desc(case_id: str, value: str) -> Doc:
    body = f"name: review\ndescription: {value}\n"
    return Doc(id=case_id, skill_text=_wrap(body), yaml_body=body, kind="strict")


def _strict_name(case_id: str, value: str) -> Doc:
    body = f"name: {value}\ndescription: valid description\n"
    return Doc(id=case_id, skill_text=_wrap(body), yaml_body=body, kind="strict")


_PLAIN_ACCEPT = (
    "hello",
    "a b",
    "Review skill",
    "x",
    "0 review",
    "review 2",
    "v1.2",
    "a:b",
    "a:b:c",
    "a :b",
    "42x",
    "x42",
    "1d10",
    "1.5D3",
    "0b2",
    "0o",
    "0x",
    "0X2A",
    "0O755",
    "0o8",
    "0xG1",
    "yEs",
    "n",
    "Y",
    "N",
    "NULLL",
    "nul",
    "TrueX",
    "nulls",
    "~x",
    "x~",
    "-x",
    "--",
    ".",
    "+",
    "e3",
    "E3",
    "Infinity",
    "INF",
    "Nan",
    "NAN",
    "+.nan",
    "-.nan",
    "+.NaN",
    "_1",
    "a__b",
    "it's",
    'say "hi"',
    "text |",
    "a > b",
    "<<x",
    "<< x",
    "...",
    "---",
    "... x",
    "?foo",
    "=foo",
    "= foo",
    ":foo",
    ";foo",
    "$foo",
    "(foo",
    ")foo",
    "/foo",
    "<foo",
    "^foo",
    "_foo",
    "\\foo",
    ".foo",
    "+foo",
    "~foo",
    "~ foo",
    "hello world",
    "https://example.com/docs",
    "padded   ",
    "hello # comment",
    "value #hashtag",
    "a#b",
    "x#c",
)


def _plain_corpus() -> list[Doc]:
    docs = [_strict_desc(f"p-accept-{index:03d}", value) for index, value in enumerate(_PLAIN_ACCEPT)]
    # A sample of the same spellings in the name field (all portable).
    for index in (0, 1, 5, 10, 28, 40, 45, 60, 66, 70):
        docs.append(_strict_name(f"p-name-{index:03d}", _PLAIN_ACCEPT[index]))
    return docs


_PLAIN_REFUSE = (
    "null",
    "Null",
    "NULL",
    "~",
    "",
    "# comment",
    "true",
    "True",
    "TRUE",
    "false",
    "False",
    "FALSE",
    "0",
    "42",
    "+42",
    "-42",
    "007",
    "0755",
    "0x2A",
    "0x2a",
    "00",
    "-0",
    "1.5",
    "1.",
    ".5",
    "1.5E+3",
    "1.5E-3",
    ".5E+2",
    ".inf",
    "-.INF",
    "+.Inf",
    ".nan",
    ".NaN",
    ".NAN",
    "see: this",
    "a: b: c",
    "trailing:",
    "- item",
    "-",
    "{a: b}",
    "[a]",
    "*a",
    "!tag x",
    "!",
    "&a",
    "%x",
    "@x",
    "`x",
    ",foo",
    ",",
    "]foo",
    "}foo",
    "?",
    "? foo",
    "=",
    "= # comment",
    "? ",
)


def _plain_refuse_corpus() -> list[Doc]:
    docs = [_strict_desc(f"p-refuse-{index:03d}", value) for index, value in enumerate(_PLAIN_REFUSE)]
    for index in (0, 6, 13, 22, 28, 34, 37, 40, 46, 50, 53):
        docs.append(_strict_name(f"p-nrefuse-{index:03d}", _PLAIN_REFUSE[index]))
    return docs


# ---------------------------------------------------------------------------
# Systematic corpus: quoted scalars (tab-free; tab trailers live in T1)
# ---------------------------------------------------------------------------

_QUOTED_ACCEPT = (
    "'hello'",
    "'it''s'",
    "'a # b'",
    "'trailing' # comment",
    "''",
    "'  spaces  '",
    "'42'",
    "'true'",
    "'null'",
    "'a:b'",
    "'- item'",
    "'? foo'",
    "',foo'",
    '\'"q"\'',
    "'x'  #c",
    '"hello"',
    '"a\\nb"',
    '"a\\tb"',
    '"a\\"b"',
    '"a\\\\b"',
    '"a\\x41b"',
    '"a\\u0041b"',
    '"a\\U00000041b"',
    '"a\\0b"',
    '"a\\ab"',
    '"a\\bb"',
    '"a\\vb"',
    '"a\\fb"',
    '"a\\rb"',
    '"a\\eb"',
    '"a\\Nb"',
    '"a\\_b"',
    '"a\\Lb"',
    '"a\\Pb"',
    '"a\\ b"',
    '"a # b"',
    '"trailing" # comment',
    '""',
    '"1.5"',
    '"#"',
    '": "',
    '"- item"',
    '"x"  #c',
    "'x' #c",
    "'x'   #c",
    '"x" #c',
    '"x"   #c',
    '"a\\/b"',
)


def _quoted_corpus() -> list[Doc]:
    docs = [_strict_desc(f"q-accept-{index:03d}", value) for index, value in enumerate(_QUOTED_ACCEPT)]
    # Name-field sample: decoded values must stay portable (no quotes,
    # controls, or reserved characters after decoding).
    for index in (0, 3, 5, 6, 15, 20, 35):
        docs.append(_strict_name(f"q-name-{index:03d}", _QUOTED_ACCEPT[index]))
    return docs


_QUOTED_REFUSE = (
    "'abc",
    '"abc',
    "'ab''",
    '"ab""',
    "'x' y",
    '"x" y',
    "'x'y",
    '"x"y',
    '"a\\qb"',
    '"\\xZZ"',
    '"ab\\"',
    '"\\U00110000"',
    '"\\u00Z1"',
    '"a\\sb"',
    '"a\\x4"',
    '"a\\u004"',
    '"a\\U000041b"',
    '"ab\\',
)


def _quoted_refuse_corpus() -> list[Doc]:
    docs = [_strict_desc(f"q-refuse-{index:03d}", value) for index, value in enumerate(_QUOTED_REFUSE)]
    for index in (0, 4, 8):
        docs.append(_strict_name(f"q-nrefuse-{index:03d}", _QUOTED_REFUSE[index]))
    return docs


# ---------------------------------------------------------------------------
# Systematic corpus: block scalars
# ---------------------------------------------------------------------------


def _block_doc(case_id: str, header: str, lines: list[str]) -> Doc:
    body = "name: review\ndescription: " + header + "\n"
    for line in lines:
        body += line + "\n"
    return Doc(id=case_id, skill_text=_wrap(body), yaml_body=body, kind="strict")


def _block_corpus() -> list[Doc]:
    docs: list[Doc] = []
    counter = 0

    def add(header: str, lines: list[str]) -> None:
        nonlocal counter
        docs.append(_block_doc(f"b-sys-{counter:03d}", header, lines))
        counter += 1

    headers = ["|", "|-", "|+", ">", ">-", ">+"]
    # Baseline matrix: auto-detected (2, 4) and explicit (1, 2, 3, 4).
    for header in headers:
        add(header, ["  text"])
        add(header, ["    text"])
        add(header, ["  first", "  second"])
        add(header, ["  first", "    deeper"])
        add(header, ["", "  text"])
        add(header, [" ", "  text"])
        add(header, ["  ", "  text"])
        add(header, ["  text", ""])
        add(header, ["  text", "  "])
        add(header, ["  text", "    "])
        add(header, ["  one", "", "  two"])
        add(header, ["  one", "   ", "  two"])
        add(header, ["  # content", "  text"])
        add(header, ["  text", "  # content"])
        add(header, ["  - item", "  text"])
        add(header, ["  text", "  - item"])
        add(header, ["  spaced  "])
        add(header + " # note", ["  text"])
        add(header + "  # note", ["  first", "  second"])
    # Explicit indent matrix (content at, above, and around the baseline).
    for explicit in (1, 2, 3, 4):
        for header in ("|", ">"):
            ind = " " * explicit
            add(f"{header}{explicit}", [ind + "text"])
            add(f"{header}{explicit}", [ind + "first", ind + "  deeper"])
            add(f"{header}{explicit}", ["", ind + "text"])
            add(f"{header}{explicit}", [" " * (explicit + 3), ind + "text"])
            add(f"{header}{explicit}", [ind + "text", " " * (explicit + 2)])
            add(f"{header}{explicit}", [ind + "one", "", ind + "two"])
            add(f"{header}{explicit}-", [ind + "text"])
            add(f"{header}{explicit}+", [ind + "text", ""])
            add(f"{header}+{explicit} # note", [ind + "text"])
    # Empty-only and no-content shapes.
    for header in ("|", "|-", "|+", ">", ">-", ">+"):
        add(header, [""])
        add(header, ["", ""])
        add(header, [])
    add("|2", ["    "])
    add("|2", ["  "])
    add(">", ["      ", "  ", "   "])
    # Refusal shapes: under-indent, dedent, tab indentation, over-indented
    # leading blanks under auto-detection, malformed headers.
    add("|", ["    first", "  dedented"])
    add(">", ["    first", "  dedented"])
    add("|4", ["  underindented"])
    add(">4", ["  underindented"])
    add("|9", ["  text"])
    # Tab-indented content lines refuse on both sides; they live in the T1
    # boundary section because the bodies contain literal tabs.
    add("|", ["  ", " text"])
    add(">", ["      ", "   text"])
    add("|", ["   ", "  text"])
    add(">", ["    ", "   text"])
    for bad in ("|x", "||", "|0", "|12", "|+-", "|-+", "|22", "|#c", "| -", ">2x", "|- +"):
        add(bad, ["  text"])
    # Block scalar on the name field (portable single-line values).
    docs.append(_block_name_doc("b-name-000", "|", ["  review"]))
    docs.append(_block_name_doc("b-name-001", ">", ["  folded", "  name"]))
    docs.append(_block_name_doc("b-name-002", "|-", ["  review"]))
    return docs


def _block_name_doc(case_id: str, header: str, lines: list[str]) -> Doc:
    body = "name: " + header + "\n"
    for line in lines:
        body += line + "\n"
    body += "description: valid description\n"
    return Doc(id=case_id, skill_text=_wrap(body), yaml_body=body, kind="strict")


def _block_header_comment_corpus() -> list[Doc]:
    """Block-header comment separators (rev13): zero/one/several SPACE/TAB.

    Zero SPACE (``|#c``) refuses on both sides (strict refuse); one and
    several SPACE separators accept byte-identical values on both sides
    (strict accept). TAB separators raise in the 1.1 oracle scanner while
    csk accepts (T1 bound, pinned csk value).
    """

    docs: list[Doc] = []
    for index, header in enumerate(
        ("| #c", "|  #c", "|   #c", ">- #c", "|+2 #c", "|-2 #c", ">  #c")
    ):
        docs.append(_block_doc(f"h-acc-{index:03d}", header, ["  text"]))
    for index, header in enumerate(("|#c", ">-#c", "|+2#c", ">#c", "|-2#c")):
        docs.append(_block_doc(f"h-ref-{index:03d}", header, ["  text"]))
    for case_id, header, expected in (
        ("t-tab-header-two-spaces-tab", "| \t#c", "text\n"),
        ("t-tab-header-two-tabs", "|\t\t#c", "text\n"),
        ("t-tab-header-fold-mixed", ">- \t #c", "text"),
    ):
        body = f"name: review\ndescription: {header}\n  text\n"
        docs.append(
            Doc(
                id=case_id,
                skill_text=_wrap(body),
                yaml_body=body,
                kind="bound",
                note="T1",
                csk_value=expected,
                oracle_expect="raise",
            )
        )
    return docs


# ---------------------------------------------------------------------------
# Systematic corpus: nested blocks, triggers, other keys, framing
# ---------------------------------------------------------------------------


def _nested_corpus() -> list[Doc]:
    bodies = [
        "name: review\ndescription: d\nmetadata:\n  note: hello\n",
        "name: review\ndescription: d\nmetadata:\n  name: inner\n  description: inner\n",
        "name: review\ndescription: d\nallowed-tools:\n  - Read\n  - Write\n",
        "name: review\ndescription: d\nmetadata:\n  nested:\n    deep: value\n",
        "name: review\ndescription: d\nmetadata:\n  count: 42\n  flag: true\n",
        "name: review\ndescription: d\nextra: {a: b}\n",
        'name: review\ndescription: d\nextra: ["a#b", c]\n',
        "name: review\ndescription: d\nextra: [a, b]\n",
        "name: review\ndescription: d\nextra: {}\n",
        "name: review\ndescription: d\ntriggers:\n  - alpha\n  - beta\n",
        "name: review\ndescription: d\ntriggers: [alpha, beta]\n",
        'name: review\ndescription: d\ntriggers: ["42", true-word]\n',
        "name: review\ndescription: d\ntriggers:\n  - 'quoted item'\n",
        "name: review\ndescription: d\ntriggers: hello\n",
        "name: review\ndescription: d\ntriggers: []\n",
        "name: review\ndescription: d\ntriggers: [42]\n",
        "name: review\ndescription: d\ntriggers:\n  - 42\n",
        "name: review\ndescription: d\ntriggers:\n  - \n",
        "name: review\ndescription: d\ntriggers:\n  key: value\n",
        "name: review\ndescription: d\nextra: {a: b\n",
        "name:\n  nested: x\ndescription: d\n",
        "name: review\ndescription:\n  nested: x\n",
        "name: review\ndescription: d\nmetadata:\n  note: |\n    text\n",
        "name: review\ndescription: |\n  text\nmetadata:\n  note: hello\n",
    ]
    return [
        Doc(id=f"n-sys-{index:03d}", skill_text=_wrap(body), yaml_body=body, kind="strict")
        for index, body in enumerate(bodies)
    ]


def _framing_corpus() -> list[Doc]:
    docs: list[Doc] = []
    body = "name: review\ndescription: hello\n"
    ok_variants = [
        ("bom", "\ufeff---\n" + body + "---\n"),
        ("crlf", "---\r\nname: review\r\ndescription: hello\r\n---\r\n"),
        ("bom-crlf", "\ufeff---\r\nname: review\r\ndescription: hello\r\n---\r\n"),
        ("ellipsis", "---\n" + body + "...\n"),
        ("ellipsis-spaces", "---\n" + body + "...   \n"),
        ("trailing-spaces", "---   \n" + body + "---\t \n"),
        ("body-ignored", "---\n" + body + "---\n# body\ntext here\n"),
        ("block-ellipsis", "---\nname: review\ndescription: |\n  text\n...\n"),
        # The printable gate covers the framed region only: controls, DEL
        # and a mid-text BOM after the closing fence are ordinary body bytes.
        ("body-nul", "---\n" + body + "---\nbody \x00 here\n"),
        ("body-del", "---\n" + body + "---\nbody \x7f\x01 here\n"),
        ("body-feff", "---\n" + body + "---\nbody \ufeff here\n"),
        # Lone-CR refusal is framed-only too: body CR is ordinary body bytes.
        ("body-cr", "---\n" + body + "---\n# Body\ntext\rcontent\n"),
        ("body-multi-cr", "---\n" + body + "---\nline\rmore\rend\n"),
        # Rev14: EOF endings across both closing markers and both regions.
        # The fence may end at EOF with no newline; a body tail may end at
        # EOF with no newline, a lone CR, or CRLF: body lines are never
        # validated, so every body ending accepts.
        ("eof-no-newline", "---\n" + body + "---"),
        ("eof-ellipsis-no-newline", "---\n" + body + "..."),
        ("eof-ellipsis-crlf", "---\r\nname: review\r\ndescription: hello\r\n...\r\n"),
        ("body-eof-no-newline", "---\n" + body + "---\nbody tail"),
        ("body-eof-cr", "---\n" + body + "---\nbody tail\r"),
        ("body-eof-crlf", "---\n" + body + "---\nbody tail\r\n"),
    ]
    for shape, skill_text in ok_variants:
        frame_body = body if "block" not in shape else "name: review\ndescription: |\n  text\n"
        docs.append(
            Doc(id=f"f-ok-{shape}", skill_text=skill_text, yaml_body=frame_body, kind="frame-ok")
        )
    no_variants = [
        ("text-before", "hello\n---\n" + body + "---\n", body, "must open with"),
        ("blank-before", "\n---\n" + body + "---\n", body, "must open with"),
        ("indented-opening", "  ---\n" + body + "---\n", body, "must open with"),
        ("missing-closing", "---\n" + body, body, "must close with"),
        ("closing-text", "---\n" + body + "--- x\n", body, "must close with"),
        ("fence-key", "---\nname: review\n---: nope\ndescription: d\n---\n", body, "trailing text"),
        # The oracle folds the indented fence into the plain scalar above it
        # (name becomes 'review ---'); csk refuses: values are single-line.
        (
            "indented-fence",
            "---\nname: review\n  ---\ndescription: d\n---\n",
            "name: review\n  ---\ndescription: d\n",
            "without a parent block",
        ),
        ("lone-cr", "---\nname: review\ndescription: a\rb\n---\n", body, "lone"),
        # A separator before the fence leaves line 1 non-fence: the
        # frontmatter must still open at line 1 with `---` at column 0.
        ("break-before-nel", "\x85---\n" + body + "---\n", body, "must open with"),
        ("break-before-ls", "\u2028---\n" + body + "---\n", body, "must open with"),
        # Fences allow only trailing spaces/tabs: NBSP or a separator after
        # the marker is not a fence.
        ("opening-nbsp", "---\xa0\n" + body + "---\n", body, "must open with"),
        ("opening-ls", "---\u2028\n" + body + "---\n", body, "must open with"),
        ("closing-nbsp", "---\n" + body + "---\xa0\n", body, "must close with"),
        ("closing-ls", "---\n" + body + "---\u2028\n", body, "must close with"),
        ("closing-ps", "---\n" + body + "...\u2029\n", body, "must close with"),
        # Rev14: a final unpaired CR is lone, not CRLF. The fence line keeps
        # its CR, is not a clean fence, and the frontmatter never closes.
        ("closing-cr-eof", "---\n" + body + "---\r", body, "must close with"),
        ("ellipsis-cr-eof", "---\n" + body + "...\r", body, "must close with"),
        # The empty block parses to {} and refuses at the gate; it has a
        # dedicated level-split test below, not a frame-no corpus row.
    ]
    for shape, skill_text, logical_body, fragment in no_variants:
        docs.append(
            Doc(
                id=f"f-no-{shape}",
                skill_text=skill_text,
                yaml_body=logical_body,
                kind="frame-no",
                oracle_expect="accept",
                csk_fragment=fragment,
            )
        )
    return docs


# ---------------------------------------------------------------------------
# TYPE_ALLOWLIST: YAML 1.1 (PyYAML) vs YAML 1.2 core schema (csk) typing
# ---------------------------------------------------------------------------
#
# Each entry pins the 1.1 oracle outcome and csk's 1.2 behaviour for one
# spelling class. Direction A: oracle types the spelling (bool/int/float),
# csk keeps it as a 1.2 string. Direction B: oracle keeps the spelling as a
# string, csk refuses it as a 1.2 non-string. Justification per entry:
#
# * 1.1-bool: yes/no/on/off (+case variants) are 1.1 booleans, 1.2 strings.
#   Single letters y/n/Y/N are strings in BOTH (probed), so they stay strict.
# * 1.1-binary: 0b... is a 1.1 int; the 1.2 core schema has no binary form.
# * 1.1-signed-hex: +0x/-0x are 1.1 ints; 1.2 hex is unsigned-only.
# * 1.1-underscore: digit separators are 1.1 ints/floats, 1.2 strings.
# * 1.1-sexagesimal: 1:20-style values are 1.1 ints/floats, 1.2 strings.
# * 1.2-octal: 0o... is a 1.2 int; 1.1 has only 0... octal (and rejects 8/9).
# * 1.2-leading-zero-89: 08/09-style spellings are 1.2 decimal ints; 1.1
#   rejects them as invalid octal and keeps them as strings.
# * 1.2-exponent: 1.2 floats accept every [eE] exponent; 1.1 recognises only
#   dot-mantissa + uppercase-E + signed-exponent floats (1.5E+3 and kin stay
#   strict because both sides refuse them).
# * 1.2-signed-dot-float: +.5/-.5 are 1.2 floats; 1.1 keeps them as strings.

_ALLOW_ACCEPT: tuple[tuple[str, str, str], ...] = (
    # (entry key, spelling, pinned oracle type)
    ("1.1-bool", "yes", "bool"),
    ("1.1-bool", "Yes", "bool"),
    ("1.1-bool", "YES", "bool"),
    ("1.1-bool", "no", "bool"),
    ("1.1-bool", "No", "bool"),
    ("1.1-bool", "NO", "bool"),
    ("1.1-bool", "on", "bool"),
    ("1.1-bool", "On", "bool"),
    ("1.1-bool", "ON", "bool"),
    ("1.1-bool", "off", "bool"),
    ("1.1-bool", "Off", "bool"),
    ("1.1-bool", "OFF", "bool"),
    ("1.1-binary", "0b101", "int"),
    ("1.1-binary", "+0b1", "int"),
    ("1.1-signed-hex", "+0x2A", "int"),
    ("1.1-signed-hex", "-0x2A", "int"),
    ("1.1-underscore", "1_000", "int"),
    ("1.1-underscore", "+1_000", "int"),
    ("1.1-underscore", "1__0", "int"),
    ("1.1-underscore", "0_0", "int"),
    ("1.1-underscore", "0x2_A", "int"),
    ("1.1-underscore", "0b1_0", "int"),
    ("1.1-underscore", "1_0.0", "float"),
    ("1.1-underscore", "1.0_0", "float"),
    ("1.1-sexagesimal", "1:20", "int"),
    ("1.1-sexagesimal", "1:20.5", "float"),
    ("1.1-sexagesimal", "1:2:3", "int"),
    ("1.1-sexagesimal", "12:34:56", "int"),
    ("1.1-sexagesimal", "-1:20", "int"),
)

_ALLOW_REFUSE: tuple[tuple[str, str], ...] = (
    # (entry key, spelling); the oracle keeps each spelling as a string.
    ("1.2-octal", "0o755"),
    ("1.2-octal", "0o7"),
    ("1.2-octal", "0o0"),
    ("1.2-leading-zero-89", "08"),
    ("1.2-leading-zero-89", "09"),
    ("1.2-leading-zero-89", "089"),
    ("1.2-leading-zero-89", "018"),
    ("1.2-exponent", "1e3"),
    ("1.2-exponent", "1E3"),
    ("1.2-exponent", "1e+3"),
    ("1.2-exponent", "1e-3"),
    ("1.2-exponent", "1.5e3"),
    ("1.2-exponent", ".5e2"),
    ("1.2-exponent", "1.e3"),
    ("1.2-exponent", "15E-3"),
    ("1.2-exponent", "1.5E3"),
    ("1.2-exponent", "0E3"),
    ("1.2-signed-dot-float", "+.5"),
    ("1.2-signed-dot-float", "-.5"),
)

_ALLOW_NAME_SAMPLE: tuple[tuple[str, str, str | None], ...] = (
    # (entry key, spelling, pinned oracle type or None when csk refuses)
    ("1.1-bool-in-name", "yes", "bool"),
    ("1.1-binary-in-name", "0b101", "int"),
    ("1.1-underscore-in-name", "1_000", "int"),
    ("1.2-octal-in-name", "0o755", None),
    ("1.2-exponent-in-name", "1e3", None),
    ("1.2-signed-dot-float-in-name", "+.5", None),
)


def _allow_corpus() -> list[Doc]:
    docs: list[Doc] = []
    for index, (key, spelling, oracle_kind) in enumerate(_ALLOW_ACCEPT):
        body = f"name: review\ndescription: {spelling}\n"
        docs.append(
            Doc(
                id=f"a-accept-{index:03d}",
                skill_text=_wrap(body),
                yaml_body=body,
                kind="allow",
                note=key,
                csk_value=spelling,
                oracle_kind=oracle_kind,
            )
        )
    for index, (key, spelling) in enumerate(_ALLOW_REFUSE):
        body = f"name: review\ndescription: {spelling}\n"
        docs.append(
            Doc(
                id=f"a-refuse-{index:03d}",
                skill_text=_wrap(body),
                yaml_body=body,
                kind="allow",
                note=key,
                csk_value=None,
                oracle_value=spelling,
                oracle_kind="str",
            )
        )
    for index, (key, spelling, oracle_kind) in enumerate(_ALLOW_NAME_SAMPLE):
        body = f"name: {spelling}\ndescription: valid description\n"
        docs.append(
            Doc(
                id=f"a-name-{index:03d}",
                skill_text=_wrap(body),
                yaml_body=body,
                kind="allow",
                note=key,
                csk_value=spelling if oracle_kind is not None else None,
                oracle_value=None if oracle_kind is not None else spelling,
                oracle_kind=oracle_kind or "str",
            )
        )
    return docs


# ---------------------------------------------------------------------------
# BOUNDARY: intended csk-subset behaviour that differs from PyYAML
# ---------------------------------------------------------------------------
#
# Each entry pins BOTH outcomes with the mandate that requires the csk side.
# None of these is a 1.1-vs-1.2 typing question, so none belongs in the
# allowlist; changing any csk side would weaken a brief-mandated gate.
#
# * B1 duplicates: rev6 grammar refuses duplicate keys; PyYAML last-wins.
# * B2 below-baseline comments: the rev8 structural-error rule refuses any
#   non-blank line with 0 < indent < baseline; PyYAML ends the scalar and
#   silently drops the comment. Fail-closed refusal keeps the gate uniform
#   with the (both-refuse) dedented-content shape.
# * B3 hash-first over-indent: leading blank more indented than a #-first
#   content line. csk refuses per the spec 8.3 uniformity rule (the text
#   version raises on both sides); PyYAML yields an empty scalar, treating
#   the #-line as a comment instead of erroring. PyYAML is internally
#   inconsistent here (raise for text, empty for #); csk matches its text
#   behaviour and the spec error rule.
# * B5 anchor/tag values: the subset has no anchors or tags (rev5 indicator
#   list); PyYAML resolves `&a value` and `! value` to strings.
# * B6 slash escape: REMOVED in rev15. The rev9 claim (that `\/` is not in
#   the YAML 1.2 section 5.7 escape table) was a spec misreading: YAML 1.2.2
#   section 5.7 productions [42]-[62] include `\/` and backslash + literal
#   TAB, and the oracle accepts both. `\/` is now a strict accept row in
#   `_QUOTED_ACCEPT` (exact oracle agreement); the literal-TAB spelling is a
#   T1 bound row (`t-tab-quote-escape`) because strict docs are tab-free by
#   construction while the oracle accepts tab-in-quotes. The B6 note id is
#   retired, never reused.
# * B7 multiline quoted: the subset is single-line quoted scalars only
#   (rev8 deviation); PyYAML folds across lines.
# * B8 multiline flow: the subset consumes single-line balanced flow only;
#   PyYAML accepts multi-line flow.
# * B10 bare merge mark: `<<` alone tag-resolves in PyYAML (1.1 merge tag)
#   and raises; csk reads it as a 1.2 plain string (merge is key-position
#   only). `<<x` and `<< x` are strings on both sides and stay strict.
# * T1 tabs: rev4 mandates tab `#` separation and rev5 allows tabs in scalar
#   trailers and block headers; the PyYAML 1.1 scanner raises on separation
#   tabs (each such doc pins csk's exact value and the oracle raise), while
#   tabs inside block-scalar content agree byte-for-byte (pinned as a pair).
# * SB1/SB2 separator line breaks: NEL (U+0085), LS (U+2028) and PS (U+2029)
#   are 1.2 content in csk but 1.1 line breaks in the oracle. YAML 1.1
#   section 5.4 treats all three as line breaks (b-break covers
#   b-next-line, b-line-separator and b-paragraph-separator; implemented in
#   PyYAML reader.py, which counts lines on '\n\x85\u2028\u2029', and
#   scanner.py scan_line_break, which maps NEL to LF and preserves LS/PS).
#   YAML 1.2.2 section 5.4 restricts b-break to CR/LF/CRLF while section
#   5.1 lists U+0085 and U+2028/U+2029 as printable content, so csk never
#   splits, folds, strips, or reinterprets them -- in every position,
#   including trailing in block scalars, where chomping acts only on
#   trailing empty lines and the final LF (clip keeps the separator AND
#   one trailing LF, strip keeps the separator only). SB1 covers
#   separators inside scalar values (plain mid/trail, quoted NEL, block
#   mid/trail, folded separator-only lines, trigger items, NEL flow
#   items); SB2 covers separators at structural positions (standalone
#   lines, leading indentation, trailers, comments, block headers,
#   under-indented and below-baseline block lines). Each row pins the
#   oracle outcome and csk's 1.2 outcome asserted directly; every other
#   separator shape (LS/PS in quotes, literal separator-only lines, the
#   `|+` LS row and the literal multi-line row, Zs everywhere) is strict
#   byte agreement, never an exception.
# * B12 mid-text U+FEFF: the oracle keeps it as content; csk refuses because
#   a BOM is permitted only as the very first character of the file.
# * B13 decoded controls in names: backslash escapes decoding to Cc values
#   are valid content at parse level but refuse at the portable-name gate.
# * B14 quoted-comment separation (rev13): a `#` comment after a quoted
#   scalar needs separation spaces (YAML 1.2 section 6.5: comments follow
#   separation whitespace; the plain-scalar and block-header rules already
#   enforce it). PyYAML accepts `"x"#c`/`'x'#c` without separation (pinned
#   oracle value `x`); csk refuses `source_member_invalid` (pinned fragment
#   `trailing content`). One/several SPACE separators agree byte-for-byte
#   (strict); TAB separators raise in the 1.1 scanner (T1).
# * B15 surrogate escapes (rev15): `\uD800`-`\uDFFF` / `\U0000D800`-style
#   escapes decode to lone surrogates, which are not Unicode scalar values
#   and not YAML `c-printable` content. The oracle yields them as-is
#   (pinned); csk refuses `source_member_invalid` fail-closed (pinned
#   fragment `surrogate`). Above-max `\U00110000` refuses on both sides and
#   stays a strict row.


def _bound_corpus() -> list[Doc]:
    docs = [
        Doc(
            id="t-dup-name",
            skill_text=_wrap("name: first\nname: second\ndescription: d\n"),
            yaml_body="name: first\nname: second\ndescription: d\n",
            kind="bound",
            note="B1",
            oracle_expect="accept",
            csk_fragment="repeats key",
        ),
        Doc(
            id="t-dup-desc",
            skill_text=_wrap("name: review\ndescription: one\ndescription: two\n"),
            yaml_body="name: review\ndescription: one\ndescription: two\n",
            kind="bound",
            note="B1",
            oracle_expect="accept",
            oracle_value="two",
            csk_fragment="repeats key",
        ),
        Doc(
            id="t-dup-other",
            skill_text=_wrap("name: review\ndescription: d\nlicense: a\nlicense: b\n"),
            yaml_body="name: review\ndescription: d\nlicense: a\nlicense: b\n",
            kind="bound",
            note="B1",
            oracle_expect="accept",
            oracle_value="d",
            csk_fragment="repeats key",
        ),
        Doc(
            id="t-below-comment-literal",
            skill_text=_wrap("name: review\ndescription: |\n    first\n  #c\n"),
            yaml_body="name: review\ndescription: |\n    first\n  #c\n",
            kind="bound",
            note="B2",
            oracle_expect="accept",
            oracle_value="first\n",
            csk_fragment="below the content indentation",
        ),
        Doc(
            id="t-below-comment-folded",
            skill_text=_wrap("name: review\ndescription: >\n    first\n  #c\n"),
            yaml_body="name: review\ndescription: >\n    first\n  #c\n",
            kind="bound",
            note="B2",
            oracle_expect="accept",
            oracle_value="first\n",
            csk_fragment="below the content indentation",
        ),
        Doc(
            id="t-below-comment-then-key",
            skill_text=_wrap("name: review\ndescription: |\n    first\n  #c\nlicense: mit\n"),
            yaml_body="name: review\ndescription: |\n    first\n  #c\nlicense: mit\n",
            kind="bound",
            note="B2",
            oracle_expect="accept",
            oracle_value="first\n",
            csk_fragment="below the content indentation",
        ),
        Doc(
            id="t-hash-first-clip",
            skill_text=_wrap("name: review\ndescription: |\n   \n  # note\n"),
            yaml_body="name: review\ndescription: |\n   \n  # note\n",
            kind="bound",
            note="B3",
            oracle_expect="accept",
            oracle_value="",
            csk_fragment="leading empty line",
        ),
        Doc(
            id="t-hash-first-keep",
            skill_text=_wrap("name: review\ndescription: |+\n      \n # note\n"),
            yaml_body="name: review\ndescription: |+\n      \n # note\n",
            kind="bound",
            note="B3",
            oracle_expect="accept",
            oracle_value="\n",
            csk_fragment="leading empty line",
        ),
        Doc(
            id="t-anchor-value",
            skill_text=_wrap("name: review\ndescription: &a hello\n"),
            yaml_body="name: review\ndescription: &a hello\n",
            kind="bound",
            note="B5",
            oracle_expect="accept",
            oracle_value="hello",
            csk_fragment="unsupported node kind",
        ),
        Doc(
            id="t-anchor-quoted",
            skill_text=_wrap('name: review\ndescription: &a "quoted"\n'),
            yaml_body='name: review\ndescription: &a "quoted"\n',
            kind="bound",
            note="B5",
            oracle_expect="accept",
            oracle_value="quoted",
            csk_fragment="unsupported node kind",
        ),
        Doc(
            id="t-tag-spaced",
            skill_text=_wrap("name: review\ndescription: ! foo\n"),
            yaml_body="name: review\ndescription: ! foo\n",
            kind="bound",
            note="B5",
            oracle_expect="accept",
            oracle_value="foo",
            csk_fragment="unsupported node kind",
        ),
        Doc(
            id="t-multiline-single",
            skill_text=_wrap("name: review\ndescription: 'aaa\n  bbb'\n"),
            yaml_body="name: review\ndescription: 'aaa\n  bbb'\n",
            kind="bound",
            note="B7",
            oracle_expect="accept",
            oracle_value="aaa bbb",
            csk_fragment="unterminated",
        ),
        Doc(
            id="t-multiline-double",
            skill_text=_wrap('name: review\ndescription: "aaa\n  bbb"\n'),
            yaml_body='name: review\ndescription: "aaa\n  bbb"\n',
            kind="bound",
            note="B7",
            oracle_expect="accept",
            oracle_value="aaa bbb",
            csk_fragment="unterminated",
        ),
        Doc(
            id="t-multiline-flow",
            skill_text=_wrap("name: review\ndescription: d\nextra: [aaa,\n  bbb]\n"),
            yaml_body="name: review\ndescription: d\nextra: [aaa,\n  bbb]\n",
            kind="bound",
            note="B8",
            oracle_expect="accept",
            oracle_value="d",
            csk_fragment="balanced",
        ),
        Doc(
            id="t-merge-mark",
            skill_text=_wrap("name: review\ndescription: <<\n"),
            yaml_body="name: review\ndescription: <<\n",
            kind="bound",
            note="B10",
            oracle_expect="raise",
            csk_value="<<",
        ),
    ]
    twin_body = "name: review\ndescription: | # note\n  text\n"
    docs.append(
        Doc(
            id="t-tab-header-literal",
            skill_text=_wrap("name: review\ndescription: |\t# note\n  text\n"),
            yaml_body="name: review\ndescription: |\t# note\n  text\n",
            kind="bound",
            note="T1",
            csk_value="text\n",
            oracle_value="text\n",
            oracle_expect="raise",
            paired_body=twin_body,
        )
    )
    twin_fold = "name: review\ndescription: >- # note\n  text\n"
    docs.append(
        Doc(
            id="t-tab-header-fold",
            skill_text=_wrap("name: review\ndescription: >-\t# note\n  text\n"),
            yaml_body="name: review\ndescription: >-\t# note\n  text\n",
            kind="bound",
            note="T1",
            csk_value="text",
            oracle_value="text",
            oracle_expect="raise",
            paired_body=twin_fold,
        )
    )
    tab_values = [
        ("t-tab-mid-plain", "a\tb", "a\tb"),
        ("t-tab-trailing-plain", "a\t", "a"),
        ("t-tab-comment", "x\t# c", "x"),
        ("t-tab-comment-glued", "x\t#", "x"),
        ("t-tab-quote-single", "'x'\t", "x"),
        ("t-tab-quote-double", '"x"\t', "x"),
        ("t-tab-quote-comment", "'review'\t# comment", "review"),
        ("t-tab-plain-comment", "review\t# comment", "review"),
    ]
    for case_id, value, expected in tab_values:
        body = f"name: review\ndescription: {value}\n"
        docs.append(
            Doc(
                id=case_id,
                skill_text=_wrap(body),
                yaml_body=body,
                kind="bound",
                note="T1",
                csk_value=expected,
                oracle_expect="raise",
            )
        )
    # Tab-indented lines refuse on both sides (consistent, pinned as a pair).
    for case_id, tab_body in (
        ("t-tab-indent-block", "name: review\ndescription: |\n\tbad\n"),
        ("t-tab-indent-key", "name: review\ndescription: d\nmetadata:\n\tbad: tab\n"),
    ):
        docs.append(
            Doc(
                id=case_id,
                skill_text=_wrap(tab_body),
                yaml_body=tab_body,
                kind="bound",
                note="T1",
                oracle_expect="raise",
                csk_fragment="tab indentation",
            )
        )
    # Tabs after the block indentation are content (spec 8.2/8.7 shapes): the
    # oracle accepts tab-in-content (only separation tabs break its scanner),
    # so both values are pinned exactly.
    docs.append(
        Doc(
            id="t-tab-block-content",
            skill_text=_wrap("name: review\ndescription: >\n \t\n detected\n"),
            yaml_body="name: review\ndescription: >\n \t\n detected\n",
            kind="bound",
            note="T1",
            csk_value="\t\ndetected\n",
            oracle_value="\t\ndetected\n",
            oracle_expect="accept",
        )
    )
    # Backslash + literal TAB inside double quotes (YAML 1.2.2 section 5.7
    # production [53], same value as `\t`): the oracle accepts tab-in-quotes,
    # so both values are pinned exactly. A T1 bound row (not strict) because
    # strict docs are tab-free by construction. The id sorts after the
    # sampled `t-tab-` window, so the entry sample is unchanged.
    docs.append(
        Doc(
            id="t-tab-quote-escape",
            skill_text=_wrap('name: review\ndescription: "a\\\tb"\n'),
            yaml_body='name: review\ndescription: "a\\\tb"\n',
            kind="bound",
            note="T1",
            csk_value="a\tb",
            oracle_value="a\tb",
            oracle_expect="accept",
        )
    )
    # B14 quoted-comment separation (rev13): a `#` comment after a quoted
    # scalar needs at least one separating SPACE/TAB. PyYAML accepts the
    # zero-separator shape (pinned oracle value); csk refuses it as
    # YAML 1.2 separation-strictness, matching the plain-scalar and
    # block-header comment rules. Each row pins the oracle accept and the
    # csk refusal fragment.
    for case_id, value in (
        ("b14-single-zero", "'x'#c"),
        ("b14-double-zero", '"x"#c'),
        ("b14-single-zero-bare", "'x'#"),
        ("b14-double-zero-bare", '"x"#'),
    ):
        body = f"name: review\ndescription: {value}\n"
        docs.append(
            Doc(
                id=case_id,
                skill_text=_wrap(body),
                yaml_body=body,
                kind="bound",
                note="B14",
                oracle_expect="accept",
                oracle_value="x",
                csk_fragment="trailing content",
            )
        )
    for case_id, value in (
        ("b14-single-zero-name", "'x'#c"),
        ("b14-double-zero-name", '"x"#c'),
    ):
        body = f"name: {value}\ndescription: valid description\n"
        docs.append(
            Doc(
                id=case_id,
                skill_text=_wrap(body),
                yaml_body=body,
                kind="bound",
                note="B14",
                oracle_expect="accept",
                oracle_name="x",
                csk_fragment="trailing content",
            )
        )
    # B15 surrogate escapes (rev15): the oracle yields the lone surrogate
    # as-is (pinned value); csk refuses fail-closed (pinned fragment).
    for case_id, value, decoded in (
        ("t-surrogate-escape-u", '"a\\uD800b"', "a\ud800b"),
        ("t-surrogate-escape-U", '"a\\U0000DC00b"', "a\udc00b"),
    ):
        body = f"name: review\ndescription: {value}\n"
        docs.append(
            Doc(
                id=case_id,
                skill_text=_wrap(body),
                yaml_body=body,
                kind="bound",
                note="B15",
                oracle_expect="accept",
                oracle_value=decoded,
                csk_fragment="surrogate",
            )
        )
    # T1 quoted separators with tabs (rev13 extension): zero/one/several
    # SPACE shapes are strict (above); every TAB shape raises in the 1.1
    # oracle scanner while csk accepts the pinned value.
    for case_id, value, expected in (
        ("t-tab-quote-single-zero-tab", "'x'\t#c", "x"),
        ("t-tab-quote-double-zero-tab", '"x"\t#c', "x"),
        ("t-tab-quote-single-two-tabs", "'x'\t\t#c", "x"),
        ("t-tab-quote-double-two-tabs", '"x"\t\t#c', "x"),
        ("t-tab-quote-single-mixed", "'x' \t #c", "x"),
        ("t-tab-quote-double-mixed", '"x"\t #c', "x"),
    ):
        body = f"name: review\ndescription: {value}\n"
        docs.append(
            Doc(
                id=case_id,
                skill_text=_wrap(body),
                yaml_body=body,
                kind="bound",
                note="T1",
                csk_value=expected,
                oracle_expect="raise",
            )
        )
    return docs


# ---------------------------------------------------------------------------
# Systematic corpus: YAML source characters (rev10)
# ---------------------------------------------------------------------------
#
# Per scalar form (plain, single-quoted, double-quoted, literal block, folded
# block) and per required field, one document per probe character:
#
# * Forbidden (strict refuse): the oracle raises ReaderError and the
#   c-printable gate refuses. Category draw U+0001 (Cc) plus U+007F, U+FFFE
#   and U+FFFF across all ten positions; extra Cc/C1 breadth (U+0000, U+0008,
#   U+000B, U+000C, U+0080, U+009F) in the description field.
# * Allowed and agreeing (strict accept, byte-exact): U+00A0, U+200B (Cf),
#   U+E000 (Co), U+0378 (Cn) and U+10FFFF across all ten positions, plus
#   double-quoted backslash escapes decoding to controls (the gate sees raw
#   source spellings, so decoded control values are valid content).
# * Separators NEL/LS/PS (SB1 or strict): csk reads them as 1.2 content
#   while the 1.1 oracle treats them as line breaks. Mid-value separators in
#   plain/block shapes (oracle splits and raises), NEL inside quotes
#   (oracle folds to a space), and trailing block separators (oracle breaks,
#   csk keeps content + LF-only chomping) are SB1 divergence rows with both
#   outcomes pinned; LS/PS inside quotes, literal separator-only lines, the
#   `|+` LS row and the literal multi-line row agree byte for byte (strict).
#   NEL names install (the installable-name gate admits NEL), so no SB1
#   row carries gate_raise for NEL.
# * U+FEFF past the file start (B12): the oracle keeps it as content, csk
#   refuses (the BOM-only-at-start mandate). Bound, both outcomes pinned.
# * Decoded controls in the name field (B13): backslash escapes decoding to
#   Cc values parse (the value is valid content) but refuse at the
#   portable-name gate. Bound with the parsed name pinned and gate_raise set.

# (character, class, note): "refuse" and "accept" are strict; "break" marks
# NEL/LS/PS, which are 1.2 content in csk but 1.1 line breaks in the oracle,
# so each form resolves to strict agreement or an SB1 divergence row (see the
# BOUNDARY citations); "feff" is bound everywhere past the file start.
_CONTROL_MATRIX: tuple[tuple[str, str, str], ...] = (
    ("\x01", "refuse", "Cc"),
    ("\u200b", "accept", "Cf"),
    ("\ue000", "accept", "Co"),
    ("\u0378", "accept", "Cn"),
    ("\x7f", "refuse", "DEL"),
    ("\x85", "break", "NEL"),
    ("\xa0", "accept", "NBSP"),
    ("\ufeff", "feff", "ZWNBSP"),
    ("\ufffe", "refuse", "noncharacter"),
    ("\uffff", "refuse", "noncharacter"),
    ("\U0010ffff", "accept", "max"),
    ("\u2028", "break", "LS"),
    ("\u2029", "break", "PS"),
)

_CONTROL_FORMS: tuple[str, ...] = ("plain", "single", "double", "literal", "folded")

_CONTROL_DESC_ONLY: tuple[str, ...] = ("\x00", "\x08", "\x0b", "\x0c", "\x80", "\x9f")


def _control_desc_body(form: str, char: str) -> str:
    if form == "plain":
        return f"name: review\ndescription: hello{char}world\n"
    if form == "single":
        return f"name: review\ndescription: 'hello{char}world'\n"
    if form == "double":
        return f'name: review\ndescription: "hello{char}world"\n'
    if form == "literal":
        return f"name: review\ndescription: |\n  hello{char}world\n"
    assert form == "folded"
    return f"name: review\ndescription: >\n  hello{char}world\n"


def _control_name_body(form: str, char: str) -> str:
    if form == "plain":
        return f"name: rev{char}iew\ndescription: valid description\n"
    if form == "single":
        return f"name: 'rev{char}iew'\ndescription: valid description\n"
    if form == "double":
        return f'name: "rev{char}iew"\ndescription: valid description\n'
    if form == "literal":
        return f"name: |\n  rev{char}iew\ndescription: valid description\n"
    assert form == "folded"
    return f"name: >\n  rev{char}iew\ndescription: valid description\n"


def _control_corpus() -> list[Doc]:
    docs: list[Doc] = []
    ref_count = 0
    acc_count = 0
    bnd_count = 0

    def add_strict(body: str, accept: bool) -> None:
        nonlocal ref_count, acc_count
        if accept:
            doc_id = f"c-acc-{acc_count:03d}"
            acc_count += 1
        else:
            doc_id = f"c-ref-{ref_count:03d}"
            ref_count += 1
        docs.append(Doc(id=doc_id, skill_text=_wrap(body), yaml_body=body, kind="strict"))

    for char, klass, _note in _CONTROL_MATRIX:
        for form in _CONTROL_FORMS:
            desc_body = _control_desc_body(form, char)
            name_body = _control_name_body(form, char)
            if klass in ("refuse", "accept"):
                add_strict(desc_body, klass == "accept")
                add_strict(name_body, klass == "accept")
            elif klass == "break":
                if form in ("single", "double") and char != "\x85":
                    # LS/PS inside quotes: both sides preserve them as
                    # content, byte for byte.
                    add_strict(desc_body, True)
                    add_strict(name_body, True)
                    continue
                if form in ("single", "double"):
                    # SB1: NEL inside quotes. The 1.1 oracle folds the break
                    # to a space; csk (1.2) keeps NEL as content. NEL names
                    # install (the gate admits NEL).
                    docs.append(
                        Doc(
                            id=f"c-bnd-{bnd_count:03d}",
                            skill_text=_wrap(desc_body),
                            yaml_body=desc_body,
                            kind="bound",
                            note="SB1",
                            oracle_expect="accept",
                            oracle_value="hello world",
                            csk_value=f"hello{char}world",
                        )
                    )
                    bnd_count += 1
                    docs.append(
                        Doc(
                            id=f"c-bnd-{bnd_count:03d}",
                            skill_text=_wrap(name_body),
                            yaml_body=name_body,
                            kind="bound",
                            note="SB1-in-name",
                            oracle_expect="accept",
                            oracle_name="rev iew",
                            csk_value="valid description",
                            csk_name=f"rev{char}iew",
                        )
                    )
                    bnd_count += 1
                    continue
                # SB1: mid-line separator in a plain scalar or block content.
                # The 1.1 oracle splits the physical line and raises; csk
                # (1.2) reads the separator as content. NEL names install
                # (the gate admits NEL).
                block = form in ("literal", "folded")
                newline = "\n" if block else ""
                docs.append(
                    Doc(
                        id=f"c-bnd-{bnd_count:03d}",
                        skill_text=_wrap(desc_body),
                        yaml_body=desc_body,
                        kind="bound",
                        note="SB1",
                        oracle_expect="raise",
                        csk_value=f"hello{char}world{newline}",
                    )
                )
                bnd_count += 1
                docs.append(
                    Doc(
                        id=f"c-bnd-{bnd_count:03d}",
                        skill_text=_wrap(name_body),
                        yaml_body=name_body,
                        kind="bound",
                        note="SB1-in-name",
                        oracle_expect="raise",
                        csk_value="valid description",
                        csk_name=f"rev{char}iew{newline}",
                    )
                )
                bnd_count += 1
            else:
                assert klass == "feff"
                # B12: the oracle keeps a mid-text U+FEFF as content; csk
                # refuses (a BOM is permitted only as the very first
                # character of the file).
                block = form in ("literal", "folded")
                oracle_desc = f"hello{char}world" + ("\n" if block else "")
                oracle_name = f"rev{char}iew" + ("\n" if block else "")
                docs.append(
                    Doc(
                        id=f"c-bnd-{bnd_count:03d}",
                        skill_text=_wrap(desc_body),
                        yaml_body=desc_body,
                        kind="bound",
                        note="B12",
                        oracle_expect="accept",
                        oracle_value=oracle_desc,
                        csk_fragment="U+FEFF",
                    )
                )
                bnd_count += 1
                docs.append(
                    Doc(
                        id=f"c-bnd-{bnd_count:03d}",
                        skill_text=_wrap(name_body),
                        yaml_body=name_body,
                        kind="bound",
                        note="B12",
                        oracle_expect="accept",
                        oracle_name=oracle_name,
                        csk_fragment="U+FEFF",
                    )
                )
                bnd_count += 1
    # Extra Cc/C1 breadth: forbidden points in every description form.
    for char in _CONTROL_DESC_ONLY:
        for form in _CONTROL_FORMS:
            add_strict(_control_desc_body(form, char), False)
    # A separator ending a block content line is 1.2 content in csk (clip
    # keeps the separator AND one trailing LF, strip the separator only)
    # but a 1.1 line break in the oracle: SB1 rows pinning both outcomes.
    # Three shapes still agree byte for byte (strict): `|+` LS (both keep
    # the separator and one LF), the literal multi-line row, and the
    # literal LS-only row.
    for trail_body in (
        "name: review\ndescription: |+\n  hello\u2028\n",
        "name: review\ndescription: |\n  one\n  two\u2028\n  three\n",
        "name: review\ndescription: |\n  \u2028\n  text\n",
    ):
        add_strict(trail_body, True)
    for trail_body, oracle_trail, csk_trail in (
        (
            "name: review\ndescription: |\n  hello\x85\n",
            "hello\n",
            "hello\x85\n",
        ),
        (
            "name: review\ndescription: >\n  hello\x85\n",
            "hello\n",
            "hello\x85\n",
        ),
        (
            "name: review\ndescription: |\n  hello\u2028\n",
            "hello\u2028",
            "hello\u2028\n",
        ),
        (
            "name: review\ndescription: >\n  hello\u2028\n",
            "hello\u2028",
            "hello\u2028\n",
        ),
        (
            "name: review\ndescription: |\n  hello\u2029\n",
            "hello\u2029",
            "hello\u2029\n",
        ),
        (
            "name: review\ndescription: >\n  hello\u2029\n",
            "hello\u2029",
            "hello\u2029\n",
        ),
        (
            "name: review\ndescription: |-\n  hello\u2028\n",
            "hello",
            "hello\u2028",
        ),
        (
            "name: review\ndescription: >-\n  hello\u2029\n",
            "hello",
            "hello\u2029",
        ),
        (
            "name: review\ndescription: >+\n  hello\x85\n",
            "hello\n\n",
            "hello\x85\n",
        ),
        (
            "name: review\ndescription: >\n  one\n  two\u2028\n  three\n",
            "one two\u2028\nthree\n",
            "one two\u2028 three\n",
        ),
        (
            "name: review\ndescription: |\n  hello\u2028  \n",
            "hello\u2028",
            "hello\u2028  \n",
        ),
    ):
        docs.append(
            Doc(
                id=f"c-bnd-{bnd_count:03d}",
                skill_text=_wrap(trail_body),
                yaml_body=trail_body,
                kind="bound",
                note="SB1",
                oracle_expect="accept",
                oracle_value=oracle_trail,
                csk_value=csk_trail,
            )
        )
        bnd_count += 1
    # Trailing separators in the name field: the oracle breaks (NEL to LF,
    # LS/PS preserved without the clip LF, strip dropping the separator)
    # while csk parses 1.2 content; the gate strips the clip LF and the
    # installable-name gate accepts (NEL included), so the installed name
    # keeps the separator.
    for name_body, oracle_trail_name, csk_trail_name in (
        (
            "name: |\n  rev\u2028\ndescription: valid description\n",
            "rev\u2028",
            "rev\u2028\n",
        ),
        (
            "name: >\n  rev\u2029\ndescription: valid description\n",
            "rev\u2029",
            "rev\u2029\n",
        ),
        (
            "name: |-\n  rev\u2028\ndescription: valid description\n",
            "rev",
            "rev\u2028",
        ),
        (
            "name: |\n  rev\x85\ndescription: valid description\n",
            "rev\n",
            "rev\x85\n",
        ),
    ):
        docs.append(
            Doc(
                id=f"c-bnd-{bnd_count:03d}",
                skill_text=_wrap(name_body),
                yaml_body=name_body,
                kind="bound",
                note="SB1-in-name",
                oracle_expect="accept",
                oracle_name=oracle_trail_name,
                csk_value="valid description",
                csk_name=csk_trail_name,
            )
        )
        bnd_count += 1
    # Escaped control values in double quotes: raw source spellings pass the
    # gate and decode to identical control values on both sides.
    for spelling in ('"a\\x01b"', '"a\\u0007b"', '"a\\0b"', '"a\\x7fb"'):
        add_strict(f"name: review\ndescription: {spelling}\n", True)
    # B13: decoded Cc values in the name field parse but refuse at the
    # portable-name gate.
    for spelling, value in (('"a\\0b"', "a\x00b"), ('"a\\x01b"', "a\x01b")):
        body = f"name: {spelling}\ndescription: valid description\n"
        docs.append(
            Doc(
                id=f"c-bnd-{bnd_count:03d}",
                skill_text=_wrap(body),
                yaml_body=body,
                kind="bound",
                note="B13",
                oracle_expect="accept",
                oracle_name=value,
                csk_value="valid description",
                csk_name=value,
                gate_raise=True,
            )
        )
        bnd_count += 1
    # U+FEFF at the start of a later line is content for the oracle (the key
    # no longer matches, so refusal is expected) and refuses at the gate.
    add_strict("name: review\n\ufeffdescription: hello\n", False)
    # U+FEFF inside a comment is ignored by the oracle but refused: B12.
    comment_body = "name: review\ndescription: hello # c\ufeffomment\n"
    docs.append(
        Doc(
            id=f"c-bnd-{bnd_count:03d}",
            skill_text=_wrap(comment_body),
            yaml_body=comment_body,
            kind="bound",
            note="B12",
            oracle_expect="accept",
            oracle_value="hello",
            csk_fragment="U+FEFF",
        )
    )
    bnd_count += 1
    return docs


# ---------------------------------------------------------------------------
# Systematic corpus: Unicode whitespace at every structural position (rev11)
# ---------------------------------------------------------------------------
#
# Every Unicode whitespace character -- the 16 non-space Zs characters plus
# NEL/LS/PS and a mid-text FEFF -- at every structural position: standalone
# line, leading indentation, between key and colon, after the colon (directly
# and spaced), before `#`, trailing and mid plain-scalar content, single- and
# double-quoted content, block mid/trailing/leading/under-indented content,
# trailing after a closing quote (bare and with a comment), in the block
# header (bare and with a comment), in trailing and full-line comments, and in
# block and flow trigger items. Description field unless noted, plus a
# name-field sample for value positions. Zs rows are strict everywhere (the
# oracle agrees: content stays content, structural Zs refuses on both sides);
# separator rows are strict where 1.1 and 1.2 agree and SB1/SB2 divergence
# rows with both outcomes pinned where the oracle's break treatment differs
# (see the BOUNDARY citations); FEFF rows are strict where the oracle also
# refuses and B12 where it keeps FEFF as content. ASCII space/tab blank-line
# controls stay positive (the tab control is bound T1: the oracle scanner
# raises on the separation tab while csk skips the blank line).

_WS_ZS: tuple[str, ...] = (
    "\u00a0",
    "\u1680",
    "\u2000",
    "\u2001",
    "\u2002",
    "\u2003",
    "\u2004",
    "\u2005",
    "\u2006",
    "\u2007",
    "\u2008",
    "\u2009",
    "\u200a",
    "\u202f",
    "\u205f",
    "\u3000",
)
_WS_SEPS: tuple[str, ...] = ("\x85", "\u2028", "\u2029")
_WS_FEFF = "\ufeff"
_WS_NAME_SAMPLE: tuple[str, ...] = ("\u00a0", "\u2003", "\u3000")


def _whitespace_corpus() -> list[Doc]:
    docs: list[Doc] = []
    ref_count = 0
    acc_count = 0
    bnd_count = 0

    def add_strict(body: str, accept: bool) -> None:
        nonlocal ref_count, acc_count
        if accept:
            doc_id = f"w-acc-{acc_count:03d}"
            acc_count += 1
        else:
            doc_id = f"w-ref-{ref_count:03d}"
            ref_count += 1
        docs.append(Doc(id=doc_id, skill_text=_wrap(body), yaml_body=body, kind="strict"))

    def add_bound(
        body: str,
        note: str,
        *,
        oracle_expect: str,
        oracle_value: str | None = None,
        oracle_name: str | None = None,
        csk_value: str | None = None,
        csk_name: str | None = None,
        csk_fragment: str = "",
        gate_raise: bool = False,
    ) -> None:
        nonlocal bnd_count
        docs.append(
            Doc(
                id=f"w-bnd-{bnd_count:03d}",
                skill_text=_wrap(body),
                yaml_body=body,
                kind="bound",
                note=note,
                oracle_expect=oracle_expect,
                oracle_value=oracle_value,
                oracle_name=oracle_name,
                csk_value=csk_value,
                csk_name=csk_name,
                csk_fragment=csk_fragment,
                gate_raise=gate_raise,
            )
        )
        bnd_count += 1

    # Standalone line between the required entries.
    for char in _WS_ZS:
        add_strict(f"name: review\n{char}\ndescription: valid\n", False)
    for char in _WS_SEPS:
        add_bound(
            f"name: review\n{char}\ndescription: valid\n",
            "SB2",
            oracle_expect="accept",
            oracle_value="valid",
            csk_fragment="not 'key: value'",
        )
    add_strict(f"name: review\n{_WS_FEFF}\ndescription: valid\n", False)
    # Leading indentation before the first key.
    for char in _WS_ZS:
        add_strict(f"{char}name: review\ndescription: valid\n", False)
    for char in _WS_SEPS:
        add_bound(
            f"{char}name: review\ndescription: valid\n",
            "SB2",
            oracle_expect="accept",
            oracle_value="valid",
            oracle_name="review",
            csk_fragment="invalid key",
        )
    add_bound(
        f"{_WS_FEFF}name: review\ndescription: valid\n",
        "B12",
        oracle_expect="accept",
        oracle_value="valid",
        oracle_name="review",
        csk_fragment="U+FEFF",
    )
    # Between the key and the colon.
    for char in (*_WS_ZS, *_WS_SEPS):
        add_strict(f"name{char}: review\ndescription: valid\n", False)
    add_strict(f"name{_WS_FEFF}: review\ndescription: valid\n", False)
    # Directly after the colon.
    for char in (*_WS_ZS, *_WS_SEPS):
        add_strict(f"name:{char}review\ndescription: valid\n", False)
    add_strict(f"name:{_WS_FEFF}review\ndescription: valid\n", False)
    # Spaced after the colon (name field: the value starts with the char).
    for char in _WS_ZS:
        add_strict(f"name: {char}review\ndescription: valid\n", True)
    for char in _WS_SEPS:
        add_bound(
            f"name: {char}review\ndescription: valid\n",
            "SB2-in-name",
            oracle_expect="raise",
            csk_value="valid",
            csk_name=f"{char}review",
        )
    add_bound(
        f"name: {_WS_FEFF}review\ndescription: valid\n",
        "B12",
        oracle_expect="accept",
        oracle_name=f"{_WS_FEFF}review",
        csk_fragment="U+FEFF",
    )
    # Before `#`: only SPACE/TAB separate a comment.
    for char in _WS_ZS:
        add_strict(f"name: review\ndescription: hello{char}# c\n", True)
    for char in _WS_SEPS:
        add_bound(
            f"name: review\ndescription: hello{char}# c\n",
            "SB1",
            oracle_expect="accept",
            oracle_value="hello",
            csk_value=f"hello{char}# c",
        )
    add_bound(
        f"name: review\ndescription: hello{_WS_FEFF}# c\n",
        "B12",
        oracle_expect="accept",
        oracle_value=f"hello{_WS_FEFF}# c",
        csk_fragment="U+FEFF",
    )
    # Trailing and mid plain-scalar content.
    for char in _WS_ZS:
        add_strict(f"name: review\ndescription: hello{char}\n", True)
        add_strict(f"name: review\ndescription: hello{char}world\n", True)
    for char in _WS_SEPS:
        add_bound(
            f"name: review\ndescription: hello{char}\n",
            "SB1",
            oracle_expect="accept",
            oracle_value="hello",
            csk_value=f"hello{char}",
        )
        add_bound(
            f"name: review\ndescription: hello{char}world\n",
            "SB1",
            oracle_expect="raise",
            csk_value=f"hello{char}world",
        )
    add_bound(
        f"name: review\ndescription: hello{_WS_FEFF}\n",
        "B12",
        oracle_expect="accept",
        oracle_value=f"hello{_WS_FEFF}",
        csk_fragment="U+FEFF",
    )
    add_bound(
        f"name: review\ndescription: hello{_WS_FEFF}world\n",
        "B12",
        oracle_expect="accept",
        oracle_value=f"hello{_WS_FEFF}world",
        csk_fragment="U+FEFF",
    )
    # Single- and double-quoted content.
    for char in _WS_ZS:
        add_strict(f"name: review\ndescription: 'hello{char}world'\n", True)
        add_strict(f'name: review\ndescription: "hello{char}world"\n', True)
    for char in _WS_SEPS:
        if char == "\x85":
            add_bound(
                f"name: review\ndescription: 'hello{char}world'\n",
                "SB1",
                oracle_expect="accept",
                oracle_value="hello world",
                csk_value=f"hello{char}world",
            )
            add_bound(
                f'name: review\ndescription: "hello{char}world"\n',
                "SB1",
                oracle_expect="accept",
                oracle_value="hello world",
                csk_value=f"hello{char}world",
            )
        else:
            add_strict(f"name: review\ndescription: 'hello{char}world'\n", True)
            add_strict(f'name: review\ndescription: "hello{char}world"\n', True)
    add_bound(
        f"name: review\ndescription: 'hello{_WS_FEFF}world'\n",
        "B12",
        oracle_expect="accept",
        oracle_value=f"hello{_WS_FEFF}world",
        csk_fragment="U+FEFF",
    )
    add_bound(
        f'name: review\ndescription: "hello{_WS_FEFF}world"\n',
        "B12",
        oracle_expect="accept",
        oracle_value=f"hello{_WS_FEFF}world",
        csk_fragment="U+FEFF",
    )
    # Block mid and trailing content.
    for char in _WS_ZS:
        add_strict(f"name: review\ndescription: |\n  hello{char}world\n", True)
        add_strict(f"name: review\ndescription: |\n  hello{char}\n", True)
    for char in _WS_SEPS:
        add_bound(
            f"name: review\ndescription: |\n  hello{char}world\n",
            "SB1",
            oracle_expect="raise",
            csk_value=f"hello{char}world\n",
        )
        # Trailing block separator: the 1.1 oracle breaks (NEL to LF, LS/PS
        # kept without the clip LF) while csk keeps 1.2 content + clip LF.
        oracle_trail = "hello\n" if char == "\x85" else f"hello{char}"
        add_bound(
            f"name: review\ndescription: |\n  hello{char}\n",
            "SB1",
            oracle_expect="accept",
            oracle_value=oracle_trail,
            csk_value=f"hello{char}\n",
        )
    add_bound(
        f"name: review\ndescription: |\n  hello{_WS_FEFF}world\n",
        "B12",
        oracle_expect="accept",
        oracle_value=f"hello{_WS_FEFF}world\n",
        csk_fragment="U+FEFF",
    )
    add_bound(
        f"name: review\ndescription: |\n  hello{_WS_FEFF}\n",
        "B12",
        oracle_expect="accept",
        oracle_value=f"hello{_WS_FEFF}\n",
        csk_fragment="U+FEFF",
    )
    # Separator-only and under-indented block lines.
    for char in _WS_ZS:
        add_strict(f"name: review\ndescription: |\n  {char}\n  text\n", True)
        add_strict(f"name: review\ndescription: |\n {char}\n  text\n", True)
    for char in _WS_SEPS:
        # Separator-only block lines: literal LS/PS still agree byte for
        # byte (the separator is a content line on both sides); literal NEL
        # and every folded shape diverge (the oracle breaks, csk folds the
        # content line), so those are SB1 rows.
        if char == "\x85":
            add_bound(
                f"name: review\ndescription: |\n  {char}\n  text\n",
                "SB1",
                oracle_expect="accept",
                oracle_value="\n\ntext\n",
                csk_value=f"{char}\ntext\n",
            )
        else:
            add_strict(f"name: review\ndescription: |\n  {char}\n  text\n", True)
        add_bound(
            f"name: review\ndescription: >\n  {char}\n  text\n",
            "SB1",
            oracle_expect="accept",
            oracle_value="\n\ntext\n" if char == "\x85" else f"{char}\ntext\n",
            csk_value=f"{char} text\n",
        )
        if char == "\x85":
            oracle_under = "\n\ntext\n"
            csk_under = "\x85\n text\n"
        else:
            oracle_under = f"{char}\ntext\n"
            csk_under = f"{char}\n text\n"
        add_bound(
            f"name: review\ndescription: |\n {char}\n  text\n",
            "SB2",
            oracle_expect="accept",
            oracle_value=oracle_under,
            csk_value=csk_under,
        )
        add_bound(
            f"name: review\ndescription: |\n    first\n  {char}\n",
            "SB2",
            oracle_expect="accept",
            oracle_value="first\n",
            csk_fragment="below the content indentation",
        )
    add_strict(f"name: review\ndescription: |\n    first\n  {_WS_FEFF}\n", False)
    add_bound(
        f"name: review\ndescription: |\n  {_WS_FEFF}\n  text\n",
        "B12",
        oracle_expect="accept",
        oracle_value=f"{_WS_FEFF}\ntext\n",
        csk_fragment="U+FEFF",
    )
    # Trailing after a closing quote, bare and with a comment.
    for char in _WS_ZS:
        add_strict(f'name: review\ndescription: "review"{char}\n', False)
        add_strict(f'name: review\ndescription: "review"{char}# c\n', False)
    for char in _WS_SEPS:
        add_bound(
            f'name: review\ndescription: "review"{char}\n',
            "SB2",
            oracle_expect="accept",
            oracle_value="review",
            csk_fragment="trailing content",
        )
        add_bound(
            f'name: review\ndescription: "review"{char}# c\n',
            "SB2",
            oracle_expect="accept",
            oracle_value="review",
            csk_fragment="trailing content",
        )
    add_strict(f'name: review\ndescription: "review"{_WS_FEFF}\n', False)
    add_strict(f'name: review\ndescription: "review"{_WS_FEFF}# c\n', False)
    # In the block header, bare and with a comment.
    for char in _WS_ZS:
        add_strict(f"name: review\ndescription: |{char}# c\n  text\n", False)
        add_strict(f"name: review\ndescription: |{char}\n  text\n", False)
    for char in _WS_SEPS:
        add_strict(f"name: review\ndescription: |{char}# c\n  text\n", False)
        add_bound(
            f"name: review\ndescription: |{char}\n  text\n",
            "SB2",
            oracle_expect="accept",
            oracle_value="\ntext\n",
            csk_fragment="invalid trailing text",
        )
    add_strict(f"name: review\ndescription: |{_WS_FEFF}# c\n  text\n", False)
    add_strict(f"name: review\ndescription: |{_WS_FEFF}\n  text\n", False)
    # In trailing and full-line comments.
    for char in _WS_ZS:
        add_strict(f"name: review\ndescription: hello # c{char}omment\n", True)
        add_strict(f"name: review\n# c{char}omment\ndescription: valid\n", True)
    for char in _WS_SEPS:
        add_bound(
            f"name: review\ndescription: hello # c{char}omment\n",
            "SB2",
            oracle_expect="raise",
            csk_value="hello",
        )
        add_bound(
            f"name: review\n# c{char}omment\ndescription: valid\n",
            "SB2",
            oracle_expect="raise",
            csk_value="valid",
        )
    add_bound(
        f"name: review\n# c{_WS_FEFF}omment\ndescription: valid\n",
        "B12",
        oracle_expect="accept",
        oracle_value="valid",
        csk_fragment="U+FEFF",
    )
    # In block and flow trigger items.
    for char in _WS_ZS:
        add_strict(f"name: review\ndescription: d\ntriggers:\n  - a{char}b\n", True)
        add_strict(f"name: review\ndescription: d\ntriggers: [a{char}b]\n", True)
    for char in _WS_SEPS:
        add_bound(
            f"name: review\ndescription: d\ntriggers:\n  - a{char}b\n",
            "SB1",
            oracle_expect="raise",
            csk_value="d",
        )
        if char == "\x85":
            add_bound(
                f"name: review\ndescription: d\ntriggers: [a{char}b]\n",
                "SB1",
                oracle_expect="accept",
                csk_value="d",
            )
        else:
            add_strict(f"name: review\ndescription: d\ntriggers: [a{char}b]\n", True)
    add_bound(
        f"name: review\ndescription: d\ntriggers:\n  - a{_WS_FEFF}b\n",
        "B12",
        oracle_expect="accept",
        csk_fragment="U+FEFF",
    )
    add_bound(
        f"name: review\ndescription: d\ntriggers: [a{_WS_FEFF}b]\n",
        "B12",
        oracle_expect="accept",
        csk_fragment="U+FEFF",
    )
    # Name-field samples for value positions.
    for char in _WS_NAME_SAMPLE:
        add_strict(f"name: rev{char}iew\ndescription: valid description\n", True)
        add_strict(f"name: 'rev{char}iew'\ndescription: valid description\n", True)
        add_strict(f"name: |\n  rev{char}iew\ndescription: valid description\n", True)
        add_strict(f"name: rev{char}\ndescription: valid description\n", True)
    for char in _WS_SEPS:
        # NEL names install (the installable-name gate admits NEL), so no
        # name-sample row carries gate_raise.
        nel = char == "\x85"
        add_bound(
            f"name: rev{char}iew\ndescription: valid description\n",
            "SB1-in-name",
            oracle_expect="raise",
            csk_value="valid description",
            csk_name=f"rev{char}iew",
        )
        if nel:
            add_bound(
                f"name: 'rev{char}iew'\ndescription: valid description\n",
                "SB1-in-name",
                oracle_expect="accept",
                oracle_name="rev iew",
                csk_value="valid description",
                csk_name=f"rev{char}iew",
            )
        else:
            add_strict(f"name: 'rev{char}iew'\ndescription: valid description\n", True)
        add_bound(
            f"name: |\n  rev{char}iew\ndescription: valid description\n",
            "SB1-in-name",
            oracle_expect="raise",
            csk_value="valid description",
            csk_name=f"rev{char}iew\n",
        )
        add_bound(
            f"name: rev{char}\ndescription: valid description\n",
            "SB1-in-name",
            oracle_expect="accept",
            oracle_name="rev",
            csk_value="valid description",
            csk_name=f"rev{char}",
        )
    add_bound(
        f"name: rev{_WS_FEFF}iew\ndescription: valid description\n",
        "B12",
        oracle_expect="accept",
        oracle_name=f"rev{_WS_FEFF}iew",
        csk_fragment="U+FEFF",
    )
    add_bound(
        f"name: 'rev{_WS_FEFF}iew'\ndescription: valid description\n",
        "B12",
        oracle_expect="accept",
        oracle_name=f"rev{_WS_FEFF}iew",
        csk_fragment="U+FEFF",
    )
    add_bound(
        f"name: |\n  rev{_WS_FEFF}iew\ndescription: valid description\n",
        "B12",
        oracle_expect="accept",
        oracle_name=f"rev{_WS_FEFF}iew\n",
        csk_fragment="U+FEFF",
    )
    # Blank-class values: ASCII-blank refuses at the gate while NBSP-only
    # is content (the gate strips spaces/tabs/line-breaks only, exactly as
    # the oracle reads these values).
    add_strict('name: review\ndescription: "   "\n', True)
    add_strict('name: review\ndescription: "\xa0"\n', True)
    add_strict("name: review\ndescription: \xa0\n", True)
    add_strict('name: review\ndescription: "\u2003"\n', True)
    add_strict('name: "\xa0"\ndescription: valid description\n', True)
    # ASCII blank-line controls stay positive (the tab control is T1: the
    # 1.1 scanner raises on the separation tab).
    add_strict("name: review\n   \ndescription: valid\n", True)
    docs.append(
        Doc(
            id=f"w-bnd-{bnd_count:03d}",
            skill_text=_wrap("name: review\n\t\ndescription: valid\n"),
            yaml_body="name: review\n\t\ndescription: valid\n",
            kind="bound",
            note="T1",
            csk_value="valid",
            oracle_expect="raise",
        )
    )
    bnd_count += 1
    return docs


# ---------------------------------------------------------------------------
# Seeded random corpus (deterministic, strict-only shapes)
# ---------------------------------------------------------------------------

_WORDS = ("alpha", "beta", "skill", "review", "delta", "helper", "tool", "kit")


def _random_blocks(rng: random.Random, count: int) -> list[Doc]:
    docs: list[Doc] = []
    made = 0
    attempts = 0
    while made < count and attempts < count * 40:
        attempts += 1
        style = rng.choice(["|", ">"])
        chomp = rng.choice(["", "-", "+"])
        explicit = rng.choice([None, None, rng.randint(1, 4)])
        header = style + chomp + (str(explicit) if explicit else "")
        if rng.random() < 0.25:
            header += " # note"
        base = explicit if explicit else rng.randint(1, 4)
        lines: list[str] = []
        for _ in range(rng.randint(1, 6)):
            roll = rng.randrange(10)
            if roll < 2:
                lines.append(rng.choice(["", " ", "  ", "    ", "      "]))
            elif roll < 7:
                indent = base + rng.choice([0, 0, 0, 1, 2, 3])
                word = rng.choice(_WORDS)
                text = rng.choice([word, f"{word} words", f"# {word}", "- item", word * 2])
                lines.append(" " * indent + text)
            elif roll < 9:
                lines.append(" " * rng.choice([0, 1, 2, 3, 4, 5, 6]))
            else:
                indent = base + rng.choice([0, 1, 2])
                lines.append(" " * indent + "#c")
        # Keep the random blocks in the strict accept space: no tabs (none
        # generated), no below-baseline lines, no hash-first over-indent.
        if _block_needs_boundary(lines, explicit):
            continue
        body = "name: review\ndescription: " + header + "\n"
        for line in lines:
            body += line + "\n"
        docs.append(
            Doc(id=f"r-block-{made:03d}", skill_text=_wrap(body), yaml_body=body, kind="strict")
        )
        made += 1
    assert made == count, f"random block generator stalled at {made}/{count}"
    return docs


def _block_needs_boundary(lines: list[str], explicit: int | None) -> bool:
    """Detect random block shapes the strict rule cannot cover (B2/B3)."""
    first_indent: int | None = None
    for line in lines:
        if line.strip(" ") != "":
            stripped = line.lstrip(" ")
            first_indent = len(line) - len(stripped)
            break
    if first_indent is None:
        return False
    baseline = explicit if explicit is not None else first_indent
    for line in lines:
        if line.strip(" ") == "":
            continue
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if indent < baseline:
            return True
    # B3 shape: over-indented leading blank with hash-first content.
    if explicit is None:
        seen_content = False
        for line in lines:
            if line.strip(" ") == "":
                if not seen_content and len(line) > first_indent:
                    # Find the first content line: hash means B3.
                    for later in lines:
                        if later.strip(" ") != "":
                            return later.lstrip(" ").startswith("#")
                continue
            seen_content = True
    return False


def _random_scalars(rng: random.Random, count: int) -> list[Doc]:
    docs: list[Doc] = []
    for made in range(count):
        word = f"{rng.choice(_WORDS)}{made}"
        roll = rng.random()
        if roll < 0.35:
            value = word if rng.random() < 0.5 else f"{word} {rng.choice(_WORDS)}"
            if rng.random() < 0.3:
                value += " # note"
        elif roll < 0.6:
            inner = word if rng.random() < 0.5 else f"it''s {word}"
            value = f"'{inner}'"
            if rng.random() < 0.3:
                value += " # note"
        elif roll < 0.8:
            inner = word if rng.random() < 0.5 else f"{word}\\n# tag"
            value = f'"{inner}"'
        else:
            value = rng.choice(
                [
                    f"key{made}: value",
                    f"- item{made}",
                    str(1000 + made),
                    f"{made}.5",
                    "true",
                    "{flow}",
                    "[flow]",
                    f"'{word}' trailing",
                ]
            )
        body = f"name: review\ndescription: {value}\n"
        docs.append(
            Doc(id=f"r-scalar-{made:03d}", skill_text=_wrap(body), yaml_body=body, kind="strict")
        )
    return docs


def _random_nested(rng: random.Random, count: int) -> list[Doc]:
    docs: list[Doc] = []
    for made in range(count):
        word = f"{rng.choice(_WORDS)}{made}"
        shape = rng.randrange(4)
        if shape == 0:
            body = f"name: review\ndescription: d\nmetadata:\n  note: {word}\n  name: inner\n"
        elif shape == 1:
            body = f"name: review\ndescription: d\ntriggers:\n  - {word}\n  - second\n"
        elif shape == 2:
            body = f"name: review\ndescription: d\nextra: [{word}, other]\n"
        else:
            body = f"name: review\ndescription: d\nallowed-tools:\n  - Read\n"
        docs.append(
            Doc(id=f"r-nested-{made:03d}", skill_text=_wrap(body), yaml_body=body, kind="strict")
        )
    return docs


def _random_framing(rng: random.Random, count: int) -> list[Doc]:
    docs: list[Doc] = []
    for made in range(count):
        word = f"{rng.choice(_WORDS)}{made}"
        body = f"name: review\ndescription: {word}\n"
        variant = rng.randrange(4)
        if variant == 0:
            skill_text = "\ufeff---\n" + body + "---\n"
        elif variant == 1:
            skill_text = "---\r\n" + body.replace("\n", "\r\n") + "---\r\n"
        elif variant == 2:
            skill_text = "---\n" + body + "...\n"
        else:
            skill_text = "---  \n" + body + "---  \n# tail\n"
        docs.append(
            Doc(
                id=f"r-framing-{made:03d}",
                skill_text=skill_text,
                yaml_body=body,
                kind="frame-ok",
            )
        )
    return docs


# ---------------------------------------------------------------------------
# Corpus assembly, guards, and tests
# ---------------------------------------------------------------------------


def _build_corpus() -> list[Doc]:
    rng = random.Random(SEED)
    docs: list[Doc] = []
    docs.extend(_plain_corpus())
    docs.extend(_plain_refuse_corpus())
    docs.extend(_quoted_corpus())
    docs.extend(_quoted_refuse_corpus())
    docs.extend(_block_corpus())
    docs.extend(_block_header_comment_corpus())
    docs.extend(_nested_corpus())
    docs.extend(_framing_corpus())
    docs.extend(_allow_corpus())
    docs.extend(_bound_corpus())
    docs.extend(_control_corpus())
    docs.extend(_whitespace_corpus())
    docs.extend(_random_blocks(rng, 80))
    docs.extend(_random_scalars(rng, 40))
    docs.extend(_random_nested(rng, 20))
    docs.extend(_random_framing(rng, 16))
    return docs


CORPUS: list[Doc] = _build_corpus()
_BY_ID: dict[str, Doc] = {doc.id: doc for doc in CORPUS}


def _guard_corpus() -> None:
    assert len(_BY_ID) == len(CORPUS), "duplicate corpus ids"
    assert len(CORPUS) >= 450, f"corpus too small: {len(CORPUS)}"
    for doc in CORPUS:
        assert doc.kind in ("strict", "allow", "bound", "frame-ok", "frame-no"), doc.id
        if doc.kind in ("strict", "allow", "frame-no"):
            assert "\t" not in doc.yaml_body, doc.id
            assert "\t" not in doc.skill_text, doc.id
        if doc.kind == "frame-ok":
            assert "\t" not in doc.yaml_body, doc.id
        if doc.kind == "bound" and doc.note != "T1":
            assert "\t" not in doc.yaml_body, doc.id
            assert "\t" not in doc.skill_text, doc.id
        if doc.kind in ("strict", "frame-ok"):
            status, loaded = _oracle(doc.yaml_body)
            if status == "ok" and loaded is not None:
                name = loaded.get("name")
                if isinstance(name, str) and name.strip(" \t\n"):
                    assert selection.is_installable_name(name.strip(" \t\n")), doc.id
                    assert len(name.strip(" \t\n")) <= 128, doc.id


_guard_corpus()


@pytest.mark.parametrize("doc_id", sorted(_BY_ID))
def test_differential_parse_level(doc_id: str) -> None:
    """Full corpus through ``_parse_frontmatter`` against the oracle."""
    assert_parse_level(_BY_ID[doc_id])


@pytest.mark.parametrize("doc_id", sorted(_BY_ID))
def test_differential_gate_level(doc_id: str, tmp_path: Path) -> None:
    """Full corpus through ``read_skill_md_name`` (oracle-driven gates)."""
    doc = _BY_ID[doc_id]
    root = tmp_path / "src"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(doc.skill_text, encoding="utf-8")
    if gate_expects_raise(doc):
        with pytest.raises(source_errors.SourceError) as excinfo:
            selection.read_skill_md_name(package, "'probe'", resolved_root=root.resolve())
        assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID, doc_id
    else:
        assert (
            selection.read_skill_md_name(package, "'probe'", resolved_root=root.resolve())
            == gate_expected_name(doc)
        ), doc_id


def test_differential_empty_block_levels(tmp_path: Path) -> None:
    """Level split: an empty block parses to {} and refuses at the gate."""
    assert selection._parse_frontmatter("---\n---\n", "'empty'") == {}
    root = tmp_path / "src"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("---\n---\n", encoding="utf-8")
    with pytest.raises(source_errors.SourceError) as excinfo:
        selection.read_skill_md_name(package, "'probe'", resolved_root=root.resolve())
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


def test_differential_invalid_utf8(tmp_path: Path) -> None:
    """Bytes-level framing refusal: invalid UTF-8 never reaches the parser."""
    root = tmp_path / "src"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_bytes(b"---\nname: \xff\xfe\ndescription: d\n---\n")
    with pytest.raises(source_errors.SourceError) as excinfo:
        selection.read_skill_md_name(package, "'probe'", resolved_root=root.resolve())
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID


_SAMPLE_PREFIXES: tuple[tuple[str, int], ...] = (
    ("p-accept-", 6),
    ("p-refuse-", 4),
    ("p-name-", 2),
    ("q-accept-", 5),
    ("q-refuse-", 3),
    ("b-sys-", 8),
    ("b-name-", 2),
    ("r-block-", 6),
    ("r-scalar-", 4),
    ("n-sys-", 5),
    ("f-ok-", 3),
    ("f-no-", 3),
    ("a-accept-", 3),
    ("a-refuse-", 3),
    ("a-name-", 2),
    ("t-tab-", 3),
    ("t-dup-", 1),
    ("t-below-", 1),
    ("t-hash-", 1),
    ("t-anchor-", 1),
    ("t-multiline-", 1),
    ("t-merge-", 1),
    ("t-tag-", 1),
    ("c-ref-", 4),
    ("c-acc-", 4),
    ("c-bnd-", 4),
    ("w-ref-", 6),
    ("w-acc-", 6),
    ("w-bnd-", 6),
)


def _entry_sample() -> list[str]:
    sample: list[str] = []
    for prefix, take in _SAMPLE_PREFIXES:
        matches = sorted(doc_id for doc_id in _BY_ID if doc_id.startswith(prefix))
        assert len(matches) >= take, prefix
        sample.extend(matches[:take])
    # Refuse-path coverage for the block/nested/random groups (oracle-driven
    # selection, deterministic and independent of corpus numbering).
    for prefix, take in (("b-sys-", 2), ("n-sys-", 2), ("r-scalar-", 2)):
        totale = sorted(
            doc_id
            for doc_id in _BY_ID
            if doc_id.startswith(prefix)
            and gate_expects_raise(_BY_ID[doc_id])
            and doc_id not in sample
        )
        assert len(totale) >= take, prefix
        sample.extend(totale[:take])
    assert len(sample) == len(set(sample))
    assert len(sample) >= 60
    return sample


@pytest.mark.parametrize("entry", ["collection", "individual"])
@pytest.mark.parametrize("doc_id", _entry_sample())
def test_differential_entry_points(tmp_path: Path, entry: str, doc_id: str) -> None:
    """Corpus sample through both public entry points on filesystem fixtures."""
    doc = _BY_ID[doc_id]
    root = tmp_path / "source"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(doc.skill_text, encoding="utf-8")
    if gate_expects_raise(doc):
        with pytest.raises(source_errors.SourceError) as excinfo:
            if entry == "collection":
                expand_collection(
                    root,
                    CollectionSelector(from_alias="local", directory=".", include=("*",), exclude=()),
                )
            else:
                resolve_individual(
                    root,
                    IndividualSelector(name="review", from_alias="local", directory="pkg"),
                )
        assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID, doc_id
        return
    expected = gate_expected_name(doc)
    if entry == "collection":
        members = expand_collection(
            root,
            CollectionSelector(from_alias="local", directory=".", include=("*",), exclude=()),
        )
        assert [member.name for member in members] == [expected], doc_id
    else:
        selected = resolve_individual(
            root,
            IndividualSelector(name=expected, from_alias="local", directory="pkg"),
        )
        assert selected.name == expected, doc_id


def test_runtime_never_imports_yaml() -> None:
    """The runtime package stays standard-library-only (PyYAML is test-only)."""
    roots = [Path(__file__).resolve().parent.parent / "src" / "csk"]
    offenders: list[str] = []
    for base in roots:
        for path in sorted(base.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("import yaml") or stripped.startswith("from yaml"):
                    offenders.append(f"{path}:{lineno}: {stripped}")
    assert offenders == []
