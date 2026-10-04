"""Narrowing-mutant harness for the dispatch slice.

Each mutant weakens one gate to admit exactly one member of the class it
must reject; a named committed test must then fail while an unaffected
sanity test still passes. Anchors are exact source substrings verified
before application: a missing or ambiguous anchor is a loud ERROR (never
a skip), so the harness cannot pass against a tree it no longer matches.
Mutants run against a full copy of the shipped tree, never the worktree,
and every file hash is verified before and after each mutant.

Usage:
    python tests/dispatch_mutants.py [--list] [--mutant ID] [--keep]

Exit status is 0 only when every selected mutant is killed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


WORKTREE = Path(__file__).resolve().parent.parent
TEST_FILE = "tests/test_dispatch.py"

MUTANTS: list[dict] = [
    {
        "id": "M-identity-ino-dropped",
        "gate": "root matching requires the recorded inode",
        "narrows_to": (
            "path-only matching serves a replaced root under the same spelling"
        ),
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "        live = [\n"
                "            entry\n"
                "            for entry in candidates\n"
                "            if _recorded_ino(entry) == ancestor_ino\n"
                "        ]\n",
                "        live = [  # MUTANT: path-only matching, inode ignored\n"
                "            entry\n"
                "            for entry in candidates\n"
                "            if True\n"
                "        ]\n",
            ),
        ],
        "killing": ["test_d4_replaced_case_alias_root_refuses"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-identity-stale-fallthrough",
        "gate": "a stale deepest record refuses with re-register guidance",
        "narrows_to": "stale roots fall through to another scope",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "        raise _stale_root_error(candidates[0])\n",
                "        return None  # MUTANT: stale falls through\n",
            ),
        ],
        "killing": ["test_replaced_root_refuses_with_reregister_guidance"],
        "sanity": ["test_outside_projects_global_version_runs_and_root_is_unset"],
    },
    {
        "id": "M-identity-dev-restored",
        "gate": "device numbers are never identity (remount immunity)",
        "narrows_to": "requiring the publication-time st_dev re-bricks remounts",
        "edits": [
            (
                "src/csk/dispatch.py",
                '        "root_identity": {\n'
                '            "st_ino": str(root_info.st_ino),\n'
                "        },\n",
                '        "root_identity": {\n'
                '            "st_ino": str(root_info.st_ino),\n'
                '            "st_dev": str(root_info.st_dev),  # MUTANT\n'
                "        },\n",
            ),
            (
                "src/csk/_dispatch_runtime.py",
                '    if (\n'
                "        not isinstance(identity, dict)\n"
                '        or set(identity.keys()) != {"st_ino"}\n'
                '        or not _is_identity_component(identity["st_ino"])\n'
                "    ):\n",
                "    if (\n"
                "        not isinstance(identity, dict)\n"
                '        or set(identity.keys()) != {"st_ino", "st_dev"}  # MUTANT\n'
                '        or not _is_identity_component(identity["st_ino"])\n'
                '        or not _is_identity_component(identity["st_dev"])  # MUTANT\n'
                "    ):\n",
            ),
            (
                "src/csk/_dispatch_runtime.py",
                '    return {"st_ino": identity["st_ino"]}\n',
                '    return {"st_ino": identity["st_ino"], "st_dev": identity["st_dev"]}  # MUTANT\n',
            ),
            (
                "src/csk/_dispatch_runtime.py",
                "        live = [\n"
                "            entry\n"
                "            for entry in candidates\n"
                "            if _recorded_ino(entry) == ancestor_ino\n"
                "        ]\n",
                "        live = [\n"
                "            entry\n"
                "            for entry in candidates\n"
                "            if _recorded_ino(entry) == ancestor_ino\n"
                "            and os.stat(chain[len(wanted) - best_depth][0]).st_dev\n"
                '            == int(entry["root_identity"]["st_dev"])  # MUTANT\n'
                "        ]\n",
            ),
        ],
        "killing": ["test_r4_remount_simulation_ignores_device_change"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-identity-resolve-no-dedupe",
        "gate": "re-registration reuses the directory's checkout id",
        "narrows_to": "a case-alias add mints a second empty record",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                '        if _paths_equal(entry["canonical_root"], canonical_root):\n'
                "            return checkout_id\n",
                '        if _paths_equal(entry["canonical_root"], canonical_root):\n'
                "            continue  # MUTANT: never dedupe by directory\n",
            ),
        ],
        "killing": ["test_r4_case_alias_registration_deduplicates_identity"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-identity-moved-fallthrough",
        "gate": "a moved root refuses with re-register guidance",
        "narrows_to": "moved roots fall through to the global fallback",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "        raise DispatchError(\n"
                '            f"registered project root {entry[\'canonical_root\']} moved "\n'
                '            f"(its recorded path no longer resolves to the registered "\n'
                '            f"directory). Re-register the project with \'csk install\' to "\n'
                '            f"repair dispatch",\n'
                '            kind="moved_root",\n'
                "        )\n",
                "        continue  # MUTANT: moved root falls through\n",
            ),
        ],
        "killing": ["test_renamed_root_refuses_with_reregister_guidance"],
        "sanity": ["test_outside_projects_global_version_runs_and_root_is_unset"],
    },
    {
        "id": "M-taxonomy-missing-current-empty",
        "gate": "a missing pointer after publication is corruption",
        "narrows_to": "losing the pointer silently means a fresh manager",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "        if has_publication_artifacts(manager_home):\n"
                "            raise DispatchError(\n"
                "                f\"dispatch pointer {current_path} for manager at \"\n"
                "                f\"{manager_home} is missing though generations or shims \"\n"
                "                f\"were published; reinstall the affected project \"\n"
                "                f\"('csk install') or the global set ('csk global install') \"\n"
                "                f\"to republish it, or run 'csk dispatch recover' to \"\n"
                "                f\"restore the last readable generation. This is an error, \"\n"
                "                f\"not 'outside any project'\",\n"
                "                kind=\"current_missing\",\n"
                "            )\n"
                "        return empty_registry()\n",
                "        return empty_registry()  # MUTANT: missing pointer means fresh\n",
            ),
        ],
        "killing": ["test_r5_missing_current_after_publication_is_corrupt"],
        "sanity": ["test_failed_registry_read_is_error_never_absence"],
    },
    {
        "id": "M-taxonomy-unicode-replaced",
        "gate": "undecodable bytes report registry_unreadable",
        "narrows_to": "replacement decoding misclassifies undecodable bytes",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                '        generation_id = raw_pointer.decode("utf-8").strip()\n',
                '        generation_id = raw_pointer.decode(  # MUTANT\n'
                '            "utf-8", errors="replace"\n'
                "        ).strip()\n",
            ),
            (
                "src/csk/_dispatch_runtime.py",
                '        text = raw.decode("utf-8")\n',
                '        text = raw.decode("utf-8", errors="replace")  # MUTANT\n',
            ),
        ],
        "killing": ["test_r5_nonutf8_bytes_are_guided_errors"],
        "sanity": ["test_failed_registry_read_is_error_never_absence"],
    },
    {
        "id": "M-taxonomy-heal-restored",
        "gate": "publication refuses an unreadable registry (no silent heal)",
        "narrows_to": "publication silently reverts to the last readable pin",
        "edits": [
            (
                "src/csk/dispatch.py",
                "    try:\n"
                "        registry = _dispatch_runtime.load_registry(str(home))\n"
                "    except _dispatch_runtime.DispatchError as exc:\n"
                "        raise DispatchPublishError(_with_recover_guidance(exc)) from exc\n"
                "    return registry, current_id, []\n",
                "    try:\n"
                "        registry = _dispatch_runtime.load_registry(str(home))\n"
                "    except _dispatch_runtime.DispatchError:  # MUTANT: silent heal\n"
                "        healed = _find_last_readable_generation(home)\n"
                "        if healed is None:\n"
                "            return _dispatch_runtime.empty_registry(), None, []\n"
                "        healed_id, registry = healed\n"
                "        return registry, healed_id, []\n"
                "    return registry, current_id, []\n",
            ),
        ],
        "killing": ["test_r5_publication_refuses_unreadable_registry"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-compose-no-replacement",
        "gate": "whole-skill replacement applies before owner uniqueness",
        "narrows_to": "owner clashes fire before the replaced skill is dropped",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                '        if entry["owner"] not in project_skill_names\n',
                "        if True  # MUTANT: no whole-skill replacement\n",
            ),
            (
                "src/csk/_dispatch_runtime.py",
                '        if entry["owner"] in project_skill_names\n',
                "        if False  # MUTANT: no suppression set\n",
            ),
        ],
        "killing": ["test_r9_whole_skill_replacement_precedes_owner_check"],
        "sanity": ["test_cross_layer_different_owners_refuse_at_publication"],
    },
    {
        "id": "M-compose-case-exact",
        "gate": "command names use the shims volume's case rules",
        "narrows_to": "case aliases collide silently on insensitive volumes",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "    return name.casefold() if case_insensitive else name\n",
                "    return name  # MUTANT: exact-only comparison\n",
            ),
        ],
        "killing": ["test_r9_case_alias_across_layers_refuses"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-exec-sigpipe-kept",
        "gate": "SIGPIPE/SIGXFSZ return to SIG_DFL before exec",
        "narrows_to": "targets inherit CPython's ignored dispositions",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "            signal.signal(signum, signal.SIG_DFL)\n",
                "            pass  # MUTANT: keep ignored disposition\n",
            ),
        ],
        "killing": ["test_r11_sigpipe_disposition_matches_direct"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-exec-env-norestore",
        "gate": "caller-absent locale variables are removed before exec",
        "narrows_to": "interpreter coercion leaks into the target",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                '        elif flag == "0":\n'
                "            env.pop(variable, None)\n",
                '        elif flag == "0":\n'
                "            pass  # MUTANT: keep coerced value\n",
            ),
        ],
        "killing": ["test_r11_env_parity_matches_direct"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-atomic-unlink-first",
        "gate": "atomic writer never unlinks the live file before staging",
        "narrows_to": "unlink-then-create loses the working dispatcher",
        "edits": [
            (
                "src/csk/dispatch.py",
                "    target.parent.mkdir(parents=True, exist_ok=True)\n"
                "    fd, temporary_name = tempfile.mkstemp(\n",
                "    target.unlink(missing_ok=True)  # MUTANT: unlink before staging\n"
                "    target.parent.mkdir(parents=True, exist_ok=True)\n"
                "    fd, temporary_name = tempfile.mkstemp(\n",
            ),
        ],
        "killing": ["test_r12_interrupted_dispatcher_refresh_preserves_old"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-setup-unlocked",
        "gate": "dispatch setup runs under the manager lock",
        "narrows_to": "setup reconciles without holding the lock",
        "edits": [
            (
                "src/csk/cli.py",
                "        with GlobalLock(csk_home):\n"
                "            dispatch.recover_launchers(csk_home)\n",
                "        if True:  # MUTANT: setup without the manager lock\n"
                "            dispatch.recover_launchers(csk_home)\n",
            ),
        ],
        "killing": ["test_r12_setup_acquires_manager_lock"],
        "sanity": ["test_dispatch_setup_cli_reports_and_installs_idempotently"],
    },
    {
        "id": "M-target-no-ino",
        "gate": "the per-call tuple binds the activation inode",
        "narrows_to": "same size and mtime with a new inode is accepted",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "    if isinstance(expected_digest, dict):\n"
                '        for key in ("st_ino", "size", "mtime_ns"):\n',
                "    if isinstance(expected_digest, dict):\n"
                '        for key in ("size", "mtime_ns"):  # MUTANT: no inode\n',
            ),
            (
                "src/csk/_dispatch_runtime.py",
                "    live = (\n"
                "        str(info.st_ino),\n"
                '        str(info.st_size),\n'
                '        str(info.st_mtime_ns),\n'
                "    )\n",
                "    live = (\n"
                '        str(info.st_size),  # MUTANT: no inode\n'
                '        str(info.st_mtime_ns),\n'
                "    )\n",
            ),
        ],
        "killing": ["test_n1_inode_change_with_same_size_mtime_refuses"],
        "sanity": ["test_corrupt_pinned_target_refuses_without_global_fallback"],
    },
    {
        "id": "M-target-no-containment",
        "gate": "targets inside a registered checkout refuse",
        "narrows_to": "checkout-contained targets execute",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "    checkout_hit = _checkout_container(real, projects)\n",
                "    checkout_hit = None  # MUTANT: no checkout containment\n",
            ),
        ],
        "killing": ["test_dispatch_refuses_target_inside_checkout"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-scope-f3-dropped",
        "gate": "skipped registered projects publish an empty scope",
        "narrows_to": "a Skillfile-less nested project inherits the parent pin",
        "edits": [
            (
                "src/csk/installer.py",
                '            result.messages.append(f"{project.alias}: Skillfile.json not found; skipped")\n'
                "            _publish_empty_dispatch_scope(config, project, options, result)\n"
                "            return result\n",
                '            result.messages.append(f"{project.alias}: Skillfile.json not found; skipped")\n'
                "            # MUTANT: no empty scope for the skipped project\n"
                "            return result\n",
            ),
        ],
        "killing": ["test_r3_install_publishes_empty_scope_for_skipped_nested"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-read-open-str",
        "gate": "dispatch opens nothing under a checkout (str path)",
        "narrows_to": "a relative str open of checkout bytes goes undetected",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "    wanted = _split_components(physical)\n",
                '    open("Skillfile.json").close()  # MUTANT: checkout read (str)\n'
                "    wanted = _split_components(physical)\n",
            ),
        ],
        "killing": ["test_dispatcher_reads_nothing_under_checkout"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-read-open-bytes",
        "gate": "dispatch opens nothing under a checkout (bytes path)",
        "narrows_to": "a bytes open of checkout bytes goes undetected",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "    wanted = _split_components(physical)\n",
                '    open(b"Skillfile.json").close()  # MUTANT: checkout read (bytes)\n'
                "    wanted = _split_components(physical)\n",
            ),
        ],
        "killing": ["test_dispatcher_reads_nothing_under_checkout"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-read-os-open-dir-fd",
        "gate": "dispatch opens nothing under a checkout (dir_fd)",
        "narrows_to": "a dir_fd-relative open of checkout bytes goes undetected",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "    wanted = _split_components(physical)\n",
                "    os.close(os.open(\"Skillfile.json\", os.O_RDONLY, dir_fd=os.open(\".\", os.O_RDONLY)))  # MUTANT: checkout read (dir_fd)\n"
                "    wanted = _split_components(physical)\n",
            ),
        ],
        "killing": ["test_dispatcher_reads_nothing_under_checkout"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-read-listdir",
        "gate": "dispatch lists nothing under a checkout",
        "narrows_to": "a checkout listing goes undetected",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "    wanted = _split_components(physical)\n",
                '    os.listdir(".")  # MUTANT: checkout listing\n'
                "    wanted = _split_components(physical)\n",
            ),
        ],
        "killing": ["test_dispatcher_reads_nothing_under_checkout"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-drift-token-preserving",
        "gate": "the runtime template must stay stdlib-only (source-text gate)",
        "narrows_to": "the searched-for tokens stay but the check never fires",
        "edits": [
            (
                "src/csk/dispatch.py",
                "        if marker in template:\n",
                "        if marker in template and False:  # MUTANT: token kept, gate dead\n",
            ),
        ],
        "killing": ["test_ensure_dispatcher_refuses_csk_importing_template"],
        "sanity": ["test_shim_and_dispatcher_use_absolute_entry_points_only"],
    },
    {
        "id": "M-wrapper-sentinel-export-dropped",
        "gate": "the wrapper exports exact caller-locale snapshots",
        "narrows_to": "exact snapshots degrade to the strip heuristic",
        "edits": [
            (
                "src/csk/dispatch.py",
                '        "export _CSK_ENV_SET_LC_CTYPE _CSK_ENV_VAL_LC_CTYPE "\n'
                '        "_CSK_ENV_SET_CF _CSK_ENV_VAL_CF",\n',
                '        "# MUTANT: sentinels never exported",\n',
            ),
        ],
        "killing": ["test_r11_env_parity_matches_direct"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-global-stale-skip-removed",
        "gate": "global publication skips stale records with a warning",
        "narrows_to": "a dead record vetoes unrelated global installs",
        "edits": [
            (
                "src/csk/dispatch.py",
                '        if status != "live":\n',
                '        if False:  # MUTANT: never skip stale\n',
            ),
        ],
        "killing": ["test_r4_deleted_record_does_not_block_global_install"],
        "sanity": ["test_cross_layer_different_owners_refuse_at_publication"],
    },
    {
        "id": "M-empty-scope-clobber",
        "gate": "re-adding a scope keeps an installed record untouched",
        "narrows_to": "an empty re-add overwrites installed pins",
        "edits": [
            (
                "src/csk/dispatch.py",
                "    if (\n"
                '        entry["checkout_id"] in registry["projects"]\n'
                "        and current_id is not None\n"
                "    ):\n"
                "        return current_id, warnings\n",
                "    if False:  # MUTANT: empty scope overwrites installed\n"
                "        return current_id, warnings\n",
            ),
        ],
        "killing": ["test_r4_case_alias_registration_deduplicates_identity"],
        "sanity": ["test_project_add_publishes_empty_scope_boundary"],
    },
]


def _hash_tree(root: Path, files: list[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name in files:
        data = (root / name).read_bytes()
        hashes[name] = hashlib.sha256(data).hexdigest()
    return hashes


def _mutant_files() -> list[str]:
    files: list[str] = []
    for mutant in MUTANTS:
        for name, _anchor, _replacement in mutant["edits"]:
            if name not in files:
                files.append(name)
    return files


def _copy_shipped_tree() -> Path:
    target = Path(tempfile.mkdtemp(prefix="dispatch-mutants-"))

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for name in names:
            if name in {
                ".git",
                ".venv",
                ".temp",
                "__pycache__",
                ".pytest_cache",
                ".hypothesis",
                ".mypy_cache",
                ".ruff_cache",
            } or name.endswith(".pyc"):
                ignored.add(name)
        return ignored

    for child in WORKTREE.iterdir():
        if child.name in {".git", ".venv", ".temp"}:
            continue
        if child.is_dir():
            shutil.copytree(child, target / child.name, ignore=ignore)
        else:
            shutil.copy2(child, target / child.name)
    return target


def _preflight(tree: Path) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tree / "src")
    proc = subprocess.run(
        [sys.executable, "-c", "import csk; print(csk.__file__)"],
        cwd=tree,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"mutant-tree import preflight failed: {proc.stderr}")
    resolved = proc.stdout.strip()
    if not resolved.startswith(str(tree)):
        raise RuntimeError(
            f"mutant tree resolves csk to {resolved}, not the copy; refusing"
        )


def _apply_edits(tree: Path, edits: list[tuple[str, str, str]]) -> None:
    for name, anchor, replacement in edits:
        path = tree / name
        text = path.read_text(encoding="utf-8")
        found = text.count(anchor)
        if found != 1:
            raise RuntimeError(
                f"anchor drift in {name}: expected 1 occurrence, found "
                f"{found} for {anchor[:80]!r}"
            )
        path.write_text(text.replace(anchor, replacement), encoding="utf-8")


_RESULT_RE = re.compile(r"^(\S+::\S+?)\s+(PASSED|FAILED|SKIPPED|ERROR)\b")


def _parse_outcomes(output: str, node_ids: list[str]) -> dict[str, str]:
    verdicts: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for line in output.splitlines():
        # Only -v result lines count: warning summaries mention node
        # ids next to words like OSError and must never parse.
        match = _RESULT_RE.match(line.strip())
        if not match:
            continue
        node, verdict = match.group(1), match.group(2).lower()
        base = node.split("::")[-1].split("[")[0]
        for node_id in node_ids:
            if base == node_id.split("::")[-1]:
                if verdict == "error":
                    verdict = "failed"
                verdicts[node_id].append(verdict)
    outcomes: dict[str, str] = {}
    for node_id, seen in verdicts.items():
        if not seen:
            continue
        if all(verdict == "failed" for verdict in seen):
            outcomes[node_id] = "failed"
        elif all(verdict == "passed" for verdict in seen):
            outcomes[node_id] = "passed"
        elif all(verdict == "skipped" for verdict in seen):
            outcomes[node_id] = "skipped"
        else:
            outcomes[node_id] = "mixed"
    return outcomes


def _run_mutant(tree: Path, mutant: dict) -> tuple[str, str]:
    nodes = [f"{TEST_FILE}::{name}" for name in mutant["killing"]]
    nodes += [f"{TEST_FILE}::{name}" for name in mutant["sanity"]]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tree / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *nodes, "-v", "-p", "no:cacheprovider"],
        cwd=tree,
        env=env,
        text=True,
        capture_output=True,
        timeout=900,
    )
    combined = proc.stdout + "\n" + proc.stderr
    if os.environ.get("DISPATCH_MUTANTS_DEBUG"):
        debug_path = Path(tempfile.mkdtemp(prefix="mutant-debug-")) / "pytest.log"
        debug_path.write_text(combined)
        print(f"pytest output: {debug_path}")
    outcomes = _parse_outcomes(combined, nodes)
    killing = [f"{TEST_FILE}::{name}" for name in mutant["killing"]]
    sanity = [f"{TEST_FILE}::{name}" for name in mutant["sanity"]]
    missing = [node for node in nodes if node not in outcomes]
    if missing:
        return ("error", f"no outcome parsed for {missing}\n{combined[-3000:]}")
    if any(outcomes[node] == "skipped" for node in killing):
        return ("unproven", "killing test skipped on this host")
    if any(outcomes[node] == "mixed" for node in killing):
        return ("survived", f"killing test only partly failed: {outcomes}")
    if any(outcomes[node] != "failed" for node in killing):
        passed = [node for node in killing if outcomes[node] != "failed"]
        return ("survived", f"killing test did not fail: {passed}")
    if any(outcomes[node] != "passed" for node in sanity):
        bad = [node for node in sanity if outcomes[node] != "passed"]
        return ("invalid", f"sanity did not pass: {bad}")
    return ("killed", "")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list mutants")
    parser.add_argument(
        "--mutant",
        default=None,
        help="run mutants by id (comma-separated) instead of all",
    )
    parser.add_argument("--keep", action="store_true", help="keep the tree copy")
    args = parser.parse_args(argv)
    if args.list:
        for mutant in MUTANTS:
            print(f"{mutant['id']}: {mutant['gate']}")
        return 0
    selected = MUTANTS
    if args.mutant is not None:
        wanted = [name.strip() for name in args.mutant.split(",")]
        selected = [m for m in MUTANTS if m["id"] in wanted]
        unknown = [name for name in wanted if name not in {m["id"] for m in MUTANTS}]
        if unknown:
            print(f"unknown mutants {unknown}", file=sys.stderr)
            return 2
    tree = _copy_shipped_tree()
    print(f"mutant tree: {tree}")
    try:
        _preflight(tree)
        files = _mutant_files()
        pristine = _hash_tree(tree, files)
        rows: list[tuple[str, str, str, str]] = []
        failed = False
        for mutant in selected:
            before = _hash_tree(tree, files)
            if before != pristine:
                raise RuntimeError(
                    f"tree is dirty before {mutant['id']}; refusing to continue"
                )
            try:
                _apply_edits(tree, mutant["edits"])
            except RuntimeError as exc:
                rows.append((mutant["id"], "error", mutant["killing"][0], str(exc)))
                failed = True
                continue
            try:
                status, detail = _run_mutant(tree, mutant)
            finally:
                for name in files:
                    original = (WORKTREE / name).read_bytes()
                    (tree / name).write_bytes(original)
            after = _hash_tree(tree, files)
            if after != pristine:
                raise RuntimeError(
                    f"tree did not restore after {mutant['id']}; refusing"
                )
            rows.append(
                (
                    mutant["id"],
                    status,
                    ",".join(mutant["killing"]),
                    detail,
                )
            )
            if status != "killed":
                failed = True
        print()
        print("mutant | verdict | killing test | detail")
        for mutant_id, status, killing, detail in rows:
            print(f"{mutant_id} | {status} | {killing} | {detail}")
        print()
        killed = sum(1 for _i, s, _k, _d in rows if s == "killed")
        print(f"{killed} of {len(rows)} mutants killed")
        return 1 if failed else 0
    finally:
        if not args.keep:
            shutil.rmtree(tree, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
