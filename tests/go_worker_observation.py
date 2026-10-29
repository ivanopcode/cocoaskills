"""Opt-in, bounded observations for one trusted fixture's owned Python tree.

Nothing installs automatically. In particular, never install the audit hook in
pytest: it is non-removable and belongs only in an explicitly bootstrapped child.
See go_worker_observation.md for the ownership and missing-coverage contract.
"""

from __future__ import annotations

import json
import math
import os
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path


STAGES = frozenset({
    "node-start", "node-end", "failure", "launch-start", "domain-launch",
    "domain-launched", "client-deadline", "request-send", "ready-wait",
    "ready-validation", "ready", "list-send", "list-wait", "list-result",
    "manager-validation", "manager-validated", "build-permit", "build-wait",
    "build-result", "launch-failure", "teardown-start", "teardown-end",
    "before-cleanup", "identity-events", "identity-event", "cache-intent", "cache-post",
    "cache-completed", "coverage",
})
NUMBERS = frozenset({
    "worker_pid", "exit_code", "deadline", "remaining", "path_index",
    "event_flags", "event_count", "dev", "ino", "mode", "nlink", "size",
})
BOOLS = frozenset({"expired", "present", "no_site", "dont_write_bytecode"})
COVERAGE = frozenset({
    "python-audit-intent-only", "explicit-post-state", "action-returned",
    "hardened-worker-uncovered", "native-and-other-writers-uncovered",
})


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def _plain(path: Path, *, directory: bool):
    info = path.lstat()
    if _linked(info) or (stat.S_ISDIR(info.st_mode) != directory):
        raise ValueError("not a plain owned path")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular owned file")
    return info


def _plain_chain(path: Path):
    # Walk named ancestors without resolving any symlink/reparse referent.
    for parent in (*reversed(path.parents), path):
        _plain(parent, directory=True)


class Recorder:
    """No path creation, chmod, unlink, general scanning or implicit enablement.

    The caller owns anchor and sink for the whole lifetime. Its already-open fd
    remains caller-owned. Namespace races by another writer inside that private
    anchor are outside this fixture contract; static substitutions are refused.
    """

    MAX_EVENTS = 128
    MAX_BYTES = 16 * 1024

    def __init__(self, anchor: Path, sink: Path, stream, *, role="manager"):
        if role not in {"manager", "test-child"}:
            raise ValueError("unknown role")
        self.anchor, self.sink, self.stream = anchor, sink, stream
        self.descriptor = stream.fileno()
        self.role = role
        self.events = []
        self.dropped = 0
        self.sink_failures = 0
        self._lock = threading.RLock()
        self._busy = False
        self._encoding = False

    def record(self, stage, **values):
        with self._lock:
            # Same-thread Python callbacks may reenter despite the RLock. A
            # nested observation consumes no event slot and is counted once.
            if self._busy:
                self.dropped += 1
                return
            self._busy = True
            try:
                self._record(stage, **values)
            finally:
                self._busy = False

    def drop(self):
        with self._lock:
            self.dropped += 1

    def _record(self, stage, **values):
        # Inputs are closed before any JSON encoding. No arbitrary repr/str call.
        if type(stage) is not str or stage not in STAGES or set(values) - (NUMBERS | BOOLS | {"root", "relative", "operation", "coverage"}):
            self.dropped += 1
            return
        clean = {}
        for key, value in values.items():
            if key in NUMBERS:
                if value is None and key == "exit_code":
                    clean[key] = None  # cached returncode unavailable, not a poll
                    continue
                if type(value) not in {int, float} or abs(value) > 2**64 or not math.isfinite(value):
                    self.dropped += 1
                    return
            elif key in BOOLS:
                if type(value) is not bool:
                    self.dropped += 1
                    return
            elif type(value) is not str:
                self.dropped += 1
                return
            elif key == "operation" and value not in {"mkdir", "write", "rename"}:
                self.dropped += 1
                return
            elif key == "coverage" and value not in COVERAGE:
                self.dropped += 1
                return
            elif key == "root" and value != "manager":
                self.dropped += 1
                return
            elif key == "relative":
                # Only CacheAudit may supply an exact configured canonical name.
                if value not in getattr(self, "_relative_names", frozenset()):
                    self.dropped += 1
                    return
            clean[key] = value
        row = dict(stage=stage, monotonic=time.monotonic(), pid=os.getpid(),
                   ppid=os.getppid(), role=self.role, **clean)
        # Reserve bounded space for schema, counters and numeric growth at flush.
        if len(self.events) >= self.MAX_EVENTS or len(self._encode(self.events + [row])) > self.MAX_BYTES - 256:
            self.dropped += 1
            return
        self.events.append(row)

    def _encode(self, events=None):
        with self._lock:
            if self._encoding:
                self.dropped += 1
                raise ValueError("reentrant trace snapshot")
            previous_busy = self._busy
            self._busy = self._encoding = True
            try:
                # Snapshot only sanitized rows; callbacks cannot mutate this
                # container through a nested recorder operation.
                rows = list(self.events if events is None else events)
                return json.dumps(dict(schema=1, events=rows,
                                       dropped=min(self.dropped, 2**63 - 1),
                                       sink_failures=min(self.sink_failures, 2**63 - 1)),
                                  separators=(",", ":"), allow_nan=False).encode("ascii")
            finally:
                self._encoding = False
                self._busy = previous_busy

    def _check_sink(self):
        if not self.anchor.is_absolute() or not self.sink.is_absolute():
            raise ValueError("absolute owned paths required")
        parts = self.sink.relative_to(self.anchor).parts
        if not parts or any(part in {".", ".."} for part in parts):
            raise ValueError("sink outside anchor")
        parent = self.anchor
        _plain_chain(parent)
        anchor_info = _plain(parent, directory=True)
        if os.name != "nt" and (anchor_info.st_mode & 0o077):
            raise ValueError("anchor is not private")
        for part in parts[:-1]:
            parent = parent / part
            _plain(parent, directory=True)
        path_info = _plain(self.sink, directory=False)
        if (self.stream.closed or self.stream.mode not in {"x+b", "xb+"}
                or not self.stream.seekable() or self.stream.fileno() != self.descriptor):
            raise ValueError("sink requires the original seekable x+b stream")
        if os.name != "nt":
            import fcntl
            flags = fcntl.fcntl(self.descriptor, fcntl.F_GETFL)
            if flags & os.O_APPEND or flags & os.O_ACCMODE != os.O_RDWR:
                raise ValueError("sink requires a non-append read/write descriptor")
        # Stream admission is portable; POSIX additionally checks raw fd flags.
        # Adversarial fd reuse/rewrapping is outside the private caller contract.
        fd_info = os.fstat(self.descriptor)
        if (not stat.S_ISREG(fd_info.st_mode) or fd_info.st_nlink != 1
                or path_info.st_nlink != 1
                or (fd_info.st_dev, fd_info.st_ino) != (path_info.st_dev, path_info.st_ino)):
            raise ValueError("sink identity is not exclusive")

    def flush(self):
        with self._lock:
            if self._busy:
                self.sink_failures += 1
                return False
            self._busy = True
            try:
                return self._flush()
            finally:
                self._busy = False

    def _flush(self):
        try:
            self._check_sink()
            data = self._encode()
            if len(data) > self.MAX_BYTES:
                raise ValueError("trace bound exceeded")
            os.lseek(self.descriptor, 0, os.SEEK_SET)
            view = memoryview(data)
            while view:
                written = os.write(self.descriptor, view)
                if written <= 0:
                    raise OSError("no trace write progress")
                view = view[written:]
            os.ftruncate(self.descriptor, len(data))
            return True
        except BaseException:
            # Only optional sink work is isolated, including an interrupted sink.
            self.sink_failures += 1
            return False

    def observe(self, stage, **values):
        self.record(stage, **values)
        if stage in {"domain-launch", "domain-launched", "client-deadline", "before-cleanup"}:
            self.flush()

    @contextmanager
    def node(self):
        from csk.builds import go_v1

        token = go_v1._OBSERVATION_SINK.set(self.observe)

        def retain(stage):
            try:
                self.record(stage)
                self.flush()
            except BaseException:
                # Isolate optional recording, never the yielded operation.
                pass

        try:
            retain("node-start")
            try:
                self.record("coverage", coverage="hardened-worker-uncovered")
                self.record("coverage", coverage="native-and-other-writers-uncovered")
            except BaseException:
                pass
            try:
                yield self
            except BaseException:
                retain("failure")
                raise
            finally:
                retain("node-end")
        finally:
            go_v1._OBSERVATION_SINK.reset(token)


class CacheAudit:
    """One exact trusted root, explicit relative cache/module allowlist, no scan."""

    def __init__(self, recorder: Recorder, root: Path, cache_modules):
        self.recorder, self.root = recorder, root
        if not root.is_absolute():
            raise ValueError("absolute root required")
        _plain_chain(root)
        self.modules = {}
        if not 1 <= len(cache_modules) <= 128:
            raise ValueError("bounded cache allowlist required")
        for cache, modules in cache_modules.items():
            path = Path(cache)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.name != "__pycache__":
                raise ValueError("explicit relative cache required")
            if len(path.parts) > 8 or any(not part.isidentifier() or len(part) > 64 for part in path.parts):
                raise ValueError("bounded known relative components required")
            if not 1 <= len(modules) <= 128 or any(not module.isidentifier() or len(module) > 64 for module in modules):
                raise ValueError("explicit bounded module stems required")
            self.modules[path] = tuple(modules)
        self.names = {cache.as_posix() for cache in self.modules}
        self.names.update((cache / (module + ".pyc")).as_posix()
                          for cache, modules in self.modules.items() for module in modules)
        recorder._relative_names = frozenset(self.names)
        self.enabled = False
        self._local = threading.local()

    def relative(self, raw):
        if not isinstance(raw, (str, bytes)):
            return None
        try:
            _plain_chain(self.root)
            path = Path(os.fsdecode(raw))
            if not path.is_absolute():
                return None
            relative = path.relative_to(self.root)
            if ".." in relative.parts:
                return None
            cache = relative if relative in self.modules else relative.parent
            if cache not in self.modules:
                return None
            canonical = cache.as_posix()
            if relative != cache:
                # CPython's version tag and temporary integer suffix are never
                # serialized. Match only the explicitly trusted module stem.
                pieces = relative.name.split(".")
                if (len(pieces) < 3 or pieces[0] not in self.modules[cache]
                        or not pieces[1].startswith("cpython-")
                        or not pieces[1][8:].isdigit() or len(pieces[1]) > 12):
                    return None
                suffix = pieces[2:]
                if suffix[0] == "opt-1" or suffix[0] == "opt-2":
                    suffix = suffix[1:]
                if not suffix or suffix[0] != "pyc" or len(suffix) > 2 or (len(suffix) == 2 and not suffix[1].isdigit()):
                    return None
                canonical = (cache / (pieces[0] + ".pyc")).as_posix()
            parent = self.root
            for index, part in enumerate(relative.parts):
                parent = parent / part
                try:
                    info = parent.lstat()
                except FileNotFoundError:
                    # A nonexistent cache subtree has no referent to follow.
                    break
                if _linked(info) or (index < len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode)):
                    return None
            return canonical
        except (OSError, ValueError):
            return None

    def _drop(self):
        try:
            self.recorder.drop()
        except BaseException:
            # Incomplete telemetry is preferable to a new operation failure.
            # If accounting also fails, its count is explicitly unavailable.
            pass

    def _record(self, stage, **values):
        try:
            self.recorder.record(stage, **values)
        except BaseException:
            self._drop()

    def _hook(self, event, args):
        if not self.enabled or getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            operation, paths = None, ()
            if event == "os.mkdir" and args[2] == -1:
                operation, paths = "mkdir", (args[0],)
            elif event == "open":
                flags = args[2]
                if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                    operation, paths = "write", (args[0],)
            elif event == "os.rename" and args[2:] == (-1, -1):
                operation, paths = "rename", (args[0], args[1])
            if operation is not None:
                for path in paths:
                    name = self.relative(path)
                    if name is not None:
                        self._record("cache-intent", root="manager", relative=name,
                                             operation=operation, coverage="python-audit-intent-only",
                                             no_site=bool(sys.flags.no_site),
                                             dont_write_bytecode=bool(sys.dont_write_bytecode))
        except BaseException:
            # Diagnostic-hook failures alone cannot veto the audited operation.
            self._drop()
        finally:
            self._local.busy = False

    def install_in_owned_child(self, *, owner_pid: int):
        if os.getpid() == owner_pid or os.getppid() != owner_pid or self.recorder.role != "test-child":
            raise ValueError("audit installation requires an explicit owned child")
        sys.addaudithook(self._hook)
        self.enabled = True
        self._record("coverage", coverage="python-audit-intent-only",
                             no_site=bool(sys.flags.no_site),
                             dont_write_bytecode=bool(sys.dont_write_bytecode))

    def post_state(self, path: Path):
        try:
            name = self.relative(os.fspath(path))
            if name is None:
                return
            try:
                info = path.lstat()
            except FileNotFoundError:
                self._record("cache-post", root="manager", relative=name,
                             coverage="explicit-post-state", present=False)
            else:
                self._record("cache-post", root="manager", relative=name,
                             coverage="explicit-post-state", present=True,
                             dev=info.st_dev, ino=info.st_ino, mode=info.st_mode,
                             nlink=info.st_nlink, size=info.st_size)
        except BaseException:
            # Every operation here is optional observation, not the real body.
            self._drop()

    def action(self, operation, path, call):
        result = call()  # exact body exception propagates; no false completion
        try:
            name = self.relative(os.fspath(path))
            if name is not None:
                self._record("cache-completed", root="manager", relative=name,
                                     operation=operation, coverage="action-returned")
        except BaseException:
            # The action has returned; optional completion recording cannot veto it.
            self._drop()
        return result
