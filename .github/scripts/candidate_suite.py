"""Fail-closed candidate protocol-suite input for the CocoaSkills Go E2E lanes.

The released conformance suite is pinned once, in ``RELEASED_SUITE_PIN`` in
``.github/workflows/ci.yml``, and every default protocol lane runs against that
revision. The Go E2E lanes additionally consume a *candidate* suite, and this
module is the only place that decides which candidate that is.

A candidate is an explicit pair: a full 40-hex ``relux-works/curator-spec``
revision and the sha256 of ``conformance/v1/manifest.json`` at that revision.
The pair is declared together in ``.github/ci/candidate-suite.json`` and can be
overridden together through ``workflow_dispatch`` inputs. Half a pair, a branch,
a tag, a short hash, the null commit or the released pin itself are all rejected
before anything is fetched, so a candidate can never be re-baselined silently
and a candidate run can never impersonate the qualified released pin.

What the candidate produces is candidate evidence: the recorded identity states,
in the artifact itself, that it is neither a published release nor a conformance
claim.

Usage::

    candidate_suite.py resolve
    candidate_suite.py record --checkout <dir> --root <dir> --evidence <file>

Environment:
    CANDIDATE_REF                 override revision, full 40-hex (optional)
    CANDIDATE_MANIFEST_SHA256     override manifest digest (optional)
    CANDIDATE_PROTOCOL_VERSION    override protocol version (optional)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DESCRIPTOR_PATH = Path(__file__).parents[1] / "ci" / "candidate-suite.json"
WORKFLOW_PATH = Path(__file__).parents[1] / "workflows" / "ci.yml"
PIN_RE = re.compile(r"^  RELEASED_SUITE_PIN: (?P<pin>[0-9a-f]{40})$", re.MULTILINE)
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
NULL_REVISION = "0" * 40
DESCRIPTOR_FIELDS = ("repository", "revision", "manifest_sha256", "protocol_version")
OVERRIDE_ENV = {
    "revision": "CANDIDATE_REF",
    "manifest_sha256": "CANDIDATE_MANIFEST_SHA256",
    "protocol_version": "CANDIDATE_PROTOCOL_VERSION",
}
EVIDENCE_HEADER = (
    "# CANDIDATE PROTOCOL SUITE EVIDENCE -- NOT A RELEASE",
    "#",
    "# This file records the identity of the explicitly supplied candidate",
    "# protocol suite the Go E2E lanes ran against. It is NOT a published",
    "# release, NOT a release claim and NOT a conformance claim, and it must",
    "# not be cited as any of them. The committed released pin is unchanged by",
    "# this run.",
    "#",
)


class CandidateError(RuntimeError):
    """The candidate suite input is ambiguous, non-immutable or unverified."""


@dataclass(frozen=True)
class CandidateInput:
    """The declared identity of one candidate protocol suite."""

    repository: str
    revision: str
    manifest_sha256: str
    protocol_version: str
    tree_sha256: str
    source: str


def normalize_digest(raw: str, *, field: str) -> str:
    """Accept an optional ``sha256:`` prefix and reject anything else."""
    digest = raw.strip()
    if digest.startswith("sha256:"):
        digest = digest[len("sha256:") :]
    if not DIGEST_RE.match(digest):
        raise CandidateError(
            f"{field} must be a 64-character lowercase sha256 digest, got {raw!r}"
        )
    return digest


def verify_revision(revision: str, *, pin: str) -> str:
    """Reject every revision that is not an immutable, non-pin commit id."""
    if not revision:
        raise CandidateError("candidate revision is empty")
    if not REVISION_RE.match(revision):
        raise CandidateError(
            f"candidate revision must be a full 40-character lowercase commit id, "
            f"got {revision!r} (a branch, tag, short hash or placeholder is never "
            f"a valid candidate pin)"
        )
    if revision == NULL_REVISION:
        raise CandidateError("candidate revision is the null commit")
    if revision == pin:
        raise CandidateError(
            f"candidate revision equals the released suite pin {pin}; "
            f"a candidate run must not impersonate the qualified pin"
        )
    return revision


def released_pin(workflow_path: Path = WORKFLOW_PATH) -> str:
    """Read the committed released pin straight from the workflow that uses it.

    The pin is declared exactly once, so a workflow that lost it or grew a
    second declaration fails closed instead of leaving the anti-impersonation
    check silently unenforced.
    """
    if not workflow_path.is_file():
        raise CandidateError(f"workflow does not exist: {workflow_path}")
    found = PIN_RE.findall(workflow_path.read_text(encoding="utf-8"))
    if len(found) != 1:
        raise CandidateError(
            f"workflow must declare exactly one RELEASED_SUITE_PIN row as a full "
            f"40-character lowercase commit id, found {len(found)}: {workflow_path}"
        )
    return str(found[0])


def load_descriptor(path: Path) -> dict[str, str]:
    """Load the committed candidate declaration, or fail closed."""
    if not path.is_file():
        raise CandidateError(f"candidate descriptor does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CandidateError(f"candidate descriptor is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CandidateError("candidate descriptor must be a JSON object")
    declared: dict[str, str] = {}
    for field in DESCRIPTOR_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise CandidateError(
                f"candidate descriptor field is missing or not a string: {field}"
            )
        declared[field] = value.strip()
    tree = payload.get("tree_sha256", "")
    if not isinstance(tree, str):
        raise CandidateError("candidate descriptor field tree_sha256 must be a string")
    declared["tree_sha256"] = tree.strip()
    return declared


def read_overrides(env: Mapping[str, str]) -> dict[str, str]:
    """Collect the supplied ``workflow_dispatch`` overrides.

    The revision and its manifest digest are one identity. Supplying one without
    the other would let the run name a revision while measuring nothing, so the
    partial combination is rejected before any checkout happens.
    """
    supplied = {
        field: env.get(name, "").strip() for field, name in OVERRIDE_ENV.items()
    }
    given = {field for field, value in supplied.items() if value}
    if not given:
        return {}
    missing = {"revision", "manifest_sha256"} - given
    if missing:
        names = ", ".join(sorted(OVERRIDE_ENV[field] for field in missing))
        raise CandidateError(
            f"candidate override is incomplete: a revision and its manifest digest "
            f"must be supplied together, missing {names}"
        )
    return {field: value for field, value in supplied.items() if value}


def resolve(
    *,
    descriptor_path: Path = DESCRIPTOR_PATH,
    workflow_path: Path = WORKFLOW_PATH,
    env: Mapping[str, str] | None = None,
) -> CandidateInput:
    """Resolve exactly one candidate identity from the declaration and overrides."""
    env = os.environ if env is None else env
    pin = released_pin(workflow_path)
    declared = load_descriptor(descriptor_path)
    overrides = read_overrides(env)
    source = "workflow-dispatch-input" if overrides else "committed-descriptor"
    if overrides:
        # An override replaces the whole declared identity: keeping the declared
        # protocol version alongside a different revision would assert something
        # the caller never claimed.
        declared = {**declared, "protocol_version": "", "tree_sha256": "", **overrides}
    return CandidateInput(
        repository=declared["repository"],
        revision=verify_revision(declared["revision"], pin=pin),
        manifest_sha256=normalize_digest(
            declared["manifest_sha256"], field="candidate manifest_sha256"
        ),
        protocol_version=declared["protocol_version"],
        tree_sha256=(
            normalize_digest(declared["tree_sha256"], field="candidate tree_sha256")
            if declared["tree_sha256"]
            else ""
        ),
        source=source,
    )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root: Path) -> tuple[str, int]:
    """Digest the whole suite tree from sorted POSIX-relative paths.

    Enumeration, digesting and counting are separate steps: a truncated walk
    would otherwise produce a short but internally consistent answer, which is
    exactly what this digest exists to make impossible.
    """
    paths = sorted(
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.is_file() and not item.is_symlink()
    )
    if not paths:
        raise CandidateError(f"candidate root enumerated zero files: {root}")
    rows = [f"{sha256_of(root / relative)}  {relative}\n" for relative in paths]
    if len(rows) != len(paths):
        raise CandidateError("digest count does not match the enumerated file count")
    digest = hashlib.sha256("".join(rows).encode("utf-8")).hexdigest()
    return digest, len(paths)


def checkout_head(checkout: Path) -> str:
    """Read the revision the candidate was actually checked out at."""
    if not (checkout / ".git").exists():
        raise CandidateError(f"candidate checkout is not a git repository: {checkout}")
    completed = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise CandidateError(
            f"cannot read the candidate checkout revision: "
            f"{completed.stderr.strip() or completed.returncode}"
        )
    return completed.stdout.strip()


def authenticate(
    candidate: CandidateInput, *, checkout: Path, root: Path
) -> dict[str, str]:
    """Measure the materialised candidate and reject any drift from the declaration."""
    if not root.is_dir():
        raise CandidateError(f"candidate root is not a directory: {root}")
    manifest = root / "manifest.json"
    if not manifest.is_file():
        raise CandidateError(f"candidate root has no manifest.json: {root}")

    head = checkout_head(checkout)
    if head != candidate.revision:
        raise CandidateError(
            f"candidate checkout revision mismatch: declared {candidate.revision}, "
            f"checked out {head}"
        )

    measured_manifest = sha256_of(manifest)
    if measured_manifest != candidate.manifest_sha256:
        raise CandidateError(
            f"candidate manifest digest mismatch: declared "
            f"{candidate.manifest_sha256}, measured {measured_manifest} -- this is a "
            f"different candidate, never re-baseline it silently"
        )

    measured_tree, files = tree_digest(root)
    if candidate.tree_sha256 and measured_tree != candidate.tree_sha256:
        raise CandidateError(
            f"candidate tree digest mismatch: declared {candidate.tree_sha256}, "
            f"measured {measured_tree}"
        )

    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CandidateError(f"candidate manifest is not valid JSON: {exc}") from exc
    measured_protocol = payload.get("protocol_version")
    if not isinstance(measured_protocol, str) or not measured_protocol:
        raise CandidateError("candidate manifest declares no protocol_version")
    if candidate.protocol_version and measured_protocol != candidate.protocol_version:
        raise CandidateError(
            f"candidate protocol version mismatch: declared "
            f"{candidate.protocol_version}, measured {measured_protocol}"
        )

    return {
        "revision": head,
        "manifest_sha256": measured_manifest,
        "tree_sha256": measured_tree,
        "protocol_version": measured_protocol,
        "file_count": str(files),
    }


def evidence_text(
    candidate: CandidateInput,
    measured: Mapping[str, str],
    *,
    root: Path,
    pin: str,
) -> str:
    rows = (
        ("candidate_source", candidate.source),
        ("candidate_repository", candidate.repository),
        ("candidate_revision", measured["revision"]),
        ("candidate_root", str(root)),
        ("protocol_version", measured["protocol_version"]),
        ("manifest_sha256", f"sha256:{measured['manifest_sha256']}"),
        ("tree_sha256", f"sha256:{measured['tree_sha256']}"),
        ("file_count", measured["file_count"]),
        ("released_suite_pin", pin),
        ("evidence_class", "candidate-only"),
        ("release_claim", "none"),
        ("conformance_claim", "none"),
    )
    lines = [*EVIDENCE_HEADER, *(f"{key:<24}{value}" for key, value in rows)]
    return "\n".join(lines) + "\n"


def _write_outputs(values: Mapping[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as stream:
            stream.writelines(f"{key}={value}\n" for key, value in values.items())


def _resolve_command(args: argparse.Namespace) -> None:
    candidate = resolve(descriptor_path=Path(args.descriptor))
    values = {
        "repository": candidate.repository,
        "revision": candidate.revision,
        "manifest_sha256": candidate.manifest_sha256,
        "protocol_version": candidate.protocol_version,
        "source": candidate.source,
    }
    _write_outputs(values)
    print(json.dumps(values, sort_keys=True))


def _record_command(args: argparse.Namespace) -> None:
    candidate = resolve(descriptor_path=Path(args.descriptor))
    root = Path(args.root)
    measured = authenticate(candidate, checkout=Path(args.checkout), root=root)
    text = evidence_text(candidate, measured, root=root, pin=released_pin())
    evidence = Path(args.evidence)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"candidate-suite: identity recorded at {evidence}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    resolve_command = commands.add_parser(
        "resolve", help="resolve and validate the candidate identity"
    )
    resolve_command.add_argument("--descriptor", default=str(DESCRIPTOR_PATH))
    resolve_command.set_defaults(handler=_resolve_command)

    record_command = commands.add_parser(
        "record", help="authenticate the materialised candidate and record evidence"
    )
    record_command.add_argument("--descriptor", default=str(DESCRIPTOR_PATH))
    record_command.add_argument("--checkout", required=True)
    record_command.add_argument("--root", required=True)
    record_command.add_argument("--evidence", required=True)
    record_command.set_defaults(handler=_record_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.handler(args)
    except CandidateError as exc:
        print(f"candidate-suite: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
