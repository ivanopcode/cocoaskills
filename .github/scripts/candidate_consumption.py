"""Presence and consumption ledgers for the candidate protocol suite.

``candidate_suite.py`` decides *which* candidate a run measures. This module
decides whether that candidate was actually **read**. Publishing a family in a
conformance root was never evidence that anything consumed it: a lane can
return exit 0 against a schema-8 suite while every schema-8 case sits unread,
which is the false green the schema-8 impact analysis measured.

Two ledgers close it, and they answer different questions.

``.github/ci/candidate-artifacts.tsv`` -- **presence**. Each consumer declares
the root-relative artifacts its unguarded reads require. The partition it
produces is derived from the root, never from the lane: against a root that
publishes the whole surface a consumer is *served* and runs, and against a root
that publishes none of it the consumer is *deferred* and takes the skip path it
already implements. ``require`` asserts the deferred set is empty, and the
candidate lane runs it, so a candidate can never be qualified while a family it
publishes went unread.

``.github/ci/candidate-cases.tsv`` -- **consumption**. Each row names a test
that must be observed passing on the runners it lists. A rename, a deletion, a
selection that matches nothing, or a module-level skip fails by name here
instead of quietly shrinking the run.

Usage::

    candidate_consumption.py require --root <dir>
    candidate_consumption.py gate --results <junit.xml> --platform linux
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

CI_CONFIG = Path(__file__).parents[1] / "ci"
ARTIFACT_LEDGER_PATH = CI_CONFIG / "candidate-artifacts.tsv"
CASE_LEDGER_PATH = CI_CONFIG / "candidate-cases.tsv"
PLATFORMS = ("linux", "darwin", "windows")


class LedgerError(RuntimeError):
    """A ledger, a candidate root, or a result stream did not satisfy a gate."""


@dataclass(frozen=True)
class ArtifactRow:
    """One consumer and the root-relative artifacts its reads require."""

    consumer: str
    artifacts: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class CaseRow:
    """One test that must be observed passing on the runners it names."""

    nodeid: str
    must_run_on: tuple[str, ...]
    note: str


def _rows(path: Path, columns: int) -> list[tuple[str, ...]]:
    if not path.is_file():
        raise LedgerError(f"ledger does not exist: {path}")
    rows: list[tuple[str, ...]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != columns:
            raise LedgerError(
                f"{path}:{number} must carry exactly {columns} tab separated columns, "
                f"found {len(fields)}"
            )
        stripped = tuple(field.strip() for field in fields)
        if not all(stripped):
            raise LedgerError(f"{path}:{number} carries an empty column")
        rows.append(stripped)
    if not rows:
        raise LedgerError(f"ledger declares no rows: {path}")
    return rows


def load_artifact_ledger(path: Path = ARTIFACT_LEDGER_PATH) -> tuple[ArtifactRow, ...]:
    """Load the presence ledger, rejecting duplicate or empty declarations."""
    rows: list[ArtifactRow] = []
    seen: set[str] = set()
    for consumer, artifacts, note in _rows(path, 3):
        if consumer in seen:
            raise LedgerError(f"{path} declares {consumer} twice")
        seen.add(consumer)
        names = tuple(name.strip() for name in artifacts.split(",") if name.strip())
        if not names:
            raise LedgerError(f"{path} declares no artifact for {consumer}")
        if len(names) != len(set(names)):
            raise LedgerError(f"{path} repeats an artifact for {consumer}")
        rows.append(ArtifactRow(consumer=consumer, artifacts=names, note=note))
    return tuple(rows)


def load_case_ledger(path: Path = CASE_LEDGER_PATH) -> tuple[CaseRow, ...]:
    """Load the consumption ledger, rejecting unknown platforms and duplicates."""
    rows: list[CaseRow] = []
    seen: set[str] = set()
    for nodeid, must_run_on, note in _rows(path, 3):
        if nodeid in seen:
            raise LedgerError(f"{path} declares {nodeid} twice")
        seen.add(nodeid)
        if "::" not in nodeid:
            raise LedgerError(f"{path} row {nodeid!r} is not a pytest node id")
        if "[" in nodeid:
            raise LedgerError(
                f"{path} row {nodeid!r} names one parameterization; a row names a test "
                f"function and covers every parameterization it produces"
            )
        platforms = tuple(name.strip() for name in must_run_on.split(",") if name.strip())
        unknown = sorted(set(platforms) - set(PLATFORMS))
        if unknown:
            raise LedgerError(f"{path} row {nodeid} names unknown platforms: {', '.join(unknown)}")
        if not platforms:
            raise LedgerError(f"{path} row {nodeid} requires no platform, so it proves nothing")
        rows.append(CaseRow(nodeid=nodeid, must_run_on=platforms, note=note))
    return tuple(rows)


def manifest_inventory(root: Path) -> Mapping[str, str]:
    """Read the candidate manifest's published path set.

    Presence is decided against the manifest rather than the filesystem: a file
    that the suite does not publish is not part of the candidate, and a
    published entry whose file is absent is a broken root either way.
    """
    manifest = root / "manifest.json"
    if not manifest.is_file():
        raise LedgerError(f"candidate root has no manifest.json: {root}")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LedgerError(f"candidate manifest is not valid JSON: {exc}") from exc
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        raise LedgerError("candidate manifest publishes no files")
    inventory: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise LedgerError("candidate manifest entry is not an object")
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not relative:
            raise LedgerError("candidate manifest entry publishes no path")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise LedgerError(f"candidate manifest entry {relative} publishes no sha256")
        if relative in inventory:
            raise LedgerError(f"candidate manifest publishes {relative} twice")
        inventory[relative] = digest
    return inventory


def missing_artifacts(
    artifacts: Iterable[str], inventory: Mapping[str, str]
) -> tuple[str, ...]:
    """Name every declared artifact the root does not publish.

    An artifact is either one published path or a family directory, which is
    present when the manifest publishes at least one file below it. Both forms
    are checked against the same published inventory, so removing a family and
    removing a single vector fail the same way.
    """
    missing: list[str] = []
    for artifact in artifacts:
        if artifact in inventory:
            continue
        prefix = artifact.rstrip("/") + "/"
        if any(path.startswith(prefix) for path in inventory):
            continue
        missing.append(artifact)
    return tuple(missing)


def deferred_consumers(
    root: Path, ledger: Sequence[ArtifactRow] | None = None
) -> Mapping[str, tuple[str, ...]]:
    """Partition the ledger against one root, returning consumer -> missing."""
    rows = load_artifact_ledger() if ledger is None else ledger
    inventory = manifest_inventory(root)
    deferred: dict[str, tuple[str, ...]] = {}
    for row in rows:
        missing = missing_artifacts(row.artifacts, inventory)
        if missing:
            deferred[row.consumer] = missing
    return deferred


@dataclass(frozen=True)
class Observation:
    """One observed test result, reduced to the outcome a gate cares about."""

    nodeid: str
    outcome: str


def read_junit(path: Path) -> tuple[Observation, ...]:
    """Reduce a pytest JUnit XML stream to node id and outcome."""
    if not path.is_file():
        raise LedgerError(f"result stream does not exist: {path}")
    try:
        tree = ElementTree.parse(path)
    except ElementTree.ParseError as exc:
        raise LedgerError(f"result stream is not valid XML: {exc}") from exc
    observations: list[Observation] = []
    for case in tree.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        if not name:
            raise LedgerError("result stream carries a testcase without a name")
        module = classname.split(".")
        if module and module[-1][:1].isupper():
            path_parts, klass = module[:-1], module[-1]
        else:
            path_parts, klass = module, ""
        nodeid = "/".join(path_parts)
        if nodeid:
            nodeid += ".py"
        nodeid = f"{nodeid}::{klass}::{name}" if klass else f"{nodeid}::{name}"
        outcome = "passed"
        for child in case:
            if child.tag in {"failure", "error"}:
                outcome = "failed"
                break
            if child.tag == "skipped":
                outcome = "skipped"
        observations.append(Observation(nodeid=nodeid, outcome=outcome))
    if not observations:
        raise LedgerError(f"result stream carries no testcase: {path}")
    return tuple(observations)


def gate_cases(
    observations: Sequence[Observation],
    platform: str,
    ledger: Sequence[CaseRow] | None = None,
) -> tuple[str, ...]:
    """Return one failure line per required case that was not observed passing."""
    if platform not in PLATFORMS:
        raise LedgerError(f"unknown platform {platform!r}, expected one of {', '.join(PLATFORMS)}")
    rows = load_case_ledger() if ledger is None else ledger
    failures: list[str] = []
    for row in rows:
        if platform not in row.must_run_on:
            continue
        matched = [
            observation
            for observation in observations
            if observation.nodeid == row.nodeid
            or observation.nodeid.startswith(f"{row.nodeid}[")
        ]
        if not matched:
            failures.append(
                f"{row.nodeid} is required on {platform} but the run observed it "
                f"zero times -- a rename, a deletion or a module-level skip removes "
                f"the evidence this row exists to require"
            )
            continue
        bad = sorted(
            {observation.outcome for observation in matched} - {"passed"}
        )
        if bad:
            failures.append(
                f"{row.nodeid} is required on {platform} and was observed "
                f"{', '.join(bad)} in {len(matched)} case(s)"
            )
    return tuple(failures)


def _require_command(args: argparse.Namespace) -> None:
    root = Path(args.root)
    if not root.is_dir():
        raise LedgerError(f"candidate root is not a directory: {root}")
    deferred = deferred_consumers(root, load_artifact_ledger(Path(args.ledger)))
    if deferred:
        lines = "\n".join(
            f"  {consumer}: missing {', '.join(missing)}"
            for consumer, missing in sorted(deferred.items())
        )
        raise LedgerError(
            f"the candidate root does not serve every declared consumer, so a lane "
            f"against it would report success while a family went unread:\n{lines}"
        )
    rows = load_artifact_ledger(Path(args.ledger))
    served = ", ".join(row.consumer for row in rows)
    print(f"candidate-consumption: every declared artifact is published for {served}")


def _gate_command(args: argparse.Namespace) -> None:
    observations = read_junit(Path(args.results))
    failures = gate_cases(observations, args.platform, load_case_ledger(Path(args.ledger)))
    if failures:
        raise LedgerError(
            "the declared consumption cases were not all observed passing:\n"
            + "\n".join(f"  {line}" for line in failures)
        )
    required = [row for row in load_case_ledger(Path(args.ledger)) if args.platform in row.must_run_on]
    print(
        f"candidate-consumption: {len(required)} declared case(s) observed passing "
        f"on {args.platform} across {len(observations)} results"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    require = commands.add_parser(
        "require", help="assert the candidate root serves every declared consumer"
    )
    require.add_argument("--root", required=True)
    require.add_argument("--ledger", default=str(ARTIFACT_LEDGER_PATH))
    require.set_defaults(handler=_require_command)

    gate = commands.add_parser(
        "gate", help="assert every declared consumption case was observed passing"
    )
    gate.add_argument("--results", required=True)
    gate.add_argument("--platform", required=True)
    gate.add_argument("--ledger", default=str(CASE_LEDGER_PATH))
    gate.set_defaults(handler=_gate_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.handler(args)
    except LedgerError as exc:
        print(f"candidate-consumption: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
