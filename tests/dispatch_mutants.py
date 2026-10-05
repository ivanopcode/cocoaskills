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
                "    live = [\n"
                "        entry\n"
                "        for entry in candidates\n"
                '        if classify_record(entry) == "live"\n'
                "    ]\n",
                "    live = [  # MUTANT: path-only matching, inode ignored\n"
                "        entry\n"
                "        for entry in candidates\n"
                "        if True\n"
                "    ]\n",
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
                "    raise _stale_root_error(candidates[0])\n",
                "    return None  # MUTANT: stale falls through\n",
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
                "    live = [\n"
                "        entry\n"
                "        for entry in candidates\n"
                '        if classify_record(entry) == "live"\n'
                "    ]\n",
                "    live = [  # MUTANT: device required, remounts re-brick\n"
                "        entry\n"
                "        for entry in candidates\n"
                '        if classify_record(entry) == "live"\n'
                '        and os.stat(entry["canonical_root"]).st_dev\n'
                '        == int(entry["root_identity"]["st_dev"])\n'
                "    ]\n",
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
                "        if components_equal(\n"
                "            recorded,\n"
                "            wanted,\n"
                "            case_insensitive=case_insensitive,\n"
                "            normalization_insensitive=normalization_insensitive,\n"
                "        ):\n"
                "            return checkout_id\n",
                "        if components_equal(\n"
                "            recorded,\n"
                "            wanted,\n"
                "            case_insensitive=case_insensitive,\n"
                "            normalization_insensitive=normalization_insensitive,\n"
                "        ):\n"
                "            continue  # MUTANT: never dedupe by directory\n",
            ),
        ],
        "killing": ["test_r4_case_alias_registration_deduplicates_identity"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-registry-migration-fallthrough",
        "gate": "a needs_migration scope refuses with reinstall guidance",
        "narrows_to": "pre-dispatch projects silently serve the global fallback",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                '    if project is not None and project.get("needs_migration", False):\n',
                "    if False:  # MUTANT: migration tombstone never refuses\n",
            ),
        ],
        "killing": [
            "test_d7_pre_upgrade_project_refuses_then_migrates_on_reinstall"
        ],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
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
        "id": "M-shim-disposition-clobber",
        "gate": "the shim execs the target without touching dispositions",
        "narrows_to": "a shim-ignored SIGPIPE lets the target survive it",
        "edits": [
            (
                "src/csk/dispatch.py",
                "    lines.append('exec \"$@\"')\n",
                "    lines.append(\"trap '' PIPE; exec \\\"$@\\\"\")  # MUTANT: shim ignores SIGPIPE\n",
            ),
        ],
        "killing": ["test_r11_sigpipe_disposition_matches_direct"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-shim-nonce-restore-dropped",
        "gate": "the shim restores every nonce working name before exec",
        "narrows_to": "exactly one working name is dropped from the target env",
        "edits": [
            (
                "src/csk/dispatch.py",
                "    for key in keys:\n"
                "        lines.extend(\n"
                "            [\n",
                "    for key in keys:  # MUTANT: one working name never restored\n"
                '        if key == "out":\n'
                '            lines.append(f"  unset {var[key]}")\n'
                "            continue\n"
                "        lines.extend(\n"
                "            [\n",
            ),
        ],
        "killing": ["test_shim_generation_nonce_names_preserved"],
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
                "    checkout_hit = _checkout_container(real, projects, manager_home=manager_home)\n",
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
        "killing": ["test_r3_single_skipped_project_publishes_empty_scope_immediately"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-read-open-str",
        "gate": "dispatch opens nothing under a checkout (str path)",
        "narrows_to": "a relative str open of checkout bytes goes undetected",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "    wanted = split_components(physical)\n",
                '    open("Skillfile.json").close()  # MUTANT: checkout read (str)\n'
                "    wanted = split_components(physical)\n",
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
                "    wanted = split_components(physical)\n",
                '    open(b"Skillfile.json").close()  # MUTANT: checkout read (bytes)\n'
                "    wanted = split_components(physical)\n",
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
                "    wanted = split_components(physical)\n",
                "    os.close(os.open(\"Skillfile.json\", os.O_RDONLY, dir_fd=os.open(\".\", os.O_RDONLY)))  # MUTANT: checkout read (dir_fd)\n"
                "    wanted = split_components(physical)\n",
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
                "    wanted = split_components(physical)\n",
                '    os.listdir(".")  # MUTANT: checkout listing\n'
                "    wanted = split_components(physical)\n",
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
        "id": "M-wrapper-var-restored",
        "gate": "the wrapper holds no shell variables at all",
        "narrows_to": "an assigned name keeps its export and leaks in",
        "edits": [
            (
                "src/csk/dispatch.py",
                '        f"if [ ! -x {quoted_python} ]; then",\n',
                "        '_CSK_PYTHON=\"shadowed\"',  # MUTANT\n"
                '        f"if [ ! -x {quoted_python} ]; then",\n',
            ),
        ],
        "killing": ["test_generated_wrapper_text_is_literal_property"],
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
    {
        "id": "M-identity-ino-only",
        "gate": "an inode match without a path match is never a match",
        "narrows_to": "inode-only matching serves a same-inode alien directory",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "        if not components_equal(\n"
                "            parts,\n"
                "            wanted[: len(parts)],\n"
                "            case_insensitive=case_insensitive,\n"
                "            normalization_insensitive=normalization_insensitive,\n"
                "        ):\n"
                "            continue\n",
                "        if False:  # MUTANT: inode-only matching, path ignored\n"
                "            continue\n",
            ),
        ],
        "killing": ["test_r4_cross_volume_inode_coincidence_ignored"],
        "sanity": ["test_dotdot_and_trailing_slash_match_project"],
    },
    {
        "id": "M-resolve-dedup-always-folds",
        "gate": "checkout-id dedup folds only under the volume's rules",
        "narrows_to": "a case/normalization alias reuses the id on a sensitive volume",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "        if components_equal(\n"
                "            recorded,\n"
                "            wanted,\n"
                "            case_insensitive=case_insensitive,\n"
                "            normalization_insensitive=normalization_insensitive,\n"
                "        ):\n"
                "            return checkout_id\n",
                "        if components_equal(  # MUTANT: dedup always folds\n"
                "            recorded,\n"
                "            wanted,\n"
                "            case_insensitive=True,\n"
                "            normalization_insensitive=True,\n"
                "        ):\n"
                "            return checkout_id\n",
            ),
        ],
        "killing": ["test_resolve_checkout_id_dedup_follows_volume_rule"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-union-collision-allowed",
        "gate": "publication refuses case-only shim collisions anywhere",
        "narrows_to": "colliding spellings publish over one shim file",
        "edits": [
            (
                "src/csk/dispatch.py",
                "        spellings = sorted({name for name, _owner, _scope in group})\n"
                "        if len(spellings) < 2:\n"
                "            continue\n",
                "        spellings = sorted({name for name, _owner, _scope in group})\n"
                "        if True:  # MUTANT: union collisions never refuse\n"
                "            continue\n",
            ),
        ],
        "killing": ["test_public_shim_union_validation"],
        "sanity": ["test_cross_layer_different_owners_refuse_at_publication"],
    },
    {
        "id": "M-atomic-temp-cleanup-dropped",
        "gate": "a failed atomic write removes its temp file",
        "narrows_to": "staged temp files accumulate beside the live file",
        "edits": [
            (
                "src/csk/dispatch.py",
                "    except BaseException:\n"
                "        Path(temporary_name).unlink(missing_ok=True)\n"
                "        raise\n",
                "    except BaseException:\n"
                "        raise  # MUTANT: temp file never cleaned up\n",
            ),
        ],
        "killing": ["test_atomic_writer_failure_removes_temp_file"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-shim-former-name-clobbered",
        "gate": "the shim never touches caller-owned working names",
        "narrows_to": "exactly one former working name is unconditionally unset",
        "edits": [
            (
                "src/csk/dispatch.py",
                "    lines.append(\'exec \"$@\"\')\n",
                "    lines.append(\"unset _csk_target  # MUTANT: one former name clobbered\")\n"
                "    lines.append(\'exec \"$@\"\')\n",
            ),
        ],
        "killing": ["test_shim_leaves_caller_working_names_untouched"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-identity-no-normalization-fold",
        "gate": "path comparison folds NFC/NFD on insensitive volumes",
        "narrows_to": "NFC/NFD spellings register as two records on APFS",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "        if normalization_insensitive:\n"
                '            left = unicodedata.normalize("NFC", left)\n'
                '            right = unicodedata.normalize("NFC", right)\n'
                "            if left == right:\n"
                "                continue\n",
                "        if False:  # MUTANT: normalization never folds\n"
                '            left = unicodedata.normalize("NFC", left)\n'
                '            right = unicodedata.normalize("NFC", right)\n'
                "            if left == right:\n"
                "                continue\n",
            ),
        ],
        "killing": ["test_resolve_checkout_id_dedup_follows_volume_rule"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-history-unknown-as-empty",
        "gate": "unreadable legacy history tombstones the scope",
        "narrows_to": "malformed JSON history grants the absence fallback",
        "edits": [
            (
                "src/csk/dispatch.py",
                '            f"dispatch consumers record {path} is not valid JSON; install "\n'
                '            f"history is unknown, so the scope is flagged for migration"\n'
                "        ], True\n",
                '            f"dispatch consumers record {path} is not valid JSON; install "\n'
                '            f"history is unknown, so the scope is flagged for migration"\n'
                "        ], False  # MUTANT: malformed history means empty\n",
            ),
        ],
        "killing": ["test_d7_malformed_consumers_refuses_unknown_history"],
        "sanity": ["test_d7_never_installed_project_gets_plain_empty_scope"],
    },
    {
        "id": "M-stage-includes-stale",
        "gate": "staging writes exactly the validated live union",
        "narrows_to": "a stale export overwrites the live shim file",
        "edits": [
            (
                "src/csk/dispatch.py",
                "    union: dict[str, None] = {}\n"
                "    for name, _owner, _scope in _union_members(\n"
                '        registry["projects"], registry["global"]["commands"]\n'
                "    ):\n"
                "        union.setdefault(name)\n"
                "    return union\n",
                "    union: dict[str, None] = {}  # MUTANT: stale exports staged\n"
                '    for entry in registry["projects"].values():\n'
                '        for name in entry["commands"]:\n'
                "            union.setdefault(name)\n"
                '    for name in registry["global"]["commands"]:\n'
                "        union.setdefault(name)\n"
                "    return union\n",
            ),
        ],
        "killing": ["test_r9_stale_case_export_does_not_shadow_live_shim"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-activate-link-first",
        "gate": "the live surface switches only with the pointer",
        "narrows_to": "the link points at the new generation before the switch",
        "edits": [
            (
                "src/csk/dispatch.py",
                '    _maybe_fault("activate:stage_launchers")\n'
                "    stage_launchers(home, registry, generation_id=generation_id)\n"
                '    _maybe_fault("activate:swap_current")\n'
                "    _swap_current_pointer(home, generation_id)\n"
                '    if os.name != "nt":\n'
                "        _point_shims_at(home, generation_id)\n"
                '    _maybe_fault("activate:reconcile")\n'
                "    prune_launchers(home, registry)\n",
                '    _maybe_fault("activate:stage_launchers")\n'
                '    if os.name != "nt":  # MUTANT: link swaps before staging\n'
                "        _point_shims_at(home, generation_id)\n"
                "    stage_launchers(home, registry, generation_id=generation_id)\n"
                '    _maybe_fault("activate:swap_current")\n'
                "    _swap_current_pointer(home, generation_id)\n"
                '    _maybe_fault("activate:reconcile")\n'
                "    prune_launchers(home, registry)\n",
            ),
        ],
        "killing": ["test_r12_case_rename_stage_leaves_old_surface_intact"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-refuse-manager-ino-only",
        "gate": "manager/checkout separation needs path ancestry",
        "narrows_to": "a cross-volume inode coincidence vetoes publication",
        "edits": [
            (
                "src/csk/dispatch.py",
                "    for root in candidates:\n"
                "        try:\n"
                "            want = os.stat(root).st_ino\n",
                "    _chain: set[int] = set()  # MUTANT: bare-ino separation\n"
                "    _node = home_real\n"
                "    while True:\n"
                "        _chain.add(os.stat(_node).st_ino)\n"
                "        _parent = os.path.dirname(_node)\n"
                "        if _parent == _node:\n"
                "            break\n"
                "        _node = _parent\n"
                "    for root in candidates:\n"
                "        try:\n"
                "            want = os.stat(root).st_ino\n"
                "            if want in _chain:\n"
                "                raise DispatchPublishError(\n"
                '                    f"dispatch manager home {home} is inside the registered "\n'
                '                    f"checkout at {root}; refusing to publish. Use a manager "\n'
                '                    f"home outside every checkout"\n'
                "                )\n",
            ),
        ],
        "killing": ["test_r4_manager_check_ignores_cross_volume_inode_coincidence"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-checkout-container-ino-only",
        "gate": "target/checkout containment needs path ancestry",
        "narrows_to": "a cross-volume inode coincidence vetoes a resolve",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "            if ancestors[len(parts) - len(root_parts)][1] == recorded:\n"
                '                hit: str = entry["canonical_root"]\n'
                "                return hit\n"
                "    return None\n",
                "            if ancestors[len(parts) - len(root_parts)][1] == recorded:\n"
                '                hit: str = entry["canonical_root"]\n'
                "                return hit\n"
                "        if recorded in {ino for _path, ino in ancestors}:  # MUTANT\n"
                '            hit = entry["canonical_root"]\n'
                "            return hit\n"
                "    return None\n",
            ),
        ],
        "killing": ["test_r4_target_containment_ignores_cross_volume_inode_coincidence"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-latency-resolve-sleep",
        "gate": "the latency gate times production resolve under 50ms p95",
        "narrows_to": "a 60ms production resolve still passes the gate",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "    registry = load_registry(manager_home)\n"
                "    if cwd is None:\n",
                "    import time as _mutant_time  # MUTANT: slow production resolve\n"
                "    _mutant_time.sleep(0.06)\n"
                "    registry = load_registry(manager_home)\n"
                "    if cwd is None:\n",
            ),
        ],
        "killing": ["test_dispatch_latency_warm_p95_under_target"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-shim-nonce-unvalidated",
        "gate": "a shim nonce outside lowercase hex is refused",
        "narrows_to": "a non-empty non-hex nonce is spliced into the shim",
        "edits": [
            (
                "src/csk/dispatch.py",
                '    if not nonce or any(char not in "0123456789abcdef" for char in nonce):\n',
                "    if not nonce:  # MUTANT: non-hex nonces admitted\n",
            ),
        ],
        "killing": ["test_shim_nonce_must_be_hex"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-split-drive-anchor-dropped",
        "gate": "split keeps the Windows drive/UNC anchor as first component",
        "narrows_to": "cross-drive paths compare equal by components alone",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "    if drive:\n        parts.insert(0, drive)\n",
                "    if False:  # MUTANT: drive anchor dropped\n"
                "        parts.insert(0, drive)\n",
            ),
        ],
        "killing": ["test_split_components_windows_drive_and_unc"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
    },
    {
        "id": "M-join-drive-anchor-dropped",
        "gate": "join restores the Windows drive/UNC anchor it split",
        "narrows_to": "the inode confirm re-states a drive-less ancestor",
        "edits": [
            (
                "src/csk/_dispatch_runtime.py",
                "    if drive:\n        joined: str = mod.join(parts[0] + sep, *parts[1:])\n",
                "    if False:  # MUTANT: join drops the drive anchor\n"
                "        joined: str = mod.join(parts[0] + sep, *parts[1:])\n",
            ),
        ],
        "killing": ["test_split_components_windows_drive_and_unc"],
        "sanity": ["test_bare_calls_run_each_projects_pinned_version"],
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
