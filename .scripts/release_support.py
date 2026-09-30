#!/usr/bin/env python3
"""Small, dependency-free helpers for the stable release shell script."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID = re.compile(r"^[0-9]+$")


def cut_changelog_text(text: str, version: str, release_date: str) -> str:
    """Move the current Unreleased section under a dated version heading."""
    if _VERSION.fullmatch(version) is None:
        raise ValueError("version must be stable MAJOR.MINOR.PATCH")
    dt.date.fromisoformat(release_date)

    version_heading = re.compile(rf"(?m)^## \[{re.escape(version)}\](?:\s|$)")
    if version_heading.search(text):
        raise ValueError(f"CHANGELOG.md already contains version {version}")

    unreleased = list(re.finditer(r"(?m)^## \[Unreleased\][ \t]*\r?$", text))
    if len(unreleased) != 1:
        raise ValueError("CHANGELOG.md must contain exactly one Unreleased heading")

    match = unreleased[0]
    newline = "\r\n" if "\r\n" in text else "\n"
    replacement = (
        f"## [Unreleased]{newline}{newline}## [{version}] - {release_date}"
    )
    return text[: match.start()] + replacement + text[match.end() :]


def cut_changelog_file(file_path: Path, version: str, release_date: str) -> None:
    source = file_path.read_bytes().decode("utf-8")
    updated = cut_changelog_text(source, version, release_date)
    file_path.write_bytes(updated.encode("utf-8"))


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON object field")
        document[key] = value
    return document


def is_green_trunk_evidence(document_text: str, expected_head: str) -> bool:
    """Accept only the delivery v1 green document for the exact release head."""
    if _COMMIT_SHA.fullmatch(expected_head) is None:
        return False
    try:
        document = json.loads(document_text, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError):
        return False
    if not isinstance(document, dict):
        return False
    return (
        document.get("schema") == "delivery.trunk-verify/v1"
        and document.get("command") == "trunk-verify"
        and document.get("head") == expected_head
        and document.get("verdict") == "green"
        and type(document.get("exit_code")) is int
        and document.get("exit_code") == 0
    )


def _parse_provider_time(value: object, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"workflow run has no valid {field}")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"workflow run has an invalid {field}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"workflow run {field} has no timezone")
    return parsed


def select_workflow_run(
    document_text: str,
    expected_event: str,
    expected_head: str,
    after_created_at: str | None = None,
) -> tuple[str, str] | None:
    """Select one exact workflow run; return its ID and startedAt, if present."""
    if _COMMIT_SHA.fullmatch(expected_head) is None:
        raise ValueError("expected workflow head must be a full 40-character SHA")
    if not expected_event:
        raise ValueError("expected workflow event is empty")
    threshold = (
        _parse_provider_time(after_created_at, "release start time")
        if after_created_at is not None
        else None
    )
    try:
        rows = json.loads(document_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("workflow run list is not valid JSON") from exc
    if not isinstance(rows, list):
        raise ValueError("workflow run list must be a JSON array")

    matches: list[tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("workflow run list contains a non-object row")
        if row.get("event") != expected_event or row.get("headSha") != expected_head:
            continue
        run_id = row.get("databaseId")
        if isinstance(run_id, int):
            run_id = str(run_id)
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise ValueError("matching workflow run has an invalid databaseId")
        created_at = _parse_provider_time(row.get("createdAt"), "createdAt")
        if threshold is not None and created_at <= threshold:
            continue
        started_at = ""
        if after_created_at is None:
            started_at = row.get("startedAt", "")
            if not started_at:
                continue
            _parse_provider_time(started_at, "startedAt")
        matches.append((run_id, started_at))

    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("multiple workflow runs match the exact release identity")
    return matches[0]


def failed_job_rows(document_text: str, run_id: str) -> list[tuple[str, str]]:
    """Return every failed job and its same-run GitHub job URL."""
    if _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("workflow run ID is malformed")
    try:
        document = json.loads(document_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("workflow jobs response is not valid JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("jobs"), list):
        raise ValueError("workflow jobs response is malformed")
    result: list[tuple[str, str]] = []
    expected_prefix = (
        f"https://github.com/ivanopcode/cocoaskills/actions/runs/{run_id}/job/"
    )
    for job in document["jobs"]:
        if not isinstance(job, dict):
            raise ValueError("workflow jobs response contains a non-object row")
        if job.get("conclusion") != "failure":
            continue
        name = job.get("name")
        url = job.get("url")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("failed workflow job has no name")
        if not isinstance(url, str) or not url.startswith(expected_prefix):
            raise ValueError("failed workflow job URL is missing or mismatched")
        parsed_url = urlsplit(url)
        job_suffix = url[len(expected_prefix) :]
        if (
            parsed_url.scheme != "https"
            or parsed_url.netloc != "github.com"
            or not _RUN_ID.fullmatch(job_suffix)
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("failed workflow job URL is malformed")
        result.append((name.strip(), url))
    if not result:
        raise ValueError("failed workflow run contains no failed jobs")
    return result


def homebrew_formula_has_version(formula: str, version: str) -> bool:
    if _VERSION.fullmatch(version) is None:
        return False
    urls = re.findall(r'(?m)^\s*url\s+"([^"]+)"\s*$', formula)
    expected = f"/download/v{version}/cocoaskills-{version}.tar.gz"
    return len(urls) == 1 and urls[0].endswith(expected)


def mise_output_has_version(output: str, version: str) -> bool:
    if _VERSION.fullmatch(version) is None:
        return False
    tokens = re.split(r"[\s,]+", output.strip())
    return any(token.lstrip("v").split("@")[-1] == version for token in tokens)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release_support.py")
    subparsers = parser.add_subparsers(dest="command", required=True)

    cut = subparsers.add_parser("cut-changelog")
    cut.add_argument("file", type=Path)
    cut.add_argument("version")
    cut.add_argument("date")

    trunk_evidence = subparsers.add_parser("trunk-evidence")
    trunk_evidence.add_argument("head")

    match_run = subparsers.add_parser("match-run")
    match_run.add_argument("event")
    match_run.add_argument("head")
    match_run.add_argument("--after-created-at")

    failed_jobs = subparsers.add_parser("failed-jobs")
    failed_jobs.add_argument("run_id")

    homebrew = subparsers.add_parser("check-homebrew")
    homebrew.add_argument("version")

    mise = subparsers.add_parser("check-mise")
    mise.add_argument("version")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "cut-changelog":
            cut_changelog_file(args.file, args.version, args.date)
            return 0
        source = sys.stdin.read()
        if args.command == "trunk-evidence":
            return 0 if is_green_trunk_evidence(source, args.head) else 1
        if args.command == "match-run":
            selected = select_workflow_run(
                source, args.event, args.head, args.after_created_at
            )
            if selected is None:
                return 1
            print("\t".join(selected))
            return 0
        if args.command == "failed-jobs":
            for name, url in failed_job_rows(source, args.run_id):
                print(f"{name}\t{url}")
            return 0
        if args.command == "check-homebrew":
            if homebrew_formula_has_version(source, args.version):
                return 0
            print(
                f"Homebrew formula does not publish cocoaskills {args.version}",
                file=sys.stderr,
            )
            return 1
        if args.command == "check-mise":
            if mise_output_has_version(source, args.version):
                return 0
            print(
                f"mise does not list cocoaskills {args.version}", file=sys.stderr
            )
            return 1
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"release_support: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
