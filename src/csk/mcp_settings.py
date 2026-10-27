"""Explicit local-target management and byte-preserving legacy migration."""

from __future__ import annotations

import argparse
import json
import stat
import sys
import tempfile
import uuid
from typing import Any
from pathlib import Path
from dataclasses import replace

from . import config, manifest, locking, protocol_json, transactions, skillfile_deprecations
from . import mcp_command_resolution, mcp_project
from . import mcp_declarations as d, mcp_local_targets as local, mcp_ownership as o
from .sources import boundaries, publish


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="csk mcp")
    sub = result.add_subparsers(dest="command", required=True)
    target = sub.add_parser(
        "local-target",
        help="manage separately approved operator-local Cursor destinations",
    )
    target.add_argument("action", choices=("add", "remove", "list"))
    target.add_argument("target", nargs="?", default="cursor")
    target.add_argument("--server")
    target.add_argument("--project", type=Path, default=Path.cwd())
    migration = sub.add_parser(
        "migrate", help="remove legacy Cursor selectors and record local targets"
    )
    migration.add_argument("--server")
    migration.add_argument("--targets")
    migration.add_argument("--project", type=Path, default=Path.cwd())
    return result


def main(argv: list[str]) -> int:
    try:
        args = parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    try:
        with skillfile_deprecations.command_warnings():
            run(args)
        return 0
    except (
        ValueError,
        OSError,
        manifest.ManifestError,
        config.ConfigError,
        transactions.TransactionError,
        locking.LockError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _approve(subjects: list[o.ConsentSubject]) -> None:
    for subject in subjects:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError(
                "mcp_plan_unapproved: local: cursor needs its own terminal approval; run csk mcp local-target add cursor"
            )
        try:
            answer = input(
                f"Approve local: cursor for {subject.name} ({subject.scope})? [y/N] "
            )
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            raise ValueError("mcp_plan_unapproved: local target not approved")


def run(args: argparse.Namespace) -> None:
    # Read the config path only: these commands do not need credential-bearing configuration.
    home = config.config_path().parent.resolve()
    root = args.project.expanduser().resolve()
    local.validate_location(root, home)
    if args.command == "local-target" and args.target != "cursor":
        raise ValueError(
            "mcp_local_target_invalid: only cursor is a local writable target"
        )
    manifest_path = root / "Skillfile.json"
    if manifest_path.is_symlink() or not stat.S_ISREG(manifest_path.lstat().st_mode):
        raise skillfile_deprecations.refusal(
            "Skillfile must be a regular file without links"
        )
    before = manifest_path.read_bytes()
    loaded = manifest.parse_manifest(protocol_json.loads(before), manifest_path)
    parsed = d.parse_mcp_servers(
        loaded.mcp_servers,
        invocation_scope="project",
        target_agents=loaded.agents,
        allow_deprecated=True,
    )
    current = local.read(root, home)
    state = {target: dict(grants) for target, grants in current.items()}
    local_digest = transactions.digest_target(local.path(root, home))
    if args.command == "local-target" and args.action == "list":
        for target in sorted(state):
            if args.server is None or target in ("*", args.server):
                print(
                    f'local: cursor server={target} approved={",".join(sorted(state[target])) or "none"}'
                )
        return
    parsed = d.resolve_launchers(parsed)
    desired = before
    subjects = []
    if args.command == "migrate":
        targets = tuple(args.targets.split(",")) if args.targets is not None else None
        desired, names = skillfile_deprecations.migrate_cursor(
            before, server=args.server, targets=targets
        )
        after = manifest.parse_manifest(protocol_json.loads(desired), manifest_path)
        parsed = d.parse_mcp_servers(
            after.mcp_servers,
            invocation_scope="project",
            target_agents=after.agents,
            allow_deprecated=True,
        )
        parsed = d.resolve_launchers(parsed)
        for name in names:
            state.setdefault(name, {})
            subjects.append(local.subject(parsed[name], name))
    else:
        target = args.server or "*"
        if args.server is not None and args.server not in parsed:
            raise ValueError(
                "mcp_local_target_invalid: server is not declared in this project"
            )
        if args.action == "remove":
            state.pop(target, None)  # Revokes only this target's grants.
        else:
            state.setdefault(target, {})
            for decl in parsed.values():
                if decl.environments is not None and (
                    args.server is None or args.server == decl.name
                ):
                    subject = local.subject(decl, target)
                    if state[target].get(decl.name) != o.consent_key(
                        local.consumer(root), subject
                    ):
                        subjects.append(subject)
    parsed = d.resolve_launchers(parsed)
    mcp_command_resolution.validate_windows_batch_commands(
        parsed, windows=sys.platform == "win32"
    )
    mcp_command_resolution.validate_host_commands(parsed, cwd=root)
    # A project-wide preference with no current explicit server approves only
    # the local setting; each future server still needs an independent grant.
    if (
        args.command == "local-target"
        and args.action == "add"
        and not subjects
        and (args.server or "*") not in current
    ):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError(
                "mcp_plan_unapproved: new local target needs terminal approval"
            )
        try:
            answer = input(
                "Enable project-local Cursor targeting for future explicitly selected servers? [y/N] "
            )
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            raise ValueError("mcp_plan_unapproved: local setting not approved")
    # Rebind approval subjects after launcher identity resolution.
    subjects = [
        local.subject(
            parsed[subject.name], subject.scope.removeprefix("project-local:")
        )
        for subject in subjects
    ]
    # No lock, journal, config or native file is changed until every approval is available.
    mcp_project.print_plan(
        {
            subject.name: replace(
                parsed[subject.name], agents=("cursor",), local_agents=("cursor",)
            )
            for subject in subjects
        }
    )
    _approve(subjects)
    for subject in subjects:
        target = subject.scope.removeprefix("project-local:")
        state[target][subject.name] = o.consent_key(local.consumer(root), subject)
    data = local.encode(root, state)
    with locking.ProjectLock(home, root), locking.ManagerHomeLock(home) as lock:
        engine = transactions.TransactionEngine(
            home, pre_write_hook=publish.make_publication_hook()
        )
        # This command planned from current preimages. Never forward-publish a
        # prior transaction and then refuse on those now-stale preimages.
        engine.require_recovered(lock)
        engine.recover(lock)
        if (
            manifest_path.read_bytes() != before
            or transactions.digest_target(local.path(root, home)) != local_digest
        ):
            raise ValueError(
                "mcp_local_target_invalid: project or local configuration changed; retry"
            )
        if desired == before and state == current:
            print("MCP local configuration unchanged")
            return
        local.ensure_directory(home)
        record = boundaries.freeze_boundaries(root, home)
        record_json = boundaries.boundary_record_to_json(record)
        transaction_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="mcp-settings-", dir=home) as temp:
            staging = Path(temp)
            specs = []
            for identifier, live, content, expected in (
                ("local-targets", local.path(root, home), data, local_digest),
                (
                    "skillfile",
                    manifest_path,
                    desired,
                    transactions.digest_target(manifest_path),
                ),
            ):
                if identifier == "skillfile" and desired == before:
                    continue
                staged = staging / identifier
                staged.write_bytes(content)
                staged.chmod(
                    stat.S_IMODE(manifest_path.stat().st_mode)
                    if identifier == "skillfile"
                    else 0o600
                )
                if identifier == "skillfile":
                    payload: dict[str, Any] = {
                        "kind": publish._RECHECK_MCP_NATIVE,
                        "record": record_json,
                        "root": str(root),
                        "leaf": "Skillfile.json",
                        "components": [],
                        "ancestors": [],
                        "expected_live": expected,
                    }
                else:
                    components = list(live.relative_to(home).parts[:-1])
                    ancestors = publish._freeze_home_ancestors(
                        str(home), components, subject="MCP local target"
                    )
                    payload = {
                        "kind": publish._RECHECK_HOME_ENTRY,
                        "record": record_json,
                        "home": str(home),
                        "components": components,
                        "leaf": live.name,
                        "ancestors": publish._identity_pairs_to_json(ancestors),
                        "expected_live": expected,
                    }
                specs.append(
                    transactions.MutableTarget(
                        "mcp-settings",
                        identifier,
                        live,
                        staged,
                        expected_preimage_digest=expected,
                        publication_recheck=payload,
                    )
                )
            engine.prepare(
                lock,
                transactions.TransactionPlan(
                    transaction_id,
                    locking.canonical_project_identity(root),
                    tuple(specs),
                ),
            )
            engine.commit(lock, transaction_id)
    print("MCP settings saved; run csk install to apply the reviewed destinations")
