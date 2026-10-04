"""Every test git invocation routes through the one shared runner.

Static guard for BUG-261004-473myt revision 2: the recording review found
fixture helpers that bypassed the shared scrub (``tests/test_git_admission.py``,
``tests/test_sources_transport.py``, ``tests/test_git_admission_ssh.py``), so
``GIT_INDEX_FILE``/``GIT_DIR``/``GIT_WORK_TREE`` redirected their writes into
unrelated repositories. The fix is one shared fixture git runner --
``git_fixture_isolation.run_fixture_git`` -- and this scan fails if any test
file calls git via subprocess without it.

The scanner resolves git executables through the shapes the audit found:
``"git"`` literals, ``(git, ...)`` variables, ``_git_path()`` calls,
``shutil.which("git")`` (inline or aliased through ``os.fspath``/``Path``/
``resolve`` chains), and ``subprocess`` itself aliased. Anything it cannot
resolve -- an opaque ``*args`` passthrough, a fully variable argv -- is a
stated bound, not a proof; the snippet tests below pin both the catches and
the intentional passes.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parent
RUNNER_MODULE = "git_fixture_isolation.py"
_SUBPROCESS_FUNCS = frozenset({"run", "Popen", "call", "check_output", "check_call"})
_SHELL_GIT = re.compile(r"(?:^|[\s;|&])git(?:\s|$)")


@dataclass(frozen=True)
class Bypass:
    path: Path
    line: int
    function: str
    snippet: str


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _is_subprocess_call(func: ast.AST, from_imports: set[str], run_aliases: set[str]) -> bool:
    if isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_FUNCS:
        return _dotted_name(func.value).split(".")[-1] == "subprocess" or (
            isinstance(func.value, ast.Name) and func.value.id == "subprocess"
        )
    if isinstance(func, ast.Name):
        return func.id in from_imports or func.id in run_aliases
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


def _collect_aliases(tree: ast.Module) -> tuple[set[str], set[str], dict[str, set[str]]]:
    """Collect subprocess from-imports, run aliases, and per-scope git aliases.

    Returns ``(from_imports, run_aliases, git_aliases)`` where ``git_aliases``
    maps a scope key (``""`` for module level, else the qualified function
    name) to the git-executable variable names assigned there.
    """
    from_imports: set[str] = set()
    run_aliases: set[str] = set()
    git_aliases: dict[str, set[str]] = {}

    def scope_aliases(scope: str) -> set[str]:
        return git_aliases.setdefault(scope, set())

    def is_run_alias_value(value: ast.AST) -> bool:
        if isinstance(value, ast.Attribute) and value.attr in _SUBPROCESS_FUNCS:
            return "subprocess" in _dotted_name(value.value)
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
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _SUBPROCESS_FUNCS:
                    from_imports.add(alias.asname or alias.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            for target in targets:
                visit_assign(target, value, "")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _visit_function(node, node.name, from_imports, run_aliases, git_aliases)
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
    return from_imports, run_aliases, git_aliases


def _visit_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    scope: str,
    from_imports: set[str],
    run_aliases: set[str],
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
                if isinstance(value, ast.Attribute) and value.attr in _SUBPROCESS_FUNCS:
                    if "subprocess" in _dotted_name(value.value):
                        run_aliases.add(target.id)
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


def find_git_bypasses(path: Path) -> list[Bypass]:
    """Flag direct git-via-subprocess calls in one test file.

    The shared runner's own module is the terminal: its git calls are
    returned too, so the terminal-set test can pin them, and the live scan
    excludes them by file.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    from_imports, run_aliases, git_aliases = _collect_aliases(tree)
    bypasses: list[Bypass] = []

    def visit(node: ast.AST, scope: str, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, f"{scope}.{child.name}" if scope else child.name, child.name)
            else:
                if isinstance(child, ast.Call) and _is_subprocess_call(
                    child.func, from_imports, run_aliases
                ):
                    names = git_aliases.get(scope, set()) | git_aliases.get("", set())
                    if _call_invokes_git(child, names):
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
                visit(child, scope, function)

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
    ("run-alias", 'real = subprocess.run\nreal(["git", "log"], cwd=r)'),
    (
        "nested-git-call",
        "subprocess.run((os.fspath(_external_git_tool().executable),"
        ' "repack", "-a"), cwd=r)',
    ),
    ("shell-string", 'subprocess.run("git status", shell=True, cwd=r)'),
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
        "direct git-via-subprocess calls outside git_fixture_isolation.py: "
        + "; ".join(
            f"{item.path.name}:{item.line} ({item.function}): {item.snippet}"
            for item in bypasses
        )
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
