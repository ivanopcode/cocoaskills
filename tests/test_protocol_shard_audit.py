"""Keep the audited protocol surface honest on every pull request.

The deterministic Windows protocol shards are selected by
`.research/TASK-260803-2ol7ok_verify-protocol-shards.py`, which fails closed
when any audited test file drifts from the sha256 recorded in the
classification. That verifier runs only in `merge_protocol`, which is gated on
a push to `main`, so drift used to reach `main` before anything reported it.
These tests re-measure the same bytes with the verifier's own code and run in
the ordinary suite, which the fast tier executes on the pull request.

The measurement is over raw bytes, so it is only meaningful when the working
tree carries the committed bytes. `.gitattributes` pins `*.py` to LF for that
reason; without it a Windows checkout converts every audited file to CRLF and
the audit reports drift that does not exist.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).parents[1]
RESEARCH = ROOT / ".research"
VERIFIER_SCRIPT = RESEARCH / "TASK-260803-2ol7ok_verify-protocol-shards.py"
CLASSIFICATION = RESEARCH / "TASK-260803-2ol7ok_protocol-isolation-classification.json"
GITATTRIBUTES = ROOT / ".gitattributes"
LF_RULE = "*.py text eol=lf"
AUDITED_FILES = {
    "tests/conftest.py",
    "tests/protocol_conformance_adapters.py",
    "tests/protocol_lifecycle_observations.py",
    "tests/test_protocol_conformance.py",
}


def load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_protocol_shards", VERIFIER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def audited_hashes() -> dict[str, str]:
    classification = json.loads(CLASSIFICATION.read_text(encoding="utf-8"))
    hashes = classification["audited_files_sha256"]
    assert isinstance(hashes, dict)
    return hashes


def test_audited_inventory_covers_the_whole_protocol_surface() -> None:
    """A file may not leave the audit by being deleted from the classification."""
    assert set(audited_hashes()) == AUDITED_FILES


def test_every_audited_file_still_matches_its_pinned_digest() -> None:
    verifier = load_verifier()
    verifier.verify_audited_hashes(ROOT, audited_hashes())


@pytest.mark.parametrize("relative", sorted(AUDITED_FILES))
def test_audited_digest_is_the_digest_of_the_file_in_the_tree(relative: str) -> None:
    raw = (ROOT / relative).read_bytes()
    measured = hashlib.sha256(raw).hexdigest()
    pinned = audited_hashes()[relative]
    if measured != pinned:
        # Separate a checkout artifact from real drift. Re-pinning a CRLF
        # digest would fix nothing and break every LF platform instead.
        normalized = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
        assert normalized != pinned, (
            f"{relative} was checked out with CRLF line endings, so its bytes no "
            f"longer match the pinned digest. The audit book is correct and the "
            f"checkout is not: restore the {LF_RULE!r} rule in .gitattributes and "
            f"check the tree out again. Do not re-pin to {measured}."
        )
    assert pinned == measured, (
        f"{relative} changed without re-auditing the Windows protocol shard "
        f"isolation; update audited_files_sha256 in {CLASSIFICATION.name} to "
        f"{measured} and state why the change is isolation-neutral"
    )


def test_audited_bytes_survive_checkout_on_every_platform() -> None:
    """The byte pins only hold while the checkout stops rewriting line endings."""
    rules = [
        line.split("#", 1)[0].strip()
        for line in GITATTRIBUTES.read_text(encoding="utf-8").splitlines()
    ]
    assert LF_RULE in rules, f".gitattributes must pin the audited surface: {LF_RULE}"
    assert all(relative.endswith(".py") for relative in AUDITED_FILES), (
        f"an audited file is outside the {LF_RULE!r} rule; extend .gitattributes"
    )
    for relative in sorted(AUDITED_FILES):
        assert b"\r" not in (ROOT / relative).read_bytes(), (
            f"{relative} carries CR bytes; this checkout ignored the {LF_RULE!r} "
            "rule in .gitattributes and the audited digests cannot be trusted"
        )


def test_the_guard_fails_closed_on_drift(tmp_path: Path) -> None:
    """The guard must reject a mutated audited file, not just accept a clean one."""
    verifier = load_verifier()
    for relative in sorted(AUDITED_FILES):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    (tmp_path / "tests" / "conftest.py").write_bytes(b"# mutated\n")

    with pytest.raises(ValueError) as error:
        verifier.verify_audited_hashes(tmp_path, audited_hashes())
    assert "audited file hash mismatch: tests/conftest.py" in str(error.value)
