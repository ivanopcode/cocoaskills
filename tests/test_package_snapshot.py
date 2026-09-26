"""Local package snapshot capture, freeze and revalidation (spec section 3).

Drives ``csk.sources.snapshot`` on real temporary-filesystem fixtures:
working-tree capture with dirty/staged/untracked bytes and ``.git`` pruning,
process-free capture, opened-descriptor admission of regular files with
named refusals for links and special files, end-to-end conformance vectors
from disk through the production inventory function, the probed host
filesystem-equivalence predicate, capture-mutation and frozen-copy-mutation
revalidations, and the three-revalidation ownership table.

Instruments: a ``sys.addaudithook`` confinement plus read-only property
(S-FS, S-TXN shape), fault injection at the real ``os.*`` call sites with
positive controls (S-ERRORS), a raw-bytes identity property over generated
content (S-IDENTITY), and before/after tree hashes around faulted captures
(S-TXN).
"""

from __future__ import annotations

import ast
import errno
import hashlib
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from csk.sources import _selection_fs, local_snapshot
from csk.sources import snapshot as snapshot_module
from csk.sources._selection_fs import (
    ConflationProbe,
    Directory,
    PreflightPath,
    PreflightRequest,
    SelectionSession,
)
from csk.sources.errors import (
    CODE_INVENTORY_INVALID,
    CODE_MEMBER_INVALID,
    CODE_OUTPUT_OVERLAP,
    CODE_SELECTION_INVALID,
    CODE_SNAPSHOT_CHANGED,
    SourceError,
)
from csk.sources.selection import PRUNED_CHILD_NAMES
from csk.sources.snapshot import FilesystemEquivalence, FrozenFile
from conftest import commit_all, init_git_repo

_AUDIT_EVENTS: list[tuple[str, tuple[Any, ...]]] = []


def _record_audit_event(event: str, args: tuple[Any, ...]) -> None:
    if event in ("open", "os.scandir"):
        _AUDIT_EVENTS.append((event, args))


sys.addaudithook(_record_audit_event)


@pytest.fixture(autouse=True)
def _require_descriptor_traversal(request: pytest.FixtureRequest) -> None:
    """Skip traversal-bound snapshot tests where the runtime lacks it."""

    if request.node.get_closest_marker("posix_traversal_independent"):
        return
    if not _selection_fs.supports_descriptor_traversal():
        pytest.skip(_selection_fs.NO_DESCRIPTOR_TRAVERSAL_REASON)


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for relative, raw in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def _open_session(
    source_root: Path, home: Path, components: tuple[str, ...] = ()
) -> SelectionSession:
    return SelectionSession.open(
        source_root,
        home,
        managed_names=PRUNED_CHILD_NAMES,
        preflight=PreflightRequest(
            paths=(
                PreflightPath(
                    components,
                    code=CODE_SELECTION_INVALID,
                    context="Snapshot test session",
                ),
            )
        ),
    )


def _capture_production(
    session: SelectionSession, member: Directory, *, label: str = "test"
) -> _selection_fs.CapturedTree:
    return session.capture_tree(
        member,
        label=label,
        code=snapshot_module.CODE_CAPTURE,
        missing_code=snapshot_module.CODE_CAPTURE_ABSENT,
        changed_code=snapshot_module.CODE_CAPTURE_CHANGED,
    )


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for current, _dirs, files in os.walk(root):
        for name in sorted(files):
            path = Path(current) / name
            digest.update(os.fspath(path.relative_to(root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _raises_structured(code: str, func: Callable[[], Any]) -> SourceError:
    with pytest.raises(SourceError) as excinfo:
        func()
    assert excinfo.value.code == code
    return excinfo.value


@contextmanager
def _faulty_os(
    func_name: str, predicate: Callable[[Any], bool], error: Exception
) -> Iterator[list[str]]:
    """Replace one os function with a failing wrapper inside the window."""

    real = getattr(os, func_name)
    fired: list[str] = []

    def wrapper(first: Any, *args: Any, **kwargs: Any) -> Any:
        if predicate(first):
            fired.append(repr(first))
            raise error
        return real(first, *args, **kwargs)

    setattr(os, func_name, wrapper)
    try:
        yield fired
    finally:
        setattr(os, func_name, real)


class _SpoofedEntry:
    """A directory entry whose reported stat is overridden for one name."""

    def __init__(
        self,
        real: os.DirEntry[str],
        stat_result: os.stat_result | None = None,
        error: Exception | None = None,
    ) -> None:
        self._real = real
        self._stat_result = stat_result
        self._error = error

    @property
    def name(self) -> str:
        return self._real.name

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        if self._error is not None:
            raise self._error
        if self._stat_result is not None:
            return self._stat_result
        return self._real.stat(follow_symlinks=follow_symlinks)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._real, attr)


class _SpoofedScandirIterator:
    def __init__(
        self,
        real: Any,
        spoof: Callable[[os.DirEntry[str]], _SpoofedEntry | None],
    ) -> None:
        self._real = real
        self._spoof = spoof

    def __enter__(self) -> "_SpoofedScandirIterator":
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def __iter__(self) -> "_SpoofedScandirIterator":
        return self

    def __next__(self) -> Any:
        real = next(self._real)
        return self._spoof(real) or real

    def close(self) -> None:
        self._real.close()


@contextmanager
def _spoofed_listing(
    spoof: Callable[[os.DirEntry[str]], _SpoofedEntry | None],
) -> Iterator[None]:
    """Spoof one listing's reported stats; opens still see the truth."""

    real_scandir = os.scandir

    def fake_scandir(path: Any, *args: Any, **kwargs: Any) -> Any:
        return _SpoofedScandirIterator(real_scandir(path, *args, **kwargs), spoof)

    setattr(os, "scandir", fake_scandir)
    try:
        yield
    finally:
        setattr(os, "scandir", real_scandir)


def _mutate_stat(
    value: os.stat_result, *, ino: int | None = None, dev: int | None = None
) -> os.stat_result:
    return os.stat_result(
        (
            value.st_mode,
            value.st_ino if ino is None else ino,
            value.st_dev if dev is None else dev,
            value.st_nlink,
            value.st_uid,
            value.st_gid,
            value.st_size,
            value.st_atime,
            value.st_mtime,
            value.st_ctime,
        )
    )


def _run_with_timeout(func: Callable[[], Any], timeout: float = 30.0) -> Any:
    """Run one capture; a hang fails the test instead of the suite."""

    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = func()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError("capture hung instead of refusing")
    if "error" in box:
        raise box["error"]
    return box.get("result")


# AC (a): a local path means admitted working-tree bytes, never Git HEAD.


def _git_available() -> bool:
    return shutil.which("git") is not None


def test_capture_reads_working_tree_bytes_not_git_head(tmp_path: Path) -> None:
    """Dirty, staged-shadowed and untracked bytes are captured; HEAD is not.

    Production call site: ``snapshot.capture_package_snapshot``.
    """

    if not _git_available():
        pytest.skip("the working-tree fixture needs a git binary")
    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    init_git_repo(root)
    (root / "tracked.txt").write_bytes(b"HEAD-bytes\n")
    (root / "staged.txt").write_bytes(b"HEAD-staged\n")
    commit_all(root, "base")
    (root / "tracked.txt").write_bytes(b"dirty-worktree\n")
    (root / "staged.txt").write_bytes(b"staged-bytes\n")
    subprocess.run(["git", "add", "staged.txt"], cwd=root, check=True)
    (root / "staged.txt").write_bytes(b"worktree-beats-index\n")
    (root / "untracked.txt").write_bytes(b"untracked-bytes\n")

    package = snapshot_module.capture_package_snapshot(root, ".", home=home)

    frozen = package.frozen_files()
    assert set(frozen) == {"tracked.txt", "staged.txt", "untracked.txt"}
    assert frozen["tracked.txt"].data == b"dirty-worktree\n"
    assert frozen["staged.txt"].data == b"worktree-beats-index\n"
    assert frozen["untracked.txt"].data == b"untracked-bytes\n"
    assert all(".git" not in path.split("/") for path in frozen)
    expected = local_snapshot.build_inventory(
        [
            ("tracked.txt", _sha256(b"dirty-worktree\n"), False),
            ("staged.txt", _sha256(b"worktree-beats-index\n"), False),
            ("untracked.txt", _sha256(b"untracked-bytes\n"), False),
        ],
        equivalent=lambda _left, _right: False,
    )
    assert package.inventory["snapshot"] == expected["snapshot"]


def test_capture_ignores_simulated_git_metadata_without_a_git_binary(
    tmp_path: Path,
) -> None:
    """A present-but-unread ``.git`` never reaches the snapshot.

    Production call site: ``snapshot.capture_package_snapshot``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(
        root,
        {
            ".git/HEAD": b"ref: refs/heads/main\n",
            ".git/objects/ab/cd": b"HEAD-bytes\n",
            ".git/refs/heads/main": b"0" * 40 + b"\n",
            "tracked.txt": b"worktree-bytes\n",
        },
    )

    package = snapshot_module.capture_package_snapshot(root, ".", home=home)

    frozen = package.frozen_files()
    assert set(frozen) == {"tracked.txt"}
    assert frozen["tracked.txt"].data == b"worktree-bytes\n"


def test_capture_without_any_git_metadata(tmp_path: Path) -> None:
    """Capture needs no ``.git`` at all.

    Production call site: ``snapshot.capture_package_snapshot``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# plain\n"})

    package = snapshot_module.capture_package_snapshot(root, ".", home=home)

    assert set(package.frozen_files()) == {"SKILL.md"}


def test_top_level_git_directory_is_pruned_but_a_git_file_is_plain_bytes(
    tmp_path: Path,
) -> None:
    """The frozen managed set prunes ``.git/``; a ``.git`` file is admitted.

    A top-level ``.git`` directory is a managed location in the frozen
    Phase-A record, so it prunes. A ``.git`` file (a worktree pointer) is
    a regular file the record does not name, so it captures as ordinary
    admitted bytes. The machine-specific ``gitdir:`` contents that choice
    admits are recorded as a seam note: the boundary table owns the
    file-vs-directory question, and this leaf must not re-decide it.

    Production call site: ``snapshot.capture_package_snapshot``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    package_dir = root / "pkg"
    _write_tree(
        package_dir,
        {
            ".git/objects/pack/data": b"metadata\n",
            "SKILL.md": b"# review\n",
        },
    )
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")

    package = snapshot_module.capture_package_snapshot(root, "pkg", home=home)

    frozen = package.frozen_files()
    assert set(frozen) == {"SKILL.md"}
    assert all(".git" not in path.split("/") for path in frozen)

    worktree = tmp_path / "linked"
    _write_tree(
        worktree,
        {
            ".git": b"gitdir: /elsewhere/worktrees/main\n",
            "SKILL.md": b"# review\n",
        },
    )
    linked = snapshot_module.capture_package_snapshot(worktree, ".", home=home)
    assert linked.frozen_files()[".git"].data == b"gitdir: /elsewhere/worktrees/main\n"


def test_nested_git_directory_is_captured_as_authored_bytes(tmp_path: Path) -> None:
    """Only the frozen managed set prunes; a nested ``.git`` is admitted.

    The Phase-A record names managed locations at the source root and
    along the preflighted descent, never by name at depth. A nested
    ``.git`` is therefore ordinary admitted bytes here. Name-based
    pruning inside capture would re-decide admission owned by
    TASK-260916-100uew, so the behaviour is pinned, not patched.

    Production call site: ``snapshot.capture_package_snapshot``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(
        root,
        {
            "sub/.git/config": b"[core]\n",
            "SKILL.md": b"# review\n",
        },
    )

    package = snapshot_module.capture_package_snapshot(root, ".", home=home)

    assert package.frozen_files()["sub/.git/config"].data == b"[core]\n"


def test_capture_spawns_no_git_process(tmp_path: Path) -> None:
    """The whole capture path runs with process spawning disabled.

    Production call site: ``snapshot.capture_package_snapshot`` (which
    covers session open, descent, capture, probing and revalidation).
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(
        root,
        {
            ".git/HEAD": b"ref: refs/heads/main\n",
            "SKILL.md": b"# review\n",
        },
    )
    real_popen = subprocess.Popen
    real_run = subprocess.run
    real_posix_spawn = getattr(os, "posix_spawn", None)
    real_spawn_names = [
        name for name in ("spawnlp", "spawnlpe", "spawnvp", "spawnvpe") if hasattr(os, name)
    ]
    saved_spawns = {name: getattr(os, name) for name in real_spawn_names}

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"capture spawned a process: {args!r}")

    subprocess.Popen = _forbidden  # type: ignore[assignment]
    subprocess.run = _forbidden  # type: ignore[assignment]
    if real_posix_spawn is not None:
        setattr(os, "posix_spawn", _forbidden)
    for name in real_spawn_names:
        setattr(os, name, _forbidden)
    try:
        package = snapshot_module.capture_package_snapshot(root, ".", home=home)
    finally:
        subprocess.Popen = real_popen
        subprocess.run = real_run
        if real_posix_spawn is not None:
            setattr(os, "posix_spawn", real_posix_spawn)
        for name, func in saved_spawns.items():
            setattr(os, name, func)

    assert set(package.frozen_files()) == {"SKILL.md"}


@pytest.mark.posix_traversal_independent
def test_snapshot_module_reaches_no_process_spawning_primitive() -> None:
    """``snapshot.py`` cannot spawn Git: no such primitive is referenced."""

    path = Path(snapshot_module.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden = {
        "Popen",
        "run",
        "call",
        "check_output",
        "check_call",
        "posix_spawn",
        "spawnlp",
        "spawnlpe",
        "spawnvp",
        "spawnvpe",
        "spawnl",
        "spawnle",
        "spawnv",
        "spawnve",
        "system",
        "popen",
        "execv",
        "execve",
        "execl",
        "execlp",
        "execlpe",
        "execvp",
        "execvpe",
    }
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in {"subprocess", "multiprocessing"}:
                    violations.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in {"subprocess", "multiprocessing"}:
                violations.append(f"from {node.module} import")
        elif isinstance(node, ast.Attribute) and node.attr in forbidden:
            violations.append(f".{node.attr} at line {node.lineno}")
    assert violations == []


# AC (b): only regular files admit, decided on the opened descriptor.


def _special_kinds() -> dict[str, Callable[[Path], str]]:
    def _symlink_file(root: Path) -> str:
        (root / "elsewhere.txt").write_bytes(b"real\n")
        (root / "link.txt").symlink_to(root / "elsewhere.txt")
        return "link.txt"

    def _symlink_dir(root: Path) -> str:
        (root / "realdir").mkdir()
        (root / "linkdir").symlink_to(root / "realdir", target_is_directory=True)
        return "linkdir"

    def _symlink_dangling(root: Path) -> str:
        (root / "dangling").symlink_to(root / "no-such-target")
        return "dangling"

    def _symlink_nested(root: Path) -> str:
        (root / "sub").mkdir()
        (root / "sub" / "inner").symlink_to(root / "sub")
        return "sub/inner"

    def _fifo(root: Path) -> str:
        if not hasattr(os, "mkfifo"):
            pytest.skip("os.mkfifo is unavailable (POSIX-only special-file fixture)")
        os.mkfifo(root / "pipe")
        return "pipe"

    def _socket(root: Path) -> str:
        # AF_UNIX paths are capped at 104 bytes on macOS, so the socket
        # name stays one character; the caller shortens the fixture root.
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.bind(os.fspath(root / "s"))
        except OSError as exc:
            probe.close()
            pytest.skip(f"AF_UNIX socket files are unavailable on this host ({exc})")
        probe.close()
        return "s"

    return {
        "symlink-to-file": _symlink_file,
        "symlink-to-dir": _symlink_dir,
        "dangling-symlink": _symlink_dangling,
        "nested-symlink": _symlink_nested,
        "fifo": _fifo,
        "unix-socket": _socket,
    }


@pytest.mark.parametrize("kind", sorted(_special_kinds()))
def test_admitted_special_files_and_links_refuse_naming_the_path(
    tmp_path: Path, kind: str
) -> None:
    """Every non-regular member refuses with its package-relative path.

    Production call site: ``snapshot.capture_package``.
    """

    import tempfile

    if kind == "unix-socket" and not hasattr(socket, "AF_UNIX"):
        pytest.skip("Python on this Windows runner has no AF_UNIX socket fixture")

    if kind == "unix-socket":
        # pytest's tmp_path already exhausts the AF_UNIX path budget on
        # macOS; the socket fixture lives directly under TMPDIR instead.
        base = Path(tempfile.mkdtemp(prefix="csk-sock-"))
        owned = True
    else:
        base = tmp_path
        owned = False
    try:
        root = base / "src"
        home = base / "home"
        home.mkdir()
        root.mkdir()
        (root / "SKILL.md").write_bytes(b"# review\n")
        shown = _special_kinds()[kind](root)

        with _open_session(root, home) as session:
            error = _raises_structured(
                CODE_MEMBER_INVALID,
                lambda: snapshot_module.capture_package(
                    session, session.root, label="."
                ),
            )
    finally:
        if owned:
            shutil.rmtree(base, ignore_errors=True)

    assert shown in error.detail


def test_device_nodes_refuse_where_the_host_can_make_them(
    tmp_path: Path,
) -> None:
    """Device nodes refuse; unprivileged hosts declare the bound instead.

    Production call site: ``snapshot.capture_package``.
    """

    if not hasattr(os, "mknod"):
        pytest.skip("os.mknod is unavailable (POSIX-only special-file fixture)")
    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    root.mkdir()
    (root / "SKILL.md").write_bytes(b"# review\n")
    try:
        os.mknod(root / "node", 0o600 | stat.S_IFCHR, os.makedev(0, 0))
    except (PermissionError, OSError) as exc:
        pytest.skip(
            "creating device nodes needs privilege "
            f"(declared platform bound; the fstat type gate covers them: {exc})"
        )

    with _open_session(root, home) as session:
        error = _raises_structured(
            CODE_MEMBER_INVALID,
            lambda: snapshot_module.capture_package(session, session.root, label="."),
        )

    assert "node" in error.detail


def test_directory_offered_as_a_file_refuses_without_traversal(
    tmp_path: Path,
) -> None:
    """``read_captured_file`` on a directory refuses with the caller code.

    A direct call carries no listing evidence, so race semantics cannot
    apply: the anomaly reports ``code``, never ``changed_code``.

    Production call site: ``SelectionSession.read_captured_file``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    (root / "sub").mkdir(parents=True)

    with _open_session(root, home) as session:
        error = _raises_structured(
            CODE_MEMBER_INVALID,
            lambda: session.read_captured_file(
                session.root,
                "sub",
                relative=("sub",),
                code=CODE_MEMBER_INVALID,
                context="Snapshot test",
                missing_code=CODE_SNAPSHOT_CHANGED,
                changed_code=CODE_SNAPSHOT_CHANGED,
            ),
        )

    assert "sub" in error.detail


def test_direct_file_read_maps_absence_to_the_missing_code(
    tmp_path: Path,
) -> None:
    """``read_captured_file`` on a missing name refuses as missing.

    Production call site: ``SelectionSession.read_captured_file``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    root.mkdir()

    with _open_session(root, home) as session:
        error = _raises_structured(
            CODE_SNAPSHOT_CHANGED,
            lambda: session.read_captured_file(
                session.root,
                "gone.txt",
                relative=("gone.txt",),
                code=CODE_MEMBER_INVALID,
                context="Snapshot test",
                missing_code=CODE_SNAPSHOT_CHANGED,
                changed_code=CODE_SNAPSHOT_CHANGED,
            ),
        )

    assert "gone.txt" in error.detail


@pytest.mark.skipif(
    os.name == "nt",
    reason="this fault fixture spoofs POSIX os.DirEntry.stat; Windows has handle-based race coverage",
)
def test_regular_file_admission_uses_the_opened_descriptor(
    tmp_path: Path,
) -> None:
    """A listing that lies about identity cannot admit a replacement.

    The listing reports the victim's old identity while the opened
    descriptor is a different file; admission follows the descriptor and
    the skew refuses as a concurrent mutation.

    Production call site: ``snapshot.capture_package``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n", "victim.txt": b"v1\n"})

    def spoof(entry: os.DirEntry[str]) -> _SpoofedEntry | None:
        if entry.name != "victim.txt":
            return None
        real_stat = entry.stat(follow_symlinks=False)
        return _SpoofedEntry(entry, _mutate_stat(real_stat, ino=real_stat.st_ino + 2**40))

    with _open_session(root, home) as session:
        with _spoofed_listing(spoof):
            error = _raises_structured(
                CODE_SNAPSHOT_CHANGED,
                lambda: snapshot_module.capture_package(
                    session, session.root, label="."
                ),
            )

    assert "victim.txt" in error.detail


@pytest.mark.skipif(
    os.name == "nt",
    reason="this fault fixture spoofs POSIX os.DirEntry.stat; Windows has handle-based race coverage",
)
def test_directory_identity_skew_between_listing_and_open_refuses(
    tmp_path: Path,
) -> None:
    """A replaced directory cannot hide behind its old listed identity.

    Production call site: ``snapshot.capture_package``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"sub/nested.txt": b"n\n", "SKILL.md": b"# review\n"})

    def spoof(entry: os.DirEntry[str]) -> _SpoofedEntry | None:
        if entry.name != "sub":
            return None
        real_stat = entry.stat(follow_symlinks=False)
        return _SpoofedEntry(entry, _mutate_stat(real_stat, ino=real_stat.st_ino + 2**40))

    with _open_session(root, home) as session:
        with _spoofed_listing(spoof):
            error = _raises_structured(
                CODE_SNAPSHOT_CHANGED,
                lambda: snapshot_module.capture_package(
                    session, session.root, label="."
                ),
            )

    assert "sub" in error.detail


@pytest.mark.skipif(
    os.name == "nt",
    reason="this deterministic cross-device fixture spoofs POSIX os.DirEntry.stat",
)
def test_cross_device_entry_refuses_with_the_admission_code(
    tmp_path: Path,
) -> None:
    """A stably cross-device entry is admission, never a mutation report.

    The listing already shows the foreign device, so the refusal carries
    the capture code. A second writable filesystem is not requirable on
    test hosts; the listing spoof stands in for it deterministically.

    Production call site: ``snapshot.capture_package``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n", "foreign.txt": b"f\n"})

    def spoof(entry: os.DirEntry[str]) -> _SpoofedEntry | None:
        if entry.name != "foreign.txt":
            return None
        real_stat = entry.stat(follow_symlinks=False)
        return _SpoofedEntry(entry, _mutate_stat(real_stat, dev=real_stat.st_dev + 2**40))

    with _open_session(root, home) as session:
        with _spoofed_listing(spoof):
            error = _raises_structured(
                CODE_MEMBER_INVALID,
                lambda: snapshot_module.capture_package(
                    session, session.root, label="."
                ),
            )

    assert "foreign.txt" in error.detail
    assert "filesystem boundary" in error.detail


@pytest.mark.skipif(
    os.name == "nt",
    reason="this race fixture intercepts POSIX os.open(dir_fd=); Windows link races use NT handles",
)
def test_replacement_by_symlink_between_listing_and_open_refuses(
    tmp_path: Path,
) -> None:
    """An entry swapped for a link refuses at the no-follow open.

    Without ``O_NOFOLLOW`` the open would follow the planted link into
    bytes outside the capture; the refusal proves the open did not.

    Production call site: ``snapshot.capture_package``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    outside = tmp_path / "outside.txt"
    home.mkdir()
    outside.write_bytes(b"foreign-bytes\n")
    _write_tree(root, {"SKILL.md": b"# review\n", "victim.txt": b"v1\n"})
    real_open = os.open
    swapped: list[str] = []

    def swapping_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if path == "victim.txt" and kwargs.get("dir_fd") is not None and not swapped:
            swapped.append("victim.txt")
            (root / "victim.txt").unlink()
            (root / "victim.txt").symlink_to(outside)
        return real_open(path, *args, **kwargs)

    setattr(os, "open", swapping_open)
    try:
        with _open_session(root, home) as session:
            error = _raises_structured(
                CODE_MEMBER_INVALID,
                lambda: snapshot_module.capture_package(
                    session, session.root, label="."
                ),
            )
    finally:
        setattr(os, "open", real_open)

    assert swapped == ["victim.txt"]
    assert "victim.txt" in error.detail


def test_replacement_by_fifo_between_listing_and_open_refuses_without_hanging(
    tmp_path: Path,
) -> None:
    """An entry swapped for a FIFO refuses; the open never blocks.

    A blocking open here would hang the operation instead of reaching
    the fstat type gate. The timeout wrapper turns that hang into a
    test failure rather than a stuck suite.

    Production call site: ``snapshot.capture_package``.
    """

    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo is unavailable (POSIX-only race fixture)")
    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n", "victim.txt": b"v1\n"})
    real_open = os.open

    def swapping_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if path == "victim.txt" and kwargs.get("dir_fd") is not None:
            try:
                (root / "victim.txt").unlink()
            except FileNotFoundError:
                pass
            try:
                os.mkfifo(root / "victim.txt")
            except FileExistsError:
                pass
        return real_open(path, *args, **kwargs)

    def drive() -> SourceError:
        with _open_session(root, home) as session:
            return _raises_structured(
                CODE_SNAPSHOT_CHANGED,
                lambda: snapshot_module.capture_package(
                    session, session.root, label="."
                ),
            )

    setattr(os, "open", swapping_open)
    try:
        error = _run_with_timeout(drive)
    finally:
        setattr(os, "open", real_open)

    assert "victim.txt" in error.detail


@pytest.mark.parametrize("moment", ["stable", "race"])
def test_hard_links_refuse_stable_and_race(tmp_path: Path, moment: str) -> None:
    """A multiply-linked entry refuses; a link created mid-walk is a race.

    Production call site: ``snapshot.capture_package``.
    """

    if os.name == "nt" and moment == "race":
        pytest.skip(
            "the race injector patches POSIX os.open(dir_fd=); stable Windows hard-link refusal still runs"
        )

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n", "victim.txt": b"v1\n"})
    if moment == "stable":
        os.link(root / "victim.txt", root / "alias.txt")

        def drive_stable() -> SourceError:
            with _open_session(root, home) as session:
                return _raises_structured(
                    CODE_MEMBER_INVALID,
                    lambda: snapshot_module.capture_package(
                        session, session.root, label="."
                    ),
                )

        error = drive_stable()
        assert "victim.txt" in error.detail or "alias.txt" in error.detail
        return

    real_open = os.open
    linked: list[str] = []

    def linking_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if path == "victim.txt" and kwargs.get("dir_fd") is not None and not linked:
            linked.append("victim.txt")
            os.link(root / "victim.txt", root / "alias.txt")
        return real_open(path, *args, **kwargs)

    setattr(os, "open", linking_open)
    try:
        with _open_session(root, home) as session:
            error = _raises_structured(
                CODE_SNAPSHOT_CHANGED,
                lambda: snapshot_module.capture_package(
                    session, session.root, label="."
                ),
            )
    finally:
        setattr(os, "open", real_open)

    assert linked == ["victim.txt"]
    assert "victim.txt" in error.detail


# AC (c): the inventory and digest are called, with real host inputs.


_SNAPSHOT_VECTORS: tuple[dict[str, Any], ...] = (
    {
        "id": "base",
        "utf8_files": {
            "SKILL.md": "name: review\n",
            "scripts/run.sh": "echo one\n",
            "build/main.go": "package main\n",
        },
        "expected": "sha256:481f362d0c82cdf8c32ed854edfc125d21a7c470c0128086f7eebb1956d832a9",
    },
    {
        "id": "runtime-edit",
        "utf8_files": {
            "SKILL.md": "name: review\n",
            "scripts/run.sh": "echo two\n",
            "build/main.go": "package main\n",
        },
        "expected": "sha256:15ac234d51e71847165d42e2695df3d91ca49314bd5e5811eb17cad6ce8cea80",
    },
    {
        "id": "build-edit",
        "utf8_files": {
            "SKILL.md": "name: review\n",
            "scripts/run.sh": "echo one\n",
            "build/main.go": "package changed\n",
        },
        "expected": "sha256:2aa1bc2a835d6fd931de8f032761c17288bfefeaba492fe229158c602b8750ac",
    },
)


def _assert_vectors_match_the_pinned_suite() -> None:
    """Bind the hardcoded vector expectations to the conformance suite."""

    import json

    suite_root = os.environ.get("CSK_DRAFT_SOURCES_SUITE_ROOT")
    if not suite_root:
        pytest.skip("CSK_DRAFT_SOURCES_SUITE_ROOT is not set")
    assert suite_root is not None
    vectors = json.loads((Path(suite_root) / "snapshot-cases.json").read_bytes())
    by_id = {vector["id"]: vector for vector in vectors}
    assert {vector["id"] for vector in _SNAPSHOT_VECTORS} == set(by_id)
    for vector in _SNAPSHOT_VECTORS:
        suite_vector = by_id[vector["id"]]
        assert suite_vector["utf8_files"] == vector["utf8_files"]
        assert suite_vector["inventory"]["snapshot"] == vector["expected"]


@pytest.mark.parametrize("vector", _SNAPSHOT_VECTORS, ids=[item["id"] for item in _SNAPSHOT_VECTORS])
def test_conformance_vectors_end_to_end_from_disk(
    tmp_path: Path, vector: dict[str, Any]
) -> None:
    """Each snapshot vector reproduces byte-exact from real files on disk.

    Files are written with the vector bytes, ``scripts/run.sh`` carries
    the real POSIX execute bit, and the production capture plus the
    production inventory function yield the pinned digest.

    Production call sites: ``snapshot.capture_package_snapshot`` and
    ``local_snapshot.build_inventory`` (via ``captured_to_inventory``).
    """

    if os.name == "nt":
        pytest.skip(
            "the pinned vector includes a POSIX executable bit; Windows snapshot inventory reports false"
        )

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    for relative, text in vector["utf8_files"].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    script = root / "scripts" / "run.sh"
    script.chmod(0o755)

    package = snapshot_module.capture_package_snapshot(root, ".", home=home)

    assert package.inventory["snapshot"] == vector["expected"]
    assert local_snapshot.inventory_digest(package.inventory) == vector["expected"]
    assert package.inventory["files"] == [
        {
            "path": "SKILL.md",
            "sha256": _sha256(b"name: review\n"),
            "executable": False,
        },
        {
            "path": "build/main.go",
            "sha256": _sha256(
                vector["utf8_files"]["build/main.go"].encode("utf-8")
            ),
            "executable": False,
        },
        {
            "path": "scripts/run.sh",
            "sha256": _sha256(
                vector["utf8_files"]["scripts/run.sh"].encode("utf-8")
            ),
            "executable": True,
        },
    ]


@pytest.mark.posix_traversal_independent
def test_vector_expectations_match_the_pinned_suite() -> None:
    """The hardcoded vector oracle is the pinned suite's bytes."""

    _assert_vectors_match_the_pinned_suite()


@pytest.mark.parametrize(
    "mode",
    [0o644, 0o755, 0o600, 0o700, 0o400, 0o444, 0o777, 0o555, 0o650, 0o601],
    ids=["644", "755", "600", "700", "400", "444", "777", "555", "650", "601"],
)
def test_executable_bit_is_the_real_opened_descriptor_bit(
    tmp_path: Path, mode: int
) -> None:
    """``executable`` is any POSIX execute bit on the opened descriptor.

    Production call site: ``snapshot.capture_package``.
    """

    if os.name == "nt":
        pytest.skip(
            "Windows snapshot inventory reports executable=false when the filesystem cannot report it"
        )

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n", "probe.sh": b"x\n"})
    (root / "probe.sh").chmod(mode)

    package = snapshot_module.capture_package_snapshot(root, ".", home=home)

    assert package.frozen_files()["probe.sh"].executable == bool(mode & 0o111)


@pytest.mark.parametrize("mode", [0o000, 0o100, 0o111], ids=["000", "100", "111"])
def test_unreadable_files_refuse_as_structured_inspection_failures(
    tmp_path: Path, mode: int
) -> None:
    """Modes without read permission refuse; the failure names the file.

    Production call site: ``snapshot.capture_package``.
    """

    if os.name == "nt":
        pytest.skip("chmod mode bits do not enforce Windows file-read ACLs")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads through permission bits (named platform bound)")
    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n", "probe.sh": b"x\n"})
    (root / "probe.sh").chmod(mode)
    try:
        with _open_session(root, home) as session:
            error = _raises_structured(
                CODE_MEMBER_INVALID,
                lambda: snapshot_module.capture_package(
                    session, session.root, label="."
                ),
            )
    finally:
        (root / "probe.sh").chmod(0o644)

    assert "probe.sh" in error.detail


@pytest.mark.posix_traversal_independent
@pytest.mark.parametrize(
    "facts",
    [
        FilesystemEquivalence(False, False, True, True),
        FilesystemEquivalence(True, False, True, True),
        FilesystemEquivalence(False, True, True, True),
        FilesystemEquivalence(True, True, True, True),
        FilesystemEquivalence(False, False, False, False),
        FilesystemEquivalence(True, True, False, False),
    ],
    ids=[
        "sensitive",
        "case-folds",
        "norm-folds",
        "both-fold",
        "unknown-exact",
        "impossible-contract-exact",
    ],
)
@pytest.mark.parametrize(
    ("left", "right", "axis"),
    [
        ("A", "a", "case"),
        ("SKILL.md", "skill.md", "case"),
        ("caf\u00e9", "cafe\u0301", "normalization"),
        ("same", "same", "identical"),
        ("alpha", "beta", "distinct"),
    ],
)
def test_filesystem_equivalence_folding_logic(
    facts: FilesystemEquivalence, left: str, right: str, axis: str
) -> None:
    """The predicate folds only relations the probe proved and knew."""

    if axis == "identical":
        assert facts.equivalent(left, right) is False
    elif axis == "case":
        expected = facts.case_conflates and facts.case_known
        assert facts.equivalent(left, right) is expected
    elif axis == "normalization":
        expected = facts.normalization_conflates and facts.normalization_known
        assert facts.equivalent(left, right) is expected
    else:
        assert facts.equivalent(left, right) is False
    assert type(facts.equivalent(left, right)) is bool


def test_equivalence_predicate_matches_the_host_filesystem(
    tmp_path: Path,
) -> None:
    """The probed predicate agrees with the host on every spelling pair.

    Ground truth opens both spellings and compares identities; the
    predicate comes from an unrelated production capture. No branch
    consults the platform name: the test adapts through ground truth.

    Production call site: ``snapshot.probe_filesystem_equivalence`` (via
    ``snapshot.capture_package``).
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(
        root,
        {
            "SKILL.md": b"# review\n",
            "caf\u00e9.txt": b"nfc\n",
            "plain": b"p\n",
        },
    )
    package = snapshot_module.capture_package_snapshot(root, ".", home=home)
    assert package.equivalence.case_known
    assert package.equivalence.normalization_known

    ground = tmp_path / "ground"
    ground.mkdir()
    pairs = [
        ("CaseFile", "casefile"),
        ("CASEFILE", "CaseFile"),
        ("caf\u00e9", "cafe\u0301"),
        ("CAFE\u0301", "caf\u00e9"),
        ("alpha", "beta"),
    ]
    if os.name != "nt":
        # Win32 path lookup trims trailing dots and spaces. The NT backend
        # deliberately validates such components before opening them, so a
        # Win32 ground-truth lookup would measure a different namespace.
        pairs.append(("trailing ", "trailing"))
    for first, second in pairs:
        target = ground / first
        target.write_bytes(b"g\n")

        def _identity(name: str) -> tuple[int, int] | None:
            try:
                value = os.stat(ground / name)
            except OSError:
                return None
            return (value.st_dev, value.st_ino)

        expected = _identity(first) == _identity(second) and _identity(second) is not None
        assert package.equivalence.equivalent(first, second) is expected, (first, second)
        assert package.equivalence.equivalent(second, first) is expected, (second, first)
        target.unlink()
    assert package.equivalence.equivalent("same", "same") is False


def test_unknown_axes_fail_toward_the_exact_predicate(tmp_path: Path) -> None:
    """A capture with no probed spelling yields the exact predicate.

    Production call site: ``snapshot.capture_package``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"123": b"a\n", "456.789": b"b\n"})

    package = snapshot_module.capture_package_snapshot(root, ".", home=home)

    assert package.equivalence == FilesystemEquivalence(
        case_conflates=False,
        normalization_conflates=False,
        case_known=False,
        normalization_known=False,
    )
    assert package.equivalence.equivalent("A", "a") is False


def test_probe_failures_leave_the_axis_unknown_and_capture_succeeds(
    tmp_path: Path,
) -> None:
    """An inconclusive probe is visible unknown, never a failure or fold.

    Production call site: ``snapshot.capture_package``.
    """

    if os.name == "nt":
        pytest.skip("this probe fault fixture intercepts POSIX os.open(dir_fd=)")

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n"})

    with _open_session(root, home) as session:
        package = snapshot_module.capture_package(session, session.root, label=".")
    assert package.equivalence.case_known

    variant = "skill.md"
    assert variant != "SKILL.md"
    with _open_session(root, home) as session:
        with _faulty_os(
            "open",
            lambda value: value == variant,
            PermissionError(errno.EACCES, "injected"),
        ) as fired:
            faulted = snapshot_module.capture_package(session, session.root, label=".")

    assert fired, "the variant probe open was never reached"
    assert faulted.equivalence.case_known is False
    assert faulted.equivalence.case_conflates is False
    assert set(faulted.frozen_files()) == {"SKILL.md"}


def test_probe_votes_match_the_host_ground_truth(tmp_path: Path) -> None:
    """Direct probe votes agree with open-both-spellings ground truth.

    Production call site: ``snapshot.probe_filesystem_equivalence``.
    """

    probe_dir = tmp_path / "probes"
    probe_dir.mkdir()
    names = ["CaseProbe", "MiXeD", "lower", "UPPER"]
    for name in names:
        (probe_dir / name).write_bytes(b"g\n")
    fd = _selection_fs._open_descriptor(
        probe_dir, _selection_fs._directory_flags(nofollow=False)
    )
    try:
        parent_identity = _selection_fs._safe_identity(fd)
        assert parent_identity is not None
        parent = Directory(
            fd=fd,
            identity=parent_identity,
            name=probe_dir.name,
            display=probe_dir,
            parent=None,
        )
        probes: list[ConflationProbe] = []
        for name in names:
            value = _selection_fs._stat_child(
                fd, name, parent_path=probe_dir, follow_symlinks=False
            )
            probes.append(
                ConflationProbe(
                    parent,
                    name,
                    _selection_fs._identity_from_stat(value),
                    frozenset({"case"}),
                )
            )
        facts = snapshot_module.probe_filesystem_equivalence(tuple(probes))
    finally:
        _selection_fs._close_quietly(fd)

    assert facts.case_known
    for name in names:
        lowered = name.lower()
        if lowered == name:
            continue
        try:
            same = os.path.samefile(probe_dir / name, probe_dir / lowered)
        except OSError:
            same = False
        assert facts.case_conflates is same, name


def test_probe_link_variant_votes_distinction(tmp_path: Path) -> None:
    """A link at the variant spelling proves the filesystem distinguishes.

    On a conflating filesystem the variant would name the same entry, so
    a link there is positive distinction evidence, not an inconclusive
    failure.

    Production call site: ``snapshot.probe_filesystem_equivalence``.
    """

    probe_dir = tmp_path / "probes"
    probe_dir.mkdir()
    (probe_dir / "A").write_bytes(b"file\n")
    if (probe_dir / "a").exists():
        pytest.skip(
            "this filesystem conflates A/a, so a file and a link cannot "
            "share the spellings (named platform bound)"
        )
    (probe_dir / "other").write_bytes(b"other\n")
    (probe_dir / "a").symlink_to(probe_dir / "other")
    fd = _selection_fs._open_descriptor(
        probe_dir, _selection_fs._directory_flags(nofollow=False)
    )
    try:
        parent_identity = _selection_fs._safe_identity(fd)
        assert parent_identity is not None
        value = _selection_fs._stat_child(
            fd, "A", parent_path=probe_dir, follow_symlinks=False
        )
        parent = Directory(
            fd=fd,
            identity=parent_identity,
            name=probe_dir.name,
            display=probe_dir,
            parent=None,
        )
        probe = ConflationProbe(
            parent,
            "A",
            _selection_fs._identity_from_stat(value),
            frozenset({"case"}),
        )
        facts = snapshot_module.probe_filesystem_equivalence((probe,))
    finally:
        _selection_fs._close_quietly(fd)

    assert facts.case_known is True
    assert facts.case_conflates is False


# AC (d): revalidation after capture over the complete admitted path set.


def _mutation_fixture(root: Path) -> None:
    _write_tree(
        root,
        {
            "SKILL.md": b"# review\n",
            "victim.txt": b"v1\n",
            "run.sh": b"#!/bin/sh\n",
            "sub/nested.txt": b"n\n",
        },
    )
    (root / "run.sh").chmod(0o755)
    (root / "lonely").mkdir(exist_ok=True)


def _mutations() -> dict[str, Callable[[Path], str]]:
    def _overwrite(root: Path) -> str:
        (root / "victim.txt").write_bytes(b"v2\n")
        return "victim.txt"

    def _append(root: Path) -> str:
        with (root / "victim.txt").open("ab") as handle:
            handle.write(b"more\n")
        return "victim.txt"

    def _truncate(root: Path) -> str:
        (root / "victim.txt").write_bytes(b"")
        return "victim.txt"

    def _chmod_add_exec(root: Path) -> str:
        (root / "victim.txt").chmod(0o755)
        return "victim.txt"

    def _chmod_drop_exec(root: Path) -> str:
        (root / "run.sh").chmod(0o644)
        return "run.sh"

    def _replace_same_bytes(root: Path) -> str:
        (root / "victim.txt").unlink()
        (root / "victim.txt").write_bytes(b"v1\n")
        return "victim.txt"

    def _add_file(root: Path) -> str:
        (root / "added.txt").write_bytes(b"new\n")
        return "added.txt"

    def _remove_file(root: Path) -> str:
        (root / "victim.txt").unlink()
        return "victim.txt"

    def _add_dir(root: Path) -> str:
        (root / "newdir").mkdir()
        (root / "newdir" / "file.txt").write_bytes(b"n\n")
        return "newdir/file.txt"

    def _add_empty_dir(root: Path) -> str:
        (root / "emptydir").mkdir()
        return "emptydir"

    def _remove_dir(root: Path) -> str:
        shutil.rmtree(root / "sub")
        return "sub"

    def _remove_empty_dir(root: Path) -> str:
        (root / "lonely").rmdir()
        return "lonely"

    def _add_symlink(root: Path) -> str:
        (root / "planted").symlink_to(root / "victim.txt")
        return "planted"

    def _file_to_dir(root: Path) -> str:
        (root / "victim.txt").unlink()
        (root / "victim.txt").mkdir()
        return "victim.txt"

    def _dir_to_file(root: Path) -> str:
        shutil.rmtree(root / "sub")
        (root / "sub").write_bytes(b"now-a-file\n")
        return "sub"

    def _rename(root: Path) -> str:
        (root / "victim.txt").rename(root / "renamed.txt")
        return "renamed.txt"

    def _rename_empty_dir(root: Path) -> str:
        (root / "lonely").rename(root / "relocated")
        return "relocated"

    return {
        "content-overwrite": _overwrite,
        "content-append": _append,
        "content-truncate": _truncate,
        "chmod-add-exec": _chmod_add_exec,
        "chmod-drop-exec": _chmod_drop_exec,
        "replace-same-bytes-new-inode": _replace_same_bytes,
        "add-file": _add_file,
        "remove-file": _remove_file,
        "add-dir": _add_dir,
        "add-empty-dir": _add_empty_dir,
        "remove-dir": _remove_dir,
        "remove-empty-dir": _remove_empty_dir,
        "add-symlink": _add_symlink,
        "file-to-dir": _file_to_dir,
        "dir-to-file": _dir_to_file,
        "rename": _rename,
        "rename-empty-dir": _rename_empty_dir,
    }


def _mutation_identity_changed(
    captured: _selection_fs.CapturedTree, root: Path, shown: str
) -> bool:
    """Return whether one mutated file's ``(st_dev, st_ino)`` identity changed."""

    components = tuple(shown.split("/"))
    value = os.stat(root / shown)
    return (value.st_dev, value.st_ino) != captured.files[components].identity


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(
            kind,
            marks=pytest.mark.skipif(
                os.name == "nt" and kind in {"chmod-add-exec", "chmod-drop-exec"},
                reason="Windows snapshot inventory reports executable=false when the filesystem cannot report it",
            ),
        )
        for kind in sorted(_mutations())
    ],
)
def test_revalidation_refuses_every_mutation_class(
    tmp_path: Path, kind: str
) -> None:
    """Any post-capture change fails, naming its subject.

    Production call site: ``snapshot.revalidate_capture``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _mutation_fixture(root)

    with _open_session(root, home) as session:
        captured = _capture_production(session, session.root)
        shown = _mutations()[kind](root)
        if kind == "replace-same-bytes-new-inode" and not _mutation_identity_changed(
            captured, root, shown
        ):
            # The filesystem reused the inode number for the replacement
            # file, so the mutation this case names was not constructed:
            # identity, bytes and mode are all unchanged and no observable
            # difference exists for revalidation to catch. A passing
            # revalidation then proves the frozen bytes still describe the
            # live file exactly, which is the content property the digest
            # carries. Bound, probed at runtime rather than by platform
            # name: byte-identical same-mode replacement is
            # integrity-neutral and unreported on inode-reusing
            # filesystems; nothing content-bearing goes undetected.
            snapshot_module.revalidate_capture(
                session, session.root, captured, label="."
            )
            assert (root / shown).read_bytes() == captured.files[
                tuple(shown.split("/"))
            ].data
            return
        error = _raises_structured(
            CODE_SNAPSHOT_CHANGED,
            lambda: snapshot_module.revalidate_capture(
                session, session.root, captured, label="."
            ),
        )

    assert shown in error.detail


def test_revalidation_ignores_metadata_only_changes(tmp_path: Path) -> None:
    """Timestamp-only change is not a mutation: mtime is not identity.

    Production call site: ``snapshot.revalidate_capture``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _mutation_fixture(root)

    with _open_session(root, home) as session:
        captured = _capture_production(session, session.root)
        stamp = os.stat(root / "victim.txt")
        os.utime(
            root / "victim.txt",
            (stamp.st_atime + 100.0, stamp.st_mtime + 100.0),
        )
        snapshot_module.revalidate_capture(
            session, session.root, captured, label="."
        )


def test_composed_capture_revalidates_with_no_retry_loop(tmp_path: Path) -> None:
    """The composed operation walks twice exactly, then refuses once.

    Production call site: ``snapshot.capture_package``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _mutation_fixture(root)
    calls: list[str] = []
    real_capture_tree = SelectionSession.capture_tree

    def counting_capture_tree(
        self: SelectionSession, member: Directory, **kwargs: Any
    ) -> _selection_fs.CapturedTree:
        result = real_capture_tree(self, member, **kwargs)
        calls.append("capture")
        if len(calls) == 1:
            (root / "sabotage.txt").write_bytes(b"mid-operation\n")
        return result

    setattr(SelectionSession, "capture_tree", counting_capture_tree)
    try:
        with _open_session(root, home) as session:
            error = _raises_structured(
                CODE_SNAPSHOT_CHANGED,
                lambda: snapshot_module.capture_package(
                    session, session.root, label="."
                ),
            )
    finally:
        setattr(SelectionSession, "capture_tree", real_capture_tree)

    assert calls == ["capture", "capture"]
    assert "sabotage.txt" in error.detail


def test_mid_capture_mutation_is_detected_inside_one_operation(
    tmp_path: Path,
) -> None:
    """Bytes rewritten mid-read fail even though the identity is stable.

    Production call site: ``snapshot.capture_package``.
    """

    if os.name == "nt":
        pytest.skip("this in-read mutation injector intercepts POSIX os.read")

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n", "victim.txt": b"v1\n"})
    real_read = os.read
    rewritten: list[str] = []

    def rewriting_read(fd: int, size: int, *args: Any, **kwargs: Any) -> bytes:
        chunk = real_read(fd, size, *args, **kwargs)
        if chunk == b"v1\n" and not rewritten:
            rewritten.append("victim.txt")
            helper = os.open(root / "victim.txt", os.O_WRONLY | os.O_TRUNC)
            try:
                os.write(helper, b"v2\n")
            finally:
                os.close(helper)
        return chunk

    setattr(os, "read", rewriting_read)
    try:
        with _open_session(root, home) as session:
            error = _raises_structured(
                CODE_SNAPSHOT_CHANGED,
                lambda: snapshot_module.capture_package(
                    session, session.root, label="."
                ),
            )
    finally:
        setattr(os, "read", real_read)

    assert rewritten == ["victim.txt"]
    assert "victim.txt" in error.detail


# AC (e): revalidation before publication over the frozen copy.


def _honest_frozen() -> dict[str, FrozenFile]:
    return {
        "SKILL.md": FrozenFile("SKILL.md", b"name: review\n", False),
        "scripts/run.sh": FrozenFile("scripts/run.sh", b"echo one\n", True),
    }


def _honest_digest() -> str:
    return local_snapshot.build_inventory(
        [
            ("SKILL.md", _sha256(b"name: review\n"), False),
            ("scripts/run.sh", _sha256(b"echo one\n"), True),
        ],
        equivalent=lambda _left, _right: False,
    )["snapshot"]


def _frozen_tampers() -> dict[str, Callable[[dict[str, FrozenFile]], None]]:
    def _flip_byte(frozen: dict[str, FrozenFile]) -> None:
        frozen["SKILL.md"] = FrozenFile("SKILL.md", b"name: review!\n", False)

    def _truncate(frozen: dict[str, FrozenFile]) -> None:
        frozen["scripts/run.sh"] = FrozenFile("scripts/run.sh", b"", True)

    def _flip_executable(frozen: dict[str, FrozenFile]) -> None:
        frozen["scripts/run.sh"] = FrozenFile(
            "scripts/run.sh", b"echo one\n", False
        )

    def _flip_executable_other(frozen: dict[str, FrozenFile]) -> None:
        frozen["SKILL.md"] = FrozenFile("SKILL.md", b"name: review\n", True)

    def _add_entry(frozen: dict[str, FrozenFile]) -> None:
        frozen["extra.txt"] = FrozenFile("extra.txt", b"x\n", False)

    def _remove_entry(frozen: dict[str, FrozenFile]) -> None:
        del frozen["SKILL.md"]

    def _rename_path(frozen: dict[str, FrozenFile]) -> None:
        item = frozen.pop("SKILL.md")
        frozen["RENAMED.md"] = FrozenFile("RENAMED.md", item.data, item.executable)

    def _swap_bytes(frozen: dict[str, FrozenFile]) -> None:
        first = frozen["SKILL.md"]
        second = frozen["scripts/run.sh"]
        frozen["SKILL.md"] = FrozenFile(first.path, second.data, first.executable)
        frozen["scripts/run.sh"] = FrozenFile(second.path, first.data, second.executable)

    return {
        "flip-byte": _flip_byte,
        "truncate-bytes": _truncate,
        "flip-executable": _flip_executable,
        "flip-executable-other": _flip_executable_other,
        "add-entry": _add_entry,
        "remove-entry": _remove_entry,
        "rename-path": _rename_path,
        "swap-bytes": _swap_bytes,
    }


@pytest.mark.posix_traversal_independent
@pytest.mark.parametrize("kind", sorted(_frozen_tampers()))
def test_verify_refuses_every_frozen_mutation_class(kind: str) -> None:
    """Any frozen-copy change fails against the audited digest.

    Production call site: ``snapshot.verify_frozen_copy``.
    """

    frozen = _honest_frozen()
    expected = _honest_digest()
    _frozen_tampers()[kind](frozen)

    error = _raises_structured(
        CODE_SNAPSHOT_CHANGED,
        lambda: snapshot_module.verify_frozen_copy(
            frozen, expected, equivalent=lambda _left, _right: False
        ),
    )

    assert expected in error.detail


@pytest.mark.posix_traversal_independent
def test_verify_accepts_the_untampered_frozen_copy() -> None:
    """The honest frozen copy verifies and returns its digest.

    Production call site: ``snapshot.verify_frozen_copy``.
    """

    expected = _honest_digest()

    assert (
        snapshot_module.verify_frozen_copy(
            _honest_frozen(), expected, equivalent=lambda _left, _right: False
        )
        == expected
    )


def test_verify_through_capture_round_trip(tmp_path: Path) -> None:
    """Capture output verifies; one tampered byte fails.

    Production call sites: ``snapshot.capture_package_snapshot`` and
    ``snapshot.verify_frozen_copy``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n", "data.bin": b"\x00\x01\x02"})

    package = snapshot_module.capture_package_snapshot(root, ".", home=home)
    frozen = package.frozen_files()

    assert (
        snapshot_module.verify_frozen_copy(
            frozen,
            package.inventory["snapshot"],
            equivalent=package.equivalence.equivalent,
        )
        == package.inventory["snapshot"]
    )

    frozen["data.bin"] = FrozenFile("data.bin", b"\x00\x01\x03", False)
    _raises_structured(
        CODE_SNAPSHOT_CHANGED,
        lambda: snapshot_module.verify_frozen_copy(
            frozen,
            package.inventory["snapshot"],
            equivalent=package.equivalence.equivalent,
        ),
    )


# The three revalidations, kept distinct.


@pytest.mark.posix_traversal_independent
def test_three_revalidations_are_distinct_and_owned() -> None:
    """Revalidations 1 and 2 are distinct functions; 3 is named, not claimed.

    ``revalidate_capture`` runs inside capture; ``verify_frozen_copy``
    never does (it is the later publication step). The third
    revalidation, the publication-write boundary recheck, is
    TASK-260916-100uew's ``boundaries.recheck_publication_destination``,
    called by TASK-260916-17x3o1 — this module references neither.
    """

    from csk.sources import boundaries

    assert snapshot_module.revalidate_capture is not snapshot_module.verify_frozen_copy
    assert callable(boundaries.recheck_publication_destination)

    import inspect

    capture_source = inspect.getsource(snapshot_module.capture_package)
    assert "revalidate_capture" in capture_source
    assert "verify_frozen_copy" not in capture_source

    module_source = Path(snapshot_module.__file__).read_text(encoding="utf-8")
    # The owner is NAMED in prose (required) but never referenced in code.
    assert "TASK-260916-100uew" in module_source
    assert "TASK-260916-17x3o1" in module_source
    tree = ast.parse(module_source, filename=str(snapshot_module.__file__))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "boundaries" not in node.module.split("."), node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "boundaries" not in alias.name.split("."), alias.name
        elif isinstance(node, ast.Name):
            assert node.id != "recheck_publication_destination"
        elif isinstance(node, ast.Attribute):
            assert node.attr != "recheck_publication_destination"


def test_revalidate_and_verify_fail_independently(tmp_path: Path) -> None:
    """Each revalidation fails on its own input, not the other's.

    A live-tree mutation fails ``revalidate_capture`` while the frozen
    copy still verifies, and a frozen tamper fails ``verify_frozen_copy``
    while the live tree still revalidates.

    Production call sites: ``snapshot.revalidate_capture`` and
    ``snapshot.verify_frozen_copy``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n", "victim.txt": b"v1\n"})

    with _open_session(root, home) as session:
        package = snapshot_module.capture_package(session, session.root, label=".")
        frozen = package.frozen_files()
        assert (
            snapshot_module.verify_frozen_copy(
                frozen,
                package.inventory["snapshot"],
                equivalent=package.equivalence.equivalent,
            )
            == package.inventory["snapshot"]
        )

        (root / "victim.txt").write_bytes(b"v2\n")
        _raises_structured(
            CODE_SNAPSHOT_CHANGED,
            lambda: snapshot_module.revalidate_capture(
                session, session.root, package.tree, label="."
            ),
        )
        # The frozen copy is untouched by the live mutation.
        assert (
            snapshot_module.verify_frozen_copy(
                frozen,
                package.inventory["snapshot"],
                equivalent=package.equivalence.equivalent,
            )
            == package.inventory["snapshot"]
        )

        (root / "victim.txt").write_bytes(b"v1\n")
        snapshot_module.revalidate_capture(
            session, session.root, package.tree, label="."
        )
        frozen["victim.txt"] = FrozenFile("victim.txt", b"v3\n", False)
        _raises_structured(
            CODE_SNAPSHOT_CHANGED,
            lambda: snapshot_module.verify_frozen_copy(
                frozen,
                package.inventory["snapshot"],
                equivalent=package.equivalence.equivalent,
            ),
        )


# S-FS instrument: audit-hook confinement and read-only capture.


def _audit_window() -> int:
    return len(_AUDIT_EVENTS)


def _window_events(start: int) -> list[tuple[str, tuple[Any, ...]]]:
    return _AUDIT_EVENTS[start:]


@pytest.mark.skipif(
    os.name == "nt",
    reason="Python audit hooks do not expose the Windows NT handle calls; trace-based provenance runs separately",
)
def test_capture_confines_all_filesystem_events_to_the_root(
    tmp_path: Path,
) -> None:
    """Every capture event names the root, a bare name, or a descriptor.

    Absolute audit paths resolve inside the source root; bare names are
    descriptor-relative opens that carry no location claim; integers are
    already-open descriptors. The oracle trusts only the audit tuple.

    Production call site: ``snapshot.capture_package``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(
        root,
        {
            "SKILL.md": b"# review\n",
            "sub/nested.txt": b"n\n",
            "MiXeD": b"cased\n",
        },
    )
    real_root = os.path.realpath(root)

    with _open_session(root, home) as session:
        start = _audit_window()
        snapshot_module.capture_package(session, session.root, label=".")
        events = _window_events(start)

    assert events, "the capture emitted no audited filesystem events"
    for event, args in events:
        assert args, f"audited {event} carries no path"
        path = args[0]
        if isinstance(path, int):
            continue
        text = os.fsdecode(path)
        if not os.path.isabs(text):
            assert os.sep not in text, f"relative audit path escapes: {text!r}"
            continue
        resolved = os.path.realpath(text)
        assert resolved == real_root or resolved.startswith(real_root + os.sep), (
            f"audited {event} escapes the source root: {text!r}"
        )


def _open_flags_index(calibration: Path) -> int:
    """Locate the flags element of this runtime's ``open`` audit tuple.

    The position is calibrated, not assumed: a read-only scratch open
    identifies the element whose access mode is ``O_RDONLY``.
    """

    probe = calibration / "audit-calibration.txt"
    probe.write_bytes(b"calibrate\n")
    start = _audit_window()
    fd = os.open(probe, os.O_RDONLY)
    os.close(fd)
    events = [args for event, args in _window_events(start) if event == "open"]
    assert events, "the calibration open emitted no audit event"
    for args in events:
        for index, element in enumerate(args):
            if (
                isinstance(element, int)
                and element & os.O_ACCMODE == os.O_RDONLY
                and not element & (os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            ):
                return index
    raise AssertionError(f"cannot locate open flags in audit shape: {events!r}")


@pytest.mark.skipif(
    os.name == "nt",
    reason="this read-only audit oracle requires POSIX open flags and os.O_ACCMODE",
)
def test_capture_performs_no_write_filesystem_event(tmp_path: Path) -> None:
    """Capture opens nothing writable: the S-TXN read-only property.

    Production call site: ``snapshot.capture_package``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n", "sub/nested.txt": b"n\n"})
    flags_index = _open_flags_index(tmp_path)

    with _open_session(root, home) as session:
        start = _audit_window()
        snapshot_module.capture_package(session, session.root, label=".")
        events = _window_events(start)

    opens = [args for event, args in events if event == "open"]
    assert opens, "the capture emitted no audited open events"
    for args in opens:
        assert len(args) > flags_index, f"unexpected open audit shape: {args!r}"
        flags = args[flags_index]
        assert isinstance(flags, int)
        assert flags & os.O_ACCMODE == os.O_RDONLY, f"writable open flags: {flags:#x}"
        assert not flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND), (
            f"mutating open flags: {flags:#x}"
        )


# S-TXN instrument: a faulted capture leaves the tree byte-identical.


def test_faulted_capture_leaves_the_tree_byte_identical(tmp_path: Path) -> None:
    """A fault mid-capture publishes nothing: before/after hashes agree.

    Capture has no store and no publication step, so the transaction
    property is that the source tree itself is untouched by a failed
    capture — asserted with tree hashes, as the catalog requires.

    Production call site: ``snapshot.capture_package``.
    """

    if os.name == "nt":
        pytest.skip("this fault fixture injects at POSIX os.read; Windows uses ReadFile")

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(
        root,
        {
            "SKILL.md": b"# review\n",
            "victim.txt": b"v1\n",
            "sub/nested.txt": b"n\n",
        },
    )
    before = _tree_hash(root)

    with _open_session(root, home) as session:
        snapshot_module.capture_package(session, session.root, label=".")
        with _faulty_os(
            "read", lambda _fd: True, PermissionError(errno.EACCES, "injected")
        ) as fired:
            _raises_structured(
                CODE_MEMBER_INVALID,
                lambda: snapshot_module.capture_package(
                    session, session.root, label="."
                ),
            )

    assert fired, "the injected read fault was never reached"
    assert _tree_hash(root) == before


# S-ERRORS instrument: fault injection at the real os call sites.


_FAULTS: tuple[tuple[str, Exception], ...] = (
    ("permission", PermissionError(errno.EACCES, "injected")),
    ("enoent", FileNotFoundError(errno.ENOENT, "injected")),
    ("enoent-bare", FileNotFoundError("injected")),
    ("enotdir", NotADirectoryError(errno.ENOTDIR, "injected")),
    ("eloop", OSError(errno.ELOOP, "injected")),
    ("eisdir", IsADirectoryError(errno.EISDIR, "injected")),
    ("runtime", RuntimeError("injected")),
    ("value", ValueError("injected")),
    (
        "unicode",
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "injected"),
    ),
)


def _expected_fault_code(site: str, fault_id: str) -> str:
    # Absence mid-walk is a concurrent mutation; every other failure at
    # the capture seam is a structured inspection failure. A bare
    # FileNotFoundError carries errno None, so it cannot prove absence
    # and reports the inspection code.
    if site in ("open", "scandir") and fault_id in ("enoent", "enotdir"):
        return CODE_SNAPSHOT_CHANGED
    return CODE_MEMBER_INVALID


@pytest.mark.parametrize("site", ["open", "read", "fstat", "scandir"])
@pytest.mark.parametrize("fault_id", [fault[0] for fault in _FAULTS])
@pytest.mark.skipif(
    os.name == "nt",
    reason="this matrix patches POSIX os.* call sites; Windows NT backend behavior has handle-specific tests",
)
def test_injected_filesystem_fault_is_structured(
    tmp_path: Path, site: str, fault_id: str
) -> None:
    """Every injected fault class becomes a structured refusal naming codes.

    The same fixture captures unfaulted first (positive control) and the
    injection asserts it was actually reached. DirEntry.stat faults are
    not injectable (C-level, unpatchable); listing lies are covered
    deterministically by the spoofed-listing tests instead.

    Production call site: ``snapshot.capture_package``.
    """

    fault = dict(_FAULTS)[fault_id]
    expected = _expected_fault_code(site, fault_id)
    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n", "victim.txt": b"v1\n"})
    if site == "open":
        predicate: Callable[[Any], bool] = lambda value: value == "victim.txt"  # noqa: E731
    else:
        predicate = lambda _value: True  # noqa: E731

    with _open_session(root, home) as session:
        snapshot_module.capture_package(session, session.root, label=".")
        with _faulty_os(site, predicate, fault) as fired:
            error = _raises_structured(
                expected,
                lambda: snapshot_module.capture_package(
                    session, session.root, label="."
                ),
            )

    assert fired, f"fault at os.{site} was never reached for {fault_id}"
    assert error.code == expected


@pytest.mark.skipif(
    os.name == "nt",
    reason="this fault fixture injects at POSIX os.scandir; Windows enumeration uses NtQueryDirectoryFile",
)
def test_direct_capture_tree_call_structures_non_os_faults(tmp_path: Path) -> None:
    """The walk seam structures faults for callers without the wrapper.

    ``capture_package`` wraps the walk in its own boundary, so the
    walk-level handler is defense-in-depth there — but direct
    ``capture_tree`` callers (like the conformance drivers) rely on it
    alone. A non-``OSError`` fault at ``scandir`` must still refuse
    structured.

    Production call site: ``SelectionSession.capture_tree``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n", "victim.txt": b"v1\n"})

    with _open_session(root, home) as session:
        _capture_production(session, session.root)
        with _faulty_os("scandir", lambda _value: True, RuntimeError("injected")) as fired:
            error = _raises_structured(
                CODE_MEMBER_INVALID,
                lambda: _capture_production(session, session.root),
            )

    assert fired, "the injected scandir fault was never reached"
    assert error.code == CODE_MEMBER_INVALID


# S-IDENTITY instrument: identity is a function of the raw bytes.


def _generated_contents() -> list[tuple[str, bytes]]:
    import random

    rng = random.Random(260916)
    cases: list[tuple[str, bytes]] = [
        ("empty", b""),
        ("lf", b"a\nb\n"),
        ("crlf", b"a\r\nb\r\n"),
        ("lone-cr", b"a\rb\r"),
        ("mixed-endings", b"a\r\nb\nc\rd\n"),
        ("no-trailing-lf", b"no-newline"),
        ("trailing-nul", b"data\x00"),
        ("inner-nul", b"da\x00ta\n"),
        ("invalid-utf8", b"\xff\xfe\x00bad"),
        ("bom", b"\xef\xbb\xbfname\n"),
        ("nfc", "caf\u00e9\n".encode("utf-8")),
        ("nfd", "cafe\u0301\n".encode("utf-8")),
        ("cjk", "日本語スキル\n".encode("utf-8")),
        ("emoji", "🛠 review\n".encode("utf-8")),
        ("long-line", b"x" * 100000 + b"\n"),
    ]
    alphabet = "aZ0 \t\r\n\x00ÿé".encode("utf-8") + b"\xff\xfe"
    for index in range(15):
        width = rng.randint(0, 4000)
        cases.append((f"random-{index:02d}", bytes(rng.choice(alphabet) for _ in range(width))))
    return cases


def test_identity_is_raw_bytes_over_generated_content(tmp_path: Path) -> None:
    """``identity(bytes) == hash(bytes)`` over hostile generated content.

    Production call site: ``snapshot.captured_to_inventory`` (via
    ``snapshot.capture_package``).
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    contents = _generated_contents()
    _write_tree(root, {f"{name}.dat": raw for name, raw in contents})

    package = snapshot_module.capture_package_snapshot(root, ".", home=home)

    frozen = package.frozen_files()
    assert len(frozen) == len(contents)
    by_path = {entry["path"]: entry for entry in package.inventory["files"]}
    for name, raw in contents:
        path = f"{name}.dat"
        assert frozen[path].data == raw
        assert by_path[path]["sha256"] == _sha256(raw)


def test_line_endings_are_never_normalized_before_hashing(tmp_path: Path) -> None:
    """CRLF and LF twins hash differently; frozen bytes keep every byte.

    Production call site: ``snapshot.capture_package``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"crlf.txt": b"a\r\nb\r\n", "lf.txt": b"a\nb\n"})

    package = snapshot_module.capture_package_snapshot(root, ".", home=home)

    frozen = package.frozen_files()
    assert frozen["crlf.txt"].data == b"a\r\nb\r\n"
    assert frozen["lf.txt"].data == b"a\nb\n"
    by_path = {entry["path"]: entry for entry in package.inventory["files"]}
    assert by_path["crlf.txt"]["sha256"] != by_path["lf.txt"]["sha256"]


def test_nfc_and_nfd_entry_spellings_stay_distinct(tmp_path: Path) -> None:
    """NFC/NFD twins are distinct entries where the host keeps both.

    Production call site: ``snapshot.capture_package``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    nfc = "caf\u00e9.txt"
    nfd = "cafe\u0301.txt"
    (root / nfc).parent.mkdir(parents=True, exist_ok=True)
    (root / nfc).write_bytes(b"nfc\n")
    try:
        probe = os.open(
            root / nfd,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
        )
    except FileExistsError:
        pytest.skip(
            "this filesystem conflates NFC/NFD spellings "
            "(named platform bound; the twins cannot coexist here)"
        )
    try:
        os.write(probe, b"nfd\n")
    finally:
        os.close(probe)

    package = snapshot_module.capture_package_snapshot(root, ".", home=home)

    frozen = package.frozen_files()
    assert set(frozen) == {nfc, nfd}
    assert frozen[nfc].data == b"nfc\n"
    assert frozen[nfd].data == b"nfd\n"


# Wrapper, edges and lifecycle.


@pytest.mark.posix_traversal_independent
@pytest.mark.parametrize(
    ("directory", "shown"),
    [
        ("", None),
        ("..", ".."),
        ("/abs", "/abs"),
        ("a/../b", "a/../b"),
        ("*", "*"),
        ("a?b", "a?b"),
        # ``repr`` escapes the backslash, so the raw spelling cannot be a
        # substring of the detail; the refusal reason is the assertion.
        ("a\\b", None),
        (".hidden/..", ".hidden/.."),
    ],
    ids=["empty", "dotdot", "absolute", "escape", "star", "question", "backslash", "inner-dotdot"],
)
def test_wrapper_rejects_non_selector_directories(
    tmp_path: Path, directory: str, shown: str | None
) -> None:
    """Non-selector directories refuse before any session opens.

    Production call site: ``snapshot.capture_package_snapshot``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    root.mkdir()

    error = _raises_structured(
        CODE_SELECTION_INVALID,
        lambda: snapshot_module.capture_package_snapshot(root, directory, home=home),
    )

    assert "portable contained path" in error.detail
    if shown is not None:
        assert shown in error.detail


def test_wrapper_reports_missing_and_escaping_directories(tmp_path: Path) -> None:
    """Missing and escaping selector directories refuse as selection errors.

    Production call site: ``snapshot.capture_package_snapshot``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"pkg/SKILL.md": b"# review\n"})
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)

    missing = _raises_structured(
        CODE_SELECTION_INVALID,
        lambda: snapshot_module.capture_package_snapshot(root, "nope", home=home),
    )
    assert "nope" in missing.detail

    escaping = _raises_structured(
        CODE_SELECTION_INVALID,
        lambda: snapshot_module.capture_package_snapshot(
            root, "escape/src", home=home
        ),
    )
    assert "escape" in escaping.detail


def test_wrapper_refuses_managed_and_home_roots(tmp_path: Path) -> None:
    """Capture roots inside managed output or the csk home refuse.

    Production call site: ``snapshot.capture_package_snapshot``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(
        root,
        {
            ".agents/skills/review/SKILL.md": b"# managed\n",
            "pkg/SKILL.md": b"# review\n",
        },
    )

    managed = _raises_structured(
        CODE_OUTPUT_OVERLAP,
        lambda: snapshot_module.capture_package_snapshot(root, ".agents", home=home),
    )
    assert ".agents" in managed.detail

    inside_home = home / "project"
    _write_tree(inside_home, {"SKILL.md": b"# review\n"})
    homed = _raises_structured(
        CODE_OUTPUT_OVERLAP,
        lambda: snapshot_module.capture_package_snapshot(
            inside_home, ".", home=home
        ),
    )
    assert "csk home" in homed.detail


def test_wrapper_captures_a_subdirectory_with_relative_paths(tmp_path: Path) -> None:
    """A subdirectory capture renders paths relative to the capture root.

    Production call site: ``snapshot.capture_package_snapshot``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(
        root,
        {
            "pkg/SKILL.md": b"# review\n",
            "pkg/sub/nested.txt": b"n\n",
            "top.txt": b"top\n",
        },
    )

    package = snapshot_module.capture_package_snapshot(root, "pkg", home=home)

    assert set(package.frozen_files()) == {"SKILL.md", "sub/nested.txt"}


def test_managed_subtrees_prune_silently_inside_a_capture(tmp_path: Path) -> None:
    """Managed-output subtrees vanish from the capture without a refusal.

    Production call site: ``snapshot.capture_package_snapshot``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(
        root,
        {
            "pkg/SKILL.md": b"# review\n",
            "pkg/.agents/out/cache.bin": b"cache\n",
            "pkg/.git/objects/data": b"meta\n",
        },
    )

    package = snapshot_module.capture_package_snapshot(root, "pkg", home=home)

    assert set(package.frozen_files()) == {"SKILL.md"}


def test_empty_package_captures_to_an_empty_inventory(tmp_path: Path) -> None:
    """An empty directory captures to a valid empty inventory.

    Production call site: ``snapshot.capture_package_snapshot``.
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    (root / "empty").mkdir(parents=True)

    package = snapshot_module.capture_package_snapshot(root, "empty", home=home)

    assert package.inventory["files"] == []
    assert local_snapshot.inventory_digest(package.inventory) == package.inventory["snapshot"]
    assert (
        snapshot_module.verify_frozen_copy(
            package.frozen_files(),
            package.inventory["snapshot"],
            equivalent=package.equivalence.equivalent,
        )
        == package.inventory["snapshot"]
    )


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        # ``repr`` escapes backslashes and control characters, so those
        # spellings cannot be substrings of the detail; the refusal
        # reason carries those assertions instead.
        pytest.param(
            "a\\b",
            None,
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="backslash is a Win32 path separator and cannot be materialized as one filename",
            ),
        ),
        pytest.param(
            "nul",
            "nul",
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="NUL is a reserved Win32 device name, not an ordinary file that can reach inventory",
            ),
        ),
        pytest.param(
            "trailing. ",
            "trailing. ",
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="Win32 path APIs trim trailing dots and spaces before inventory sees the spelling",
            ),
        ),
        ("a:b", "a:b"),
        ("a\x01b", None),
    ],
    ids=["backslash", "reserved", "trailing-dot-space", "colon", "control"],
)
def test_non_portable_names_refuse_as_inventory_invalid(
    tmp_path: Path, name: str, shown: str | None
) -> None:
    """Capture reads the bytes; the inventory seam refuses the spelling.

    Capture never pre-filters by portability: the file is a regular
    admitted file, and the structured refusal comes from the production
    inventory function naming the path.

    Production call sites: ``snapshot.capture_package`` (reads) and
    ``local_snapshot.build_inventory`` (refuses).
    """

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    try:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"x\n")
    except OSError:
        pytest.skip(f"this host cannot materialize {name!r} (named platform bound)")

    with _open_session(root, home) as session:
        error = _raises_structured(
            CODE_INVENTORY_INVALID,
            lambda: snapshot_module.capture_package(session, session.root, label="."),
        )

    assert "non-portable path" in error.detail
    if shown is not None:
        assert shown in error.detail


def test_session_close_is_idempotent_and_post_close_capture_is_structured(
    tmp_path: Path,
) -> None:
    """Closing twice is safe; capture after close refuses structured."""

    root = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    _write_tree(root, {"SKILL.md": b"# review\n"})

    session = _open_session(root, home)
    snapshot_module.capture_package(session, session.root, label=".")
    session.close()
    session.close()

    with pytest.raises(SourceError):
        snapshot_module.capture_package(session, session.root, label=".")
