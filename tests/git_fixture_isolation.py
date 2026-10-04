"""Harden test git fixtures against ambient repository discovery.

Stdlib only: this module never imports ``csk``, so both the golden support
module and ``conftest`` can share it. Invariant (BUG-261004-473myt): a test
fixture can never change git state outside its own fixture repository, no
matter what ``GIT_*`` variables the ambient environment carries or where the
fixture directory sits.

Three cooperating gates, applied by every test helper that writes git config:

1. ``scrub_git_child_env`` removes the six repository-discovery variables git
   consults before looking at the working tree, and pins
   ``GIT_CEILING_DIRECTORIES`` to the fixture's parent so a fixture directory
   without its own ``.git`` cannot traverse up into an enclosing checkout.
   The scrub alone is load-bearing: ``GIT_COMMON_DIR`` redirects config
   writes to another repository while ``rev-parse --absolute-git-dir`` still
   reports the fixture's own directory, so the guard below cannot catch it.
2. ``discover_git_dir`` runs ``git rev-parse --absolute-git-dir`` under the
   same scrubbed environment the fixture's config-writing children get, and
   decodes the path as UTF-8 explicitly: git emits paths as UTF-8, while the
   ambient locale (cp1252 on Windows runners) would mojibake non-ASCII
   fixture paths and false-refuse.
3. ``check_own_git_dir`` compares the discovered directory to the fixture's
   own ``.git`` before any config write. It is the backstop for a redirector
   the scrub does not know about (a future git variable, an explicit
   ``--git-dir`` that slipped into a helper's argv), and it fails closed on
   any mismatch, including undecodable output.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import MutableMapping
from pathlib import Path

#: Repository-discovery variables git consults before the working tree. Every
#: one of them can redirect a fixture's writes into another repository:
#: ``GIT_DIR``/``GIT_WORK_TREE`` repoint the repository, ``GIT_INDEX_FILE``
#: repoints ``git add``, ``GIT_COMMON_DIR`` repoints ``git config`` while the
#: reported git dir stays put, ``GIT_OBJECT_DIRECTORY`` repoints new objects,
#: and ``GIT_ALTERNATE_OBJECT_DIRECTORIES`` adds foreign object stores.
GIT_DISCOVERY_ENV_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def scrub_git_child_env(
    env: MutableMapping[str, str],
    discovery_root: str | os.PathLike[str],
) -> MutableMapping[str, str]:
    """Remove discovery redirectors and pin the ceiling above ``discovery_root``.

    Operates in place and returns ``env`` for chaining. ``discovery_root`` is
    the directory git discovers the repository from: the child's ``cwd``, or
    the ``-C`` target when the helper passes one. Callers that pass an
    explicit ``-C`` outside their ``cwd`` must pass that target here; plain
    ``cwd`` helpers pass their ``cwd``.
    """
    for var in GIT_DISCOVERY_ENV_VARS:
        env.pop(var, None)
    env["GIT_CEILING_DIRECTORIES"] = str(
        Path(os.path.realpath(discovery_root)).parent
    )
    return env


def discover_git_dir(fixture: str | os.PathLike[str]) -> str:
    """Return the git directory git discovers for ``fixture``, as UTF-8 text.

    Runs under the same scrubbed environment (and ceiling) the fixture's
    config-writing children get, so the guard observes exactly what they
    would. A nonzero rev-parse -- no repository, e.g. the ceiling blocked
    traversal into an enclosing checkout -- raises AssertionError: fail
    closed through the same channel as a mismatch.
    """
    env = scrub_git_child_env(dict(os.environ), fixture)
    proc = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=fixture,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="surrogateescape",
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "refusing to write git config outside the fixture repository: "
            f"no git repository discovered in fixture {str(fixture)!r}: "
            f"{proc.stderr.strip()}"
        )
    return proc.stdout


def check_own_git_dir(fixture: str | os.PathLike[str], actual_git_dir: str) -> None:
    """Refuse to write config when the discovered git dir is not the fixture's.

    ``actual_git_dir`` is the stdout of ``git rev-parse --absolute-git-dir``
    run in ``fixture``. Both sides are resolved and case-folded so macOS
    ``/tmp`` symlinks and Windows drive-letter case cannot cause a false
    refusal, and any mismatch -- including garbage output -- raises.
    """
    expected = os.path.normcase(os.path.realpath(os.path.join(str(fixture), ".git")))
    actual = os.path.normcase(os.path.realpath(actual_git_dir.strip()))
    if actual != expected:
        raise AssertionError(
            "refusing to write git config outside the fixture repository: "
            f"discovered git dir {actual_git_dir.strip()!r} is not the "
            f"fixture's {expected!r} (fixture {str(fixture)!r})"
        )
