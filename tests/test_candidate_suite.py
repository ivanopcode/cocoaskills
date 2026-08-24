from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import commit_all, init_git_repo, load_candidate_suite, write_files


candidate_suite = load_candidate_suite()
CandidateError = candidate_suite.CandidateError

ROOT = Path(__file__).parents[1]
DESCRIPTOR = ROOT / ".github" / "ci" / "candidate-suite.json"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PIN = "a" * 40
OTHER_REVISION = "b" * 40


def _workflow(tmp_path: Path, pin: str = PIN, *, rows: int = 1) -> Path:
    body = "".join(f"  RELEASED_SUITE_PIN: {pin}\n" for _ in range(rows))
    path = tmp_path / "ci.yml"
    path.write_text(f"env:\n{body}\njobs:\n", encoding="utf-8")
    return path


def _suite(tmp_path: Path, *, protocol: str = "1.0.0-rc.8") -> tuple[Path, Path, str]:
    """Build a throwaway candidate checkout and return checkout, root, revision."""
    checkout = init_git_repo(tmp_path / "candidate")
    write_files(
        checkout,
        {
            "conformance/v1/manifest.json": json.dumps(
                {"protocol_version": protocol, "files": []}, indent=2
            )
            + "\n",
            "conformance/v1/expected/case.json": '{"case": 1}\n',
            "README.md": "candidate\n",
        },
    )
    revision = commit_all(checkout)
    return checkout, checkout / "conformance" / "v1", revision


def _descriptor(tmp_path: Path, root: Path, head: str, **overrides: str) -> Path:
    tree, _ = candidate_suite.tree_digest(root)
    declared = {
        "repository": "relux-works/curator-spec",
        "revision": head,
        "manifest_sha256": f"sha256:{candidate_suite.sha256_of(root / 'manifest.json')}",
        "tree_sha256": f"sha256:{tree}",
        "protocol_version": "1.0.0-rc.8",
    }
    declared.update(overrides)
    path = tmp_path / "candidate-suite.json"
    path.write_text(json.dumps(declared, indent=2) + "\n", encoding="utf-8")
    return path


def _clear_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in candidate_suite.OVERRIDE_ENV.values():
        monkeypatch.delenv(name, raising=False)


def _resolve(descriptor: Path, workflow: Path, **env: str):
    return candidate_suite.resolve(
        descriptor_path=descriptor, workflow_path=workflow, env=env
    )


# --- the committed declaration ------------------------------------------------


def test_committed_declaration_is_an_immutable_non_pin_candidate() -> None:
    candidate = candidate_suite.resolve(env={})
    pin = candidate_suite.released_pin()

    assert candidate.source == "committed-descriptor"
    assert candidate.repository == "relux-works/curator-spec"
    assert candidate_suite.REVISION_RE.match(candidate.revision)
    assert candidate.revision != pin
    assert candidate_suite.DIGEST_RE.match(candidate.manifest_sha256)
    assert candidate_suite.DIGEST_RE.match(candidate.tree_sha256)
    assert candidate.protocol_version


def test_released_pin_is_declared_exactly_once_in_the_workflow(tmp_path: Path) -> None:
    assert candidate_suite.PIN_RE.findall(WORKFLOW.read_text(encoding="utf-8")) == [
        candidate_suite.released_pin()
    ]

    with pytest.raises(CandidateError, match="exactly one RELEASED_SUITE_PIN"):
        candidate_suite.released_pin(_workflow(tmp_path, rows=2))
    with pytest.raises(CandidateError, match="workflow does not exist"):
        candidate_suite.released_pin(tmp_path / "absent" / "ci.yml")


def test_no_rc6_literal_survives_outside_the_declaration() -> None:
    declared = json.loads(DESCRIPTOR.read_text(encoding="utf-8"))
    revision = declared["revision"]
    digest = declared["manifest_sha256"].removeprefix("sha256:")

    for path in (WORKFLOW, ROOT / "tests" / "conftest.py"):
        text = path.read_text(encoding="utf-8")
        assert revision not in text, path
        assert digest not in text, path


# --- declaration parsing ------------------------------------------------------


def test_resolve_reads_the_declared_identity(tmp_path: Path) -> None:
    _, root, revision = _suite(tmp_path)
    candidate = _resolve(_descriptor(tmp_path, root, revision), _workflow(tmp_path))

    assert candidate.source == "committed-descriptor"
    assert candidate.revision == revision
    assert candidate.protocol_version == "1.0.0-rc.8"
    assert candidate.manifest_sha256 == candidate_suite.sha256_of(root / "manifest.json")


def test_declaration_without_a_tree_digest_still_resolves(tmp_path: Path) -> None:
    _, root, revision = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, revision, tree_sha256="")

    assert _resolve(descriptor, _workflow(tmp_path)).tree_sha256 == ""


@pytest.mark.parametrize("field", ["repository", "revision", "manifest_sha256", "protocol_version"])
def test_declaration_missing_a_required_field_fails_closed(
    tmp_path: Path, field: str
) -> None:
    _, root, revision = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, revision)
    payload = json.loads(descriptor.read_text(encoding="utf-8"))
    payload[field] = "  "
    descriptor.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CandidateError, match=field):
        _resolve(descriptor, _workflow(tmp_path))


def test_unreadable_declaration_fails_closed(tmp_path: Path) -> None:
    workflow = _workflow(tmp_path)
    with pytest.raises(CandidateError, match="does not exist"):
        _resolve(tmp_path / "absent.json", workflow)

    broken = tmp_path / "broken.json"
    broken.write_text("[]", encoding="utf-8")
    with pytest.raises(CandidateError, match="must be a JSON object"):
        _resolve(broken, workflow)

    broken.write_text("{", encoding="utf-8")
    with pytest.raises(CandidateError, match="not valid JSON"):
        _resolve(broken, workflow)


# --- immutability -------------------------------------------------------------


@pytest.mark.parametrize(
    "revision",
    [
        "main",
        "v1.0.0-rc.8",
        "HEAD",
        "b" * 7,
        "b" * 39,
        "b" * 41,
        "B" * 40,
        "${{ vars.CSK_E2E_CURATOR_SPEC_SHA }}",
    ],
)
def test_non_immutable_revisions_are_rejected(tmp_path: Path, revision: str) -> None:
    _, root, declared = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, declared, revision=revision)

    with pytest.raises(CandidateError, match="40-character lowercase commit id"):
        _resolve(descriptor, _workflow(tmp_path))


def test_the_null_commit_is_rejected(tmp_path: Path) -> None:
    _, root, declared = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, declared, revision="0" * 40)

    with pytest.raises(CandidateError, match="null commit"):
        _resolve(descriptor, _workflow(tmp_path))


def test_a_candidate_may_not_impersonate_the_released_pin(tmp_path: Path) -> None:
    _, root, declared = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, declared, revision=PIN)

    with pytest.raises(CandidateError, match="equals the released suite pin"):
        _resolve(descriptor, _workflow(tmp_path))


@pytest.mark.parametrize("digest", ["deadbeef", "sha256:" + "z" * 64, "A" * 64, ""])
def test_malformed_manifest_digests_are_rejected(tmp_path: Path, digest: str) -> None:
    _, root, revision = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, revision, manifest_sha256=digest)

    with pytest.raises(CandidateError):
        _resolve(descriptor, _workflow(tmp_path))


def test_the_sha256_prefix_is_optional(tmp_path: Path) -> None:
    _, root, revision = _suite(tmp_path)
    bare = candidate_suite.sha256_of(root / "manifest.json")
    descriptor = _descriptor(tmp_path, root, revision, manifest_sha256=bare)

    assert _resolve(descriptor, _workflow(tmp_path)).manifest_sha256 == bare


# --- dispatch overrides -------------------------------------------------------


def test_an_override_replaces_the_whole_declared_identity(tmp_path: Path) -> None:
    _, root, revision = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, revision)
    candidate = _resolve(
        descriptor,
        _workflow(tmp_path),
        CANDIDATE_REF=OTHER_REVISION,
        CANDIDATE_MANIFEST_SHA256="c" * 64,
    )

    assert candidate.source == "workflow-dispatch-input"
    assert candidate.revision == OTHER_REVISION
    assert candidate.manifest_sha256 == "c" * 64
    # The declared expectations belong to the declared revision, not this one.
    assert candidate.protocol_version == ""
    assert candidate.tree_sha256 == ""


def test_an_override_may_carry_its_own_protocol_version(tmp_path: Path) -> None:
    _, root, revision = _suite(tmp_path)
    candidate = _resolve(
        _descriptor(tmp_path, root, revision),
        _workflow(tmp_path),
        CANDIDATE_REF=OTHER_REVISION,
        CANDIDATE_MANIFEST_SHA256="c" * 64,
        CANDIDATE_PROTOCOL_VERSION="1.0.0-rc.9",
    )

    assert candidate.protocol_version == "1.0.0-rc.9"


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({"CANDIDATE_REF": OTHER_REVISION}, "CANDIDATE_MANIFEST_SHA256"),
        ({"CANDIDATE_MANIFEST_SHA256": "c" * 64}, "CANDIDATE_REF"),
        ({"CANDIDATE_PROTOCOL_VERSION": "1.0.0-rc.9"}, "CANDIDATE_MANIFEST_SHA256"),
    ],
)
def test_a_partial_override_is_rejected(
    tmp_path: Path, env: dict[str, str], missing: str
) -> None:
    _, root, revision = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, revision)

    with pytest.raises(CandidateError, match=missing):
        _resolve(descriptor, _workflow(tmp_path), **env)


def test_blank_overrides_fall_back_to_the_declaration(tmp_path: Path) -> None:
    _, root, revision = _suite(tmp_path)
    candidate = _resolve(
        _descriptor(tmp_path, root, revision),
        _workflow(tmp_path),
        CANDIDATE_REF="",
        CANDIDATE_MANIFEST_SHA256="",
        CANDIDATE_PROTOCOL_VERSION="",
    )

    assert candidate.source == "committed-descriptor"
    assert candidate.revision == revision


# --- authentication -----------------------------------------------------------


def test_authenticate_measures_the_materialised_candidate(tmp_path: Path) -> None:
    checkout, root, revision = _suite(tmp_path)
    candidate = _resolve(_descriptor(tmp_path, root, revision), _workflow(tmp_path))

    measured = candidate_suite.authenticate(candidate, checkout=checkout, root=root)

    assert measured["revision"] == revision
    assert measured["protocol_version"] == "1.0.0-rc.8"
    assert measured["manifest_sha256"] == candidate.manifest_sha256
    assert measured["tree_sha256"] == candidate.tree_sha256
    assert measured["file_count"] == "2"


def test_a_checkout_at_another_revision_is_rejected(tmp_path: Path) -> None:
    checkout, root, revision = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, revision)
    candidate = _resolve(descriptor, _workflow(tmp_path))
    (checkout / "README.md").write_text("moved on\n", encoding="utf-8")
    commit_all(checkout, "second")

    with pytest.raises(CandidateError, match="checkout revision mismatch"):
        candidate_suite.authenticate(candidate, checkout=checkout, root=root)


def test_a_re_baselined_manifest_is_rejected(tmp_path: Path) -> None:
    checkout, root, revision = _suite(tmp_path)
    candidate = _resolve(_descriptor(tmp_path, root, revision), _workflow(tmp_path))
    (root / "manifest.json").write_text(
        json.dumps({"protocol_version": "1.0.0-rc.8", "files": [1]}), encoding="utf-8"
    )

    with pytest.raises(CandidateError, match="never re-baseline it silently"):
        candidate_suite.authenticate(candidate, checkout=checkout, root=root)


def test_a_tampered_vector_is_rejected_by_the_tree_digest(tmp_path: Path) -> None:
    checkout, root, revision = _suite(tmp_path)
    candidate = _resolve(_descriptor(tmp_path, root, revision), _workflow(tmp_path))
    (root / "expected" / "case.json").write_text('{"case": 2}\n', encoding="utf-8")

    with pytest.raises(CandidateError, match="tree digest mismatch"):
        candidate_suite.authenticate(candidate, checkout=checkout, root=root)


def test_a_declared_tree_digest_is_optional(tmp_path: Path) -> None:
    checkout, root, revision = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, revision, tree_sha256="")
    candidate = _resolve(descriptor, _workflow(tmp_path))
    (root / "expected" / "case.json").write_text('{"case": 2}\n', encoding="utf-8")

    measured = candidate_suite.authenticate(candidate, checkout=checkout, root=root)
    assert measured["tree_sha256"]


def test_a_protocol_version_mismatch_is_rejected(tmp_path: Path) -> None:
    checkout, root, revision = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, revision, protocol_version="1.0.0-rc.9")
    candidate = _resolve(descriptor, _workflow(tmp_path))

    with pytest.raises(CandidateError, match="protocol version mismatch"):
        candidate_suite.authenticate(candidate, checkout=checkout, root=root)


def test_an_absent_or_empty_root_is_rejected(tmp_path: Path) -> None:
    checkout, root, revision = _suite(tmp_path)
    candidate = _resolve(_descriptor(tmp_path, root, revision), _workflow(tmp_path))

    with pytest.raises(CandidateError, match="not a directory"):
        candidate_suite.authenticate(
            candidate, checkout=checkout, root=tmp_path / "absent"
        )

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(CandidateError, match="no manifest.json"):
        candidate_suite.authenticate(candidate, checkout=checkout, root=empty)


def test_a_root_outside_a_git_checkout_is_rejected(tmp_path: Path) -> None:
    _, root, revision = _suite(tmp_path)
    candidate = _resolve(_descriptor(tmp_path, root, revision), _workflow(tmp_path))

    with pytest.raises(CandidateError, match="not a git repository"):
        candidate_suite.authenticate(candidate, checkout=tmp_path, root=root)


# --- evidence -----------------------------------------------------------------


def test_recorded_evidence_is_stamped_candidate_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_overrides(monkeypatch)
    checkout, root, revision = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, revision)
    evidence = tmp_path / "out" / "candidate-suite-identity.txt"

    assert (
        candidate_suite.main(
            [
                "record",
                "--descriptor",
                str(descriptor),
                "--checkout",
                str(checkout),
                "--root",
                str(root),
                "--evidence",
                str(evidence),
            ]
        )
        == 0
    )

    text = evidence.read_text(encoding="utf-8")
    assert text.startswith("# CANDIDATE PROTOCOL SUITE EVIDENCE -- NOT A RELEASE")
    assert "evidence_class          candidate-only" in text
    assert "release_claim           none" in text
    assert "conformance_claim       none" in text
    assert f"candidate_revision      {revision}" in text
    assert f"released_suite_pin      {candidate_suite.released_pin()}" in text
    assert "file_count              2" in text


def test_resolve_writes_step_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, root, revision = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, revision)
    outputs = tmp_path / "outputs.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    _clear_overrides(monkeypatch)

    assert candidate_suite.main(["resolve", "--descriptor", str(descriptor)]) == 0

    written = dict(
        line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines()
    )
    assert written["revision"] == revision
    assert written["repository"] == "relux-works/curator-spec"
    assert written["source"] == "committed-descriptor"


def test_a_rejected_candidate_exits_non_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_overrides(monkeypatch)
    _, root, revision = _suite(tmp_path)
    descriptor = _descriptor(tmp_path, root, revision, revision="main")

    assert candidate_suite.main(["resolve", "--descriptor", str(descriptor)]) == 1
    assert "candidate-suite:" in capsys.readouterr().err
