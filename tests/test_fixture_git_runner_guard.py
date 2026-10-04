"""Every test git invocation routes through the one shared runner.

Static guard for BUG-261004-473myt revision 3: the recording reviews found
fixture helpers that bypassed the shared isolation (``tests/test_git_admission.py``,
``tests/test_sources_transport.py``, ``tests/test_git_admission_ssh.py``) and,
in revision 2, a documented module import alias the scanner did not resolve
(``import subprocess as sp``). The fix is one shared fixture git runner --
``git_fixture_isolation.run_fixture_git`` -- and this scan fails if any test
file invokes git without it.

The scanner resolves module import aliases (``import subprocess as sp``,
``import os as o``) at any nesting level, ``from`` imports (``from
subprocess import run``, ``from os import system``), and ``run`` aliases
(``real = subprocess.run``), over the full child-spawning surface:
``subprocess.run``/``Popen``/``call``/``check_output``/``check_call``/
``getoutput``/``getstatusoutput``, ``os.system``/``popen``,
``os.exec*``, ``os.spawn*``, and ``pty.spawn``. Anything it cannot
resolve -- an opaque ``*args`` passthrough, a fully variable argv, an
``asyncio`` subprocess -- is a stated bound, not a proof; the snippet tests
below pin both the catches and the intentional passes.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent
RUNNER_MODULE = "git_fixture_isolation.py"
_SUBPROCESS_FUNCS = frozenset(
    {
        "run",
        "Popen",
        "call",
        "check_output",
        "check_call",
        "getoutput",
        "getstatusoutput",
    }
)
_OS_SYSTEM_FUNCS = frozenset({"system", "popen"})
_OS_EXEC_FUNCS = frozenset(
    {
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execvp",
        "execvpe",
    }
)
_OS_SPAWN_FUNCS = frozenset(
    {
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
    }
)
_PTY_FUNCS = frozenset({"spawn"})
_SHELL_GIT = re.compile(r"(?:^|[\s;|&])git(?:\s|$)")


@dataclass(frozen=True)
class Bypass:
    path: Path
    line: int
    function: str
    snippet: str


@dataclass(frozen=True)
class _Scope:
    """Resolved import surface of one scanned file (over-approximated).

    Module aliases and ``from`` imports are collected from every nesting
    level into one file-wide set: a name imported inside one function flags
    the same spelling elsewhere. That over-approximation fails closed -- a
    same-named non-git call stays clean unless its argv is git-shaped, in
    which case it deserves the flag anyway.
    """

    subprocess_modules: frozenset[str]
    os_modules: frozenset[str]
    pty_modules: frozenset[str]
    subprocess_froms: frozenset[str]
    os_froms: frozenset[str]
    pty_froms: frozenset[str]


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _receiver_last(func: ast.AST) -> str | None:
    if isinstance(func, ast.Attribute):
        return _dotted_name(func.value).split(".")[-1]
    return None


def _is_subprocess_call(
    func: ast.AST, scope: _Scope, run_aliases: set[str]
) -> bool:
    if isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_FUNCS:
        last = _receiver_last(func)
        # The literal module name always matches (even without an import --
        # flag dead code rather than miss), and every resolved alias adds a
        # spelling: ``import subprocess as sp`` makes ``sp.run`` a call.
        return last == "subprocess" or (
            last is not None and last in scope.subprocess_modules
        )
    if isinstance(func, ast.Name):
        return func.id in scope.subprocess_froms or func.id in run_aliases
    return False


def _is_system_call(func: ast.AST, scope: _Scope) -> bool:
    """Whether ``func`` is an ``os.system``/``os.popen`` (or alias) call."""
    if isinstance(func, ast.Attribute) and func.attr in _OS_SYSTEM_FUNCS:
        last = _receiver_last(func)
        return last == "os" or (last is not None and last in scope.os_modules)
    if isinstance(func, ast.Name):
        return func.id in scope.os_froms
    return False


def _is_exec_call(func: ast.AST, scope: _Scope) -> bool:
    """Whether ``func`` is an ``os.exec*``/``os.spawn*`` (or alias) call."""
    if isinstance(func, ast.Attribute) and (
        func.attr in _OS_EXEC_FUNCS or func.attr in _OS_SPAWN_FUNCS
    ):
        last = _receiver_last(func)
        return last == "os" or (last is not None and last in scope.os_modules)
    if isinstance(func, ast.Name):
        return func.id in scope.os_froms
    return False


def _is_pty_call(func: ast.AST, scope: _Scope) -> bool:
    """Whether ``func`` is a ``pty.spawn`` (or alias) call."""
    if isinstance(func, ast.Attribute) and func.attr in _PTY_FUNCS:
        last = _receiver_last(func)
        return last == "pty" or (last is not None and last in scope.pty_modules)
    if isinstance(func, ast.Name):
        return func.id in scope.pty_froms
    return False


def _mentions_git_executable(node: ast.AST, known_aliases: set[str]) -> bool:
    """Whether an assigned value evidently resolves to a git executable."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = (
                child.func.id
                if isinstance(child.func, ast.Name)
                else child.func.attr
                if isinstance(child.func, ast.Attribute)
                else ""
            )
            if "git" in name.lower():
                return True
            if name == "which" and any(
                isinstance(arg, ast.Constant) and arg.value == "git"
                for arg in child.args
            ):
                return True
        if isinstance(child, ast.Name) and child.id in known_aliases:
            return True
    return False


def _collect_aliases(tree: ast.Module) -> tuple[_Scope, set[str], dict[str, set[str]]]:
    """Collect module aliases, from-imports, run aliases, and git aliases.

    Returns ``(scope, run_aliases, git_aliases)`` where ``git_aliases`` maps
    a scope key (``""`` for module level, else the qualified function name)
    to the git-executable variable names assigned there.
    """
    subprocess_modules: set[str] = set()
    os_modules: set[str] = set()
    pty_modules: set[str] = set()
    subprocess_froms: set[str] = set()
    os_froms: set[str] = set()
    pty_froms: set[str] = set()
    run_aliases: set[str] = set()
    git_aliases: dict[str, set[str]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                top = alias.name.split(".")[0]
                if top == "subprocess":
                    subprocess_modules.add(bound)
                elif top == "os":
                    os_modules.add(bound)
                elif top == "pty":
                    pty_modules.add(bound)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                for alias in node.names:
                    if alias.name in _SUBPROCESS_FUNCS:
                        subprocess_froms.add(alias.asname or alias.name)
            elif node.module == "os":
                for alias in node.names:
                    if (
                        alias.name in _OS_SYSTEM_FUNCS
                        or alias.name in _OS_EXEC_FUNCS
                        or alias.name in _OS_SPAWN_FUNCS
                    ):
                        os_froms.add(alias.asname or alias.name)
            elif node.module == "pty":
                for alias in node.names:
                    if alias.name in _PTY_FUNCS:
                        pty_froms.add(alias.asname or alias.name)

    def scope_aliases(scope: str) -> set[str]:
        return git_aliases.setdefault(scope, set())

    def is_run_alias_value(value: ast.AST) -> bool:
        if isinstance(value, ast.Attribute) and value.attr in _SUBPROCESS_FUNCS:
            last = _dotted_name(value.value).split(".")[-1]
            return last == "subprocess" or last in subprocess_modules
        if isinstance(value, ast.Name):
            return value.id in run_aliases
        return False

    def visit_assign(target: ast.AST, value: ast.AST, scope: str) -> None:
        if not isinstance(target, ast.Name):
            return
        if is_run_alias_value(value):
            run_aliases.add(target.id)
            return
        # Fixpoint over alias chains (a = which("git"); b = Path(a).resolve()).
        for _ in range(3):
            if _mentions_git_executable(value, scope_aliases(scope) | scope_aliases("")):
                scope_aliases(scope).add(target.id)
                return

    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            for target in targets:
                visit_assign(target, value, "")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _visit_function(node, node.name, git_aliases)
    # Second pass: module-level aliases assigned after their use, and
    # run-aliases referenced before assignment, still resolve.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None or not isinstance(value, ast.Attribute):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and is_run_alias_value(value):
                    run_aliases.add(target.id)
    scope = _Scope(
        subprocess_modules=frozenset(subprocess_modules),
        os_modules=frozenset(os_modules),
        pty_modules=frozenset(pty_modules),
        subprocess_froms=frozenset(subprocess_froms),
        os_froms=frozenset(os_froms),
        pty_froms=frozenset(pty_froms),
    )
    return scope, run_aliases, git_aliases


def _visit_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    scope: str,
    git_aliases: dict[str, set[str]],
) -> None:
    for child in ast.walk(node):
        if isinstance(child, (ast.Assign, ast.AnnAssign)):
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            value = child.value
            if value is None:
                continue
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                # In-function ``run`` aliases resolve in the second full-tree
                # pass of _collect_aliases; only git-executable aliases are
                # scoped here.
                if isinstance(value, ast.Attribute) and value.attr in _SUBPROCESS_FUNCS:
                    continue
                for _ in range(3):
                    known = git_aliases.setdefault(scope, set()) | git_aliases.setdefault(
                        "", set()
                    )
                    if _mentions_git_executable(value, known):
                        git_aliases.setdefault(scope, set()).add(target.id)
                        break


def _first_argv_element(call: ast.Call) -> ast.AST | None:
    if call.args:
        first = call.args[0]
    else:
        first = None
        for keyword in call.keywords:
            if keyword.arg in ("args", "argv"):
                first = keyword.value
                break
        if first is None:
            return None
    if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
        return first.elts[0]
    return first


def _call_invokes_git(
    call: ast.Call, git_names: set[str]
) -> bool:
    # F1: a literal git executable or a shell string invoking git.
    for child in ast.walk(call):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if child.value in ("git", "git.exe"):
                return True
    first = _first_argv_element(call)
    if (
        isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and _SHELL_GIT.search(first.value)
    ):
        return True
    # F2: a git-named resolver call (or which("git")) anywhere in the call.
    for child in ast.walk(call):
        if child is call or not isinstance(child, ast.Call):
            continue
        func = child.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        if "git" in name.lower():
            return True
        if name == "which" and any(
            isinstance(arg, ast.Constant) and arg.value == "git" for arg in child.args
        ):
            return True
    # F3: the argv head is a git variable, a tracked executable alias, or a
    # call wrapping one (os.fspath(exe), Path(exe) -- the version-probe shape).
    if isinstance(first, ast.Name):
        lowered = first.id.lower()
        if (
            first.id in git_names
            or first.id == "git"
            or lowered.startswith("git_")
            or lowered.endswith("_git")
        ):
            return True
    if isinstance(first, ast.Call):
        for child in ast.walk(first):
            if isinstance(child, ast.Name) and child.id in git_names:
                return True
    return False


def _system_call_invokes_git(call: ast.Call) -> bool:
    """Whether an ``os.system``/``os.popen`` command string invokes git."""
    for child in ast.walk(call):
        if (
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and _SHELL_GIT.search(child.value)
        ):
            return True
    return False


def _exec_call_invokes_git(call: ast.Call, git_names: set[str]) -> bool:
    """Whether an ``os.exec*``/``os.spawn*``/``pty.spawn`` argv invokes git.

    The git executable may be the path argument, any argv element, or a
    tracked executable alias; ``spawn`` mode flags (``os.P_WAIT``) are just
    more arguments to scan. Shell-string forms (``execlp("sh", "sh", "-c",
    "git ...")``) are caught through the same shell-token regex.
    """
    for child in ast.walk(call):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if child.value in ("git", "git.exe") or _SHELL_GIT.search(child.value):
                return True
        if isinstance(child, ast.Name) and child.id in git_names:
            return True
    return False


def find_git_bypasses(path: Path) -> list[Bypass]:
    """Flag direct git invocations outside the shared runner in one test file.

    The shared runner's own module is the terminal: its git calls are
    returned too, so the terminal-set test can pin them, and the live scan
    excludes them by file.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    scope, run_aliases, git_aliases = _collect_aliases(tree)
    bypasses: list[Bypass] = []

    def visit(node: ast.AST, scope_key: str, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(
                    child,
                    f"{scope_key}.{child.name}" if scope_key else child.name,
                    child.name,
                )
            else:
                if isinstance(child, ast.Call):
                    names = git_aliases.get(scope_key, set()) | git_aliases.get("", set())
                    git = False
                    if _is_subprocess_call(child.func, scope, run_aliases):
                        git = _call_invokes_git(child, names)
                    elif _is_system_call(child.func, scope):
                        git = _system_call_invokes_git(child)
                    elif _is_exec_call(child.func, scope) or _is_pty_call(
                        child.func, scope
                    ):
                        git = _exec_call_invokes_git(child, names)
                    if git:
                        segment = ast.get_source_segment(source, child) or ""
                        snippet = segment.splitlines()[0].strip()[:120]
                        bypasses.append(
                            Bypass(
                                path=path,
                                line=child.lineno,
                                function=function,
                                snippet=snippet,
                            )
                        )
                visit(child, scope_key, function)

    visit(tree, "", "<module>")
    return bypasses


def scan_tests_root() -> tuple[list[Bypass], list[Bypass]]:
    """Return ``(bypasses, runner_terminal_calls)`` for the whole tests tree."""
    bypasses: list[Bypass] = []
    terminal: list[Bypass] = []
    for path in sorted(TESTS_ROOT.glob("*.py")):
        for found in find_git_bypasses(path):
            if path.name == RUNNER_MODULE:
                terminal.append(found)
            else:
                bypasses.append(found)
    return bypasses, terminal


BYPASS_SNIPPETS: tuple[tuple[str, str], ...] = (
    ("literal-list", 'subprocess.run(["git", "init"], cwd=root, check=True)'),
    ("literal-tuple", 'subprocess.run(("git", "add", "x"), cwd=root, check=True)'),
    (
        "git-variable",
        'git = os.fspath(shutil.which("git"))\nsubprocess.run((git, "commit"), cwd=w)',
    ),
    ("helper-call", 'subprocess.run((_git_path(), "rev-parse"), cwd=None)'),
    (
        "aliased-executable",
        'exe = Path(shutil.which("git")).resolve()\n'
        'subprocess.run([exe, "--version"], check=True)',
    ),
    (
        "fspath-wrapped-alias",
        'exe = Path(shutil.which("git")).resolve()\n'
        'subprocess.run((os.fspath(exe), "--exec-path"), check=True)',
    ),
    (
        "inline-which",
        'subprocess.run((os.fspath(Path(shutil.which("git")).resolve()), "init", p),'
        " check=True)",
    ),
    ("popen", 'subprocess.Popen(["git", "fetch"], stdout=subprocess.PIPE)'),
    (
        "from-import",
        'from subprocess import run as srun\nsrun(["git", "status"], cwd=r)',
    ),
    (
        "from-import-plain",
        'from subprocess import run\nrun(["git", "status"], cwd=r)',
    ),
    (
        "from-import-nested",
        'def fixture(root):\n    from subprocess import run\n    run(["git", "init"], cwd=root)\n',
    ),
    ("run-alias", 'real = subprocess.run\nreal(["git", "log"], cwd=r)'),
    (
        "nested-git-call",
        "subprocess.run((os.fspath(_external_git_tool().executable),"
        ' "repack", "-a"), cwd=r)',
    ),
    ("shell-string", 'subprocess.run("git status", shell=True, cwd=r)'),
    ("getoutput", 'subprocess.getoutput("git status")'),
    (
        "subprocess-module-alias",
        'import subprocess as sp\nsp.run(["git", "init"], cwd=root, check=True)',
    ),
    (
        "subprocess-module-alias-popen",
        'import subprocess as sp\nsp.Popen(["git", "fetch"], stdout=sp.PIPE)',
    ),
    (
        "subprocess-module-alias-nested",
        'def fixture(root):\n    import subprocess as sp\n    sp.run(["git", "init"], cwd=root)\n',
    ),
    (
        "subprocess-module-alias-run-alias",
        'import subprocess as sp\nreal = sp.run\nreal(["git", "log"], cwd=r)',
    ),
    ("os-system", 'import os\nos.system("git status")'),
    ("os-system-alias", 'import os as o\no.system("git status")'),
    ("os-system-from-import", 'from os import system\nsystem("git status")'),
    (
        "os-exec",
        'import os\nos.execvp("git", ["git", "status"])',
    ),
    (
        "os-exec-alias",
        'import os as o\no.execv("/usr/bin/git", ["git", "status"])',
    ),
    (
        "os-exec-from-import",
        'from os import execvp\nexecvp("git", ["git", "status"])',
    ),
    (
        "os-spawn",
        'import os\nos.spawnvp(os.P_WAIT, "git", ["git", "status"])',
    ),
    (
        "os-spawn-alias",
        'import os as o\no.spawnv(o.P_WAIT, "/usr/bin/git", ["git", "status"])',
    ),
    ("pty-spawn", 'import pty\npty.spawn(["git", "status"])'),
    ("pty-spawn-alias", 'import pty as p\np.spawn(["git", "status"])'),
    ("pty-spawn-from-import", 'from pty import spawn\nspawn(["git", "status"])'),
)

CLEAN_SNIPPETS: tuple[tuple[str, str], ...] = (
    (
        "python-child",
        'subprocess.run([sys.executable, "-m", "csk", "install"], cwd=r)',
    ),
    ("opaque-argv", "subprocess.run(client_argv, capture_output=True)"),
    (
        "opaque-passthrough",
        "real_run = git_admission.subprocess.run\nreal_run(*args, **kwargs)",
    ),
    ("tool-child", 'subprocess.run(["tool"], cwd=r)'),
    (
        "fspath-wrapped-non-git",
        'log = root / "out.log"\nsubprocess.run([os.fspath(log)], cwd=r)',
    ),
    ("go-child", 'subprocess.run([GO, "env", "GOROOT"], capture_output=True)'),
    (
        "wrapper-string",
        'program.write_text(f"os.execv({g!r}, [\'upload-pack\'])")',
    ),
    ("patched-run", 'monkeypatch.setattr(subprocess, "run", fake)'),
    (
        "raised-not-run",
        'raise subprocess.CalledProcessError(1, ["git", "credential"])',
    ),
    ("conftest-run", 'from conftest import run\nrun(["git", "tag"], repo)'),
    (
        "alias-non-git",
        'import subprocess as sp\nsp.run([sys.executable, "-m", "pytest"], cwd=r)',
    ),
    ("os-system-non-git", 'import os\nos.system("ls -la")'),
    ("os-system-alias-non-git", 'import os as o\no.system("echo hi")'),
    ("os-exec-non-git", 'import os\nos.execvp("tool", ["tool"])'),
    (
        "os-spawn-non-git",
        'import os\nos.spawnvp(os.P_WAIT, "tool", ["tool"])',
    ),
    ("pty-non-git", 'import pty\npty.spawn(["tool"])'),
    (
        "git-word-non-command",
        'import os\nos.system("echo nogit status")',
    ),
)


@pytest.mark.parametrize(("case", "snippet"), BYPASS_SNIPPETS, ids=[c for c, _ in BYPASS_SNIPPETS])
def test_scanner_flags_direct_git_subprocess_calls(
    tmp_path: Path, case: str, snippet: str
) -> None:
    probe = tmp_path / "probe_test.py"
    probe.write_text(
        "import os\nimport subprocess\nfrom pathlib import Path\n" + snippet + "\n",
        encoding="utf-8",
    )

    found = find_git_bypasses(probe)

    assert len(found) == 1, (case, found)


@pytest.mark.parametrize(("case", "snippet"), CLEAN_SNIPPETS, ids=[c for c, _ in CLEAN_SNIPPETS])
def test_scanner_passes_non_git_subprocess_calls(
    tmp_path: Path, case: str, snippet: str
) -> None:
    probe = tmp_path / "probe_test.py"
    probe.write_text(
        "import os\nimport subprocess\nimport sys\nfrom pathlib import Path\n"
        + snippet
        + "\n",
        encoding="utf-8",
    )

    found = find_git_bypasses(probe)

    assert found == [], (case, found)


def test_no_test_file_runs_git_without_the_shared_runner() -> None:
    bypasses, _terminal = scan_tests_root()

    assert bypasses == [], (
        "direct git invocations outside git_fixture_isolation.py: "
        + "; ".join(
            f"{item.path.name}:{item.line} ({item.function}): {item.snippet}"
            for item in bypasses
        )
    )


def test_fixture_guard_rejects_subprocess_module_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A documented module import alias must not bypass the static gate.

    Regression for BUG-261004-473myt revision 2
    (``unisolated-fixture-entrypoints``): the scanner recognized a receiver
    literally named ``subprocess`` but not ``import subprocess as sp``, so
    the composed live gate accepted a direct git writer -- and that writer,
    executed under ``GIT_CONFIG``, changed a sentinel repository's config.
    This test pins all three legs: the scanner flags the alias spelling, the
    live tree scan fails while an alias bypass file is present, and the
    admitted edge is behaviorally real (it corrupts the sentinel).
    """
    from conftest import init_git_repo

    # Leg 1: the scanner resolves the alias spelling.
    probe = tmp_path / "probe_test.py"
    probe.write_text(
        "import subprocess as sp\n"
        'def fixture_write(repo):\n    sp.run(["git", "config", "x.y", "z"], cwd=repo, check=True)\n',
        encoding="utf-8",
    )
    found = find_git_bypasses(probe)
    assert len(found) == 1, found
    assert found[0].function == "fixture_write"

    # Leg 2: the composed live gate fails while the bypass file is present.
    live_probe = TESTS_ROOT / "test_review_alias_probe_tmp.py"
    assert not live_probe.exists(), "a previous run leaked its live probe file"
    live_probe.write_text(
        "import subprocess as sp\n"
        "def fixture(root):\n"
        '    sp.run(["git", "init"], cwd=root, check=True)\n',
        encoding="utf-8",
    )
    try:
        bypasses, _terminal = scan_tests_root()
        live_hits = [item for item in bypasses if item.path == live_probe]
        assert len(live_hits) == 1, (
            "live gate admitted the alias bypass: "
            + "; ".join(
                f"{item.path.name}:{item.line} ({item.function})"
                for item in bypasses
            )
        )
    finally:
        live_probe.unlink(missing_ok=True)

    # Leg 3: the admitted edge is behaviorally real -- the alias writer,
    # executed under a hostile GIT_CONFIG, corrupts a sentinel config. (The
    # writer runs from a tmp file, never through the shared runner, exactly
    # like the escaped edge would. Setup uses the shared runner; only the
    # escaped edge itself runs raw.)
    sentinel = init_git_repo(tmp_path / "sentinel")
    config = sentinel / ".git" / "config"
    before = config.read_bytes()
    fixture = init_git_repo(tmp_path / "fixture")
    writer = tmp_path / "alias_writer.py"
    writer.write_text(
        "import subprocess as sp\n"
        "def fixture_write(repo):\n"
        '    sp.run(["git", "config", "review.corrupted", "yes"], cwd=repo, check=True)\n',
        encoding="utf-8",
    )
    namespace: dict[str, object] = {}
    exec(compile(writer.read_text(encoding="utf-8"), str(writer), "exec"), namespace)
    monkeypatch.setenv("GIT_CONFIG", str(config))
    writer_fn = namespace["fixture_write"]
    assert callable(writer_fn)
    writer_fn(str(fixture))
    assert config.read_bytes() != before, (
        "alias writer did not corrupt the sentinel -- the behavioral leg is vacuous"
    )


def _subprocess_spawn_sites(path: Path, function: str) -> int:
    """Count raw child-spawn calls inside one function of one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "subprocess"
                    and child.func.attr in _SUBPROCESS_FUNCS
                ):
                    count += 1
    return count


def test_runner_module_holds_the_only_terminal_git_calls() -> None:
    import git_fixture_isolation as isolation

    if getattr(isolation, "run_fixture_git", None) is None:
        pytest.skip("the shared runner does not exist on this revision")
    _bypasses, terminal = scan_tests_root()

    # The discovery probe is the only statically visible git call; the
    # runner's own spawn takes argv as a parameter (opaque by design, like
    # conftest.run), so it is pinned structurally instead: exactly one
    # spawn site. A second spawn site anywhere fails this guard.
    assert {item.function for item in terminal} == {"discover_git_dir"}, [
        f"{item.path.name}:{item.line} ({item.function}): {item.snippet}"
        for item in terminal
    ]
    runner_path = TESTS_ROOT / RUNNER_MODULE
    assert _subprocess_spawn_sites(runner_path, "run_fixture_git") == 1
    assert _subprocess_spawn_sites(runner_path, "discover_git_dir") == 1
