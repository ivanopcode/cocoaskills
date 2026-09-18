"""Byte-exact refusal texts for the hoisted f-strings (BUG-260917-3txerf).

``src/csk/sources/selection.py`` must parse on the declared Python floor
(``requires-python >= 3.11``), so the PEP-701-only f-string expressions with
a backslash inside the braces were hoisted into locals. The rendered
``SourceError`` details are refusal surface: this test pins them
byte-exactly, so any rewording or any change to what is stripped breaks it.

Each of the seven sites carries five cases: the base refusal plus trailing
``[" ", "\\t", " \\t", "\\t "]`` twins that all render the identical detail.
The twins pin the stripped charset to exactly space-and-tab -- narrowing any
site to ``strip(" ")`` fails its tab-carrying twins, narrowing to
``strip("\\t")`` fails its space-carrying twins. At the ``raw_after``/``raw``
sites leading blanks survive slicing, so the base case already distinguishes
``strip("\\t")``; at the three ``raw[pos:]`` sites (sequence, explicit key,
unsupported node) ``parse_frontmatter_scalar`` advances ``pos`` past leading
whitespace before slicing, so only a trailing space distinguishes the two
charsets there -- which is why the trailing-space twins are required.

The floor-parse property itself cannot be tested from this interpreter (an
``ast.parse`` with ``feature_version`` does not re-impose the old f-string
rule); it is guarded by the ``floor_syntax`` CI job, which compiles the
package with the real floor interpreter resolved from ``requires-python``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from csk.sources import errors as source_errors
from csk.sources import selection


# (suffix tag, trailing characters appended before the newline).
_TRAILING: tuple[tuple[str, str], ...] = (
    ("base", ""),
    ("sp", " "),
    ("tab", "\t"),
    ("sp-tab", " \t"),
    ("tab-sp", "\t "),
)

# (site, SKILL.md maker from the trailing suffix, expected detail).
_SITES: tuple[tuple[str, Callable[[str], str], str], ...] = (
    (
        "chomping",
        lambda suffix: f"---\nname: review\ndescription: |+-{suffix}\n  text\n---\n",
        "Skill member 'probe' SKILL.md frontmatter block header "
        "'|+-' repeats the chomping indicator",
    ),
    (
        "indent",
        lambda suffix: f"---\nname: review\ndescription: |22{suffix}\n  text\n---\n",
        "Skill member 'probe' SKILL.md frontmatter block header "
        "'|22' repeats the indentation indicator",
    ),
    (
        "trailing",
        lambda suffix: f"---\nname: review\ndescription: | x{suffix}\n  text\n---\n",
        "Skill member 'probe' SKILL.md frontmatter block header "
        "'| x' carries invalid trailing text",
    ),
    (
        "below-baseline",
        lambda suffix: f"---\nname: review\ndescription: >\n  text\n text{suffix}\n---\n",
        "Skill member 'probe' SKILL.md frontmatter block scalar "
        "line 'text' is indented below the content indentation (2)",
    ),
    (
        "sequence",
        lambda suffix: f"---\nname: review\ndescription: - item{suffix}\n---\n",
        "Skill member 'probe' SKILL.md frontmatter value "
        "'- item' is a sequence entry, not a string",
    ),
    (
        "explicit",
        lambda suffix: f"---\nname: review\ndescription: ? key{suffix}\n---\n",
        "Skill member 'probe' SKILL.md frontmatter value "
        "'? key' is an explicit mapping key entry, not a string",
    ),
    (
        "unsupported",
        lambda suffix: f"---\nname: review\ndescription: &a hello{suffix}\n---\n",
        "Skill member 'probe' SKILL.md frontmatter value "
        "'&a hello' is an unsupported node kind, not a string",
    ),
)

# (case_id, SKILL.md text, expected SourceError.detail for label 'probe').
_REFUSAL_CASES: tuple[tuple[str, str, str], ...] = tuple(
    (f"{site}-{tag}", maker(suffix), expected)
    for site, maker, expected in _SITES
    for tag, suffix in _TRAILING
)


@pytest.mark.posix_traversal_independent  # direct parser probe, no filesystem
@pytest.mark.parametrize(
    "case_id,text,expected",
    [pytest.param(case_id, text, expected, id=case_id) for case_id, text, expected in _REFUSAL_CASES],
)
def test_refusal_detail_byte_exact(case_id: str, text: str, expected: str) -> None:
    """Parser probes: byte-exact refusal details over the production function.

    The entry points (``resolve_individual`` / ``expand_collection``) refuse
    these inputs with the same ``SourceError`` raised here by
    ``selection._parse_frontmatter`` (via ``read_skill_md_name``); entry-level
    refusal codes are asserted by the existing header/spec tables, while this
    test pins the full detail text each hoisted f-string renders.
    """

    _ = case_id
    with pytest.raises(source_errors.SourceError) as excinfo:
        selection._parse_frontmatter(text, "'probe'")
    assert excinfo.value.code == source_errors.CODE_MEMBER_INVALID
    assert excinfo.value.detail == expected
