"""Private, bounded same-attempt evidence for publication tests, never product policy.

The fixture owns the roots and is the sole writer. Ancestors and reparse/link
entries are checked without traversal; this is not a hostile concurrent-writer
filesystem guarantee. Raw journal/CLI files are private pytest artifacts; only
numbered summary JSON is suitable for portable reporting. Raw file persistence
requires POSIX modes; Windows ACL admission is not implemented. Other platforms
retain in-memory bounded observations as incomplete and create no output files.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from csk import transactions as tx

MAX_BYTES = 1024 * 1024
MAX_ENTRIES = 256
MAX_EVENTS = 4096  # Per CLI attempt; completed attempts persist as separate manifests.
_read_regular = tx._read_bounded_regular
CLASSES = frozenset({
    "03-source-tree", "04-source-record", "05-bindings", "07-lock", "10-context",
    "20-runtime", "30-shim-canonical", "60-adapter-ledger", "80-removal",
    "mcp-native", "mcp-consent", "mcp-ledger", "mcp-gitignore",
})
EVENTS = frozenset({
    "prepared", "before_staging_entry_create", "staging_entry_created",
    "staging_entry_completed", "before_staging_mode_finalize", "staging_mode_finalized",
    "staging_mode_finalize_completed", "after_staging_chunk_sync", "during_staging_copy",
    "before_cleanup", "after_backup", "target_committed", "after_install", "after_restore",
    "cleanup_journaled", "sidecar_tombed", "before_sidecar_mode_writable",
    "sidecar_mode_writable", "before_journal_tomb", "journal_tombed", "before_journal_unlink",
})
TYPES = frozenset({
    "OSError", "PermissionError", "FileNotFoundError", "NotADirectoryError",
    "IsADirectoryError", "RuntimeError", "ValueError", "UnicodeError",
    "TransactionError", "TransactionCorruptionError", "TransactionIncompleteError",
})


def exception_summary(error):
    """Bound both edges, including suppressed context; never stringify errors."""
    nodes, seen = [], {}

    def visit(current):
        if current is None:
            return None
        if id(current) in seen:
            return seen[id(current)]
        if len(nodes) == 8:
            return "truncated"
        index = len(nodes)
        seen[id(current)] = index
        name = type(current).__name__
        number = getattr(current, "errno", None)
        row = {"type": name if name in TYPES else "other",
               "errno": number if type(number) is int else None}
        nodes.append(row)
        row["cause"] = visit(current.__cause__)
        row["context"] = visit(current.__context__)
        row["suppress_context"] = bool(current.__suppress_context__)
        return index

    visit(error)
    return nodes


def _link(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


class PublicationCapture:
    def __init__(self, directory: Path, *, roots: tuple[Path, ...], output_anchor: Path):
        # The caller supplies an existing, trusted fixture anchor. Do not resolve
        # it or the output path through links; its ancestors are trusted inputs.
        self.directory = directory
        self.output_anchor = output_anchor
        self.roots = tuple(root.resolve() for root in roots)
        self.rows = []
        self.errors = []
        self.complete = False
        self._engines = 0
        self._cli = 0
        self._snapshots = 0
        self._manifest = 0
        self._sealed = False

    def _write(self, name, data):
        # Validate all existing components before creating even one directory.
        # These owned fixtures have one writer; this is not a race-proof walk.
        anchor = self.output_anchor
        if not anchor.is_absolute() or not self.directory.is_absolute():
            raise ValueError("capture output needs an absolute fixture anchor")
        parts = self.directory.relative_to(anchor).parts
        if ".." in parts:
            raise ValueError("capture output outside fixture anchor")
        current = anchor
        missing = []
        for part in (None, *parts):
            if part is not None:
                current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if part is None:
                    raise  # Never create the trusted fixture anchor itself.
                missing.append(current)
            else:
                if _link(info) or not stat.S_ISDIR(info.st_mode):
                    raise ValueError("non-directory capture output ancestor")
        # Do not confuse Windows chmod's read-only bit with private ACLs. Until
        # an ACL contract exists, even a plain owned Windows sink is unavailable.
        if os.name != "posix":
            raise ValueError("private capture output requires POSIX permissions")
        for path in missing:
            path.mkdir(mode=0o700)
        path = self.directory / name
        # Exclusive files: no reuse/overwrite of a pre-existing link or artifact.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)

    def _summary(self):
        # A fresh numbered manifest avoids a stale overwritten 'complete' flag.
        payload = {"complete": self.complete, "errors": self.errors, "rows": self.rows}
        self._manifest += 1
        self._write(f"summary-{self._manifest:05d}.json",
                    json.dumps(payload, sort_keys=True).encode())

    def _safe(self, action):
        try:
            action()
        except Exception as error:
            # This catch surrounds recorder work only, never a delegate/hook.
            self.complete = False
            if len(self.errors) < 8:
                self.errors.append(exception_summary(error))
            try:
                self._summary()
            except Exception:
                # Memory still says incomplete when even the failure sink fails.
                pass

    def _row(self, row):
        if self._sealed:
            self.rows = []
            self._sealed = False
        if len(self.rows) >= MAX_EVENTS:
            self.complete = False
            raise ValueError("capture event limit")
        self.rows.append(row)

    def _owned(self, path):
        # Resolve only explicit trusted anchors, never a candidate/link referent.
        for root in self.roots:
            try:
                parts = path.relative_to(root).parts
            except ValueError:
                continue
            if ".." in parts:
                continue
            info = root.lstat()
            if _link(info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("non-directory capture root")
            current = root
            for part in parts[:-1]:
                current = current / part
                info = current.lstat()
                if _link(info) or not stat.S_ISDIR(info.st_mode):
                    raise ValueError("non-directory capture ancestor")
            return
        raise ValueError("capture outside fixture roots")

    def _entry(self, path, entries):
        self._owned(path)
        if len(entries) == MAX_ENTRIES:
            raise ValueError("capture entry limit")
        try:
            info = path.lstat()
        except FileNotFoundError:
            entries.append({"kind": "absent"})
            return
        row = {"mode": stat.S_IMODE(info.st_mode)}
        entries.append(row)
        if _link(info):
            row["kind"] = "link"  # Do not read even the link destination string.
        elif stat.S_ISREG(info.st_mode):
            row["kind"] = "file"
            if info.st_size > MAX_BYTES:
                row["status"] = "truncated"
                return
            data = _read_regular(path, limit=MAX_BYTES)
            row.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        elif stat.S_ISDIR(info.st_mode):
            row["kind"] = "directory"
            # Bounded traversal, with no symlink/reparse recursion or path output.
            with os.scandir(path) as children:
                for child in children:
                    self._entry(path / child.name, entries)
        else:
            row.update(kind="other", status="unavailable")

    def _snapshot(self, engine, journal):
        self._snapshots += 1
        number = self._snapshots
        row = {"snapshot": number, "journal": "unavailable", "sidecars": [],
               "phase": "unknown", "progress": []}
        self._row(row)  # Partial evidence remains explicitly incomplete on error.
        path = engine._journal_path(journal.transaction_id)
        self._owned(path)
        try:
            data = _read_regular(path, limit=MAX_BYTES)
        except FileNotFoundError:
            row["journal"] = "absent"
        else:
            self._write(f"journal-{number:03d}.private", data)
            row.update(journal="complete", journal_sha256=hashlib.sha256(data).hexdigest())
        row["phase"] = journal.phase if journal.phase in {
            "preparing", "prepared", "committing", "cleanup", "rolling_back"} else "unknown"
        if len(journal.targets) > MAX_ENTRIES:
            raise ValueError("capture target limit")
        for target_index, target in enumerate(journal.targets):
            row["progress"].append({
                "class": target.target_class if target.target_class in CLASSES else "other",
                "state": target.state if target.state in {
                    "pending", "backed_up", "committed", "rolled_back"} else "unknown",
                "index": target.staging_index, "entries": len(target.staging_entries),
                "active": target.staging_active, "created": target.staging_created,
                "finalize_index": target.staging_finalize_index,
            })
            for role, candidate in (("staged", target.staged_path), ("backup", target.backup_path),
                                    ("rollback", target.rollback_path)):
                if candidate:
                    start = len(row["sidecars"])
                    self._entry(Path(candidate), row["sidecars"])
                    for entry in row["sidecars"][start:]:
                        entry.update(target=target_index, role=role)
        if any(entry.get("status") for entry in row["sidecars"]):
            if len(self.errors) < 8:
                self.errors.append([{"type": "bounded_snapshot_incomplete"}])

    def observe(self, engine):
        """Attach to this instance only; no product or shared-stdlib replacement."""
        self._engines += 1
        number = self._engines
        prepare, discard, hook = engine.prepare, engine._discard_prepared, engine._fault_hook

        def event(point, target):
            self._safe(lambda: self._row({
                "engine": number, "event": point if point in EVENTS else "other",
                "class": None if target is None else (
                    target.target_class if target.target_class in CLASSES else "other"),
                "index": None if target is None else target.staging_index,
            }))
            if hook is not None:
                return hook(point, target)

        def observed_discard(journal, *args, **kwargs):
            original_error = sys.exception()
            self._safe(lambda: self._row({"engine": number, "call": "discard",
                                         "exception": exception_summary(original_error)}))
            self._safe(lambda: self._snapshot(engine, journal))
            try:
                return discard(journal, *args, **kwargs)
            except Exception as error:
                self._safe(lambda: self._row({"engine": number, "call": "discard_error",
                                             "exception": exception_summary(error)}))
                raise

        def observed_prepare(*args, **kwargs):
            self.complete = False
            self._safe(lambda: self._row({"engine": number, "call": "prepare"}))
            try:
                result = prepare(*args, **kwargs)
            except Exception as error:
                self._safe(lambda: self._row({"engine": number, "call": "prepare_error",
                                             "exception": exception_summary(error)}))
                self._safe(self._summary)
                raise
            self._safe(lambda: self._snapshot(engine, result))
            self._safe(self._summary)
            return result

        engine.prepare = observed_prepare
        engine._discard_prepared = observed_discard
        engine._fault_hook = event
        return engine

    def cli(self, result, stdout, stderr):
        """Call immediately after capsys.readouterr(), before any assertions."""
        self.complete = False
        self._cli += 1
        number = self._cli

        def capture():
            self._write(f"cli-{number:03d}.stdout.private", stdout.encode("utf-8"))
            self._write(f"cli-{number:03d}.stderr.private", stderr.encode("utf-8"))
            self._row({"cli": number, "result": result, "stdout_bytes": len(stdout.encode()),
                       "stderr_bytes": len(stderr.encode())})
            self.complete = not self.errors
            self._summary()

        self._safe(capture)
        self._sealed = True
